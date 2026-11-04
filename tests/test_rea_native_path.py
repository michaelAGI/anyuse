"""Pure native-path contracts; run only with the repository's guest test gate."""
import copy

import pytest

from anyuse.execution import rea_native_path as P
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_native_lifecycle as NL
from anyuse.execution import rea_qt_moc as Q


def _path_actor():
    module = {"path": "/guest/Objects.dylib", "sha256": "a" * 64}
    abi = {"kind": "header", "file": "/guest/abi.json", "sha256": "b" * 64, "pointer": "/root"}
    getter = {"kind": "native_function", "target": {"module": copy.deepcopy(module), "symbol": "owned_root"},
              "abi": "default", "arguments": [], "return_type": "pointer", "signature_evidence": copy.deepcopy(abi)}
    call = {"module": copy.deepcopy(module), "symbol": "set_payload", "abi": "default",
            "argument_types": ["pointer", "int32"], "return_type": "int32", "context": "none",
            "pointer_policy": "borrowed_no_escape", "signature_evidence": {**abi, "pointer": "/consumer"}}

    def evidence(pointer):
        return {"claim": {"kind": "reviewed_contract", "file": "/guest/path-contracts.json",
                          "sha256": "c" * 64, "pointer": pointer},
                "originals": [{"role": "header", "file": "/guest/objects.h", "sha256": "d" * 64},
                              {"role": "original_build", "file": "/guest/build.txt", "sha256": "e" * 64},
                              {"role": "independent_review", "file": "/guest/path-review.json", "sha256": "f" * 64}]}

    def layout(name, fields):
        return {"format": P.LAYOUT, "name": name, "owner_image": copy.deepcopy(module), "abi": dict(P.ABI),
                "bytes": 16, "alignment": 8, "fields": fields, "evidence": evidence("/" + name)}

    recipe = {"format": P.FORMAT, "root": {"getter": getter}, "root_type": "Owner", "result_type": "Node",
              "types": [layout("Owner", [{"name": "ready", "type": "bool", "offset": 0},
                                           {"name": "child", "type": "pointer", "offset": 8, "target": "Node"}]),
                        layout("Node", [{"name": "generation", "type": "uint32", "offset": 0},
                                          {"name": "payload", "type": "int32", "offset": 4}])],
              "path": ["child"], "preconditions": [{"hop": 0, "field": "ready", "equals": True},
                                                       {"hop": 1, "field": "generation", "equals": 7}],
              "root_evidence": evidence("/root"),
              "lifetime": {"mode": "process_lifetime", "evidence": evidence("/lifetime")},
              "consumer": {"call": call, "argument": 0, "pointee_type": "Node", "role": "cxx_this",
                           "dispatch": "direct", "this_adjustment": 0, "evidence": evidence("/admitted_consumer")}}
    actor = {"kind": "native_function", "target": {"module": copy.deepcopy(module), "symbol": "set_payload"},
             "abi": "default", "arguments": [{"type": "pointer", "value": {"native_path": recipe}},
                                                {"type": "int32", "value": {"input": "payload"}}],
             "return_type": "int32", "signature_evidence": {**abi, "pointer": "/actor"}}
    return actor, recipe


def _observations(recipe, *, for_call=True):
    hops = []
    for index, layout in enumerate(P.path_types(recipe)):
        guards = []
        for guard in recipe["preconditions"]:
            if guard["hop"] == index:
                field = next(field for field in layout["fields"] if field["name"] == guard["field"])
                guards.append({"field": field["name"], "type": field["type"], "equals": guard["equals"], "matched": True})
        edge = None
        if index < len(recipe["path"]):
            field = next(field for field in layout["fields"] if field["name"] == recipe["path"][index])
            edge = {"field": field["name"], "target_type": field["target"], "unchanged": for_call}
        hops.append({"type": layout["name"], "pointer": {"handle": "n" + str(index + 1), "alias": None,
                        "ownership": "borrowed_no_escape", "expires": "transaction"},
                     "guards": guards, "edge": edge, "identity_checked": "runtime_identity" in layout})
    rows = [{"recipe": copy.deepcopy(recipe), "argument": recipe["consumer"]["argument"] if for_call else None,
             "consumer": P.consumer_contract(recipe["consumer"]) if for_call else None,
             "root_kind": next(iter(recipe["root"])),
             "owner_retained": next(iter(recipe["root"])) in {"objc", "read", "swift_read"},
             "queue": "main", "lifetime_mode": recipe["lifetime"]["mode"], "resolved": True,
             "sealed": for_call, "dispatch_permit_consumed": for_call, "hops": hops, "expires": "transaction"}]
    if P.is_borrowed_root(recipe):
        rows[0]["borrowed_scope"] = {"getter_epoch": 1, "checked_epoch": 1,
            "use": "dispatch" if for_call else "read", "consumed": True, "expires": "use"}
    if P.is_lifecycle_root(recipe):
        rows[0]["lifecycle_scope"] = _lifecycle_scope(recipe, for_call=for_call)
    return rows


def _admission(recipe, row):
    return {"format": P.ADMISSION, "abi": dict(P.ABI), "images": P.image_claims(recipe),
            "declarations": [P.admission_declaration(item) for item in P.evidence_plan(recipe)]}


def test_path_is_a_frozen_typed_pointer_argument_with_only_outer_scalar_bindings():
    actor, recipe = _path_actor()
    saved = copy.deepcopy(actor)
    bound = B.bind_actor(actor, {"payload": "23"})
    assert bound["arguments"][0]["value"]["native_path"] == recipe
    assert actor == saved and B.argument_leaves(actor["arguments"][0]) == []
    assert bound["arguments"][1]["value"] == 23
    assert B.native_path_recipes(actor) == [recipe]
    assert B.request_native_path_recipes(B.invocation_request(bound)) == [recipe]
    with pytest.raises(ValueError, match="typed leaf"):
        B.replace_argument_leaf(actor["arguments"][0], ["value", "native_path", "preconditions", 0, "equals"], False)


@pytest.mark.parametrize("change", ["raw_root", "alias_root", "new_getter", "getter_arguments", "getter_swift",
    "retained_getter", "format", "layout_v1", "cpu_bool", "pac", "overlap", "unaligned", "unknown_target",
    "extra_layout", "guard_type", "bound_guard", "unknown_guard", "unknown_path", "edge_not_pointer",
    "result_type", "consumer_slot", "consumer_target", "consumer_context", "consumer_adjustment", "consumer_virtual"])
