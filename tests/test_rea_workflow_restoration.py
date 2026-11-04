"""Generic recovery over real saved copies and inert public/native boundaries."""
import copy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from test_rea_campaign_coordinator import (
    _document_case, campaign, coordinated, runtime, visual_campaign_factory, visual_coordinated,
)
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_campaign_coordinator as Q
from anyuse.execution import rea_document_restoration as D
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_workflow_restoration as W


@pytest.fixture
def recovery(coordinated, tmp_path):
    e = coordinated
    e.identity["uid"] = os.getuid()
    d = _document_case(tmp_path, e.identity, e.cfg["target"])
    cfg = {"restoration": {"checkpoint": d.checkpoint, "seconds": 30}}
    journal = {"deadline_wall": time.time() + 300, "identity": copy.deepcopy(e.identity), "steps": [],
               "status": "failed_no_retry", "primary_outcome": {"status": "failed", "error": "task failed"},
               "restoration": W.initialize(cfg["restoration"], backend=d.backend)}
    folder = tmp_path / "workflow-recovery"; folder.mkdir()
    def retain():
        R.write_json(folder / "coordinator.json", journal)
    def restore(**kwargs):
        return W.restore_once(e.root, folder, cfg, journal, retain=retain,
            verify_context=lambda: G._load(e.root), backend=d.backend, **kwargs)
    return SimpleNamespace(e=e, d=d, cfg=cfg, journal=journal, folder=folder, retain=retain, restore=restore)


def test_real_owned_bytes_public_fields_and_primary_outcome_are_separate(recovery):
    e = recovery; original = D.pin(e.d.source); primary = copy.deepcopy(e.journal["primary_outcome"])
    e.d.document.write_bytes(b"changed saved task document\n")
    e.d.value = .9; e.d.position = [90., 80.]
    result = e.restore(storage_before=lambda: {"free": 10}, storage_after=lambda: {"free": 9})
    assert result["status"] == "restored" and result["storage"] == {"before": {"free": 10}, "after": {"free": 9}, "error": None}
    assert e.d.document.read_bytes() == b"baseline document\n" and e.d.value == .5 and e.d.position == [10., 20.]
    assert D.pin(e.d.source) == original and e.journal["primary_outcome"] == primary
    assert e.journal["status"] == "failed_no_retry" and e.d.calls == ["close", "reopen", "set:window.position", "set:level"]
    D.verify_restored(e.d.checkpoint, result["result"])
    assert R.load_json(e.folder / "coordinator.json")["restoration"] == result
    # A historical receipt never authorizes silently replaying cleanup.
    e.d.document.write_bytes(b"later independent change\n"); calls = list(e.d.calls)
    assert e.restore() == result and e.d.calls == calls and e.d.document.read_bytes() == b"later independent change\n"


def test_cleanup_time_is_reserved_and_fresh_replay_reserves_capture_too(recovery):
    e = recovery; e.journal["deadline_wall"] = 100
    assert W.remaining_seconds(e.journal, e.cfg["restoration"], now=50) == 20
    assert W.remaining_seconds(e.journal, e.cfg["restoration"], now=50, replay=True) == 0
    e.journal["restoration"]["status"] = "restored"
    assert W.remaining_seconds(e.journal, e.cfg["restoration"], now=50) == 50
    assert W.remaining_seconds(e.journal, e.cfg["restoration"], now=90, replay=True) == 0
    e.journal["restoration"]["status"] = "unknown"
    with pytest.raises(ValueError, match="forbids further"):
        W.remaining_seconds(e.journal, e.cfg["restoration"], now=50)


@pytest.mark.parametrize("change", ["identity", "target", "seconds", "extra"])
def test_frozen_checkpoint_contract_cannot_expand(recovery, change):
    e = recovery; state = G._load(e.e.root); identity = copy.deepcopy(e.e.identity)
    config = copy.deepcopy(e.cfg["restoration"])
    assert W.validate_configuration(config, state, identity, 300) == config
    if change == "identity": identity["pid"] += 1
    elif change == "target": state["target"] = {**state["target"], "bundle_id": "different.owned.target"}
    elif change == "seconds": config["seconds"] = True
    else: config["rollback_private_memory"] = True
    with pytest.raises(ValueError): W.validate_configuration(config, state, identity, 300)


