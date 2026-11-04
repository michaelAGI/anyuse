"""Inert runtime provider fixtures; execute only through the guest guarded suite."""
import copy
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B

ACTOR = {"kind": "objc_method", "receiver": {"class": "MappedReader", "kind": "unique", "subclasses": False},
         "selector": "readResult:reference:enabled:", "signature": "v40@0:8@16@24B32",
         "arguments": [{"type": "utf8", "value": {"input": "query"}},
                       {"type": "utf8", "value": "exact reference"}, {"type": "bool", "value": True}],
         "return_type": "void"}
SEQUENCE = {"kind": "objc_sequence", "steps": [
    {"kind": "objc_method", "receiver": {"class": "MappedMover", "kind": "unique", "subclasses": False},
     "selector": "beginSelection:", "signature": "v24@0:8C16", "return_type": "void",
     "arguments": [{"type": "uint8", "value": {"input": "source"}}]},
    {"kind": "objc_method", "receiver": {"class": "MappedMover", "kind": "unique", "subclasses": False},
     "selector": "endSelection:animate:", "signature": "v24@0:8C16B20", "return_type": "void",
     "arguments": [{"type": "uint8", "value": {"input": "destination"}},
                   {"type": "bool", "value": {"input": "animate"}}]}]}


def test_actor_binds_only_typed_inputs_and_resolves_receivers_each_transaction():
    saved = copy.deepcopy(ACTOR)
    result = B.bind_actor(saved, {"query": "requested state"})
    assert result["arguments"][0]["value"] == "requested state" and saved == ACTOR
    request = B.invocation_request(result)
    assert request == {"operations": [{"op": "invoke", **{k: v for k, v in result.items() if k != "kind"}}]}
    assert B.validate_request(request) == request
    assert B.parse_encoding('v40@0:8@16@24B32') == ["v", "@", ":", "@", "@", "B"]
    assert B.parse_encoding('@"NSString"16@0:8') == ["@", "@", ":"]
    with pytest.raises(ValueError, match="missing"):
        B.bind_actor(saved, {})
    with pytest.raises(ValueError, match="declared type"):
        B.bind_actor(saved, {"query": 123})


@pytest.mark.parametrize("selector,signature,supplied,expected", [
    ("setGeometry:", "v24@0:8@16", [], 1),
    ("setPair:with:", "v32@0:8i16i20", [{"type": "int32", "value": 3}], 2),
    ("refresh", "v16@0:8", [{"type": "nil", "value": None}], 0),
])
def test_signature_arity_diagnostic_describes_submitted_syntax_without_repair(selector, signature, supplied, expected):
    actor = {"kind": "objc_method", "receiver": {"class": "Synthetic.GeometryOwner", "kind": "unique"},
             "selector": selector, "signature": signature, "arguments": supplied, "return_type": "void"}
    before = copy.deepcopy(actor)
    with pytest.raises(ValueError) as caught:
        B.validate_actor(actor)
    message = str(caught.value)
    assert str(expected) in message and str(len(supplied)) in message
    assert "explicit" in message and "implicit" in message
    assert "self" in message and ("_cmd" in message or "selector" in message)
    assert "nil" not in message and "null" not in message
    assert actor == before


def test_signature_implicit_prefix_error_is_distinct_and_keeps_invalid_encoding_refused():
    actor = {"kind": "objc_method", "receiver": {"class": "Synthetic.Owner", "kind": "class"},
             "selector": "refresh", "signature": "v16i0:8", "arguments": [], "return_type": "void"}
    with pytest.raises(ValueError) as caught:
        B.validate_actor(actor)
    assert "self" in str(caught.value) and "argument count" not in str(caught.value)


def test_sequence_binding_preserves_order_and_converts_frozen_inputs_before_request():
    saved = copy.deepcopy(SEQUENCE)
    result = B.bind_actor(saved, {"source": "12", "destination": "28", "animate": "false"})
    methods = B.actor_methods(result)
    assert [m["selector"] for m in methods] == ["beginSelection:", "endSelection:animate:"]
    assert [a["value"] for m in methods for a in m["arguments"]] == [12, 28, False]
    request = B.invocation_request(result)
    assert [op["selector"] for op in request["operations"]] == [m["selector"] for m in methods]
    assert all(op["op"] == "invoke" and "kind" not in op for op in request["operations"])
    assert B.validate_request(request) == request and saved == SEQUENCE
    methods[0]["arguments"][0]["value"] = 0
    assert result["steps"][0]["arguments"][0]["value"] == 12
    assert B.actor_methods(ACTOR, allow_bindings=True) == [ACTOR]
    with pytest.raises(ValueError, match="missing"):
        B.bind_actor(SEQUENCE, {"source": "12", "animate": "true"})


@pytest.mark.parametrize("count", [0, 1, 9])
def test_sequence_budget_is_two_to_eight_method_children(count):
    method = B.bind_actor(ACTOR, {"query": "q"})
    with pytest.raises(ValueError, match="2–8"):
        B.validate_actor({"kind": "objc_sequence", "steps": [method] * count})


def test_sequence_accepts_its_eight_step_limit_without_merging_or_reordering_methods():
    methods = [B.bind_actor(ACTOR, {"query": str(i)}) for i in range(8)]
    actor = {"kind": "objc_sequence", "steps": methods}
    request = B.invocation_request(actor)
    assert len(request["operations"]) == 8
    assert [op["arguments"][0]["value"] for op in request["operations"]] == [str(i) for i in range(8)]


@pytest.mark.parametrize("change", ["nested", "receiver_alias", "argument_alias", "extra", "nonlist", "abi", "nonmethod"])
def test_sequence_never_flattens_nested_actors_or_persists_transaction_handles(change):
    actor = B.bind_actor(SEQUENCE, {"source": "12", "destination": "28", "animate": "true"})
    if change == "nested":
        actor["steps"][1] = copy.deepcopy(actor)
    elif change == "receiver_alias":
        actor["steps"][1]["receiver"] = {"alias": "previous-step-object"}
    elif change == "argument_alias":
        actor["steps"][1]["arguments"][0] = {"type": "object", "value": {"alias": "previous-step-object"}}
        actor["steps"][1]["signature"] = "v24@0:8@16B20"
    elif change == "extra":
        actor["atomic"] = True
    elif change == "nonlist":
        actor["steps"] = tuple(actor["steps"])
    elif change == "abi":
        actor["steps"][1]["signature"] = "v24@0:8i16B20"
    else:
        actor["steps"][1]["kind"] = "open_document"
    with pytest.raises(ValueError):
        B.validate_actor(actor)


@pytest.mark.parametrize("kind,source,expected", [
    ("utf8", "text", "text"), ("bool", "true", True), ("bool", "false", False),
    ("int8", "-128", -128), ("uint8", "255", 255), ("int16", "-32768", -32768),
    ("uint16", "65535", 65535), ("int32", "-2147483648", -2147483648), ("uint32", "4294967295", 4294967295),
    ("int64", "-9223372036854775808", "-9223372036854775808"),
    ("uint64", "18446744073709551615", "18446744073709551615"),
    ("long", "9223372036854775807", "9223372036854775807"),
    ("ulong", "18446744073709551615", "18446744073709551615")])
def test_scalar_binding_conversion_is_explicit_and_preserves_wide_integer_strings(kind, source, expected):
    actual = B.convert_binding(kind, source)
    assert type(actual) is type(expected) and actual == expected
    encoding = next(iter(B.ENCODINGS[kind]))
    actor = {"kind": "objc_method", "receiver": {"class": "MappedScalar", "kind": "class"},
             "selector": "setValue:", "signature": "v24@0:8" + encoding + "16", "return_type": "void",
             "arguments": [{"type": kind, "value": {"input": "value"}}]}
    assert B.bind_actor(actor, {"value": source})["arguments"][0]["value"] == expected


@pytest.mark.parametrize("kind,source", [
    ("uint8", "256"), ("int8", "-129"), ("uint16", "65536"), ("int16", "32768"),
    ("uint32", "4294967296"), ("int32", "-2147483649"), ("int64", "9223372036854775808"),
    ("uint64", "18446744073709551616"), ("long", "-9223372036854775809"), ("ulong", "-1"),
    ("uint8", "-0"), ("uint8", "01"), ("uint8", "+1"), ("uint8", " 1"), ("uint8", "1.0"),
    ("uint8", "1e1"), ("uint8", 1), ("uint8", True), ("bool", "True"), ("bool", "1"),
    ("bool", True), ("utf8", None), ("float", "nan"), ("nil", "null"), ("object", "0x1")])
def test_scalar_binding_refuses_ranges_noncanonical_numbers_or_implicit_coercion(kind, source):
    with pytest.raises(ValueError):
        B.convert_binding(kind, source)


@pytest.mark.parametrize("kind,encoding", [("float", "f"), ("double", "d"), ("nil", "@"), ("object", "@")])
def test_recipe_input_bindings_do_not_enable_objects_nil_or_floating_point(kind, encoding):
    actor = {"kind": "objc_method", "receiver": {"class": "X", "kind": "class"},
             "selector": "setValue:", "signature": "v24@0:8" + encoding + "16", "return_type": "void",
             "arguments": [{"type": kind, "value": {"input": "value"}}]}
    with pytest.raises(ValueError, match="binding"):
        B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("change", ["pointer", "code", "alias", "signature", "count", "argument_type", "unknown", "block"])
def test_reusable_actor_rejects_generated_source_addresses_and_guessed_abi(change):
    value = B.bind_actor(ACTOR, {"query": "q"})
    if change == "pointer":
        value["receiver"] = {"address": "0x123456"}
    elif change == "code":
        value["javascript"] = "doSomething()"
    elif change == "alias":
        value["receiver"] = {"alias": "from_previous_attach"}
    elif change == "signature":
        value["signature"] = "i40@0:8@16@24B32"
    elif change == "count":
        value["arguments"].pop()
    elif change == "argument_type":
        value["arguments"][0] = {"type": "int32", "value": 123}
    elif change == "unknown":
        value["receiver"]["choose_first"] = True
    else:
        value["signature"] = "v40@0:8@?16@24B32"
    with pytest.raises(ValueError):
        B.validate_actor(value)


def test_transaction_aliases_are_declared_before_use_and_never_escape_receipts():
    request = {"operations": [
        {"op": "instances", "class": "MappedReader", "alias": "reader"},
        {"op": "read", "receiver": {"alias": "reader"}, "fields": [
            {"name": "text", "kind": "getter", "selector": "text", "type": "utf8", "signature": "@16@0:8"},
            {"name": "count", "kind": "ivar", "ivar": "_count", "type": "int32", "encoding": "i"}]}
    ]}
    assert B.validate_request(request) == request
    changed = copy.deepcopy(request)
    changed["operations"].reverse()
    with pytest.raises(ValueError, match="alias"):
        B.validate_request(changed)
    changed = copy.deepcopy(request)
    changed["operations"].append(changed["operations"][0])
    with pytest.raises(ValueError, match="unused alias"):
        B.validate_request(changed)


def test_read_receiver_class_precondition_is_optional_closed_and_validated():
    read = {"op": "read", "receiver": {"class": "MappedReader", "kind": "unique"},
            "fields": [{"name": "text", "kind": "getter", "selector": "text", "type": "object", "signature": "@16@0:8"}]}
    assert B.validate_request({"operations": [read]}) == {"operations": [read]}
    guarded = {**read, "expected_class": "MappedReader"}
    assert B.validate_request({"operations": [guarded]}) == {"operations": [guarded]}
    for malformed in [None, "", 1, "Mapped Reader", "0x1234", "MappedReader\0"]:
        with pytest.raises(ValueError, match="expected_class"):
            B.validate_request({"operations": [{**read, "expected_class": malformed}]})
    with pytest.raises(ValueError, match="unknown"):
        B.validate_request({"operations": [{"op": "classes", "expected_class": "MappedReader"}]})


@pytest.mark.parametrize("klass", ["..NSKVONotifying_IINA.MainWindow", ".App.Window", "App.Window", "." + "A" * 255])
def test_exact_runtime_class_spelling_survives_metadata_receivers_and_saved_actors(klass):
    receiver = {"class": klass, "kind": "unique", "subclasses": False}
    field = {"name": "child", "kind": "getter", "selector": "child", "type": "object", "signature": "@16@0:8"}
    requests = [
        {"operations": [{"op": op, "class": klass}]} for op in ("methods", "ivars")]
    requests += [
        {"operations": [{"op": "instances", "class": klass, "alias": "object"}]},
        {"operations": [{"op": "read", "receiver": receiver, "expected_class": klass, "fields": [field]}]},
        {"operations": [{"op": "graph", "root": receiver, "types": [
            {"class": klass, "fields": [{**field, "target": klass}]}],
            "max_depth": 1, "max_nodes": 2, "methods_limit": 1}]},
    ]
    for request in requests:
        original = copy.deepcopy(request)
        assert B.validate_request(request) == original and request == original
    actor = B.bind_actor(ACTOR, {"query": "q"})
    actor["receiver"] = copy.deepcopy(receiver)
    assert B.validate_actor(actor)["receiver"]["class"] == klass
    assert B.invocation_request(actor)["operations"][0]["receiver"]["class"] == klass


@pytest.mark.parametrize("klass", [None, 1, "", ".", "..", "..9Bad", " App", "App Window", "App\n", "App\0",
    "App/Window", "App:Window", "App['Window']", "A" * 257, "." + "A" * 256,
    "prototype", "constructor", "__proto__", "toJSON", "toString", "valueOf", "hasOwnProperty",
    ".constructor", "..__proto__"])
def test_runtime_class_names_remain_bounded_exact_and_exclude_reserved_names(klass):
    with pytest.raises(ValueError):
        B.validate_request({"operations": [{"op": "methods", "class": klass}]})
    actor = B.bind_actor(ACTOR, {"query": "q"})
    actor["receiver"]["class"] = klass
    with pytest.raises(ValueError):
        B.validate_actor(actor)


@pytest.mark.parametrize("slot", ["selector", "ivar", "alias"])
def test_runtime_class_fix_does_not_broaden_other_identifiers(slot):
    klass = "..NSKVONotifying_IINA.MainWindow"
    field = {"name": "value", "kind": "getter", "selector": "value", "type": "object", "signature": "@16@0:8"}
    operation = {"op": "read", "receiver": {"class": klass, "kind": "unique"}, "fields": [field]}
    if slot == "selector":
        field["selector"] = ".value"
    elif slot == "ivar":
        field.update(kind="ivar", ivar=".value", encoding="@")
        del field["selector"], field["signature"]
    else:
        operation = {"op": "instances", "class": klass, "alias": ".object"}
    with pytest.raises(ValueError):
        B.validate_request({"operations": [operation]})


def test_class_singleton_getter_path_has_observed_signature_and_no_raw_object_id():
    value = B.bind_actor(ACTOR, {"query": "q"})
    value["receiver"] = {"class": "NSDocumentController", "kind": "class", "path": [
        {"selector": "sharedDocumentController", "signature": "@16@0:8"},
        {"selector": "currentDocument", "signature": "@16@0:8"}]}
    assert B.validate_actor(value) == value
    value["receiver"]["path"][1]["signature"] = "i16@0:8"
    with pytest.raises(ValueError, match="return"):
        B.validate_actor(value)


@pytest.mark.parametrize("bad_request", [
    {}, {"operations": []}, {"operations": [{"op": "classes", "query": "x", "eval": "js"}]},
    {"operations": [{"op": "classes", "limit": True}]}, {"operations": [{"op": "classes", "limit": 129}]},
    {"operations": [{"op": "classes", "owner": []}]}, {"operations": [{"op": []}]},
    {"operations": [{"op": "methods", "class": "constructor"}]},
    {"operations": [{"op": "instances", "class": "X", "alias": "x", "subclasses": 1}]},
    {"operations": [{"op": "classes"}] * 17},
    {"operations": [{"op": "read", "receiver": {"class": "X", "kind": "unique"}, "fields": [
        {"name": "bad", "kind": "getter", "selector": "setText:", "type": "utf8"}]}]},
    {"operations": [{"op": "read", "receiver": {"class": "X", "kind": "unique"}, "fields": [
        {"name": "bad", "kind": "ivar", "ivar": "_state", "type": "int32", "encoding": "^i"}]}]},
])
def test_malformed_or_unbounded_requests_fail_before_attachment(bad_request):
    with pytest.raises(ValueError):
        B.validate_request(bad_request)


@pytest.mark.parametrize("kind,value,signature", [
    ("uint8", 256, "v24@0:8C16"), ("uint8", True, "v24@0:8C16"),
    ("int32", -(2**31)-1, "v24@0:8i16"), ("float", float("inf"), "v24@0:8f16"),
    ("double", 10**1000, "v24@0:8d16"), ("bool", 1, "v24@0:8B16"),
    ("int64", 5, "v24@0:8q16"), ("uint64", "-1", "v24@0:8Q16"),
    ("utf8", "embedded\0nul", "v24@0:8@16"), ("nil", "nil", "v24@0:8@16")])
def test_typed_values_preserve_abi_ranges_and_do_not_coerce_data(kind, value, signature):
    request = {"operations": [{"op": "invoke", "receiver": {"class": "X", "kind": "unique"},
        "selector": "setValue:", "signature": signature, "arguments": [{"type": kind, "value": value}], "return_type": "void"}]}
    with pytest.raises(ValueError):
        B.validate_request(request)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = tmp_path / "Canvas.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    executable = app / "Contents/MacOS/Canvas"
    executable.write_bytes(b"inert executable fixture")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.test.Canvas", "CFBundleVersion": "1"}))
    identity = {"bundle_id": "org.test.Canvas", "bundle": str(app), "pid": 321, "launch_time": 123.25,
                "uid": os.getuid(), "executable": str(executable)}
    adapter = tmp_path / "compiled-agent.js"
    adapter.write_text("inert compiled asset fixture")
    build = {"format": "anyuse.rea-runtime-adapter/1", "source_sha256": R._file_hash(Path(B.__file__).with_name("rea_runtime_agent.js")),
             "bundle_sha256": R._file_hash(adapter), "compiler": "fixture"}
    R.write_json(str(adapter) + ".manifest.json", build)
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_AGENT", str(adapter))
    monkeypatch.delenv("ANYUSE_REA_RUNTIME_PYTHON", raising=False)
    for name in ("ANYUSE_REA_RUNTIME_TRANSPORT", "ANYUSE_REA_RUNTIME_GADGET_ENDPOINT",
                 "ANYUSE_REA_RUNTIME_GADGET_TOKEN_FILE", "ANYUSE_REA_RUNTIME_GADGET_LIBRARY",
                 "ANYUSE_REA_RUNTIME_HELPER_POLICY"):
        monkeypatch.delenv(name, raising=False)
    state = {"calls": [], "result_changes": {}, "runner_changes": {}, "after": None, "change_adapter": False,
             "change_bundle": False, "exception": None, "identity_calls": 0, "after_worker": None,
             "stderr": "retained worker diagnostic"}
    def process(bundle, bundle_id):
        assert bundle == app and bundle_id == "org.test.Canvas"
        state["identity_calls"] += 1
        return copy.deepcopy(state["after"] if state["calls"] and state["after"] is not None else identity)
    monkeypatch.setattr(B, "process_identity", process)
    def runner(argv, output, error, *, timeout, cwd):
        config = R.load_json(argv[-1])
        state["calls"].append({"argv": argv, "timeout": timeout, "cwd": cwd, "config": config})
        value = config["request"]
        rows = [{"index": i, "op": op["op"], "status": "ok"} for i, op in enumerate(value["operations"])]
        result = {"format": B.WORKER, "status": "completed", "request_sha256": R.digest(value), "attached": True,
                  "operations": rows, "native_calls": max(1, B.minimum_request_native_calls(value)),
                  "invocations": sum(op["op"] in {"invoke", "native_invoke"} for op in value["operations"]),
                  "handles_expired": True, "cleanup": {"script_unloaded": True, "detached": True}, "error": None,
                  "cleanup_errors": [],
                  "frida_lifecycle": {"format": B.FRIDA_LIFECYCLE, "manager_state": "closed",
                                      "attachment_attempted": True, "helper_exit_confirmed": False, "error": None},
                  "transport": config["transport"], "preloaded_gadget_remains": config["transport"]["kind"] == "gadget",
                  "native_resources": config.get("native_resources", {}), "native_state_uncertain": False,
                  "compiler_provenance": config.get("compiler_provenance", {}), "callable_resolutions": 0,
                  "helper_load_attempts": sum(op["op"] == "load_helper" for op in value["operations"]),
                  "helper_residency": []}
        result.update(copy.deepcopy(state["result_changes"]))
        output.write_text(json.dumps(result))
        error.write_text(state["stderr"])
        (cwd / "trace.jsonl").write_text('{"event":"main-queue-scheduled"}\n')
        if state["change_adapter"]:
            adapter.write_text("modified while native worker was active")
        if state["change_bundle"]:
            (app / "Contents/changed").write_text("same version, different app bytes")
        if state["after_worker"]:
            state["after_worker"]()
        if state["exception"]:
            raise state["exception"]
        return {"returncode": 0, "timed_out": False, "elapsed_s": .001, "output": output.name,
                "sha256": R._file_hash(output), "stderr": error.name, **state["runner_changes"]}
    monkeypatch.setattr(R, "_bounded_run", runner)
    return app, identity, adapter, state, tmp_path / "runtime"


def _gadget_fixture(runtime, monkeypatch):
    """Installed-file metadata and mock transport only; no native library load."""
    app, _, _, _, out = runtime
    token = "inert-fixture-runtime-authentication-token"
    token_file = out.parent / "gadget-token.txt"
    token_file.write_text(token)
    token_file.chmod(0o600)
    library = out.parent / "FridaGadget.dylib"
    library.write_bytes(b"inert installed Gadget fixture")
    config = library.with_suffix(".config")
    value = {"interaction": {"type": "listen", "address": "127.0.0.1", "port": 27043,
                             "token": token, "on_port_conflict": "fail", "on_load": "resume"}, "teardown": "full"}
    config.write_text(json.dumps(value))
    config.chmod(0o600)
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_TRANSPORT", "gadget")
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_ENDPOINT", "127.0.0.1:27043")
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_LIBRARY", str(library))
    assert not token_file.is_relative_to(app)
    return token, token_file, library, config


def test_collect_bound_evidence_and_direct_invocation_do_not_mint_a_task_proof(runtime):
    app, identity, adapter, state, out = runtime
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes", "query": "Canvas"}]})
    assert result["status"] == "observed" and result["actuated"] is False
    assert result["identity"] == result["after_identity"] == identity
    assert result["request_sha256"] == R.digest(result["request"])
    assert result["adapter"]["sha256"] == R._file_hash(adapter)
    assert result["adapter"]["manifest"]["sha256"] == R._file_hash(Path(str(adapter) + ".manifest.json"))
    assert result["transport"] == result["adapter"]["transport"] == {"kind": "local"}
    assert "expected_identity" not in result and "expected_identity" not in state["calls"][0]["config"]
    assert result["verified"] is False and result["proof_created"] is False and result["evidence_only"] is True
    assert result["resource_review_required"] is False and not (out / "proof.json").exists()
    assert R.load_json(out / "runtime.json") == result
    request = B.bind_actor(ACTOR, {"query": "state"})
    invoked = B.invoke(str(app), request, str(out.parent / "invoke"))
    assert invoked["status"] == "executed" and invoked["actuated"] is True and invoked["model_calls"] == 0
    assert invoked["result"]["invocations"] == 1 and invoked["verified"] is False


@pytest.mark.parametrize("change", [
    "nonobject", "missing", "foreign_uid", {"extra": True},
    {"pid": True}, {"pid": 0}, {"pid": 2**31}, {"pid": 321.0},
    {"uid": True}, {"uid": -1}, {"uid": 2**32 - 1},
    {"launch_time": True}, {"launch_time": 0}, {"launch_time": float("nan")},
    {"launch_time": float("inf")}, {"launch_time": float("-inf")},
    {"launch_time": 253402300800}, {"launch_time": 10**1000},
    {"bundle": "relative.app"}, {"bundle": "/tmp/../Canvas.app"}, {"bundle": "/tmp//Canvas.app"},
    {"bundle": Path("/tmp/Canvas.app")}, {"executable": "/tmp/Canvas\n"}, {"executable": "/" + "a" * 4096},
    {"bundle_id": ""}, {"bundle_id": "a" * 513}, {"bundle_id": None},
])
def test_expected_identity_rejects_malformed_or_foreign_pins_before_filesystem_or_worker(runtime, monkeypatch, change):
    app, identity, _, state, out = runtime
    expected = copy.deepcopy(identity)
    if change == "nonobject":
        expected = []
    elif change == "missing":
        expected.pop("executable")
    elif change == "foreign_uid":
        expected["uid"] += 1
    else:
        expected.update(change)
    def no_paths(*args, **kwargs):
        pytest.fail("malformed expected identity reached filesystem path resolution")
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "resolve", no_paths)
        with pytest.raises(ValueError, match="expected runtime process identity"):
            B.collect(str(app), str(out), {"operations": [{"op": "classes"}]}, expected_identity=expected)
    assert not out.exists() and not state["calls"] and state["identity_calls"] == 0


@pytest.mark.parametrize("reuse_pid", [False, True])
def test_expected_identity_rejects_successor_observed_during_collect_before_worker_preparation(runtime, monkeypatch, reuse_pid):
    app, identity, _, state, out = runtime
    expected = copy.deepcopy(identity)
    fingerprint = R.fingerprint
    def restart_during_fingerprint(bundle):
        target = fingerprint(bundle)
        identity["launch_time"] += 1
        if not reuse_pid:
            identity["pid"] += 1
        return target
    def no_adapter(*args, **kwargs):
        pytest.fail("successor process reached worker adapter preparation")
    monkeypatch.setattr(R, "fingerprint", restart_during_fingerprint)
    monkeypatch.setattr(B, "adapter_identity", no_adapter)
    receipt = B.collect(app, out, {"operations": [{"op": "classes"}]}, expected_identity=expected)
    assert receipt["status"] == "unavailable" and receipt["error"]["type"] == "Unavailable"
    assert receipt["expected_identity"] == expected and receipt["identity"] == identity != expected
    assert receipt["worker"] is None and receipt["result"] is None and receipt["adapter"] is None
    assert receipt["actuated"] is receipt["native_calls"] is receipt["resource_review_required"] is False
    assert receipt["proof_created"] is False and receipt["verified"] is False
    assert not state["calls"] and state["identity_calls"] == 1 and not (out / "worker-request.json").exists()
    assert R.load_json(out / "runtime.json") == receipt


@pytest.mark.parametrize("operation", ["collect", "invoke"])
def test_matching_expected_identity_is_retained_and_binds_the_worker_config(runtime, operation):
    app, identity, _, state, out = runtime
    expected = copy.deepcopy(identity)
    if operation == "invoke":
        receipt = B.invoke(app, B.bind_actor(ACTOR, {"query": "state"}), out, expected_identity=expected)
    else:
        receipt = B.collect(app, out, {"operations": [{"op": "classes"}]}, expected_identity=expected)
    assert receipt["status"] == ("executed" if operation == "invoke" else "observed")
    assert receipt["expected_identity"] == receipt["identity"] == receipt["after_identity"] == expected
    assert len(state["calls"]) == 1 and state["calls"][0]["config"]["identity"] == expected
    assert R.load_json(out / "worker-request.json")["identity"] == expected
    assert R.load_json(out / "runtime.json")["expected_identity"] == expected


def test_expected_identity_caller_mutation_during_collect_cannot_rebind_the_process_or_receipt(runtime, monkeypatch):
    app, identity, _, state, out = runtime
    expected = copy.deepcopy(identity)
    frozen = copy.deepcopy(expected)
    fingerprint = R.fingerprint
    def mutate_caller_during_fingerprint(bundle):
        expected.update(pid=frozen["pid"] + 1, launch_time=frozen["launch_time"] + 1)
        return fingerprint(bundle)
    monkeypatch.setattr(R, "fingerprint", mutate_caller_during_fingerprint)
    receipt = B.collect(app, out, {"operations": [{"op": "classes"}]}, expected_identity=expected)
    assert receipt["status"] == "observed" and expected != frozen
    assert receipt["expected_identity"] == receipt["identity"] == state["calls"][0]["config"]["identity"] == frozen
    expected.clear()
    assert receipt["expected_identity"] == R.load_json(out / "runtime.json")["expected_identity"] == frozen


def test_invoke_without_expected_identity_preserves_legacy_collect_mock_signature(runtime, monkeypatch):
    app, _, _, state, out = runtime
    calls = []
    retained = {"legacy": "collect result"}
    def legacy_collect(bundle, folder, request, *, timeout=15):
        calls.append((bundle, folder, request, timeout))
        return retained
    monkeypatch.setattr(B, "collect", legacy_collect)
    actor = B.bind_actor(ACTOR, {"query": "state"})
    assert B.invoke(app, actor, out, timeout=7) is retained
    assert B.invoke(app, actor, out, timeout=7, expected_identity=None) is retained
    assert len(calls) == 2 and all(call == (app, out, B.invocation_request(actor), 7) for call in calls)
    assert not out.exists() and not state["calls"]


@pytest.mark.parametrize("bad_destination", ["256", "-1", "+28", "028", "28.0", True])
def test_bad_later_sequence_binding_never_launches_worker_or_creates_evidence(runtime, bad_destination):
    app, _, _, state, out = runtime
    with pytest.raises(ValueError):
        B.invoke(str(app), B.bind_actor(SEQUENCE, {"source": "12", "destination": bad_destination,
                                                 "animate": "true"}), str(out))
    assert state["calls"] == [] and not out.exists()


def test_invalid_bound_later_sequence_step_is_rejected_before_collect(runtime):
    app, _, _, state, out = runtime
    actor = B.bind_actor(SEQUENCE, {"source": "12", "destination": "28", "animate": "true"})
    actor["steps"][1]["arguments"][0]["value"] = 256
    with pytest.raises(ValueError, match="declared type"):
        B.invoke(str(app), actor, str(out))
    assert state["calls"] == [] and not out.exists()


@pytest.mark.parametrize("failure", [None, "before_second_dispatch", "after_second_dispatch"])
def test_sequence_preserves_order_and_partial_execution_without_atomicity_or_retry_claim(runtime, failure):
    app, _, _, state, out = runtime
    attempted = 1 if failure == "before_second_dispatch" else 2
    if failure:
        state["result_changes"].update(status="incomplete", invocations=attempted, native_calls=attempted,
            operations=[{"index": 0, "op": "invoke", "status": "ok"}],
            error={"type": "RuntimeError", "message": "second method failed after first completed"})
        state["runner_changes"]["returncode"] = 1
    actor = B.bind_actor(SEQUENCE, {"source": "12", "destination": "28", "animate": "true"})
    receipt = B.invoke(str(app), actor, str(out))
    assert len(state["calls"]) == 1 and receipt["actuated"] is True
    assert receipt["verified"] is False and receipt["proof_created"] is False
    assert [op["selector"] for op in receipt["request"]["operations"]] == ["beginSelection:", "endSelection:animate:"]
    assert [arg["value"] for op in receipt["request"]["operations"] for arg in op["arguments"]] == [12, 28, True]
    assert receipt["status"] == ("incomplete" if failure else "executed")
    assert receipt["result"]["invocations"] == attempted
    assert len(receipt["result"]["operations"]) == (1 if failure else 2)
    assert receipt["result"]["cleanup"] == {"script_unloaded": True, "detached": True}
    assert R.load_json(out / "runtime.json") == receipt


def test_explicit_gadget_transport_pins_only_public_file_identity_and_redacts_diagnostics(runtime, monkeypatch):
    app, _, _, state, out = runtime
    token, token_file, library, config = _gadget_fixture(runtime, monkeypatch)
    state["stderr"] = "transport diagnostic accidentally echoed " + token
    state["result_changes"]["error"] = {"type": "FixtureDiagnostic", "message": token}
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
    assert result["status"] == "observed" and result["verified"] is False
    transport = result["transport"]
    assert transport == result["adapter"]["transport"] == result["result"]["transport"]
    assert transport["kind"] == "gadget" and transport["endpoint"] == "127.0.0.1:27043"
    for field, file in (("token_file", token_file), ("library", library), ("config", config)):
        assert transport[field]["file"] == str(file) and transport[field]["sha256"] == R._file_hash(file)
    assert transport["token_file"]["mode"] == transport["config"]["mode"] == "0600"
    assert result["result"]["preloaded_gadget_remains"] is True
    assert result["result"]["cleanup"] == {"script_unloaded": True, "detached": True}
    assert token not in json.dumps(result) and token not in json.dumps(state["calls"], default=str)
    for file in out.iterdir():
        if file.is_file():
            assert token.encode() not in file.read_bytes(), file.name
    assert "<redacted-runtime-secret>" in (out / "worker.stderr").read_text()
    assert result["worker"]["sha256"] == R._file_hash(out / "worker-output.json")