def test_path_refuses_unproved_types_abi_lifetime_or_consumers(change):
    actor, recipe = _path_actor()
    if change == "raw_root": recipe["root"] = {"address": "0x1000"}
    elif change == "alias_root": recipe["root"] = {"alias": "yesterday"}
    elif change == "new_getter": recipe["root"] = {"get": {"offset": 8}}
    elif change == "getter_arguments": recipe["root"]["getter"]["arguments"] = [{"type": "int32", "value": 1}]
    elif change == "getter_swift": recipe["root"]["getter"]["abi"] = "swift"
    elif change == "retained_getter": recipe["lifetime"]["mode"] = "retained_owner_main_queue_stable"
    elif change == "format": recipe["format"] = "anyuse.rea-native-pointer-path/2"
    elif change == "layout_v1": recipe["types"][0]["format"] = "anyuse.rea-native-pointer-layout/1"
    elif change == "cpu_bool": recipe["types"][0]["abi"]["cpu_subtype"] = False
    elif change == "pac": recipe["types"][0]["abi"]["cpu_subtype"] = 2
    elif change == "overlap": recipe["types"][0]["fields"][1]["offset"] = 0
    elif change == "unaligned": recipe["types"][0]["fields"][1]["offset"] = 4
    elif change == "unknown_target": recipe["types"][0]["fields"][1]["target"] = "Unknown"
    elif change == "extra_layout": recipe["types"].append({**copy.deepcopy(recipe["types"][1]), "name": "Unused"})
    elif change == "guard_type": recipe["preconditions"][0]["equals"] = 1
    elif change == "bound_guard": recipe["preconditions"][1]["equals"] = {"input": "generation"}
    elif change == "unknown_guard": recipe["preconditions"][0]["field"] = "missing"
    elif change == "unknown_path": recipe["path"] = ["missing"]
    elif change == "edge_not_pointer": recipe["path"] = ["ready"]
    elif change == "result_type": recipe["result_type"] = "Owner"
    elif change == "consumer_slot": recipe["consumer"]["argument"] = 1
    elif change == "consumer_target": recipe["consumer"]["call"]["symbol"] = "other_call"
    elif change == "consumer_context": recipe["consumer"]["call"]["context"] = "cxx_this"
    elif change == "consumer_adjustment": recipe["consumer"]["this_adjustment"] = 8
    elif change == "consumer_virtual": recipe["consumer"]["dispatch"] = "virtual"
    with pytest.raises(ValueError):
        B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("placement", ["pod_field", "swift_context", "different_slot", "different_call"])
def test_path_requires_the_actual_direct_top_level_call_and_slot(placement):
    actor, recipe = _path_actor()
    if placement == "pod_field":
        actor["arguments"][0] = {"type": {"kind": "struct", "fields": [{"name": "child", "type": "pointer"}]},
                                  "value": {"child": {"native_path": recipe}}}
    elif placement == "swift_context":
        actor["abi"] = "swift"
        actor["context"] = actor["arguments"].pop(0)
    elif placement == "different_slot": actor["arguments"].reverse()
    else: actor["target"]["symbol"] = "another_actual_call"
    with pytest.raises(ValueError): B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("root_kind", ["objc", "read", "swift_read"])
def test_existing_typed_owner_roots_are_preserved_in_walkers_and_receipts(root_kind):
    actor, recipe = _path_actor()
    receiver = {"class": "OwnerController", "kind": "unique", "subclasses": False}
    module = copy.deepcopy(actor["target"]["module"])
    evidence = copy.deepcopy(actor["signature_evidence"])
    if root_kind == "objc": recipe["root"] = {"objc": receiver}
    elif root_kind == "read":
        recipe["root"] = {"read": {"receiver": receiver, "receiver_class": "OwnerController", "module": module,
                                   "field": {"class": "OwnerController", "name": "_owner", "encoding": "^v", "offset": 8},
                                   "evidence": evidence}}
    else:
        recipe["root"] = {"swift_read": {"receiver": receiver, "receiver_class": "OwnerController", "module": module,
                    "swift_class": {"module": "Owned", "name": "OwnerController"},
                    "field": {"name": "owner", "type_name": "Swift.Optional<Swift.OpaquePointer>", "offset": 8},
                    "preconditions": [{"name": "ready", "type_name": "Swift.Bool", "equals": True}], "evidence": evidence}}
    recipe["lifetime"]["mode"] = "retained_owner_main_queue_stable"
    B.validate_actor(actor, allow_bindings=True)
    observations = _observations(recipe)
    assert P.validate_observations(observations, [recipe]) == observations
    assert [kind for kind, _ in P.walk_recipes({"native_path": recipe})] == ["native_path", root_kind]
    if root_kind == "read": assert B.pointer_read_recipes(actor) == [recipe["root"]["read"]]
    if root_kind == "swift_read": assert B.swift_read_recipes(actor) == [recipe["root"]["swift_read"]]


def test_shared_walk_preserves_nested_execution_paths_and_never_treats_pod_names_as_recipes():
    actor, recipe = _path_actor()
    outer = copy.deepcopy(actor)
    outer["arguments"] = [{"type": "int32", "value": 5}, {"type": "pointer", "value": {"getter": actor}}]
    actor["return_type"] = "pointer"
    recipe["consumer"]["call"]["return_type"] = "pointer"
    outer["arguments"][1]["value"]["getter"] = actor
    outer["arguments"].append({"type": {"kind": "struct", "fields": [{"name": "read", "type": "int32"}]}, "value": {"read": 42}})
    B.validate_actor(outer, allow_bindings=True)
    assert B.native_path_recipes(outer) == [recipe]
    assert len(list(B._native_calls_recursive(outer))) == 3
    assert B.pointer_read_recipes(outer) == []
    claims = B.native_file_claims(outer)
    assert claims["/guest/path-review.json"] == "f" * 64 and claims["/guest/objects.h"] == "d" * 64
    assert claims["/guest/abi.json"] == "b" * 64 and claims["/guest/Objects.dylib"] == "a" * 64


@pytest.mark.parametrize("change", ["unrelated_images", "bool_abi", "different_edge", "different_original", "duplicate", "only_hashes"])
def test_admission_binds_originals_to_exact_slice_abi_and_typed_contract(change):
    _, recipe = _path_actor()
    row = P.evidence_plan(recipe)[2]
    admission = _admission(recipe, row)
    P.validate_admission(admission, recipe, row)
    if change == "unrelated_images": admission["images"][0]["sha256"] = "0" * 64
    elif change == "bool_abi": admission["abi"]["cpu_subtype"] = False
    elif change == "different_edge": admission["declarations"][2]["contract"]["fields"][1]["target"] = "Unknown"
    elif change == "different_original": admission["declarations"][2]["originals"][0]["sha256"] = "0" * 64
    elif change == "duplicate": admission["declarations"].append(copy.deepcopy(admission["declarations"][2]))
    else: admission["declarations"] = [{"contract": {}, "originals": row["originals"]}]
    with pytest.raises(ValueError): P.validate_admission(admission, recipe, row)


@pytest.mark.parametrize("change", ["missing", "null", "raw", "alias", "expired", "reused_handle", "missing_final",
    "wrong_type", "missing_guard", "false_guard", "bool_integer", "stale_edge", "wrong_slot", "unsealed",
    "unused_permit", "retained_getter", "wrong_recipe", "identity_forgery", "unknown_key"])
