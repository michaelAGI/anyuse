"""Real campaign/proof/catalog gates over inert, file-pinned lower measurements."""
import copy
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-state-campaign-tests-")
(Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
from anyuse import execution
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_harness as H
from anyuse.execution import rea_actions as A
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_state_campaign as C
from anyuse.execution import rea_state_evidence as E
from anyuse.execution import rea_process_restart as RR


def pinned_json(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    R.write_json(path, value)
    path.chmod(0o600)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size}


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    """Only PB/RR are inert; C, G, H, R, A and alias-safe readers stay real."""
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = tmp_path / "Player.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "test.state.player", "CFBundleExecutable": "Player",
        "CFBundleShortVersionString": "1", "CFBundleVersion": "1"}))
    (app / "Contents/MacOS/Player").write_text("inert fixture, never executed")
    target = R.fingerprint(app)
    actor = {"kind": "objc_method", "receiver": {"kind": "class", "class": "NSApplication", "path": [
        {"selector": "sharedApplication", "signature": "@16@0:8"},
        {"selector": "mainWindow", "signature": "@16@0:8"},
        {"selector": "windowController", "signature": "@16@0:8"}]},
        "selector": "pause:", "signature": "v24@0:8@16", "arguments": [{"type": "nil", "value": None}],
        "return_type": "void"}
    actor = B.validate_actor(actor)
    policy = {"format": "test.inert.playback-policy/1", "target": target, "params": []}
    source = pinned_json(tmp_path / "lower-source.json", {"source": "inert independent lower boundary"})
    citation = pinned_json(tmp_path / "interface.json", {"method": {"selector": "pause:"}})
    profile = {"format": C.PROFILE, "id": "pause_playback", "label": "Pause current playback",
               "effect": "fixed_state_transition", "params": [], "policy": policy, "scope": C.SCOPE,
               "evidence": [{"file": citation["file"], "sha256": citation["sha256"], "pointer": "/method"}]}
    old = {"bundle": str(app), "bundle_id": target["bundle_id"], "executable": str(app / "Contents/MacOS/Player"),
           "pid": 111, "launch_time": 1000.0, "uid": os.getuid()}
    new = {**old, "pid": 222, "launch_time": 2000.0}
    kernels = {111: {"pid": 111, "epoch": 1000}, 222: {"pid": 222, "epoch": 2000}}
    state = SimpleNamespace(app=app, actor=actor, policy=policy, profile=profile, source=source,
                            old=old, new=new, calls=[], restart_calls=[], failure=None, failure_result=None)

    def validate_policy(value):
        if value != policy:
            raise ValueError("independent lower policy differs")
        return copy.deepcopy(value)

    def validate_case(selected, case):
        validate_policy(selected)
        if set(case) != {"format", "id", "phase", "evidence"} or case["phase"] not in C.PHASES:
            raise ValueError("invalid inert lower case")
        return copy.deepcopy(case)

    def validate_trial(selected, case, selected_actor):
        validate_case(selected, case)
        if selected_actor != actor:
            raise ValueError("lower actor differs")
        reader = E._Reader()
        raw = reader.json(case["evidence"]["runtime"])
        reader.read(source, "source", retain=False)
        if raw["id"] != case["id"] or raw["phase"] != case["phase"] or raw["verdict"] != "verified":
            raise ValueError("lower trial not independently verified")
        reader.recheck()
        return {"trial_measurement_validated": True, "actor_sha256": R.digest(actor),
                "policy_sha256": R.digest(policy), "case_sha256": R.digest(case), "target": target,
                "identity": raw["identity"], "kernel": raw["kernel"],
                "started_monotonic": raw["started_monotonic"], "finished_monotonic": raw["finished_monotonic"],
                "native_calls": B.minimum_native_calls(actor), "invocations": 1,
                "files": {p["file"]: p for p in [case["evidence"]["runtime"], source]}}

    def make_case(folder, case_id, phase, index, *, raw_changes=None):
        identity = old if index < 6 else new
        raw = {"id": case_id, "phase": phase, "identity": identity, "kernel": kernels[identity["pid"]],
               "started_monotonic": 10.0 + index * 10, "finished_monotonic": 11.0 + index * 10,
               "verdict": "verified"}
        raw.update(raw_changes or {})
        runtime = pinned_json(Path(folder) / "runtime.json", raw)
        case = {"format": "test.inert.playback-case/1", "id": case_id, "phase": phase,
                "evidence": {"runtime": runtime}}
        return {"case": pinned_json(Path(folder) / "case.json", case), "restart": None}

    def make_intent(bundle, selected, case_id, phase):
        validate_policy(selected)
        return {"id": case_id, "phase": phase}

    def run_trial(bundle, selected_actor, selected, intent, out, *, timeout=30):
        state.calls.append(copy.deepcopy(intent))
        if state.failure_result is not None:
            return copy.deepcopy(state.failure_result)
        if state.failure:
            raise ValueError(state.failure)
        if intent is None:
            intent = {"id": "catalog-replay", "phase": "heldout"}
            index = 10
        else:
            index = {row["id"]: i for i, row in enumerate(state.rows)}[intent["id"]]
        entry = make_case(out, intent["id"], intent["phase"], index)
        case = R.load_json(entry["case"]["file"])
        return {"status": "trial_measurement_validated", "case_file": entry["case"], "case": case,
                "measurement": validate_trial(selected, case, selected_actor)}

    lower = SimpleNamespace(validate_policy=validate_policy, validate_case=validate_case,
                            validate_trial=validate_trial, make_intent=make_intent, run_trial=run_trial)
    monkeypatch.setitem(sys.modules, "anyuse.execution.rea_playback_trial", lower)
    monkeypatch.setattr(execution, "rea_playback_trial", lower, raising=False)
    state.rows = [{"id": f"{phase}-{i}", "phase": phase} for phase in C.PHASES for i in range(3)]
    state.entries = {phase: [make_case(tmp_path / "trials" / row["id"], row["id"], phase, j)
                             for j, row in enumerate(state.rows) if row["phase"] == phase]
                     for phase in C.PHASES}
    baseline = pinned_json(tmp_path / "restart/baseline.json", {"started_monotonic": 62.0})
    intent_pin = pinned_json(tmp_path / "restart/intent.json", {"id": "inert-normal-restart", "target": target})
    receipt_data = {"baseline": baseline, "finished_monotonic": 66.0, "old_identity": old, "new_identity": new,
                    "old_kernel": kernels[111], "new_kernel": kernels[222]}
    receipt = pinned_json(tmp_path / "restart/receipt.json", receipt_data)
    state.restart = {"intent": intent_pin, "receipt": receipt, "predecessor": "fresh-2"}
    state.entries["heldout"][0]["restart"] = copy.deepcopy(state.restart)

    def validate_restart(declaration, receipt_pin):
        state.restart_calls.append(copy.deepcopy(receipt_pin))
        reader = E._Reader()
        witness = reader.json(receipt_pin)
        reader.json(witness["baseline"])
        reader.recheck()
        if declaration != R.load_json(intent_pin["file"]):
            raise ValueError("restart declaration differs")
        return {"restart_measurement_validated": True, "target": target,
                **{key: witness[key] for key in ("old_identity", "new_identity", "old_kernel", "new_kernel")},
                "files": {p["file"]: p for p in [receipt_pin, witness["baseline"]]}}

    monkeypatch.setattr(RR, "validate_restart", validate_restart)
    state.make_case = make_case
    state.receipt_data = receipt_data
    state.lower = lower
    return state


