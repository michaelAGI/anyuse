"""Closed data contracts for borrowed native pointer paths.

No pointer resolution, native import, file read or execution occurs here. The
fixed runtime and its proof consumer must both be integrated before B admits a
path transaction. Source-admission records are reviewed evidence, not automatic
ABI/layout recovery or task proof.
"""
from __future__ import annotations

import copy
from pathlib import PurePosixPath
import re
from typing import Any, Callable

if __package__:
    from . import rea_native_lifecycle as NL
    from . import rea_qt_moc as Q
else:  # B also imports this pure module from its fixed-file --worker entry.
    import rea_native_lifecycle as NL
    import rea_qt_moc as Q


FORMAT = "anyuse.rea-native-pointer-path/1"
LAYOUT = "anyuse.rea-native-pointer-layout/2"
ROOT_CONTRACT = "anyuse.rea-native-pointer-root/1"
LIFETIME_CONTRACT = "anyuse.rea-native-pointer-lifetime/1"
CONSUMER_CONTRACT = "anyuse.rea-native-pointer-consumer/1"
ABI = {"architecture": "arm64", "cpu_subtype": 0,
       "pointer_bytes": 8, "byte_order": "little"}
MAX_DEPTH, MAX_TYPES, MAX_BYTES, MAX_TOTAL_BYTES, MAX_GUARDS = 6, 7, 16384, 65536, 24
ADMISSION = "anyuse.rea-native-pointer-source-admission/1"
MAX_VISITED_BYTES = 65536  # Counts every hop, including repeated declared types.
MAX_ORIGINAL_FILES, MAX_ORIGINAL_BYTES = 64, 128 * 1024**2
SHA = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:$<> -]{0,254}\Z")
OFFSET = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})\Z")
MAX_DATA_OFFSET = 2**53 - 1
WIDTHS = {"bool": 1, "int8": 1, "uint8": 1, "int16": 2, "uint16": 2,
          "int32": 4, "uint32": 4, "int64": 8, "uint64": 8,
          "long": 8, "ulong": 8, "float": 4, "double": 8, "pointer": 8}
