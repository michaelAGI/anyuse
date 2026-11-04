"""Inert single-trial integration tests; run only in the VM guarded suite.

The ordinary action, app, Mach-O/ABI files, AX backend and Popen are all inert.
Actual B/S/T/E validators run; these fixtures never prove an app capability.
"""
import copy
from pathlib import Path

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_ax_state as S
from anyuse.execution import rea_state_task as T
from anyuse.execution import rea_state_evidence as E
from anyuse.execution import rea_state_trial as F
from anyuse.execution import rea_state_reader as SR
from anyuse.execution import rea_runtime_permit as RP
from test_rea_runtime_bridge import runtime, native_files, _phased_fixture, _lifecycle_result_changes

LIMITS = {"setup_seconds": 2, "before_seconds": 8, "after_seconds": 8, "margin_seconds": 1}


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size}


def _prepare_trial_data(runtime, native_files, monkeypatch, *, owned=False, interlocked=False):
    import test_rea_ax_state as X
    from test_rea_harness import objc_receipt
    if owned:
        import test_rea_owned_ax_state as OX
        from test_rea_state_task import owned_backend
    observer = SR.for_capability(SR.ownedcap() if owned else SR.strictcap())
    app, identity, _, state, original_out = runtime
    out = original_out.parent / "fixed-trial"
    target = R.fingerprint(app)
    # Independent whole references exist BEFORE any fake worker. They are not
    # converted from legacy research snapshots or selected after seeing output.
    refs = original_out.parent / "independent-reference-contracts"
    refs.mkdir()
    selected = {}
    for role, value in (("before", False), ("after", True)):
        if owned:
            observed = OX.observe(owned_backend(identity=identity, value=value), target=target, identity=identity)
            assert observed["complete"]
            contract = OX.contract(observed)
        else:
            backend = X.Backend()
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
    if owned: policy.update(format=T.POLICY2, observer={"capability": SR.ownedcap()})
    if interlocked:
        from test_rea_runtime_bridge import _acknowledged_fixture
        request, clock, processes, signals = _acknowledged_fixture(runtime, native_files, monkeypatch)
    else:
        request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    operation = request["operations"][0]
    actor = {"kind": "native_function", **{key: copy.deepcopy(value) for key, value in operation.items() if key != "op"}}
    recipe = B.request_native_path_recipes(request)[0]
    row = objc_receipt({"target": target}, actor, pid=identity["pid"])["result"]["operations"][0]
    row["value"] = 23
    state["result_changes"] = _lifecycle_result_changes(request, recipe, for_call=True)
    state["result_changes"].update(operations=[row], identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity),
        process={"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"}, queue="main")
    def cleanup_artifact():
        folder = state["calls"][-1]["cwd"]
        worker = R.load_json(folder / "worker-output.json")
        R.write_json(folder / "worker-cleanup.json", {"format": B.CLEANUP_JOURNAL,
            "request_sha256": R.digest(request), "status": worker["status"], "attached": worker["attached"],
            "frida_lifecycle": worker["frida_lifecycle"], "cleanup": worker["cleanup"], "cleanup_errors": [], "error": None,
            "native_state_uncertain": worker["native_state_uncertain"], "native_calls": worker["native_calls"],
            "invocations": worker["invocations"], "helper_load_attempts": worker["helper_load_attempts"],
            "completed_operations": len(worker["operations"])})
    state["after_worker"] = cleanup_artifact
    sleeping, marked = B.time.sleep if interlocked else clock.sleep, []
    def sleep(seconds):
        sleeping(seconds)
        if not interlocked and clock.now >= 120 and not marked:
            with (out / "phases/runtime/trace.jsonl").open("a") as stream:
                stream.write('{"event":"native-invoke-start"}\n{"event":"native-invoke-returned"}\n')
            marked.append(True)
    monkeypatch.setattr(B.time, "sleep", sleep)
    setup_source = original_out.parent / "trusted-setup-producer.txt"
    setup_source.write_text("Inert trusted ordinary-setup producer, independently frozen before the worker.\n")
    intent = {"format": F.INTENT, "id": "learning-0", "phase": "learning", "actor_sha256": R.digest(actor),
        "policy_sha256": T.digest(policy), "setup": {"id": "ordinary-setup-once", "producer": pin(setup_source)},
        "deployment": {"id": "existing-inert-deployment", "kind": "existing_process"}}
    controls = {"before": "correct", "setup_error": False}
    backends, setup_calls, collect_calls, observer_calls = [], [], [], []
    def backend_factory():
        after = clock.now >= 120
        backend = owned_backend(identity=identity, value=after) if owned else X.Backend(pid=identity["pid"], launched=identity["launch_time"])
        backend.identity = copy.deepcopy(identity)
        backend.current["identity"] = copy.deepcopy(identity)
        backend.current["kernel"].update(uid=identity["uid"], executable=identity["executable"])
        backend.current["session"]["uid"] = identity["uid"]
        backend.checkbox.values["AXValue"] = (0, after)
        if not after and controls["before"] == "incomplete": backend.checkbox.values["AXValue"] = (-25204, None)
        if not after and controls["before"] == "wrong_then_correct":
            backend.checkbox.values["AXValue"] = (0, True)
            context = backend.context
            def later(expected, deadline, **kwargs):
                # A third observation WOULD match the expected before state.
                # The real sampler must stop at the first wrong stable pair.
                if backend.context_count >= (7 if owned else 4): backend.checkbox.values["AXValue"] = (0, False)
                return context(expected, deadline, **kwargs)
            backend.context = later
        backends.append(backend)
        return backend
    monkeypatch.setattr(observer, "_Mac", backend_factory)
    def setup(context):
        setup_calls.append(copy.deepcopy(context))
        R.write_json(out / "ordinary-setup-result.json", {"issued_once": True, "api_result": not controls["setup_error"]})
        if controls["setup_error"]: raise ValueError("ordinary setup API result was uncertain after issuance")
        result = {"ordinary_setup_api_return": True}  # No effect/proof claim.
        return {"format": RP.SETUP, "status": "returned", "result": result} if interlocked else result
    collect = B.collect_phased
    def counted(*args, **kwargs):
        assert not (out / "case.json").exists() and not (out / "measurement.json").exists()
        assert R.load_json(out / "attempt.json")["status"] == "running"
        assert kwargs["backend"] == "local"
        collect_calls.append(copy.deepcopy(kwargs["expected_identity"]))
        observer_calls.append(kwargs.get("observer_capability"))
        return collect(*args, **kwargs)
    monkeypatch.setattr(B, "collect_phased", counted)
    return {"app": app, "identity": copy.deepcopy(identity), "state": state, "out": out, "policy": policy,
        "actor": actor, "intent": intent, "setup": setup, "controls": controls, "backends": backends,
        "setup_calls": setup_calls, "collect_calls": collect_calls, "processes": processes, "signals": signals,
        "limits": copy.deepcopy(LIMITS), "clock": clock, "observer": observer, "observer_calls": observer_calls,
        "interlocked": interlocked}


@pytest.fixture
def prepared(runtime, native_files, monkeypatch):
    return _prepare_trial_data(runtime, native_files, monkeypatch)


@pytest.fixture
def prepared_owned(tmp_path, monkeypatch):
    """Explicit named factories avoid unrelated imported runtime fixtures."""
    import test_rea_runtime_bridge as fixtures
    bridge_runtime = fixtures.runtime.__wrapped__(tmp_path, monkeypatch)
    originals = fixtures.native_files.__wrapped__(bridge_runtime)
    return _prepare_trial_data(bridge_runtime, originals, monkeypatch, owned=True)


