"""VM-only complete backend-state observations, separate from task actors.

A pinned declaration supplies the read-only/application-state interpretation;
field names or getter encodings cannot establish it. Observations compare full
typed values with an independently pinned reference, but never create a task
proof. GUI observers and their unlocked-console gate are unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from . import rea_mapping as R
from . import rea_runtime_bridge as B

SCHEMA = "anyuse.rea-state-observer/1"
CONTRACT = "anyuse.rea-state-observer-contract/1"
OBSERVATION = "anyuse.rea-state-observation/1"
MAX_BYTES = 4 * 1024**2
MAX_FIELDS = 16
SCALARS = {"utf8", "bool", "int8", "uint8", "int16", "uint16", "int32", "uint32",
           "int64", "uint64", "long", "ulong", "float", "double"}


class Unsupported(ValueError):
    pass


class Ambiguous(ValueError):
    pass


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("observer data has missing or unknown fields")


def _text(value, maximum=4096, *, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and "\0" not in value and len(value.encode("utf-8")) <= maximum


def _path(value):
    if (not _text(value) or not Path(value).is_absolute() or ".." in Path(value).parts or
            str(Path(value)) != value):
        raise ValueError("observer paths must be explicit absolute paths")
    return value


def _binding(value, params, role):
    _keys(value, {"input"})
    name = value["input"]
    if name not in params or params[name]["role"] != role:
        raise ValueError("observer binding does not match its declared independent path role")


def validate(schema):
    """Pure schema validation; no file access, runtime import or native calls."""
    _keys(schema, {"format", "kind", "target", "params", "fields", "reference", "contract"}, {"request"})
    if schema["format"] != SCHEMA or schema["kind"] not in {"file_state", "runtime_state"}:
        raise Unsupported("unsupported state observer kind")
    target = schema["target"]
    _keys(target, {"bundle_id", "version", "build", "sha256", "files", "bytes"})
    if (not _text(target["bundle_id"]) or not R.SHA.fullmatch(str(target["sha256"])) or
            not isinstance(target["version"], str) or not isinstance(target["build"], str) or
            any(type(target[key]) is not int or target[key] < 0 for key in ("files", "bytes"))):
        raise ValueError("observer must pin the complete target fingerprint")
    if not isinstance(schema["params"], list) or not 1 <= len(schema["params"]) <= 32:
        raise ValueError("observer parameters exceed their budget")
    params = {}
    for param in schema["params"]:
        _keys(param, {"name", "type", "role"})
        if (not isinstance(param["name"], str) or not R.NAME.fullmatch(param["name"]) or param["name"] in params or
                param["type"] not in {"path", "sha256", "string"} or
                param["role"] not in {"working_output", "independent_reference", "reference_hash", "task_input"} or
                param["role"] in {"working_output", "independent_reference"} and param["type"] != "path" or
                param["role"] == "reference_hash" and param["type"] != "sha256"):
            raise ValueError("observer parameters require exact types and distinct path roles")
        params[param["name"]] = param
    reference = schema["reference"]
    _keys(reference, {"file", "sha256", "format"})
    _binding(reference["file"], params, "independent_reference")
    _binding(reference["sha256"], params, "reference_hash")
    if reference["format"] not in {"json", "utf8"}:
        raise Unsupported("expected state must be a complete JSON or UTF-8 reference")
    evidence = schema["contract"]
    _keys(evidence, {"file", "sha256", "pointer"})
    _path(evidence["file"])
    if not R.SHA.fullmatch(str(evidence["sha256"])) or not _text(evidence["pointer"]) or not evidence["pointer"].startswith("/"):
        raise ValueError("observer requires a pinned independent read-only state declaration")
    fields = schema["fields"]
    if not isinstance(fields, dict) or not 1 <= len(fields) <= MAX_FIELDS:
        raise ValueError("observer needs 1–16 complete named output fields")
    sources = set()
    request_fields = {}
    if schema["kind"] == "runtime_state":
        if "request" not in schema:
            raise ValueError("runtime observer needs an explicit read request")
        request = B.validate_request(schema["request"])
        for index, operation in enumerate(request["operations"]):
            if operation["op"] == "swift_read":
                raise Unsupported("current Swift read exposes an opaque pointer, not a stable task-state value")
            if operation["op"] not in {"read", "native_read"}:
                raise Unsupported("state observers accept explicit reads, never invocation, helper loading or actor results")
            for field in operation["fields"]:
                if field["type"] not in SCALARS or "alias" in field:
                    raise Unsupported("opaque objects, pointers and handles cannot be stable expected state")
                if operation["op"] == "read" and (field["kind"] == "getter" and "signature" not in field or
                                                      field["kind"] == "ivar" and "encoding" not in field):
                    raise ValueError("state fields require their exact declared signature or encoding")
                request_fields[(index, field["name"])] = field["type"]
    elif "request" in schema:
        raise ValueError("file observer cannot carry runtime operations")
    for name, field in fields.items():
        if not isinstance(name, str) or not R.NAME.fullmatch(name):
            raise ValueError("observer output names must be exact and unique")
        _keys(field, {"type", "source"})
        if schema["kind"] == "file_state":
            if field["type"] not in {"utf8", "json"}:
                raise Unsupported("file observer preserves whole UTF-8 text or JSON values")
            _binding(field["source"], params, "working_output")
        else:
            _keys(field["source"], {"operation", "field"})
            source = field["source"]
            if type(source["operation"]) is not int or not isinstance(source["field"], str):
                raise ValueError("state source must select an exact declared operation/field")
            key = source["operation"], source["field"]
            if key in sources or request_fields.get(key) != field["type"]:
                raise ValueError("state source must cover one exact typed runtime field")
            sources.add(key)
    if schema["kind"] == "runtime_state" and sources != set(request_fields):
        raise ValueError("observer cannot discard declared runtime fields")
    if reference["format"] == "utf8" and (len(fields) != 1 or next(iter(fields.values()))["type"] != "utf8"):
        raise ValueError("plain-text expected state requires one complete text field")
    return copy.deepcopy(schema)


def bind(schema, inputs):
    """Bind data only. References keep an explicit frozen SHA input per case."""
    schema = validate(schema)
    if not isinstance(inputs, dict) or set(inputs) != {param["name"] for param in schema["params"]}:
        raise ValueError("observer inputs must match its frozen parameter names")
    for param in schema["params"]:
        value = inputs[param["name"]]
        if param["type"] == "path":
            _path(value)
        elif param["type"] == "sha256":
            if not isinstance(value, str) or not R.SHA.fullmatch(value):
                raise ValueError("reference hash must be a frozen SHA-256")
        elif not _text(value, empty=True):
            raise ValueError("invalid bounded task input")
    schema["reference"]["file"] = inputs[schema["reference"]["file"]["input"]]
    schema["reference"]["sha256"] = inputs[schema["reference"]["sha256"]["input"]]
    if schema["kind"] == "file_state":
        for field in schema["fields"].values():
            field["source"] = inputs[field["source"]["input"]]
    return schema


def declaration(schema):
    """Exact interpretation to be reviewed/pinned independently of the actor."""
    schema = validate(schema)
    return {"format": CONTRACT, "kind": schema["kind"], "target": schema["target"],
            "fields": schema["fields"], "request": schema.get("request"),
            "request_sha256": R.digest(schema["request"]) if "request" in schema else None,
            "purpose": "complete_application_state", "read_only": True,
            "independent_of_actor": True, "input_echo_or_helper_completion": False}


def _read(path, *, owned=True):
    path = Path(_path(str(path)))
    before = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(before.st_mode) or
            owned and before.st_uid != os.getuid() or before.st_size > MAX_BYTES):
        raise ValueError("state files must be canonical regular guest-owned non-symlinks within the byte budget")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        opened = os.fstat(stream.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError("state file changed before capture")
        data = stream.read(MAX_BYTES + 1)
        if len(data) != opened.st_size or identity(os.fstat(stream.fileno())) != identity(opened) or identity(path.lstat()) != identity(opened):
            raise ValueError("state file changed during capture")
    return data, {"file": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}, (opened.st_dev, opened.st_ino)


def _json(raw):
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValueError("duplicate JSON state key")
            value[key] = item
        return value
    def invalid(_value):
        raise ValueError("nonfinite JSON state value")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


def _value(value, kind):
    if kind == "json":
        if len(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()) > MAX_BYTES:
            raise ValueError("complete JSON state exceeds its budget")
    elif kind == "utf8":
        if not _text(value, MAX_BYTES, empty=True):
            raise ValueError("state text is missing, partial or invalid")
    elif kind in SCALARS:
        B._value({"type": kind, "value": value})
        if kind in {"float", "double"}:
            value = float(value)
    else:
        raise Unsupported("unsupported substantive state value")
    return value


def _material(schema, inputs, *, backend=None):
    R.assert_guest()
    original, bound = validate(schema), bind(schema, inputs)
    immutable, roles, inodes = {}, {}, {}
    for param in original["params"]:
        if param["type"] != "path":
            continue
        path = inputs[param["name"]]
        data, pin, inode = _read(path, owned=param["role"] == "working_output")
        inodes[param["name"]] = inode
        roles[param["name"]] = {"role": param["role"], "file": path}
        if param["role"] == "working_output":
            roles[param["name"]]["uid"] = os.getuid()
        if param["role"] != "working_output":
            immutable[path] = pin
    raw_contract, contract_pin, contract_inode = _read(original["contract"]["file"], owned=False)
    if contract_pin["sha256"] != original["contract"]["sha256"]:
        raise ValueError("independent observer declaration changed")
    if B._json_pointer(_json(raw_contract), original["contract"]["pointer"]) != declaration(original):
        raise ValueError("pinned declaration does not establish this exact application-state/read-only interpretation")
    immutable[contract_pin["file"]] = contract_pin
    working = [name for name, role in roles.items() if role["role"] == "working_output"]
    protected = [inodes[name] for name, role in roles.items() if role["role"] != "working_output"] + [contract_inode]
    if any(inodes[name] in protected for name in working):
        raise Ambiguous("working outputs and independent reference/input/declaration files overlap or alias")
    reference = bound["reference"]
    raw, pin, _ = _read(reference["file"], owned=False)
    if pin["sha256"] != reference["sha256"] or pin != immutable[reference["file"]]:
        raise ValueError("independent expected reference changed")
    expected = _json(raw) if reference["format"] == "json" else {next(iter(bound["fields"])): raw.decode("utf-8")}
    if not isinstance(expected, dict) or set(expected) != set(bound["fields"]):
        raise ValueError("independent expected state must cover every full named field exactly")
    expected = {name: _value(expected[name], field["type"]) for name, field in bound["fields"].items()}
    native = B.request_resources(bound["request"]) if bound["kind"] == "runtime_state" else {}
    for path, native_pin in native.items():
        if path in immutable and immutable[path] != native_pin:
            raise ValueError("conflicting observer resource pins")
        immutable[path] = native_pin
        info = Path(path).stat()
        if any(inodes[name] == (info.st_dev, info.st_ino) for name in working):
            raise Ambiguous("working outputs alias a pinned native observer resource")
    pinned = {"immutable": immutable, "path_roles": roles}
    if bound["kind"] == "runtime_state":
        pinned["runtime_adapter"] = B.adapter_identity(backend=backend)
        pinned["bridge_source_sha256"] = R._file_hash(Path(B.__file__))
    return bound, expected, pinned


def resources(schema, inputs, *, backend=None):
    R.assert_guest()
    return _material(schema, inputs, backend=backend)[2]


def _same(left, right):
    return json.dumps(left, ensure_ascii=False, sort_keys=True, allow_nan=False) == json.dumps(right, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _native_values(bound, receipt):
    result = receipt.get("result") or {}
    rows = result.get("operations")
    if not isinstance(rows, list) or len(rows) != len(bound["request"]["operations"]):
        raise ValueError("native observer is missing complete operation results")
    values = {}
    for index, (operation, row) in enumerate(zip(bound["request"]["operations"], rows)):
        if (not isinstance(row, dict) or type(row.get("index")) is not int or
                row["index"] != index or row.get("op") != operation["op"] or row.get("status") != "ok"):
            raise ValueError("native observer result is not bound to the exact read request")
        fields = row.get("fields")
        if not isinstance(fields, list) or len(fields) != len(operation["fields"]):
            raise ValueError("native observer returned partial fields")
        for declared, actual in zip(operation["fields"], fields):
            if (not isinstance(actual, dict) or actual.get("name") != declared["name"] or actual.get("type") != declared["type"] or
                    "value" not in actual or operation["op"] == "native_read" and actual.get("offset") != declared["offset"] or
                    operation["op"] == "read" and actual.get("kind") != declared["kind"]):
                raise ValueError("native observer field name/type/layout does not match its declaration")
            if operation["op"] == "read":
                metadata = actual.get("metadata") or {}
                if (declared["kind"] == "getter" and (metadata.get("signature") != declared["signature"] or metadata.get("selector") != declared["selector"]) or
                        declared["kind"] == "ivar" and (metadata.get("encoding") != declared["encoding"] or metadata.get("name") != declared["ivar"])):
                    raise ValueError("native read-back lost its exact getter/ivar metadata")
            values[(index, declared["name"])] = _value(actual["value"], declared["type"])
    return {name: values[(field["source"]["operation"], field["source"]["field"])] for name, field in bound["fields"].items()}


def _identity(value, app, target):
    """Complete process identity, distinct from an opaque runtime object token."""
    return (isinstance(value, dict) and set(value) == {"bundle_id", "bundle", "executable", "pid", "launch_time", "uid"} and
            type(value["pid"]) is int and value["pid"] > 0 and type(value["uid"]) is int and value["uid"] == os.getuid() and
            type(value["launch_time"]) in {int, float} and math.isfinite(value["launch_time"]) and value["launch_time"] > 0 and
            value["bundle"] == str(app) and value["bundle_id"] == target["bundle_id"] and
            isinstance(value["executable"], str) and Path(value["executable"]).is_absolute() and
            Path(value["executable"]).is_relative_to(app))


def collect(bundle, schema, inputs, out, *, timeout=15, backend=None, before=None, require_change=False):
    """One independent observation; no actor calls, GUI readiness, retry or proof."""
    R.assert_guest()  # Before validation IO, output directories or runtime work.
    if type(timeout) is not int or not 1 <= timeout <= 60 or type(require_change) is not bool:
        raise ValueError("observer requires a typed bounded deadline/change rule")
    started = time.monotonic()
    receipt = {"format": OBSERVATION, "status": "unknown", "provider": None, "observer_kind": None,
               "target": None, "identity": None, "schema_sha256": None, "resources": None,
               "values": {}, "expected": None, "complete": False, "matches": None,
               "cleanup": None, "runtime_receipt": None, "resource_review_required": False,
               "evidence_only": True, "proof_created": False, "model_calls": 0, "error": None}
    folder = None
    try:
        checked = validate(schema)
        receipt.update(schema_sha256=R.digest(bind(schema, inputs)), observer_kind=checked["kind"],
                       provider="working_file" if checked["kind"] == "file_state" else "frida_native_read")
        app = Path(bundle).resolve(strict=True)
        target = receipt["target"] = R.fingerprint(app)
        if target != checked["target"]:
            raise ValueError("observer target fingerprint changed")
        proposed = Path(out).absolute()
        if proposed.resolve() != proposed or proposed.is_relative_to(app):
            raise ValueError("state receipts must be canonical and outside the target app")
        proposed.mkdir(parents=True, mode=0o700, exist_ok=False)
        folder = proposed
        bound, expected, pinned = _material(schema, inputs, backend=backend)
        if (any(Path(path).is_relative_to(folder) for path in pinned["immutable"]) or
                any(Path(role["file"]).is_relative_to(folder) for role in pinned["path_roles"].values()) or
                any(Path(role["file"]).is_relative_to(app) for role in pinned["path_roles"].values() if role["role"] == "working_output")):
            raise ValueError("state receipts/working outputs must not overlap pinned app/reference evidence")
        receipt.update(resources=pinned, expected=expected)
        if bound["kind"] == "file_state":
            values, files = {}, {}
            for name, field in bound["fields"].items():
                raw, pin, _ = _read(field["source"])
                values[name] = _value(_json(raw) if field["type"] == "json" else raw.decode("utf-8"), field["type"])
                files[name] = pin
            receipt.update(values=values, files=files, identity={"kind": "on_disk_bundle", "bundle": str(app), "bundle_id": target["bundle_id"]}, cleanup={"read_handles_closed": True})
        else:
            receipt["resource_review_required"] = True
            native = B.collect(str(app), str(folder / "native"), bound["request"], timeout=timeout, backend=backend)
            result = native.get("result") or {}
            receipt.update(identity=native.get("identity"), cleanup=result.get("cleanup"), runtime_receipt=native.get("receipt"))
            identity = native.get("identity")
            clean = (native.get("status") == "observed" and native.get("resource_review_required") is False and
                     native.get("actuated") is False and native.get("target") == target and native.get("identity") == native.get("after_identity") and
                     _identity(identity, app, target) and
                     native.get("adapter") == pinned["runtime_adapter"] and
                     native.get("request") == bound["request"] and native.get("request_sha256") == R.digest(bound["request"]) and
                     native.get("native_resources") == B.request_resources(bound["request"]) and
                     result.get("status") == "completed" and result.get("attached") is True and
                     result.get("handles_expired") is True and result.get("native_state_uncertain") is False and
                     type(result.get("invocations")) is int and result["invocations"] == 0 and
                     type(result.get("helper_load_attempts", 0)) is int and result.get("helper_load_attempts", 0) == 0 and
                     type(result.get("native_calls")) is int and 0 <= result["native_calls"] <= 4096 and
                     result.get("request_sha256") == R.digest(bound["request"]) and
                     result.get("native_resources", {}) == B.request_resources(bound["request"]) and
                     result.get("cleanup") == {"script_unloaded": True, "detached": True})
            if not clean:
                if (native.get("status") == "unavailable" and native.get("resource_review_required") is False and
                        native.get("actuated") is False and result.get("attached") is False and
                        result.get("invocations") == 0 and result.get("native_calls") == 0):
                    receipt["resource_review_required"] = False
                message = str((native.get("error") or {}).get("message", "native observer completion/cleanup is unknown"))
                if ("unique live instance" in message and "at least 2" in message or
                        "unambiguous running target" in message):
                    receipt["status"] = "ambiguous"
                raise ValueError(message)
            receipt["resource_review_required"] = False
            receipt["values"] = _native_values(bound, native)
        if R.fingerprint(app) != target or _material(schema, inputs, backend=backend)[2] != pinned:
            raise ValueError("observer target/reference/resource identity changed during capture")
        if bound["kind"] == "file_state" and any(_read(pin["file"])[1] != pin for pin in receipt["files"].values()):
            raise ValueError("working output changed during the complete multi-field capture")
        if require_change:
            if (not isinstance(before, dict) or before.get("format") != OBSERVATION or before.get("complete") is not True or
                    before.get("status") not in {"observed", "refuted"} or
                    before.get("schema_sha256") != receipt["schema_sha256"] or before.get("target") != target or
                    before.get("resources") != pinned or before.get("expected") != expected or
                    before.get("resource_review_required") is not False or before.get("identity") != receipt["identity"] or
                    not isinstance(before.get("values"), dict) or set(before["values"]) != set(bound["fields"])):
                raise ValueError("change verification needs a complete same-contract independent before observation")
            for name, field in bound["fields"].items():
                _value(before["values"][name], field["type"])
            if _same(before.get("values"), expected):
                receipt["error"] = {"type": "NoOp", "message": "expected state was already present before the actor"}
        receipt["complete"] = True
        receipt["matches"] = _same(receipt["values"], expected) and receipt["error"] is None
        receipt["status"] = "observed" if receipt["matches"] else "refuted"
    except Exception as error:
        if isinstance(error, Unsupported):
            receipt["status"] = "unsupported"
        elif isinstance(error, Ambiguous):
            receipt["status"] = "ambiguous"
        elif receipt["status"] not in {"ambiguous", "unsupported"}:
            receipt["status"] = "unknown"
        receipt.update(complete=False, matches=None, error={"type": type(error).__name__, "message": str(error)[:4096]})
    receipt["elapsed_s"] = round(time.monotonic() - started, 3)
    if folder is not None:
        receipt["receipt"] = str(folder / "observation.json")
        R.write_json(folder / "observation.json", receipt)
    return receipt