INTEGER_TYPES = set(WIDTHS) - {"pointer", "float", "double"}
BORROWED_ROOT_MODE = "main_queue_borrowed_root"
BORROWED_SCALAR_TYPES = frozenset(WIDTHS) - {"pointer"}


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def same_data(left: Any, right: Any) -> bool:
    """Exact JSON data equality: in particular, false is not integer zero."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(same_data(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(same_data(a, b) for a, b in zip(left, right))
    return left == right


def keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    require(type(value) is dict and required <= set(value) and
            not set(value) - required - (optional or set()), "closed native pointer-path schema required")


def name(value: Any) -> None:
    require(type(value) is str and NAME.fullmatch(value) is not None,
            "bounded literal type/field name required")


def integer(value: Any, low: int, high: int) -> None:
    require(type(value) is int and low <= value <= high, "bounded exact integer required")


def image(value: Any) -> None:
    keys(value, {"path", "sha256"})
    path = value["path"]
    require(type(path) is str and len(path.encode("utf-8")) <= 4096 and "\0" not in path and
            path.startswith("/") and not path.startswith("//") and
            str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts,
            "canonical lexical image path required; VM must additionally reject aliases")
    require(type(value["sha256"]) is str and SHA.fullmatch(value["sha256"]) is not None,
            "exact image file SHA required")


def evidence_claim(value: Any) -> None:
    keys(value, {"kind", "file", "sha256", "pointer"})
    require(type(value["kind"]) is str and value["kind"] in {"compiler", "dwarf", "header", "reviewed_contract"},
            "attributable original evidence kind required")
    image({"path": value["file"], "sha256": value["sha256"]})
    pointer = value["pointer"]
    require(type(pointer) is str and len(pointer.encode("utf-8")) <= 2048 and "\0" not in pointer and
            (not pointer or pointer.startswith("/")) and not re.search(r"~(?![01])", pointer),
            "exact bounded JSON pointer required")


def evidence(value: Any) -> None:
    keys(value, {"claim", "originals"})
    evidence_claim(value["claim"])
    originals = value["originals"]
    require(type(originals) is list and 1 <= len(originals) <= 16,
            "explicit independently attributed source closure required")
    seen = set()
    for original in originals:
        keys(original, {"role", "file", "sha256"})
        require(type(original["role"]) is str and original["role"] in {"header", "dwarf", "source", "original_ir", "original_build",
                                     "symbol_correlation", "independent_review"},
                "closed original evidence artifact role required")
        image({"path": original["file"], "sha256": original["sha256"]})
        require(original["file"] not in seen and original["file"] != value["claim"]["file"],
                "source artifacts must be distinct from the selected contract, not a self-echo")
        seen.add(original["file"])
    require(any(original["role"] in {"header", "dwarf", "source", "original_ir", "independent_review"}
                for original in originals), "a build label or symbol name alone does not establish the contract")


def literal_scalar(kind: str, value: Any) -> None:
    require(kind in INTEGER_TYPES, "guards support Bool/integer fields only")
    if kind == "bool":
        require(type(value) is bool, "Bool guard must be exactly true/false")
        return
    signed = kind.startswith("int") or kind == "long"
    bits = WIDTHS[kind] * 8
    if bits == 64:
        require(type(value) is str and re.fullmatch(r"-?(0|[1-9][0-9]{0,19})", value) is not None and
                value != "-0", "64-bit guard must use canonical decimal text")
        number = int(value)
    else:
        require(type(value) is int, "small integer guard must be an exact JSON integer")
        number = value
    require(-(2 ** (bits - 1)) <= number < 2 ** (bits - 1) if signed else 0 <= number < 2 ** bits,
            "guard value exceeds its declared width")


def layout_contract(layout: dict) -> dict:
    return {key: copy.deepcopy(value) for key, value in layout.items() if key != "evidence"}


def validate_layout(value: Any) -> None:
    keys(value, {"format", "name", "owner_image", "abi", "bytes", "alignment", "fields", "evidence"},
         {"runtime_identity"})
    keys(value["abi"], set(ABI))
    require(value["format"] == LAYOUT and all(type(value["abi"][key]) is type(expected) and
            value["abi"][key] == expected for key, expected in ABI.items()),
            "explicit typed plain-arm64 layout-v2 required")
    name(value["name"])
    image(value["owner_image"])
    evidence(value["evidence"])
    integer(value["bytes"], 1, MAX_BYTES)
    require(type(value["alignment"]) is int and value["alignment"] in {1, 2, 4, 8, 16} and
            value["bytes"] % value["alignment"] == 0, "exact natural declared extent/alignment required")
    fields = value["fields"]
    require(type(fields) is list and 1 <= len(fields) <= 32, "1..32 declared fields required")
    occupied, names = [], set()
    for field in fields:
        keys(field, {"name", "type", "offset"}, {"target"})
        name(field["name"])
        require(field["name"] not in names, "duplicate declared field")
        names.add(field["name"])
        require(type(field["type"]) is str and field["type"] in WIDTHS, "unsupported native field type")
        width = WIDTHS[field["type"]]
        integer(field["offset"], 0, value["bytes"] - width)
        require(field["offset"] % min(width, 8) == 0, "packed/unaligned fields are outside native pointer-path v1")
        interval = (field["offset"], field["offset"] + width)
        require(all(interval[1] <= start or stop <= interval[0] for start, stop in occupied),
                "overlapping/union fields are outside this draft")
        occupied.append(interval)
        if "target" in field:
            require(field["type"] == "pointer", "only a native pointer may have a declared target type")
            name(field["target"])
    require(value["alignment"] >= max(min(WIDTHS[field["type"]], 8) for field in fields),
            "declared object base must satisfy every natural field alignment")
    if "runtime_identity" in value:
        identity = value["runtime_identity"]
        keys(identity, {"kind", "field", "image", "data_offset"})
        require(identity["kind"] == "pinned_data_vptr" and type(identity["field"]) is str and
                any(field["name"] == identity["field"] and field["type"] == "pointer" and
                    "target" not in field for field in fields), "vptr witness needs one non-traversed pointer field")
        image(identity["image"])
        require(type(identity["data_offset"]) is str and OFFSET.fullmatch(identity["data_offset"]) is not None,
                "vptr witness must pin a data address relative to its exact loaded image")
        require(int(identity["data_offset"], 16) % ABI["pointer_bytes"] == 0,
                "native vptr witness data offset must have natural pointer alignment")


def root_contract(recipe: dict) -> dict:
    root = copy.deepcopy(recipe["root"])
    if set(root) == {"lifecycle"}:
        # Evidence is pinned as its own plan row. Embedding its file hash into
        # a declaration stored in that file would create a self-hash cycle.
        root["lifecycle"].pop("evidence")
    return {"format": ROOT_CONTRACT, "root": root,
            "pointee_type": recipe["root_type"], "pointer_policy": "borrowed_no_escape"}


def lifetime_contract(recipe: dict) -> dict:
    contract = {"format": LIFETIME_CONTRACT, "root": root_contract(recipe),
            "types": [layout_contract(item) for item in recipe["types"]],
            "path": list(recipe["path"]), "mode": recipe["lifetime"]["mode"],
            "preconditions": copy.deepcopy(recipe["preconditions"]),
            "queue": "main", "scope": "single_dispatch", "pointer_policy": "borrowed_no_escape",
            "concurrent_invalidation": "independently_excluded", "native_child_retained": False}
    if is_borrowed_root(recipe):
        # These are exact independently admitted source premises, not runtime
        # deductions from a readable vptr or a main-queue callback.
        contract.update(scope="single_immediate_use", root_acquisition="zero_arg_callback_free_getter",
            root_type_admission="primary_vptr_at_zero", intervening_target_calls=0,
            post_dispatch_access=False, target_call_epoch="root_return_equals_terminal_use",
            live_initialized_primary_base="independently_established", this_adjustment=0,
            owner_thread="main_queue_independently_established",
            construction_destruction="excluded_for_entire_scope", reentrant_event_loop="excluded_for_entire_scope",
            terminal=borrowed_terminal_contract(recipe))
    if is_lifecycle_root(recipe):
        contract.update(scope="same_rpc_single_immediate_use", root_acquisition="constructor_observed_private_slot",
            root_type_admission="primary_vptr_at_zero", observer=NL.observer_contract(recipe["root"]["lifecycle"],
                recipe["root_type"], recipe["types"][0]), intervening_target_calls=0, post_dispatch_access=False,
            target_call_epoch="slot_acquisition_equals_terminal_use",
            live_initialized_primary_base="witnessed_normal_constructor_return", this_adjustment=0,
            owner_thread="observed_constructor_and_use_on_macos_main",
            construction_destruction="excluded_until_terminal_use", reentrant_event_loop="excluded_for_entire_scope",
            terminal=borrowed_terminal_contract(recipe))
    return contract


def is_borrowed_root(recipe: dict) -> bool:
    return recipe["lifetime"]["mode"] == BORROWED_ROOT_MODE


def is_lifecycle_root(recipe: dict) -> bool:
    return recipe["lifetime"]["mode"] == NL.MODE


def is_immediate_root(recipe: dict) -> bool:
    return is_borrowed_root(recipe) or is_lifecycle_root(recipe)


def borrowed_terminal_contract(recipe: dict) -> dict:
    if "consumer" in recipe:
        return {"op": "qt_moc_invoke" if Q.is_qt_consumer(recipe["consumer"]) else "native_invoke",
                "consumer": consumer_contract(recipe["consumer"])}
    layout = recipe["types"][0]
    return {"op": "native_read", "bytes": layout["bytes"],
            "fields": [{key: copy.deepcopy(field[key]) for key in ("name", "type", "offset")}
                       for field in layout["fields"] if field["type"] in BORROWED_SCALAR_TYPES]}


def consumer_contract(consumer: dict) -> dict:
    if Q.is_qt_consumer(consumer):
        return Q.consumer_contract(consumer)
    call = consumer["call"]
    return {"format": CONSUMER_CONTRACT,
            "callable": {"entry": {key: copy.deepcopy(call[key]) for key in ("module", "symbol", "offset") if key in call},
                         **{key: copy.deepcopy(call[key]) for key in
                            ("abi", "argument_types", "return_type", "context", "pointer_policy")}},
            **{key: copy.deepcopy(consumer[key]) for key in
               ("argument", "pointee_type", "role", "dispatch", "this_adjustment")}}


def validate_data_root(value: Any) -> None:
    """A static image-relative location, admitted only within a typed path."""
    keys(value, {"module", "offset", "mode"})
    image(value["module"])
    require(type(value["offset"]) is str and OFFSET.fullmatch(value["offset"]) is not None and
            int(value["offset"], 16) <= MAX_DATA_OFFSET,
            "native data root needs one canonical JavaScript-safe image offset")
    require(type(value["mode"]) is str and value["mode"] in {"object", "pointer"},
            "native data root must select object storage or a pointer slot")


def validate_recipe(value: Any, *, validate_existing_root: Callable[[dict], None],
                    validate_existing_callable: Callable[[dict], None]) -> dict:
    """Pure validation; callbacks must be B's trusted existing validators."""
    keys(value, {"format", "root", "root_type", "result_type", "types", "path", "preconditions",
                 "root_evidence", "lifetime"}, {"consumer"})
    require(value["format"] == FORMAT, "explicit native pointer-path format required")
    name(value["root_type"])
    name(value["result_type"])
    root = value["root"]
    require(type(root) is dict and len(root) == 1 and
            next(iter(root)) in {"objc", "read", "swift_read", "getter", "data", "lifecycle"}, "typed borrowed root recipe required")
    if "data" in root:
        validate_data_root(root["data"])
    elif "lifecycle" in root:
        NL.validate_observer(root["lifecycle"])
    else:
        validate_existing_root(root)
    if "getter" in root:
        call = root["getter"]
        require(call["arguments"] == [] and call["abi"] == "default" and "context" not in call and
                "compiler_adapter" not in call,
                "first draft allows only zero-argument ordinary borrowed-root getters")
    if "objc" in root:
        require(root["objc"]["kind"] == "unique", "pointer root must be one exact live instance")
    evidence(value["root_evidence"])
    lifetime = value["lifetime"]
    keys(lifetime, {"mode", "evidence"})
    require(type(lifetime["mode"]) is str and lifetime["mode"] in
            {"process_lifetime", "retained_owner_main_queue_stable", BORROWED_ROOT_MODE, NL.MODE},
            "explicit independently established lifetime mode required")
    evidence(lifetime["evidence"])
    borrowed = is_borrowed_root(value)
    observed = is_lifecycle_root(value)
    immediate = borrowed or observed
    require(("lifecycle" in root) is observed, "lifecycle root and observed lifetime must occur together")
    types, steps = value["types"], value["path"]
    require(type(types) is list and 1 <= len(types) <= MAX_TYPES, "bounded declared path layouts required")
    require(type(steps) is list and (len(steps) == 0 if immediate else 1 <= len(steps) <= MAX_DEPTH),
            "borrowed root needs zero edges; existing paths must have 1..6 named pointer edges")
    layouts = {}
    for item in types:
        validate_layout(item)
        require(item["name"] not in layouts, "duplicate path layout")
        layouts[item["name"]] = item
    require(sum(item["bytes"] for item in types) <= MAX_TOTAL_BYTES, "total declared extent budget exceeded")
    current, reached, hop_types = value["root_type"], set(), []
    for index in range(len(steps) + 1):
        require(current in layouts, "path references a missing declared type")
        reached.add(current)
        hop_types.append(current)
        if index == len(steps):
            break
        name(steps[index])
        matches = [field for field in layouts[current]["fields"] if field["name"] == steps[index]]
        require(len(matches) == 1 and matches[0]["type"] == "pointer" and "target" in matches[0],
                "every hop must select one declared pointer-to-type field")
        current = matches[0]["target"]
    require(current == value["result_type"] and reached == set(layouts),
            "result type must follow the exact path; unused layout declarations are refused")
    if immediate:
        require(set(root) == ({"lifecycle"} if observed else {"getter"}) and
                len(types) == 1 and value["root_type"] == value["result_type"],
                "main-queue borrowed scope requires one zero-edge native getter layout")
        layout = layouts[current]
        identity = layout.get("runtime_identity")
        require(identity is not None and any(field["name"] == identity["field"] and field["offset"] == 0
                for field in layout["fields"]), "borrowed root requires an admitted primary vptr at offset zero")
        require(all("target" not in field and (field["type"] in BORROWED_SCALAR_TYPES or
                    field["name"] == identity["field"]) for field in layout["fields"]),
                "borrowed root layout permits only scalar fields and its identity pointer leaf")
    if "data" in root:
        data = root["data"]
        alignment = layouts[value["root_type"]]["alignment"] if data["mode"] == "object" else ABI["pointer_bytes"]
        require(int(data["offset"], 16) % alignment == 0,
                "native data root location must satisfy its object or pointer-slot alignment")
    require(sum(layouts[type_name]["bytes"] for type_name in hop_types) <= MAX_VISITED_BYTES,
            "visited-instance extent budget exceeded, including repeated types")
    guards, seen = value["preconditions"], set()
    require(type(guards) is list and len(guards) <= MAX_GUARDS, "bounded literal guards required")
    for guard in guards:
        keys(guard, {"hop", "field", "equals"})
        integer(guard["hop"], 0, len(steps))
        name(guard["field"])
        key = guard["hop"], guard["field"]
        require(key not in seen, "duplicate pointer-path guard")
        seen.add(key)
        field = next((field for field in layouts[hop_types[guard["hop"]]]["fields"]
                      if field["name"] == guard["field"]), None)
        require(field is not None, "guard field missing from declared hop layout")
        literal_scalar(field["type"], guard["equals"])
    if "data" in root:
        require(lifetime["mode"] == "process_lifetime", "static native data roots require process lifetime evidence")
    if lifetime["mode"] == "retained_owner_main_queue_stable":
        require("getter" not in root, "retained-owner lifetime needs an actual retained ObjC root owner")
    if "consumer" in value:
        consumer = value["consumer"]
        if Q.is_qt_consumer(consumer):
            Q.validate_consumer(consumer, root_type=current)
            evidence(consumer["evidence"])
            require(observed and value["path"] == [] and value["preconditions"] == [] and len(types) == 1 and
                    len(types[0]["fields"]) == 1 and types[0]["fields"][0]["type"] == "pointer" and
                    types[0]["fields"][0]["offset"] == 0 and
                    types[0]["fields"][0]["name"] == types[0]["runtime_identity"]["field"],
                    "Qt consumer requires one constructor-observed primary-vptr-only root with no guards/edges")
        else:
            keys(consumer, {"call", "argument", "pointee_type", "role", "dispatch", "this_adjustment", "evidence"})
            validate_existing_callable(consumer["call"])
            call = consumer["call"]
            require(call["abi"] == "default" and call["context"] == "none" and
                    call["pointer_policy"] == "borrowed_no_escape", "ordinary direct pointer call required")
            def ordinary(kind):
                return (type(kind) is str or type(kind) is dict and
                        kind.get("kind") in {"struct", "buffer"} and
                        (kind["kind"] == "buffer" or all(ordinary(field["type"]) for field in kind["fields"])))
            require(all(ordinary(kind) for kind in [*call["argument_types"], call["return_type"]]),
                    "path consumer cannot introduce owned or Swift metadata ABI types")
            integer(consumer["argument"], 0, len(call["argument_types"]) - 1)
            require(call["argument_types"][consumer["argument"]] == "pointer" and
                    consumer["pointee_type"] == current and type(consumer["role"]) is str and
                    consumer["role"] in {"borrowed_pointer", "cxx_this"} and
                    consumer["dispatch"] == "direct" and type(consumer["this_adjustment"]) is int and
                    consumer["this_adjustment"] == 0 and
                    (consumer["role"] != "cxx_this" or consumer["argument"] == 0),
                    "consumer must be the exact unadjusted borrowed pointer slot/type")
            evidence(consumer["evidence"])
            if immediate:
                require(consumer["role"] == "cxx_this" and consumer["argument"] == 0 and
                        call["argument_types"][0] == "pointer" and
                        all(type(kind) is str and kind in BORROWED_SCALAR_TYPES for kind in call["argument_types"][1:]) and
                        type(call["return_type"]) is str and call["return_type"] in BORROWED_SCALAR_TYPES | {"void"},
                        "borrowed root consumer requires arg0 this, scalar-only tail and scalar/void result")
    # Establish one nonconflicting pure closure before a VM resource collector
    # can perform any IO. Existing root declarations retain their own schemas.
    file_claims(value)
    return copy.deepcopy(value)


