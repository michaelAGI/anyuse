"""Pure fixed-task declarations; run only in the VM guarded suite."""
import builtins
import copy
import hashlib
import json

import pytest

from anyuse.execution import rea_ax_state as S
from anyuse.execution import rea_state_task as T
from anyuse.execution import rea_state_reader as SR


TARGET = {"bundle_id": "org.test.fixed-state", "version": "1", "build": "7", "sha256": "a" * 64, "files": 3, "bytes": 1000}
SELECTOR = {"format": S.SELECTOR, "role": "AXWindow", "subrole": "AXDialog", "title": "Options", "geometry": {"kind": "full_display"}}


def state(value):
    def node(index, role, parent, path, children, position, size, title, selected=None):
        row = {"id": index, "parent": parent, "path": path, "children": children,
            "role": role, "subrole": "AXDialog" if index == 0 else None, "title": title,
            "description": "", "enabled": True, "value": selected, "position": position, "size": size,
            "availability": {key: "present" for key in S.ATTRS}}
        if index != 0: row["availability"]["subrole"] = "no_value"
        return row
    return {"format": S.STATE, "displays": [{"position": [0.0, 0.0], "size": [800.0, 600.0]}],
        "focus": {"frontmost_owner": True, "focused_owner": True}, "root": 0, "node_count": 2,
        "nodes": [node(0, "AXWindow", None, [], [1], [0.0, 0.0], [800.0, 600.0], "Options"),
                  node(1, "AXCheckBox", 0, [0], [], [12.0, 30.0], [120.0, 20.0], "Flag", value)]}