@pytest.fixture(params=[False, True], ids=["strict", "owned"])
def prepared_ack(request, tmp_path, monkeypatch):
    """Prospective references plus actual new private P/W/controller path.

    The target/native functions are inert. No completed old phase receipt is
    converted, and no hand-added permit flag supplies the parent's decision.
    """
    import test_rea_runtime_bridge as fixtures
    bridge_runtime = fixtures.runtime.__wrapped__(tmp_path, monkeypatch)
    originals = fixtures.native_files.__wrapped__(bridge_runtime)
    return _prepare_trial_data(bridge_runtime, originals, monkeypatch, owned=request.param, interlocked=True)


def run(data, *, require_pre_actuation_ack=False):
    return F.run_trial(data["app"], data["actor"], data["policy"], data["intent"], data["out"],
        expected_identity=data["identity"], setup_hook=data["setup"], limits=data["limits"],
        require_pre_actuation_ack=require_pre_actuation_ack)


@pytest.fixture
def prepared_qt(tmp_path, monkeypatch):
    """New prospective Qt files and worker, sharing only inert phase plumbing.

    No previous native receipt is converted. The complete independent external
    references already exist; no worker has started when this fixture returns.
    SDK substitutions are inert test materials, never vendor evidence.
    """
    import test_rea_runtime_bridge as fixtures
    from test_rea_qt_moc import _qt_moc_files, _qt_moc_result
    from test_rea_runtime_bridge import _refreeze_native_path_contracts
    # Call the named inert factories explicitly, so importing this fixture
    # into another test module cannot resolve an unrelated local `runtime`.
    bridge_runtime = fixtures.runtime.__wrapped__(tmp_path, monkeypatch)
    native_files = fixtures.native_files.__wrapped__(bridge_runtime)
    prepared = _prepare_trial_data(bridge_runtime, native_files, monkeypatch)
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    recipe["root"]["lifecycle"]["observe_ms"] = 20000
    _refreeze_native_path_contracts(recipe, files)
    request = B.invocation_request(actor)
    resources = B.request_resources(request)
    worker = _qt_moc_result(actor, resources)
    identity, state = prepared["identity"], prepared["state"]
    worker.update(identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity),
        process={"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"})
    state["result_changes"] = worker
    def cleanup_artifact():
        folder = state["calls"][-1]["cwd"]
        actual = R.load_json(folder / "worker-output.json")
        R.write_json(folder / "worker-cleanup.json", {"format": B.CLEANUP_JOURNAL,
            "request_sha256": R.digest(request), "status": "completed", "attached": True,
            "frida_lifecycle": actual["frida_lifecycle"], "cleanup": actual["cleanup"], "cleanup_errors": [], "error": None,
            "native_state_uncertain": actual["native_state_uncertain"], "native_calls": 1, "invocations": 1,
            "helper_load_attempts": 0, "completed_operations": 1})
    state["after_worker"] = cleanup_artifact
    prepared.update(actor=actor, qt_files=files)
    prepared["intent"]["actor_sha256"] = R.digest(actor)
    assert not state["calls"] and not prepared["processes"] and not prepared["out"].exists()
    return prepared


def assert_native_once(data, result, *, runtime_status="executed"):
    assert len(data["collect_calls"]) == len(data["processes"]) == len(data["state"]["calls"]) == 1
    assert result["native_disposition"]["native_calls"] == result["native_disposition"]["invocations"] == 1
    assert result["native_disposition"]["runtime_status"] == runtime_status
    assert result["native_disposition"]["owned_worker"]["reaped"] is True


@pytest.mark.parametrize("phase", ["learning", "fresh", "heldout"])
def test_one_real_inert_phase_chain_binds_only_actual_outputs_and_remains_measurement_only(prepared, monkeypatch, phase):
    prepared["intent"]["phase"] = phase
    result = run(prepared)
    assert result["status"] == "measured" and result["trial_measurement_validated"] is True
    assert_native_once(prepared, result)
    assert len(prepared["setup_calls"]) == 1 and len(prepared["backends"]) == 2 and not prepared["signals"]
    case = R.load_json(result["case"]["file"])
    assert case["inputs"] == {} and case["deployment"]["identity"] == prepared["identity"]
    assert set(case["evidence"]) == set(T.REQUIRED_EVIDENCE)
    for role, relative in F.EVIDENCE_FILES.items(): assert case["evidence"][role] == pin(prepared["out"] / "phases" / relative)
    for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created", "aggregation_validated",
                "restart_deployment_proven", "input_generalization_claimed"):
        assert result[key] is False
    # A historical check does not require this process/backend to remain alive.
    def forbidden(*args, **kwargs): raise AssertionError("historical validation attempted live work")
    monkeypatch.setattr(B, "process_identity", forbidden)
    assert E.validate_trial(prepared["policy"], case, prepared["actor"])["trial_measurement_validated"]


@pytest.mark.parametrize("fault", ["setup", "incomplete_before", "wrong_stable_before", "cleanup_uncertain"])
def test_failed_external_or_cleanup_phase_retains_actual_call_and_never_retries(prepared, fault):
    if fault == "setup": prepared["controls"]["setup_error"] = True
    elif fault == "incomplete_before": prepared["controls"]["before"] = "incomplete"
    elif fault == "wrong_stable_before": prepared["controls"]["before"] = "wrong_then_correct"
    else: prepared["state"]["result_changes"]["native_state_uncertain"] = True
    result = run(prepared)
    assert result["trial_measurement_validated"] is False and result["errors"]
    assert_native_once(prepared, result, runtime_status="incomplete" if fault == "cleanup_uncertain" else "executed")
    assert len(prepared["setup_calls"]) == 1
    if fault == "setup":
        assert R.load_json(prepared["out"] / "ordinary-setup-result.json")["api_result"] is False
        assert not prepared["backends"]
    elif fault == "incomplete_before":
        baseline = R.load_json(prepared["out"] / "phases/before-settlement.json")
        assert S.validate_settlement(baseline)["complete"] is False and len(baseline["samples"]) == 16
        assert all(row["observation"] is not None for row in baseline["samples"])
    elif fault == "wrong_stable_before":
        baseline = R.load_json(prepared["out"] / "phases/before-settlement.json")
        assert baseline["stable_pair"] == [0, 1] and len(baseline["samples"]) == 2
        assert prepared["backends"][0].context_count == 4
        assert baseline["samples"][-1]["observation"]["state"]["nodes"][2]["value"] is True
        assert len(prepared["backends"]) == 2  # One before phase and one after, not a favorable retry.
    else:
        assert result["status"] == "resource_review_required"
        assert result["native_disposition"]["resource_review_required"] is True
        assert result["native_disposition"]["worker_status"] == "completed"
        assert result["native_disposition"]["raw_worker"] == pin(prepared["out"] / "phases/runtime/worker-output.json")
        assert result["native_disposition"]["raw_worker_provenance"] == "request_bound_raw_report_not_validated_execution"


@pytest.mark.parametrize("fault", ["policy_target", "intent_actor", "producer", "reference", "resource", "current_identity", "restart"])
def test_changed_or_unsupported_inputs_refuse_before_collection_or_setup(prepared, monkeypatch, fault):
    if fault == "policy_target":
        for role in ("before", "after"): prepared["policy"][role]["contract"]["target"]["sha256"] = "0" * 64
        prepared["intent"]["policy_sha256"] = T.digest(prepared["policy"])
    elif fault == "intent_actor": prepared["intent"]["actor_sha256"] = "0" * 64
    elif fault == "producer": Path(prepared["intent"]["setup"]["producer"]["file"]).write_text("changed producer")
    elif fault == "reference": Path(prepared["policy"]["before"]["reference"]["file"]).write_text("{}")
    elif fault == "resource":
        header = next(Path(name) for name in B.native_file_claims(prepared["actor"]) if name.endswith(".h"))
        header.write_bytes(header.read_bytes() + b"\nchanged original source")
    elif fault == "current_identity":
        current = copy.deepcopy(prepared["identity"])
        current["launch_time"] += 1
        monkeypatch.setattr(B, "process_identity", lambda *args, **kwargs: copy.deepcopy(current))
    else: prepared["intent"]["deployment"]["kind"] = "restart"
    with pytest.raises(ValueError): run(prepared)
    assert not prepared["out"].exists() and not prepared["collect_calls"] and not prepared["processes"]
    assert not prepared["setup_calls"] and not prepared["backends"]


