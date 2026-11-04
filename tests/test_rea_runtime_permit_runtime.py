"""Guest-only whole-JS permit checks in the existing inert NodeVM fixture.

These exercise the actual fixed adapter, not Frida scheduling, native target
instructions, parent authentication, independent task evidence or publication.
All added message delivery and transition faults exist only in this test copy.
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_runtime_permit as RP
from anyuse.execution import rea_qt_moc as Q
import test_rea_native_path_runtime as N
import test_rea_qt_moc_runtime as QT


@pytest.fixture(scope="module", autouse=True)
def _guest_only():
    R.assert_guest()


def _case(*, qt=False, read=False, selector=None):
    assert not (qt and read)
    case, recipes = QT._case() if qt else N._observed_root_case(read=read)
    selector = copy.deepcopy(selector or {"format": "anyuse.rea-ax-window-selector/1",
        "role": "AXWindow", "subrole": None, "title": None, "geometry": {"kind": "full_display"}})
    config = {"identity": {"pid": 321, "executable": N.MAIN},
        "target": {"bundle": "/fixture/Path.app", "backend": "frida"},
        "adapter": {"sha256": "1" * 64, "source_sha256": "2" * 64,
                    "manifest": {"sha256": "3" * 64}, "build": {"interlock_protocol": RP.PROTOCOL}},
        "request": {"operations": copy.deepcopy(case["operations"])},
        "native_resources": copy.deepcopy(case["resources"]), "compiler_provenance": {}, "deadline_ms": 10000}
    spec = recipes[0]["root"]["lifecycle"]
    capability = "ax_owned_visible_readonly/1"
    binding = RP.make_binding(config, run_nonce="4" * 64, observer_capability=capability,
                              observer_selector=selector, observer_spec=spec)
    private = {"format": RP.PROTOCOL, "binding": binding, "binding_sha256": RP.digest(binding),
        "worker_pid": 654, "worker_nonce": "5" * 64, "observer_selector": selector,
        "observer_spec": copy.deepcopy(spec), "request_canonical_json": RP.canonical(config["request"]),
        "observer_canonical_json": RP.canonical(RP.observer_descriptor(config, observer_capability=capability,
            observer_selector=selector, observer_spec=spec)), "permit_wait_ms": 5000}
    pin = lambda name: {"file": "/fixture/" + name + ".json", "sha256": "6" * 64, "bytes": 12}
    frame = {"format": RP.FRAME, "kind": "decision", "binding": copy.deepcopy(binding),
        "worker_pid": private["worker_pid"], "worker_nonce": private["worker_nonce"],
        "selection": {"generation": 1, "owner_thread_id": 100}, "decision": "allow", "ordinal": 1,
        "setup": pin("setup"), "before": pin("before"), "checkpoint": pin("checkpoint"), "reason": None}
    RP.validate_frame(frame)
    case.update(permit_config=private, permit_frame=frame, permit_target=config["target"],
                permit_compiler=config["compiler_provenance"])
    return case, recipes


def _replace_once(source, old, new):
    assert source.count(old) == 1, old[:180]
    return source.replace(old, new, 1)


def _fixture(*, qt):
    source = QT._fixture() if qt else N._FIXTURE
    source = _replace_once(source, "let context;\nfunction send(event)", r"""
