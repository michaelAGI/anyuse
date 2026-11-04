"""Pure raw-memory boundary/forgery tests; execute only in the VM runner.

These synthetic journals test independent rejection gates. Actual fixed-JS
outputs, including partial failures, are separately passed through this same
validator by test_rea_memory_inspection_runtime.py. No real app is accessed.
"""
import copy
import os

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_memory_inspection as MI
from anyuse.execution import rea_runtime_bridge as B


MEMORY_FILE = {"path": "/fixture/Original", "sha256": "a" * 64}


def _ranges(limit=2, alias="maps"):
    return {"op": "memory_ranges", "scope": {"kind": "all"}, "limit": limit, "as": alias}


def _bytes(source=None, count=8, offset=0):
    return {"op": "memory_bytes", "from": source or {"span": "child"}, "offset": offset, "bytes": count}


def _pointer(source=None, alias="child"):
    return {"op": "memory_pointer_range", "from": source or {"ranges": "maps", "index": 0},
            "offset": 0, "pointee_bytes": 16, "as": alias}


def _error(stage="read"):
    return {"stage": stage, "error": {"type": "Error", "message": "inert failure", "stack": None}}


def _mapping(base, at):
    return {"at_ms": at, "status": "mapped", "base": base, "bytes": 256,
            "protection": "rw-", "file": None, "error": None}


def _span(base, size, index): return {"base": base, "bytes": size, "mapping_index": index}


def _attempt(index, op, start):
    return {"index": index, "op": op, "status": "observed", "stage": "complete", "started_ms": start,
        "finished_ms": start + 10, "errors": [], "module_before": None, "module_after": None,
        "selections": [], "source": None, "mapping_before": None, "mapping_after": None,
        "pointee_mapping": None, "read": None, "matched": None, "truncated": None}


