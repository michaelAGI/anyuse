"""Independent backend observations; run only in the guest guarded suite."""
import copy
import json
import os
from pathlib import Path
import plistlib

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_state_observer as S


READ = {"operations": [{"op": "read", "receiver": {"class": "MappedStore", "kind": "unique", "subclasses": False},
                       "fields": [{"name": "text", "kind": "getter", "selector": "text", "type": "utf8", "signature": "@16@0:8"},
                                  {"name": "count", "kind": "ivar", "ivar": "_count", "type": "int32", "encoding": "i"}]}]}


def seal(schema):
    path = Path(schema["contract"]["file"])
    path.write_text(json.dumps({"observer": S.declaration(schema)}, ensure_ascii=False))
    schema["contract"]["sha256"] = R._file_hash(path)
    return schema


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = (tmp_path / "Target.app").resolve()
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.test.backend",
                                                                   "CFBundleExecutable": "target", "CFBundleVersion": "1"}))
    executable = app / "Contents" / "MacOS" / "target"
    executable.write_bytes(b"inert executable fixture, never launched")
    working = (tmp_path / "working.json").resolve()
    working.write_text(json.dumps({"name": "final", "items": [1, True, "Grüße 東京"]}))
    expected = (tmp_path / "expected.json").resolve()
    expected.write_text(json.dumps({"document": {"name": "final", "items": [1, True, "Grüße 東京"]}}))
    contract = (tmp_path / "contract.json").resolve()
    schema = {"format": S.SCHEMA, "kind": "file_state", "target": R.fingerprint(app),
              "params": [{"name": "output", "type": "path", "role": "working_output"},
                         {"name": "expected", "type": "path", "role": "independent_reference"},
                         {"name": "expected_sha", "type": "sha256", "role": "reference_hash"}],
              "fields": {"document": {"type": "json", "source": {"input": "output"}}},
              "reference": {"format": "json", "file": {"input": "expected"}, "sha256": {"input": "expected_sha"}},
              "contract": {"file": str(contract), "sha256": "0" * 64, "pointer": "/observer"}}
    inputs = {"output": str(working), "expected": str(expected), "expected_sha": R._file_hash(expected)}
    return app, seal(schema), inputs, tmp_path


def runtime_files(files, monkeypatch, *, request=None, expected=None):
    app, schema, inputs, root = files
    schema["kind"] = "runtime_state"
    schema["params"] = [param for param in schema["params"] if param["name"] != "output"]
    inputs.pop("output")
    schema["request"] = copy.deepcopy(READ if request is None else request)
    schema["fields"] = {field["name"]: {"type": field["type"], "source": {"operation": index, "field": field["name"]}}
                        for index, operation in enumerate(schema["request"]["operations"]) for field in operation["fields"]}
    expected = {"text": "Complete native state", "count": 4} if expected is None else expected
    Path(inputs["expected"]).write_text(json.dumps(expected))
    inputs["expected_sha"] = R._file_hash(Path(inputs["expected"]))
    seal(schema)
    adapter = {"source_sha256": "a" * 64, "bundle_sha256": "b" * 64, "transport": {"kind": "local"}}
    monkeypatch.setattr(B, "adapter_identity", lambda **_: copy.deepcopy(adapter))
    monkeypatch.setattr(B, "request_resources", lambda _: {})
    identity = {"bundle_id": schema["target"]["bundle_id"], "bundle": str(app),
                "executable": str(app / "Contents" / "MacOS" / "target"), "pid": 42, "launch_time": 123.5, "uid": os.getuid()}
    state = {"values": copy.deepcopy(expected), "tamper": None, "calls": 0}

    def collect(bundle, out, request, **kwargs):
        state["calls"] += 1
        rows = []
        for index, operation in enumerate(request["operations"]):
            fields = []
            for field in operation["fields"]:
                item = {"name": field["name"], "type": field["type"], "value": state["values"][field["name"]]}
                if operation["op"] == "read":
                    item.update(kind=field["kind"], metadata={"signature": field["signature"], "selector": field["selector"]}
                                if field["kind"] == "getter" else {"name": field["ivar"], "encoding": field["encoding"]})
                else:
                    item["offset"] = field["offset"]
                fields.append(item)
            rows.append({"index": index, "op": operation["op"], "status": "ok", "fields": fields})
        result = {"status": "completed", "attached": True, "handles_expired": True, "native_state_uncertain": False,
                  "invocations": 0, "native_calls": 1, "helper_load_attempts": 0,
                  "request_sha256": R.digest(request), "native_resources": B.request_resources(request),
                  "cleanup": {"script_unloaded": True, "detached": True}, "operations": rows}
        receipt = {"status": "observed", "resource_review_required": False, "actuated": False,
                   "target": schema["target"], "identity": copy.deepcopy(identity), "after_identity": copy.deepcopy(identity),
                   "adapter": copy.deepcopy(adapter), "request": copy.deepcopy(request), "request_sha256": R.digest(request),
                   "native_resources": B.request_resources(request), "result": result, "receipt": str(Path(out) / "runtime.json")}
        if state["tamper"] is not None:
            state["tamper"](receipt)
        return receipt

    monkeypatch.setattr(B, "collect", collect)
    return app, schema, inputs, root, state


