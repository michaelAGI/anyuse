"""Inert retained-evidence corruption tests; VM guarded execution only.

All source declarations, Mach-O images, AX backends and worker receipts here
are explicitly inert test material. None is actual app/task/attachment proof.
"""
import copy
import json
import math
import os
from pathlib import Path

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_ax_state as S
from anyuse.execution import rea_state_task as T
from anyuse.execution import rea_state_evidence as E
from anyuse.execution import rea_state_reader as SR
from anyuse.execution import rea_runtime_permit as RP
from test_rea_runtime_bridge import runtime, native_files, _phased_fixture, _phase_settlement
from test_rea_state_trial import prepared_qt, prepared_owned, prepared_ack, run as run_prospective


LIMITS = {"setup_seconds": 2, "before_seconds": 8, "after_seconds": 8, "margin_seconds": 1}


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size}


def _commit(data, *, mirror_worker=True, worker_raw=None):
    """Repin actually written inert bytes, so semantic corruptions reach the gate."""
    out, native_folder = data["out"], data["out"] / "runtime"
    for name, body in (("worker-output.json", data["worker"]), ("worker-request.json", data["config"]),
                       ("worker-cleanup.json", data["journal"])):
        if name == "worker-output.json" and worker_raw is not None:
            (native_folder / name).write_bytes(worker_raw)
        else: R.write_json(native_folder / name, body)
    if mirror_worker: data["native"]["result"] = copy.deepcopy(data["worker"])
    data["native"]["worker"]["sha256"] = pin(native_folder / "worker-output.json")["sha256"]
    names = {"worker-output.json", "worker-request.json", "worker-cleanup.json", "worker.stderr", "trace.jsonl"}
    if "permit_worker" in data: names.add("worker-permit.json")
    data["native"]["artifacts"] = [{"file": name, "bytes": pin(native_folder / name)["bytes"],
        "sha256": pin(native_folder / name)["sha256"], "oversized": False} for name in
        sorted(names)]
    R.write_json(native_folder / "runtime.json", data["native"])
    selected = data["context"]["native_receipt"]
    selected["sha256"] = pin(native_folder / "runtime.json")["sha256"]
    for phase in ("before", "after"):
        R.write_json(out / (phase + "-settlement.json"), data[phase])
        data["context"]["phases"][phase]["return"] = pin(out / (phase + "-settlement.json"))
    R.write_json(out / "run-context.json", data["context"])
    data["wrapper"]["runtime"] = copy.deepcopy(data["native"])
    data["wrapper"]["run_context"] = copy.deepcopy(data["context"])
    R.write_json(out / "phased.json", data["wrapper"])
    for role, name in {"before_settlement": "before-settlement.json", "after_settlement": "after-settlement.json",
                       "execution": "phased.json", "run_context": "run-context.json",
                       "worker": "runtime/worker-output.json"}.items():
        data["case"]["evidence"][role] = pin(out / name)