def learn_campaign(state, tmp_path):
    demos = [{"actor": state.actor, "case": entry["case"]} for entry in state.entries["learning"]]
    return H.learn(state.profile, demos, str(state.app), str(tmp_path / "learn"))


def fresh_campaign(state, tmp_path):
    candidate = learn_campaign(state, tmp_path)
    proof = R.verify(candidate, state.entries["fresh"], str(state.app), str(tmp_path / "fresh"))
    return candidate, proof


def complete_campaign(state, tmp_path):
    candidate, fresh = fresh_campaign(state, tmp_path)
    proof = C.heldout(candidate, fresh, state.entries["heldout"], str(state.app), str(tmp_path / "heldout"))
    return candidate, proof


def test_three_phase_promotion_catalog_and_current_replay(campaign, tmp_path):
    candidate, proof = complete_campaign(campaign, tmp_path)
    assert proof["measurement"]["phase_counts"] == {"learning": 3, "fresh": 3, "heldout": 3}
    assert proof["measurement"]["task_invocations"] == 9
    assert proof["measurement"]["native_calls"] == 36
    assert proof["measurement"]["restart_deployment_proven"] and campaign.restart_calls
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    card = A.describe(candidate, proof, str(campaign.app), "Pause playback", "Only the measured fixed transition.", str(tmp_path / "card"))
    assert card["contract"]["policy"] == campaign.policy and card["contract"]["scope"] == C.SCOPE
    assert "trial_policy" not in candidate and "trial_policy" not in proof["measurement"]
    assert set(card["contract"]) == {"effect", "item_kind", "mode", "params", "scope", "reference", "policy"}
    candidate_file = pinned_json(tmp_path / "candidate.json", candidate)
    proof_file = pinned_json(tmp_path / "proof.json", proof)
    catalog = A.catalog([{"candidate": candidate_file["file"], "proof": proof_file["file"],
                          "card": str(tmp_path / "card/action.json"), "bundle": str(campaign.app)}], str(tmp_path / "catalog"))
    action = catalog["actions"][0]
    selection = {"format": A.SELECTION, "catalog_sha256": R.digest(catalog), "status": "selected",
                 "action": action["id"], "candidate_sha256": action["candidate_sha256"], "card_sha256": action["card_sha256"]}
    assert A.execute(catalog, selection, {}, str(tmp_path / "replay"))["verdict"] == "verified"
    assert campaign.calls == [None]