def pin(file, raw):
    return {"file": file, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def policy():
    references, material = {}, {}
    for role, selected in (("before", False), ("after", True)):
        contract = {"format": S.CONTRACT, "target": copy.deepcopy(TARGET), "selector": copy.deepcopy(SELECTOR), "state": state(selected)}
        raw = (json.dumps(contract, sort_keys=True, indent=2) + "\n").encode()
        references[role] = {"contract": contract, "reference": pin("/frozen/" + role + "-contract.json", raw)}
        material[role] = raw
    return {"format": T.POLICY, "id": "fixed-flag-transition", "kind": "fixed_state_transition", "params": [],
        "input_scope": "fixed_no_actor_inputs", "heldout_scope": "fresh_trials_with_declared_restart",
        "reference_policy": "independently_frozen_complete_states", "phase_minimums": dict(T.MINIMUMS), **references}, material


def owned_backend(*, identity=None, value=False):
    """A new owned-observer backend; never convert an old S observation/state."""
    import test_rea_owned_ax_state as OX
    backend = OX.Backend(**({"pid": identity["pid"], "launched": identity["launch_time"]} if identity else {}))
    if identity is not None:
        backend.identity = copy.deepcopy(identity)
        backend.current["identity"] = copy.deepcopy(identity)
        backend.current["kernel"].update(uid=identity["uid"], executable=identity["executable"])
        backend.current["session"]["uid"] = identity["uid"]
    backend.checkbox.values["AXValue"] = (0, value)
    return backend


def owned_policy():
    import test_rea_owned_ax_state as OX
    references, material = {}, {}
    for role, value in (("before", False), ("after", True)):
        backend = owned_backend(identity=identity(), value=value)
        observation = OX.observe(backend, target=copy.deepcopy(TARGET), identity=identity())
        assert observation["complete"]
        contract = OX.contract(observation)
        raw = (json.dumps(contract, sort_keys=True, indent=2) + "\n").encode()
        references[role] = {"contract": contract, "reference": pin("/frozen/owned-" + role + "-contract.json", raw)}
        material[role] = raw
    return {"format": T.POLICY2, "observer": {"capability": SR.ownedcap()}, "id": "fixed-owned-window-transition",
        "kind": "fixed_state_transition", "params": [], "input_scope": "fixed_no_actor_inputs",
        "heldout_scope": "fresh_trials_with_declared_restart", "reference_policy": "independently_frozen_complete_states",
        "phase_minimums": dict(T.MINIMUMS), **references}, material


def identity(pid=42, launched=1000.25):
    return {"bundle": "/fixture/Target.app", "bundle_id": TARGET["bundle_id"],
        "executable": "/fixture/Target.app/Contents/MacOS/target", "uid": 501, "pid": pid, "launch_time": launched}


def evidence(case_id, role):
    # These inert references are deliberately NOT valid runtime/settlement
    # bodies. A declarations-only validator must never call them passed trials.
    raw = json.dumps({"declared_case": case_id, "artifact_role": role}, sort_keys=True).encode()
    return pin("/trials/" + case_id + "/" + role + ".json", raw)


def plan(selected):
    phases = {}
    for phase in T.PHASES:
        cases = []
        for index in range(3):
            case_id = phase + "-" + str(index)
            restarted = phase == "heldout"
            restart = restarted and index == 0
            deployment = {"id": "process-1" if restarted else "process-0", "kind": "restart" if restart else "existing_process",
                "identity": identity(100, 2000.5) if restarted else identity(),
                "restart_from": "fresh-2" if restart else None,
                "restart_evidence": evidence(case_id, "restart") if restart else None}
            cases.append({"format": T.CASE, "id": case_id, "phase": phase, "inputs": {}, "deployment": deployment,
                          "evidence": {role: evidence(case_id, role) for role in T.REQUIRED_EVIDENCE}})
        phases[phase] = cases
    return {"format": T.PLAN, "policy_sha256": T.digest(selected), "cases": phases}


def test_fixed_inputless_policy_and_phase_bookkeeping_never_claim_success_from_ids():
    selected, raw = policy()
    declared = plan(selected)
    saved = copy.deepcopy((selected, declared))
    checked = T.validate_policy(selected)
    assert checked["params"] == [] and T.validate_inputs({}) == {}
    assert T.reference_material(selected, "before", raw["before"]) == selected["before"]["contract"]
    report = T.bookkeeping(selected, declared)
    assert report["status"] == "declarations_only" and report["evidence_status"] == "pending"
    assert report["declared_cases"] == {"learning": 3, "fresh": 3, "heldout": 3}
    assert report["declared_heldout_restarts"] == 1 and report["actor_inputs"] == {}
    assert report["trials_validated"] == 0 and not report["task_proof_created"] and not report["public_proof_created"]
    assert not report["restart_deployment_proven"] and not report["input_generalization_claimed"] and not report["novel_actor_inputs_claimed"]
    assert len(report["required_files"]) == 48 and "runtime_request_results_and_cleanup" in report["pending_bindings"]
    assert (selected, declared) == saved
    checked["after"]["contract"]["state"]["nodes"][1]["value"] = False
    assert selected["after"]["contract"]["state"]["nodes"][1]["value"] is True


def test_fresh_cases_can_be_genuinely_new_trials_in_the_same_declared_process():
    selected, _ = policy()
    declared = plan(selected)
    rows = [*declared["cases"]["learning"], *declared["cases"]["fresh"]]
    assert all(case["inputs"] == {} for case in rows)
    assert all(case["deployment"]["identity"] == identity() for case in rows)
    assert all(case["deployment"]["kind"] == "existing_process" for case in rows)
    assert T.validate_plan(selected, declared) == declared


def test_equal_worker_and_state_hashes_are_not_fake_input_or_trial_novelty():
    selected, _ = policy()
    declared = plan(selected)
    for phase in T.PHASES:
        for case in declared["cases"][phase]:
            case["evidence"]["worker"]["sha256"] = "b" * 64
            case["evidence"]["before_settlement"]["sha256"] = "c" * 64
            case["evidence"]["after_settlement"]["sha256"] = "d" * 64
    assert T.validate_plan(selected, declared) == declared
    assert T.bookkeeping(selected, declared)["trials_validated"] == 0


@pytest.mark.parametrize("change", ["lookup", "dummy_param", "novel_inputs", "weak_minimum", "extra", "identical_states", "partial_state", "different_target", "different_selector", "same_reference"])
def test_policy_rejects_unsupported_modes_fake_parameters_and_unfrozen_or_incomplete_contracts(change):
    selected, _ = policy()
    if change == "lookup": selected["kind"] = "scoped_find"
    elif change == "dummy_param": selected["params"] = [{"name": "trial_id", "type": "string"}]
    elif change == "novel_inputs": selected["heldout_scope"] = "novel_actor_inputs"
    elif change == "weak_minimum": selected["phase_minimums"]["fresh"] = 1
    elif change == "extra": selected["verified"] = True
    elif change == "identical_states": selected["after"]["contract"] = copy.deepcopy(selected["before"]["contract"])
    elif change == "partial_state": selected["after"]["contract"]["state"]["nodes"][1].pop("availability")
    elif change == "different_target": selected["after"]["contract"]["target"]["sha256"] = "f" * 64
    elif change == "different_selector":
        selected["after"]["contract"]["selector"]["title"] = "Other"
        selected["after"]["contract"]["state"]["nodes"][0]["title"] = "Other"
    else: selected["after"]["reference"]["file"] = selected["before"]["reference"]["file"]
    with pytest.raises(ValueError): T.validate_policy(selected)


@pytest.mark.parametrize("inputs", [{"case_id": "fresh-1"}, {"value": "constant"}, [], None])
def test_case_identity_never_becomes_a_dummy_actor_input(inputs):
    with pytest.raises(ValueError): T.validate_inputs(inputs)


@pytest.mark.parametrize("role", ["execution", "before_settlement", "after_settlement", "worker", "run_context"])
def test_renaming_case_ids_does_not_allow_reusing_retained_files_across_phases(role):
    selected, _ = policy()
    declared = plan(selected)
    declared["cases"]["fresh"][0]["evidence"][role] = copy.deepcopy(declared["cases"]["learning"][0]["evidence"][role])
    declared["cases"]["fresh"][0]["id"] = "new-label"
    with pytest.raises(ValueError, match="cannot be reused"): T.validate_plan(selected, declared)


def test_one_case_cannot_reuse_its_before_file_as_its_after_or_oracle():
    selected, _ = policy()
    declared = plan(selected)
    first = declared["cases"]["learning"][0]
    first["evidence"]["after_settlement"] = copy.deepcopy(first["evidence"]["before_settlement"])
    with pytest.raises(ValueError, match="cannot be reused"): T.validate_plan(selected, declared)
    declared = plan(selected)
    declared["cases"]["learning"][0]["evidence"]["before_settlement"] = copy.deepcopy(selected["before"]["reference"])
    with pytest.raises(ValueError, match="cannot be reused"): T.validate_plan(selected, declared)


@pytest.mark.parametrize("change", ["missing_case", "phase", "duplicate_id", "unknown_predecessor", "same_epoch", "earlier_epoch", "different_owner", "relabel_epoch", "rebind_deployment", "missing_restart", "missing_evidence", "fake_inputs", "policy_pin"])
def test_phase_and_deployment_bookkeeping_refuses_false_restart_or_incomplete_declarations(change):
    selected, _ = policy()
    declared = plan(selected)
    heldout = declared["cases"]["heldout"][0]
    if change == "missing_case": declared["cases"]["fresh"].pop()
    elif change == "phase": declared["cases"]["fresh"][0]["phase"] = "learning"
    elif change == "duplicate_id": declared["cases"]["fresh"][0]["id"] = "learning-0"
    elif change == "unknown_predecessor": heldout["deployment"]["restart_from"] = "not-recorded"
    elif change == "same_epoch": heldout["deployment"]["identity"] = identity()
    elif change == "earlier_epoch": heldout["deployment"]["identity"]["launch_time"] = 900.0
    elif change == "different_owner": heldout["deployment"]["identity"]["uid"] = 502
    elif change == "relabel_epoch": declared["cases"]["fresh"][0]["deployment"]["id"] = "same-process-new-name"
    elif change == "rebind_deployment": declared["cases"]["fresh"][0]["deployment"]["identity"]["pid"] = 777
    elif change == "missing_restart":
        heldout["deployment"].update(kind="existing_process", restart_from=None, restart_evidence=None)
    elif change == "missing_evidence": declared["cases"]["fresh"][0]["evidence"].pop("execution")
    elif change == "fake_inputs": declared["cases"]["fresh"][0]["inputs"] = {"case_id": "fresh-0"}
    else: declared["policy_sha256"] = "0" * 64
    with pytest.raises(ValueError): T.validate_plan(selected, declared)


@pytest.mark.parametrize("change", ["relative", "traversal", "control", "bad_sha", "zero_bytes", "bool_bytes", "extra"])
def test_real_evidence_refs_require_explicit_bounded_file_pins(change):
    selected, _ = policy()
    declared = plan(selected)
    item = declared["cases"]["fresh"][0]["evidence"]["execution"]
    if change == "relative": item["file"] = "runtime.json"
    elif change == "traversal": item["file"] = "/trials/../runtime.json"
    elif change == "control": item["file"] = "/trials/runtime\n.json"
    elif change == "bad_sha": item["sha256"] = "x" * 64
    elif change == "zero_bytes": item["bytes"] = 0
    elif change == "bool_bytes": item["bytes"] = True
    else: item["passed"] = True
    with pytest.raises(ValueError): T.validate_plan(selected, declared)


def test_reference_material_checks_actual_bytes_and_the_whole_contract_not_a_selected_value():
    selected, raw = policy()
    assert T.reference_material(selected, "after", raw["after"]) == selected["after"]["contract"]
    with pytest.raises(ValueError, match="frozen file pin"): T.reference_material(selected, "after", raw["after"] + b" ")
    changed = copy.deepcopy(selected["after"]["contract"])
    changed["state"]["nodes"][1]["size"][0] += 1
    altered = json.dumps(changed).encode()
    selected["after"]["reference"] = pin("/frozen/after-contract.json", altered)
    with pytest.raises(ValueError, match="entire independently declared"): T.reference_material(selected, "after", altered)


def test_duplicate_keys_in_frozen_reference_are_not_treated_as_independent_contracts():
    selected, _ = policy()
    original = json.dumps(selected["after"]["contract"])
    raw = ('{"format":' + json.dumps(S.CONTRACT) + ',' + original[1:]).encode()
    selected["after"]["reference"] = pin("/frozen/after-contract.json", raw)
    with pytest.raises(ValueError, match="duplicate"): T.reference_material(selected, "after", raw)


def test_pure_bookkeeping_does_no_file_io_observation_actor_or_publication_work(monkeypatch):
    selected, raw = policy()
    declared = plan(selected)
    def forbidden(*_args, **_kwargs): pytest.fail("declaration helper attempted IO or observation")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(S, "capture", forbidden)
    monkeypatch.setattr(S, "settle", forbidden, raising=False)
    assert T.validate_plan(selected, declared) == declared
    assert T.reference_material(selected, "before", raw["before"]) == selected["before"]["contract"]
    assert T.bookkeeping(selected, declared)["evidence_status"] == "pending"


def test_legacy_nonempty_parameter_policy_remains_unchanged():
    from anyuse.execution import rea_harness as H
    from anyuse.execution import rea_agent as A
    with pytest.raises(ValueError): H._params([])
    with pytest.raises(ValueError): A._plan([], {})


def test_explicit_owned_policy_binds_new_complete_references_without_changing_minimums_or_bookkeeping():
    selected, material = owned_policy()
    saved = copy.deepcopy(selected)
    checked = T.validate_policy(selected)
    assert SR.capability(checked) == SR.ownedcap() and "focus" not in checked["before"]["contract"]["state"]
    for role in ("before", "after"):
        assert T.reference_material(checked, role, material[role]) == checked[role]["contract"]
    assert T.bookkeeping(checked, plan(checked))["trials_validated"] == 0
    assert checked["phase_minimums"] == T.MINIMUMS and T.validate_inputs({}) == {}
    assert selected == saved


@pytest.mark.parametrize("fault", ["missing", "unknown", "strict_cap", "extra_observer", "legacy_format",
                                   "legacy_contract", "wrong_state", "weaken_minimum"])
def test_owned_policy_requires_explicit_exact_capability_and_never_relabels_legacy_states(fault):
    selected, _ = owned_policy()
    if fault == "missing": selected.pop("observer")
    elif fault == "unknown": selected["observer"]["capability"] = "custom.module:observer"
    elif fault == "strict_cap": selected["observer"]["capability"] = SR.strictcap()
    elif fault == "extra_observer": selected["observer"]["factory"] = "caller callback"
    elif fault == "legacy_format": selected["format"] = T.POLICY
    elif fault == "legacy_contract": selected["before"]["contract"] = policy()[0]["before"]["contract"]
    elif fault == "wrong_state": selected["after"]["contract"]["state"]["focused_owner"] = True
    else: selected["phase_minimums"]["heldout"] = 1
    with pytest.raises(ValueError): T.validate_policy(selected)


def test_old_policy_schema_and_references_remain_strict_and_cannot_acquire_owned_capability():
    selected, material = policy()
    assert SR.capability(T.validate_policy(selected)) == SR.strictcap()
    assert T.reference_material(selected, "before", material["before"]) == selected["before"]["contract"]
    selected["observer"] = {"capability": SR.ownedcap()}
    with pytest.raises(ValueError): T.validate_policy(selected)
    owned, _ = owned_policy()
    with pytest.raises(ValueError): T.reference_material(owned, "before", material["before"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
