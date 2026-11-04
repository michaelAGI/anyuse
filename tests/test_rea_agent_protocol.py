"""Pure full-native wire validation; no model, desktop or native execution."""
import copy
import json

import jsonschema
import pytest

from anyuse.execution import rea_agent_protocol as P
from anyuse.execution import rea_runtime_bridge as B


def view(*tools):
    return {"tools": {name: {} for name in tools or P.SUPPORTED_TOOLS},
            "artifacts": {"inventory.json": {}, "s0000/runtime/runtime.json": {}},
            "params": [{"name": "amount", "type": "string"}],
            "observer_profile_sha256": "a" * 64}


def decision(request=None):
    return {"action": "stop" if request is None else "step", "request": request,
            "reason": "Use the retained current-session evidence."}


def receiver():
    return {"class": "ExampleObject", "kind": "unique", "subclasses": None, "path": None}


def actor():
    return {"kind": "objc_method", "receiver": {"class": "ExampleRoot", "kind": "class",
            "path": [{"selector": "exampleObject", "signature": "@16@0:8"}]},
            "selector": "exampleAction", "signature": "v16@0:8", "arguments": [], "return_type": "void"}


def proposal():
    return {"op": "propose", "rationale": "A hypothesis bound to observed method metadata.",
            "citations": [{"artifact": "s0000/runtime/runtime.json", "pointer": "/operations/0"}],
            "actor": actor(), "observer_profile_sha256": "a" * 64}


def runtime(operations, op="runtime"):
    return {"op": op, "provider": "native", "request": {"operations": operations}, "timeout": None}


def checked(request, model_view=None):
    model_view = model_view or view()
    wire = decision(request)
    jsonschema.Draft202012Validator(P.schema(list(model_view["tools"]))).validate(wire)
    before = copy.deepcopy(wire)
    result = P.normalize_request(wire, model_view)
    assert wire == before
    return result


def test_schema_is_bounded_closed_strict_provider_data_and_independent_copy():
    schema = P.schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    def walk(value):
        if isinstance(value, dict):
            assert "_omit_null" not in value
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            if value.get("type") == "array":
                assert 0 <= value["minItems"] <= value["maxItems"] <= 32
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(schema)
    assert len(json.dumps(schema)) < 22000
    schema["$defs"]["actor"]["properties"]["kind"]["enum"].append("invented")
    assert "invented" not in json.dumps(P.schema())


def test_stop_empty_scope_and_strict_action_request_relation():
    empty = {"tools": {}}
    jsonschema.Draft202012Validator(P.schema([])).validate(decision())
    assert P.normalize_request(decision(), empty) is None
    for wire in [{**decision(), "action": "step"},
                 {**decision({"op": "resource", "relative": "Contents/Info.plist"}), "action": "stop"}]:
        with pytest.raises(ValueError):
            P.normalize_request(wire, view())


def test_invalid_action_reports_its_exact_path_and_allowed_choices_without_repair():
    wire = {**decision(proposal()), "action": "propose"}
    before = copy.deepcopy(wire)
    with pytest.raises(ValueError) as caught:
        P.normalize_request(wire, view())
    assert "$.action" in str(caught.value)
    assert "step" in str(caught.value) and "stop" in str(caught.value)
    assert wire == before


def test_invalid_nested_enum_reports_its_own_path_and_remains_invalid():
    request = runtime([{"op": "classes", "query": None, "owner": "guessed_owner", "limit": None}], "discover")
    wire = decision(request)
    with pytest.raises(ValueError) as caught:
        P.normalize_request(wire, view())
    assert ".owner" in str(caught.value)
    assert "app" in str(caught.value) and "all" in str(caught.value)


@pytest.mark.parametrize("admitted", [["heldout"], ["runtime", "runtime"], "runtime", [1]])
def test_unsupported_or_ambiguous_tool_configuration_refuses(admitted):
    with pytest.raises(ValueError):
        P.schema(admitted)