@pytest.fixture
def trial(runtime, native_files, monkeypatch):
    import test_rea_ax_state as X
    from test_rea_harness import objc_receipt
    from test_rea_runtime_bridge import _lifecycle_result_changes
    app, identity, _, state, out = runtime
    target, original_backend = R.fingerprint(app), X.Backend
    # Independently authored COMPLETE contracts exist before the fake worker.
    refs = out.parent / "independent-full-contracts"
    refs.mkdir()
    selected = {}
    for role, value in (("before", False), ("after", True)):
        backend = original_backend()
        backend.checkbox.values["AXValue"] = (0, value)
        observed = X.observe(backend)
        contract = {"format": S.CONTRACT, "target": copy.deepcopy(target), "selector": copy.deepcopy(X.SELECTOR),
                    "state": copy.deepcopy(observed["state"])}
        file = refs / (role + ".json")
        R.write_json(file, contract)
        selected[role] = {"contract": contract, "reference": pin(file)}
    policy = {"format": T.POLICY, "id": "fixed-inert-transition", "kind": "fixed_state_transition", "params": [],
        "input_scope": "fixed_no_actor_inputs", "heldout_scope": "fresh_trials_with_declared_restart",
        "reference_policy": "independently_frozen_complete_states", "phase_minimums": dict(T.MINIMUMS), **selected}
    request, clock, processes, _ = _phased_fixture(runtime, native_files, monkeypatch)
    operation = request["operations"][0]
    actor = {"kind": "native_function", **{key: copy.deepcopy(value) for key, value in operation.items() if key != "op"}}
    recipe = B.request_native_path_recipes(request)[0]
    # Reuse the real H function/section fixture shape; modern envelope/ledger
    # and full NS pins are then supplied explicitly, with no app execution.
    row = objc_receipt({"target": target}, actor, pid=identity["pid"])["result"]["operations"][0]
    row["value"] = 23
    state["result_changes"] = _lifecycle_result_changes(request, recipe, for_call=True)
    state["result_changes"].update(operations=[row], identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity),
        process={"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"}, queue="main")
    def cleanup_artifact():
        folder = state["calls"][-1]["cwd"]
        worker = R.load_json(folder / "worker-output.json")
        journal = {"format": B.CLEANUP_JOURNAL, "request_sha256": R.digest(request), "status": "completed", "attached": True,
            "frida_lifecycle": worker["frida_lifecycle"], "cleanup": worker["cleanup"], "cleanup_errors": [], "error": None,
            "native_state_uncertain": False, "native_calls": 1, "invocations": 1, "helper_load_attempts": 0, "completed_operations": 1}
        R.write_json(folder / "worker-cleanup.json", journal)
    state["after_worker"] = cleanup_artifact
    sleeping, marked = clock.sleep, []
    def sleep(seconds):
        sleeping(seconds)
        if clock.now >= 120 and not marked:
            with (out / "runtime/trace.jsonl").open("a") as stream:
                stream.write('{"event":"native-invoke-start"}\n{"event":"native-invoke-returned"}\n')
            marked.append(True)
    monkeypatch.setattr(B.time, "sleep", sleep)
    def hook(phase, context):
        if phase == "setup": return {"inert_ordinary_setup_return": True}
        def backend(*args, **kwargs):
            result = original_backend(*args, **kwargs)
            result.checkbox.values["AXValue"] = (0, phase == "after")
            return result
        with monkeypatch.context() as patch:
            patch.setattr(X, "Backend", backend)
            return _phase_settlement(context, clock, patch)
    wrapper = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=LIMITS)
    assert wrapper["phases_complete"] and len(processes) == 1
    context, native = wrapper["run_context"], wrapper["runtime"]
    case = {"format": T.CASE, "id": "inert-learning-0", "phase": "learning", "inputs": {},
        "deployment": {"id": "inert-process-0", "kind": "existing_process", "identity": copy.deepcopy(identity),
                       "restart_from": None, "restart_evidence": None}, "evidence": {}}
    data = {"policy": policy, "case": case, "actor": actor, "out": out, "wrapper": wrapper, "native": native,
        "context": context, "worker": R.load_json(out / "runtime/worker-output.json"),
        "config": R.load_json(out / "runtime/worker-request.json"), "journal": R.load_json(out / "runtime/worker-cleanup.json"),
        "before": R.load_json(out / "before-settlement.json"), "after": R.load_json(out / "after-settlement.json")}
    _commit(data)
    return data


def validate(data):
    return E.validate_trial(data["policy"], data["case"], data["actor"])


@pytest.fixture
def owned_trial(prepared_owned):
    """New O references/settlements and a real inert F/B chain, not converted S records."""
    result = run_prospective(prepared_owned)
    assert result["format"] == "anyuse.rea-fixed-state-trial-outcome/2" and result["trial_measurement_validated"]
    out = prepared_owned["out"] / "phases"
    wrapper = R.load_json(out / "phased.json")
    return {"policy": prepared_owned["policy"], "actor": prepared_owned["actor"], "out": out,
        "case": R.load_json(result["case"]["file"]), "wrapper": wrapper, "context": wrapper["run_context"],
        "native": wrapper["runtime"], "worker": R.load_json(out / "runtime/worker-output.json"),
        "config": R.load_json(out / "runtime/worker-request.json"),
        "journal": R.load_json(out / "runtime/worker-cleanup.json"),
        "before": R.load_json(out / "before-settlement.json"), "after": R.load_json(out / "after-settlement.json")}


@pytest.fixture
def ack_trial(prepared_ack):
    """New actual inert F/B/P/W chain; independent references predate its worker."""
    result = run_prospective(prepared_ack, require_pre_actuation_ack=True)
    assert result["format"] == "anyuse.rea-fixed-state-trial-outcome/3" and result["trial_measurement_validated"], {
        "actual_F_errors": json.dumps(result.get("errors"), ensure_ascii=False, sort_keys=True)[:4096],
        "actual_native_disposition": json.dumps(result.get("native_disposition"), ensure_ascii=False, sort_keys=True)[:4096]}
    out = prepared_ack["out"] / "phases"
    wrapper = R.load_json(out / "phased.json")
    parent = R.load_json(wrapper["pre_actuation_ack"]["file"])
    return {"policy": prepared_ack["policy"], "actor": prepared_ack["actor"], "out": out,
        "case": R.load_json(result["case"]["file"]), "wrapper": wrapper, "context": wrapper["run_context"],
        "native": wrapper["runtime"], "worker": R.load_json(out / "runtime/worker-output.json"),
        "config": R.load_json(out / "runtime/worker-request.json"),
        "journal": R.load_json(out / "runtime/worker-cleanup.json"),
        "before": R.load_json(out / "before-settlement.json"), "after": R.load_json(out / "after-settlement.json"),
        "parent": parent, "checkpoint": R.load_json(parent["decision"]["checkpoint"]["file"]),
        "permit_worker": R.load_json(out / "runtime/worker-permit.json")}


