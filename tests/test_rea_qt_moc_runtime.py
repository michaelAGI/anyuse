"""Offline executable Qt dispatch checks; every target and native call is inert.

The real fixed JS adapter runs in the existing NodeVM/Frida mock. No Frida
attachment, app action, original binary analysis or capability proof occurs.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_native_path as P
from anyuse.execution import rea_native_lifecycle as L
from anyuse.execution import rea_qt_moc as Q
import test_rea_native_path_runtime as N
from test_rea_native_receiver_contract import no_live_runtime


QT_CORE = "/fixture/QtCore.framework/Versions/A/QtCore"
CATALOG = "/fixture/qt-catalog.json"
SELECTION = "anyuse.rea-qt-moc-selection/1"
PIN = "anyuse.rea-qt-moc-runtime-pin/1"
OBSERVATION = "anyuse.rea-qt-moc-observation/1"


def _case(*, independent_dispatch_image=False, fresh_getter=False):
    case, recipes = N._borrowed_root_case() if fresh_getter else N._observed_root_case()
    recipe = recipes[0]
    module = copy.deepcopy(recipe["types"][0]["owner_image"])
    if independent_dispatch_image:
        # The fresh object's ctor/dtor/vptr image stays MAIN. Only its declared
        # Qt dispatcher is in a second independently pinned original image.
        module = {"path": "/fixture/QtSlots.dylib", "sha256": "7" * 64}
    layout = recipe["types"][0]
    layout["bytes"] = 8
    layout["fields"] = [{"name": "vptr", "type": "pointer", "offset": 0}]
    recipe["preconditions"] = []
    selection = {"format": SELECTION, "module": module,
        "qt_core": {"path": QT_CORE, "sha256": "6" * 64},
        "catalog": {"file": CATALOG, "sha256": "2" * 64},
        "code": {"file": "/fixture/qt-code.json", "sha256": "3" * 64},
        "table": {"file": "/fixture/qt-table.json", "sha256": "4" * 64},
        "qt_sources": {"file": "/fixture/qt-sources.json", "sha256": "5" * 64},
        "class_name": "Node", "record_offset": "0x680", "dispatcher_offset": "0x4c0",
        "local_index": 2, "signature": "flipPanel()"}
    evidence = copy.deepcopy(recipe["consumer"]["evidence"])
    recipe["consumer"] = {"kind": "qt_moc", "selection": copy.deepcopy(selection), "argument": 0,
        "pointee_type": recipe["result_type"], "role": "qobject_primary_base",
        "dispatch": "static_metacall", "this_adjustment": 0, "evidence": evidence}
    case["operations"] = [{"op": "qt_moc_invoke", "receiver": {"native_path": recipe},
                           "selection": copy.deepcopy(selection)}]
    N._refresh_contract_pins(case, recipes)
    # These are producer-supplied runtime pins, not an invented original Qt
    # catalog. The JS layer must still correlate every loaded byte and pointer.
    pointers = [{"slot_offset": "0x688", "target_offset": "0x700"},
                {"slot_offset": "0x690", "target_offset": "0x780"},
                {"slot_offset": "0x698", "target_offset": "0x4c0"}]
    ranges = []
    for offset, raw, kind in (("0x700", b"Node\0flipPanel()\0", "data"),
                              ("0x780", bytes.fromhex("0d000000020000000000000000000000"), "data"),
                              ("0x4c0", bytes.fromhex("1f2003d5"), "code")):
        ranges.append({"module": copy.deepcopy(module), "offset": offset, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "original_hex": raw.hex(), "kind": kind})
    runtime = {"format": PIN, "selection": copy.deepcopy(selection),
        "consumer": Q.consumer_contract(recipe["consumer"]),
        "method": {"class_name": "Node", "local_index": 2, "name": "flipPanel",
                   "signature": "flipPanel()", "access": "public", "kind": "slot",
                   "return_type": "void", "parameters": []},
        "record_pointers": pointers, "ranges": ranges}
    for key in ("catalog", "code", "table", "qt_sources"):
        claim = selection[key]
        case["resources"][claim["file"]] = {"file": claim["file"], "sha256": claim["sha256"], "bytes": 100}
    case["resources"][CATALOG]["qt_moc_dispatches"] = [runtime]
    main_pin = case["resources"][N.MAIN]
    # The inert source image expands the original const section, preserving its
    # exact file translation and non-overlap with the instruction section.
    for row in main_pin["native_path_data_sections"]:
        row["sections"][0]["bytes"] = 0x300
    if independent_dispatch_image:
        case["resources"][module["path"]] = copy.deepcopy(main_pin)
        case["resources"][module["path"]].update(file=module["path"], sha256=module["sha256"])
    case["resources"][QT_CORE] = copy.deepcopy(main_pin)
    case["resources"][QT_CORE].update(file=QT_CORE, sha256=selection["qt_core"]["sha256"])
    case.update(qt_moc=True, qt_config={"selection": copy.deepcopy(selection),
        "record_pointers": copy.deepcopy(pointers), "ranges": copy.deepcopy(ranges)})
    B.validate_request({"operations": case["operations"]})
    return case, recipes


def _replace_once(source, old, new):
    # Test-only mock composition, not a product hook or a substitute assertion.
    assert source.count(old) == 1, old[:160]
    return source.replace(old, new, 1)


def _fixture():
    source = N._FIXTURE
    source = _replace_once(source, "borrowedTrace.push({address,reader,after_intervention:interventions});",
                           "borrowedTrace.push({address,reader,after_intervention:interventions,after_qt_call:qtCalls.length});")
    source = _replace_once(source, "let context;\nfunction send(event)", r"""