@pytest.mark.parametrize("role", ["settlement", "worker", "case", "input_journal"])
def test_evidence_changed_after_real_E_validation_cannot_finalize_success(prepared, monkeypatch, role):
    original = E.validate_trial
    def validate(*args, **kwargs):
        result = original(*args, **kwargs)
        file = {"settlement": "phases/after-settlement.json", "worker": "phases/runtime/worker-output.json",
                "case": "case.json", "input_journal": "inputs.json"}[role]
        path = prepared["out"] / file
        path.write_bytes(path.read_bytes() + b"\n")  # Semantic JSON equality cannot waive exact frozen bytes.
        return result
    monkeypatch.setattr(E, "validate_trial", validate)
    result = run(prepared)
    assert result["trial_measurement_validated"] is False and result["errors"]
    assert_native_once(prepared, result)
    assert result["status"] == "resource_review_required"


@pytest.mark.parametrize("fault", ["missing", "hardlink"])
def test_missing_or_aliased_emitted_files_cannot_be_assembled_as_a_measured_case(prepared, monkeypatch, fault):
    import os
    original = B.collect_phased
    def collect(*args, **kwargs):
        result = original(*args, **kwargs)
        path = prepared["out"] / "phases/after-settlement.json"
        path.unlink()
        if fault == "hardlink": os.link(prepared["out"] / "phases/before-settlement.json", path)
        return result
    monkeypatch.setattr(B, "collect_phased", collect)
    result = run(prepared)
    assert result["trial_measurement_validated"] is False and result["measurement"] is None and result["errors"]
    assert_native_once(prepared, result)


def test_inputs_are_frozen_before_backend_and_setup_caller_mutation(prepared, monkeypatch):
    saved = copy.deepcopy({key: prepared[key] for key in ("actor", "policy", "intent", "identity", "limits")})
    adapter, changed = B.adapter_identity, []
    def mutate(*args, **kwargs):
        if not changed:
            prepared["actor"]["arguments"][1]["value"] = 999
            prepared["policy"]["after"]["contract"]["state"]["nodes"][2]["value"] = False
            prepared["intent"]["id"] = "caller-mutation"
            prepared["identity"]["launch_time"] += 10
            prepared["limits"]["margin_seconds"] = 5
            changed.append(True)
        return adapter(*args, **kwargs)
    monkeypatch.setattr(B, "adapter_identity", mutate)
    result = run(prepared)
    assert result["trial_measurement_validated"] and changed == [True]
    inputs = R.load_json(prepared["out"] / "inputs.json")
    for key in saved:
        assert inputs["expected_identity" if key == "identity" else key] == saved[key]
    assert prepared["collect_calls"] == [saved["identity"]]


def test_same_output_refuses_a_second_attempt_without_a_global_case_registry_claim(prepared):
    result = run(prepared)
    assert result["trial_measurement_validated"]
    with pytest.raises(FileExistsError): run(prepared)
    assert len(prepared["collect_calls"]) == len(prepared["processes"]) == 1