def test_receipts_refuse_null_stale_unsealed_or_unbound_pointer_claims(change):
    _, recipe = _path_actor()
    rows = _observations(recipe)
    assert P.validate_observations(rows, [recipe]) == rows
    row, hops = rows[0], rows[0]["hops"]
    if change == "missing": rows = []
    elif change == "null": hops[0]["pointer"] = None
    elif change == "raw": hops[0]["pointer"]["handle"] = "0x1234"
    elif change == "alias": hops[0]["pointer"]["alias"] = "saved_root"
    elif change == "expired": hops[0]["pointer"]["expires"] = "restart"
    elif change == "reused_handle": hops[1]["pointer"]["handle"] = hops[0]["pointer"]["handle"]
    elif change == "missing_final": hops.pop()
    elif change == "wrong_type": hops[1]["type"] = "OtherNode"
    elif change == "missing_guard": hops[1]["guards"] = []
    elif change == "false_guard": hops[0]["guards"][0]["matched"] = False
    elif change == "bool_integer": hops[0]["guards"][0]["equals"] = 1
    elif change == "stale_edge": hops[0]["edge"]["unchanged"] = False
    elif change == "wrong_slot": row["argument"] = 1
    elif change == "unsealed": row["sealed"] = False
    elif change == "unused_permit": row["dispatch_permit_consumed"] = False
    elif change == "retained_getter": row["owner_retained"] = True
    elif change == "wrong_recipe": row["recipe"]["preconditions"][0]["equals"] = False
    elif change == "identity_forgery": hops[1]["identity_checked"] = True
    else: row["address"] = "0x1234"
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe])


def test_investigation_receipt_can_never_serve_as_dispatch_proof():
    _, recipe = _path_actor()
    rows = _observations(recipe, for_call=False)
    assert P.validate_observations(rows, [recipe], for_call=False) == rows
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=True)
    rows[0]["dispatch_permit_consumed"] = True
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=False)
    assert P.validate_observations([], []) == []


def _data_path_actor(mode="object", offset="0x2000"):
    actor, recipe = _path_actor()
    recipe["root"] = {"data": {"module": {"path": "/guest/Static.dylib", "sha256": "0" * 64},
                               "offset": offset, "mode": mode}}
    return actor, recipe


@pytest.mark.parametrize("mode", ["object", "pointer"])
def test_static_data_root_is_frozen_path_data_with_no_root_call_or_owner_retain(mode):
    actor, recipe = _data_path_actor(mode)
    bound = B.bind_actor(actor, {"payload": "23"})
    assert bound["arguments"][0]["value"]["native_path"] == recipe
    assert B.argument_leaves(actor["arguments"][0]) == []
    with pytest.raises(ValueError, match="typed leaf"):
        B.replace_argument_leaf(actor["arguments"][0], ["value", "native_path", "root", "data", "offset"], "0x2008")
    assert [tag for tag, _ in P.walk_recipes({"native_path": recipe})] == ["native_path", "data"]
    assert B.minimum_native_calls(actor) == 1 and len(list(B._native_calls_recursive(actor))) == 1
    assert B.native_file_claims(actor)["/guest/Static.dylib"] == "0" * 64
    assert {claim["path"] for claim in P.image_claims(recipe)} == {"/guest/Objects.dylib", "/guest/Static.dylib"}
    for for_call in (False, True):
        rows = _observations(recipe, for_call=for_call)
        assert rows[0]["owner_retained"] is False
        assert P.validate_observations(rows, [recipe], for_call=for_call) == rows


@pytest.mark.parametrize("change", ["numeric_offset", "bound_offset", "leading_zero", "uppercase", "negative",
    "unsafe_offset", "unaligned_object", "unaligned_slot", "unknown_mode", "missing_mode", "extra_callable",
    "relative_image", "retained_lifetime", "empty_path", "missing_root_evidence"])
def test_static_data_root_rejects_addresses_bindings_unsafe_locations_and_unproved_lifetime(change):
    actor, recipe = _data_path_actor()
    data = recipe["root"]["data"]
    if change == "numeric_offset": data["offset"] = 8192
    elif change == "bound_offset": data["offset"] = {"input": "offset"}
    elif change == "leading_zero": data["offset"] = "0x02000"
    elif change == "uppercase": data["offset"] = "0x20A0"
    elif change == "negative": data["offset"] = "-0x2000"
    elif change == "unsafe_offset": data["offset"] = "0x20000000000000"
    elif change == "unaligned_object": data["offset"] = "0x2004"
    elif change == "unaligned_slot": data.update(mode="pointer", offset="0x2004")
    elif change == "unknown_mode": data["mode"] = "callable"
    elif change == "missing_mode": data.pop("mode")
    elif change == "extra_callable": data["symbol"] = "owned_root"
    elif change == "relative_image": data["module"]["path"] = "Static.dylib"
    elif change == "retained_lifetime": recipe["lifetime"]["mode"] = "retained_owner_main_queue_stable"
    elif change == "empty_path": recipe["path"] = []
    else: recipe.pop("root_evidence")
    with pytest.raises(ValueError): B.validate_actor(actor, allow_bindings=True)


def test_static_pointer_slot_alignment_is_independent_of_pointee_alignment_and_offset_is_js_safe():
    actor, recipe = _data_path_actor("pointer", "0x2008")
    recipe["types"][0]["alignment"] = 16
    B.validate_actor(actor, allow_bindings=True)
    recipe["root"]["data"]["mode"] = "object"
    with pytest.raises(ValueError, match="alignment"): B.validate_actor(actor, allow_bindings=True)
    recipe["root"]["data"].update(mode="pointer", offset=hex(2**53 - 8))
    B.validate_actor(actor, allow_bindings=True)
    recipe["root"]["data"]["offset"] = hex(2**53)
    with pytest.raises(ValueError, match="JavaScript-safe"): B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("placement", ["argument", "context", "receiver"])
def test_static_data_location_never_becomes_an_outside_path_pointer_capability(placement):
    actor, recipe = _data_path_actor()
    root = copy.deepcopy(recipe["root"])
    if placement == "receiver":
        request = {"operations": [{"op": "native_read", "receiver": root, "bytes": 16,
                                   "fields": [{"name": "payload", "type": "int32", "offset": 4}],
                                   "layout_evidence": actor["signature_evidence"]}]}
        with pytest.raises(ValueError): B.validate_request(request)
    else:
        if placement == "argument": actor["arguments"][0]["value"] = root
        else:
            actor.update(abi="swift", arguments=[], context={"type": "pointer", "value": root})
        with pytest.raises(ValueError): B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("change", ["mode", "offset", "module", "lifetime"])
def test_static_data_source_admission_binds_the_exact_root_location_mode_and_lifetime(change):
    _, recipe = _data_path_actor()
    admission = _admission(recipe, P.evidence_plan(recipe)[0])
    if change == "mode": recipe["root"]["data"]["mode"] = "pointer"
    elif change == "offset": recipe["root"]["data"]["offset"] = "0x2008"
    elif change == "module": recipe["root"]["data"]["module"]["sha256"] = "1" * 64
    else: admission["declarations"][1]["contract"]["native_child_retained"] = True
    row = P.evidence_plan(recipe)[1 if change == "lifetime" else 0]
    with pytest.raises(ValueError): P.validate_admission(admission, recipe, row)


@pytest.mark.parametrize("change", ["retained", "getter_kind", "mode", "offset", "module"])
def test_static_data_receipts_cannot_relabel_root_or_claim_an_owner(change):
    _, recipe = _data_path_actor()
    rows = _observations(recipe, for_call=False)
    if change == "retained": rows[0]["owner_retained"] = True
    elif change == "getter_kind": rows[0]["root_kind"] = "getter"
    elif change == "mode": rows[0]["recipe"]["root"]["data"]["mode"] = "pointer"
    elif change == "offset": rows[0]["recipe"]["root"]["data"]["offset"] = "0x2008"
    else: rows[0]["recipe"]["root"]["data"]["module"]["sha256"] = "1" * 64
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=False)