def _reading(value="0001020304050607"):
    return {"offset": 0, "requested_bytes": 8, "issued": True, "returned": True,
            "bytes_hex": value, "returned_bytes": len(value) // 2, "unknown_partial": False}


def _case():
    request = {"operations": [_ranges(), _pointer(), _bytes()]}
    maps = [_mapping("0x1000", 1001), _mapping("0x8000", 1002), _mapping("0x1000", 1011),
            _mapping("0x1000", 1012), _mapping("0x8000", 1013), _mapping("0x8000", 1021), _mapping("0x8000", 1022)]
    attempts = [_attempt(index, row["op"], 1000 + 10 * index) for index, row in enumerate(request["operations"])]
    attempts[0].update(selections=[_span("0x1000", 256, 0), _span("0x8000", 256, 1)], matched=2, truncated=False)
    attempts[1].update(source=_span("0x1000", 256, 0), mapping_before=2, mapping_after=3, pointee_mapping=4,
                       read=_reading("0080000000000000"), selections=[_span("0x8000", 16, 4)])
    attempts[2].update(source=_span("0x8000", 16, 4), mapping_before=5, mapping_after=6, read=_reading())
    summary = {"format": MI.FORMAT, "pointer_size": 8, "byte_order": "little", "loaded_binding": "path_metadata_only",
        "loaded_image_verified": False, "raw_observation_only": True, "atomic_snapshot": False, "verified": False,
        "proof_created": False, "signature_inferred": False, "type_admitted": False, "lifetime_admitted": False,
        "range_inventory_materialization_bounded": False, **MI.request_budget(request), "charged_bytes": 16,
        "known_returned_bytes": 16, "actual_read_bytes": 16, "aliases_expired": True, "mappings": maps, "attempts": attempts}
    result = {"status": "completed", "operations": [{"index": index, "op": row["op"], "status": "ok", "inspection_index": index}
        for index, row in enumerate(request["operations"])], "native_calls": 0, "invocations": 0, "helper_load_attempts": 0,
        "callable_resolutions": 0, "helper_residency": [], "error": None, "handles_expired": True,
        "queue": "runtime_inspection", "native_state_uncertain": False,
        "process": {"pid": 42, "executable": "/fixture/App.app/Contents/MacOS/App", "architecture": "arm64"},
        "memory_inspection": summary}
    return request, result


def _module_stamp():
    return {"path": MEMORY_FILE["path"], "base": "0x10000000", "bytes": 0x4a0000,
            "file": copy.deepcopy(MEMORY_FILE), "loaded_binding": "path_metadata_only", "loaded_image_verified": False,
            "ranges": [{"base": "0x10000000", "bytes": 0x4a0000, "protection": "r-x", "file": None},
                       {"base": "0x104b0000", "bytes": 0x10000, "protection": "rw-", "file": None}]}


def _module_case():
    """A retained DATA slot outside reported Module.size, inside actual attributed ranges."""
    _, result = _case()
    request = {"operations": [{"op": "memory_range", "module": copy.deepcopy(MEMORY_FILE),
                               "rva": "0x4bb398", "bytes": 8, "as": "slot"}, _bytes({"span": "slot"})]}
    module = _module_stamp()
    maps = [_mapping("0x104b0000", at) for at in (1001, 1011, 1012)]
    for mapping in maps: mapping["bytes"] = 0x10000
    attempts = [_attempt(index, row["op"], 1000 + 10 * index) for index, row in enumerate(request["operations"])]
    for attempt in attempts:
        attempt.update(module_before=copy.deepcopy(module), module_after=copy.deepcopy(module))
    attempts[0].update(mapping_before=0, selections=[_span("0x104bb398", 8, 0)])
    attempts[1].update(source=_span("0x104bb398", 8, 0), mapping_before=1, mapping_after=2, read=_reading())
    result["operations"] = [{"index": index, "op": row["op"], "status": "ok", "inspection_index": index}
                            for index, row in enumerate(request["operations"])]
    result["memory_inspection"].update(MI.request_budget(request), mappings=maps, attempts=attempts,
                                       charged_bytes=8, known_returned_bytes=8, actual_read_bytes=8)
    return request, result


def _module_inventory_case():
    """One actual process map has two module-attributed subranges with an intervening hole."""
    _, result = _case()
    request = {"operations": [{"op": "memory_ranges", "scope": {"kind": "module", "module": copy.deepcopy(MEMORY_FILE)},
                               "limit": 2, "as": "maps"}]}
    module = _module_stamp()
    module.update(base="0x1000", bytes=128,
                  ranges=[{"base": "0x1000", "bytes": 256, "protection": "r--", "file": None},
                          {"base": "0x1400", "bytes": 256, "protection": "rw-", "file": None}])
    maps = [_mapping("0x1000", at) for at in (1001, 1002)]
    for mapping in maps: mapping["bytes"] = 0x1000
    attempt = _attempt(0, "memory_ranges", 1000)
    attempt.update(module_before=copy.deepcopy(module), module_after=copy.deepcopy(module),
                   selections=[_span("0x1000", 256, 0), _span("0x1400", 256, 1)], matched=2, truncated=False)
    result["operations"] = [{"index": 0, "op": "memory_ranges", "status": "ok", "inspection_index": 0}]
    result["memory_inspection"].update(MI.request_budget(request), mappings=maps, attempts=[attempt],
                                       charged_bytes=0, known_returned_bytes=0, actual_read_bytes=0)
    return request, result


def _worker(request, result):
    identity = {"bundle_id": "org.fixture.App", "bundle": "/fixture/App.app", "executable": result["process"]["executable"],
                "pid": result["process"]["pid"], "launch_time": 1000.25, "uid": os.getuid()}
    return {**copy.deepcopy(result), "format": B.WORKER, "request_sha256": R.digest(request), "transport": {"kind": "local"},
        "attached": True, "preloaded_gadget_remains": False, "compiler_provenance": {}, "native_resources": {},
        "identity": copy.deepcopy(identity), "after_identity": copy.deepcopy(identity),
        "cleanup": {"script_unloaded": True, "detached": True}, "cleanup_errors": [],
        "frida_lifecycle": {"format": B.FRIDA_LIFECYCLE, "manager_state": "closed", "attachment_attempted": True,
                            "helper_exit_confirmed": False, "error": None}}


def test_whole_chain_is_raw_only_bound_and_defensively_copied_without_actor_credit():
    request, result = _case()
    saved = copy.deepcopy((request, result))
    assert B.validate_request(request) == MI.validate_request(request) == request
    assert MI.validate_result(request, result) == result["memory_inspection"]
    worker = _worker(request, result)
    assert B._worker_result(worker, R.digest(request), request, {"kind": "local"}) == worker
    B._memory_process_identity(request, worker, worker["identity"])
    assert MI.request_budget(request) == {"metadata_reserved": 7, "read_bytes_reserved": 16}
    assert B.minimum_request_native_calls(request) == 0
    returned = MI.validate_result(request, result)
    returned["attempts"][1]["read"]["bytes_hex"] = "00"
    assert (request, result) == saved
    with pytest.raises(ValueError): B.validate_actor({"kind": "memory_bytes", **request["operations"][2]})


@pytest.mark.parametrize("fault", ["mixed", "forward", "duplicate", "wrong_kind", "stale", "raw_pointer", "code", "bool_limit",
                                   "metadata", "read_bytes", "offset_wrap", "oversized_read", "negative", "index", "span", "rva", "pin_conflict"])
def test_closed_request_refuses_bad_aliases_calls_and_whole_batch_budgets(fault):
    request, _ = _case()
    ops = request["operations"]
    if fault == "mixed": ops.append({"op": "modules"})
    elif fault == "forward": ops.insert(0, _bytes({"span": "child"}))
    elif fault == "duplicate": ops.append(_ranges(alias="maps"))
    elif fault == "wrong_kind": ops[1]["from"] = {"span": "maps"}
    elif fault == "stale": request = {"operations": [_bytes({"span": "old"})]}
    elif fault == "raw_pointer": ops[1]["from"] = {"address": "0x1000"}
    elif fault == "code": ops[0]["source"] = "injected program"
    elif fault == "bool_limit": ops[0]["limit"] = True
    elif fault == "metadata": ops[0]["limit"] = 128
    elif fault == "read_bytes": request = {"operations": [_ranges(1), *[_bytes({"ranges": "maps", "index": 0}, 16384) for _ in range(4)], _pointer()]}
    elif fault == "offset_wrap": ops[1]["offset"] = MI.SAFE_INTEGER
    elif fault == "oversized_read": ops[2]["bytes"] = 16385
    elif fault == "negative": ops[2]["offset"] = -1
    elif fault == "index": ops[1]["from"]["index"] = 2
    elif fault == "span": ops[2]["offset"] = 9
    elif fault == "rva": request = {"operations": [{"op": "memory_range", "module": MEMORY_FILE, "rva": "0x01", "bytes": 8, "as": "s"}]}
    else:
        request = {"operations": [{"op": "memory_range", "module": {**MEMORY_FILE, "sha256": sha * 64}, "rva": "0x0", "bytes": 8, "as": alias}
                                   for alias, sha in (("a", "a"), ("b", "b"))]}
    with pytest.raises(ValueError): MI.validate_request(request)
    with pytest.raises(ValueError): B.validate_request(request)


def test_full_byte_reservation_is_available_and_capability_uses_no_file_io(monkeypatch):
    request = {"operations": [_ranges(1), *[_bytes({"ranges": "maps", "index": 0}, 16384) for _ in range(4)]]}
    assert MI.request_budget(request) == {"metadata_reserved": 9, "read_bytes_reserved": 65536}
    monkeypatch.setattr(R, "assert_guest", lambda: pytest.fail("pure request validation reached VM/native IO"))
    assert B.validate_request(request) == request


@pytest.mark.parametrize("fault", ["missing", "legacy", "native_call", "helper", "helper_residency", "compiler", "queue", "expiry",
    "atomic", "type", "cpu", "pointer", "byte_count", "bool_counter", "incomplete_prefix", "unrequested_read", "extra_field",
    "map_before_after", "pointee_reuse", "pointee_reorder", "inventory_duplicate", "inventory_reorder", "cross_time", "huge_time"])
def test_forged_result_cannot_claim_a_full_raw_observation(fault):
    request, result = _case()
    journal = result["memory_inspection"]
    if fault == "missing": result.pop("memory_inspection")
    elif fault == "legacy": result["format"] = B.LEGACY_WORKER
    elif fault == "native_call": result["native_calls"] = 1
    elif fault == "helper": result["helper_load_attempts"] = 1
    elif fault == "helper_residency": result["helper_residency"] = [{}]
    elif fault == "compiler": result["compiler_provenance"] = {"unexpected": {}}
    elif fault == "queue": result["queue"] = "main"
    elif fault == "expiry": journal["aliases_expired"] = False
    elif fault == "atomic": journal["atomic_snapshot"] = True
    elif fault == "type": journal["type_admitted"] = True
    elif fault == "cpu": result["process"]["architecture"] = "unknown"
    elif fault == "pointer": journal["pointer_size"] = 4
    elif fault == "byte_count": journal["known_returned_bytes"] = 0
    elif fault == "bool_counter": journal["charged_bytes"] = True
    elif fault == "incomplete_prefix": result["operations"].pop()
    elif fault == "unrequested_read": journal["attempts"][2]["read"]["offset"] = 1
    elif fault == "extra_field": journal["attempts"][0]["write"] = "forbidden"
    elif fault == "map_before_after": journal["attempts"][2]["mapping_after"] = 5
    elif fault == "pointee_reuse": journal["attempts"][1]["pointee_mapping"] = 3
    elif fault == "pointee_reorder":
        journal["attempts"][1]["mapping_after"], journal["attempts"][1]["pointee_mapping"] = 4, 3
    elif fault == "inventory_duplicate": journal["attempts"][0]["selections"][1] = copy.deepcopy(journal["attempts"][0]["selections"][0])
    elif fault == "inventory_reorder": journal["attempts"][0]["selections"].reverse()
    elif fault == "cross_time": journal["attempts"][1]["started_ms"] = 1009
    else: journal["attempts"][0]["started_ms"] = 10**10000
    with pytest.raises(ValueError): MI.validate_result(request, result)


@pytest.mark.parametrize("role", ["same_destination", "destination_before_after", "inventory_duplicate", "inventory_reorder"])
def test_coherent_mapping_role_forgery_cannot_collapse_or_reorder_real_observations(role):
    """Keep every other binding/count coherent so only role freshness refuses.

    A set of indices alone accepted these shapes: duplicate inventory rows,
    and a self-pointing pointer that reuses its source's post-read observation.
    """
    request, result = _case()
    journal = result["memory_inspection"]
    if role.startswith("inventory"):
        request["operations"] = request["operations"][:1]
        result["operations"] = result["operations"][:1]
        journal["attempts"] = journal["attempts"][:1]
        inventory = journal["attempts"][0]
        if role == "inventory_duplicate":
            inventory["selections"][1] = copy.deepcopy(inventory["selections"][0])
            journal["mappings"] = journal["mappings"][:1]
        else:
            inventory["selections"].reverse()
            journal["mappings"] = journal["mappings"][:2]
        journal.update(charged_bytes=0, known_returned_bytes=0, actual_read_bytes=0)
    else:
        request["operations"] = request["operations"][:2]
        result["operations"] = result["operations"][:2]
        journal["attempts"] = journal["attempts"][:2]
        pointer = journal["attempts"][1]
        pointer["read"]["bytes_hex"] = "0010000000000000"
        pointer["pointee_mapping"] = 3
        pointer["selections"] = [_span("0x1000", 16, 3)]
        journal["mappings"] = journal["mappings"][:4 if role == "same_destination" else 5]
        if role == "destination_before_after":
            journal["mappings"][4] = _mapping("0x1000", 1013)
            pointer["mapping_after"] = 4
        journal.update(charged_bytes=8, known_returned_bytes=8, actual_read_bytes=8)
    journal.update(MI.request_budget(request))
    with pytest.raises(ValueError, match="mapping roles"):
        MI.validate_result(request, result)


@pytest.mark.parametrize("kind", ["mapping", "span", "module"])
def test_exact_one_past_native_pointer_endpoint_is_not_representable(kind):
    mapping = _mapping("0xffffffffffffff00", 1)
    if kind == "mapping":
        with pytest.raises(ValueError): MI._mapping(mapping)
    elif kind == "span":
        # A valid enclosing mapping ending one byte before the wrap point.
        mapping["bytes"] = 255
        with pytest.raises(ValueError): MI._span(_span("0xffffffffffffff00", 256, 0), [mapping])
    else:
        module = {"path": MEMORY_FILE["path"], "base": "0xffffffffffffff00", "bytes": 256, "file": MEMORY_FILE,
                  "loaded_binding": "path_metadata_only", "loaded_image_verified": False,
                  "ranges": [{"base": "0xffffffffffffff00", "bytes": 255, "protection": "r--", "file": None}]}
        with pytest.raises(ValueError): MI._module(module, MEMORY_FILE)
    mapping["bytes"] = 255
    MI._mapping(mapping)


def _read_failure():
    request, result = _case()
    result["status"], result["error"] = "incomplete", _error()["error"]
    result["operations"] = result["operations"][:1]
    journal = result["memory_inspection"]
    journal["attempts"] = journal["attempts"][:2]
    last = journal["attempts"][-1]
    last.update(status="incomplete", stage="read", errors=[_error()], selections=[], pointee_mapping=None)
    last["read"].update(returned=False, bytes_hex=None, returned_bytes=None, unknown_partial=True)
    journal["mappings"] = journal["mappings"][:4]
    journal.update(charged_bytes=8, known_returned_bytes=0, actual_read_bytes=None)
    return request, result


def test_failed_actual_read_retains_unknown_extent_and_never_creates_zero_or_completed_credit():
    request, result = _read_failure()
    journal = MI.validate_result(request, result)
    assert journal["charged_bytes"] == 8 and journal["actual_read_bytes"] is None
    assert journal["known_returned_bytes"] == 0 and journal["attempts"][-1]["read"]["bytes_hex"] is None
    for changed in (0, 8):
        bad = copy.deepcopy(result); bad["memory_inspection"]["actual_read_bytes"] = changed
        with pytest.raises(ValueError): MI.validate_result(request, bad)


@pytest.mark.parametrize("fault", ["erase_read", "erase_pointee_read", "same_mapping", "wrong_first_stage", "fake_unknown_zero"])
def test_partial_failure_cannot_erase_issuance_or_reuse_mapping_observations(fault):
    request, result = _read_failure()
    journal, last = result["memory_inspection"], result["memory_inspection"]["attempts"][-1]
    if fault in {"erase_read", "erase_pointee_read"}:
        last["read"] = None; journal.update(charged_bytes=0, actual_read_bytes=0)
        if fault == "erase_pointee_read": last.update(stage="pointee", errors=[_error("pointee")])
    elif fault == "same_mapping": last["mapping_after"] = last["mapping_before"]
    elif fault == "wrong_first_stage": last["stage"] = "module_after"
    else: last["read"]["unknown_partial"] = False; journal["actual_read_bytes"] = 0
    with pytest.raises(ValueError): MI.validate_result(request, result)


def test_empty_early_refusal_and_missing_rpc_journal_remain_partial_without_invented_observations():
    request, result = _case()
    result.update(status="incomplete", operations=[], error=_error("validate")["error"])
    result["memory_inspection"].update(metadata_reserved=0, read_bytes_reserved=0, mappings=[], attempts=[],
                                       charged_bytes=0, known_returned_bytes=0, actual_read_bytes=0)
    assert MI.validate_result(request, result)["attempts"] == []
    result.pop("memory_inspection"); result["native_calls"] = None
    assert MI.validate_result(request, result) is None
    result["status"] = "completed"
    with pytest.raises(ValueError): MI.validate_result(request, result)


def test_partial_raw_journal_cannot_claim_a_worker_that_never_attached_but_pre_rpc_failure_can():
    request, partial = _read_failure()
    assert MI.validate_result(request, partial)["actual_read_bytes"] is None
    result = _worker(request, partial)
    result.update(attached=False, cleanup={"script_unloaded": False, "detached": False},
                  frida_lifecycle={"format": B.FRIDA_LIFECYCLE, "manager_state": "not_acquired", "attachment_attempted": False,
                                   "helper_exit_confirmed": False, "error": None})
    with pytest.raises(ValueError, match="actual worker attachment"):
        B._worker_result(result, R.digest(request), request, {"kind": "local"})
    result.pop("memory_inspection")
    result.update(status="unavailable", operations=[], identity=None, after_identity=None)
    assert B._worker_result(result, R.digest(request), request, {"kind": "local"})["status"] == "unavailable"


def test_typed_result_cannot_carry_raw_summary_or_raw_operation_reference():
    request, raw = _case()
    typed = {"operations": [{"op": "modules"}]}
    with pytest.raises(ValueError): MI.validate_result(typed, raw)
    raw.pop("memory_inspection")
    with pytest.raises(ValueError): MI.validate_result(typed, raw)
    assert MI.validate_result(typed, {"operations": []}) is None


@pytest.mark.parametrize("fault", ["process_pid", "process_executable", "worker_identity", "worker_after", "caller_identity", "caller_epoch", "cleanup"])
def test_raw_worker_observation_is_bound_to_worker_and_independently_frozen_caller(fault):
    request, raw = _case()
    result = _worker(request, raw)
    caller = copy.deepcopy(result["identity"])
    if fault == "process_pid": result["process"]["pid"] += 1
    elif fault == "process_executable": result["process"]["executable"] += "-other"
    elif fault == "worker_identity": result["identity"]["pid"] += 1
    elif fault == "worker_after": result["after_identity"]["launch_time"] += 1
    elif fault == "caller_identity": caller["pid"] += 1
    elif fault == "caller_epoch": caller["launch_time"] += 1
    else: result["cleanup"]["detached"] = False
    if fault.startswith("caller_"):
        B._worker_result(result, R.digest(request), request, {"kind": "local"})
        with pytest.raises(ValueError): B._memory_process_identity(request, result, caller)
    else:
        with pytest.raises(ValueError): B._worker_result(result, R.digest(request), request, {"kind": "local"})


def test_raw_module_resources_pin_files_without_claiming_loaded_image_identity(monkeypatch):
    request = {"operations": [{"op": "memory_range", "module": MEMORY_FILE, "rva": "0x0", "bytes": 8, "as": "s"}]}
    calls = []
    monkeypatch.setattr(R, "assert_guest", lambda: calls.append("guard"))
    def installed(path, limit, message):
        calls.append((path, limit)); return {"file": path, "sha256": MEMORY_FILE["sha256"], "bytes": 4096}
    monkeypatch.setattr(B, "_installed_file", installed)
    for name in ("_macho_images", "_macho_sections", "_macho_segments"):
        monkeypatch.setattr(B, name, lambda path, label=name: [{"fixture": label}])
    resources = B.request_resources(request)
    pin = resources[MEMORY_FILE["path"]]
    installed_call = (MEMORY_FILE["path"], B.MAX_NATIVE_MODULE_BYTES)
    assert calls[0] == "guard" and calls.count(installed_call) == 1
    assert all(call == "guard" or call == installed_call for call in calls)
    assert set(pin) == {"file", "sha256", "bytes", "macho_images", "macho_sections", "macho_segments"}
    assert "loaded_image_verified" not in pin and B.request_compiler_provenance(request) == {}
    monkeypatch.setattr(B, "_installed_file", lambda *args: {"file": MEMORY_FILE["path"], "sha256": "b" * 64, "bytes": 4096})
    with pytest.raises(ValueError): B.request_resources(request)


def test_module_data_outside_reported_size_is_readable_only_with_actual_range_and_fresh_process_map():
    request, result = _module_case()
    journal = MI.validate_result(request, result)
    first = journal["attempts"][0]
    assert int(first["selections"][0]["base"], 16) > int(first["module_before"]["base"], 16) + first["module_before"]["bytes"]
    assert journal["charged_bytes"] == journal["actual_read_bytes"] == 8
    assert journal["type_admitted"] is False and journal["loaded_image_verified"] is False
    # Module SDK/init protection and present Process protection are separate facts.
    for attempt in result["memory_inspection"]["attempts"]:
        attempt["module_before"]["ranges"][1]["protection"] = "r--"
        attempt["module_after"]["ranges"][1]["protection"] = "r--"
    assert MI.validate_result(request, result)["attempts"][1]["read"]["returned_bytes"] == 8


@pytest.mark.parametrize("fault", ["missing", "empty", "many", "order", "overlap", "unreadable", "below_base",
                                   "outside_cap", "wrap", "extra_field", "bool_extent", "bad_file", "data_hole",
                                   "neighbor", "cross_end", "after_changed", "alias_changed", "process_unreadable"])
def test_module_coverage_cannot_be_invented_from_reported_size_disk_extents_or_neighbor_maps(fault):
    request, result = _module_case()
    journal = result["memory_inspection"]
    first, second = journal["attempts"]
    stamp = first["module_before"]
    if fault == "missing": stamp.pop("ranges")
    elif fault == "empty": stamp["ranges"] = []
    elif fault == "many":
        stamp["ranges"] = [{"base": hex(0x10000000 + index * 16), "bytes": 8, "protection": "r--", "file": None}
                           for index in range(33)]
    elif fault == "order": stamp["ranges"].reverse()
    elif fault == "overlap": stamp["ranges"][1]["base"] = "0x10490000"
    elif fault == "unreadable": stamp["ranges"][1]["protection"] = "-w-"
    elif fault == "below_base": stamp["ranges"][0]["base"] = "0xfffffff"
    elif fault == "outside_cap": stamp["ranges"][1]["base"] = "0x30000000"
    elif fault == "wrap": stamp["ranges"][1].update(base="0xffffffffffffff00", bytes=256)
    elif fault == "extra_field": stamp["ranges"][1]["owner_verified"] = True
    elif fault == "bool_extent": stamp["ranges"][1]["bytes"] = True
    elif fault == "bad_file": stamp["ranges"][1]["file"] = {"path": MEMORY_FILE["path"], "offset": -1, "size": None}
    elif fault == "data_hole": stamp["ranges"] = stamp["ranges"][:1]
    elif fault == "neighbor": stamp["ranges"][1].update(base="0x104c0000", bytes=0x10000)
    elif fault == "cross_end": stamp["ranges"][1]["bytes"] = 0xb39c
    elif fault == "after_changed": first["module_after"]["ranges"][1]["bytes"] -= 1
    elif fault == "alias_changed":
        for phase in ("module_before", "module_after"): second[phase]["ranges"][1]["bytes"] -= 1
    else:
        for mapping in journal["mappings"]: mapping["protection"] = "-w-"
    if fault not in {"after_changed", "alias_changed", "process_unreadable"}:
        for attempt in journal["attempts"]:
            for phase in ("module_before", "module_after"): attempt[phase] = copy.deepcopy(stamp)
    with pytest.raises(ValueError): MI.validate_result(request, result)


def test_module_attributed_ranges_have_a_separate_closed_32_row_bound():
    stamp = _module_stamp()
    stamp["ranges"] = [{"base": hex(0x10000000 + index * 16), "bytes": 8, "protection": "r--", "file": None}
                       for index in range(32)]
    MI._module(stamp, MEMORY_FILE)
    stamp["ranges"].append({"base": "0x10000200", "bytes": 8, "protection": "r--", "file": None})
    with pytest.raises(ValueError): MI._module(stamp, MEMORY_FILE)


@pytest.mark.parametrize("base", [2**64 - MI.MAX_MODULE_BYTES, 2**64 - 256])
def test_module_investigation_ceiling_cannot_wrap_even_when_reported_and_attributed_spans_fit(base):
    stamp = _module_stamp()
    stamp.update(base=hex(base), bytes=8,
                 ranges=[{"base": hex(base), "bytes": 8, "protection": "r--", "file": None}])
    MI._range(stamp["ranges"][0])
    assert base + stamp["bytes"] < 2**64
    with pytest.raises(ValueError, match="ceiling"): MI._module(stamp, MEMORY_FILE)
    # A ceiling one byte below the native wrap point remains representable.
    stamp["base"] = hex(2**64 - MI.MAX_MODULE_BYTES - 1)
    stamp["ranges"][0]["base"] = stamp["base"]
    MI._module(stamp, MEMORY_FILE)


def test_module_inventory_uses_exact_actual_range_intersections_and_preserves_holes():
    request, result = _module_inventory_case()
    journal = MI.validate_result(request, result)
    assert [span["base"] for span in journal["attempts"][0]["selections"]] == ["0x1000", "0x1400"]
    assert journal["metadata_reserved"] == len(journal["mappings"]) == 2
    assert len(journal["attempts"][0]["module_before"]["ranges"]) == 2
    assert journal["actual_read_bytes"] == 0


@pytest.mark.parametrize("fault", ["fill_hole", "truncate_to_size", "invent_intersection", "unrecorded_range"])
def test_module_inventory_rejects_reported_size_clips_holes_and_forged_intersections(fault):
    request, result = _module_inventory_case()
    attempt = result["memory_inspection"]["attempts"][0]
    if fault == "fill_hole": attempt["selections"][0]["bytes"] = 0x500
    elif fault == "truncate_to_size": attempt["selections"][0]["bytes"] = 128
    elif fault == "invent_intersection": attempt["selections"][1].update(base="0x1480", bytes=128)
    else:
        for phase in ("module_before", "module_after"): attempt[phase]["ranges"] = attempt[phase]["ranges"][:1]
    with pytest.raises(ValueError): MI.validate_result(request, result)


def test_current_raw_reader_rejects_legacy_range_ownership_envelope_even_for_otherwise_valid_data():
    request, result = _case()
    result["memory_inspection"]["format"] = "anyuse.rea-memory-inspection/1"
    with pytest.raises(ValueError): MI.validate_result(request, result)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
