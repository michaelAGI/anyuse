from __future__ import annotations

import base64
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess

import pytest

try:
    from anyuse.execution import (
        rea_actor_evidence as E,
        rea_agent as G,
        rea_mapping as R,
        rea_native_path as NP,
        rea_native_receiver_v2 as v2,
        rea_runtime_bridge as B,
        rea_runtime_providers as P,
        rea_static_artifacts as A,
    )
except ImportError:
    from execution import (
        rea_actor_evidence as E,
        rea_agent as G,
        rea_mapping as R,
        rea_native_path as NP,
        rea_native_receiver_v2 as v2,
        rea_runtime_bridge as B,
        rea_runtime_providers as P,
        rea_static_artifacts as A,
    )

from test_rea_runtime_bridge import _inert_macho_with_data


@contextmanager
def _raises(exc_type: type[BaseException], match: str):
    try:
        yield
    except exc_type as exc:
        if not re.search(match, str(exc)):
            raise AssertionError(f"Expected {match!r} in {str(exc)!r}") from exc
    else:
        raise AssertionError(f"Expected {exc_type.__name__} matching {match!r}")


SAMPLE_IMAGE = {
    "path": "/Applications/TestApp.app/Contents/MacOS/TestApp",
    "sha256": "a" * 64,
}


def _pin(file: Path) -> dict:
    raw = file.read_bytes()
    return {"file": str(file), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _write(file: Path, value: object) -> dict:
    file.write_text(json.dumps(value, sort_keys=True, allow_nan=False))
    return _pin(file)


def _base_vtable_request(module: dict | None = None) -> dict:
    image = copy.deepcopy(module or SAMPLE_IMAGE)
    return {
        "op": "declare_native_receiver_v2",
        "getter": {
            "kind": "native_function",
            "target": {"module": copy.deepcopy(image), "offset": "0x1000"},
            "abi": "default",
            "arguments": [],
            "return_type": "pointer",
        },
        "hops": [
            {"kind": "field_pointer", "name": "active_doc", "field_offset": 16, "non_null": True},
            {
                "kind": "step_getter",
                "name": "page_controller",
                "target": {"module": copy.deepcopy(image), "offset": "0x1004"},
                "abi": "default",
            },
        ],
        "consumer": {
            "kind": "native_function",
            "dispatch": "vtable_slot",
            "vtable_slot_offset": 16,
            "target_entry": {"module": copy.deepcopy(image), "offset": "0x1008"},
            "this_adjustment": -16,
            "abi": "default",
            "arguments": [
                {"type": "pointer", "value": {"receiver": "fresh"}},
                {"type": "int32", "value": {"input": "amount"}},
            ],
            "return_type": "void",
        },
        "vptr": {
            "image": copy.deepcopy(image),
            "data_offset": "0x2000",
        },
        "citations": [
            {"role": "getter_code", "artifact": "s0000/original.json", "pointer": "/range"},
            {"role": "consumer_code", "artifact": "s0001/original.json", "pointer": "/range"},
            {"role": "vptr_data", "artifact": "s0002/original.json", "pointer": "/range"},
            {"role": "vtable_slot_data", "artifact": "s0003/original.json", "pointer": "/range"},
        ],
    }


def test_vtable_slot_and_multihop_declaration_builds_recipe():
    req = _base_vtable_request()
    validated = v2.validate_request_v2(req, params=[{"name": "amount"}])
    assert len(validated["hops"]) == 2
    assert validated["consumer"]["dispatch"] == "vtable_slot"
    assert validated["consumer"]["vtable_slot_offset"] == 16
    assert validated["consumer"]["this_adjustment"] == -16

    recipe, actor = v2.build_recipe_v2(validated, "/tmp/session/s0004/decl.json", "b" * 64)
    # 1 root getter + 1 step_getter hop + 1 consumer = 3 expected native calls
    assert recipe["lifetime"]["expected_native_calls"] == 3
    assert recipe["hops"][1]["signature_evidence"]["pointer"] == "/signatures/hop_1"
    assert actor["dispatch"] == "vtable_slot"
    assert actor["vtable_slot_offset"] == 16
    assert actor["arguments"][0]["value"]["native_path"]["evidence_basis"]["abi_recovered"] is False
    # Contracts strip hop signature_evidence so there is no self-hash cycle
    assert "signature_evidence" not in json.dumps(v2.contracts(recipe))
    assert len(v2.contracts(recipe)) == 4


def test_rejects_unaligned_vtable_slot_or_excess_hops_or_duplicate_entries():
    bad_slot = _base_vtable_request()
    bad_slot["consumer"]["vtable_slot_offset"] = 20  # not 8-byte aligned
    with _raises(ValueError, "vtable_slot_offset"):
        v2.validate_request_v2(bad_slot, params=[{"name": "amount"}])

    too_many_hops = _base_vtable_request()
    too_many_hops["hops"] = [
        {"kind": "field_pointer", "name": f"hop_{i}", "field_offset": 8 * i, "non_null": True}
        for i in range(4)
    ]
    with _raises(ValueError, "at most 3 pointer hops"):
        v2.validate_request_v2(too_many_hops, params=[{"name": "amount"}])

    bad_subobj = _base_vtable_request()
    bad_subobj["vptr"]["subobject_offset"] = 4
    with _raises(ValueError, "subobject_offset"):
        v2.validate_request_v2(bad_subobj, params=[{"name": "amount"}])

    bad_this_adj = _base_vtable_request()
    bad_this_adj["consumer"]["this_adjustment"] = 8192
    with _raises(ValueError, "this_adjustment"):
        v2.validate_request_v2(bad_this_adj, params=[{"name": "amount"}])

    dup_entry = _base_vtable_request()
    dup_entry["hops"][1]["target"]["offset"] = "0x1008"  # same as consumer
    with _raises(ValueError, "distinct"):
        v2.validate_request_v2(dup_entry, params=[{"name": "amount"}])

    bad_queue = _base_vtable_request()
    bad_queue["queue_policy"] = "caller_thread"
    with _raises(ValueError, "queue_policy"):
        v2.validate_request_v2(bad_queue, params=[{"name": "amount"}])


def test_crash_blocklist_prevents_repeated_crashing_abi_probes():
    ledger = v2.CrashBlocklist()
    assert not ledger.is_blocked(
        module_sha256="a" * 64,
        offset="0x1008",
        argument_types=["pointer", "int64"],
        dispatch="vtable_slot",
        slot_offset=16,
    )
    ledger.record_crash(
        module_sha256="a" * 64,
        offset="0x1008",
        argument_types=["pointer", "int64"],
        signal_or_reason="SIGSEGV",
        step=5,
        dispatch="vtable_slot",
        slot_offset=16,
    )
    assert ledger.is_blocked(
        module_sha256="a" * 64,
        offset="0x1008",
        argument_types=["pointer", "int64"],
        dispatch="vtable_slot",
        slot_offset=16,
    )
    # Different signature (int32 instead of int64) remains unblocked
    assert not ledger.is_blocked(
        module_sha256="a" * 64,
        offset="0x1008",
        argument_types=["pointer", "int32"],
        dispatch="vtable_slot",
        slot_offset=16,
    )
    exported = ledger.export_ledger()
    assert exported["blocked_count"] == 1
    reloaded = v2.CrashBlocklist.from_ledger(exported)
    assert reloaded.is_blocked(
        module_sha256="a" * 64,
        offset="0x1008",
        argument_types=["pointer", "int64"],
        dispatch="vtable_slot",
        slot_offset=16,
    )


def test_differential_trace_correlator_ranks_ui_actuated_candidates():
    baseline = [
        {"offset": "0x1000", "x0": "0x60000100", "scalar_args": [0]},
    ]
    observed_getters = [
        {"offset": "0xc44", "returned_x0": "0x60000200", "vptr_rva": "0x8020"},
        {"offset": "0x900", "returned_x0": "0x60000100", "vptr_rva": "0x8000"},
    ]
    actuated = [
        # Noise that also occurred during idle baseline
        {"offset": "0x1000", "x0": "0x60000100", "scalar_args": [0]},
        # Real target method fired only during UI actuation with input value 17
        {
            "offset": "0x1480",
            "x0": "0x60000200",
            "scalar_args": [17],
            "vtable_slot_offset": 24,
            "vptr_rva": "0x8020",
        },
    ]
    result = v2.correlate_differential_trace(
        baseline_calls=baseline,
        actuated_calls=actuated,
        observed_getters=observed_getters,
        target_input_value=17,
    )
    assert result["candidate_count"] == 2
    top = result["candidates"][0]
    assert top["getter_offset"] == "0xc44"
    assert top["consumer_offset"] == "0x1480"
    assert top["dispatch"] == "vtable_slot"
    assert top["vtable_slot_offset"] == 24
    assert top["matched_input_value"] is True
    assert top["differential_only"] is True


def test_agent_guide_examples_validate_without_becoming_target_evidence():
    tools = ["declare_native_receiver_v2", "correlate_native_trace_v2"]
    guide = v2.discovery_guide(tools)
    assert guide["evidence_basis"] == v2.BASIS_V2
    assert guide["evidence_basis"]["proof_created"] is False
    declaration = guide["declare_native_receiver_v2"]
    v2.validate_request_v2(declaration["example"], params=[{"name": "example_amount"}])
    for hop in declaration["hop_examples"]:
        request = copy.deepcopy(declaration["example"])
        request["hops"] = [hop]
        v2.validate_request_v2(request, params=[{"name": "example_amount"}])
    trace = guide["correlate_native_trace_v2"]["example"]
    assert v2.validate_trace_request(trace) == trace
    assert "declare_native_receiver_v2" not in v2.discovery_guide(["correlate_native_trace_v2"])
    assert v2.discovery_guide(["evidence"]) is None
    guide["rules"].clear()
    assert v2.discovery_guide(tools)["rules"]


@pytest.mark.parametrize("field,value", [
    ("observed_getters", [7]),
    ("actuated_calls", [None]),
    ("baseline_calls", [{"offset": []}]),
    ("actuated_calls", [{"offset": "0x1008"}]),
    ("actuated_calls", [{"offset": "0x1008", "x0": "saved_receiver"}]),
    ("observed_getters", [{"offset": "0x1000", "returned_x0": "0x00004000"}]),
    ("actuated_calls", [{"offset": "0x1008", "x0": "0x4000", "scalar_args": {"bad": 7}}]),
    ("actuated_calls", [{"offset": "0x1008", "x0": "0x4000", "scalar_args": [float("inf")]}]),
    ("actuated_calls", [{"offset": "0x1008", "x0": "0x4000", "vtable_slot_offset": 3}]),
    ("actuated_calls", [{"offset": "0x1008", "x0": "0x4000", "unknown": True}]),
    ("observed_getters", [{"offset": "0x1000", "returned_x0": "0x4000"}] * 257),
    ("target_input_value", float("nan")),
])
def test_trace_validation_rejects_malformed_rows_before_dispatch(field, value):
    request = v2.discovery_guide(["correlate_native_trace_v2"])["correlate_native_trace_v2"]["example"]
    request[field] = value
    with pytest.raises(ValueError):
        v2.validate_trace_request(request)


def test_trace_cross_product_retains_bounded_best_candidates_and_explicit_total():
    getters = [{"offset": hex(0x1000 + 4 * i), "returned_x0": "0x4000"} for i in range(32)]
    calls = [{"offset": hex(0x2000 + 4 * i), "x0": "0x4000", "scalar_args": [int(i == 31)]}
             for i in range(32)]
    before = copy.deepcopy((getters, calls))
    result = v2.correlate_differential_trace(baseline_calls=[], actuated_calls=calls,
                                           observed_getters=getters, target_input_value=1)
    assert result["matched_candidate_count"] == 1024
    assert result["candidate_count"] == len(result["candidates"]) == v2.MAX_TRACE_CANDIDATES
    assert result["candidates_truncated"] is True
    assert all(row["consumer_offset"] == calls[-1]["offset"] for row in result["candidates"][:32])
    assert result["input_basis"] == "caller_supplied_trace_observations"
    assert result["proof_created"] is False
    assert (getters, calls) == before
    assert result == v2.correlate_differential_trace(baseline_calls=[], actuated_calls=calls,
                                                   observed_getters=getters, target_input_value=1)


@pytest.mark.parametrize("number", [0, 1])
def test_trace_numeric_input_does_not_match_a_boolean_argument(number):
    result = v2.correlate_differential_trace(
        baseline_calls=[], observed_getters=[{"offset": "0x1000", "returned_x0": "0x4000"}],
        actuated_calls=[{"offset": "0x1008", "x0": "0x4000", "scalar_args": [bool(number)]},
                        {"offset": "0x100c", "x0": "0x4000", "scalar_args": [float(number)]}],
        target_input_value=number)
    assert result["candidates"][0]["consumer_offset"] == "0x100c"
    assert [row["matched_input_value"] for row in result["candidates"]] == [True, False]


def _build_declared_session(tmp_path: Path, monkeypatch) -> tuple[Path, dict, dict, Path, dict]:
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    root = tmp_path.resolve() / "session"
    root.mkdir()
    app = tmp_path.resolve() / "Owned.app"
    image = app / "Contents/MacOS/Owned"
    image.parent.mkdir(parents=True)
    image.write_bytes(_inert_macho_with_data())
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "fixture.owned",
                "CFBundleExecutable": "Owned",
                "CFBundleVersion": "1",
            }
        )
    )
    module = {"path": str(image), "sha256": _pin(image)["sha256"]}
    request = _base_vtable_request(module)
    params = [{"name": "amount", "type": "integer"}]
    observer_decl = {"format": "fixture", "id": "fx", "label": "fx", "effect": "fx", "params": params}
    state = {
        "bundle": str(app),
        "target": R.fingerprint(app),
        "task": "Set a fixture scalar via V2 receiver",
        "params": params,
        "plan": {"fixture": True},
        "success_criteria": ["Fixture scalar changes"],
        "workflow": "guest_model_judged_state_v1",
        "native_contract_mode": "declared",
        "native_receiver_mode": v2.POLICY_V2,
        "observer_profile": observer_decl,
        "observer_profile_sha256": R.digest(observer_decl),
        "status": "investigating",
        "artifacts": {},
        "steps": [],
    }
    citation_sources = [
        {"kind": "original_code", "relative": "Contents/MacOS/Owned", "offset": "0x1000", "bytes": 4, "unit_bytes": 4},
        {"kind": "original_code", "relative": "Contents/MacOS/Owned", "offset": "0x1008", "bytes": 4, "unit_bytes": 4},
        {"kind": "original_range", "relative": "Contents/MacOS/Owned", "offset": "0x2000", "bytes": 8, "unit_bytes": 8},
        {"kind": "original_range", "relative": "Contents/MacOS/Owned", "offset": "0x2010", "bytes": 8, "unit_bytes": 8},
    ]
    for index, source in enumerate(citation_sources):
        folder = root / f"s{index:04d}"
        folder.mkdir()
        producing = {"op": "static_evidence", "request": source}
        original = A.OriginalImage(app, source["relative"])
        try:
            observation = A.original_range(original, source)
        finally:
            original.close()
        row = {"step": index, "status": "completed", "request": producing, "feedback": {"fixture": True}}
        state["steps"].append(row)
        for name, value in [("original.json", observation), ("request.json", producing), ("feedback.json", row)]:
            file = folder / name
            state["artifacts"][file.relative_to(root).as_posix()] = _write(file, value)

    decl_folder = root / "s0004"
    decl_folder.mkdir()
    state["steps"].append({"step": 4, "status": "running", "request": request})
    _write(decl_folder / "request.json", request)
    _write(root / "session.json", state)
    result = v2.declare(root, state, request, decl_folder)
    state["steps"][4].update(status="completed", feedback=result)
    _write(decl_folder / "feedback.json", state["steps"][4])
    for name in [v2.RECEIPT_V2, "request.json", "feedback.json"]:
        state["artifacts"][f"s0004/{name}"] = _pin(decl_folder / name)
    _write(root / "session.json", state)
    return root, state, result, image, request


