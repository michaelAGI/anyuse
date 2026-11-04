"""Pure failed-startup recovery contracts; execute in the guarded guest only."""
import json
from pathlib import Path

import pytest

from anyuse.execution import rea_inspector_recovery as E
from anyuse.execution import rea_inspector_lifecycle as L
from test_rea_inspector_lifecycle import fixture, private, runtime


def failed_startup(tmp_path, monkeypatch, *, restart=False):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    prior = None
    if restart:
        prior = L.launch(f.policy_file, f.root / "launch", timeout=10)
        assert prior["status"] == "ready"
        state.alive, state.pid, state.out_name, state.is_restart = False, 7102, "restart", True
    state.snapshot_error = ValueError("preserved failed startup fixture")
    original = L.launch(f.policy_file, f.root / state.out_name,
                        previous=prior["receipt"] if prior else None, timeout=10)
    assert original["status"] == "incomplete" and state.alive
    monkeypatch.setattr(E.I, "_process", lambda pid, deadline: state.kernel())
    # None of these providers may run during resource review or normal close.
    monkeypatch.setattr(E.I, "_socket", lambda *a: pytest.fail("recovery opened a CDP connection"))
    monkeypatch.setattr(E.I, "collect", lambda *a, **k: pytest.fail("recovery executed an inspector transaction"))
    monkeypatch.setattr(L.B, "collect", lambda *a, **k: pytest.fail("recovery executed a native task"))
    monkeypatch.setattr(L, "_open", lambda *a: pytest.fail("recovery attempted setup/relaunch"))
    return f, state, original, prior


@pytest.mark.parametrize("name,args", [("review_failed", ("missing", "out")),
                                      ("close_failed", ("missing", "missing-review", "out"))])
def test_vm_guard_precedes_all_reads_and_native_operations(monkeypatch, name, args):
    def denied(): raise PermissionError("VM required")
    monkeypatch.setattr(E.R, "assert_guest", denied)
    monkeypatch.setattr(E.L, "_private", lambda *a: pytest.fail("read before VM guard"))
    monkeypatch.setattr(E, "_observe", lambda *a: pytest.fail("native operation before VM guard"))
    with pytest.raises(PermissionError, match="VM required"): getattr(E, name)(*args)


@pytest.mark.parametrize("field,value", [
    ("status", "ready"), ("launch_attempts", 0), ("launch_attempts", True),
    ("termination_requests", 1), ("task_calls", 1), ("app_task_methods", 1),
    ("model_calls", 1), ("proof_created", True), ("task_result_verified", True),
    ("identity", None), ("kernel_identity", None), ("resource_review_required", False),
    ("identity", {}), ("kernel_identity", {}), ("launch_requested_at", True),
])
def test_incomplete_intent_without_exact_zero_task_provenance_refuses_before_review(tmp_path, monkeypatch, field, value):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    original[field] = value
    private(Path(original["receipt"]), original)
    monkeypatch.setattr(E, "_observe", lambda *a: pytest.fail("invalid intent reached native review"))
    with pytest.raises(ValueError): E.review_failed(original["receipt"], f.root / "review")
    assert not (f.root / "review").exists() and not state.terminations


@pytest.mark.parametrize("kind", ["lifecycle_source", "policy", "target", "profile_inode", "hardlink", "alias"])
def test_original_source_policy_target_and_owned_paths_are_exact(tmp_path, monkeypatch, kind):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    selected = Path(original["receipt"])
    if kind == "lifecycle_source":
        original["source"]["sha256"] = "0" * 64
        private(selected, original)
    elif kind == "policy":
        private(f.policy_file, {**f.policy, "relative_url": "Contents/Resources/app.asar/changed.html"})
    elif kind == "target":
        original["target"] = {**original["target"], "sha256": "0" * 64}
        private(selected, original)
    elif kind == "profile_inode":
        f.profile.rename(f.root / "old-profile")
        f.profile.mkdir(mode=0o700)
    elif kind == "hardlink":
        import os
        os.link(selected, f.root / "receipt-copy")
    else:
        selected = f.root / "receipt-alias"
        selected.symlink_to(original["receipt"])
    with pytest.raises((OSError, ValueError)): E.review_failed(selected, f.root / "review")
    assert not (f.root / "review").exists() and not state.terminations