def test_expected_epoch_reaches_playback_runner_through_mapping_and_harness(campaign, tmp_path, monkeypatch):
    candidate, proof = complete_campaign(campaign, tmp_path)
    lower = C._playback()
    original = lower.run_trial
    supplied = copy.deepcopy(campaign.new)
    disposition = R._ReplayDisposition()
    seen = []
    def run(*args, expected_identity, _disposition, **kwargs):
        seen.append(copy.deepcopy(expected_identity))
        assert expected_identity == supplied
        assert _disposition is disposition
        return original(*args, **kwargs)
    monkeypatch.setattr(lower, "run_trial", run)
    result = R.replay(candidate, proof, {}, str(campaign.app), str(tmp_path / "pinned-replay"),
                      expected_identity=supplied, _disposition=disposition)
    assert result["verdict"] == "verified" and seen == [supplied]


def test_fresh_only_and_asserted_proof_cannot_publish(campaign, tmp_path):
    candidate, proof = fresh_campaign(campaign, tmp_path)
    assert proof["status"] == "fresh_verified"
    for unsupported in (proof, {**proof, "status": "verified"}, {"status": "verified"}):
        assert not R.proof_status(candidate, unsupported, candidate["target"])[0]
        with pytest.raises(ValueError):
            A.describe(candidate, unsupported, str(campaign.app), "Pause", "Not enough evidence", str(tmp_path / "no-card"))
    assert not (tmp_path / "no-card").exists()


@pytest.mark.parametrize("phase", ["learning", "fresh", "heldout"])
def test_every_phase_requires_three_measured_trials(campaign, tmp_path, phase):
    campaign.entries[phase] = campaign.entries[phase][:2]
    with pytest.raises(ValueError, match="three|3"):
        complete_campaign(campaign, tmp_path)