@pytest.mark.parametrize("endpoint", ["localhost:27043", "0.0.0.0:27043", "192.168.66.3:27043", "[::1]:27043",
                                      "http://127.0.0.1:27043", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:027043"])
def test_gadget_rejects_nonliteral_loopback_or_invalid_ports_before_starting_worker(runtime, monkeypatch, endpoint):
    app, _, _, state, out = runtime
    _gadget_fixture(runtime, monkeypatch)
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_ENDPOINT", endpoint)
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
    assert result["status"] == "unavailable" and not state["calls"] and result["actuated"] is False


@pytest.mark.parametrize("failure", ["token_symlink", "token_directory", "token_mode", "token_owner", "token_oversized",
                                     "token_empty", "token_short", "config_mode", "unauthenticated", "public_listener", "wrong_token",
                                     "port_fallback", "port_mismatch", "app_secret", "evidence_secret", "transport_unknown"])
def test_gadget_requires_private_bounded_files_and_matching_authenticated_listener(runtime, monkeypatch, failure):
    app, _, _, state, out = runtime
    token, token_file, _, config = _gadget_fixture(runtime, monkeypatch)
    value = json.loads(config.read_text())
    if failure == "token_symlink":
        alias = token_file.with_name("token-link")
        alias.symlink_to(token_file)
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_TOKEN_FILE", str(alias))
    elif failure == "token_directory":
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_TOKEN_FILE", str(out.parent))
    elif failure == "token_mode":
        token_file.chmod(0o644)
    elif failure == "token_owner":
        monkeypatch.setattr(B.os, "getuid", lambda: token_file.stat().st_uid + 1)
    elif failure == "token_oversized":
        token_file.write_bytes(b"a" * (B.MAX_TOKEN_BYTES + 1))
    elif failure == "token_empty":
        token_file.write_bytes(b"")
    elif failure == "token_short":
        token_file.write_text("a")
    elif failure == "config_mode":
        config.chmod(0o644)
    elif failure in {"app_secret", "evidence_secret"}:
        # The evidence directory is created by collect, so a nested source file
        # there would also violate its exclusive private-directory contract.
        nested = app / "Contents/token" if failure == "app_secret" else out / "token"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text(token)
        nested.chmod(0o600)
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_TOKEN_FILE", str(nested))
        if failure == "evidence_secret":
            with pytest.raises(FileExistsError):
                B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
            assert not state["calls"]
            return
    elif failure == "transport_unknown":
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_TRANSPORT", "auto")
    else:
        if failure == "unauthenticated":
            value["interaction"].pop("token")
        elif failure == "public_listener":
            value["interaction"]["address"] = "0.0.0.0"
        elif failure == "wrong_token":
            value["interaction"]["token"] = "another-inert-token"
        elif failure == "port_fallback":
            value["interaction"]["on_port_conflict"] = "pick-next"
        elif failure == "port_mismatch":
            value["interaction"]["port"] = 27044
        config.write_text(json.dumps(value))
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
    assert result["status"] == "unavailable" and not state["calls"] and result["actuated"] is False
    assert token not in json.dumps(result) and token.encode() not in (out / "runtime.json").read_bytes()


@pytest.mark.parametrize("denial", [None, "config", "endpoint", "identity", "fingerprint"])
def test_protected_token_in_request_is_redacted_even_if_later_validation_fails(runtime, monkeypatch, denial):
    app, _, _, state, out = runtime
    token, _, _, config = _gadget_fixture(runtime, monkeypatch)
    if denial == "config":
        config.write_text('{"interaction": ')
    elif denial == "endpoint":
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_ENDPOINT", "localhost:27043")
    elif denial in {"identity", "fingerprint"}:
        def refuse(*args):
            raise B.Unavailable("inert early denial with accidental diagnostic " + token)
        if denial == "identity":
            monkeypatch.setattr(B, "process_identity", refuse)
        else:
            monkeypatch.setattr(R, "fingerprint", refuse)
    result = B.invoke(str(app), B.bind_actor(ACTOR, {"query": token}), str(out))
    assert result["status"] == "unavailable" and not state["calls"] and result["actuated"] is False
    assert token not in json.dumps(result) and token.encode() not in (out / "runtime.json").read_bytes()
    assert "<redacted-runtime-secret>" in json.dumps(result["request"])
    assert not (out / "worker-request.json").exists()


@pytest.mark.parametrize("field", ["endpoint", "token", "library", "config", "backend", "worker_echo"])
def test_gadget_transport_or_secret_changes_after_worker_prevent_success_receipt(runtime, monkeypatch, field):
    app, _, _, state, out = runtime
    _, token_file, library, config = _gadget_fixture(runtime, monkeypatch)
    def mutate():
        if field == "endpoint":
            monkeypatch.setenv("ANYUSE_REA_RUNTIME_GADGET_ENDPOINT", "127.0.0.1:27044")
        elif field == "token":
            token_file.write_text("rotated-inert-fixture-authentication-token")
        elif field == "library":
            library.write_bytes(b"changed installed Gadget")
        elif field == "config":
            config.write_text(config.read_text() + "\n")
        elif field == "backend":
            monkeypatch.setenv("ANYUSE_REA_RUNTIME_TRANSPORT", "local")
    if field == "worker_echo":
        state["result_changes"]["transport"] = {"kind": "local"}
    else:
        state["after_worker"] = mutate
    result = B.invoke(str(app), B.bind_actor(ACTOR, {"query": "q"}), str(out))
    assert result["status"] == "incomplete" and result["verified"] is False and result["resource_review_required"] is True
    assert len(state["calls"]) == 1 and result["error"]


def test_worker_argv_preserves_venv_executable_symlink(runtime, monkeypatch):
    app, _, _, state, out = runtime
    venv = out.parent / "scratch-venv/bin"
    venv.mkdir(parents=True)
    interpreter = venv / "python"
    interpreter.symlink_to(Path(sys.executable).resolve())
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_PYTHON", str(interpreter))
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
    assert result["status"] == "observed"
    assert state["calls"][0]["argv"][0] == str(interpreter)
    assert result["python"] == {"file": str(interpreter), "resolved": str(interpreter.resolve())}
    assert state["calls"][0]["argv"][1] == str(Path(B.__file__).resolve())


@pytest.mark.parametrize("failure", ["timeout", "cleanup", "rpc", "wrong_hash", "missing_rows", "live_handles", "counter_bool", "adapter", "bundle", "pid_reused"])
def test_uncertain_failures_preserve_evidence_and_do_not_claim_verified_or_safely_repeatable(runtime, failure):
    app, identity, _, state, out = runtime
    if failure == "timeout":
        state["runner_changes"]["timed_out"] = True
    elif failure == "cleanup":
        state["result_changes"]["cleanup"] = {"script_unloaded": False, "detached": True}
    elif failure == "rpc":
        state["result_changes"].update(status="incomplete", native_calls=None, invocations=None,
                                        error={"type": "RPCException", "message": "outcome unknown"})
    elif failure == "wrong_hash":
        state["result_changes"]["request_sha256"] = "0" * 64
    elif failure == "missing_rows":
        state["result_changes"]["operations"] = []
    elif failure == "live_handles":
        state["result_changes"]["handles_expired"] = False
    elif failure == "counter_bool":
        state["result_changes"]["invocations"] = True
    elif failure in {"adapter", "bundle"}:
        state["change_" + failure] = True
    else:
        state["after"] = {**identity, "launch_time": identity["launch_time"] + 1}
    result = B.invoke(str(app), B.bind_actor(ACTOR, {"query": "state"}), str(out))
    assert result["status"] == "incomplete" and result["error"]
    assert result["verified"] is False and result["proof_created"] is False
    assert result["resource_review_required"] is True and len(state["calls"]) == 1
    assert (out / "worker.stderr").read_text() == "retained worker diagnostic"
    assert R.load_json(out / "runtime.json") == result
    if failure in {"timeout", "rpc", "wrong_hash", "missing_rows", "live_handles", "counter_bool"}:
        assert result["actuated"] is None


def test_attach_denial_is_retained_without_permission_change_or_retry(runtime):
    app, _, _, state, out = runtime
    state["result_changes"].update(status="unavailable", attached=False, operations=[], invocations=0, native_calls=0,
                                    cleanup={"script_unloaded": False, "detached": False},
                                    error={"type": "PermissionDeniedError", "message": "stock target refused attachment"})
    state["runner_changes"]["returncode"] = 1
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
    assert result["status"] == "unavailable" and result["error"]["type"] == "PermissionDeniedError"
    # A failed attach can have acquired partial instrumentation even when no
    # Session was returned. Closing its manager does not settle that outcome.
    assert result["actuated"] is False and result["resource_review_required"] and len(state["calls"]) == 1


@pytest.mark.parametrize("failure", ["unset", "manifest", "source", "symlink"])
def test_uninstalled_or_unbound_adapter_fails_before_worker(runtime, monkeypatch, failure):
    app, _, adapter, state, out = runtime
    if failure == "unset":
        monkeypatch.delenv("ANYUSE_REA_RUNTIME_AGENT")
    elif failure == "manifest":
        Path(str(adapter) + ".manifest.json").unlink()
    elif failure == "source":
        build = R.load_json(str(adapter) + ".manifest.json")
        build["source_sha256"] = "0" * 64
        R.write_json(str(adapter) + ".manifest.json", build)
    else:
        link = out.parent / "agent-link.js"
        link.symlink_to(adapter)
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_AGENT", str(link))
    result = B.collect(str(app), str(out), {"operations": [{"op": "classes"}]})
    assert result["status"] == "unavailable" and not state["calls"] and result["actuated"] is False


def test_vm_guard_precedes_paths_outputs_configuration_or_native_imports(runtime, monkeypatch):
    _, _, _, state, out = runtime
    def deny():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", deny)
    with pytest.raises(PermissionError, match="VM only"):
        B.collect("missing.app", str(out), {"operations": []})
    with pytest.raises(PermissionError, match="VM only"):
        B.collect("missing.app", str(out), {"operations": []}, expected_identity={"pid": True})
    with pytest.raises(PermissionError, match="VM only"):
        B.invoke("missing.app", {}, str(out), expected_identity={"pid": True})
    with pytest.raises(PermissionError, match="VM only"):
        B.adapter_identity()
    assert not out.exists() and not state["calls"]


def _worker_fixture(runtime, monkeypatch, *, failure=None, transport_kind="local", cleanup_failure=None,
                    request=None, observed_changes=None, remaining_ms=5000, worker_config_path=None):
    app, identity, adapter, _, out = runtime
    token = None
    if transport_kind == "gadget":
        token, token_file, _, gadget_config = _gadget_fixture(runtime, monkeypatch)
    out.mkdir()
    custom_request = request is not None
    request = request if custom_request else B.invocation_request(B.bind_actor(ACTOR, {"query": "q"}))
    pinned_adapter = B.adapter_identity()
    config = {"format": B.WORKER, "bundle": str(app), "target": R.fingerprint(app), "identity": identity,
              "request": request, "adapter": pinned_adapter, "transport": pinned_adapter["transport"],
              "deadline_ms": int(time.time() * 1000) + remaining_ms,
              "trace": str(out / "trace.jsonl")}
    if custom_request:
        config["native_resources"] = B.request_resources(request)
        config["compiler_provenance"] = B.request_compiler_provenance(request)
    R.write_json(out / "config.json", config)
    if failure == "transport_before":
        token_file.write_text("rotated-inert-fixture-authentication-token")
    calls = []
    identity_count = 0
    def identify(bundle, bundle_id):
        nonlocal identity_count
        identity_count += 1
        if failure == "restart_before_attach" or failure == "restart" and identity_count == 2:
            return {**identity, "launch_time": 456}
        return copy.deepcopy(identity)
    monkeypatch.setattr(B, "process_identity", identify)
    class Script:
        def on(self, name, handler):
            calls.append(("message_handler", name))
            self.handler = handler
        def load(self):
            calls.append(("load",))
            if failure == "trace_secret":
                self.handler({"type": "send", "payload": {"event": "inert-diagnostic", "detail": token}}, None)
        def unload(self):
            calls.append(("unload",))
            if failure == "unload" or cleanup_failure in {"unload", "unload_and_detach"}:
                raise RuntimeError("unload uncertain")
        def run(self, payload):
            calls.append(("run", payload))
            if failure == "rpc":
                raise RuntimeError("transport lost after invocation may have entered target")
            if failure == "transport_during":
                gadget_config.write_text(gadget_config.read_text() + "\n")
            observed = {"status": "completed", "operations": [{"index": 0, "op": "invoke", "status": "ok"}],
                    "native_calls": 1, "invocations": 1, "error": None, "handles_expired": True,
                    "queue": "main", "native_state_uncertain": False,
                    "process": {"pid": identity["pid"], "executable": identity["executable"]}}
            if failure == "fault_missing":
                observed.pop("native_state_uncertain")
            elif failure == "fault_nonbool":
                observed["native_state_uncertain"] = 0
            if observed_changes is not None:
                observed.update(copy.deepcopy(observed_changes))
            return observed
        @property
        def exports_sync(self):
            return self
    class Session:
        def create_script(self, source, **kwargs):
            calls.append(("create", source, kwargs))
            return Script()
        def detach(self):
            calls.append(("detach",))
            if cleanup_failure in {"detach", "unload_and_detach"}:
                raise RuntimeError("detach uncertain")
    class Device:
        def enumerate_processes(self):
            calls.append(("enumerate",))
            return [SimpleNamespace(pid=identity["pid"] + (1 if failure == "remote_pid" else 0),
                                    name="wrong-target" if failure == "remote_name" else "Gadget")]
        def attach(self, pid):
            calls.append(("attach", pid))
            if failure == "attach":
                raise PermissionError("existing guest policy refused this target")
            return Session()
    class Manager:
        def get_local_device(self):
            assert transport_kind == "local", "Gadget must never retry local injection"
            calls.append(("local",))
            if failure == "device":
                raise RuntimeError("local device acquisition failed")
            return Device()
        def add_remote_device(self, endpoint, **options):
            calls.append(("remote", endpoint, options))
            if failure == "authentication":
                raise PermissionError("inert server rejected token " + options["token"])
            return Device()
        def close(self):
            calls.append(("manager_close",))
            if cleanup_failure == "manager":
                raise RuntimeError("manager close uncertain")
            if cleanup_failure == "interrupt":
                raise KeyboardInterrupt("inert worker interrupted while closing")
    owned_manager = Manager()
    def local():
        raise AssertionError("worker must use its retained manager for local acquisition")
    def manager():
        calls.append(("manager",))
        if failure == "manager":
            raise RuntimeError("manager acquisition failed")
        return owned_manager
    def close_manager():
        owned_manager.close()
    monkeypatch.setitem(sys.modules, "frida", SimpleNamespace(__version__="inert-fixture", get_local_device=local,
                                                            get_device_manager=manager, shutdown=close_manager))
    result = B._worker(out / "config.json" if worker_config_path is None else worker_config_path)
    return result, calls, config, token


def test_worker_rejects_changed_captured_identity_before_attachment_or_script(runtime, monkeypatch):
    result, calls, config, _ = _worker_fixture(runtime, monkeypatch, failure="restart_before_attach")
    assert result["status"] == "unavailable" and result["attached"] is False
    assert result["identity"] != config["identity"] and result["invocations"] == result["native_calls"] == 0
    assert calls == [] and result["cleanup"] == {"script_unloaded": False, "detached": False}
    assert result["frida_lifecycle"] == {"format": B.FRIDA_LIFECYCLE, "manager_state": "not_acquired",
                                         "attachment_attempted": False, "helper_exit_confirmed": False, "error": None}


@pytest.mark.parametrize("transport_kind", ["local", "gadget"])
@pytest.mark.parametrize("failure", [None, "attach", "rpc", "unload", "restart"])
def test_worker_loads_only_fixed_asset_and_detaches_its_session_on_each_exit(runtime, monkeypatch, failure, transport_kind):
    _, identity, adapter, _, _ = runtime
    result, calls, config, token = _worker_fixture(runtime, monkeypatch, failure=failure, transport_kind=transport_kind)
    assert calls[0] == ("manager",) and calls[-1] == ("manager_close",)
    assert result["frida_lifecycle"] == {"format": B.FRIDA_LIFECYCLE, "manager_state": "closed",
                                         "attachment_attempted": True, "helper_exit_confirmed": False, "error": None}
    if failure == "unload":
        assert [row["phase"] for row in result["cleanup_errors"]] == ["script_unload"]
    else:
        assert result["cleanup_errors"] == []
    activity = [row for row in calls if row[0] not in {"manager", "local", "manager_close"}]
    assert result["transport"] == config["transport"] and result["preloaded_gadget_remains"] is (transport_kind == "gadget")
    if transport_kind == "gadget":
        assert activity[0] == ("remote", "127.0.0.1:27043", {"token": token})
        assert activity[1] == ("enumerate",) and activity[2] == ("attach", identity["pid"])
        assert token not in json.dumps(result) and token not in json.dumps(config)
    else:
        assert calls[1] == ("local",) and activity[0] == ("attach", identity["pid"])
    if failure == "attach":
        assert result["status"] == "unavailable" and result["attached"] is False
        assert result["invocations"] == 0 and len(activity) == (3 if transport_kind == "gadget" else 1)
    else:
        assert activity[-1] == ("detach",) and result["cleanup"]["detached"] is True
        if failure == "restart":
            assert result["status"] == "incomplete" and not any(row[0] == "create" for row in calls)
        else:
            create = next(row for row in calls if row[0] == "create")
            assert create == ("create", adapter.read_text(), {"name": "anyuse-fixed-rea-runtime", "runtime": "qjs"})
            assert any(row[0] == "unload" for row in calls)
            if failure is None:
                assert result["status"] == "completed" and result["cleanup"] == {"script_unloaded": True, "detached": True}
            elif failure == "rpc":
                assert result["status"] == "incomplete" and result["native_calls"] is None and result["invocations"] is None
            else:
                assert result["status"] == "incomplete" and result["cleanup"]["script_unloaded"] is False


@pytest.mark.parametrize("failure", ["remote_pid", "remote_name", "authentication", "transport_before"])
def test_gadget_worker_refuses_unauthenticated_or_changed_target_before_fixed_script(runtime, monkeypatch, failure):
    result, calls, config, token = _worker_fixture(runtime, monkeypatch, failure=failure, transport_kind="gadget")
    assert result["status"] == "unavailable" and result["attached"] is False
    assert result["invocations"] == result["native_calls"] == 0
    assert not any(row[0] in {"attach", "create", "load", "run"} for row in calls)
    assert token not in json.dumps(result) and token not in json.dumps(config)
    if failure == "transport_before":
        assert calls == []
        assert result["frida_lifecycle"]["manager_state"] == "not_acquired"
    elif failure == "authentication":
        assert [row[0] for row in calls] == ["manager", "remote", "manager_close"]
        assert "<redacted-runtime-secret>" in result["error"]["message"]
    else:
        assert [row[0] for row in calls] == ["manager", "remote", "enumerate", "manager_close"]
    if failure != "transport_before":
        assert result["frida_lifecycle"]["manager_state"] == "closed"
        assert result["frida_lifecycle"]["attachment_attempted"] is False


@pytest.mark.parametrize("failure", ["fault_missing", "fault_nonbool"])
def test_worker_unknown_fault_disposition_keeps_outcome_unknown_despite_clean_detach(runtime, monkeypatch, failure):
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch, failure=failure)
    assert result["status"] == "incomplete" and result["native_calls"] is None and result["invocations"] is None
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}
    assert result["error"] and calls[-2:] == [("detach",), ("manager_close",)]
    assert result["frida_lifecycle"]["manager_state"] == "closed"


def test_gadget_worker_rechecks_pinned_config_after_native_execution(runtime, monkeypatch):
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch, failure="transport_during", transport_kind="gadget")
    assert result["status"] == "incomplete" and result["attached"] is True and result["invocations"] == 1
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}
    assert result["preloaded_gadget_remains"] is True and calls[-2:] == [("detach",), ("manager_close",)]
    assert "transport changed" in result["error"]["message"]


def test_gadget_worker_redacts_trace_payloads_before_writing_evidence(runtime, monkeypatch):
    result, _, config, token = _worker_fixture(runtime, monkeypatch, failure="trace_secret", transport_kind="gadget")
    assert result["status"] == "completed"
    trace = Path(config["trace"]).read_text()
    assert token not in trace and "<redacted-runtime-secret>" in trace
    assert token not in (Path(config["trace"]).parent / "worker-cleanup.json").read_text()


@pytest.mark.parametrize("transport_kind", ["local", "gadget"])
@pytest.mark.parametrize("cleanup_failure,phases", [
    (None, []), ("unload", ["script_unload"]), ("detach", ["session_detach"]),
    ("unload_and_detach", ["script_unload", "session_detach"]), ("manager", ["manager_close"]),
])
def test_worker_closes_its_manager_after_each_applicable_phase_and_preserves_primary_rpc_failure(
        runtime, monkeypatch, transport_kind, cleanup_failure, phases):
    result, calls, config, _ = _worker_fixture(runtime, monkeypatch, failure="rpc",
                                              transport_kind=transport_kind, cleanup_failure=cleanup_failure)
    assert calls[-3:] == [("unload",), ("detach",), ("manager_close",)]
    assert sum(row[0] == "manager" for row in calls) == 1
    assert sum(row[0] == "attach" for row in calls) == 1
    assert sum(row[0] == "run" for row in calls) == 1
    assert result["status"] == "incomplete"
    assert result["error"]["message"] == "transport lost after invocation may have entered target"
    assert result["native_calls"] is None and result["invocations"] is None
    assert [row["phase"] for row in result["cleanup_errors"]] == phases
    assert result["cleanup"]["script_unloaded"] is (cleanup_failure not in {"unload", "unload_and_detach"})
    assert result["cleanup"]["detached"] is (cleanup_failure not in {"detach", "unload_and_detach"})
    lifecycle = B._frida_lifecycle(result)
    assert lifecycle["manager_state"] == ("failed" if cleanup_failure == "manager" else "closed")
    assert lifecycle["helper_exit_confirmed"] is False
    journal = R.load_json(Path(config["trace"]).parent / "worker-cleanup.json")
    assert journal["format"] == B.CLEANUP_JOURNAL and journal["request_sha256"] == result["request_sha256"]
    assert journal["frida_lifecycle"] == lifecycle and journal["cleanup_errors"] == result["cleanup_errors"]
    assert journal["native_calls"] is None and journal["invocations"] is None
    assert journal["error"] == result["error"]


@pytest.mark.parametrize("transport_kind", ["local", "gadget"])
@pytest.mark.parametrize("cleanup_failure", [None, "manager"])
def test_worker_attach_failure_without_a_session_still_closes_its_own_manager_once(
        runtime, monkeypatch, transport_kind, cleanup_failure):
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch, failure="attach",
                                         transport_kind=transport_kind, cleanup_failure=cleanup_failure)
    assert result["attached"] is False and result["operations"] == []
    assert not any(row[0] in {"create", "load", "run", "unload", "detach"} for row in calls)
    assert calls[-1] == ("manager_close",) and sum(row[0] == "attach" for row in calls) == 1
    assert result["cleanup"] == {"script_unloaded": False, "detached": False}
    assert result["error"]["message"] == "existing guest policy refused this target"
    lifecycle = B._frida_lifecycle(result)
    assert lifecycle["attachment_attempted"] is True and lifecycle["helper_exit_confirmed"] is False
    assert lifecycle["manager_state"] == ("closed" if cleanup_failure is None else "failed")
    assert result["status"] == ("unavailable" if cleanup_failure is None else "incomplete")


@pytest.mark.parametrize("failure", ["manager", "device"])
def test_worker_manager_or_local_device_acquisition_failure_gets_cooperative_close_without_attach(
        runtime, monkeypatch, failure):
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch, failure=failure)
    assert calls[-1] == ("manager_close",) and sum(row[0] == "manager_close" for row in calls) == 1
    assert not any(row[0] in {"attach", "create", "run", "unload", "detach"} for row in calls)
    assert result["attached"] is False and result["status"] == "unavailable"
    assert B._frida_lifecycle(result)["manager_state"] == "closed"
    assert result["frida_lifecycle"]["attachment_attempted"] is False
    assert result["error"]["message"] == ("manager acquisition failed" if failure == "manager" else "local device acquisition failed")


def test_worker_manager_close_failure_after_success_does_not_claim_transaction_completion(runtime, monkeypatch):
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch, cleanup_failure="manager")
    assert result["status"] == "incomplete" and result["attached"] is True
    assert result["native_calls"] == result["invocations"] == 1
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}
    assert calls[-3:] == [("unload",), ("detach",), ("manager_close",)]
    assert result["error"]["message"] == "manager close uncertain"
    lifecycle = B._frida_lifecycle(result)
    assert lifecycle["manager_state"] == "failed" and lifecycle["helper_exit_confirmed"] is False
    assert lifecycle["error"] == {"type": "RuntimeError", "message": "manager close uncertain"}


def test_worker_interrupted_during_manager_close_leaves_pending_atomic_journal_without_exit_claim(runtime, monkeypatch):
    # An inert exception models interruption at the API boundary. This does not
    # measure a real timeout, cancellation latency or an OS helper's lifetime.
    with pytest.raises(KeyboardInterrupt, match="inert worker interrupted"):
        _worker_fixture(runtime, monkeypatch, cleanup_failure="interrupt")
    journal = R.load_json(runtime[-1] / "worker-cleanup.json")
    assert journal["format"] == B.CLEANUP_JOURNAL and journal["attached"] is True
    assert journal["cleanup"] == {"script_unloaded": True, "detached": True}
    assert journal["frida_lifecycle"] == {"format": B.FRIDA_LIFECYCLE, "manager_state": "pending",
                                          "attachment_attempted": True, "helper_exit_confirmed": False, "error": None}
    assert journal["completed_operations"] == 1 and journal["native_calls"] == journal["invocations"] == 1


def test_worker_cleanup_journal_io_failure_cannot_skip_unload_detach_or_manager_close(runtime, monkeypatch):
    write_json = R.write_json
    writes = 0
    def unreliable_journal(path, value):
        nonlocal writes
        if Path(path).name == "worker-cleanup.json":
            writes += 1
            if writes >= 3:  # Manager and pre-attach checkpoints already exist.
                raise OSError("inert cleanup journal storage failure")
        return write_json(path, value)
    monkeypatch.setattr(R, "write_json", unreliable_journal)
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch)
    assert calls[-3:] == [("unload",), ("detach",), ("manager_close",)]
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}
    assert result["status"] == "incomplete" and result["error"]["message"] == "inert cleanup journal storage failure"
    assert [row["phase"] for row in result["cleanup_errors"]] == ["journal"]
    assert B._frida_lifecycle(result)["manager_state"] == "closed"


@pytest.mark.parametrize("manager_state", ["pending", "failed"])
def test_collect_retains_resource_review_when_native_result_is_known_but_manager_close_is_not(runtime, manager_state):
    app, _, _, state, out = runtime
    detail = {"type": "RuntimeError", "message": "manager close not confirmed"}
    state["result_changes"].update(
        status="incomplete", error=detail,
        frida_lifecycle={"format": B.FRIDA_LIFECYCLE, "manager_state": manager_state,
                         "attachment_attempted": True, "helper_exit_confirmed": False,
                         "error": detail if manager_state == "failed" else None},
        cleanup_errors=[{"phase": "manager_close", "error": detail}] if manager_state == "failed" else [])
    state["runner_changes"]["returncode"] = 1
    result = B.invoke(app, B.bind_actor(ACTOR, {"query": "q"}), out)
    assert result["status"] == "incomplete" and result["resource_review_required"] is True
    assert result["actuated"] is True and result["native_calls"] is True
    assert result["error"] == detail and result["result"]["cleanup"] == {"script_unloaded": True, "detached": True}
    assert len(state["calls"]) == 1 and result["verified"] is False


def test_collect_hard_timeout_keeps_pending_journal_as_evidence_without_promoting_finished_native_rows(runtime):
    app, _, _, state, out = runtime
    def retained_pending_journal():
        R.write_json(out / "worker-cleanup.json", {"format": B.CLEANUP_JOURNAL,
            "frida_lifecycle": {"format": B.FRIDA_LIFECYCLE, "manager_state": "pending",
                                 "attachment_attempted": True, "helper_exit_confirmed": False, "error": None}})
    state["after_worker"] = retained_pending_journal
    state["runner_changes"]["timed_out"] = True
    result = B.invoke(app, B.bind_actor(ACTOR, {"query": "q"}), out)
    assert result["status"] == "incomplete" and result["result"] is None
    assert result["resource_review_required"] is True and result["actuated"] is None and result["native_calls"] is None
    artifact = next(row for row in result["artifacts"] if row["file"] == "worker-cleanup.json")
    assert artifact["sha256"] == R._file_hash(out / "worker-cleanup.json") and artifact["oversized"] is False
    assert len(state["calls"]) == 1


@pytest.mark.parametrize("change", ["missing", "helper_exit_claim", "unclosed_completion"])
def test_collect_requires_current_lifecycle_disposition_even_with_confirmed_script_and_session_cleanup(runtime, change):
    app, _, _, state, out = runtime
    lifecycle = {"format": B.FRIDA_LIFECYCLE, "manager_state": "closed",
                 "attachment_attempted": True, "helper_exit_confirmed": False, "error": None}
    if change == "missing": lifecycle = None
    elif change == "helper_exit_claim": lifecycle["helper_exit_confirmed"] = True
    else: lifecycle["manager_state"] = "pending"
    state["result_changes"]["frida_lifecycle"] = lifecycle
    result = B.collect(app, out, {"operations": [{"op": "classes"}]})
    assert result["status"] == "incomplete" and result["resource_review_required"] is True
    assert result["actuated"] is None and result["native_calls"] is None
    assert "Frida lifecycle" in result["error"]["message"] and len(state["calls"]) == 1


@pytest.mark.parametrize("change", ["lifecycle_missing", "cleanup_errors_missing", "mixed_legacy_fields"])
def test_versioned_worker_envelope_cannot_silently_drop_or_relabel_manager_disposition(runtime, monkeypatch, change):
    result, _, config, _ = _worker_fixture(runtime, monkeypatch)
    assert result["format"] == B.WORKER == "anyuse.rea-runtime-worker/2"
    if change == "lifecycle_missing": result.pop("frida_lifecycle")
    elif change == "cleanup_errors_missing": result.pop("cleanup_errors")
    else: result["format"] = B.LEGACY_WORKER
    with pytest.raises(ValueError):
        B._worker_result(result, R.digest(config["request"]), config["request"], config["transport"])


def test_historical_worker_validation_does_not_add_manager_claim_and_new_collect_refuses_old_envelope(runtime):
    app, _, _, state, out = runtime
    def legacy_output():
        output = out / "worker-output.json"
        result = R.load_json(output)
        result["format"] = B.LEGACY_WORKER
        result.pop("frida_lifecycle")
        result.pop("cleanup_errors")
        historical = copy.deepcopy(result)
        request = state["calls"][0]["config"]["request"]
        assert B._worker_result(result, R.digest(request), request, {"kind": "local"}) == historical
        assert "frida_lifecycle" not in result and "cleanup_errors" not in result
        R.write_json(output, result)
    state["after_worker"] = legacy_output
    result = B.collect(app, out, {"operations": [{"op": "classes"}]})
    assert result["format"] == B.RUNTIME == "anyuse.rea-runtime/2"
    assert result["status"] == "incomplete" and result["resource_review_required"] is True
    assert result["actuated"] is None and "current worker lifecycle envelope" in result["error"]["message"]
    assert len(state["calls"]) == 1


def _inert_runtime_js_source():
    source = Path(B.__file__).with_name("rea_runtime_agent.js").read_text()
    assert source.count("import ObjC from 'frida-objc-bridge';") == 1
    assert "import Swift from 'frida-swift-bridge';" not in source
    source = source.replace("import ObjC from 'frida-objc-bridge';", "", 1)
    source = source.replace("await import('frida-swift-bridge')", "({default: globalThis.Swift})")
    source = source.replace("await import('frida-swift-bridge/dist/abi/metadata.js')",
                            "({TargetStructMetadata: globalThis.TargetStructMetadata})")
    return source


def test_fixed_js_discovers_runtime_generated_classes_and_preserves_unknown_provenance():
    """Execute fixed JS with inert native objects, not a Frida/native process."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("static Frida adapter fixture requires the guest's existing Node")
    source = _inert_runtime_js_source()
    fixture = r"""
