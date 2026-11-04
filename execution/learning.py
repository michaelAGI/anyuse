"""LearningOrchestrator: the auto-learn / auto-replay wrapper around the
computer-use orchestrator.

``Agent.run`` goes through it: a saved workflow that matches the task is replayed
without a model; otherwise the orchestrator drives the task while a recorder
traces it, and a successful run is saved as a workflow for next time. It also
exposes the learning-stats surface the Agent reports.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _slug(task: str) -> str:
    """A short, filesystem/name-friendly workflow id derived from the task text."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", (task or "task").strip().lower()).strip("_")
    return s[:60].rstrip("_") or "task"


def resolve_mapping_effort(
    effort: Optional[str] = None,
    *,
    trials_learn: Optional[int] = None,
    trials_fresh: Optional[int] = None,
    trials_heldout: Optional[int] = None,
    require_restart: Optional[bool] = None,
) -> Dict[str, Any]:
    """Resolve the active mapping effort tier and overridable REA proof trial counts.

    * ``default``: always record HTTP/DOM and AX workflows on normal runs (0 Frida overhead).
    * ``rea-fast`` (or ``fast``): enable V2 REA (``binary_runtime_hypothesis``) with 1/1/1 trials.
    * ``rea-strict`` (or ``strict``): enable V2 REA with 3/3/3 benchmark trials.
    """
    from anyuse import settings
    from anyuse.execution.rea_state_campaign import validate_trial_policy

    effective = str(effort if effort is not None else settings.get("ANYUSE_MAP_EFFORT") or "default").strip().lower()
    if effective not in {"default", "fast", "strict", "rea-fast", "rea-strict"}:
        raise ValueError("effort must be default, rea-fast, or rea-strict")
    has_overrides = any(x is not None for x in (trials_learn, trials_fresh, trials_heldout, require_restart))
    rea_enabled = effective in {"fast", "strict", "rea-fast", "rea-strict"} or has_overrides
    preset = "fast" if effective in {"fast", "rea-fast"} else "strict"
    raw_tp: Dict[str, Any] = {"preset": preset}
    if trials_learn is not None:
        raw_tp["min_learning"] = trials_learn
    if trials_fresh is not None:
        raw_tp["min_verification"] = trials_fresh
    if trials_heldout is not None:
        raw_tp["min_heldout"] = trials_heldout
    if require_restart is not None:
        raw_tp["require_restart"] = require_restart
    tp = validate_trial_policy(raw_tp)
    return {
        "effort": "rea-fast" if effective == "fast" else ("rea-strict" if effective == "strict" else effective),
        "record_http_dom": True,
        "record_ax": True,
        "rea_enabled": rea_enabled,
        "native_contract_mode": "declared" if rea_enabled else "attributable",
        "native_receiver_mode": "binary_runtime_hypothesis" if rea_enabled else None,
        "trial_policy": tp,
    }


def resolve_gui_capture_mode(*, escalate_to_rea: bool = False) -> str:
    """Resolve requested GUI capture effort without attaching a native probe.

    Proof effort and collection remain separate decisions. Explicit collection
    modes override both effort and frequency; a live request needs a trusted
    application-supplied collector before any native observation can occur.
    """
    from anyuse import settings

    requested = settings.get("ANYUSE_REA_GUI_CAPTURE")
    if requested != "auto":
        return requested
    if resolve_mapping_effort()["effort"] in {"rea-fast", "rea-strict"}:
        return "live"
    return "windows" if escalate_to_rea is True else "off"


def computer_use_was_executed(orchestrator) -> bool:
    """A preflight refusal or a native-only attempt is not a vision fallback."""
    history = getattr(orchestrator, "history", None)
    if history is None:
        # Older computer-use adapters do not provide per-turn receipts.
        return not bool(getattr(orchestrator, "rea_executions", None))
    if not isinstance(history, (list, tuple)):
        return False
    return any(
        isinstance(row, dict) and isinstance(row.get("decision"), dict)
        and isinstance(row["decision"].get("action"), str)
        and row["decision"]["action"].lower() not in {"", "rea_action", "done", "failed", "stop"}
        and isinstance(row.get("actuation"), dict)
        and row["actuation"].get("success") is True
        and not row["actuation"].get("gated") and not row["actuation"].get("blocked")
        for row in history
    )


