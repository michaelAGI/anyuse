"""Bounded GUI windows and optional around-actuation differential hypotheses.

An injected passive probe owns native collection; settings never attach one.
Saved records are mapping hints, never raw runtime traces or replay authority. Text,
key strings, task text, window titles and unknown parameters are never retained.
The monotonic clock applies only to the producing run on the same boot; a PID
here is historical context, not a reusable process-identity binding.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from anyuse.paths import anyuse_data_path

logger = logging.getLogger(__name__)
FORMAT = "anyuse.rea-ui-actuation-trace/1"
LIVE_FORMAT = "anyuse.rea-ui-actuation-trace/2"
MAX_ACTIONS = 256
MAX_BYTES = 256 * 1024
MAX_ARTIFACTS = 64
FILE_NAME = re.compile(r"task_[0-9a-f]{24}_[0-9a-f]{16}\.json\Z")
ACTIONS = frozenset({"click_at", "type_text_at", "key_combination", "scroll_at", "hover_at", "drag_to"})
REDACTED = "[redacted]"


def _number(value):
    return type(value) in {int, float} and abs(value) <= 10**15 and math.isfinite(value)


def redact_params(params):
    """Keep only measured geometry, finite counts and closed actuator enums."""
    result = {}
    for key in ("x", "y", "x1", "y1", "x2", "y2", "magnitude", "clicks", "duration"):
        if _number(params.get(key)):
            result[key] = params[key]
    for key, values in (("button", {"left", "middle", "right"}),
                        ("direction", {"up", "down", "left", "right"})):
        if type(params.get(key)) is str and params[key] in values:
            result[key] = params[key]
    if type(params.get("press_enter")) is bool:
        result["press_enter"] = params["press_enter"]
    # Do not retain arbitrary parameter names: those can themselves contain text.
    if "text" in params:
        result["text"] = REDACTED
    if "keys" in params:
        result["keys"] = REDACTED
    return result


def _front(front):
    front = front if type(front) is dict else {}
    name, bundle, pid = front.get("name"), front.get("bundle_id"), front.get("pid")
    # Display names can be user-renamed and may contain document/credential text.
    # Keep the bundle identifier for attribution without retaining free text.
    return {"front_app": REDACTED if type(name) is str else None,
            "bundle_id": bundle if type(bundle) is str and re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", bundle)
                and len(bundle) <= 192 else None,
            "pid": pid if type(pid) is int and 0 < pid < 2**31 else None}


def _read(path):
    path = Path(path).expanduser().absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("canonical UI trace resource required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("regular UI trace resource required")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("UI trace byte budget exceeded")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate GUI trace key")
            value[key] = item
        return value
    def invalid_constant(_):
        raise ValueError("nonfinite GUI trace value")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    return value, {"file": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def load_candidate_trace(path, *, bundle_id=None):
    """Validate redacted historical windows/hypotheses; never attach or act."""
    value, pin = _read(path)
    keys = {"format", "task_ref", "run_id", "clock", "proof_created", "truncated", "ui_actuation_trace"}
    live = type(value) is dict and value.get("format") == LIVE_FORMAT
    if (type(value) is not dict or set(value) != keys | ({"capture_mode"} if live else set())
            or value["format"] not in {FORMAT, LIVE_FORMAT} or live and value["capture_mode"] != "live"
            or value["clock"] != "monotonic_seconds_local_run" or value["proof_created"] is not False
            or type(value["truncated"]) is not bool
            or type(value["task_ref"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["task_ref"])
            or type(value["run_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
            or type(value["ui_actuation_trace"]) is not list or not 1 <= len(value["ui_actuation_trace"]) <= MAX_ACTIONS):
        raise ValueError("invalid UI trace artifact")
    rows = value["ui_actuation_trace"]
    previous_end = 0
    for row in rows:
        row_keys = {"timestamp_monotonic", "end_monotonic", "wall_time_ms", "action",
                    "params_redacted", "front_app", "bundle_id", "pid"}
        if live:
            row_keys.add("differential_capture")
            if type(row) is dict and "differential_candidates" in row:
                row_keys.add("differential_candidates")
        if (type(row) is not dict or set(row) != row_keys
                or not _number(row["timestamp_monotonic"]) or not _number(row["end_monotonic"])
                or not previous_end <= row["timestamp_monotonic"] <= row["end_monotonic"]
                or type(row["wall_time_ms"]) is not int or not 0 <= row["wall_time_ms"] < 10**15
                or type(row["action"]) is not str or row["action"] not in ACTIONS
                or type(row["params_redacted"]) is not dict
                or redact_params(row["params_redacted"]) != row["params_redacted"]
                or _front({"name": row["front_app"], "bundle_id": row["bundle_id"], "pid": row["pid"]}) !=
                    {k: row[k] for k in ("front_app", "bundle_id", "pid")}):
            raise ValueError("invalid or unredacted UI actuation window")
        if live:
            from .rea_gui_probe import validate_differential
            validate_differential(row["differential_capture"], row.get("differential_candidates", []), row)
            if row["differential_capture"]["status"] == "captured":
                window = row["differential_capture"]["action_window"]
                if window != {"started_monotonic": row["timestamp_monotonic"], "ended_monotonic": row["end_monotonic"]}:
                    raise ValueError("differential capture differs from the recorded GUI dispatch window")
        previous_end = row["end_monotonic"]
    if bundle_id is not None:
        rows = [row for row in rows if row["bundle_id"] == bundle_id]
        if not rows:
            raise ValueError("UI trace has no windows for the requested bundle")
    correlated = any(row.get("differential_capture", {}).get("status") == "captured" for row in rows)
    return {"artifact": pin, "run_id": value["run_id"], "ui_actuation_trace": rows,
            "truncated": value["truncated"], "proof_created": False, "actuated": False,
            "basis": ("historical_gui_differential_hypotheses" if correlated else
                      "historical_gui_windows_not_native_observations"),
            "instructions": ("Ranked candidates describe an enclosing collector lease, including final target guards and bounded drain. "
                "They retain no live receiver address and establish no ABI, lifetime, callable actor or task proof. "
                "Neither individual native-call timestamps nor exclusive causal attribution are established. "
                "Process epochs here are historical context, never a binding for a new execution." if correlated else
                "Match only independently collected native observations from the same run, clock and process epoch. "
                "These windows supply no native calls, ABI, receiver, process binding or task proof.")}


class ReaUiTraceCapture:
    def __init__(self, task, *, directory=None, mode="windows", differential_probe=None, identity_reader=None):
        if mode not in {"windows", "live"}:
            raise ValueError("resolved GUI capture mode must be windows or live")
        self.directory = Path(directory) if directory is not None else anyuse_data_path("workflows", "rea_candidates")
        self.directory = self.directory.expanduser().absolute()
        self.task_ref = hashlib.sha256(task.encode("utf-8")).hexdigest()
        self.run_id = uuid.uuid4().hex
        self.path = self.directory / f"task_{self.task_ref[:24]}_{self.run_id[:16]}.json"
        self.rows = []
        self.truncated = False
        self.error = None
        self.mode = mode
        # Trusted application code, never a serialized model/tool parameter.
        self.differential_probe = differential_probe
        self._identity_reader = identity_reader
        self.probe_disabled = False

    @classmethod
    def for_task(cls, task, *, tracker=None, directory=None, differential_probe=None, identity_reader=None):
        from .learning import ReaFallbackTracker, resolve_gui_capture_mode
        try:
            from anyuse import settings
            if settings.get("ANYUSE_REA_GUI_CAPTURE") == "off":
                return None
            tracker = tracker if tracker is not None else ReaFallbackTracker()
            mode = resolve_gui_capture_mode()
            if mode == "off":
                mode = resolve_gui_capture_mode(escalate_to_rea=tracker.rea_escalation_status(task)["escalate_to_rea"])
            if mode == "off":
                return None
            if directory is None:
                directory = tracker.path.parent / "rea_candidates"
            return cls(task, directory=directory, mode=mode, differential_probe=differential_probe,
                       identity_reader=identity_reader)
        except (OSError, ValueError) as exc:
            logger.warning("REA UI trace unavailable: %s", exc)
            return None

    @staticmethod
    def _degraded(reason):
        return {"differential_capture": {"status": "unavailable" if reason in {
                "probe_unavailable", "around_capture_required", "actuator_capture_unavailable"} else "degraded",
                "reason": reason, "evidence_only": True, "proof_created": False}}

    def execute(self, executor, action, params, front, *, front_reader=None, before_dispatch=None):
        """Native setup runs only after the actuator's own drive/BOUNDARY gates.

        Opaque adapters can still retain windows. They must implement an admitted
        actuator seam before they can lend their policy boundary to a native probe.
        """
        from .actions import ActionExecutor
        params = copy.deepcopy(params)
        front = copy.deepcopy(front)
        def capture(dispatch, *, probe_allowed):
            sent = {"started": False, "returned": False}
            initial_rows = len(self.rows)
            def send():
                if sent["started"]:
                    if sent["returned"]:
                        return sent["result"]
                    raise RuntimeError("GUI dispatch cannot be entered twice")
                sent.update(started=True, start=time.monotonic(), wall=time.time_ns() // 1_000_000)
                denied = before_dispatch() if before_dispatch is not None else None
                result = denied if denied is not None else dispatch()
                sent.update(returned=True, result=result, end=time.monotonic())
                return result
            try:
                return self.actuate(action, params, front, send, front_reader=front_reader, probe_allowed=probe_allowed)
            except Exception:
                logger.warning("REA GUI capture unavailable (capture adapter failure)")
                if sent["started"] and not sent["returned"]:
                    raise  # An actuator exception is never permission to resend.
                result = sent["result"] if sent["returned"] else send()
                if len(self.rows) == initial_rows:
                    self.record(action, params, front, result, started=sent["start"], ended=sent["end"],
                                wall_time_ms=sent["wall"], differential=self._degraded("invalid_after"))
                return result
        if (isinstance(executor, ActionExecutor) and
                getattr(executor.execute, "__func__", None) is ActionExecutor.execute):
            return executor.execute(action, params, around=lambda dispatch: capture(dispatch, probe_allowed=True))
        return capture(lambda: executor.execute(action, params), probe_allowed=False)

    def actuate(self, action, params, front, dispatch, *, front_reader=None, probe_allowed=True):
        """before -> owned GUI dispatch -> after/close, with no automatic attach.

        Only the probe receives safe identity context, never task text or input
        parameters. Probe methods must be nonblocking, deadline-bound and own all
        native workers; a forgotten timeout thread is not a cleanup strategy.
        """
        params = copy.deepcopy(params)
        front = copy.deepcopy(front)
        attempt, detail = None, None
        if self.mode == "live" and len(self.rows) < MAX_ACTIONS:
            if not probe_allowed:
                detail = self._degraded("actuator_capture_unavailable")
            else:
                from .rea_gui_probe import ProbeCapture
                attempt = ProbeCapture(self.differential_probe, identity_reader=self._identity_reader)
        finished = False
        try:
            if attempt is not None:
                attempt.before(action, front)
            if front_reader is not None and type(front) is dict:
                try:
                    current = front_reader()
                except Exception:
                    from .outcome import refusal
                    return refusal("front app unavailable during GUI capture; fresh observation required",
                                   code="focus_unavailable", method="coordinate")
                current = current if type(current) is dict else None
                focus_keys = ("pid", "bundle_id", "name", "window_id", "window_title", "has_window")
                if any((current or {}).get(key) != front.get(key) for key in focus_keys if key in front):
                    from .outcome import refusal
                    return refusal("focus changed during GUI capture; fresh observation required",
                                   code="focus_changed", method="coordinate")
            started, wall_ms = time.monotonic(), time.time_ns() // 1_000_000
            result = dispatch()
            ended = time.monotonic()
            if attempt is not None:
                if type(result) is dict and result.get("success") is True:
                    detail = attempt.after(started, ended)
                    finished = True
                else:
                    attempt.cancel()
                    finished = True
            self.record(action, params, front, result, started=started, ended=ended,
                        wall_time_ms=wall_ms, differential=detail)
            return result
        finally:
            if attempt is not None:
                if not finished:
                    attempt.cancel()
                if attempt.disabled:
                    self.probe_disabled = True
                    self.differential_probe = None

    def _artifact(self, rows):
        return {"format": LIVE_FORMAT if self.mode == "live" else FORMAT,
                "task_ref": self.task_ref, "run_id": self.run_id,
                "clock": "monotonic_seconds_local_run", "proof_created": False,
                "truncated": self.truncated, "ui_actuation_trace": rows,
                **({"capture_mode": "live"} if self.mode == "live" else {})}

    def _fit(self, row):
        rows = self.rows + [row]
        def size():
            return len(json.dumps(self._artifact(rows), sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
        # Keep GUI windows when optional differential metadata exhausts the
        # artifact budget. Dropped metadata is explicit, never partial candidates.
        for candidate in rows:
            if size() <= MAX_BYTES:
                break
            if candidate.get("differential_capture", {}).get("status") == "captured":
                candidate.pop("differential_candidates", None)
                candidate.update(self._degraded("candidate_budget"))
                self.truncated = True
        if size() > MAX_BYTES:
            self.truncated = True
            raise ValueError("UI trace byte budget exceeded")
        return row

    def record(self, action, params, front, result, *, started, ended, wall_time_ms, differential=None):
        """Called immediately after the executor returns; capture never affects input."""
        if (type(result) is not dict or result.get("success") is not True or
                any(result.get(k) for k in ("gated", "blocked", "refused"))):
            return
        try:
            if type(action) is not str or action not in ACTIONS or type(params) is not dict:
                raise ValueError("unsupported trace action")
            if not (_number(started) and _number(ended) and 0 <= started <= ended
                    and type(wall_time_ms) is int and 0 <= wall_time_ms < 10**15
                    and (not self.rows or started >= self.rows[-1]["end_monotonic"])):
                raise ValueError("invalid actuation time window")
            if len(self.rows) >= MAX_ACTIONS:
                self.truncated = True
            else:
                row = {"timestamp_monotonic": started, "end_monotonic": ended,
                       "wall_time_ms": wall_time_ms, "action": action, "params_redacted": redact_params(params), **_front(front)}
                if self.mode == "live":
                    from .rea_gui_probe import validate_differential
                    differential = differential or self._degraded("around_capture_required")
                    try:
                        detail = validate_differential(differential["differential_capture"],
                            differential.get("differential_candidates", []), front)
                    except Exception:
                        logger.warning("REA differential capture unavailable (invalid_after)")
                        detail = self._degraded("invalid_after")
                    row.update(detail)
                self.rows.append(self._fit(row))
            self._persist()
            self.error = None
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            logger.warning("REA UI trace capture unavailable: %s", exc)

    def _persist(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.resolve(strict=True) != self.directory:
            raise ValueError("canonical UI trace directory required")
        lock_fd = os.open(self.directory / ".trace.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "a+b") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError("regular UI trace lock required")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            value = self._artifact(self.rows)
            raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > MAX_BYTES:
                raise ValueError("UI trace byte budget exceeded")
            artifacts = [p for p in self.directory.iterdir() if FILE_NAME.fullmatch(p.name) and p != self.path]
            artifacts.sort(key=lambda p: p.lstat().st_mtime_ns)
            for old in artifacts[:max(0, len(artifacts) - MAX_ARTIFACTS + 1)]:
                old.unlink()
            # One fixed staging name under the store lock bounds interrupted writes too.
            temporary = self.directory / ".pending.json"
            try:
                temporary.unlink()  # A stale name or symlink is removed without following it.
            except FileNotFoundError:
                pass
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