@pytest.mark.parametrize("wire_request,canonical", [
    ({"op": "analyze", "query": None, "function": None, "timeout": None}, {"op": "analyze"}),
    ({"op": "symbols", "query": "", "limit": 12, "timeout": None}, {"op": "symbols", "query": "", "limit": 12}),
    ({"op": "evidence", "artifact": "inventory.json", "offset": None, "limit": 4096},
     {"op": "evidence", "artifact": "inventory.json", "limit": 4096}),
    ({"op": "resource", "relative": "Contents/Info.plist"}, {"op": "resource", "relative": "Contents/Info.plist"}),
])
def test_static_subset_keeps_native_object_wire_and_declared_optional_nulls(wire_request, canonical):
    assert checked(wire_request) == canonical


def test_native_metadata_normalizes_before_real_bridge_validation():
    request = runtime([
        {"op": "classes", "query": None, "owner": None, "limit": None},
        {"op": "methods", "class": "ExampleObject", "query": "example", "own": True, "limit": 8},
        {"op": "ivars", "class": "ExampleObject", "query": None, "own": None, "limit": None},
        {"op": "instances", "class": "ExampleObject", "alias": "object", "subclasses": False},
    ], "discover")
    result = checked(request)
    assert result["request"]["operations"][0] == {"op": "classes"}
    assert result["request"]["operations"][2] == {"op": "ivars", "class": "ExampleObject"}
    assert B.validate_request(result["request"]) == result["request"]
    assert "timeout" not in result


@pytest.mark.parametrize("klass", ["..NSKVONotifying_IINA.MainWindow", ".App.Window", "." + "A" * 255])
def test_measured_runtime_class_spelling_survives_wire_schema_and_saved_actor(klass):
    metadata = checked(runtime([{"op": "methods", "class": klass, "query": None, "own": None, "limit": None}], "discover"))
    assert metadata["request"]["operations"][0]["class"] == klass
    request = proposal()
    request["actor"]["receiver"]["class"] = klass
    result = checked(request)
    assert result["actor"]["receiver"]["class"] == klass
    assert B.validate_actor(result["actor"], allow_bindings=True) == result["actor"]
    B.invocation_request(result["actor"])


@pytest.mark.parametrize("klass", [".", "..", "..9Bad", "App Window", "App/Window", "App:Window", "App\0", "." + "A" * 256])
def test_class_schema_remains_bounded_and_rejects_nonclass_syntax(klass):
    with pytest.raises(jsonschema.ValidationError):
        checked(runtime([{"op": "methods", "class": klass, "query": None, "own": None, "limit": None}], "discover"))


def test_class_schema_change_does_not_expand_ivar_identifiers():
    request = runtime([{"op": "read", "receiver": receiver(), "fields": [
        {"name": "flag", "kind": "ivar", "type": "bool", "ivar": ".flag", "encoding": "B", "alias": None}]}])
    with pytest.raises(jsonschema.ValidationError):
        checked(request)


def test_oversized_stop_reason_remains_rejected_with_exact_field_and_bound():
    wire = decision()
    wire["reason"] = "x" * 4993
    before = copy.deepcopy(wire)
    with pytest.raises(ValueError, match=r"\$\.reason: invalid bounded full-native text; expected 1\.\.1200.*received 4993"):
        P.normalize_request(wire, view())
    assert wire == before and wire["request"] is None
    wire["reason"] = "x" * 1200
    assert P.normalize_request(wire, view()) is None
    with pytest.raises(ValueError, match=r"\$\.reason.*contains_NUL=True"):
        P.normalize_request({**wire, "reason": "x\0x"}, view())
    assert P.schema()["properties"]["reason"]["maxLength"] == 1200


def test_transaction_aliases_read_fields_and_typed_nil_survive_only_in_scope():
    request = runtime([
        {"op": "instances", "class": "ExampleObject", "alias": "object", "subclasses": None},
        {"op": "read", "receiver": {"alias": "object"}, "fields": [
            {"name": "child", "kind": "getter", "type": "object", "selector": "exampleChild",
             "signature": "@16@0:8", "alias": "child"},
            {"name": "flag", "kind": "ivar", "type": "bool", "ivar": "_exampleFlag",
             "encoding": "B", "alias": None},
        ]},
        {"op": "invoke", "receiver": {"alias": "child"}, "selector": "exampleSet:",
         "signature": "v24@0:8@16", "arguments": [{"type": "nil", "value": None}],
         "return_type": "void", "alias": None},
    ])
    result = checked(request)
    assert B.validate_request(result["request"]) == result["request"]
    invoke = result["request"]["operations"][-1]
    assert invoke["arguments"] == [{"type": "nil", "value": None}] and "alias" not in invoke
    with pytest.raises(ValueError, match="alias"):
        checked(runtime([request["request"]["operations"][-1]]))


