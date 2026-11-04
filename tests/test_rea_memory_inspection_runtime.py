"""Execute the actual fixed adapter in an inert Node runtime; VM runner only."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from anyuse.execution import rea_memory_inspection as MI


SOURCE = Path(__file__).resolve().parent.parent / "execution/rea_runtime_agent.js"
MODULE = {"path": "/fixture/Original", "sha256": "a" * 64}


def ranges(alias="maps", limit=8, *, module=False):
    return {"op": "memory_ranges", "scope": {"kind": "module", "module": MODULE} if module else {"kind": "all"},
            "limit": limit, "as": alias}


def span(alias="slot", rva="0x100", size=8):
    return {"op": "memory_range", "module": MODULE, "rva": rva, "bytes": size, "as": alias}


def read(source=None, *, offset=0, size=8):
    return {"op": "memory_bytes", "from": {"span": "slot"} if source is None else source, "offset": offset, "bytes": size}


def follow(source=None, *, offset=0, size=16, alias="object"):
    return {"op": "memory_pointer_range", "from": {"span": "slot"} if source is None else source,
            "offset": offset, "pointee_bytes": size, "as": alias}


FAKE = r"""
const vm = require('vm');
const events = [], reads = [], chargeAtIssue = [];
const mask = (1n << 64n) - 1n;
let sandbox, readHook = null, findHook = null, moduleHook = null, enumHook = null, moduleRangeHook = null;
const cells = new Map();
class Pointer {
  constructor(value) { this.value = (value instanceof Pointer ? value.value : BigInt(value)) & mask; }
  add(n) { return new Pointer(this.value + BigInt(n)); }
  sub(n) { return new Pointer(this.value - (n instanceof Pointer ? n.value : BigInt(n))); }
  compare(other) { const n = ptr(other).value; return this.value < n ? -1 : this.value > n ? 1 : 0; }
  isNull() { return this.value === 0n; }
  toString() { return '0x' + this.value.toString(16); }
  readVolatile(n) {
    reads.push({address: this.toString(), bytes: n});
    const j = sandbox.__memoryJournal;
    const r = j.attempts[j.attempts.length - 1].read;
    chargeAtIssue.push({charged: j.charged_bytes, requested: r.requested_bytes, issued: r.issued});
    if (!r.issued || j.charged_bytes < n) throw Error('read issued before charging');
    return readHook ? readHook(this, n) : bufferAt(this, n);
  }
}
for (const method of ['readPointer','readByteArray','readU8','readU16','readU32','readU64','readUtf8String',
                       'writePointer','writeByteArray','writeU8','writeU32','strip']) {
  Pointer.prototype[method] = function () { events.push('FORBIDDEN:' + method); throw Error('forbidden access ' + method); };
}
function ptr(value) { return value instanceof Pointer ? value : new Pointer(value); }
function bytesBuffer(values) { return Uint8Array.from(values).buffer; }
function bufferAt(address, n) {
  return bytesBuffer(Array.from({length:n}, (_, i) => {
    const position = address.value + BigInt(i);
    return cells.has(position.toString()) ? cells.get(position.toString()) : Number(position & 255n);
  }));
}
function writeHex(address, value) {
  const at = ptr(address).value;
  for (let i = 0; i < value.length; i += 2) cells.set((at + BigInt(i / 2)).toString(), parseInt(value.slice(i, i + 2), 16));
}
let mappings = [
  {base:ptr('0x1000'),size:0x4000,protection:'rw-',file:{path:'/fixture/Original',offset:0,size:0x4000}},
  {base:ptr('0x8000'),size:0x4000,protection:'r--'}
];
let moduleRanges = [{base:ptr('0x1800'),size:0x1000,protection:'rw-',file:{path:'/fixture/Original',offset:0x800,size:0x1000}}];
let modules = [{path:'/fixture/Original',base:ptr('0x1800'),size:0x1000,
  enumerateRanges(protection) {
    events.push('module_ranges:' + protection);
    if (protection !== 'r--') throw Error('wrong module range query');
    return moduleRangeHook ? moduleRangeHook() : moduleRanges;
  }}];