@pytest.mark.parametrize("damage", ["duplicate", "shared_runtime", "reversed", "wrong_phase"])
def test_cross_trial_reuse_phase_and_order_rejected(campaign, tmp_path, damage):
    candidate = learn_campaign(campaign, tmp_path)
    rows = copy.deepcopy(campaign.entries["fresh"])
    if damage == "duplicate": rows[1] = rows[0]
    elif damage == "reversed": rows.reverse()
    else:
        case = R.load_json(rows[1]["case"]["file"])
        if damage == "shared_runtime": case["evidence"] = R.load_json(rows[0]["case"]["file"])["evidence"]
        else: case["phase"] = "heldout"
        rows[1]["case"] = pinned_json(tmp_path / "changed-case.json", case)
    with pytest.raises(ValueError):
        R.verify(candidate, rows, str(campaign.app), str(tmp_path / "bad-fresh"))
    assert not (tmp_path / "bad-fresh").exists()


@pytest.mark.parametrize("damage", ["old_identity", "new_kernel", "late_finish", "early_baseline", "missing"])
def test_restart_requires_bound_identity_kernel_and_timeline(campaign, tmp_path, damage):
    candidate, fresh = fresh_campaign(campaign, tmp_path)
    rows = copy.deepcopy(campaign.entries["heldout"])
    raw = copy.deepcopy(campaign.receipt_data)
    if damage == "missing": rows[0]["restart"] = None
    else:
        if damage == "old_identity": raw["old_identity"]["pid"] = 333
        elif damage == "new_kernel": raw["new_kernel"]["epoch"] = 999
        elif damage == "late_finish": raw["finished_monotonic"] = 71.0
        else: raw["baseline"] = pinned_json(tmp_path / "bad-baseline.json", {"started_monotonic": 59.0})
        rows[0]["restart"]["receipt"] = pinned_json(tmp_path / "bad-restart.json", raw)
    with pytest.raises(ValueError):
        C.heldout(candidate, fresh, rows, str(campaign.app), str(tmp_path / "bad-heldout"))
    assert not (tmp_path / "bad-heldout").exists()


def test_restart_cannot_precede_fresh_trials_and_claim_a_heldout_restart(campaign, tmp_path):
    # A real restart occurred after learning; all fresh and held-out trials use
    # its new process. Reattaching that old witness to heldout[0] is not a second
    # restart at the claimed boundary.
    for i in range(3):
        campaign.entries["fresh"][i] = campaign.make_case(tmp_path / "new-fresh" / str(i),
            f"fresh-{i}", "fresh", 3 + i,
            raw_changes={"identity": campaign.new, "kernel": {"pid": 222, "epoch": 2000}})
    raw = copy.deepcopy(campaign.receipt_data)
    raw["baseline"] = pinned_json(tmp_path / "early/baseline.json", {"started_monotonic": 32.0})
    raw["finished_monotonic"] = 36.0
    campaign.entries["heldout"][0]["restart"] = {**campaign.restart,
        "receipt": pinned_json(tmp_path / "early/receipt.json", raw), "predecessor": "learning-2"}
    with pytest.raises(ValueError, match="without a witnessed restart"):
        complete_campaign(campaign, tmp_path / "invalid-campaign")


@pytest.mark.parametrize("damage", ["older_predecessor", "kernel_change"])
def test_trial_epoch_continuity_and_immediate_restart_predecessor(campaign, tmp_path, damage):
    if damage == "older_predecessor":
        campaign.entries["heldout"][0]["restart"]["predecessor"] = "learning-2"
        match = "immediate measured predecessor"
    else:
        campaign.entries["fresh"][1] = campaign.make_case(tmp_path / "changed-kernel", "fresh-1", "fresh", 4,
            raw_changes={"kernel": {"pid": 111, "epoch": 1001}})
        match = "without a witnessed restart"
    with pytest.raises(ValueError, match=match):
        complete_campaign(campaign, tmp_path / "invalid-campaign")