def _commit_ack(data):
    """Rehash actual new inert records coherently so corruption reaches semantics."""
    out = data["out"]
    for role in ("before", "after"):
        R.write_json(out / (role + "-settlement.json"), data[role])
        data["context"]["phases"][role]["return"] = pin(out / (role + "-settlement.json"))
    setup, before = (data["context"]["phases"][role]["return"] for role in ("setup", "before"))
    data["checkpoint"].update(setup=copy.deepcopy(setup), before=copy.deepcopy(before))
    R.write_json(out / "pre-actuation-checkpoint.json", data["checkpoint"])
    data["parent"]["decision"].update(setup=copy.deepcopy(setup), before=copy.deepcopy(before),
                                      checkpoint=pin(out / "pre-actuation-checkpoint.json"))
    data["permit_worker"]["decision"] = copy.deepcopy(data["parent"]["decision"])
    data["worker"]["pre_actuation_ack"]["decision_sha256"] = RP.digest(data["parent"]["decision"])
    R.write_json(out / "pre-actuation-parent.json", data["parent"])
    data["context"]["pre_actuation_ack"] = pin(out / "pre-actuation-parent.json")
    data["wrapper"]["pre_actuation_ack"] = copy.deepcopy(data["context"]["pre_actuation_ack"])
    R.write_json(out / "runtime/worker-permit.json", data["permit_worker"])
    _commit(data)


def test_one_full_trial_validates_historical_measurement_without_live_lookup_or_public_proof(trial, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("historical validation attempted live work")
    for name in ("process_identity", "collect", "invoke", "adapter_identity"):
        monkeypatch.setattr(B, name, forbidden)
    monkeypatch.setattr(S, "settle", forbidden)
    monkeypatch.setattr(S, "_Mac", forbidden)
    saved = copy.deepcopy((trial["policy"], trial["case"], trial["actor"]))
    result = validate(trial)
    assert result["trial_measurement_validated"] and result["native_calls"] == result["invocations"] == 1
    for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created", "aggregation_validated",
                "restart_deployment_proven", "input_generalization_claimed"):
        assert result[key] is False
    assert result["state_sha256"]["before"] != result["state_sha256"]["after"]
    assert (trial["policy"], trial["case"], trial["actor"]) == saved


def test_full_trial_accepts_exact_producer_phase_bound_but_refuses_its_next_float(trial):
    # Translate a genuine complete after-settlement, preserving both actual
    # state samples, their cadence and every native/identity/cleanup binding.
    # This begin crosses the binary exponent boundary at128 when adding8.
    row, settlement = trial["context"]["phases"]["after"], trial["after"]
    begin, limit = 121.3, trial["context"]["limits"]["after_seconds"]
    shift = begin - row["started_monotonic"]
    assert shift >= 0 and limit == 8
    row["started_monotonic"] = begin
    for key in ("returned_monotonic", "finished_monotonic"):
        row[key] += shift
    for key in ("started_monotonic", "finished_monotonic"):
        settlement[key] += shift
    for sample in settlement["samples"]:
        for key in ("started_monotonic", "finished_monotonic"):
            sample[key] += shift
    bound = begin + limit  # The producer constructs this exact absolute bound.
    row["deadline_monotonic"] = settlement["deadline_monotonic"] = bound
    assert bound - begin > limit  # Subtraction would reject this valid bound.
    assert begin >= trial["context"]["worker_anchor_monotonic"] + trial["native"]["worker"]["elapsed_s"] - .001
    _commit(trial)
    result = validate(trial)  # Whole historical E gate, not an isolated helper.
    assert result["trial_measurement_validated"] and result["native_calls"] == result["invocations"] == 1
    assert result["proof_created"] is False and result["task_proof_created"] is False

    # One representable value beyond the producer bound is still too late;
    # no epsilon or rounding tolerance can admit it.
    row["deadline_monotonic"] = math.nextafter(bound, math.inf)
    _commit(trial)
    with pytest.raises(ValueError, match="phase order/deadline or completion gate differs"):
        validate(trial)


@pytest.mark.parametrize("change", ["raw_worker", "actor", "identity", "abi", "cleanup_bool", "hook_cleanup", "counter", "resource_bool"])
def test_semantically_corrupted_but_rehashed_native_material_cannot_become_a_valid_trial(trial, change):
    mirror = True
    if change == "raw_worker":
        trial["worker"]["process"]["pid"] += 1
        mirror = False
    elif change == "actor": trial["actor"]["arguments"][1]["value"] += 1
    elif change == "identity": trial["worker"]["after_identity"]["launch_time"] += 1
    elif change == "abi": trial["worker"]["operations"][0]["function"]["abi"] = "win64"
    elif change == "cleanup_bool": trial["worker"]["cleanup"]["detached"] = 1
    elif change == "hook_cleanup": trial["worker"]["lifecycle_observer"]["hooks_detached"] = 0
    elif change == "counter": trial["worker"]["native_calls"] = 2
    else:
        pins = copy.deepcopy(trial["native"]["native_resources"])
        changed = False
        for row in pins.values():
            for selected in row.get("native_path_contracts", []):
                contract = selected["contract"]
                if contract.get("native_child_retained") is False:
                    contract["native_child_retained"], changed = 0, True
        assert changed
        for body in (trial["native"], trial["worker"], trial["context"], trial["config"]):
            body["native_resources"] = copy.deepcopy(pins)
    _commit(trial, mirror_worker=mirror)
    with pytest.raises(ValueError): validate(trial)