def observe(case, name="observation", **kwargs):
    app, schema, inputs, root, *_ = case
    return S.collect(app, schema, inputs, root / name, **kwargs)


def test_schema_is_data_only_and_pins_the_exact_independent_read_request(files, monkeypatch):
    case = runtime_files(files, monkeypatch)
    _, schema, inputs, _, _ = case
    saved = copy.deepcopy(schema)
    checked = S.validate(schema)
    bound = S.bind(schema, inputs)
    declaration = S.declaration(schema)
    assert declaration["request_sha256"] == R.digest(READ)
    assert declaration["purpose"] == "complete_application_state" and declaration["read_only"] is True
    assert declaration["input_echo_or_helper_completion"] is False
    assert bound["reference"] == {"format": "json", "file": inputs["expected"], "sha256": inputs["expected_sha"]}
    checked["request"]["operations"][0]["fields"].clear()
    assert schema == saved
    with pytest.raises(ValueError, match="parameter names"):
        S.bind(schema, {**inputs, "undeclared": "data"})


def test_guest_guard_precedes_all_io_and_runtime_work(files, monkeypatch):
    app, schema, inputs, root = files
    def deny():
        raise PermissionError("not guest")
    monkeypatch.setattr(R, "assert_guest", deny)
    monkeypatch.setattr(Path, "lstat", lambda *_: pytest.fail("file access before guard"))
    monkeypatch.setattr(B, "collect", lambda *_a, **_k: pytest.fail("runtime before guard"))
    with pytest.raises(PermissionError, match="not guest"):
        S.collect(app, schema, inputs, root / "never-created")
    with pytest.raises(PermissionError, match="not guest"):
        S.resources(schema, inputs)


def test_file_observer_preserves_the_entire_typed_json_and_only_creates_evidence(files):
    result = observe(files)
    assert result["status"] == "observed" and result["complete"] is True and result["matches"] is True
    assert result["values"] == {"document": {"name": "final", "items": [1, True, "Grüße 東京"]}}
    assert result["provider"] == "working_file" and result["cleanup"] == {"read_handles_closed": True}
    assert result["proof_created"] is False and result["evidence_only"] is True and result["model_calls"] == 0
    assert result["resources"]["immutable"][files[2]["expected"]]["sha256"] == files[2]["expected_sha"]


def test_partial_file_json_is_refuted_even_if_it_contains_expected_words(files):
    Path(files[2]["output"]).write_text(json.dumps({"name": "final", "items": [1]}))
    result = observe(files)
    assert result["status"] == "refuted" and result["complete"] is True and result["matches"] is False


def test_plain_text_is_compared_whole_including_whitespace_and_unicode(files):
    app, schema, inputs, root = files
    schema["fields"]["document"]["type"] = "utf8"
    schema["reference"]["format"] = "utf8"
    text = "  Complete text\nGrüße 東京\n"
    Path(inputs["output"]).write_text(text)
    Path(inputs["expected"]).write_text(text)
    inputs["expected_sha"] = R._file_hash(Path(inputs["expected"]))
    seal(schema)
    assert observe(files)["values"] == {"document": text}
    Path(inputs["output"]).write_text(text.strip())
    assert observe(files, "trimmed")["status"] == "refuted"


