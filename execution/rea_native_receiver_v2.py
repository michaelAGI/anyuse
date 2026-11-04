"""V2 Binary-Correlated C++ Receiver Hypotheses: Vtable Dispatch, Multi-Hop Chains & Crash-Isolated Discovery.

Capabilities added over V1 (`rea_native_receiver_contract.py`):
1. Virtual method dispatch (`dispatch="vtable_slot"`, `vtable_slot_offset`) in addition to `"direct"`.
2. Bounded multi-hop pointer traversal (`hops` up to MAX_HOPS=3: `field_pointer`, `step_getter`,
   `array_element` pointer slots, or `vector_element` contiguous pointer elements).
3. Bounded multiple-inheritance subobject `this_adjustment` (-4096..4096, 8-byte aligned) and `subobject_offset`.
4. Crash-isolated hypothesis ledger (`CrashBlocklist`) to prevent re-probing crashing ABI guesses.
5. Differential UI-to-native trace correlator (`correlate_differential_trace`) to rank candidate
   `(getter, consumer, vptr, vtable_slot)` tuples captured during a single UI/AX actuation.
"""
from __future__ import annotations

import copy
import hashlib
import heapq
import importlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

FORMAT_V2 = "anyuse.rea-native-receiver-model-declaration/2"
RECIPE_FORMAT_V2 = "anyuse.rea-native-receiver-hypothesis/2"
KIND_V2 = "declared_native_receiver_v2"
MODE_V2 = "main_queue_binary_runtime_hypothesis_v2"
POLICY_V2 = "binary_runtime_hypothesis"
ROOT_CONTRACT_V2 = "anyuse.rea-native-receiver-hypothesis-root/2"
LIFETIME_CONTRACT_V2 = "anyuse.rea-native-receiver-hypothesis-lifetime/2"
VPTR_CONTRACT_V2 = "anyuse.rea-native-receiver-hypothesis-vptr/2"
CONSUMER_CONTRACT_V2 = "anyuse.rea-native-receiver-hypothesis-consumer/2"
TYPE_BASIS_V2 = "model_declared_binary_correlated_v2_not_independently_reviewed"
RECEIPT_V2 = "native-receiver-declaration-v2.json"
CRASH_LEDGER_FORMAT = "anyuse.rea-native-receiver-crash-ledger/1"
TRACE_CORRELATION_FORMAT = "anyuse.rea-native-receiver-trace-correlation/1"
BINDING_KEYS = (
    "bundle", "target", "task", "params", "plan", "success_criteria", "workflow",
    "observer_profile_sha256", "native_contract_mode", "native_receiver_mode",
)

MAX_BYTES = 256 * 1024
MAX_HOPS = 3
MAX_VTABLE_SLOT_OFFSET = 1024
MAX_THIS_ADJUSTMENT = 4096
MAX_FIELD_OFFSET = 65536
MAX_DATA_OFFSET = 512 * 1024 * 1024
MAX_TRACE_ROWS = 256
MAX_TRACE_CANDIDATES = 256

HEX_OFFSET = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,7})\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TRACE_POINTER = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})\Z")

BORROWED_SCALAR_TYPES = frozenset({
    "int8", "uint8", "int16", "uint16", "int32", "uint32",
    "int64", "uint64", "long", "ulong", "float", "double", "bool",
})
ALLOWED_QUEUE_POLICIES = frozenset({"main"})
ALLOWED_PAC_MODES = frozenset({"none", "arm64e_strip"})
USER_ARM64E_ADDRESS_MASK = 0x00007FFFFFFFFFFF

BASIS_V2 = {
    "abi_basis": "model_declared",
    "receiver_basis": "binary_correlated_hypothesis_v2",
    "lifetime_basis": "assumed_not_established",
    "abi_recovered": False,
    "type_proven": False,
    "lifetime_proven": False,
    "proof_created": False,
}


def discovery_guide(allowed_tools: Any) -> dict[str, Any] | None:
    """Data-only syntax for the caller-admitted V2 tools, never target facts."""
    sections = set(allowed_tools) & {"declare_native_receiver_v2", "correlate_native_trace_v2"}
    if not sections:
        return None
    image = {"path": "/example/Owned.app/Contents/MacOS/Owned", "sha256": "a" * 64}
    entry = lambda offset: {"module": copy.deepcopy(image), "offset": offset}
    guide = {
        "format": "anyuse.rea-native-receiver-guide/2",
        "scope": "Opt-in binary-correlated C++ hypotheses; examples are syntax, not observations or ready mappings.",
        "requirements": {"native_contract_mode": "declared", "native_receiver_mode": POLICY_V2},
        "evidence_basis": dict(BASIS_V2),
        "rules": [
            "Replace all example paths, hashes, entries, fields, citations and parameter names with current-session observations. Never execute an example or invent a live receiver from a symbol name.",
            "Original code/data observations are static_evidence artifacts. Read their registered evidence before declaring. Replays reacquire the root and recheck resources; old process pointers are never reusable receivers.",
            "A declaration and trace ranking are hypotheses only. Keep the caller-frozen independent observer, learning/fresh/held-out trials, witnessed restart and catalog/replay gates. Type and lifetime are not established by readable memory or a matching vptr.",
        ],
    }
    if "declare_native_receiver_v2" in sections:
        guide["declare_native_receiver_v2"] = {
            "example": {
                "op": "declare_native_receiver_v2", "queue_policy": "main",
                "getter": {"kind": "native_function", "target": entry("0x1000"), "abi": "default", "arguments": [], "return_type": "pointer"},
                "hops": [],
                "consumer": {"kind": "native_function", "dispatch": "vtable_slot", "vtable_slot_offset": 16,
                             "target_entry": entry("0x1008"), "this_adjustment": 0, "abi": "default",
                             "arguments": [{"type": "pointer", "value": {"receiver": "fresh"}},
                                           {"type": "int32", "value": {"input": "example_amount"}}],
                             "return_type": "void"},
                "vptr": {"image": copy.deepcopy(image), "data_offset": "0x2000"},
                "citations": [{"role": role, "artifact": f"s{idx:04d}/original.json", "pointer": "/range"}
                              for idx, role in enumerate(("getter_code", "consumer_code", "vptr_data", "vtable_slot_data"))],
            },
            "hop_examples": [
                {"kind": "field_pointer", "name": "example_child", "field_offset": 16, "non_null": True},
                {"kind": "step_getter", "name": "example_getter", "target": entry("0x1004"), "abi": "default"},
                {"kind": "array_element", "name": "example_slot", "base_offset": 16, "index": 0, "stride": 8, "non_null": True},
                {"kind": "vector_element", "name": "example_vector", "vector_offset": 16, "index": 0, "non_null": True},
            ],
            "rules": [
                "The root is a zero-argument pointer getter. At most three hops: field_pointer, step_getter, array_element (pointer slots), vector_element (contiguous pointer elements). No arbitrary object layout inference or saved pointer values.",
                "For direct dispatch use target, omit target_entry/vtable_slot_offset and omit the fourth citation. For a vtable slot, retain the exact target_entry and separate slot-data citation. Citation roles/order must match the example; code uses original_code/unit_bytes=4, data original_range/unit_bytes=8.",
                "The only queue_policy is main. Arg0 is the fresh receiver. Up to seven tail arguments use supported scalars, utf8, bounded buffers or scalar POD structs; return is scalar or void. Bind only a caller-declared parameter, never a hidden case value.",
                "this_adjustment is 8-byte aligned in [-4096,4096]; slot offsets are 8-byte aligned in [0,1024]. vptr.subobject_offset is separately observed. Runtime readability, full-chain seal and exact vptr/slot checks do not prove ownership or lifetime.",
                "Arm64e is explicitly opt-in: vptr.pac_mode=arm64e_strip and, for virtual dispatch, consumer.pac_mode=arm64e_strip. PAC stripping is not ABI or lifetime recovery; live arm64e execution needs separate validation.",
            ],
        }
    if "correlate_native_trace_v2" in sections:
        guide["correlate_native_trace_v2"] = {
            "example": {"op": "correlate_native_trace_v2", "baseline_calls": [],
                        "actuated_calls": [{"offset": "0x1008", "x0": "0x4000", "scalar_args": [7]}],
                        "observed_getters": [{"offset": "0x1000", "returned_x0": "0x4000"}],
                        "target_input_value": 7, "module_sha256": image["sha256"]},
            "rules": [
                "Ranks supplied observations only; it does not collect a trace, press UI controls, attach to a process or invoke native code. Supply actual observations from one image/process context, never invented traces. Correlation creates no static evidence or proof.",
                "Each list has at most 256 rows. Calls have offset and optional x0/scalar_args/vptr_rva/vtable_slot_offset/this_adjustment; actuated calls need x0. Getters have offset/returned_x0 and optional vptr_rva. Offsets and pointer strings are bounded lowercase hex without leading zeros. Scalar arguments are finite numbers or booleans, at most seven per call.",
                "At most 256 ranked candidates are returned with matched_candidate_count and candidates_truncated. Ephemeral receiver addresses are hints only, never a saved receiver. Crash-blocked signatures remain annotated; execution still enforces the session blocklist.",
            ],
        }
    return guide


def _path():
    if __package__:
        from . import rea_native_path
    else:
        import rea_native_path
    return rea_native_path


def _bridge():
    if __package__:
        from . import rea_runtime_bridge
    else:
        import rea_runtime_bridge
    return rea_runtime_bridge


def _resource_module(name: str):
    if __package__:
        return importlib.import_module("." + name, __package__)
    directory = Path(__file__).resolve().parent
    fullname = "execution." + name
    for module_name, expected in (("execution", directory / "__init__.py"),
                                  (fullname, directory / (name + ".py"))):
        existing = sys.modules.get(module_name)
        if existing is not None:
            actual = getattr(existing, "__file__", None)
            _need(type(actual) is str and Path(actual).resolve() == expected,
                  "standalone receiver v2 resource import found a foreign package/module")
            if module_name == "execution":
                _need([Path(entry).resolve() for entry in getattr(existing, "__path__", ())] == [directory],
                      "standalone receiver v2 resource import found a foreign package search path")
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(directory.parent))
        module = importlib.import_module(fullname)
        _need(Path(module.__file__).resolve(strict=True) == directory / (name + ".py"),
              "standalone receiver v2 resource import escaped its installed sibling package")
        return module
    finally:
        sys.path[:] = previous


def _need(ok: bool, why: str) -> None:
    if not ok:
        raise ValueError(why)