const qtAllocations=[],qtCalls=[];
let qtDispatchModule=module,qtDispatchOriginalBase=codeBase,qtCoreModule=null;
const alloc=Memory.alloc;
Memory.alloc=function(n){
 if(data.qt_bad_alloc==='null'&&n===8)return new Pointer(0);
 const extra=data.qt_bad_alloc==='misaligned'&&n===8;
 const value=alloc(n+(extra?1:0)).add(extra?1:0);
 qtAllocations.push({address:value.address,bytes:n});return value;
};
Pointer.prototype.writePointer=function(value){w64(this.address,value instanceof Pointer?value.address:value);};
Pointer.prototype.writeU64=function(value){w64(this.address,value);};
if(data.qt_moc){
 const original=memory.get(codeBase);original.writeBigUInt64LE(0x300n,248);
 const independent=data.qt_config.selection.module.path!==main;
 qtDispatchOriginalBase=independent?0xd000:codeBase;
 const h=independent?Buffer.from(original):original;
 if(independent){
  memory.set(qtDispatchOriginalBase,h);
  qtDispatchModule={path:data.qt_config.selection.module.path,name:'QtSlots',
   base:new Pointer(qtDispatchOriginalBase),size:4096};
 }
 for(const row of data.qt_config.ranges)Buffer.from(row.original_hex,'hex').copy(h,Number(row.offset));
 for(const row of data.qt_config.record_pointers)
  w64(qtDispatchOriginalBase+Number(row.slot_offset),qtDispatchOriginalBase+Number(row.target_offset));
 const core=Buffer.from(h);memory.set(0xb000,core);
 qtCoreModule={path:data.qt_config.selection.qt_core.path,name:'QtCore',base:new Pointer(0xb000),size:4096};
 const enumerate=Process.enumerateModules,byAddress=Process.findModuleByAddress,range=Process.findRangeByAddress;
 Process.enumerateModules=()=>[...enumerate(),qtCoreModule,...(independent?[qtDispatchModule]:[])];
 Process.findModuleByAddress=p=>p.address>=qtCoreModule.base.address&&p.address<qtCoreModule.base.address+4096?qtCoreModule:
  independent&&p.address>=qtDispatchModule.base.address&&p.address<qtDispatchModule.base.address+4096
   ?qtDispatchModule:byAddress(p);
 Process.findRangeByAddress=p=>p.address>=qtCoreModule.base.address&&p.address<qtCoreModule.base.address+4096
  ?{base:qtCoreModule.base,size:4096,protection:'r-x'}:range(p);
 if(independent){
  const ordinary=Process.findRangeByAddress;
  Process.findRangeByAddress=p=>p.address>=qtDispatchModule.base.address&&p.address<qtDispatchModule.base.address+4096
   ?{base:qtDispatchModule.base,size:4096,protection:'r-x'}:ordinary(p);
 }
 if(data.qt_initial==='code')h[0x4c0]^=1;
 if(data.qt_initial==='data')h[0x780]^=1;
 if(data.qt_initial==='record_pointer')w64(codeBase+0x688,codeBase+0x708);
 if(data.qt_initial==='qt_core_uuid')core[40]^=1;
}
let context;
function send(event)""")
    source = _replace_once(source, " if(address.address===symbols.legacy)return values[0][0]+values[1];", r"""
 if(data.qt_moc&&address.address===qtDispatchOriginalBase+0x4c0){
  consumerCalls++;consumed.push(values[0].address);
  const argv=values[3],owned=argv instanceof Pointer&&qtAllocations.some(row=>row.address===argv.address&&row.bytes===8);
  const details={receiver:values[0].address,call:values[1],index:values[2],argv:argv.address,
   argv_nonnull:!argv.isNull(),owned8:owned,slot0:argv.readPointer().address,
   argument_types:_args,return_type:_ret};
  qtCalls.push(details);
  if(!details.argv_nonnull||!owned||details.slot0!==0||values.length!==4||values[1]!==0||values[2]!==2)
   throw Error('inert invalid Qt fixed dispatch arguments');
  if(data.scenario==='qt_call_throw')throw Error('inert Qt target fault');
  if(data.scenario==='qt_discard_after_call'){
   death(values[0].address);memory.delete(values[0].address);discarded.push(values[0].address);
  }
  return undefined;
 }
 if(address.address===symbols.legacy)return values[0][0]+values[1];""")
    source = _replace_once(source, " if(scenario==='lifecycle_rpc_reentry')", r"""
 if(data.qt_moc){
  if(scenario==='qt_code_drift')memory.get(codeBase)[0x4c0]^=1;
  if(scenario==='qt_data_drift')memory.get(codeBase)[0x780]^=1;
  if(scenario==='qt_record_pointer_drift')w64(codeBase+0x698,codeBase+0x4c4);
  if(scenario==='qt_catalog_drift')data.resources[data.qt_config.selection.catalog.file].sha256='f'.repeat(64);
  if(scenario==='qt_catalog_row_drift')data.resources[data.qt_config.selection.catalog.file].qt_moc_dispatches[0].method.local_index=3;
  if(scenario==='qt_selection_drift')data.operations[0].selection.local_index=3;
  if(scenario==='qt_argv_slot_drift'){
   const vector=qtAllocations.filter(row=>row.bytes===8).at(-1);
   if(!vector)throw Error('inert Qt vector was not prepared before observation');
   w64(vector.address,0x3000);
  }
  if(scenario==='qt_dispatch_base_drift'){
   if(qtDispatchModule===module)throw Error('inert relocation needs an independent dispatcher image');
   memory.set(0xe000,Buffer.from(memory.get(qtDispatchModule.base.address)));
   qtDispatchModule.base=new Pointer(0xe000);
   for(const row of data.qt_config.record_pointers)
    w64(0xe000+Number(row.slot_offset),0xe000+Number(row.target_offset));
  }
  if(scenario==='qt_core_base_drift'){
   memory.set(0xc000,Buffer.from(memory.get(qtCoreModule.base.address)));
   qtCoreModule.base=new Pointer(0xc000);
  }
 }
 if(scenario==='lifecycle_rpc_reentry')""")
    source = _replace_once(source,
        "Checksum:{compute:(algorithm,value)=>crypto.createHash(algorithm).update(value,'utf8').digest('hex')},",
        "Checksum:{compute:(algorithm,value)=>crypto.createHash(algorithm).update(typeof value==='string'?value:"
        "ArrayBuffer.isView(value)?Buffer.from(value.buffer,value.byteOffset,value.byteLength):Buffer.from(value)).digest('hex')},")
    source = _replace_once(source, "  calls,consumed,events,memoryTrace,borrowedTrace,interventions,discarded,hookLog,",
                           "  calls,consumed,events,memoryTrace,borrowedTrace,interventions,discarded,hookLog,qtCalls,qtAllocations,")
    return source


def _run(case, scenario="stable", *, repeat=False, source_copy=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("inert Qt fixture requires an installed Node")
    source = Path(B.__file__).with_name("rea_runtime_agent.js").read_text()
    source = _replace_once(source, "import ObjC from 'frida-objc-bridge';", "")
    for old, new in (source_copy or {}).items():
        source = _replace_once(source, old, new)
    value = {**copy.deepcopy(case), "source": source, "scenario": scenario, "repeat": repeat}
    result = subprocess.run([node, "-e", _fixture()], input=json.dumps(value, allow_nan=False),
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _refused(output):
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"]
    assert result["native_calls"] == result["invocations"] == 0
    assert result["handles_expired"] is True
    assert output["qtCalls"] == [] and output["consumerCalls"] == 0
    assert result["helper_load_attempts"] == result["callable_resolutions"] == 0


def test_fixed_private_argv_and_fresh_main_epoch_dispatch_once_without_pointer_escape():
    case, recipes = _case()
    output = _run(case)
    result = N._checked_observer_result(output, recipes)
    assert result["status"] == "completed" and result["error"] is None
    assert result["native_calls"] == result["invocations"] == output["consumerCalls"] == 1
    assert len(output["qtCalls"]) == 1 and output["calls"] == [0x14c0]
    actual = output["qtCalls"][0]
    assert actual["receiver"] == 0x3000 and actual["call"] == 0 and actual["index"] == 2
    assert actual["argv_nonnull"] and actual["owned8"] and actual["argv"] % 8 == 0 and actual["slot0"] == 0
    assert actual["return_type"] == "void" and actual["argument_types"] == ["pointer", "int32", "int32", "pointer"]
    row = result["operations"][0]
    assert row["value"] is None and row["return_type"] == "void"
    P.validate_observations(row["native_paths"], recipes, for_call=True)
    Q.validate_row(row, case["operations"][0])
    scope = row["native_paths"][0]["lifecycle_scope"]
    L.validate_result(result["lifecycle_observer"], recipes[0]["root"]["lifecycle"],
                      scope=scope, for_call=True, native_calls=1, invocations=1)
    assert row["native_paths"][0]["sealed"] is True and row["native_paths"][0]["dispatch_permit_consumed"] is True
    assert scope["acquire_epoch"] == scope["checked_epoch"] == 0
    observed = row["qt_moc"]
    assert observed["format"] == OBSERVATION and observed["selection"] == case["operations"][0]["selection"]
    assert observed["argv"] == {"bytes": 8, "slots": 1, "slot0": "null", "ownership": "private_no_escape",
                                 "expires": "call", "expired": True}
    assert observed["permit_consumed"] and observed["returned"] and observed["expires"] == "transaction"
    assert "pointer" not in observed["argv"] and "handle" not in observed["argv"]
    assert row["out_buffers"] == row["pointer_reads"] == row["swift_field_reads"] == row["callable_entries"] == []
    assert row["compiler_completion"] is None


@pytest.mark.parametrize("key,value", [
    ("argv", {"pointer": "0x9000"}), ("arguments", []), ("call", 1), ("local_index", 9), ("vector", [None]),
])
def test_external_requests_cannot_supply_qt_private_vector_or_dispatch_arguments(key, value):
    case, _ = _case()
    case["operations"][0][key] = value
    _refused(_run(case))


@pytest.mark.parametrize("fault", ["null", "misaligned"])
def test_invalid_private_allocator_pointer_refuses_before_root_acquisition(fault):
    case, _ = _case()
    case["qt_bad_alloc"] = fault
    output = _run(case)
    _refused(output)
    assert not output["hookLog"] and not any(row.get("event") == "native-path-resolved" for row in output["events"])


@pytest.mark.parametrize("fault", ["code", "data", "record_pointer", "qt_core_uuid"])
def test_initial_loaded_image_data_and_record_pointer_mismatches_refuse_before_acquisition(fault):
    case, _ = _case()
    case["qt_initial"] = fault
    output = _run(case)
    _refused(output)
    assert not any(row.get("event") == "native-path-resolved" for row in output["events"])


@pytest.mark.parametrize("scenario", [
    "qt_code_drift", "qt_data_drift", "qt_record_pointer_drift", "qt_catalog_drift",
    "qt_catalog_row_drift", "qt_selection_drift", "qt_argv_slot_drift", "qt_core_base_drift",
    "lifecycle_wrong_terminal_thread", "lifecycle_selected_death", "lifecycle_selected_new_birth",
])
def test_observation_window_drift_or_stale_owner_refuses_before_any_qt_dispatch(scenario):
    case, _ = _case()
    output = _run(case, scenario)
    _refused(output)
    observation = output["results"][0]["lifecycle_observer"]
    assert observation["status"] == "incomplete" and observation["tokens_expired"]
    assert not any(row["kind"] in {"acquire", "consume"} for row in observation["events"])


def test_independent_dispatcher_image_can_be_admitted_without_relocating_the_live_root():
    case, recipes = _case(independent_dispatch_image=True)
    output = _run(case)
    result = N._checked_observer_result(output, recipes)
    assert result["status"] == "completed" and len(output["qtCalls"]) == 1
    P.validate_observations(result["operations"][0]["native_paths"], recipes, for_call=True)
    Q.validate_row(result["operations"][0], case["operations"][0])
    assert output["calls"] == [0xd4c0]


def test_relocated_dispatcher_cannot_reuse_the_old_cached_callable_even_with_matching_fresh_bytes():
    case, _ = _case(independent_dispatch_image=True)
    output = _run(case, "qt_dispatch_base_drift")
    _refused(output)
    # Every new range/pointer is valid at the new module base, while the ctor/
    # dtor/vptr image remains unchanged. Refusal must preserve the prepared
    # callable's original location, rather than silently adopting this reload.
    observation = output["results"][0]["lifecycle_observer"]
    assert not any(row["kind"] in {"acquire", "consume"} for row in observation["events"])
    assert "callback location changed" in output["results"][0]["error"]["message"]


def test_qt_throw_is_one_real_attempt_with_uncertainty_no_retry_and_hook_cleanup():
    case, _ = _case()
    output = _run(case, "qt_call_throw")
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"]
    assert result["native_calls"] == result["invocations"] == 1
    assert len(output["qtCalls"]) == output["consumerCalls"] == 1
    assert result["native_state_uncertain"] and result["handles_expired"]
    assert result["lifecycle_observer"]["hooks_detached"] == 2
    assert result["lifecycle_observer"]["tokens_expired"]
    assert len([row for row in output["hookLog"] if row["kind"] == "detach"]) == 2


@pytest.mark.parametrize("scenario,installed,detached,calls", [
    ("lifecycle_install_failure", 1, 1, 0),
    ("lifecycle_install_flush_failure", 2, 2, 0),
    ("lifecycle_detach_failure", 2, 1, 1),
    ("lifecycle_cleanup_flush_failure", 2, 2, 1),
])
def test_qt_hook_failure_preserves_exact_dispatch_count_and_unclean_disposition(scenario, installed, detached, calls):
    case, _ = _case()
    output = _run(case, scenario)
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"] and result["handles_expired"] is True
    assert result["native_calls"] == result["invocations"] == output["consumerCalls"] == calls
    assert len(output["qtCalls"]) == calls
    assert result["helper_load_attempts"] == result["callable_resolutions"] == 0
    observation = result["lifecycle_observer"]
    assert observation["status"] == "incomplete" and observation["tokens_expired"] is True
    assert observation["hooks_requested"] == 2
    assert observation["hooks_installed"] == installed and observation["hooks_detached"] == detached
    assert all(row["address"] in {0x1500, 0x1540} for row in output["hookLog"] if "address" in row)
    if calls:
        assert result["native_state_uncertain"] is True


def test_qt_return_does_not_reread_a_revoked_or_freed_receiver():
    case, _ = _case()
    output = _run(case, "qt_discard_after_call")
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == result["invocations"] == 1
    assert output["discarded"] == [0x3000] and len(output["qtCalls"]) == 1
    assert result["lifecycle_observer"]["tokens_expired"] and result["handles_expired"]
    assert output["interventions"] == 0
    assert output["borrowedTrace"] and all(row["after_qt_call"] == 0 for row in output["borrowedTrace"])


def test_intervening_target_epoch_invalidates_qt_receiver_before_dispatch():
    case, _ = _case()
    needle = "const receiver = resolveNativePath(operation.receiver.native_path, {call: token, argument: 0}).pointer;"
    output = _run(case, source_copy={needle: needle + "\n        targetCall(() => inertIntervention(), []);"})
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"]
    assert output["interventions"] == 1 and output["qtCalls"] == []
    assert result["native_calls"] == 1 and result["invocations"] == 0
    assert result["handles_expired"] and result["lifecycle_observer"]["tokens_expired"]


def test_consumed_qt_permit_cannot_make_a_second_dispatch():
    case, _ = _case()
    needle = "targetCall(call.qtMoc.found.function_, arguments_, token);"
    output = _run(case, source_copy={needle: needle + "\n        " + needle})
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"]
    assert result["native_calls"] == result["invocations"] == 1
    assert len(output["qtCalls"]) == output["consumerCalls"] == 1
    assert result["handles_expired"] and result["lifecycle_observer"]["tokens_expired"]


def test_qt_private_vector_and_permit_expire_before_the_rpc_result_resolves():
    case, recipes = _case()
    needle = "targetCall(call.qtMoc.found.function_, arguments_, token);"
    # Deliberate test-only access to the private frame verifies disposal after
    # the real RPC resolves. The product source never exports this probe.
    probe = """globalThis.pathProbe = () => {
          if (call.qtMoc.vector !== null || call.qtMoc.expired !== true)
            throw Error('Qt private vector did not expire before RPC completion');
          return consumeNativePathPermits(token, call.qtMoc.found.function_, arguments_);
        };
        """
    output = _run(case, "stale_permit", source_copy={needle: probe + needle})
    N._checked_observer_result(output, recipes)
    assert len(output["qtCalls"]) == output["consumerCalls"] == 1
    assert "not current" in output["stale"]


def test_reentrant_rpc_cannot_adopt_the_private_qt_token_or_vector():
    case, _ = _case()
    output = _run(case, "lifecycle_rpc_reentry")
    assert output["results"][0]["status"] == "completed" and len(output["qtCalls"]) == 1
    assert "reentered" in output["reentrant"]


def test_fresh_transactions_use_distinct_private_vectors_and_private_generations():
    case, _ = _case()
    output = _run(case, repeat=True)
    assert [row["status"] for row in output["results"]] == ["completed", "completed"]
    assert [row["native_calls"] for row in output["results"]] == [1, 1]
    assert [row["receiver"] for row in output["qtCalls"]] == [0x3000, 0x5000]
    assert len({row["argv"] for row in output["qtCalls"]}) == 2
    assert [row["operations"][0]["native_paths"][0]["lifecycle_scope"]["generation"] for row in output["results"]] == [1, 1]


def test_existing_scalar_native_consumer_still_dispatches_without_qt_authority():
    case, _ = N._observed_root_case()
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == result["invocations"] == 1
    assert output["qtCalls"] == [] and result["operations"][0]["value"] == 64
    assert "qt_moc" not in result["operations"][0]


def test_old_native_scalar_tail_does_not_gain_a_qt_private_pointer_escape():
    case, recipes = N._observed_root_case()
    getter = {"kind": "native_function", "target": {"module": copy.deepcopy(recipes[0]["types"][0]["owner_image"]),
              "symbol": "root"}, "abi": "default", "arguments": [], "return_type": "pointer",
              "signature_evidence": copy.deepcopy(case["operations"][0]["signature_evidence"])}
    case["operations"][0]["arguments"][-1] = {"type": "pointer", "value": {"getter": getter}}
    recipes[0]["consumer"]["call"]["argument_types"][-1] = "pointer"
    N._refresh_contract_pins(case, recipes)
    output = _run(case)
    _refused(output)
    assert output["rootCalls"] == output["mutatorCalls"] == 0


def test_fresh_getter_qt_receiver_uses_existing_borrowed_scope_without_constructor_hooks():
    case, recipes = _case(fresh_getter=True)
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed", result.get("error")
    assert result["native_calls"] == 2 and result["invocations"] == 1
    assert len(output["qtCalls"]) == 1
    row = result["operations"][0]
    P.validate_observations(row["native_paths"], recipes, for_call=True)
    Q.validate_row(row, case["operations"][0])
    scope = row["native_paths"][0]["borrowed_scope"]
    assert scope["getter_epoch"] == scope["checked_epoch"] == 1
    assert scope["consumed"] is True and scope["expires"] == "use"
    assert "lifecycle_scope" not in row["native_paths"][0]
    assert row["qt_moc"]["argv"]["expired"] is True
    assert result["handles_expired"] is True


def test_fresh_getter_qt_receiver_refuses_an_intervening_target_call_before_dispatch():
    case, _ = _case(fresh_getter=True)
    needle = "const receiver = resolveNativePath(operation.receiver.native_path, {call: token, argument: 0}).pointer;"
    output = _run(case, source_copy={needle: needle + "\n        targetCall(() => inertIntervention(), []);"})
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"]
    assert output["interventions"] == 1 and output["qtCalls"] == []
    assert result["native_calls"] == 2 and result["invocations"] == 0
    assert result["handles_expired"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