@pytest.mark.parametrize("raw", ['{"name":1,"name":2}', '{"name":NaN}', '{"name":1e999}', '{"name":', '\\xff'])
def test_invalid_duplicate_or_nonfinite_complete_json_is_unknown(files, raw):
    Path(files[2]["output"]).write_bytes(b"\xff" if raw == '\\xff' else raw.encode())
    result = observe(files)
    assert result["status"] == "unknown" and result["complete"] is False and result["matches"] is None


@pytest.mark.parametrize("how", ["same_path", "hardlink", "symlink", "parent_symlink", "relative", "dot", "directory"])
def test_working_outputs_cannot_alias_independent_oracles_or_use_uncanonical_files(files, how):
    app, schema, inputs, root = files
    output, expected = Path(inputs["output"]), Path(inputs["expected"])
    if how == "same_path":
        inputs["output"] = str(expected)
    elif how == "hardlink":
        output.unlink(); os.link(expected, output)
    elif how == "symlink":
        output.unlink(); output.symlink_to(expected)
    elif how == "parent_symlink":
        link = root / "linked"; link.symlink_to(root, target_is_directory=True)
        inputs["output"] = str(link / output.name)
    elif how == "relative":
        inputs["output"] = output.name
    elif how == "dot":
        inputs["output"] = str(root) + "/./" + output.name
    else:
        output.unlink(); output.mkdir()
    result = observe(files)
    assert result["status"] == ("ambiguous" if how in {"same_path", "hardlink"} else "unknown")
    assert result["complete"] is False


@pytest.mark.parametrize("part", ["expected", "contract", "contract_read_only", "request_hash"])
def test_independent_references_and_read_only_declaration_are_rechecked_before_runtime(files, monkeypatch, part):
    case = runtime_files(files, monkeypatch)
    _, schema, inputs, _, state = case
    if part == "expected":
        Path(inputs["expected"]).write_text('{"text":"different","count":4}')
    elif part == "contract":
        Path(schema["contract"]["file"]).write_text('{}')
    else:
        path = Path(schema["contract"]["file"])
        body = json.loads(path.read_text())
        body["observer"]["read_only" if part == "contract_read_only" else "request_sha256"] = False if part == "contract_read_only" else "f" * 64
        path.write_text(json.dumps(body)); schema["contract"]["sha256"] = R._file_hash(path)
    result = observe(case)
    assert result["status"] == "unknown" and result["complete"] is False and state["calls"] == 0


def test_complete_runtime_state_is_independent_of_actor_and_exactly_typed(files, monkeypatch):
    result = observe(runtime_files(files, monkeypatch))
    assert result["status"] == "observed" and result["complete"] and result["matches"]
    assert result["values"] == {"text": "Complete native state", "count": 4}
    assert result["provider"] == "frida_native_read" and result["resource_review_required"] is False
    assert result["identity"]["pid"] == 42 and result["runtime_receipt"].endswith("native/runtime.json")
    assert result["proof_created"] is False


@pytest.mark.parametrize("part", ["parent_hash", "worker_hash", "request", "adapter", "identity", "launch_time", "uid",
                                   "worker_cleanup", "expired", "uncertain", "actuated", "invocations", "helper_load",
                                   "partial_fields", "field_name", "field_type", "value_type", "signature", "ivar_encoding", "row_index"])