const forbidden = name => new Proxy(function () { events.push('FORBIDDEN:' + name); throw Error('forbidden ' + name); }, {
  get(_target, key) { events.push('FORBIDDEN:' + name + '.' + String(key)); throw Error('forbidden ' + name); }
});
const Process = {
  id:42,platform:'darwin',arch:'arm64',pointerSize:8,mainModule:{path:'/fixture/App'},
  enumerateModules() { events.push('modules'); return moduleHook ? moduleHook() : modules; },
  enumerateRanges(options) {
    events.push('ranges');
    if (options.protection !== 'r--' || options.coalesce !== false) throw Error('wrong range inventory request');
    return enumHook ? enumHook() : mappings;
  },
  findRangeByAddress(address) {
    events.push('find:' + address.toString());
    if (findHook) return findHook(address);
    return mappings.find(row => address.compare(row.base) >= 0 && address.compare(row.base.add(row.size)) < 0) || null;
  }
};
sandbox = {rpc:{exports:{}}, Process, ptr, ArrayBuffer, Uint8Array,
  ObjC:forbidden('ObjC'), Swift:forbidden('Swift'), Memory:forbidden('Memory'),
  NativeFunction:forbidden('NativeFunction'), Interceptor:forbidden('Interceptor'), ModuleMap:forbidden('ModuleMap'),
  send:forbidden('send'), setTimeout:forbidden('setTimeout')};