def evidence_plan(recipe: dict) -> list[dict]:
    """Exact contracts to pin BEFORE root code runs, not evidence generation."""
    rows = [{**recipe["root_evidence"], "contract": root_contract(recipe)},
            {**recipe["lifetime"]["evidence"], "contract": lifetime_contract(recipe)}]
    rows += [{**item["evidence"], "contract": layout_contract(item)} for item in recipe["types"]]
    if is_lifecycle_root(recipe):
        observer = recipe["root"]["lifecycle"]
        rows.append({**observer["evidence"], "contract": NL.observer_contract(observer,
            recipe["root_type"], recipe["types"][0])})
    if "consumer" in recipe:
        rows.append({**recipe["consumer"]["evidence"], "contract": consumer_contract(recipe["consumer"])})
    originals = {}
    for row in rows:
        for original in row["originals"]:
            prior = originals.setdefault(original["file"], original["sha256"])
            require(prior == original["sha256"], "conflicting original identities in one path closure")
    require(len(originals) <= MAX_ORIGINAL_FILES, "independent original-file closure exceeds fixed budget")
    return copy.deepcopy(rows)


def require_consumer(recipe: dict, actor: dict, argument: int) -> None:
    """Call AFTER existing B actor validation; never reinterprets an ABI."""
    if "consumer" in recipe and Q.is_qt_consumer(recipe["consumer"]):
        require(actor["kind"] == "qt_moc_method", "a Qt consumer cannot enter an ordinary native_function")
        Q.require_consumer(recipe, actor, argument)
        return
    require("consumer" in recipe and actor["kind"] == "native_function" and
            "compiler_adapter" not in actor and "context" not in actor,
            "first draft requires a direct existing C/POD actor, not helper/Swift context")
    selected = recipe["consumer"]
    actual = {"entry": actor["target"], "abi": actor["abi"],
              "argument_types": [item["type"] for item in actor["arguments"]],
              "return_type": actor["return_type"], "context": "none", "pointer_policy": "borrowed_no_escape"}
    require(type(argument) is int and selected["argument"] == argument and
            same_data(consumer_contract(selected)["callable"], actual),
            "native path must match the actual exact callable/argument slot")