const permitReceivers=new Map(),permitPosts=[],permitDelivered=[],permitSnapshots=[],rpcFailures=[];
let permitObservationExpired=false,permitClosedProbe=null;
function recv(type,callback){
 if(typeof type!=='string'||typeof callback!=='function')throw Error('inert recv contract');
 const rows=permitReceivers.get(type)||[];rows.push(callback);permitReceivers.set(type,rows);
 return{wait(){throw Error('private permit must not block with recv.wait');}};
}
function permitPost(type,payload){
 const message={type,payload:JSON.parse(JSON.stringify(payload))};permitPosts.push(message);
 const rows=permitReceivers.get(type)||[],callback=rows.shift();
 if(!callback)throw Error('inert post has no genuine registered receiver');
 permitDelivered.push(type);callback(message);
}
function permitAbort(reason='inert parent EOF'){
 permitPost('anyuse.rea-phase-permit-abort',{binding_sha256:data.permit_config.binding_sha256,
  worker_nonce:data.permit_config.worker_nonce,reason});
}
function permitDecision(selection){
 const frame=JSON.parse(JSON.stringify(data.permit_frame));
 if(!data.permit_fixed_selection)frame.selection=selection;
 return frame;
}
function permitEvent(event){
 if(data.permit_config===undefined)return;
 const mode=data.permit_mode;
 if(event.event==='lifecycle-observer-ready'&&mode==='early_allow')
  permitPost('anyuse.rea-phase-permit',permitDecision({generation:1,owner_thread_id:100}));
 if(event.event==='native-invoke-start'&&mode==='abort_after_acquire')permitAbort();
 if(event.event!=='phase-permit-needed')return;
 permitSnapshots.push({rootCalls,mutatorCalls,consumerCalls,borrowedReads:borrowedTrace.length,
  attached:[...listeners.values()].filter(row=>!row.detached).length,selection:event.selection});
 if(mode==='missing')return;
 if(mode==='abort'){permitAbort();return;}
 if(mode==='destroy_selected')death(activeRoot);
 const frame=permitDecision(event.selection);
 if(mode==='deny'){frame.decision='deny';frame.reason='inert setup failed';
  frame.setup=null;frame.before=null;frame.checkpoint=null;}
 permitPost('anyuse.rea-phase-permit',frame);
 if(mode==='allow_then_abort')permitAbort();
}
function permitNativeEntry(){if(data.permit_mode==='late_abort')permitAbort();}
let context;
function send(event)""")
    source = _replace_once(source, " events.push(JSON.parse(JSON.stringify(event)));",
                           " events.push(JSON.parse(JSON.stringify(event)));permitEvent(event);")
    source = _replace_once(source, "rpc:{exports:{}},send,", "rpc:{exports:{}},send,recv,")
    source = _replace_once(source, "return;naturalLifecycleEvents();",
                           "return;if(!permitObservationExpired){permitObservationExpired=true;naturalLifecycleEvents();}")
    count = 2 if qt else 1
    native = "consumerCalls++;consumed.push(values[0].address);"
    assert source.count(native) == count
    source = source.replace(native, native + "permitNativeEntry();")
    source = _replace_once(source,
        "function payload(){return{request:{operations:data.operations},native_resources:data.resources,deadline_ms:data.deadline_ms??(data.lifecycle_root?10000:4000),\n pid:321,bundle:'/fixture/Path.app',executable:main};}",
        "function payload(){const value={request:{operations:data.operations},native_resources:data.resources,"
        "deadline_ms:data.deadline_ms??(data.lifecycle_root?10000:4000),pid:321,bundle:'/fixture/Path.app',executable:main};"
        "if(data.permit_config!==undefined){value.pre_actuation_ack=data.permit_config;value.target=data.permit_target;"
        "if(!data.permit_omit_compiler)value.compiler_provenance=data.permit_compiler;}return value;}")
    source = _replace_once(source, " const results=[await context.rpc.exports.run(payload())];",
        " let first;try{first=await context.rpc.exports.run(payload());}catch(error){"
        "rpcFailures.push({name:String(error.name),message:String(error.message)});first=null;}const results=[first];"
        "if(data.probe_closed){try{context.phaseProbe();permitClosedProbe='incorrectly admitted';}"
        "catch(error){permitClosedProbe=String(error.message);}}")
    source = _replace_once(source, "  calls,consumed,events,memoryTrace,borrowedTrace,interventions,discarded,hookLog,",
        "  calls,consumed,events,memoryTrace,borrowedTrace,interventions,discarded,hookLog,"
        "permitPosts,permitDelivered,permitSnapshots,permitClosedProbe,rpcFailures,")
    return source


def _run(case, mode="allow", *, scenario="stable", source_copy=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("whole-adapter permit checks require the guest's existing Node")
    source = Path(B.__file__).resolve(strict=True).with_name("rea_runtime_agent.js").read_text()
    source = _replace_once(source, "import ObjC from 'frida-objc-bridge';", "")
    for old, new in (source_copy or {}).items():
        source = _replace_once(source, old, new)
    data = {**copy.deepcopy(case), "source": source, "scenario": scenario,
            "permit_mode": mode, "repeat": False}
    process = subprocess.run([node, "-e", _fixture(qt=bool(case.get("qt_moc")))],
        input=json.dumps(data, ensure_ascii=False, allow_nan=False), capture_output=True,
        text=True, timeout=20, check=False)
    assert process.returncode == 0, process.stderr
    output = json.loads(process.stdout)
    # Every actual returned private journal, including failure/partial journals,
    # passes the real shared pure validator. No idealized replacement receipt.
    for result in output["results"]:
        if result is not None and "pre_actuation_ack" in result:
            assert RP.validate_agent(result["pre_actuation_ack"]) == result["pre_actuation_ack"]
    return output


def _refused(output, *, calls=0):
    result = output["results"][0]
    assert not output["rpcFailures"]
    assert result["status"] == "incomplete" and result["error"] and result["handles_expired"]
    assert result["native_calls"] == result["invocations"] == output["consumerCalls"] == calls
    if "pre_actuation_ack" in result:
        assert result["pre_actuation_ack"]["closed"] and result["pre_actuation_ack"]["error"]
    return result


@pytest.mark.parametrize("qt", [False, True])
def test_private_positive_permit_waits_without_receiver_and_commits_once_then_closes(qt):
    case, recipes = _case(qt=qt)
    saved = copy.deepcopy(case)
    output = _run(case)
    assert case == saved and not output["rpcFailures"]
    result = N._checked_observer_result(output, recipes)
    if qt:
        Q.validate_row(result["operations"][0], case["operations"][0])
        assert len(output["qtCalls"]) == 1 and output["qtCalls"][0]["owned8"]
    agent = result["pre_actuation_ack"]
    assert agent["state"] == "consumed" and agent["permit_consumed"] and agent["native_entry"] and agent["closed"]
    assert agent["error"] is None
    assert [row["kind"] for row in agent["events"]] == ["prepared", "waiting", "permit_accepted",
        "permit_consumed", "native_entry", "native_returned", "closed"]
    assert output["permitDelivered"] == ["anyuse.rea-phase-permit"]
    assert len(output["permitPosts"]) == 1
    frame = RP.validate_frame(output["permitPosts"][0]["payload"])
    assert agent["binding_sha256"] == RP.digest(case["permit_config"]["binding"])
    assert agent["decision_sha256"] == RP.digest(frame)
    assert agent["selection"] == frame["selection"] == {"generation": 1, "owner_thread_id": 100}
    assert output["permitSnapshots"] == [{"rootCalls": 0, "mutatorCalls": 0, "consumerCalls": 0,
        "borrowedReads": 0, "attached": 2, "selection": {"generation": 1, "owner_thread_id": 100}}]
    assert result["helper_load_attempts"] == result["callable_resolutions"] == 0


def test_exact_python_canonical_observer_string_preserves_float_values_without_encoder_assumptions():
    selector = {"format": "anyuse.rea-ax-window-selector/1", "role": "AXWindow", "subrole": None,
        "title": "Ångström 🧪", "geometry": {"kind": "exact", "position": [1.0, 2.5], "size": [900.0, 700.0]}}
    case, recipes = _case(selector=selector)
    assert '900.0' in case["permit_config"]["observer_canonical_json"]
    N._checked_observer_result(_run(case), recipes)


def test_long_literal_ascii_pin_uses_actual_frame_bytes_instead_of_a_conservative_multiplier():
    case, recipes = _case()
    frame = case["permit_frame"]
    frame["setup"]["file"] = "/fixture/" + "a" * 3000 + ".json"
    RP.validate_frame(frame)
    assert len(RP.canonical(frame)) * 3 > 8192
    assert len(RP.canonical(frame).encode("utf-8")) <= 8192
    N._checked_observer_result(_run(case), recipes)


@pytest.mark.parametrize("character", ["é", "🧪"])
@pytest.mark.parametrize("size", [8192, 8193])
def test_multibyte_frame_boundary_is_exact_and_shared_with_the_real_frame_validator(character, size):
    case, recipes = _case()
    frame = case["permit_frame"]
    frame["setup"]["file"] = "/fixture/"
    available = size - len(RP.canonical(frame).encode("utf-8"))
    count, extra = divmod(available, len(character.encode("utf-8")))
    frame["setup"]["file"] += character * count + "x" * extra
    assert len(frame["setup"]["file"]) <= 4096
    assert len(RP.canonical(frame).encode("utf-8")) == size
    if size == 8192:
        RP.validate_frame(frame)
        output = _run(case)
        N._checked_observer_result(output, recipes)
        assert output["results"][0]["pre_actuation_ack"]["decision_sha256"] == RP.digest(frame)
    else:
        with pytest.raises(ValueError): RP.validate_frame(frame)
        _refused(_run(case))


@pytest.mark.parametrize("mode,state", [("deny", "denied"), ("missing", "expired"),
    ("abort", "denied"), ("allow_then_abort", "denied"), ("early_allow", "failed"),
    ("abort_after_acquire", "denied")])
def test_absent_negative_early_or_parent_loss_never_enters_native_work(mode, state):
    case, _ = _case()
    output = _run(case, mode)
    result = _refused(output)
    agent = result["pre_actuation_ack"]
    assert agent["state"] == state and not agent["permit_consumed"] and not agent["native_entry"]
    assert len([row for row in output["hookLog"] if row["kind"] == "detach"]) == 2
    if mode == "missing":
        assert not output["permitPosts"] and output["permitSnapshots"][0]["borrowedReads"] == 0


@pytest.mark.parametrize("field,value", [("worker_pid", 655), ("worker_nonce", "9" * 64),
    ("ordinal", 2), ("ordinal", True), ("format", "old/1"), ("kind", "ready"),
    ("setup", None), ("before", None), ("checkpoint", None), ("reason", "not null"),
    ("unknown", 1)])
def test_wrong_closed_decision_binding_shape_or_phase_pins_refuses_before_native_entry(field, value):
    case, _ = _case()
    case["permit_frame"][field] = value
    _refused(_run(case))


def test_well_shaped_decision_for_another_run_cannot_be_adopted():
    case, _ = _case()
    case["permit_frame"]["binding"]["run_nonce"] = "9" * 64
    RP.validate_frame(case["permit_frame"])
    _refused(_run(case))


def test_read_only_observed_operation_cannot_use_the_explicit_invocation_permit():
    case, _ = _case(read=True)
    output = _run(case)
    _refused(output)
    assert not output["hookLog"] and not output["borrowedTrace"] and not output["permitPosts"]


@pytest.mark.parametrize("selection", [{"generation": 2, "owner_thread_id": 100},
    {"generation": 1, "owner_thread_id": 101}, {"generation": True, "owner_thread_id": 100}, None])
def test_decision_cannot_adopt_another_selected_generation_or_owner(selection):
    case, _ = _case()
    case["permit_fixed_selection"] = True
    case["permit_frame"]["selection"] = selection
    _refused(_run(case))


def test_same_address_new_generation_remains_bound_to_actual_selected_generation():
    case, recipes = _case()
    output = _run(case, scenario="lifecycle_witnessed_reuse")
    result = N._checked_observer_result(output, recipes, generation=2)
    assert result["pre_actuation_ack"]["selection"]["generation"] == 2
    assert output["permitPosts"][0]["payload"]["selection"]["generation"] == 2


@pytest.mark.parametrize("fault", ["binding", "request", "resources", "target", "compiler", "omit_compiler", "observer"])
def test_private_hashes_are_compared_to_actual_payload_before_hooks_or_receiver_reads(fault):
    case, _ = _case()
    if fault == "binding":
        case["permit_config"]["binding_sha256"] = "8" * 64
    elif fault == "request":
        case["permit_config"]["request_canonical_json"] = RP.canonical({"operations": []})
    elif fault == "resources":
        case["resources"][N.MAIN]["sha256"] = "8" * 64
    elif fault == "target":
        case["permit_target"]["bundle"] = "/fixture/Other.app"
    elif fault == "compiler":
        case["permit_compiler"] = {"unexpected": True}
    elif fault == "omit_compiler":
        case["permit_omit_compiler"] = True
    else:
        case["permit_config"]["observer_canonical_json"] = "{}"
    output = _run(case)
    _refused(output)
    assert not output["hookLog"] and not output["permitSnapshots"] and not output["borrowedTrace"]


@pytest.mark.parametrize("mode,scenario", [("destroy_selected", "stable"), ("allow", "lifecycle_selected_death"),
    ("allow", "lifecycle_selected_new_birth"), ("allow", "lifecycle_wrong_terminal_thread")])
def test_live_hooks_and_final_owner_scope_can_still_refuse_after_positive_decision(mode, scenario):
    case, _ = _case()
    output = _run(case, mode, scenario=scenario)
    result = _refused(output)
    assert result["pre_actuation_ack"]["decision_sha256"] is not None
    assert not result["pre_actuation_ack"]["permit_consumed"]
    assert result["lifecycle_observer"]["tokens_expired"]


@pytest.mark.parametrize("qt", [False, True])
def test_native_throw_retains_one_attempt_without_return_event_or_retry(qt):
    case, _ = _case(qt=qt)
    output = _run(case, scenario="qt_call_throw" if qt else "consumer_fault")
    result = _refused(output, calls=1)
    agent = result["pre_actuation_ack"]
    assert agent["state"] == "consumed" and agent["permit_consumed"] and agent["native_entry"]
    assert "native_returned" not in [row["kind"] for row in agent["events"]]
    assert result["native_state_uncertain"] and len(output["permitPosts"]) == 1


def test_parent_loss_after_commit_is_uncertain_one_attempt_and_never_claims_atomic_cancellation():
    case, _ = _case()
    output = _run(case, "late_abort")
    result = _refused(output, calls=1)
    agent = result["pre_actuation_ack"]
    assert agent["state"] == "consumed" and agent["permit_consumed"] and agent["native_entry"]
    assert agent["error"]["type"] == "PhasePermitLateAbort" and result["native_state_uncertain"]
    assert [row["kind"] for row in agent["events"]][-3:] == ["late_abort", "native_returned", "closed"]


def test_last_gate_rechecks_the_complete_request_after_existing_path_checks_before_counters():
    case, _ = _case()
    needle = "if (phasePermit !== null) phasePermit.consume(pathCall, pathInvocation, rootCapture);"
    replacement = "request.extra = true; " + needle
    result = _refused(_run(case, source_copy={needle: replacement}))
    assert "consume" in [row["kind"] for row in result["lifecycle_observer"]["events"]]
    assert not result["pre_actuation_ack"]["permit_consumed"]


def test_closed_one_use_permit_cannot_be_called_again_from_a_stale_inert_reference():
    case, recipes = _case()
    case["probe_closed"] = True
    needle = "if (phasePermit !== null) phasePermit.consume(pathCall, pathInvocation, rootCapture);"
    replacement = "globalThis.phaseProbe = () => phasePermit.consume(pathCall, pathInvocation, rootCapture); " + needle
    output = _run(case, source_copy={needle: replacement})
    N._checked_observer_result(output, recipes)
    assert output["permitClosedProbe"] and output["permitClosedProbe"] != "incorrectly admitted"
    assert output["consumerCalls"] == 1


@pytest.mark.parametrize("qt", [False, True])
def test_default_false_path_keeps_old_observed_transaction_without_private_summary_or_messages(qt):
    case, recipes = _case(qt=qt)
    for key in ("permit_config", "permit_frame", "permit_target", "permit_compiler"):
        del case[key]
    output = _run(case)
    result = N._checked_observer_result(output, recipes)
    assert "pre_actuation_ack" not in result and not output["permitPosts"] and not output["permitSnapshots"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
