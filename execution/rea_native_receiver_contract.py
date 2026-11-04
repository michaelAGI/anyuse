"""Explicit binary-correlated receiver hypotheses, never source admission.

The immutable declaration binds original observations to a narrow proposed ABI
and receiver. Readable bytes, vptr equality and immediate use do not establish
C++ type, lifetime, thread ownership or target-side non-escape. Only the caller's
explicit model-judged experimental policy admits this separate evidence tier.
Pure validators do no file IO; declare/resources are guest-only and execute no
target code. The existing runtime owns acquisition, consumption and cleanup.
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

FORMAT = "anyuse.rea-native-receiver-model-declaration/1"
RECIPE_FORMAT = "anyuse.rea-native-receiver-hypothesis/1"
KIND = "declared_native_receiver"
MODE = "main_queue_binary_runtime_hypothesis"
POLICY = "binary_runtime_hypothesis"
ROOT_CONTRACT = "anyuse.rea-native-receiver-hypothesis-root/1"
LIFETIME_CONTRACT = "anyuse.rea-native-receiver-hypothesis-lifetime/1"
CONSUMER_CONTRACT = "anyuse.rea-native-receiver-hypothesis-consumer/1"
TYPE_BASIS = "model_declared_binary_correlated_not_independently_reviewed"
MAX_BYTES = 256 * 1024
RECEIPT = "native-receiver-declaration.json"
ROLES = ("getter_code", "consumer_code", "vptr_data")
BINDING_KEYS = ("bundle", "target", "task", "params", "plan", "success_criteria", "workflow",
                "observer_profile_sha256", "native_contract_mode", "native_receiver_mode")
BASIS = {"abi_basis": "model_declared", "receiver_basis": "binary_correlated_hypothesis",
         "lifetime_basis": "assumed_not_established", "abi_recovered": False,
         "type_proven": False, "lifetime_proven": False, "proof_created": False}


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


def _resource_module(name):
    """Load package-only siblings from the fixed worker's own installation.

    The bridge's file entry imports this module without a package. Its resource
    readers themselves use relative imports, so importing them by bare name
    loses their package context. Match the bridge's sibling ``execution``
    bootstrap, without retaining an added search path or accepting a different
    installation already registered under that package name.
    """
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
                  "standalone receiver resource import found a foreign package/module")
            if module_name == "execution":
                _need([Path(entry).resolve() for entry in getattr(existing, "__path__", ())] == [directory],
                      "standalone receiver resource import found a foreign package search path")
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(directory.parent))
        module = importlib.import_module(fullname)
        _need(Path(module.__file__).resolve(strict=True) == directory / (name + ".py"),
              "standalone receiver resource import escaped its installed sibling package")
        return module
    finally:
        sys.path[:] = previous


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _same(left, right):
    return _path().same_data(left, right)


def _bounded(value):
    _need(len(json.dumps(value, allow_nan=False).encode()) <= MAX_BYTES,
          "bounded native receiver hypothesis required")


def is_recipe(value):
    return type(value) is dict and value.get("format") == RECIPE_FORMAT


def is_layout(value):
    return (type(value) is dict and type(value.get("evidence")) is dict and
            type(value["evidence"].get("claim")) is dict and value["evidence"]["claim"].get("kind") == KIND)


def is_consumer(value):
    return is_layout(value)


def evidence_claim(value):
    p = _path()
    p.keys(value, {"kind", "file", "sha256", "pointer"})
    _need(value["kind"] == KIND, "separate experimental receiver evidence kind required")
    p.image({"path": value["file"], "sha256": value["sha256"]})
    _need(value["pointer"] in {"/signatures/getter", "/signatures/consumer", *(f"/contracts/{i}" for i in range(4))},
          "exact experimental receiver contract selector required")


def evidence(value):
    p = _path()
    p.keys(value, {"claim", "originals"})
    evidence_claim(value["claim"])
    rows = value["originals"]
    _need(type(rows) is list and len(rows) == 3, "three distinct original receiver observations required")
    seen = {value["claim"]["file"]}
    for role, row in zip(ROLES, rows):
        p.keys(row, {"role", "file", "sha256"})
        _need(row["role"] == role and row["file"] not in seen,
              "ordered original code/data observations cannot be self-attestations")
        p.image({"path": row["file"], "sha256": row["sha256"]})
        seen.add(row["file"])


def _entry(value):
    p = _path()
    p.keys(value, {"module", "offset"})
    p.image(value["module"])
    _need(type(value["offset"]) is str and p.OFFSET.fullmatch(value["offset"]) is not None and
          int(value["offset"], 16) <= p.MAX_DATA_OFFSET - 4 and int(value["offset"], 16) % 4 == 0,
          "exact aligned plain-arm64 original entry required")


def _unsigned_actor(value, *, getter, params):
    p = _path()
    p.keys(value, {"kind", "target", "abi", "arguments", "return_type"})
    _need(value["kind"] == "native_function" and value["abi"] == "default",
          "ordinary direct default-ABI receiver call required")
    _entry(value["target"])
    args = value["arguments"]
    _need(type(args) is list and (args == [] if getter else 1 <= len(args) <= 8),
          "zero-argument getter or bounded scalar receiver call required")
    if getter:
        _need(value["return_type"] == "pointer", "fresh receiver getter must return a pointer")
        return copy.deepcopy(value)
    _need(type(value["return_type"]) is str and value["return_type"] in p.BORROWED_SCALAR_TYPES | {"void"},
          "experimental receiver result must be scalar or void")
    _need(_same(args[0], {"type": "pointer", "value": {"receiver": "fresh"}}),
          "declare arg0 as the freshly acquired unadjusted receiver")
    names = None if params is None else {item["name"] for item in params}
    for argument in args[1:]:
        p.keys(argument, {"type", "value"})
        _need(type(argument["type"]) is str and argument["type"] in p.BORROWED_SCALAR_TYPES,
              "experimental receiver call accepts scalar-only tail arguments")
        _bridge()._native_argument(argument, allow_bindings=True)
        scalar_value = argument["value"]
        if type(scalar_value) is dict:
            _need(names is None or scalar_value["input"] in names, "receiver argument names an undeclared input")
    return value


def _actor(value, *, getter, params):
    _unsigned_actor(value, getter=getter, params=params)
    return copy.deepcopy(value)


def validate_request(request, params=()):
    p = _path()
    p.keys(request, {"getter", "consumer", "vptr", "citations"}, {"op"})
    _need("op" not in request or request["op"] == "declare_native_receiver", "native receiver declaration operation required")
    _actor(request["getter"], getter=True, params=params)
    _actor(request["consumer"], getter=False, params=params)
    _need(not _same(request["getter"]["target"], request["consumer"]["target"]),
          "receiver getter and consumer must be distinct exact entries")
    vptr = request["vptr"]
    p.keys(vptr, {"image", "data_offset"})
    p.image(vptr["image"])
    _need(type(vptr["data_offset"]) is str and p.OFFSET.fullmatch(vptr["data_offset"]) is not None and
          int(vptr["data_offset"], 16) <= p.MAX_DATA_OFFSET - 8 and int(vptr["data_offset"], 16) % 8 == 0,
          "exact naturally aligned original vptr data address point required")
    citations = request["citations"]
    _need(type(citations) is list and len(citations) == 3, "cite getter code, consumer code and vptr data once")
    seen = set()
    for role, row in zip(ROLES, citations):
        p.keys(row, {"role", "artifact", "pointer"})
        name = row["artifact"]
        _need(row["role"] == role and type(name) is str and 0 < len(name) <= 4096 and "\0" not in name and
              not name.startswith("/") and str(PurePosixPath(name)) == name and
              ".." not in PurePosixPath(name).parts and name not in seen and row["pointer"] == "/range",
              "ordered distinct current-session original observation citations required")
        seen.add(name)
    _bounded(request)
    return copy.deepcopy(request)


def signature_contract(actor):
    return {"abi": actor["abi"], "argument_types": [item["type"] for item in actor["arguments"]],
            "return_type": actor["return_type"], "context": False, "pointer_policy": "borrowed_no_escape"}


def _claim(file, sha, pointer):
    return {"kind": KIND, "file": str(file), "sha256": sha, "pointer": pointer}


def _decorate(request, file, sha, originals):
    p = _path()
    def wrapper(index):
        return {"claim": _claim(file, sha, f"/contracts/{index}"), "originals": copy.deepcopy(originals)}
    getter = {**copy.deepcopy(request["getter"]), "signature_evidence": _claim(file, sha, "/signatures/getter")}
    unsigned = request["consumer"]
    call = {"module": copy.deepcopy(unsigned["target"]["module"]), "offset": unsigned["target"]["offset"],
            **{key: copy.deepcopy(value) for key, value in signature_contract(unsigned).items() if key != "context"},
            "context": "none", "signature_evidence": _claim(file, sha, "/signatures/consumer")}
    image = copy.deepcopy(request["vptr"]["image"])
    recipe = {"format": RECIPE_FORMAT, "root": {"getter": getter}, "root_type": "DeclaredReceiver",
        "result_type": "DeclaredReceiver", "types": [{"format": p.LAYOUT, "name": "DeclaredReceiver",
            "owner_image": image, "abi": dict(p.ABI), "bytes": 8, "alignment": 8,
            "fields": [{"name": "vptr", "type": "pointer", "offset": 0}], "evidence": wrapper(2),
            "runtime_identity": {"kind": "pinned_data_vptr", "field": "vptr", "image": copy.deepcopy(image),
                                 "data_offset": request["vptr"]["data_offset"]}}],
        "path": [], "preconditions": [], "root_evidence": wrapper(0), "lifetime": {"mode": MODE, "evidence": wrapper(1)},
        "consumer": {"call": call, "argument": 0, "pointee_type": "DeclaredReceiver", "role": "cxx_this",
                     "dispatch": "direct", "this_adjustment": 0, "evidence": wrapper(3)}}
    actor = {**copy.deepcopy(unsigned), "signature_evidence": _claim(file, sha, "/signatures/consumer")}
    actor["arguments"][0]["value"] = {"native_path": recipe}
    return recipe, actor


def _signature_claim(value, pointer):
    evidence_claim(value)
    _need(value["pointer"] == pointer, "receiver signature selects the wrong declaration role")


def validate_getter(value):
    p = _path()
    p.keys(value, {"kind", "target", "abi", "arguments", "return_type", "signature_evidence"})
    _actor({key: child for key, child in value.items() if key != "signature_evidence"}, getter=True, params=None)
    _signature_claim(value["signature_evidence"], "/signatures/getter")
    return copy.deepcopy(value)


def validate_layout(value):
    p = _path()
    p.keys(value, {"format", "name", "owner_image", "abi", "bytes", "alignment", "fields", "runtime_identity", "evidence"})
    _need(value["format"] == p.LAYOUT and value["name"] == "DeclaredReceiver" and _same(value["abi"], p.ABI) and
          type(value["bytes"]) is int and value["bytes"] == 8 and type(value["alignment"]) is int and value["alignment"] == 8 and
          _same(value["fields"], [{"name": "vptr", "type": "pointer", "offset": 0}]),
          "experimental receiver exposes only its eight-byte primary vptr hypothesis")
    p.image(value["owner_image"])
    evidence(value["evidence"])
    identity = value["runtime_identity"]
    p.keys(identity, {"kind", "field", "image", "data_offset"})
    _need(identity["kind"] == "pinned_data_vptr" and identity["field"] == "vptr" and
          _same(identity["image"], value["owner_image"]), "exact correlated primary vptr hypothesis required")
    p.image(identity["image"])
    _need(type(identity["data_offset"]) is str and p.OFFSET.fullmatch(identity["data_offset"]) is not None and
          int(identity["data_offset"], 16) <= p.MAX_DATA_OFFSET - 8 and int(identity["data_offset"], 16) % 8 == 0,
          "bounded aligned original vptr address point required")
    return copy.deepcopy(value)


def validate_recipe(value, **_existing_validators):
    p = _path()
    p.keys(value, {"format", "root", "root_type", "result_type", "types", "path", "preconditions", "root_evidence", "lifetime", "consumer"})
    _need(is_recipe(value) and value["root_type"] == value["result_type"] == "DeclaredReceiver" and
          _same(value["path"], []) and _same(value["preconditions"], []) and
          type(value["types"]) is list and len(value["types"]) == 1,
          "one zero-edge experimental receiver hypothesis required")
    p.keys(value["root"], {"getter"})
    getter = value["root"]["getter"]
    validate_getter(getter)
    validate_layout(value["types"][0])
    p.keys(value["lifetime"], {"mode", "evidence"})
    _need(value["lifetime"]["mode"] == MODE, "separate explicitly unproven receiver lifetime mode required")
    consumer = value["consumer"]
    p.keys(consumer, {"call", "argument", "pointee_type", "role", "dispatch", "this_adjustment", "evidence"})
    _need(type(consumer["argument"]) is int and consumer["argument"] == 0 and
          consumer["pointee_type"] == "DeclaredReceiver" and consumer["role"] == "cxx_this" and
          consumer["dispatch"] == "direct" and type(consumer["this_adjustment"]) is int and consumer["this_adjustment"] == 0,
          "exact unadjusted direct arg0 receiver hypothesis required")
    call = consumer["call"]
    p.keys(call, {"module", "offset", "abi", "argument_types", "return_type", "context", "pointer_policy", "signature_evidence"})
    _entry({key: call[key] for key in ("module", "offset")})
    args = call["argument_types"]
    _need(call["abi"] == "default" and call["context"] == "none" and call["pointer_policy"] == "borrowed_no_escape" and
          type(args) is list and 1 <= len(args) <= 8 and args[0] == "pointer" and
          all(type(kind) is str and kind in p.BORROWED_SCALAR_TYPES for kind in args[1:]) and
          type(call["return_type"]) is str and call["return_type"] in p.BORROWED_SCALAR_TYPES | {"void"},
          "receiver consumer must retain its ordinary scalar-only proposed signature")
    _signature_claim(call["signature_evidence"], "/signatures/consumer")
    _need(not _same(getter["target"], {key: call[key] for key in ("module", "offset")}),
          "receiver getter and consumer must be distinct entries")
    owners = [(value["root_evidence"], 0), (value["lifetime"]["evidence"], 1),
              (value["types"][0]["evidence"], 2), (consumer["evidence"], 3)]
    root_claim = value["root_evidence"]["claim"]
    for wrapper, index in owners:
        evidence(wrapper)
        expected = {**root_claim, "pointer": f"/contracts/{index}"}
        _need(_same(wrapper["claim"], expected) and _same(wrapper["originals"], value["root_evidence"]["originals"]),
              "receiver contracts must share one exact immutable declaration and original closure")
    for claim, pointer in [(getter["signature_evidence"], "/signatures/getter"),
                           (call["signature_evidence"], "/signatures/consumer")]:
        _need(_same(claim, {**root_claim, "pointer": pointer}), "receiver ABI and path declarations must share their exact producer")
    file_claims(value)
    _bounded(value)
    return copy.deepcopy(value)


def layout_contract(layout):
    return {**{key: copy.deepcopy(value) for key, value in layout.items() if key != "evidence"}, "evidence_basis": dict(BASIS)}


def consumer_contract(consumer):
    call = consumer["call"]
    return {"format": CONSUMER_CONTRACT, "callable": {
        "entry": {key: copy.deepcopy(call[key]) for key in ("module", "offset")},
        **{key: copy.deepcopy(call[key]) for key in ("abi", "argument_types", "return_type", "context", "pointer_policy")}},
        **{key: copy.deepcopy(consumer[key]) for key in ("argument", "pointee_type", "role", "dispatch", "this_adjustment")},
        "evidence_basis": dict(BASIS)}


def root_contract(recipe):
    getter = {key: copy.deepcopy(value) for key, value in recipe["root"]["getter"].items() if key != "signature_evidence"}
    return {"format": ROOT_CONTRACT, "root": {"getter": getter}, "pointee_type": recipe["root_type"],
            "pointer_policy": "borrowed_no_escape", "evidence_basis": dict(BASIS)}


def lifetime_contract(recipe):
    return {"format": LIFETIME_CONTRACT, "root": root_contract(recipe),
        "types": [layout_contract(recipe["types"][0])], "path": [], "mode": MODE, "preconditions": [],
        "queue": "main", "scope": "single_immediate_use", "pointer_policy": "borrowed_no_escape",
        "intervening_target_calls": 0, "post_dispatch_access": False,
        "target_call_epoch": "root_return_equals_terminal_use", "this_adjustment": 0,
        "terminal": {"op": "native_invoke", "consumer": consumer_contract(recipe["consumer"])}, "evidence_basis": dict(BASIS)}


def contracts(recipe):
    return [root_contract(recipe), lifetime_contract(recipe), layout_contract(recipe["types"][0]), consumer_contract(recipe["consumer"])]


def evidence_plan(recipe):
    wrappers = [recipe["root_evidence"], recipe["lifetime"]["evidence"], recipe["types"][0]["evidence"], recipe["consumer"]["evidence"]]
    return [{**copy.deepcopy(wrapper), "contract": contract} for wrapper, contract in zip(wrappers, contracts(recipe))]


def image_claims(recipe):
    claims = [recipe["root"]["getter"]["target"]["module"], recipe["consumer"]["call"]["module"],
              recipe["types"][0]["owner_image"], recipe["types"][0]["runtime_identity"]["image"]]
    result = {}
    for claim in claims:
        _path().image(claim)
        _need(claim["path"] not in result or _same(result[claim["path"]], claim), "conflicting receiver image identities")
        result[claim["path"]] = copy.deepcopy(claim)
    return [result[key] for key in sorted(result)]


def file_claims(recipe):
    result = {claim["path"]: claim["sha256"] for claim in image_claims(recipe)}
    def add(file, sha):
        _need(file not in result or result[file] == sha, "conflicting receiver evidence identities")
        result[file] = sha
    for row in evidence_plan(recipe):
        add(row["claim"]["file"], row["claim"]["sha256"])
        for original in row["originals"]:
            add(original["file"], original["sha256"])
    return result


def require_consumer(recipe, actor, argument):
    _need(type(argument) is int and argument == 0 and actor.get("kind") == "native_function" and
          "context" not in actor and "compiler_adapter" not in actor, "receiver hypothesis needs its actual direct arg0 call")
    actual = {"entry": actor["target"], "abi": actor["abi"],
              "argument_types": [item["type"] for item in actor["arguments"]], "return_type": actor["return_type"],
              "context": "none", "pointer_policy": "borrowed_no_escape"}
    _need(_same(actual, consumer_contract(recipe["consumer"])["callable"]), "receiver hypothesis consumer differs from the actual call")


def validate_actor(actor, *, allow_bindings=False):
    p = _path()
    p.keys(actor, {"kind", "target", "abi", "arguments", "return_type", "signature_evidence"})
    _signature_claim(actor["signature_evidence"], "/signatures/consumer")
    args = actor["arguments"]
    _need(type(args) is list and 1 <= len(args) <= 8, "one direct receiver and bounded scalar tail required")
    p.keys(args[0], {"type", "value"})
    p.keys(args[0]["value"], {"native_path"})
    _need(args[0]["type"] == "pointer", "receiver must occupy pointer arg0")
    recipe = validate_recipe(args[0]["value"]["native_path"])
    require_consumer(recipe, actor, 0)
    _need(_same(actor["signature_evidence"], recipe["consumer"]["call"]["signature_evidence"]),
          "actual consumer and receiver signatures must select one declaration")
    for argument in args[1:]:
        _bridge()._native_argument(argument, allow_bindings=allow_bindings)
    _bounded(actor)
    return copy.deepcopy(actor)


def has_hypothesis(value):
    return is_recipe(value) or any(kind == "native_path" and is_recipe(recipe) for kind, recipe in _path().walk_recipes(value))


def evidence_summary(value):
    return dict(BASIS) if has_hypothesis(value) else None


def validate_observation(value, recipe, *, for_call=True):
    p = _path()
    validate_recipe(recipe)
    _need(for_call is True, "receiver hypotheses admit only an actual scalar consumer, never investigation reads")
    p.keys(value, {"recipe", "argument", "consumer", "root_kind", "owner_retained", "queue", "lifetime_mode", "resolved",
                   "sealed", "dispatch_permit_consumed", "hops", "expires", "borrowed_scope", "evidence_basis"})
    _need(_same(value["recipe"], recipe) and type(value["argument"]) is int and value["argument"] == 0 and
          _same(value["consumer"], consumer_contract(recipe["consumer"])) and value["root_kind"] == "getter" and
          value["owner_retained"] is False and value["queue"] == "main" and value["lifetime_mode"] == MODE and
          value["resolved"] is value["sealed"] is value["dispatch_permit_consumed"] is True and value["expires"] == "transaction" and
          _same(value["evidence_basis"], BASIS), "receiver receipt lost its declared basis or actual one-use binding")
    scope = value["borrowed_scope"]
    p.keys(scope, {"getter_epoch", "checked_epoch", "use", "consumed", "expires"})
    p.integer(scope["getter_epoch"], 1, 1)
    p.integer(scope["checked_epoch"], 1, 1)
    _need(scope["getter_epoch"] == scope["checked_epoch"] and scope["use"] == "dispatch" and
          scope["consumed"] is True and scope["expires"] == "use", "receiver receipt lost its immediate acquisition/use epoch")
    hops = value["hops"]
    _need(type(hops) is list and len(hops) == 1, "one zero-edge receiver observation required")
    hop = hops[0]
    p.keys(hop, {"type", "pointer", "guards", "edge", "identity_checked"})
    p._handle(hop["pointer"])
    _need(hop["type"] == "DeclaredReceiver" and hop["guards"] == [] and hop["edge"] is None and hop["identity_checked"] is True,
          "receiver observation must retain its sole correlated vptr check")
    return copy.deepcopy(value)


def _policy(state):
    _need(state.get("native_receiver_mode") == POLICY and state.get("native_contract_mode") == "declared" and
          state.get("workflow") == "guest_model_judged_state_v1", "caller must freeze the explicit experimental receiver and native ABI policies")


def _binding(state):
    R = _resource_module("rea_mapping")
    return R.digest({key: state[key] for key in BINDING_KEYS if key in state})


def _registered(reader, root, state, name):
    _need(type(name) is str and name in state.get("artifacts", {}), "cite registered current-session receiver evidence")
    file = root / name
    _need(not Path(name).is_absolute() and ".." not in Path(name).parts and file.is_relative_to(root) and
          state["artifacts"][name].get("file") == str(file), "receiver artifact escaped its producing session")
    match = re.fullmatch(r"s([0-9]{4})", Path(name).parts[0])
    _need(match is not None, "receiver evidence requires an actual producing session step")
    index = int(match.group(1))
    _need(index < len(state["steps"]) and state["steps"][index]["status"] == "completed", "receiver evidence producer is not completed")
    value, pin = reader.data(file, state["artifacts"][name])
    return value, pin, state["steps"][index]["request"]


def _observations(reader, root, state, request):
    A = _resource_module("rea_static_artifacts")
    bundle = Path(state["bundle"])
    _need(bundle.resolve(strict=True) == bundle, "canonical owned target bundle required")
    observations, images, opened = [], {}, {}
    try:
        for citation in request["citations"]:
            role = citation["role"]
            value, pin, producing = _registered(reader, root, state, citation["artifact"])
            prefix = Path(citation["artifact"]).parts[0]
            request_artifact = _registered(reader, root, state, prefix + "/request.json")[0]
            feedback_artifact = _registered(reader, root, state, prefix + "/feedback.json")[0]
            producer_index = int(prefix[1:])
            _need(_same(request_artifact, producing) and _same(feedback_artifact, state["steps"][producer_index]),
                  "receiver original observation dispatch provenance changed")
            kind = "original_range" if role == "vptr_data" else "original_code"
            expected_format = "anyuse.rea-original-file-range/1" if role == "vptr_data" else "anyuse.rea-original-instruction-range/1"
            source = producing.get("request", {})
            _need(producing.get("op") == "static_evidence" and source.get("kind") == kind and
                  value.get("format") == expected_format and value.get("original_unchanged") is True and
                  value.get("task_proof") is False and value.get("automatic_abi_or_lifetime_admission") is False and
                  value.get("byte_order") == "little", "receiver evidence lacks its actual fixed original-code/data producer")
            if role == "vptr_data":
                module, offset = request["vptr"]["image"], request["vptr"]["data_offset"]
            else:
                target = request["getter" if role == "getter_code" else "consumer"]["target"]
                module, offset = target["module"], target["offset"]
            path = Path(module["path"])
            _need(path.is_relative_to(bundle), "receiver image is outside the owned target bundle")
            _need(source.get("relative") == path.relative_to(bundle).as_posix() and source.get("offset") == offset,
                  "receiver evidence names a different producing entry/data range")
            A.validate_request(source)
            original = opened.get(str(path))
            if original is None:
                original = opened[str(path)] = A.OriginalImage(bundle, path.relative_to(bundle).as_posix())
            _need(_same(value.get("image"), original.pin) and original.pin["sha256"] == module["sha256"],
                  "receiver original image changed after its fixed observation")
            row = value.get("range")
            expected = A.original_range(original, source)
            _need(_same(value, expected) and row["image_offset"] == offset and row["bytes"] >= (8 if role == "vptr_data" else 4) and
                  (source["unit_bytes"] == 8 if role == "vptr_data" else source["unit_bytes"] == 4),
                  "receiver evidence differs from the exact current original bytes or supported unit width")
            observations.append({"role": role, "citation": copy.deepcopy(citation), "artifact_pin": pin,
                                 "image": copy.deepcopy(original.pin), "range": copy.deepcopy(row)})
            images[str(path)] = copy.deepcopy(original.pin)
        _need(sum(pin["bytes"] for pin in images.values()) <= _path().MAX_ORIGINAL_BYTES,
              "receiver original image closure exceeds its fixed byte budget")
        for original in opened.values():
            original.unchanged()
    finally:
        for original in opened.values():
            original.close()
    return observations, images


def _sources():
    R, E, A, N = (_resource_module(name) for name in
                  ("rea_mapping", "rea_actor_evidence", "rea_static_artifacts", "rea_native_contract"))
    modules = {"receiver": __import__(__name__, fromlist=["_"]), "path": _path(), "bridge": _bridge(),
               "mapping": R, "evidence": E, "static_artifacts": A, "native_declaration": N}
    return {name: {"file": str(Path(module.__file__).resolve()), "sha256": R._file_hash(Path(module.__file__).resolve()),
                   "bytes": Path(module.__file__).stat().st_size} for name, module in modules.items()}


def _runtime_modules():
    return _resource_module("rea_mapping"), _resource_module("rea_native_contract")


def _originals(observations):
    return [{"role": row["role"], "file": row["artifact_pin"]["file"], "sha256": row["artifact_pin"]["sha256"]} for row in observations]


def declare(session, state, request, folder):
    R, N = _runtime_modules()
    R.assert_guest()
    _policy(state)
    _need(state["status"] == "investigating", "receiver declaration closes before evaluation or proof")
    request = validate_request(request, state["params"])
    root, folder = Path(session), Path(folder)
    index = len(state["steps"]) - 1
    _need(root.resolve(strict=True) == root and folder.resolve(strict=True) == folder and
          folder == root / f"s{index:04d}" and 0 <= index < 64, "receiver declaration output must be its new producing step")
    _need(_same(state["steps"][index]["request"], request), "receiver declaration differs from its current producing request")
    reader = N.Reader()
    observations, images = _observations(reader, root, state, request)
    file = folder / RECEIPT
    temporary, _ = _decorate(request, file, "0" * 64, _originals(observations))
    validate_recipe(temporary)
    document = {"format": FORMAT, "type_basis": TYPE_BASIS, "evidence_basis": dict(BASIS),
        "independently_reviewed": False, "actuated": False, "proof_created": False, "model_calls": 0,
        "session": str(root), "session_binding": _binding(state), "step": index,
        "request_sha256": R.digest(request), "request": request, "observations": observations,
        "images": images, "signatures": {key: signature_contract(request[key]) for key in ("getter", "consumer")},
        "contracts": contracts(temporary), "sources": _sources()}
    _bounded(document)
    reader.recheck()
    _need(not file.exists(), "receiver declaration never overwrites an artifact")
    with file.open("x") as stream:
        json.dump(document, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    recipe, actor = _decorate(request, file, R._file_hash(file), _originals(observations))
    return {"status": "native_receiver_hypothesis_declared", "actor": actor, "recipe": recipe,
            "receipt": str(file), "type_basis": TYPE_BASIS, "evidence_basis": dict(BASIS),
            "abi_recovered": False, "type_proven": False, "lifetime_proven": False,
            "independently_reviewed": False, "proof_created": False, "actuated": False, "model_calls": 0}


def validate_resource(subject):
    """Reconstruct the exact producer and recipe before every permitted use.

    Subject is a complete receiver actor, its getter, or its receiver recipe.
    Returned metadata describes declarations; it grants no live receiver token.
    """
    R, N = _runtime_modules()
    R.assert_guest()
    if is_recipe(subject):
        validate_recipe(subject)
        claim = subject["root_evidence"]["claim"]
    else:
        _need(type(subject) is dict and "signature_evidence" in subject, "signed experimental receiver subject required")
        claim = subject["signature_evidence"]
        evidence_claim(claim)
        _need(claim["pointer"] in {"/signatures/getter", "/signatures/consumer"}, "experimental receiver subject has no signature role")
    reader = N.Reader()
    document, pin = reader.data(claim["file"])
    _need(pin["sha256"] == claim["sha256"] and pin["bytes"] <= MAX_BYTES and
          document.get("format") == FORMAT and document.get("type_basis") == TYPE_BASIS and
          _same(document.get("evidence_basis"), BASIS) and document.get("independently_reviewed") is False and
          document.get("actuated") is document.get("proof_created") is False and
          type(document.get("model_calls")) is int and document["model_calls"] == 0,
          "receiver declaration or its explicitly unproven evidence tier changed")
    root, index = Path(document["session"]), document["step"]
    _need(root.resolve(strict=True) == root and type(index) is int and 0 <= index < 64 and
          Path(pin["file"]) == root / f"s{index:04d}" / RECEIPT, "receiver declaration escaped its producing session step")
    state = reader.data(root / "session.json")[0]
    _policy(state)
    _need(_binding(state) == document["session_binding"], "receiver declaration frozen task or experimental policy changed")
    registered, registered_pin, producing = _registered(reader, root, state, f"s{index:04d}/{RECEIPT}")
    _need(_same(registered_pin, pin) and _same(registered, document) and producing.get("op") == "declare_native_receiver",
          "receiver declaration lacks its registered completed producing step")
    _need(_same(_registered(reader, root, state, f"s{index:04d}/request.json")[0], producing) and
          _same(_registered(reader, root, state, f"s{index:04d}/feedback.json")[0], state["steps"][index]),
          "receiver declaration dispatch provenance changed")
    request = validate_request(producing, state["params"])
    _need(_same(request, document["request"]) and R.digest(request) == document["request_sha256"],
          "receiver declaration differs from its actual producing request")
    observations, images = _observations(reader, root, state, request)
    recipe, actor = _decorate(request, pin["file"], pin["sha256"], _originals(observations))
    _need(_same(observations, document["observations"]) and _same(images, document["images"]) and
          _same(contracts(recipe), document["contracts"]) and
          _same(document["signatures"], {key: signature_contract(request[key]) for key in ("getter", "consumer")}),
          "receiver declaration contract or original observation closure changed")
    if is_recipe(subject):
        _need(_same(subject, recipe), "receiver recipe differs from its exact declaration")
    elif claim["pointer"] == "/signatures/getter":
        _need(_same(subject, recipe["root"]["getter"]), "receiver getter differs from its exact declaration")
    else:
        validate_actor(subject, allow_bindings=True)
        normalized = copy.deepcopy(subject)
        _need(len(normalized["arguments"]) == len(actor["arguments"]), "receiver argument count changed")
        for index0, declared in enumerate(actor["arguments"][1:], 1):
            if type(declared["value"]) is dict and set(declared["value"]) == {"input"}:
                current_value = normalized["arguments"][index0]["value"]
                _need(type(current_value) is not dict or _same(current_value, declared["value"]),
                      "receiver declaration input binding changed")
                normalized["arguments"][index0]["value"] = copy.deepcopy(declared["value"])
        _need(_same(normalized, actor), "receiver actor, ABI, constants or root recipe changed")
    sources = _sources()
    _need(_same(document.get("sources"), sources), "receiver declaration producer implementation changed")
    for source in sources.values():
        reader.read(source["file"], source)
    reader.recheck()
    result = {name: copy.deepcopy(value) for name, value in reader.files.items() if name != str(root / "session.json")}
    for name, image in images.items():
        _need(name not in result or _same(result[name], image), "receiver evidence/image pin collision")
        result[name] = image
    result[pin["file"]]["native_receiver_signatures"] = [
        {"pointer": f"/signatures/{key}", "signature": copy.deepcopy(document["signatures"][key])} for key in ("getter", "consumer")]
    return result


def resources(subject):
    return validate_resource(subject)


def validate_declaration(actor, *, state, current=True):
    """Bind a proposal to its own current-session registered declaration.

    Current means the current immutable code/task closure, never that a saved
    pointer is still alive. Process/context admission belongs to each runtime
    invocation; this declaration has not acquired any receiver.
    """
    _need(type(current) is bool, "receiver declaration current flag must be Boolean")
    _policy(state)
    validate_actor(actor, allow_bindings=True)
    resources0 = validate_resource(actor)
    R, N = _runtime_modules()
    reader = N.Reader()
    claim = actor["signature_evidence"]
    document, pin = reader.data(claim["file"])
    saved = reader.data(Path(document["session"]) / "session.json")[0]
    _need(_same(state, saved) and _binding(state) == document["session_binding"],
          "receiver proposal belongs to a different or changed current session")
    index = document["step"]
    request = validate_request(document["request"], state["params"])
    _, declared = _decorate(request, pin["file"], pin["sha256"], _originals(document["observations"]))
    _need(_same(actor, declared), "receiver proposal must retain its declared inputs and constants")
    name = f"s{index:04d}/{RECEIPT}"
    _need(_same(state.get("artifacts", {}).get(name), pin), "receiver proposal declaration is not registered in this session")
    if current:
        _need(R.fingerprint(state["bundle"]) == state["target"], "receiver proposal target bytes changed")
    reader.recheck()
    return {"document": document, "resources": resources0, "type_basis": TYPE_BASIS,
            "evidence_basis": dict(BASIS), "independently_reviewed": False, "proof_created": False}