def test_declare_persists_receipt_and_identifies_hypothesis(tmp_path, monkeypatch):
    root, state, result, image, request = _build_declared_session(tmp_path, monkeypatch)
    assert result["status"] == "native_receiver_v2_hypothesis_declared"
    assert Path(result["receipt"]).is_file()
    assert v2.has_hypothesis_v2(result["actor"]) is True
    summary = v2.evidence_summary_v2(result["actor"])
    assert summary is not None and summary["receiver_basis"] == "binary_correlated_hypothesis_v2"

    actor, recipe = result["actor"], result["recipe"]
    resources = v2.resources(recipe)
    assert resources == v2.resources(actor) == v2.resources(recipe["root"]["getter"])
    assert str(root / "session.json") not in resources
    assert str(image) in resources
    assert all(str(root / f"s{index:04d}/request.json") in resources for index in range(5))
    assert v2.validate_declaration(actor, state=state)["evidence_basis"] == v2.BASIS_V2
    receipt = resources[result["receipt"]]
    assert [row["pointer"] for row in receipt["native_receiver_signatures"]] == [
        "/signatures/getter",
        "/signatures/hop_1",
        "/signatures/consumer",
    ]

    # Bound actor still validates resources and bridges cleanly through request_resources
    bound = B.bind_actor(actor, {"amount": "7"})
    assert v2.resources(bound) == resources
    bridge_pins = B.request_resources(B.invocation_request(bound))
    assert str(image) in bridge_pins and result["receipt"] in bridge_pins
    assert len(bridge_pins[result["receipt"]]["native_path_contracts"]) == 4

    # Crash blocklist in state refuses re-declaring a blocked hypothesis
    ledger = v2.CrashBlocklist()
    ledger.record_crash(
        module_sha256=_pin(image)["sha256"],
        offset="0x1008",
        argument_types=["pointer", "int32"],
        signal_or_reason="SIGSEGV",
        step=3,
        dispatch="vtable_slot",
        slot_offset=16,
    )
    blocked_state = copy.deepcopy(state)
    blocked_state["crash_blocklist"] = ledger.export_ledger()
    step5 = root / "s0005"
    step5.mkdir()
    blocked_state["steps"].append({"step": 5, "status": "running", "request": request})
    _write(step5 / "request.json", request)
    _write(root / "session.json", blocked_state)
    with _raises(ValueError, "crash-blocklisted"):
        v2.declare(root, blocked_state, request, step5)