vm.createContext(sandbox);
// Observe the public result object being built, without modifying its source.
// The reader asserts the counters/issued bit ALREADY exist at API entry.
vm.runInContext(`Object.defineProperty(Object.prototype, 'memory_inspection', {
  configurable:true, set(value) { globalThis.__memoryJournal=value;
    Object.defineProperty(this,'memory_inspection',{value,enumerable:true,writable:true,configurable:true}); }
})`, sandbox);
"""


def run_adapter(operations, *, setup="", again=None):
    """Only the trusted frozen adapter source is evaluated; no app or VM IO."""
    node = os.environ.get("ANYUSE_REA_TEST_NODE") or shutil.which("node")
    assert node, "existing Node is required to execute the actual adapter tests"
    source = SOURCE.read_text()
    assert source.count("import ObjC from 'frida-objc-bridge';") == 1
    source = source.replace("import ObjC from 'frida-objc-bridge';", "")
    payload = {"pid": 42, "executable": "/fixture/App", "deadline_ms": 4_000_000_000_000,
               "request": {"operations": operations},
               "native_resources": {MODULE["path"]: {"file": MODULE["path"], "sha256": MODULE["sha256"]}}}
    script = (FAKE + "\n" + setup + "\n" + "vm.runInContext(" + json.dumps(source) +
              ", sandbox, {filename:'actual-rea-runtime-agent.js', "
              "importModuleDynamically(){ events.push('FORBIDDEN:import'); throw Error('unexpected dependency initialization'); }});\n")
    script += "(async()=>{const first=await sandbox.rpc.exports.run(" + json.dumps(payload) + ");\n"
    if again is not None:
        second = {**payload, "request": {"operations": again}}
        script += "const second=await sandbox.rpc.exports.run(" + json.dumps(second) + ");\n"
    else:
        script += "const second=null;\n"
    script += "process.stdout.write(JSON.stringify({first,second,events,reads,chargeAtIssue}));})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});\n"
    completed = subprocess.run([node, "-"], input=script, text=True, capture_output=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert not any(event.startswith("FORBIDDEN:") for event in output["events"]), output
    observations = [(operations, output["first"])]
    if output["second"] is not None:
        observations.append((again, output["second"]))
    for requested, result in observations:
        # Exercise the real pure boundary on EVERY actual returned JS journal,
        # including failures. Only requests independently rejected by that real
        # validator can omit result validation; there are no idealized fixtures.
        expected = {"operations": requested}
        try:
            MI.validate_request(expected)
        except ValueError:
            assert result["status"] == "incomplete" and result["operations"] == []
        else:
            assert MI.validate_result(expected, result) == result["memory_inspection"]
        assert result["handles_expired"] is True and result["memory_inspection"]["aliases_expired"] is True
        assert result["queue"] == "runtime_inspection"
        assert all(result[key] == 0 for key in ("native_calls", "invocations", "helper_load_attempts", "callable_resolutions"))
        assert result["helper_residency"] == [] and result["native_state_uncertain"] is False
        journal = result["memory_inspection"]
        assert journal["format"] == "anyuse.rea-memory-inspection/2"
        assert journal["raw_observation_only"] is True and journal["range_inventory_materialization_bounded"] is False
        assert all(journal[key] is False for key in ("atomic_snapshot", "verified", "proof_created", "signature_inferred",
                                                   "type_admitted", "lifetime_admitted", "loaded_image_verified"))
        assert journal["loaded_binding"] == "path_metadata_only"
    return output


def journal(output):
    return output["first"]["memory_inspection"]


def test_actual_rpc_all_range_read_is_volatile_only_charged_before_issue_and_has_exact_prefix():
    output = run_adapter([ranges(), read({"ranges": "maps", "index": 0}, offset=0x20, size=16)])
    result, summary = output["first"], journal(output)
    assert result["status"] == "completed" and result["error"] is None
    assert result["operations"] == [{"index": 0, "op": "memory_ranges", "status": "ok", "inspection_index": 0},
                                    {"index": 1, "op": "memory_bytes", "status": "ok", "inspection_index": 1}]
    assert output["reads"] == [{"address": "0x1020", "bytes": 16}]
    assert output["chargeAtIssue"] == [{"charged": 16, "requested": 16, "issued": True}]
    assert summary["charged_bytes"] == summary["known_returned_bytes"] == summary["actual_read_bytes"] == 16
    assert summary["attempts"][1]["read"]["bytes_hex"] == "202122232425262728292a2b2c2d2e2f"
    assert summary["attempts"][1]["mapping_before"] != summary["attempts"][1]["mapping_after"]
    assert len(summary["mappings"]) == 4 and summary["attempts"][0]["matched"] == 2


def test_module_range_scope_clips_alias_and_retains_underlying_mapping_and_provenance_only():
    output = run_adapter([ranges(module=True), read({"ranges": "maps", "index": 0}, offset=0xff8)])
    first = journal(output)["attempts"][0]
    assert output["first"]["status"] == "completed"
    assert first["selections"] == [{"base": "0x1800", "bytes": 0x1000, "mapping_index": 0}]
    assert journal(output)["mappings"][0]["base"] == "0x1000" and journal(output)["mappings"][0]["bytes"] == 0x4000
    assert first["module_before"] == first["module_after"]
    assert journal(output)["attempts"][1]["module_before"] == first["module_before"]
    assert output["reads"] == [{"address": "0x27f8", "bytes": 8}]


def test_module_scoped_alias_cannot_reach_readable_neighboring_bytes():
    output = run_adapter([ranges(module=True), read({"ranges": "maps", "index": 0}, offset=0xffc)])
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert len(output["first"]["operations"]) == 1 and journal(output)["attempts"][-1]["read"] is None


def test_module_rva_span_then_pointer_follow_reads_slot_once_and_leaves_destination_unbound():
    output = run_adapter([span(), follow(), read({"span": "object"}, size=16)],
                         setup="writeHex('0x1900','0080000000000000');")
    summary = journal(output)
    assert output["first"]["status"] == "completed"
    assert output["reads"] == [{"address": "0x1900", "bytes": 8}, {"address": "0x8000", "bytes": 16}]
    pointer = summary["attempts"][1]
    assert pointer["read"]["bytes_hex"] == "0080000000000000"
    assert pointer["selections"] == [{"base": "0x8000", "bytes": 16, "mapping_index": pointer["pointee_mapping"]}]
    assert summary["attempts"][2]["module_before"] is None and summary["attempts"][2]["module_after"] is None
    assert summary["charged_bytes"] == summary["known_returned_bytes"] == summary["actual_read_bytes"] == 24


@pytest.mark.parametrize("mode", ["throw", "null", "short"])
def test_failed_volatile_read_retains_actual_disposition_and_never_retries_or_runs_next_op(mode):
    hooks = {"throw": "readHook=()=>{throw Error('racing inaccessible memory');};",
             "null": "readHook=()=>null;", "short": "readHook=()=>bytesBuffer([0xaa,0xbb,0xcc]);"}
    output = run_adapter([span(size=16), read(size=16), ranges("later")], setup=hooks[mode])
    summary, last = journal(output), journal(output)["attempts"][-1]
    assert output["first"]["status"] == "incomplete" and len(output["reads"]) == 1
    assert len(output["first"]["operations"]) == 1 and len(summary["attempts"]) == 2
    assert last["mapping_after"] is not None and last["module_after"] is not None
    assert summary["charged_bytes"] == 16 and summary["actual_read_bytes"] is None
    assert last["read"]["issued"] is True and last["read"]["unknown_partial"] is True
    if mode == "short":
        assert last["read"]["returned"] is True and last["read"]["bytes_hex"] == "aabbcc"
        assert last["read"]["returned_bytes"] == summary["known_returned_bytes"] == 3
    else:
        assert last["read"]["returned"] is False and last["read"]["bytes_hex"] is None
        assert last["read"]["returned_bytes"] is None and summary["known_returned_bytes"] == 0


def test_mapping_replacement_after_read_preserves_bytes_and_marks_incomplete():
    output = run_adapter([span(), read()], setup="readHook=(p,n)=>{const b=bufferAt(p,n);mappings[0]={...mappings[0],protection:'r-x'};return b;};")
    last = journal(output)["attempts"][-1]
    assert output["first"]["status"] == "incomplete"
    assert last["read"]["bytes_hex"] == "0001020304050607" and last["read"]["returned_bytes"] == 8
    assert {error["stage"] for error in last["errors"]} == {"mapping_after"}
    assert journal(output)["actual_read_bytes"] == 8


def test_mapping_and_module_after_audits_are_independent_when_read_throws():
    setup = """