@pytest.mark.parametrize("change", ["incomplete", "incomplete_missing_observer", "incomplete_bad_observer",
                                   "attached_false", "handles_live", "legacy_worker"])
def test_outer_executed_receipt_cannot_waive_actual_worker_success_and_global_lifecycle_gate(trial, change):
    worker = trial["worker"]
    if change.startswith("incomplete"):
        worker["status"] = "incomplete"
        if change == "incomplete_missing_observer": worker.pop("lifecycle_observer")
        if change == "incomplete_bad_observer": worker["lifecycle_observer"]["hooks_detached"] = 0
    elif change == "attached_false": worker["attached"] = False
    elif change == "handles_live": worker["handles_expired"] = False
    else: worker["format"] = B.LEGACY_WORKER
    _commit(trial)
    assert trial["native"]["status"] == "executed" and trial["journal"]["status"] == "completed"
    assert trial["worker"]["operations"][0]["status"] == "ok"  # Preserve valid path/function row.
    with pytest.raises(ValueError, match="completed attached current worker"): validate(trial)


@pytest.mark.parametrize("change", ["earliest", "late_baseline", "wrong_full_value_type", "incomplete", "skipped_pair", "after_before_exit"])
def test_phase_or_first_pair_corruption_refuses_without_searching_for_another_state(trial, change):
    baseline = trial["before"]
    if change == "earliest":
        trial["context"]["earliest_terminal_monotonic"] += 1
        trial["context"]["pre_terminal_deadline_monotonic"] += 1
    elif change == "late_baseline":
        row = trial["context"]["phases"]["before"]
        for key in ("started_monotonic", "returned_monotonic", "finished_monotonic", "deadline_monotonic"): row[key] += 20
        for key in ("started_monotonic", "finished_monotonic", "deadline_monotonic"): baseline[key] += 20
        for sample in baseline["samples"]:
            sample["started_monotonic"] += 20
            sample["finished_monotonic"] += 20
    elif change == "after_before_exit":
        trial["native"]["worker"]["elapsed_s"] += 5
    elif change == "wrong_full_value_type":
        for sample in baseline["samples"]: sample["observation"]["state"]["nodes"][2]["value"] = 0
        assert S.validate_settlement(baseline)["complete"]  # Whole pair is stable, but WRONG.
    elif change == "incomplete":
        baseline["samples"][1]["observation"]["state"]["nodes"][2]["value"] = True
        baseline.update(complete=False, stable_pair=None, issues=[{"kind": "sample_limit"}])
        assert S.validate_settlement(baseline)["complete"] is False
    else:
        later = copy.deepcopy(baseline["samples"])
        for sample in baseline["samples"]: sample["observation"]["state"]["nodes"][2]["value"] = True
        for sample in later:
            sample["index"] += 2
            sample["started_monotonic"] += 1
            sample["finished_monotonic"] += 1
        baseline["samples"].extend(later)
        baseline["max_samples"], baseline["stable_pair"] = 4, [2, 3]
        baseline["finished_monotonic"] += 1
        for key in ("returned_monotonic", "finished_monotonic"): trial["context"]["phases"]["before"][key] += 1
    _commit(trial)
    with pytest.raises(ValueError): validate(trial)


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "role_overlap"])
def test_canonical_trial_reader_rejects_aliases_instead_of_counting_them_as_independent(trial, alias):
    path = Path(trial["case"]["evidence"]["run_context"]["file"])
    if alias == "symlink":
        other = path.with_name("saved-context.json")
        path.rename(other)
        path.symlink_to(other)
    elif alias == "hardlink": os.link(path, path.with_name("duplicate-context.json"))
    else: trial["case"]["evidence"]["run_context"] = copy.deepcopy(trial["policy"]["before"]["reference"])
    with pytest.raises(ValueError): validate(trial)


def test_duplicate_json_keys_and_original_resource_drift_are_not_waived_by_valid_case_labels(trial):
    file = Path(trial["case"]["evidence"]["execution"]["file"])
    raw = file.read_text()
    file.write_text('{"format":"ignored",' + raw.lstrip()[1:])
    trial["case"]["evidence"]["execution"] = pin(file)
    with pytest.raises(ValueError, match="duplicate"): validate(trial)


def test_json_numeric_overflow_is_rejected_instead_of_becoming_infinity():
    with pytest.raises(ValueError, match="nonfinite"): E._json(b'{"unused_numeric":1e309}')