@pytest.mark.parametrize("change", ["mode", "foreign_session", "image", "producing_kind", "registered_pin", "actor_target"])
def test_v2_resource_and_proposal_reuse_refuses_policy_producer_or_recipe_tampering(tmp_path, monkeypatch, change):
    root, state, result, image, _ = _build_declared_session(tmp_path, monkeypatch)
    actor = copy.deepcopy(result["actor"])
    if change == "mode":
        state["native_receiver_mode"] = None
        _write(root / "session.json", state)
    elif change == "foreign_session":
        foreign = copy.deepcopy(state)
        foreign["task"] = "Different task"
        with pytest.raises(ValueError, match="current session"):
            v2.validate_declaration(actor, state=foreign)
        return
    elif change == "image":
        image.write_bytes(image.read_bytes()[:-1] + b"x")
    elif change == "producing_kind":
        state["steps"][0]["request"]["request"]["kind"] = "ghidra_code"
        _write(root / "session.json", state)
    elif change == "registered_pin":
        state["artifacts"]["s0000/original.json"]["sha256"] = "0" * 64
        _write(root / "session.json", state)
    else:
        actor["target"]["offset"] = "0x100c"
    with pytest.raises((ValueError, E.EvidenceIntegrityError)):
        v2.resources(actor)


def test_v2_guest_guard_runs_before_any_declaration_or_resource_file_read(monkeypatch):
    def refuse():
        raise PermissionError("guest fixture only")

    monkeypatch.setattr(R, "assert_guest", refuse)
    with pytest.raises(PermissionError, match="guest fixture only"):
        v2.declare("/missing", {}, {}, "/missing")
    with pytest.raises(PermissionError, match="guest fixture only"):
        v2.resources({})


def test_native_path_and_runtime_bridge_accept_v2_actor():
    req = _base_vtable_request()
    recipe, actor = v2.build_recipe_v2(req, "/tmp/session/s0010/decl.json", "b" * 64)

    # Native path delegates V2 recipe contracts and claims cleanly
    assert NP.is_immediate_root(recipe) is True
    assert NP.root_contract(recipe)["format"] == v2.ROOT_CONTRACT_V2
    assert NP.lifetime_contract(recipe)["format"] == v2.LIFETIME_CONTRACT_V2
    assert len(NP.image_claims(recipe)) == 1
    assert "/tmp/session/s0010/decl.json" in NP.file_claims(recipe)
    assert B.minimum_native_calls(actor) == 3  # root getter + step_getter + consumer

    # Runtime bridge validates, binds inputs, and constructs invocation requests
    validated = B.validate_actor(actor, allow_bindings=True)
    assert validated["dispatch"] == "vtable_slot"
    bound = B.bind_actor(actor, {"amount": "42"})
    assert bound["arguments"][1]["value"] == 42
    inv_req = B.invocation_request(bound)
    assert inv_req["operations"][0]["op"] == "native_invoke"
    assert B.native_receiver_evidence_basis(bound)["receiver_basis"] == "binary_correlated_hypothesis_v2"


def test_extended_v2_strings_containers_pac_and_receipt_validation():
    # 1. Apple ARM64 & LLVM LSB libc++ SSO std::string encoding
    sso_arm64 = v2.encode_libcxx_sso_string("hello.pdf", layout="apple_arm64")
    assert len(sso_arm64["value"]) == 24
    assert sso_arm64["value"][23] == len("hello.pdf")
    sso_lsb = v2.encode_libcxx_sso_string("hello.pdf", layout="llvm_lsb")
    assert sso_lsb["value"][0] == (len("hello.pdf") << 1)
    with _raises(ValueError, "borrowed_const_ref"):
        v2.encode_libcxx_sso_string("hello.pdf", ownership="by_value")

    # 2. arm64e PAC pointer stripping
    signed_ptr = 0x8012000102030408
    assert v2.strip_arm64e_pac(signed_ptr) == 0x0000000102030408

    # 3. Reject non-pointer element_kind in vector_element / array_element, and reject usize/isize/utf8 return_type
    bad_vec = _base_vtable_request()
    bad_vec["hops"] = [
        {"kind": "vector_element", "name": "InlineDocs", "vector_offset": 16, "index": 0, "non_null": True, "element_kind": "inline_object"}
    ]
    with _raises(ValueError, "contiguous 8-byte pointer elements"):
        v2.validate_request_v2(bad_vec, params=[{"name": "amount"}])

    for bad_ret in ("utf8", "usize", "isize"):
        bad_ret_req = _base_vtable_request()
        bad_ret_req["consumer"]["return_type"] = bad_ret
        with _raises(ValueError, "consumer return_type must be scalar or void"):
            v2.validate_request_v2(bad_ret_req, params=[{"name": "amount"}])

    # Reject whole-struct input bindings, out-buffer input bindings, and invalid struct field identifiers
    bad_struct_binding = _base_vtable_request()
    bad_struct_binding["consumer"]["arguments"] = [
        {"type": "pointer", "value": {"receiver": "fresh"}},
        {"type": {"kind": "struct", "fields": [{"name": "x", "type": "int32"}]}, "value": {"input": "amount"}},
    ]
    with _raises(ValueError, "struct value at arg\\[1\\] must match declared fields"):
        v2.validate_request_v2(bad_struct_binding, params=[{"name": "amount"}])

    # Accept valid single-field struct whose field is named "input" (both concrete and field-level binding)
    for field_val in (7, {"input": "amount"}):
        ok_input_struct = _base_vtable_request()
        ok_input_struct["consumer"]["arguments"] = [
            {"type": "pointer", "value": {"receiver": "fresh"}},
            {"type": {"kind": "struct", "fields": [{"name": "input", "type": "int32"}]}, "value": {"input": field_val}},
        ]
        v2.validate_request_v2(ok_input_struct, params=[{"name": "amount"}])

    bad_out_binding = _base_vtable_request()
    bad_out_binding["consumer"]["arguments"] = [
        {"type": "pointer", "value": {"receiver": "fresh"}},
        {"type": {"kind": "buffer", "bytes": 8, "direction": "out"}, "value": {"input": "amount"}},
    ]
    with _raises(ValueError, "input binding at arg\\[1\\] requires a scalar/utf8 or in/inout buffer"):
        v2.validate_request_v2(bad_out_binding, params=[{"name": "amount"}])

    bad_field_name = _base_vtable_request()
    bad_field_name["consumer"]["arguments"] = [
        {"type": "pointer", "value": {"receiver": "fresh"}},
        {"type": {"kind": "struct", "fields": [{"name": "UpperCase", "type": "int32"}]}, "value": {"UpperCase": 1}},
    ]
    with _raises(ValueError, "struct fields must be unique valid identifiers"):
        v2.validate_request_v2(bad_field_name, params=[{"name": "amount"}])

    # 4. Request with vector_element + array_element + step_getter hops, utf8 + SSO buffer + POD struct args,
    #    main queue policy, and arm64e_strip PAC mode
    req = _base_vtable_request()
    req["queue_policy"] = "main"
    req["vptr"]["pac_mode"] = "arm64e_strip"
    req["hops"] = [
        {"kind": "vector_element", "name": "open_docs", "vector_offset": 16, "index": 2, "non_null": True},
        {"kind": "array_element", "name": "views", "base_offset": 32, "index": 0, "stride": 8, "non_null": True},
        {
            "kind": "step_getter",
            "name": "page_controller",
            "target": {"module": copy.deepcopy(SAMPLE_IMAGE), "offset": "0x1004"},
            "abi": "default",
        },
    ]
    req["consumer"]["pac_mode"] = "arm64e_strip"
    req["consumer"]["arguments"] = [
        {"type": "pointer", "value": {"receiver": "fresh"}},
        {"type": "utf8", "value": {"input": "query"}},
        {"type": sso_arm64["type"], "value": {"input": "sso_title"}},
        {
            "type": {"kind": "struct", "fields": [{"name": "x", "type": "int32"}, {"name": "y", "type": "int32"}]},
            "value": {"x": 10, "y": 20},
        },
    ]
    recipe, actor = v2.build_recipe_v2(req, "/tmp/session/s0010/decl.json", "b" * 64)
    assert recipe["lifetime"]["queue"] == "main"
    assert recipe["lifetime"]["expected_native_calls"] == 3  # 1 root getter + 1 step_getter + 1 consumer

    bound = B.bind_actor(actor, {"query": "chapter 4", "sso_title": sso_arm64["hex"]})
    assert bound["arguments"][1]["value"] == "chapter 4"
    assert bound["arguments"][2]["value"] == sso_arm64["value"]

    # 5. Validate evidence plan (4 contracts: root, lifetime, vptr, consumer) and post-call worker observation receipt
    plan = NP.evidence_plan(recipe)
    assert len(plan) == 4
    observation = {
        "recipe": copy.deepcopy(recipe),
        "argument": 0,
        "consumer": NP.consumer_contract(recipe["consumer"]),
        "root_kind": "getter",
        "owner_retained": False,
        "queue": "main",
        "lifetime_mode": v2.MODE_V2,
        "resolved": True,
        "sealed": True,
        "dispatch_permit_consumed": True,
        "hops": [
            {
                "index": 0,
                "kind": "root_getter",
                "name": "root",
                "non_null_verified": True,
                "vptr_checked": False,
                "vtable_slot_verified": False,
                "this_adjusted_verified": False,
            },
            {
                "index": 1,
                "kind": "vector_element",
                "name": "open_docs",
                "non_null_verified": True,
                "vptr_checked": False,
                "vtable_slot_verified": False,
                "this_adjusted_verified": False,
            },
            {
                "index": 2,
                "kind": "array_element",
                "name": "views",
                "non_null_verified": True,
                "vptr_checked": False,
                "vtable_slot_verified": False,
                "this_adjusted_verified": False,
            },
            {
                "index": 3,
                "kind": "step_getter",
                "name": "page_controller",
                "non_null_verified": True,
                "vptr_checked": True,
                "vtable_slot_verified": True,
                "this_adjusted_verified": True,
            },
        ],
        "expires": "transaction",
        "borrowed_scope": {
            "getter_epoch": 2,
            "checked_epoch": 2,
            "use": "dispatch",
            "consumed": True,
            "expires": "use",
        },
        "evidence_basis": dict(v2.BASIS_V2),
    }
    validated_obs = NP.validate_observations([observation], [recipe], for_call=True)
    assert len(validated_obs) == 1

    # Tampered hop observation is rejected
    bad_obs = copy.deepcopy(observation)
    bad_obs["hops"][-1]["vtable_slot_verified"] = False
    with _raises(ValueError, "exact typed root and hop verification records"):
        NP.validate_observations([bad_obs], [recipe], for_call=True)