def _borrowed_root_actor():
    actor, recipe = _path_actor()
    node = recipe["types"][1]
    node["fields"] = [{"name": "vptr", "type": "pointer", "offset": 0},
                      {"name": "generation", "type": "uint32", "offset": 8},
                      {"name": "payload", "type": "int32", "offset": 12}]
    node["runtime_identity"] = {"kind": "pinned_data_vptr", "field": "vptr",
                                "image": copy.deepcopy(actor["target"]["module"]), "data_offset": "0x2000"}
    recipe.update(root_type="Node", types=[node], path=[],
                  preconditions=[{"hop": 0, "field": "generation", "equals": 7}])
    recipe["lifetime"]["mode"] = "main_queue_borrowed_root"
    return actor, recipe


def _borrowed_root_read():
    actor, recipe = _borrowed_root_actor()
    recipe.pop("consumer")
    fields = [{key: copy.deepcopy(field[key]) for key in ("name", "type", "offset")}
              for field in recipe["types"][0]["fields"][1:]]
    return {"operations": [{"op": "native_read", "receiver": {"native_path": recipe}, "bytes": 16,
                            "fields": fields, "layout_evidence": copy.deepcopy(actor["signature_evidence"])}]}, recipe


def test_borrowed_root_is_one_guarded_same_object_with_only_outer_bindings():
    actor, recipe = _borrowed_root_actor()
    original = copy.deepcopy(actor)
    bound = B.bind_actor(actor, {"payload": "23"})
    assert actor == original and bound["arguments"][1]["value"] == 23
    assert bound["arguments"][0]["value"]["native_path"] == recipe
    assert B.argument_leaves(actor["arguments"][0]) == []
    assert B.native_path_recipes(actor) == [recipe] and B.minimum_native_calls(actor) == 2
    assert B.request_native_path_recipes(B.invocation_request(bound)) == [recipe]
    rows = _observations(recipe)
    assert len(rows[0]["hops"]) == 1 and rows[0]["hops"][0]["edge"] is None
    assert rows[0]["hops"][0]["identity_checked"] and rows[0]["owner_retained"] is False
    assert P.validate_observations(rows, [recipe]) == rows


@pytest.mark.parametrize("kind,value,text", [
    ("bool", True, "true"), ("int8", -7, "-7"), ("uint8", 7, "7"), ("int16", -7, "-7"),
    ("uint16", 7, "7"), ("int32", -7, "-7"), ("uint32", 7, "7"), ("int64", "-7", "-7"),
    ("uint64", "7", "7"), ("long", "-7", "-7"), ("ulong", "7", "7"),
    ("float", 1.25, "1.25"), ("double", -2.5, "-2.5")])
def test_borrowed_root_allows_only_the_enumerated_scalar_literals_bindings_and_results(kind, value, text):
    actor, recipe = _borrowed_root_actor()
    actor["arguments"][1] = {"type": kind, "value": {"input": "payload"}}
    actor["return_type"] = recipe["consumer"]["call"]["return_type"] = kind
    recipe["consumer"]["call"]["argument_types"][1] = kind
    bound = B.bind_actor(actor, {"payload": text})
    assert bound["arguments"][1] == {"type": kind, "value": value}
    B.validate_request(B.invocation_request(bound))
    actor["return_type"] = recipe["consumer"]["call"]["return_type"] = "void"
    B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("change", ["edge", "extra_layout", "other_result", "objc_root", "data_root",
    "getter_arg", "getter_abi", "identity_missing", "secondary_identity", "extra_pointer", "target",
    "root_evidence", "lifetime_evidence", "adjustment", "role", "slot", "pointer_result", "string_result"])
def test_borrowed_root_cannot_relax_shape_primary_type_or_original_admission(change):
    actor, recipe = _borrowed_root_actor()
    layout = recipe["types"][0]
    if change == "edge": recipe["path"] = ["vptr"]
    elif change == "extra_layout": recipe["types"].append({**copy.deepcopy(layout), "name": "Other"})
    elif change == "other_result": recipe["result_type"] = "Other"
    elif change == "objc_root": recipe["root"] = {"objc": {"kind": "unique", "class": "Owner"}}
    elif change == "data_root": recipe["root"] = {"data": {"module": actor["target"]["module"], "offset": "0x2000", "mode": "object"}}
    elif change == "getter_arg": recipe["root"]["getter"]["arguments"] = [{"type": "int32", "value": 1}]
    elif change == "getter_abi": recipe["root"]["getter"]["abi"] = "unix64"
    elif change == "identity_missing": layout.pop("runtime_identity")
    elif change == "secondary_identity":
        layout["fields"][0]["offset"], layout["fields"][1]["offset"], layout["fields"][2]["offset"] = 8, 0, 4
    elif change == "extra_pointer":
        layout["bytes"] = 24
        layout["fields"].append({"name": "unadmitted_pointer", "type": "pointer", "offset": 16})
    elif change == "target": layout["fields"][0]["target"] = "Node"
    elif change == "root_evidence": recipe.pop("root_evidence")
    elif change == "lifetime_evidence": recipe["lifetime"].pop("evidence")
    elif change == "adjustment": recipe["consumer"]["this_adjustment"] = 8
    elif change == "role": recipe["consumer"]["role"] = "borrowed_pointer"
    elif change == "slot": recipe["consumer"]["argument"] = 1
    else: actor["return_type"] = recipe["consumer"]["call"]["return_type"] = "pointer" if change == "pointer_result" else "utf8"
    with pytest.raises(ValueError): B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("kind,value", [("pointer", {"getter": _path_actor()[1]["root"]["getter"]}),
    ("utf8", "text"), ({"kind": "struct", "fields": [{"name": "value", "type": "int32"}]}, {"value": 1}),
    ({"kind": "buffer", "bytes": 1, "direction": "in"}, [1])])
def test_borrowed_root_refuses_other_pointer_getter_string_aggregate_or_buffer_arguments(kind, value):
    actor, recipe = _borrowed_root_actor()
    actor["arguments"][1] = {"type": kind, "value": value}
    recipe["consumer"]["call"]["argument_types"][1] = copy.deepcopy(kind)
    with pytest.raises(ValueError): B.validate_actor(actor)


def test_borrowed_root_cannot_be_nested_or_followed_by_another_call():
    actor, _ = _borrowed_root_actor()
    ordinary, _ = _path_actor()
    outer = copy.deepcopy(ordinary)
    outer["arguments"][0] = {"type": "pointer", "value": {"getter": actor}}
    with pytest.raises(ValueError, match="nested getter"): B.validate_actor(outer, allow_bindings=True)
    accepted = {"kind": "runtime_sequence", "steps": [ordinary, actor]}
    bound = B.bind_actor(accepted, {"payload": "23"})
    request = B.invocation_request(bound)
    assert B.validate_request(request) == request and B.minimum_request_native_calls(request) == 4
    for steps in ([actor, ordinary], [actor, copy.deepcopy(actor)]):
        with pytest.raises(ValueError, match="single final"): B.validate_actor({"kind": "runtime_sequence", "steps": steps}, allow_bindings=True)
    with pytest.raises(ValueError, match="single final"):
        B.validate_request({"operations": [request["operations"][-1], {"op": "modules"}]})