def walk_recipes(value: Any):
    """One pure semantic walk, shared by all transitive resource/recipe users.

    Inputs have already passed B's closed actor/request validators. Declaration
    metadata and evidence are not executable recipes. A path's consumer declares
    the enclosing call; it is not a second invocation or callable resolution.
    """
    if isinstance(value, list):
        for child in value:
            yield from walk_recipes(child)
        return
    if not isinstance(value, dict):
        return
    if set(value) == {"operations"}:
        yield from walk_recipes(value["operations"])
        return
    if value.get("kind") in {"objc_sequence", "runtime_sequence"}:
        yield from walk_recipes(value["steps"])
        return
    if value.get("op") in {"graph", "native_read"}:
        yield from walk_recipes(value["root"] if value["op"] == "graph" else value["receiver"])
        return
    if set(value) == {"native_path"}:
        recipe = value["native_path"]
        yield "native_path", recipe
        yield from walk_recipes(recipe["root"])
        return
    if value.get("kind") == "qt_moc_method" or value.get("op") == "qt_moc_invoke":
        yield "qt_moc", value["selection"]
        yield from walk_recipes(value["receiver"])
        return
    if value.get("kind") == "native_function" or value.get("op") == "native_invoke":
        call = value if "kind" in value else {
            "kind": "native_function", **{key: child for key, child in value.items() if key not in {"op", "alias"}}}
        yield "native_function", call
        if "context" in call:
            yield from _walk_argument(call["context"])
        for argument in call["arguments"]:
            yield from _walk_argument(argument)
        return
    for tag in ("read", "swift_read", "callable", "getter", "objc", "native", "data", "lifecycle"):
        if set(value) == {tag}:
            if tag in {"getter", "native"}:
                yield from walk_recipes(value[tag])
            else:
                yield tag, value[tag]
            return