def test_rehashed_full_trial_cannot_hide_overflow_in_unrelated_worker_metadata(trial):
    marker = "INERT_OVERFLOW_LITERAL_PLACEHOLDER"
    encoded_marker = json.dumps(marker).encode()
    trial["worker"]["unused_numeric"] = marker
    raw = json.dumps(trial["worker"], ensure_ascii=False, allow_nan=False).encode().replace(encoded_marker, b"1e309")
    _commit(trial, worker_raw=raw)
    # Keep raw worker, native embedded result, wrapper and every hash binding
    # coherent. This is valid JSON syntax with overflow, never a NaN token.
    native = trial["out"] / "runtime/runtime.json"
    native.write_bytes(native.read_bytes().replace(encoded_marker, b"1e309"))
    trial["context"]["native_receipt"]["sha256"] = pin(native)["sha256"]
    R.write_json(trial["out"] / "run-context.json", trial["context"])
    trial["wrapper"]["run_context"] = copy.deepcopy(trial["context"])
    file = trial["out"] / "phased.json"
    R.write_json(file, trial["wrapper"])
    file.write_bytes(file.read_bytes().replace(encoded_marker, b"1e309"))
    trial["case"]["evidence"]["execution"] = pin(file)
    trial["case"]["evidence"]["run_context"] = pin(trial["out"] / "run-context.json")
    assert b'"unused_numeric": 1e309' in file.read_bytes()
    with pytest.raises(ValueError, match="nonfinite"): validate(trial)


def test_current_original_resource_drift_refuses_without_looking_up_historical_process(trial):
    header = next(Path(name) for name in B.native_file_claims(trial["actor"]) if Path(name).suffix == ".h")
    header.write_bytes(header.read_bytes() + b"\nchanged inert original declaration")
    with pytest.raises(ValueError): validate(trial)


def test_final_recheck_catches_reference_mutation_after_initial_oracle_read(trial, monkeypatch):
    original, changed = B.request_resources, []
    file = Path(trial["policy"]["before"]["reference"]["file"])
    def resources(request):
        result = original(request)
        if not changed:
            file.write_bytes(file.read_bytes() + b"\n")
            changed.append(True)
        return result
    monkeypatch.setattr(B, "request_resources", resources)
    with pytest.raises(ValueError): validate(trial)
    assert changed == [True]


def test_semantically_equal_rehashed_worker_still_cannot_exceed_actual_producer_stdout_cap(trial):
    raw = json.dumps(trial["worker"], ensure_ascii=False, allow_nan=False).encode()
    assert len(raw) < B.MAX_RESULT_BYTES
    padded = raw + b" " * (B.MAX_RESULT_BYTES + 1 - len(raw))
    _commit(trial, worker_raw=padded)
    assert trial["case"]["evidence"]["worker"]["bytes"] == B.MAX_RESULT_BYTES + 1
    with pytest.raises(ValueError): validate(trial)


@pytest.mark.parametrize("oversized_logical", [False, True])
def test_setup_uses_exact_logical_json_cap_and_accepts_larger_valid_pretty_file(trial, oversized_logical):
    returned = {"payload": "x" * (64 * 1024)} if oversized_logical else {"items": [0] * 10000}
    file = trial["out"] / "setup-return.json"
    R.write_json(file, returned)
    trial["context"]["phases"]["setup"]["return"] = pin(file)
    _commit(trial)
    assert file.stat().st_size > 64 * 1024  # Pretty file bytes are NOT the logical cap.
    logical = len(json.dumps(returned, ensure_ascii=False, allow_nan=False).encode())
    assert (logical > 64 * 1024) is oversized_logical
    if oversized_logical:
        with pytest.raises(ValueError, match="logical JSON byte cap"): validate(trial)
    else: assert validate(trial)["trial_measurement_validated"]


def test_declared_restart_has_no_restart_or_campaign_credit_from_ids(trial):
    case = trial["case"]
    case["phase"] = "heldout"
    evidence = trial["out"] / "unvalidated-restart.json"
    R.write_json(evidence, {"label": "restart-is-not-proven"})
    case["deployment"].update(kind="restart", restart_from="some-earlier-case", restart_evidence=pin(evidence))
    result = validate(trial)
    assert result["trial_measurement_validated"] and result["restart_deployment_proven"] is False
    assert result["aggregation_validated"] is False and result["public_proof_created"] is False


def test_guest_guard_precedes_invalid_declarations_and_all_retained_io(monkeypatch):
    def deny(): raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", deny)
    with pytest.raises(PermissionError, match="VM only"): E.validate_trial({}, {}, {})


@pytest.fixture
def qt_trial(prepared_qt):
    """An actual inert F/B/S/T/E phase chain, not a relabelled native receipt.

    All native calls and operating-system backends remain inert fixtures. This
    exercises complete prospective file admission and historical gates only.
    """
    from test_rea_state_trial import run
    outcome = run(prepared_qt)
    assert outcome["status"] == "measured" and outcome["trial_measurement_validated"] is True
    out = prepared_qt["out"] / "phases"
    native = R.load_json(out / "runtime/runtime.json")
    return {"policy": prepared_qt["policy"], "case": R.load_json(outcome["case"]["file"]),
        "actor": prepared_qt["actor"], "out": out, "wrapper": R.load_json(out / "phased.json"),
        "native": native, "context": R.load_json(out / "run-context.json"),
        "worker": R.load_json(out / "runtime/worker-output.json"),
        "config": R.load_json(out / "runtime/worker-request.json"),
        "journal": R.load_json(out / "runtime/worker-cleanup.json"),
        "before": R.load_json(out / "before-settlement.json"), "after": R.load_json(out / "after-settlement.json"),
        "qt_files": prepared_qt["qt_files"]}