def test_source_mutation_and_nonempty_actor_inputs_fail_closed(campaign, tmp_path):
    candidate, proof = complete_campaign(campaign, tmp_path)
    with pytest.raises(ValueError): C.bind(candidate, {"trial_id": "invented novelty"})
    with pytest.raises(ValueError): R.replay(candidate, proof, {"file": "not-an-actor-parameter"}, str(campaign.app), str(tmp_path / "bad-replay"))
    assert campaign.calls == []
    Path(campaign.source["file"]).write_text("changed lower source")
    assert not R.proof_status(candidate, proof, candidate["target"])[0]


def test_catalog_replay_preserves_uncertain_cleanup_disposition(campaign, tmp_path):
    candidate, proof = complete_campaign(campaign, tmp_path)
    A.describe(candidate, proof, str(campaign.app), "Pause", "Measured playback transition", str(tmp_path / "review-card"))
    candidate_file = pinned_json(tmp_path / "review-candidate.json", candidate)
    proof_file = pinned_json(tmp_path / "review-proof.json", proof)
    catalog = A.catalog([{"candidate": candidate_file["file"], "proof": proof_file["file"],
                          "card": str(tmp_path / "review-card/action.json"), "bundle": str(campaign.app)}], str(tmp_path / "review-catalog"))
    action = catalog["actions"][0]
    selection = {"format": A.SELECTION, "catalog_sha256": R.digest(catalog), "status": "selected",
                 "action": action["id"], "candidate_sha256": action["candidate_sha256"], "card_sha256": action["card_sha256"]}
    campaign.failure_result = {"status": "failed", "resource_review_required": True,
                               "error": {"message": "native cleanup is uncertain"}}
    with pytest.raises(R.ResourceReviewRequired):
        A.execute(catalog, selection, {}, str(tmp_path / "uncertain-replay"))
    assert campaign.calls == [None]


def test_hardlink_alias_cannot_supply_independent_trial(campaign, tmp_path):
    candidate = learn_campaign(campaign, tmp_path)
    rows = copy.deepcopy(campaign.entries["fresh"])
    source = Path(rows[0]["case"]["file"])
    alias = tmp_path / "alias-case.json"
    alias.hardlink_to(source)
    rows[0]["case"] = {**rows[0]["case"], "file": str(alias)}
    with pytest.raises(ValueError, match="unaliased"):
        R.verify(candidate, rows, str(campaign.app), str(tmp_path / "alias-fresh"))


def test_fixed_observer_declaration_needs_no_citations_or_oracle_io(campaign, monkeypatch):
    declaration = {k: copy.deepcopy(v) for k, v in campaign.profile.items() if k != "evidence"}
    monkeypatch.setattr(R, "load_json", lambda *a: pytest.fail("declaration validation must not read evidence"))
    validated = C.validate_profile_declaration(declaration)
    assert validated == declaration and "evidence" not in validated
    validated["policy"]["target"]["version"] = "caller mutation"
    assert declaration == {k: v for k, v in campaign.profile.items() if k != "evidence"}
    with pytest.raises(ValueError, match="closed"):
        C.validate_profile_declaration(campaign.profile)
    with pytest.raises(ValueError, match="closed"):
        C.validate_profile({**declaration, "evidence": [{"fake": "not a real citation"}]})