class ReaFallbackTracker:
    """Persistent, advisory mapping recommendations shared by public run paths.

    Only task slugs and counts are stored. Atomic replacement protects readers;
    a separate file lock serializes increments across CLI/MCP processes. Keeping
    at most 4096 recently used tasks bounds both the file and listing work.
    Tracking never dispatches REA or makes a model call.
    """

    MAX_TASKS = 4096
    MAX_BYTES = 1024 * 1024
    MAX_COUNT = 2**31 - 1

    def __init__(self, storage=None, *, storage_dir=None):
        from anyuse.paths import anyuse_data_path
        directory = storage_dir
        if directory is None:
            directory = getattr(storage, "storage_dir", None) or getattr(storage, "base_dir", None)
        self.path = (Path(directory) / "vision_fallbacks.json" if directory is not None
                     else anyuse_data_path("workflows", "vision_fallbacks.json"))
        self.counts: Dict[str, int] = {}
        self.tracking_error: Optional[str] = None

    def _read(self) -> Dict[str, int]:
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(self.MAX_BYTES + 1)
        if len(payload) > self.MAX_BYTES:
            raise ValueError("vision fallback state exceeds size limit")
        value = json.loads(payload)
        counts = value.get("counts") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get("format") != "anyuse.vision-fallbacks/1"
                or not isinstance(counts, dict) or len(counts) > self.MAX_TASKS
                or any(not isinstance(key, str) or not key or len(key) > 60 or _slug(key) != key
                       or type(count) is not int or not 0 <= count <= self.MAX_COUNT
                       for key, count in counts.items())):
            raise ValueError("invalid vision fallback state")
        return counts

    def _refresh(self):
        try:
            counts = self._read()
        except (OSError, ValueError) as exc:
            self.tracking_error = str(exc)
            logger.warning("REA fallback tracking unavailable: %s", exc)
        else:
            self.counts.clear()
            self.counts.update(counts)
            self.tracking_error = None

    def _increment(self, slug: str):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            counts = self._read()
            count = counts.pop(slug, 0)
            if len(counts) >= self.MAX_TASKS:
                del counts[next(iter(counts))]
            counts[slug] = min(count + 1, self.MAX_COUNT)
            temporary_fd, temporary_path = tempfile.mkstemp(prefix=".vision-fallbacks-", dir=self.path.parent)
            try:
                with os.fdopen(temporary_fd, "w") as stream:
                    json.dump({"format": "anyuse.vision-fallbacks/1", "counts": counts}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
            self.counts.clear()
            self.counts.update(counts)

    def _status(self, slug: str) -> Dict[str, Any]:
        from anyuse import settings
        count = self.counts.get(slug, 0)
        threshold = int(settings.get("ANYUSE_REA_AUTO_THRESHOLD") or 0)
        effort = str(settings.get("ANYUSE_MAP_EFFORT") or "default")
        escalate = bool(threshold > 0 and count >= threshold) or effort in {"rea-fast", "rea-strict"}
        recommended = effort if effort in {"rea-fast", "rea-strict"} else ("rea-fast" if escalate else "default")
        return {
            "task_slug": slug, "vision_fallbacks": count, "threshold": threshold,
            "escalate_to_rea": escalate, "recommended_config": resolve_mapping_effort(recommended),
            **({"tracking_error": self.tracking_error} if self.tracking_error else {}),
        }

    def note_vision_fallback(self, task: str) -> Dict[str, Any]:
        from anyuse import settings
        slug = _slug(task)
        if int(settings.get("ANYUSE_REA_AUTO_THRESHOLD") or 0) > 0:
            try:
                self._increment(slug)
                self.tracking_error = None
            except (OSError, ValueError, ImportError) as exc:
                self.tracking_error = str(exc)
                logger.warning("REA fallback tracking unavailable: %s", exc)
        else:
            # Disabled means no new counts, lock files, or writes.
            self._refresh()
        return self._status(slug)

    def rea_escalation_status(self, task: str) -> Dict[str, Any]:
        self._refresh()
        return self._status(_slug(task))

    def list_rea_candidates(self) -> List[Dict[str, Any]]:
        from anyuse import settings
        threshold = int(settings.get("ANYUSE_REA_AUTO_THRESHOLD") or 0)
        if threshold <= 0:
            return []
        self._refresh()
        return [self._status(slug) for slug, count in sorted(self.counts.items()) if count >= threshold]

    def record_run(self, task: str, *, rea_executions=(), used_computer_use: bool = True):
        if not used_computer_use or any(isinstance(row, dict) and row.get("ok") is True
                                       for row in (rea_executions or ())):
            return None
        return self.note_vision_fallback(task)


class LearningOrchestrator:
    def __init__(
        self,
        orchestrator,
        storage=None,
        auto_learn: bool = True,
        auto_replay: bool = True,
        verbose: bool = False,
        guide_storage=None,
        replay_fn=None,
        **_kwargs: Any,
    ):
        self.orchestrator = orchestrator
        self.storage = storage
        self.auto_learn = auto_learn
        self.auto_replay = auto_replay
        self.verbose = verbose
        self.guide_storage = guide_storage
        # Callback name->result that replays a saved workflow deterministically
        # (the Agent passes its .play). Lets run() take the cheap token-free path
        # before the expensive computer-use teacher.
        self._replay_fn = replay_fn
        # Re-entry guard: a replay (Agent.play) can fall through to WorkflowPlayer's
        # LLM fallback, which calls agent.run → learner.run → here again. Without a
        # guard a failing step could re-match the SAME workflow and recurse.
        self._replaying = False
        self._rea_fallback_tracker = ReaFallbackTracker(storage)
        # The capture trigger must use the same threshold store as this wrapper,
        # including explicitly supplied workflow storage directories.
        try:
            self.orchestrator.rea_trace_tracker = self._rea_fallback_tracker
        except (AttributeError, TypeError):
            pass  # Older adapters may not expose this optional recording seam.
        self._vision_fallback_counts = self._rea_fallback_tracker.counts
        self.last_rea_escalation: Optional[Dict[str, Any]] = None

    def run(self, task: str, replay: bool = True, learn: bool = True) -> str:
        """Drive ``task``, recording the trace when auto-learn is on.

        When ``auto_learn`` is set and a storage backend is present, we hand the
        orchestrator a StateGraphRecorder so a SUCCESSFUL run is persisted as a
        replayable Workflow + WorkflowTemplate. Failed runs are discarded by the
        orchestrator. A matching saved workflow (auto_replay) is replayed first.
        ``replay`` False: no saved workflow is tried; ``learn`` False: nothing is recorded (a
        run that starts mid-task, such as a failed replay's takeover, is no workflow of the task).
        """
        # CHEAP PATH FIRST: if a saved workflow matches this task, replay it
        # token-free (the core promise). Match via storage's LLM-over-candidates
        # (NOT the embeddings mock). Only on no-match / replay-failure do we fall to
        # the expensive computer-use teacher (and record the new path).
        self.last_rea_escalation = None
        replayed = self._try_replay(task) if replay else None
        if replayed is not None:
            return replayed

        recorder = self._make_recorder(task) if learn else None
        result = self.orchestrator.run(task, recorder=recorder)
        self.last_rea_escalation = self._rea_fallback_tracker.record_run(
            task, rea_executions=getattr(self.orchestrator, "rea_executions", ()) or (),
            used_computer_use=computer_use_was_executed(self.orchestrator),
        )
        return result

    def note_vision_fallback(self, task: str) -> Dict[str, Any]:
        """Record one vision/computer-use fallback for ``task`` and return its REA escalation status."""
        self.last_rea_escalation = self._rea_fallback_tracker.note_vision_fallback(task)
        return self.last_rea_escalation

    def rea_escalation_status(self, task: str) -> Dict[str, Any]:
        """Check whether ``task`` has crossed ANYUSE_REA_AUTO_THRESHOLD for REA V2 mapping."""
        return self._rea_fallback_tracker.rea_escalation_status(task)

    def list_rea_candidates(self) -> List[Dict[str, Any]]:
        """Return tasks whose vision fallback count meets or exceeds ANYUSE_REA_AUTO_THRESHOLD."""
        return self._rea_fallback_tracker.list_rea_candidates()

    def _try_replay(self, task: str):
        """Return the replay result text if a saved workflow matched and replayed
        successfully, else None (caller falls through to computer-use)."""
        if not (self.auto_replay and self.storage is not None and self._replay_fn is not None):
            return None
        # Already replaying (re-entered via WorkflowPlayer→agent.run) → skip straight
        # to computer-use rather than re-matching the same workflow and recursing.
        if self._replaying:
            return None
        try:
            match = self.storage.find_with_details(task)  # (workflow, confidence, reason)
        except Exception as exc:
            logger.debug("replay-match lookup failed: %s", exc)
            return None
        if not match:
            return None
        workflow, confidence, reason = match
        if confidence < 0.6:
            return None
        self._replaying = True
        try:
            res = self._replay_fn(workflow.name)
        except Exception as exc:
            # Once replay starts, an exception cannot establish that no effects
            # happened. Repeating the task could submit or write a second time.
            from anyuse.execution.outcome import INCOMPLETE, stop_text
            logger.warning("replay of %r stopped with unknown disposition: %s", workflow.name, exc)
            return stop_text(INCOMPLETE, f"replay of {workflow.name!r} raised {type(exc).__name__}: {exc}")
        finally:
            self._replaying = False
        ok = getattr(res, "success", None)
        if ok is None and isinstance(res, dict):
            ok = res.get("success")
        if ok:
            msg = getattr(res, "message", None) or (res.get("message") if isinstance(res, dict) else None)
            if self.verbose:
                logger.info("token-free replay of %r succeeded (%s)", workflow.name, reason)
            return msg or f"Replayed '{workflow.name}' ({reason})"
        # a replay the execution STOPPED (a refusal, a drift, an unobserved or deviating
        # effect) ends the run with its reason; the screen is not handed to computer-use
        # (a deviation stops; it does not ask). A plain miss still falls.
        from anyuse.execution.outcome import stopped_kind
        reason = str((res.get("outcome_reason", "") if isinstance(res, dict)
                      else getattr(res, "outcome_reason", "")) or "")
        if stopped_kind(reason):
            logger.info("replay of %r stopped: %s", workflow.name, reason[:200])
            return reason
        logger.info("replay of %r did not succeed; falling to computer-use", workflow.name)
        return None

    def _make_recorder(self, task: str):
        if not (self.auto_learn and self.storage is not None):
            return None
        try:
            from anyuse.memory.recorder import StateGraphRecorder
            return StateGraphRecorder(
                name=_slug(task),
                description=task,
                storage=self.storage,
                guide_storage=self.guide_storage,
            )
        except Exception as exc:
            # said out loud: a run without a recorder is not learned
            logger.warning("could not create the recorder, so this run is not learned: %s", exc)
            return None

    # -- learning-stats surface the Agent exposes -------------------------------
    def get_learning_stats(self) -> Dict[str, Any]:
        from anyuse import settings
        names = self.list_learned()
        runs = successes = 0
        if self.storage is not None and hasattr(self.storage, "list_all"):
            try:
                for wf in self.storage.list_all():
                    runs += int(getattr(wf, "run_count", 0) or 0)
                    successes += int(getattr(wf, "success_count", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("the workflow run stats could not be read: %s", exc)
        return {
            "workflows_learned": len(names),
            "total_runs": runs,
            "overall_success_rate": (successes / runs) if runs else 0.0,
            "workflows": names,
            "map_effort": str(settings.get("ANYUSE_MAP_EFFORT") or "default"),
            "rea_candidates": [c["task_slug"] for c in self.list_rea_candidates()],
        }

    def list_learned(self) -> List[str]:
        if self.storage is not None and hasattr(self.storage, "list_names"):
            try:
                return list(self.storage.list_names())
            except Exception as exc:
                logger.debug("list_learned failed: %s", exc)
        return []

    def forget(self, name: str) -> bool:
        if self.storage is not None and hasattr(self.storage, "delete"):
            try:
                return bool(self.storage.delete(name))
            except Exception as exc:
                logger.debug("forget failed: %s", exc)
        return False