def test_runtime_state_refuses_incomplete_unbound_mutating_or_unclean_results(files, monkeypatch, part):
    case = runtime_files(files, monkeypatch)
    state = case[-1]
    def tamper(receipt):
        result = receipt["result"]
        field = result["operations"][0]["fields"][0]
        if part == "parent_hash": receipt["request_sha256"] = "f" * 64
        elif part == "worker_hash": result["request_sha256"] = "f" * 64
        elif part == "request": receipt["request"]["operations"][0]["fields"].pop()
        elif part == "adapter": receipt["adapter"]["source_sha256"] = "f" * 64
        elif part == "identity": receipt["after_identity"]["pid"] += 1
        elif part == "launch_time": receipt["identity"].pop("launch_time"); receipt["after_identity"].pop("launch_time")
        elif part == "uid": receipt["identity"]["uid"] += 1; receipt["after_identity"]["uid"] += 1
        elif part == "worker_cleanup": result["cleanup"]["detached"] = False
        elif part == "expired": result["handles_expired"] = False
        elif part == "uncertain": result["native_state_uncertain"] = True
        elif part == "actuated": receipt["actuated"] = True
        elif part == "invocations": result["invocations"] = 1
        elif part == "helper_load": result["helper_load_attempts"] = 1
        elif part == "partial_fields": result["operations"][0]["fields"].pop()
        elif part == "field_name": field["name"] = "actor_completion"
        elif part == "field_type": field["type"] = "object"
        elif part == "value_type": result["operations"][0]["fields"][1]["value"] = True
        elif part == "signature": field["metadata"]["signature"] = "v16@0:8"
        elif part == "ivar_encoding": result["operations"][0]["fields"][1]["metadata"]["encoding"] = "B"
        elif part == "row_index": result["operations"][0]["index"] = False
    state["tamper"] = tamper
    result = observe(case)
    assert result["status"] == "unknown" and result["complete"] is False and result["matches"] is None
    if part in {"worker_cleanup", "expired", "uncertain", "actuated", "invocations", "helper_load"}:
        assert result["resource_review_required"] is True


def test_complete_wrong_native_state_is_refuted_without_retry_or_resource_ambiguity(files, monkeypatch):
    case = runtime_files(files, monkeypatch)
    case[-1]["values"]["count"] = 3
    result = observe(case)
    assert result["status"] == "refuted" and result["complete"] and result["matches"] is False
    assert result["resource_review_required"] is False and case[-1]["calls"] == 1


def test_immutable_expected_change_during_runtime_invalidates_the_capture(files, monkeypatch):
    case = runtime_files(files, monkeypatch)
    case[-1]["tamper"] = lambda _: Path(case[2]["expected"]).write_text('{}')
    result = observe(case)
    assert result["status"] == "unknown" and result["complete"] is False
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}


@pytest.mark.parametrize("message,status", [("need one unambiguous running target app", "ambiguous"),
                                            ("unique live instance: at least 2", "ambiguous"),
                                            ("attachment denied", "unknown")])
def test_explicit_unattached_denial_preserves_unknown_or_ambiguous_without_a_success(files, monkeypatch, message, status):
    case = runtime_files(files, monkeypatch)
    def denial(receipt):
        receipt.update(status="unavailable", error={"type": "Unavailable", "message": message})
        receipt["result"].update(status="unavailable", attached=False, invocations=0, native_calls=0)
    case[-1]["tamper"] = denial
    result = observe(case)
    assert result["status"] == status and not result["complete"] and result["resource_review_required"] is False


def test_before_observation_must_be_same_process_complete_and_expected_must_be_new(files, monkeypatch):
    case = runtime_files(files, monkeypatch)
    state = case[-1]
    state["values"] = {"text": "Old complete state", "count": 1}
    before = observe(case, "before")
    assert before["status"] == "refuted" and before["complete"]
    state["values"] = {"text": "Complete native state", "count": 4}
    after = observe(case, "after", before=before, require_change=True)
    assert after["status"] == "observed" and after["matches"]
    noop = observe(case, "noop", before=after, require_change=True)
    assert noop["status"] == "refuted" and noop["complete"] and noop["error"]["type"] == "NoOp"
    before["identity"]["pid"] += 1
    replaced = observe(case, "replaced", before=before, require_change=True)
    assert replaced["status"] == "unknown" and replaced["complete"] is False


def test_all_declared_native_fields_must_be_covered_without_handle_aliases(files, monkeypatch):
    case = runtime_files(files, monkeypatch)
    schema = case[1]
    schema["fields"].pop("count")
    with pytest.raises(ValueError, match="discard"):
        S.validate(schema)
    schema["fields"]["duplicate_text"] = {"type": "utf8", "source": {"operation": 0, "field": "text"}}
    with pytest.raises(ValueError, match="one exact"):
        S.validate(schema)


@pytest.mark.parametrize("kind", ["getter", "ivar"])
def test_objc_state_requires_the_exact_declared_getter_signature_or_ivar_encoding(files, monkeypatch, kind):
    case = runtime_files(files, monkeypatch)
    field = case[1]["request"]["operations"][0]["fields"][0 if kind == "getter" else 1]
    field.pop("signature" if kind == "getter" else "encoding")
    with pytest.raises(ValueError, match="signature or encoding"):
        S.validate(case[1])


