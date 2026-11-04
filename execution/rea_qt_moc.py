"""Closed Qt revision13 zero-argument/void static-metacall contracts.

Pure data validation only: no B/P/NL imports, file reads, native imports or target
calls. Original bytes and independent receiver/ABI admissions remain separate
requirements. Ghidra prototypes and declaration flags are never ABI authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath
import re


SELECTION = "anyuse.rea-qt-moc-selection/1"
CONSUMER = "anyuse.rea-qt-moc-consumer/1"
RUNTIME_PIN = "anyuse.rea-qt-moc-runtime-pin/1"
OBSERVATION = "anyuse.rea-qt-moc-observation/1"
ACTOR = "qt_moc_method"
OPERATION = "qt_moc_invoke"
QT_COMMIT = "59c81a3c2247b821b9b84b4eb8d939b77e07e276"
QT_VERSION = "6.11.1"
MAX_OFFSET, MAX_RANGES, MAX_RANGE_BYTES, MAX_TOTAL_BYTES = 2**53 - 1, 256, 16384, 128 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OFFSET = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:$<> -]{0,254}\Z")
_SOURCES = {
    "qobjectdefs.h": "68c7065f0a09d621977a14bd11b6afe5bc9deac6c09f55707d7cfdc27e946724",
    "qtmocconstants.h": "7445fe93d002c126fb07a520a7c65780e32ada50fb035569f92e98d7c73cf2d5",
    "qmetaobject_p.h": "864fde7c4f9d4df5fa492d1d45b50e899f0783b26199fd24d5a0c8c70366cd65",
    "qmetaobject.h": "c34685ab484430ff02ace477cd6edd19740acbdd4972e3e9fddbb575aa0b1809",
    "qtmochelpers.h": "da21ef709a2680cc1712c66959070e04d0e5c3998a693441a52f11847962823e",
    "qmetaobject.cpp": "d91a6cea78c54ef455e206bd8042eadfc7fbd4dff6fdceea8b7d7645fd0757ba",
    "qmetatype.h": "ae574e3f47fefb57609a2362c681df69343cf8a46bcc889253fdb4ff0d50ffe4",
}


def require(value, message):
    if not value:
        raise ValueError(message)


def same_data(a, b):
    if type(a) is not type(b):
        return False
    if type(a) is dict:
        return set(a) == set(b) and all(same_data(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(same_data(x, y) for x, y in zip(a, b))
    return a == b


def _keys(value, required):
    require(type(value) is dict and set(value) == set(required), "closed Qt metacall schema required")


def _int(value, low, high):
    require(type(value) is int and low <= value <= high, "bounded exact Qt integer required")


def _text(value, limit=4096):
    require(type(value) is str and 0 < len(value.encode("utf-8")) <= limit and "\0" not in value,
            "bounded literal Qt text required")


def _path(value):
    _text(value)
    require(value.startswith("/") and not value.startswith("//") and
            str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts,
            "canonical lexical Qt file path required")


def _hash(value):
    require(type(value) is str and _SHA.fullmatch(value) is not None, "exact Qt file SHA256 required")


def _image(value):
    _keys(value, {"path", "sha256"})
    _path(value["path"])
    _hash(value["sha256"])


def _file(value):
    _keys(value, {"file", "sha256"})
    _path(value["file"])
    _hash(value["sha256"])


def _offset(value, alignment=1):
    require(type(value) is str and _OFFSET.fullmatch(value) is not None and
            int(value, 16) <= MAX_OFFSET and int(value, 16) % alignment == 0,
            "canonical bounded aligned Qt image offset required")
    return int(value, 16)


def _name(value):
    require(type(value) is str and _NAME.fullmatch(value) is not None, "literal Qt class/type name required")


def _evidence(value):
    _keys(value, {"claim", "originals"})
    claim = value["claim"]
    _keys(claim, {"kind", "file", "sha256", "pointer"})
    require(type(claim["kind"]) is str and claim["kind"] in {"compiler", "dwarf", "header", "reviewed_contract"}, "attributable Qt evidence required")
    _file({k: claim[k] for k in ("file", "sha256")})
    pointer = claim["pointer"]
    require(type(pointer) is str and len(pointer.encode("utf-8")) <= 2048 and "\0" not in pointer and
            (not pointer or pointer.startswith("/")) and not re.search(r"~(?![01])", pointer),
            "bounded Qt evidence JSON pointer required")
    originals = value["originals"]
    require(type(originals) is list and 1 <= len(originals) <= 16, "bounded independent Qt originals required")
    seen = set()
    for original in originals:
        _keys(original, {"role", "file", "sha256"})
        require(type(original["role"]) is str and original["role"] in {
            "header", "dwarf", "source", "original_ir", "original_build", "symbol_correlation", "independent_review"},
            "closed original Qt evidence role required")
        _file({k: original[k] for k in ("file", "sha256")})
        require(original["file"] not in seen and original["file"] != claim["file"], "Qt evidence cannot cite itself")
        seen.add(original["file"])
    require(any(o["role"] == "independent_review" for o in originals), "Qt consumer needs a separate independent review")


def is_qt_consumer(value):
    return type(value) is dict and value.get("kind") == "qt_moc"


def validate_selection(value):
    _keys(value, {"format", "module", "qt_core", "catalog", "code", "table", "qt_sources",
                  "class_name", "record_offset", "dispatcher_offset", "local_index", "signature"})
    require(value["format"] == SELECTION, "explicit Qt metacall selection format required")
    _image(value["module"])
    _image(value["qt_core"])
    require(value["module"]["path"] != value["qt_core"]["path"], "original class image and QtCore must be distinct")
    files = [value[k] for k in ("catalog", "code", "table", "qt_sources")]
    for pin in files:
        _file(pin)
    require(len({p["file"] for p in files}) == 4 and not {p["file"] for p in files} &
            {value["module"]["path"], value["qt_core"]["path"]}, "Qt artifacts must be distinct pinned files")
    _name(value["class_name"])
    _offset(value["record_offset"], 8)
    _offset(value["dispatcher_offset"], 4)
    _int(value["local_index"], 0, 511)
    _text(value["signature"], 1024)
    require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)", value["signature"]) is not None,
            "first Qt dispatch accepts an ordinary zero-argument slot signature only")
    return copy.deepcopy(value)


def validate_consumer(value, *, root_type):
    _keys(value, {"kind", "selection", "argument", "pointee_type", "role", "dispatch", "this_adjustment", "evidence"})
    require(value["kind"] == "qt_moc", "explicit Qt consumer required")
    selection = validate_selection(value["selection"])
    _name(root_type)
    require(value["pointee_type"] == root_type == selection["class_name"] and
            type(value["argument"]) is int and value["argument"] == 0 and
            value["role"] == "qobject_primary_base" and value["dispatch"] == "static_metacall" and
            type(value["this_adjustment"]) is int and value["this_adjustment"] == 0,
            "Qt consumer needs the independently admitted unadjusted primary QObject arg0")
    _evidence(value["evidence"])
    return copy.deepcopy(value)


def consumer_contract(value):
    validate_consumer(value, root_type=value["pointee_type"])
    selection = value["selection"]
    return {"format": CONSUMER, "selection": copy.deepcopy(selection),
            "callable": {"entry": {"module": copy.deepcopy(selection["module"]), "offset": selection["dispatcher_offset"]},
                         "abi": "default", "argument_types": ["pointer", "int32", "int32", "pointer"],
                         "return_type": "void", "context": "none", "pointer_policy": "borrowed_no_escape"},
            "argument": 0, "pointee_type": value["pointee_type"], "role": "qobject_primary_base",
            "dispatch": "static_metacall", "this_adjustment": 0,
            "qobject_base": "independently_admitted_primary_base_at_zero", "call": 0,
            "argv": {"bytes": 8, "slots": 1, "slot0": "null", "ownership": "private_no_escape", "expires": "call"}}


def image_claims(consumer):
    selection = validate_selection(consumer["selection"])
    return sorted([copy.deepcopy(selection["module"]), copy.deepcopy(selection["qt_core"])], key=lambda p: p["path"])


def file_claims(consumer):
    validate_consumer(consumer, root_type=consumer["pointee_type"])
    selection = consumer["selection"]
    result = {p["path"]: p["sha256"] for p in image_claims(consumer)}
    def add(pin):
        require(pin["file"] not in result or result[pin["file"]] == pin["sha256"], "conflicting Qt file identities")
        result[pin["file"]] = pin["sha256"]
    for key in ("catalog", "code", "table", "qt_sources"):
        add(selection[key])
    # The first supported SDK revision has an exact source closure. Derive it
    # purely from the pinned manifest's canonical parent, before resource IO.
    source_parent = PurePosixPath(selection["qt_sources"]["file"]).parent
    for name, digest in _SOURCES.items():
        add({"file": str(source_parent / name), "sha256": digest})
    add(consumer["evidence"]["claim"])
    for original in consumer["evidence"]["originals"]:
        add(original)
    return result


def _actor(value):
    actor = copy.deepcopy(value)
    if type(actor) is dict and actor.get("op") == OPERATION:
        actor = {"kind": ACTOR, **{k: v for k, v in actor.items() if k != "op"}}
    _keys(actor, {"kind", "receiver", "selection"})
    require(actor["kind"] == ACTOR, "Qt actor is a distinct typed operation")
    validate_selection(actor["selection"])
    _keys(actor["receiver"], {"native_path"})
    return actor


def _observed(recipe):
    require(type(recipe) is dict and type(recipe.get("root")) is dict and type(recipe.get("lifetime")) is dict,
            "Qt dispatch requires a typed immediate receiver")
    mode = recipe["lifetime"].get("mode")
    root_kind = "lifecycle" if mode == "main_queue_observed_root" else "getter" if mode == "main_queue_borrowed_root" else None
    require(root_kind is not None and set(recipe["root"]) == {root_kind} and
            same_data(recipe.get("path"), []) and same_data(recipe.get("preconditions"), []) and
            type(recipe.get("types")) is list and len(recipe["types"]) == 1,
            "Qt dispatch requires one admitted zero-edge observed or freshly borrowed root with no guards")
    layout = recipe["types"][0]
    require(type(layout) is dict and type(layout.get("runtime_identity")) is dict and
            layout["runtime_identity"].get("kind") == "pinned_data_vptr" and
            type(layout.get("fields")) is list and len(layout["fields"]) == 1,
            "Qt receiver needs solely its admitted primary vptr layout")
    field = layout["fields"][0]
    require(type(field) is dict and set(field) == {"name", "type", "offset"} and field["type"] == "pointer" and
            type(field["offset"]) is int and field["offset"] == 0 and
            field["name"] == layout["runtime_identity"].get("field") and
            layout.get("name") == recipe.get("root_type") == recipe.get("result_type"),
            "Qt receiver must preserve its one primary vptr at zero")


def require_consumer(recipe, actor, argument):
    actor = _actor(actor)
    _observed(recipe)
    require(type(argument) is int and argument == 0 and same_data(actor["receiver"]["native_path"], recipe),
            "Qt consumer must bind this actual receiver slot")
    consumer = validate_consumer(recipe.get("consumer"), root_type=recipe["root_type"])
    require(same_data(consumer["selection"], actor["selection"]), "Qt method selection differs from its consumer admission")


def validate_actor(actor, *, validate_path):
    value = _actor(actor)
    require(type(actor) is dict and actor.get("kind") == ACTOR and "op" not in actor, "explicit Qt actor required")
    validated = validate_path(value["receiver"]["native_path"])
    require(type(validated) is dict and same_data(validated, value["receiver"]["native_path"]),
            "trusted Qt path validator must preserve the exact frozen recipe")
    require_consumer(validated, value, 0)
    return value


def source_claims(selection, manifest):
    validate_selection(selection)
    require(type(manifest) is dict and manifest.get("format") == "anyuse.rea-qt-moc-source-retention/1" and
            manifest.get("status") == "complete" and manifest.get("qtbase_commit") == QT_COMMIT and
            manifest.get("version_under_review") == QT_VERSION and manifest.get("source_only") is True and
            manifest.get("downloaded_source_executed") is False and manifest.get("native_abi_admission") is False and
            type(manifest.get("files")) is dict and set(manifest["files"]) == set(_SOURCES),
            "exact retained Qt public-source protocol required")
    parent = PurePosixPath(selection["qt_sources"]["file"]).parent
    result = {}
    for name, expected in _SOURCES.items():
        row = manifest["files"][name]
        require(type(row) is dict and row.get("sha256") == expected and
                row.get("status") == "retained_source_bytes_only", "Qt source SHA/provenance differs from the reviewed revision")
        _int(row.get("bytes"), 1, 1024 * 1024)
        result[str(parent / name)] = expected
    return result


def _image_fact(value, expected):
    require(type(value) is dict and value.get("file") == expected["path"] and value.get("sha256") == expected["sha256"],
            "Qt static artifact belongs to another original image")
    _int(value.get("bytes"), 32, 512 * 1024**2)


def _range(value, module, kind, *, catalog=False):
    require(type(value) is dict, "original Qt range must be data")
    offset = value.get("image_offset")
    _offset(offset)
    count = value.get("bytes")
    _int(count, 1, MAX_RANGE_BYTES)
    raw = value.get("original_hex")
    require(type(raw) is str and re.fullmatch(r"[0-9a-f]+", raw) is not None and len(raw) == 2 * count,
            "original Qt range needs exact bounded lowercase bytes")
    digest = value.get("original_sha256") if catalog else value.get("sha256")
    _hash(digest)
    require(hashlib.sha256(bytes.fromhex(raw)).hexdigest() == digest, "Qt static range digest differs from its bytes")
    if catalog:
        require(value.get("original_hex") == value.get("imported_hex") or value.get("role") == "direct_superdata_record",
                "relocated non-record Qt data is unsupported")
        require(value.get("original_sha256") == value.get("imported_sha256") or value.get("role") == "direct_superdata_record",
                "non-record Qt data differs in imported image")
    else:
        require(value.get("original_equals_imported", True) is True, "Qt instruction bytes were modified")
    return {"module": copy.deepcopy(module), "offset": offset, "bytes": count, "sha256": digest,
            "original_hex": raw, "kind": kind}


def static_plan(consumer, catalog, code, table, sources):
    """Pure expected-range plan. B must compare ALL originals before publishing pin.

    Independent P admission additionally binds callback ABI, primary QObject base
    and selected original code/table interpretation. This is not an ABI decoder.
    """
    validate_consumer(consumer, root_type=consumer["pointee_type"])
    plan = static_selection_plan(consumer["selection"], catalog, code, table, sources)
    pin = {"format": RUNTIME_PIN, "selection": copy.deepcopy(plan["selection"]), "consumer": consumer_contract(consumer),
           "method": plan["method"], "record_pointers": plan["record_pointers"], "ranges": plan["ranges"]}
    return {"runtime_pin": pin, "original_ranges": plan["original_ranges"]}


def static_selection_plan(selection, catalog, code, table, sources):
    """Validate static interpretation independently of receiver/ABI admission.

    Evidence builders use this pure checker before retaining a selection. A
    runtime consumer still enters through static_plan and validate_consumer.
    """
    s = validate_selection(selection)
    source_claims(s, sources)
    require(type(catalog) is dict and catalog.get("format") == "anyuse.rea-qt-moc-static-catalog/1" and
            catalog.get("status") == "completed_class_local_static_catalog" and catalog.get("completed") is True and
            catalog.get("inputs_unchanged") is True and catalog.get("callable_abi_admitted") is False and
            catalog.get("lifetime_admitted") is False and catalog.get("task_proof") is False and
            catalog.get("global_method_indices_available") is False and
            same_data(catalog.get("source_protocol"), {"qt_version": QT_VERSION, "qtbase_commit": QT_COMMIT, "metadata_revision": 13}) and
            catalog.get("record_layout") == "direct-superdata-56" and
            catalog.get("static_storage_layout") == "qtmochelpers-contiguous-data-then-strings",
            "a completed bounded original Qt catalog is required, not an ABI declaration flag")
    _image_fact(catalog.get("image"), s["module"])
    _image_fact(catalog.get("qt_core"), s["qt_core"])
    require(type(catalog.get("source_manifest")) is dict and
            all(catalog["source_manifest"].get(k) == s["qt_sources"][k] for k in ("file", "sha256")) and
            type(catalog.get("locator")) is dict and catalog["locator"].get("image_offset") == s["record_offset"] and
            type(catalog.get("dispatcher_code_site")) is dict and
            catalog["dispatcher_code_site"].get("image_offset") == s["dispatcher_offset"] and
            catalog.get("class_name") == s["class_name"], "catalog locator/class/public-source identity differs")
    macho = catalog.get("macho")
    require(type(macho) is dict and macho.get("architecture") == "arm64" and
            type(macho.get("cpu_subtype")) is int and macho["cpu_subtype"] == 0,
            "Qt dispatch requires the original plain-arm64 image")
    base = _offset(macho.get("image_base"))
    methods = catalog.get("methods")
    require(type(methods) is list and 1 <= len(methods) <= 512 and
            type(catalog.get("local_method_count")) is int and catalog["local_method_count"] == len(methods) and
            s["local_index"] < len(methods), "Qt local method index exceeds its completed catalog")
    method = methods[s["local_index"]]
    require(type(method) is dict and type(method.get("local_index")) is int and method["local_index"] == s["local_index"] and
            method.get("kind") == "slot" and method.get("signature_as_recorded") == s["signature"] and
            method.get("parameters") == [] and type(method.get("access")) is str and
            method["access"] in {"private", "protected", "public"} and
            all(method.get(key) is False for key in ("compatibility", "cloned", "scriptable", "is_const", "callable_abi_admitted")) and
            type(method.get("revision_encoded")) is int and method["revision_encoded"] == 0,
            "Qt selected row must be an ordinary class-local zero-argument slot")
    require(type(method.get("name")) is str and method["name"] + "()" == s["signature"],
            "Qt slot name differs from the selected zero-argument signature")
    flags = _offset(method.get("flags"))
    require(flags & 12 == 8 and flags & ~15 == 0 and flags & 3 != 3 and
            method["access"] == ("private", "protected", "public")[flags & 3],
            "unsupported Qt slot flags/revision")
    result_type = method.get("return_type")
    require(type(result_type) is dict and result_type.get("kind") == "source_builtin_id" and
            type(result_type.get("id")) is int and result_type["id"] == 43 and
            result_type.get("name") == "void" and result_type.get("encoding") == "0x2b" and
            result_type.get("callable") is False, "Qt first dispatch requires the original builtin void return")
    ranges = catalog.get("ranges")
    require(type(ranges) is list and 1 <= len(ranges) <= MAX_RANGES, "bounded original Qt catalog ranges required")
    original_ranges, runtime_ranges = [], []
    records = []
    for index, value in enumerate(ranges):
        require(type(value) is dict and type(value.get("id")) is int and value["id"] == index,
                "Qt catalog range IDs differ")
        item = _range(value, s["module"], "data", catalog=True)
        require(value.get("address") == hex(base + int(item["offset"], 16)), "catalog range address/base differs")
        original_ranges.append(item)
        if value.get("role") == "direct_superdata_record":
            require(item["offset"] == s["record_offset"] and item["bytes"] == 56, "Qt static record extent differs")
            records.append(value)
        else:
            runtime_ranges.append(item)
    require(len(records) == 1, "exact one original Qt record required")
    row_id, payload_id = method.get("row_range"), method.get("payload_range")
    _int(row_id, 0, len(ranges) - 1)
    _int(payload_id, 0, len(ranges) - 1)
    row_offset = method.get("row_byte_offset")
    _int(row_offset, 0, MAX_RANGE_BYTES - 24)
    raw_rows = bytes.fromhex(ranges[row_id]["original_hex"])
    require(ranges[row_id].get("role") == "method_rows" and len(raw_rows) == 24 * len(methods) and
            row_offset == 24 * s["local_index"] and row_offset + 24 <= len(raw_rows), "Qt local row byte offset differs")
    raw_row = raw_rows[row_offset:row_offset + 24]
    raw_payload = bytes.fromhex(ranges[payload_id]["original_hex"])
    require(len(raw_row) == 24 and int.from_bytes(raw_row[4:8], "little") == 0 and
            int.from_bytes(raw_row[16:20], "little") == flags and
            ranges[payload_id].get("role") == "method_payload_" + str(s["local_index"]) and
            len(raw_payload) == 4 and int.from_bytes(raw_payload, "little") == 43,
            "Qt selected method row/payload bytes differ from the catalog interpretation")
    data = [value for value in ranges if value["role"] == "generated_metadata_data_array"]
    pairs = [value for value in ranges if value["role"] == "string_pairs"]
    require(len(data) == len(pairs) == 1, "Qt catalog needs its original contiguous integer/string storage")
    data_bytes = bytes.fromhex(data[0]["original_hex"])
    require(len(data_bytes) >= 60 and len(data_bytes) % 4 == 0 and data_bytes[-4:] == b"\0" * 4 and
            type(catalog.get("metadata_data_bytes")) is int and catalog["metadata_data_bytes"] == len(data_bytes),
            "Qt generated metadata extent/end marker differs")
    words = [int.from_bytes(data_bytes[i:i + 4], "little") for i in range(0, len(data_bytes), 4)]
    require(words[0] == 13 and words[4] == len(methods) and same_data(catalog.get("header_u32"), words[:14]) and
            int(ranges[row_id]["image_offset"], 16) == int(data[0]["image_offset"], 16) + 4 * words[5] and
            int(ranges[payload_id]["image_offset"], 16) == int(data[0]["image_offset"], 16) + 4 * int.from_bytes(raw_row[8:12], "little") and
            data_bytes[4 * words[5] + row_offset:4 * words[5] + row_offset + 24] == raw_row and
            data_bytes[4 * int.from_bytes(raw_row[8:12], "little"):4 * int.from_bytes(raw_row[8:12], "little") + 4] == raw_payload and
            int(pairs[0]["image_offset"], 16) == int(data[0]["image_offset"], 16) + len(data_bytes),
            "Qt selected row/payload is outside its actual original integer array")
    count = catalog.get("string_count")
    _int(count, 1, 4096)
    pair_bytes = bytes.fromhex(pairs[0]["original_hex"])
    require(len(pair_bytes) == count * 8, "Qt original string-pair array extent differs")
    strings = {}
    for index in range(count):
        offset = int.from_bytes(pair_bytes[index * 8:index * 8 + 4], "little")
        size = int.from_bytes(pair_bytes[index * 8 + 4:index * 8 + 8], "little")
        matches = [value for value in ranges if value["role"] == "string_payload_" + str(index)]
        require(len(matches) == 1 and size <= 4096 and matches[0]["bytes"] == size + 1 and
                int(matches[0]["image_offset"], 16) == int(pairs[0]["image_offset"], 16) + offset,
                "Qt original string payload/pair correlation differs")
        raw = bytes.fromhex(matches[0]["original_hex"])
        require(raw[-1:] == b"\0" and b"\0" not in raw[:-1], "Qt string must retain its exact terminator")
        strings[index] = raw[:-1].decode("utf-8", errors="strict")
    name_index = int.from_bytes(raw_row[0:4], "little")
    require(type(method.get("name_index")) is int and method["name_index"] == name_index and
            strings.get(name_index) == method["name"] and strings.get(words[1]) == s["class_name"],
            "Qt class/method names differ from their original string bytes")
    pointers = []
    authority = catalog.get("table_pointer_authority")
    dispatcher = catalog.get("dispatcher_pointer_authority")
    require(type(authority) is list and len(authority) == 2 and type(dispatcher) is dict,
            "original Qt table/dispatcher pointer correlation required")
    raw_record = bytes.fromhex(records[0]["original_hex"])
    for word, value in zip((1, 2, 3), [*authority, dispatcher]):
        require(type(value) is dict and value.get("pointer_format") == "DYLD_CHAINED_PTR_64_OFFSET" and
                value.get("relocation_status") == "APPLIED" and type(value.get("relocation_type")) is int and
                value["relocation_type"] == 0x8888, "Qt record requires checked original applied rebases")
        slot = int(s["record_offset"], 16) + 8 * word
        target = _offset(value.get("target_image_offset"))
        require(value.get("slot") == hex(base + slot) and value.get("target") == hex(base + target) and
                value.get("original") == raw_record[word * 8:word * 8 + 8].hex(), "Qt consumed pointer slot differs")
        encoded = int.from_bytes(raw_record[word * 8:word * 8 + 8], "little")
        require(encoded >> 63 == 0 and (encoded >> 44) & 127 == 0 and (encoded >> 36) & 255 == 0 and
                encoded & ((1 << 36) - 1) == target,
                "Qt record pointer is bound/authenticated/tagged or has a different original target")
        pointers.append({"slot_offset": hex(slot), "target_offset": hex(target)})
    require(pointers[0]["target_offset"] == pairs[0]["image_offset"] and
            pointers[1]["target_offset"] == data[0]["image_offset"] and
            pointers[2]["target_offset"] == s["dispatcher_offset"], "Qt consumed record pointer target differs from its actual storage")
    require(type(code) is dict and code.get("format") == "anyuse.rea-ghidra-code-at-offsets/1" and
            code.get("status") == "completed_static_code_observation" and code.get("original_file_unchanged") is True and
            code.get("automatic_abi_or_lifetime_admission") is False and code.get("cfg_complete_claimed") is False and
            code.get("task_proof") is False and code.get("ghidra") == "12.1.4" and
            code.get("language") == "AARCH64:LE:64:AppleSilicon", "completed original code observation required")
    _image_fact(code.get("image"), s["module"])
    sites = code.get("sites")
    require(type(sites) is list and 1 <= len(sites) <= 4, "bounded original code sites required")
    require(sum(type(site) is dict and site.get("image_offset") == s["dispatcher_offset"] for site in sites) == 1,
            "original code observation lacks its exact Qt dispatcher")
    for site in sites:
        require(type(site) is dict and site.get("status") == "decompiled_static_code_observation" and
                site.get("decompile_completed") is True and site.get("cfg_complete_claimed") is False and
                type(site.get("body_original_ranges")) is list and 1 <= len(site["body_original_ranges"]) <= 128,
                "original code site must retain a complete bounded observation")
        body = []
        for value in site["body_original_ranges"]:
            item = _range(value, s["module"], "code")
            original_ranges.append(item)
            runtime_ranges.append(item)
            body.append(item)
        instructions = site.get("instructions")
        require(type(instructions) is list and 1 <= len(instructions) <= 4096, "bounded original instructions required")
        for instruction in instructions:
            require(type(instruction) is dict, "original instruction must be data")
            at = _offset(instruction.get("image_offset"), 4)
            require(instruction.get("address") == hex(base + at), "instruction base/RVA differs")
            matches = [item for item in body if int(item["offset"], 16) <= at <= int(item["offset"], 16) + item["bytes"] - 4]
            require(len(matches) == 1, "instruction must belong to one original code range")
            relative = at - int(matches[0]["offset"], 16)
            require(instruction.get("bytes") == matches[0]["original_hex"][relative * 2:relative * 2 + 8],
                    "instruction bytes differ from their pinned original range")
    require(type(table) is dict and table.get("format") == "anyuse.rea-original-file-range/1" and
            table.get("status") == "retained_original_file_bytes" and table.get("original_unchanged") is True and
            table.get("automatic_abi_or_lifetime_admission") is False and table.get("task_proof") is False and
            table.get("byte_order") == "little" and type(table.get("unit_bytes")) is int and table["unit_bytes"] == 2,
            "original bounded uint16 dispatch data required")
    _image_fact(table.get("image"), s["module"])
    item = _range(table.get("range"), s["module"], "data")
    require(item["bytes"] % 2 == 0 and 1 <= item["bytes"] // 2 <= 512 and s["local_index"] < item["bytes"] // 2,
            "Qt selected index exceeds original dispatch data")
    units = [int.from_bytes(bytes.fromhex(item["original_hex"])[i:i + 2], "little") for i in range(0, item["bytes"], 2)]
    require(same_data(table.get("unsigned_units"), units), "original dispatch numbers differ from retained bytes")
    original_ranges.append(item)
    runtime_ranges.append(item)
    def dedup(values):
        result = {}
        for value in values:
            key = value["offset"], value["bytes"], value["kind"]
            require(key not in result or same_data(result[key], value), "conflicting original Qt range records")
            result[key] = value
        rows = [result[key] for key in sorted(result)]
        require(len(rows) <= MAX_RANGES and sum(row["bytes"] for row in rows) <= MAX_TOTAL_BYTES,
                "original Qt dispatch ranges exceed their fixed budget")
        return rows
    original_ranges, runtime_ranges = dedup(original_ranges), dedup(runtime_ranges)
    return {"selection": copy.deepcopy(s),
           "method": {"class_name": s["class_name"], "local_index": s["local_index"], "name": method["name"],
                      "signature": s["signature"], "access": method["access"], "kind": "slot", "return_type": "void", "parameters": []},
           "record_pointers": pointers, "ranges": runtime_ranges, "original_ranges": original_ranges}


def validate_row(row, operation, *, validate_entry=None):
    actor = _actor(operation)
    recipe = actor["receiver"]["native_path"]
    require_consumer(recipe, actor, 0)
    consumer = recipe["consumer"]
    selection = actor["selection"]
    _keys(row, {"index", "op", "status", "function", "return_type", "value", "out_buffers", "pointer_reads",
                "swift_field_reads", "callable_entries", "compiler_completion", "native_paths", "qt_moc"})
    require(type(row["index"]) is int and row["index"] == 0 and row["op"] == OPERATION and row["status"] == "ok" and
            row["return_type"] == "void" and row["value"] is None and row["compiler_completion"] is None and
            all(same_data(row[key], []) for key in ("out_buffers", "pointer_reads", "swift_field_reads", "callable_entries")),
            "Qt row must be its one void direct dispatch without pointer or helper outputs")
    _keys(row["qt_moc"], {"format", "selection", "consumer", "call", "local_index", "argv", "permit_consumed", "returned", "expires"})
    observed = row["qt_moc"]
    require(observed["format"] == OBSERVATION and same_data(observed["selection"], selection) and
            same_data(observed["consumer"], consumer_contract(consumer)) and
            type(observed["call"]) is int and observed["call"] == 0 and
            type(observed["local_index"]) is int and observed["local_index"] == selection["local_index"] and
            observed["permit_consumed"] is True and observed["returned"] is True and observed["expires"] == "transaction" and
            same_data(observed["argv"], {**consumer_contract(consumer)["argv"], "expired": True}),
            "Qt dispatch lost exact method/vector/permit/expiry binding")
    paths = row["native_paths"]
    scope = "borrowed_scope" if recipe["lifetime"]["mode"] == "main_queue_borrowed_root" else "lifecycle_scope"
    require(type(paths) is list and len(paths) == 1 and type(paths[0]) is dict and
            same_data(paths[0].get("recipe"), recipe) and type(paths[0].get("argument")) is int and paths[0]["argument"] == 0 and
            same_data(paths[0].get("consumer"), consumer_contract(consumer)) and paths[0].get("sealed") is True and
            paths[0].get("dispatch_permit_consumed") is True and type(paths[0].get(scope)) is dict,
            "Qt row lost its exact observed receiver/path/consumer")
    function = row["function"]
    _keys(function, {"target", "module", "image_identity", "instruction_section", "module_size", "reported_module_size",
                     "executable", "symbol", "implementation_offset", "abi", "argument_types", "return_type", "signature_evidence", "bridge"})
    target = {"module": selection["module"], "offset": selection["dispatcher_offset"]}
    require(same_data(function["target"], target) and function["module"] == selection["module"]["path"] and
            function["symbol"] is None and function["implementation_offset"] == selection["dispatcher_offset"] and
            function["executable"] is True and function["abi"] == "default" and
            same_data(function["argument_types"], ["pointer", "int32", "int32", "pointer"]) and
            function["return_type"] == "void" and same_data(function["signature_evidence"], consumer["evidence"]["claim"]) and
            function["bridge"] == "frida-native", "Qt function metadata differs from the fixed public callback ABI")
    _int(function["module_size"], 4, 512 * 1024**2)
    _int(function["reported_module_size"], 4, 512 * 1024**2)
    identity = function["image_identity"]
    _keys(identity, {"cpu_type", "cpu_subtype", "uuid"})
    require(type(identity["cpu_type"]) is int and identity["cpu_type"] == 0x0100000c and
            type(identity["cpu_subtype"]) is int and identity["cpu_subtype"] == 0 and
            type(identity["uuid"]) is str and re.fullmatch(r"[0-9a-f]{32}", identity["uuid"]) is not None,
            "Qt callback requires a plain-arm64 loaded image identity")
    section = function["instruction_section"]
    _keys(section, {"segment", "name", "offset", "bytes", "flags"})
    _text(section["segment"], 16)
    _text(section["name"], 16)
    start = _offset(section["offset"])
    _int(section["bytes"], 4, 512 * 1024**2)
    _int(section["flags"], 0, 2**32 - 1)
    entry = int(selection["dispatcher_offset"], 16)
    require(section["flags"] & 0x80000400 and section["flags"] & 255 not in {1, 12, 18} and
            start <= entry and entry + 4 <= start + section["bytes"] <= function["module_size"],
            "Qt callback is outside its exact original instruction extent")
    if validate_entry is not None:
        require(validate_entry(function, target) is True, "Qt loaded callback failed independent entry pin validation")
    return copy.deepcopy(observed)