@pytest.mark.parametrize("mode", ["process_lifetime", "retained_owner_main_queue_stable"])
def test_existing_lifetime_modes_keep_their_nonempty_edge_requirement(mode):
    actor, recipe = _borrowed_root_actor()
    recipe["lifetime"]["mode"] = mode
    with pytest.raises(ValueError, match="1..6"): B.validate_actor(actor, allow_bindings=True)


def test_borrowed_read_is_a_scalar_projection_and_never_dispatch_evidence():
    request, recipe = _borrowed_root_read()
    assert B.validate_request(request) == request and B.minimum_request_native_calls(request) == 1
    request["operations"].insert(0, {"op": "modules"})
    request["operations"][-1]["fields"] = request["operations"][-1]["fields"][1:]
    assert B.validate_request(request) == request
    rows = _observations(recipe, for_call=False)
    assert rows[0]["sealed"] is rows[0]["dispatch_permit_consumed"] is False
    assert rows[0]["borrowed_scope"]["consumed"] is True
    assert P.validate_observations(rows, [recipe], for_call=False) == rows
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=True)


@pytest.mark.parametrize("change", ["pointer", "alias", "reordered", "offset", "extent", "consumer", "graph", "root_native", "helper", "later"])
def test_borrowed_read_refuses_pointer_escape_other_roots_graphs_or_later_operations(change):
    request, recipe = _borrowed_root_read()
    operation = request["operations"][0]
    if change == "pointer": operation["fields"] = [copy.deepcopy(recipe["types"][0]["fields"][0])]
    elif change == "alias": operation["fields"][0]["alias"] = "escaped"
    elif change == "reordered": operation["fields"].reverse()
    elif change == "offset": operation["fields"][0]["offset"] = 12
    elif change == "extent": operation["bytes"] = 24
    elif change == "consumer": recipe["consumer"] = _borrowed_root_actor()[1]["consumer"]
    elif change == "graph":
        operation = {"op": "graph", "root": {"native_path": recipe}, "root_type": "Node",
                     "types": [{"name": "Node", "bytes": 16, "fields": recipe["types"][0]["fields"],
                                "layout_evidence": operation["layout_evidence"]}], "max_depth": 0, "max_nodes": 1, "methods_limit": 0}
        request["operations"] = [operation]
    elif change == "root_native":
        actor, _ = _borrowed_root_actor()
        operation["receiver"] = {"native": actor}
    elif change == "helper":
        request["operations"].insert(0, {"op": "load_helper", "module": recipe["types"][0]["owner_image"],
                                       "build_evidence": operation["layout_evidence"]})
    else: request["operations"].append({"op": "modules"})
    with pytest.raises(ValueError): B.validate_request(request)


@pytest.mark.parametrize("change", ["scope_missing", "scope_extra", "bool_epoch", "float_epoch", "zero_epoch",
    "huge_epoch", "unequal", "wrong_use", "not_consumed", "expiry", "retained", "premise_tamper"])
@pytest.mark.parametrize("for_call", [False, True])
def test_borrowed_receipt_requires_exact_private_epoch_and_single_use_disposition(change, for_call):
    _, recipe = _borrowed_root_actor() if for_call else _borrowed_root_read()
    rows = _observations(recipe, for_call=for_call)
    row, scope = rows[0], rows[0]["borrowed_scope"]
    if change == "scope_missing": row.pop("borrowed_scope")
    elif change == "scope_extra": scope["source_proved"] = True
    elif change == "bool_epoch": scope["getter_epoch"] = scope["checked_epoch"] = True
    elif change == "float_epoch": scope["getter_epoch"] = scope["checked_epoch"] = 1.0
    elif change == "zero_epoch": scope["getter_epoch"] = scope["checked_epoch"] = 0
    elif change == "huge_epoch": scope["getter_epoch"] = scope["checked_epoch"] = 4097
    elif change == "unequal": scope["checked_epoch"] = 2
    elif change == "wrong_use": scope["use"] = "read" if for_call else "dispatch"
    elif change == "not_consumed": scope["consumed"] = False
    elif change == "expiry": scope["expires"] = "transaction"
    elif change == "retained": row["owner_retained"] = True
    else: row["recipe"]["lifetime"]["mode"] = "process_lifetime"
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=for_call)


def test_old_receipts_cannot_silently_claim_the_new_borrowed_scope():
    _, recipe = _path_actor()
    rows = _observations(recipe)
    rows[0]["borrowed_scope"] = {"getter_epoch": 1, "checked_epoch": 1, "use": "dispatch", "consumed": True, "expires": "use"}
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe])


@pytest.mark.parametrize("for_call", [False, True])
def test_borrowed_lifetime_admission_binds_source_premises_and_terminal_not_vptr_success(for_call):
    _, recipe = _borrowed_root_actor() if for_call else _borrowed_root_read()
    row = P.evidence_plan(recipe)[1]
    contract = row["contract"]
    assert contract["scope"] == "single_immediate_use" and contract["intervening_target_calls"] == 0
    assert contract["live_initialized_primary_base"] == "independently_established"
    assert contract["construction_destruction"] == contract["reentrant_event_loop"] == "excluded_for_entire_scope"
    assert contract["post_dispatch_access"] is False and contract["native_child_retained"] is False
    if for_call:
        assert contract["terminal"] == {"op": "native_invoke", "consumer": P.consumer_contract(recipe["consumer"])}
    else:
        assert contract["terminal"] == {"op": "native_read", "bytes": 16, "fields": [
            {"name": "generation", "type": "uint32", "offset": 8}, {"name": "payload", "type": "int32", "offset": 12}]}
    admission = _admission(recipe, row)
    P.validate_admission(admission, recipe, row)
    admission["declarations"][1]["contract"]["live_initialized_primary_base"] = "vptr_matched"
    with pytest.raises(ValueError): P.validate_admission(admission, recipe, row)


def _lifecycle_root_actor():
    """Inert source contracts, not vendor ABI/lifetime or observed construction."""
    actor, recipe = _borrowed_root_actor()
    def entry(path, sha, offset):
        return {"module": {"path": path, "sha256": sha * 64}, "offset": offset}
    observer_evidence = copy.deepcopy(recipe["root_evidence"])
    observer_evidence["claim"]["pointer"] = "/observer"
    observer = {"format": NL.FORMAT,
        "constructor": {"entry": entry("/guest/Construct.dylib", "1", "0x1000"),
            "return_sites": [entry("/guest/Creator.dylib", "2", "0x2004")],
            "this_argument": 0, "this_adjustment": 0, "completion": "complete_object_normal_return"},
        "destructors": [{"entry": entry("/guest/Destroy.dylib", "3", "0x3000"),
            "this_argument": 0, "this_adjustment": 0}],
        "owner_thread": "observed_constructor_on_macos_main", "selection": "sole_live_at_observation_end",
        "observe_ms": 20000, "event_limit": 64, "generation_limit": 8, "evidence": observer_evidence}
    recipe["root"] = {"lifecycle": observer}
    recipe["lifetime"]["mode"] = NL.MODE
    return actor, recipe


