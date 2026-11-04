"""One-shot declared recovery for the generic discovery/proof workflow.

The coordinator owns its lock, frozen source/configuration closure and durable
journal. This adapter reuses document recovery's existing saved-copy/public-state
contract. It cannot restore private memory, arbitrary unsaved buffers or state
which the caller did not checkpoint. Historical publication recovery and fresh
per-replay recovery remain separate obligations.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import time

from . import rea_agent as G
from . import rea_ax_state as S
from . import rea_document_restoration as D
from . import rea_mapping as R
from . import rea_runtime_bridge as B


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def validate_configuration(recovery, state, identity, total_seconds):
    """Freeze a bounded checkpoint for this exact original process and target."""
    _need(type(recovery) is dict and set(recovery) == {"checkpoint", "seconds"},
          "closed workflow restoration declaration required")
    seconds = recovery["seconds"]
    _need(type(seconds) is int and 1 <= seconds <= 60 and
          type(total_seconds) is int and seconds < total_seconds,
          "explicit restoration time reserve inside the original workflow deadline required")
    identity = S.validate_identity(identity)
    checkpoint = D.inspect_checkpoint(recovery["checkpoint"])
    _need(checkpoint["context"]["identity"] == identity and
          checkpoint["context"]["target"] == state["target"] and identity["bundle"] == state["bundle"],
          "restoration checkpoint must bind the original workflow process and target")
    _need(sum(row.get("restart") is True for role in ("learning", "verification", "heldout")
              for row in state.get("plan", {}).get(role, [])) <= 8,
          "declared restoration supports at most eight witnessed process restarts")
    return copy.deepcopy(recovery)


def initialize(recovery, *, backend=None):
    """Bind only the captured public backend; no action is taken here."""
    if recovery is None:
        _need(backend is None, "restoration adapter requires a frozen checkpoint declaration")
        return None
    checkpoint = D.inspect_checkpoint(recovery["checkpoint"])
    selected = D._backend(backend)
    _need(selected.producers == checkpoint["producers"],
          "restoration adapter differs from the captured checkpoint")
    return {"status": "pending", "checkpoint": copy.deepcopy(recovery["checkpoint"]),
            "producers": copy.deepcopy(selected.producers), "result": None, "error": None,
            "scope": copy.deepcopy(checkpoint["scope"]), "unsupported": list(D.UNSUPPORTED),
            "automatic_retry": False}


def remaining_seconds(journal, recovery=None, *, now=None, replay=False):
    """Budget work inside the original deadline, retaining cleanup headroom.

    A fresh restored replay needs both capture and cleanup headroom. Restoration
    itself uses the unsubtracted original deadline in ``restore_once``.
    """
    moment = time.time() if now is None else now
    deadline = journal["deadline_wall"]
    _need(type(moment) in {int, float} and math.isfinite(moment) and
          type(deadline) in {int, float} and math.isfinite(deadline),
          "finite original workflow deadline required")
    reserve = 0
    if recovery is not None:
        status = journal.get("restoration", {}).get("status")
        _need(status in {"pending", "restored"},
              "incomplete or interrupted restoration forbids further workflow actions")
        reserve = (2 if replay else int(status == "pending")) * recovery["seconds"]
    return max(0, math.floor(deadline - moment - reserve))


def replay_declaration(recovery, identity):
    """Declare a fresh A.execute checkpoint; never reuse historical recovery."""
    checkpoint = D.inspect_checkpoint(recovery["checkpoint"])
    context = checkpoint["context"]
    identity = S.validate_identity(identity)
    _need(identity["bundle"] == context["identity"]["bundle"] and
          identity["bundle_id"] == checkpoint["context"]["target"]["bundle_id"] and
          identity["uid"] == context["identity"]["uid"],
          "fresh replay restoration must retain the exact owned target")
    value = {"workspace": copy.deepcopy(checkpoint["workspace"]), "identity": copy.deepcopy(identity),
             "fields": copy.deepcopy(context["fields"]), "seconds": recovery["seconds"]}
    if "metadata_policy" in checkpoint:
        value["metadata_policy"] = checkpoint["metadata_policy"]
    if "text_buffer" in context:
        value["text_buffer"] = copy.deepcopy(context["text_buffer"])
    return value


def _uncertain(value):
    if type(value) is dict:
        if (value.get("resource_review_required") is True or value.get("status") == "resource_review_required" or
            "native_state_uncertain" in value and value["native_state_uncertain"] is not False or
            any(key in value and value[key] is None for key in ("native_calls", "invocations", "helper_load_attempts")) or
            "reaped" in value and value["reaped"] is not True or value.get("manager_state") in {"pending", "failed"}):
            return True
        return any(_uncertain(child) for child in value.values())
    return type(value) is list and any(_uncertain(child) for child in value)


def _json(pin):
    return json.loads(D._verified(pin))


def _clean_effects(session, state, journal):
    """Reopen actual retained worker evidence; absence is never cleanup proof."""
    _need(state.get("status") != "resource_review_required" and
          not any(row.get("status") == "running" for row in state["steps"]),
          "interrupted or uncertain G action has no final disposition; recovery refused")
    _need(not _uncertain(state) and not _uncertain(journal["steps"]),
          "native resource cleanup is unknown; recovery actions refused")
    artifacts = state["artifacts"]
    for pin in artifacts.values():
        path = Path(pin["file"])
        _need(path.is_relative_to(session), "registered recovery evidence escaped the session")
        if path.suffix == ".json":
            _need(not _uncertain(_json(pin)), "retained native worker evidence is uncertain; recovery refused")
    native_ops = {"record", "verify", "heldout"}
    incomplete = [row for row in journal["steps"] if row["status"] != "completed"]
    _need(not any(row["kind"] == "restart" for row in incomplete),
          "interrupted public restart has no final epoch disposition; recovery refused")
    if any(row["kind"] in native_ops for row in incomplete):
        _need(state["steps"] and state["steps"][-1]["request"]["op"] in native_ops,
              "native-capable coordinator failure lacks final G disposition")
    for row in state["steps"]:
        if row.get("status") != "failed" or row["request"]["op"] not in native_ops:
            continue
        base = f"s{row['step']:04d}/record/"
        identity = row.get("expected_runtime_identity", journal["identity"])
        if state.get("workflow") == "guest_model_judged_state_v1":
            from . import rea_visual_effect as V
            lifecycle_pin = artifacts.get(base + "journal.json")
            outcome_pin = artifacts.get(base + "outcome.json")
            _need(lifecycle_pin is not None and outcome_pin is not None,
                  "failed visual action lacks positive final dispatch/cleanup evidence")
            lifecycle, outcome = _json(lifecycle_pin), _json(outcome_pin)
            _need(lifecycle.get("format") == V.JOURNAL and lifecycle.get("status") == "failed" and
                  outcome.get("resource_review_required") is False,
                  "failed visual action disposition is uncertain")
            if outcome.get("actuated") is False:
                _need("dispatch_started_monotonic" in lifecycle and
                      lifecycle["dispatch_started_monotonic"] is None and
                      "dispatch_finished_monotonic" in lifecycle and
                      lifecycle["dispatch_finished_monotonic"] is None and
                      not any(name.startswith(base + "runtime/") for name in artifacts),
                      "visual action has no positive final pre-dispatch refusal")
                continue
            _need(outcome.get("actuated") is True,
                  "failed visual action has uncertain native dispatch")
            policy = state["hypothesis"]["profile"]["policy"]
            actor = V.bind_actor(state["hypothesis"]["actor"], policy, lifecycle["intent"]["inputs"])
            runtime_name = "inspector.json" if V.P.for_actor(actor).name == "inspector" else "runtime.json"
            runtime_pin = artifacts.get(base + "runtime/" + runtime_name)
            _need(runtime_pin is not None, "failed visual action lacks its completed runtime transaction")
            reader = V._Reader()
            V._runtime(reader, runtime_pin, actor, identity, state["target"], lifecycle["resources"])
            reader.recheck()
        else:
            from . import rea_playback_trial as P
            runtime_pin = artifacts.get(base + "runtime/runtime.json")
            worker_pin = artifacts.get(base + "runtime/worker-output.json")
            _need(runtime_pin is not None and worker_pin is not None,
                  "failed native action lacks positive final cleanup evidence")
            reader = P.E._Reader()
            P._native(reader, state["hypothesis"]["actor"], reader.json(runtime_pin, "trial"),
                      worker_pin, identity, state["target"])
            reader.recheck()


def restore_once(session, folder, cfg, journal, *, retain, verify_context, backend=None,
                 storage_before=None, storage_after=None):
    """Attempt required cleanup once without altering the primary task outcome.

    ``verify_context`` reopens the coordinator's frozen source/configuration
    closure and returns its actual G state, allowing terminal clean failure.
    The caller holds its coordinator lock. Neither callback can be selected by
    model JSON. A retained ``started`` state prevents retry after interruption.
    """
    recovery_cfg = cfg.get("restoration")
    if recovery_cfg is None:
        _need(backend is None, "restoration adapter requires a frozen checkpoint declaration")
        return None
    recovery = journal["restoration"]
    if recovery["status"] != "pending":
        if recovery["status"] == "started":
            recovery.update(status="unknown", error={"type": "InterruptedRestoration",
                "message": "a prior restoration attempt has no final disposition; no automatic retry"})
            retain()
        return copy.deepcopy(recovery)
    _need(callable(retain) and callable(verify_context), "explicit trusted coordinator lifecycle required")
    _need((storage_before is None) == (storage_after is None) and
          (storage_before is None or callable(storage_before) and callable(storage_after)),
          "paired trusted restoration storage observations required")
    recovery.update(status="started", error=None)
    retain()  # Persist intent before checking or invoking any public recovery action.
    session, folder = Path(session), Path(folder)
    output = folder / "document-restoration"
    failure = None
    sampled = False
    try:
        state = verify_context()
        _need(state == G._load(session), "restoration context differs from actual retained G state")
        checkpoint = D.inspect_checkpoint(recovery_cfg["checkpoint"])
        selected = D._backend(backend)
        _need(recovery["checkpoint"] == recovery_cfg["checkpoint"] and
              recovery["producers"] == selected.producers == checkpoint["producers"] and
              recovery.get("scope") == checkpoint["scope"] and checkpoint["context"]["target"] == state["target"],
              "frozen restoration checkpoint, backend or target changed")
        _clean_effects(session, state, journal)
        identity = S.validate_identity(journal["identity"])
        _need(B.process_identity(Path(state["bundle"]), state["target"]["bundle_id"]) == identity,
              "process changed before restoration; no successor adoption")
        remaining = math.floor(journal["deadline_wall"] - time.time())
        _need(remaining >= 1, "original workflow authorization expired before restoration")
        restarts = []
        for row in journal["steps"]:
            if row["kind"] == "restart" and row["status"] == "completed":
                entry = _json(row["result"])["restart"]
                restarts.append({key: entry[key] for key in ("intent", "receipt")})
        context = copy.deepcopy(checkpoint["context"])
        D._successor(context, identity, restarts)
        current = D.current_document(recovery_cfg["checkpoint"])
        if storage_before is not None:
            recovery["storage"] = {"before": storage_before(), "after": None, "error": None}
            sampled = True
        result = D.restore(recovery_cfg["checkpoint"], current, output, backend=selected,
                           identity=identity, restarts=restarts,
                           timeout=min(recovery_cfg["seconds"], remaining))
        recovery.update(status=result["status"], result=result["receipt"], error=result.get("error"))
        if result["status"] == "restored":
            D.verify_restored(recovery_cfg["checkpoint"], result["receipt"])
    except BaseException as error:
        failure = error
        recovery.update(status="unknown", error={"type": type(error).__name__, "message": str(error)[:2048]})
        receipt = output / "result.json"
        if receipt.is_file():
            try:
                recovery["result"] = D.pin(receipt)
            except Exception as evidence_error:
                recovery["receipt_error"] = {"type": type(evidence_error).__name__, "message": str(evidence_error)[:2048]}
    finally:
        if sampled:
            try:
                recovery["storage"]["after"] = storage_after()
            except BaseException as error:
                recovery["storage"]["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
                recovery["status"] = "unknown"
                failure = failure or error
        try:
            retain()
        except BaseException as retention_error:
            if failure is not None:
                raise failure from retention_error
            raise
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return copy.deepcopy(recovery)