@pytest.mark.parametrize("listener", ["owned", "absent"])
def test_review_and_separate_normal_close_preserve_original_failure_and_no_task_credit(tmp_path, monkeypatch, listener):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    before = Path(original["receipt"]).read_bytes()
    if listener == "absent": monkeypatch.setattr(L, "_port_absent", lambda *a: True)
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert reviewed["status"] == "resource_reviewed" and reviewed["resource_review_required"]
    assert reviewed["listener"]["status"] == ("absent" if listener == "absent" else "owned_loopback_listener")
    assert reviewed["config_state"]["kind"] == "absent_initial" and not f.config.exists()
    assert reviewed["metadata_only"] and reviewed["ownership_revalidated"] and not state.terminations
    assert not reviewed["original_uncertainty_cleared"] and not reviewed["resources_closed"]
    assert not reviewed["isolation_verified"] and reviewed["observed_profile_use"] is None
    closed = E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=10)
    assert closed["status"] == "closed" and closed["resources_closed"]
    assert closed["termination_requests"] == 1 and len(state.terminations) == 1 and not state.alive
    assert closed["original_status"] == "incomplete" and closed["original_resource_review_required"]
    assert not closed["original_uncertainty_cleared"] and not closed["effect_rollback_claimed"]
    assert not closed["metadata_only"] and not closed["proof_created"] and not closed["task_result_verified"]
    assert closed["launch_attempts"] == closed["task_calls"] == closed["model_calls"] == closed["app_task_methods"] == 0
    assert all(closed["closure_observation"].values())
    assert Path(original["receipt"]).read_bytes() == before


def test_missing_or_replaced_first_process_is_never_adopted(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    state.pid = 9999
    result = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert result["status"] == "incomplete" and "no adoption" in result["error"]["message"]
    assert not result["ownership_revalidated"] and not state.terminations


def test_foreign_listener_refuses_without_cdp_or_termination(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    def foreign(*a): raise ValueError("listener belongs to another process")
    monkeypatch.setattr(E.I, "_listener", foreign)
    result = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert result["status"] == "incomplete" and result["resource_review_required"]
    assert not state.terminations and state.alive and not f.config.exists()


def test_present_unpinned_initial_configuration_cannot_be_adopted(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    private(f.config, E.L._config_data(f.policy, state.suffix))
    with pytest.raises(ValueError, match="present unpinned"):
        E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert not (f.root / "review").exists() and not state.terminations


def test_initial_frozen_config_must_match_original_pin_and_raw_bytes(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    private(f.config, L._config_data(f.policy, state.suffix))
    config, pin = E.I._config_for_target(f.policy["target"], str(f.config))
    original["transport"] = E.I._transport(config, pin)
    private(Path(original["receipt"]), original)
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert reviewed["status"] == "resource_reviewed"
    assert reviewed["config_state"]["provenance"] == "original_frozen_transport"
    f.config.write_bytes(f.config.read_bytes() + b" ")
    with pytest.raises(ValueError, match="configuration changed"):
        E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=10)
    assert not (f.root / "close").exists() and not state.terminations


def test_restart_failed_recovery_requires_exact_pinned_previous_ready_transport(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch, restart=True)
    raw = f.config.read_bytes()
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert reviewed["status"] == "resource_reviewed"
    assert reviewed["config_state"]["provenance"] == "pinned_previous_ready_launch"
    assert reviewed["config_state"]["transport"] == prior["transport"]
    closed = E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=10)
    assert closed["status"] == "closed" and f.config.read_bytes() == raw and len(state.terminations) == 1


@pytest.mark.parametrize("kind", ["missing", "edited", "not_ready", "unbound_transport"])
def test_restart_provenance_ambiguity_refuses_without_adoption(tmp_path, monkeypatch, kind):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch, restart=True)
    prior_path = Path(prior["receipt"])
    if kind == "missing": prior_path.unlink()
    elif kind == "edited": prior_path.write_bytes(prior_path.read_bytes() + b" ")
    elif kind == "not_ready":
        prior["status"] = "incomplete"
        private(prior_path, prior)
    else:
        original["previous"] = None
        private(Path(original["receipt"]), original)
    with pytest.raises((OSError, ValueError)): E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert not (f.root / "review").exists() and not state.terminations


@pytest.mark.parametrize("change", ["original", "review_source", "review_original", "review_promoted"])
def test_close_requires_exact_explicit_review_and_unchanged_original(tmp_path, monkeypatch, change):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    if change == "original": Path(original["receipt"]).write_bytes(Path(original["receipt"]).read_bytes() + b" ")
    else:
        if change == "review_source": reviewed["source"]["sha256"] = "0" * 64
        elif change == "review_original": reviewed["original"]["sha256"] = "0" * 64
        else: reviewed["original_uncertainty_cleared"] = True
        private(Path(reviewed["receipt"]), reviewed)
    with pytest.raises(ValueError):
        E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=10)
    assert not (f.root / "close").exists() and not state.terminations