const fs = require('fs');
const vm = require('vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = '/fixture/Canvas.app';
const main = root + '/Contents/MacOS/Canvas';
const owners = {AppModel: main, DynamicModel: null, SystemModel: '/System/Library/Frameworks/AppKit'};
function image(value) { return {isNull: () => value === null, readUtf8String: () => {
  if (value === null) throw Error('nil image pointer must not be read');
  return value;
}}; }
const classes = {};
for (const name of Object.keys(owners)) {
  const method = function () { throw Error('metadata discovery must not invoke an app method'); };
  method.types = '@16@0:8';
  method.implementation = {owner: owners[name], sub: () => ({toString: () => '0x10'})};
  classes[name] = {$kind: 'class', $className: name, handle: {name}, $ownMethods: ['- content'],
    $methods: ['- content'], '- content': method};
}
const ObjC = {available: true, classes, mainQueue: 'main', api: {
  class_getImageName: handle => image(owners[handle.name]), objc_msgSend: 'objc_msgSend'
}, schedule: (_queue, work) => work(), enumerateLoadedClassesSync: options => {
  const result = {};
  for (const name of Object.keys(owners)) {
    const owner = owners[name];
    if (owner === null || !options.ownedBy.accept({path: owner})) continue;
    (result[owner] ||= []).push(name);
  }
  return result;
}, selector: name => name};
let rawStringReads = 0;
function NativeFunction(address, returnType, argumentTypes) {
  if (address !== 'objc_msgSend' || returnType !== 'pointer' || argumentTypes.join(',') !== 'pointer,pointer')
    throw Error('string conversion must use the one fixed Objective-C ABI');
  return (handle, selector) => {
    if (selector !== 'UTF8String') throw Error('unexpected fixed conversion selector');
    return {isNull: () => false, readUtf8String: bytes => {
      if (bytes !== Buffer.byteLength(handle.text, 'utf8')) throw Error('string bytes must be read exactly');
      rawStringReads++;
      return handle.text;
    }};
  };
}
const context = vm.createContext({ObjC, rpc: {exports: {}}, send: () => {},
  Process: {id: 321, platform: 'darwin', arch: 'arm64', pointerSize: 8, mainModule: {path: main},
    findModuleByAddress: pointer => pointer.owner === null ? null : {path: pointer.owner, base: 1}},
  ModuleMap: class {constructor(accept) {this.accept = accept;}},
  Date, Map, Set, Promise, Error, NativeFunction, uint64: value => value});
vm.runInContext(data.source, context, {timeout: 1000});
async function run(operation) {
  return await context.rpc.exports.run({request: {operations: [operation]},
    deadline_ms: Date.now() + 3000, pid: 321, bundle: root, executable: main});
}
(async () => {
  const dynamic = await run({op: 'classes', owner: 'all', query: 'dynamic'});
  const app = await run({op: 'classes', owner: 'app', query: 'dynamic'});
  const bounded = await run({op: 'classes', owner: 'all', query: '', limit: 1});
  const methods = await run({op: 'methods', class: 'DynamicModel'});
  function method(signature, implementation) {
    implementation.types = signature;
    implementation.implementation = {owner: '/System/Library/Frameworks/Foundation', sub: () => ({toString: () => '0x20'})};
    return implementation;
  }
  const text = 'A\0B😀é';
  const stringValue = {$kind: 'instance', $className: 'FakeNSString',
    $class: {$kind: 'class', $className: 'NSString', $superClass: null},
    handle: {text, isNull: () => false}, $methods: ['- length', '- lengthOfBytesUsingEncoding:', '- UTF8String'],
    '- length': method('Q16@0:8', () => text.length),
    '- lengthOfBytesUsingEncoding:': method('Q24@0:8Q16', encoding => {
      if (encoding !== '4') throw Error('UTF-8 encoding must be explicit');
      return Buffer.byteLength(text, 'utf8');
    }),
    '- UTF8String': method('r*16@0:8', () => {throw Error('Frida C-string wrapper loses NUL and must not be called');})};
  classes.StringModel = {$kind: 'class', $className: 'StringModel', $methods: ['+ content', '+ broken'],
    '+ content': method('@16@0:8', () => stringValue),
    '+ broken': method('@16@0:8', () => {throw Error('diagnostic failure');})};
  const string = await run({op: 'read', receiver: {class: 'StringModel', kind: 'class'},
    fields: [{name: 'content', kind: 'getter', selector: 'content', type: 'utf8'}]});
  const diagnostic = await run({op: 'read', receiver: {class: 'StringModel', kind: 'class'},
    fields: [{name: 'content', kind: 'getter', selector: 'broken', type: 'utf8'}]});
  const classMismatch = await run({op: 'read', receiver: {class: 'StringModel', kind: 'class'}, expected_class: 'OtherModel',
    fields: [{name: 'content', kind: 'getter', selector: 'broken', type: 'utf8'}]});
  const classMatched = await run({op: 'read', receiver: {class: 'StringModel', kind: 'class'}, expected_class: 'StringModel',
    fields: [{name: 'content', kind: 'getter', selector: 'content', type: 'utf8'}]});
  console.log(JSON.stringify({dynamic, app, bounded, methods, string, diagnostic, classMismatch, classMatched, rawStringReads}));
})().catch(error => {console.error(error); process.exit(1);});
"""
    run = subprocess.run([node, "-e", fixture], input=json.dumps({"source": source}), capture_output=True,
                         text=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["classMismatch"]["status"] == "incomplete" and result["classMismatch"]["native_calls"] == 0
    assert "receiver class differs" in result["classMismatch"]["error"]["message"]
    assert result["classMatched"]["status"] == "completed"
    assert result["classMatched"]["operations"][0]["fields"][0]["value"] == "A\0B😀é"
    for name in ("dynamic", "app", "bounded", "methods", "string"):
        receipt = result[name]
        assert receipt["status"] == "completed" and receipt["invocations"] == 0 and receipt["handles_expired"] is True
    assert result["dynamic"]["operations"][0]["rows"] == [{"name": "DynamicModel", "module": None, "origin": "runtime_generated"}]
    assert result["app"]["operations"][0]["rows"] == []
    assert result["bounded"]["operations"][0]["matched"] == 3
    assert result["bounded"]["operations"][0]["truncated"] is True
    method = result["methods"]["operations"][0]["rows"][0]
    assert method["class"] == "DynamicModel" and method["signature"] == "@16@0:8"
    assert method["module"] is None and method["implementation_offset"] is None and method["origin"] == "runtime_generated"
    assert result["string"]["operations"][0]["fields"][0]["value"] == "A\0B😀é"
    assert result["rawStringReads"] == 2
    assert result["diagnostic"]["status"] == "incomplete" and result["diagnostic"]["invocations"] == 0
    assert result["diagnostic"]["error"]["type"] == "Error" and len(result["diagnostic"]["error"]["stack"]) <= 8192
    assert result["diagnostic"]["native_state_uncertain"] is True


NATIVE = {"kind": "native_function", "target": {"module": {"path": "/guest/Native.dylib", "sha256": "a" * 64},
                                                        "symbol": "_ZN8Internal5applyEi"},
          "abi": "default", "arguments": [{"type": "int32", "value": {"input": "amount"}}],
          "return_type": "int32", "signature_evidence": {"kind": "compiler", "file": "/guest/contracts.json",
                                                         "sha256": "b" * 64, "pointer": "/call"}}


def _inert_macho(uuid="0123456789abcdef0123456789abcdef", cpu=0x0100000C, subtype=0):
    # This is an inert header, not a compiled fixture or executable analysis.
    uuid_command = struct.pack("<II", 0x1B, 24) + bytes.fromhex(uuid)
    segment = struct.pack("<II16sQQQQIIII", 0x19, 152, b"__TEXT", 0, 0x2000, 0, 0x1020, 5, 5, 1, 0)
    section = struct.pack("<16s16sQQIIIIIIII", b"__text", b"__TEXT", 0x1000, 0x20, 0x1000, 2, 0, 0, 0x80000400, 0, 0, 0)
    header = struct.pack("<8I", 0xFEEDFACF, cpu, subtype, 6, 2, len(uuid_command + segment + section), 0, 0)
    return (header + uuid_command + segment + section).ljust(0x1020, b"\0")


@pytest.fixture
def native_files(runtime):
    app, identity, adapter, state, out = runtime
    module = app.parent / "Native.dylib"
    module.write_bytes(_inert_macho())
    actor = B.bind_actor(NATIVE, {"amount": "7"})
    getter = copy.deepcopy(actor)
    getter["target"]["symbol"] = "borrowed_root"
    getter["arguments"] = []
    getter["return_type"] = "pointer"
    getter["signature_evidence"]["pointer"] = "/getter"
    contract_file = app.parent / "contracts.json"
    R.write_json(contract_file, {"call": B._contract(actor), "getter": B._contract(getter)})
    for call in (actor, getter):
        call["target"]["module"] = {"path": str(module), "sha256": R._file_hash(module)}
        call["signature_evidence"].update(file=str(contract_file), sha256=R._file_hash(contract_file))
    return actor, getter, module, contract_file


def test_native_nested_struct_buffer_and_getter_bindings_preserve_all_route_metadata():
    actor = copy.deepcopy(NATIVE)
    getter = copy.deepcopy(NATIVE)
    getter["target"]["symbol"] = "borrowed_root"
    getter["return_type"] = "pointer"
    getter["arguments"] = [{"type": "uint8", "value": {"input": "identity"}}]
    actor["arguments"] = [
        {"type": "pointer", "value": {"getter": getter}},
        {"type": {"kind": "struct", "fields": [{"name": "input", "type": "int32"},
            {"name": "nested", "type": {"kind": "struct", "fields": [{"name": "ratio", "type": "double"}]}}]},
         "value": {"input": {"input": "amount"}, "nested": {"ratio": {"input": "ratio"}}}},
        {"type": {"kind": "buffer", "bytes": 3, "direction": "inout"}, "value": {"input": "bytes"}}]
    saved = copy.deepcopy(actor)
    bound = B.bind_actor(actor, {"identity": "3", "amount": "-12", "ratio": "0.5", "bytes": "007fff"})
    assert bound["arguments"][0]["value"]["getter"]["arguments"][0]["value"] == 3
    assert bound["arguments"][1]["value"] == {"input": -12, "nested": {"ratio": .5}}
    assert bound["arguments"][2]["value"] == [0, 127, 255]
    assert actor == saved and bound["signature_evidence"] == saved["signature_evidence"]
    leaves = B.argument_leaves(actor["arguments"][0])
    assert leaves == [{"path": ["value", "getter", "arguments", 0, "value"], "type": "uint8", "value": {"input": "identity"}}]
    leaf = B.argument_leaves(actor["arguments"][1])[1]
    updated = B.replace_argument_leaf(actor["arguments"][1], leaf["path"], .75)
    assert updated["value"]["nested"]["ratio"] == .75 and actor["arguments"][1] == saved["arguments"][1]
    with pytest.raises(ValueError, match="typed leaf"):
        B.replace_argument_leaf(actor["arguments"][0], ["value", "getter", "target", "symbol"], "anything")


def test_one_field_pod_named_input_is_data_rather_than_a_whole_struct_binding():
    actor = copy.deepcopy(NATIVE)
    actor["arguments"] = [{"type": {"kind": "struct", "fields": [{"name": "input", "type": "int32"}]}, "value": {"input": 9}}]
    assert B.validate_actor(actor)["arguments"][0]["value"] == {"input": 9}
    assert B.argument_leaves(actor["arguments"][0]) == [{"path": ["value", "input"], "type": "int32", "value": 9}]


@pytest.mark.parametrize("kind,source", [("float", "nan"), ("double", "inf"), ("double", "1e9999"),
    ("float", "3.5e38"), ("double", " 1.0"), ("double", ".5"), ("double", "1e01")])
def test_native_float_bindings_are_explicit_finite_bounded_decimal(kind, source):
    with pytest.raises(ValueError):
        B.convert_binding(kind, source)
    assert B.convert_binding("double", "-1.5e2") == -150


@pytest.mark.parametrize("source", ["ABCDEF", "000", "00000000", "00 7fff", "0x007fff"])
def test_native_buffer_binding_never_infers_encoding_or_truncates(source):
    with pytest.raises(ValueError):
        B.convert_binding({"kind": "buffer", "bytes": 3, "direction": "in"}, source)


@pytest.mark.parametrize("change", ["address", "alias", "both_locations", "offset", "missing_contract", "owned",
                                     "packed", "void_field", "nested_void", "nested_buffer", "nonbyte", "out_value",
                                     "swift_argument_wrong_abi", "swift_float", "swift_bool", "swift_context_default"])
def test_native_schema_refuses_guessed_abi_layout_ownership_or_persisted_addresses(change):
    actor = B.bind_actor(NATIVE, {"amount": "1"})
    if change == "address":
        actor["arguments"] = [{"type": "pointer", "value": "0x1234"}]
    elif change == "alias":
        actor["arguments"] = [{"type": "pointer", "value": {"alias": "past"}}]
    elif change == "both_locations":
        actor["target"]["offset"] = "0x100"
    elif change == "offset":
        actor["target"].pop("symbol"); actor["target"]["offset"] = "0x010"
    elif change == "missing_contract":
        actor.pop("signature_evidence")
    elif change == "owned":
        actor["ownership"] = "owned"
    elif change == "packed":
        actor["return_type"] = {"kind": "struct", "fields": [{"name": "x", "type": "int32"}], "packed": True}
    elif change in {"void_field", "nested_void"}:
        actor["return_type"] = {"kind": "struct", "fields": [{"name": "x", "type": "void"}]}
        if change == "nested_void":
            actor["return_type"] = {"kind": "struct", "fields": [{"name": "parent", "type": actor["return_type"]}]}
    elif change == "nested_buffer":
        actor["arguments"] = [{"type": {"kind": "struct", "fields": [{"name": "x", "type": {"kind": "buffer", "bytes": 2, "direction": "out"}}]},
                               "value": {"x": None}}]
    elif change in {"nonbyte", "out_value"}:
        actor["arguments"] = [{"type": {"kind": "buffer", "bytes": 2, "direction": "in" if change == "nonbyte" else "out"},
                               "value": [0, 256] if change == "nonbyte" else [0, 0]}]
    elif change == "swift_argument_wrong_abi":
        actor["arguments"] = [{"type": {"kind": "swift_type", "module": "Native", "name": "Pair", "category": "struct", "bytes": 16},
                               "value": {"getter": {**actor, "abi": "swift", "return_type": {"kind": "swift_type", "module": "Native", "name": "Pair", "category": "struct", "bytes": 16}}}}]
    elif change in {"swift_float", "swift_bool"}:
        actor["abi"] = "swift"; actor["arguments"][0] = {"type": "double" if change == "swift_float" else "bool", "value": .5 if change == "swift_float" else True}
    else:
        getter = {**actor, "arguments": [], "return_type": "pointer"}
        actor["context"] = {"type": "pointer", "value": {"getter": getter}}
    with pytest.raises(ValueError):
        B.validate_actor(actor)


def test_mixed_runtime_sequence_and_pointer_aliases_are_ordered_but_not_persisted():
    native = B.bind_actor(NATIVE, {"amount": "12"})
    objc = B.bind_actor(ACTOR, {"query": "q"})
    actor = {"kind": "runtime_sequence", "steps": [objc, native]}
    assert [call["kind"] for call in B.actor_calls(actor)] == ["objc_method", "native_function"]
    request = B.invocation_request(actor)
    assert [op["op"] for op in request["operations"]] == ["invoke", "native_invoke"]
    getter = {**native, "arguments": [], "return_type": "pointer"}
    operations = B.invocation_request(getter)["operations"]
    operations[0]["alias"] = "borrowed"
    operations.append({**B.invocation_request(native)["operations"][0], "arguments": [{"type": "pointer", "value": {"alias": "borrowed"}}]})
    assert B.validate_request({"operations": operations})["operations"][1]["arguments"][0]["value"] == {"alias": "borrowed"}
    with pytest.raises(ValueError, match="expired"):
        B.validate_request({"operations": list(reversed(operations))})
    with pytest.raises(ValueError, match="actor_methods"):
        B.actor_methods(actor)


def test_native_module_and_exact_prototype_contract_are_pinned_before_worker(native_files, runtime):
    actor, getter, module, contract = native_files
    actor["arguments"].append({"type": "pointer", "value": {"getter": getter}})
    body = R.load_json(contract); body["call"] = B._contract(actor); R.write_json(contract, body)
    sha = R._file_hash(contract)
    actor["signature_evidence"]["sha256"] = getter["signature_evidence"]["sha256"] = sha
    resources = B.native_resources(actor)
    assert set(resources) == {str(module), str(contract)}
    assert resources[str(module)]["macho_images"] == [{"cpu_type": 0x0100000C, "cpu_subtype": 0,
                                                        "uuid": "0123456789abcdef0123456789abcdef"}]
    assert resources[str(module)]["macho_sections"] == [{"image_identity": resources[str(module)]["macho_images"][0],
        "sections": [{"segment": "__TEXT", "name": "__text", "offset": "0x1000", "bytes": 0x20, "flags": 0x80000400}]}]
    assert resources[str(module)]["macho_segments"] == [{"image_identity": resources[str(module)]["macho_images"][0],
        "segments": [{"segment": "__TEXT", "offset": "0x0", "bytes": 0x2000, "file_offset": 0, "file_bytes": 0x1020,
                      "max_protection": 5, "initial_protection": 5}]}]
    assert B.request_resources(B.invocation_request(actor)) == resources
    app, _, _, state, out = runtime
    receipt = B.invoke(app, actor, out)
    assert receipt["status"] == "executed" and receipt["native_resources"] == resources
    assert state["calls"][0]["config"]["native_resources"] == resources
    assert receipt["result"]["native_resources"] == resources and receipt["verified"] is False


@pytest.mark.parametrize("change", ["abi", "return_type", "pointer_policy", "hash", "module", "uuid_missing"])
def test_native_contract_or_image_mismatch_denies_before_attachment(native_files, runtime, change):
    actor, _, module, contract = native_files
    app, _, _, state, out = runtime
    if change in {"abi", "return_type", "pointer_policy"}:
        body = R.load_json(contract)
        body["call"][change] = "swift" if change == "abi" else "void" if change == "return_type" else "unknown"
        R.write_json(contract, body); actor["signature_evidence"]["sha256"] = R._file_hash(contract)
    elif change == "hash":
        actor["signature_evidence"]["sha256"] = "0" * 64
    elif change == "module":
        module.write_bytes(module.read_bytes() + b"changed")
    else:
        module.write_bytes(struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 6, 1, 8, 0, 0) + struct.pack("<II", 2, 8))
        actor["target"]["module"]["sha256"] = R._file_hash(module)
    receipt = B.invoke(app, actor, out)
    assert receipt["status"] != "executed" and receipt["actuated"] is False and state["calls"] == []


def test_native_module_and_contract_drift_after_execution_preserves_uncertainty(native_files, runtime):
    actor, _, module, _ = native_files
    app, _, _, state, out = runtime
    state["after_worker"] = lambda: module.write_bytes(module.read_bytes() + b"changed after dispatch")
    receipt = B.invoke(app, actor, out)
    assert receipt["status"] == "incomplete" and receipt["actuated"] is True
    assert receipt["resource_review_required"] is True and receipt["result"]["cleanup"]["detached"] is True


def test_known_native_exception_with_confirmed_detach_still_requires_resource_review(native_files, runtime):
    actor, _, _, _ = native_files
    app, _, _, state, out = runtime
    state["result_changes"] = {"status": "incomplete", "operations": [], "native_state_uncertain": True,
                                "error": {"type": "Error", "message": "native fault after entry"}}
    receipt = B.invoke(app, actor, out)
    assert receipt["actuated"] is True and receipt["result"]["cleanup"] == {"script_unloaded": True, "detached": True}
    assert receipt["resource_review_required"] is True and receipt["status"] == "incomplete"


def test_native_graph_pins_explicit_layout_and_bounds_passive_reads(native_files, runtime):
    _, getter, module, contract = native_files
    fields = [{"name": "value", "offset": 0, "type": "int32"}, {"name": "child", "offset": 8, "type": "pointer", "target": "Node"}]
    body = R.load_json(contract)
    body["layout"] = {"bytes": 16, "fields": fields, "pointer_policy": "borrowed_no_escape"}
    R.write_json(contract, body)
    evidence = {**getter["signature_evidence"], "sha256": R._file_hash(contract), "pointer": "/layout"}
    getter["signature_evidence"]["sha256"] = evidence["sha256"]
    graph = {"op": "graph", "root": {"native": getter}, "root_type": "Node",
             "types": [{"name": "Node", "bytes": 16, "fields": fields, "layout_evidence": evidence}],
             "max_depth": 3, "max_nodes": 8, "methods_limit": 0}
    request = {"operations": [graph]}
    assert B.validate_request(request) == request
    assert set(B.request_resources(request)) == {str(module), str(contract)}
    bad = copy.deepcopy(request); bad["operations"][0]["types"][0]["fields"][0]["offset"] = 15
    with pytest.raises(ValueError, match="fit"):
        B.validate_request(bad)
    bad = copy.deepcopy(request); bad["operations"][0]["types"][0]["fields"][0]["offset"] = 4
    with pytest.raises(ValueError, match="layout evidence"):
        B.request_resources(bad)
    bad = copy.deepcopy(request); bad["operations"][0]["max_nodes"] = 65
    with pytest.raises(ValueError, match="node budget"):
        B.validate_request(bad)


def test_objc_graph_requires_exact_declared_getter_signature_and_edge_layouts():
    graph = {"op": "graph", "root": {"class": "Root", "kind": "unique"},
             "types": [{"class": "Root", "fields": [{"name": "next", "kind": "getter", "type": "object",
                                                         "selector": "next", "signature": "@16@0:8", "target": "Root"}]}],
             "max_depth": 2, "max_nodes": 4, "methods_limit": 8}
    assert B.validate_request({"operations": [graph]})["operations"][0] == graph
    graph["types"][0]["fields"][0].pop("signature")
    with pytest.raises(ValueError, match="observed"):
        B.validate_request({"operations": [graph]})


def test_explicit_backend_override_never_mutates_process_environment(runtime, monkeypatch):
    app, _, _, state, out = runtime
    _gadget_fixture(runtime, monkeypatch)
    before = dict(os.environ)
    receipt = B.collect(app, out, {"operations": [{"op": "classes"}]}, backend="local")
    assert dict(os.environ) == before
    assert receipt["status"] == "observed" and receipt["transport"] == {"kind": "local"}
    assert state["calls"][0]["config"]["backend"] == "local"
    assert state["calls"][0]["config"]["adapter"]["transport"] == {"kind": "local"}


def test_fixed_native_js_checks_image_provenance_lifetime_buffers_and_partial_failures():
    node = shutil.which("node")
    if node is None:
        pytest.skip("static Frida adapter fixture requires the guest's existing Node")
    source = _inert_runtime_js_source()
    fixture = r"""
const fs=require('fs'), vm=require('vm');
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const main='/fixture/Native.app/Contents/MacOS/Native', uuid='0123456789abcdef0123456789abcdef';
const memory=new Map([[0x1000,Buffer.alloc(4096)],[0x2000,Buffer.alloc(4096)],[0x3000,Buffer.alloc(32)],[0x4000,Buffer.alloc(32)],[0x5000,Buffer.alloc(4096)],[0x6000,Buffer.alloc(4096)],[0x7000,Buffer.alloc(4096)]]);
const header=memory.get(0x1000);
[0xfeedfacf,0x0100000c,0,6,3,488,0,0].forEach((v,i)=>header.writeUInt32LE(v,i*4));
header.writeUInt32LE(0x1b,32);header.writeUInt32LE(24,36);Buffer.from(uuid,'hex').copy(header,40);
header.writeUInt32LE(0x19,56);header.writeUInt32LE(312,60);header.write('__TEXT',64);
header.writeBigUInt64LE(0n,80);header.writeBigUInt64LE(4096n,88);header.writeBigUInt64LE(0n,96);header.writeBigUInt64LE(4096n,104);header.writeUInt32LE(3,120);
for(const[i,name,address,size,flags]of [[0,'__text',0x400,0x200,0x80000400],[1,'__const',0x600,0x900,0],[2,'__swift5_types',0xf00,12,0]]){
 const o=128+i*80;header.write(name,o);header.write('__TEXT',o+16);header.writeBigUInt64LE(BigInt(address),o+32);header.writeBigUInt64LE(BigInt(size),o+40);header.writeUInt32LE(address,o+48);header.writeUInt32LE(flags,o+64);}
header.writeUInt32LE(0x19,368);header.writeUInt32LE(152,372);header.write('__DATA',376);
header.writeBigUInt64LE(0x4000n,392);header.writeBigUInt64LE(4096n,400);header.writeBigUInt64LE(4096n,408);header.writeBigUInt64LE(4096n,416);
header.writeUInt32LE(3,424);header.writeUInt32LE(3,428);header.writeUInt32LE(1,432);header.write('__data',440);header.write('__DATA',456);
header.writeBigUInt64LE(0x4000n,472);header.writeBigUInt64LE(4096n,480);header.writeUInt32LE(4096,488);
memory.get(0x3000).writeInt32LE(1,0);memory.get(0x3000).writeBigUInt64LE(0x4000n,8);
memory.get(0x4000).writeInt32LE(2,0);memory.get(0x4000).writeBigUInt64LE(0x3000n,8);
memory.get(0x5000).writeBigUInt64LE(0x3000n,8);
function backing(address, size) {
 for(const [base,bytes] of memory)if(address>=base&&address+size<=base+bytes.length)return [bytes,address-base];
 throw Error('inert address outside mapped memory');
}
class Pointer {
 constructor(value){this.address=typeof value==='string'?Number(value):Number(value);}
 add(n){return new Pointer(this.address+Number(n));} sub(n){return new Pointer(this.address-(n instanceof Pointer?n.address:Number(n)));}
 compare(n){return this.address<n.address?-1:this.address>n.address?1:0;}
 isNull(){return this.address===0;}toString(){return '0x'+this.address.toString(16);}
 readU32(){const [b,o]=backing(this.address,4);return b.readUInt32LE(o);}
 readU8(){const[b,o]=backing(this.address,1);return b[o];}
 readU16(){const[b,o]=backing(this.address,2);return b.readUInt16LE(o);}
 readU64(){const[b,o]=backing(this.address,8);return {toString:()=>b.readBigUInt64LE(o).toString()};}
 readS64(){const[b,o]=backing(this.address,8);return {toString:()=>b.readBigInt64LE(o).toString()};}
 readUtf8String(n){if(n!==undefined)return Buffer.from(Array.from({length:n},(_,i)=>this.add(i).readU8())).toString('utf8');const[b,o]=backing(this.address,1);const end=b.indexOf(0,o);return b.subarray(o,end<0?b.length:end).toString('utf8');}
 readS32(){const [b,o]=backing(this.address,4);return b.readInt32LE(o);}
 readS16(){const [b,o]=backing(this.address,2);return b.readInt16LE(o);}
 readPointer(){const [b,o]=backing(this.address,8);return new Pointer(b.readBigUInt64LE(o));}
 readByteArray(n){const [b,o]=backing(this.address,n);const v=b.subarray(o,o+n);return v.buffer.slice(v.byteOffset,v.byteOffset+v.byteLength);}
 readVolatile(n){return this.readByteArray(n);}
 writeByteArray(v){const [b,o]=backing(this.address,v.length);Buffer.from(v).copy(b,o);}
 writeU8(v){const[b,o]=backing(this.address,1);b[o]=v;}
 writeU32(v){const[b,o]=backing(this.address,4);b.writeUInt32LE(v,o);}
}
let allocateAt=0x8000,calls=[],functionsCreated=0;
const cloakedAllocations=new Set();
const Memory={_alloc(n,protection,near,maxDistance){
 if(protection!=='rw'||!near.isNull()||maxDistance!==0)throw Error('unexpected native allocation options');
 const p=new Pointer(allocateAt);allocateAt+=Math.max(256,n);memory.set(p.address,Buffer.alloc(n,0xa5));cloakedAllocations.add(p.address);return p;
 },
 allocUtf8String(text){const b=Buffer.from(text+'\0','utf8');const p=this.alloc(b.length);p.writeByteArray(b);return p;}};
// Match Frida17.23.3 qjs: immutable public wrapper, mutable nonenumerable
// backing native method. This fixture cannot replace the public allocator.
Object.defineProperty(Memory,'_alloc',{enumerable:false});
Object.defineProperty(Memory,'alloc',{enumerable:true,value:function(size,{near,maxDistance,protection}={}){
 if(near!==undefined&&maxDistance===undefined)throw Error('missing maxDistance option');
 return Memory._alloc(size,protection??'rw',near??new Pointer(0),maxDistance??0);
}});
const symbols={root:0x1400,compound:0x1440,fault:0x1480,swiftSet:0x14c0,ivarUse:0x1500,pairMake:0x1510,pairApply:0x1520,pairMutate:0x1530,dataMetadata:0x1700};
const module={path:main,name:'Native',base:new Pointer(0x1000),size:4096,
 findSymbolByName(name){return symbols[name]===undefined?null:new Pointer(symbols[name]);},findExportByName(){return null;},
 enumerateSymbols(){return Object.keys(symbols).map(name=>({name,type:'function',isGlobal:true,address:new Pointer(symbols[name])}));}};
function NativeFunction(address,ret,args,options){
 functionsCreated++;
 if(options.exceptions!=='steal')throw Error('native exception policy must be explicit');
 return (...values)=>{
   calls.push(address.address);
   if(address.address===symbols.root)return new Pointer(0x3000);
   if(address.address===symbols.fault)throw Error('native fault after entry');
   if(address.address===symbols.ivarUse)return values[0].readS32();
   if(address.address===symbols.pairMutate){
     if(pairMutation==='handle')pairLastValue.handle=new Pointer(0x3000);
     else if(pairMutation==='metadata')pairLastValue.$metadata=new TargetStructMetadata(new Pointer(0x7600));
     else if(pairMutation==='witness')w64(0x7d48,8);
     else throw Error('unknown POD mutation fixture');
     return new Pointer(0x3000);
   }
   if(address.address===symbols.compound){
     if(ret.join(',')!=='int32,int16'||args[0].join(',')!=='int32,int16')throw Error('aggregate must preserve declared field order');
     if(values[1].readByteArray(4)===null)throw Error('buffer missing');
     const initial=Array.from(new Uint8Array(values[1].readVolatile(4)));
     if(initial.some(byte=>byte!==0))throw Error('out allocation was not explicitly zeroed');
     values[1].writeU8(9);return [values[0][0]+1,values[0][1]+2];
   }
   throw Error('unknown inert native function');
 };
}
function w32(a,v){const[b,o]=backing(a,4);b.writeUInt32LE(v>>>0,o);}
function w64(a,v){const[b,o]=backing(a,8);b.writeBigUInt64LE(BigInt(v),o);}
function rel(a,target){const[b,o]=backing(a,4);b.writeInt32LE(target-a,o);}
function text(a,t){const[b,o]=backing(a,t.length+1);b.write(t+'\0',o);}
const canonicalNames=['s13OpaquePointerVSg','s13OpaquePointerV','Sb','s4Int8V','s5UInt8V','s5Int16V','s6UInt16V','s5Int32V','s6UInt32V','s5Int64V','s6UInt64V','Si','Su'];
const canonical=new Map();
canonicalNames.forEach((name,i)=>{
 const metadata=0x7000+i*32,table=0x7800+i*96;canonical.set(name,new Pointer(metadata));
 const width=name==='Sb'||name.includes('Int8')?1:name.includes('Int16')?2:name.includes('Int32')?4:8;
 w64(metadata-8,table);w32(metadata,name==='s13OpaquePointerVSg'?0x202:0x200);w64(table+0x40,width);w64(table+0x48,width);w32(table+0x50,width-1);
});
w32(0x16c0,0);rel(0x16c8,0x16d0);text(0x16d0,'Test');
w32(0x5300,16);rel(0x5304,0x16c0);rel(0x5308,0x53c0);rel(0x5310,0x5340);w32(0x5324,2);w32(0x5328,10);text(0x53c0,'SwiftController');
rel(0x5340,0x53e0);text(0x53e0,'4Test15SwiftControllerC');w32(0x5348,0x000c0001);w32(0x534c,2);
rel(0x5354,0x5390);rel(0x5358,0x5380);text(0x5380,'context');text(0x5390,'s13OpaquePointerVSg');
rel(0x5360,0x53b0);rel(0x5364,0x53a8);text(0x53a8,'closed');text(0x53b0,'Sb');
w32(0x6320,2);w32(0x632c,0);w32(0x6330,24);w32(0x6338,128);w32(0x633c,16);w64(0x6340,0x5300);w64(0x6350,8);w64(0x6358,16);
w64(0x5048,0x3000);memory.get(0x5000)[0x50]=0;
// Independently encode Swift ABI StructDescriptor, not the bridge's wrong
// convenience offsets: common descriptor20B, NumFields+20, vector+24. Poison
// +28 as an unrelated next record so the earlier implementation cannot pass.
w32(0x1800,17);rel(0x1804,0x16c0);rel(0x1808,0x1840);rel(0x1810,0x1860);w32(0x1814,2);w32(0x1818,2);w32(0x181c,0xffffa701);text(0x1840,'Pair');
rel(0x1860,0x18c0);text(0x18c0,'4Test4PairV');w32(0x1868,0x000c0000);w32(0x186c,2);
rel(0x1874,0x18d0);rel(0x1878,0x18b0);rel(0x1880,0x18d0);rel(0x1884,0x18b4);text(0x18b0,'x');text(0x18b4,'y');text(0x18d0,'s5Int64V');
w64(0x75f8,0x7d00);w32(0x7600,0x200);w64(0x7608,0x1800);w32(0x7610,0);w32(0x7614,8);w32(0x7618,0);w32(0x761c,8);w64(0x7d40,16);w64(0x7d48,16);w32(0x7d50,7);
w32(0x1e00,17|0x80);rel(0x1e04,0x16c0);rel(0x1e08,0x1e40);text(0x1e40,'Unrelated');
rel(0x1f00,0x1e00);rel(0x1f04,0x5300);rel(0x1f08,0x1800);
let pairApplied=null,pairConstructorCalls=0,pairApplyCalls=0,pairMode='owned',pairLastValue=null,pairMutation=null;
let pairCloakedChecks=0,pairHandleGetterCalls=0;
class TargetStructMetadata{constructor(handle){this.handle=handle;}}
const runtimeModule={path:'/usr/lib/swift/libswiftCore.dylib',name:'libswiftCore.dylib',base:new Pointer(0x2000),size:4096,
 findExportByName:name=>name==='swift_getTypeByMangledNameInContext'?new Pointer(0x2100):null};
const Swift={available:true,NativeFunction(address,ret,args,context){
 if(address.address===0x2100)return (pointer,length,owner,generic)=>{
  if(ret!=='pointer'||args.join(',')!=='pointer,uint64,pointer,pointer'||!generic.isNull())throw Error('fixed type resolver ABI is wrong');
  const name=pointer.readUtf8String(Number(length.toString()));
  if(name==='4Test4PairV')return new Pointer(0x7600);
  return canonical.get(name)||new Pointer(0);
 };
 if(address.address===symbols.pairMake)return (x,y)=>{
  pairConstructorCalls++;
  if(!(ret instanceof TargetStructMetadata)||ret.handle.address!==0x7600)throw Error('actual SDK metadata needed');
  if(pairMode==='protection')Memory.alloc(16,{protection:'rwx'});
  if(pairMode==='near')Memory.alloc(16,{near:new Pointer(0x10000),maxDistance:4096});
  if(pairMode==='oversize')Memory.alloc(16385);
  if(pairMode==='countBudget')for(let i=0;i<129;i++)Memory.alloc(16);
  if(pairMode==='byteBudget')for(let i=0;i<65;i++)Memory.alloc(16384);
  const handle=pairMode==='untracked'?new Pointer(0x3000):Memory.alloc(pairMode==='short'?8:16);
  if(pairMode!=='untracked'){w64(handle.address,x.toString());if(pairMode!=='short')w64(handle.address+8,y.toString());}
  if(pairMode==='owned'){
   if(Process.findRangeByAddress(handle)!==null)throw Error('SDK allocation must be cloaked in this fixture');
   pairCloakedChecks++;
  }
  const value={handle:pairMode==='interior'?handle.add(8):handle,$metadata:ret};
  if(pairMode==='differentMetadata')value.$metadata=new TargetStructMetadata(ret.handle);
  if(pairMode==='accessor')Object.defineProperty(value,'handle',{get(){pairHandleGetterCalls++;return handle;}});
  pairLastValue=value;return value;
 };
 if(address.address===symbols.pairApply)return pair=>{pairApplyCalls++;pairApplied=Array.from(new Uint8Array(pair.handle.readByteArray(16)));};
 if(address.address!==symbols.swiftSet||ret!=='void'||args.join(',')!=='int32'||context.address!==0x3000)
   throw Error('native Swift signature/context must be exact');
 return value=>{calls.push(address.address);const[b,o]=backing(context.address,4);b.writeInt32LE(value,o);};}};
Object.defineProperty(Swift,'modules',{get(){throw Error('global Swift registry must never be constructed');}});
const Process={id:321,platform:'darwin',arch:'arm64',pointerSize:8,mainModule:module,
 enumerateModules:()=>[module,runtimeModule],findModuleByAddress(pointer){return pointer.address>=0x1000&&pointer.address<0x2000?module:pointer.address>=0x2000&&pointer.address<0x3000?runtimeModule:null;},
 findRangeByAddress(pointer){for(const[base,bytes]of memory)if(pointer.address>=base&&pointer.address<base+bytes.length)
   return cloakedAllocations.has(base)?null:{base:new Pointer(base),size:bytes.length,protection:base===0x1000||base===0x2000?'r-x':'rw-'};return null;}};
let retainCount=0,releaseCount=0,swiftRetainCount=0,swiftReleaseCount=0,ivarEncoding='^v',ivarOffset=8;
const controllerClass={$kind:'class',$className:'OpaqueController',handle:{className:'OpaqueController'},$superClass:null};
const controller={$kind:'instance',$className:'OpaqueController',$class:controllerClass,handle:new Pointer(0x5000),
 $methods:['- retain','- release'],'- retain':()=>retainCount++,'- release':()=>releaseCount++};
const swiftControllerClass={$kind:'class',$className:'SwiftController',handle:new Pointer(0x6300),$superClass:null};
const swiftController={...controller,$className:'SwiftController',$class:swiftControllerClass,handle:new Pointer(0x5040),
 '- retain':()=>swiftRetainCount++,'- release':()=>swiftReleaseCount++};
const stringPointer=text=>({isNull:()=>false,readUtf8String:()=>text});
const ObjC={available:true,mainQueue:'main',schedule:(_q,work)=>work(),classes:{OpaqueController:controllerClass,SwiftController:swiftControllerClass},
 choose(spec,callbacks){callbacks.onMatch(spec.class.$className==='SwiftController'?swiftController:controller);callbacks.onComplete();},api:{
 class_getImageName:()=>stringPointer(main),class_getInstanceSize:()=>24,
 class_copyIvarList(_class,count){count.writeU32(1);const list=Memory.alloc(8);const b=Buffer.alloc(8);b.writeBigUInt64LE(0x6000n);list.writeByteArray(b);return list;},
 ivar_getName:()=>stringPointer('context'),ivar_getTypeEncoding:()=>stringPointer(ivarEncoding),ivar_getOffset:()=>({toString:()=>String(ivarOffset)}),free:()=>{}}};
const context=vm.createContext({ObjC,Swift,TargetStructMetadata,Memory,Process,NativeFunction,ptr:x=>new Pointer(x),
 int64:x=>({toString:()=>x}),uint64:x=>({toString:()=>x}),rpc:{exports:{}},send:()=>{},Date,Map,Set,Promise,Error,Uint8Array});
vm.runInContext(data.source,context,{timeout:1000});
const target={module:{path:main,sha256:'a'.repeat(64)},symbol:'root'};
const evidence={kind:'compiler',file:'/fixture/contracts.json',sha256:'b'.repeat(64),pointer:'/call'};
const root={kind:'native_function',target,abi:'default',arguments:[],return_type:'pointer',signature_evidence:evidence};
const pins={[main]:{file:main,sha256:'a'.repeat(64),bytes:4096,macho_images:[{cpu_type:0x0100000c,cpu_subtype:0,uuid}]},
 '/fixture/contracts.json':{file:'/fixture/contracts.json',sha256:'b'.repeat(64),bytes:100}};
pins[main].macho_sections=[{image_identity:pins[main].macho_images[0],sections:[{segment:'__TEXT',name:'__text',offset:'0x400',bytes:0x200,flags:0x80000400}]}];
pins[main].macho_segments=[{image_identity:pins[main].macho_images[0],segments:[
 {segment:'__TEXT',offset:'0x0',bytes:4096,file_offset:0,file_bytes:4096,max_protection:0,initial_protection:0},
 {segment:'__DATA',offset:'0x4000',bytes:4096,file_offset:4096,file_bytes:4096,max_protection:3,initial_protection:3}]}];
async function run(operations,resources=pins){
 const before=Object.getOwnPropertyDescriptor(Memory,'_alloc'),publicBefore=Object.getOwnPropertyDescriptor(Memory,'alloc');
 const result=await context.rpc.exports.run({request:{operations},native_resources:resources,
  deadline_ms:Date.now()+3000,pid:321,bundle:'/fixture/Native.app',executable:main});
 const after=Object.getOwnPropertyDescriptor(Memory,'_alloc'),publicAfter=Object.getOwnPropertyDescriptor(Memory,'alloc');
 if(before.value!==after.value||before.writable!==after.writable||before.enumerable!==after.enumerable||before.configurable!==after.configurable)
  throw Error('trusted SDK allocator must restore exactly on every exit');
 if(publicBefore.value!==publicAfter.value||publicAfter.writable!==false||publicAfter.configurable!==false||publicAfter.enumerable!==true)
  throw Error('immutable public allocator must remain unchanged');
 return result;
}
function operation(call){const {kind,...op}=call;return {op:'native_invoke',...op};}
(async()=>{
 const pod={kind:'struct',fields:[{name:'x',type:'int32'},{name:'y',type:'int16'}]};
 const compound=await run([operation({...root,target:{...target,symbol:'compound'},return_type:pod,arguments:[
   {type:pod,value:{x:10,y:3}},{type:{kind:'buffer',bytes:4,direction:'out'},value:null}]})]);
 const count=calls.length,created=functionsCreated;
 const wrong=JSON.parse(JSON.stringify(pins));wrong[main].macho_images[0].uuid='0'.repeat(32);
 const stale=await run([operation(root)],wrong);
 if(calls.length!==count||functionsCreated!==created)throw Error('stale image denial must precede callable construction/invocation');
 const changedSegments=JSON.parse(JSON.stringify(pins));changedSegments[main].macho_segments[0].segments[1].file_offset++;
 const staleSegments=await run([operation(root)],changedSegments);
 if(calls.length!==count||functionsCreated!==created)throw Error('loaded segment drift must deny before native entry construction');
 const graph=await run([{op:'graph',root:{native:root},root_type:'Node',types:[{name:'Node',bytes:16,
   fields:[{name:'value',offset:0,type:'int32'},{name:'child',offset:8,type:'pointer',target:'Node'}]}],max_depth:3,max_nodes:8,methods_limit:0}]);
 const bounded=await run([{op:'graph',root:{native:root},root_type:'Node',types:[{name:'Node',bytes:16,
   fields:[{name:'child',offset:8,type:'pointer',target:'Node'}]}],max_depth:0,max_nodes:8,methods_limit:0}]);
 const swift=await run([operation({...root,target:{...target,symbol:'swiftSet'},abi:'swift',return_type:'void',
   context:{type:'pointer',value:{getter:root}},arguments:[{type:'int32',value:27}]})]);
 const afterSwift=memory.get(0x3000).readInt32LE(0);
 const read={receiver:{class:'OpaqueController',kind:'unique',subclasses:false},receiver_class:'OpaqueController',
   field:{class:'OpaqueController',name:'context',encoding:'^v',offset:8},module:target.module,evidence};
 const ivarOperation=operation({...root,target:{...target,symbol:'ivarUse'},return_type:'int32',arguments:[{type:'pointer',value:{read}}]});
 const ivar=await run([ivarOperation]);
 const metadataCount=calls.length,metadataConstructed=functionsCreated;
 const nonCallable=await run([operation({...root,target:{...target,symbol:'dataMetadata'}})]);
 if(calls.length!==metadataCount||functionsCreated!==metadataConstructed)throw Error('RX metadata must be refused before constructing a callable');
 const inventory=await run([{op:'swift_types',image:target.module,module:'Test',query:'Pair',limit:4}]);
 const swiftRead={receiver:{class:'SwiftController',kind:'unique',subclasses:false},receiver_class:'SwiftController',
  swift_class:{module:'Test',name:'SwiftController'},field:{name:'context',type_name:'Swift.Optional<Swift.OpaquePointer>',offset:8},
  module:target.module,evidence,preconditions:[{name:'closed',type_name:'Swift.Bool',equals:false}]};
 const reflected=await run([{op:'swift_fields',...Object.fromEntries(['receiver','receiver_class','swift_class','module'].map(key=>[key,swiftRead[key]])),query:'context',limit:4}]);
 const typedOp=operation({...root,target:{...target,symbol:'ivarUse'},return_type:'int32',arguments:[{type:'pointer',value:{swift_read:swiftRead}}]});
 const typed=await run([typedOp]);
 const typedCount=calls.length;
 memory.get(0x5000)[0x50]=2;const invalidBool=await run([typedOp]);memory.get(0x5000)[0x50]=0;
 memory.get(0x5000)[0x50]=1;const lifecycleRefused=await run([typedOp]);memory.get(0x5000)[0x50]=0;
 text(0x5390,'Sb');const wrongSwiftType=await run([typedOp]);text(0x5390,'s13OpaquePointerVSg');
 w64(0x6340,0x3000);const imageHole=await run([typedOp]);w64(0x6340,0x5300);
 memory.get(0x5000).write('Swif',0xffc);memory.get(0x6000).write('tController\0',0);
 rel(0x5308,0x5ffc);const crossingName=await run([typedOp]);rel(0x5308,0x53c0);
 w32(0x6338,80);const badVector=await run([typedOp]);w32(0x6338,128);
 const forbiddenSwift=[];
 for(const flag of [0x10000,0x100000,0x400000,0x800000,0x1000000,0x2000000]){
  w32(0x7850,7|flag);forbiddenSwift.push(await run([typedOp]));
 }w32(0x7850,7);
 w64(0x5048,0);const nilSwift=await run([typedOp]);const nilObservation=await run([{op:'swift_read',read:swiftRead}]);w64(0x5048,0x3000);
 if(calls.length!==typedCount)throw Error('bad typed fields must refuse before the target native call');
 w32(0x5300,16|0x20000000);rel(0x5318,0x1df0);w64(0x1df0,16);w32(0x5328,8);
 const resilient=await run([typedOp]);w32(0x5300,16);w32(0x5328,10);
 const pairType={kind:'swift_type',module:'Test',name:'Pair',category:'struct',bytes:16,fields:[{name:'x',type:'int64'},{name:'y',type:'int64'}]};
 const pairGetter={...root,target:{...target,symbol:'pairMake'},abi:'swift',return_type:pairType,arguments:[{type:'int64',value:'3'},{type:'int64',value:'9'}]};
 const pairCall=operation({...root,target:{...target,symbol:'pairApply'},abi:'swift',return_type:'void',arguments:[{type:pairType,value:{getter:pairGetter}}]});
 const pair=await run([pairCall]);
 // A different valid vector offset must not be mistaken for the field count.
 w32(0x1818,3);const shiftedPair=await run([pairCall]);w32(0x1818,2);
 const pairReturn=await run([operation(pairGetter)]);
 const storageApplyCount=pairApplyCalls,pairStorageFailures=[];
 for(const mode of ['untracked','short','interior','accessor','differentMetadata']){
  pairMode=mode;pairStorageFailures.push({mode,receipt:await run([pairCall])});
  if(pairApplyCalls!==storageApplyCount)throw Error('invalid SDK storage must refuse before apply');
 }pairMode='owned';
 const mutatingPairCall=JSON.parse(JSON.stringify(pairCall));
 mutatingPairCall.arguments.push({type:'pointer',value:{getter:{...root,target:{...target,symbol:'pairMutate'}}}});
 const changedPairStorage=[];
 for(const mutation of ['handle','metadata','witness']){
  pairMutation=mutation;changedPairStorage.push({mutation,receipt:await run([mutatingPairCall])});w64(0x7d48,16);
  if(pairApplyCalls!==storageApplyCount)throw Error('later getter changed earlier POD argument before apply');
 }pairMutation=null;
 const allocationPolicyFailures=[];
 for(const mode of ['protection','near','oversize','countBudget','byteBudget']){
  pairMode=mode;const before=calls.length;
  allocationPolicyFailures.push({mode,receipt:await run([pairCall,operation(root)])});
  if(pairApplyCalls!==storageApplyCount||calls.length!==before)throw Error('allocator refusal must stop apply and later operations');
 }pairMode='owned';
 Object.defineProperty(Memory,'_alloc',{writable:false});
 const allocatorGuard=await run([pairCall]);
 Object.defineProperty(Memory,'_alloc',{writable:true});
 const constructorCount=pairConstructorCalls,applyCount=pairApplyCalls;
 w32(0x1814,1);const wrongPairCount=await run([pairCall]);w32(0x1814,2);
 const badPairVectors=[];
 for(const vector of [0,1,131073]){w32(0x1818,vector);badPairVectors.push(await run([pairCall]));}w32(0x1818,2);
 w32(0x7614,1);const misalignedPair=await run([pairCall]);w32(0x7614,8);
 w32(0x7614,16);const outsidePair=await run([pairCall]);w32(0x7614,8);
 w32(0x7614,0);const overlappingPair=await run([pairCall]);w32(0x7614,8);
 w64(0x7608,0x1804);const wrongPairDescriptor=await run([pairCall]);w64(0x7608,0x1800);
 // The common header still fits, but the struct's extra words would cross
 // into the separate runtime image. Exact-image bounds must refuse the read.
 w32(0x1fec,17);rel(0x1ff0,0x16c0);rel(0x1ff4,0x1840);w32(0x1ff8,0);rel(0x1ffc,0x1860);
 rel(0x1f08,0x1fec);w64(0x7608,0x1fec);const truncatedPairDescriptor=await run([pairCall]);
 rel(0x1f08,0x1800);w64(0x7608,0x1800);
 if(pairConstructorCalls!==constructorCount||pairApplyCalls!==applyCount)
  throw Error('invalid actual struct metadata must refuse before constructor and aggregate entry');
 w32(0x7d50,7|0x800000);const noncopyablePair=await run([pairCall]);w32(0x7d50,7);
 const badPairCall=JSON.parse(JSON.stringify(pairCall));badPairCall.arguments[0].type.fields[0].type='double';
 badPairCall.arguments[0].value.getter.return_type.fields[0].type='double';
 const fpPair=await run([badPairCall]);
 const callCount=calls.length;
 ivarEncoding='@';const changedIvar=await run([ivarOperation]);ivarEncoding='^v';
 ivarOffset=24;const outside=await run([ivarOperation]);ivarOffset=8;
 memory.get(0x5000).writeBigUInt64LE(0n,8);const nilIvar=await run([ivarOperation]);memory.get(0x5000).writeBigUInt64LE(0x3000n,8);
 if(calls.length!==callCount)throw Error('invalid or null ivar must deny before the target call');
 if(retainCount!==releaseCount||swiftRetainCount!==swiftReleaseCount)throw Error('fresh ivar and Swift receivers must be released on every exit');
 const partial=await run([operation(root),operation({...root,target:{...target,symbol:'fault'},return_type:'void'})]);
 console.log(JSON.stringify({compound,stale,staleSegments,graph,bounded,swift,afterSwift,ivar,changedIvar,outside,nilIvar,nonCallable,inventory,reflected,
  typed,invalidBool,lifecycleRefused,wrongSwiftType,imageHole,crossingName,badVector,forbiddenSwift,nilSwift,nilObservation,resilient,pair,shiftedPair,
  wrongPairCount,badPairVectors,misalignedPair,outsidePair,overlappingPair,wrongPairDescriptor,truncatedPairDescriptor,
  pairReturn,pairStorageFailures,changedPairStorage,allocationPolicyFailures,allocatorGuard,pairCloakedChecks,pairHandleGetterCalls,
  pairApplied,pairConstructorCalls,pairApplyCalls,noncopyablePair,fpPair,retainCount,releaseCount,swiftRetainCount,swiftReleaseCount,partial,calls}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    run = subprocess.run([node, "-e", fixture], input=json.dumps({"source": source}), capture_output=True,
                         text=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    compound = result["compound"]
    assert compound["status"] == "completed" and compound["operations"][0]["value"] == {"x": 11, "y": 5}
    assert compound["operations"][0]["out_buffers"] == [{"argument": 1, "bytes": [9, 0, 0, 0]}]
    assert result["stale"]["status"] == "incomplete" and result["stale"]["native_calls"] == 0
    assert result["staleSegments"]["status"] == "incomplete" and result["staleSegments"]["native_calls"] == 0
    graph = result["graph"]["operations"][0]
    assert len(graph["nodes"]) == 2 and any(edge["cycle"] for edge in graph["edges"])
    assert graph["getter_calls"] == 1 and graph["verified"] is False and graph["errors"] == []
    assert graph["nodes"][1]["lineage"][1]["field"] == "child"
    assert result["bounded"]["operations"][0]["truncated"] is True
    assert result["swift"]["status"] == "completed" and result["afterSwift"] == 27
    assert result["swift"]["operations"][0]["function"]["bridge"] == "frida-swift"
    assert result["nonCallable"]["status"] == "incomplete" and result["nonCallable"]["native_calls"] == 0
    assert result["inventory"]["status"] == "completed"
    assert [row["name"] for row in result["inventory"]["operations"][0]["rows"]] == ["Pair"]
    assert result["reflected"]["operations"][0]["rows"][0]["type_name"] == "Swift.Optional<Swift.OpaquePointer>"
    assert result["typed"]["status"] == "completed" and result["typed"]["operations"][0]["value"] == 27
    typed_read = result["typed"]["operations"][0]["swift_field_reads"][0]
    assert typed_read["source"] == "swift_metadata" and typed_read["layout"]["canonical_type_verified"] is True
    assert typed_read["module_size"] == 0x5000 and typed_read["reported_module_size"] == 4096
    assert int(typed_read["field"]["descriptor_offset"], 16) > typed_read["reported_module_size"]
    assert typed_read["layout"]["encoding"] == "zero_is_none" and typed_read["preconditions"][0]["raw_byte"] == 0
    assert typed_read["field"]["vector_offset_words"] * 8 + typed_read["field"]["num_fields"] * 8 <= typed_read["class_size"] - typed_read["class_address_point"]
    for failure in [result[name] for name in ("invalidBool", "lifecycleRefused", "wrongSwiftType", "imageHole", "crossingName", "badVector", "nilSwift", "noncopyablePair", "fpPair")] + result["forbiddenSwift"]:
        assert failure["status"] == "incomplete" and failure["invocations"] == 0
        assert failure["native_state_uncertain"] is False and failure["handles_expired"] is True
    assert result["nilObservation"]["operations"][0]["observation"]["pointer"] is None
    assert result["resilient"]["operations"][0]["swift_field_reads"][0]["field"]["vector_offset_words"] == 10
    assert result["pair"]["status"] == "completed" and result["pair"]["invocations"] == 1
    assert result["shiftedPair"]["status"] == "completed" and result["shiftedPair"]["invocations"] == 1
    assert result["pairApplied"] == list(struct.pack("<qq", 3, 9))
    pair_return = result["pairReturn"]
    assert pair_return["status"] == "completed" and pair_return["invocations"] == 1
    assert pair_return["operations"][0]["value"]["bytes"] == list(struct.pack("<qq", 3, 9))
    assert pair_return["operations"][0]["value"]["ownership"] == "trivial_pod"
    assert pair_return["operations"][0]["value"]["expires"] == "transaction"
    assert result["pairConstructorCalls"] == 16 and result["pairApplyCalls"] == 2
    assert result["pairCloakedChecks"] == 6 and result["pairHandleGetterCalls"] == 0
    assert {item["mode"] for item in result["pairStorageFailures"]} == {"untracked", "short", "interior", "accessor", "differentMetadata"}
    assert {item["mutation"] for item in result["changedPairStorage"]} == {"handle", "metadata", "witness"}
    for failure in [item["receipt"] for item in result["pairStorageFailures"] + result["changedPairStorage"]] + [result["allocatorGuard"]]:
        assert failure["status"] == "incomplete" and failure["invocations"] == 0
        assert failure["native_state_uncertain"] is False and failure["handles_expired"] is True
    assert {item["mode"] for item in result["allocationPolicyFailures"]} == {"protection", "near", "oversize", "countBudget", "byteBudget"}
    for item in result["allocationPolicyFailures"]:
        failure = item["receipt"]
        assert failure["status"] == "incomplete" and failure["invocations"] == 0 and failure["operations"] == []
        assert failure["native_state_uncertain"] is True and failure["handles_expired"] is True
    for failure in [result[name] for name in ("wrongPairCount", "misalignedPair", "outsidePair", "overlappingPair",
                                           "wrongPairDescriptor", "truncatedPairDescriptor")] + result["badPairVectors"]:
        assert failure["status"] == "incomplete" and failure["invocations"] == 0
        assert failure["native_state_uncertain"] is False and failure["handles_expired"] is True
    ivar = result["ivar"]
    assert ivar["status"] == "completed" and ivar["operations"][0]["value"] == 27
    read = ivar["operations"][0]["pointer_reads"][0]
    assert read["receiver_class"] == "OpaqueController" and read["field"]["encoding"] == "^v"
    assert read["field"]["offset"] == "8" and read["instance_bytes"] == 24
    assert read["pointer"]["ownership"] == "borrowed_no_escape" and read["pointer"]["expires"] == "transaction"
    for failure in ("changedIvar", "outside", "nilIvar"):
        assert result[failure]["status"] == "incomplete" and result[failure]["invocations"] == 0
        assert result[failure]["native_state_uncertain"] is False and result[failure]["handles_expired"] is True
    assert result["retainCount"] == result["releaseCount"] == 4
    assert result["swiftRetainCount"] == result["swiftReleaseCount"] == 17
    partial = result["partial"]
    assert partial["status"] == "incomplete" and len(partial["operations"]) == 1 and partial["invocations"] == 2
    assert partial["native_state_uncertain"] is True and partial["handles_expired"] is True


def _ivar_pointer_read(module, evidence):
    return {"receiver": {"class": "OpaqueController", "kind": "unique", "subclasses": False},
            "receiver_class": "OpaqueController", "field": {"class": "OpaqueController", "name": "context", "encoding": "^v", "offset": 8},
            "module": copy.deepcopy(module), "evidence": {**copy.deepcopy(evidence), "pointer": "/ivar"}}


def test_borrowed_pointer_owner_accepts_exact_runtime_class_without_relaxing_field_names(native_files):
    actor, _, _, _ = native_files
    read = _ivar_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    klass = "..NSKVONotifying_IINA.MainWindow"
    read["receiver"]["class"] = read["receiver_class"] = read["field"]["class"] = klass
    B._pointer_read(read)
    read["field"]["name"] = ".context"
    with pytest.raises(ValueError, match="field name"):
        B._pointer_read(read)


def test_borrowed_ivar_pointer_recipe_is_fixed_and_never_promotes_a_raw_offset(native_files):
    actor, getter, module, contract = native_files
    read = _ivar_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    actor["arguments"].append({"type": "pointer", "value": {"read": read}})
    body = R.load_json(contract)
    body["call"] = B._contract(actor)
    body["ivar"] = B._pointer_read_contract(read)
    R.write_json(contract, body)
    sha = R._file_hash(contract)
    actor["signature_evidence"]["sha256"] = read["evidence"]["sha256"] = sha
    resources = B.native_resources(actor)
    assert set(resources) == {str(module), str(contract)}
    assert B.pointer_read_recipes(actor) == [read]
    assert B.argument_leaves(actor["arguments"][-1]) == []
    bound = B.bind_actor({**actor, "arguments": [{"type": "int32", "value": {"input": "amount"}}, actor["arguments"][-1]]}, {"amount": "12"})
    assert bound["arguments"][-1] == actor["arguments"][-1]
    assert B.request_resources(B.invocation_request(bound)) == resources
    bad = copy.deepcopy(actor); bad["arguments"][-1]["value"]["read"]["field"]["offset"] = 16
    with pytest.raises(ValueError, match="ivar evidence"):
        B.native_resources(bad)


@pytest.mark.parametrize("change", ["raw", "owner", "receiver_class", "encoding", "offset", "evidence", "extra"])
def test_ivar_pointer_recipe_rejects_missing_provenance_or_an_untyped_slot(change):
    actor = B.bind_actor(NATIVE, {"amount": "1"})
    read = _ivar_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    actor["arguments"] = [{"type": "pointer", "value": {"read": read}}]
    if change == "raw":
        read["receiver"] = {"address": "0x1234"}
    elif change == "owner":
        read["field"].pop("class")
    elif change == "receiver_class":
        read.pop("receiver_class")
    elif change == "encoding":
        read["field"]["encoding"] = "@"
    elif change == "offset":
        read["field"]["offset"] = 2**32
    elif change == "evidence":
        read.pop("evidence")
    else:
        read["fallback_address"] = "0x1234"
    with pytest.raises(ValueError):
        B.validate_actor(actor)


@pytest.mark.parametrize("encoding", ["^v", "r^v", "^^?", '^@"NSObject"', "^{opaque}", "^{Node=i^v}"])
def test_pointer_slot_encodings_are_observed_exactly_without_assuming_the_pointee_abi(encoding):
    B._pointer_encoding(encoding)


@pytest.mark.parametrize("encoding", ["@", "v", "^", "^vi", "^v16", "^{Node", "^@\"NSObject", "^{Node=^}"])
def test_pointer_slot_encoding_must_be_one_complete_pointer(encoding):
    with pytest.raises(ValueError):
        B._pointer_encoding(encoding)


def test_ivar_pointer_recipe_order_includes_swift_context_and_nested_getter_reads():
    actor = B.bind_actor(NATIVE, {"amount": "1"})
    first = _ivar_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    second = copy.deepcopy(first); second["field"]["name"] = "second"
    third = copy.deepcopy(first); third["field"]["name"] = "third"
    getter = {**copy.deepcopy(actor), "arguments": [{"type": "pointer", "value": {"read": second}}], "return_type": "pointer"}
    actor["abi"] = "swift"
    actor["context"] = {"type": "pointer", "value": {"read": first}}
    actor["arguments"] = [{"type": "pointer", "value": {"getter": getter}}, {"type": "pointer", "value": {"read": third}}]
    assert B.pointer_read_recipes(actor) == [first, second, third]
    assert B.pointer_read_recipes({**actor, "arguments": [{"type": "int32", "value": {"input": "amount"}}]}) == [first]


def _swift_pointer_read(module, evidence):
    return {"receiver": {"class": "Mapped.Controller", "kind": "unique", "subclasses": False},
            "receiver_class": "Mapped.Controller", "swift_class": {"module": "Mapped", "name": "Controller"},
            "field": {"name": "context", "type_name": "Swift.Optional<Swift.OpaquePointer>", "offset": 8},
            "preconditions": [{"name": "closed", "type_name": "Swift.Bool", "equals": False}],
            "module": copy.deepcopy(module), "evidence": {**copy.deepcopy(evidence), "pointer": "/swift_read"}}


def test_swift_owner_accepts_runtime_receiver_but_keeps_source_identifiers_strict(native_files):
    actor, _, _, _ = native_files
    read = _swift_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    read["receiver"]["class"] = read["receiver_class"] = "..NSKVONotifying_IINA.MainWindow"
    B._swift_read(read)
    for key in ("module", "name"):
        changed = copy.deepcopy(read)
        changed["swift_class"][key] = "." + changed["swift_class"][key]
        with pytest.raises(ValueError, match="module/class"):
            B._swift_read(changed)
    read["field"]["name"] = ".context"
    with pytest.raises(ValueError, match="Swift pointer field"):
        B._swift_read(read)


def test_typed_swift_field_pins_contract_and_freezes_metadata_without_weakening_objc(native_files):
    actor, _, module, contract = native_files
    read = _swift_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    actor["arguments"] = [{"type": "int32", "value": {"input": "amount"}}, {"type": "pointer", "value": {"swift_read": read}}]
    body = R.load_json(contract); body["call"] = B._contract(actor); body["swift_read"] = B._swift_read_contract(read)
    R.write_json(contract, body)
    actor["signature_evidence"]["sha256"] = read["evidence"]["sha256"] = R._file_hash(contract)
    assert B.swift_read_recipes(actor) == [read] and B.pointer_read_recipes(actor) == []
    assert B.argument_leaves(actor["arguments"][1]) == []
    bound = B.bind_actor(actor, {"amount": "3"})
    assert bound["arguments"][1]["value"]["swift_read"] == read
    resources = B.native_resources(bound)
    assert set(resources) == {str(module), str(contract)}
    assert B.request_resources({"operations": [{"op": "swift_read", "read": read}]}) == resources
    bad = copy.deepcopy(bound); bad["arguments"][1]["value"]["swift_read"]["field"]["offset"] = 16
    with pytest.raises(ValueError, match="evidence differs"):
        B.native_resources(bad)
    with pytest.raises(ValueError):
        B._pointer_encoding("")


@pytest.mark.parametrize("change", ["raw", "class", "module", "type", "encoding", "offset", "contract", "generic", "bool", "duplicates"])
def test_typed_swift_field_refuses_guessed_slot_type_and_unsupported_lifecycle(change):
    actor = B.bind_actor(NATIVE, {"amount": "1"})
    read = _swift_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    actor["arguments"] = [{"type": "pointer", "value": {"swift_read": read}}]
    if change == "raw":
        read["receiver"] = {"address": "0x1234"}
    elif change == "class":
        read.pop("receiver_class")
    elif change == "module":
        read["swift_class"].pop("module")
    elif change == "type":
        read["field"]["type_name"] = "Swift.Optional<Swift.Double>"
    elif change == "encoding":
        read["field"]["encoding"] = "^v"
    elif change == "offset":
        read["field"]["offset"] = 2**32
    elif change == "contract":
        read.pop("evidence")
    elif change == "generic":
        read["swift_class"]["name"] = "Controller<Int>"
    elif change == "bool":
        read["preconditions"][0]["equals"] = 0
    else:
        read["preconditions"] *= 2
    with pytest.raises(ValueError):
        B.validate_actor(actor)


def test_swift_field_read_order_preserves_context_and_nested_getter_provenance():
    actor = B.bind_actor(NATIVE, {"amount": "1"})
    first = _swift_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    second = copy.deepcopy(first); second["field"]["name"] = "second"
    third = copy.deepcopy(first); third["field"]["name"] = "third"
    getter = {**copy.deepcopy(actor), "arguments": [{"type": "pointer", "value": {"swift_read": second}}], "return_type": "pointer"}
    actor["abi"] = "swift"; actor["context"] = {"type": "pointer", "value": {"swift_read": first}}
    actor["arguments"] = [{"type": "pointer", "value": {"getter": getter}}, {"type": "pointer", "value": {"swift_read": third}}]
    assert B.swift_read_recipes(actor) == [first, second, third]


def test_receiver_bound_swift_metadata_discovery_pins_only_the_declared_image(native_files):
    actor, _, module, _ = native_files
    read = _swift_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    operation = {"op": "swift_fields", **{key: read[key] for key in ("receiver", "receiver_class", "swift_class", "module")}, "query": "context", "limit": 4}
    request = {"operations": [operation]}
    assert B.validate_request(request) == request and set(B.request_resources(request)) == {str(module)}
    with pytest.raises(ValueError):
        B.validate_request({"operations": [{**operation, "offset": 8}]})
    assert B.request_resources({"operations": [{"op": "swift_types", "image": actor["target"]["module"], "module": "Mapped"}]}) == B.request_resources(request)


@pytest.mark.parametrize("fault", ["missing", None, 0, "false", []])
def test_metadata_worker_requires_an_exact_fault_boolean_before_agent_can_continue(native_files, runtime, monkeypatch, fault):
    actor, _, _, _ = native_files
    app, _, _, state, out = runtime
    read = _swift_pointer_read(actor["target"]["module"], actor["signature_evidence"])
    operation = {"op": "swift_fields", **{key: read[key] for key in ("receiver", "receiver_class", "swift_class", "module")}}
    original_load = R.load_json
    def load(path):
        value = original_load(path)
        if Path(path).name == "worker-output.json":
            if fault == "missing":
                value.pop("native_state_uncertain", None)
            else:
                value["native_state_uncertain"] = fault
        return value
    monkeypatch.setattr(R, "load_json", load)
    receipt = B.collect(app, out, {"operations": [operation]})
    assert receipt["status"] == "incomplete" and receipt["actuated"] is None and receipt["native_calls"] is None
    assert receipt["resource_review_required"] is True and receipt["error"] and len(state["calls"]) == 1


@pytest.mark.parametrize("field_type", ["double", "float", "bool", "utf8", "void", {"kind": "struct", "fields": [{"name": "x", "type": "int64"}]}])
def test_swift_register_aggregate_requires_attributable_integer_pointer_fields(field_type):
    pair = {"kind": "swift_type", "module": "Mapped", "name": "Pair", "category": "struct", "bytes": 16,
            "fields": [{"name": "x", "type": field_type}, {"name": "y", "type": "int64"}]}
    with pytest.raises(ValueError):
        B._native_type(pair, result=True)
    pair["fields"][0]["type"] = "int64"
    B._native_type(pair, result=True)
    pair.pop("fields")
    with pytest.raises(ValueError):
        B._native_type(pair, result=True)


def test_macho_instruction_ranges_exclude_metadata_and_reject_file_layout_overflows(runtime):
    app, _, _, _, _ = runtime
    image = app.parent / "Inert.dylib"; original = _inert_macho(); image.write_bytes(original)
    assert B._macho_sections(image)[0]["sections"][0]["offset"] == "0x1000"
    metadata = bytearray(original); struct.pack_into("<I", metadata, 56 + 72 + 64, 0)
    image.write_bytes(metadata)
    assert B._macho_sections(image)[0]["sections"] == []
    overflow = bytearray(original); struct.pack_into("<Q", overflow, 56 + 72 + 40, 0x3000)
    image.write_bytes(overflow)
    with pytest.raises(ValueError, match="section exceeds"):
        B._macho_sections(image)


def _compiler_actor(family="swift_double_bool_context"):
    """Frozen declaration fixture; not executable code or an asserted build."""
    original_module = {"path": "/fixture/Original.dylib", "sha256": "a" * 64}
    helper_module = {"path": "/fixture/CompilerHelper.dylib", "sha256": "c" * 64}
    evidence = {"kind": "reviewed_contract", "file": "/fixture/declarations.json", "sha256": "b" * 64, "pointer": "/original"}
    cpp = family == "cxx_owned_string_vector_double"
    owned_return = family == "cxx_owned_string_bytes"
    string = {"kind": "cxx_owned", "name": "std::string", "library": "libc++", "abi_version": 1}
    entry = {"module": original_module, "symbol": "private_entry", "abi": "default" if cpp or owned_return else "swift",
             "argument_types": ([{"kind": "cxx_owned", "name": name, "library": "libc++", "abi_version": 1}
                                 for name in ("std::string", "std::vector<double>")] if cpp else
                                [copy.deepcopy(string)] if owned_return else ["double", "bool"]),
             "return_type": copy.deepcopy(string) if owned_return else "double", "context": "cxx_this" if cpp or owned_return else "swiftself",
             "signature_evidence": evidence, "pointer_policy": "borrowed_no_escape"}
    getter = {"kind": "native_function", "target": {"module": original_module, "symbol": "borrowed_root"},
              "abi": "default", "arguments": [], "return_type": "pointer", "signature_evidence": {**evidence, "pointer": "/getter"}}
    arguments = [{"type": "pointer", "value": {"callable": entry}}, {"type": "pointer", "value": {"getter": getter}}]
    if cpp:
        arguments.extend([{"type": "utf8", "value": {"input": "label"}}, {"type": "uint32", "value": {"input": "count"}},
                          {"type": {"kind": "buffer", "bytes": 16, "direction": "in"}, "value": {"input": "vector"}}])
    elif owned_return:
        arguments.extend([{"type": "utf8", "value": {"input": "label"}},
                          {"type": {"kind": "buffer", "bytes": 16, "direction": "out"}, "value": None}])
    else:
        arguments.extend([{"type": "double", "value": {"input": "amount"}}, {"type": "bool", "value": {"input": "enabled"}}])
    arguments.append({"type": {"kind": "buffer", "bytes": 24, "direction": "out"}, "value": None})
    return {"kind": "native_function", "target": {"module": helper_module, "symbol": "rea_compiler_adapter"}, "abi": "default",
            "arguments": arguments, "return_type": "uint32", "signature_evidence": {**evidence, "pointer": "/outer"},
            "compiler_adapter": {"family": family, "completion_argument": len(arguments) - 1,
                                 "evidence": {**evidence, "file": "/fixture/build.json", "pointer": "/contract", "sha256": "d" * 64}}}


@pytest.mark.parametrize("family,parameters", [
    ("swift_double_bool_context", {"amount": "-2.5", "enabled": "false"}),
    ("cxx_owned_string_bytes", {"label": "owned return Grüße"}),
    ("cxx_owned_string_vector_double", {"label": "owned temporary", "count": "2", "vector": "000000000000f03f0000000000000040"})])
def test_compiler_transport_binds_only_data_leaves_and_preserves_original_entry_declaration(family, parameters):
    actor = _compiler_actor(family)
    saved = copy.deepcopy(actor)
    bound = B.bind_actor(actor, parameters)
    entry = bound["arguments"][0]["value"]["callable"]
    assert B.callable_recipes(actor) == [entry] and actor == saved
    assert B.argument_leaves(actor["arguments"][0]) == []
    contract = B.compiler_adapter_contract(bound)
    assert contract["original"] == B.callable_contract(entry, allow_owned_return=family == "cxx_owned_string_bytes")
    assert contract["original"]["entry"]["module"] != contract["helper"]["module"]
    assert contract["original"]["context"] == ("cxx_this" if family.startswith("cxx") else "swiftself")
    completion = {"format": B.COMPILER_COMPLETION, "argument": len(bound["arguments"]) - 1,
                  "bytes": 24, "status_return": "uint32", "dispatch_flag": "dispatch_attempted"}
    if family == "cxx_owned_string_bytes":
        completion.update(format=B.COMPILER_BYTES_COMPLETION, result_type="bytes", payload_argument=3, payload_bytes=16, length_offset=16)
    assert contract["completion"] == completion
    request = B.invocation_request(bound)
    assert B.validate_request(request) == request
    assert "signature_evidence" not in contract["original"]


@pytest.mark.parametrize("change", ["no_family", "old_family", "vector_return", "double_return", "extra_owned_argument",
    "unix_original", "wrong_self", "input_payload", "seeded_payload", "zero_capacity", "huge_capacity", "boolean_capacity", "nul_input"])
def test_owned_return_is_confined_to_its_fixed_compiler_transport(change):
    actor = B.bind_actor(_compiler_actor("cxx_owned_string_bytes"), {"label": "input"})
    entry = actor["arguments"][0]["value"]["callable"]
    if change == "no_family":
        actor.pop("compiler_adapter")
    elif change == "old_family":
        actor["compiler_adapter"]["family"] = "cxx_owned_string_vector_double"
    elif change == "vector_return":
        entry["return_type"]["name"] = "std::vector<double>"
    elif change == "double_return":
        entry["return_type"] = "double"
    elif change == "extra_owned_argument":
        entry["argument_types"].append(copy.deepcopy(entry["argument_types"][0]))
    elif change == "unix_original":
        entry["abi"] = "unix64"
    elif change == "wrong_self":
        entry["context"] = "none"
    elif change == "input_payload":
        actor["arguments"][3]["type"]["direction"] = "in"
        actor["arguments"][3]["value"] = [0] * 16
    elif change == "seeded_payload":
        actor["arguments"][3]["value"] = [0] * 16
    elif change in {"zero_capacity", "huge_capacity", "boolean_capacity"}:
        actor["arguments"][3]["type"]["bytes"] = {"zero_capacity": 0, "huge_capacity": 16385, "boolean_capacity": True}[change]
    else:
        actor["arguments"][2]["value"] = "embedded\0input"
    with pytest.raises(ValueError):
        B.invocation_request(actor)


def test_owned_return_declaration_alone_never_authorizes_native_lowering_or_bare_resolution():
    actor = B.bind_actor(_compiler_actor("cxx_owned_string_bytes"), {"label": "input"})
    entry = actor["arguments"][0]["value"]["callable"]
    with pytest.raises(ValueError):
        B.callable_contract(entry)
    with pytest.raises(ValueError):
        B.validate_request({"operations": [{"op": "resolve_callable", "entry": entry}]})
    direct = {"kind": "native_function", "target": {"module": entry["module"], "symbol": entry["symbol"]},
              "abi": "default", "arguments": [], "return_type": entry["return_type"], "signature_evidence": entry["signature_evidence"]}
    with pytest.raises(ValueError):
        B.invocation_request(direct)


@pytest.mark.parametrize("change", ["address", "both", "code", "alias", "context", "ownership", "owned_result", "abi", "missing_evidence"])
def test_callable_recipe_never_lowers_a_guessed_abi_or_persists_a_native_address(change):
    entry = copy.deepcopy(_compiler_actor()["arguments"][0]["value"]["callable"])
    if change == "address":
        entry.pop("symbol"); entry["address"] = "0x1000"
    elif change == "both":
        entry["offset"] = "0x1000"
    elif change == "code":
        entry["source"] = "arbitrary native code"
    elif change == "alias":
        entry["alias"] = "n1"
    elif change == "context":
        entry["context"] = "cxx_this"
    elif change == "ownership":
        entry["argument_types"] = [{"kind": "cxx_owned", "name": "std::string", "library": "libstdc++", "abi_version": 1}]
    elif change == "owned_result":
        entry["return_type"] = {"kind": "cxx_owned", "name": "std::string", "library": "libc++", "abi_version": 1}
    elif change == "abi":
        entry["abi"] = "guessed_swift_async"
    else:
        entry.pop("signature_evidence")
    with pytest.raises(ValueError):
        B.validate_request({"operations": [{"op": "resolve_callable", "entry": entry}]})


@pytest.mark.parametrize("change", ["family", "context", "module", "status", "completion", "same_self", "capacity", "owned_layout"])
def test_compiler_adapter_rejects_contract_mismatches_before_worker(runtime, change):
    app, _, _, state, out = runtime
    actor = B.bind_actor(_compiler_actor("cxx_owned_string_vector_double"), {"label": "s", "count": "2", "vector": "00" * 16})
    if change == "family":
        actor["compiler_adapter"]["family"] = "arbitrary_source"
    elif change == "context":
        actor["arguments"][0]["value"]["callable"]["context"] = "none"
    elif change == "module":
        actor["target"]["module"] = copy.deepcopy(actor["arguments"][0]["value"]["callable"]["module"])
    elif change == "status":
        actor["return_type"] = "double"
    elif change == "completion":
        actor["arguments"][-1]["type"]["bytes"] = 16
    elif change == "same_self":
        actor["arguments"][1] = copy.deepcopy(actor["arguments"][0])
    elif change == "capacity":
        actor["arguments"][3]["value"] = 3
    else:
        actor["arguments"][0]["value"]["callable"]["argument_types"][0]["abi_version"] = 2
    with pytest.raises(ValueError):
        B.invoke(app, actor, out)
    assert state["calls"] == [] and not out.exists()


def test_late_bound_vector_count_exceeding_capacity_never_prepares_a_transaction(runtime):
    app, _, _, state, out = runtime
    actor = _compiler_actor("cxx_owned_string_vector_double")
    with pytest.raises(ValueError, match="capacity"):
        B.bind_actor(actor, {"label": "s", "count": "3", "vector": "00" * 16})
    assert state["calls"] == [] and not out.exists()


@pytest.fixture
def compiler_files(native_files, runtime, monkeypatch):
    _, getter, original, declarations = native_files
    helper = original.with_name("CompilerHelper.dylib")
    helper.write_bytes(_inert_macho(uuid="fedcba9876543210fedcba9876543210"))
    actor = B.bind_actor(_compiler_actor(), {"amount": "2.5", "enabled": "true"})
    entry = actor["arguments"][0]["value"]["callable"]
    entry["module"] = {"path": str(original), "sha256": R._file_hash(original)}
    entry["signature_evidence"].update(file=str(declarations))
    actor["arguments"][1]["value"]["getter"] = getter
    actor["target"]["module"] = {"path": str(helper), "sha256": R._file_hash(helper)}
    actor["signature_evidence"].update(file=str(declarations))
    R.write_json(declarations, {"original": B.callable_contract(entry), "getter": B._contract(getter), "outer": B._contract(actor)})
    for claim in (entry["signature_evidence"], getter["signature_evidence"], actor["signature_evidence"]):
        claim["sha256"] = R._file_hash(declarations)
    build = helper.with_name("compiler-build.json")
    artifacts = [{"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size, "roles": [role]}
                 for path, role in [(helper, "generated_helper"), (original, "original_module"), (declarations, "original_declaration")]]
    R.write_json(build, {"format": "inert_verifier_boundary_fixture", "contract": B.compiler_adapter_contract(actor), "artifacts": artifacts})
    evidence = actor["compiler_adapter"]["evidence"]
    evidence.update(file=str(build), sha256=R._file_hash(build))
    verified = []
    def verifier(claim):
        assert claim == evidence
        assert R._file_hash(build) == claim["sha256"]
        verified.append(copy.deepcopy(claim))
        rows = R.load_json(build)["artifacts"]
        pins = {row["file"]: {key: row[key] for key in ("file", "sha256", "bytes")} for row in rows}
        for path, pin in pins.items():
            assert R._file_hash(Path(path)) == pin["sha256"] and Path(path).stat().st_size == pin["bytes"]
        pins[str(build)] = {"file": str(build), "sha256": R._file_hash(build), "bytes": build.stat().st_size}
        return pins
    monkeypatch.setattr(B, "_compiler_resources", verifier)
    return actor, build, artifacts, verified


def test_compiler_provenance_is_verified_and_echoed_separately_from_native_image_pins(compiler_files, runtime):
    actor, build, artifacts, verified = compiler_files
    app, _, _, state, out = runtime
    receipt = B.invoke(app, actor, out)
    evidence = actor["compiler_adapter"]["evidence"]
    expected = {str(build): {"evidence": evidence, "artifacts": artifacts, "manifest_sha256": evidence["sha256"]}}
    assert receipt["status"] == "executed" and receipt["resource_review_required"] is False
    assert receipt["compiler_provenance"] == receipt["result"]["compiler_provenance"] == expected
    assert state["calls"][0]["config"]["compiler_provenance"] == expected and len(verified) >= 4
    assert receipt["native_resources"][actor["target"]["module"]["path"]]["macho_images"]
    assert B.adapter_identity().get("helper_policy") is None


def test_worker_cannot_drop_compiler_artifact_roles_even_after_confirmed_detachment(compiler_files, runtime):
    actor, _, _, _ = compiler_files
    app, _, _, state, out = runtime
    state["result_changes"]["compiler_provenance"] = {}
    receipt = B.invoke(app, actor, out)
    assert receipt["status"] == "incomplete" and receipt["resource_review_required"] is True
    assert receipt["actuated"] is None and "provenance" in receipt["error"]["message"]


def test_original_callable_must_match_its_reviewed_entry_before_worker(compiler_files, runtime):
    actor, _, _, _ = compiler_files
    app, _, _, state, out = runtime
    entry = copy.deepcopy(actor["arguments"][0]["value"]["callable"])
    entry["symbol"] = "unreviewed_entry"
    receipt = B.collect(app, out, {"operations": [{"op": "resolve_callable", "entry": entry}]})
    assert receipt["status"] == "incomplete" and receipt["actuated"] is False and state["calls"] == []
    assert "exact entry" in receipt["error"]["message"]


def test_compiler_build_artifact_drift_after_dispatch_never_becomes_verified(compiler_files, runtime):
    actor, build, _, _ = compiler_files
    app, _, _, state, out = runtime
    state["after_worker"] = lambda: build.write_text(build.read_text() + "\n")
    receipt = B.invoke(app, actor, out)
    assert receipt["status"] == "incomplete" and receipt["actuated"] is True
    assert receipt["resource_review_required"] is True and receipt["verified"] is False
    assert receipt["result"]["cleanup"] == {"script_unloaded": True, "detached": True}


@pytest.mark.parametrize("denial", ["missing", "mode", "wrong_app", "not_allowlisted", "inside_app"])
def test_explicit_helper_policy_is_required_and_bound_to_the_exact_owned_debug_target(compiler_files, runtime, monkeypatch, denial):
    actor, build, _, _ = compiler_files
    app, _, _, state, out = runtime
    helper = {"module": actor["target"]["module"], "build_evidence": actor["compiler_adapter"]["evidence"]}
    policy_file = app.parent / "helper-policy.json"
    policy = {"format": B.HELPER_POLICY, "target": {"bundle": str(app), "fingerprint": R.fingerprint(app), "scope": "owned_debug_target"},
              "helpers": [copy.deepcopy(helper)]}
    if denial == "wrong_app":
        policy["target"]["fingerprint"]["sha256"] = "f" * 64
    elif denial == "not_allowlisted":
        policy["helpers"][0]["module"]["sha256"] = "f" * 64
    elif denial == "inside_app":
        policy_file = app / "Contents/helper-policy.json"
    R.write_json(policy_file, policy); policy_file.chmod(0o644 if denial == "mode" else 0o600)
    if denial != "missing":
        monkeypatch.setenv("ANYUSE_REA_RUNTIME_HELPER_POLICY", str(policy_file))
    receipt = B.collect(app, out, {"operations": [{"op": "load_helper", **helper}]})
    assert receipt["status"] == "unavailable" and receipt["actuated"] is False
    assert state["calls"] == [] and receipt["resource_review_required"] is False


def test_explicit_helper_load_pins_policy_build_and_constructor_attempt_disposition(compiler_files, runtime, monkeypatch):
    actor, build, _, _ = compiler_files
    app, _, _, state, out = runtime
    helper = {"module": actor["target"]["module"], "build_evidence": actor["compiler_adapter"]["evidence"]}
    policy_file = app.parent / "helper-policy.json"
    R.write_json(policy_file, {"format": B.HELPER_POLICY,
        "target": {"bundle": str(app), "fingerprint": R.fingerprint(app), "scope": "owned_debug_target"}, "helpers": [helper]})
    policy_file.chmod(0o600); monkeypatch.setenv("ANYUSE_REA_RUNTIME_HELPER_POLICY", str(policy_file))
    receipt = B.collect(app, out, {"operations": [{"op": "load_helper", **helper}]})
    assert receipt["status"] == "executed" and receipt["actuated"] is True
    assert receipt["result"]["helper_load_attempts"] == 1 and receipt["result"]["invocations"] == 0
    assert receipt["adapter"]["helper_policy"]["sha256"] == R._file_hash(policy_file)
    assert str(build) in receipt["compiler_provenance"]


@pytest.mark.parametrize("attempts", [None, "1", True, -1, 2])
def test_unknown_or_malformed_loader_attempts_require_review_even_with_clean_detach(compiler_files, runtime, monkeypatch, attempts):
    actor, _, _, _ = compiler_files
    app, _, _, state, out = runtime
    helper = {"module": actor["target"]["module"], "build_evidence": actor["compiler_adapter"]["evidence"]}
    policy_file = app.parent / "helper-policy.json"
    R.write_json(policy_file, {"format": B.HELPER_POLICY,
        "target": {"bundle": str(app), "fingerprint": R.fingerprint(app), "scope": "owned_debug_target"}, "helpers": [helper]})
    policy_file.chmod(0o600); monkeypatch.setenv("ANYUSE_REA_RUNTIME_HELPER_POLICY", str(policy_file))
    state["result_changes"].update(helper_load_attempts=attempts, status="incomplete")
    receipt = B.collect(app, out, {"operations": [{"op": "load_helper", **helper}]})
    assert receipt["status"] == "incomplete" and receipt["actuated"] is None and receipt["resource_review_required"] is True


def test_fixed_compiler_js_keeps_entry_resolution_data_only_and_stops_failed_sequences():
    node = shutil.which("node")
    if node is None:
        pytest.skip("inert compiler adapter fixture requires the guest's existing Node")
    source = _inert_runtime_js_source()
    actor = B.bind_actor(_compiler_actor(), {"amount": "2.5", "enabled": "true"})
    main = "/fixture/Args.app/Contents/MacOS/Args"
    actor["arguments"][0]["value"]["callable"]["module"]["path"] = main
    actor["arguments"][1]["value"]["getter"]["target"]["module"]["path"] = main
    bytes_actor = B.bind_actor(_compiler_actor("cxx_owned_string_bytes"), {"label": "input Grüße"})
    bytes_actor["arguments"][0]["value"]["callable"]["module"]["path"] = main
    bytes_actor["arguments"][1]["value"]["getter"]["target"]["module"]["path"] = main
    fixture = r"""
const fs=require('fs'),vm=require('vm'),data=JSON.parse(fs.readFileSync(0,'utf8'));
const main='/fixture/Args.app/Contents/MacOS/Args',helper='/fixture/CompilerHelper.dylib';
const uuid='0123456789abcdef0123456789abcdef',helperUUID='fedcba9876543210fedcba9876543210';
const memory=new Map([[0x1000,Buffer.alloc(4096)],[0x3000,Buffer.alloc(32)],[0x8000,Buffer.alloc(4096)]]);
function header(base,id){
 const b=memory.get(base);[0xfeedfacf,0x0100000c,0,6,2,176,0,0].forEach((v,i)=>b.writeUInt32LE(v,i*4));
 b.writeUInt32LE(0x1b,32);b.writeUInt32LE(24,36);Buffer.from(id,'hex').copy(b,40);
 b.writeUInt32LE(0x19,56);b.writeUInt32LE(152,60);b.write('__TEXT',64);
 b.writeBigUInt64LE(0n,80);b.writeBigUInt64LE(4096n,88);b.writeBigUInt64LE(0n,96);b.writeBigUInt64LE(4096n,104);
 b.writeUInt32LE(5,112);b.writeUInt32LE(5,116);b.writeUInt32LE(1,120);
 b.write('__text',128);b.write('__TEXT',144);b.writeBigUInt64LE(0x400n,160);b.writeBigUInt64LE(0x200n,168);
 b.writeUInt32LE(0x400,176);b.writeUInt32LE(0x80000400,192);
}
header(0x1000,uuid);header(0x8000,helperUUID);
function backing(address,size){for(const[base,bytes]of memory)if(address>=base&&address+size<=base+bytes.length)return[bytes,address-base];throw Error('unmapped inert address');}
class Pointer{
 constructor(v){this.address=Number(v);}add(v){return new Pointer(this.address+Number(v));}
 sub(v){return new Pointer(this.address-(v instanceof Pointer?v.address:Number(v)));}
 compare(v){return this.address<v.address?-1:this.address>v.address?1:0;}isNull(){return this.address===0;}
 toString(){return'0x'+this.address.toString(16);}readU32(){const[b,o]=backing(this.address,4);return b.readUInt32LE(o);}
 readU64(){const[b,o]=backing(this.address,8);return{toString:()=>b.readBigUInt64LE(o).toString()};}
 readByteArray(n){const[b,o]=backing(this.address,n),v=b.subarray(o,o+n);return v.buffer.slice(v.byteOffset,v.byteOffset+v.byteLength);}
 readVolatile(n){return this.readByteArray(n);}writeByteArray(v){const[b,o]=backing(this.address,v.length);Buffer.from(v).copy(b,o);}
}
let allocation=0x10000,helperPresent=true,mode='success',loaderMode='success',loads=0,laterCalls=0,originalWrappers=0,swiftWrappers=0,expectedEnabled=1;
const nativeBools=[],objcBools=[];
const Memory={alloc(n){const p=new Pointer(allocation);allocation+=Math.max(256,n);memory.set(p.address,Buffer.alloc(n,0xa5));return p;},
 allocUtf8String(t){const b=Buffer.from(t+'\0');const p=this.alloc(b.length);p.writeByteArray(b);return p;}};
function module(path,base,names){return{path,name:path.split('/').pop(),base:new Pointer(base),size:4096,
 findSymbolByName(n){return names[n]===undefined?null:new Pointer(names[n]);},findExportByName:()=>null};}
const original=module(main,0x1000,{borrowed_root:0x1400,private_entry:0x1440,later:0x1480,data_entry:0x1700});
const adapter=module(helper,0x8000,{rea_compiler_adapter:0x8400});
const Process={id:321,platform:'darwin',arch:'arm64',pointerSize:8,mainModule:original,
 enumerateModules:()=>helperPresent?[original,adapter]:[original],
 findModuleByAddress(p){return p.address>=0x1000&&p.address<0x2000?original:p.address>=0x8000&&p.address<0x9000?adapter:null;},
 findRangeByAddress(p){for(const[base,bytes]of memory)if(p.address>=base&&p.address<base+bytes.length)return{base:new Pointer(base),size:bytes.length,protection:base===0x1000||base===0x8000?'r-x':'rw-'};return null;}};
const Module={load(path){loads++;if(path!==helper)throw Error('wrong requested helper');
 if(loaderMode==='throw')throw Error('constructor failed after possible native effects');
 helperPresent=true;return loaderMode==='wrong_path'?{path:'/fixture/unexpected.dylib'}:adapter;}};
function NativeFunction(address,ret,args,options){
 if(options.exceptions!=='steal')throw Error('exception policy missing');
 if(address.address===0x1440){originalWrappers++;throw Error('original unsupported Swift ABI was constructed');}
 if(address.address===0x1400)return()=>new Pointer(0x3000);
 if(address.address===0x1480)return()=>{laterCalls++;return 0;};
 if(address.address!==0x8400||ret!=='uint32')throw Error('incorrect helper C ABI');
 if(args.join(',')==='pointer,pointer,pointer,pointer,pointer')return(entry,self,text,payload,out)=>{
  if(entry.address!==0x1440||self.address!==0x3000)throw Error('original byte entry/context mismatch');
  const[input,offset]=backing(text.address,1);
  if(input.subarray(offset,input.indexOf(0,offset)).toString('utf8')!=='input Grüße')throw Error('incorrect UTF8 argument');
  if(Array.from(new Uint8Array(out.readVolatile(24))).some(v=>v!==0)||
     Array.from(new Uint8Array(payload.readVolatile(16))).some(v=>v!==0))throw Error('byte storage was not zeroed');
  const b=Buffer.alloc(24),p=Buffer.alloc(16);let status=0,attempt=1,uncertain=0,length=4,reserved=0,tail=0;
  Buffer.from([65,0,255,66]).copy(p);
  if(mode==='bytes_empty'){length=0;p.fill(0);}
  if(mode==='bytes_exact'){length=16;for(let i=0;i<16;i++)p[i]=i;}
  if(mode==='bytes_refusal'){status=1;attempt=0;length=0;p.fill(0);}
  if(mode==='bytes_caught'||mode==='bytes_caught_before'){status=2;uncertain=1;length=0;p.fill(0);if(mode==='bytes_caught_before')attempt=0;}
  if(mode==='bytes_oversize'){status=3;uncertain=1;length=0;p.fill(0);}
  if(mode==='bytes_padding')p[15]=1;
  if(mode==='bytes_truncated')length=17;
  if(mode==='bytes_tail')tail=1;
  if(mode==='bytes_reserved')reserved=1;
  if(mode==='bytes_failed_payload'){status=1;attempt=0;length=0;p.fill(0);p[0]=1;}
  if(mode==='bytes_no_dispatch')attempt=0;
  if(mode==='bytes_bad_flags')uncertain=2;
  if(mode==='bytes_unknown_status')status=4;
  const returned=mode==='bytes_return_mismatch'?1:status;
  b.writeUInt32LE(status,0);b.writeUInt32LE(attempt,4);b.writeUInt32LE(uncertain,8);b.writeUInt32LE(reserved,12);
  b.writeUInt32LE(length,16);b.writeUInt32LE(tail,20);payload.writeByteArray(p);out.writeByteArray(b);return returned;
 };
 if(args.join(',')!=='pointer,pointer,double,bool,pointer')throw Error('incorrect helper C ABI');
 return(entry,self,amount,enabled,out)=>{
  if(typeof enabled!=='number'||![0,1].includes(enabled))throw Error('expected an integer C bool');
  if(entry.address!==0x1440||self.address!==0x3000||amount!==2.5||enabled!==expectedEnabled)throw Error('original entry/context/data mismatch');
  nativeBools.push(enabled);
  if(Array.from(new Uint8Array(out.readVolatile(24))).some(v=>v!==0))throw Error('completion storage was not zeroed');
  const b=Buffer.alloc(24);let status=0,attempt=1,uncertain=0,reserved=0,value=5.25,returned;
  if(mode==='refusal'){status=1;attempt=0;}
  if(mode==='caught'){status=2;uncertain=1;}
  if(mode==='caught_before_dispatch'){status=2;attempt=0;uncertain=1;}
  if(mode==='nonfinite'){status=3;uncertain=1;value=Infinity;}
  if(mode==='bad_flags')attempt=2;if(mode==='reserved')reserved=1;
  if(mode==='infinite_success')value=Infinity;if(mode==='no_dispatch')attempt=0;
  returned=mode==='return_mismatch'?1:status;
  b.writeUInt32LE(status,0);b.writeUInt32LE(attempt,4);b.writeUInt32LE(uncertain,8);b.writeUInt32LE(reserved,12);b.writeDoubleLE(value,16);
  out.writeByteArray(b);return returned;
 };
}
function setEnabled(value){if(typeof value!=='boolean')throw Error('ObjC bool conversion was changed');objcBools.push(value);}
setEnabled.types='v24@0:8B16';setEnabled.implementation=new Pointer(0x14c0);
const toggle={$kind:'class',$className:'Toggle',$methods:['+ setEnabled:'],'+ setEnabled:':setEnabled};
const ObjC={available:true,mainQueue:'main',schedule:(_q,f)=>f(),classes:{Toggle:toggle}};
const Swift={available:true,NativeFunction(){swiftWrappers++;throw Error('Swift FP/BOOL wrapper must not be constructed');}};
const context=vm.createContext({ObjC,Swift,TargetStructMetadata:class{},Memory,Process,Module,NativeFunction,rpc:{exports:{}},send:()=>{},ptr:v=>new Pointer(v),
 int64:v=>({toString:()=>v}),uint64:v=>({toString:()=>v}),Date,Map,Set,Promise,Error,Uint8Array});
vm.runInContext(data.source,context,{timeout:1000});
function pin(path,sha,id){const identity={cpu_type:0x0100000c,cpu_subtype:0,uuid:id};return{file:path,sha256:sha,bytes:4096,
 macho_images:[identity],macho_sections:[{image_identity:identity,sections:[{segment:'__TEXT',name:'__text',offset:'0x400',bytes:0x200,flags:0x80000400}]}],
 macho_segments:[{image_identity:identity,segments:[{segment:'__TEXT',offset:'0x0',bytes:4096,file_offset:0,file_bytes:4096,max_protection:5,initial_protection:5}]}]};}
const pins={[main]:pin(main,'a'.repeat(64),uuid),[helper]:pin(helper,'c'.repeat(64),helperUUID),
 '/fixture/declarations.json':{file:'/fixture/declarations.json',sha256:'b'.repeat(64),bytes:100},
 '/fixture/build.json':{file:'/fixture/build.json',sha256:'d'.repeat(64),bytes:100}};
const actor=data.actor,entry=actor.arguments[0].value.callable;
function operation(a){const{kind,...rest}=a;return{op:'native_invoke',...rest};}
const later={kind:'native_function',target:{module:entry.module,symbol:'later'},abi:'default',arguments:[],return_type:'uint32',signature_evidence:actor.signature_evidence};
const helperRequest={op:'load_helper',module:actor.target.module,build_evidence:actor.compiler_adapter.evidence};
const target={bundle_id:'org.fixture.Args',sha256:'f'.repeat(64)};
const policy={file:'/fixture/operator-policy.json',sha256:'e'.repeat(64),mode:0o600,policy:{format:'anyuse.rea-runtime-helper-policy/1',
 target:{bundle:'/fixture/Args.app',fingerprint:target,scope:'owned_debug_target'},helpers:[{module:helperRequest.module,build_evidence:helperRequest.build_evidence}]}};
async function run(operations,resources=pins,helperPolicy=null){return await context.rpc.exports.run({request:{operations},native_resources:resources,
 deadline_ms:Date.now()+3000,pid:321,bundle:'/fixture/Args.app',target,executable:main,helper_policy:helperPolicy});}
(async()=>{
 const resolved=await run([{op:'resolve_callable',entry}]);
 const dataEntry=JSON.parse(JSON.stringify(entry));dataEntry.symbol='data_entry';
 const noninstruction=await run([{op:'resolve_callable',entry:dataEntry}]);
 const stalePins=JSON.parse(JSON.stringify(pins));stalePins[main].macho_images[0].uuid='0'.repeat(32);
 const stale=await run([{op:'resolve_callable',entry}],stalePins);
 const offsetEntry=JSON.parse(JSON.stringify(entry));delete offsetEntry.symbol;offsetEntry.offset='0x440';
 const offset=await run([{op:'resolve_callable',entry:offsetEntry}]);
 const success=await run([operation(actor),operation(later)]);
 const falseActor=JSON.parse(JSON.stringify(actor));falseActor.arguments[3].value=false;expectedEnabled=0;
 const falseBool=await run([operation(falseActor)]);expectedEnabled=1;
 const objcBool=await run([true,false].map(value=>({op:'invoke',receiver:{class:'Toggle',kind:'class'},selector:'setEnabled:',
  signature:'v24@0:8B16',arguments:[{type:'bool',value}],return_type:'void'})));
 const swiftBool=await run([{op:'native_invoke',target:{module:entry.module,symbol:'private_entry'},abi:'swift',
  arguments:[{type:'bool',value:true}],return_type:'uint32',signature_evidence:actor.signature_evidence}]);
 const failures={};
 for(const failure of['refusal','caught','caught_before_dispatch','nonfinite','bad_flags','reserved','return_mismatch','infinite_success','no_dispatch']){
  mode=failure;const before=laterCalls;const result=await run([operation(actor),operation(later)]);failures[failure]={result,later_delta:laterCalls-before};
 }
 const bytesSuccess={};
 for(const value of['bytes_success','bytes_empty','bytes_exact']){mode=value;bytesSuccess[value]=await run([operation(data.bytes_actor)]);}
 const bytesFailures={};
 for(const failure of['bytes_refusal','bytes_caught','bytes_caught_before','bytes_oversize','bytes_padding','bytes_truncated',
  'bytes_tail','bytes_reserved','bytes_failed_payload','bytes_no_dispatch','bytes_bad_flags','bytes_unknown_status','bytes_return_mismatch']){
  mode=failure;const before=laterCalls;const result=await run([operation(data.bytes_actor),operation(later)]);
  bytesFailures[failure]={result,later_delta:laterCalls-before};
 }
 mode='success';helperPresent=false;
 const unloaded=await run([operation(actor)]);const noPolicy=await run([helperRequest,operation(later)]);
 const wrongCPU=JSON.parse(JSON.stringify(pins));wrongCPU[helper].macho_images[0].cpu_type=0x01000007;
 const cpuRefused=await run([helperRequest],wrongCPU,policy);
 const loaded=await run([helperRequest],pins,policy),acknowledged=await run([helperRequest],pins,policy);
 const loaderFailures={};
 for(const failure of['throw','wrong_path']){
  helperPresent=false;loaderMode=failure;const before=laterCalls;const result=await run([helperRequest,operation(later)],pins,policy);
  loaderFailures[failure]={result,later_delta:laterCalls-before};
 }
 console.log(JSON.stringify({resolved,noninstruction,stale,offset,success,falseBool,objcBool,swiftBool,failures,bytesSuccess,bytesFailures,unloaded,noPolicy,cpuRefused,loaded,acknowledged,loaderFailures,
  laterCalls,loads,originalWrappers,swiftWrappers,nativeBools,objcBools}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    run = subprocess.run([node, "-e", fixture], input=json.dumps({"source": source, "actor": actor, "bytes_actor": bytes_actor}),
                         capture_output=True, text=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout)
    for name in ("resolved", "offset"):
        resolved = results[name]
        assert resolved["status"] == "completed" and resolved["native_calls"] == resolved["invocations"] == 0
        assert resolved["callable_resolutions"] == 1 and resolved["handles_expired"] is True
        observation = resolved["operations"][0]["callable"]
        assert observation["wrapper_constructed"] is False and observation["original_dispatched"] is False
        assert observation["pointer"]["alias"] is None and observation["pointer"]["expires"] == "transaction"
        assert observation["entry"]["implementation_offset"] == "0x440"
    for name in ("noninstruction", "stale", "unloaded", "noPolicy", "cpuRefused"):
        result = results[name]
        assert result["status"] == "incomplete" and result["native_calls"] == result["invocations"] == 0
        assert result["helper_load_attempts"] == 0 and result["native_state_uncertain"] is False
    success = results["success"]
    assert success["status"] == "completed" and success["invocations"] == 2
    row = success["operations"][0]
    assert row["function"]["module"] == "/fixture/CompilerHelper.dylib"
    assert row["callable_entries"][0]["entry"]["module"] == main
    assert row["compiler_completion"]["dispatch_attempted"] is True
    assert row["compiler_completion"]["original_body_entry_proved"] is False and row["compiler_completion"]["value"] == 5.25
    assert results["falseBool"]["status"] == results["objcBool"]["status"] == "completed"
    assert results["nativeBools"][:2] == [1, 0] and all(type(value) is int and value in {0, 1} for value in results["nativeBools"])
    assert results["objcBools"] == [True, False] and all(type(value) is bool for value in results["objcBools"])
    assert results["swiftBool"]["status"] == "incomplete" and results["swiftBool"]["native_calls"] == results["swiftBool"]["invocations"] == 0
    for name, failure in results["failures"].items():
        result = failure["result"]
        assert result["status"] == "incomplete" and result["invocations"] == 1 and len(result["operations"]) == 1
        assert result["handles_expired"] is True and failure["later_delta"] == 0
        assert result["native_state_uncertain"] is (name != "refusal")
        assert result["operations"][0]["compiler_completion"]["status"] != 0 or result["operations"][0]["compiler_completion"]["valid"] is False
    for name, expected in {"bytes_success": [65, 0, 255, 66], "bytes_empty": [], "bytes_exact": list(range(16))}.items():
        result = results["bytesSuccess"][name]
        assert result["status"] == "completed" and result["invocations"] == 1 and result["native_state_uncertain"] is False
        row = result["operations"][0]
        completion = row["compiler_completion"]
        assert completion["format"] == B.COMPILER_BYTES_COMPLETION and completion["result_type"] == "bytes"
        assert completion["length"] == len(expected) and completion["value"] == expected
        assert completion["payload_bytes"] == 16 and completion["padding_zero"] is True and completion["valid"] is True
        assert completion["original_body_entry_proved"] is False
        assert row["out_buffers"][0]["bytes"] == expected + [0] * (16 - len(expected))
    for name, failure in results["bytesFailures"].items():
        result = failure["result"]
        assert result["status"] == "incomplete" and result["invocations"] == 1 and len(result["operations"]) == 1
        assert result["handles_expired"] is True and failure["later_delta"] == 0
        assert result["native_state_uncertain"] is (name != "bytes_refusal")
        completion = result["operations"][0]["compiler_completion"]
        assert completion["status"] != 0 or completion["valid"] is False
        if name == "bytes_truncated":
            assert completion["value"] is None and completion["valid"] is False
    assert results["loaded"]["status"] == "completed" and results["loaded"]["helper_load_attempts"] == 1
    assert results["loaded"]["helper_residency"][0]["remains_loaded"] is True
    assert results["acknowledged"]["helper_load_attempts"] == 0 and results["acknowledged"]["native_calls"] == 0
    assert results["acknowledged"]["operations"][0]["helper"]["already_loaded"] is True
    for failure in results["loaderFailures"].values():
        result = failure["result"]
        assert result["status"] == "incomplete" and result["native_state_uncertain"] is True
        assert result["helper_load_attempts"] == 1 and result["helper_residency"][0]["remains_loaded"] is None
        assert result["invocations"] == 0 and failure["later_delta"] == 0 and result["handles_expired"] is True
    assert results["originalWrappers"] == results["swiftWrappers"] == 0 and results["loads"] == 3 and results["laterCalls"] == 1


def test_swift_import_is_lazy_and_initialization_fault_preempts_the_whole_transaction():
    """Real ES module imports with inert exports; no native process or Frida."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("lazy module fixture requires the guest's existing Node")
    source = Path(B.__file__).with_name("rea_runtime_agent.js").read_text()
    assert "import Swift from 'frida-swift-bridge';" not in source
    assert "import { TargetStructMetadata }" not in source
    source = source.replace("import ObjC from 'frida-objc-bridge';", "", 1)
    native = {
        "op": "native_invoke", "target": {"module": {"path": "/fixture/App", "sha256": "a" * 64},
                                        "symbol": "entry"}, "abi": "default",
        "arguments": [], "return_type": "void",
        "signature_evidence": {"kind": "reviewed_contract", "file": "/fixture/contract.json",
                               "sha256": "b" * 64, "pointer": "/call"}}
    swift = {**copy.deepcopy(native), "abi": "swift"}
    getter = {**copy.deepcopy(swift), "return_type": "pointer"}
    getter.pop("op")
    swift_read = {"receiver": {"class": "SwiftModel", "kind": "unique"},
                  "receiver_class": "SwiftModel", "swift_class": {"module": "App", "name": "SwiftModel"},
                  "module": native["target"]["module"],
                  "field": {"name": "context", "offset": 8, "type_name": "Swift.OpaquePointer"},
                  "evidence": native["signature_evidence"]}
    nested = {**copy.deepcopy(native), "arguments": [{"type": "pointer", "value": {"getter": getter}}]}
    reflected = {**copy.deepcopy(native), "arguments": [{"type": "pointer", "value": {"swift_read": swift_read}}]}
    pod = {**copy.deepcopy(native), "arguments": [{
        "type": {"kind": "struct", "fields": [{"name": "root", "type": "pointer"}]},
        "value": {"root": {"getter": getter}}}]}
    swift_pod = {**copy.deepcopy(swift), "arguments": [{
        "type": {"kind": "swift_type", "module": "App", "name": "Pair", "bytes": 16, "fields": []},
        "value": {"getter": getter}}]}
    context = {**copy.deepcopy(native), "context": {"type": "pointer", "value": {"getter": getter}}}
    compiler = B.invocation_request(B.bind_actor(_compiler_actor(), {"amount": "2.5", "enabled": "true"}))["operations"][0]
    cases = [
        {"name": "objc_metadata", "request": {"operations": [{"op": "classes", "owner": "all"}]}, "required": False},
        {"name": "objc_read", "request": {"operations": [{"op": "read", "receiver": {"class": "Absent", "kind": "unique"}, "fields": []}]}, "required": False},
        {"name": "default_c", "request": {"operations": [native]}, "required": False},
        {"name": "compiler_swift_declaration", "request": {"operations": [compiler]}, "required": False},
        {"name": "resolve_swift_declaration", "request": {"operations": [{"op": "resolve_callable", "entry": compiler["arguments"][0]["value"]["callable"]}]}, "required": False},
        {"name": "native_swift", "request": {"operations": [native, swift]}, "required": True},
        {"name": "nested_swift_getter", "request": {"operations": [nested]}, "required": True},
        {"name": "reflected_pointer", "request": {"operations": [reflected]}, "required": True},
        {"name": "nested_pod_pointer", "request": {"operations": [pod]}, "required": True},
        {"name": "swift_pod", "request": {"operations": [swift_pod]}, "required": True},
        {"name": "swift_context_getter", "request": {"operations": [context]}, "required": True},
        {"name": "native_read", "request": {"operations": [{"op": "native_read", "receiver": {"native": getter}, "bytes": 8, "fields": []}]}, "required": True},
        {"name": "native_graph", "request": {"operations": [{"op": "graph", "root": {"native": getter}, "types": [], "max_nodes": 1}]}, "required": True},
        {"name": "swift_inventory", "request": {"operations": [{"op": "swift_types"}]}, "required": True},
        {"name": "swift_reflection", "request": {"operations": [{"op": "swift_fields"}]}, "required": True},
        {"name": "swift_read", "request": {"operations": [{"op": "swift_read", "read": swift_read}]}, "required": True},
    ]
    fixture = r"""
const fs=require('fs'), vm=require('vm'), data=JSON.parse(fs.readFileSync(0,'utf8'));
async function trial(request, reject) {
 const imports=[],events=[];let scheduled=0,constructed=0;
 const ObjC={available:true,classes:{},mainQueue:'main',schedule:(_q,fn)=>{scheduled++;fn();}};
 const context=vm.createContext({ObjC,rpc:{exports:{}},send:event=>events.push(event),
  Process:{id:321,platform:'darwin',arch:'arm64',pointerSize:8,mainModule:{path:'/fixture/App'}},
  NativeFunction:()=>{constructed++;throw Error('no native wrapper expected in import fixture');},
  fixtureSwift:{available:false}});
 const module=new vm.SourceTextModule(data.source,{context,
  importModuleDynamically:async specifier=>{
   imports.push(specifier);
   if((reject==='bridge'&&specifier==='frida-swift-bridge')||
      (reject==='metadata'&&specifier.endsWith('/metadata.js')))
    throw Error('controlled metadata access violation');
   const body=specifier==='frida-swift-bridge'
    ? 'export default globalThis.fixtureSwift;'
    : 'export class TargetStructMetadata {}';
   const dependency=new vm.SourceTextModule(body,{context});
   await dependency.link(()=>{throw Error('unexpected dependency import');});
   await dependency.evaluate();return dependency;
  }});
 await module.link(()=>{throw Error('Swift must not be statically linked or evaluated');});
 await module.evaluate();
 if(typeof context.rpc.exports.run!=='function')throw Error('RPC export unavailable before lazy import');
 const result=await context.rpc.exports.run({request,pid:321,executable:'/fixture/App',bundle:'/fixture',
  native_resources:{},deadline_ms:Date.now()+3000});
 return{result,imports,events,scheduled,constructed};
}
(async()=>{
 const rows=[];
 for(const test of data.cases)for(const reject of ['bridge','metadata'])
  rows.push({name:test.name,required:test.required,reject,...await trial(test.request,reject)});
 const available=await trial({operations:[{op:'swift_types'}]},null);
 console.log(JSON.stringify({rows,available}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    executed = subprocess.run([node, "--experimental-vm-modules", "-e", fixture],
        input=json.dumps({"source": source, "cases": cases}), capture_output=True, text=True, timeout=10, check=False)
    assert executed.returncode == 0, executed.stderr
    output = json.loads(executed.stdout)
    for row in output["rows"]:
        result = row["result"]
        assert result["native_calls"] == result["invocations"] == row["constructed"] == 0
        assert result["native_state_uncertain"] is False and result["handles_expired"] is True
        if row["required"]:
            assert row["scheduled"] == 0 and result["operations"] == []
            assert result["status"] == "incomplete" and result["error"]["stage"] == "swift_initialization"
            assert result["error"]["message"] == "controlled metadata access violation"
            assert row["imports"] == (["frida-swift-bridge"] if row["reject"] == "bridge" else [
                "frida-swift-bridge", "frida-swift-bridge/dist/abi/metadata.js"])
        else:
            assert row["imports"] == [] and row["scheduled"] == 1
            assert not result["error"] or result["error"].get("stage") != "swift_initialization"
            if row["name"] == "objc_metadata":
                assert result["status"] == "completed"
    available = output["available"]
    assert available["imports"] == ["frida-swift-bridge", "frida-swift-bridge/dist/abi/metadata.js"]
    assert available["scheduled"] == 1 and available["result"]["status"] == "completed"
    assert available["result"]["operations"][0]["unsupported"] and available["result"]["native_calls"] == 0


def _inert_macho_with_data():
    """Inert original headers with separate instruction and data declarations."""
    original = _inert_macho()
    command = struct.pack("<II16sQQQQIIII", 0x19, 152, b"__DATA", 0x2000, 0x1000, 0x2000, 0x20, 3, 3, 1, 0)
    section = struct.pack("<16s16sQQIIIIIIII", b"__const", b"__DATA", 0x2000, 0x20, 0x2000, 3, 0, 0, 0, 0, 0, 0)
    header = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 6, 3, 24 + 152 + 152, 0, 0)
    return (header + original[32:32 + 24 + 152] + command + section).ljust(0x2020, b"\0")


def _native_path_files(native_files, *, arguments=None, root_kind="getter", return_type="int32",
                       runtime_identity=False, identity_offset="0x2000", receiver_path=None,
                       data_mode="object", data_offset="0x2000", data_module=None, borrowed_root=False):
    """Prospectively prepare inert original/ABI/review contracts, never task proof.

    arguments is the scalar tail after the automatically supplied path pointer.
    H uses the same factory with its own scalar input fixture and real resource
    collector, so publication checks exercise transitive files without mocks.
    """
    from test_rea_native_path import _path_actor, _borrowed_root_actor
    P = B.NP
    base, getter, module, abi_file = native_files
    if borrowed_root and root_kind != "getter":
        raise ValueError("inert borrowed fixture requires its native getter root")
    actor, recipe = _borrowed_root_actor() if borrowed_root else _path_actor()
    getter = copy.deepcopy(getter)
    if borrowed_root or runtime_identity or root_kind == "data" and data_module is None:
        module.write_bytes(_inert_macho_with_data())
    module_claim = {"path": str(module), "sha256": R._file_hash(module)}
    actor["target"] = {**copy.deepcopy(base["target"]), "module": copy.deepcopy(module_claim)}
    actor["return_type"] = return_type
    if arguments is not None:
        actor["arguments"] = [actor["arguments"][0], *copy.deepcopy(arguments)]
    getter["target"]["module"] = copy.deepcopy(module_claim)
    getter["arguments"] = []
    getter["return_type"] = "pointer"
    call = recipe["consumer"]["call"]
    for key in ("symbol", "offset"):
        call.pop(key, None)
    call.update(**{key: copy.deepcopy(value) for key, value in actor["target"].items()},
                argument_types=[argument["type"] for argument in actor["arguments"]], return_type=return_type)
    for layout in recipe["types"]:
        layout["owner_image"] = copy.deepcopy(module_claim)
    if borrowed_root:
        recipe["types"][0]["runtime_identity"].update(image=copy.deepcopy(module_claim), data_offset=identity_offset)
    elif runtime_identity:
        recipe["types"][-1]["fields"].append({"name": "vptr", "type": "pointer", "offset": 8})
        recipe["types"][-1]["runtime_identity"] = {"kind": "pinned_data_vptr", "field": "vptr",
                                                     "image": copy.deepcopy(module_claim), "data_offset": identity_offset}

    parent = module.parent
    files = {"module": module, "abi": abi_file,
             "getter_abi": parent / "native-path-root-abi.json", "consumer_abi": parent / "native-path-consumer-abi.json",
             "contracts": parent / "native-path-contracts.json", "header": parent / "native-path-original.h",
             "build": parent / "native-path-original-build.json", "review": parent / "native-path-review.json"}
    abi_evidence = {"kind": "reviewed_contract", "file": str(abi_file), "sha256": "0" * 64, "pointer": "/call"}
    actor["signature_evidence"] = copy.deepcopy(abi_evidence)
    getter["signature_evidence"] = {**abi_evidence, "file": str(files["getter_abi"]), "pointer": "/getter"}
    call["signature_evidence"] = {**abi_evidence, "file": str(files["consumer_abi"]), "pointer": "/consumer"}
    recipe["root"] = {"getter": getter}
    root_body = {"getter": B._contract(getter)}
    if root_kind == "objc":
        recipe["root"] = {"objc": {"class": "OpaqueController", "kind": "unique", "subclasses": False}}
    elif root_kind == "data":
        data_module = data_module if data_module is not None else module
        files["data_module"] = data_module
        recipe["root"] = {"data": {"module": {"path": str(data_module), "sha256": R._file_hash(data_module)},
                                   "offset": data_offset, "mode": data_mode}}
    elif root_kind in {"read", "swift_read"}:
        root_read = (_ivar_pointer_read if root_kind == "read" else _swift_pointer_read)(module_claim, getter["signature_evidence"])
        recipe["root"] = {root_kind: root_read}
        root_body["ivar" if root_kind == "read" else "swift_read"] = (
            B._pointer_read_contract(root_read) if root_kind == "read" else B._swift_read_contract(root_read))
    if root_kind not in {"getter", "data"}:
        recipe["lifetime"]["mode"] = "retained_owner_main_queue_stable"
        receiver = recipe["root"]["objc"] if root_kind == "objc" else recipe["root"][root_kind]["receiver"]
        if receiver_path is not None:
            receiver["path"] = copy.deepcopy(receiver_path)
    R.write_json(abi_file, {"call": B._contract(actor)})
    R.write_json(files["getter_abi"], root_body)
    R.write_json(files["consumer_abi"], {"consumer": B._callable_contract(call)})
    for evidence in [actor["signature_evidence"], getter["signature_evidence"], call["signature_evidence"]]:
        evidence["sha256"] = R._file_hash(Path(evidence["file"]))
    if root_kind in {"read", "swift_read"}:
        recipe["root"][root_kind]["evidence"]["sha256"] = R._file_hash(files["getter_abi"])

    data_declaration = ""
    if root_kind == "data":
        data_declaration = "extern Owner static_owner;\n" if data_mode == "object" else "extern Owner *static_owner;\n"
    declaration = ("struct Node { void *vptr; unsigned generation; int payload; };\n" if borrowed_root else
                   "struct Node; struct Owner { bool ready; Node *child; }; struct Node { unsigned generation; int payload; };\n")
    files["header"].write_text("// Inert test declaration; not vendor/source evidence.\n" + declaration + data_declaration)
    R.write_json(files["build"], {"fixture": True, "original_image": module_claim,
                               "abi": dict(P.ABI), "declared_symbols": [actor["target"], getter["target"]],
                               **({"data_root": recipe["root"]["data"]} if root_kind == "data" else {})})
    _refreeze_native_path_contracts(recipe, files)
    B.validate_actor(actor, allow_bindings=True)
    return actor, recipe, files


def _refreeze_native_path_contracts(recipe, files):
    """Prepare independent inert review data before validation; never app proof."""
    P = B.NP
    wrappers = [recipe["root_evidence"], recipe["lifetime"]["evidence"],
                *(layout["evidence"] for layout in recipe["types"])]
    if P.is_lifecycle_root(recipe):
        wrappers.append(recipe["root"]["lifecycle"]["evidence"])
    if "consumer" in recipe:
        wrappers.append(recipe["consumer"]["evidence"])
    for wrapper in wrappers:
        wrapper["claim"].update(file=str(files["contracts"]))
        wrapper["originals"] = [{"role": role, "file": str(files[key]),
                                "sha256": R._file_hash(files[key]) if key != "review" else "0" * 64}
                               for role, key in (("header", "header"), ("original_build", "build"),
                                                 ("independent_review", "review"))]
    R.write_json(files["contracts"], {row["claim"]["pointer"][1:]: row["contract"] for row in P.evidence_plan(recipe)})
    for wrapper in wrappers:
        wrapper["claim"]["sha256"] = R._file_hash(files["contracts"])
    R.write_json(files["review"], {"format": P.ADMISSION, "abi": dict(P.ABI), "images": P.image_claims(recipe),
                                 "declarations": [P.admission_declaration(row) for row in P.evidence_plan(recipe)]})
    for wrapper in wrappers:
        wrapper["originals"][-1]["sha256"] = R._file_hash(files["review"])


def _lifecycle_path_files(native_files, *, arguments=None, return_type="int32", external_hooks=False):
    """Extend the actual inert file closure, without a constructor or app call."""
    from test_rea_native_path import _lifecycle_root_actor
    actor, recipe, files = _native_path_files(native_files, borrowed_root=True,
        arguments=arguments, return_type=return_type)
    _, prospective = _lifecycle_root_actor()
    spec = copy.deepcopy(prospective["root"]["lifecycle"])
    spec["observe_ms"] = 1000
    hooks = files["module"]
    if external_hooks:
        hooks = hooks.with_name("LifecycleHooks.dylib")
        hooks.write_bytes(_inert_macho())
        files["hooks"] = hooks
    image = {"path": str(hooks), "sha256": R._file_hash(hooks)}
    spec["constructor"]["entry"] = {"module": copy.deepcopy(image), "offset": "0x1000"}
    spec["constructor"]["return_sites"] = [{"module": copy.deepcopy(image), "offset": "0x1004"}]
    spec["destructors"][0]["entry"] = {"module": copy.deepcopy(image), "offset": "0x1008"}
    recipe["root"] = {"lifecycle": spec}
    recipe["lifetime"]["mode"] = B.NL.MODE
    build = R.load_json(files["build"])
    build["lifecycle_entries"] = B.NL.entries(spec)
    R.write_json(files["build"], build)
    _refreeze_native_path_contracts(recipe, files)
    B.validate_actor(actor, allow_bindings=True)
    return actor, recipe, files


def _lifecycle_request_files(native_files, *, for_call=False, external_hooks=False):
    actor, recipe, files = _lifecycle_path_files(native_files, external_hooks=external_hooks)
    if for_call:
        request = B.invocation_request(B.bind_actor(actor, {"payload": "23"}))
        recipe = B.request_native_path_recipes(request)[0]
    else:
        recipe.pop("consumer")
        _refreeze_native_path_contracts(recipe, files)
        request, _ = _native_path_investigation(actor, recipe, files, "native_read")
    return request, recipe, files


def _lifecycle_result_changes(request, recipe, *, for_call=False):
    from test_rea_native_path import _observations, _lifecycle_result
    return {"operations": [{"index": 0, "op": request["operations"][0]["op"], "status": "ok",
                            "native_paths": _observations(recipe, for_call=for_call)}],
            "lifecycle_observer": _lifecycle_result(recipe, for_call=for_call),
            "native_calls": int(for_call), "invocations": int(for_call)}


@pytest.mark.parametrize("root_kind", ["getter", "objc", "read", "swift_read"])
def test_native_path_resource_closure_includes_real_nested_files_and_selected_contracts(native_files, root_kind):
    actor, recipe, files = _native_path_files(native_files, root_kind=root_kind)
    pins = B.native_resources(actor)
    assert all(pins[path]["sha256"] == sha for path, sha in B.native_file_claims(actor).items())
    records = pins[str(files["contracts"])]["native_path_contracts"]
    assert {row["pointer"] for row in records} == {row["claim"]["pointer"] for row in B.NP.evidence_plan(recipe)}
    assert all(row["originals"] == recipe["root_evidence"]["originals"] for row in records)
    assert pins[str(files["module"])]["native_path_data_sections"][0]["sections"] == []
    assert B.native_path_recipes(B.bind_actor(actor, {"payload": "11"})) == [recipe]
    if root_kind != "objc": assert str(files["getter_abi"]) in pins
    assert str(files["consumer_abi"]) in pins


@pytest.mark.parametrize("changed", ["module", "abi", "getter_abi", "consumer_abi", "contracts", "header", "build", "review"])
def test_native_path_changed_transitive_pin_refuses_before_worker_or_native_root(runtime, native_files, monkeypatch, changed):
    actor, _, files = _native_path_files(native_files)
    B.native_resources(actor)
    files[changed].write_bytes(files[changed].read_bytes() + b"\nchanged nested artifact")
    with pytest.raises(ValueError): B.native_resources(actor)
    monkeypatch.setattr(B, "NATIVE_PATH_RUNTIME_READY", True)
    app, _, _, state, out = runtime
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "11"}), out)
    assert receipt["status"] == "incomplete" and receipt["worker"] is None
    assert receipt["actuated"] is False and receipt["native_calls"] is False and state["calls"] == []
    assert receipt["error"]["type"] == "ValueError"


@pytest.mark.parametrize("change", ["bool_to_int", "int_to_float", "cpu_to_bool"])
def test_collector_refuses_worker_resource_type_tampering_even_when_recipe_receipt_matches(runtime, native_files, monkeypatch, change):
    from test_rea_native_path import _observations
    actor, recipe, files = _native_path_files(native_files)
    resources = copy.deepcopy(B.native_resources(actor))
    if change == "cpu_to_bool": resources[str(files["module"])]["macho_images"][0]["cpu_subtype"] = False
    else:
        pointer = "/lifetime" if change == "bool_to_int" else "/Node"
        record = next(record for record in resources[str(files["contracts"])]["native_path_contracts"] if record["pointer"] == pointer)
        if change == "bool_to_int": record["contract"]["native_child_retained"] = 0
        else: record["contract"]["bytes"] = float(record["contract"]["bytes"])
    app, _, _, state, out = runtime
    state["result_changes"] = {"operations": [{"index": 0, "op": "native_invoke", "status": "ok",
                                              "native_paths": _observations(recipe)}],
                               "native_calls": 2, "native_resources": resources}
    monkeypatch.setattr(B, "NATIVE_PATH_RUNTIME_READY", True)
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "5"}), out)
    assert receipt["status"] == "incomplete" and len(state["calls"]) == 1
    assert "pinned native resources" in receipt["error"]["message"] and receipt["resource_review_required"]
    assert receipt["request"]["operations"][0]["arguments"][0]["value"]["native_path"] == recipe


def test_native_path_backend_stays_disabled_before_filesystem_or_process_identity(runtime, native_files, monkeypatch):
    actor, _, _ = _native_path_files(native_files)
    monkeypatch.setattr(B, "NATIVE_PATH_RUNTIME_READY", False)
    app, _, _, state, out = runtime
    with pytest.raises(B.Unavailable, match="not enabled"):
        B.invoke(app.parent / "absent.app", B.bind_actor(actor, {"payload": "5"}), out)
    assert not out.exists() and state["identity_calls"] == 0 and state["calls"] == []
    config = app.parent / "path-disabled-worker.json"
    R.write_json(config, {"format": B.WORKER, "request": B.invocation_request(B.bind_actor(actor, {"payload": "5"})),
                          "trace": str(app.parent / "unused-trace.jsonl")})
    result = B._worker(config)
    assert result["status"] == "unavailable" and result["attached"] is False
    assert result["native_calls"] == result["invocations"] == 0 and state["identity_calls"] == 0


@pytest.mark.parametrize("offset,accepted", [("0x2000", True), ("0x1000", False), ("0x2020", False)])
def test_native_path_vptr_requires_original_data_section_not_instruction_or_range_end(native_files, offset, accepted):
    actor, _, files = _native_path_files(native_files, runtime_identity=True, identity_offset=offset)
    if not accepted:
        with pytest.raises(ValueError, match="data sections"): B.native_resources(actor)
        return
    pins = B.native_resources(actor)
    data = pins[str(files["module"])]["native_path_data_sections"][0]
    code = pins[str(files["module"])]["macho_sections"][0]
    assert data["sections"] == [{"segment": "__DATA", "name": "__const", "offset": "0x2000", "bytes": 32, "flags": 0}]
    assert code["sections"][0]["offset"] == "0x1000"


def test_native_path_refuses_arm64e_slice_even_when_every_hash_and_contract_matches(native_files):
    native_files[2].write_bytes(_inert_macho(subtype=2))
    actor, _, _ = _native_path_files(native_files)
    with pytest.raises(ValueError, match="plain-arm64"): B.native_resources(actor)


@pytest.mark.parametrize("change", ["vm_zero_tail", "file_before_segment", "file_after_segment", "translation"])
def test_native_path_original_data_sections_require_exact_segment_file_backing(native_files, change):
    raw = bytearray(_inert_macho_with_data().ljust(0x2040, b"\0"))
    command = 32 + 24 + 152
    section = command + 72
    if change == "vm_zero_tail": struct.pack_into("<Q", raw, section + 32, 0x2040)
    elif change == "file_before_segment": struct.pack_into("<I", raw, section + 48, 0x100)
    elif change == "file_after_segment": struct.pack_into("<I", raw, section + 48, 0x2010)
    else:
        struct.pack_into("<Q", raw, command + 48, 0x40)
        struct.pack_into("<I", raw, section + 48, 0x2010)
    native_files[2].write_bytes(raw)
    actor, _, _ = _native_path_files(native_files)
    with pytest.raises(ValueError, match="file-backed data section"): B.native_resources(actor)


@pytest.mark.parametrize("change", ["missing_review", "unrelated_contract", "unrelated_original", "unrelated_image"])
def test_native_path_resource_gate_checks_review_correlation_even_with_current_file_hashes(native_files, change):
    actor, recipe, files = _native_path_files(native_files)
    wrappers = [recipe["root_evidence"], recipe["lifetime"]["evidence"],
                *(layout["evidence"] for layout in recipe["types"]), recipe["consumer"]["evidence"]]
    if change == "missing_review":
        for wrapper in wrappers: wrapper["originals"].pop()
    else:
        review = R.load_json(files["review"])
        if change == "unrelated_contract": review["declarations"][0]["contract"]["pointee_type"] = "Unrelated"
        elif change == "unrelated_original": review["declarations"][0]["originals"][0]["sha256"] = "0" * 64
        else: review["images"][0]["sha256"] = "0" * 64
        R.write_json(files["review"], review)
        for wrapper in wrappers: wrapper["originals"][-1]["sha256"] = R._file_hash(files["review"])
    with pytest.raises(ValueError): B.native_resources(actor)


def _native_path_investigation(actor, recipe, files, op):
    result = B.NP.path_types(recipe)[-1]
    fields = copy.deepcopy(result["fields"] if op == "graph" else [result["fields"][1]])
    proof = files["contracts"].with_name("native-path-" + op + "-layout.json")
    R.write_json(proof, {"bytes": result["bytes"], "fields": fields, "pointer_policy": "borrowed_no_escape"})
    evidence = {"kind": "reviewed_contract", "file": str(proof), "sha256": R._file_hash(proof), "pointer": ""}
    if op == "native_read":
        operation = {"op": op, "receiver": {"native_path": recipe}, "bytes": result["bytes"],
                     "fields": fields, "layout_evidence": evidence}
    else:
        operation = {"op": op, "root": {"native_path": recipe}, "root_type": recipe["result_type"],
                     "types": [{"name": recipe["result_type"], "bytes": result["bytes"], "fields": fields,
                                "layout_evidence": evidence}], "max_depth": 1, "max_nodes": 2, "methods_limit": 0}
    return {"operations": [operation]}, proof


@pytest.mark.parametrize("op", ["graph", "native_read"])
def test_native_path_investigation_uses_existing_v1_layout_and_has_no_dispatch_permission(native_files, op):
    from test_rea_native_path import _observations
    actor, recipe, files = _native_path_files(native_files)
    request, proof = _native_path_investigation(actor, recipe, files, op)
    assert B.validate_request(request) == request
    pins = B.request_resources(request)
    path_pins = B._native_path_resources(recipe)
    assert str(proof) in pins and {path: pins[path] for path in path_pins} == path_pins
    row = {"native_paths": _observations(recipe, for_call=False)}
    assert B.validate_native_path_observations(request["operations"][0], row) == row["native_paths"]
    with pytest.raises(ValueError):
        B.validate_native_path_observations(B.invocation_request(B.bind_actor(actor, {"payload": "1"}))["operations"][0], row)


@pytest.mark.parametrize("change", ["read_alias", "read_offset", "read_type", "read_extent", "graph_type", "graph_fields", "nested_call_path"])
def test_native_path_investigation_cannot_expand_or_relabel_admitted_result(native_files, change):
    actor, recipe, files = _native_path_files(native_files)
    op = "graph" if change.startswith("graph") else "native_read"
    request, _ = _native_path_investigation(actor, recipe, files, op)
    operation = request["operations"][0]
    if change == "read_alias":
        recipe["types"][-1]["fields"][1].update(type="pointer", offset=8)
        operation["fields"][0].update(type="pointer", offset=8, alias="escaped")
    elif change == "read_offset": operation["fields"][0]["offset"] = 8
    elif change == "read_type": operation["fields"][0]["type"] = "uint32"
    elif change == "read_extent": operation["bytes"] = 32
    elif change == "graph_type": operation["root_type"] = "Other"
    elif change == "graph_fields": operation["types"][0]["fields"][0]["offset"] = 8
    else:
        operation["receiver"] = {"native": {**B.bind_actor(actor, {"payload": "1"}), "return_type": "pointer"}}
        operation["receiver"]["native"]["arguments"][0]["value"]["native_path"]["consumer"]["call"]["return_type"] = "pointer"
    with pytest.raises(ValueError): B.validate_request(request)


@pytest.mark.parametrize("native_calls,accepted", [(0, False), (1, False), (2, True)])
def test_worker_receipt_native_counter_includes_path_getter_and_consumer(native_calls, accepted):
    from test_rea_native_path import _path_actor, _observations
    actor, recipe = _path_actor()
    request = B.invocation_request(B.bind_actor(actor, {"payload": "1"}))
    transport = {"kind": "local"}
    result = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": True, "native_state_uncertain": False,
              "status": "completed", "operations": [{"index": 0, "op": "native_invoke", "status": "ok",
                                                         "native_paths": _observations(recipe)}],
              "native_calls": native_calls, "invocations": 1, "handles_expired": True}
    if accepted: assert B._worker_result(result, R.digest(request), request, transport) == result
    else:
        with pytest.raises(ValueError, match="counter omitted"): B._worker_result(result, R.digest(request), request, transport)


@pytest.mark.parametrize("root_kind,getters", [("getter", 0), ("objc", 0), ("objc", 1), ("objc", 2),
    ("read", 0), ("read", 1), ("read", 2), ("swift_read", 0), ("swift_read", 1), ("swift_read", 2)])
def test_minimum_native_calls_counts_all_fixed_owner_receiver_getters_for_actor_and_observation(native_files, root_kind, getters):
    path = [{"selector": "ownedState", "signature": "@16@0:8"}] * getters
    actor, recipe, files = _native_path_files(native_files, root_kind=root_kind, receiver_path=path)
    expected_root = 1 if root_kind == "getter" else getters
    assert B.minimum_native_calls(actor) == expected_root + 1
    request = B.invocation_request(B.bind_actor(actor, {"payload": "5"}))
    assert B.minimum_request_native_calls(request) == expected_root + 1
    for op in ("graph", "native_read"):
        observation, _ = _native_path_investigation(actor, recipe, files, op)
        assert B.minimum_request_native_calls(observation) == expected_root
    # The same owner routes existed before native_path and retain their counts.
    legacy = copy.deepcopy(actor)
    legacy["arguments"][0]["value"] = copy.deepcopy(recipe["root"])
    assert B.minimum_native_calls(legacy) == expected_root + 1


def test_minimum_native_calls_includes_top_objc_and_nested_pointer_receivers_without_double_counting():
    from test_rea_native_path import _path_actor
    native, _ = _path_actor()
    receiver = {"class": "Owner", "kind": "unique", "path": [
        {"selector": "ownedState", "signature": "@16@0:8"}, {"selector": "child", "signature": "@16@0:8"}]}
    inner = copy.deepcopy(native)
    inner["arguments"] = [{"type": "pointer", "value": {"objc": copy.deepcopy(receiver)}}]
    inner["return_type"] = "pointer"
    native["arguments"] = [{"type": "pointer", "value": {"getter": inner}}]
    method = copy.deepcopy(ACTOR)
    method["receiver"] = receiver
    combined = {"kind": "runtime_sequence", "steps": [method, native]}
    assert B.minimum_native_calls(native) == 4  # Two native entries and two object getters.
    assert B.minimum_native_calls(method) == 3
    assert B.minimum_native_calls(combined) == 7


@pytest.mark.parametrize("native_calls,accepted", [(0, False), (2, False), (3, True)])
def test_worker_receipt_counts_objc_read_receiver_and_each_successful_field_getter(native_calls, accepted):
    receiver = {"class": "Owner", "kind": "unique", "path": [{"selector": "ownedState", "signature": "@16@0:8"}]}
    read = {"op": "read", "receiver": receiver, "fields": [
        {"name": "ready", "kind": "getter", "selector": "ready", "signature": "B16@0:8", "type": "bool"},
        {"name": "value", "kind": "getter", "selector": "value", "signature": "i16@0:8", "type": "int32"}]}
    request = {"operations": [read, {"op": "classes"}]}
    assert B.minimum_request_native_calls(request, completed_operations=0) == 0
    assert B.minimum_request_native_calls(request, completed_operations=1) == 3
    transport = {"kind": "local"}
    result = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": True, "native_state_uncertain": False,
              "status": "incomplete", "operations": [{"index": 0, "op": "read", "status": "ok"}],
              "native_calls": native_calls, "invocations": 0, "handles_expired": True}
    if accepted: assert B._worker_result(result, R.digest(request), request, transport) == result
    else:
        with pytest.raises(ValueError, match="counter omitted"): B._worker_result(result, R.digest(request), request, transport)


def test_native_path_shared_contract_pin_merge_retains_each_selected_row_and_rejects_conflict():
    pins = {}
    one = {"pointer": "/one", "contract": {"type": "Owner"}, "originals": []}
    two = {"pointer": "/two", "contract": {"type": "Node"}, "originals": []}
    for rows in ([one], [two], [one]):
        B._merge_pins(pins, {"/file": {"file": "/file", "sha256": "a" * 64, "native_path_contracts": rows}})
    assert pins["/file"]["native_path_contracts"] == [one, two]
    with pytest.raises(ValueError, match="conflicting exact contracts"):
        B._merge_pins(pins, {"/file": {"native_path_contracts": [{**one, "contract": {"type": "Changed"}}]}})


@pytest.mark.parametrize("original,changed", [(0, False), (16, 16.0), ({"retained": False}, {"retained": 0})])
def test_resource_pin_merging_never_equates_boolean_integer_or_float_metadata(original, changed):
    resources = {"/file": {"metadata": original}}
    with pytest.raises(ValueError, match="conflicting identities"):
        B._merge_pins(resources, {"/file": {"metadata": changed}})


@pytest.mark.parametrize("mode,offset", [("object", "0x2000"), ("object", "0x2010"), ("pointer", "0x2018")])
def test_static_data_root_pins_whole_object_or_pointer_slot_without_a_getter_abi(native_files, mode, offset):
    actor, recipe, files = _native_path_files(native_files, root_kind="data", data_mode=mode, data_offset=offset)
    pins = B.native_resources(actor)
    claims = B.native_file_claims(actor)
    assert all(pins[path]["sha256"] == sha for path, sha in claims.items())
    assert str(files["getter_abi"]) not in pins and str(files["getter_abi"]) not in claims
    assert B.minimum_native_calls(actor) == 1  # Only the actual outer consumer.
    assert recipe["lifetime"]["mode"] == "process_lifetime"
    assert pins[str(files["data_module"])]["native_path_data_sections"][0]["sections"] == [
        {"segment": "__DATA", "name": "__const", "offset": "0x2000", "bytes": 32, "flags": 0}]
    root = next(row for row in pins[str(files["contracts"])]["native_path_contracts"] if row["pointer"] == "/root")
    assert root["contract"]["root"] == recipe["root"]


@pytest.mark.parametrize("mode,offset", [("object", "0x1000"), ("object", "0x1800"),
                                       ("object", "0x2018"), ("object", "0x2020"), ("pointer", "0x2020")])
def test_static_data_root_refuses_code_holes_cross_section_extents_and_range_end_before_worker(runtime, native_files, mode, offset):
    actor, _, _ = _native_path_files(native_files, root_kind="data", data_mode=mode, data_offset=offset)
    app, _, _, state, out = runtime
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "5"}), out)
    assert receipt["status"] == "incomplete" and receipt["worker"] is None and state["calls"] == []
    assert receipt["actuated"] is False and receipt["native_calls"] is False
    assert "one exact original file-backed noninstruction section" in receipt["error"]["message"]


@pytest.mark.parametrize("mode,accepted", [("object", False), ("pointer", True)])
def test_static_root_cannot_join_two_adjacent_file_backed_sections(native_files, mode, accepted):
    # Both halves are genuinely file-backed; a module/segment extent alone is
    # insufficient. The same location admits an 8-byte slot, not a 16-byte object.
    original = _inert_macho()
    command = struct.pack("<II16sQQQQIIII", 0x19, 232, b"__DATA", 0x2000, 0x1000, 0x2000, 0x20, 3, 3, 2, 0)
    sections = b"".join(struct.pack("<16s16sQQIIIIIIII", name, b"__DATA", offset, 0x10, offset, 3, 0, 0, 0, 0, 0, 0)
                        for name, offset in ((b"__first", 0x2000), (b"__second", 0x2010)))
    header = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 6, 3, 24 + 152 + 232, 0, 0)
    data_module = native_files[2].with_name("Adjacent.dylib")
    data_module.write_bytes((header + original[32:32 + 24 + 152] + command + sections).ljust(0x2020, b"\0"))
    actor, _, _ = _native_path_files(native_files, root_kind="data", data_mode=mode,
                                     data_offset="0x2008", data_module=data_module)
    if accepted:
        assert len(B.native_resources(actor)[str(data_module)]["native_path_data_sections"][0]["sections"]) == 2
    else:
        with pytest.raises(ValueError, match="one exact original file-backed"): B.native_resources(actor)


@pytest.mark.parametrize("section_kind", ["zerofill", "instructions", "arm64e"])
def test_static_data_root_checks_its_own_original_image_and_section_before_attachment(runtime, native_files, section_kind):
    raw = bytearray(_inert_macho_with_data())
    if section_kind == "arm64e": struct.pack_into("<I", raw, 8, 2)
    else:
        section = 32 + 24 + 152 + 72
        struct.pack_into("<I", raw, section + 64, 1 if section_kind == "zerofill" else 0x80000400)
    data_module = native_files[2].with_name("Static.dylib")
    data_module.write_bytes(raw)
    actor, recipe, files = _native_path_files(native_files, root_kind="data", data_module=data_module)
    assert recipe["types"][0]["owner_image"]["path"] != str(data_module)
    assert B.native_file_claims(actor)[str(data_module)] == R._file_hash(data_module)
    app, _, _, state, out = runtime
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "5"}), out)
    assert receipt["worker"] is None and state["calls"] == [] and receipt["actuated"] is False
    assert ("plain-arm64" if section_kind == "arm64e" else "file-backed noninstruction section") in receipt["error"]["message"]


@pytest.mark.parametrize("change", ["changed", "deleted"])
def test_external_static_root_image_is_in_transitive_resources_and_drift_stops_before_worker(runtime, native_files, change):
    data_module = native_files[2].with_name("Static.dylib")
    data_module.write_bytes(_inert_macho_with_data())
    actor, recipe, files = _native_path_files(native_files, root_kind="data", data_module=data_module)
    pins = B.native_resources(actor)
    assert pins[str(data_module)]["sha256"] == recipe["root"]["data"]["module"]["sha256"]
    assert B.NP.image_claims(recipe) == sorted([recipe["root"]["data"]["module"], actor["target"]["module"]], key=lambda item: item["path"])
    if change == "changed": data_module.write_bytes(data_module.read_bytes() + b"changed original bytes")
    else: data_module.unlink()
    app, _, _, state, out = runtime
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "5"}), out)
    assert receipt["status"] in {"incomplete", "unavailable"} and receipt["worker"] is None and state["calls"] == []
    assert receipt["actuated"] is False and receipt["native_calls"] is False


@pytest.mark.parametrize("change", ["mode", "offset", "missing_lifetime_admission"])
def test_static_root_file_backing_never_replaces_exact_source_and_lifetime_admission(runtime, native_files, change):
    actor, recipe, files = _native_path_files(native_files, root_kind="data")
    B.native_resources(actor)  # Establish a valid independently pinned baseline.
    if change == "mode": recipe["root"]["data"]["mode"] = "pointer"
    elif change == "offset": recipe["root"]["data"]["offset"] = "0x2008"
    else:
        review = R.load_json(files["review"])
        review["declarations"] = [row for row in review["declarations"] if row["contract"].get("format") != B.NP.LIFETIME_CONTRACT]
        R.write_json(files["review"], review)
        # Update the original pin only, retaining all selected contracts.
        for wrapper in [recipe["root_evidence"], recipe["lifetime"]["evidence"],
                        *(layout["evidence"] for layout in recipe["types"]), recipe["consumer"]["evidence"]]:
            wrapper["originals"][-1]["sha256"] = R._file_hash(files["review"])
    app, _, _, state, out = runtime
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "5"}), out)
    assert receipt["worker"] is None and state["calls"] == [] and receipt["status"] == "incomplete"
    assert receipt["error"]["type"] == "ValueError"


@pytest.mark.parametrize("op", ["native_read", "graph"])
def test_static_data_investigation_accepts_zero_native_calls_but_requires_real_root_receipt_binding(native_files, op):
    from test_rea_native_path import _observations
    actor, recipe, files = _native_path_files(native_files, root_kind="data")
    request, _ = _native_path_investigation(actor, recipe, files, op)
    B.request_resources(request)
    assert B.minimum_request_native_calls(request) == 0
    transport = {"kind": "local"}
    result = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": True, "native_state_uncertain": False,
              "status": "completed", "operations": [{"index": 0, "op": op, "status": "ok",
                                                         "native_paths": _observations(recipe, for_call=False)}],
              "native_calls": 0, "invocations": 0, "handles_expired": True}
    assert B._worker_result(result, R.digest(request), request, transport) == result
    result["operations"][0]["native_paths"][0]["owner_retained"] = True
    with pytest.raises(ValueError, match="binding"): B._worker_result(result, R.digest(request), request, transport)


@pytest.mark.parametrize("native_calls,accepted", [(0, False), (1, True)])
def test_static_data_root_does_not_hide_the_actual_consumer_dispatch(native_calls, accepted):
    from test_rea_native_path import _data_path_actor, _observations
    actor, recipe = _data_path_actor()
    request = B.invocation_request(B.bind_actor(actor, {"payload": "1"}))
    transport = {"kind": "local"}
    result = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": True, "native_state_uncertain": False,
              "status": "completed", "operations": [{"index": 0, "op": "native_invoke", "status": "ok",
                                                         "native_paths": _observations(recipe)}],
              "native_calls": native_calls, "invocations": 1, "handles_expired": True}
    if accepted: assert B._worker_result(result, R.digest(request), request, transport) == result
    else:
        with pytest.raises(ValueError, match="counter omitted"): B._worker_result(result, R.digest(request), request, transport)


def _borrowed_read_files(native_files):
    actor, recipe, files = _native_path_files(native_files, borrowed_root=True)
    recipe.pop("consumer")
    _refreeze_native_path_contracts(recipe, files)
    request, proof = _native_path_investigation(actor, recipe, files, "native_read")
    return request, recipe, files, proof


@pytest.mark.parametrize("for_call", [False, True])
def test_borrowed_resource_admission_pins_original_primary_identity_and_exact_terminal_scope(native_files, for_call):
    if for_call:
        actor, recipe, files = _native_path_files(native_files, borrowed_root=True)
        request = B.invocation_request(B.bind_actor(actor, {"payload": "11"}))
    else:
        request, recipe, files, proof = _borrowed_read_files(native_files)
    pins = B.request_resources(request)
    assert {path: pin["sha256"] for path, pin in pins.items() if path in B.NP.file_claims(recipe)} == B.NP.file_claims(recipe)
    records = pins[str(files["contracts"])]["native_path_contracts"]
    lifetime = next(row["contract"] for row in records if row["pointer"] == "/lifetime")
    assert lifetime["mode"] == "main_queue_borrowed_root" and lifetime["path"] == []
    assert lifetime["scope"] == "single_immediate_use" and lifetime["this_adjustment"] == 0
    assert lifetime["terminal"]["op"] == ("native_invoke" if for_call else "native_read")
    assert str(files["getter_abi"]) in pins and str(files["header"]) in pins and str(files["build"]) in pins
    assert pins[str(files["module"])]["native_path_data_sections"][0]["sections"]
    assert B.minimum_request_native_calls(request) == (2 if for_call else 1)
    if not for_call:
        assert str(proof) in pins
        assert lifetime["terminal"]["fields"] == [
            {"name": "generation", "type": "uint32", "offset": 8}, {"name": "payload", "type": "int32", "offset": 12}]
        assert request["operations"][0]["fields"] == [lifetime["terminal"]["fields"][0]]


@pytest.mark.parametrize("change", ["missing_premise", "vptr_as_lifetime", "zero_as_bool", "other_terminal"])
def test_borrowed_lifetime_review_cannot_omit_premises_or_relabel_a_different_terminal(native_files, change):
    actor, recipe, files = _native_path_files(native_files, borrowed_root=True)
    B.native_resources(actor)
    review = R.load_json(files["review"])
    contract = next(row["contract"] for row in review["declarations"] if row["contract"].get("format") == B.NP.LIFETIME_CONTRACT)
    if change == "missing_premise": contract.pop("construction_destruction")
    elif change == "vptr_as_lifetime": contract["live_initialized_primary_base"] = "vptr_matched"
    elif change == "zero_as_bool": contract["this_adjustment"] = False
    else: contract["terminal"]["consumer"]["callable"]["entry"]["symbol"] = "unreviewed_other_call"
    R.write_json(files["review"], review)
    # Keep every file hash current: refusal must concern the independent
    # source correlation, not a conveniently stale SHA or changed recipe.
    wrappers = [recipe["root_evidence"], recipe["lifetime"]["evidence"],
                *(layout["evidence"] for layout in recipe["types"]), recipe["consumer"]["evidence"]]
    for wrapper in wrappers:
        next(item for item in wrapper["originals"] if item["role"] == "independent_review")["sha256"] = R._file_hash(files["review"])
    with pytest.raises(ValueError, match="correlation"): B.native_resources(actor)


@pytest.mark.parametrize("changed,deleted", [("getter_abi", False), ("header", True), ("review", False)])
def test_borrowed_changed_or_deleted_original_stops_before_a_worker(runtime, native_files, monkeypatch, changed, deleted):
    actor, _, files = _native_path_files(native_files, borrowed_root=True)
    B.native_resources(actor)
    if deleted: files[changed].unlink()
    else: files[changed].write_bytes(files[changed].read_bytes() + b"\nchanged original")
    app, _, _, state, out = runtime
    monkeypatch.setattr(B, "NATIVE_PATH_RUNTIME_READY", True)
    receipt = B.invoke(app, B.bind_actor(actor, {"payload": "11"}), out)
    assert receipt["status"] == "incomplete" and receipt["worker"] is None and state["calls"] == []
    assert receipt["actuated"] is False and receipt["native_calls"] is False


@pytest.mark.parametrize("for_call", [False, True])
@pytest.mark.parametrize("change", ["none", "counter_omits_use", "counter_extra_call", "missing_scope", "intervening_epoch", "scope_bool"])
def test_worker_borrowed_scope_binds_terminal_native_count_and_exact_epoch(for_call, change):
    from test_rea_native_path import _borrowed_root_actor, _borrowed_root_read, _observations
    if for_call:
        actor, recipe = _borrowed_root_actor()
        request = B.invocation_request(B.bind_actor(actor, {"payload": "11"}))
    else:
        request, recipe = _borrowed_root_read()
    rows = _observations(recipe, for_call=for_call)
    transport = {"kind": "local"}
    result = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": True, "native_state_uncertain": False,
              "status": "completed", "operations": [{"index": 0, "op": "native_invoke" if for_call else "native_read",
                  "status": "ok", "native_paths": rows}], "native_calls": 2 if for_call else 1,
              "invocations": 1 if for_call else 0, "handles_expired": True}
    if change == "counter_omits_use": result["native_calls"] -= 1
    elif change == "counter_extra_call": result["native_calls"] += 1
    elif change == "missing_scope": rows[0].pop("borrowed_scope")
    elif change == "intervening_epoch": rows[0]["borrowed_scope"]["checked_epoch"] = 2
    elif change == "scope_bool": rows[0]["borrowed_scope"]["getter_epoch"] = True
    if change == "none": assert B._worker_result(result, R.digest(request), request, transport) == result
    else:
        with pytest.raises(ValueError): B._worker_result(result, R.digest(request), request, transport)


@pytest.mark.parametrize("for_call", [False, True])
def test_terminal_borrowed_epoch_counts_prior_operations_without_reusing_their_roots(for_call):
    from test_rea_native_path import _path_actor, _borrowed_root_actor, _borrowed_root_read, _observations
    ordinary, old_recipe = _path_actor()
    prior = B.invocation_request(B.bind_actor(ordinary, {"payload": "1"}))["operations"][0]
    if for_call:
        actor, recipe = _borrowed_root_actor()
        terminal = B.invocation_request(B.bind_actor(actor, {"payload": "2"}))["operations"][0]
    else:
        read, recipe = _borrowed_root_read()
        terminal = read["operations"][0]
    request = B.validate_request({"operations": [prior, terminal]})
    rows = _observations(recipe, for_call=for_call)
    rows[0]["borrowed_scope"].update(getter_epoch=3, checked_epoch=3)
    transport = {"kind": "local"}
    result = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": True, "native_state_uncertain": False,
              "status": "completed", "operations": [
                  {"index": 0, "op": "native_invoke", "status": "ok", "native_paths": _observations(old_recipe)},
                  {"index": 1, "op": terminal["op"], "status": "ok", "native_paths": rows}],
              "native_calls": 4 if for_call else 3, "invocations": 2 if for_call else 1, "handles_expired": True}
    assert B._worker_result(result, R.digest(request), request, transport) == result
    rows[0]["borrowed_scope"].update(getter_epoch=1, checked_epoch=1)
    with pytest.raises(ValueError, match="epoch"): B._worker_result(result, R.digest(request), request, transport)


@pytest.mark.parametrize("for_call", [False, True])
@pytest.mark.parametrize("change", ["none", "prefix_only", "extra_row", "row_order", "bool_index", "wrong_op",
    "failed_row", "bool_counter", "float_counter", "extra_call", "scope_epoch", "missing_paths"])
def test_shared_borrowed_counter_boundary_validates_complete_rows_and_selected_receipt(for_call, change):
    from test_rea_native_path import _borrowed_root_actor, _borrowed_root_read, _observations
    if for_call:
        actor, recipe = _borrowed_root_actor()
        request = B.invocation_request(B.bind_actor(actor, {"payload": "11"}))
    else:
        request, recipe = _borrowed_root_read()
    request["operations"].insert(0, {"op": "modules"})
    rows = [{"index": 0, "op": "modules", "status": "ok"},
            {"index": 1, "op": request["operations"][1]["op"], "status": "ok",
             "native_paths": _observations(recipe, for_call=for_call)}]
    count = 2 if for_call else 1
    saved = copy.deepcopy((request, rows))
    if change == "prefix_only": rows.pop()
    elif change == "extra_row": rows.append(copy.deepcopy(rows[-1]))
    elif change == "row_order": rows.reverse()
    elif change == "bool_index": rows[0]["index"] = False
    elif change == "wrong_op": rows[-1]["op"] = "native_read" if for_call else "native_invoke"
    elif change == "failed_row": rows[0]["status"] = "failed"
    elif change == "bool_counter": count = True
    elif change == "float_counter": count = float(count)
    elif change == "extra_call": count += 1
    elif change == "scope_epoch": rows[-1]["native_paths"][0]["borrowed_scope"]["checked_epoch"] = 2
    elif change == "missing_paths": rows[-1].pop("native_paths")
    if change == "none":
        assert B.validate_native_path_counters(request, rows, count) is None
        assert (request, rows) == saved
    else:
        with pytest.raises(ValueError): B.validate_native_path_counters(request, rows, count)


def test_shared_borrowed_counter_cannot_hide_prior_dispatches_behind_matching_small_epochs():
    from test_rea_native_path import _path_actor, _borrowed_root_actor, _observations
    ordinary, old_recipe = _path_actor()
    actor, recipe = _borrowed_root_actor()
    first = B.invocation_request(B.bind_actor(ordinary, {"payload": "1"}))["operations"][0]
    last = B.invocation_request(B.bind_actor(actor, {"payload": "2"}))["operations"][0]
    request = {"operations": [first, last]}
    rows = [{"index": 0, "op": "native_invoke", "status": "ok", "native_paths": _observations(old_recipe)},
            {"index": 1, "op": "native_invoke", "status": "ok", "native_paths": _observations(recipe)}]
    # Equality N+1 alone is insufficient if N omits known earlier dispatches.
    with pytest.raises(ValueError, match="prior native calls"):
        B.validate_native_path_counters(request, rows, 2)
    rows[-1]["native_paths"][0]["borrowed_scope"].update(getter_epoch=3, checked_epoch=3)
    assert B.validate_native_path_counters(request, rows, 4) is None


def test_incomplete_borrowed_worker_prefix_is_preserved_and_existing_modes_keep_old_counter_rules():
    from test_rea_native_path import _path_actor, _borrowed_root_actor, _observations
    actor, _ = _borrowed_root_actor()
    request = B.invocation_request(B.bind_actor(actor, {"payload": "1"}))
    transport = {"kind": "local"}
    denied = {"format": B.LEGACY_WORKER, "transport": transport, "preloaded_gadget_remains": False,
              "request_sha256": R.digest(request), "attached": False, "native_state_uncertain": False,
              "status": "incomplete", "operations": [], "native_calls": None, "invocations": None,
              "handles_expired": True, "error": {"type": "PermissionDeniedError", "message": "inert denied"}}
    assert B._worker_result(denied, R.digest(request), request, transport) == denied
    with pytest.raises(ValueError): B.validate_native_path_counters(request, [], None)
    ordinary, recipe = _path_actor()
    old_request = B.invocation_request(B.bind_actor(ordinary, {"payload": "1"}))
    old = {**denied, "request_sha256": R.digest(old_request), "attached": True, "status": "completed", "error": None,
           "operations": [{"index": 0, "op": "native_invoke", "status": "ok", "native_paths": _observations(recipe)}],
           "native_calls": 3, "invocations": 1}
    assert B._worker_result(old, R.digest(old_request), old_request, transport) == old
    assert B.validate_native_path_counters(old_request, old["operations"], old["native_calls"]) is None


@pytest.mark.parametrize("for_call", [False, True])
def test_lifecycle_resource_closure_pins_external_hooks_source_admission_and_exact_terminal(native_files, for_call):
    request, recipe, files = _lifecycle_request_files(native_files, for_call=for_call, external_hooks=True)
    pins = B.request_resources(request)
    assert all(pins[path]["sha256"] == sha for path, sha in B.NP.file_claims(recipe).items())
    assert str(files["hooks"]) in pins and str(files["getter_abi"]) not in pins
    assert pins[str(files["hooks"])]["macho_sections"][0]["sections"] == [
        {"segment": "__TEXT", "name": "__text", "offset": "0x1000", "bytes": 32, "flags": 0x80000400}]
    records = pins[str(files["contracts"])]["native_path_contracts"]
    observer = next(row for row in records if row["pointer"] == "/observer")
    assert observer["contract"] == B.NL.observer_contract(recipe["root"]["lifecycle"],
        recipe["root_type"], recipe["types"][0])
    assert observer["originals"] == recipe["root"]["lifecycle"]["evidence"]["originals"]
    assert B.minimum_request_native_calls(request) == int(for_call)


@pytest.mark.parametrize("change", ["deleted", "changed", "observer_admission"])
def test_lifecycle_hook_and_independent_observer_claim_drift_refuse_before_worker(runtime, native_files, change):
    request, recipe, files = _lifecycle_request_files(native_files, external_hooks=True)
    B.request_resources(request)
    if change == "deleted":
        files["hooks"].unlink()
    elif change == "changed":
        files["hooks"].write_bytes(files["hooks"].read_bytes() + b"changed original")
    else:
        review = R.load_json(files["review"])
        review["declarations"] = [row for row in review["declarations"]
                                   if row["contract"].get("format") != B.NL.CONTRACT]
        R.write_json(files["review"], review)
        wrappers = [recipe["root_evidence"], recipe["lifetime"]["evidence"],
                    *(layout["evidence"] for layout in recipe["types"]),
                    recipe["root"]["lifecycle"]["evidence"]]
        for wrapper in wrappers:
            wrapper["originals"][-1]["sha256"] = R._file_hash(files["review"])
    app, _, _, state, out = runtime
    receipt = B.collect(app, out, request)
    assert receipt["status"] == ("unavailable" if change == "deleted" else "incomplete")
    assert receipt["worker"] is None and state["calls"] == []
    assert receipt["actuated"] is False and receipt["native_calls"] is False


@pytest.mark.parametrize("role,offset", [("constructor", "0x1020"), ("destructor", "0x2000"),
                                        ("return", "0x1800")])
def test_lifecycle_every_hook_and_return_pc_requires_original_instruction_bytes(runtime, native_files, role, offset):
    request, recipe, files = _lifecycle_request_files(native_files, external_hooks=True)
    spec = recipe["root"]["lifecycle"]
    entry = (spec["constructor"]["entry"] if role == "constructor" else
             spec["destructors"][0]["entry"] if role == "destructor" else spec["constructor"]["return_sites"][0])
    entry["offset"] = offset
    _refreeze_native_path_contracts(recipe, files)
    app, _, _, state, out = runtime
    receipt = B.collect(app, out, request)
    assert receipt["worker"] is None and state["calls"] == [] and receipt["actuated"] is False
    assert "one exact original executable section" in receipt["error"]["message"]


@pytest.mark.parametrize("change", ["file_translation", "nonexecutable", "file_tail", "partial_instruction"])
def test_lifecycle_instruction_pins_require_whole_original_file_backing_without_changing_old_parser(
        runtime, native_files, change):
    request, recipe, files = _lifecycle_request_files(native_files, external_hooks=True)
    raw = bytearray(files["hooks"].read_bytes())
    segment, section = 32 + 24, 32 + 24 + 72
    if change == "file_translation":
        struct.pack_into("<I", raw, section + 48, 0xFFC)
    elif change == "nonexecutable":
        struct.pack_into("<II", raw, segment + 56, 3, 3)
    elif change == "file_tail":
        struct.pack_into("<Q", raw, segment + 48, 0x101C)
    else:
        struct.pack_into("<Q", raw, section + 40, 0x1E)
        recipe["root"]["lifecycle"]["constructor"]["entry"]["offset"] = "0x101c"
    files["hooks"].write_bytes(raw)
    spec = recipe["root"]["lifecycle"]
    entries = [spec["constructor"]["entry"], *spec["constructor"]["return_sites"],
               *(row["entry"] for row in spec["destructors"])]
    for entry in entries:
        entry["module"]["sha256"] = R._file_hash(files["hooks"])
    _refreeze_native_path_contracts(recipe, files)
    # Older instruction consumers keep their established parser behavior.
    assert B._macho_sections(files["hooks"])[0]["sections"]
    app, _, _, state, out = runtime
    receipt = B.collect(app, out, request)
    assert receipt["worker"] is None and state["calls"] == [] and receipt["actuated"] is False
    assert receipt["error"]["type"] == "ValueError"
    assert ("one exact original executable section" if change == "partial_instruction" else
            "exact original executable file-backed bytes") in receipt["error"]["message"]


@pytest.mark.parametrize("for_call", [False, True])
def test_lifecycle_completed_collect_binds_scope_ledger_counters_and_modern_manager_cleanup(runtime, native_files, for_call):
    request, recipe, _ = _lifecycle_request_files(native_files, for_call=for_call)
    app, _, _, state, out = runtime
    state["result_changes"] = _lifecycle_result_changes(request, recipe, for_call=for_call)
    receipt = B.collect(app, out, request)
    assert receipt["status"] == ("executed" if for_call else "observed")
    assert receipt["actuated"] is for_call and receipt["native_calls"] is for_call
    assert receipt["resource_review_required"] is False and len(state["calls"]) == 1
    result = receipt["result"]
    assert B.validate_lifecycle_observer_result(request, result) == result["lifecycle_observer"]
    assert B._frida_lifecycle(result)["manager_state"] == "closed"
    assert result["frida_lifecycle"]["helper_exit_confirmed"] is False
    assert state["calls"][0]["config"]["request"] == request


@pytest.mark.parametrize("change", ["missing", "digest", "birth", "generation", "owner", "order",
                                    "hooks", "callbacks", "expiry", "epoch", "calls", "bool_counter"])
def test_lifecycle_live_result_cannot_replace_global_birth_and_cleanup_with_a_valid_path_row(
        runtime, native_files, change):
    request, recipe, _ = _lifecycle_request_files(native_files, for_call=True)
    app, _, _, state, out = runtime
    changes = _lifecycle_result_changes(request, recipe, for_call=True)
    observed = changes["lifecycle_observer"]
    if change == "missing": del changes["lifecycle_observer"]
    elif change == "digest": observed["observer_sha256"] = "0" * 64
    elif change == "birth": observed["events"][1]["generation"] = 2
    elif change == "generation": observed["selected_generation"] = 2
    elif change == "owner": observed["owner_thread_id"] = 102
    elif change == "order": observed["events"][1], observed["events"][2] = observed["events"][2], observed["events"][1]
    elif change == "hooks": observed["hooks_detached"] -= 1
    elif change == "callbacks": observed["callbacks_inflight"] = 1
    elif change == "expiry": observed["tokens_expired"] = False
    elif change == "epoch": changes["operations"][0]["native_paths"][0]["lifecycle_scope"]["checked_epoch"] = 1
    elif change == "calls": changes["native_calls"] = 2
    else: changes["native_calls"] = True
    if change != "epoch":
        B.validate_native_path_observations(request["operations"][0], changes["operations"][0])
    state["result_changes"] = changes
    receipt = B.collect(app, out, request)
    assert receipt["status"] == "incomplete" and receipt["resource_review_required"] is True
    assert receipt["error"]["type"] == "ValueError" and len(state["calls"]) == 1
    assert R.load_json(out / "worker-output.json").get("lifecycle_observer") == changes.get("lifecycle_observer")


def test_lifecycle_partial_observer_failure_is_retained_without_success_or_retry(runtime, native_files):
    request, recipe, _ = _lifecycle_request_files(native_files)
    app, _, _, state, out = runtime
    raw = {"status": "incomplete", "hooks_installed": 1, "tokens_expired": False,
           "error": {"type": "Error", "message": "listener rollback uncertain"}}
    state["result_changes"] = {"status": "incomplete", "operations": [], "native_calls": 0, "invocations": 0,
                               "lifecycle_observer": raw, "error": raw["error"]}
    receipt = B.collect(app, out, request)
    assert receipt["status"] == "incomplete" and receipt["resource_review_required"] is True
    assert receipt["result"]["lifecycle_observer"] == raw and len(state["calls"]) == 1
    assert receipt["error"]["message"] == "listener rollback uncertain"


def test_lifecycle_budget_refuses_before_bundle_io_and_worker_checks_real_residual_before_frida(
        runtime, native_files, monkeypatch):
    request, recipe, _ = _lifecycle_request_files(native_files)
    app, _, _, _, out = runtime
    with pytest.raises(ValueError, match="remaining worker budget"):
        B.collect(app / "absent.app", out, request, timeout=4)
    assert not out.exists()
    monkeypatch.setattr(B.time, "time", lambda: 1000.0)
    result, calls, _, _ = _worker_fixture(runtime, monkeypatch, request=request, remaining_ms=3999)
    assert result["status"] == "unavailable" and calls == []
    assert result["native_calls"] == result["invocations"] == 0 and result["attached"] is False
    assert "remaining worker budget" in result["error"]["message"]


@pytest.mark.parametrize("for_call", [False, True])
def test_worker_copies_actual_observer_ledger_and_closes_manager_without_claiming_helper_exit(
        runtime, native_files, monkeypatch, for_call):
    request, recipe, _ = _lifecycle_request_files(native_files, for_call=for_call)
    changes = _lifecycle_result_changes(request, recipe, for_call=for_call)
    monkeypatch.setattr(B.time, "time", lambda: 1000.0)
    result, calls, config, _ = _worker_fixture(runtime, monkeypatch, request=request, observed_changes=changes)
    assert result["status"] == "completed" and result["lifecycle_observer"] == changes["lifecycle_observer"]
    assert B._worker_result(result, R.digest(request), request, config["transport"]) == result
    assert calls[-1] == ("manager_close",) and sum(row[0] == "run" for row in calls) == 1
    assert result["frida_lifecycle"]["helper_exit_confirmed"] is False


def test_lifecycle_forbids_metadata_prefix_and_old_requests_forbid_unrequested_observer_result():
    from test_rea_native_path import _lifecycle_root_read
    request, _ = _lifecycle_root_read()
    with pytest.raises(ValueError, match="one operation"):
        B.validate_request({"operations": [{"op": "classes", "query": "Inert"}, *request["operations"]]})
    ordinary = {"operations": [{"op": "classes", "query": "Inert"}]}
    assert B.validate_lifecycle_observer_result(ordinary, {"operations": []}) is None
    for observer in (None, {}):
        with pytest.raises(ValueError, match="unrequested"):
            B.validate_lifecycle_observer_result(ordinary, {"lifecycle_observer": observer})


PHASE_LIMITS = {"setup_seconds": 2, "before_seconds": 8, "after_seconds": 8, "margin_seconds": 1}


def _phased_fixture(runtime, native_files, monkeypatch, *, ready_after=.1, finish_after=21, hang=False, ignore_term=False):
    """Real validators and owned-worker control, no Frida/app/framework calls."""
    app, identity, _, state, out = runtime
    actor, recipe, files = _lifecycle_path_files(native_files)
    # The shared lifecycle fixture deliberately uses a 1s window for its old
    # worker-budget tests. These phase tests model a 20s observation delay and
    # a 1s conservative margin, so freeze that actual spec/contracts/admission
    # BEFORE deriving this request and its inert result ledger.
    recipe["root"]["lifecycle"]["observe_ms"] = 20000
    _refreeze_native_path_contracts(recipe, files)
    request = B.invocation_request(B.bind_actor(actor, {"payload": "23"}))
    recipe = B.request_native_path_recipes(request)[0]
    state["result_changes"] = _lifecycle_result_changes(request, recipe, for_call=True)
    inert_result_runner = R._bounded_run  # Existing inert receipt fixture only.
    class Clock:
        now = 100.0
        def clock(self): return self.now
        def sleep(self, seconds):
            assert 0 <= seconds <= 10
            self.now += seconds
    clock, processes, signals = Clock(), [], []
    monkeypatch.setattr(B.time, "monotonic", clock.clock)
    monkeypatch.setattr(B.time, "sleep", clock.sleep)
    class Process:
        def __init__(self, argv, *, stdout, stderr, stdin, cwd, start_new_session):
            assert stdin is subprocess.DEVNULL and start_new_session is True
            self.pid, self.argv, self.cwd, self.started = 9182, argv, cwd, clock.now
            self.stdout, self.stderr, self.returncode = Path(stdout.name), Path(stderr.name), None
            self.ready, self.waits, self.completed = False, [], False
            processes.append(self)
        def poll(self):
            if not self.ready and ready_after is not None and clock.now >= self.started + ready_after:
                event = {"event": "lifecycle-observer-ready", "observe_ms": recipe["root"]["lifecycle"]["observe_ms"],
                         "hooks": 1 + len(recipe["root"]["lifecycle"]["destructors"])}
                with (self.cwd / "trace.jsonl").open("a") as stream: stream.write(json.dumps(event) + "\n")
                self.ready = True
            if self.returncode is None and not hang and clock.now >= self.started + finish_after:
                prefix = (self.cwd / "trace.jsonl").read_bytes()
                result = inert_result_runner(self.argv, self.stdout, self.stderr, timeout=35, cwd=self.cwd)
                (self.cwd / "trace.jsonl").write_bytes(prefix + b'{"event":"operation-returned"}\n')
                self.returncode, self.completed = result["returncode"], True
            return self.returncode
        def wait(self, *, timeout):
            self.waits.append(timeout)
            if self.poll() is None: raise subprocess.TimeoutExpired(self.argv, timeout)
            return self.returncode
    def killpg(pid, signum):
        assert len(processes) == 1 and pid == processes[0].pid
        signals.append((pid, signum))
        if signum != B.signal.SIGTERM or not ignore_term:
            processes[0].returncode = -signum
    monkeypatch.setattr(B.subprocess, "Popen", Process)
    monkeypatch.setattr(B.os, "killpg", killpg)
    return request, clock, processes, signals


def _acknowledged_fixture(runtime, native_files, monkeypatch, *, ready_after=.1, finish_after=21,
                          hang=False, ignore_term=False, channel_eof_before_exit=False, stop_during_recheck=False):
    """NEW inert P/W channel run; the descriptive J facade is not SDK/app proof.

    Real B34 framing, decisions, current-file checks, control-thread forwarding
    and journals run prospectively. It never converts a completed33 receipt.
    """
    from anyuse.execution import rea_runtime_permit as RP
    app, identity, adapter, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch,
        ready_after=ready_after, finish_after=finish_after, hang=hang, ignore_term=ignore_term)
    build = R.load_json(str(adapter) + ".manifest.json")
    build["interlock_protocol"] = RP.PROTOCOL  # Explicit inert installed-asset fixture.
    R.write_json(str(adapter) + ".manifest.json", build)
    inert_result_runner = R._bounded_run
    actual_pid = os.getpid()
    monkeypatch.setattr(B.os, "getppid", lambda: actual_pid)  # Same-process inert W only.
    parent_sent = B.threading.Event()
    actual_permit_save = B._AcknowledgedWait.permit_save
    def observe_parent_save(parent):
        result = actual_permit_save(parent)
        if parent.permit["sent"] is True:
            parent_sent.set()  # Only after the real durable parent journal returned.
        return result
    monkeypatch.setattr(B._AcknowledgedWait, "permit_save", observe_parent_save)

    def wait_for_forward():
        # The real forwarding thread hashes files and fsyncs journals. Advancing
        # this fixture's fake clock while that work is scheduled can manufacture
        # a timeout. Before the parent sends, its baseline still needs fake time.
        # The intentional stop/recheck test must keep driving its poll/release.
        if stop_during_recheck or not parent_sent.is_set():
            return
        for proc in processes:
            assert proc.decision_settled.wait(timeout=3), (
                "inert permit forwarding did not settle after durable parent send: "
                f"fake_time={clock.now}, deadline={proc.controller.deadline}, "
                f"thread_alive={proc.controller.thread.is_alive()}, worker={proc.controller.data!r}")

    old_sleep = clock.sleep
    def cooperative_sleep(seconds):
        wait_for_forward()
        old_sleep(seconds)
        B.threading.Event().wait(.003)  # Yield to the actual single forwarding thread.
    monkeypatch.setattr(B.time, "sleep", cooperative_sleep)
    recipe = B.request_native_path_recipes(request)[0]

    class Process:
        def __init__(self, argv, *, stdout, stderr, stdin, cwd, start_new_session, pass_fds):
            assert stdin is subprocess.DEVNULL and start_new_session is True and len(pass_fds) == 1
            self.pid, self.argv, self.cwd, self.started = actual_pid, argv, cwd, clock.now
            self.stdout, self.stderr = Path(stdout.name), Path(stderr.name)
            self.returncode, self.ready, self.needed, self.completed, self.waits = None, False, False, False, []
            self.channel_closed = False
            config = R.load_json(argv[-1])
            assert config["pre_actuation_ack"]["control_fd"] == pass_fds[0]
            config["pre_actuation_ack"]["control_fd"] = os.dup(pass_fds[0])
            self.controller = B._PermitWorker(config, cwd / "worker-permit.json", [])
            self.decision_settled = B.threading.Event()
            actual_decision = self.controller._decision
            def settled_decision(frame):
                result = actual_decision(frame)
                # Includes the real post_returned journal write. On exception,
                # wait for _run's original error/abort/final-save path instead.
                self.decision_settled.set()
                return result
            self.controller._decision = settled_decision
            actual_run = self.controller._run
            def settled_run():
                try:
                    return actual_run()
                finally:
                    self.decision_settled.set()
            self.controller._run = settled_run
            self.recheck_entered, self.recheck_release = B.threading.Event(), B.threading.Event()
            self.stop_error = None
            if stop_during_recheck:
                actual_recheck = self.controller._recheck
                def delayed_recheck():
                    self.recheck_entered.set()
                    if not self.recheck_release.wait(timeout=3): raise TimeoutError("inert blocked recheck")
                    actual_recheck()
                self.controller._recheck = delayed_recheck
            self.rpc_returned = B.threading.Event()
            self.agent = {"format": RP.AGENT, "binding_sha256": RP.digest(self.controller.binding),
                "worker_nonce": self.controller.data["worker_nonce"], "selection": None, "decision_sha256": None,
                "state": "prepared", "permit_consumed": False, "native_entry": False, "closed": False,
                "events": [], "error": None}
            self.event("prepared")
            self.controller.start(self)
            processes.append(self)

        def event(self, kind):
            self.agent["events"].append({"seq": len(self.agent["events"]) + 1, "kind": kind, "at": clock.now * 1000})

        def post(self, message):
            if message["type"] == "anyuse.rea-phase-permit-abort":
                if self.agent["closed"]: return
                self.agent["state"] = "denied"
                self.event("late_abort" if self.agent["permit_consumed"] else "denied")
            else:
                assert message["type"] == "anyuse.rea-phase-permit"
                frame = RP.validate_frame(message["payload"])
                assert RP.same(frame["binding"], self.controller.binding)
                assert frame["worker_nonce"] == self.agent["worker_nonce"] and frame["worker_pid"] == self.pid
                self.agent["decision_sha256"] = RP.digest(frame)
                if frame["decision"] == "allow":
                    assert RP.same(frame["selection"], self.agent["selection"]) and not self.agent["permit_consumed"]
                    self.agent["state"] = "allowed"; self.event("permit_accepted")
                    self.agent["permit_consumed"] = True; self.event("permit_consumed")
                    self.agent["native_entry"] = True; self.event("native_entry")
                    with (self.cwd / "trace.jsonl").open("a") as stream:
                        stream.write('{"event":"native-invoke-start"}\n')
                    self.agent["state"] = "consumed"; self.event("native_returned")
                    with (self.cwd / "trace.jsonl").open("a") as stream:
                        stream.write('{"event":"native-invoke-returned"}\n')
                else:
                    self.agent["state"] = "denied"; self.event("denied")
            self.agent["closed"] = True; self.event("closed")
            self.rpc_returned.set()

        def poll(self):
            if self.returncode is not None: return self.returncode
            # script.post returns before W persists its post_returned receipt;
            # complete that real write before EOF/process cleanup can race it.
            wait_for_forward()
            if stop_during_recheck and self.recheck_entered.is_set() and not self.recheck_release.is_set():
                try: self.controller.stop()
                except Exception as failure: self.stop_error = failure
                finally: self.recheck_release.set()
            if not self.agent["closed"] and not self.ready and ready_after is not None and clock.now >= self.started + ready_after:
                event = {"event": "lifecycle-observer-ready", "observe_ms": recipe["root"]["lifecycle"]["observe_ms"],
                         "hooks": 1 + len(recipe["root"]["lifecycle"]["destructors"])}
                with (self.cwd / "trace.jsonl").open("a") as stream: stream.write(json.dumps(event) + "\n")
                self.controller.notify(event)
                self.ready = True
            if (not self.agent["closed"] and self.ready and not self.needed and
                    clock.now >= self.started + recipe["root"]["lifecycle"]["observe_ms"] / 1000):
                lifecycle = state["result_changes"]["lifecycle_observer"]
                self.agent["selection"] = {"generation": lifecycle["selected_generation"],
                                           "owner_thread_id": lifecycle["owner_thread_id"]}
                self.agent["state"] = "waiting"; self.event("waiting")
                self.controller.notify({"event": "phase-permit-needed", "binding_sha256": self.agent["binding_sha256"],
                    "worker_nonce": self.agent["worker_nonce"], "selection": copy.deepcopy(self.agent["selection"])})
                self.needed = True
            if channel_eof_before_exit and self.rpc_returned.is_set() and not self.channel_closed:
                self.controller.stop()
                self.controller.channel.close()
                self.channel_closed = True
            if not hang and self.rpc_returned.is_set() and clock.now >= self.started + finish_after:
                self.controller.stop()
                prefix = (self.cwd / "trace.jsonl").read_bytes()
                after_worker, state["after_worker"] = state["after_worker"], None
                try: run = inert_result_runner(self.argv, self.stdout, self.stderr, timeout=35, cwd=self.cwd)
                finally: state["after_worker"] = after_worker
                result = R.load_json(self.stdout)
                result.update(identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity),
                              pre_actuation_ack=copy.deepcopy(self.agent))
                if not self.agent["native_entry"]:
                    result.update(status="incomplete", operations=[], native_calls=0, invocations=0,
                                  error={"type": "PermitDenied", "message": "inert actual J denial"})
                self.stdout.write_text(json.dumps(result))
                (self.cwd / "trace.jsonl").write_bytes(prefix + (b'{"event":"operation-returned"}\n' if self.agent["native_entry"] else b""))
                if after_worker: after_worker()
                self.returncode = run["returncode"] if self.agent["native_entry"] else 1
                self.completed = True
                self.controller.channel.close()
            return self.returncode

        def wait(self, *, timeout):
            self.waits.append(timeout)
            if self.poll() is None: raise subprocess.TimeoutExpired(self.argv, timeout)
            return self.returncode

    def killpg(pid, signum):
        assert len(processes) == 1 and pid == processes[0].pid
        signals.append((pid, signum))
        if signum != B.signal.SIGTERM or not ignore_term:
            proc = processes[0]
            proc.controller.stop_requested.set()
            proc.controller.thread.join(timeout=1)
            proc.controller.channel.close()
            proc.returncode = -signum
    monkeypatch.setattr(B.subprocess, "Popen", Process)
    monkeypatch.setattr(B.os, "killpg", killpg)
    return request, clock, processes, signals


def _phase_settlement(context, clock, monkeypatch, *, complete=True):
    """Construct genuine S15 records from its inert AX backend, not a passed task."""
    from anyuse.execution import rea_ax_state as S
    from test_rea_ax_state import Backend, SELECTOR
    backend = Backend(pid=context["expected_identity"]["pid"], launched=context["expected_identity"]["launch_time"])
    backend.identity = copy.deepcopy(context["expected_identity"])
    backend.current["identity"] = copy.deepcopy(backend.identity)
    backend.current["kernel"].update(uid=backend.identity["uid"], executable=backend.identity["executable"])
    backend.current["session"]["uid"] = backend.identity["uid"]
    backend.now = clock.now
    if not complete: backend.checkbox.values["AXValue"] = (-25204, None)
    observed = S._capture(context["target"], backend.identity, SELECTOR,
                          deadline=context["deadline_monotonic"], backend=backend, clock=clock.clock)
    with monkeypatch.context() as patch:
        def capture(*args, **kwargs):
            clock.now += .05
            return copy.deepcopy(observed)
        patch.setattr(S, "_capture", capture)
        return S._settle(context["target"], backend.identity, SELECTOR,
                         deadline=context["deadline_monotonic"], interval=.4, max_samples=2,
                         backend=backend, clock=clock.clock, sleep=clock.sleep)


def _owned_phase_settlement(context, clock, monkeypatch, *, complete=True):
    """Real owned-reader capture/settle on its explicit inert backend; no S conversion."""
    from anyuse.execution import rea_owned_ax_state as O
    from test_rea_owned_ax_state import Backend as OwnedBackend
    from test_rea_ax_state import SELECTOR
    backend = OwnedBackend(pid=context["expected_identity"]["pid"],
                           launched=context["expected_identity"]["launch_time"])
    backend.identity = copy.deepcopy(context["expected_identity"])
    backend.current["identity"] = copy.deepcopy(backend.identity)
    backend.current["kernel"].update(uid=backend.identity["uid"], executable=backend.identity["executable"])
    backend.current["session"]["uid"] = backend.identity["uid"]
    backend.now = clock.now
    if not complete:
        backend.checkbox.values["AXValue"] = (-25204, None)
    actual_capture = O._capture
    with monkeypatch.context() as patch:
        def capture(*args, **kwargs):
            clock.now += .05
            backend.now = clock.now
            return actual_capture(*args, **kwargs)
        patch.setattr(O, "_capture", capture)
        return O._settle(context["target"], backend.identity, SELECTOR,
                         deadline=context["deadline_monotonic"], interval=.4, max_samples=2,
                         backend=backend, clock=clock.clock, sleep=clock.sleep)


def test_phased_collect_orders_hooks_around_one_owned_worker_without_a_task_oracle(runtime, native_files, monkeypatch):
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    def hook(phase, context):
        calls.append(phase)
        if phase == "setup": return {"api_returned": True}  # Not an effect claim.
        return _phase_settlement(context, clock, monkeypatch)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    assert calls == ["setup", "before", "after"] and len(processes) == len(state["calls"]) == 1 and signals == []
    assert result["phases_complete"] and result["runtime"]["status"] == "executed"
    assert result["runtime"]["result"]["native_calls"] == result["runtime"]["result"]["invocations"] == 1
    context = result["run_context"]
    assert context["owned_worker"]["reaped"] and processes[0].waits == [0]
    assert context["phases"]["before"]["finished_monotonic"] < context["pre_terminal_deadline_monotonic"]
    assert context["earliest_terminal_monotonic"] == context["worker_anchor_monotonic"] + 20
    assert all(context["phases"][phase]["return"] for phase in calls)
    assert not result["proof_created"] and result["evidence_only"]
    # Subsequent phase journaling never invalidates an already bound native artifact.
    for row in result["runtime"]["artifacts"]:
        if row["sha256"] is not None:
            assert R._file_hash(out / "runtime" / row["file"]) == row["sha256"]
    assert R.load_json(out / "phased.json") == result


@pytest.mark.parametrize("phase", ["setup", "before"])
def test_phased_setup_or_baseline_exception_still_drains_real_owned_child_and_retains_dispatch(
        runtime, native_files, monkeypatch, phase):
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    def hook(actual, context):
        calls.append(actual)
        if actual == phase: raise LookupError("retained host observation failure")
        if actual == "setup": return {"ordinary_setup_return": "uncertain"}
        return _phase_settlement(context, clock, monkeypatch)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    assert not result["phases_complete"] and "after" not in calls
    assert result["runtime"]["status"] == "executed" and result["runtime"]["result"]["native_calls"] == 1
    assert result["runtime"]["actuated"] is True and not result["runtime"]["resource_review_required"]
    assert len(processes) == len(state["calls"]) == 1 and processes[0].completed and processes[0].waits == [0]
    assert signals == [] and result["run_context"]["owned_worker"]["reaped"]
    assert result["run_context"]["phases"][phase]["error"]["type"] == "LookupError"
    if phase == "before": assert R.load_json(out / "setup-return.json") == {"ordinary_setup_return": "uncertain"}


def test_phased_baseline_crossing_earliest_bound_is_rejected_even_without_terminal_trace(runtime, native_files, monkeypatch):
    app, identity, _, state, out = runtime
    request, clock, processes, _ = _phased_fixture(runtime, native_files, monkeypatch, finish_after=25)
    def hook(phase, context):
        if phase == "setup": return {"returned": True}
        settlement = _phase_settlement(context, clock, monkeypatch)
        clock.now = context["earliest_terminal_monotonic"] + .1
        return settlement
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    baseline = result["run_context"]["phases"]["before"]
    assert baseline["return"] and baseline["status"] == "incomplete" and baseline["error"]["type"] == "TimeoutError"
    assert not result["phases_complete"] and result["run_context"]["phases"]["after"] is None
    assert result["runtime"]["result"]["native_calls"] == 1 and processes[0].completed and len(state["calls"]) == 1


@pytest.mark.parametrize("ready_after", [None, 19.5])
def test_phased_missing_or_late_ready_never_runs_setup_and_never_moves_the_earliest_bound(
        runtime, native_files, monkeypatch, ready_after):
    app, identity, _, state, out = runtime
    request, _, processes, _ = _phased_fixture(runtime, native_files, monkeypatch, ready_after=ready_after)
    calls = []
    result = B.collect_phased(app, out, request, expected_identity=identity,
                              phase_hook=lambda *args: calls.append(args), limits=PHASE_LIMITS)
    assert calls == [] and not result["phases_complete"] and result["run_context"]["errors"][0]["phase"] == "ready"
    assert result["runtime"]["status"] == "executed" and len(state["calls"]) == 1 and processes[0].completed
    assert result["run_context"]["earliest_terminal_monotonic"] == 120


def test_phased_incomplete_settlement_is_raw_retained_but_not_credited(runtime, native_files, monkeypatch):
    from anyuse.execution import rea_ax_state as S
    app, identity, _, _, out = runtime
    request, clock, processes, _ = _phased_fixture(runtime, native_files, monkeypatch)
    def hook(phase, context):
        return {} if phase == "setup" else _phase_settlement(context, clock, monkeypatch, complete=False)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    raw = R.load_json(out / "before-settlement.json")
    assert S.validate_settlement(raw) == raw and raw["complete"] is False
    assert result["run_context"]["phases"]["before"]["status"] == "incomplete" and not result["phases_complete"]
    assert result["runtime"]["status"] == "executed" and processes[0].completed


def test_phased_expiry_terminates_only_owned_group_reaps_and_preserves_unknown_native_outcome(runtime, native_files, monkeypatch):
    app, identity, _, _, out = runtime
    request, _, processes, signals = _phased_fixture(runtime, native_files, monkeypatch, ready_after=None, hang=True, ignore_term=True)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=lambda *args: {},
                              limits=PHASE_LIMITS, timeout=25)
    assert signals == [(processes[0].pid, B.signal.SIGTERM), (processes[0].pid, B.signal.SIGKILL)]
    assert result["run_context"]["owned_worker"]["reaped"] and result["run_context"]["owned_worker"]["kill_attempted"]
    assert processes[0].waits == [5, 5, 0] and len(processes) == 1
    assert result["runtime"]["resource_review_required"] and result["runtime"]["native_calls"] is None
    assert result["runtime"]["actuated"] is None and not result["phases_complete"]


def test_phased_raw_hook_return_survives_later_native_cleanup_failure(runtime, native_files, monkeypatch):
    app, identity, _, state, out = runtime
    request, clock, processes, _ = _phased_fixture(runtime, native_files, monkeypatch)
    state["result_changes"]["cleanup"] = {"script_unloaded": False, "detached": True}
    def hook(phase, context):
        return {"retained": "setup returned"} if phase == "setup" else _phase_settlement(context, clock, monkeypatch)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    assert result["runtime"]["resource_review_required"] and result["run_context"]["phases"]["after"] is None
    assert R.load_json(out / "setup-return.json") == {"retained": "setup returned"}
    assert R.load_json(out / "before-settlement.json")["complete"] and result["run_context"]["owned_worker"]["reaped"]
    assert len(processes) == 1 and result["runtime"]["result"]["native_calls"] == 1


def test_phased_identity_drift_after_setup_blocks_baseline_without_adopting_or_erasing_dispatch(runtime, native_files, monkeypatch):
    app, identity, _, state, out = runtime
    request, _, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    def hook(phase, context):
        calls.append(phase)
        monkeypatch.setattr(B, "process_identity", lambda *args: {**identity, "launch_time": identity["launch_time"] + 1})
        return {"ordinary_setup_return": "retained"}
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    assert calls == ["setup"] and not result["phases_complete"] and signals == []
    assert result["run_context"]["phases"]["setup"]["return"] and result["run_context"]["phases"]["before"] is None
    assert "frozen process" in result["run_context"]["phases"]["setup"]["error"]["message"]
    assert result["runtime"]["resource_review_required"] and result["runtime"]["result"]["native_calls"] == 1
    assert len(state["calls"]) == len(processes) == 1 and result["run_context"]["owned_worker"]["reaped"]


def test_phased_hook_interrupt_audits_owned_child_and_does_not_claim_zero_native_effects(runtime, native_files, monkeypatch):
    app, identity, _, _, out = runtime
    request, _, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    def hook(phase, context):
        calls.append(phase)
        raise KeyboardInterrupt("inert interrupted host phase")
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS)
    assert calls == ["setup"] and signals == [(processes[0].pid, B.signal.SIGTERM)]
    assert result["run_context"]["owned_worker"]["reaped"] and processes[0].waits == [5, 0]
    assert result["run_context"]["errors"][0]["error"]["type"] == "KeyboardInterrupt"
    assert result["runtime"]["resource_review_required"] and result["runtime"]["native_calls"] is None
    assert result["runtime"]["actuated"] is None and not result["phases_complete"]


def test_phased_journal_exception_cannot_skip_owned_child_cleanup(runtime, native_files, monkeypatch):
    app, identity, _, _, out = runtime
    request, _, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    original_write, failed = R.write_json, []
    def write(path, value):
        if Path(path).name == "run-context.json" and processes and not failed:
            failed.append(True)
            raise OSError("inert one-shot journal failure after spawn")
        return original_write(path, value)
    monkeypatch.setattr(R, "write_json", write)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=lambda *args: {}, limits=PHASE_LIMITS)
    assert failed == [True] and signals == [(processes[0].pid, B.signal.SIGTERM)]
    assert len(processes) == 1 and result["run_context"]["owned_worker"]["reaped"]
    assert result["run_context"]["errors"][0]["error"]["type"] == "OSError"
    assert result["runtime"]["resource_review_required"] and not result["phases_complete"]


def test_phased_inputs_and_context_are_copied(runtime, native_files, monkeypatch):
    app, identity, _, state, out = runtime
    request, clock, processes, _ = _phased_fixture(runtime, native_files, monkeypatch)
    pin, limits = copy.deepcopy(identity), copy.deepcopy(PHASE_LIMITS)
    def hook(phase, context):
        if phase == "setup":
            pin["pid"], limits["before_seconds"] = 999, 10
            context["expected_identity"]["pid"] = 888
            context["target"]["sha256"] = "0" * 64
            return {"returned": True}
        return _phase_settlement(context, clock, monkeypatch)
    result = B.collect_phased(app, out, request, expected_identity=pin, phase_hook=hook, limits=limits)
    assert result["phases_complete"] and result["run_context"]["expected_identity"] == identity
    assert result["run_context"]["limits"] == PHASE_LIMITS and len(processes) == 1


def test_ordinary_collect_keeps_legacy_runner_without_phase_fixture_overrides(runtime, monkeypatch):
    app, _, _, state, out = runtime  # Fresh original runtime fixture, no phase ledger/counter overrides.
    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary collect entered the phased worker/hook path")
    monkeypatch.setattr(B._PhasedWait, "run", forbidden)
    monkeypatch.setattr(B.subprocess, "Popen", forbidden)
    request = {"operations": [{"op": "classes"}]}
    ordinary = B.collect(app, out, request)
    assert ordinary["status"] == "observed" and len(state["calls"]) == 1
    assert state["calls"][0]["config"]["request"] == request
    assert ordinary["result"]["operations"] == [{"index": 0, "op": "classes", "status": "ok"}]
    assert "lifecycle_observer" not in ordinary["result"]


def test_phased_vm_guard_and_closed_schema_precede_paths_hooks_or_worker(runtime, native_files, monkeypatch):
    app, identity, _, _, out = runtime
    request, _, processes, _ = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    def deny(): raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", deny)
    with pytest.raises(PermissionError, match="VM only"):
        B.collect_phased("absent.app", out, {}, expected_identity={}, phase_hook=lambda *args: calls.append(args), limits={})
    assert not out.exists() and not calls and not processes
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    for altered in ({**PHASE_LIMITS, "expected_state": {}}, {**PHASE_LIMITS, "margin_seconds": False}):
        with pytest.raises(ValueError):
            B.collect_phased("absent.app", out, request, expected_identity=identity, phase_hook=lambda *args: {}, limits=altered)
    with pytest.raises(ValueError, match="one operation"):
        B.collect_phased("absent.app", out, {"operations": [{"op": "classes"}]},
                          expected_identity=identity, phase_hook=lambda *args: {}, limits=PHASE_LIMITS)
    assert not out.exists() and not processes


def test_phased_margin_equal_to_real_observation_window_refuses_before_paths_or_hook(runtime, native_files):
    _, identity, _, state, out = runtime
    request, recipe, _ = _lifecycle_request_files(native_files, for_call=True)
    assert recipe["root"]["lifecycle"]["observe_ms"] == 1000
    hooks = []
    with pytest.raises(ValueError, match="margin leaves no conservative pre-terminal window"):
        B.collect_phased("absent.app", out, request, expected_identity=identity,
                          phase_hook=lambda *args: hooks.append(args), limits=PHASE_LIMITS)
    assert not hooks and not state["calls"] and not out.exists()


@pytest.mark.parametrize("capability", [None, False, 1, {}, [], "unknown/1",
                                        "anyuse.execution.rea_owned_ax_state", {"capability": "ax_owned_visible_readonly/1"}])
def test_phased_observer_registry_refuses_unknown_or_forged_capability_before_bundle_io_or_worker(
        runtime, native_files, monkeypatch, capability):
    _, identity, _, state, out = runtime
    request, _, processes, _ = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    with pytest.raises(ValueError):
        B.collect_phased("absent.app", out, request, expected_identity=identity,
                         phase_hook=lambda *args: calls.append(args), limits=PHASE_LIMITS,
                         observer_capability=capability)
    assert not out.exists() and not calls and not processes and state["calls"] == []


@pytest.mark.parametrize("explicit", [False, True])
def test_default_and_explicit_strict_observer_keep_exact_legacy_phase_versions_and_fields(
        runtime, native_files, monkeypatch, explicit):
    from anyuse.execution import rea_state_reader as SR
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    contexts = []
    def hook(phase, context):
        contexts.append(copy.deepcopy(context))
        assert "observer_capability" not in context
        return {"ordinary_setup_returned": True} if phase == "setup" else _phase_settlement(context, clock, monkeypatch)
    options = {"observer_capability": SR.STRICT_CAPABILITY} if explicit else {}
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook,
                              limits=PHASE_LIMITS, **options)
    assert result["format"] == B.PHASED == "anyuse.rea-runtime-phased/1"
    assert result["run_context"]["format"] == B.PHASE_CONTEXT == "anyuse.rea-runtime-phase-context/1"
    assert "observer_capability" not in result and "observer_capability" not in result["run_context"]
    assert set(result) == {"format", "evidence_only", "proof_created", "phases_complete", "runtime", "run_context"}
    assert result["phases_complete"] and [row["phase"] for row in contexts] == ["setup", "before", "after"]
    assert result["runtime"]["result"]["native_calls"] == result["runtime"]["result"]["invocations"] == 1
    assert len(processes) == len(state["calls"]) == 1 and signals == []


def test_ordinary_collect_never_selects_a_result_observer_capability(runtime, monkeypatch):
    from anyuse.execution import rea_state_reader as SR
    app, _, _, state, out = runtime
    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary collect selected the phased observer registry")
    monkeypatch.setattr(SR, "for_capability", forbidden)
    result = B.collect(app, out, {"operations": [{"op": "classes"}]})
    assert result["status"] == "observed" and len(state["calls"]) == 1
    assert result["result"]["operations"] == [{"index": 0, "op": "classes", "status": "ok"}]


def test_owned_phased_observer_is_frozen_before_worker_and_never_changes_the_native_request(
        runtime, native_files, monkeypatch):
    from anyuse.execution import rea_state_reader as SR
    from anyuse.execution import rea_owned_ax_state as O
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    popen = B.subprocess.Popen
    before_worker, calls = [], []
    def launch(*args, **kwargs):
        context = R.load_json(out / "run-context.json")
        assert context["format"] == B.PHASE_CONTEXT2 and context["observer_capability"] == SR.OWNED_CAPABILITY
        assert context["request_sha256"] == R.digest(request)
        assert context["phases"] == {"setup": None, "before": None, "after": None}
        before_worker.append(context)
        return popen(*args, **kwargs)
    monkeypatch.setattr(B.subprocess, "Popen", launch)
    def hook(phase, context):
        assert context["observer_capability"] == SR.OWNED_CAPABILITY
        calls.append(phase)
        if phase == "setup":
            context["observer_capability"] = SR.STRICT_CAPABILITY  # Only this copied callback input changes.
            return {"ordinary_setup_returned": True}
        return _owned_phase_settlement(context, clock, monkeypatch)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook,
                              limits=PHASE_LIMITS, observer_capability=SR.OWNED_CAPABILITY)
    assert result["format"] == B.PHASED2 == "anyuse.rea-phased-runtime/2"
    assert result["observer_capability"] == result["run_context"]["observer_capability"] == SR.OWNED_CAPABILITY
    assert result["run_context"]["format"] == B.PHASE_CONTEXT2 == "anyuse.rea-native-run-context/2"
    assert result["phases_complete"] and calls == ["setup", "before", "after"]
    assert len(before_worker) == len(processes) == len(state["calls"]) == 1 and signals == []
    assert state["calls"][0]["config"]["request"] == request
    assert "observer_capability" not in state["calls"][0]["config"]
    assert result["runtime"]["result"]["native_calls"] == result["runtime"]["result"]["invocations"] == 1
    for phase in ("before", "after"):
        settlement = R.load_json(out / (phase + "-settlement.json"))
        assert O.validate_settlement(settlement)["complete"] is True
        observation = settlement["samples"][0]["observation"]
        assert "focus" not in observation["state"]
        assert observation["context"]["before"]["focus"]["focused_pid"] is None
    assert result["evidence_only"] and not result["proof_created"]


@pytest.mark.parametrize("selected_owned", [False, True])
def test_phased_selected_capability_cannot_be_replaced_by_a_foreign_valid_settlement_format(
        runtime, native_files, monkeypatch, selected_owned):
    from anyuse.execution import rea_state_reader as SR
    from anyuse.execution import rea_ax_state as S
    from anyuse.execution import rea_owned_ax_state as O
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    calls, raw = [], []
    def hook(phase, context):
        calls.append(phase)
        if phase == "setup": return {"ordinary_setup_returned": True}
        value = (_phase_settlement if selected_owned else _owned_phase_settlement)(context, clock, monkeypatch)
        assert (S if selected_owned else O).validate_settlement(value)["complete"] is True
        raw.append(copy.deepcopy(value))
        return value
    cap = SR.OWNED_CAPABILITY if selected_owned else SR.STRICT_CAPABILITY
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook,
                              limits=PHASE_LIMITS, observer_capability=cap)
    before = result["run_context"]["phases"]["before"]
    assert not result["phases_complete"] and before["status"] == "incomplete"
    assert before["error"]["type"] == "ValueError" and before["return"]
    assert R.load_json(out / "before-settlement.json") == raw[0]
    assert calls == ["setup", "before"] and result["run_context"]["phases"]["after"] is None
    # Observer refusal is not dispatch cancellation: retain the sole actual call.
    assert result["runtime"]["status"] == "executed" and result["runtime"]["actuated"] is True
    assert result["runtime"]["result"]["native_calls"] == result["runtime"]["result"]["invocations"] == 1
    assert len(processes) == len(state["calls"]) == 1 and processes[0].completed and signals == []


@pytest.mark.parametrize("phase", ["before", "after"])
def test_owned_phased_incomplete_actual_prefix_is_retained_without_completeness_or_zero_call_claim(
        runtime, native_files, monkeypatch, phase):
    from anyuse.execution import rea_state_reader as SR
    from anyuse.execution import rea_owned_ax_state as O
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _phased_fixture(runtime, native_files, monkeypatch)
    calls = []
    def hook(actual, context):
        calls.append(actual)
        return ({"ordinary_setup_returned": True} if actual == "setup" else
                _owned_phase_settlement(context, clock, monkeypatch, complete=actual != phase))
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook,
                              limits=PHASE_LIMITS, observer_capability=SR.OWNED_CAPABILITY)
    retained = R.load_json(out / (phase + "-settlement.json"))
    assert O.validate_settlement(retained)["complete"] is False and retained["stable_pair"] is None
    assert retained["samples"] and retained["samples"][0]["observation"]["state"]["node_count"] > 0
    row = result["run_context"]["phases"][phase]
    assert row["return"] and row["status"] == "incomplete" and row["error"]["type"] == "ValueError"
    assert not result["phases_complete"] and not result["proof_created"]
    if phase == "before": assert calls == ["setup", "before"] and result["run_context"]["phases"]["after"] is None
    else: assert calls == ["setup", "before", "after"]
    assert result["runtime"]["result"]["native_calls"] == result["runtime"]["result"]["invocations"] == 1
    assert result["runtime"]["status"] == "executed" and len(processes) == len(state["calls"]) == 1
    assert signals == []


@pytest.mark.parametrize("capability,eof_before_exit", [
    ("ax_focused_readonly/1", False), ("ax_owned_visible_readonly/1", False),
    ("ax_focused_readonly/1", True),
])
def test_acknowledged_real_channel_binds_fresh_phases_and_one_actual_forward(
        runtime, native_files, monkeypatch, capability, eof_before_exit):
    from anyuse.execution import rea_runtime_permit as RP
    from test_rea_ax_state import SELECTOR as WINDOW_SELECTOR
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _acknowledged_fixture(runtime, native_files, monkeypatch,
        channel_eof_before_exit=eof_before_exit)
    def hook(phase, context):
        if phase == "setup": return {"format": RP.SETUP, "status": "returned", "result": {"ordinary_setup": "returned"}}
        settle = _owned_phase_settlement if capability == "ax_owned_visible_readonly/1" else _phase_settlement
        return settle(context, clock, monkeypatch)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook,
        limits=PHASE_LIMITS, observer_capability=capability, require_pre_actuation_ack=True, observer_selector=WINDOW_SELECTOR)
    assert result["format"] == B.PHASED3 and result["run_context"]["format"] == B.PHASE_CONTEXT3
    assert result["phases_complete"] and result["runtime"]["status"] == "executed"
    assert len(processes) == len(state["calls"]) == 1 and signals == []
    assert processes[0].completed and processes[0].waits == [0]
    context = result["run_context"]
    assert context["observer_capability"] == result["observer_capability"] == capability
    assert context["pre_actuation_ack"] == result["pre_actuation_ack"] == B._permit_pin(out / "pre-actuation-parent.json")
    assert context["runtime_timeout_seconds"] == 35 and context["permit_deadline_monotonic"] == context["worker_anchor_monotonic"] + 32
    parent = B._permit_read(result["pre_actuation_ack"])
    worker = R.load_json(out / "runtime/worker-permit.json")
    observed = R.load_json(out / "runtime/worker-output.json")
    checkpoint = B._permit_read(parent["decision"]["checkpoint"])
    summary = RP.validate_completed(parent, worker, observed["pre_actuation_ack"], checkpoint,
        binding=parent["binding"], setup_pin=context["phases"]["setup"]["return"],
        before_pin=context["phases"]["before"]["return"], checkpoint_pin=parent["decision"]["checkpoint"],
        native_calls=observed["native_calls"], invocations=observed["invocations"])
    assert summary["proof_created"] is False and not worker["parent_eof"]
    assert [row["kind"] for row in observed["pre_actuation_ack"]["events"]].count("native_entry") == 1
    assert len([event for event in worker["events"] if event["kind"] == "post_started"]) == 1


@pytest.mark.parametrize("failure", ["setup_exception", "opaque_setup", "incomplete_before"])
def test_acknowledged_failed_fresh_phase_denies_real_forward_and_retains_actual_zero(
        runtime, native_files, monkeypatch, failure):
    from anyuse.execution import rea_runtime_permit as RP
    from test_rea_ax_state import SELECTOR as WINDOW_SELECTOR
    app, identity, _, state, out = runtime
    request, clock, processes, signals = _acknowledged_fixture(runtime, native_files, monkeypatch)
    def hook(phase, context):
        if phase == "setup":
            if failure == "setup_exception": raise LookupError("inert setup failed before permission")
            if failure == "opaque_setup": return {"returned": True}
            return {"format": RP.SETUP, "status": "returned", "result": {"ordinary_setup": "returned"}}
        return _phase_settlement(context, clock, monkeypatch, complete=failure != "incomplete_before")
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS,
        require_pre_actuation_ack=True, observer_selector=WINDOW_SELECTOR)
    raw = R.load_json(out / "runtime/worker-output.json")
    parent = B._permit_read(result["pre_actuation_ack"])
    assert not result["phases_complete"] and result["run_context"]["phases"]["after"] is None
    assert parent["decision"]["decision"] == "deny" and parent["sent"] is True
    assert raw["status"] == "incomplete" and raw["operations"] == []
    assert raw["native_calls"] == raw["invocations"] == 0 and raw["pre_actuation_ack"]["native_entry"] is False
    assert raw["pre_actuation_ack"]["permit_consumed"] is False
    assert result["runtime"]["result"]["native_calls"] == 0 and result["runtime"]["resource_review_required"] is True
    assert len(processes) == len(state["calls"]) == 1 and signals == []
    if failure == "opaque_setup": assert R.load_json(out / "setup-return.json") == {"returned": True}
    if failure == "incomplete_before": assert R.load_json(out / "before-settlement.json")["complete"] is False


def test_acknowledged_rejects_legacy_compiled_bundle_before_worker_arming(runtime, native_files, monkeypatch):
    from test_rea_ax_state import SELECTOR as WINDOW_SELECTOR
    app, identity, adapter, state, out = runtime
    request, _, processes, signals = _acknowledged_fixture(runtime, native_files, monkeypatch)
    build = R.load_json(str(adapter) + ".manifest.json")
    build.pop("interlock_protocol")
    R.write_json(str(adapter) + ".manifest.json", build)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=lambda *args: {}, limits=PHASE_LIMITS,
        require_pre_actuation_ack=True, observer_selector=WINDOW_SELECTOR)
    assert result["runtime"]["worker"] is None and result["runtime"]["actuated"] is False
    assert "compiled fixed adapter" in result["runtime"]["error"]["message"]
    assert state["calls"] == processes == signals == [] and not result["phases_complete"]


def test_acknowledged_interruption_immediately_after_popen_still_audits_owned_child(runtime, native_files, monkeypatch):
    from anyuse.execution import rea_runtime_permit as RP
    from test_rea_ax_state import SELECTOR as WINDOW_SELECTOR
    app, identity, _, _, out = runtime
    request, _, processes, signals = _acknowledged_fixture(runtime, native_files, monkeypatch, hang=True)
    actual = B._permit_event
    def event(journal, kind):
        if journal["format"] == RP.PARENT and kind == "worker_started":
            raise KeyboardInterrupt("inert interruption after actual fake Popen")
        return actual(journal, kind)
    monkeypatch.setattr(B, "_permit_event", event)
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=lambda *args: {}, limits=PHASE_LIMITS,
        require_pre_actuation_ack=True, observer_selector=WINDOW_SELECTOR)
    owned = result["run_context"]["owned_worker"]
    assert len(processes) == 1 and signals == [(processes[0].pid, B.signal.SIGTERM)]
    assert owned["reaped"] and owned["term_attempted"] and owned["returncode"] == -B.signal.SIGTERM
    assert result["runtime"]["actuated"] is None and result["runtime"]["native_calls"] is None
    assert result["runtime"]["resource_review_required"] and not result["phases_complete"]


def test_acknowledged_stop_during_actual_forward_recheck_never_posts_after_cleanup_request(runtime, native_files, monkeypatch):
    from anyuse.execution import rea_runtime_permit as RP
    from test_rea_ax_state import SELECTOR as WINDOW_SELECTOR
    app, identity, _, _, out = runtime
    request, clock, processes, signals = _acknowledged_fixture(runtime, native_files, monkeypatch,
        stop_during_recheck=True, hang=True)
    def hook(phase, context):
        return ({"format": RP.SETUP, "status": "returned", "result": {}} if phase == "setup" else
                _phase_settlement(context, clock, monkeypatch))
    result = B.collect_phased(app, out, request, expected_identity=identity, phase_hook=hook, limits=PHASE_LIMITS,
        require_pre_actuation_ack=True, observer_selector=WINDOW_SELECTOR)
    proc = processes[0]
    assert proc.recheck_entered.is_set() and proc.recheck_release.is_set() and isinstance(proc.stop_error, TimeoutError)
    assert proc.agent["native_entry"] is False and proc.controller.data["post_returned"] is False
    assert not any(event["kind"] == "post_returned" for event in proc.controller.data["events"])
    assert result["runtime"]["actuated"] is None and result["runtime"]["native_calls"] is None
    assert result["runtime"]["resource_review_required"] and len(signals) == 1


@pytest.mark.parametrize("enabled,selector", [(1, None), (None, None), ("true", None), (False, {}), (True, None), (True, {})])
def test_acknowledged_trusted_options_reject_before_any_bundle_io(runtime, enabled, selector):
    _, identity, _, state, out = runtime
    with pytest.raises(ValueError):
        B.collect_phased("/absent/application", out, {"operations": []}, expected_identity=identity,
            phase_hook=lambda *args: {}, limits=PHASE_LIMITS, require_pre_actuation_ack=enabled, observer_selector=selector)
    assert not out.exists() and state["calls"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