def test_hash_bound_actor_keeps_fresh_recipe_and_declared_scalar_binding():
    request = proposal()
    request["actor"].update(selector="exampleSet:", signature="v24@0:8Q16",
                             arguments=[{"type": "uint64", "value": {"input": "amount"}}])
    result = checked(request)
    assert B.validate_actor(result["actor"], allow_bindings=True) == result["actor"]
    assert result["observer_profile_sha256"] == "a" * 64 and "profile" not in result
    assert result["actor"]["receiver"]["path"][0]["signature"] == "@16@0:8"
    result["actor"]["receiver"]["path"].clear()
    assert request["actor"]["receiver"]["path"]


@pytest.mark.parametrize("mutation", ["hash", "missing_approval", "profile", "private_artifact", "bad_pointer", "unknown_input", "saved_alias"])
def test_proposal_cannot_replace_private_contract_or_use_unbound_evidence(mutation):
    request, model_view = proposal(), view()
    if mutation == "hash":
        request["observer_profile_sha256"] = "b" * 64
    elif mutation == "missing_approval":
        model_view.pop("observer_profile_sha256")
    elif mutation == "profile":
        request["profile"] = {"oracle": "private"}
    elif mutation == "private_artifact":
        request["citations"][0]["artifact"] = "s0007/heldout/proof.json"
    elif mutation == "bad_pointer":
        request["citations"][0]["pointer"] = "/bad~2pointer"
    elif mutation == "unknown_input":
        request["actor"].update(selector="exampleSet:", signature="v24@0:8@16",
            arguments=[{"type": "utf8", "value": {"input": "oracle"}}])
    else:
        request["actor"]["receiver"] = {"alias": "earlier"}
    with pytest.raises(ValueError):
        P.normalize_request(decision(request), model_view)


@pytest.mark.parametrize("mutation", ["signature", "getter_signature", "ivar_encoding", "bool_as_integer", "integer_overflow", "uint64_overflow", "saved_object", "float_binding"])
def test_real_bridge_rejects_invalid_abi_types_and_nonpersistent_arguments(mutation):
    request = proposal()
    if mutation == "signature":
        request["actor"]["signature"] = "B16@0:8"
    elif mutation == "getter_signature":
        request["actor"]["receiver"]["path"][0]["signature"] = "v16@0:8"
    elif mutation == "ivar_encoding":
        request = runtime([{"op": "read", "receiver": receiver(), "fields": [
            {"name": "flag", "kind": "ivar", "type": "bool", "ivar": "_exampleFlag", "encoding": "d", "alias": None}]}])
    else:
        kind, value, encoding = {
            "bool_as_integer": ("uint8", True, "C"), "integer_overflow": ("uint8", 256, "C"),
            "uint64_overflow": ("uint64", str(2**64), "Q"),
            "saved_object": ("object", {"alias": "old"}, "@"),
            "float_binding": ("double", {"input": "amount"}, "d"),
        }[mutation]
        request["actor"].update(selector="exampleSet:", signature=f"v24@0:8{encoding}16",
            arguments=[{"type": kind, "value": value}])
    with pytest.raises(ValueError):
        P.normalize_request(decision(request), view())


@pytest.mark.parametrize("wire_request", [
    {"op": "heldout", "case": 0, "restart": {}},
    {"op": "analyze", "query": None, "function": None, "timeout": None, "javascript": "source"},
    runtime([{"op": "native_invoke", "target": "address"}]),
    runtime([{"op": "invoke", "receiver": receiver(), "selector": "exampleAction", "signature": "v16@0:8",
              "arguments": [], "return_type": "void", "alias": None}], "discover"),
    {**runtime([{"op": "classes", "query": None, "owner": None, "limit": None}]), "provider": "inspector"},
])
def test_protocol_never_widens_to_other_transports_or_lifecycle(wire_request):
    with pytest.raises(ValueError):
        P.normalize_request(decision(wire_request), view())