def test_qt_historical_trial_uses_shared_qt_gate_without_live_lookup_or_public_promotion(qt_trial, monkeypatch):
    checked, original = [], B.validate_qt_moc_result
    def shared(request, result):
        observed = original(request, result)
        checked.append(copy.deepcopy(observed))
        return observed
    monkeypatch.setattr(B, "validate_qt_moc_result", shared)
    def forbidden(*args, **kwargs): raise AssertionError("historical Qt trial attempted live work")
    for name in ("process_identity", "collect", "invoke", "adapter_identity"):
        monkeypatch.setattr(B, name, forbidden)
    monkeypatch.setattr(S, "settle", forbidden)
    monkeypatch.setattr(S, "_Mac", forbidden)
    result = validate(qt_trial)
    assert result["trial_measurement_validated"] is True and checked
    assert checked[-1] == qt_trial["worker"]["operations"][0]["qt_moc"]
    assert result["native_calls"] == result["invocations"] == 1
    assert result["state_sha256"]["before"] != result["state_sha256"]["after"]
    assert qt_trial["case"]["inputs"] == {} and qt_trial["policy"]["params"] == []
    assert "signature_evidence" not in qt_trial["actor"] and "return_type" not in qt_trial["actor"]
    for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created", "aggregation_validated",
                "restart_deployment_proven", "input_generalization_claimed"):
        assert result[key] is False


@pytest.mark.parametrize("fault", ["vector_live", "slot_nonnull", "method_bool", "permit", "nonvoid_value",
                                    "legacy", "error", "missing_plan", "missing_source"])
def test_rehashed_qt_native_evidence_cannot_replace_the_shared_callback_resource_and_cleanup_gate(qt_trial, fault):
    row = qt_trial["worker"]["operations"][0]
    if fault == "vector_live": row["qt_moc"]["argv"]["expired"] = False
    elif fault == "slot_nonnull": row["qt_moc"]["argv"]["slot0"] = "n1"
    elif fault == "method_bool": row["qt_moc"]["local_index"] = False
    elif fault == "permit": row["native_paths"][0]["dispatch_permit_consumed"] = False
    elif fault == "nonvoid_value": row["value"] = 0
    elif fault == "legacy": qt_trial["worker"]["format"] = B.LEGACY_WORKER
    elif fault == "error": qt_trial["worker"]["error"] = {"type": "Error", "message": "inert completed Qt failure"}
    else:
        pins, selection = copy.deepcopy(qt_trial["native"]["native_resources"]), qt_trial["actor"]["selection"]
        if fault == "missing_plan": pins[selection["catalog"]["file"]].pop("qt_moc_dispatches")
        else: pins.pop(str(qt_trial["qt_files"]["qt_sources"].parent / "qobjectdefs.h"))
        for body in (qt_trial["native"], qt_trial["worker"], qt_trial["context"], qt_trial["config"]):
            body["native_resources"] = copy.deepcopy(pins)
    _commit(qt_trial)
    with pytest.raises(ValueError): validate(qt_trial)


@pytest.mark.parametrize("role", ["before", "after"])
def test_valid_qt_callback_and_void_result_do_not_replace_complete_external_state_verification(qt_trial, role):
    request = B.invocation_request(qt_trial["actor"])
    B.validate_qt_moc_result(request, qt_trial["worker"])
    settlement = qt_trial[role]
    # Stable and well-formed, but bool and int are different full state values.
    for sample in settlement["samples"]: sample["observation"]["state"]["nodes"][2]["value"] = 0
    assert S.validate_settlement(settlement)["complete"] is True
    _commit(qt_trial)
    with pytest.raises(ValueError, match="FIRST complete stable whole state"):
        validate(qt_trial)


def test_qt_original_static_file_drift_is_not_waived_by_a_valid_historical_void_row(qt_trial):
    file = qt_trial["qt_files"]["code"]
    file.write_bytes(file.read_bytes() + b"\nchanged original code observation")
    with pytest.raises(ValueError): validate(qt_trial)


def test_owned_full_trial_independently_reopens_its_real_new_observer_evidence_without_live_lookup(owned_trial, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("historical owned validation attempted live work or strict observer")
    observer = SR.for_policy(owned_trial["policy"])
    for name in ("process_identity", "collect", "collect_phased", "invoke", "adapter_identity"):
        monkeypatch.setattr(B, name, forbidden)
    monkeypatch.setattr(observer, "settle", forbidden)
    monkeypatch.setattr(observer, "_Mac", forbidden)
    monkeypatch.setattr(S, "validate_settlement", forbidden)
    result = validate(owned_trial)
    assert result["format"] == E.REPORT2 and result["trial_measurement_validated"]
    assert result["observer_capability"] == SR.ownedcap()
    assert result["state_observation_scope"] == "selected_owned_visible_ax_window_tree"
    assert result["native_calls"] == result["invocations"] == 1 and len(result["files"]) > 5
    assert result["state_sha256"]["before"] != result["state_sha256"]["after"]
    assert not any(result[key] for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created",
                                          "aggregation_validated", "restart_deployment_proven", "input_generalization_claimed"))