def _lifecycle_root_read():
    actor, recipe = _lifecycle_root_actor()
    recipe.pop("consumer")
    fields = [{key: copy.deepcopy(field[key]) for key in ("name", "type", "offset")}
              for field in recipe["types"][0]["fields"][1:]]
    return {"operations": [{"op": "native_read", "receiver": {"native_path": recipe}, "bytes": 16,
        "fields": fields, "layout_evidence": copy.deepcopy(actor["signature_evidence"])}]}, recipe


def _lifecycle_scope(recipe, *, for_call=False):
    return {"observer_sha256": NL.observer_digest(recipe["root"]["lifecycle"]), "generation": 1,
        "constructor_enter_event": 2, "constructor_return_event": 3, "selection_event": 5,
        "acquire_event": 6, "consume_event": 7, "owner_thread_id": 101,
        "acquire_epoch": 0, "checked_epoch": 0, "use": "dispatch" if for_call else "read",
        "consumed": True, "expires": "use"}


def _lifecycle_result(recipe, *, for_call=False):
    observer = recipe["root"]["lifecycle"]
    def event(seq, kind, generation=None, entry=None, return_site=None, thread=None):
        return {"seq": seq, "kind": kind, "generation": generation, "entry": entry,
                "return_site": return_site, "thread": thread}
    hooks = 1 + len(observer["destructors"])
    return {"format": NL.OBSERVATION, "observer_sha256": NL.observer_digest(observer), "status": "completed",
        "owner_thread_id": 101, "selected_generation": 1,
        "events": [event(1, "armed", thread=101), event(2, "constructor_enter", 1, 0, 0, 101),
                   event(3, "constructor_return", 1, 0, 0, 101), event(4, "observation_end"),
                   event(5, "selected", 1), event(6, "acquire", 1, thread=101),
                   event(7, "consume", 1, thread=101), event(8, "stop")],
        "hooks_requested": hooks, "hook_attempts": hooks, "hooks_installed": hooks, "hooks_detached": hooks,
        "listeners_cleanup_flushed": True, "callbacks_inflight": 0, "lifecycle_frames_inflight": 0,
        "poisoned": False, "tokens_expired": True}


def _validate_lifecycle(recipe, *, for_call=False, observation=None, scope=None, native_calls=None, invocations=None):
    return NL.validate_result(_lifecycle_result(recipe, for_call=for_call) if observation is None else observation,
        recipe["root"]["lifecycle"], scope=_lifecycle_scope(recipe, for_call=for_call) if scope is None else scope,
        for_call=for_call, native_calls=int(for_call) if native_calls is None else native_calls,
        invocations=int(for_call) if invocations is None else invocations)


@pytest.mark.parametrize("for_call", [False, True])
def test_lifecycle_birth_use_and_cleanup_are_bound_without_a_synthetic_getter(for_call):
    actor, recipe = _lifecycle_root_actor() if for_call else _lifecycle_root_read()
    original = copy.deepcopy(recipe)
    assert P.is_lifecycle_root(recipe) and P.is_immediate_root(recipe) and not P.is_borrowed_root(recipe)
    if for_call:
        bound = B.bind_actor(actor, {"payload": "23"})
        assert bound["arguments"][1]["value"] == 23 and B.minimum_native_calls(bound) == 1
        assert B.argument_leaves(bound["arguments"][0]) == []
    else:
        assert B.validate_request(actor) == actor and B.minimum_request_native_calls(actor) == 0
    rows = _observations(recipe, for_call=for_call)
    assert rows[0]["root_kind"] == "lifecycle" and rows[0]["owner_retained"] is False
    assert "borrowed_scope" not in rows[0] and P.validate_observations(rows, [recipe], for_call=for_call) == rows
    observed = _validate_lifecycle(recipe, for_call=for_call)
    observed["events"][1]["generation"] = 7
    assert _validate_lifecycle(recipe, for_call=for_call)["events"][1]["generation"] == 1
    assert recipe == original


@pytest.mark.parametrize("change", ["raw_entry", "unsafe_offset", "unaligned", "noncanonical", "bool_arg",
    "adjustment", "base_completion", "duplicate_return", "duplicate_hook", "hook_role", "missing_destructor",
    "return_is_constructor", "return_is_destructor", "newest", "preexisting", "bool_limit", "too_many_events",
    "too_many_generations", "evidence", "conflict"])
def test_lifecycle_discovery_cannot_turn_unknown_entries_or_policies_into_a_root(change):
    _, recipe = _lifecycle_root_actor()
    spec = recipe["root"]["lifecycle"]
    ctor = spec["constructor"]
    if change == "raw_entry": ctor["entry"] = {"address": "0x1000"}
    elif change == "unsafe_offset": ctor["entry"]["offset"] = "0x20000000000000"
    elif change == "unaligned": ctor["entry"]["offset"] = "0x1001"
    elif change == "noncanonical": ctor["entry"]["offset"] = "0x01000"
    elif change == "bool_arg": ctor["this_argument"] = False
    elif change == "adjustment": ctor["this_adjustment"] = -16
    elif change == "base_completion": ctor["completion"] = "base_constructor_return"
    elif change == "duplicate_return": ctor["return_sites"] *= 2
    elif change == "duplicate_hook": spec["destructors"] *= 2
    elif change == "hook_role": spec["destructors"][0]["entry"] = copy.deepcopy(ctor["entry"])
    elif change == "missing_destructor": spec["destructors"] = []
    elif change == "return_is_constructor": ctor["return_sites"] = [copy.deepcopy(ctor["entry"])]
    elif change == "return_is_destructor": ctor["return_sites"] = [copy.deepcopy(spec["destructors"][0]["entry"])]
    elif change == "newest": spec["selection"] = "newest"
    elif change == "preexisting": spec["adopt_preexisting"] = True
    elif change == "bool_limit": spec["generation_limit"] = True
    elif change == "too_many_events": spec["event_limit"] = 65
    elif change == "too_many_generations": spec["generation_limit"] = 9
    elif change == "evidence": spec.pop("evidence")
    else:
        ctor["return_sites"][0]["module"] = {**ctor["entry"]["module"], "sha256": "0" * 64}
    with pytest.raises(ValueError): NL.validate_observer(spec)


def test_lifecycle_resources_and_admission_bind_independent_creator_and_destructor_images():
    _, recipe = _lifecycle_root_actor()
    spec = recipe["root"]["lifecycle"]
    assert [entry["module"]["path"] for entry in NL.entries(spec)] == [
        "/guest/Construct.dylib", "/guest/Destroy.dylib", "/guest/Creator.dylib"]
    assert [kind for kind, _ in P.walk_recipes({"native_path": recipe})] == ["native_path", "lifecycle"]
    claims = P.file_claims(recipe)
    for path, sha in NL.file_claims(spec).items():
        assert claims[path] == sha
    assert "/guest/Creator.dylib" in claims and "/guest/Destroy.dylib" in claims
    assert "evidence" not in P.root_contract(recipe)["root"]["lifecycle"]
    rows = P.evidence_plan(recipe)
    row = next(row for row in rows if row["contract"].get("format") == NL.CONTRACT)
    admission = _admission(recipe, row)
    P.validate_admission(admission, recipe, row)
    prior_digest = NL.observer_digest(spec)
    spec["constructor"]["return_sites"][0]["offset"] = "0x2010"
    assert NL.observer_digest(spec) != prior_digest
    changed_row = next(row for row in P.evidence_plan(recipe) if row["contract"].get("format") == NL.CONTRACT)
    with pytest.raises(ValueError): P.validate_admission(admission, recipe, changed_row)