def _walk_argument(argument: dict):
    kind, value = argument["type"], argument["value"]
    if type(kind) is dict and kind.get("kind") == "struct":
        for field in kind["fields"]:
            yield from _walk_argument({"type": field["type"], "value": value[field["name"]]})
    elif kind == "pointer" or type(kind) is dict and kind.get("kind") == "swift_type":
        yield from walk_recipes(value)


def image_claims(recipe: dict) -> list[dict]:
    """Whole original image closure, including roots and declaration-only consumer."""
    claims = {}
    def add(claim):
        image(claim)
        prior = claims.setdefault(claim["path"], copy.deepcopy(claim))
        require(prior == claim, "conflicting native path image identity")
    for layout in recipe["types"]:
        add(layout["owner_image"])
        if "runtime_identity" in layout:
            add(layout["runtime_identity"]["image"])
    for tag, value in walk_recipes(recipe["root"]):
        if tag == "native_function":
            add(value["target"]["module"])
        elif tag in {"read", "swift_read", "callable", "data"}:
            add(value["module"])
        elif tag == "lifecycle":
            for claim in NL.image_claims(value):
                add(claim)
    if "consumer" in recipe:
        if Q.is_qt_consumer(recipe["consumer"]):
            for claim in Q.image_claims(recipe["consumer"]):
                add(claim)
        else:
            add(recipe["consumer"]["call"]["module"])
    return [claims[key] for key in sorted(claims)]