_JS_V2_FIXTURE = r"""
const rawJson = typeof readFile === 'function'
  ? readFile(arguments[0])
  : require('fs').readFileSync(process.argv[2], 'utf8');
const emit = typeof print === 'function' ? print : (s => process.stdout.write(s + '\n'));
const data = JSON.parse(rawJson);
const main = data.main_path;
const codeBase = 0x100000;
const memory = new Map([
  [codeBase, Uint8Array.from(data.loaded_image)],
  [0x2000, new Uint8Array(64)],   // root object
  [0x3000, new Uint8Array(64)],   // doc object from field_pointer (+16)
  [0x3100, new Uint8Array(64)],   // alternate doc object for drift test
  [0x4000, new Uint8Array(64)],   // vector backing buffer (3 pointers)
  [0x4300, new Uint8Array(64)],   // relocated vector backing buffer
  [0x4100, new Uint8Array(64)],   // vector_element[0] object
  [0x5000, new Uint8Array(64)],   // vector_element[1] object
  [0x4200, new Uint8Array(64)],   // vector_element[2] object
  [data.unmapped_this_adjustment ? 0x6000 : 0x5ff0, new Uint8Array(data.unmapped_this_adjustment ? 64 : 80)],
]);

function backing(address, n) {
  for (const [base, bytes] of memory) {
    if (address >= base && address + n <= base + bytes.length) {
      return [new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), address - base, bytes];
    }
  }
  throw Error('inert unmapped memory at 0x' + address.toString(16));
}
function w64(a, v) { const [dv, o] = backing(a, 8); dv.setBigUint64(o, BigInt(v), true); }

// Populate vtable in loaded image __DATA,__const at codeBase + 0x2000
// Slot at +16 points to consumer target_entry (codeBase + 0x1008), optionally PAC-signed
const rawSlotTarget = BigInt(codeBase + (data.slot_entry_offset ?? 0x1008));
const signedSlotTarget = data.pac_sign ? (rawSlotTarget | 0x8010000000000000n) : rawSlotTarget;
w64(codeBase + 0x2010, signedSlotTarget);

// Root object at 0x2000 has field_pointer at +16 -> 0x3000 (doc)
w64(0x2000 + 16, 0x3000);
// Doc object at 0x3000 has std::vector at +24: __begin_=0x4000, __end_=0x4018 (3 elements), __cap_=0x4020
w64(0x3000 + 24, 0x4000);
w64(0x3000 + 32, data.vector_empty ? 0x4008 : 0x4018);
w64(0x3000 + 40, 0x4020);
// Vector elements at 0x4000: index 0 = 0x4100, index 1 = 0x5000 (or unmapped 0x7000)
w64(0x4000, 0x4100);
w64(0x4008, data.unmapped_step_getter_input ? 0x7000 : 0x5000);
w64(0x4010, 0x4200);
// Relocated vector backing at 0x4300 preserving index 1 = 0x5000
w64(0x4300, 0x4100);
w64(0x4308, 0x5000);
w64(0x4310, 0x4200);

// Final receiver at 0x6000 has vptr at +0 -> codeBase + (data.vptr_offset ?? 0x2000), optionally PAC-signed
const rawVptr = BigInt(codeBase + (data.vptr_offset ?? 0x2000));
const signedVptr = data.pac_sign ? (rawVptr | 0x8020000000000000n) : rawVptr;
w64(0x6000, signedVptr);

class Pointer {
  constructor(value) {
    if (value instanceof Pointer) this.address = value.address;
    else if (typeof value === 'bigint') this.address = Number(value & 0x0000ffffffffffffn);
    else this.address = Number(value);
  }
  add(n) { return new Pointer(this.address + Number(n)); }
  sub(n) { return new Pointer(this.address - Number(n instanceof Pointer ? n.address : n)); }
  and(n) { return new Pointer(BigInt(this.address) & BigInt(n instanceof Pointer ? n.address : n)); }
  compare(n) { return this.address < n.address ? -1 : this.address > n.address ? 1 : 0; }
  isNull() { return this.address === 0; }
  toString() { return '0x' + this.address.toString(16); }
  readU8() { const [dv, o] = backing(this.address, 1); return dv.getUint8(o); }
  readS8() { const [dv, o] = backing(this.address, 1); return dv.getInt8(o); }
  readU16() { const [dv, o] = backing(this.address, 2); return dv.getUint16(o, true); }
  readS16() { const [dv, o] = backing(this.address, 2); return dv.getInt16(o, true); }
  readU32() { const [dv, o] = backing(this.address, 4); return dv.getUint32(o, true); }
  readS32() { const [dv, o] = backing(this.address, 4); return dv.getInt32(o, true); }
  readU64() { const [dv, o] = backing(this.address, 8); return { toString: () => dv.getBigUint64(o, true).toString() }; }
  readS64() { const [dv, o] = backing(this.address, 8); return { toString: () => dv.getBigInt64(o, true).toString() }; }
  readPointer() {
    const [dv, o] = backing(this.address, 8);
    const raw = dv.getBigUint64(o, true);
    if (data.pac_sign && (raw & 0xffff000000000000n) !== 0n && data.omit_strip) {
      return new Pointer(Number(raw & 0x0000ffffffffffffn) + 0x100000);
    }
    return new Pointer(Number(raw & 0x0000ffffffffffffn));
  }
  readByteArray(n) {
    const [, o, bytes] = backing(this.address, n);
    return bytes.slice(o, o + n).buffer;
  }
  readVolatile(n) { return this.readByteArray(n); }
}
if (!data.omit_strip) {
  Pointer.prototype.strip = function(key) {
    if (key !== 'da' && key !== 'ia') throw Error('unexpected strip key: ' + key);
    return new Pointer(this.address);
  };
}

const calls = [], consumedThis = [], stepGetterArgs = [];
const mainMod = {
  path: main,
  name: 'Owned',
  base: new Pointer(codeBase),
  size: data.image_extent,
  findSymbolByName: () => null,
  findExportByName: () => null,
};
globalThis.ObjC = { available: true, mainQueue: 'main', classes: {}, schedule: (_q, work) => work(), api: {} };
globalThis.Process = {
  id: 321,
  platform: 'darwin',
  arch: 'arm64',
  pointerSize: 8,
  mainModule: mainMod,
  getCurrentThreadId: () => 100,
  enumerateModules: () => [mainMod],
  findModuleByAddress: p => (p.address >= codeBase && p.address < codeBase + data.image_extent) ? mainMod : null,
  findRangeByAddress(p) {
    for (const [base, b] of memory) {
      if (p.address >= base && p.address < base + b.length) {
        return { base: new Pointer(base), size: b.length, protection: base === codeBase ? 'r-x' : 'rw-' };
      }
    }
    return null;
  },
};
globalThis.Memory = {
  alloc(n) {
    const addr = 0x9000;
    memory.set(addr, new Uint8Array(n));
    return new Pointer(addr);
  }
};
globalThis.NativeFunction = function(address, _ret, _args, options) {
  if (options.abi !== 'default' || options.exceptions !== 'steal') throw Error('unexpected ABI options');
  return (...values) => {
    calls.push(address.address);
    if (address.address === codeBase + 0x1000) {
      return new Pointer(0x2000);
    }
    if (address.address === codeBase + 0x1004) {
      stepGetterArgs.push(values[0].address);
      if (data.vptr_drift_on_step_getter) {
        w64(0x6000, BigInt(codeBase + 0x2008));
      }
      if (data.drift_earlier_memory_hop) {
        w64(0x2000 + 16, 0x3100);
      }
      if (data.drift_vector_end) {
        w64(0x3000 + 32, 0x4020);
      }
      if (data.drift_vector_cap) {
        w64(0x3000 + 40, 0x4028);
      }
      if (data.drift_vector_relocate) {
        w64(0x3000 + 24, 0x4300);
        w64(0x3000 + 32, 0x4318);
        w64(0x3000 + 40, 0x4320);
      }
      return new Pointer(0x6000);
    }
    if (address.address === codeBase + 0x1008) {
      consumedThis.push(values[0].address);
      return undefined;
    }
    throw Error('unexpected native call to 0x' + address.address.toString(16));
  };
};
const events = [];
globalThis.send = function(event) {
  events.push(JSON.parse(JSON.stringify(event)));
  if (data.vptr_drift_before_seal && event.event === 'native-path-resolved') {
    w64(0x6000, BigInt(codeBase + 0x2008));
  }
};
globalThis.ptr = x => new Pointer(x);
globalThis.rpc = { exports: {} };
globalThis.int64 = x => ({ toString: () => String(x) });
globalThis.uint64 = x => ({ toString: () => String(x) });
Date.now = () => 1000;

(new Function(data.source))();
(async () => {
  const result = await globalThis.rpc.exports.run({
    request: data.request,
    native_resources: data.resources,
    deadline_ms: 5000,
    pid: 321,
    bundle: data.bundle,
    executable: main,
  });
  emit(JSON.stringify({ result, calls, consumedThis, stepGetterArgs, events }));
})();
"""