readHook=()=>{throw Error('read failed');};
findHook=p=>{if(reads.length)throw Error('post mapping unavailable');return mappings[0];};
moduleHook=()=>{if(reads.length)throw Error('post module unavailable');return modules;};
"""
    output = run_adapter([span(), read()], setup=setup)
    last = journal(output)["attempts"][-1]
    assert {row["stage"] for row in last["errors"]} == {"read", "mapping_after", "module_after"}
    assert journal(output)["mappings"][last["mapping_after"]]["status"] == "error"
    assert last["read"]["bytes_hex"] is None and journal(output)["charged_bytes"] == 8


@pytest.mark.parametrize("slot", ["0000000000000000", "00800000000000ab", "00e0000000000000"])
def test_null_tagged_or_unmapped_pointer_bits_are_retained_without_masking_or_follow_read(slot):
    output = run_adapter([span(), follow(), read({"span": "object"})], setup="writeHex('0x1900'," + json.dumps(slot) + ");")
    last = journal(output)["attempts"][-1]
    assert output["first"]["status"] == "incomplete" and output["reads"] == [{"address": "0x1900", "bytes": 8}]
    assert last["read"]["bytes_hex"] == slot and last["selections"] == []
    assert journal(output)["charged_bytes"] == journal(output)["known_returned_bytes"] == 8
    if slot != "0000000000000000":
        assert journal(output)["mappings"][last["pointee_mapping"]]["status"] == "unmapped"


def test_destination_span_must_fit_one_current_mapping():
    output = run_adapter([span(), follow(size=32)], setup="writeHex('0x1900','f0bf000000000000');")
    assert output["first"]["status"] == "incomplete" and len(output["reads"]) == 1
    assert journal(output)["attempts"][-1]["selections"] == []


@pytest.mark.parametrize("operations", [
    [ranges(limit=128), read({"ranges": "maps", "index": 0})],
    [ranges(limit=1), *[read({"ranges": "maps", "index": 0}, size=16384) for _ in range(5)]],
    [ranges(limit=1), read({"ranges": "maps", "index": 0}, offset=2**53 - 1)],
    [read({"span": "future"}), span("future")],
    [ranges(), read({"span": "maps"})],
    [span(), span()],
    [ranges(), {"op": "modules"}],
    [ranges(), {"op": "invoke", "receiver": "arbitrary"}],
    [{**ranges(), "address": "0x1000"}],
    [{**ranges(), "limit": True}],
    [{**span(), "rva": "0x01"}],
    [{**span(), "module": {"path": "/fixture/../Original", "sha256": "a" * 64}}],
    [span("first"), {**span("second"), "module": {**MODULE, "sha256": "b" * 64}}],
    [ranges(), read({"ranges": "maps", "index": 0}, size=16385)],
    [ranges(), read({"ranges": "maps", "index": 0}, offset=-1)],
    [ranges(), follow({"ranges": "maps", "index": 0}, alias="maps")],
    [ranges("r" + str(i)) for i in range(17)],
])
def test_invalid_whole_batch_is_rejected_before_inventory_module_or_read_apis(operations):
    output = run_adapter(operations)
    assert output["first"]["status"] == "incomplete" and output["first"]["operations"] == []
    assert output["events"] == [] and output["reads"] == [] and journal(output)["attempts"] == []
    assert journal(output)["charged_bytes"] == 0


def test_full_byte_budget_is_usable_then_one_extra_pointer_reservation_is_rejected_before_io():
    ops = [ranges(limit=1), *[read({"ranges": "maps", "index": 0}, size=16384) for _ in range(4)]]
    success = run_adapter(ops)
    assert success["first"]["status"] == "completed" and journal(success)["charged_bytes"] == 65536
    denied = run_adapter([*ops, follow({"ranges": "maps", "index": 0})])
    assert denied["first"]["status"] == "incomplete" and denied["events"] == [] and denied["reads"] == []


def test_aliases_expire_after_success_and_failure_and_next_rpc_cannot_reuse_them():
    for ops, setup in (([span(), read()], ""), ([span(), read()], "readHook=()=>{throw Error('failed read');};")):
        output = run_adapter(ops, setup=setup, again=[read()])
        assert output["second"]["status"] == "incomplete" and output["second"]["operations"] == []
        assert output["second"]["memory_inspection"]["attempts"] == []
        assert len(output["reads"]) == 1


def test_current_range_list_index_cannot_adopt_unreturned_inventory_row():
    output = run_adapter([ranges(limit=4), read({"ranges": "maps", "index": 3})])
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert journal(output)["attempts"][0]["matched"] == 2


def test_mapping_change_before_use_prevents_read_without_discarding_original_selection():
    setup = "let enumerated=false;enumHook=()=>{enumerated=true;return mappings;};findHook=p=>enumerated?{...mappings[0],protection:'r-x'}:mappings[0];"
    output = run_adapter([ranges(limit=1), read({"ranges": "maps", "index": 0})], setup=setup)
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert journal(output)["attempts"][-1]["read"] is None


def test_changed_bound_module_metadata_prevents_later_alias_read():
    setup = "let moduleCalls=0;moduleHook=()=>++moduleCalls>2?[{...modules[0],size:0x1001}]:modules;"
    output = run_adapter([span(), read()], setup=setup)
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert journal(output)["attempts"][-1]["module_before"]["bytes"] == 0x1001


def test_native_address_wrap_is_refused_before_any_memory_read():
    output = run_adapter([ranges(limit=1)], setup="mappings=[{base:ptr('0xfffffffffffffff8'),size:16,protection:'r--'}];")
    assert output["first"]["status"] == "incomplete" and output["reads"] == []


def test_equal_prepost_mapping_metadata_does_not_claim_atomic_or_aba_safe_data():
    output = run_adapter([span(), read()], setup="readHook=(p,n)=>{writeHex(p,'8877665544332211');return bufferAt(p,n);};")
    assert output["first"]["status"] == "completed"
    assert journal(output)["attempts"][-1]["read"]["bytes_hex"] == "8877665544332211"
    assert journal(output)["atomic_snapshot"] is False and journal(output)["lifetime_admitted"] is False


def test_volatile_reader_unavailable_has_no_unsafe_fallback_or_charged_read():
    output = run_adapter([span(), read()], setup="Pointer.prototype.readVolatile=undefined;")
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert journal(output)["charged_bytes"] == 0 and journal(output)["attempts"][-1]["read"] is None


def test_deadline_after_issued_read_retains_bytes_and_independent_post_audit_failures():
    setup = "let clock=100;sandbox.Date={now:()=>clock};readHook=(p,n)=>{const b=bufferAt(p,n);clock=4000000000000;return b;};"
    output = run_adapter([span(), read(), ranges("never")], setup=setup)
    last = journal(output)["attempts"][-1]
    assert output["first"]["status"] == "incomplete" and len(output["reads"]) == 1
    assert len(output["first"]["operations"]) == 1 and len(journal(output)["attempts"]) == 2
    assert last["read"]["bytes_hex"] == "0001020304050607" and last["read"]["returned_bytes"] == 8
    assert {row["stage"] for row in last["errors"]} == {"mapping_after", "module_after", "deadline"}
    assert last["mapping_after"] is None and last["module_after"] is None
    assert journal(output)["charged_bytes"] == journal(output)["known_returned_bytes"] == 8


def test_expired_initial_deadline_has_no_attempts_or_requested_read_credit():
    output = run_adapter([span(), read()], setup="sandbox.Date={now:()=>4000000000000};")
    assert output["first"]["status"] == "incomplete" and output["events"] == [] and output["reads"] == []
    assert journal(output)["attempts"] == [] and journal(output)["charged_bytes"] == 0


@pytest.mark.parametrize("setup", ["Process.pointerSize=4;", "Process.arch='arm64e';", "Process.platform='linux';"])
def test_unsupported_runtime_never_reads_or_initializes_typed_bridges(setup):
    output = run_adapter([ranges()], setup=setup)
    assert output["first"]["status"] == "incomplete" and output["events"] == [] and output["reads"] == []


SPLIT_MODULE = """
modules[0].size=0x100;
moduleRanges=[{base:ptr('0x1800'),size:0x100,protection:'r-x'},
              {base:ptr('0x1920'),size:0x100,protection:'rw-'}];