def test_original_change_after_review_cannot_be_hidden_by_current_metadata(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    # Same main PID, changed kernel start is not the reviewed process.
    original_kernel = state.kernel
    state.kernel = lambda: {**original_kernel(), "started_at": "replacement start"}
    result = E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=10)
    assert result["status"] == "incomplete" and result["termination_requests"] == 0 and not state.terminations


def test_full_identity_recheck_after_provenance_read_precedes_normal_termination(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    actual = E._unchanged
    def changed(*args, **kwargs):
        actual(*args, **kwargs)
        state.pid = 9999
    monkeypatch.setattr(E, "_unchanged", changed)
    result = E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=10)
    assert result["status"] == "incomplete" and result["termination_requests"] == 0 and not state.terminations


@pytest.mark.parametrize("behavior", ["refuse", "dialog"])
def test_normal_close_failure_or_deadline_never_escalates_or_retries(tmp_path, monkeypatch, behavior):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    reviewed = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    def no_exit(*args, before_request):
        before_request()
        state.terminations.append(state.pid)
        return behavior != "refuse"
    monkeypatch.setattr(L, "_terminate", no_exit)
    started = L.time.monotonic()
    result = E.close_failed(original["receipt"], reviewed["receipt"], f.root / "close", timeout=1)
    assert result["status"] == "incomplete" and result["resource_review_required"] and state.alive
    assert result["termination_requests"] == 1 and len(state.terminations) == 1
    assert not result["resources_closed"] and not result["original_uncertainty_cleared"]
    if behavior == "dialog":
        assert result["error"]["type"] == "TimeoutError" and 1 <= L.time.monotonic() - started < 1.2


def test_unrelated_same_bundle_kernel_candidate_refuses_review(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    main = state.kernel()
    other = {**main, "pid": 9000, "ppid": 3}
    ancestor = {**main, "pid": 3, "ppid": 1, "executable": "/unrelated/process"}
    monkeypatch.setattr(L, "_kernel_candidates", lambda *a: [main, other])
    monkeypatch.setattr(E.I, "_process", lambda pid, deadline: {main["pid"]: main, 9000: other, 3: ancestor}[pid])
    result = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert result["status"] == "incomplete" and "owned descendant" in result["error"]["message"]
    assert not state.terminations


def test_current_absence_and_review_deadlines_cannot_silently_change(tmp_path, monkeypatch):
    f, state, original, prior = failed_startup(tmp_path, monkeypatch)
    actual = E._observe
    def after_observation(*args, **kwargs):
        result = actual(*args, **kwargs)
        private(f.config, L._config_data(f.policy, state.suffix))
        return result
    monkeypatch.setattr(E, "_observe", after_observation)
    result = E.review_failed(original["receipt"], f.root / "review", timeout=10)
    assert result["status"] == "incomplete" and not result["ownership_revalidated"] and not state.terminations


if __name__ == "__main__":
    E.R.assert_guest()
    raise SystemExit(pytest.main([__file__, "-q"]))