def test_state_observer_refuses_actor_invocations_and_opaque_object_outputs(files, monkeypatch):
    case = runtime_files(files, monkeypatch)
    schema = case[1]
    schema["request"] = {"operations": [{"op": "invoke", "receiver": {"class": "MappedStore", "kind": "unique"},
                                        "selector": "apply", "signature": "v16@0:8", "arguments": [], "return_type": "void"}]}
    with pytest.raises(S.Unsupported, match="explicit reads"):
        S.validate(schema)
    schema["request"] = copy.deepcopy(READ)
    schema["request"]["operations"][0]["fields"][0]["type"] = "object"
    with pytest.raises(S.Unsupported, match="opaque objects"):
        S.validate(schema)


def test_current_swift_pointer_token_is_not_a_substantive_state_observer(files):
    _, schema, _, _ = files
    schema["kind"] = "runtime_state"
    schema["request"] = {"operations": [{"op": "swift_read", "read": {
        "receiver": {"class": "MappedOwner", "kind": "unique"}, "receiver_class": "MappedOwner",
        "swift_class": {"module": "MappedModule", "name": "MappedOwner"},
        "field": {"name": "store", "type_name": "Swift.Optional<Swift.OpaquePointer>"},
        "module": {"path": "/tmp/owned-module", "sha256": "a" * 64},
        "evidence": {"kind": "reviewed_contract", "file": "/tmp/swift-layout.json", "sha256": "b" * 64, "pointer": "/layout"}}}]}
    with pytest.raises(S.Unsupported, match="opaque pointer"):
        S.validate(schema)


def native_read():
    evidence = {"kind": "reviewed_contract", "file": "/tmp/native-layout.json", "sha256": "a" * 64, "pointer": "/layout"}
    getter = {"kind": "native_function", "target": {"module": {"path": "/tmp/owned-module", "sha256": "b" * 64}, "symbol": "borrowedStore"},
              "abi": "default", "arguments": [], "return_type": "pointer", "signature_evidence": {**evidence, "pointer": "/getter"}}
    return {"operations": [{"op": "native_read", "receiver": {"native": getter}, "bytes": 16,
                            "fields": [{"name": "total", "offset": 0, "type": "double"},
                                       {"name": "count", "offset": 8, "type": "uint64"}], "layout_evidence": evidence}]}


def test_native_read_full_scalar_state_includes_exact_offsets_and_wide_integer_strings(files, monkeypatch):
    case = runtime_files(files, monkeypatch, request=native_read(), expected={"total": -0.125, "count": "18446744073709551615"})
    assert observe(case)["status"] == "observed"
    case[-1]["tamper"] = lambda receipt: receipt["result"]["operations"][0]["fields"][0].update(offset=8)
    result = observe(case, "wrong-offset")
    assert result["status"] == "unknown" and result["complete"] is False


def test_native_read_rejects_pointer_tokens_and_persisted_addresses(files, monkeypatch):
    case = runtime_files(files, monkeypatch, request=native_read(), expected={"total": 1.25, "count": "1"})
    case[1]["request"]["operations"][0]["fields"][0]["type"] = "pointer"
    with pytest.raises(S.Unsupported, match="opaque objects, pointers"):
        S.validate(case[1])
    case[1]["request"]["operations"][0]["receiver"] = {"address": "0x1234"}
    with pytest.raises(ValueError, match="persisted addresses"):
        S.validate(case[1])


def test_working_output_cannot_alias_a_native_layout_or_module_resource(files, monkeypatch):
    app, schema, inputs, root = files
    request = native_read()
    # Retain an explicit mutable path role while reading actual runtime state.
    runtime_case = runtime_files((app, copy.deepcopy(schema), copy.deepcopy(inputs), root), monkeypatch,
                                 request=request, expected={"total": 1.25, "count": "1"})
    runtime_case[1]["params"].append({"name": "output", "type": "path", "role": "working_output"})
    runtime_case[2]["output"] = inputs["output"]
    seal(runtime_case[1])
    path = Path(inputs["output"])
    native_pin = {"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size}
    monkeypatch.setattr(B, "request_resources", lambda _: {str(path): native_pin})
    result = observe(runtime_case)
    assert result["status"] == "ambiguous" and runtime_case[-1]["calls"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