def file_claims(recipe: dict) -> dict[str, str]:
    """Complete path image/evidence/original closure, including existing roots."""
    claims = {image["path"]: image["sha256"] for image in image_claims(recipe)}
    def add(path, sha):
        require(path not in claims or claims[path] == sha, "conflicting native path file identity")
        claims[path] = sha
    for row in evidence_plan(recipe):
        claim = row["claim"]
        add(claim["file"], claim["sha256"])
        for original in row["originals"]:
            add(original["file"], original["sha256"])
    for tag, value in walk_recipes(recipe["root"]):
        if tag in {"native_function", "read", "swift_read", "callable"}:
            declaration = value["signature_evidence"] if tag in {"native_function", "callable"} else value["evidence"]
            evidence_claim(declaration)
            add(declaration["file"], declaration["sha256"])
        elif tag == "lifecycle":
            for path, sha in NL.file_claims(value).items():
                add(path, sha)
    if "consumer" in recipe:
        if Q.is_qt_consumer(recipe["consumer"]):
            for path, sha in Q.file_claims(recipe["consumer"]).items():
                add(path, sha)
        else:
            call = recipe["consumer"]["call"]
            evidence_claim(call["signature_evidence"])
            add(call["signature_evidence"]["file"], call["signature_evidence"]["sha256"])
    return claims


