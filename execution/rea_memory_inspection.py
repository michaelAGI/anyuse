"""Pure closed raw-inspection schemas; observations never admit a typed action.

No imports perform filesystem, process, native, network or runtime operations.
The VM-only bridge owns all acquisition and resource/lifecycle checks.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import PurePosixPath
import re

FORMAT = "anyuse.rea-memory-inspection/2"
WORKER = "anyuse.rea-runtime-worker/2"
OPERATIONS = frozenset({"memory_ranges", "memory_range", "memory_bytes", "memory_pointer_range"})
STAGES = frozenset({"validate", "module_before", "ranges", "select", "source", "mapping_before",
                    "charge", "read", "mapping_after", "pointee", "module_after", "deadline", "complete"})
MAX_OPERATIONS, MAX_METADATA, MAX_READ, MAX_READ_BYTES = 16, 128, 16384, 65536
MAX_MODULE_BYTES, MAX_MODULE_RANGES, SAFE_INTEGER, POINTER_BYTES = 512 * 1024**2, 32, 2**53 - 1, 8
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
HEX = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
PERMISSIONS = re.compile(r"[r-][w-][x-]\Z")


def _need(value, message):
    if not value: raise ValueError(message)


def _keys(value, fields):
    _need(type(value) is dict and set(value) == set(fields), "closed memory inspection schema required")


def _integer(value, minimum=0, maximum=SAFE_INTEGER):
    _need(type(value) is int and minimum <= value <= maximum, "memory integer exceeds its exact bound")
    return value


def _text(value, maximum, *, empty=False):
    _need(type(value) is str and (empty or bool(value)) and len(value) <= maximum and "\0" not in value,
          "bounded literal memory metadata required")
    return value


def _address(value):
    _need(type(value) is str and HEX.fullmatch(value), "canonical bounded memory hex required")
    return int(value, 16)


def _path(value, *, canonical=False):
    _text(value, 4096)
    _need(value.startswith("/"), "absolute observed memory path required")
    if canonical:
        _need(not value.startswith("//") and str(PurePosixPath(value)) == value and
              ".." not in PurePosixPath(value).parts, "canonical memory module path required")
    return value


def _file(value):
    _keys(value, {"path", "sha256"})
    _path(value["path"], canonical=True)
    _need(type(value["sha256"]) is str and SHA.fullmatch(value["sha256"]), "exact memory module file pin required")


def _name(value):
    _need(type(value) is str and NAME.fullmatch(value), "bounded memory alias required")
    return value


def _same(left, right):
    if type(left) is not type(right): return False
    if type(left) is dict: return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if type(left) is list: return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def is_request(request):
    return (type(request) is dict and type(request.get("operations")) is list and
            any(type(row) is dict and type(row.get("op")) is str and row["op"] in OPERATIONS
                for row in request["operations"]))


def module_claims(operation):
    """File provenance claims in one already validated operation; no IO."""
    if operation["op"] == "memory_range": return [copy.deepcopy(operation["module"])]
    if operation["op"] == "memory_ranges" and operation["scope"]["kind"] == "module":
        return [copy.deepcopy(operation["scope"]["module"])]
    return []


def _budget(operations):
    metadata = reads = 0
    for row in operations:
        if row["op"] == "memory_ranges": metadata += row["limit"]
        elif row["op"] == "memory_range": metadata += 1
        elif row["op"] == "memory_bytes": metadata += 2; reads += row["bytes"]
        else: metadata += 3; reads += POINTER_BYTES
    _need(metadata <= MAX_METADATA and reads <= MAX_READ_BYTES, "whole memory batch exceeds reserved budget")
    return {"metadata_reserved": metadata, "read_bytes_reserved": reads}


def validate_request(request):
    _keys(request, {"operations"})
    operations = request["operations"]
    _need(type(operations) is list and 1 <= len(operations) <= MAX_OPERATIONS, "memory batch needs1..16 operations")
    aliases, pins = {}, {}
    for row in operations:
        _need(type(row) is dict and type(row.get("op")) is str and row["op"] in OPERATIONS,
              "memory inspection batch cannot mix calls or other operations")
        op = row["op"]
        if op == "memory_ranges":
            _keys(row, {"op", "scope", "limit", "as"})
            scope = row["scope"]
            _need(type(scope) is dict and type(scope.get("kind")) is str, "closed literal memory scope required")
            if scope["kind"] == "all": _keys(scope, {"kind"})
            else:
                _keys(scope, {"kind", "module"})
                _need(scope["kind"] == "module", "literal all/module memory scope required")
                _file(scope["module"])
            _integer(row["limit"], 1, MAX_METADATA)
            alias = {"kind": "ranges", "extent": row["limit"]}
        elif op == "memory_range":
            _keys(row, {"op", "module", "rva", "bytes", "as"})
            _file(row["module"])
            _integer(row["bytes"], 1, MAX_READ)
            _need(_address(row["rva"]) + row["bytes"] <= MAX_MODULE_BYTES, "memory RVA exceeds bounded module interval")
            alias = {"kind": "span", "extent": row["bytes"]}
        else:
            pointer = op == "memory_pointer_range"
            _keys(row, {"op", "from", "offset", "pointee_bytes", "as"} if pointer else {"op", "from", "offset", "bytes"})
            width = POINTER_BYTES if pointer else _integer(row["bytes"], 1, MAX_READ)
            _integer(row["offset"], 0, SAFE_INTEGER - width)
            source = row["from"]
            _need(type(source) is dict, "earlier typed memory alias required")
            if "span" in source:
                _keys(source, {"span"}); _name(source["span"])
                found = aliases.get(source["span"])
                _need(found is not None and found["kind"] == "span", "memory span alias is unknown, forward or expired")
                _need(row["offset"] + width <= found["extent"], "memory read exceeds prior declared span")
            else:
                _keys(source, {"ranges", "index"}); _name(source["ranges"])
                found = aliases.get(source["ranges"])
                _need(found is not None and found["kind"] == "ranges", "memory ranges alias is unknown, forward or expired")
                _integer(source["index"], 0, found["extent"] - 1)
            alias = None
            if pointer:
                _integer(row["pointee_bytes"], 1, MAX_READ)
                alias = {"kind": "span", "extent": row["pointee_bytes"]}
        if alias is not None:
            name = _name(row["as"])
            _need(name not in aliases, "memory aliases cannot be redefined")
            aliases[name] = alias
        for claim in module_claims(row):
            _need(claim["path"] not in pins or pins[claim["path"]] == claim["sha256"], "conflicting memory module pins")
            pins[claim["path"]] = claim["sha256"]
    _budget(operations)
    try: encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
    except (UnicodeError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("memory request is not bounded plain JSON") from error
    _need(len(encoded) <= 64 * 1024, "memory request exceeds byte bound")
    return copy.deepcopy(request)


def request_budget(request): return _budget(validate_request(request)["operations"])


def _time(value):
    _need(type(value) in (int, float) and 0 <= value <= 253402300800000 and math.isfinite(value),
          "finite bounded memory observation time required")
    return value


def _error(value):
    _keys(value, {"type", "message", "stack"})
    _text(value["type"], 128); _text(value["message"], 4096, empty=True)
    if value["stack"] is not None: _text(value["stack"], 8192, empty=True)


def _range(value):
    _keys(value, {"base", "bytes", "protection", "file"})
    _need(_address(value["base"]) + _integer(value["bytes"], 1) < 2**64,
          "bounded mapped address interval required")
    _need(type(value["protection"]) is str and PERMISSIONS.fullmatch(value["protection"]), "literal mapping protection required")
    if value["file"] is not None:
        file = value["file"]
        _keys(file, {"path", "offset", "size"}); _path(file["path"]); _integer(file["offset"])
        if file["size"] is not None: _integer(file["size"])


def _mapping(value):
    _keys(value, {"at_ms", "status", "base", "bytes", "protection", "file", "error"})
    _time(value["at_ms"])
    _need(type(value["status"]) is str and value["status"] in {"mapped", "unmapped", "error"}, "literal mapping disposition required")
    if value["status"] != "mapped":
        _need(all(value[key] is None for key in ("base", "bytes", "protection", "file")), "unknown mapping cannot invent data")
        if value["status"] == "error": _error(value["error"])
        else: _need(value["error"] is None, "unmapped result cannot invent a diagnostic")
        return
    _need(value["error"] is None, "mapped observation cannot carry an error")
    _range(_mapping_data(value))


def _mapping_data(value): return {key: value[key] for key in ("base", "bytes", "protection", "file")}


def _inside(base, size, mapping):
    _need(mapping["status"] == "mapped" and mapping["protection"][0] == "r" and
          _address(mapping["base"]) <= base and base + size <= _address(mapping["base"]) + mapping["bytes"],
          "selected memory span is not inside one readable observed mapping")


def _span(value, mappings):
    _keys(value, {"base", "bytes", "mapping_index"})
    base, size = _address(value["base"]), _integer(value["bytes"], 1)
    _integer(value["mapping_index"], 0, len(mappings) - 1)
    _need(base + size < 2**64, "memory span address wrapped")
    _inside(base, size, mappings[value["mapping_index"]])


def _module(value, claim):
    _keys(value, {"path", "base", "bytes", "file", "loaded_binding", "loaded_image_verified", "ranges"})
    _file(value["file"])
    _need(_same(value["file"], claim) and value["path"] == claim["path"] and
          value["loaded_binding"] == "path_metadata_only" and value["loaded_image_verified"] is False,
          "module observation cannot claim live image verification")
    _need(_address(value["base"]) + _integer(value["bytes"], 1, MAX_MODULE_BYTES) < 2**64,
          "observed module interval wrapped")
    ranges = value["ranges"]
    _need(type(ranges) is list and 1 <= len(ranges) <= MAX_MODULE_RANGES,
          "bounded actual readable module ranges required")
    base, previous_end = _address(value["base"]), _address(value["base"])
    _need(base + MAX_MODULE_BYTES < 2**64, "module investigation ceiling is not representable")
    for observed in ranges:
        _range(observed)
        first, last = _address(observed["base"]), _address(observed["base"]) + observed["bytes"]
        _need(observed["protection"][0] == "r" and previous_end <= first and last <= base + MAX_MODULE_BYTES,
              "actual module ranges must be readable, ordered, disjoint and inside the investigation bound")
        previous_end = last


def _module_coverage(base, size, module):
    """One module-attributed range; current permissions still need a Process map."""
    matches = [observed for observed in module["ranges"]
               if _address(observed["base"]) <= base and base + size <= _address(observed["base"]) + observed["bytes"]]
    _need(len(matches) == 1, "selected memory span lacks one actual readable module range")
    return matches[0]


def _source(source, aliases):
    if "span" in source: return aliases[source["span"]]
    values = aliases[source["ranges"]]["spans"]
    return values[source["index"]] if source["index"] < len(values) else None


def _read(value, operation):
    _keys(value, {"offset", "requested_bytes", "issued", "returned", "bytes_hex", "returned_bytes", "unknown_partial"})
    width = POINTER_BYTES if operation["op"] == "memory_pointer_range" else operation["bytes"]
    _need(type(value["offset"]) is int and value["offset"] == operation["offset"] and
          type(value["requested_bytes"]) is int and value["requested_bytes"] == width and
          all(type(value[key]) is bool for key in ("issued", "returned", "unknown_partial")), "read journal differs from requested width/offset")
    if not value["returned"]:
        _need(value["bytes_hex"] is None and value["returned_bytes"] is None and
              value["unknown_partial"] is value["issued"], "unreturned read cannot invent bytes/count/extent")
        return
    _need(value["issued"] is True and type(value["bytes_hex"]) is str and
          re.fullmatch(r"(?:[0-9a-f]{2})*", value["bytes_hex"]) and
          type(value["returned_bytes"]) is int and value["returned_bytes"] == len(value["bytes_hex"]) // 2 and
          value["returned_bytes"] <= width and value["unknown_partial"] is (value["returned_bytes"] != width),
          "returned raw bytes/count/short-buffer disposition differs")


def validate_result(request, result, *, worker=False):
    """Pure fixed-agent journal validation; worker=True also checks its envelope.

    Neither form establishes process eligibility, typed access or task proof.
    Only B owns actual attachment/identity/resources and Frida lifecycle checks.
    """
    _need(type(worker) is bool, "worker envelope option must be an exact boolean")
    raw = is_request(request)
    _need(type(result) is dict, "runtime result must be data")
    journal = result.get("memory_inspection")
    rows = result.get("operations", [])
    if not raw:
        _need(journal is None and not (type(rows) is list and any(type(row) is dict and "inspection_index" in row for row in rows)),
              "typed runtime result carries unrequested raw inspection evidence")
        return None
    request = validate_request(request)
    operations = request["operations"]
    _need((not worker and "format" not in result or result.get("format") == WORKER) and
          type(result.get("status")) is str and result["status"] in {"completed", "incomplete", "unavailable"},
          "raw inspection requires a current agent/worker envelope")
    _need(type(rows) is list and len(rows) <= len(operations), "bounded raw successful prefix required")
    for key in ("invocations", "helper_load_attempts", "callable_resolutions"):
        _need(type(result.get(key)) is int and result[key] == 0, "raw inspection cannot carry app/helper/callable calls")
    _need(_same(result.get("helper_residency"), []) and _same(result.get("compiler_provenance", None if worker else {}), {}) and
          result.get("lifecycle_observer") is None, "raw inspection cannot carry typed/helper/hook provenance")
    if journal is None:
        _need(result["status"] != "completed" and rows == [] and
              (result.get("native_calls") is None or type(result.get("native_calls")) is int and result["native_calls"] == 0),
              "missing raw journal cannot create completed observations")
        return None
    _need(not worker or result.get("attached") is True, "returned raw journal requires actual worker attachment")
    _need(type(result.get("native_calls")) is int and result["native_calls"] == 0 and
          result.get("queue") == "runtime_inspection", "raw journal requires its zero-call inspection path")
    _keys(journal, {"format", "pointer_size", "byte_order", "loaded_binding", "loaded_image_verified", "raw_observation_only",
        "atomic_snapshot", "verified", "proof_created", "signature_inferred", "type_admitted", "lifetime_admitted",
        "range_inventory_materialization_bounded", "metadata_reserved", "read_bytes_reserved", "charged_bytes",
        "known_returned_bytes", "actual_read_bytes", "aliases_expired", "mappings", "attempts"})
    _need(journal["format"] == FORMAT and type(journal["pointer_size"]) is int and journal["pointer_size"] == POINTER_BYTES and
          journal["byte_order"] == "little" and journal["loaded_binding"] == "path_metadata_only" and
          journal["raw_observation_only"] is True and
          all(journal[key] is False for key in ("loaded_image_verified", "atomic_snapshot", "verified", "proof_created",
              "signature_inferred", "type_admitted", "lifetime_admitted", "range_inventory_materialization_bounded")) and
          type(journal["aliases_expired"]) is bool, "raw observation scope/architecture/expiry flags differ")
    process = result.get("process")
    _keys(process, {"pid", "executable", "architecture"})
    _integer(process["pid"], 1, 2**31 - 1); _path(process["executable"])
    budget = _budget(operations)
    mappings, attempts = journal["mappings"], journal["attempts"]
    early_refusal = (result["status"] != "completed" and rows == [] and attempts == [] and mappings == [] and result.get("error") is not None)
    _text(process["architecture"], 32)
    _need(early_refusal or process["architecture"] in {"arm64", "x64"},
          "unsupported raw process architecture cannot create observations")
    _need(all(type(journal[key]) is int and (journal[key] == value or early_refusal and journal[key] == 0)
              for key, value in budget.items()), "raw reservation differs from whole request")
    _need(type(mappings) is list and len(mappings) <= budget["metadata_reserved"] and type(attempts) is list and
          len(rows) <= len(attempts) <= min(len(rows) + 1, len(operations)), "raw attempt/mapping prefix exceeds its budget")
    for mapping in mappings: _mapping(mapping)
    aliases, cursor, charged, returned, unknown, previous_finished = {}, 0, 0, 0, False, None
    for index, attempt in enumerate(attempts):
        operation, op = operations[index], operations[index]["op"]
        observed = index < len(rows)
        _keys(attempt, {"index", "op", "status", "stage", "started_ms", "finished_ms", "errors", "module_before", "module_after",
            "selections", "source", "mapping_before", "mapping_after", "pointee_mapping", "read", "matched", "truncated"})
        _need(type(attempt["index"]) is int and attempt["index"] == index and attempt["op"] == op and
              attempt["status"] == ("observed" if observed else "incomplete") and
              type(attempt["stage"]) is str and attempt["stage"] in STAGES, "raw attempt order/disposition differs")
        start = _time(attempt["started_ms"])
        end = None if attempt["finished_ms"] is None else _time(attempt["finished_ms"])
        _need(end is None or start <= end, "raw attempt time order differs")
        _need(index == 0 or previous_finished is not None and previous_finished <= start,
              "raw operation attempts are not in actual serial order")
        previous_finished = end
        errors = attempt["errors"]
        _need(type(errors) is list and len(errors) <= len(STAGES), "bounded raw error journal required")
        stages = set()
        for error in errors:
            _keys(error, {"stage", "error"})
            _need(type(error["stage"]) is str and error["stage"] in STAGES and error["stage"] not in stages,
                  "raw error stage is unknown or repeated")
            stages.add(error["stage"]); _error(error["error"])
        if observed:
            _keys(rows[index], {"index", "op", "status", "inspection_index"})
            _need(type(rows[index]["index"]) is int and rows[index]["index"] == index and rows[index]["op"] == op and
                  rows[index]["status"] == "ok" and type(rows[index]["inspection_index"]) is int and
                  rows[index]["inspection_index"] == index and end is not None and not errors and attempt["stage"] == "complete",
                  "raw successful row lost its exact observed attempt")
        else: _need(bool(errors) and attempt["stage"] == errors[0]["stage"], "incomplete raw attempt needs its actual first failure")
        selections = attempt["selections"]
        _need(type(selections) is list and len(selections) <= (operation["limit"] if op == "memory_ranges" else 1), "bounded raw selection list required")
        for span in selections: _span(span, mappings)
        source_entry = None
        if op in {"memory_bytes", "memory_pointer_range"}:
            source_entry = _source(operation["from"], aliases)
            if attempt["source"] is not None:
                _need(source_entry is not None and _same(attempt["source"], source_entry["span"]), "raw read adopted another span")
                _span(attempt["source"], mappings)
            if observed: _need(attempt["source"] is not None, "raw observation omitted its selected source")
        else: _need(attempt["source"] is None, "range acquisition cannot carry a read source")
        claims = module_claims(operation)
        claim = claims[0] if claims else source_entry["claim"] if source_entry is not None else None
        original_stamp = source_entry["module"] if source_entry is not None else None
        for phase in ("module_before", "module_after"):
            if attempt[phase] is not None:
                _need(claim is not None, "unbound memory span acquired module authority")
                _module(attempt[phase], claim)
        if observed and claim is not None:
            _need(attempt["module_before"] is not None and _same(attempt["module_before"], attempt["module_after"]) and
                  (original_stamp is None or _same(original_stamp, attempt["module_before"])), "memory module metadata changed or went unchecked")
        if claim is None: _need(attempt["module_before"] is None and attempt["module_after"] is None, "raw unbound span cannot inherit module ownership")
        positions = {}
        for key in ("mapping_before", "mapping_after", "pointee_mapping"):
            if attempt[key] is not None:
                _integer(attempt[key], 0, len(mappings) - 1); positions[key] = attempt[key]
        if op == "memory_ranges": current = [span["mapping_index"] for span in selections]
        elif op == "memory_range": current = [positions["mapping_before"]] if "mapping_before" in positions else []
        else: current = [positions[key] for key in ("mapping_before", "mapping_after", "pointee_mapping") if key in positions]
        _need(current == list(range(cursor, cursor + len(current))), "mapping roles must be fresh, ordered and retained exactly once")
        for position in current:
            _need(start <= mappings[position]["at_ms"] and (end is None or mappings[position]["at_ms"] <= end), "mapping time escapes its actual attempt")
        cursor += len(current)
        _need(len(current) <= (operation["limit"] if op == "memory_ranges" else 1 if op == "memory_range" else 2 if op == "memory_bytes" else 3),
              "operation exceeded reserved mapping observations")
        if op in {"memory_ranges", "memory_range"}:
            _need(attempt["read"] is None and attempt["mapping_after"] is None and attempt["pointee_mapping"] is None,
                  "range acquisition cannot invent a read")
            module = attempt["module_before"]
            if op == "memory_ranges":
                _need(attempt["mapping_before"] is None, "range inventory has no separate before mapping")
                if attempt["matched"] is not None: _integer(attempt["matched"], len(selections))
                _need(attempt["truncated"] is None or type(attempt["truncated"]) is bool, "typed inventory truncation required")
                if observed:
                    _need(type(attempt["matched"]) is int and len(selections) == min(attempt["matched"], operation["limit"]) and
                          attempt["truncated"] is (attempt["matched"] > len(selections)), "inventory cardinality/truncation differs")
                for span in selections:
                    mapping = mappings[span["mapping_index"]]
                    first, last = _address(mapping["base"]), _address(mapping["base"]) + mapping["bytes"]
                    if claim is not None:
                        _need(module is not None, "module-clipped inventory lacks its stamp")
                        coverage = _module_coverage(_address(span["base"]), span["bytes"], module)
                        first, last = max(first, _address(coverage["base"])), min(last, _address(coverage["base"]) + coverage["bytes"])
                    _need(_address(span["base"]) == first and span["bytes"] == last - first, "inventory span exceeds its exact clipped mapping")
            else:
                _need(attempt["matched"] is None and attempt["truncated"] is None, "single range has no inventory cardinality")
                if observed: _need(len(selections) == 1 and attempt["mapping_before"] is not None, "single range observation omitted its selection")
                if selections:
                    span = selections[0]
                    _need(module is not None and span["mapping_index"] == attempt["mapping_before"] and
                          _address(span["base"]) == _address(module["base"]) + int(operation["rva"], 16) and
                          span["bytes"] == operation["bytes"],
                          "selected range differs from pinned module/RVA/extent")
                    _module_coverage(_address(span["base"]), span["bytes"], module)
            if observed:
                entries = [{"span": copy.deepcopy(span), "claim": copy.deepcopy(claim), "module": copy.deepcopy(module)} for span in selections]
                aliases[operation["as"]] = {"spans": entries} if op == "memory_ranges" else entries[0]
            continue
        _need(attempt["matched"] is None and attempt["truncated"] is None, "memory read cannot carry inventory metadata")
        if op == "memory_bytes":
            _need(not selections and attempt["pointee_mapping"] is None, "raw byte read cannot introduce another span")
        read = attempt["read"]
        if read is None:
            _need(not observed and errors[0]["stage"] in {"validate", "source", "module_before", "mapping_before", "charge"},
                  "post-issuance memory failure cannot erase its actual read journal")
        if read is not None:
            _read(read, operation)
            _need(attempt["source"] is not None, "raw read omitted its alias source")
            width = read["requested_bytes"]
            _need(read["offset"] + width <= attempt["source"]["bytes"], "raw read exceeds its observed selected span")
            if read["issued"]:
                before_index = attempt["mapping_before"]
                _need(before_index is not None and _same(_mapping_data(mappings[before_index]),
                      _mapping_data(mappings[attempt["source"]["mapping_index"]])), "read issued after source mapping changed")
                _inside(_address(attempt["source"]["base"]) + read["offset"], width, mappings[before_index])
                if original_stamp is not None: _need(_same(original_stamp, attempt["module_before"]), "read issued after source module changed")
                charged += width
            if read["returned"]: returned += read["returned_bytes"]
            unknown |= read["unknown_partial"]
        if "read" in stages:
            _need(read is not None and read["issued"] is True and read["unknown_partial"] is True,
                  "read failure lost its actual issued/unknown disposition")
        if "pointee" in stages:
            _need(op == "memory_pointer_range" and read is not None and read["issued"] and read["returned"] and
                  not read["unknown_partial"] and read["returned_bytes"] == POINTER_BYTES and
                  attempt["mapping_before"] is not None and attempt["mapping_after"] is not None and
                  _same(_mapping_data(mappings[attempt["mapping_before"]]), _mapping_data(mappings[attempt["mapping_after"]])),
                  "pointee failure cannot invent a zero/unobserved pointer-slot read")
        if observed:
            _need(read is not None and read["issued"] and read["returned"] and not read["unknown_partial"] and
                  attempt["mapping_before"] is not None and attempt["mapping_after"] is not None and
                  attempt["mapping_before"] < attempt["mapping_after"] and
                  _same(_mapping_data(mappings[attempt["mapping_before"]]), _mapping_data(mappings[attempt["mapping_after"]])),
                  "raw observation lacks exact full return and unchanged pre/post mapping")
        if op == "memory_pointer_range":
            if observed: _need(len(selections) == 1 and attempt["pointee_mapping"] is not None, "pointer observation omitted destination mapping")
            if selections:
                _need(read is not None and read["returned"] and not read["unknown_partial"] and read["returned_bytes"] == POINTER_BYTES,
                      "pointer destination was not derived from one retained full slot")
                pointer = int.from_bytes(bytes.fromhex(read["bytes_hex"]), "little")
                _need(pointer > 0 and _address(selections[0]["base"]) == pointer and selections[0]["bytes"] == operation["pointee_bytes"] and
                      selections[0]["mapping_index"] == attempt["pointee_mapping"], "raw pointer selection changed the observed bits or extent")
            if observed: aliases[operation["as"]] = {"span": copy.deepcopy(selections[0]), "claim": None, "module": None}
    _need(cursor == len(mappings), "raw journal carries unclaimed mapping observations")
    for key, actual in (("charged_bytes", charged), ("known_returned_bytes", returned)):
        _need(type(journal[key]) is int and journal[key] == actual and actual <= budget["read_bytes_reserved"], "raw byte disposition counters differ")
    _need(journal["actual_read_bytes"] is None if unknown else type(journal["actual_read_bytes"]) is int and journal["actual_read_bytes"] == returned,
          "unknown partial read cannot become an invented actual byte count")
    if result["status"] == "completed":
        _need(len(rows) == len(operations) and len(attempts) == len(rows) and result.get("error") is None and
              result.get("handles_expired") is True and journal["aliases_expired"] is True and
              result.get("native_state_uncertain") is False and (not worker or result.get("attached") is True and
              _same(result.get("cleanup"), {"script_unloaded": True, "detached": True}) and _same(result.get("cleanup_errors"), [])),
              "completed raw observation lacks full clean current-worker disposition")
    return copy.deepcopy(journal)