def test_fixed_begin_approval_binds_policy_and_allows_hash_only_actor_proposal(campaign, tmp_path):
    from anyuse.execution import rea_agent as G
    declaration = {k: copy.deepcopy(v) for k, v in campaign.profile.items() if k != "evidence"}
    plan = {"format": C.PLAN, **{role: [{"id": f"{role}-{i}", "inputs": {}, "restart": role == "heldout" and i == 0}
        for i in range(3)] for role in ("learning", "verification", "heldout")}}
    rea = tmp_path / "inert-rea"; rea.write_text("never run"); rea.chmod(0o700)
    root = tmp_path / "approved-fixed-session"
    context = G.begin(campaign.app, rea, "Pause playback", [], ["Independent playback transition"], plan, root,
                      observer_profile=declaration)
    assert context["observer_profile"]["declaration"]["policy"] == campaign.policy
    assert context["observer_profile_sha256"] == R.digest(declaration)
    request = {"op": "propose", "actor": campaign.actor, "rationale": "An attributable typed hypothesis, not proof",
        "citations": [{"artifact": "inventory.json", "pointer": "/target"}],
        "observer_profile_sha256": context["observer_profile_sha256"]}
    result = G.step(root, request)
    assert result["feedback"]["status"] == "completed" and result["context"]["status"] == "hypothesis"
    assert result["context"]["proof_created"] is False and campaign.calls == []
    state = R.load_json(root / "session.json")
    assert state["hypothesis"]["profile"]["policy"] == state["observer_profile"]["policy"]
    assert state["hypothesis"]["profile"]["evidence"][0]["file"] == str(root / "inventory.json")
    changed = {**request, "profile": {**declaration, "policy": {**declaration["policy"], "invented_threshold": 0}}}
    assert G.step(root, changed)["feedback"]["status"] == "failed" and campaign.calls == []
    wrong = copy.deepcopy(declaration); wrong["policy"]["target"]["build"] = "other"
    refused = tmp_path / "wrong-target"
    with pytest.raises(ValueError):
        G.begin(campaign.app, rea, "Pause playback", [], ["Independent playback transition"], plan, refused,
                observer_profile=wrong)
    assert not refused.exists()


def test_trial_policy_fast_preset_and_custom_overrides(campaign, tmp_path):
    assert C.validate_trial_policy() == {"min_learning": 3, "min_verification": 3, "min_heldout": 3, "require_restart": True}
    assert C.validate_trial_policy("fast") == {"min_learning": 1, "min_verification": 1, "min_heldout": 1, "require_restart": True}
    assert C.validate_trial_policy({"preset": "fast", "fresh": 2, "restart": False}) == {
        "min_learning": 1, "min_verification": 2, "min_heldout": 1, "require_restart": False}
    with pytest.raises(ValueError):
        C.validate_trial_policy({"min_learning": 0})
    with pytest.raises(ValueError):
        C.validate_trial_policy({"learn": 1, "min_learning": 2})

    fast_plan = {
        "format": C.PLAN,
        "trial_policy": "fast",
        "learning": [{"id": "l0", "inputs": {}, "restart": False}],
        "verification": [{"id": "v0", "inputs": {}, "restart": False}],
        "heldout": [{"id": "h0", "inputs": {}, "restart": True}],
    }
    validated_plan = C.validate_plan(fast_plan)
    assert validated_plan["trial_policy"] == C.FAST_TRIAL_POLICY

    # Promote a 1-learn, 1-fresh, 1-heldout campaign (with restart on heldout[0] binding v0 -> h0)
    learn_entry = campaign.entries["learning"][0]
    fresh_entry = campaign.entries["fresh"][2]
    heldout_entry = campaign.entries["heldout"][0]
    demos = [{"actor": campaign.actor, "case": learn_entry["case"]}]
    candidate = C.learn(campaign.profile, demos, str(campaign.app), str(tmp_path / "fast-learn"), trial_policy="fast")
    assert candidate["trial_policy"] == C.FAST_TRIAL_POLICY
    fresh_proof = C.verify(candidate, [fresh_entry], str(campaign.app), str(tmp_path / "fast-verify"))
    complete = C.heldout(candidate, fresh_proof, [heldout_entry], str(campaign.app), str(tmp_path / "fast-heldout"))
    assert complete["measurement"]["phase_counts"] == {"learning": 1, "fresh": 1, "heldout": 1}
    assert complete["measurement"]["trial_policy"] == C.FAST_TRIAL_POLICY
    assert complete["measurement"]["restart_deployment_proven"] is True
    assert C.proof_status(candidate, complete, R.fingerprint(campaign.app))[0] is True