@pytest.mark.parametrize("change", ["early_return", "wrong_thread", "another_generation", "reuse_generation",
    "missing_birth", "wrong_return_site", "ambiguous", "death_before_use", "event_inside_use", "unbalanced",
    "poison", "expiry", "unhook", "flush", "callbacks", "extra_call", "public_epoch", "raw_pointer"])
def test_lifecycle_same_scalar_output_cannot_hide_stale_birth_phase_or_cleanup(change):
    _, recipe = _lifecycle_root_read()
    spec, result, scope = recipe["root"]["lifecycle"], _lifecycle_result(recipe), _lifecycle_scope(recipe)
    calls = 0
    if change == "early_return": result["events"][1], result["events"][2] = result["events"][2], result["events"][1]
    elif change == "wrong_thread": result["events"][1]["thread"] = 102
    elif change == "another_generation": scope["generation"] = result["selected_generation"] = 2
    elif change == "reuse_generation":
        result["events"].insert(3, {**result["events"][1], "kind": "constructor_enter"})
    elif change == "missing_birth": result["events"][1]["generation"] = result["events"][1]["return_site"] = None
    elif change == "wrong_return_site": result["events"][2]["return_site"] = None
    elif change == "ambiguous":
        result["events"][3:3] = [{**result["events"][1], "generation": 2}, {**result["events"][2], "generation": 2}]
    elif change == "death_before_use":
        result["events"].insert(5, {**result["events"][1], "kind": "destructor_enter", "return_site": None})
    elif change == "event_inside_use":
        result["events"].insert(6, {**result["events"][1], "kind": "destructor_enter", "generation": None, "return_site": None})
    elif change == "unbalanced": result["events"][2]["kind"] = "destructor_leave"
    elif change == "poison": result["poisoned"] = True
    elif change == "expiry": result["tokens_expired"] = False
    elif change == "unhook": result["hooks_detached"] -= 1
    elif change == "flush": result["listeners_cleanup_flushed"] = False
    elif change == "callbacks": result["callbacks_inflight"] = 1
    elif change == "extra_call": calls = 1
    elif change == "public_epoch": scope["acquire_epoch"] = scope["checked_epoch"] = 1
    else: result["events"][1]["pointer"] = "0x1000"
    # Retain structural sequence numbering after stimuli, so the semantic event
    # mismatch rather than an incidental old sequence number is under test.
    for index, event in enumerate(result["events"], 1): event["seq"] = index
    with pytest.raises(ValueError):
        NL.validate_result(result, spec, scope=scope, for_call=False, native_calls=calls, invocations=0)


def test_lifecycle_post_window_birth_cannot_extend_an_already_consumed_valid_ledger():
    _, recipe = _lifecycle_root_read()
    result, scope = _lifecycle_result(recipe), _lifecycle_scope(recipe)
    assert _validate_lifecycle(recipe, observation=result, scope=scope) == result
    # Preserve the selected generation, scope references and zero explicit-call
    # counters. Only a fresh, balanced birth after consume changes the ledger.
    birth = [dict(event, generation=2) for event in result["events"][1:3]]
    result["events"][-1:-1] = birth
    for index, event in enumerate(result["events"], 1): event["seq"] = index
    assert result["selected_generation"] == scope["generation"] == 1
    with pytest.raises(ValueError, match="post-window"):
        _validate_lifecycle(recipe, observation=result, scope=scope, native_calls=0, invocations=0)


def test_lifecycle_destructor_after_consumed_dispatch_does_not_allow_a_second_use():
    _, recipe = _lifecycle_root_actor()
    spec, result, scope = recipe["root"]["lifecycle"], _lifecycle_result(recipe, for_call=True), _lifecycle_scope(recipe, for_call=True)
    template = {"seq": 0, "generation": 1, "entry": 0, "return_site": None, "thread": 101}
    result["events"][7:7] = [{**template, "kind": "destructor_enter"}, {**template, "kind": "destructor_leave"}]
    for index, event in enumerate(result["events"], 1): event["seq"] = index
    assert NL.validate_result(result, spec, scope=scope, for_call=True, native_calls=1, invocations=1) == result
    result["events"].insert(-1, {**result["events"][5], "seq": len(result["events"]), "kind": "acquire"})
    for index, event in enumerate(result["events"], 1): event["seq"] = index
    with pytest.raises(ValueError): NL.validate_result(result, spec, scope=scope, for_call=True, native_calls=1, invocations=1)


@pytest.mark.parametrize("field", ["callbacks_inflight", "poisoned", "tokens_expired", "hooks_installed"])
def test_lifecycle_resource_or_result_booleans_are_not_interchangeable_with_integers(field):
    _, recipe = _lifecycle_root_read()
    result = _lifecycle_result(recipe)
    result[field] = {"callbacks_inflight": False, "poisoned": 0, "tokens_expired": 1, "hooks_installed": True}[field]
    with pytest.raises(ValueError): _validate_lifecycle(recipe, observation=result)


def test_lifecycle_and_old_getter_receipts_cannot_exchange_scopes_or_lifetime_modes():
    request, recipe = _lifecycle_root_read()
    rows = _observations(recipe, for_call=False)
    rows[0]["borrowed_scope"] = {"getter_epoch": 1, "checked_epoch": 1, "use": "read", "consumed": True, "expires": "use"}
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=False)
    _, old = _borrowed_root_read()
    old_rows = _observations(old, for_call=False)
    old_rows[0]["lifecycle_scope"] = _lifecycle_scope(recipe)
    with pytest.raises(ValueError): P.validate_observations(old_rows, [old], for_call=False)
    for mode in ("process_lifetime", "main_queue_borrowed_root"):
        recipe["lifetime"]["mode"] = mode
        with pytest.raises(ValueError): B.validate_request(request)


@pytest.mark.parametrize("change", ["edge", "primary_identity", "extra_pointer", "pointer_result", "nonthis", "getter_mode"])
def test_lifecycle_root_keeps_the_same_immediate_scalar_primary_base_boundary(change):
    actor, recipe = _lifecycle_root_actor()
    if change == "edge": recipe["path"] = ["vptr"]
    elif change == "primary_identity": recipe["types"][0].pop("runtime_identity")
    elif change == "extra_pointer":
        recipe["types"][0]["bytes"] = 24
        recipe["types"][0]["fields"].append({"name": "escape", "type": "pointer", "offset": 16})
    elif change == "pointer_result": actor["return_type"] = recipe["consumer"]["call"]["return_type"] = "pointer"
    elif change == "nonthis": recipe["consumer"]["role"] = "borrowed_pointer"
    else: recipe["lifetime"]["mode"] = P.BORROWED_ROOT_MODE
    with pytest.raises(ValueError): B.validate_actor(actor, allow_bindings=True)


