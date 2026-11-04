"""Optional, trusted passive differential capture across one GUI actuation.

No collector is installed, attached or chosen here. An application-injected probe
owns its nonblocking callbacks, native deadlines and cleanup. A callback timeout
is not cancellation; this adapter never abandons a background collector thread.
Raw calls, scalar arguments and receiver addresses are transient correlation
inputs. Only closed, pointer-free historical hypotheses may leave this adapter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import time

logger = logging.getLogger(__name__)
BEFORE = "anyuse.rea-gui-probe-before/1"
AFTER = "anyuse.rea-gui-probe-after/1"
MAX_ROWS = 256
MAX_RAW_BYTES = 128 * 1024
MAX_CANDIDATES = 8
MAX_MODULE_BYTES = 512 * 1024 * 1024
CALLBACK_SECONDS = 1.0
CLEANUP_SECONDS = .5
SAFE_REASONS = frozenset({
    "probe_unavailable", "around_capture_required", "candidate_budget",
    "actuator_capture_unavailable", "drive_disabled",
    "capture_disabled", "capture_already_active", "invalid_target", "invalid_front",
    "guest_required", "target_unavailable", "module_changed", "epoch_changed",
    "invalid_baseline", "invalid_after", "invalid_window", "probe_before_failed",
    "probe_after_failed", "probe_deadline", "cleanup_unconfirmed", "correlation_failed",
})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OFFSET = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,7})\Z")
_BUNDLE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\Z")
_GUI_ACTIONS = frozenset({"click_at", "type_text_at", "key_combination", "scroll_at", "hover_at", "drag_to"})
_CANDIDATE_KEYS = {"getter_offset", "consumer_offset", "vptr_rva", "dispatch", "vtable_slot_offset",
                   "this_adjustment", "differential_only", "score"}


def _same(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _need(value):
    if not value:
        raise ValueError("invalid differential capture")


def _number(value, *, positive=False):
    return (type(value) in {int, float} and (0 < value if positive else 0 <= value)
            and value < 10**15 and math.isfinite(value))


def _bundle_id(value):
    return type(value) is str and len(value) <= 192 and _BUNDLE.fullmatch(value) is not None


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _window(value):
    _need(type(value) is dict and set(value) == {"started_monotonic", "ended_monotonic"})
    _need(_number(value["started_monotonic"]) and _number(value["ended_monotonic"])
          and value["started_monotonic"] <= value["ended_monotonic"])


def _candidate(value):
    _need(type(value) is dict and set(value) == _CANDIDATE_KEYS)
    for field in ("getter_offset", "consumer_offset"):
        _need(type(value[field]) is str and _OFFSET.fullmatch(value[field]) is not None)
    _need(value["getter_offset"] != value["consumer_offset"])
    _need(value["vptr_rva"] is None or type(value["vptr_rva"]) is str and _OFFSET.fullmatch(value["vptr_rva"]) is not None)
    _need(type(value["dispatch"]) is str and value["dispatch"] in {"direct", "vtable_slot"}
          and type(value["differential_only"]) is bool)
    slot = value["vtable_slot_offset"]
    _need(slot is None if value["dispatch"] == "direct" else type(slot) is int and 0 <= slot <= 1024 and slot % 8 == 0)
    adjustment = value["this_adjustment"]
    _need(type(adjustment) is int and abs(adjustment) <= 4096 and adjustment % 8 == 0)
    # This adapter never derives an input value from typed GUI text or keys.
    score = (4 if value["differential_only"] else 1) + (2 if slot is not None else 1)
    _need(type(value["score"]) is int and value["score"] == score)


def validate_differential(detail, candidates, front):
    """Pure, closed loader validation; returned data cannot authorize replay."""
    _need(type(detail) is dict and detail.get("evidence_only") is True and detail.get("proof_created") is False)
    _need(type(detail.get("status")) is str)
    _need(type(candidates) is list and len(candidates) <= MAX_CANDIDATES)
    if detail.get("status") in {"unavailable", "degraded"}:
        _need(set(detail) == {"status", "reason", "evidence_only", "proof_created"}
              and type(detail["reason"]) is str and detail["reason"] in SAFE_REASONS and not candidates)
        return {"differential_capture": copy.deepcopy(detail)}
    _need(detail.get("status") == "captured" and set(detail) == {
        "status", "process_epoch", "module_sha256", "baseline_window", "collector_window", "action_window", "counts",
        "candidate_count", "matched_candidate_count", "candidates_truncated", "evidence_only", "proof_created"})
    epoch = detail["process_epoch"]
    _need(type(epoch) is dict and set(epoch) == {"bundle_id", "pid", "launch_time", "uid", "identity_sha256"})
    _need(_bundle_id(epoch["bundle_id"]) and type(epoch["pid"]) is int and 0 < epoch["pid"] < 2**31
          and type(epoch["uid"]) is int and 0 <= epoch["uid"] < 2**32 - 1
          and _number(epoch["launch_time"], positive=True) and epoch["launch_time"] < 253402300800
          and _sha(epoch["identity_sha256"]) and _sha(detail["module_sha256"]))
    _need(type(front) is dict and front.get("bundle_id") == epoch["bundle_id"]
          and type(front.get("pid")) is int and front["pid"] == epoch["pid"])
    _window(detail["baseline_window"])
    _window(detail["collector_window"])
    _window(detail["action_window"])
    _need(detail["baseline_window"]["ended_monotonic"] <= detail["collector_window"]["started_monotonic"]
          <= detail["action_window"]["started_monotonic"] <= detail["action_window"]["ended_monotonic"]
          <= detail["collector_window"]["ended_monotonic"]
          and detail["baseline_window"]["ended_monotonic"] - detail["baseline_window"]["started_monotonic"] <= CALLBACK_SECONDS)
    counts = detail["counts"]
    _need(type(counts) is dict and set(counts) == {"baseline_calls", "actuated_calls", "observed_getters"}
          and all(type(value) is int and 0 <= value <= MAX_ROWS for value in counts.values()))
    _need(type(detail["candidate_count"]) is int and detail["candidate_count"] == len(candidates)
          and type(detail["matched_candidate_count"]) is int
          and len(candidates) <= detail["matched_candidate_count"] <= counts["actuated_calls"] * counts["observed_getters"]
          and type(detail["candidates_truncated"]) is bool
          and (detail["candidates_truncated"] or detail["matched_candidate_count"] == len(candidates)))
    for value in candidates:
        _candidate(value)
    _need(len({json.dumps(value, sort_keys=True, allow_nan=False) for value in candidates}) == len(candidates))
    _need(candidates == sorted(candidates, key=lambda value: (-value["score"], value["getter_offset"], value["consumer_offset"])))
    return {"differential_capture": copy.deepcopy(detail), "differential_candidates": copy.deepcopy(candidates)}


def _pin_module(pin, bundle, deadline):
    _need(type(pin) is dict and set(pin) == {"file", "sha256", "bytes"})
    _need(type(pin["file"]) is str and len(pin["file"]) <= 4096 and _sha(pin["sha256"])
          and type(pin["bytes"]) is int and 0 < pin["bytes"] <= MAX_MODULE_BYTES)
    path = Path(pin["file"])
    _need(path.is_absolute() and path.resolve(strict=True) == path and path.is_relative_to(bundle))
    before = path.lstat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
    _need(stat.S_ISREG(before.st_mode) and before.st_size == pin["bytes"])
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        _need(signature(os.fstat(stream.fileno())) == signature(before))
        while True:
            _need(time.monotonic() <= deadline)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        _need(signature(os.fstat(stream.fileno())) == signature(before) == signature(path.lstat()))
    _need(digest.hexdigest() == pin["sha256"])
    return copy.deepcopy(pin)


def _trace(baseline, actuated, getters):
    from . import rea_native_receiver_v2 as V2
    request = V2.validate_trace_request({"op": "correlate_native_trace_v2", "baseline_calls": baseline,
        "actuated_calls": actuated, "observed_getters": getters})
    _need(len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) <= MAX_RAW_BYTES)
    return request


class ProbeCapture:
    """One passive collector lease across one caller-owned GUI action.

    Injected callbacks must return within their supplied deadlines and implement
    finite collector lifetimes. Python callbacks cannot be forcibly cancelled;
    an overrun is refused as capture evidence and unconfirmed cleanup disables
    further probe use. The GUI action itself is always the caller's responsibility.
    """

    def __init__(self, probe, *, identity_reader=None):
        self.probe, self.identity_reader = probe, identity_reader
        self.disabled = False
        self._failure = None
        self._epoch = self._target = self._baseline = self._front = None
        self._touched, self._closed = False, False
        self._before_returned = self._armed = None
        self._closed_monotonic = None

    def _fail(self, reason, *, unavailable=False):
        self._failure = {"status": "unavailable" if unavailable else "degraded", "reason": reason,
                         "evidence_only": True, "proof_created": False}
        logger.warning("REA differential capture unavailable (%s)", reason)

    def _identity(self):
        from . import rea_runtime_bridge as B
        reader = self.identity_reader if self.identity_reader is not None else B.process_identity
        epoch = dict(B._expected_identity(reader(Path(self._target["bundle"]), self._target["bundle_id"])))
        _need(epoch["bundle"] == self._target["bundle"] and epoch["bundle_id"] == self._target["bundle_id"]
              and Path(epoch["executable"]).is_relative_to(Path(epoch["bundle"]))
              and epoch["pid"] == self._front["pid"])
        return epoch

    def _close(self):
        self._baseline = None
        if not self._touched or self._closed:
            return
        self._closed = True
        try:
            deadline = time.monotonic() + CLEANUP_SECONDS
            result = self.probe.close(expected_identity=copy.deepcopy(self._epoch), deadline_monotonic=deadline)
            closed_at = time.monotonic()
            _need(type(result) is dict and set(result) == {"closed"} and result["closed"] is True
                  and closed_at <= deadline)
            self._closed_monotonic = closed_at
        except BaseException as exc:
            self.disabled = True
            self._fail("cleanup_unconfirmed")
            if not isinstance(exc, Exception):
                raise

    def before(self, action, front):
        if self.disabled:
            self._fail("capture_disabled", unavailable=True)
            return
        if self._touched and not self._closed:
            self._fail("capture_already_active")
            self._close()
            return
        self._failure = None
        self._epoch = self._target = self._baseline = None
        self._touched = self._closed = False
        self._before_returned = self._armed = self._closed_monotonic = None
        if self.probe is None:
            self._fail("probe_unavailable", unavailable=True)
            return
        stage = "drive_disabled"
        try:
            from .actions import drive_enabled
            _need(drive_enabled())
            stage = "guest_required"
            from . import rea_mapping as R
            if R.host_allowed():
                raise PermissionError("registered capture refuses inherited host permission")
            R.assert_guest()
            stage = "invalid_front"
            _need(type(front) is dict and _bundle_id(front.get("bundle_id")) and type(front.get("pid")) is int
                  and 0 < front["pid"] < 2**31 and type(action) is str and action in _GUI_ACTIONS)
            self._front = {"bundle_id": front["bundle_id"], "pid": front["pid"]}
            stage = "invalid_target"
            target = copy.deepcopy(self.probe.target)
            _need(type(target) is dict and set(target) == {"bundle", "bundle_id", "module"}
                  and target["bundle_id"] == front["bundle_id"] and type(target["bundle"]) is str
                  and len(target["bundle"]) <= 4096)
            bundle = Path(target["bundle"])
            _need(bundle.is_absolute() and bundle.is_dir() and bundle.resolve(strict=True) == bundle)
            _need(all(callable(getattr(self.probe, method, None)) for method in ("before", "after", "close")))
            self._target = target
            _pin_module(target["module"], bundle, time.monotonic() + CALLBACK_SECONDS)
            stage = "target_unavailable"
            self._epoch = self._identity()
            stage = "probe_before_failed"
            started = time.monotonic()
            deadline = started + CALLBACK_SECONDS
            self._touched = True
            result = self.probe.before(copy.deepcopy(self._front), action=action, expected_identity=copy.deepcopy(self._epoch),
                                       deadline_monotonic=deadline, limits={"max_rows": MAX_ROWS, "max_bytes": MAX_RAW_BYTES})
            received = time.monotonic()
            stage = "probe_deadline"
            _need(received <= deadline)
            stage = "invalid_baseline"
            _need(type(result) is dict and set(result) == {"format", "identity", "module", "baseline_calls",
                "observed_getters", "read_only", "armed", "baseline_started_monotonic", "baseline_finished_monotonic", "armed_monotonic"}
                and result["format"] == BEFORE and result["read_only"] is True and result["armed"] is True
                and _same(result["identity"], self._epoch) and _same(result["module"], target["module"]))
            baseline_start, baseline_end, armed = (result[key] for key in
                ("baseline_started_monotonic", "baseline_finished_monotonic", "armed_monotonic"))
            _need(all(_number(value) for value in (baseline_start, baseline_end, armed))
                  and baseline_start <= baseline_end <= armed <= received and started <= armed
                  and baseline_start >= started - CALLBACK_SECONDS
                  and baseline_end - baseline_start <= CALLBACK_SECONDS)
            request = _trace(result["baseline_calls"], [], result["observed_getters"])
            self._baseline = {"baseline_calls": request["baseline_calls"], "observed_getters": request["observed_getters"],
                              "started_monotonic": baseline_start, "ended_monotonic": baseline_end}
            self._armed = armed
            stage = "epoch_changed"
            _need(_same(self._identity(), self._epoch))
            self._before_returned = time.monotonic()
            stage = "probe_deadline"
            _need(self._before_returned <= deadline)
        except BaseException as exc:
            self._fail(stage, unavailable=not self._touched)
            self._close()
            if not isinstance(exc, Exception):
                raise

    def after(self, started, ended):
        if self._failure is not None:
            return validate_differential(self._failure, [], self._front)
        if self._baseline is None or not self._touched or self._closed:
            self._fail("around_capture_required", unavailable=True)
            return validate_differential(self._failure, [], self._front)
        stage = "invalid_window"
        safe = None
        try:
            now = time.monotonic()
            _need(_number(started) and _number(ended) and self._armed <= self._before_returned <= started <= ended <= now)
            stage = "probe_after_failed"
            deadline = now + CALLBACK_SECONDS
            result = self.probe.after(expected_identity=copy.deepcopy(self._epoch), started_monotonic=started,
                                      ended_monotonic=ended, deadline_monotonic=deadline)
            stage = "probe_deadline"
            _need(time.monotonic() <= deadline)
            stage = "invalid_after"
            window = {"started_monotonic": started, "ended_monotonic": ended}
            _need(type(result) is dict and set(result) == {"format", "identity", "after_identity", "module", "window",
                "actuated_calls", "complete", "read_only"} and result["format"] == AFTER
                and result["complete"] is True and result["read_only"] is True
                and _same(result["identity"], self._epoch) and _same(result["after_identity"], self._epoch)
                and _same(result["module"], self._target["module"]) and _same(result["window"], window))
            stage = "epoch_changed"
            _need(_same(self._identity(), self._epoch))
            stage = "module_changed"
            _pin_module(self._target["module"], Path(self._target["bundle"]), time.monotonic() + CALLBACK_SECONDS)
            stage = "correlation_failed"
            request = _trace(self._baseline["baseline_calls"], result["actuated_calls"], self._baseline["observed_getters"])
            from . import rea_native_receiver_v2 as V2
            correlation = V2.correlate_differential_trace(baseline_calls=request["baseline_calls"],
                actuated_calls=request["actuated_calls"], observed_getters=request["observed_getters"])
            candidates, seen = [], set()
            for candidate in correlation["candidates"]:
                value = {key: candidate[key] for key in _CANDIDATE_KEYS}
                _candidate(value)
                identity = json.dumps(value, sort_keys=True, allow_nan=False)
                if identity not in seen and len(candidates) < MAX_CANDIDATES:
                    candidates.append(value)
                    seen.add(identity)
            epoch = {key: self._epoch[key] for key in ("bundle_id", "pid", "launch_time", "uid")}
            from . import rea_mapping as R
            epoch["identity_sha256"] = R.digest(self._epoch)
            detail = {"status": "captured", "process_epoch": epoch, "module_sha256": self._target["module"]["sha256"],
                "baseline_window": {key: self._baseline[key] for key in ("started_monotonic", "ended_monotonic")},
                "action_window": window, "counts": {key: len(request[key]) for key in
                    ("baseline_calls", "actuated_calls", "observed_getters")},
                "candidate_count": len(candidates), "matched_candidate_count": correlation["matched_candidate_count"],
                "candidates_truncated": correlation["candidates_truncated"] or correlation["matched_candidate_count"] > len(candidates),
                "evidence_only": True, "proof_created": False}
            safe = {"differential_capture": detail, "differential_candidates": candidates}
        except BaseException as exc:
            self._fail(stage)
            if not isinstance(exc, Exception):
                raise
        finally:
            self._close()
        if self._failure is not None:
            return validate_differential(self._failure, [], self._front)
        try:
            # GUI event posts may return before their native handlers run.
            # Retain the enclosing collector lease separately from the exact
            # dispatch interval; neither asserts per-call timing or causality.
            safe["differential_capture"]["collector_window"] = {
                "started_monotonic": self._armed, "ended_monotonic": self._closed_monotonic}
            return validate_differential(safe["differential_capture"], safe["differential_candidates"], self._front)
        except Exception:
            self._fail("invalid_after")
            return validate_differential(self._failure, [], self._front)

    def cancel(self):
        """Release an armed collector when the GUI actuator fails or raises."""
        self._close()