def test_js_runtime_executes_v2_multihop_vtable_slot_this_adjustment_and_pac_strip(tmp_path, monkeypatch):
    node = shutil.which("node")
    jsc = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
    if node is None and not Path(jsc).is_file():
        pytest.skip("Node.js or JavaScriptCore jsc required for JS runtime fixture")

    monkeypatch.setattr(R, "assert_guest", lambda: None)
    root = tmp_path.resolve() / "session"
    root.mkdir()
    app = tmp_path.resolve() / "Owned.app"
    image = app / "Contents/MacOS/Owned"
    image.parent.mkdir(parents=True)
    image.write_bytes(_inert_macho_with_data())
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "fixture.owned",
                "CFBundleExecutable": "Owned",
                "CFBundleVersion": "1",
            }
        )
    )
    module = {"path": str(image), "sha256": _pin(image)["sha256"]}
    req = _base_vtable_request(module)
    req["vptr"]["pac_mode"] = "arm64e_strip"
    req["consumer"]["pac_mode"] = "arm64e_strip"
    req["hops"] = [
        {"kind": "field_pointer", "name": "active_doc", "field_offset": 16, "non_null": True},
        {"kind": "vector_element", "name": "open_pages", "vector_offset": 24, "index": 1, "non_null": True},
        {
            "kind": "step_getter",
            "name": "page_controller",
            "target": {"module": copy.deepcopy(module), "offset": "0x1004"},
            "abi": "default",
        },
    ]
    req["consumer"]["arguments"] = [
        {"type": "pointer", "value": {"receiver": "fresh"}},
        {"type": {"kind": "struct", "fields": [{"name": "input", "type": "int32"}]}, "value": {"input": {"input": "amount"}}},
    ]
    params = [{"name": "amount", "type": "integer"}]
    state = {
        "bundle": str(app),
        "target": R.fingerprint(app),
        "task": "Set a fixture scalar via V2 multihop receiver",
        "params": params,
        "plan": {"fixture": True},
        "success_criteria": ["Fixture scalar changes"],
        "workflow": "guest_model_judged_state_v1",
        "native_contract_mode": "declared",
        "native_receiver_mode": v2.POLICY_V2,
        "observer_profile_sha256": "f" * 64,
        "status": "investigating",
        "artifacts": {},
        "steps": [],
    }
    citation_sources = [
        {"kind": "original_code", "relative": "Contents/MacOS/Owned", "offset": "0x1000", "bytes": 4, "unit_bytes": 4},
        {"kind": "original_code", "relative": "Contents/MacOS/Owned", "offset": "0x1008", "bytes": 4, "unit_bytes": 4},
        {"kind": "original_range", "relative": "Contents/MacOS/Owned", "offset": "0x2000", "bytes": 8, "unit_bytes": 8},
        {"kind": "original_range", "relative": "Contents/MacOS/Owned", "offset": "0x2010", "bytes": 8, "unit_bytes": 8},
    ]
    for index, source in enumerate(citation_sources):
        folder = root / f"s{index:04d}"
        folder.mkdir()
        producing = {"op": "static_evidence", "request": source}
        original = A.OriginalImage(app, source["relative"])
        try:
            observation = A.original_range(original, source)
        finally:
            original.close()
        row = {"step": index, "status": "completed", "request": producing, "feedback": {"fixture": True}}
        state["steps"].append(row)
        for name, value in [("original.json", observation), ("request.json", producing), ("feedback.json", row)]:
            file = folder / name
            state["artifacts"][file.relative_to(root).as_posix()] = _write(file, value)

    decl_folder = root / "s0004"
    decl_folder.mkdir()
    request = req
    state["steps"].append({"step": 4, "status": "running", "request": request})
    _write(decl_folder / "request.json", request)
    _write(root / "session.json", state)
    declared = v2.declare(root, state, request, decl_folder)
    state["steps"][4].update(status="completed", feedback=declared)
    _write(decl_folder / "feedback.json", state["steps"][4])
    for name in [v2.RECEIPT_V2, "request.json", "feedback.json"]:
        state["artifacts"][f"s0004/{name}"] = _pin(decl_folder / name)
    _write(root / "session.json", state)

    bound = B.bind_actor(declared["actor"], {"amount": "19"})
    inv_request = B.invocation_request(bound)
    resources = B.request_resources(inv_request)
    pin = resources[str(image)]
    extent = max(int(row["offset"], 16) + row["bytes"] for row in pin["macho_segments"][0]["segments"])

    js_source = Path(B.__file__).with_name("rea_runtime_agent.js").read_text()
    js_source = js_source.replace("import ObjC from 'frida-objc-bridge';", "", 1)

    fixture_js = tmp_path / "fixture.js"
    fixture_js.write_text(_JS_V2_FIXTURE)
    payload_file = tmp_path / "payload.json"

    def run_js(**overrides):
        payload = {
            "source": js_source,
            "request": inv_request,
            "resources": copy.deepcopy(resources),
            "main_path": str(image),
            "bundle": str(app),
            "image_extent": extent,
            "loaded_image": list(image.read_bytes()),
            "pac_sign": True,
            **overrides,
        }
        payload_file.write_text(json.dumps(payload, allow_nan=False))
        cmd = [node, str(fixture_js), str(payload_file)] if node else [jsc, str(fixture_js), "--", str(payload_file)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    # 1. Happy path: 3 native calls (getter 0x101000, step_getter 0x101004, consumer 0x101008),
    #    step_getter receives 0x5000 (vector_element[1]), consumer receives 0x6000 - 16 = 0x5ff0 (this_adjustment=-16)
    good = run_js()
    res = good["result"]
    assert res["status"] == "completed", res.get("error")
    assert res["native_calls"] == 3 and res["invocations"] == 1
    assert good["calls"] == [0x101000, 0x101004, 0x101008]
    assert good["stepGetterArgs"] == [0x5000]
    assert good["consumedThis"] == [0x5FF0]
    obs_list = res["operations"][0]["native_paths"]
    NP.validate_observations(obs_list, [declared["recipe"]], for_call=True)
    B.validate_native_path_counters(inv_request, res["operations"], res["native_calls"])

    # 2. Refuses before consumer when vptr drifts before seal
    drift = run_js(vptr_drift_before_seal=True)
    assert drift["result"]["status"] == "incomplete"
    assert drift["result"]["invocations"] == 0 and drift["consumedThis"] == []
    assert "runtime vptr differs" in drift["result"]["error"]["message"]

    # 3. Refuses before consumer when vtable slot entry drifts from target_entry
    slot_drift = run_js(slot_entry_offset=0x1004)
    assert slot_drift["result"]["status"] == "incomplete"
    assert slot_drift["result"]["invocations"] == 0 and slot_drift["consumedThis"] == []
    assert "vtable slot target differs" in slot_drift["result"]["error"]["message"]

    # 4. Refuses when vector_element index is out of bounds
    vec_oob = run_js(vector_empty=True)
    assert vec_oob["result"]["status"] == "incomplete"
    assert vec_oob["result"]["invocations"] == 0 and vec_oob["stepGetterArgs"] == []
    assert "out of bounds" in vec_oob["result"]["error"]["message"]

    # 5. Refuses when arm64e_strip is requested but NativePointer.strip is unavailable
    no_strip = run_js(omit_strip=True)
    assert no_strip["result"]["status"] == "incomplete"
    assert no_strip["result"]["invocations"] == 0 and no_strip["consumedThis"] == []
    assert "NativePointer.strip" in no_strip["result"]["error"]["message"]

    # 6. Refuses at seal when an earlier memory hop before step_getter drifts during acquisition
    earlier_drift = run_js(drift_earlier_memory_hop=True)
    assert earlier_drift["result"]["status"] == "incomplete"
    assert earlier_drift["result"]["invocations"] == 0 and earlier_drift["consumedThis"] == []
    assert "pointer chain or container header drifted before seal" in earlier_drift["result"]["error"]["message"]

    # 6b. Refuses at seal when vector header (end, capacity, or relocated begin) drifts even if element pointer is unchanged
    for kw in ("drift_vector_end", "drift_vector_cap", "drift_vector_relocate"):
        vec_drift = run_js(**{kw: True})
        assert vec_drift["result"]["status"] == "incomplete"
        assert vec_drift["result"]["invocations"] == 0 and vec_drift["consumedThis"] == []
        assert "pointer chain or container header drifted before seal" in vec_drift["result"]["error"]["message"]

    # 7. Refuses before step_getter call when an earlier hop produces an unmapped pointer
    unmapped_step = run_js(unmapped_step_getter_input=True)
    assert unmapped_step["result"]["status"] == "incomplete"
    assert unmapped_step["result"]["invocations"] == 0 and unmapped_step["stepGetterArgs"] == []
    assert "not wholly readable" in unmapped_step["result"]["error"]["message"]

    # 8. Refuses before consumer call when this_adjustment moves the receiver into unmapped memory
    unmapped_adj = run_js(unmapped_this_adjustment=True)
    assert unmapped_adj["result"]["status"] == "incomplete"
    assert unmapped_adj["result"]["invocations"] == 0 and unmapped_adj["consumedThis"] == []
    assert "not wholly readable" in unmapped_adj["result"]["error"]["message"]


def test_arm64e_slice_admitted_for_v2_pac_strip_and_rejected_when_pac_mode_none(tmp_path, monkeypatch):
    node = shutil.which("node")
    jsc = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
    if node is None and not Path(jsc).is_file():
        pytest.skip("Node.js or JavaScriptCore jsc required for JS runtime fixture")

    monkeypatch.setattr(R, "assert_guest", lambda: None)
    root = tmp_path.resolve() / "session-arm64e"
    root.mkdir()
    app = tmp_path.resolve() / "OwnedArm64e.app"
    image = app / "Contents/MacOS/Owned"
    image.parent.mkdir(parents=True)
    raw_arm64 = bytearray(_inert_macho_with_data())
    raw_arm64[8:12] = (2).to_bytes(4, "little")
    image.write_bytes(bytes(raw_arm64))
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "fixture.owned.arm64e",
                "CFBundleExecutable": "Owned",
                "CFBundleVersion": "1",
            }
        )
    )
    # Default OriginalImage (plain-arm64 only) still rejects cpu_subtype=2
    with _raises(ValueError, "plain-arm64 Mach-O"):
        A.OriginalImage(app, "Contents/MacOS/Owned")

    module = {"path": str(image), "sha256": _pin(image)["sha256"]}
    params = [{"name": "amount", "type": "integer"}]
    state = {
        "bundle": str(app),
        "target": R.fingerprint(app),
        "task": "Set a fixture scalar via arm64e V2 receiver",
        "params": params,
        "plan": {"fixture": True},
        "success_criteria": ["Fixture scalar changes"],
        "workflow": "guest_model_judged_state_v1",
        "native_contract_mode": "declared",
        "native_receiver_mode": v2.POLICY_V2,
        "observer_profile_sha256": "f" * 64,
        "status": "investigating",
        "artifacts": {},
        "steps": [],
    }
    citation_sources = [
        {"kind": "original_code", "relative": "Contents/MacOS/Owned", "offset": "0x1000", "bytes": 4, "unit_bytes": 4},
        {"kind": "original_code", "relative": "Contents/MacOS/Owned", "offset": "0x1008", "bytes": 4, "unit_bytes": 4},
        {"kind": "original_range", "relative": "Contents/MacOS/Owned", "offset": "0x2000", "bytes": 8, "unit_bytes": 8},
        {"kind": "original_range", "relative": "Contents/MacOS/Owned", "offset": "0x2010", "bytes": 8, "unit_bytes": 8},
    ]
    for index, source in enumerate(citation_sources):
        folder = root / f"s{index:04d}"
        folder.mkdir()
        producing = {"op": "static_evidence", "request": source}
        built = A.build(app, source, folder / "ev")
        observation = json.loads(Path(built["receipt"]).read_text())
        row = {"step": index, "status": "completed", "request": producing, "feedback": {"fixture": True}}
        state["steps"].append(row)
        for name, value in [("original.json", observation), ("request.json", producing), ("feedback.json", row)]:
            file = folder / name
            state["artifacts"][file.relative_to(root).as_posix()] = _write(file, value)

    # Without pac_mode='arm64e_strip', arm64e image is rejected by declare()
    no_pac_req = _base_vtable_request(module)
    step4_bad = root / "s0004"
    step4_bad.mkdir()
    bad_state = copy.deepcopy(state)
    bad_state["steps"].append({"step": 4, "status": "running", "request": no_pac_req})
    _write(step4_bad / "request.json", no_pac_req)
    _write(root / "session.json", bad_state)
    with _raises(ValueError, "plain-arm64 Mach-O"):
        v2.declare(root, bad_state, no_pac_req, step4_bad)

    # With pac_mode='arm64e_strip' on both vptr and consumer, arm64e is admitted end-to-end
    shutil.rmtree(step4_bad)
    req = _base_vtable_request(module)
    req["vptr"]["pac_mode"] = "arm64e_strip"
    req["consumer"]["pac_mode"] = "arm64e_strip"
    req["hops"] = [
        {"kind": "field_pointer", "name": "active_doc", "field_offset": 16, "non_null": True},
        {"kind": "vector_element", "name": "open_pages", "vector_offset": 24, "index": 1, "non_null": True},
        {
            "kind": "step_getter",
            "name": "page_controller",
            "target": {"module": copy.deepcopy(module), "offset": "0x1004"},
            "abi": "default",
        },
    ]
    decl_folder = root / "s0004"
    decl_folder.mkdir()
    state["steps"].append({"step": 4, "status": "running", "request": req})
    _write(decl_folder / "request.json", req)
    _write(root / "session.json", state)
    declared = v2.declare(root, state, req, decl_folder)
    state["steps"][4].update(status="completed", feedback=declared)
    _write(decl_folder / "feedback.json", state["steps"][4])
    for name in [v2.RECEIPT_V2, "request.json", "feedback.json"]:
        state["artifacts"][f"s0004/{name}"] = _pin(decl_folder / name)
    _write(root / "session.json", state)

    bound = B.bind_actor(declared["actor"], {"amount": "19"})
    inv_request = B.invocation_request(bound)
    resources = B.request_resources(inv_request)
    pin = resources[str(image)]
    assert pin["macho_images"][0]["cpu_subtype"] == 2
    extent = max(int(row["offset"], 16) + row["bytes"] for row in pin["macho_segments"][0]["segments"])

    js_source = Path(B.__file__).with_name("rea_runtime_agent.js").read_text()
    js_source = js_source.replace("import ObjC from 'frida-objc-bridge';", "", 1)
    fixture_js = tmp_path / "fixture-arm64e.js"
    fixture_js.write_text(_JS_V2_FIXTURE)
    payload_file = tmp_path / "payload-arm64e.json"
    payload = {
        "source": js_source,
        "request": inv_request,
        "resources": copy.deepcopy(resources),
        "main_path": str(image),
        "bundle": str(app),
        "image_extent": extent,
        "loaded_image": list(image.read_bytes()),
        "pac_sign": True,
    }
    payload_file.write_text(json.dumps(payload, allow_nan=False))
    cmd = [node, str(fixture_js), str(payload_file)] if node else [jsc, str(fixture_js), "--", str(payload_file)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"]["status"] == "completed", out["result"].get("error")
    assert out["result"]["invocations"] == 1
    assert out["consumedThis"] == [0x5FF0]


def test_agent_step_correlate_trace_and_automatic_crash_blocklist_on_failed_runtime(tmp_path, monkeypatch):
    root, state, result, image, request = _build_declared_session(tmp_path, monkeypatch)
    monkeypatch.setattr(G, "_files", lambda _params, _plan: [])
    monkeypatch.setattr(G, "_plan", lambda _params, _plan: None)
    monkeypatch.setattr(G, "_observer_declaration", lambda profile, *_a, **_kw: profile)
    monkeypatch.setattr(G, "_public_observer", lambda profile: profile)
    state["max_steps"] = 24
    state["deadline"] = 9999999999.0
    state["rea"] = "/bin/echo"
    state["plan_sha256"] = R.digest(state["plan"])
    state["frozen_files"] = []
    state["recordings"] = {}
    state["proof"] = None
    state["format"] = G.SESSION
    _write(root / "session.json", state)

    # 1. Run correlate_native_trace_v2 via G.step
    trace_req = {
        "op": "correlate_native_trace_v2",
        "baseline_calls": [{"offset": "0x900", "x0": "0x60000100", "scalar_args": [0]}],
        "observed_getters": [{"offset": "0x1000", "returned_x0": "0x60000200", "vptr_rva": "0x2000"}],
        "actuated_calls": [
            {
                "offset": "0x1008",
                "x0": "0x60000200",
                "scalar_args": [7],
                "vtable_slot_offset": 16,
                "vptr_rva": "0x2000",
                "this_adjustment": -16,
            }
        ],
        "target_input_value": 7,
        "module_sha256": _pin(image)["sha256"],
    }
    step_out = G.step(root, trace_req)
    assert step_out["feedback"]["status"] == "completed"
    fb = step_out["feedback"]["feedback"]
    assert fb["status"] == "native_receiver_trace_correlated"
    assert fb["candidate_count"] == 1
    assert fb["candidates"][0]["consumer_offset"] == "0x1008"

    # 2. Pre-dispatch failures (unavailable transport or visual precondition refusal with actuated=False) do NOT blocklist
    bound = B.bind_actor(result["actor"], {"amount": "7"})
    inv_req = B.invocation_request(bound)

    class _UnavailableProvider:
        name = "native"

        @staticmethod
        def transaction(_bundle, out_dir, _tx, **_kw):
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            receipt = {
                "status": "unavailable",
                "actuated": False,
                "native_calls": False,
                "resource_review_required": False,
                "error": {"type": "Unavailable", "message": "transport temporarily offline before attach"},
                "receipt": str(Path(out_dir) / "runtime.json"),
            }
            _write(Path(out_dir) / "runtime.json", receipt)
            return receipt

    monkeypatch.setattr(P, "for_name", lambda _name: _UnavailableProvider)
    unavail_step = G.step(root, {"op": "runtime", "provider": "native", "request": inv_req})
    assert unavail_step["feedback"]["status"] == "completed"
    assert "crash_blocklist" not in unavail_step["context"]

    pre_trial_state = json.loads((root / "session.json").read_text())
    assert (
        v2.record_failed_runtime_or_trial(
            pre_trial_state,
            result["actor"],
            {
                "status": "refused",
                "actuated": False,
                "resource_review_required": False,
                "error": {"type": "ValueError", "message": "visual precondition judgment did not pass"},
            },
            step=99,
        )
        is None
    )
    assert (
        v2.record_failed_runtime_or_trial(
            pre_trial_state,
            result["actor"],
            {
                "status": "refused",
                "actuated": None,
                "resource_review_required": True,
                "error": {"type": "ValueError", "message": "runtime effects, target identity or cleanup are uncertain"},
                "runtime_disposition": {
                    "status": "unavailable",
                    "actuated": False,
                    "native_calls": False,
                    "resource_review_required": False,
                    "native_state_uncertain": False,
                    "worker_native_calls": None,
                    "attached": None,
                    "error": {"type": "Unavailable", "message": "transport temporarily offline before attach"},
                },
            },
            step=100,
        )
        is None
    )
    assert (
        v2.record_failed_runtime_or_trial(
            pre_trial_state,
            result["actor"],
            {
                "status": "refused",
                "actuated": None,
                "resource_review_required": True,
                "error": {"type": "ValueError", "message": "runtime effects, target identity or cleanup are uncertain"},
                "runtime_disposition": {
                    "status": "incomplete",
                    "actuated": False,
                    "native_calls": False,
                    "resource_review_required": False,
                    "native_state_uncertain": False,
                    "worker_native_calls": 0,
                    "attached": True,
                    "error": {"type": "RuntimeError", "message": "script.load failed before run RPC"},
                },
            },
            step=101,
        )
        is None
    )
    assert "crash_blocklist" not in pre_trial_state

    # 3. Cross-session runtime execution of another session's V2 declaration is refused before provider.transaction
    other_dir = tmp_path / "other_session"
    other_dir.mkdir()
    other_root, other_state, _, _, _ = _build_declared_session(other_dir, monkeypatch)
    other_state["max_steps"] = 24
    other_state["deadline"] = 9999999999.0
    other_state["rea"] = "/bin/echo"
    other_state["plan_sha256"] = R.digest(other_state["plan"])
    other_state["frozen_files"] = []
    other_state["recordings"] = {}
    other_state["proof"] = None
    other_state["format"] = G.SESSION
    _write(other_root / "session.json", other_state)
    foreign_step = G.step(other_root, {"op": "runtime", "provider": "native", "request": inv_req})
    assert foreign_step["feedback"]["status"] == "failed"
    assert "belongs to a different or changed current session" in foreign_step["feedback"]["feedback"]["error"]

    # 4. Simulate a crashing V2 runtime step via G.step(op='runtime')
    provider_calls = 0

    class _CrashingProvider:
        name = "native"

        @staticmethod
        def transaction(_bundle, out_dir, _tx, **_kw):
            nonlocal provider_calls
            provider_calls += 1
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            receipt = {
                "status": "incomplete",
                "actuated": None,
                "native_state_uncertain": True,
                "resource_review_required": False,
                "error": {"type": "CrashError", "message": "process terminated with SIGSEGV during consumer 0x1008"},
                "receipt": str(Path(out_dir) / "runtime.json"),
            }
            _write(Path(out_dir) / "runtime.json", receipt)
            return receipt

    monkeypatch.setattr(P, "for_name", lambda _name: _CrashingProvider)
    crash_step = G.step(root, {"op": "runtime", "provider": "native", "request": inv_req})
    assert crash_step["feedback"]["status"] == "completed"
    assert provider_calls == 1
    assert "crash_blocklist" in crash_step["context"]
    assert crash_step["context"]["crash_blocklist"]["blocked_count"] == 1
    assert (root / "s0007/crash-blocklist.json").is_file()

    # 5. Subsequent correlate_native_trace_v2 step reports the blocked signature on the candidate
    trace_step2 = G.step(root, trace_req)
    cand = trace_step2["feedback"]["feedback"]["candidates"][0]
    assert cand["crash_blocked_count"] == 1
    assert cand["crash_blocked_argument_signatures"] == [["pointer", "int32"]]

    # 6. Re-using the crashed declaration via G.step(op='runtime'), resources(), validate_declaration(), or re-declaring is refused
    retry_runtime = G.step(root, {"op": "runtime", "provider": "native", "request": inv_req})
    assert retry_runtime["feedback"]["status"] == "failed"
    assert "crash-blocklisted" in retry_runtime["feedback"]["feedback"]["error"]
    assert provider_calls == 1
    updated_state = json.loads((root / "session.json").read_text())
    with _raises(ValueError, "crash-blocklisted"):
        v2.resources(result["actor"])
    with _raises(ValueError, "crash-blocklisted"):
        v2.validate_declaration(result["actor"], state=updated_state)
    res_redeclare = G.step(root, request)
    assert res_redeclare["feedback"]["status"] == "failed"
    assert "crash-blocklisted" in res_redeclare["feedback"]["feedback"]["error"]

    # 7. Declaring a corrected ABI signature (int64 instead of int32) succeeds in the same session!
    corrected_req = copy.deepcopy(request)
    corrected_req["consumer"]["arguments"][1]["type"] = "int64"
    ok_step = G.step(root, corrected_req)
    assert ok_step["feedback"]["status"] == "completed", ok_step["feedback"]
    assert ok_step["feedback"]["feedback"]["status"] == "native_receiver_v2_hypothesis_declared"

    # 8. Declaring and running G.step(op='runtime') with a POD struct field named 'signature_evidence' succeeds
    struct_req = copy.deepcopy(request)
    struct_req["consumer"]["arguments"][1] = {
        "type": {"kind": "struct", "fields": [{"name": "signature_evidence", "type": "int32"}]},
        "value": {"signature_evidence": {"input": "amount"}},
    }
    struct_decl = G.step(root, struct_req)
    assert struct_decl["feedback"]["status"] == "completed", struct_decl["feedback"]
    struct_actor = B.bind_actor(struct_decl["feedback"]["feedback"]["actor"], {"amount": "7"})
    struct_tx = B.invocation_request(struct_actor)
    monkeypatch.setattr(P, "for_name", lambda _name: _UnavailableProvider)
    struct_run = G.step(root, {"op": "runtime", "provider": "native", "request": struct_tx})
    assert struct_run["feedback"]["status"] == "completed", struct_run["feedback"]


def test_real_clang_compiled_cpp_multiple_inheritance_vtable_and_vector_pipeline(tmp_path, monkeypatch):
    import platform
    import sys
    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("macOS arm64 required to compile and run real C++ Mach-O binary")
    clangxx = shutil.which("clang++") or "/usr/bin/clang++"
    node = shutil.which("node")
    jsc = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
    if not Path(clangxx).is_file():
        pytest.skip("clang++ required to compile real C++ Mach-O binary")
    if node is None and not Path(jsc).is_file():
        pytest.skip("Node.js or JavaScriptCore jsc required for JS runtime fixture")

    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = tmp_path.resolve() / "RealCppApp.app"
    exe = app / "Contents/MacOS/RealCppApp"
    exe.parent.mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "fixture.realcpp",
                "CFBundleExecutable": "RealCppApp",
                "CFBundleVersion": "1",
            }
        )
    )

    cpp_src = tmp_path / "real_cpp_target.cc"
    cpp_src.write_text(
        r"""
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <mach-o/dyld.h>

struct PrimaryBase {
    uint64_t primary_tag = 0x1111222233334444ULL;
    virtual ~PrimaryBase() = default;
    virtual int primaryId() const { return 10; }
};

struct SecondaryConsumer {
    uint64_t secondary_tag = 0x5555666677778888ULL;
    virtual ~SecondaryConsumer() = default;
    virtual void padSlot() {}
    virtual void setVolume(int32_t level) = 0;
};

struct MixerChannel : PrimaryBase, SecondaryConsumer {
    int32_t volume = 0;
    __attribute__((noinline)) void setVolume(int32_t level) override {
        volume = level;
    }
};

struct AudioSession {
    uint64_t flags = 1;
    uint64_t reserved = 2;
    uint64_t pad = 3;
    std::vector<MixerChannel*> channels;
};

struct EngineRoot {
    uint64_t magic = 0xfeedfaceULL;
    uint64_t version = 1;
    AudioSession* active_session = nullptr;
};

static MixerChannel g_ch0;
static MixerChannel g_ch1;
static AudioSession g_session;
static EngineRoot g_root;

extern "C" __attribute__((noinline, used)) EngineRoot* GetEngineRoot() {
    return &g_root;
}

extern "C" __attribute__((noinline, used)) SecondaryConsumer* AsSecondaryConsumer(MixerChannel* ch) {
    return static_cast<SecondaryConsumer*>(ch);
}

int main(int argc, char** argv) {
    g_session.channels.push_back(&g_ch0);
    g_session.channels.push_back(&g_ch1);
    g_root.active_session = &g_session;
    g_ch1.setVolume(5);

    const uintptr_t base = reinterpret_cast<uintptr_t>(_dyld_get_image_header(0));
    SecondaryConsumer* sec = AsSecondaryConsumer(&g_ch1);
    const uintptr_t sec_vptr = *reinterpret_cast<const uintptr_t*>(sec);
    const uintptr_t slot_fn = *reinterpret_cast<const uintptr_t*>(sec_vptr + 24);
    const intptr_t this_adj = reinterpret_cast<intptr_t>(&g_ch1) - reinterpret_cast<intptr_t>(sec);

    std::printf(
        "{\"getter_rva\":\"0x%llx\",\"step_rva\":\"0x%llx\",\"vptr_rva\":\"0x%llx\",\"slot_fn_rva\":\"0x%llx\",\"slot_offset\":24,\"this_adjustment\":%lld}\n",
        static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(&GetEngineRoot) - base),
        static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(&AsSecondaryConsumer) - base),
        static_cast<unsigned long long>(sec_vptr - base),
        static_cast<unsigned long long>(slot_fn - base),
        static_cast<long long>(this_adj)
    );
    return 0;
}
"""
    )
    compile_proc = subprocess.run(
        [
            clangxx,
            "-std=c++17",
            "-arch",
            "arm64",
            "-O1",
            "-fno-pie",
            "-Wl,-no_pie",
            str(cpp_src),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if compile_proc.returncode != 0:
        # Fallback without -Wl,-no_pie on newer ld64
        compile_proc = subprocess.run(
            [clangxx, "-std=c++17", "-arch", "arm64", "-O1", str(cpp_src), "-o", str(exe)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    probe_proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=15, check=True)
    layout = json.loads(probe_proc.stdout.strip().splitlines()[-1])
    assert layout["this_adjustment"] == -16
    assert layout["slot_offset"] == 24

    # Build V2 static evidence and declaration directly against the real clang++-compiled Mach-O binary
    root = tmp_path.resolve() / "session-real-cpp"
    root.mkdir()
    module = {"path": str(exe), "sha256": _pin(exe)["sha256"]}
    params = [{"name": "amount", "type": "integer"}]
    state = {
        "bundle": str(app),
        "target": R.fingerprint(app),
        "task": "Set channel volume on real compiled C++ Mach-O binary",
        "params": params,
        "plan": {"fixture": True},
        "success_criteria": ["Channel volume updated"],
        "workflow": "guest_model_judged_state_v1",
        "native_contract_mode": "declared",
        "native_receiver_mode": v2.POLICY_V2,
        "observer_profile_sha256": "f" * 64,
        "status": "investigating",
        "artifacts": {},
        "steps": [],
    }
    slot_data_offset = hex(int(layout["vptr_rva"], 16) + layout["slot_offset"])
    citation_sources = [
        {"kind": "original_code", "relative": "Contents/MacOS/RealCppApp", "offset": layout["getter_rva"], "bytes": 4, "unit_bytes": 4},
        {"kind": "original_code", "relative": "Contents/MacOS/RealCppApp", "offset": layout["slot_fn_rva"], "bytes": 4, "unit_bytes": 4},
        {"kind": "original_range", "relative": "Contents/MacOS/RealCppApp", "offset": layout["vptr_rva"], "bytes": 8, "unit_bytes": 8},
        {"kind": "original_range", "relative": "Contents/MacOS/RealCppApp", "offset": slot_data_offset, "bytes": 8, "unit_bytes": 8},
    ]
    for index, source in enumerate(citation_sources):
        folder = root / f"s{index:04d}"
        folder.mkdir()
        producing = {"op": "static_evidence", "request": source}
        built = A.build(app, source, folder / "ev")
        observation = json.loads(Path(built["receipt"]).read_text())
        row = {"step": index, "status": "completed", "request": producing, "feedback": {"fixture": True}}
        state["steps"].append(row)
        for name, value in [("original.json", observation), ("request.json", producing), ("feedback.json", row)]:
            file = folder / name
            state["artifacts"][file.relative_to(root).as_posix()] = _write(file, value)

    req = {
        "op": "declare_native_receiver_v2",
        "getter": {
            "kind": "native_function",
            "target": {"module": copy.deepcopy(module), "offset": layout["getter_rva"]},
            "abi": "default",
            "arguments": [],
            "return_type": "pointer",
        },
        "hops": [
            {"kind": "field_pointer", "name": "active_session", "field_offset": 16, "non_null": True},
            {"kind": "vector_element", "name": "channels", "vector_offset": 24, "index": 1, "non_null": True},
            {
                "kind": "step_getter",
                "name": "as_secondary_consumer",
                "target": {"module": copy.deepcopy(module), "offset": layout["step_rva"]},
                "abi": "default",
            },
        ],
        "consumer": {
            "kind": "native_function",
            "dispatch": "vtable_slot",
            "vtable_slot_offset": layout["slot_offset"],
            "target_entry": {"module": copy.deepcopy(module), "offset": layout["slot_fn_rva"]},
            "this_adjustment": layout["this_adjustment"],
            "abi": "default",
            "arguments": [
                {"type": "pointer", "value": {"receiver": "fresh"}},
                {"type": "int32", "value": {"input": "amount"}},
            ],
            "return_type": "void",
        },
        "vptr": {
            "image": copy.deepcopy(module),
            "data_offset": layout["vptr_rva"],
        },
        "citations": [
            {"role": "getter_code", "artifact": "s0000/original.json", "pointer": "/range"},
            {"role": "consumer_code", "artifact": "s0001/original.json", "pointer": "/range"},
            {"role": "vptr_data", "artifact": "s0002/original.json", "pointer": "/range"},
            {"role": "vtable_slot_data", "artifact": "s0003/original.json", "pointer": "/range"},
        ],
    }
    decl_folder = root / "s0004"
    decl_folder.mkdir()
    state["steps"].append({"step": 4, "status": "running", "request": req})
    _write(decl_folder / "request.json", req)
    _write(root / "session.json", state)
    declared = v2.declare(root, state, req, decl_folder)
    state["steps"][4].update(status="completed", feedback=declared)
    _write(decl_folder / "feedback.json", state["steps"][4])
    for name in [v2.RECEIPT_V2, "request.json", "feedback.json"]:
        state["artifacts"][f"s0004/{name}"] = _pin(decl_folder / name)
    _write(root / "session.json", state)

    bound = B.bind_actor(declared["actor"], {"amount": "77"})
    inv_request = B.invocation_request(bound)
    resources = B.request_resources(inv_request)
    assert str(exe) in resources and declared["receipt"] in resources


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