@pytest.mark.parametrize("change", ["bool_epoch", "bool_generation", "unconsumed", "wrong_use", "expiry", "drifted_observer"])
def test_lifecycle_scope_cannot_be_relabelled_when_the_scalar_read_is_identical(change):
    _, recipe = _lifecycle_root_read()
    rows = _observations(recipe, for_call=False)
    scope = rows[0]["lifecycle_scope"]
    if change == "bool_epoch": scope["acquire_epoch"] = scope["checked_epoch"] = False
    elif change == "bool_generation": scope["generation"] = True
    elif change == "unconsumed": scope["consumed"] = 1
    elif change == "wrong_use": scope["use"] = "dispatch"
    elif change == "expiry": scope["expires"] = "transaction"
    else: scope["observer_sha256"] = "0" * 64
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe], for_call=False)


def test_lifecycle_observer_provenance_changes_invalidate_an_otherwise_identical_birth_and_use():
    _, recipe = _lifecycle_root_read()
    old_result, old_scope = _lifecycle_result(recipe), _lifecycle_scope(recipe)
    spec = recipe["root"]["lifecycle"]
    declaration = P.root_contract(recipe)
    spec["evidence"]["claim"]["sha256"] = "4" * 64
    # Declarations omit their evidence hash to avoid a self-reference, while
    # complete observer/resource and receipt identities still bind that hash.
    assert P.root_contract(recipe) == declaration
    assert NL.file_claims(spec)[spec["evidence"]["claim"]["file"]] == "4" * 64
    with pytest.raises(ValueError):
        NL.validate_result(old_result, spec, scope=old_scope, for_call=False, native_calls=0, invocations=0)


def _qt_moc_path_actor():
    """Inert Qt/source declarations, not recovered app type/dispatcher proof."""
    original, recipe = _lifecycle_root_actor()
    recipe["types"][0]["fields"] = [copy.deepcopy(recipe["types"][0]["fields"][0])]
    recipe["preconditions"] = []
    selection = {"format": "anyuse.rea-qt-moc-selection/1", "module": copy.deepcopy(original["target"]["module"]),
        "qt_core": {"path": "/guest/QtCore", "sha256": "6" * 64},
        "catalog": {"file": "/guest/qt-catalog.json", "sha256": "4" * 64},
        "code": {"file": "/guest/qt-code.json", "sha256": "5" * 64},
        "table": {"file": "/guest/qt-table.json", "sha256": "7" * 64},
        "qt_sources": {"file": "/guest/qt-sources.json", "sha256": "8" * 64},
        "class_name": recipe["root_type"], "record_offset": "0x2000", "dispatcher_offset": "0x3000",
        "local_index": 0, "signature": "togglePanel()"}
    recipe["consumer"] = {"kind": "qt_moc", "selection": copy.deepcopy(selection), "argument": 0,
        "pointee_type": recipe["result_type"], "role": "qobject_primary_base", "dispatch": "static_metacall",
        "this_adjustment": 0, "evidence": copy.deepcopy(recipe["consumer"]["evidence"])}
    return {"kind": "qt_moc_method", "receiver": {"native_path": recipe}, "selection": selection}, recipe


def test_qt_consumer_has_its_own_terminal_and_complete_original_closure():
    actor, recipe = _qt_moc_path_actor()
    saved = copy.deepcopy(actor)
    assert B.validate_actor(actor) == actor and actor == saved
    terminal = P.borrowed_terminal_contract(recipe)
    assert terminal == {"op": "qt_moc_invoke", "consumer": Q.consumer_contract(recipe["consumer"])}
    assert P.lifetime_contract(recipe)["terminal"] == terminal
    assert terminal["consumer"]["qobject_base"] == "independently_admitted_primary_base_at_zero"
    assert terminal["consumer"]["callable"]["argument_types"] == ["pointer", "int32", "int32", "pointer"]
    assert [tag for tag, _ in P.walk_recipes(actor)] == ["qt_moc", "native_path", "lifecycle"]
    request = B.invocation_request(actor)
    assert [tag for tag, _ in P.walk_recipes(request)] == ["qt_moc", "native_path", "lifecycle"]
    claims = P.file_claims(recipe)
    selection = actor["selection"]
    for field in ("module", "qt_core"):
        assert claims[selection[field]["path"]] == selection[field]["sha256"]
    for field in ("catalog", "code", "table", "qt_sources"):
        assert claims[selection[field]["file"]] == selection[field]["sha256"]
    from pathlib import PurePosixPath
    for name, sha in Q._SOURCES.items():
        assert claims[str(PurePosixPath(selection["qt_sources"]["file"]).parent / name)] == sha
    assert B.minimum_native_calls(actor) == 1
    assert P.validate_observations(_observations(recipe), [recipe])


@pytest.mark.parametrize("fault", ["getter_wrong_mode", "extra_scalar", "guard", "adjustment", "native_role", "slot", "selection"])
def test_qt_consumer_never_expands_the_observed_primary_qobject_scope(fault):
    actor, recipe = _qt_moc_path_actor()
    if fault == "getter_wrong_mode":
        borrowed = _borrowed_root_actor()[1]
        recipe["root"] = borrowed["root"]
    elif fault == "extra_scalar": recipe["types"][0]["fields"].append({"name": "payload", "type": "int32", "offset": 8})
    elif fault == "guard": recipe["preconditions"] = [{"hop": 0, "field": "vptr", "equals": "0"}]
    elif fault == "adjustment": recipe["consumer"]["this_adjustment"] = 8
    elif fault == "native_role": recipe["consumer"]["role"] = "cxx_this"
    elif fault == "slot": recipe["consumer"]["argument"] = 1
    else: recipe["consumer"]["selection"]["local_index"] = 1
    with pytest.raises(ValueError): B.validate_actor(actor)


def test_qt_consumer_cannot_be_lowered_through_native_function_or_inferred_from_class_name():
    qt_actor, recipe = _qt_moc_path_actor()
    ordinary = _lifecycle_root_actor()[0]
    ordinary["arguments"][0]["value"] = {"native_path": recipe}
    with pytest.raises(ValueError): B.validate_actor(ordinary, allow_bindings=True)
    with pytest.raises(ValueError): P.require_consumer(recipe, ordinary, 0)
    row = P.evidence_plan(recipe)[-1]
    admission = _admission(recipe, row)
    P.validate_admission(admission, recipe, row)
    changed = next(item for item in admission["declarations"] if item["contract"].get("format") == "anyuse.rea-qt-moc-consumer/1")
    changed["contract"]["qobject_base"] = "inferred_from_class_name_or_vptr"
    with pytest.raises(ValueError): P.validate_admission(admission, recipe, row)


@pytest.mark.parametrize("fault", ["missing", "native_consumer", "unsealed", "owner_retained", "old_terminal"])
def test_qt_receipts_bind_the_exact_admitted_consumer_and_seal(fault):
    actor, recipe = _qt_moc_path_actor()
    rows = _observations(recipe)
    if fault == "missing": rows = []
    elif fault == "native_consumer": rows[0]["consumer"] = P.consumer_contract(_lifecycle_root_actor()[1]["consumer"])
    elif fault == "unsealed": rows[0]["sealed"] = False
    elif fault == "owner_retained": rows[0]["owner_retained"] = True
    else:
        changed = copy.deepcopy(recipe)
        changed["lifetime"]["mode"] = "main_queue_borrowed_root"
        rows[0]["recipe"] = changed
    with pytest.raises(ValueError): P.validate_observations(rows, [recipe])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