@pytest.mark.parametrize("mutation", ["missing_nullable", "extra", "tuple", "nan", "infinity", "repr", "legacy", "hidden_evidence", "unadmitted", "oversize", "blank_reason"])
def test_malformed_wire_is_refused_without_repair_or_dispatch(mutation, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("protocol validation must never collect or invoke a native target")
    monkeypatch.setattr(B, "collect", forbidden)
    monkeypatch.setattr(B, "invoke", forbidden)
    wire = decision({"op": "evidence", "artifact": "inventory.json", "offset": None, "limit": None})
    model_view = view()
    if mutation == "missing_nullable":
        wire["request"].pop("offset")
    elif mutation == "extra":
        wire["request"]["source"] = "private"
    elif mutation == "tuple":
        wire = decision(runtime(tuple()))
    elif mutation in {"nan", "infinity"}:
        wire["request"]["offset"] = float("nan" if mutation == "nan" else "inf")
    elif mutation == "repr":
        wire["request"] = "{'op':'evidence','artifact':'inventory.json'}"
    elif mutation == "legacy":
        wire["request_json"] = json.dumps(wire.pop("request"))
    elif mutation == "hidden_evidence":
        wire["request"]["artifact"] = "private/oracle.json"
    elif mutation == "unadmitted":
        model_view = view("symbols")
    elif mutation == "oversize":
        wire["reason"] = "x" * (P.MAX_WIRE_BYTES + 1)
    else:
        wire["reason"] = " \n "
    with pytest.raises(ValueError):
        P.normalize_request(wire, model_view)




def compatible_view():
    return {**view(), "decision_protocol": {"format": P.FORMAT, "nullable_omissions": True}}


@pytest.mark.parametrize("wire,omitted", [
    (runtime([{"op": "classes", "query": None, "owner": None, "limit": None}], "discover"),
     {"op": "discover", "provider": "native", "request": {"operations": [{"op": "classes"}]}}),
    (runtime([{"op": "methods", "class": "ExampleObject", "query": None, "own": None, "limit": None}], "discover"),
     {"op": "discover", "provider": "native", "request": {"operations": [{"op": "methods", "class": "ExampleObject"}]}}),
    (runtime([{"op": "read", "receiver": {"class": "ExampleRoot", "kind": "class", "path": None}, "fields": [
        {"name": "child", "kind": "getter", "type": "object", "selector": "exampleChild", "signature": "@16@0:8", "alias": None}]}]),
     {"op": "runtime", "provider": "native", "request": {"operations": [{"op": "read", "receiver": {"class": "ExampleRoot", "kind": "class"}, "fields": [
        {"name": "child", "kind": "getter", "type": "object", "selector": "exampleChild", "signature": "@16@0:8"}]}]}}),
])
def test_json_object_optional_omission_equals_explicit_null_without_defaults(wire, omitted):
    original = copy.deepcopy(omitted)
    expected = P.normalize_request(decision(wire), view())
    result = P.normalize_request(decision(omitted), compatible_view())
    assert result == expected and omitted == original
    jsonschema.Draft202012Validator(P.schema(nullable_omissions=True)).validate(decision(omitted))
    with pytest.raises(ValueError):
        P.normalize_request(decision(omitted), view())


@pytest.mark.parametrize("mode", [False, True])
@pytest.mark.parametrize("change", ["root_request", "nil_value", "return_type", "selector", "signature", "unknown", "invalid_optional", "observer_hash"])
def test_nullable_omission_never_relaxes_required_fields_values_or_proof_bindings(mode, change):
    model_view = compatible_view() if mode else view()
    request = proposal()
    request["actor"].update(selector="exampleAction:", signature="v24@0:8@16", arguments=[{"type": "nil", "value": None}])
    wire = decision(request)
    if change == "root_request": wire.pop("request")
    elif change == "nil_value": request["actor"]["arguments"][0].pop("value")
    elif change in {"return_type", "selector", "signature"}: request["actor"].pop(change)
    elif change == "unknown": wire["nullable_omissions"] = True
    elif change == "invalid_optional": request["actor"]["receiver"]["path"] = "not a typed getter path"
    else: request["observer_profile_sha256"] = "b" * 64
    with pytest.raises(ValueError): P.normalize_request(wire, model_view)
    if change != "observer_hash":
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(P.schema(nullable_omissions=mode)).validate(wire)


def test_nullable_omission_does_not_change_default_schema_or_accept_untrusted_mode():
    assert P.schema() == P.schema(nullable_omissions=False)
    for mode in (1, None, "true"):
        with pytest.raises(ValueError): P.schema(nullable_omissions=mode)
        with pytest.raises(ValueError): P.normalize_request(decision(), {**view(), "decision_protocol": {"nullable_omissions": mode}})


def inspector_view(*tools, omissions=False):
    return {"tools": {name: {} for name in tools or P.INSPECTOR_SUPPORTED_TOOLS},
            "artifacts": {"inventory.json": {}, "s0000/runtime/inspector.json": {},
                "s0001/inspector-declaration.json": {"file": "/tmp/session/s0001/inspector-declaration.json",
                                                     "sha256": "d" * 64, "bytes": 1024}},
            "params": [{"name": "amount", "type": "string"}],
            "observer_profile_sha256": "a" * 64,
            "decision_protocol": {"format": P.INSPECTOR_FORMAT, "nullable_omissions": omissions}}


def inspector_recipe(steps=None, root=None):
    return {"root": root or {"kind": "global", "name": "observedObject"}, "steps": steps or []}


def inspector_actor(complete=False):
    result = {"kind": "inspector_method", "receiver": inspector_recipe(),
              "method": "observedMethod", "function_sha256": "b" * 64,
              "arguments": [{"type": "number", "value": {"input": "amount"}}], "return_type": "void"}
    if complete:
        result["signature_evidence"] = {"kind": "declared_js",
            "file": "/tmp/session/s0001/inspector-declaration.json", "sha256": "d" * 64, "pointer": "/contract"}
    return result


def inspector_citations():
    return [{"artifact": "s0000/runtime/inspector.json", "pointer": "/operations/0"}]


def inspector_proposal():
    return {"op": "propose", "rationale": "A typed hypothesis using the returned declaration.",
            "actor": inspector_actor(True), "citations": inspector_citations(), "observer_profile_sha256": "a" * 64}


def inspector_discovery(operations):
    return {"op": "discover", "provider": "inspector", "request": {"operations": operations}, "timeout": None}


def inspector_checked(request, model_view=None):
    model_view = model_view or inspector_view()
    wire = decision(request)
    jsonschema.Draft202012Validator(P.inspector_schema(list(model_view["tools"]),
        nullable_omissions=model_view["decision_protocol"]["nullable_omissions"])).validate(wire)
    before = copy.deepcopy(wire)
    result = P.normalize_inspector_request(wire, model_view)
    assert wire == before
    return result


def test_inspector_schema_is_closed_bounded_and_does_not_change_native_protocol():
    schema = P.inspector_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    def walk(value):
        if type(value) is dict:
            assert "_omit_null" not in value
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            if value.get("type") == "array":
                assert 0 <= value["minItems"] <= value["maxItems"] <= 32
            for child in value.values(): walk(child)
        elif type(value) is list:
            for child in value: walk(child)
    walk(schema)
    schema["$defs"]["root"]["anyOf"][0]["properties"]["kind"]["enum"].append("invented")
    assert "invented" not in json.dumps(P.inspector_schema())
    assert P.FORMAT == "full_native_v1" and P.INSPECTOR_FORMAT == "inspector_structured_v1"
    assert P.SUPPORTED_TOOLS == ("analyze", "symbols", "evidence", "resource", "discover", "runtime", "propose")
    assert "inspector_method" not in json.dumps(P.schema())
    with pytest.raises(ValueError):
        P.normalize_request(decision(inspector_discovery([{"op": "dom_query", "selector": "#observed", "max_nodes": 4}])), view())


def test_inspector_instructions_include_schema_valid_double_quoted_json_and_fresh_scope():
    instructions = P.inspector_instructions()
    example, = [text.split(": ", 1)[1] for text in instructions if text.startswith("JSON syntax example")]
    wire = json.loads(example)
    jsonschema.Draft202012Validator(P.inspector_schema()).validate(wire)
    assert P.normalize_inspector_request(wire, inspector_view())["request"]["operations"][0]["op"] == "dom_query"
    assert '"op": "discover"' in example and "'op'" not in example
    instructions.clear()
    assert P.inspector_instructions() and "JSON syntax example" not in " ".join(P.inspector_instructions(["evidence"]))


@pytest.mark.parametrize("tools", [["runtime"], ["analyze"], ["symbols"], ["discover", "discover"], "discover", [1]])
def test_inspector_protocol_rejects_unsupported_or_ambiguous_tool_scopes(tools):
    with pytest.raises(ValueError): P.inspector_schema(tools)
    with pytest.raises(ValueError): P.inspector_instructions(tools)
    if tools == ["discover", "discover"]:
        # A model-view dictionary cannot retain duplicate keys; the list API
        # rejects that caller configuration before creating the view.
        assert P.normalize_inspector_request(decision(), {"tools": {"discover": {}}}) is None
    else:
        with pytest.raises(ValueError): P.normalize_inspector_request(decision(), {"tools": dict.fromkeys(tools) if type(tools) is list else tools})


def test_inspector_all_metadata_operations_are_validated_by_existing_provider(monkeypatch):
    from anyuse.execution import rea_runtime_providers as providers
    from anyuse.execution import rea_inspector_bridge as I
    operations = [
        {"op": "dom_query", "selector": "#observed", "max_nodes": 8},
        {"op": "describe", "recipe": inspector_recipe(), "max_properties": 8,
         "prototype_depth": None, "offset": 32, "property_query": "literal"},
        {"op": "method", "recipe": inspector_recipe(), "method": "observedMethod"},
        {"op": "function_scopes", "recipe": inspector_recipe(), "function_sha256": "b" * 64,
         "max_scopes": 8, "max_bindings": 8},
        {"op": "prototype_instances", "recipe": inspector_recipe(), "constructor_sha256": "b" * 64,
         "max_instances": 8, "max_properties": 8},
        {"op": "graph", "recipe": inspector_recipe(), "max_properties": 8, "max_nodes": None, "max_depth": None},
        {"op": "map_keys", "recipe": inspector_recipe(), "max_keys": 8},
    ]
    calls = []
    original = providers.Provider.validate_discovery
    def checked_provider(self, request):
        calls.append((self.name, copy.deepcopy(request)))
        return original(self, request)
    monkeypatch.setattr(providers.Provider, "validate_discovery", checked_provider)
    result = inspector_checked(inspector_discovery(operations))
    assert calls == [("inspector", result["request"])]
    assert I.validate_request(result["request"]) == result["request"]
    assert "timeout" not in result and "prototype_depth" not in result["request"]["operations"][1]
    assert result["request"]["operations"][1]["offset"] == 32
    assert "max_nodes" not in result["request"]["operations"][5]


@pytest.mark.parametrize("root", [{"kind": "global", "name": "observedObject"}, {"kind": "dom", "selector": "#observed"}])
def test_inspector_all_typed_recipe_steps_survive_existing_recipe_validation(root):
    from anyuse.execution import rea_inspector_bridge as I
    steps = [
        {"kind": "property", "name": "data", "access": "data"},
        {"kind": "property", "name": "measuredGetter", "access": "getter", "getter_sha256": "b" * 64},
        {"kind": "index", "index": 2}, {"kind": "private_property", "name": "#observed"},
        {"kind": "map_key", "key": {"type": "utf8", "value": "observed"}},
        {"kind": "scope_binding", "function_sha256": "b" * 64, "name": "captured", "scope": {
            "index": 1, "description": "Closure (observed)", "chain_sha256": "b" * 64, "bindings_sha256": "c" * 64}},
        {"kind": "unique_instance", "constructor_sha256": "b" * 64, "max_candidates": 8,
            "own_invariants": [{"name": "observedFlag", "type": "bool", "equals": True}]},
    ]
    recipe = inspector_recipe(steps, root)
    request = inspector_discovery([{"op": "method", "recipe": recipe, "method": "observedMethod"}])
    result = inspector_checked(request)
    assert result["request"]["operations"][0]["recipe"] == I.validate_recipe(recipe)


@pytest.mark.parametrize("kind,value", [("null", None), ("utf8", "literal"), ("bool", False),
    ("number", 1.25), ("int32", -(2**31)), ("uint32", 2**32 - 1),
    ("null", {"input": "amount"}), ("json", {"input": "amount"})])
def test_inspector_declare_and_propose_preserve_typed_values_and_frozen_bindings(kind, value):
    from anyuse.execution import rea_inspector_bridge as I
    unsigned = inspector_actor()
    unsigned["arguments"] = [{"type": kind, "value": value}]
    declared = inspector_checked({"op": "declare_inspector", "actor": unsigned, "citations": inspector_citations()})
    assert declared["actor"] == unsigned and "signature_evidence" not in declared["actor"]
    proposed = inspector_proposal()
    proposed["actor"].update(unsigned)
    result = inspector_checked(proposed)
    assert I.validate_actor(result["actor"], allow_bindings=True) == result["actor"]
    assert result["actor"]["arguments"][0]["value"] == value
    assert "profile" not in result and result["observer_profile_sha256"] == "a" * 64


@pytest.mark.parametrize("op", ["declare_inspector", "propose"])
@pytest.mark.parametrize("change", ["unknown_input", "old_artifact", "nonraw_artifact", "nonoperation_pointer", "duplicate_citation"])
def test_inspector_actor_admission_preserves_parameter_and_raw_current_citation_gates(op, change):
    request = inspector_proposal() if op == "propose" else {"op": op, "actor": inspector_actor(), "citations": inspector_citations()}
    model_view = inspector_view()
    if change == "unknown_input": request["actor"]["arguments"][0]["value"] = {"input": "private"}
    elif change == "old_artifact": request["citations"][0]["artifact"] = "s0009/runtime/inspector.json"
    elif change == "nonraw_artifact": request["citations"][0]["artifact"] = "inventory.json"
    elif change == "nonoperation_pointer": request["citations"][0]["pointer"] = "/observation"
    else: request["citations"] *= 2
    with pytest.raises(ValueError): P.normalize_inspector_request(decision(request), model_view)


@pytest.mark.parametrize("change", ["hash", "missing_approval", "profile", "signature_kind", "signature_pin", "signature_file", "signature_pointer", "hidden_declaration"])
def test_inspector_proposal_needs_approved_observer_and_returned_declaration_pin(change):
    request, model_view = inspector_proposal(), inspector_view()
    if change == "hash": request["observer_profile_sha256"] = "b" * 64
    elif change == "missing_approval": model_view.pop("observer_profile_sha256")
    elif change == "profile": request["profile"] = {"private": "oracle"}
    elif change == "signature_kind": request["actor"]["signature_evidence"]["kind"] = "reviewed_js"
    elif change == "signature_pin": request["actor"]["signature_evidence"]["sha256"] = "b" * 64
    elif change == "signature_file": request["actor"]["signature_evidence"]["file"] = "/tmp/old/inspector-declaration.json"
    elif change == "signature_pointer": request["actor"]["signature_evidence"]["pointer"] = "/contract/method"
    else: model_view["artifacts"].pop("s0001/inspector-declaration.json")
    with pytest.raises(ValueError): P.normalize_inspector_request(decision(request), model_view)


@pytest.mark.parametrize("change", ["scope_budget", "heap_budget", "getter_scopes", "getter_instances", "bad_scope", "repeated_invariant", "utf8_query_bytes", "unsafe_number", "negative_zero"])
def test_inspector_existing_bridge_semantic_gates_still_refuse_schema_shaped_values(change):
    scope = {"op": "function_scopes", "recipe": inspector_recipe(), "function_sha256": "b" * 64,
             "max_scopes": 8, "max_bindings": 8}
    instance = {"op": "prototype_instances", "recipe": inspector_recipe(), "constructor_sha256": "b" * 64,
                "max_instances": 8, "max_properties": 8}
    request = inspector_discovery([copy.deepcopy(scope)])
    if change == "scope_budget": request["request"]["operations"] *= 3
    elif change == "heap_budget": request["request"]["operations"] = [copy.deepcopy(instance) for _ in range(5)]
    elif change in {"getter_scopes", "getter_instances"}:
        operation = copy.deepcopy(scope if change == "getter_scopes" else instance)
        operation["recipe"]["steps"] = [{"kind": "property", "name": "observed", "access": "getter", "getter_sha256": "b" * 64}]
        request["request"]["operations"] = [operation]
    elif change == "utf8_query_bytes":
        request["request"]["operations"] = [{"op": "describe", "recipe": inspector_recipe(), "max_properties": 8,
            "prototype_depth": None, "offset": None, "property_query": "é" * 200}]
    elif change in {"unsafe_number", "negative_zero"}:
        request = inspector_proposal()
        request["actor"]["arguments"] = [{"type": "number", "value": 2**53 if change == "unsafe_number" else -0.0}]
    else:
        recipe = inspector_recipe([{"kind": "scope_binding", "function_sha256": "b" * 64, "name": "captured", "scope": {
            "index": 1, "description": "Global", "chain_sha256": "b" * 64, "bindings_sha256": "c" * 64}}])
        if change == "repeated_invariant":
            invariant = {"name": "same", "type": "bool", "equals": True}
            recipe = inspector_recipe([{"kind": "unique_instance", "constructor_sha256": "b" * 64,
                "max_candidates": 8, "own_invariants": [invariant, copy.deepcopy(invariant)]}])
        request["request"]["operations"] = [{"op": "method", "recipe": recipe, "method": "observedMethod"}]
    with pytest.raises(ValueError): P.normalize_inspector_request(decision(request), inspector_view())


@pytest.mark.parametrize("change", ["nested_string", "request_json", "missing_op", "runtime", "read", "call", "javascript", "live_handle", "json_literal", "signed_declaration", "nan", "oversize", "blank_reason", "unadmitted"])
def test_inspector_refuses_malformed_legacy_or_unadmitted_requests_without_repair_or_io(change, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I
    def forbidden(*args, **kwargs): pytest.fail("pure inspector wire validation performed IO or dispatch")
    monkeypatch.setattr(I, "collect", forbidden)
    monkeypatch.setattr(I, "invoke", forbidden)
    monkeypatch.setattr(I, "resources", forbidden)
    wire = decision(inspector_discovery([{"op": "method", "recipe": inspector_recipe(), "method": "observedMethod"}]))
    model_view = inspector_view()
    if change == "nested_string": wire["request"] = "{op:'discover',request:{operations:[]}}"
    elif change == "request_json": wire["request_json"] = json.dumps(wire.pop("request"))
    elif change == "missing_op": wire["request"].pop("op")
    elif change == "runtime": wire["request"]["op"] = "runtime"
    elif change in {"read", "call"}: wire["request"]["request"]["operations"] = [{"op": change}]
    elif change == "javascript": wire["request"]["request"]["operations"][0]["javascript"] = "return source"
    elif change == "live_handle": wire["request"]["request"]["operations"][0]["recipe"]["root"] = {"kind": "object_id", "id": "old"}
    elif change == "json_literal":
        wire = decision(inspector_proposal());wire["request"]["actor"]["arguments"] = [{"type": "json", "value": {"arbitrary": "data"}}]
    elif change == "signed_declaration": wire = decision({"op": "declare_inspector", "actor": inspector_actor(True), "citations": inspector_citations()})
    elif change == "nan": wire["request"]["timeout"] = float("nan")
    elif change == "oversize": wire["reason"] = "x" * (P.MAX_WIRE_BYTES + 1)
    elif change == "blank_reason": wire["reason"] = " \n "
    else: model_view = inspector_view("evidence")
    before = copy.deepcopy(wire)
    with pytest.raises(ValueError): P.normalize_inspector_request(wire, model_view)
    if change != "nan": assert wire == before


@pytest.mark.parametrize("omissions", [False, True])
def test_inspector_nullable_omission_only_omits_declared_optional_fields(omissions):
    wire = inspector_discovery([{"op": "describe", "recipe": inspector_recipe(), "max_properties": 8,
        "prototype_depth": None, "offset": None, "property_query": None}])
    expected = inspector_checked(wire)
    if omissions:
        wire.pop("timeout")
        for key in ["prototype_depth", "offset", "property_query"]: wire["request"]["operations"][0].pop(key)
    assert inspector_checked(wire, inspector_view(omissions=omissions)) == expected
    for invalid in [decision({**wire, "provider": "native"}), {"action": "step", "reason": "No request"}]:
        with pytest.raises(ValueError): P.normalize_inspector_request(invalid, inspector_view(omissions=omissions))
    assert P.normalize_inspector_request(decision(), {"tools": {}}) is None
    for bad in [1, None, "true"]:
        with pytest.raises(ValueError): P.inspector_schema(nullable_omissions=bad)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