def admission_declaration(row: dict) -> dict:
    """The selected claim and separate source review cannot cite themselves."""
    return {"contract": copy.deepcopy(row["contract"]),
            "originals": [copy.deepcopy(original) for original in row["originals"]
                          if original["role"] != "independent_review"]}


def validate_admission(value: Any, recipe: dict, row: dict) -> None:
    """Check review correlation, never infer C++/Swift types from file hashes.

    The reviewed attestation is independently prepared from original source,
    build/symbol/DWARF evidence. This check only verifies its exact declared
    correlation to the pinned ABI/images/typed contract and those artifacts.
    """
    keys(value, {"format", "abi", "images", "declarations"})
    require(value["format"] == ADMISSION and same_data(value["abi"], ABI) and
            same_data(value["images"], image_claims(recipe)), "native path source review ABI/image correlation differs")
    declarations = value["declarations"]
    require(type(declarations) is list and 1 <= len(declarations) <= 32,
            "bounded independent source review declarations required")
    for declaration in declarations:
        keys(declaration, {"contract", "originals"})
        require(type(declaration["contract"]) is dict and type(declaration["originals"]) is list,
                "source review must retain its contract and original artifacts")
    wanted = admission_declaration(row)
    require(sum(same_data(declaration, wanted) for declaration in declarations) == 1,
            "native path source review lacks the exact typed-edge/lifetime/consumer correlation")
    roles = {original["role"] for original in wanted["originals"]}
    require(bool(roles & {"header", "source", "dwarf", "original_ir"}) and
            bool(roles & {"symbol_correlation", "original_build"}),
            "source review needs original type/source and binary/build correlation, not a self-echo")


def _handle(value: Any) -> str:
    keys(value, {"handle", "alias", "ownership", "expires"})
    require(type(value["handle"]) is str and re.fullmatch(r"n[1-9][0-9]{0,8}", value["handle"]) is not None and
            value["alias"] is None and value["ownership"] == "borrowed_no_escape" and
            value["expires"] == "transaction", "native path handle is null, persistent, raw or malformed")
    return value["handle"]


def path_types(recipe: dict) -> list[dict]:
    layouts = {layout["name"]: layout for layout in recipe["types"]}
    reached, current = [], recipe["root_type"]
    for index in range(len(recipe["path"]) + 1):
        layout = layouts[current]
        reached.append(layout)
        if index < len(recipe["path"]):
            field = next(field for field in layout["fields"] if field["name"] == recipe["path"][index])
            current = field["target"]
    return reached