def test_interrupted_attempt_is_never_repeated(recovery):
    e = recovery; e.journal["restoration"]["status"] = "started"
    assert e.restore()["status"] == "unknown" and not e.d.calls
    assert e.restore()["status"] == "unknown" and not e.d.calls


def test_start_intent_must_be_durable_before_public_actions(recovery):
    e = recovery
    def fail(): raise OSError("inert journal retention failed")
    with pytest.raises(OSError, match="retention failed"):
        W.restore_once(e.e.root, e.folder, e.cfg, e.journal, retain=fail,
                       verify_context=lambda: G._load(e.e.root), backend=e.d.backend)
    assert not e.d.calls and not (e.folder / "document-restoration").exists()


@pytest.mark.parametrize("unknown", [
    {"resource_review_required": True}, {"native_state_uncertain": True}, {"reaped": False},
    {"native_calls": None}, {"manager_state": "failed"},
])
def test_retained_unknown_worker_blocks_all_public_cleanup(recovery, unknown):
    e = recovery; path = e.e.root / "retained-worker.json"; R.write_json(path, unknown)
    state = G._load(e.e.root); state["artifacts"]["retained-worker.json"] = D.pin(path)
    R.write_json(e.e.root / "session.json", state)
    assert e.restore()["status"] == "unknown" and not e.d.calls
    assert not (e.folder / "document-restoration").exists()


def test_unknown_epoch_and_failed_public_close_never_claim_recovery(recovery, monkeypatch):
    e = recovery; monkeypatch.setattr(W.B, "process_identity", lambda *a: {**e.e.identity, "pid": e.e.identity["pid"] + 1})
    result = e.restore()
    assert result["status"] == "unknown" and "no successor adoption" in result["error"]["message"] and not e.d.calls


def test_partial_recovery_retains_receipt_and_original_operation_result(recovery):
    e = recovery; e.d.ignore_field = True; e.d.value = .9
    result = e.restore()
    assert result["status"] == "partial" and result["result"] and e.d.calls == ["close", "reopen", "set:level"]
    assert e.journal["primary_outcome"]["status"] == "failed"
    with pytest.raises(ValueError, match="incomplete"):
        D.verify_restored(e.d.checkpoint, result["result"])


def test_fresh_replay_declaration_contains_scope_not_historical_receipt(recovery):
    e = recovery; declaration = W.replay_declaration(e.cfg["restoration"], e.e.identity)
    checkpoint = D.inspect_checkpoint(e.d.checkpoint)
    assert declaration == {"workspace": e.d.workspace, "identity": e.e.identity,
                           "fields": checkpoint["context"]["fields"], "seconds": 30}
    assert "receipt" not in declaration and "checkpoint" not in declaration


def test_unsaved_buffer_is_not_discarded_by_generic_recovery(recovery):
    e = recovery; e.d.edited = True
    result = e.restore()
    assert result["status"] == "failed" and not e.d.calls
    assert result["unsupported"] == D.UNSUPPORTED


def test_frozen_context_failure_prevents_cleanup_actions(recovery):
    e = recovery
    def changed_sources(): raise ValueError("frozen source closure changed")
    result = W.restore_once(e.e.root, e.folder, e.cfg, e.journal, retain=e.retain,
                            verify_context=changed_sources, backend=e.d.backend)
    assert result["status"] == "unknown" and not e.d.calls
    assert result["error"]["message"] == "frozen source closure changed"


@pytest.fixture
def visual_recovery(visual_coordinated, tmp_path, monkeypatch):
    e = visual_coordinated
    monkeypatch.setattr(D.os, "statvfs", lambda path: SimpleNamespace(f_bavail=16 * 1024**3, f_frsize=1))
    d = _document_case(tmp_path, e.identity, e.cfg["target"])
    e.cfg["restoration"] = {"checkpoint": d.checkpoint, "seconds": 30}
    _, effect = Q._visual_modules(); trial = effect.run_trial
    def mutate_saved_copy(*args, **kwargs):
        result = trial(*args, **kwargs)
        d.identity = copy.deepcopy(e.identity)
        d.document.write_bytes(b"effect changed only the task-owned saved copy\n")
        d.value = .9
        return result
    monkeypatch.setattr(effect, "run_trial", mutate_saved_copy)
    def advance(journal):
        checkpoint = e.checkpoint(journal)
        d.identity = copy.deepcopy(e.identity)
        return Q.run_visual(e.root, e.cfg, e.out, resume=True, checkpoint=checkpoint,
                            restoration_backend=d.backend)
    def finish(journal):
        for _ in range(16):
            if journal["status"] not in {"needs_setup", "needs_restart"}: break
            journal = advance(journal)
        return journal
    return SimpleNamespace(e=e, d=d, effect=effect, advance=advance, finish=finish)