@pytest.mark.parametrize("fault", ["wrapper_cap", "context_cap", "missing_cap", "legacy_wrapper", "legacy_context",
                                   "legacy_settlement", "extra_routing", "wrong_after", "cleanup", "reused_reference"])
def test_owned_mode_refuses_rehashed_capability_protocol_or_complete_state_substitution(owned_trial, fault):
    if fault == "wrapper_cap": owned_trial["wrapper"]["observer_capability"] = SR.strictcap()
    elif fault == "context_cap": owned_trial["context"]["observer_capability"] = SR.strictcap()
    elif fault == "missing_cap": owned_trial["wrapper"].pop("observer_capability")
    elif fault == "legacy_wrapper":
        owned_trial["wrapper"]["format"] = B.PHASED
        owned_trial["wrapper"].pop("observer_capability")
    elif fault == "legacy_context":
        owned_trial["context"]["format"] = B.PHASE_CONTEXT
        owned_trial["context"].pop("observer_capability")
    elif fault == "legacy_settlement": owned_trial["before"]["format"] = S.SETTLEMENT
    elif fault == "extra_routing": owned_trial["wrapper"]["observer_module"] = "caller-selected-source"
    elif fault == "wrong_after":
        for sample in owned_trial["after"]["samples"]:
            node = next(row for row in sample["observation"]["state"]["nodes"] if row["role"] == "AXCheckBox")
            node["value"] = False
    elif fault == "cleanup": owned_trial["worker"]["cleanup"]["detached"] = False
    else: owned_trial["policy"]["after"] = copy.deepcopy(owned_trial["policy"]["before"])
    _commit(owned_trial)
    with pytest.raises(ValueError): validate(owned_trial)


def test_owned_phase_protocol_cannot_be_relabelled_as_a_legacy_focused_trial(owned_trial):
    owned_trial["policy"]["format"] = T.POLICY
    owned_trial["policy"].pop("observer")
    # No source records or focus facts are stripped or synthesized to migrate it.
    with pytest.raises(ValueError): validate(owned_trial)


@pytest.mark.parametrize("location", ["worker", "wrapper", "context", "config"])
def test_legacy_native_evidence_cannot_adopt_unrequested_ack_data_by_rehashing(trial, location):
    target = {"worker": trial["worker"], "wrapper": trial["wrapper"],
              "context": trial["context"], "config": trial["config"]}[location]
    target["pre_actuation_ack"] = {"state": "consumed", "permit_consumed": True}
    _commit(trial)
    with pytest.raises(ValueError): validate(trial)


def _permit_only(data):
    return RP.validate_completed(data["parent"], data["permit_worker"], data["worker"]["pre_actuation_ack"],
        data["checkpoint"], binding=data["config"]["pre_actuation_ack"]["binding"],
        setup_pin=data["context"]["phases"]["setup"]["return"],
        before_pin=data["context"]["phases"]["before"]["return"],
        checkpoint_pin=data["parent"]["decision"]["checkpoint"],
        native_calls=data["worker"]["native_calls"], invocations=data["worker"]["invocations"])