def test_exception_before_any_native_receipt_keeps_unknown_counts_and_no_retry(prepared, monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("collection transport failed; no retained native receipt")
    monkeypatch.setattr(B, "collect_phased", fail)
    result = run(prepared)
    assert calls == [True] and result["status"] == "resource_review_required"
    assert result["native_disposition"]["native_calls"] is None and result["native_disposition"]["invocations"] is None
    assert result["native_disposition"]["receipt"] is None and result["native_disposition"]["read_error"]
    assert not prepared["processes"] and not prepared["setup_calls"]


def test_setup_interrupt_retains_real_owned_worker_reap_and_unknown_native_outcome(prepared):
    def interrupt(context):
        R.write_json(prepared["out"] / "ordinary-setup-result.json", {"issued_once": True, "api_result": "unknown"})
        raise KeyboardInterrupt("trusted setup interrupted after issuance")
    prepared["setup"] = interrupt
    result = run(prepared)
    assert result["status"] == "resource_review_required" and result["trial_measurement_validated"] is False
    assert len(prepared["collect_calls"]) == len(prepared["processes"]) == 1
    assert len(prepared["signals"]) == 1 and prepared["signals"][0][1] == B.signal.SIGTERM
    native = result["native_disposition"]
    assert native["native_calls"] is None and native["invocations"] is None
    assert native["owned_worker"]["reaped"] is True and native["owned_worker"]["term_attempted"] is True
    assert native["owned_worker"]["returncode"] == -B.signal.SIGTERM
    context = R.load_json(native["phase_context"]["file"])
    assert native["phase_errors"] == context["errors"]
    assert any(row["phase"] == "worker_wait" and row["error"]["type"] == "KeyboardInterrupt" for row in context["errors"])
    assert R.load_json(prepared["out"] / "ordinary-setup-result.json")["issued_once"] is True


def test_direct_collection_interrupt_is_retained_then_reraised_without_retry(prepared, monkeypatch):
    calls = []
    def interrupt(*args, **kwargs):
        calls.append(True)
        raise KeyboardInterrupt("collection interrupted with unknown native outcome")
    monkeypatch.setattr(B, "collect_phased", interrupt)
    with pytest.raises(KeyboardInterrupt, match="collection interrupted"): run(prepared)
    result = R.load_json(prepared["out"] / "outcome.json")
    assert calls == [True] and result["status"] == "resource_review_required"
    assert result["errors"][0]["type"] == "KeyboardInterrupt"
    assert result["native_disposition"]["native_calls"] is None and result["native_disposition"]["invocations"] is None
    assert not prepared["processes"] and not prepared["setup_calls"]


def test_missing_native_receipt_does_not_erase_actual_independent_owned_worker_context(prepared, monkeypatch):
    original = B.collect_phased
    def collect(*args, **kwargs):
        result = original(*args, **kwargs)
        (prepared["out"] / "phases/runtime/runtime.json").unlink()
        return result
    monkeypatch.setattr(B, "collect_phased", collect)
    result = run(prepared)
    native = result["native_disposition"]
    assert result["trial_measurement_validated"] is False and result["status"] == "resource_review_required"
    assert len(prepared["collect_calls"]) == len(prepared["processes"]) == len(prepared["state"]["calls"]) == 1
    assert native["receipt"] is None and native["runtime_status"] is None
    assert native["native_calls"] == native["invocations"] == 1
    assert native["raw_worker"] == pin(prepared["out"] / "phases/runtime/worker-output.json")
    assert native["raw_worker_provenance"] == "request_bound_raw_report_not_validated_execution"
    assert native["read_error"] and native["owned_worker"]["reaped"] is True
    context = R.load_json(native["phase_context"]["file"])
    assert native["owned_worker"] == context["owned_worker"] and context["owned_worker"]["returncode"] == 0


def test_guest_guard_precedes_invalid_inputs_callback_and_all_io(monkeypatch):
    def deny(): raise PermissionError("VM only")
    def forbidden(*args, **kwargs): raise AssertionError("host boundary reached IO/setup/backend")
    monkeypatch.setattr(R, "assert_guest", deny)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(B, "adapter_identity", forbidden)
    with pytest.raises(PermissionError, match="VM only"):
        F.run_trial("/not-read", {}, {}, {}, "/not-written", expected_identity={}, setup_hook=forbidden, limits={})


@pytest.mark.parametrize("fault", ["callback_key", "future_evidence", "future_epoch", "approval_flag"])
def test_json_cannot_supply_callback_future_pins_or_authority(fault):
    intent = {"format": F.INTENT, "id": "slot", "phase": "learning", "actor_sha256": "a" * 64,
        "policy_sha256": "b" * 64, "setup": {"id": "not-code", "producer": {"file": "/fixture/source",
            "sha256": "c" * 64, "bytes": 1}}, "deployment": {"id": "deployment", "kind": "existing_process"}}
    if fault == "callback_key": intent["setup"]["callback"] = "evaluate_generated_source"
    elif fault == "future_evidence": intent["evidence"] = {"worker": "future"}
    elif fault == "future_epoch": intent["deployment"]["launch_time"] = 100000
    else: intent["setup"]["authorized"] = True
    with pytest.raises(ValueError):
        F.validate_intent(intent)


def test_callback_name_is_not_executable_setup_before_input_io(monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    def forbidden(*args, **kwargs): raise AssertionError("JSON callback name reached file/backend acquisition")
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(B, "adapter_identity", forbidden)
    with pytest.raises(ValueError, match="fixed trusted host setup callback"):
        F.run_trial("/not-read", {}, {}, {}, "/not-written", expected_identity={}, setup_hook="callback.from.JSON", limits={})


@pytest.mark.parametrize("phase", ["learning", "fresh", "heldout"])
def test_qt_trial_uses_actual_shared_gates_and_complete_independent_external_states(prepared_qt, monkeypatch, phase):
    data, checked = prepared_qt, []
    original = B.validate_qt_moc_result
    def gate(request, result):
        observed = original(request, result)
        if request["operations"][0]["op"] == "qt_moc_invoke": checked.append(copy.deepcopy(observed))
        return observed
    monkeypatch.setattr(B, "validate_qt_moc_result", gate)
    data["intent"]["phase"] = phase
    references = {role: Path(data["policy"][role]["reference"]["file"]).read_bytes() for role in ("before", "after")}
    result = run(data)
    assert result["status"] == "measured" and result["trial_measurement_validated"] is True and checked
    assert_native_once(data, result)
    assert len(data["setup_calls"]) == 1 and len(data["backends"]) == 2 and data["signals"] == []
    case = R.load_json(result["case"]["file"])
    assert case["phase"] == phase and case["inputs"] == {} and data["policy"]["params"] == []
    assert "signature_evidence" not in data["actor"] and "arguments" not in data["actor"]
    for role, raw in references.items(): assert Path(data["policy"][role]["reference"]["file"]).read_bytes() == raw
    worker = R.load_json(case["evidence"]["worker"]["file"])
    assert worker["operations"][0]["qt_moc"]["argv"]["expired"] is True
    assert worker["operations"][0]["value"] is None and checked[-1] == worker["operations"][0]["qt_moc"]
    for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created", "aggregation_validated",
                "restart_deployment_proven", "input_generalization_claimed"):
        assert result[key] is False
    def forbidden(*args, **kwargs): raise AssertionError("historical Qt validation attempted live work")
    monkeypatch.setattr(B, "process_identity", forbidden)
    assert E.validate_trial(data["policy"], case, data["actor"])["trial_measurement_validated"] is True


@pytest.mark.parametrize("fault", ["wrong_before", "incomplete_before", "setup", "qt_vector", "qt_cleanup"])
def test_qt_external_or_runtime_refusal_preserves_the_one_actual_attempt(prepared_qt, fault):
    data = prepared_qt
    if fault == "wrong_before": data["controls"]["before"] = "wrong_then_correct"
    elif fault == "incomplete_before": data["controls"]["before"] = "incomplete"
    elif fault == "setup": data["controls"]["setup_error"] = True
    elif fault == "qt_vector": data["state"]["result_changes"]["operations"][0]["qt_moc"]["argv"]["expired"] = False
    else: data["state"]["result_changes"]["lifecycle_observer"]["hooks_detached"] = 0
    result = run(data)
    assert result["trial_measurement_validated"] is False and result["errors"]
    assert len(data["collect_calls"]) == len(data["processes"]) == len(data["state"]["calls"]) == 1
    assert result["native_disposition"]["native_calls"] == result["native_disposition"]["invocations"] == 1
    assert result["native_disposition"]["owned_worker"]["reaped"] is True
    assert len(data["setup_calls"]) == 1 and data["signals"] == []
    if fault == "wrong_before":
        before = R.load_json(data["out"] / "phases/before-settlement.json")
        assert before["stable_pair"] == [0, 1] and len(before["samples"]) == 2
    for key in ("proof_created", "public_proof_created", "card_created", "aggregation_validated"):
        assert result[key] is False


@pytest.mark.parametrize("fault", ["catalog", "source", "extra_scalar", "old_native_actor", "restart"])
def test_qt_missing_originals_or_unsupported_scope_refuses_before_worker_and_setup(prepared_qt, fault):
    data = prepared_qt
    if fault == "catalog": data["qt_files"]["catalog"].write_text("{}")
    elif fault == "source":
        file = data["qt_files"]["qt_sources"].parent / "qobjectdefs.h"
        file.write_bytes(file.read_bytes() + b"\nchanged source")
    elif fault == "extra_scalar":
        data["actor"]["receiver"]["native_path"]["types"][0]["fields"].append({"name": "payload", "type": "int32", "offset": 8})
    elif fault == "old_native_actor":
        qt = data["actor"]
        data["actor"] = {"kind": "native_function", "target": {"module": qt["selection"]["module"],
            "offset": qt["selection"]["dispatcher_offset"]}, "abi": "default", "arguments": [
                {"type": "pointer", "value": qt["receiver"]}], "return_type": "void",
            "signature_evidence": qt["receiver"]["native_path"]["consumer"]["evidence"]["claim"]}
    else: data["intent"]["deployment"]["kind"] = "restart"
    data["intent"]["actor_sha256"] = R.digest(data["actor"])
    with pytest.raises(ValueError): run(data)
    assert not data["out"].exists() and not data["collect_calls"] and not data["processes"]
    assert not data["setup_calls"] and not data["backends"]



def _reported_disposition_files(tmp_path, *, embedded=False):
    """Only inert failure reports: no valid execution/E proof is manufactured."""
    folder = tmp_path / "reported-disposition"
    (folder / "runtime").mkdir(parents=True)
    request_sha = R.digest({"inert_frozen_request_identity": "one sole F request"})
    worker = {"format": B.WORKER, "request_sha256": request_sha, "status": "completed",
              "native_calls": 1, "invocations": 1, "native_state_uncertain": True}
    owned = {"pid": 9182, "returncode": 0, "reaped": True, "term_attempted": False,
             "kill_attempted": False, "errors": []}
    context = {"format": B.PHASE_CONTEXT, "request_sha256": request_sha, "owned_worker": owned, "errors": []}
    runtime = {"format": B.RUNTIME, "request_sha256": request_sha, "status": "executed",
               "resource_review_required": False, "result": copy.deepcopy(worker)}
    R.write_json(folder / "run-context.json", context)
    R.write_json(folder / "runtime/worker-output.json", worker)
    if embedded: R.write_json(folder / "runtime/runtime.json", runtime)
    return folder, request_sha, worker, context, runtime


def test_raw_failure_report_preserves_available_counts_pin_and_missing_runtime_error_without_proof(tmp_path):
    folder, request_sha, worker, context, _ = _reported_disposition_files(tmp_path)
    result = F._disposition(folder, None, request_sha)
    assert result["receipt"] is None and result["runtime_status"] is None
    assert result["raw_worker"] == pin(folder / "runtime/worker-output.json")
    assert result["worker_status"] == "completed" and result["native_calls"] == result["invocations"] == 1
    assert result["raw_worker_provenance"] == "request_bound_raw_report_not_validated_execution"
    assert result["reported_counts_provenance"] == "request_bound_raw_worker_report"
    assert result["owned_context_confirmed"] is True and result["worker_conflict"] is False
    assert result["resource_review_required"] is True and result["read_error"]
    assert result["worker_reports"]["raw_worker"] == {"status": "completed", "native_calls": 1, "invocations": 1}


@pytest.mark.parametrize("fault", ["request", "legacy", "bool", "float", "negative", "phase_request",
                                    "unreaped", "returncode", "term", "kill", "cleanup_error", "raw_alias"])
def test_unbound_malformed_or_not_naturally_reaped_raw_reports_never_supply_primary_counts(tmp_path, fault):
    folder, request_sha, worker, context, _ = _reported_disposition_files(tmp_path)
    if fault == "request": worker["request_sha256"] = "0" * 64
    elif fault == "legacy": worker["format"] = B.LEGACY_WORKER
    elif fault == "bool": worker["native_calls"] = True
    elif fault == "float": worker["native_calls"] = 1.0
    elif fault == "negative": worker["invocations"] = -1
    elif fault == "phase_request": context["request_sha256"] = "0" * 64
    elif fault == "unreaped": context["owned_worker"]["reaped"] = False
    elif fault == "returncode": context["owned_worker"]["returncode"] = -15
    elif fault == "term": context["owned_worker"]["term_attempted"] = True
    elif fault == "kill": context["owned_worker"]["kill_attempted"] = True
    elif fault == "cleanup_error": context["owned_worker"]["errors"] = [{"type": "Error", "message": "inert reap uncertainty"}]
    R.write_json(folder / "run-context.json", context)
    R.write_json(folder / "runtime/worker-output.json", worker)
    if fault == "raw_alias":
        path = folder / "runtime/worker-output.json"
        saved = path.with_name("saved-worker.json")
        path.rename(saved)
        path.symlink_to(saved)
    result = F._disposition(folder, None, request_sha)
    assert result["native_calls"] is None and result["invocations"] is None and result["reported_counts_provenance"] is None
    assert result["receipt"] is None and result["runtime_status"] is None and result["read_error"]
    if fault in {"unreaped", "returncode", "term", "kill", "cleanup_error"}:
        assert result["raw_worker"] == pin(folder / "runtime/worker-output.json")
        assert result["worker_reports"]["raw_worker"]["native_calls"] == 1
        assert result["owned_context_confirmed"] is False
    elif fault in {"request", "legacy", "bool", "float", "negative", "raw_alias"}:
        assert result["owned_context_confirmed"] is True
        assert result["worker_reports"]["raw_worker"] is None


@pytest.mark.parametrize("fault", ["count", "state", "request"])
def test_conflicting_embedded_and_raw_reports_retain_both_pins_and_leave_primary_counts_unknown(tmp_path, fault):
    folder, request_sha, worker, context, runtime = _reported_disposition_files(tmp_path, embedded=True)
    if fault == "count": runtime["result"]["native_calls"] = 2
    elif fault == "state": runtime["result"]["native_state_uncertain"] = False
    else: worker["request_sha256"] = "0" * 64
    R.write_json(folder / "runtime/runtime.json", runtime)
    R.write_json(folder / "runtime/worker-output.json", worker)
    result = F._disposition(folder, None, request_sha)
    assert result["receipt"] == pin(folder / "runtime/runtime.json")
    assert result["raw_worker"] == pin(folder / "runtime/worker-output.json")
    assert result["worker_conflict"] is True and result["read_error"] and result["worker_status"] is None
    assert result["native_calls"] is None and result["invocations"] is None and result["reported_counts_provenance"] is None
    assert result["worker_reports"]["runtime_result"]["native_calls"] == (2 if fault == "count" else 1)
    if fault != "request": assert result["worker_reports"]["raw_worker"]["native_calls"] == 1
    assert any("conflicts" in error["message"] for error in result["read_errors"])


def test_conflicting_retained_worker_reports_keep_the_actual_one_attempt_and_cannot_finalize_success(prepared, monkeypatch):
    original = B.collect_phased
    def collect(*args, **kwargs):
        wrapper = original(*args, **kwargs)
        file = prepared["out"] / "phases/runtime/runtime.json"
        runtime = R.load_json(file)
        runtime["result"]["native_calls"] = 2
        R.write_json(file, runtime)
        return wrapper
    monkeypatch.setattr(B, "collect_phased", collect)
    result = run(prepared)
    assert result["trial_measurement_validated"] is False and result["status"] == "resource_review_required"
    assert len(prepared["collect_calls"]) == len(prepared["processes"]) == len(prepared["state"]["calls"]) == 1
    native = result["native_disposition"]
    assert native["worker_conflict"] is True and native["read_error"]
    assert native["native_calls"] is None and native["invocations"] is None
    assert native["receipt"] == pin(prepared["out"] / "phases/runtime/runtime.json")
    assert native["raw_worker"] == pin(prepared["out"] / "phases/runtime/worker-output.json")
    assert native["worker_reports"]["runtime_result"]["native_calls"] == 2
    assert native["worker_reports"]["raw_worker"]["native_calls"] == 1
    for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created", "aggregation_validated"):
        assert result[key] is False


def test_owned_observer_runs_real_settler_and_gate_without_legacy_focus_fabrication(prepared_owned, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("owner mode routed through strict S")
    monkeypatch.setattr(S, "settle", forbidden)
    result = run(prepared_owned)
    assert result["format"] == F.REPORT2 and result["trial_measurement_validated"]
    assert result["observer_capability"] == SR.ownedcap()
    assert result["state_observation_scope"] == "selected_owned_visible_ax_window_tree"
    assert prepared_owned["observer_calls"] == [SR.ownedcap()]
    assert_native_once(prepared_owned, result)
    inputs = R.load_json(prepared_owned["out"] / "inputs.json")
    assert inputs["observer_capability"] == SR.ownedcap()
    for role in ("before", "after"):
        settlement = R.load_json(prepared_owned["out"] / "phases" / (role + "-settlement.json"))
        assert settlement["format"] == prepared_owned["observer"].SETTLEMENT
        for sample in settlement["samples"]:
            assert "focus" not in sample["observation"]["state"]
            assert sample["observation"]["context"]["before"]["focus"]["focused_pid"] is None
    measurement = R.load_json(result["measurement"]["file"])
    assert measurement["format"] == E.REPORT2 and measurement["observer_capability"] == SR.ownedcap()
    assert not any(result[key] for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created"))


@pytest.mark.parametrize("fault", ["incomplete_before", "wrong_stable_before", "setup_failure", "legacy_report", "wrong_report_cap"])
def test_owned_failure_retains_actual_once_counts_and_never_falls_back_to_strict_observer(prepared_owned, monkeypatch, fault):
    if fault == "incomplete_before": prepared_owned["controls"]["before"] = "incomplete"
    elif fault == "wrong_stable_before": prepared_owned["controls"]["before"] = "wrong_then_correct"
    elif fault == "setup_failure": prepared_owned["controls"]["setup_error"] = True
    else:
        validate = E.validate_trial
        def changed(*args, **kwargs):
            value = validate(*args, **kwargs)
            if fault == "legacy_report": value["format"] = E.REPORT
            else: value["observer_capability"] = SR.strictcap()
            return value
        monkeypatch.setattr(E, "validate_trial", changed)
    result = run(prepared_owned)
    assert result["format"] == F.REPORT2 and not result["trial_measurement_validated"]
    assert result["measurement"] is None and not result["task_proof_created"]
    assert_native_once(prepared_owned, result)
    assert prepared_owned["observer_calls"] == [SR.ownedcap()] and not prepared_owned["signals"]


def test_legacy_trial_keeps_default_collector_call_and_old_report_shape(prepared):
    result = run(prepared)
    assert result["format"] == F.REPORT and "observer_capability" not in result
    assert prepared["observer_calls"] == [None]
    inputs = R.load_json(prepared["out"] / "inputs.json")
    assert "observer_capability" not in inputs


def test_legacy_unknown_focus_still_fails_without_automatic_owned_observer_fallback(prepared, monkeypatch):
    factory = S._Mac
    def unknown():
        backend = factory()
        backend.current["focus"]["focused_pid"] = None
        return backend
    def forbidden(*args, **kwargs): raise AssertionError("legacy focus denial fell back to owned observer")
    monkeypatch.setattr(S, "_Mac", unknown)
    monkeypatch.setattr(SR.for_capability(SR.ownedcap()), "settle", forbidden)
    result = run(prepared)
    assert result["format"] == F.REPORT and not result["trial_measurement_validated"]
    assert prepared["observer_calls"] == [None] and result["measurement"] is None
    assert_native_once(prepared, result)


def test_legacy_measurement_report_cannot_silently_add_owned_observation_scope(prepared, monkeypatch):
    validate = E.validate_trial
    def changed(*args, **kwargs):
        value = validate(*args, **kwargs)
        assert value["format"] == E.REPORT
        value.update(observer_capability=SR.ownedcap(), state_observation_scope="selected_owned_visible_ax_window_tree")
        return value
    monkeypatch.setattr(E, "validate_trial", changed)
    result = run(prepared)
    assert result["format"] == F.REPORT and not result["trial_measurement_validated"]
    assert result["measurement"] is None
    assert_native_once(prepared, result)


@pytest.mark.parametrize("option", [None, 0, 1, "true", {}, []])
def test_private_ack_option_is_exact_trusted_bool_before_any_output_or_worker(prepared, option):
    with pytest.raises(ValueError):
        F.run_trial(prepared["app"], prepared["actor"], prepared["policy"], prepared["intent"], prepared["out"],
            expected_identity=prepared["identity"], setup_hook=prepared["setup"], limits=prepared["limits"],
            require_pre_actuation_ack=option)
    assert not prepared["out"].exists() and not prepared["setup_calls"] and not prepared["processes"]
    assert not prepared["state"]["calls"] and not prepared["collect_calls"]


@pytest.mark.parametrize("field", ["require_pre_actuation_ack", "observer_selector", "setup_validator"])
def test_json_intent_cannot_select_private_ack_or_callable_setup_authority(prepared, field):
    prepared["intent"][field] = True
    with pytest.raises(ValueError): run(prepared)
    assert not prepared["out"].exists() and not prepared["state"]["calls"] and not prepared["setup_calls"]


@pytest.mark.parametrize("owned", [False, True])
def test_ack_opt_in_forwards_only_frozen_selector_and_capability_and_retains_unknown_unstarted_outcome(
        tmp_path, monkeypatch, owned):
    import test_rea_runtime_bridge as fixtures
    inert_runtime = fixtures.runtime.__wrapped__(tmp_path, monkeypatch)
    originals = fixtures.native_files.__wrapped__(inert_runtime)
    data = _prepare_trial_data(inert_runtime, originals, monkeypatch, owned=owned)
    calls = []
    def not_started(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        raise ValueError("inert refusal before Popen; no worker outcome exists")
    monkeypatch.setattr(B, "collect_phased", not_started)
    outcome = run(data, require_pre_actuation_ack=True)
    cap = SR.ownedcap() if owned else SR.strictcap()
    assert len(calls) == 1 and calls[0]["require_pre_actuation_ack"] is True
    assert calls[0]["observer_capability"] == cap
    assert calls[0]["observer_selector"] == data["policy"]["before"]["contract"]["selector"]
    assert "state" not in calls[0]["observer_selector"]
    assert outcome["format"] == F.REPORT3 and outcome["pre_actuation_ack_required"] is True
    assert outcome["pre_actuation_ack_validated"] is False and outcome["pre_actuation_ack"] is None
    assert outcome["observer_capability"] == cap and not outcome["trial_measurement_validated"]
    assert outcome["native_disposition"]["native_calls"] is None
    assert outcome["native_disposition"]["invocations"] is None
    assert not data["processes"] and not data["setup_calls"] and not data["state"]["calls"]
    frozen = R.load_json(data["out"] / "inputs.json")
    assert frozen["require_pre_actuation_ack"] is True and frozen["observer_capability"] == cap
    assert frozen["policy"] == data["policy"]


@pytest.mark.parametrize("field", ["pre_actuation_ack", "pre_actuation_ack_required", "pre_actuation_ack_validated"])
def test_non_ack_trial_refuses_added_ack_measurement_claims_after_actual_legacy_validation(prepared, monkeypatch, field):
    original = E.validate_trial
    def forged(*args, **kwargs):
        result = original(*args, **kwargs)
        result[field] = {} if field == "pre_actuation_ack" else True
        return result
    monkeypatch.setattr(E, "validate_trial", forged)
    outcome = run(prepared)
    assert outcome["format"] == F.REPORT and not outcome["trial_measurement_validated"]
    assert outcome["measurement"] is None and "pre_actuation_ack" not in outcome
    assert_native_once(prepared, outcome)


def test_actual_new_ack_chain_is_independently_measured_and_expires_without_task_or_public_proof(prepared_ack):
    outcome = run(prepared_ack, require_pre_actuation_ack=True)
    assert outcome["format"] == F.REPORT3 and outcome["trial_measurement_validated"]
    assert outcome["pre_actuation_ack_required"] is True and outcome["pre_actuation_ack_validated"] is True
    assert outcome["pre_actuation_ack"]["format"] == RP.PROTOCOL
    assert outcome["pre_actuation_ack"]["parent_committed"] is True
    assert outcome["pre_actuation_ack"]["agent_consumed"] is True
    assert_native_once(prepared_ack, outcome)
    case = R.load_json(outcome["case"]["file"])
    wrapper = R.load_json(case["evidence"]["execution"]["file"])
    context = R.load_json(case["evidence"]["run_context"]["file"])
    worker = R.load_json(case["evidence"]["worker"]["file"])
    parent = R.load_json(wrapper["pre_actuation_ack"]["file"])
    control = R.load_json(prepared_ack["out"] / "phases/runtime/worker-permit.json")
    assert wrapper["format"] == B.PHASED3 and context["format"] == B.PHASE_CONTEXT3
    assert wrapper["pre_actuation_ack"] == context["pre_actuation_ack"] == pin(
        prepared_ack["out"] / "phases/pre-actuation-parent.json")
    assert parent["decision"] == control["decision"] and parent["decision"]["decision"] == "allow"
    assert parent["sent"] is True and control["thread_stopped"] is True
    assert worker["pre_actuation_ack"]["state"] == "consumed"
    assert worker["pre_actuation_ack"]["permit_consumed"] is True and worker["pre_actuation_ack"]["closed"] is True
    result = E.validate_trial(prepared_ack["policy"], case, prepared_ack["actor"])
    assert result["format"] == E.REPORT3 and result["pre_actuation_ack_validated"] is True
    assert result["pre_actuation_ack"] == outcome["pre_actuation_ack"]
    for file in (wrapper["pre_actuation_ack"]["file"], parent["decision"]["checkpoint"]["file"],
                 str(prepared_ack["out"] / "phases/runtime/worker-permit.json")):
        assert result["files"][file] == pin(file)
    assert all(outcome[key] is False for key in ("proof_created", "task_proof_created", "public_proof_created",
        "card_created", "aggregation_validated", "restart_deployment_proven", "input_generalization_claimed"))


@pytest.mark.parametrize("fault", ["setup_raise", "setup_missing_marker", "setup_failed_marker", "before_incomplete"])
def test_actual_new_ack_denies_before_native_entry_preserves_raw_failure_and_does_not_retry(prepared_ack, fault):
    if fault == "setup_raise": prepared_ack["controls"]["setup_error"] = True
    elif fault == "before_incomplete": prepared_ack["controls"]["before"] = "incomplete"
    else:
        original = prepared_ack["setup"]
        def malformed(context):
            result = original(context)
            if fault == "setup_missing_marker": return result["result"]
            result["status"] = "failed"
            return result
        prepared_ack["setup"] = malformed
    outcome = run(prepared_ack, require_pre_actuation_ack=True)
    assert outcome["format"] == F.REPORT3 and not outcome["trial_measurement_validated"]
    assert outcome["pre_actuation_ack_validated"] is False and outcome["pre_actuation_ack"] is None
    assert len(prepared_ack["setup_calls"]) == len(prepared_ack["processes"]) == len(prepared_ack["state"]["calls"]) == 1
    raw = R.load_json(prepared_ack["out"] / "phases/runtime/worker-output.json")
    assert raw["native_calls"] == raw["invocations"] == 0
    assert raw["pre_actuation_ack"]["permit_consumed"] is False and raw["pre_actuation_ack"]["native_entry"] is False
    disposition = outcome["native_disposition"]
    # The real private CLI returns1 for an incomplete/denied worker. Existing
    # F conservatively retains those raw reports without promoting top-level
    # counts through its natural-exit0 gate; DENY itself supplies no zero proof.
    assert disposition["native_calls"] is None and disposition["invocations"] is None
    assert disposition["owned_worker"]["returncode"] == 1 and disposition["owned_worker"]["reaped"] is True
    assert disposition["worker_reports"]["raw_worker"]["native_calls"] == 0
    assert disposition["worker_reports"]["raw_worker"]["invocations"] == 0
    parent = R.load_json(prepared_ack["out"] / "phases/pre-actuation-parent.json")
    assert parent["decision"]["decision"] == "deny"
    if fault == "before_incomplete":
        retained = R.load_json(prepared_ack["out"] / "phases/before-settlement.json")
        assert prepared_ack["observer"].validate_settlement(retained)["complete"] is False and retained["samples"]
    elif fault != "setup_raise":
        retained = R.load_json(prepared_ack["out"] / "phases/setup-return.json")
        assert retained.get("status") == ("failed" if fault == "setup_failed_marker" else None)


def test_ack_complete_wrong_before_does_not_gain_task_credit_or_an_expected_state_oracle(prepared_ack):
    prepared_ack["controls"]["before"] = "wrong_then_correct"
    outcome = run(prepared_ack, require_pre_actuation_ack=True)
    assert not outcome["trial_measurement_validated"] and outcome["measurement"] is None
    assert_native_once(prepared_ack, outcome)
    before = R.load_json(prepared_ack["out"] / "phases/before-settlement.json")
    assert before["complete"] is True and before["stable_pair"] == [0, 1] and len(before["samples"]) == 2
    assert before["samples"][-1]["observation"]["state"]["nodes"][2]["value"] is True
    parent = R.load_json(prepared_ack["out"] / "phases/pre-actuation-parent.json")
    assert parent["decision"]["decision"] == "allow"  # Permit is not semantic-before contract proof.
    assert outcome["pre_actuation_ack_validated"] is False and not outcome["task_proof_created"]


def _prepare_real_restart_admission(data, tmp_path, monkeypatch, *, restart_offset=1):
    """Actual E predecessor and actual R25 producer/validator, all OS inert.

    The new native dispatch is deliberately not faked as successful. These
    tests establish prospective admission/refusal, not live app restart or
    post-restart task success. Kernel normalization happens in the backend
    BEFORE producing the predecessor, never by editing completed evidence.
    """
    from anyuse.execution import rea_process_restart as RR
    import test_rea_process_restart as RX
    original_backend = data["observer"]._Mac
    def backend():
        value = original_backend()
        value.current["kernel"] = RX._kernel(data["identity"])
        return value
    monkeypatch.setattr(data["observer"], "_Mac", backend)
    before = run(data, require_pre_actuation_ack=True)
    assert before["trial_measurement_validated"] is True
    case = R.load_json(data["out"] / "case.json")
    context = R.load_json(case["evidence"]["run_context"]["file"])
    class Clock:
        now = context["phases"]["after"]["finished_monotonic"] + restart_offset
        def monotonic(self):
            self.now += .00001
            return self.now
        def time(self): return 1792317600 + self.now
    clock = Clock()
    monkeypatch.setattr(RR.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(RR.time, "time", clock.time)
    old = copy.deepcopy(data["identity"])
    old_kernel = RX._kernel(old)
    class RestartBackend:
        phase, new_identity, new_kernel = "old", None, None
        def registrations(self, bundle_id):
            assert bundle_id == old["bundle_id"]
            return [] if self.phase == "absent" else [RX._ns(old if self.phase == "old" else self.new_identity)]
        def query(self, pid, folder, deadline):
            kernel = old_kernel if self.phase == "old" else self.new_kernel if self.phase == "new" else None
            assert kernel is None or kernel["pid"] == pid
            return RX._fake_query(pid, folder, deadline, clock, kernel)
        def tick(self, deadline):
            assert clock.now < deadline
            clock.now += RR.INTERVAL
    restart_backend = RestartBackend()
    monkeypatch.setattr(RR, "_Mac", lambda: restart_backend)
    monkeypatch.setattr(B, "process_identity", lambda *args: copy.deepcopy(
        restart_backend.new_identity if restart_backend.phase == "new" else old))
    producer = tmp_path / "inert-normal-restart-producer.txt"
    producer.write_text("Inert ordinary callback provenance; not authentication or a running app.\n")
    restart_intent = {"format": RR.INTENT, "id": "actual-inert-restart", "target": R.fingerprint(data["app"]),
        "old_identity": old, "stop": {"id": "one-normal-stop", "producer": pin(producer)},
        "launch": {"id": "one-normal-launch", "producer": pin(producer)}}
    ordinary_calls = []
    def stop(context):
        ordinary_calls.append("stop")
        assert context["old_identity"] == old and context["old_kernel"] == old_kernel
        restart_backend.phase = "absent"
        return {"normal_stop_api_return": True}
    def launch(context):
        ordinary_calls.append("launch")
        assert restart_backend.phase == "absent"
        restart_backend.new_identity = {**copy.deepcopy(old), "pid": old["pid"] + 37, "launch_time": clock.time()}
        restart_backend.new_kernel = RX._kernel(restart_backend.new_identity)
        restart_backend.phase = "new"
        return {"application_pid": restart_backend.new_identity["pid"], "result": {"normal_launch_completion": True}}
    restarted = RR.run_restart(data["app"], old, restart_intent, tmp_path / "restart-witness",
        stop_hook=stop, launch_hook=launch)
    assert restarted["status"] == "restart_measurement_validated" and ordinary_calls == ["stop", "launch"]
    raw = R.load_json(restarted["receipt"]["file"])
    intent = {**copy.deepcopy(data["intent"]), "format": F.INTENT2, "id": "heldout-restart-0", "phase": "heldout",
        "deployment": {"id": "independently-witnessed-new-epoch", "kind": "restart",
            "predecessor_case": pin(data["out"] / "case.json"), "restart_intent": copy.deepcopy(raw["intent"]),
            "restart_receipt": copy.deepcopy(restarted["receipt"])}}
    return {"trial": data, "case": case, "intent": F.validate_intent(intent), "restart": restarted,
        "identity": copy.deepcopy(restart_backend.new_identity), "target": R.fingerprint(data["app"]),
        "clock": clock, "producer": producer, "ordinary_calls": ordinary_calls,
        "out": tmp_path / "new-native-trial", "reader": E._Reader()}


@pytest.fixture
def restart_admission_data(prepared_ack, tmp_path, monkeypatch):
    return _prepare_real_restart_admission(prepared_ack, tmp_path, monkeypatch)


def _admit_restart(data, *, intent=None, identity=None):
    original = data["trial"]
    return F._restart_admission(data["reader"], original["actor"], original["policy"],
        data["intent"] if intent is None else intent, data["identity"] if identity is None else identity, data["target"])


def _run_restart_trial(data, *, intent=None, acknowledged=True):
    original = data["trial"]
    def forbidden_setup(context):
        raise AssertionError("admission-only fixture must not perform setup or native work")
    return F.run_trial(original["app"], original["actor"], original["policy"],
        data["intent"] if intent is None else intent, data["out"], expected_identity=data["identity"],
        setup_hook=forbidden_setup, limits=original["limits"], require_pre_actuation_ack=acknowledged)


def test_restart_admission_reopens_real_E_and_R25_closures_without_native_or_campaign_credit(restart_admission_data):
    data = restart_admission_data
    actual = _admit_restart(data)
    assert actual["format"] == F.RESTART_ADMISSION
    assert actual["new_identity"] == data["identity"] and actual["old_identity"] == data["case"]["deployment"]["identity"]
    assert actual["predecessor_measurement"]["trial_measurement_validated"] is True
    assert actual["restart_measurement"]["restart_measurement_validated"] is True
    assert actual["predecessor_finished_monotonic"] < actual["restart_started_monotonic"]
    assert actual["restart_finished_monotonic"] < actual["checked_monotonic"]
    for report in (actual, actual["predecessor_measurement"], actual["restart_measurement"]):
        assert report["task_proof_created"] is False and report["public_proof_created"] is False
    assert data["ordinary_calls"] == ["stop", "launch"] and not data["out"].exists()
    data["reader"].recheck()


def test_genuine_predecessor_measurement_cannot_be_reused_as_the_new_post_restart_trial(restart_admission_data):
    data = restart_admission_data
    admitted = _admit_restart(data)
    # These are genuine complete old E artifacts, not forged success flags.
    # A new admission cannot turn them into a trial on the witnessed new epoch.
    with pytest.raises(ValueError, match="new trial kernel/identity/time"):
        F._restart_trial_binding(E._Reader(), data["case"]["evidence"], admitted, data["identity"])


@pytest.mark.parametrize("fault", ["extra", "existing_process", "missing_receipt", "same_file", "wrong_format"])
def test_restart_intent2_is_closed_and_cannot_sneak_into_legacy_deployment(restart_admission_data, fault):
    intent = copy.deepcopy(restart_admission_data["intent"])
    if fault == "extra": intent["deployment"]["pre_actuation_ack"] = True
    elif fault == "existing_process": intent["deployment"]["kind"] = "existing_process"
    elif fault == "missing_receipt": del intent["deployment"]["restart_receipt"]
    elif fault == "same_file": intent["deployment"]["restart_receipt"] = copy.deepcopy(intent["deployment"]["restart_intent"])
    else: intent["format"] = F.INTENT
    with pytest.raises(ValueError): F.validate_intent(intent)


@pytest.mark.parametrize("fault", ["case_id", "deployment_id", "pid", "launch_time", "uid", "receipt_pin", "producer_changed"])
def test_restart_admission_refuses_identity_pin_source_and_relabeling_without_dispatch(restart_admission_data, fault):
    data = restart_admission_data
    intent, identity = copy.deepcopy(data["intent"]), copy.deepcopy(data["identity"])
    if fault == "case_id": intent["id"] = data["case"]["id"]
    elif fault == "deployment_id": intent["deployment"]["id"] = data["case"]["deployment"]["id"]
    elif fault in {"pid", "launch_time", "uid"}: identity[fault] += 1
    elif fault == "receipt_pin": intent["deployment"]["restart_receipt"]["sha256"] = "f" * 64
    else: data["producer"].write_text("changed after actual restart measurement")
    with pytest.raises(ValueError): _admit_restart(data, intent=intent, identity=identity)
    assert not data["out"].exists() and data["ordinary_calls"] == ["stop", "launch"]


def test_restart_source_boundary_is_explicitly_trusted_ACK_before_any_new_output(restart_admission_data, monkeypatch):
    data = restart_admission_data
    def forbidden(*args, **kwargs): raise AssertionError("missing trusted option reached IO or dispatch")
    monkeypatch.setattr(B, "collect_phased", forbidden)
    with pytest.raises(ValueError, match="private acknowledgement"):
        _run_restart_trial(data, acknowledged=False)
    assert not data["out"].exists()


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_restart_predecessor_alias_cannot_substitute_for_canonical_frozen_case(restart_admission_data, tmp_path, alias):
    import os
    data = restart_admission_data
    intent = copy.deepcopy(data["intent"])
    original = Path(intent["deployment"]["predecessor_case"]["file"])
    link = tmp_path / "predecessor-alias.json"
    if alias == "symlink": link.symlink_to(original)
    else: os.link(original, link)
    # Deliberately retain the alias name: the helper pin() canonicalizes paths
    # and would otherwise accidentally erase the symlink negative condition.
    intent["deployment"]["predecessor_case"] = {"file": str(link), "bytes": original.stat().st_size,
        "sha256": R._file_hash(original)}
    with pytest.raises(ValueError): _admit_restart(data, intent=intent)
    assert not data["out"].exists()


def test_restart_finished_before_predecessor_cannot_be_admitted_even_if_both_individual_measurements_pass(
        prepared_ack, tmp_path, monkeypatch):
    data = _prepare_real_restart_admission(prepared_ack, tmp_path, monkeypatch, restart_offset=-40)
    assert data["restart"]["status"] == "restart_measurement_validated"
    with pytest.raises(ValueError, match="follow the measured predecessor"):
        _admit_restart(data)


def test_restart_admission_cannot_accept_a_future_finished_witness(restart_admission_data):
    data = restart_admission_data
    raw = R.load_json(data["restart"]["receipt"]["file"])
    data["clock"].now = raw["finished_monotonic"] - .01
    with pytest.raises(ValueError, match="finish before admission"):
        _admit_restart(data)


def test_new_restart_intent_and_real_witness_are_frozen_then_revalidated_before_one_dispatch(
        restart_admission_data, monkeypatch):
    data, validations, dispatches = restart_admission_data, [], []
    validate = F._restart_admission
    def counted(*args, **kwargs):
        result = validate(*args, **kwargs)
        validations.append(copy.deepcopy(result))
        return result
    monkeypatch.setattr(F, "_restart_admission", counted)
    def dispatch(*args, **kwargs):
        dispatches.append(True)
        inputs = R.load_json(data["out"] / "inputs.json")
        frozen = R.load_json(data["out"] / "restart-admission.json")
        assert inputs["intent"] == data["intent"] and inputs["restart_admission"] == frozen == validations[0]
        assert len(validations) == 2 and kwargs["require_pre_actuation_ack"] is True
        assert kwargs["expected_identity"] == data["identity"] and not (data["out"] / "case.json").exists()
        raise RuntimeError("inert dispatch sentinel before any worker or native operation")
    monkeypatch.setattr(B, "collect_phased", dispatch)
    outcome = _run_restart_trial(data)
    assert outcome["format"] == F.REPORT4 and len(dispatches) == 1
    assert outcome["trial_measurement_validated"] is False and outcome["restart_deployment_proven"] is False
    assert outcome["aggregation_validated"] is False and outcome["public_proof_created"] is False
    assert outcome["native_disposition"]["native_calls"] is None and outcome["measurement"] is None
    assert R.load_json(outcome["restart_admission"]["file"]) == validations[0]


def test_witness_change_after_prospective_freeze_refuses_before_backend_or_setup(restart_admission_data, monkeypatch):
    data, calls = restart_admission_data, []
    validate = F._restart_admission
    def mutate_before_second_validation(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2: data["producer"].write_text("changed after prospective freeze, before dispatch")
        return validate(*args, **kwargs)
    def forbidden(*args, **kwargs): raise AssertionError("changed restart witness reached native backend")
    monkeypatch.setattr(F, "_restart_admission", mutate_before_second_validation)
    monkeypatch.setattr(B, "collect_phased", forbidden)
    outcome = _run_restart_trial(data)
    assert len(calls) == 2 and outcome["format"] == F.REPORT4
    assert outcome["trial_measurement_validated"] is False and outcome["native_disposition"] is None
    assert R.load_json(outcome["attempt"]["file"])["collection_attempted"] is False
    assert data["ordinary_calls"] == ["stop", "launch"] and not (data["out"] / "case.json").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