def _same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(_same(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _keys(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    _need(
        type(value) is dict and required <= set(value) <= (required | optional),
        f"closed object keys required: expected {sorted(required)}",
    )


def _bounded(value: Any) -> None:
    _need(len(json.dumps(value, allow_nan=False).encode()) <= MAX_BYTES, "declaration exceeds byte budget")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def strip_arm64e_pac(pointer_value: int) -> int:
    """Strip Apple Silicon arm64e user-space Pointer Authentication Code (PAC) upper bits."""
    _need(type(pointer_value) is int and 0 <= pointer_value < (1 << 64), "64-bit unsigned integer pointer required")
    return pointer_value & USER_ARM64E_ADDRESS_MASK


def encode_libcxx_sso_string(
    text: str,
    *,
    layout: str = "apple_arm64",
    ownership: str = "borrowed_const_ref",
) -> dict[str, Any]:
    """Encode a UTF-8 string (<= 22 bytes) into a 24-byte libc++ ABI v1 short-string (SSO) buffer.

    Restricted to borrowed `const std::string&` reference parameters (`ownership='borrowed_const_ref'`)
    where the callee reads the 24-byte inline SSO storage without taking heap ownership or requiring
    caller-side non-trivial destructor cleanup.
    """
    _need(type(text) is str and "\0" not in text, "non-null UTF-8 string required")
    _need(layout in {"apple_arm64", "llvm_lsb"}, "layout must be 'apple_arm64' or 'llvm_lsb'")
    _need(ownership == "borrowed_const_ref", "SSO buffer helper supports only borrowed_const_ref std::string parameters")
    raw = text.encode("utf-8")
    _need(len(raw) <= 22, "libc++ 24-byte SSO string requires at most 22 UTF-8 bytes")
    buf = [0] * 24
    if layout == "apple_arm64":
        for i, b in enumerate(raw):
            buf[i] = b
        buf[23] = len(raw)
    else:
        buf[0] = (len(raw) << 1) & 0xFF
        for i, b in enumerate(raw):
            buf[1 + i] = b
    return {
        "type": {"kind": "buffer", "bytes": 24, "direction": "in"},
        "value": buf,
        "hex": bytes(buf).hex(),
        "layout": layout,
        "ownership": ownership,
    }


def _validate_image(image: Any) -> None:
    _keys(image, {"path", "sha256"})
    _need(
        type(image["path"]) is str
        and 0 < len(image["path"]) <= 4096
        and image["path"].startswith("/")
        and "\0" not in image["path"]
        and ".." not in PurePosixPath(image["path"]).parts
        and str(PurePosixPath(image["path"])) == image["path"]
        and type(image["sha256"]) is str
        and SHA256_RE.fullmatch(image["sha256"]) is not None,
        "canonical absolute image path and lowercase sha256 required",
    )


def _validate_code_entry(entry: Any) -> None:
    _keys(entry, {"module", "offset"})
    _validate_image(entry["module"])
    _need(
        type(entry["offset"]) is str
        and HEX_OFFSET.fullmatch(entry["offset"]) is not None
        and int(entry["offset"], 16) <= MAX_DATA_OFFSET - 4
        and int(entry["offset"], 16) % 4 == 0,
        "exact 4-byte-aligned arm64 code offset required",
    )


def evidence_claim(value: Any) -> None:
    _keys(value, {"kind", "file", "sha256", "pointer"})
    _need(value["kind"] == KIND_V2, "separate V2 experimental receiver evidence kind required")
    _validate_image({"path": value["file"], "sha256": value["sha256"]})
    allowed_pointers = {
        "/signatures/getter",
        "/signatures/consumer",
        *(f"/signatures/hop_{i}" for i in range(MAX_HOPS)),
        *(f"/contracts/{i}" for i in range(4)),
    }
    _need(value["pointer"] in allowed_pointers, "exact V2 receiver contract selector required")


def validate_hop(hop: Any, index: int, *, allow_signature_evidence: bool = False) -> dict[str, Any]:
    """Validate a single pointer-chain hop (`field_pointer`, `step_getter`, `array_element`, or `vector_element`).

    `array_element` and `vector_element` are strictly restricted to 8-byte pointer elements (`element_kind == 'pointer'`).
    """
    _need(type(hop) is dict and "kind" in hop, f"hop[{index}] must be a typed dict")
    kind = hop["kind"]
    _need(
        kind in {"field_pointer", "step_getter", "array_element", "vector_element"},
        f"unsupported hop kind: {kind}",
    )
    if kind == "field_pointer":
        _keys(hop, {"kind", "name", "field_offset", "non_null"})
        _need(
            type(hop["name"]) is str and 1 <= len(hop["name"]) <= 64,
            "bounded hop name required",
        )
        _need(
            type(hop["field_offset"]) is int
            and 0 <= hop["field_offset"] <= MAX_FIELD_OFFSET
            and hop["field_offset"] % 8 == 0,
            "8-byte-aligned non-negative field_offset required",
        )
        _need(hop["non_null"] is True, "pointer hop must enforce non_null guard")
    elif kind == "step_getter":
        if allow_signature_evidence:
            _keys(hop, {"kind", "name", "target", "abi", "signature_evidence"})
            evidence_claim(hop["signature_evidence"])
            _need(
                hop["signature_evidence"]["pointer"] == f"/signatures/hop_{index}",
                f"step_getter hop[{index}] must select /signatures/hop_{index}",
            )
        else:
            _keys(hop, {"kind", "name", "target", "abi"})
        _need(
            type(hop["name"]) is str and 1 <= len(hop["name"]) <= 64 and hop["abi"] == "default",
            "default-ABI named step_getter required",
        )
        _validate_code_entry(hop["target"])
    elif kind == "array_element":
        _keys(hop, {"kind", "name", "base_offset", "index", "stride", "non_null"}, {"element_kind"})
        _need(hop.get("element_kind", "pointer") == "pointer", "array_element hop supports only pointer slots")
        _need(type(hop["name"]) is str and 1 <= len(hop["name"]) <= 64, "bounded hop name required")
        _need(
            type(hop["base_offset"]) is int
            and 0 <= hop["base_offset"] <= MAX_FIELD_OFFSET
            and hop["base_offset"] % 8 == 0,
            "8-byte-aligned base_offset required",
        )
        _need(type(hop["index"]) is int and 0 <= hop["index"] <= 1024, "bounded array index in [0, 1024] required")
        _need(
            type(hop["stride"]) is int and 8 <= hop["stride"] <= 4096 and hop["stride"] % 8 == 0,
            "8-byte-aligned array stride in [8, 4096] required",
        )
        _need(hop["non_null"] is True, "array_element hop must enforce non_null guard")
    else:
        _keys(hop, {"kind", "name", "vector_offset", "index", "non_null"}, {"element_kind"})
        _need(
            hop.get("element_kind", "pointer") == "pointer",
            "vector_element hop supports only contiguous 8-byte pointer elements",
        )
        _need(type(hop["name"]) is str and 1 <= len(hop["name"]) <= 64, "bounded hop name required")
        _need(
            type(hop["vector_offset"]) is int
            and 0 <= hop["vector_offset"] <= MAX_FIELD_OFFSET
            and hop["vector_offset"] % 8 == 0,
            "8-byte-aligned vector_offset required",
        )
        _need(type(hop["index"]) is int and 0 <= hop["index"] <= 1024, "bounded vector index in [0, 1024] required")
        _need(hop["non_null"] is True, "vector_element hop must enforce non_null guard")
    return copy.deepcopy(hop)


def _validate_tail_arg_type(kind: Any, idx: int) -> None:
    if type(kind) is str:
        _need(
            kind in (BORROWED_SCALAR_TYPES | {"utf8"}),
            f"arg[{idx}] must have a scalar, utf8, buffer, or POD struct type",
        )
        return
    _need(type(kind) is dict and kind.get("kind") in {"buffer", "struct"}, f"arg[{idx}] aggregate must be buffer or struct")
    if kind["kind"] == "buffer":
        _keys(kind, {"kind", "bytes", "direction"})
        _need(
            type(kind["bytes"]) is int and 1 <= kind["bytes"] <= 16384 and kind["direction"] in {"in", "out", "inout"},
            f"arg[{idx}] buffer requires 1..16384 bytes and in/out/inout direction",
        )
    else:
        _keys(kind, {"kind", "fields"})
        fields = kind["fields"]
        _need(type(fields) is list and 1 <= len(fields) <= 16, f"arg[{idx}] struct requires 1..16 fields")
        seen = set()
        for f in fields:
            _keys(f, {"name", "type"})
            _need(
                type(f["name"]) is str
                and _bridge().R.NAME.fullmatch(f["name"]) is not None
                and f["name"] not in seen
                and f["type"] in BORROWED_SCALAR_TYPES,
                f"arg[{idx}] struct fields must be unique valid identifiers with scalar types",
            )
            seen.add(f["name"])


def _validate_tail_arg_value(kind: Any, val: Any, idx: int, *, allow_bindings: bool, allowed_inputs: set[str] | None = None) -> None:
    if type(val) is dict and set(val) == {"input"} and not (type(kind) is dict and kind.get("kind") == "struct"):
        _need(allow_bindings, f"concrete value required at arg[{idx}] when bindings disallowed")
        _need(
            type(kind) is str
            or (type(kind) is dict and kind.get("kind") == "buffer" and kind.get("direction") in {"in", "inout"}),
            f"input binding at arg[{idx}] requires a scalar/utf8 or in/inout buffer argument",
        )
        _need(
            type(val["input"]) is str and _bridge().R.NAME.fullmatch(val["input"]) is not None,
            "input binding requires a valid parameter identifier",
        )
        if allowed_inputs is not None:
            _need(val["input"] in allowed_inputs, f"undeclared input parameter: {val['input']}")
        _bridge()._native_argument({"type": kind, "value": val}, allow_bindings=allow_bindings)
        return
    if type(kind) is str:
        if kind == "utf8":
            _need(type(val) is str and "\0" not in val and len(val.encode("utf-8")) <= 4096, f"bounded UTF-8 string required at arg[{idx}]")
        else:
            _bridge()._native_argument({"type": kind, "value": val}, allow_bindings=allow_bindings)
    elif kind["kind"] == "buffer":
        if kind["direction"] == "out":
            _need(val is None, f"out buffer at arg[{idx}] must start as None")
        else:
            _need(
                type(val) is list and len(val) == kind["bytes"] and all(type(x) is int and 0 <= x <= 255 for x in val),
                f"in/inout buffer at arg[{idx}] must be {kind['bytes']} byte integers",
            )
    elif kind["kind"] == "struct":
        _need(type(val) is dict and set(val) == {f["name"] for f in kind["fields"]}, f"struct value at arg[{idx}] must match declared fields")
        for f in kind["fields"]:
            _validate_tail_arg_value(f["type"], val[f["name"]], idx, allow_bindings=allow_bindings, allowed_inputs=allowed_inputs)
    _bridge()._native_argument({"type": kind, "value": val}, allow_bindings=allow_bindings)


def validate_consumer_v2(consumer: Any, params: tuple[dict[str, Any], ...] | list[dict[str, Any]] = ()) -> dict[str, Any]:
    """Validate either a `direct` consumer or a `vtable_slot` virtual consumer."""
    _need(type(consumer) is dict and "dispatch" in consumer, "consumer must declare dispatch mode")
    dispatch = consumer["dispatch"]
    _need(dispatch in {"direct", "vtable_slot"}, "consumer dispatch must be 'direct' or 'vtable_slot'")

    this_adj = consumer.get("this_adjustment", 0)
    _need(
        type(this_adj) is int
        and -MAX_THIS_ADJUSTMENT <= this_adj <= MAX_THIS_ADJUSTMENT
        and this_adj % 8 == 0,
        "this_adjustment must be an 8-byte-aligned integer within [-4096, 4096]",
    )

    if dispatch == "direct":
        _keys(
            consumer,
            {"kind", "dispatch", "target", "abi", "arguments", "return_type"},
            {"this_adjustment", "pac_mode"},
        )
        _validate_code_entry(consumer["target"])
    else:
        _keys(
            consumer,
            {"kind", "dispatch", "vtable_slot_offset", "target_entry", "abi", "arguments", "return_type"},
            {"this_adjustment", "pac_mode"},
        )
        slot_offset = consumer["vtable_slot_offset"]
        _need(
            type(slot_offset) is int
            and 0 <= slot_offset <= MAX_VTABLE_SLOT_OFFSET
            and slot_offset % 8 == 0,
            "vtable_slot_offset must be an 8-byte-aligned offset in [0, 1024]",
        )
        _validate_code_entry(consumer["target_entry"])

    pac_mode = consumer.get("pac_mode", "none")
    _need(pac_mode in ALLOWED_PAC_MODES, "consumer pac_mode must be 'none' or 'arm64e_strip'")

    _need(
        consumer["kind"] == "native_function" and consumer["abi"] == "default",
        "default-ABI native_function consumer required",
    )
    args = consumer["arguments"]
    _need(type(args) is list and 1 <= len(args) <= 8, "consumer requires receiver arg0 and up to 7 tail arguments")
    _need(
        _same(args[0], {"type": "pointer", "value": {"receiver": "fresh"}}),
        "arg0 must be {'type': 'pointer', 'value': {'receiver': 'fresh'}}",
    )
    _need(
        type(consumer["return_type"]) is str and consumer["return_type"] in (BORROWED_SCALAR_TYPES | {"void"}),
        "consumer return_type must be scalar or void",
    )

    allowed_inputs = {p["name"] for p in params} if params is not None and len(params) >= 0 else None
    if params == ():
        allowed_inputs = None
    for idx, arg in enumerate(args[1:], start=1):
        _keys(arg, {"type", "value"})
        _validate_tail_arg_type(arg["type"], idx)
        _validate_tail_arg_value(arg["type"], arg["value"], idx, allow_bindings=True, allowed_inputs=allowed_inputs)

    return copy.deepcopy(consumer)


def validate_request_v2(request: Any, params: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate a V2 native receiver declaration supporting vtable slots, hops, and this_adjustment."""
    _keys(request, {"getter", "consumer", "vptr", "citations"}, {"op", "hops", "queue_policy"})
    _need(
        "op" not in request or request["op"] == "declare_native_receiver_v2",
        "declare_native_receiver_v2 operation required",
    )
    queue_policy = request.get("queue_policy", "main")
    _need(queue_policy in ALLOWED_QUEUE_POLICIES, "queue_policy must be 'main'")

    getter = request["getter"]
    _keys(getter, {"kind", "target", "abi", "arguments", "return_type"})
    _need(
        getter["kind"] == "native_function"
        and getter["abi"] == "default"
        and getter["arguments"] == []
        and getter["return_type"] == "pointer",
        "root getter must be a zero-argument default-ABI pointer function",
    )
    _validate_code_entry(getter["target"])

    hops = request.get("hops", [])
    _need(type(hops) is list and len(hops) <= MAX_HOPS, f"at most {MAX_HOPS} pointer hops permitted")
    validated_hops = [validate_hop(h, i, allow_signature_evidence=False) for i, h in enumerate(hops)]

    consumer = validate_consumer_v2(request["consumer"], params=() if params is None else params)
    consumer_entry = consumer["target"] if consumer["dispatch"] == "direct" else consumer["target_entry"]
    _need(not _same(getter["target"], consumer_entry), "getter and consumer entries must be distinct")
    for idx, h in enumerate(validated_hops):
        if h["kind"] == "step_getter":
            _need(
                not _same(h["target"], consumer_entry) and not _same(h["target"], getter["target"]),
                f"step_getter hop[{idx}] entry must be distinct from root getter and consumer",
            )

    vptr = request["vptr"]
    _keys(vptr, {"image", "data_offset"}, {"subobject_offset", "pac_mode"})
    _need(vptr.get("pac_mode", "none") in ALLOWED_PAC_MODES, "vptr pac_mode must be 'none' or 'arm64e_strip'")
    subobject_offset = vptr.get("subobject_offset", 0)
    _need(
        type(subobject_offset) is int
        and 0 <= subobject_offset <= MAX_FIELD_OFFSET
        and subobject_offset % 8 == 0,
        "subobject_offset must be an 8-byte-aligned integer in [0, 65536]",
    )
    _validate_image(vptr["image"])
    _need(
        type(vptr["data_offset"]) is str
        and HEX_OFFSET.fullmatch(vptr["data_offset"]) is not None
        and int(vptr["data_offset"], 16) <= MAX_DATA_OFFSET - 8
        and int(vptr["data_offset"], 16) % 8 == 0,
        "8-byte-aligned vptr data_offset required",
    )

    expected_roles = ["getter_code", "consumer_code", "vptr_data"]
    if consumer["dispatch"] == "vtable_slot":
        expected_roles.append("vtable_slot_data")

    citations = request["citations"]
    _need(
        type(citations) is list and len(citations) == len(expected_roles),
        f"expected citations for roles: {expected_roles}",
    )
    seen_artifacts: set[str] = set()
    for expected_role, row in zip(expected_roles, citations):
        _keys(row, {"role", "artifact", "pointer"})
        name = row["artifact"]
        _need(
            row["role"] == expected_role
            and type(name) is str
            and 0 < len(name) <= 4096
            and "\0" not in name
            and not name.startswith("/")
            and str(PurePosixPath(name)) == name
            and ".." not in PurePosixPath(name).parts
            and name not in seen_artifacts
            and row["pointer"] == "/range",
            f"invalid or duplicate citation for role {expected_role}",
        )
        seen_artifacts.add(name)

    normalized = copy.deepcopy(request)
    normalized["hops"] = validated_hops
    normalized["queue_policy"] = queue_policy
    if "this_adjustment" not in normalized["consumer"]:
        normalized["consumer"]["this_adjustment"] = 0
    if "pac_mode" not in normalized["consumer"]:
        normalized["consumer"]["pac_mode"] = "none"
    if "subobject_offset" not in normalized["vptr"]:
        normalized["vptr"]["subobject_offset"] = subobject_offset
    if "pac_mode" not in normalized["vptr"]:
        normalized["vptr"]["pac_mode"] = "none"
    _bounded(normalized)
    return normalized


def signature_contract(actor: dict[str, Any]) -> dict[str, Any]:
    return {
        "abi": actor["abi"],
        "argument_types": [copy.deepcopy(item["type"]) for item in actor["arguments"]],
        "return_type": actor["return_type"],
        "context": False,
        "pointer_policy": "borrowed_no_escape",
    }


def signatures_map(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {
        "getter": signature_contract(request["getter"]),
        "consumer": signature_contract(request["consumer"]),
    }
    for idx, hop in enumerate(request.get("hops", [])):
        if hop["kind"] == "step_getter":
            result[f"hop_{idx}"] = {
                "abi": hop["abi"],
                "argument_types": ["pointer"],
                "return_type": "pointer",
                "context": False,
                "pointer_policy": "borrowed_no_escape",
            }
    return result


def _default_originals(validated: dict[str, Any], declaration_file: str) -> list[dict[str, Any]]:
    parent = PurePosixPath(declaration_file).parent.parent
    result = []
    for idx, row in enumerate(validated["citations"]):
        candidate = str(parent / row["artifact"])
        if candidate == declaration_file:
            candidate = str(parent / f"citation_{idx}" / row["artifact"])
        result.append({
            "role": row["role"],
            "file": candidate,
            "sha256": f"{(idx + 1) % 10}" * 64,
        })
    return result


def build_recipe_v2(
    request: dict[str, Any],
    declaration_file: str,
    declaration_sha256: str,
    originals: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct the immutable V2 runtime recipe and bound actor."""
    validated = validate_request_v2(request)
    consumer = validated["consumer"]
    dispatch = consumer["dispatch"]
    consumer_entry = consumer["target"] if dispatch == "direct" else consumer["target_entry"]

    claim_base = {"kind": KIND_V2, "file": str(declaration_file), "sha256": declaration_sha256}
    expected_native_calls = 1 + sum(1 for h in validated["hops"] if h["kind"] == "step_getter") + 1

    decorated_hops = []
    for idx, hop in enumerate(validated["hops"]):
        hop_copy = copy.deepcopy(hop)
        if hop_copy["kind"] == "step_getter":
            hop_copy["signature_evidence"] = {**claim_base, "pointer": f"/signatures/hop_{idx}"}
        decorated_hops.append(hop_copy)

    resolved_originals = copy.deepcopy(originals) if originals is not None else _default_originals(validated, str(declaration_file))

    recipe = {
        "format": RECIPE_FORMAT_V2,
        "root": {
            "getter": {
                **copy.deepcopy(validated["getter"]),
                "signature_evidence": {**claim_base, "pointer": "/signatures/getter"},
            }
        },
        "hops": decorated_hops,
        "vptr": copy.deepcopy(validated["vptr"]),
        "lifetime": {
            "mode": MODE_V2,
            "queue": validated["queue_policy"],
            "scope": "single_immediate_transaction",
            "expected_native_calls": expected_native_calls,
        },
        "consumer": {
            "dispatch": dispatch,
            "vtable_slot_offset": consumer.get("vtable_slot_offset"),
            "this_adjustment": consumer["this_adjustment"],
            "pac_mode": consumer["pac_mode"],
            "entry": copy.deepcopy(consumer_entry),
            "abi": consumer["abi"],
            "argument_types": [copy.deepcopy(a["type"]) for a in consumer["arguments"]],
            "return_type": consumer["return_type"],
            "signature_evidence": {**claim_base, "pointer": "/signatures/consumer"},
        },
        "originals": resolved_originals,
        "evidence_basis": dict(BASIS_V2),
    }

    actor = {
        "kind": "native_function",
        "dispatch": dispatch,
        "target": copy.deepcopy(consumer_entry),
        "abi": consumer["abi"],
        "arguments": copy.deepcopy(consumer["arguments"]),
        "return_type": consumer["return_type"],
        "signature_evidence": {**claim_base, "pointer": "/signatures/consumer"},
    }
    if dispatch == "vtable_slot":
        actor["vtable_slot_offset"] = consumer["vtable_slot_offset"]
    actor["arguments"][0]["value"] = {"native_path": recipe}
    return recipe, actor


def is_recipe_v2(value: Any) -> bool:
    return type(value) is dict and value.get("format") == RECIPE_FORMAT_V2


def _iter_v2_argument(arg: Any):
    if type(arg) is not dict:
        return
    arg_type = arg.get("type")
    arg_val = arg.get("value")
    if arg_type == "pointer" or (type(arg_type) is dict and arg_type.get("kind") == "swift_type"):
        yield from _iter_v2_subjects(arg_val)
    elif (
        type(arg_type) is dict
        and arg_type.get("kind") == "struct"
        and type(arg_type.get("fields")) is list
        and type(arg_val) is dict
    ):
        for field in arg_type["fields"]:
            if type(field) is dict and type(field.get("name")) is str and field["name"] in arg_val:
                yield from _iter_v2_argument({"type": field.get("type"), "value": arg_val[field["name"]]})


def _iter_v2_subjects(value: Any):
    if type(value) is list:
        for item in value:
            yield from _iter_v2_subjects(item)
        return
    if type(value) is not dict:
        return
    if is_recipe_v2(value):
        yield value
        root = value.get("root")
        if type(root) is dict and type(root.get("getter")) is dict:
            yield from _iter_v2_subjects(root["getter"])
        hops = value.get("hops")
        if type(hops) is list:
            for hop in hops:
                if type(hop) is dict and hop.get("kind") == "step_getter":
                    yield from _iter_v2_subjects(hop)
        return
    if set(value) == {"native_path"}:
        yield from _iter_v2_subjects(value["native_path"])
        return
    if set(value) in ({"getter"}, {"native"}):
        yield from _iter_v2_subjects(next(iter(value.values())))
        return
    if "operations" in value and type(value.get("operations")) is list:
        for op in value["operations"]:
            yield from _iter_v2_subjects(op)
        return
    if value.get("kind") in {"objc_sequence", "runtime_sequence"} and type(value.get("steps")) is list:
        for step in value["steps"]:
            yield from _iter_v2_subjects(step)
        return
    if value.get("op") in {"graph", "native_read"}:
        yield from _iter_v2_subjects(value.get("root") if value.get("op") == "graph" else value.get("receiver"))
        return
    if value.get("kind") == "qt_moc_method" or value.get("op") == "qt_moc_invoke":
        yield from _iter_v2_subjects(value.get("receiver"))
        return
    sig = value.get("signature_evidence")
    if (
        value.get("kind") == "native_function"
        or value.get("op") == "native_invoke"
        or ("target" in value and "abi" in value and "signature_evidence" in value)
    ):
        if type(sig) is dict and sig.get("kind") == KIND_V2:
            yield value
        if type(value.get("context")) is dict:
            yield from _iter_v2_argument(value["context"])
        if type(value.get("arguments")) is list:
            for arg in value["arguments"]:
                yield from _iter_v2_argument(arg)
        return
    if type(sig) is dict and sig.get("kind") == KIND_V2:
        yield value


def has_hypothesis_v2(value: Any) -> bool:
    return any(True for _ in _iter_v2_subjects(value))


def evidence_summary_v2(value: Any) -> dict[str, Any] | None:
    return dict(BASIS_V2) if has_hypothesis_v2(value) else None


def validate_recipe(value: Any, **_unused: Any) -> dict[str, Any]:
    _keys(value, {"format", "root", "hops", "vptr", "lifetime", "consumer", "originals", "evidence_basis"})
    _need(is_recipe_v2(value) and _same(value["evidence_basis"], BASIS_V2), "valid V2 receiver recipe required")
    _keys(value["root"], {"getter"})
    getter = value["root"]["getter"]
    _keys(getter, {"kind", "target", "abi", "arguments", "return_type", "signature_evidence"})
    _need(
        getter["kind"] == "native_function"
        and getter["abi"] == "default"
        and getter["arguments"] == []
        and getter["return_type"] == "pointer",
        "V2 recipe root getter must be a zero-argument default-ABI pointer function",
    )
    _validate_code_entry(getter["target"])
    evidence_claim(getter["signature_evidence"])
    _need(getter["signature_evidence"]["pointer"] == "/signatures/getter", "root getter must select /signatures/getter")

    claim_base = {
        "kind": KIND_V2,
        "file": getter["signature_evidence"]["file"],
        "sha256": getter["signature_evidence"]["sha256"],
    }

    hops = value["hops"]
    _need(type(hops) is list and len(hops) <= MAX_HOPS, "bounded V2 hops required")
    for idx, hop in enumerate(hops):
        validate_hop(hop, idx, allow_signature_evidence=True)
        if hop["kind"] == "step_getter":
            _need(
                _same(hop["signature_evidence"], {**claim_base, "pointer": f"/signatures/hop_{idx}"}),
                "step_getter hop signature_evidence must share exact declaration producer",
            )

    vptr = value["vptr"]
    _keys(vptr, {"image", "data_offset"}, {"subobject_offset", "pac_mode"})
    _validate_image(vptr["image"])
    _need(vptr.get("pac_mode", "none") in ALLOWED_PAC_MODES, "invalid vptr pac_mode")
    subobject_offset = vptr.get("subobject_offset", 0)
    _need(
        type(subobject_offset) is int
        and 0 <= subobject_offset <= MAX_FIELD_OFFSET
        and subobject_offset % 8 == 0,
        "subobject_offset must be an 8-byte-aligned integer in [0, 65536]",
    )
    _need(
        type(vptr["data_offset"]) is str
        and HEX_OFFSET.fullmatch(vptr["data_offset"]) is not None
        and int(vptr["data_offset"], 16) <= MAX_DATA_OFFSET - 8
        and int(vptr["data_offset"], 16) % 8 == 0,
        "8-byte-aligned vptr data_offset required",
    )

    consumer = value["consumer"]
    _keys(
        consumer,
        {"dispatch", "vtable_slot_offset", "this_adjustment", "entry", "abi", "argument_types", "return_type", "signature_evidence"},
        {"pac_mode"},
    )
    dispatch = consumer["dispatch"]
    _need(dispatch in {"direct", "vtable_slot"}, "consumer dispatch must be direct or vtable_slot")
    if dispatch == "direct":
        _need(consumer["vtable_slot_offset"] is None, "direct consumer must have vtable_slot_offset=None")
    else:
        slot_offset = consumer["vtable_slot_offset"]
        _need(
            type(slot_offset) is int
            and 0 <= slot_offset <= MAX_VTABLE_SLOT_OFFSET
            and slot_offset % 8 == 0,
            "vtable_slot_offset must be an 8-byte-aligned offset in [0, 1024]",
        )
    this_adj = consumer["this_adjustment"]
    _need(
        type(this_adj) is int
        and -MAX_THIS_ADJUSTMENT <= this_adj <= MAX_THIS_ADJUSTMENT
        and this_adj % 8 == 0,
        "this_adjustment must be an 8-byte-aligned integer within [-4096, 4096]",
    )
    _need(consumer.get("pac_mode", "none") in ALLOWED_PAC_MODES, "invalid consumer pac_mode")
    _validate_code_entry(consumer["entry"])
    _need(not _same(getter["target"], consumer["entry"]), "getter and consumer entries must be distinct")
    for idx, hop in enumerate(hops):
        if hop["kind"] == "step_getter":
            _need(
                not _same(hop["target"], consumer["entry"]) and not _same(hop["target"], getter["target"]),
                f"step_getter hop[{idx}] entry must be distinct from root getter and consumer",
            )
    _need(consumer["abi"] == "default", "consumer abi must be 'default'")
    arg_types = consumer["argument_types"]
    _need(type(arg_types) is list and 1 <= len(arg_types) <= 8 and arg_types[0] == "pointer", "consumer requires pointer arg0 and up to 7 tail argument types")
    for idx, kind in enumerate(arg_types[1:], start=1):
        _validate_tail_arg_type(kind, idx)
    _need(
        type(consumer["return_type"]) is str and consumer["return_type"] in (BORROWED_SCALAR_TYPES | {"void"}),
        "consumer return_type must be scalar or void",
    )
    evidence_claim(consumer["signature_evidence"])
    _need(
        _same(consumer["signature_evidence"], {**claim_base, "pointer": "/signatures/consumer"}),
        "consumer signature_evidence must share exact declaration producer",
    )

    lifetime = value["lifetime"]
    _keys(lifetime, {"mode", "queue", "scope", "expected_native_calls"})
    expected_calls = 1 + sum(1 for h in hops if h["kind"] == "step_getter") + 1
    _need(
        lifetime["mode"] == MODE_V2
        and lifetime["queue"] in ALLOWED_QUEUE_POLICIES
        and lifetime["scope"] == "single_immediate_transaction"
        and lifetime["expected_native_calls"] == expected_calls,
        "V2 lifetime expected_native_calls, scope, or queue mismatch",
    )

    expected_roles = ["getter_code", "consumer_code", "vptr_data"]
    if dispatch == "vtable_slot":
        expected_roles.append("vtable_slot_data")
    originals = value["originals"]
    _need(
        type(originals) is list and len(originals) == len(expected_roles),
        f"V2 recipe requires original observations for roles {expected_roles}",
    )
    seen_files = {claim_base["file"]}
    for expected_role, row in zip(expected_roles, originals):
        _keys(row, {"role", "file", "sha256"})
        _need(row["role"] == expected_role and row["file"] not in seen_files, "ordered distinct original observations required")
        _validate_image({"path": row["file"], "sha256": row["sha256"]})
        seen_files.add(row["file"])

    file_claims(value)
    _bounded(value)
    return copy.deepcopy(value)


def require_consumer(recipe: dict[str, Any], actor: dict[str, Any], argument: int) -> None:
    _need(
        type(argument) is int
        and argument == 0
        and actor.get("kind") == "native_function"
        and "context" not in actor
        and "compiler_adapter" not in actor,
        "V2 receiver must be arg0 of an ordinary native_function call",
    )
    consumer = recipe["consumer"]
    dispatch = actor.get("dispatch", "direct")
    _need(dispatch == consumer["dispatch"], "V2 consumer dispatch mismatch")
    if dispatch == "vtable_slot":
        _need(actor.get("vtable_slot_offset") == consumer["vtable_slot_offset"], "V2 consumer vtable_slot_offset mismatch")
    else:
        _need("vtable_slot_offset" not in actor or actor["vtable_slot_offset"] is None, "direct V2 consumer cannot specify vtable_slot_offset")
    _need(_same(actor["target"], consumer["entry"]), "V2 consumer entry mismatch")
    _need(actor["abi"] == consumer["abi"], "V2 consumer ABI mismatch")
    _need(
        type(actor.get("arguments")) is list
        and _same([a["type"] for a in actor["arguments"]], consumer["argument_types"]),
        "V2 consumer argument_types mismatch",
    )
    _need(actor.get("return_type") == consumer["return_type"], "V2 consumer return_type mismatch")
    _need(_same(actor.get("signature_evidence"), consumer["signature_evidence"]), "V2 consumer signature_evidence mismatch")


def validate_actor(actor: Any, *, allow_bindings: bool = False) -> dict[str, Any]:
    _keys(
        actor,
        {"kind", "target", "abi", "arguments", "return_type", "signature_evidence"},
        {"dispatch", "vtable_slot_offset"},
    )
    evidence_claim(actor["signature_evidence"])
    _need(actor["signature_evidence"]["pointer"] == "/signatures/consumer", "V2 actor signature must select /signatures/consumer")
    args = actor["arguments"]
    _need(type(args) is list and 1 <= len(args) <= 8, "V2 actor requires receiver arg0 and up to 7 tail arguments")
    _keys(args[0], {"type", "value"})
    _keys(args[0]["value"], {"native_path"})
    _need(args[0]["type"] == "pointer", "V2 actor requires pointer arg0")
    recipe = validate_recipe(args[0]["value"]["native_path"])
    require_consumer(recipe, actor, 0)
    for idx, arg in enumerate(args[1:], start=1):
        _keys(arg, {"type", "value"})
        _validate_tail_arg_type(arg["type"], idx)
        _validate_tail_arg_value(arg["type"], arg["value"], idx, allow_bindings=allow_bindings)
    _bounded(actor)
    return copy.deepcopy(actor)


def root_contract(recipe: dict[str, Any]) -> dict[str, Any]:
    getter = {k: copy.deepcopy(v) for k, v in recipe["root"]["getter"].items() if k != "signature_evidence"}
    hops = [
        {k: copy.deepcopy(v) for k, v in h.items() if k != "signature_evidence"}
        for h in recipe["hops"]
    ]
    return {
        "format": ROOT_CONTRACT_V2,
        "root": {"getter": getter},
        "hops": hops,
        "pointer_policy": "borrowed_no_escape",
        "evidence_basis": dict(BASIS_V2),
    }


def vptr_contract(vptr: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": VPTR_CONTRACT_V2,
        "image": copy.deepcopy(vptr["image"]),
        "data_offset": vptr["data_offset"],
        "subobject_offset": vptr.get("subobject_offset", 0),
        "pac_mode": vptr.get("pac_mode", "none"),
        "evidence_basis": dict(BASIS_V2),
    }


def consumer_contract(consumer: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": CONSUMER_CONTRACT_V2,
        "dispatch": consumer["dispatch"],
        "vtable_slot_offset": consumer.get("vtable_slot_offset"),
        "this_adjustment": consumer["this_adjustment"],
        "pac_mode": consumer.get("pac_mode", "none"),
        "entry": copy.deepcopy(consumer["entry"]),
        "abi": consumer["abi"],
        "argument_types": copy.deepcopy(consumer["argument_types"]),
        "return_type": consumer["return_type"],
        "evidence_basis": dict(BASIS_V2),
    }


def lifetime_contract(recipe: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": LIFETIME_CONTRACT_V2,
        "root": root_contract(recipe),
        "mode": MODE_V2,
        "queue": recipe["lifetime"].get("queue", "main"),
        "scope": "single_immediate_transaction",
        "expected_native_calls": recipe["lifetime"]["expected_native_calls"],
        "terminal": {"op": "native_invoke", "consumer": consumer_contract(recipe["consumer"])},
        "evidence_basis": dict(BASIS_V2),
    }


def contracts(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        root_contract(recipe),
        lifetime_contract(recipe),
        vptr_contract(recipe["vptr"]),
        consumer_contract(recipe["consumer"]),
    ]


def evidence_plan(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    sig = recipe["root"]["getter"]["signature_evidence"]
    claim_base = {"kind": KIND_V2, "file": sig["file"], "sha256": sig["sha256"]}
    originals = copy.deepcopy(recipe.get("originals", []))
    return [
        {"claim": {**claim_base, "pointer": f"/contracts/{i}"}, "originals": copy.deepcopy(originals), "contract": contract}
        for i, contract in enumerate(contracts(recipe))
    ]


def validate_observation(value: Any, recipe: dict[str, Any], *, for_call: bool = True) -> dict[str, Any]:
    validate_recipe(recipe)
    _need(for_call is True, "V2 receiver hypotheses admit only an actual consumer call")
    _keys(
        value,
        {
            "recipe", "argument", "consumer", "root_kind", "owner_retained", "queue",
            "lifetime_mode", "resolved", "sealed", "dispatch_permit_consumed",
            "hops", "expires", "borrowed_scope", "evidence_basis",
        },
    )
    _need(
        _same(value["recipe"], recipe)
        and type(value["argument"]) is int
        and value["argument"] == 0
        and _same(value["consumer"], consumer_contract(recipe["consumer"]))
        and value["root_kind"] == "getter"
        and value["owner_retained"] is False
        and value["queue"] == recipe["lifetime"].get("queue", "main")
        and value["lifetime_mode"] == MODE_V2
        and value["resolved"] is True
        and value["sealed"] is True
        and value["dispatch_permit_consumed"] is True
        and value["expires"] == "transaction"
        and _same(value["evidence_basis"], BASIS_V2),
        "V2 receiver observation lost its declared basis or one-use binding",
    )
    scope = value["borrowed_scope"]
    _keys(scope, {"getter_epoch", "checked_epoch", "use", "consumed", "expires"})
    expected_getter_epoch = 1 + sum(1 for h in recipe["hops"] if h["kind"] == "step_getter")
    _need(
        type(scope["getter_epoch"]) is int
        and type(scope["checked_epoch"]) is int
        and scope["getter_epoch"] == expected_getter_epoch
        and scope["checked_epoch"] == expected_getter_epoch
        and scope["use"] == "dispatch"
        and scope["consumed"] is True
        and scope["expires"] == "use",
        "V2 receiver observation lost its immediate acquisition/use epoch",
    )
    obs_hops = value["hops"]
    _need(
        type(obs_hops) is list and len(obs_hops) == 1 + len(recipe["hops"]),
        "V2 receiver observation hop count must match root + declared hops",
    )
    is_vtable_slot = recipe["consumer"]["dispatch"] == "vtable_slot"
    expected_hops = [
        {
            "index": 0,
            "kind": "root_getter",
            "name": "root",
            "non_null_verified": True,
            "vptr_checked": len(recipe["hops"]) == 0,
            "vtable_slot_verified": len(recipe["hops"]) == 0 and is_vtable_slot,
            "this_adjusted_verified": len(recipe["hops"]) == 0,
        },
        *[
            {
                "index": idx + 1,
                "kind": h["kind"],
                "name": h["name"],
                "non_null_verified": True,
                "vptr_checked": (idx + 1) == len(recipe["hops"]),
                "vtable_slot_verified": (idx + 1) == len(recipe["hops"]) and is_vtable_slot,
                "this_adjusted_verified": (idx + 1) == len(recipe["hops"]),
            }
            for idx, h in enumerate(recipe["hops"])
        ],
    ]
    _need(
        _same(obs_hops, expected_hops),
        "V2 receiver observation hops must match exact typed root and hop verification records",
    )
    return copy.deepcopy(value)


def image_claims(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    claims = [
        recipe["root"]["getter"]["target"]["module"],
        recipe["consumer"]["entry"]["module"],
        recipe["vptr"]["image"],
        *(h["target"]["module"] for h in recipe["hops"] if h["kind"] == "step_getter"),
    ]
    by_path: dict[str, dict[str, Any]] = {}
    for c in claims:
        _validate_image(c)
        _need(c["path"] not in by_path or _same(by_path[c["path"]], c), "conflicting V2 image claims")
        by_path[c["path"]] = copy.deepcopy(c)
    return [by_path[k] for k in sorted(by_path)]


def file_claims(recipe: dict[str, Any]) -> dict[str, str]:
    result = {c["path"]: c["sha256"] for c in image_claims(recipe)}

    def add(file: str, sha: str) -> None:
        _need(file not in result or result[file] == sha, "conflicting V2 receiver evidence identities")
        result[file] = sha

    for row in evidence_plan(recipe):
        add(row["claim"]["file"], row["claim"]["sha256"])
        for original in row["originals"]:
            add(original["file"], original["sha256"])
    return result


def _policy(state: dict[str, Any]) -> None:
    _need(
        type(state) is dict
        and state.get("native_receiver_mode") == POLICY_V2
        and state.get("native_contract_mode") == "declared"
        and state.get("workflow") == "guest_model_judged_state_v1",
        "caller must freeze the explicit experimental receiver and native ABI policies",
    )


def _binding(state: dict[str, Any]) -> str:
    R = _resource_module("rea_mapping")
    return R.digest({key: state[key] for key in BINDING_KEYS if key in state})


def _registered(reader: Any, root: Path, state: dict[str, Any], name: str) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    _need(type(name) is str and name in state.get("artifacts", {}), "cite registered current-session receiver evidence")
    file = root / name
    _need(
        not Path(name).is_absolute()
        and ".." not in Path(name).parts
        and file.is_relative_to(root)
        and state["artifacts"][name].get("file") == str(file),
        "receiver v2 artifact escaped its producing session",
    )
    match = re.fullmatch(r"s([0-9]{4})", Path(name).parts[0])
    _need(match is not None, "receiver v2 evidence requires an actual producing session step")
    index = int(match.group(1))
    _need(
        index < len(state["steps"]) and state["steps"][index]["status"] == "completed",
        "receiver v2 evidence producer is not completed",
    )
    value, pin = reader.data(file, state["artifacts"][name])
    return value, pin, state["steps"][index]["request"]


def _observations(reader: Any, root: Path, state: dict[str, Any], request: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    A = _resource_module("rea_static_artifacts")
    _need(type(state.get("bundle")) is str and len(state["bundle"]) > 0, "canonical owned target bundle required")
    bundle = Path(state["bundle"])
    _need(bundle.exists() and bundle.resolve(strict=True) == bundle, "canonical owned target bundle required")
    allow_arm64e = request["vptr"].get("pac_mode") == "arm64e_strip" and (
        request["consumer"]["dispatch"] != "vtable_slot"
        or request["consumer"].get("pac_mode") == "arm64e_strip"
    )
    observations, images, opened = [], {}, {}
    try:
        for citation in request["citations"]:
            role = citation["role"]
            value, pin, producing = _registered(reader, root, state, citation["artifact"])
            prefix = Path(citation["artifact"]).parts[0]
            request_artifact = _registered(reader, root, state, prefix + "/request.json")[0]
            feedback_artifact = _registered(reader, root, state, prefix + "/feedback.json")[0]
            producer_index = int(prefix[1:])
            _need(
                _same(request_artifact, producing) and _same(feedback_artifact, state["steps"][producer_index]),
                "receiver v2 original observation dispatch provenance changed",
            )
            is_data = role in {"vptr_data", "vtable_slot_data"}
            kind = "original_range" if is_data else "original_code"
            expected_format = "anyuse.rea-original-file-range/1" if is_data else "anyuse.rea-original-instruction-range/1"
            source = producing.get("request", {})
            _need(
                producing.get("op") == "static_evidence"
                and source.get("kind") == kind
                and value.get("format") == expected_format
                and value.get("original_unchanged") is True
                and value.get("task_proof") is False
                and value.get("automatic_abi_or_lifetime_admission") is False
                and value.get("byte_order") == "little",
                "receiver v2 evidence lacks its actual fixed original-code/data producer",
            )
            if role == "vptr_data":
                module, offset = request["vptr"]["image"], request["vptr"]["data_offset"]
            elif role == "vtable_slot_data":
                slot_offset_hex = hex(int(request["vptr"]["data_offset"], 16) + request["consumer"]["vtable_slot_offset"])
                module, offset = request["vptr"]["image"], slot_offset_hex
            elif role == "getter_code":
                module, offset = request["getter"]["target"]["module"], request["getter"]["target"]["offset"]
            else:
                consumer = request["consumer"]
                target = consumer["target"] if consumer["dispatch"] == "direct" else consumer["target_entry"]
                module, offset = target["module"], target["offset"]
            path = Path(module["path"])
            _need(path.is_relative_to(bundle), "receiver v2 image is outside the owned target bundle")
            _need(
                source.get("relative") == path.relative_to(bundle).as_posix() and source.get("offset") == offset,
                "receiver v2 evidence names a different producing entry/data range",
            )
            A.validate_request(source)
            original = opened.get(str(path))
            if original is None:
                original = opened[str(path)] = A.OriginalImage(
                    bundle,
                    path.relative_to(bundle).as_posix(),
                    allow_arm64e=allow_arm64e,
                )
            _need(
                _same(value.get("image"), original.pin) and original.pin["sha256"] == module["sha256"],
                "receiver v2 original image changed after its fixed observation",
            )
            row = value.get("range")
            expected = A.original_range(original, source)
            _need(
                _same(value, expected)
                and row["image_offset"] == offset
                and row["bytes"] >= (8 if is_data else 4)
                and (source["unit_bytes"] == 8 if is_data else source["unit_bytes"] == 4),
                "receiver v2 evidence differs from the exact current original bytes or supported unit width",
            )
            observations.append({
                "role": role,
                "citation": copy.deepcopy(citation),
                "artifact_pin": pin,
                "image": copy.deepcopy(original.pin),
                "range": copy.deepcopy(row),
            })
            images[str(path)] = copy.deepcopy(original.pin)

        for hop in request.get("hops", []):
            if hop["kind"] == "step_getter":
                hop_mod = hop["target"]["module"]
                hop_path = Path(hop_mod["path"])
                _need(hop_path.is_relative_to(bundle), "step_getter image is outside the owned target bundle")
                original = opened.get(str(hop_path))
                if original is None:
                    original = opened[str(hop_path)] = A.OriginalImage(
                        bundle,
                        hop_path.relative_to(bundle).as_posix(),
                        allow_arm64e=allow_arm64e,
                    )
                _need(original.pin["sha256"] == hop_mod["sha256"], "step_getter image sha256 differs from original")
                original.read(int(hop["target"]["offset"], 16), 4, kind="code")
                images[str(hop_path)] = copy.deepcopy(original.pin)

        _need(
            sum(pin["bytes"] for pin in images.values()) <= _path().MAX_ORIGINAL_BYTES,
            "receiver v2 original image closure exceeds its fixed byte budget",
        )
        for original in opened.values():
            original.unchanged()
    finally:
        for original in opened.values():
            original.close()
    return observations, images


def _sources() -> dict[str, dict[str, Any]]:
    R, E, A, N = (
        _resource_module(name)
        for name in ("rea_mapping", "rea_actor_evidence", "rea_static_artifacts", "rea_native_contract")
    )
    modules = {
        "receiver_v2": __import__(__name__, fromlist=["_"]),
        "path": _path(),
        "bridge": _bridge(),
        "mapping": R,
        "evidence": E,
        "static_artifacts": A,
        "native_declaration": N,
    }
    return {
        name: {
            "file": str(Path(module.__file__).resolve()),
            "sha256": R._file_hash(Path(module.__file__).resolve()),
            "bytes": Path(module.__file__).stat().st_size,
        }
        for name, module in modules.items()
    }


def _runtime_modules():
    return _resource_module("rea_mapping"), _resource_module("rea_native_contract")


def _originals(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": row["role"], "file": row["artifact_pin"]["file"], "sha256": row["artifact_pin"]["sha256"]}
        for row in observations
    ]


def _restore_declared_bindings(arg_type: Any, current_val: Any, declared_val: Any) -> Any:
    if type(arg_type) is dict and arg_type.get("kind") == "struct":
        if type(declared_val) is dict and type(current_val) is dict and set(declared_val) == set(current_val):
            field_types = {f["name"]: f["type"] for f in arg_type["fields"]}
            return {
                k: _restore_declared_bindings(field_types[k], current_val[k], declared_val[k])
                for k in declared_val
            }
        return current_val
    if type(declared_val) is dict and set(declared_val) == {"input"}:
        _need(
            type(current_val) is not dict or _same(current_val, declared_val),
            "receiver v2 declaration input binding changed",
        )
        return copy.deepcopy(declared_val)
    return current_val


def declare(session: Any, state: dict[str, Any], request: dict[str, Any], folder: Any) -> dict[str, Any]:
    """Persist a V2 receiver hypothesis declaration in the current session step folder."""
    R, N = _runtime_modules()
    R.assert_guest()
    _policy(state)
    _need(state.get("status") == "investigating", "receiver v2 declaration closes before evaluation or proof")
    validated = validate_request_v2(request, state.get("params"))
    if state.get("crash_blocklist") is not None:
        blocklist = CrashBlocklist.from_ledger(state["crash_blocklist"])
        consumer = validated["consumer"]
        consumer_entry = consumer["target"] if consumer["dispatch"] == "direct" else consumer["target_entry"]
        _need(
            not blocklist.is_blocked(
                module_sha256=consumer_entry["module"]["sha256"],
                offset=consumer_entry["offset"],
                argument_types=[a["type"] for a in consumer["arguments"]],
                dispatch=consumer["dispatch"],
                slot_offset=consumer.get("vtable_slot_offset"),
            ),
            "receiver v2 declaration matches a crash-blocklisted ABI hypothesis",
        )
    root, folder_path = Path(session), Path(folder)
    index = len(state.get("steps", [])) - 1
    _need(
        root.exists()
        and folder_path.exists()
        and root.resolve(strict=True) == root
        and folder_path.resolve(strict=True) == folder_path
        and folder_path == root / f"s{index:04d}"
        and 0 <= index < 64,
        "receiver v2 declaration output must be its new producing step",
    )
    _need(
        _same(validate_request_v2(state["steps"][index]["request"], state.get("params")), validated),
        "receiver v2 declaration differs from its current producing request",
    )
    reader = N.Reader()
    observations, images = _observations(reader, root, state, validated)
    receipt_file = folder_path / RECEIPT_V2
    temp_recipe, _ = build_recipe_v2(validated, str(receipt_file), "0" * 64, _originals(observations))
    validate_recipe(temp_recipe)
    document = {
        "format": FORMAT_V2,
        "type_basis": TYPE_BASIS_V2,
        "evidence_basis": dict(BASIS_V2),
        "independently_reviewed": False,
        "actuated": False,
        "proof_created": False,
        "model_calls": 0,
        "session": str(root),
        "session_binding": _binding(state),
        "step": index,
        "request_sha256": R.digest(validated),
        "request": validated,
        "observations": observations,
        "images": images,
        "signatures": signatures_map(validated),
        "contracts": contracts(temp_recipe),
        "sources": _sources(),
    }
    _bounded(document)
    reader.recheck()
    _need(not receipt_file.exists(), "receiver v2 declaration never overwrites an artifact")
    with receipt_file.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    file_sha = R._file_hash(receipt_file)
    recipe, actor = build_recipe_v2(validated, str(receipt_file), file_sha, _originals(observations))
    return {
        "status": "native_receiver_v2_hypothesis_declared",
        "actor": actor,
        "recipe": recipe,
        "receipt": str(receipt_file),
        "type_basis": TYPE_BASIS_V2,
        "evidence_basis": dict(BASIS_V2),
        "abi_recovered": False,
        "type_proven": False,
        "lifetime_proven": False,
        "independently_reviewed": False,
        "proof_created": False,
        "actuated": False,
        "model_calls": 0,
    }


def validate_resource(subject: Any) -> dict[str, dict[str, Any]]:
    """Reconstruct the exact producer and V2 recipe before every permitted use."""
    R, N = _runtime_modules()
    R.assert_guest()
    if is_recipe_v2(subject):
        validate_recipe(subject)
        claim = subject["root"]["getter"]["signature_evidence"]
    else:
        _need(type(subject) is dict and "signature_evidence" in subject, "signed V2 receiver subject required")
        claim = subject["signature_evidence"]
        evidence_claim(claim)
        _need(
            claim["pointer"] in {"/signatures/getter", "/signatures/consumer", *(f"/signatures/hop_{i}" for i in range(MAX_HOPS))},
            "V2 receiver subject has no signature role",
        )
    reader = N.Reader()
    document, pin = reader.data(claim["file"])
    _need(
        pin["sha256"] == claim["sha256"]
        and pin["bytes"] <= MAX_BYTES
        and document.get("format") == FORMAT_V2
        and document.get("type_basis") == TYPE_BASIS_V2
        and _same(document.get("evidence_basis"), BASIS_V2)
        and document.get("independently_reviewed") is False
        and document.get("actuated") is False
        and document.get("proof_created") is False
        and type(document.get("model_calls")) is int
        and document["model_calls"] == 0,
        "receiver v2 declaration or its explicitly unproven evidence tier changed",
    )
    root, index = Path(document["session"]), document["step"]
    _need(
        root.exists()
        and root.resolve(strict=True) == root
        and type(index) is int
        and 0 <= index < 64
        and Path(pin["file"]) == root / f"s{index:04d}" / RECEIPT_V2,
        "receiver v2 declaration escaped its producing session step",
    )
    state = reader.data(root / "session.json")[0]
    _policy(state)
    _need(_binding(state) == document["session_binding"], "receiver v2 declaration frozen task or experimental policy changed")
    registered, registered_pin, producing = _registered(reader, root, state, f"s{index:04d}/{RECEIPT_V2}")
    _need(
        _same(registered_pin, pin) and _same(registered, document) and producing.get("op") == "declare_native_receiver_v2",
        "receiver v2 declaration lacks its registered completed producing step",
    )
    _need(
        _same(_registered(reader, root, state, f"s{index:04d}/request.json")[0], producing)
        and _same(_registered(reader, root, state, f"s{index:04d}/feedback.json")[0], state["steps"][index]),
        "receiver v2 declaration dispatch provenance changed",
    )
    request = validate_request_v2(producing, state.get("params"))
    _need(
        _same(request, document["request"]) and R.digest(request) == document["request_sha256"],
        "receiver v2 declaration differs from its actual producing request",
    )
    if state.get("crash_blocklist") is not None:
        blocklist = CrashBlocklist.from_ledger(state["crash_blocklist"])
        consumer = request["consumer"]
        consumer_entry = consumer["target"] if consumer["dispatch"] == "direct" else consumer["target_entry"]
        _need(
            not blocklist.is_blocked(
                module_sha256=consumer_entry["module"]["sha256"],
                offset=consumer_entry["offset"],
                argument_types=[a["type"] for a in consumer["arguments"]],
                dispatch=consumer["dispatch"],
                slot_offset=consumer.get("vtable_slot_offset"),
            ),
            "receiver v2 resource matches a crash-blocklisted ABI hypothesis",
        )
    observations, images = _observations(reader, root, state, request)
    recipe, actor = build_recipe_v2(request, pin["file"], pin["sha256"], _originals(observations))
    _need(
        _same(observations, document["observations"])
        and _same(images, document["images"])
        and _same(contracts(recipe), document["contracts"])
        and _same(document["signatures"], signatures_map(request)),
        "receiver v2 declaration contract or original observation closure changed",
    )
    if is_recipe_v2(subject):
        _need(_same(subject, recipe), "receiver v2 recipe differs from its exact declaration")
    elif claim["pointer"] == "/signatures/getter":
        _need(_same(subject, recipe["root"]["getter"]), "receiver v2 getter differs from its exact declaration")
    elif claim["pointer"].startswith("/signatures/hop_"):
        hop_idx = int(claim["pointer"].rsplit("_", 1)[1])
        _need(
            0 <= hop_idx < len(recipe["hops"]) and recipe["hops"][hop_idx]["kind"] == "step_getter",
            "receiver v2 step_getter index invalid",
        )
        hop = recipe["hops"][hop_idx]
        expected_hop_call = {
            "kind": "native_function",
            "target": hop["target"],
            "abi": hop["abi"],
            "arguments": [{"type": "pointer", "value": {"null": True}}],
            "return_type": "pointer",
            "signature_evidence": hop["signature_evidence"],
        }
        _need(_same(subject, expected_hop_call), "receiver v2 step_getter differs from its exact declaration")
    else:
        validate_actor(subject, allow_bindings=True)
        normalized = copy.deepcopy(subject)
        _need(len(normalized["arguments"]) == len(actor["arguments"]), "receiver v2 argument count changed")
        for index0, declared in enumerate(actor["arguments"][1:], 1):
            if _same(normalized["arguments"][index0]["type"], declared["type"]):
                normalized["arguments"][index0]["value"] = _restore_declared_bindings(
                    declared["type"],
                    normalized["arguments"][index0]["value"],
                    declared["value"],
                )
        _need(_same(normalized, actor), "receiver v2 actor, ABI, constants or root recipe changed")

    sources = _sources()
    _need(_same(document.get("sources"), sources), "receiver v2 declaration producer implementation changed")
    for source in sources.values():
        reader.read(source["file"], source)
    reader.recheck()
    result = {name: copy.deepcopy(value) for name, value in reader.files.items() if name != str(root / "session.json")}
    for name, image in images.items():
        _need(name not in result or _same(result[name], image), "receiver v2 evidence/image pin collision")
        result[name] = image
    sig_keys = [
        "getter",
        *(f"hop_{idx}" for idx, h in enumerate(request["hops"]) if h["kind"] == "step_getter"),
        "consumer",
    ]
    result[pin["file"]]["native_receiver_signatures"] = [
        {"pointer": f"/signatures/{key}", "signature": copy.deepcopy(document["signatures"][key])}
        for key in sig_keys
    ]
    return result


def resources(subject: Any) -> dict[str, dict[str, Any]]:
    return validate_resource(subject)


def validate_declaration(actor: dict[str, Any], *, state: dict[str, Any], current: bool = True) -> dict[str, Any]:
    """Bind a V2 proposal to its own current-session registered declaration."""
    _need(type(current) is bool, "receiver v2 declaration current flag must be Boolean")
    _policy(state)
    validate_actor(actor, allow_bindings=True)
    if state.get("crash_blocklist") is not None:
        blocklist = CrashBlocklist.from_ledger(state["crash_blocklist"])
        dispatch = actor.get("dispatch", "direct")
        _need(
            not blocklist.is_blocked(
                module_sha256=actor["target"]["module"]["sha256"],
                offset=actor["target"]["offset"],
                argument_types=[a["type"] for a in actor["arguments"]],
                dispatch=dispatch,
                slot_offset=actor.get("vtable_slot_offset") if dispatch == "vtable_slot" else None,
            ),
            "receiver v2 proposal matches a crash-blocklisted ABI hypothesis",
        )
    resources0 = validate_resource(actor)
    R, N = _runtime_modules()
    reader = N.Reader()
    claim = actor["signature_evidence"]
    document, pin = reader.data(claim["file"])
    saved = reader.data(Path(document["session"]) / "session.json")[0]
    _need(
        _same(state, saved) and _binding(state) == document["session_binding"],
        "receiver v2 proposal belongs to a different or changed current session",
    )
    index = document["step"]
    request = validate_request_v2(document["request"], state.get("params"))
    _, declared = build_recipe_v2(request, pin["file"], pin["sha256"], _originals(document["observations"]))
    _need(_same(actor, declared), "receiver v2 proposal must retain its declared inputs and constants")
    name = f"s{index:04d}/{RECEIPT_V2}"
    _need(_same(state.get("artifacts", {}).get(name), pin), "receiver v2 proposal declaration is not registered in this session")
    if current:
        _need(R.fingerprint(state["bundle"]) == state["target"], "receiver v2 proposal target bytes changed")
    reader.recheck()
    return {
        "document": document,
        "resources": resources0,
        "type_basis": TYPE_BASIS_V2,
        "evidence_basis": dict(BASIS_V2),
        "independently_reviewed": False,
        "proof_created": False,
    }


class CrashBlocklist:
    """Crash-isolated hypothesis blocklist so discovery never repeats a crashing C++ ABI guess."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_ledger(cls, ledger: dict[str, Any]) -> CrashBlocklist:
        _keys(ledger, {"format", "blocked_count", "entries"})
        _need(ledger["format"] == CRASH_LEDGER_FORMAT, "invalid crash blocklist ledger format")
        inst = cls()
        for row in ledger["entries"]:
            inst.record_crash(
                module_sha256=row["module_sha256"],
                offset=row["offset"],
                argument_types=row["argument_types"],
                signal_or_reason=row["signal_or_reason"],
                step=row["step"],
                dispatch=row.get("dispatch", "direct"),
                slot_offset=row.get("vtable_slot_offset"),
            )
        return inst

    @staticmethod
    def hypothesis_key(module_sha256: str, offset: str, argument_types: list[Any], dispatch: str = "direct", slot_offset: int | None = None) -> str:
        _need(SHA256_RE.fullmatch(module_sha256) is not None, "valid module sha256 required")
        _need(HEX_OFFSET.fullmatch(offset) is not None, "valid hex offset required")
        payload = {
            "module_sha256": module_sha256,
            "offset": offset,
            "argument_types": list(argument_types),
            "dispatch": dispatch,
            "vtable_slot_offset": slot_offset,
        }
        return _digest(payload)

    def record_crash(
        self,
        *,
        module_sha256: str,
        offset: str,
        argument_types: list[Any],
        signal_or_reason: str,
        step: int,
        dispatch: str = "direct",
        slot_offset: int | None = None,
    ) -> str:
        key = self.hypothesis_key(module_sha256, offset, argument_types, dispatch=dispatch, slot_offset=slot_offset)
        self._entries[key] = {
            "key": key,
            "module_sha256": module_sha256,
            "offset": offset,
            "argument_types": list(argument_types),
            "dispatch": dispatch,
            "vtable_slot_offset": slot_offset,
            "signal_or_reason": signal_or_reason,
            "step": step,
        }
        return key

    def is_blocked(
        self,
        *,
        module_sha256: str,
        offset: str,
        argument_types: list[Any],
        dispatch: str = "direct",
        slot_offset: int | None = None,
    ) -> bool:
        key = self.hypothesis_key(module_sha256, offset, argument_types, dispatch=dispatch, slot_offset=slot_offset)
        return key in self._entries

    def export_ledger(self) -> dict[str, Any]:
        return {
            "format": CRASH_LEDGER_FORMAT,
            "blocked_count": len(self._entries),
            "entries": [copy.deepcopy(self._entries[k]) for k in sorted(self._entries)],
        }


def _validate_trace_inputs(baseline_calls: Any, actuated_calls: Any, observed_getters: Any,
                           target_input_value: Any = None) -> None:
    def offset(value, field):
        _need(type(value) is str and HEX_OFFSET.fullmatch(value) is not None,
              field + " must be a bounded lowercase hex offset")

    def pointer(value, field):
        _need(type(value) is str and TRACE_POINTER.fullmatch(value) is not None,
              field + " must be a bounded lowercase hex pointer")

    def scalar(value):
        return (type(value) is bool or
                type(value) is int and -(1 << 63) <= value < (1 << 64) or
                type(value) is float and math.isfinite(value))

    for name, rows in (("baseline_calls", baseline_calls), ("actuated_calls", actuated_calls),
                       ("observed_getters", observed_getters)):
        _need(type(rows) is list and len(rows) <= MAX_TRACE_ROWS,
              f"{name} must be a list of at most {MAX_TRACE_ROWS} rows")
        for index, row in enumerate(rows):
            field = f"{name}[{index}]"
            _need(type(row) is dict, field + " must be an observation object")
            if name == "observed_getters":
                required, optional = {"offset", "returned_x0"}, {"vptr_rva"}
            else:
                required = {"offset", "x0"} if name == "actuated_calls" else {"offset"}
                optional = {"x0", "scalar_args", "vptr_rva", "vtable_slot_offset", "this_adjustment"}
            _need(required <= set(row) <= required | optional, field + " has missing or unsupported observation fields")
            offset(row["offset"], field + ".offset")
            for key in ("x0", "returned_x0"):
                if key in row:
                    pointer(row[key], field + "." + key)
            if row.get("vptr_rva") is not None:
                offset(row["vptr_rva"], field + ".vptr_rva")
            if "scalar_args" in row:
                values = row["scalar_args"]
                _need(type(values) is list and len(values) <= 7 and all(scalar(v) for v in values),
                      field + ".scalar_args must contain at most seven finite scalars")
            if row.get("vtable_slot_offset") is not None:
                slot = row["vtable_slot_offset"]
                _need(type(slot) is int and 0 <= slot <= MAX_VTABLE_SLOT_OFFSET and slot % 8 == 0,
                      field + ".vtable_slot_offset must be bounded and 8-byte aligned")
            if "this_adjustment" in row:
                adjust = row["this_adjustment"]
                _need(type(adjust) is int and abs(adjust) <= MAX_THIS_ADJUSTMENT and adjust % 8 == 0,
                      field + ".this_adjustment must be bounded and 8-byte aligned")
    if target_input_value is not None:
        _need(type(target_input_value) in (int, float) and scalar(target_input_value),
              "target_input_value must be a bounded finite number")


def validate_trace_request(request: Any) -> dict[str, Any]:
    """Pure validation shared by the reasoning driver and dispatched handler."""
    _keys(request, {"op", "baseline_calls", "actuated_calls", "observed_getters"},
          {"target_input_value", "module_sha256"})
    _need(request["op"] == "correlate_native_trace_v2", "correlate_native_trace_v2 op required")
    _validate_trace_inputs(request["baseline_calls"], request["actuated_calls"], request["observed_getters"],
                           request.get("target_input_value"))
    if request.get("module_sha256") is not None:
        _need(type(request["module_sha256"]) is str and SHA256_RE.fullmatch(request["module_sha256"]) is not None,
              "module_sha256 must be a valid 64-hex digest")
    _bounded(request)
    return copy.deepcopy(request)


def correlate_differential_trace(
    *,
    baseline_calls: list[dict[str, Any]],
    actuated_calls: list[dict[str, Any]],
    observed_getters: list[dict[str, Any]],
    target_input_value: int | float | None = None,
) -> dict[str, Any]:
    """Rank supplied observations only; no trace capture or native execution."""
    _validate_trace_inputs(baseline_calls, actuated_calls, observed_getters, target_input_value)
    baseline_offsets = {row["offset"] for row in baseline_calls}
    getters_by_ptr: dict[str, list[dict[str, Any]]] = {}
    for g in observed_getters:
        ptr = g.get("returned_x0")
        if int(ptr, 16) != 0:
            getters_by_ptr.setdefault(ptr, []).append(g)

    matched_count = 0

    def matches():
        nonlocal matched_count
        for call in actuated_calls:
            offset, x0 = call["offset"], call["x0"]
            if x0 not in getters_by_ptr:
                continue
            is_differential = offset not in baseline_offsets
            matches_input = target_input_value is not None and any(
                type(value) in (int, float) and value == target_input_value
                for value in call.get("scalar_args", [])
            )
            for getter in getters_by_ptr[x0]:
                if getter["offset"] == offset:
                    continue
                matched_count += 1
                score = (4 if is_differential else 1) + (4 if matches_input else 0) + (2 if call.get("vtable_slot_offset") is not None else 1)
                yield {
                    "getter_offset": getter["offset"], "consumer_offset": offset,
                    "receiver_address_ephemeral": x0,
                    "vptr_rva": getter.get("vptr_rva") or call.get("vptr_rva"),
                    "dispatch": "vtable_slot" if call.get("vtable_slot_offset") is not None else "direct",
                    "vtable_slot_offset": call.get("vtable_slot_offset"),
                    "this_adjustment": call.get("this_adjustment", 0),
                    "matched_input_value": matches_input, "differential_only": is_differential, "score": score,
                }

    # A bounded pair of lists can still form 65,536 matches. Keep only the best
    # bounded result set without materializing the Cartesian product.
    candidates = heapq.nsmallest(MAX_TRACE_CANDIDATES, matches(),
                                key=lambda item: (-item["score"], item["getter_offset"], item["consumer_offset"]))
    return {
        "format": TRACE_CORRELATION_FORMAT,
        "candidate_count": len(candidates),
        "matched_candidate_count": matched_count,
        "candidates_truncated": matched_count > len(candidates),
        "candidates": candidates,
        "evidence_basis": dict(BASIS_V2),
        "input_basis": "caller_supplied_trace_observations",
        "evidence_only": True,
        "proof_created": False,
    }


def record_crash_for_actor(
    state: dict[str, Any],
    actor: dict[str, Any],
    reason: str,
    step: int,
    folder: Path | str | None = None,
) -> dict[str, Any] | None:
    """Record a crashed/faulted V2 ABI hypothesis in the session crash blocklist."""
    if not has_hypothesis_v2(actor):
        return None
    blocklist = (
        CrashBlocklist.from_ledger(state["crash_blocklist"])
        if type(state.get("crash_blocklist")) is dict
        else CrashBlocklist()
    )
    module_sha256 = actor["target"]["module"]["sha256"]
    offset = actor["target"]["offset"]
    argument_types = [arg["type"] for arg in actor["arguments"]]
    dispatch = actor.get("dispatch", "direct")
    slot_offset = actor.get("vtable_slot_offset") if dispatch == "vtable_slot" else None
    key = blocklist.record_crash(
        module_sha256=module_sha256,
        offset=offset,
        argument_types=argument_types,
        signal_or_reason=str(reason)[:256] or "native_crash_or_fault",
        step=step,
        dispatch=dispatch,
        slot_offset=slot_offset,
    )
    ledger = blocklist.export_ledger()
    state["crash_blocklist"] = ledger
    if folder is not None:
        receipt_path = Path(folder) / "crash-blocklist.json"
        with receipt_path.open("w", encoding="utf-8") as stream:
            json.dump(ledger, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
    return {"key": key, "ledger": ledger}


def _collect_v2_subjects(value: Any, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if out is None:
        out = []
    out.extend(_iter_v2_subjects(value))
    return out


def validate_runtime_transaction(state: dict[str, Any], root: Path | str, transaction: Any) -> None:
    """Ensure any V2 receiver hypothesis in a runtime transaction belongs to this session and is not crash-blocklisted."""
    if not has_hypothesis_v2(transaction):
        return
    _policy(state)
    R, N = _runtime_modules()
    R.assert_guest()
    root_path = Path(root).resolve(strict=True)
    reader = N.Reader()
    blocklist = (
        CrashBlocklist.from_ledger(state["crash_blocklist"])
        if type(state.get("crash_blocklist")) is dict
        else None
    )
    for subject in _collect_v2_subjects(transaction):
        if is_recipe_v2(subject):
            validate_recipe(subject)
            claim = subject["root"]["getter"]["signature_evidence"]
            consumer = subject["consumer"]
            module_sha256 = consumer["entry"]["module"]["sha256"]
            offset = consumer["entry"]["offset"]
            arg_types = list(consumer["argument_types"])
            dispatch = consumer["dispatch"]
            slot_offset = consumer.get("vtable_slot_offset") if dispatch == "vtable_slot" else None
        else:
            claim = subject["signature_evidence"]
            evidence_claim(claim)
            if (
                type(subject.get("target")) is dict
                and type(subject["target"].get("module")) is dict
                and type(subject["target"]["module"].get("sha256")) is str
                and type(subject["target"].get("offset")) is str
                and type(subject.get("arguments")) is list
                and all(type(a) is dict and "type" in a for a in subject["arguments"])
                and claim.get("pointer") == "/signatures/consumer"
            ):
                module_sha256 = subject["target"]["module"]["sha256"]
                offset = subject["target"]["offset"]
                arg_types = [a["type"] for a in subject["arguments"]]
                dispatch = subject.get("dispatch", "direct")
                slot_offset = subject.get("vtable_slot_offset") if dispatch == "vtable_slot" else None
            else:
                module_sha256 = offset = arg_types = dispatch = slot_offset = None
        if blocklist is not None and module_sha256 is not None:
            _need(
                not blocklist.is_blocked(
                    module_sha256=module_sha256,
                    offset=offset,
                    argument_types=arg_types,
                    dispatch=dispatch,
                    slot_offset=slot_offset,
                ),
                "receiver v2 runtime operation matches a crash-blocklisted ABI hypothesis",
            )
        document, pin = reader.data(claim["file"])
        _need(
            Path(document.get("session", "")).resolve(strict=True) == root_path
            and _binding(state) == document.get("session_binding"),
            "receiver v2 runtime operation belongs to a different or changed current session",
        )
        index = document.get("step")
        name = f"s{index:04d}/{RECEIPT_V2}" if type(index) is int else ""
        _need(
            _same(state.get("artifacts", {}).get(name), pin),
            "receiver v2 runtime declaration is not registered in this session",
        )


def record_failed_runtime_or_trial(
    state: dict[str, Any],
    subject: Any,
    receipt: Any,
    step: int,
    folder: Path | str | None = None,
) -> dict[str, Any] | None:
    """Automatically blocklist a V2 receiver ABI hypothesis when a runtime transaction or visual trial crashes/faults during dispatch."""
    if type(receipt) is not dict:
        return None
    actors: list[dict[str, Any]] = []
    if type(subject) is dict and isinstance(subject.get("operations"), list):
        for op in subject["operations"]:
            if type(op) is dict and has_hypothesis_v2(op) and "target" in op and "arguments" in op:
                actors.append(op)
    elif type(subject) is dict and has_hypothesis_v2(subject) and "target" in subject and "arguments" in subject:
        actors.append(subject)
    if not actors:
        return None
    disp = receipt.get("runtime_disposition") if type(receipt.get("runtime_disposition")) is dict else None
    if disp is not None:
        if disp.get("status") == "unavailable" or (
            disp.get("actuated") is False
            and not disp.get("native_state_uncertain")
            and not disp.get("native_calls")
            and not disp.get("worker_native_calls")
            and not (disp.get("attached") is True and disp.get("worker_native_calls") is None)
        ):
            return None
        if (
            disp.get("status") in {"completed", "executed", "observed"}
            and not disp.get("resource_review_required")
            and not disp.get("native_state_uncertain")
            and disp.get("error") is None
            and receipt.get("actuated") is True
            and not receipt.get("resource_review_required")
        ):
            return None
    status = receipt.get("status")
    if status in {"completed", "executed", "observed", "model_judged", "unavailable"}:
        return None
    result_obj = receipt.get("result") if type(receipt.get("result")) is dict else {}
    actuated = receipt.get("actuated")
    native_calls = receipt.get("native_calls")
    worker_native_calls = result_obj.get("native_calls")
    native_uncertain = bool(
        receipt.get("native_state_uncertain")
        or result_obj.get("native_state_uncertain")
        or (disp is not None and disp.get("native_state_uncertain"))
    )
    # Never blocklist pre-dispatch refusals (e.g., transport unavailable before attach, script.load failure with confirmed zero native calls, or visual precondition failure with actuated=False)
    if (
        actuated is False
        and not native_uncertain
        and not native_calls
        and not worker_native_calls
        and not (result_obj.get("attached") is True and worker_native_calls is None)
    ):
        return None
    dispatch_attempted_or_uncertain = bool(
        native_uncertain
        or actuated is None
        or actuated is True
        or native_calls is True
        or (type(worker_native_calls) is int and worker_native_calls > 0)
        or (result_obj.get("attached") is True and worker_native_calls is None)
    )
    if not dispatch_attempted_or_uncertain:
        return None
    crashed_or_faulted = bool(
        native_uncertain
        or receipt.get("resource_review_required")
        or status in {"failed", "incomplete", "error", "crashed"}
        or (status == "refused" and actuated is None)
    )
    if not crashed_or_faulted:
        return None
    error_obj = (disp.get("error") if disp is not None and disp.get("error") else None) or receipt.get("error") or result_obj.get("error")
    error_msg = (
        error_obj.get("message", "")
        if type(error_obj) is dict
        else str(error_obj or "")
    )
    reason = error_msg or str(status or "native_crash_or_fault")
    recorded = None
    for actor in actors:
        recorded = record_crash_for_actor(state, actor, reason, step, folder=folder)
    return recorded


def correlate_trace_step(
    session: Any,
    state: dict[str, Any],
    request: dict[str, Any],
    folder: Any,
) -> dict[str, Any]:
    """Agent step handler for `correlate_native_trace_v2`."""
    R, _ = _runtime_modules()
    R.assert_guest()
    _policy(state)
    _need(state.get("status") == "investigating", "trace correlation closes before evaluation or proof")
    request = validate_trace_request(request)
    target_val = request.get("target_input_value")
    module_sha256 = request.get("module_sha256")
    correlation = correlate_differential_trace(
        baseline_calls=request["baseline_calls"],
        actuated_calls=request["actuated_calls"],
        observed_getters=request["observed_getters"],
        target_input_value=target_val,
    )
    if type(state.get("crash_blocklist")) is dict:
        blocklist = CrashBlocklist.from_ledger(state["crash_blocklist"])
        entries = state["crash_blocklist"].get("entries", [])
        for candidate in correlation["candidates"]:
            blocked_matches = [
                row["argument_types"]
                for row in entries
                if (module_sha256 is None or row["module_sha256"] == module_sha256)
                and row["offset"] == candidate["consumer_offset"]
                and row.get("dispatch", "direct") == candidate["dispatch"]
                and row.get("vtable_slot_offset") == candidate["vtable_slot_offset"]
            ]
            candidate["crash_blocked_argument_signatures"] = blocked_matches
            candidate["crash_blocked_count"] = len(blocked_matches)
        _need(blocklist.export_ledger()["blocked_count"] == len(entries), "crash blocklist ledger invariant failed")
    folder_path = Path(folder)
    receipt_file = folder_path / "native-receiver-trace-correlation.json"
    _need(not receipt_file.exists(), "trace correlation receipt never overwrites an artifact")
    with receipt_file.open("x", encoding="utf-8") as stream:
        json.dump(correlation, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    return {
        "status": "native_receiver_trace_correlated",
        "receipt": str(receipt_file),
        "candidate_count": correlation["candidate_count"],
        "matched_candidate_count": correlation["matched_candidate_count"],
        "candidates_truncated": correlation["candidates_truncated"],
        "candidates": correlation["candidates"],
        "input_basis": correlation["input_basis"],
        "evidence_basis": dict(BASIS_V2),
        "evidence_only": True,
        "proof_created": False,
    }