@pytest.mark.parametrize("pairs", [
    [("learning", 3), ("learn", 1)],
    [("min_learning", 3), ("learning", 1)],
    [("fresh", 2), ("verification", 1)],
    [("restart", False), ("require_restart", True)],
    [("learning", True), ("learn", 1)],
    [("restart", 1), ("require_restart", True)],
])
def test_trial_policy_alias_conflicts_are_order_and_type_independent(pairs):
    for ordered in (pairs, list(reversed(pairs))):
        with pytest.raises(ValueError, match="conflicting trial_policy values"):
            C.validate_trial_policy(dict(ordered))


def test_equal_trial_policy_aliases_survive_sorted_serialization(tmp_path):
    raw = {"learning": 2, "learn": 2, "min_learning": 2, "restart": False, "require_restart": False}
    expected = {**C.DEFAULT_TRIAL_POLICY, "min_learning": 2, "require_restart": False}
    assert C.validate_trial_policy(raw) == expected
    path = tmp_path / "aliases.json"
    R.write_json(path, raw)
    assert C.validate_trial_policy(R.load_json(path)) == expected


@pytest.mark.parametrize("witness_restart", [False, True])
def test_optional_restart_campaign_reports_only_actual_restart_coverage(campaign, tmp_path, witness_restart):
    tp = C.validate_trial_policy({"preset": "fast", "restart": False})
    profile = {**campaign.profile, "scope": C.SCOPE_NO_RESTART}
    assert C.validate_profile(profile)["scope"] == C.SCOPE_NO_RESTART
    candidate = C.learn(profile, [{"actor": campaign.actor, "case": campaign.entries["learning"][0]["case"]}],
                        str(campaign.app), str(tmp_path / "optional-learn"), trial_policy=tp)
    assert candidate["scope"] == C.SCOPE_NO_RESTART
    with pytest.raises(ValueError, match="scope differs"):
        C.validate({**candidate, "scope": C.SCOPE})
    fresh = C.verify(candidate, [campaign.entries["fresh"][2]], str(campaign.app), str(tmp_path / "optional-fresh"))
    assert fresh["measurement"]["scope"] == C.SCOPE_NO_RESTART
    entry = campaign.entries["heldout"][0] if witness_restart else campaign.make_case(
        tmp_path / "without-restart", "heldout-without-restart", "heldout", 6,
        raw_changes={"identity": campaign.old, "kernel": {"pid": 111, "epoch": 1000}})
    complete = C.heldout(candidate, fresh, [entry], str(campaign.app), str(tmp_path / "optional-heldout"))
    assert complete["measurement"]["restart_deployment_proven"] is witness_restart
    assert complete["measurement"]["scope"] == (C.SCOPE if witness_restart else C.SCOPE_NO_RESTART)
    ready, reason = C.proof_status(candidate, complete, candidate["target"])
    assert ready and ("and witnessed restart" if witness_restart else "without witnessed restart") in reason
    card = A.describe(candidate, complete, str(campaign.app), "Pause playback", "A measured fixed transition.", str(tmp_path / "optional-card"))
    assert card["contract"]["scope"] == C.SCOPE_NO_RESTART
    assert card["contract"]["trial_policy"] == tp
    entry = {"candidate": pinned_json(tmp_path / "optional-candidate.json", candidate)["file"],
             "proof": pinned_json(tmp_path / "optional-proof.json", complete)["file"],
             "card": str(tmp_path / "optional-card/action.json"), "bundle": str(campaign.app)}
    assert A._entry(entry)[2] == card
    card["contract"].pop("trial_policy")
    R.write_json(entry["card"], card)
    with pytest.raises(ValueError, match="measured contract changed"):
        A._entry(entry)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