def test_visual_proof_publication_and_replay_each_honor_required_recovery(visual_recovery):
    f = visual_recovery; e, d = f.e, f.d
    journal = Q.run_visual(e.root, e.cfg, e.out, restoration_backend=d.backend)
    result = f.finish(journal)
    assert result["status"] == "completed", result.get("error")
    assert result["restoration"]["status"] == "restored" and len(e.calls) == 10
    assert d.document.read_bytes() == b"baseline document\n" and d.value == .5
    assert d.calls == ["close", "reopen", "set:level"] * 2
    catalog = R.load_json(result["published"]["file"])
    assert catalog["restoration"] == {"checkpoint": d.checkpoint, "receipt": result["restoration"]["result"]}
    replay = result["steps"][-1]["restored_execution"]
    assert replay["completion_status"] == "completed" and replay["restoration"]["status"] == "restored"
    assert replay["checkpoint"] != d.checkpoint
    assert replay["operation"]["result"]["verification_tier"] == "model_judged"


def test_visual_clean_before_refusal_still_restores_without_publication(visual_recovery, monkeypatch):
    f = visual_recovery; e, d = f.e, f.d
    def refuse(bundle, actor, policy, intent, out, **kwargs):
        folder = Path(out); folder.mkdir()
        d.document.write_bytes(b"public setup had saved changed task contents\n")
        f.effect._save(folder / "journal.json", {"format": f.effect.JOURNAL, "status": "failed",
            "dispatch_started_monotonic": None, "dispatch_finished_monotonic": None})
        f.effect._save(folder / "outcome.json", {"actuated": False, "resource_review_required": False})
        return {"status": "refused", "resource_review_required": False, "error": {"message": "before judge rejected setup"}}
    monkeypatch.setattr(f.effect, "run_trial", refuse)
    journal = Q.run_visual(e.root, e.cfg, e.out, restoration_backend=d.backend)
    result = f.advance(journal)
    assert result["status"] == "failed_no_retry" and result["published"] is None
    assert result["restoration"]["status"] == "restored", result["restoration"].get("error")
    assert d.document.read_bytes() == b"baseline document\n" and d.calls == ["close", "reopen"]
    assert G._load(e.root)["status"] == "hypothesis_failed" and not e.calls


def test_visual_unknown_dispatch_refuses_cleanup_and_publication(visual_recovery):
    f = visual_recovery; e, d = f.e, f.d
    e.fail_after_dispatch = True
    journal = Q.run_visual(e.root, e.cfg, e.out, restoration_backend=d.backend)
    result = f.advance(journal)
    assert result["status"] == "failed_no_retry" and result["published"] is None
    assert result["restoration"]["status"] == "unknown" and not d.calls
    assert len(e.calls) == 1 and not (e.out / "document-restoration").exists()


def test_partial_visual_recovery_cannot_be_bypassed_through_g_publish(visual_recovery):
    f = visual_recovery; e, d = f.e, f.d
    d.ignore_field = True
    journal = Q.run_visual(e.root, e.cfg, e.out, restoration_backend=d.backend)
    result = f.finish(journal)
    assert result["status"] == "failed_no_retry" and result["published"] is None
    assert result["restoration"]["status"] == "partial" and len(e.calls) == 9
    assert result["primary_outcome"] == "proof_completed"
    forced = G.step(e.root, {"op": "publish", **e.cfg["publication"]})
    assert forced["feedback"]["status"] == "failed"
    assert "required restoration must finish" in forced["feedback"]["feedback"]["error"]
    assert G._load(e.root).get("published") is None and len(e.calls) == 9


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