def test_new_ack_historical_gate_reopens_actual_independent_journals_without_live_work(ack_trial, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("historical ACK validation attempted live work")
    for name in ("process_identity", "collect", "collect_phased", "invoke", "adapter_identity"):
        monkeypatch.setattr(B, name, forbidden)
    observer = SR.for_policy(ack_trial["policy"])
    monkeypatch.setattr(observer, "settle", forbidden)
    monkeypatch.setattr(observer, "_Mac", forbidden)
    result = validate(ack_trial)
    assert result["format"] == E.REPORT3 and result["pre_actuation_ack_required"] is True
    assert result["pre_actuation_ack_validated"] is True and result["pre_actuation_ack"] == _permit_only(ack_trial)
    assert result["native_calls"] == result["invocations"] == 1
    paths = (ack_trial["out"] / "pre-actuation-parent.json", ack_trial["out"] / "pre-actuation-checkpoint.json",
             ack_trial["out"] / "runtime/worker-permit.json")
    for path in paths: assert result["files"][str(path)] == pin(path)
    assert not any(result[key] for key in ("proof_created", "task_proof_created", "public_proof_created",
        "card_created", "aggregation_validated", "restart_deployment_proven", "input_generalization_claimed"))


@pytest.mark.parametrize("fault", ["different_generation", "different_thread", "phase_finish", "permit_deadline"])
def test_internally_consistent_rehashed_permit_is_not_authority_for_another_native_generation_or_phase(ack_trial, fault):
    if fault in {"different_generation", "different_thread"}:
        selected = copy.deepcopy(ack_trial["parent"]["selection"])
        selected["generation" if fault == "different_generation" else "owner_thread_id"] += 1
        for row in (ack_trial["parent"], ack_trial["permit_worker"], ack_trial["worker"]["pre_actuation_ack"],
                    ack_trial["checkpoint"]):
            row["selection"] = copy.deepcopy(selected)
        ack_trial["parent"]["decision"]["selection"] = copy.deepcopy(selected)
    elif fault == "phase_finish": ack_trial["checkpoint"]["before_finished_monotonic"] -= .001
    else: ack_trial["checkpoint"]["deadline_monotonic"] += 1
    _commit_ack(ack_trial)
    # P/W/J consistency alone really admits this inert forgery. E must bind
    # it to separately retained phase timing and actual native lifecycle proof.
    assert _permit_only(ack_trial)["agent_consumed"] is True
    with pytest.raises(ValueError): validate(ack_trial)


@pytest.mark.parametrize("fault", ["parent_deny", "worker_thread_live", "agent_unconsumed", "agent_not_closed",
                                  "wrong_selector", "control_stdio", "checkpoint_current", "worker_count",
                                  "missing_worker_artifact", "legacy_wrapper", "missing_context_cap"])
def test_rehashed_ack_failures_cannot_replace_full_native_cleanup_or_frozen_input_admission(ack_trial, fault):
    if fault == "parent_deny":
        ack_trial["parent"]["decision"].update(decision="deny", reason="inert explicitly denied parent")
    elif fault == "worker_thread_live": ack_trial["permit_worker"]["thread_stopped"] = False
    elif fault == "agent_unconsumed": ack_trial["worker"]["pre_actuation_ack"]["permit_consumed"] = False
    elif fault == "agent_not_closed": ack_trial["worker"]["pre_actuation_ack"]["closed"] = False
    elif fault == "wrong_selector": ack_trial["config"]["pre_actuation_ack"]["observer_selector"]["title"] = "different"
    elif fault == "control_stdio": ack_trial["config"]["pre_actuation_ack"]["control_fd"] = 1
    elif fault == "checkpoint_current": ack_trial["checkpoint"]["current"]["identity"]["launch_time"] += 1
    elif fault == "worker_count": ack_trial["worker"]["native_calls"] = 0
    elif fault == "legacy_wrapper":
        ack_trial["wrapper"]["format"] = B.PHASED2 if SR.capability(ack_trial["policy"]) == SR.ownedcap() else B.PHASED
        ack_trial["wrapper"].pop("pre_actuation_ack")
    elif fault == "missing_context_cap": ack_trial["context"].pop("observer_capability")
    _commit_ack(ack_trial)
    if fault == "missing_worker_artifact":
        ack_trial["native"]["artifacts"] = [row for row in ack_trial["native"]["artifacts"] if row["file"] != "worker-permit.json"]
        # Repin the new native and wrapper without restoring the intentionally
        # incomplete actual artifact inventory.
        R.write_json(ack_trial["out"] / "runtime/runtime.json", ack_trial["native"])
        ack_trial["context"]["native_receipt"]["sha256"] = pin(ack_trial["out"] / "runtime/runtime.json")["sha256"]
        R.write_json(ack_trial["out"] / "run-context.json", ack_trial["context"])
        ack_trial["wrapper"].update(runtime=copy.deepcopy(ack_trial["native"]), run_context=copy.deepcopy(ack_trial["context"]))
        R.write_json(ack_trial["out"] / "phased.json", ack_trial["wrapper"])
        ack_trial["case"]["evidence"]["execution"] = pin(ack_trial["out"] / "phased.json")
        ack_trial["case"]["evidence"]["run_context"] = pin(ack_trial["out"] / "run-context.json")
    with pytest.raises(ValueError): validate(ack_trial)


@pytest.mark.parametrize("role", ["before", "after"])
def test_real_consumed_ack_does_not_replace_independent_whole_state_contracts(ack_trial, role):
    for sample in ack_trial[role]["samples"]:
        sample["observation"]["state"]["nodes"][2]["value"] = not (role == "after")
    _commit_ack(ack_trial)
    assert _permit_only(ack_trial)["agent_consumed"] is True
    with pytest.raises(ValueError, match="FIRST complete stable whole state"): validate(ack_trial)


@pytest.mark.parametrize("which", ["parent", "checkpoint", "worker"])
def test_ack_reader_rejects_deleted_or_changed_pinned_journals(ack_trial, which):
    path = ack_trial["out"] / {"parent": "pre-actuation-parent.json", "checkpoint": "pre-actuation-checkpoint.json",
                               "worker": "runtime/worker-permit.json"}[which]
    if which == "checkpoint": path.unlink()
    else: path.write_bytes(path.read_bytes() + b"\nchanged pinned evidence")
    with pytest.raises((ValueError, OSError)): validate(ack_trial)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