def validate_observations(observations: Any, recipes: list[dict], *, for_call: bool = True) -> list[dict]:
    """Pure receipt binding after B validated each actual enclosing call/slot.

    Read/graph observations never carry a consumer or sealed dispatch permit.
    Handles remain transaction-only evidence; a receipt does not establish the
    original source/lifetime premises or independently prove a task result.
    """
    require(type(for_call) is bool, "native path receipt mode must be an exact Boolean")
    require(type(observations) is list and len(observations) == len(recipes),
            "native path receipt count/order differs from frozen recipes")
    for observation, recipe in zip(observations, recipes):
        borrowed = is_borrowed_root(recipe)
        observed = is_lifecycle_root(recipe)
        keys(observation, {"recipe", "argument", "consumer", "root_kind", "owner_retained", "queue",
                           "lifetime_mode", "resolved", "sealed", "dispatch_permit_consumed", "hops", "expires"} |
                          ({"borrowed_scope"} if borrowed else set()) |
                          ({"lifecycle_scope"} if observed else set()))
        require(same_data(observation["recipe"], recipe) and observation["root_kind"] == next(iter(recipe["root"])) and
                type(observation["owner_retained"]) is bool and
                observation["owner_retained"] is (next(iter(recipe["root"])) in {"objc", "read", "swift_read"}) and
                observation["queue"] == "main" and observation["lifetime_mode"] == recipe["lifetime"]["mode"] and
                observation["resolved"] is True and observation["sealed"] is for_call and
                observation["dispatch_permit_consumed"] is for_call and observation["expires"] == "transaction",
                "native path receipt lost source/root/lifetime/seal/expiry binding")
        if borrowed:
            scope = observation["borrowed_scope"]
            keys(scope, {"getter_epoch", "checked_epoch", "use", "consumed", "expires"})
            integer(scope["getter_epoch"], 1, 4096)
            integer(scope["checked_epoch"], 1, 4096)
            require(scope["getter_epoch"] == scope["checked_epoch"] and
                    scope["use"] == ("dispatch" if for_call else "read") and
                    scope["consumed"] is True and scope["expires"] == "use" and
                    ("consumer" in recipe) is for_call,
                    "borrowed root receipt lost its exact single-use getter/terminal epoch")
        if observed:
            NL.validate_scope(observation["lifecycle_scope"], recipe["root"]["lifecycle"], for_call=for_call)
            require(("consumer" in recipe) is for_call,
                    "lifecycle root receipt differs from its terminal read/consumer admission")
        if for_call:
            require("consumer" in recipe and type(observation["argument"]) is int and
                    observation["argument"] == recipe["consumer"]["argument"] and
                    same_data(observation["consumer"], consumer_contract(recipe["consumer"])),
                    "native path receipt consumer/call/slot differs")
        else:
            require(observation["argument"] is None and observation["consumer"] is None,
                    "investigation-only native path cannot mint dispatch evidence")
        layouts, hops = path_types(recipe), observation["hops"]
        require(type(hops) is list and len(hops) == len(layouts),
                "native path receipt omitted a hop or final result type")
        handles = set()
        for index, (hop, layout) in enumerate(zip(hops, layouts)):
            keys(hop, {"type", "pointer", "guards", "edge", "identity_checked"})
            handle = _handle(hop["pointer"])
            require(handle not in handles and hop["type"] == layout["name"] and
                    hop["identity_checked"] is ("runtime_identity" in layout),
                    "native path cycle/type/identity evidence differs")
            handles.add(handle)
            guards = []
            for guard in recipe["preconditions"]:
                if guard["hop"] == index:
                    field = next(field for field in layout["fields"] if field["name"] == guard["field"])
                    guards.append({"field": field["name"], "type": field["type"],
                                   "equals": copy.deepcopy(guard["equals"]), "matched": True})
            require(same_data(hop["guards"], guards),
                    "native path guard verdicts differ")
            if index == len(recipe["path"]):
                require(hop["edge"] is None, "final native path hop cannot traverse another edge")
            else:
                field = next(field for field in layout["fields"] if field["name"] == recipe["path"][index])
                wanted = {"field": field["name"], "target_type": field["target"], "unchanged": for_call}
                require(same_data(hop["edge"], wanted),
                        "native path edge/seal evidence differs")
    return copy.deepcopy(observations)