"""


def test_data_range_beyond_reported_text_size_is_authorized_only_by_actual_module_ranges():
    output = run_adapter([span(rva="0x128"), read()], setup=SPLIT_MODULE)
    assert output["first"]["status"] == "completed"
    assert output["reads"] == [{"address": "0x1928", "bytes": 8}]
    stamp = journal(output)["attempts"][0]["module_before"]
    assert stamp["bytes"] == 0x100 and stamp["ranges"][1]["base"] == "0x1920"
    assert stamp["ranges"][1]["bytes"] == 0x100 and stamp["loaded_image_verified"] is False
    assert stamp == journal(output)["attempts"][0]["module_after"]


@pytest.mark.parametrize("rva,size", [("0x108", 8), ("0xf8", 48), ("0x218", 16)])
def test_readable_process_neighbors_and_holes_are_not_module_scope_authority(rva, size):
    output = run_adapter([span(rva=rva, size=size), read(size=size)], setup=SPLIT_MODULE)
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert journal(output)["attempts"][0]["mapping_before"] is None
    assert "must fit exactly one" in output["first"]["error"]["message"]


def test_module_inventory_keeps_disjoint_actual_ranges_and_truncates_clipped_spans():
    output = run_adapter([ranges(module=True, limit=1)], setup=SPLIT_MODULE)
    attempt = journal(output)["attempts"][0]
    assert output["first"]["status"] == "completed"
    assert attempt["matched"] == 2 and attempt["truncated"] is True
    assert attempt["selections"] == [{"base": "0x1800", "bytes": 0x100, "mapping_index": 0}]
    assert len(journal(output)["mappings"]) == 1


def test_module_inventory_data_alias_can_read_past_reported_size_without_adopting_hole():
    output = run_adapter([ranges(module=True, limit=2), read({"ranges": "maps", "index": 1}, offset=8)], setup=SPLIT_MODULE)
    assert output["first"]["status"] == "completed" and output["reads"] == [{"address": "0x1928", "bytes": 8}]
    spans = journal(output)["attempts"][0]["selections"]
    assert [(item["base"], item["bytes"]) for item in spans] == [("0x1800", 0x100), ("0x1920", 0x100)]


def test_module_ranges_are_sorted_before_stamp_comparison_without_coalescing_adjacent_entries():
    setup = SPLIT_MODULE + "moduleRanges.reverse();"
    output = run_adapter([span(rva="0x128"), read()], setup=setup)
    assert output["first"]["status"] == "completed"
    assert [row["base"] for row in journal(output)["attempts"][0]["module_before"]["ranges"]] == ["0x1800", "0x1920"]
    adjacent = "moduleRanges=[{base:ptr('0x1800'),size:0x100,protection:'r--'},{base:ptr('0x1900'),size:0x100,protection:'r--'}];"
    denied = run_adapter([span(rva="0xf8", size=16)], setup=adjacent)
    assert denied["first"]["status"] == "incomplete" and denied["reads"] == []


def test_module_coverage_replacement_before_alias_use_prevents_any_read():
    setup = SPLIT_MODULE + "let n=0;moduleRangeHook=()=>++n>2?moduleRanges.slice(0,1):moduleRanges;"
    output = run_adapter([span(rva="0x128"), read()], setup=setup)
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert len(journal(output)["attempts"][1]["module_before"]["ranges"]) == 1


def test_module_coverage_change_after_read_preserves_actual_bytes_but_refuses_completion():
    setup = SPLIT_MODULE + "readHook=(p,n)=>{const b=bufferAt(p,n);moduleRanges=moduleRanges.slice(0,1);return b;};"
    output = run_adapter([span(rva="0x128"), read()], setup=setup)
    last = journal(output)["attempts"][-1]
    assert output["first"]["status"] == "incomplete" and len(output["reads"]) == 1
    assert last["read"]["bytes_hex"] == "28292a2b2c2d2e2f"
    assert {item["stage"] for item in last["errors"]} == {"module_after"}


@pytest.mark.parametrize("setup", [
    "modules[0].enumerateRanges=undefined;",
    "moduleRangeHook=()=>{throw Error('module ranges unavailable');};",
    "moduleRanges=[];",
    "moduleRanges=Array.from({length:33},(_,i)=>({base:ptr(0x1800+i*16),size:8,protection:'r--'}));",
    "moduleRanges=[{base:ptr('0x1800'),size:0x100,protection:'r--'},{base:ptr('0x1880'),size:0x100,protection:'r--'}];",
    "moduleRanges=[{base:ptr('0x1700'),size:0x100,protection:'r--'}];",
    "moduleRanges=[{base:ptr('0x20001800'),size:8,protection:'r--'}];",
    "moduleRanges=[{base:ptr('0x1800'),size:0x100,protection:'-w-'}];",
    "moduleRanges=[{base:ptr('0xfffffffffffffff8'),size:16,protection:'r--'}];",
])
def test_unavailable_ambiguous_or_unbounded_module_coverage_has_no_disk_or_size_fallback(setup):
    output = run_adapter([span(), read()], setup=setup)
    assert output["first"]["status"] == "incomplete" and output["reads"] == []
    assert journal(output)["attempts"][0]["stage"] == "module_before"
    assert journal(output)["attempts"][0]["module_before"] is None
    assert not any(event.startswith("find:") or event == "ranges" for event in output["events"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
