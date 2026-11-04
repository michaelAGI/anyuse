"""Learn declarative invocation/output recipes from recordings and test in a VM.

No app names, bundle identifiers, handler tables, shell snippets or generated
Python occur in the interpreter. A reviewed interface recording supplies facts;
an independent reference supplies the task's meaning. Learning creates a
candidate, never a proof. Fresh live checks are required before replay.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from . import rea_mapping as R
from . import rea_apple_event as AE
from . import rea_native_path as NP

BACKEND = "guest_learned_ax_v1"
SVG_BACKEND = "guest_learned_svg_v1"
STATE_BACKEND = "guest_fixed_state_v1"
SVG_REFERENCE = "file_json_string_list"
PROFILE = "anyuse.rea-harness-profile/1"
ATTRS = {"AXValue", "AXTitle"}
NORMALIZATIONS = {"exact", "compact"}
OBJC_KINDS = {"objc_method", "objc_sequence"}
NATIVE_RUNTIME_KINDS = OBJC_KINDS | {"native_function", "runtime_sequence", "qt_moc_method"}
INSPECTOR_KINDS = {"inspector_method"}
RUNTIME_KINDS = NATIVE_RUNTIME_KINDS | INSPECTOR_KINDS
OBJC_BINDINGS = {"utf8", "bool", "int8", "uint8", "int16", "uint16", "int32", "uint32",
                 "int64", "uint64", "long", "ulong"}
SCALAR_ROLES = {"decimal": {"AXSlider", "AXProgressIndicator"}, "boolean": {"AXCheckBox"}}
SCALAR_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,307})(?:\.[0-9]{1,308})?(?:[eE][+-]?[0-9]{1,3})?\Z")


def _code(value):
    return isinstance(value, str) and len(value) == 4 and value.isascii() and value.isprintable()


def _text(value, limit=4096):
    return isinstance(value, str) and "\0" not in value and len(value.encode("utf-8")) <= limit


def _params(params):
    if not isinstance(params, list) or not 1 <= len(params) <= 16:
        raise ValueError("need 1–16 typed task parameters")
    names = set()
    for p in params:
        if (not isinstance(p, dict) or not {"name", "type", "source"} <= set(p) or set(p) - {"name", "type", "source", "mutable"} or
                not R.NAME.fullmatch(str(p.get("name", ""))) or p["name"] in names or
                p["type"] not in {"string", "path"} or p["source"] not in {"lookup", "text", "path", "name"} or
                "mutable" in p and (type(p["mutable"]) is not bool or p["type"] != "path")):
            raise ValueError("invalid typed task parameter")
        names.add(p["name"])
    return names


def _scalar_decimal(value):
    """Exact finite decimal conversion; no binary float rounding or locale guess."""
    if not _text(value, 1024) or SCALAR_DECIMAL.fullmatch(value) is None:
        raise ValueError("scalar decimal must be bounded explicit finite decimal text")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid scalar decimal") from exc
    if not number.is_finite():
        raise ValueError("scalar decimal must be finite")
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return number, "0" if number == 0 else canonical


def _scalar_reference_spec(field, params):
    """Staged scalar schema; not a live observer/learner capability yet."""
    _params(params)
    required = {"kind", "param", "scalar_type", "unit"}
    if (not isinstance(field, dict) or not required <= set(field) or
            field.get("kind") != "file_scalar" or not isinstance(field.get("scalar_type"), str) or
            field["scalar_type"] not in SCALAR_ROLES or not isinstance(field.get("unit"), str)):
        raise ValueError("invalid staged scalar reference contract")
    parameter = next((p for p in params if p["name"] == field["param"]), None)
    if parameter is None or parameter["type"] != "path" or parameter.get("mutable", False):
        raise ValueError("scalar oracle must be an explicit immutable path parameter")
    if field["scalar_type"] == "boolean":
        if set(field) != required or field["unit"] != "boolean":
            raise ValueError("boolean scalar requires boolean unit and no numeric bounds")
    else:
        if set(field) != required | {"min", "max"} or field["unit"] not in {"number", "percent", "ratio"}:
            raise ValueError("decimal scalar requires explicit supported unit and finite bounds")
        lower, minimum = _scalar_decimal(field["min"])
        upper, maximum = _scalar_decimal(field["max"])
        if minimum != field["min"] or maximum != field["max"] or lower > upper:
            raise ValueError("scalar bounds must be ordered canonical decimals")
        limit = {"percent": Decimal(100), "ratio": Decimal(1)}.get(field["unit"])
        if limit is not None and (lower < 0 or upper > limit):
            raise ValueError("scalar bounds exceed their declared unit")
    return copy.deepcopy(field)


def _scalar_value(value, field, *, observation=False):
    if not _text(value, 4096):
        raise ValueError("scalar value must be bounded text")
    text = value.strip()
    if field["scalar_type"] == "boolean":
        if text in {"true", "false"}:
            return text
        if observation and text in {"0", "1"}:
            return "true" if text == "1" else "false"
        raise ValueError("boolean oracle requires true/false; observed mixed state is unsupported")
    number, canonical = _scalar_decimal(text)
    if not Decimal(field["min"]) <= number <= Decimal(field["max"]):
        raise ValueError("scalar value exceeds the declared property range")
    return canonical


def _scalar_reference_file(field, params, inputs):
    """Prepare an independent scalar oracle without actuating or making a proof."""
    R.assert_guest()
    field = _scalar_reference_spec(field, params)
    _inputs(params, inputs)
    path = Path(inputs[field["param"]])
    if (path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path or
            path.stat().st_size > 4096):
        raise ValueError("scalar oracle must be a bounded canonical regular UTF-8 file")
    raw = path.read_bytes()
    return _scalar_value(raw.decode("utf-8"), field), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _binding(binding, names):
    if not isinstance(binding, dict) or len(binding) != 1:
        raise ValueError("bindings contain one input name or constant")
    if set(binding) == {"input"} and binding["input"] in names:
        return
    if set(binding) == {"constant"} and _text(binding["constant"]):
        return
    raise ValueError("unsupported parameter binding")


def _objc_actor(actor, params):
    """Validate shared runtime facts and bindings without attaching a process."""
    from . import rea_runtime_bridge as B
    methods = B.actor_methods(actor, allow_bindings=True)
    names = _params(params)
    for method in methods:
        for argument in method["arguments"]:
            value = argument["value"]
            if isinstance(value, dict) and (set(value) != {"input"} or value["input"] not in names or argument["type"] not in OBJC_BINDINGS):
                raise ValueError("runtime bindings require a frozen input and an explicitly supported scalar type")
    return copy.deepcopy(actor)


def _runtime_actor(actor, params):
    """Validate callable recipes, including every nested frozen input name."""
    from . import rea_runtime_providers as P
    result = P.for_actor(actor).validate_actor(actor, allow_bindings=True)
    names = _params(params)
    if actor.get("kind") in INSPECTOR_KINDS:
        for argument in result["arguments"]:
            value = argument["value"]
            if isinstance(value, dict) and set(value) == {"input"} and value["input"] not in names:
                raise ValueError("inspector argument binding must name a frozen task input")
        return result
    if actor.get("kind") in OBJC_KINDS:
        return _objc_actor(actor, params)
    from . import rea_runtime_bridge as B
    for call in B.actor_calls(result, allow_bindings=True):
        if call["kind"] == "objc_method":
            _objc_actor(call, params)
        elif call["kind"] == "qt_moc_method":
            # Qt selection and constructor-observed receiver are frozen data;
            # there are no scalar actor inputs or inferred binding leaves.
            continue
        else:
            arguments = [*call["arguments"], *([call["context"]] if "context" in call else [])]
            for argument in arguments:
                for leaf in B.argument_leaves(argument):
                    value = leaf["value"]
                    if isinstance(value, dict) and set(value) == {"input"} and value["input"] not in names:
                        raise ValueError("native argument binding must name a frozen task input")
    return result


def _reference(spec, names, params):
    if not isinstance(spec, dict) or set(spec) - {"fields", "window_path_param"}:
        raise ValueError("invalid independent reference contract")
    fields = spec.get("fields")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 16:
        raise ValueError("reference requires named output fields")
    substantive = False
    for name, field in fields.items():
        if not R.NAME.fullmatch(name) or not isinstance(field, dict):
            raise ValueError("invalid reference field")
        if field.get("normalization", "exact") not in NORMALIZATIONS:
            raise ValueError("unknown text normalization")
        kind = field.get("kind")
        if kind in {"file_text", "fen_plist", "dictionary_xml", SVG_REFERENCE} and any(
                p["name"] == field.get("param") and p.get("mutable", False) for p in params):
            raise ValueError("mutable working paths cannot be substantive task references")
        allowed = {"kind", "normalization"}
        if kind in {"input", "basename", "file_identity", "file_text", "fen_plist", "dictionary_xml", SVG_REFERENCE}:
            allowed.add("param")
            if field.get("param") not in names:
                raise ValueError("reference input must be an explicit task parameter")
        if kind == "constant":
            allowed.add("value")
            if not _text(field.get("value"), 256 * 1024):
                raise ValueError("invalid constant reference")
        elif kind == "dictionary_xml":
            allowed |= {"dictionary_path", "dictionary_label", "dictionary_sha256", "reference_xml"}
            observer = {k: field.get(k) for k in allowed if k.startswith(("dictionary_", "reference_"))}
            # Reuse a file-format reader, rather than a Dictionary app driver.
            path, xml = observer["dictionary_path"], observer["reference_xml"]
            if (not _text(path) or not Path(path).is_absolute() or ".." in Path(path).parts or
                    Path(path).suffix != ".dictionary" or not _text(observer["dictionary_label"]) or
                    not R.SHA.fullmatch(str(observer["dictionary_sha256"])) or not _text(xml) or
                    not xml.startswith("Contents/Resources/") or not xml.endswith(".xml") or ".." in Path(xml).parts):
                raise ValueError("invalid pinned dictionary reference")
            substantive = True
        elif kind == SVG_REFERENCE:
            allowed = {"kind", "param"}
            substantive = True
        elif kind == "file_text":
            substantive = True
        elif kind == "fen_plist":
            allowed |= {"key", "labels"}
            if not _text(field.get("key"), 128) or not field["key"]:
                raise ValueError("position projection needs a bounded property-list key")
            from .rea_formats import fen_labels
            import plistlib
            # Validate projection data against a standard neutral position;
            # never derive the expected state from the actor's observation.
            fen_labels(plistlib.dumps({field.get("key", ""): "8/8/8/4k3/8/8/4K3/8 w - - 0 1"}),
                       field.get("key"), field.get("labels"))
            substantive = True
        elif kind not in {"input", "basename", "file_identity"}:
            raise ValueError("unknown independent reference source")
        if set(field) - allowed:
            raise ValueError("unknown reference options")
    if not substantive:
        raise ValueError("input echoes and labels alone cannot establish task success")
    if spec.get("window_path_param") is not None and spec["window_path_param"] not in names:
        raise ValueError("window scope must bind an explicit path parameter")
    paths = {p["name"] for p in params if p["type"] == "path"}
    if any(f["kind"] in {"basename", "file_identity", "file_text", "fen_plist", SVG_REFERENCE} and f["param"] not in paths for f in fields.values()):
        raise ValueError("file references require typed path inputs")
    p = spec.get("window_path_param")
    if p is not None and (p not in paths or not any(f["kind"] == "file_identity" and f["param"] == p for f in fields.values())):
        raise ValueError("document scope requires full independent file identity")


def _svg_template(c):
    """Pure, explicit SVG contract; no widening of the existing AX backend."""
    from . import rea_svg_observer as S
    observer = c.get("observer")
    fields = c.get("reference", {}).get("fields", {})
    if observer is None:
        if any(field.get("kind") == SVG_REFERENCE for field in fields.values()) or c.get("backend") == SVG_BACKEND:
            raise ValueError("ordered SVG references require the explicit SVG observer/backend")
        return None
    S._keys(observer, {"kind", "selector", "limits", "contract_param", "contract_pointer"}, {"namespace_mode"})
    if c.get("backend") not in {None, SVG_BACKEND}:
        raise ValueError("SVG observer cannot masquerade as the AX backend")
    # S owns the closed interpretation/mode pairs. This is pure syntax checking;
    # these placeholder pins are never read, collected or used as proof evidence.
    probe = {"format": S.SCHEMA, "kind": observer["kind"],
             "target": c.get("target", {"bundle_id": "rea.observer.template", "version": "", "build": "",
                                         "sha256": "0" * 64, "files": 1, "bytes": 1}),
             "selector": observer["selector"], "limits": observer["limits"],
             "reference": {"format": "json_string_list", "file": "/rea-independent-oracle.json", "sha256": "0" * 64},
             "contract": {"file": "/rea-independent-contract.json", "sha256": "0" * 64,
                          "pointer": observer["contract_pointer"]}}
    if "namespace_mode" in observer:
        probe["namespace_mode"] = observer["namespace_mode"]
    S.validate(probe)
    if (len(fields) != 1 or next(iter(fields.values()), {}).get("kind") != SVG_REFERENCE or
            set(next(iter(fields.values()))) != {"kind", "param"} or "window_path_param" in c["reference"]):
        raise ValueError("SVG observation needs one complete ordered JSON-list reference without AX fields or normalization")
    field = next(iter(fields.values()))
    params = {p["name"]: p for p in c["params"]}
    if not isinstance(observer["contract_param"], str) or not R.NAME.fullmatch(observer["contract_param"]):
        raise ValueError("SVG declaration must bind one explicit typed parameter")
    contract = params.get(observer["contract_param"])
    oracle = params.get(field["param"])
    if (contract is None or oracle is None or observer["contract_param"] == field["param"] or
            any(p["type"] != "path" or p.get("mutable", False) for p in (contract, oracle)) or
            not _text(observer["contract_pointer"], 1024) or not observer["contract_pointer"].startswith("/") or
            re.search(r"~(?![01])", observer["contract_pointer"])):
        raise ValueError("SVG oracle and observer declaration must be distinct immutable typed paths")
    if not set(params) - {observer["contract_param"], field["param"]}:
        raise ValueError("SVG mapping needs substantive task inputs separate from proof-file paths")
    actor = c.get("actor")
    if actor is not None:
        if not isinstance(actor, dict) or actor.get("kind") not in INSPECTOR_KINDS:
            raise ValueError("this SVG proof backend requires the independently checked inspector actor")
        forbidden = {observer["contract_param"], field["param"]}
        if any(isinstance(arg.get("value"), dict) and set(arg["value"]) == {"input"} and
               arg["value"]["input"] in forbidden for arg in actor.get("arguments", [])):
            raise ValueError("an actor cannot consume the independent SVG oracle or observer declaration")
    return copy.deepcopy(observer)


def semantic_inputs(c, inputs):
    """Proof-file names do not create distinct actuation examples."""
    observer = _svg_template(c)
    if observer is None:
        return copy.deepcopy(inputs)
    oracle = next(iter(c["reference"]["fields"].values()))["param"]
    return {name: value for name, value in inputs.items() if name not in {oracle, observer["contract_param"]}}


def _svg_schema(c, inputs, files):
    from . import rea_svg_observer as S
    observer = _svg_template(c)
    field = next(iter(c["reference"]["fields"].values()))
    expected_files = {p["name"] for p in c["params"] if p["type"] == "path" and not p.get("mutable", False)}
    if not isinstance(files, dict) or set(files) != expected_files:
        raise ValueError("SVG reference provenance must bind every immutable case path")
    for name, pin in files.items():
        S._keys(pin, {"file", "sha256", "bytes"})
        if (pin["file"] != inputs[name] or S._path(pin["file"]) != pin["file"] or
                not isinstance(pin["sha256"], str) or not R.SHA.fullmatch(pin["sha256"]) or
                type(pin["bytes"]) is not int or not 1 <= pin["bytes"] <= S.MAX_FILE_BYTES):
            raise ValueError("SVG immutable case file identity is malformed")
    oracle, contract = files[field["param"]], files[observer["contract_param"]]
    if oracle["file"] == contract["file"]:
        raise ValueError("SVG oracle and declaration cannot overlap")
    if any(pin["file"] in {oracle["file"], contract["file"]} for name, pin in files.items()
           if name not in {field["param"], observer["contract_param"]}):
        raise ValueError("independent SVG proof files cannot alias task input files")
    schema = {"format": S.SCHEMA, "kind": observer["kind"], "target": c["target"],
              "selector": observer["selector"], "limits": observer["limits"],
              "reference": {"format": "json_string_list", "file": oracle["file"], "sha256": oracle["sha256"]},
              "contract": {"file": contract["file"], "sha256": contract["sha256"], "pointer": observer["contract_pointer"]}}
    if "namespace_mode" in observer:
        schema["namespace_mode"] = observer["namespace_mode"]
    return S.validate(schema)


def _svg_case(c, inputs):
    """Freeze exact independent data/declaration before any actor dispatch."""
    R.assert_guest()
    from . import rea_svg_observer as S
    from . import rea_runtime_bridge as B
    _svg_template(c)
    _inputs(c["params"], inputs, reference=c["reference"])
    files, raw = {}, {}
    for param in c["params"]:
        if param["type"] == "path" and not param.get("mutable", False):
            value, pin, _ = S._read(inputs[param["name"]])
            raw[param["name"]], files[param["name"]] = value, pin
    schema = _svg_schema(c, inputs, files)
    observer = c["observer"]
    if B._json_pointer(S._json(raw[observer["contract_param"]]), observer["contract_pointer"]) != S.declaration(schema):
        raise ValueError("independent SVG declaration does not establish this exact case/observer")
    name, field = next(iter(c["reference"]["fields"].items()))
    values = S._values(S._json(raw[field["param"]]), observer["limits"])
    return {name: values}, {"schema": schema, "schema_sha256": R.digest(schema), "files": files}


def validate(candidate):
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    if candidate.get("backend") == STATE_BACKEND:
        from . import rea_state_campaign as C
        return C.validate(candidate)
    c = copy.deepcopy(candidate)
    if c.get("format") != R.CANDIDATE or c.get("backend") not in {BACKEND, SVG_BACKEND} or not R.NAME.fullmatch(str(c.get("id", ""))):
        raise ValueError("invalid learned candidate")
    if not _text(c.get("label")) or not c["label"]:
        raise ValueError("candidate needs a label")
    target = c.get("target", {})
    if not isinstance(target, dict) or not target.get("bundle_id") or not R.SHA.fullmatch(str(target.get("sha256", ""))):
        raise ValueError("candidate needs the full analysed bundle fingerprint")
    if c.get("effect") not in {"scoped_find", "read_content", "load_state"} or c.get("item_kind", "") not in {"", "document", "file"} or c.get("mode", ""):
        raise ValueError("this harness supports lookup, document reading and verified state loading")
    names = _params(c.get("params"))
    _reference(c.get("reference"), names, c["params"])
    svg = _svg_template(c)
    actor = c.get("actor", {})
    if not isinstance(actor, dict):
        raise ValueError("invocation primitive must be an object")
    if actor.get("kind") == "apple_event":
        if set(actor) != {"kind", "class", "id", "parameters"} or not all(_code(actor[k]) for k in ("class", "id")):
            raise ValueError("invalid typed event template")
        if not isinstance(actor["parameters"], dict) or not 1 <= len(actor["parameters"]) <= 16:
            raise ValueError("invalid event parameter table")
        for keyword, p in actor["parameters"].items():
            if not _code(keyword) or not isinstance(p, dict) or set(p) != {"type", "value"} or p["type"] != "utf8":
                raise ValueError("only typed UTF-8 event parameters are supported")
            _binding(p["value"], names)
    elif actor.get("kind") == "open_document":
        if set(actor) != {"kind", "path"} or set(actor["path"]) != {"input"}:
            raise ValueError("document opening must bind a task path")
        _binding(actor["path"], names)
        name = actor["path"]["input"]
        if not any(p["name"] == name and p["type"] == "path" for p in c["params"]):
            raise ValueError("document opening requires a typed path")
        if not any(f["kind"] in {"file_text", "fen_plist"} and f["param"] == name for f in c["reference"]["fields"].values()):
            raise ValueError("document opening requires independent read-back of the requested file")
        if not any(f["kind"] == "file_identity" and f["param"] == name for f in c["reference"]["fields"].values()):
            raise ValueError("document opening requires full independent file identity")
    elif actor.get("kind") in RUNTIME_KINDS:
        _runtime_actor(actor, c["params"])
    else:
        raise ValueError("unsupported invocation primitive")
    scope = c.get("scope")
    if scope is not None and (not isinstance(scope, dict) or set(scope) != {"source", "value"} or
                              scope["source"] != "path" or not _text(scope["value"])):
        raise ValueError("invalid explicit task scope")
    if svg is not None and (c["backend"] != SVG_BACKEND or "selectors" in c):
        raise ValueError("SVG candidates require their distinct backend and cannot carry AX selectors")
    selectors = {} if svg is not None else c.get("selectors")
    if svg is None and (not isinstance(selectors, dict) or set(selectors) != set(c["reference"]["fields"])):
        raise ValueError("every independent output field needs a learned selector")
    for name, s in selectors.items():
        if (not isinstance(s, dict) or set(s) - {"outermost"} != ({"where", "read", "grid"} if s.get("read") == "grid_labels" else {"where", "read"}) or
                not isinstance(s["where"], dict) or set(s["where"]) - {"role", "AXSubrole", "AXValue", "AXTitle"} or
                not _text(s["where"].get("role")) or not s["where"]["role"].startswith("AX") or
                any(not _text(v) for v in s["where"].values()) or
                s["read"] not in ATTRS | {"subtree_text", "document_path", "grid_labels"}):
            raise ValueError("invalid declarative AX selector")
        field = c["reference"]["fields"][name]
        if "outermost" in s and (s["outermost"] is not True or
                field["kind"] not in {"file_text", "dictionary_xml"} or
                (s["where"]["role"], s["read"]) != ("AXTextArea", "AXValue")):
            raise ValueError("outermost requires literal true and a complete AXTextArea text reference")
        if s["read"] == "grid_labels":
            grid = s["grid"]
            if (s["where"]["role"] not in {"AXGroup", "AXLayoutArea", "AXTable"} or not isinstance(grid, dict) or
                    set(grid) != {"child_role", "count"} or not _text(grid.get("child_role")) or
                    not grid["child_role"].startswith("AX") or type(grid.get("count")) is not int or not 1 <= grid["count"] <= 1024):
                raise ValueError("grid read-back needs a complete bounded group of uniformly typed cells")
        if s["read"] == "subtree_text" and s["where"]["role"] != "AXWebArea":
            raise ValueError("full subtree text requires an AXWebArea content root")
        if s["read"] == "document_path" and s["where"]["role"] != "AXWindow":
            raise ValueError("document identity requires an AXWindow document attribute")
        if "AXValue" in s["where"]:
            selected = s["where"]["role"] in {"AXRadioButton", "AXCheckBox"} and s["where"]["AXValue"] == "1"
            constant = (field["kind"] == "constant" and s["read"] == "AXValue" and
                        _normalize(s["where"]["AXValue"], field) == _normalize(field["value"], field))
            if not selected and not constant:
                raise ValueError("only selected-control state or the independent constant may constrain a value")
        if "AXTitle" in s["where"] and (field["kind"] != "constant" or s["read"] != "AXTitle" or
                _normalize(s["where"]["AXTitle"], field) != _normalize(field["value"], field)):
            raise ValueError("only the independently declared constant may constrain a title")
    for name, field in c["reference"]["fields"].items():
        if field["kind"] in {"file_text", "dictionary_xml"}:
            s = selectors[name]
            if (s["where"]["role"], s["read"]) not in {("AXWebArea", "subtree_text"), ("AXTextArea", "AXValue")}:
                raise ValueError("content references require complete text roots, not labels or control states")
        if field["kind"] == "fen_plist" and (c["selectors"][name]["read"] != "grid_labels" or c["selectors"][name]["grid"]["count"] != 64):
            raise ValueError("position references require every one of the 64 visible squares")
    citations = c.get("evidence", [])
    if not isinstance(citations, list) or not citations or any(
            not isinstance(e, dict) or not e.get("file") or not e.get("pointer") or not R.SHA.fullmatch(str(e.get("sha256", "")))
            for e in citations):
        raise ValueError("reviewed interface evidence is required")
    learned = c.get("learned_from", {})
    learned_fields = {"inputs", "recordings"} | ({"semantic_inputs"} if svg is not None else set())
    if (not isinstance(learned, dict) or set(learned) != learned_fields or
            any(not isinstance(learned[k], list) or len(set(learned[k])) < 3 or
                any(not R.SHA.fullmatch(str(v)) for v in learned[k]) for k in learned)):
        raise ValueError("need three distinct learning inputs and recordings")
    fields = ("format", "backend", "id", "label", "target", "effect", "item_kind", "mode",
              "params", "actor", "reference", "scope", "selectors", "observer", "evidence", "learned_from")
    return {k: c[k] for k in fields if k in c}


def _mutable_path(value):
    """Pin location/owner, not document bytes or an autosave's replacement inode."""
    file = Path(value)
    try:
        info = file.lstat()
        canonical = file.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("mutable working path is unavailable") from exc
    if (not file.is_absolute() or str(canonical) != str(value) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != os.getuid()):
        raise ValueError("mutable working path must be canonical, regular, non-symlink and owned by the guest user")
    return {"kind": "mutable_path", "canonical": str(canonical), "uid": info.st_uid}, (info.st_dev, info.st_ino)


def _path_roles(params, inputs, reference=None):
    if not any(p.get("mutable", False) for p in params):
        return
    mutable = []
    immutable = []
    cases = inputs if isinstance(inputs, list) else [inputs]
    for case in cases:
        for p in params:
            if p["type"] != "path":
                continue
            value = case[p["name"]]
            if p.get("mutable", False):
                identity, inode = _mutable_path(value)
                mutable.append((Path(identity["canonical"]), inode))
            else:
                immutable.append(Path(value))
        for field in (reference or {}).get("fields", {}).values():
            if field["kind"] in {"file_text", "fen_plist"}:
                immutable.append(Path(case[field["param"]]))
            elif field["kind"] == "dictionary_xml":
                immutable.append(Path(field["dictionary_path"]) / field["reference_xml"])
    if not mutable:
        return
    for file in immutable:
        canonical = file.resolve()
        try:
            info = file.stat()
            inode = (info.st_dev, info.st_ino)
        except FileNotFoundError:
            inode = None
        if any(canonical == target or inode is not None and inode == identity for target, identity in mutable):
            raise ValueError("mutable working paths and immutable reference/input files must not overlap or alias")


def _inputs(params, inputs, *, reference=None, check_files=True):
    if not isinstance(inputs, dict) or set(inputs) != {p["name"] for p in params}:
        raise ValueError("inputs must match the learned task parameters")
    for p in params:
        value = inputs[p["name"]]
        if not _text(value) or not value.strip() or (p["type"] == "path" and not Path(value).is_absolute()):
            raise ValueError("invalid bounded text or absolute path input")
    if check_files and any(p.get("mutable", False) for p in params):
        _path_roles(params, inputs, reference)


def bind(c, inputs, *, check_files=True):
    if c.get("backend") == STATE_BACKEND:
        from . import rea_state_campaign as C
        return C.bind(c, inputs)
    _inputs(c["params"], inputs, reference=c.get("reference"), check_files=check_files)
    def value(binding):
        return inputs[binding["input"]] if "input" in binding else binding["constant"]
    actor = c["actor"]
    if actor["kind"] == "open_document":
        return {"kind": "open_document", "path": value(actor["path"])}
    if actor["kind"] in RUNTIME_KINDS:
        from . import rea_runtime_providers as P
        _runtime_actor(actor, c["params"])
        return P.for_actor(actor).bind_actor(actor, inputs)
    return {"kind": "apple_event", "class": actor["class"], "id": actor["id"],
            "parameters": {k: {"type": p["type"], "value": value(p["value"])} for k, p in actor["parameters"].items()}}


def _normalize(value, field):
    value = unicodedata.normalize("NFKC", value)
    return "".join(value.split()) if field.get("normalization", "exact") == "compact" else value


_ABSENT_TRANSPORT = object()


def _runtime_transport(value=_ABSENT_TRANSPORT):
    """Validate public provenance; only old missing fields mean local attach."""
    if value is _ABSENT_TRANSPORT:
        return {"kind": "local"}
    if not isinstance(value, dict):
        raise ValueError("runtime transport provenance must be explicit data")
    if value.get("kind") == "local" and set(value) == {"kind"}:
        return copy.deepcopy(value)
    if value.get("kind") != "gadget" or set(value) != {"kind", "endpoint", "token_file", "library", "config"}:
        raise ValueError("unknown runtime transport provenance")
    endpoint = value["endpoint"]
    match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", endpoint) if isinstance(endpoint, str) else None
    if match is None or int(match[1]) > 65535:
        raise ValueError("Gadget provenance needs an exact loopback endpoint")
    for key in ("token_file", "library", "config"):
        file = value[key]
        expected = {"file", "sha256", "bytes"} | ({"uid", "mode"} if key != "library" else set())
        if (not isinstance(file, dict) or set(file) != expected or not _text(file.get("file")) or
                not Path(file["file"]).is_absolute() or ".." in Path(file["file"]).parts or
                not R.SHA.fullmatch(str(file.get("sha256", ""))) or type(file.get("bytes")) is not int or file["bytes"] < 1 or
                key != "library" and (type(file.get("uid")) is not int or file["uid"] < 0 or file.get("mode") != "0600")):
            raise ValueError("Gadget provenance needs pinned owned token/config and library files")
    return copy.deepcopy(value)


def _runtime_adapter(value):
    if not isinstance(value, dict):
        raise ValueError("runtime adapter identity must be data")
    result = copy.deepcopy(value)
    result["transport"] = _runtime_transport(value.get("transport", _ABSENT_TRANSPORT))
    return result


def _inspector_adapter(value):
    """Validate separately pinned inspector code and operator transport data."""
    if not isinstance(value, dict) or set(value) != {"adapter", "transport"}:
        raise ValueError("inspector resources need separate adapter and transport identities")
    from . import rea_inspector_bridge as I
    adapter, transport = value["adapter"], value["transport"]
    if (not isinstance(adapter, dict) or
            set(adapter) != {"file", "sha256", "fixed_templates_sha256", "protocol", "caller_javascript", "protocol_limits"} or
            not _text(adapter.get("file")) or not Path(adapter["file"]).is_absolute() or
            ".." in Path(adapter["file"]).parts or
            not R.SHA.fullmatch(str(adapter.get("sha256", ""))) or
            not R.SHA.fullmatch(str(adapter.get("fixed_templates_sha256", ""))) or
            adapter.get("protocol") != "chromium_renderer_cdp" or adapter.get("caller_javascript") is not False or
            not isinstance(adapter.get("protocol_limits"), dict) or
            any(type(limit) is not int for limit in adapter["protocol_limits"].values()) or
            adapter["protocol_limits"] != I._protocol_limits()):
        raise ValueError("inspector adapter must pin the fixed reviewed implementation")
    if (not isinstance(transport, dict) or
            not {"kind", "endpoint", "relative_url", "config"} <= set(transport) or
            set(transport) - {"kind", "endpoint", "relative_url", "config", "document_suffix"} or
            transport.get("kind") != "chromium_renderer_cdp"):
        raise ValueError("inspector transport provenance must be explicit data")
    I._endpoint(transport["endpoint"])
    if "document_suffix" in transport:
        I._document_suffix(transport["document_suffix"])
    relative = transport["relative_url"]
    if (not _text(relative) or not Path(relative).parts or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("inspector renderer must be a bounded app-relative document")
    config = transport["config"]
    if (not isinstance(config, dict) or set(config) != {"file", "sha256", "bytes", "uid", "mode"} or
            not _text(config.get("file")) or not Path(config["file"]).is_absolute() or
            ".." in Path(config["file"]).parts or not R.SHA.fullmatch(str(config.get("sha256", ""))) or
            type(config.get("bytes")) is not int or not 1 <= config["bytes"] <= 16384 or
            type(config.get("uid")) is not int or config["uid"] < 0 or config.get("mode") != "0600"):
        raise ValueError("inspector transport needs the pinned owned private configuration")
    return copy.deepcopy(value)


def _resources_equal(left, right):
    """Allow the prior local receipt schema without allowing route changes."""
    if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
        return False
    try:
        return all(_runtime_adapter(left[key]) == _runtime_adapter(right[key]) if key == "runtime_bridge" else
                   _inspector_adapter(left[key]) == _inspector_adapter(right[key]) if key == "inspector_bridge" else
                   NP.same_data(left[key], right[key]) for key in left)
    except (ValueError, TypeError):
        return False


def _native_file_claims(actor):
    """Enumerate frozen module/contract claims without reading guest files."""
    from . import rea_runtime_bridge as B
    return B.native_file_claims(actor)


def resources(c):
    identities = {}
    if _svg_template(c) is not None:
        from . import rea_svg_observer as S
        from . import rea_inspector_bridge as I
        identities["svg_observer"] = {"adapter": S.adapter_identity(),
                                      "transport": I.transport_identity_for_target(c["target"])}
    if c.get("actor", {}).get("kind") in INSPECTOR_KINDS:
        from . import rea_inspector_bridge as I
        from . import rea_runtime_providers as P
        identities["inspector_bridge"] = _inspector_adapter({"adapter": I.adapter_identity(),
            "transport": I.transport_identity_for_target(c["target"])})
        identities.update(P.for_actor(c["actor"]).resources(c["actor"]))
    elif c.get("actor", {}).get("kind") in NATIVE_RUNTIME_KINDS:
        from . import rea_runtime_bridge as B
        identities["runtime_bridge"] = _runtime_adapter(B.adapter_identity())
        if _native_file_claims(c["actor"]):
            identities.update(B.native_resources(c["actor"]))
    for f in c["reference"]["fields"].values():
        if f["kind"] == "dictionary_xml":
            identity = AE.dictionary_identity(f["dictionary_path"])
            if identity["sha256"] != f["dictionary_sha256"] or identity["label"] != f["dictionary_label"]:
                raise ValueError("independent dictionary resource changed")
            identities[f["dictionary_path"]] = identity
    return identities


def expected(c, inputs):
    """Read the reference before execution; never ask the actor what is correct."""
    R.assert_guest()
    bind(c, inputs)
    if _svg_template(c) is not None:
        return _svg_case(c, inputs)
    seen, sources = {}, {}
    for name, f in c["reference"]["fields"].items():
        kind = f["kind"]
        if kind == "constant":
            text = f["value"]
        elif kind == "input":
            text = inputs[f["param"]]
        elif kind == "basename":
            text = Path(inputs[f["param"]]).name
        elif kind == "file_identity":
            text = str(Path(inputs[f["param"]]).resolve(strict=True))
        elif kind in {"file_text", "fen_plist"}:
            path = Path(inputs[f["param"]])
            if path.is_symlink() or not path.is_file() or path.stat().st_size > R.MAX_JSON_BYTES:
                raise ValueError("reference must be a bounded regular UTF-8 file")
            raw = path.read_bytes()
            if kind == "file_text":
                text = raw.decode("utf-8")
            else:
                from .rea_formats import fen_labels
                text = fen_labels(raw, f["key"], f["labels"])
            sources[name] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        else:
            text = AE.reference({"observer": f}, inputs[f["param"]])
            sources[name] = {"path": f["dictionary_path"], "sha256": f["dictionary_sha256"], "xml": f["reference_xml"]}
        if not _text(text, 256 * 1024) or not text.strip():
            raise ValueError("empty or oversized independent expected output")
        seen[name] = _normalize(text, f)
    return seen, sources


def _rows(rows):
    if not isinstance(rows, list) or len(rows) > 2500:
        raise ValueError("AX observation exceeds the node budget")
    depths = []
    for i, row in enumerate(rows):
        if (not isinstance(row, dict) or not _text(row.get("role")) or
                (row.get("parent") is not None and (type(row["parent"]) is not int or not 0 <= row["parent"] < i)) or
                any(not _text(row[k], 256 * 1024) for k in ("AXTitle", "AXValue", "AXSubrole", "AXDocument") if k in row)):
            raise ValueError("invalid AX observation tree")
        depth = 0 if row.get("parent") is None else depths[row["parent"]] + 1
        if depth > 20:
            raise ValueError("AX observation exceeds the depth budget")
        depths.append(depth)
    return rows


def _indices(rows, selector, scope=None):
    def inside(i):
        while i is not None:
            if i in scope:
                return True
            i = rows[i].get("parent")
        return False
    found = [i for i, row in enumerate(rows) if (scope is None or inside(i)) and
             all(row.get(k) == v for k, v in selector["where"].items())]
    if selector.get("read") == "grid_labels":
        # Distinguish complete grids from unrelated groups by measured shape,
        # never by matching their contents to the desired answer.
        found = [i for i in found if _read(rows, i, "grid_labels", selector["grid"]) is not None]
    if selector.get("outermost") is True:
        # Scope and the declarative predicate decide membership before looking
        # at any output value. A missing/wrong outer value cannot select a child.
        matching = set(found)
        outer = []
        for index in found:
            parent = rows[index].get("parent")
            while parent is not None and parent not in matching:
                parent = rows[parent].get("parent")
            if parent is None:
                outer.append(index)
        found = outer
    return found


def _read(rows, index, read, grid=None):
    if read == "grid_labels":
        import json
        cells = [r for r in rows if r.get("parent") == index]
        if (len(cells) != grid["count"] or any(r["role"] != grid["child_role"] or
                not isinstance(r.get("AXTitle"), str) or not r["AXTitle"] for r in cells)):
            return None
        labels = [r["AXTitle"] for r in cells]
        if len(set(labels)) != len(labels):
            return None
        return json.dumps(sorted(labels), ensure_ascii=False, separators=(",", ":"))
    if read == "document_path":
        raw = rows[index].get("AXDocument")
        if not isinstance(raw, str):
            return None
        url = urlsplit(raw)
        if url.scheme != "file" or url.netloc not in {"", "localhost"} or url.query or url.fragment:
            return None
        path = unquote(url.path, errors="strict")
        return str(Path(path).resolve()) if Path(path).is_absolute() and "\0" not in path else None
    if read != "subtree_text":
        return rows[index].get(read)
    children = {}
    for i, row in enumerate(rows):
        children.setdefault(row.get("parent"), []).append(i)
    def text(i):
        parts = [rows[i].get("AXValue", "")] if rows[i]["role"] in {"AXStaticText", "AXTextArea"} else []
        for child in children.get(i, []):
            parts.extend(text(child))
        return parts
    return " ".join(text(index))


def _scope(c, rows, inputs):
    p = c["reference"].get("window_path_param")
    if p is None:
        return None
    path = str(Path(inputs[p]).resolve())
    found = [i for i, row in enumerate(rows) if row["role"] == "AXWindow" and _read(rows, i, "document_path") == path]
    if len(found) > 1:
        raise ValueError("ambiguous document window")
    return found


def observe(c, rows, inputs):
    _rows(rows)
    scope = _scope(c, rows, inputs)
    observed = {}
    for name, s in c["selectors"].items():
        matches = _indices(rows, s, scope)
        if len(matches) > 1:
            raise ValueError("ambiguous learned AX selector")
        if (len(matches) == 1 and c["reference"]["fields"][name]["kind"] in {"file_text", "dictionary_xml"} and
                (s["where"]["role"], s["read"]) == ("AXTextArea", "AXValue")):
            roots = _indices(rows, {"where": {"role": "AXTextArea"}, "read": "AXValue", "outermost": True}, scope)
            if matches[0] not in roots:
                matches = []
        text = _read(rows, matches[0], s["read"], s.get("grid")) if matches else None
        observed[name] = _normalize(text, c["reference"]["fields"][name]) if isinstance(text, str) else None
    return observed


def _infer_binding(values, inputs, names):
    matches = [name for name in names if all(v == row[name] for v, row in zip(values, inputs)) and len({row[name] for row in inputs}) >= 3]
    if len(matches) == 1:
        return {"input": matches[0]}
    if not matches and len(set(values)) == 1:
        return {"constant": values[0]}
    raise ValueError("recordings do not establish a unique parameter binding")


def _infer_objc_binding(kind, values, inputs, names):
    from . import rea_runtime_bridge as B
    matches = []
    for name in names:
        try:
            converted = [B.convert_binding(kind, row[name]) for row in inputs]
        except (ValueError, TypeError):
            continue
        # A boolean has only two possible values; require both during learning.
        diversity = 2 if kind == "bool" else 3
        if len({R.digest(value) for value in converted}) >= diversity and all(
                type(value) is type(expected) and value == expected for value, expected in zip(values, converted)):
            matches.append(name)
    if len(matches) == 1:
        return {"input": matches[0]}
    if not matches and len({R.digest(value) for value in values}) == 1:
        return copy.deepcopy(values[0])
    raise ValueError("recordings do not establish a unique typed runtime parameter binding")


def _native_trace_shape(call):
    """Strip declared value leaves, retaining getter paths and every ABI fact."""
    from . import rea_runtime_bridge as B
    result = copy.deepcopy(call)
    for argument in result["arguments"]:
        for leaf in B.argument_leaves(argument):
            parent = argument
            for key in leaf["path"][:-1]:
                parent = parent[key]
            parent[leaf["path"][-1]] = {"typed_value_leaf": True}
    if "context" in result:
        for leaf in B.argument_leaves(result["context"]):
            parent = result["context"]
            for key in leaf["path"][:-1]:
                parent = parent[key]
            parent[leaf["path"][-1]] = {"typed_value_leaf": True}
    return result


def _infer_native_argument(arguments, inputs, names):
    """Bind typed compound leaves without deriving layouts or pointer values."""
    from . import rea_runtime_bridge as B
    leaves = [B.argument_leaves(argument) for argument in arguments]
    if any([(leaf["path"], leaf["type"]) for leaf in row] !=
           [(leaf["path"], leaf["type"]) for leaf in leaves[0]] for row in leaves[1:]):
        raise ValueError("native argument leaf structure varies across recordings")
    result = copy.deepcopy(arguments[0])
    for index, leaf in enumerate(leaves[0]):
        value = _infer_objc_binding(leaf["type"], [row[index]["value"] for row in leaves], inputs, names)
        result = B.replace_argument_leaf(result, leaf["path"], value)
    return result


def _infer_inspector_binding(kind, values, inputs, names):
    """Infer explicit typed inputs; constants require exact recorded agreement."""
    from . import rea_inspector_bridge as I
    matches = []
    for name in names:
        try:
            converted = [I.convert_binding(kind, row[name]) for row in inputs]
        except (ValueError, TypeError, KeyError):
            continue
        diversity = 2 if kind == "bool" else 3
        # JavaScript has one numeric type. By-value JSON may represent the same
        # declared number as either a Python int or float; booleans are refused
        # by the provider's number validator before this comparison.
        same = lambda actual, expected: (actual == expected if kind == "number" else
                                         type(actual) is type(expected) and R.digest(actual) == R.digest(expected))
        if len({R.digest(value) for value in converted}) >= diversity and all(
                same(actual, expected) for actual, expected in zip(values, converted)):
            matches.append(name)
    if len(matches) == 1:
        return {"input": matches[0]}
    if not matches and len({R.digest(value) for value in values}) == 1:
        return copy.deepcopy(values[0])
    raise ValueError("recordings do not establish a unique typed inspector parameter binding")


def _infer_inspector_actor(traces, inputs, names):
    from . import rea_runtime_providers as P
    provider = P.for_name("inspector")
    checked = [provider.validate_actor(trace, allow_bindings=False) for trace in traces]
    shape = lambda actor: {**actor, "arguments": [{"type": argument["type"]} for argument in actor["arguments"]]}
    if any(shape(actor) != shape(checked[0]) for actor in checked[1:]):
        raise ValueError("inspector object recipe, method, source, declaration or type shape varies across recordings")
    result = copy.deepcopy(checked[0])
    for index, argument in enumerate(result["arguments"]):
        argument["value"] = _infer_inspector_binding(argument["type"],
            [actor["arguments"][index]["value"] for actor in checked], inputs, names)
    return provider.validate_actor(result, allow_bindings=True)


def _infer_actor(demos, names):
    traces, inputs = [d["invocation"] for d in demos], [d["inputs"] for d in demos]
    kinds = {t.get("kind") for t in traces}
    if kinds == {"open_document"} and all(set(t) == {"kind", "path"} for t in traces):
        return {"kind": "open_document", "path": _infer_binding([t["path"] for t in traces], inputs, names)}
    if kinds == INSPECTOR_KINDS:
        return _infer_inspector_actor(traces, inputs, names)
    if "qt_moc_method" in kinds:
        raise ValueError("Qt method learning needs a separate fixed-state trial gate; no inferred parameters")
    if len(kinds) == 1 and kinds <= NATIVE_RUNTIME_KINDS:
        from . import rea_runtime_bridge as B
        methods = [B.actor_calls(trace) for trace in traces]
        structure = lambda entries: [_native_trace_shape(method) if method["kind"] == "native_function" else
                                     {**method, "arguments": [{"type": a["type"]} for a in method["arguments"]]}
                                     for method in entries]
        if any(structure(entries) != structure(methods[0]) for entries in methods[1:]):
            raise ValueError("runtime target, method/ABI contract or argument layout varies across recordings")
        actor = copy.deepcopy(traces[0])
        learned = actor["steps"] if actor["kind"] in {"objc_sequence", "runtime_sequence"} else [actor]
        for step, method in enumerate(learned):
            if method["kind"] == "native_function":
                for i, argument in enumerate(method["arguments"]):
                    method["arguments"][i] = _infer_native_argument([entries[step]["arguments"][i] for entries in methods], inputs, names)
                if "context" in method:
                    method["context"] = _infer_native_argument([entries[step]["context"] for entries in methods], inputs, names)
            else:
                for i, argument in enumerate(method["arguments"]):
                    argument["value"] = _infer_objc_binding(argument["type"],
                        [entries[step]["arguments"][i]["value"] for entries in methods], inputs, names)
        return B.validate_actor(actor, allow_bindings=True)
    if kinds != {"apple_event"} or any(set(t) != {"kind", "class", "id", "parameters"} for t in traces):
        raise ValueError("recordings need one supported invocation primitive")
    if len({(t["class"], t["id"]) for t in traces}) != 1 or any(set(t["parameters"]) != set(traces[0]["parameters"]) for t in traces):
        raise ValueError("event structure varies across recordings")
    params = {}
    for k in traces[0]["parameters"]:
        if any(not isinstance(t["parameters"][k], dict) or set(t["parameters"][k]) != {"type", "value"} or
               t["parameters"][k]["type"] != "utf8" or not _text(t["parameters"][k]["value"]) for t in traces):
            raise ValueError("only recorded UTF-8 event descriptors are supported")
        params[k] = {"type": "utf8", "value": _infer_binding([t["parameters"][k]["value"] for t in traces], inputs, names)}
    return {"kind": "apple_event", "class": traces[0]["class"], "id": traces[0]["id"], "parameters": params}


def _infer_selectors(c, demos, references):
    selectors = {}
    first = _rows(demos[0]["after"])
    for name, f in c["reference"]["fields"].items():
        possibilities = []
        for index, row in enumerate(first):
            where = {"role": row["role"]}
            if row.get("AXSubrole"):
                where["AXSubrole"] = row["AXSubrole"]
            if row["role"] in {"AXRadioButton", "AXCheckBox"}:
                where["AXValue"] = "1"
            reads = ["AXValue", "AXTitle"] + (["subtree_text"] if row["role"] == "AXWebArea" else []) + (
                ["document_path"] if row["role"] == "AXWindow" else [])
            cells = [r for r in first if r.get("parent") == index]
            grid = None
            if row["role"] in {"AXGroup", "AXLayoutArea", "AXTable"} and cells and len({r["role"] for r in cells}) == 1:
                grid = {"child_role": cells[0]["role"], "count": len(cells)}
                reads.append("grid_labels")
            for read in reads:
                variants = [copy.deepcopy(where)]
                # An independently declared unchanged control can have the same
                # AX role as a window title. Its constant content is a legitimate
                # selector anchor; changing task output never gets this anchor.
                if f["kind"] == "constant" and read in ATTRS and _text(row.get(read)):
                    variants.append({**where, read: row[read]})
                for selector_where in variants:
                    s = {"where": selector_where, "read": read}
                    if read == "grid_labels":
                        s["grid"] = grid
                    complete_area = (f["kind"] in {"file_text", "dictionary_xml"} and
                                     (row["role"], read) == ("AXTextArea", "AXValue"))
                    choices = [s, {**s, "outermost": True}] if complete_area else [s]
                    for choice in choices:
                        positions = []
                        for demo, reference in zip(demos, references):
                            rows = _rows(demo["after"])
                            scope = _scope(c, rows, demo["inputs"])
                            found = _indices(rows, choice, scope)
                            if complete_area and len(found) == 1:
                                # A paragraph with a distinct subrole is still
                                # partial content, even if its text fits an oracle.
                                roots = _indices(rows, {"where": {"role": "AXTextArea"},
                                    "read": "AXValue", "outermost": True}, scope)
                                if found[0] not in roots:
                                    break
                            text = _read(rows, found[0], read, choice.get("grid")) if len(found) == 1 else None
                            if not isinstance(text, str) or _normalize(text, f) != reference[name]:
                                break
                            positions.append(found[0])
                        else:
                            possibilities.append((choice, tuple(positions)))
        # A one-line recording can match both its text leaf and the complete
        # content root. Learn the root so a later result may contain more runs,
        # links or paragraphs. This preference is by AX semantics, not app name.
        if f["kind"] == "input":
            # The query also occurs in result headings. Its input field is the
            # semantic source; an echoed heading cannot replace an ambiguous
            # or missing input control.
            possibilities = [(s, positions) for s, positions in possibilities
                             if s["where"]["role"] in {"AXTextField", "AXTextArea", "AXComboBox"} and s["read"] == "AXValue"]
        elif f["kind"] == "basename":
            possibilities = [(s, positions) for s, positions in possibilities
                             if s["where"]["role"] == "AXWindow" and s["read"] == "AXTitle"]
        elif f["kind"] == "file_identity":
            possibilities = [(s, positions) for s, positions in possibilities
                             if s["where"]["role"] == "AXWindow" and s["read"] == "document_path"]
        elif f["kind"] in {"file_text", "dictionary_xml"}:
            complete = [(s, positions) for s, positions in possibilities
                        if (s["where"]["role"], s["read"]) in
                        {("AXWebArea", "subtree_text"), ("AXTextArea", "AXValue")}]
            possibilities = complete
        elif f["kind"] == "fen_plist":
            possibilities = [(s, positions) for s, positions in possibilities
                             if s["read"] == "grid_labels" and s["grid"]["count"] == 64]
        if not possibilities or len({positions for _, positions in possibilities}) != 1:
            raise ValueError(f"recordings do not establish an unambiguous full-output selector: {name}")
        selectors[name] = possibilities[0][0]
    return selectors


def learn(profile, demos, bundle, out):
    R.assert_guest()
    if isinstance(profile, dict) and profile.get("format") == "anyuse.rea-fixed-state-profile/1":
        from . import rea_state_campaign as C
        return C.learn(profile, demos, bundle, out)
    if not isinstance(profile, dict) or profile.get("format") != PROFILE:
        raise ValueError("invalid harness profile")
    if not isinstance(demos, list) or not 3 <= len(demos) <= 32 or any(
            not isinstance(d, dict) or set(d) != {"inputs", "invocation", "after"} for d in demos):
        raise ValueError("need 3–32 input/invocation/independent-output recordings")
    root = Path(bundle).resolve(strict=True)
    names = _params(profile.get("params"))
    _reference(profile.get("reference"), names, profile["params"])
    svg = _svg_template(profile)
    for demo in demos:
        _inputs(profile["params"], demo["inputs"], reference=profile["reference"])
    _path_roles(profile["params"], [demo["inputs"] for demo in demos], profile["reference"])
    c = {k: copy.deepcopy(profile[k]) for k in ("id", "label", "effect", "item_kind", "mode", "params", "reference", "scope", "observer", "evidence") if k in profile}
    c.update(format=R.CANDIDATE, backend=SVG_BACKEND if svg is not None else BACKEND, target=R.fingerprint(root),
             actor=_infer_actor(demos, names), learned_from={"inputs": [R.digest(d["inputs"]) for d in demos],
                                                          "recordings": [R.digest(d) for d in demos]})
    c.setdefault("item_kind", "")
    c.setdefault("mode", "")
    if svg is not None:
        c["learned_from"]["semantic_inputs"] = [R.digest(semantic_inputs(c, d["inputs"])) for d in demos]
    for citation in c.get("evidence", []):
        if R._file_hash(Path(citation["file"])) != citation["sha256"]:
            raise ValueError("reviewed interface evidence changed")
    pinned = resources(c)
    references = [expected(c, d["inputs"])[0] for d in demos]
    if svg is not None:
        if (len(set(c["learned_from"]["semantic_inputs"])) < 3 or
                any(not isinstance(d["after"], dict) or d["after"].get("inputs") != d["inputs"] or
                    not _svg_row_valid(c, d["after"], pinned, require_change=True) for d in demos)):
            raise ValueError("SVG recordings need three substantive input sets and complete independent before/after/runtime evidence")
    else:
        c["selectors"] = _infer_selectors(c, demos, references)
    c = validate(c)
    if resources(c) != pinned or R.fingerprint(root) != c["target"]:
        raise ValueError("source changed during learning")
    folder = Path(out).resolve()
    _outside(c, root, folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    R.write_json(folder / "candidate.json", c)
    R.write_json(folder / "learning.json", {"status": "candidate", "proof_created": False,
                                          "recordings": len(demos), "candidate_sha256": R.digest(c), "model_calls": 0})
    return c


def record(profile, invocation, inputs, bundle, out, timeout=30):
    """Capture one invocation with independent output; never create a proof."""
    R.assert_guest()
    if not isinstance(profile, dict) or profile.get("format") != PROFILE or not 1 <= timeout <= 300:
        raise ValueError("invalid profile or recording timeout")
    names = _params(profile.get("params"))
    _reference(profile.get("reference"), names, profile["params"])
    svg = _svg_template(profile)
    _inputs(profile["params"], inputs, reference=profile["reference"])
    if not isinstance(invocation, dict):
        raise ValueError("recorded invocation must be an object")
    if invocation.get("kind") == "apple_event":
        if (set(invocation) != {"kind", "class", "id", "parameters"} or
                not all(_code(invocation[k]) for k in ("class", "id")) or
                not isinstance(invocation["parameters"], dict) or not 1 <= len(invocation["parameters"]) <= 16):
            raise ValueError("invalid recorded typed event")
        actor = {"kind": "apple_event", "class": invocation["class"], "id": invocation["id"], "parameters": {}}
        for key, p in invocation["parameters"].items():
            if not _code(key) or not isinstance(p, dict) or set(p) != {"type", "value"} or p["type"] != "utf8" or not _text(p["value"]):
                raise ValueError("invalid recorded UTF-8 descriptor")
            actor["parameters"][key] = {"type": "utf8", "value": {"constant": p["value"]}}
    elif invocation.get("kind") == "open_document" and set(invocation) == {"kind", "path"}:
        matches = [p["name"] for p in profile["params"] if p["type"] == "path" and inputs[p["name"]] == invocation["path"]]
        if len(matches) != 1 or not any(f["kind"] in {"file_text", "fen_plist"} and f["param"] == matches[0] for f in profile["reference"]["fields"].values()):
            raise ValueError("document recording must match one independently referenced task path")
        actor = {"kind": "open_document", "path": {"input": matches[0]}}
    elif invocation.get("kind") in RUNTIME_KINDS:
        from . import rea_runtime_providers as P
        actor = P.for_actor(invocation).validate_actor(invocation, allow_bindings=False)
    else:
        raise ValueError("unsupported recording primitive")
    root = Path(bundle).resolve(strict=True)
    c = {"params": profile["params"], "actor": actor, "reference": profile["reference"], "target": R.fingerprint(root)}
    if svg is not None:
        c["observer"] = svg
    pinned = resources(c)
    reference, sources = expected(c, inputs)
    folder = Path(out).resolve()
    _outside(c, root, folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = {"status": "incomplete", "proof_created": False, "target": c["target"], "model_calls": 0}
    try:
        runtime = Runtime(root, c)
        if svg is not None:
            row = _svg_trial(c, inputs, runtime, folder, (reference, sources), timeout, require_change=True)
            receipt["run"] = row["run"]
            if row["verdict"] != "verified":
                raise ValueError("recorded SVG output did not match the independent task reference")
            demo = {"inputs": inputs, "invocation": invocation, "after": row}
        else:
            R.write_json(folder / "before-ax.json", runtime.read())
            R.write_json(folder / "reference.json", {"expected": reference, "sources": sources})
            started = time.monotonic()
            runtime.receipt_dir = folder / "runtime"
            run = runtime.send(c, inputs, timeout)
            receipt["run"] = run
            if run["returncode"] != 0 or run["timed_out"]:
                raise ValueError("recorded invocation failed; no retry")
            if actor["kind"] in NATIVE_RUNTIME_KINDS:
                from . import rea_runtime_bridge as B
                if B.native_path_recipes(actor) and not _runtime_proof_run(c, inputs, run, pinned):
                    raise ValueError("recorded native path invocation lacks complete bound runtime evidence; no retry")
            demo = {"inputs": inputs, "invocation": invocation, "after": runtime.read()}
            while True:
                R.write_json(folder / "after-ax.json", demo["after"])
                try:
                    _infer_selectors(c, [demo], [reference])
                    break
                except ValueError:
                    if time.monotonic() - started >= timeout:
                        raise ValueError("recorded output did not match the independent task reference")
                    time.sleep(.1)
                    demo["after"] = runtime.read()
        if R.fingerprint(root) != c["target"] or resources(c) != pinned or expected(c, inputs) != (reference, sources):
            raise ValueError("app or independent reference changed while recording")
        R.write_json(folder / "recording.json", demo)
        receipt["status"] = "observed"
    except Exception as exc:
        receipt["error"] = str(exc)
        raise
    finally:
        R.write_json(folder / "recording-check.json", receipt)
    return demo


class _SVGGUIContext:
    """Existing-process readiness only; no launch, activation, AX or AE actor."""
    def __init__(self, bundle, *, bundle_id):
        R.assert_guest()  # Before filesystem access or any native framework import.
        root = Path(bundle)
        if (not root.is_absolute() or root.resolve(strict=True) != root or
                not _text(bundle_id, 1024) or not bundle_id):
            raise ValueError("SVG context needs the exact canonical existing app bundle and identifier")
        import ApplicationServices as A
        import Quartz
        self._A, self._Quartz = A, Quartz
        self._bundle, self._bundle_id = root, bundle_id
        self._gui_ready()
        self._identity = self._process()
        self.pid = self._identity["pid"]

    def _gui_ready(self):
        R.assert_guest()
        Q = self._Quartz
        session = Q.CGSessionCopyCurrentDictionary() or {}
        if (not session.get(Q.kCGSessionOnConsoleKey) or not session.get("kCGSessionLoginDoneKey") or
                session.get(Q.kCGSessionUserIDKey) != os.getuid() or session.get("CGSSessionScreenIsLocked")):
            raise PermissionError("SVG context requires the existing unlocked same-user console session")
        if not self._A.AXIsProcessTrusted():
            raise PermissionError("SVG context needs the GUI runner's existing Accessibility grant")

    def _process(self):
        R.assert_guest()
        from . import rea_runtime_bridge as B
        identity = B.process_identity(self._bundle, self._bundle_id)
        if (not isinstance(identity, dict) or set(identity) != {"bundle_id", "bundle", "executable", "pid", "launch_time", "uid"} or
                identity["bundle_id"] != self._bundle_id or identity["bundle"] != str(self._bundle) or
                type(identity["pid"]) is not int or identity["pid"] <= 0 or
                type(identity["uid"]) is not int or identity["uid"] != os.getuid() or
                type(identity["launch_time"]) not in {int, float} or not math.isfinite(identity["launch_time"]) or identity["launch_time"] <= 0 or
                not _text(identity["executable"]) or not Path(identity["executable"]).is_absolute() or
                str(Path(identity["executable"])) != identity["executable"] or ".." in Path(identity["executable"]).parts or
                not Path(identity["executable"]).is_relative_to(self._bundle)):
            raise ValueError("SVG context needs one exact existing same-user app process")
        return copy.deepcopy(identity)

    def _ready(self):
        self._gui_ready()
        if self._process() != self._identity:
            raise ValueError("SVG app process changed; no launch, activation or fallback")


class Runtime:
    def __init__(self, bundle, c):
        self.bundle = bundle
        # Recordings use a partial candidate without backend yet. Its explicit
        # observer must select this same no-launch path before learning too.
        self._svg_context = _svg_template(c) is not None
        context = _SVGGUIContext if self._svg_context else AE.Runtime
        self.native = context(bundle, bundle_id=c["target"]["bundle_id"])
        self.receipt_dir = None
        self._native_identity = None
        if c["actor"]["kind"] in NATIVE_RUNTIME_KINDS:
            # The ordinary context has completed its launch/readiness path.
            # Bind that same PID, rather than adopting a later bundle match.
            self._native_bundle = Path(bundle).resolve(strict=True)
            self._native_bundle_id = c["target"]["bundle_id"]
            self._native_identity = self._native_process()
        self.inspector_identity = resources(c)["inspector_bridge"] if c["actor"]["kind"] in INSPECTOR_KINDS else None

    def _native_process(self):
        from . import rea_runtime_bridge as B
        identity = B._expected_identity(B.process_identity(self._native_bundle, self._native_bundle_id))
        if (identity is None or identity["bundle"] != str(self._native_bundle) or
                identity["bundle_id"] != self._native_bundle_id or
                type(self.native.pid) is not int or identity["pid"] != self.native.pid):
            raise ValueError("native actor needs the complete identity of its original context PID")
        return identity

    def read(self):
        if self._svg_context:
            raise ValueError("SVG context has no AX observer or fallback")
        return self.native.read(all_windows=True)

    def read_svg(self, c, inputs, out, timeout):
        from . import rea_svg_observer as S
        self.native._ready()  # Preserve the existing unlocked/trusted GUI gate.
        _, sources = _svg_case(c, inputs)
        pinned = resources(c)["svg_observer"]
        return S.collect(str(self.bundle), sources["schema"], str(out), timeout=max(1, min(60, math.ceil(timeout))),
                         config_file=pinned["transport"]["config"]["file"])

    def send(self, c, inputs, timeout):
        invocation = bind(c, inputs)
        if self._svg_context and invocation["kind"] not in INSPECTOR_KINDS:
            raise ValueError("SVG context only invokes its explicit inspector actor; no opening or fallback")
        self.native._ready()
        if invocation["kind"] == "apple_event":
            return self.native.send_invocation({k: v for k, v in invocation.items() if k != "kind"}, timeout)
        if invocation["kind"] in INSPECTOR_KINDS:
            from . import rea_inspector_bridge as I
            if self.receipt_dir is None or self.inspector_identity is None:
                raise ValueError("inspector invocation requires pinned resources and a local receipt directory")
            pinned = _inspector_adapter(self.inspector_identity)
            if resources(c)["inspector_bridge"] != pinned:
                raise ValueError("inspector adapter or explicit transport changed before invocation")
            started = time.monotonic()
            receipt = I.invoke(str(self.bundle), invocation, str(self.receipt_dir), timeout=min(timeout, 60),
                               config_file=pinned["transport"]["config"]["file"])
            if receipt.get("resource_review_required"):
                raise R.ResourceReviewRequired("inspector cleanup or effects need review; no retry")
            run = {"returncode": 1, "timed_out": False, "permission": "inspector_connected",
                   "pid": self.native.pid, "uid": os.getuid(), "bundle": str(Path(self.bundle).resolve()),
                   "primitive": invocation["kind"], "runtime": receipt,
                   "elapsed_s": round(time.monotonic() - started, 4)}
            if _inspector_run(c, inputs, run, pinned):
                run["returncode"] = 0
            return run
        if invocation["kind"] in NATIVE_RUNTIME_KINDS:
            from . import rea_runtime_bridge as B
            if self.receipt_dir is None:
                raise ValueError("runtime invocation requires a local receipt directory")
            if (self._native_identity is None or
                    not NP.same_data(dict(self._native_process()), dict(self._native_identity))):
                raise ValueError("native app process changed before actor; no successor adoption")
            started = time.monotonic()
            receipt = B.invoke(str(self.bundle), invocation, str(self.receipt_dir), timeout=min(timeout, 60),
                               expected_identity=dict(self._native_identity))
            if (not isinstance(receipt, dict) or any(
                    not NP.same_data(receipt.get(key), dict(self._native_identity))
                    for key in ("expected_identity", "identity", "after_identity"))):
                # Invocation has returned; do not treat incoherent retained
                # identity evidence as a clean refusal before dispatch.
                raise R.ResourceReviewRequired("runtime returned identity differs from its saved process pin; review retained receipt, no retry")
            if receipt.get("resource_review_required"):
                raise R.ResourceReviewRequired("runtime cleanup or effects need review; no retry")
            run = {"returncode": 1, "timed_out": False, "permission": "runtime_attached",
                   "pid": self.native.pid, "uid": os.getuid(), "bundle": str(Path(self.bundle).resolve()),
                   "primitive": invocation["kind"], "runtime": receipt,
                   "elapsed_s": round(time.monotonic() - started, 4)}
            if _objc_run(c, inputs, run):
                run["returncode"] = 0
            return run
        path = Path(invocation["path"])
        if path.is_symlink() or not path.is_file():
            raise ValueError("only an existing regular document may be opened")
        started = time.monotonic()
        result = subprocess.run(["/usr/bin/open", "-a", str(self.bundle), str(path)], capture_output=True, timeout=timeout, check=False)
        return {"returncode": result.returncode, "timed_out": False, "elapsed_s": round(time.monotonic() - started, 4),
                "permission": "not_required", "pid": self.native.pid, "primitive": "open_document"}


def _outside(c, root, folder):
    protected = [root]
    for name, identity in resources(c).items():
        if name == "runtime_bridge":
            protected += [Path(identity["file"]).resolve(), Path(identity["manifest"]["file"]).resolve()]
            if identity["transport"]["kind"] == "gadget":
                protected += [Path(identity["transport"][key]["file"]).resolve() for key in ("token_file", "library", "config")]
        elif name == "inspector_bridge":
            protected += [Path(identity["adapter"]["file"]).resolve(), Path(identity["transport"]["config"]["file"]).resolve()]
        elif name == "svg_observer":
            protected += [Path(identity["adapter"]["source"]["file"]).resolve(),
                          Path(identity["transport"]["config"]["file"]).resolve()]
            protected += [Path(pin["file"]).resolve() for pin in identity["adapter"]["dependencies"].values()]
        else:
            protected.append(Path(name).resolve())
    if any(folder.is_relative_to(p) for p in protected):
        raise ValueError("receipts must be outside app and pinned resources")


def _svg_context_valid(context, capture, root, limits):
    from . import rea_svg_observer as S
    S._keys(context, {"format", "namespace", "source", "root_identity_sha256", "elements", "elements_sha256",
                      "element_count", "dom_node_count", "metadata_tree_sha256", "complete", "element_coverage_only",
                      "metadata_whitespace_mode", "whitespace_source"})
    rows = context["elements"]
    if (context["format"] != S.DOM_CONTEXT or context["namespace"] != S.SVG_NS or context["source"] != "DOM.describeNode" or
            context["root_identity_sha256"] != root["identity_sha256"] or context["complete"] is not True or
            context["element_coverage_only"] is not True or context["metadata_whitespace_mode"] != "all_requested" or
            context["whitespace_source"] != "DOM.getOuterHTML" or not isinstance(rows, list) or
            type(context["element_count"]) is not int or context["element_count"] != capture["node_count"] or
            not 1 <= len(rows) == context["element_count"] <= limits["max_nodes"] or
            type(context["dom_node_count"]) is not int or not len(rows) <= context["dom_node_count"] <= S.LIMITS["max_nodes"] * 4 or
            context["elements_sha256"] != R.digest(rows) or not isinstance(context["metadata_tree_sha256"], str) or
            not R.SHA.fullmatch(context["metadata_tree_sha256"])):
        return False
    pending = []
    text_count = 0
    for i, row in enumerate(rows):
        S._keys(row, {"tag", "depth", "element_children", "attributes_sha256"})
        if (not isinstance(row["tag"], str) or row["tag"] not in S._TAGS or
                type(row["depth"]) is not int or not 1 <= row["depth"] <= limits["max_depth"] or
                type(row["element_children"]) is not int or not 0 <= row["element_children"] <= limits["max_nodes"] or
                not isinstance(row["attributes_sha256"], str) or not R.SHA.fullmatch(row["attributes_sha256"])):
            return False
        if i == 0:
            if row["tag"] != "svg" or row["depth"] != 1:
                return False
        else:
            while pending and pending[-1] == 0:
                pending.pop()
            if not pending or row["depth"] != len(pending) + 1:
                return False
            pending[-1] -= 1
        pending.append(row["element_children"])
        text_count += row["tag"] == "text"
    return (not any(pending) and text_count == capture["text_element_count"] and
            max(row["depth"] for row in rows) == capture["max_depth"])


def _svg_observation_valid(c, inputs, observation, reference, pinned, *, run=None):
    """Check independent evidence, never promote a status/matches assertion."""
    from . import rea_svg_observer as S
    from . import rea_inspector_bridge as I
    try:
        correct, sources = reference
        S._keys(sources, {"schema", "schema_sha256", "files"})
        schema = _svg_schema(c, inputs, sources["files"])
        if sources["schema"] != schema or sources["schema_sha256"] != R.digest(schema):
            return False
        field_name, field = next(iter(c["reference"]["fields"].items()))
        if set(correct) != {field_name}:
            return False
        wanted = S._values(correct[field_name], schema["limits"])
        values = S._values(observation.get("values"), schema["limits"])
        static = pinned["svg_observer"]
        actor_pins = _inspector_adapter(pinned["inspector_bridge"])
        if static["transport"] != actor_pins["transport"] or static["adapter"]["inspector"] != actor_pins["adapter"]:
            return False
        expected_resources = {"reference": sources["files"][field["param"]],
                              "declaration": sources["files"][c["observer"]["contract_param"]],
                              "adapter": static["adapter"], "transport": static["transport"]}
        if (observation.get("format") != S.OBSERVATION or observation.get("kind") != schema["kind"] or
                observation.get("target") != c["target"] or observation.get("schema_sha256") != R.digest(schema) or
                observation.get("request_sha256") != R.digest(S._request(schema)) or observation.get("resources") != expected_resources or
                observation.get("expected") != wanted or observation.get("complete") is not True or
                observation.get("matches") is not (values == wanted) or
                observation.get("status") != ("observed" if values == wanted else "refuted") or
                any(observation.get(key) is not False for key in ("state_uncertain", "resource_review_required", "retry", "proof_created",
                    "task_result_verified", "visibility_proved", "geometry_proved", "connectivity_proved", "persistence_proved", "actuated")) or
                observation.get("evidence_only") is not True or
                any(type(observation.get(key)) is not int or observation[key] != 0
                    for key in ("model_calls", "app_evaluations", "method_calls", "getters_invoked")) or
                observation.get("cleanup") != {"page_socket_closed": True, "browser_socket_closed": True,
                    "requested_reads_completed": True, "local_live_ids_discarded": True} or "error" in observation):
            return False
        main = observation.get("identity")
        S._keys(main, {"bundle_id", "bundle", "executable", "pid", "launch_time", "uid"})
        if (main != observation.get("after_identity") or main["bundle_id"] != c["target"]["bundle_id"] or
                type(main["pid"]) is not int or main["pid"] <= 0 or type(main["uid"]) is not int or main["uid"] < 0 or
                type(main["launch_time"]) not in {int, float} or not math.isfinite(main["launch_time"]) or main["launch_time"] <= 0 or
                S._path(main["bundle"]) != main["bundle"] or S._path(main["executable"]) != main["executable"] or
                not Path(main["executable"]).is_relative_to(main["bundle"]) or
                static["transport"]["config"]["uid"] != main["uid"]):
            return False
        endpoint = observation.get("endpoint_identity")
        S._keys(endpoint, {"listener", "renderer_processes", "target_type", "target_url", "target_identity_sha256", "frame_sha256"})
        parsed = urlsplit(endpoint["target_url"])
        transport = static["transport"]
        suffix = I._document_suffix(transport.get("document_suffix", {"query": "", "fragment": ""}))
        if (endpoint["target_type"] != "page" or parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or
                parsed.query != suffix["query"] or parsed.fragment != suffix["fragment"] or
                Path(unquote(parsed.path)) != Path(main["bundle"]) / transport["relative_url"] or
                any(not isinstance(endpoint[key], str) or not R.SHA.fullmatch(endpoint[key])
                    for key in ("target_identity_sha256", "frame_sha256"))):
            return False
        listener = endpoint["listener"]
        S._keys(listener, {"pid", "uid", "listen_addresses"})
        _, port = I._endpoint(transport["endpoint"])
        if (listener["pid"] != main["pid"] or listener["uid"] != main["uid"] or
                not isinstance(listener["listen_addresses"], list) or not listener["listen_addresses"] or
                any(address not in {f"127.0.0.1:{port}", f"[::1]:{port}"} for address in listener["listen_addresses"])):
            return False
        renderers = endpoint["renderer_processes"]
        if not isinstance(renderers, list) or not 1 <= len(renderers) <= 16 or any(not isinstance(r, dict) for r in renderers):
            return False
        if len({r.get("pid") for r in renderers}) != len(renderers):
            return False
        for renderer in renderers:
            S._keys(renderer, {"pid", "ppid", "uid", "started_at", "executable"})
            if (type(renderer["pid"]) is not int or renderer["pid"] <= 0 or type(renderer["ppid"]) is not int or renderer["ppid"] <= 0 or
                    renderer["uid"] != main["uid"] or not _text(renderer["started_at"], 128) or
                    not Path(renderer["executable"]).is_relative_to(main["bundle"])):
                return False
        root = observation.get("root_before")
        S._keys(root, {"tag", "is_svg", "attribute_count", "attributes_sha256", "child_count", "identity_sha256"})
        if (root != observation.get("root_after") or root["tag"] != "svg" or root["is_svg"] is not True or
                type(root["attribute_count"]) is not int or not 0 <= root["attribute_count"] <= S.MAX_ATTRIBUTES or
                type(root["child_count"]) is not int or not 0 <= root["child_count"] <= S.LIMITS["max_nodes"] or
                any(not isinstance(root[key], str) or not R.SHA.fullmatch(root[key]) for key in ("attributes_sha256", "identity_sha256"))):
            return False
        capture = observation.get("capture")
        limits = schema["limits"]
        if schema["kind"] == S.MIXED_KIND:
            # Recompute units from the complete typed native DOM tree. A copied
            # `matches` flag or actor-returned graph/label array is insufficient.
            if S.validate_mixed_capture(capture, limits=limits) != values:
                return False
            witness = capture["witness"]
            witness_root = witness["nodes"][0]
            if (witness["root_identity_sha256"] != root["identity_sha256"] or
                    R.digest(witness_root["attributes"]) != root["attributes_sha256"] or
                    len(witness_root["attributes"]) != root["attribute_count"] * 2 or
                    witness_root["child_count"] != root["child_count"] or
                    observation.get("mixed_witness_stable") is not True or
                    observation.get("mixed_witness_after_sha256") != R.digest(witness)):
                return False
        else:
            S._keys(capture, {"values", "values_sha256", "complete", "markup_sha256", "markup_bytes", "node_count", "node_count_kind",
                             "max_depth", "text_element_count", "text_bytes", "visibility_proved", "normalization"},
                            {"namespace_mode", "namespace_context"})
            if (capture["values"] != values or capture["values_sha256"] != R.digest(values) or capture["complete"] is not True or
                    capture["visibility_proved"] is not False or capture["normalization"] != "none" or
                    not isinstance(capture["markup_sha256"], str) or not R.SHA.fullmatch(capture["markup_sha256"]) or
                    type(capture["markup_bytes"]) is not int or not 1 <= capture["markup_bytes"] <= limits["max_markup_bytes"] or
                    type(capture["node_count"]) is not int or not 1 <= capture["node_count"] <= limits["max_nodes"] or
                    capture["node_count_kind"] != "svg_elements" or type(capture["max_depth"]) is not int or
                    not 1 <= capture["max_depth"] <= limits["max_depth"] or type(capture["text_element_count"]) is not int or
                    capture["text_element_count"] != len(values) or capture["node_count"] < len(values) + 1 or
                    type(capture["text_bytes"]) is not int or capture["text_bytes"] != sum(len(v.encode("utf-8")) for v in values)):
                return False
            if schema.get("namespace_mode") == "dom_svg_elements":
                if (capture.get("namespace_mode") != "dom_svg_elements" or
                        not _svg_context_valid(capture.get("namespace_context"), capture, root, limits) or
                        observation.get("namespace_context_stable") is not True or
                        observation.get("namespace_context_after_sha256") != R.digest(capture["namespace_context"])):
                    return False
            elif any(key in capture for key in ("namespace_mode", "namespace_context")):
                return False
        if run is not None:
            actual = run["runtime"]
            actor_endpoint = actual["endpoint_identity"]
            if (main != actual["identity"] or main != actual["after_identity"] or run["pid"] != main["pid"] or
                    run["uid"] != main["uid"] or run["bundle"] != main["bundle"] or
                    endpoint["listener"] != actor_endpoint["listener"] or
                    endpoint["renderer_processes"] != actor_endpoint["renderer_processes"] or
                    endpoint["target_type"] != actor_endpoint["target"]["type"] or
                    endpoint["target_url"] != actor_endpoint["target"]["url"] or
                    endpoint["frame_sha256"] != R.digest(actor_endpoint["frame"])):
                return False
        return True
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _svg_read(c, inputs, runtime, out, timeout, reference, pinned):
    if expected(c, inputs) != reference or resources(c) != pinned:
        raise ValueError("SVG case/reference/observer resources changed before observation")
    observation = runtime.read_svg(c, inputs, out, timeout)
    if observation.get("resource_review_required"):
        raise R.ResourceReviewRequired("SVG observer cleanup needs review; no retry")
    if (not _svg_observation_valid(c, inputs, observation, reference, pinned) or
            observation["identity"]["bundle"] != str(Path(runtime.bundle).resolve()) or
            observation["identity"]["pid"] != runtime.native.pid):
        raise ValueError("SVG observer did not produce a complete certain independent same-process observation")
    if expected(c, inputs) != reference or resources(c) != pinned:
        raise ValueError("SVG case/reference/observer resources changed during observation")
    return observation


def _svg_trial(c, inputs, runtime, folder, reference, timeout, *, require_change):
    from . import rea_svg_observer as S
    correct, sources = reference
    pinned, started = resources(c), time.monotonic()
    before = _svg_read(c, inputs, runtime, folder / "before-svg", timeout, reference, pinned)
    name = next(iter(correct))
    before_values = {name: before["values"]}
    R.write_json(folder / "reference.json", {"expected": correct, "sources": sources})
    if require_change and before_values == correct:
        raise ValueError("a no-op cannot verify the learned SVG actor")
    if time.monotonic() - started >= timeout:
        raise ValueError("SVG observation consumed the trial deadline before actuation")
    runtime.receipt_dir = folder / "runtime"
    run = runtime.send(c, inputs, max(1, math.ceil(timeout - (time.monotonic() - started))))
    if run.get("returncode") != 0 or run.get("timed_out") is not False or not _runtime_proof_run(c, inputs, run, pinned):
        raise ValueError("SVG actor invocation failed its independent runtime gate; no retry")
    if not _svg_observation_valid(c, inputs, before, reference, pinned, run=run):
        raise ValueError("SVG initial observation and actor do not bind the same exact app/frame")
    polls = 0
    while True:
        after = _svg_read(c, inputs, runtime, folder / f"after-svg-{polls}",
                          max(1, timeout - (time.monotonic() - started)), reference, pinned)
        if not _svg_observation_valid(c, inputs, after, reference, pinned, run=run):
            raise ValueError("SVG result observation and actor do not bind the same exact app/frame")
        seen = {name: after["values"]}
        if seen == correct or time.monotonic() - started >= timeout:
            break
        time.sleep(.1)  # Complete clean mismatches may be read again; never resend.
        polls += 1
    return {"inputs": inputs, "inputs_sha256": R.digest(inputs), "semantic_inputs_sha256": R.digest(semantic_inputs(c, inputs)),
            "before": before_values, "before_state_sha256": R.digest(before_values), "before_observation": before,
            "before_observation_sha256": R.digest(before), "after_observation": after, "after_observation_sha256": R.digest(after),
            "expected": correct, "seen": seen, "reference": {"expected": correct, "sources": sources},
            "reference_sha256": R.digest({"expected": correct, "sources": sources}), "observer": c["observer"]["kind"],
            "run": run, "elapsed_s": round(time.monotonic() - started, 4), "read_only_polls": polls,
            "verdict": "verified" if seen == correct else "refuted"}


def _svg_row_valid(c, row, pinned, *, require_change):
    from . import rea_svg_observer as S
    try:
        inputs = row["inputs"]
        bind(c, inputs, check_files=False)
        reference = expected(c, inputs)
        correct, sources = reference
        stored = {"expected": correct, "sources": sources}
        name = next(iter(correct))
        before, after = row["before_observation"], row["after_observation"]
        run = row["run"]
        return (row["inputs_sha256"] == R.digest(inputs) and row["semantic_inputs_sha256"] == R.digest(semantic_inputs(c, inputs)) and
                row["observer"] == c["observer"]["kind"] and row["verdict"] == "verified" and row["reference"] == stored and
                row["reference_sha256"] == R.digest(stored) and row["expected"] == correct and row["seen"] == correct and
                row["before"] == {name: before["values"]} and row["seen"] == {name: after["values"]} and
                (not require_change or row["before"] != correct) and row["before_state_sha256"] == R.digest(row["before"]) and
                row["before_observation_sha256"] == R.digest(before) and row["after_observation_sha256"] == R.digest(after) and
                type(row["read_only_polls"]) is int and 0 <= row["read_only_polls"] <= 3000 and
                run["returncode"] == 0 and run["timed_out"] is False and _runtime_proof_run(c, inputs, run, pinned) and
                _svg_observation_valid(c, inputs, before, reference, pinned, run=run) and
                _svg_observation_valid(c, inputs, after, reference, pinned, run=run))
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _trial(c, inputs, runtime, folder, reference, timeout, *, require_change):
    if _svg_template(c) is not None:
        return _svg_trial(c, inputs, runtime, folder, reference, timeout, require_change=require_change)
    correct, sources = reference
    before_rows = runtime.read()
    before = observe(c, before_rows, inputs)
    R.write_json(folder / "before-ax.json", before_rows)
    R.write_json(folder / "reference.json", {"expected": correct, "sources": sources})
    if require_change and before == correct:
        raise ValueError("a no-op cannot verify the learned actor")
    started = time.monotonic()
    runtime.receipt_dir = folder / "runtime"
    run = runtime.send(c, inputs, timeout)
    rows = runtime.read()
    seen = observe(c, rows, inputs)
    while run["returncode"] == 0 and not run["timed_out"] and seen != correct and time.monotonic() - started < timeout:
        time.sleep(.1)
        rows = runtime.read()
        seen = observe(c, rows, inputs)
    R.write_json(folder / "after-ax.json", rows)
    if expected(c, inputs) != reference:
        raise ValueError("independent reference changed during actuation")
    return {"inputs": inputs, "inputs_sha256": R.digest(inputs), "before": before, "before_ax_sha256": R.digest(before_rows),
            "expected": correct, "seen": seen, "reference_sha256": R.digest(reference), "observer": "learned_ax",
            "run": run, "elapsed_s": round(time.monotonic() - started, 4),
            "verdict": "verified" if run["returncode"] == 0 and not run["timed_out"] and seen == correct else "refuted"}


def _objc_method_run(row, actor, index):
    """Each ordered step needs the actual named method and its observed ABI."""
    from . import rea_runtime_bridge as B
    if not isinstance(row, dict):
        return False
    method = row.get("method")
    if not isinstance(method, dict):
        return False
    if method.get("origin") == "module_backed":
        provenance = (isinstance(method.get("module"), str) and Path(method["module"]).is_absolute() and
                      isinstance(method.get("implementation_offset"), str) and
                      re.fullmatch(r"0x[0-9a-f]+", method["implementation_offset"]) is not None)
    elif method.get("origin") == "runtime_generated":
        provenance = ({"module", "implementation_offset"} <= set(method) and
                      method["module"] is None and method["implementation_offset"] is None)
    else:
        provenance = False
    return (row.get("op") == "invoke" and type(row.get("index")) is int and row["index"] == index and
            row.get("status") == "ok" and row.get("return_type") == actor["return_type"] and
            method.get("selector") == actor["selector"] and method.get("signature") == actor["signature"] and
            method.get("return_type") == actor["return_type"] and
            method.get("argument_types") == [a["type"] for a in actor["arguments"]] and
            method.get("dispatch") in {"-", "+"} and isinstance(method.get("class"), str) and
            B._class_name(method["class"]) and provenance)


def _native_entry_run(function, target):
    """Resolved code needs exact module/image/section provenance, not an ABI guess."""
    if not isinstance(function, dict):
        return False
    offset = function.get("implementation_offset")
    module_size = function.get("module_size")
    reported_size = function.get("reported_module_size")
    image = function.get("image_identity")
    if (not isinstance(image, dict) or set(image) != {"cpu_type", "cpu_subtype", "uuid"} or
            type(image["cpu_type"]) is not int or type(image["cpu_subtype"]) is not int or
            not isinstance(image["uuid"], str) or re.fullmatch(r"[0-9a-f]{32}", image["uuid"]) is None):
        return False
    section = function.get("instruction_section")
    if (not isinstance(section, dict) or set(section) != {"segment", "name", "offset", "bytes", "flags"} or
            any(not _text(section.get(key), 16) or not section[key] for key in ("segment", "name")) or
            not isinstance(section["offset"], str) or re.fullmatch(r"0x[0-9a-f]{1,16}", section["offset"]) is None or
            type(section["bytes"]) is not int or not 1 <= section["bytes"] < 2**64 or
            type(section["flags"]) is not int or not 0 <= section["flags"] < 2**32 or
            not section["flags"] & 0x80000400 or
            type(module_size) is not int or not 0 < module_size < 2**64 or
            type(reported_size) is not int or not 0 < reported_size < 2**64 or
            not isinstance(offset, str) or re.fullmatch(r"0x[0-9a-f]{1,16}", offset) is None or
            not int(section["offset"], 16) <= int(offset, 16) < int(section["offset"], 16) + section["bytes"] <= module_size):
        return False
    return (function.get("target") == target and function.get("module") == target["module"]["path"] and
            function.get("symbol") == target.get("symbol") and function.get("executable") is True and
            ("offset" not in target or offset == target["offset"]))


def _native_function_run(row, actor, index):
    """Bind the resolved native route to its attributable, declared ABI."""
    if not isinstance(row, dict) or not _native_entry_run(row.get("function"), actor["target"]):
        return False
    function = row["function"]
    target = actor["target"]
    offset = function["implementation_offset"]
    module_size = function["module_size"]
    outputs = row.get("out_buffers")
    expected = [(i, argument["type"]["bytes"]) for i, argument in enumerate(actor["arguments"])
                if isinstance(argument["type"], dict) and argument["type"].get("kind") == "buffer" and
                argument["type"].get("direction") in {"out", "inout"}]
    if not isinstance(outputs, list) or len(outputs) != len(expected):
        return False
    for output, (argument, buffer_size) in zip(outputs, expected):
        if (not isinstance(output, dict) or set(output) != {"argument", "bytes"} or
                type(output["argument"]) is not int or output["argument"] != argument or
                not isinstance(output["bytes"], list) or len(output["bytes"]) != buffer_size or
                any(type(value) is not int or not 0 <= value <= 255 for value in output["bytes"])):
            return False
    return (row.get("op") == "native_invoke" and type(row.get("index")) is int and row["index"] == index and
            row.get("status") == "ok" and row.get("return_type") == actor["return_type"] and
            function.get("target") == target and function.get("abi") == actor["abi"] and
            function.get("argument_types") == [argument["type"] for argument in actor["arguments"]] and
            function.get("return_type") == actor["return_type"] and
            function.get("signature_evidence") == actor["signature_evidence"] and
            function.get("module") == target["module"]["path"] and
            isinstance(offset, str) and re.fullmatch(r"0x[0-9a-f]+", offset) is not None and
            type(module_size) is int and module_size > 0 and int(offset, 16) < module_size and function.get("executable") is True and
            function.get("symbol") == target.get("symbol") and
            ("offset" not in target or offset == target["offset"]) and
            function.get("bridge") == ("frida-swift" if actor["abi"] == "swift" else "frida-native"))


def _qt_moc_method_run(row, actor, index):
    """Bind one explicit Qt dispatcher receipt, not a fabricated C++ body ABI."""
    from . import rea_runtime_bridge as B
    from . import rea_qt_moc as Q
    if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index:
        return False
    selection = actor["selection"]
    target = {"module": selection["module"], "offset": selection["dispatcher_offset"]}
    if not _native_entry_run(row.get("function"), target):
        return False
    try:
        Q.validate_row(row, B.invocation_request(actor)["operations"][0])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _native_proof_pins(c, run, resources):
    claims = _native_file_claims(c["actor"])
    extra = _compiler_provenance_claims(c["actor"], run.get("runtime", {}).get("compiler_provenance", {}))
    if extra is None:
        return False
    if any(path in claims and claims[path] != sha for path, sha in extra.items()):
        return False
    claims.update(extra)
    return not claims or (set(claims) <= set(resources) and
                          NP.same_data(run["runtime"].get("native_resources"), {path: resources[path] for path in claims}))


def _compiler_provenance_claims(actor, provenance):
    """Pure closure of validated compiler manifests, including SDK/source/IR bytes."""
    from . import rea_runtime_bridge as B
    adapters = [call for call in B.actor_calls(actor, allow_bindings=True) if call["kind"] == "native_function" and "compiler_adapter" in call]
    expected = {call["compiler_adapter"]["evidence"]["file"] for call in adapters}
    if not isinstance(provenance, dict) or set(provenance) != expected:
        return None
    claims = {}
    mandatory = {"generated_source", "generated_ir", "generated_helper", "outer_declaration", "toolchain_observation",
                 "original_source", "original_ir", "original_build", "original_declaration", "original_module", "original_compiler",
                 "toolchain_compiler", "toolchain_sdk_settings", "toolchain_policy", "generator_verifier_source"}
    for call in adapters:
        evidence = call["compiler_adapter"]["evidence"]
        body = provenance[evidence["file"]]
        if (not isinstance(body, dict) or set(body) != {"evidence", "artifacts", "manifest_sha256"} or
                body["evidence"] != evidence or body["manifest_sha256"] != evidence["sha256"] or
                not isinstance(body["artifacts"], list) or not 8 <= len(body["artifacts"]) <= 8192):
            return None
        roles, paths = {}, set()
        for pin in body["artifacts"]:
            if (not isinstance(pin, dict) or set(pin) != {"file", "sha256", "bytes", "roles"} or
                    not _text(pin.get("file")) or not Path(pin["file"]).is_absolute() or ".." in Path(pin["file"]).parts or
                    pin["file"] in paths or pin["file"] == evidence["file"] or not R.SHA.fullmatch(str(pin.get("sha256", ""))) or
                    type(pin.get("bytes")) is not int or not 0 <= pin["bytes"] <= 512 * 1024**2 or
                    not isinstance(pin.get("roles"), list) or not 1 <= len(pin["roles"]) <= 8 or
                    any(not _text(role, 128) for role in pin["roles"]) or len(set(pin["roles"])) != len(pin["roles"])):
                return None
            if pin["file"] in claims and claims[pin["file"]] != pin["sha256"]:
                return None
            claims[pin["file"]] = pin["sha256"]
            paths.add(pin["file"])
            for role in pin["roles"]:
                roles.setdefault(role, []).append(pin)
        if any(len(roles.get(role, [])) != 1 for role in mandatory):
            return None
        original = call["arguments"][0]["value"]["callable"]
        joined = {"generated_helper": call["target"]["module"], "original_module": original["module"],
                  "outer_declaration": {"path": call["signature_evidence"]["file"], "sha256": call["signature_evidence"]["sha256"]},
                  "original_declaration": {"path": original["signature_evidence"]["file"], "sha256": original["signature_evidence"]["sha256"]}}
        if any(roles[role][0]["file"] != claim["path"] or roles[role][0]["sha256"] != claim["sha256"] for role, claim in joined.items()):
            return None
        if not {"string", "vector", "__config"} <= {Path(pin["file"]).name for pin in roles.get("libcxx_header", [])}:
            return None
        if (not roles.get("compiler_stdout") or not roles.get("compiler_stderr") or
                len([pin for pin in roles.get("compiler_layout_probe", []) if Path(pin["file"]).name == "abi-probe.ll"]) != 1):
            return None
    return claims


def _compiler_provenance_sizes(provenance, pinned):
    return all(pin["file"] in pinned and all(pinned[pin["file"]].get(key) == pin[key] for key in ("file", "sha256", "bytes"))
               for body in provenance.values() for pin in body["artifacts"])


def _native_callable_run(row, actor, pinned):
    """A resolved callable is not a constructed wrapper or original-body entry."""
    from . import rea_runtime_bridge as B
    recipes = B.callable_recipes(actor)
    observations = row.get("callable_entries", [])
    if not isinstance(observations, list) or len(observations) != len(recipes):
        return False
    for observation, recipe in zip(observations, recipes):
        if (not isinstance(observation, dict) or observation.get("recipe") != recipe or
                observation.get("wrapper_constructed") is not False or observation.get("original_dispatched") is not False or
                observation.get("declaration") != {key: recipe[key] for key in
                                                  ("abi", "argument_types", "return_type", "context", "pointer_policy")}):
            return False
        entry, pointer = observation.get("entry"), observation.get("pointer")
        owned_return = (actor.get("compiler_adapter", {}).get("family") == "cxx_owned_string_bytes" and
                        recipe == actor["arguments"][0]["value"]["callable"])
        target = B.callable_contract(recipe, allow_owned_return=owned_return)["entry"]
        if (not _native_entry_run(entry, target) or not _native_section_pin_run(entry, pinned) or
                not isinstance(pointer, dict) or set(pointer) != {"handle", "alias", "ownership", "expires"} or
                not isinstance(pointer.get("handle"), str) or re.fullmatch(r"n(?:0|[1-9][0-9]{0,5})", pointer["handle"]) is None or
                pointer.get("alias") is not None or pointer.get("ownership") != "borrowed_no_escape" or pointer.get("expires") != "transaction"):
            return False
        module = pinned.get(target["module"]["path"])
        if not isinstance(module, dict) or entry["image_identity"] not in module.get("macho_images", []):
            return False
        if "compiler_adapter" in actor and (entry["image_identity"]["cpu_type"] != 0x0100000C or
                                           entry["image_identity"]["cpu_subtype"] & 0xFFFFFF != 0):
            return False
    return True


def _native_compiler_completion_run(row, actor):
    """Decode the fixed completion bytes independently; matching AX cannot waive it."""
    adapter = actor.get("compiler_adapter")
    completion = row.get("compiler_completion")
    if adapter is None:
        return completion is None
    if not isinstance(completion, dict):
        return False
    outputs = [output for output in row["out_buffers"] if output["argument"] == adapter["completion_argument"]]
    if len(outputs) != 1 or len(outputs[0]["bytes"]) != 24:
        return False
    if adapter["family"] == "cxx_owned_string_bytes":
        return _native_compiler_bytes_completion_run(row, actor, completion, bytes(outputs[0]["bytes"]))
    status, attempted, uncertain, reserved, value = struct.unpack("<IIIId", bytes(outputs[0]["bytes"]))
    required = {"format", "family", "argument", "bytes", "evidence", "valid", "status", "status_return",
                "dispatch_attempted", "uncertain", "reserved_zero", "value", "original_body_entry_proved"}
    return (set(completion) == required and completion["format"] == "anyuse.rea-compiler-completion/1" and
            completion["family"] == adapter["family"] and completion["evidence"] == adapter["evidence"] and
            type(completion["argument"]) is int and completion["argument"] == adapter["completion_argument"] and
            type(completion["bytes"]) is int and completion["bytes"] == 24 and completion["valid"] is True and
            type(completion["status"]) is int and completion["status"] == status == 0 and
            type(completion["status_return"]) is int and completion["status_return"] == 0 and
            type(row.get("value")) is int and row["value"] == 0 and attempted == 1 and uncertain == reserved == 0 and
            completion["dispatch_attempted"] is True and completion["uncertain"] is False and
            type(completion["reserved_zero"]) is int and completion["reserved_zero"] == reserved and
            type(completion["value"]) in {float, int} and math.isfinite(value) and completion["value"] == value and
            completion["original_body_entry_proved"] is False)


def _native_compiler_bytes_completion_run(row, actor, completion, raw):
    """A byte result has its own length/header and independently checked full payload."""
    adapter = actor["compiler_adapter"]
    status, attempted, uncertain, reserved, length, reserved_tail = struct.unpack("<IIIIII", raw)
    capacity = actor["arguments"][3]["type"]["bytes"]
    outputs = [output for output in row["out_buffers"] if output["argument"] == 3]
    if len(outputs) != 1 or len(outputs[0]["bytes"]) != capacity or length > capacity:
        return False
    payload = outputs[0]["bytes"]
    required = {"format", "family", "argument", "bytes", "evidence", "valid", "status", "status_return",
                "dispatch_attempted", "uncertain", "reserved_zero", "value", "original_body_entry_proved",
                "result_type", "payload_argument", "payload_bytes", "length", "length_offset", "reserved_tail_zero", "padding_zero"}
    return (set(completion) == required and completion["format"] == "anyuse.rea-compiler-bytes-completion/1" and
            completion["family"] == adapter["family"] and completion["evidence"] == adapter["evidence"] and
            type(completion["argument"]) is int and completion["argument"] == adapter["completion_argument"] == 4 and
            type(completion["bytes"]) is int and completion["bytes"] == 24 and completion["valid"] is True and
            type(completion["status"]) is int and completion["status"] == status == 0 and
            type(completion["status_return"]) is int and completion["status_return"] == 0 and
            type(row.get("value")) is int and row["value"] == 0 and attempted == 1 and uncertain == reserved == reserved_tail == 0 and
            completion["dispatch_attempted"] is True and completion["uncertain"] is False and
            type(completion["reserved_zero"]) is int and completion["reserved_zero"] == reserved and
            type(completion["reserved_tail_zero"]) is int and completion["reserved_tail_zero"] == reserved_tail and
            completion["result_type"] == "bytes" and type(completion["payload_argument"]) is int and completion["payload_argument"] == 3 and
            type(completion["payload_bytes"]) is int and completion["payload_bytes"] == capacity and
            type(completion["length_offset"]) is int and completion["length_offset"] == 16 and
            type(completion["length"]) is int and completion["length"] == length and completion["padding_zero"] is True and
            isinstance(completion["value"], list) and len(completion["value"]) == length and
            all(type(byte) is int and 0 <= byte <= 255 for byte in completion["value"]) and
            completion["value"] == payload[:length] and all(byte == 0 for byte in payload[length:]) and
            completion["original_body_entry_proved"] is False)


def _native_section_pin_run(function, pinned):
    """RX metadata is not callable without the exact pinned instruction section."""
    module = pinned.get(function["module"])
    if not isinstance(module, dict):
        return False
    segments = _native_image_segments_run(module, function["image_identity"], function["module_size"])
    if segments is None or not any(
            segment["segment"] == function["instruction_section"]["segment"] and
            int(segment["offset"], 16) <= int(function["instruction_section"]["offset"], 16) and
            int(function["instruction_section"]["offset"], 16) + function["instruction_section"]["bytes"] <=
            int(segment["offset"], 16) + segment["bytes"] for segment in segments):
        return False
    sections = module.get("macho_sections")
    return (isinstance(sections, list) and any(
        isinstance(image, dict) and set(image) == {"image_identity", "sections"} and
        image["image_identity"] == function["image_identity"] and isinstance(image["sections"], list) and
        function["instruction_section"] in image["sections"] for image in sections))


def _native_image_segments_run(module, image, declared_size):
    """An extent is not ownership: retain the exact positive segment intervals."""
    if not isinstance(module, dict) or not isinstance(module.get("macho_segments"), list):
        return None
    tables = [table for table in module["macho_segments"] if isinstance(table, dict) and
              set(table) == {"image_identity", "segments"} and table["image_identity"] == image]
    if len(tables) != 1 or not isinstance(tables[0]["segments"], list) or not 1 <= len(tables[0]["segments"]) <= 4096:
        return None
    segments = tables[0]["segments"]
    previous_end = 0
    for segment in segments:
        if (not isinstance(segment, dict) or set(segment) != {"segment", "offset", "bytes", "file_offset", "file_bytes",
                                                              "max_protection", "initial_protection"} or
                not _text(segment.get("segment"), 16) or not segment["segment"] or
                not isinstance(segment["offset"], str) or re.fullmatch(r"0x[0-9a-f]{1,16}", segment["offset"]) is None or
                type(segment["bytes"]) is not int or not 1 <= segment["bytes"] <= 512 * 1024**2 or
                any(type(segment[key]) is not int or not 0 <= segment[key] < 2**64 for key in ("file_offset", "file_bytes")) or
                any(type(segment[key]) is not int or not 0 <= segment[key] < 2**32 for key in ("max_protection", "initial_protection")) or
                int(segment["offset"], 16) < previous_end or
                segment["file_bytes"] > segment["bytes"] or
                type(module.get("bytes")) is not int or segment["file_offset"] + segment["file_bytes"] > module["bytes"]):
            return None
        previous_end = int(segment["offset"], 16) + segment["bytes"]
        if previous_end > 512 * 1024**2:
            return None
    return segments if type(declared_size) is int and declared_size == previous_end else None


def _native_segment_contains(segments, offset, width):
    return any(int(segment["offset"], 16) <= offset and offset + width <= int(segment["offset"], 16) + segment["bytes"]
               for segment in segments)


def _native_pointer_reads_run(row, actor, pinned):
    """A borrowed private pointer needs its resolved runtime ivar provenance."""
    from . import rea_runtime_bridge as B
    recipes = B.pointer_read_recipes(actor)
    observations = row.get("pointer_reads", [])
    if not isinstance(observations, list) or len(observations) != len(recipes):
        return False
    for observation, recipe in zip(observations, recipes):
        if not isinstance(observation, dict) or observation.get("recipe") != recipe:
            return False
        field = observation.get("field")
        image = observation.get("image_identity")
        pointer = observation.get("pointer")
        size = observation.get("instance_bytes")
        offset = field.get("offset") if isinstance(field, dict) else None
        if (not isinstance(field, dict) or not isinstance(offset, str) or
                re.fullmatch(r"0|[1-9][0-9]{0,19}", offset) is None or
                type(size) is not int or not 8 <= size <= 1024**2 or int(offset) + 8 > size or
                observation.get("receiver_class") != recipe["receiver_class"] or
                any(field.get(key) != recipe["field"][key] for key in ("name", "class", "encoding")) or
                field.get("module") != recipe["module"]["path"] or
                "offset" in recipe["field"] and int(offset) != recipe["field"]["offset"] or
                not isinstance(image, dict) or set(image) != {"cpu_type", "cpu_subtype", "uuid"} or
                type(image["cpu_type"]) is not int or type(image["cpu_subtype"]) is not int or
                not isinstance(image["uuid"], str) or re.fullmatch(r"[0-9a-f]{32}", image["uuid"]) is None or
                not isinstance(pointer, dict) or not {"handle", "ownership", "expires"} <= set(pointer) or
                set(pointer) - {"handle", "ownership", "expires", "alias"} or pointer.get("alias") is not None or
                not isinstance(pointer["handle"], str) or re.fullmatch(r"n(?:0|[1-9][0-9]{0,5})", pointer["handle"]) is None or
                pointer["ownership"] != "borrowed_no_escape" or pointer["expires"] != "transaction"):
            return False
        images = pinned[recipe["module"]["path"]].get("macho_images")
        if not isinstance(images, list) or image not in images:
            return False
    return True


def _swift_field_layout_run(layout, *, boolean=False):
    """Actual canonical VWT eligibility, separate from an Objective-C encoding."""
    if not isinstance(layout, dict):
        return False
    expected = {"metadata_kind": 0x200 if boolean else 0x202, "bytes": 1 if boolean else 8,
                "stride": 1 if boolean else 8, "alignment_mask": 0 if boolean else 7}
    flags = layout.get("flags")
    return (all(type(layout.get(key)) is int and layout[key] == value for key, value in expected.items()) and
            type(flags) is int and 0 <= flags < 2**32 and not flags & 0x03d10000 and
            flags & 0xff == expected["alignment_mask"] and
            all(layout.get(key) is True for key in ("pod", "bitwise_takable", "copyable", "bitwise_borrowable",
                                                    "complete", "canonical_type_verified")) and
            layout.get("addressable_for_dependencies") is False and
            layout.get("encoding") == ("bool_byte" if boolean else "zero_is_none"))


def _native_swift_reads_run(row, actor, pinned):
    """Each borrowed Swift field needs its actual reflected type and live bounds."""
    from . import rea_runtime_bridge as B
    recipes = B.swift_read_recipes(actor)
    observations = row.get("swift_field_reads", [])
    if not isinstance(observations, list) or len(observations) != len(recipes):
        return False
    for observation, recipe in zip(observations, recipes):
        if not isinstance(observation, dict) or observation.get("recipe") != recipe:
            return False
        field, pointer, image = observation.get("field"), observation.get("pointer"), observation.get("image_identity")
        size, module_size = observation.get("instance_bytes"), observation.get("module_size")
        reported_size = observation.get("reported_module_size")
        class_size, address_point = observation.get("class_size"), observation.get("class_address_point")
        if (observation.get("source") != "swift_metadata" or
                observation.get("receiver_class") != recipe["receiver_class"] or
                observation.get("swift_class") != recipe["swift_class"] or
                type(size) is not int or not 8 <= size <= 1024**2 or
                type(module_size) is not int or not 0 < module_size < 2**64 or
                type(reported_size) is not int or not 0 < reported_size < 2**64 or
                type(class_size) is not int or not 72 <= class_size <= 1024**2 or
                type(address_point) is not int or not 0 <= address_point <= class_size - 72 or
                not isinstance(field, dict) or field.get("name") != recipe["field"]["name"] or
                field.get("type_name") != recipe["field"]["type_name"] or
                field.get("type_name") != "Swift.Optional<Swift.OpaquePointer>" or
                type(field.get("offset")) is not int or not 8 <= field["offset"] <= size - 8 or field["offset"] % 8 or
                "offset" in recipe["field"] and field["offset"] != recipe["field"]["offset"] or
                type(field.get("index")) is not int or type(field.get("num_fields")) is not int or
                not 0 <= field["index"] < field["num_fields"] <= 128 or
                type(field.get("vector_offset_words")) is not int or not 1 <= field["vector_offset_words"] <= 1024**2 // 8 or
                (field["vector_offset_words"] + field["num_fields"]) * 8 > class_size - address_point or
                not _swift_field_layout_run(observation.get("layout")) or
                not isinstance(image, dict) or set(image) != {"cpu_type", "cpu_subtype", "uuid"} or
                type(image["cpu_type"]) is not int or type(image["cpu_subtype"]) is not int or
                not isinstance(image["uuid"], str) or re.fullmatch(r"[0-9a-f]{32}", image["uuid"]) is None or
                not isinstance(pointer, dict) or set(pointer) != {"handle", "alias", "ownership", "expires"} or
                not isinstance(pointer.get("handle"), str) or re.fullmatch(r"n(?:0|[1-9][0-9]{0,5})", pointer["handle"]) is None or
                pointer.get("alias") is not None or pointer.get("ownership") != "borrowed_no_escape" or
                pointer.get("expires") != "transaction" or
                observation.get("lifetime") != {"receiver_retained": True, "queue": "main", "native_handle_retained": False}):
            return False
        module = pinned.get(recipe["module"]["path"])
        if not isinstance(module, dict) or not isinstance(module.get("macho_images"), list) or image not in module["macho_images"]:
            return False
        segments = _native_image_segments_run(module, image, module_size)
        if segments is None:
            return False
        mangled_bytes = field.get("mangled_type_bytes")
        if type(mangled_bytes) is not int or not 1 <= mangled_bytes <= 1025:
            return False
        for key in ("descriptor_offset", "mangled_type_offset"):
            offset = field.get(key)
            if (not isinstance(offset, str) or re.fullmatch(r"0x[0-9a-f]{1,16}", offset) is None or
                    not _native_segment_contains(segments, int(offset, 16), 44 if key == "descriptor_offset" else mangled_bytes)):
                return False
        preconditions = observation.get("preconditions")
        expected = recipe.get("preconditions", [])
        if not isinstance(preconditions, list) or len(preconditions) != len(expected):
            return False
        indices, slots = {field["index"]}, set()
        for observed, required in zip(preconditions, expected):
            if (not isinstance(observed, dict) or observed.get("name") != required["name"] or
                    observed.get("type_name") != required["type_name"] or required["type_name"] != "Swift.Bool" or
                    type(observed.get("offset")) is not int or not 8 <= observed["offset"] < size or
                    type(observed.get("index")) is not int or not 0 <= observed["index"] < field["num_fields"] or
                    observed["index"] in indices or observed["offset"] in slots or
                    field["offset"] <= observed["offset"] < field["offset"] + 8 or
                    not _swift_field_layout_run(observed.get("layout"), boolean=True) or
                    type(observed.get("value")) is not bool or type(observed.get("expected")) is not bool or
                    type(observed.get("raw_byte")) is not int or observed["raw_byte"] not in {0, 1} or
                    observed["value"] != (observed["raw_byte"] == 1) or
                    observed["expected"] != required["equals"] or observed["value"] != required["equals"]):
                return False
            indices.add(observed["index"])
            slots.add(observed["offset"])
    return True


def _inspector_run(c, inputs, run, pinned, *, check_files=True):
    """Validate the inspector envelope without treating it as native attachment."""
    from . import rea_inspector_bridge as I
    try:
        identity = _inspector_adapter(pinned)
        receipt = run.get("runtime")
        if not isinstance(receipt, dict):
            return False
        process = receipt.get("identity")
        if (not isinstance(process, dict) or run.get("permission") != "inspector_connected" or
                run.get("primitive") != "inspector_method" or run.get("pid") != process.get("pid") or
                run.get("uid") != process.get("uid") or run.get("bundle") != process.get("bundle") or
                run.get("timed_out") is not False or
                identity["transport"]["config"]["uid"] != process.get("uid") or
                identity["adapter"]["sha256"] != R._file_hash(Path(I.__file__).resolve())):
            return False
        actor = bind(c, inputs, check_files=check_files)
        return I.receipt_valid(receipt, actor, c["target"], identity["adapter"], identity["transport"],
                               check_files=check_files)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _runtime_proof_run(c, inputs, run, pinned):
    if c["actor"]["kind"] in INSPECTOR_KINDS:
        identity = _inspector_adapter(pinned["inspector_bridge"])
        claim = c["actor"]["signature_evidence"]
        declarations = run.get("runtime", {}).get("declaration_resources")
        return (_inspector_run(c, inputs, run, identity, check_files=False) and
                isinstance(pinned.get(claim["file"]), dict) and
                pinned[claim["file"]].get("sha256") == claim["sha256"] and
                declarations == {claim["file"]: pinned[claim["file"]]})
    return (_objc_run(c, inputs, run, check_files=False) and
            _runtime_adapter(run["runtime"]["adapter"]) == _runtime_adapter(pinned["runtime_bridge"]) and
            _native_proof_pins(c, run, pinned))


def _runtime_cleanup_valid(receipt):
    """Validate paired receipt versions without inventing historical cleanup.

    Legacy /1 evidence makes no manager-close or helper-exit proposition. New
    /2 evidence cannot become legacy by dropping its additive disposition.
    """
    from . import rea_runtime_bridge as B
    result = receipt.get("result")
    if not isinstance(result, dict):
        return False
    if receipt.get("format") == B.LEGACY_RUNTIME and result.get("format") == B.LEGACY_WORKER:
        return "frida_lifecycle" not in result and "cleanup_errors" not in result
    if receipt.get("format") != B.RUNTIME or result.get("format") != B.WORKER:
        return False
    try:
        lifecycle = B._frida_lifecycle(result)
    except (KeyError, TypeError, ValueError):
        return False
    return (lifecycle["manager_state"] == "closed" and lifecycle["attachment_attempted"] is True and
            result["cleanup_errors"] == [])


def _objc_run(c, inputs, run, *, check_files=True):
    """All typed calls must complete; neither a sequence nor getter chain is atomic."""
    from . import rea_runtime_bridge as B
    receipt = run.get("runtime")
    if not isinstance(receipt, dict):
        return False
    request = B.invocation_request(bind(c, inputs, check_files=check_files))
    identity, result, adapter = receipt.get("identity"), receipt.get("result"), receipt.get("adapter")
    if not isinstance(result, dict) or not isinstance(adapter, dict) or not _runtime_cleanup_valid(receipt):
        return False
    try:
        transport = _runtime_transport(adapter.get("transport", _ABSENT_TRANSPORT))
        if (_runtime_transport(receipt.get("transport", _ABSENT_TRANSPORT)) != transport or
                _runtime_transport(result.get("transport", _ABSENT_TRANSPORT)) != transport):
            return False
    except (ValueError, TypeError):
        return False
    methods = B.actor_calls(c["actor"], allow_bindings=True)
    if any(method["kind"] in {"native_function", "qt_moc_method"} for method in methods):
        claims = _native_file_claims(c["actor"])
        base_claims = set(claims)
        provenance = receipt.get("compiler_provenance", {})
        extra = _compiler_provenance_claims(c["actor"], provenance)
        if (extra is None or result.get("compiler_provenance", {}) != provenance or
                any(path in claims and claims[path] != sha for path, sha in extra.items())):
            return False
        claims.update(extra)
        pinned = receipt.get("native_resources")
        if (not isinstance(pinned, dict) or set(pinned) != set(claims) or not NP.same_data(result.get("native_resources"), pinned) or
                result.get("native_state_uncertain") is not False or any(
                    not isinstance(pinned[path], dict) or pinned[path].get("file") != path or
                    pinned[path].get("sha256") != sha or type(pinned[path].get("bytes")) is not int or
                    pinned[path]["bytes"] < 0 or (path in base_claims and pinned[path]["bytes"] == 0)
                    for path, sha in claims.items()) or
                not _compiler_provenance_sizes(provenance, pinned)):
            return False
    rows = result.get("operations")
    if not isinstance(rows, list) or len(rows) != len(methods) or not all(
            (_native_function_run(row, actor, index) if actor["kind"] == "native_function" else
             _qt_moc_method_run(row, actor, index) if actor["kind"] == "qt_moc_method" else
             _objc_method_run(row, actor, index)) for index, (row, actor) in enumerate(zip(rows, methods))):
        return False
    compiler_actor = any("compiler_adapter" in method for method in methods)
    loads = result.get("helper_load_attempts", None if compiler_actor else 0)
    resolutions = sum(len(B.callable_recipes(method)) for method in methods if method["kind"] == "native_function")
    resolved = result.get("callable_resolutions", None if resolutions else 0)
    if type(loads) is not int or loads != 0 or type(resolved) is not int or resolved != resolutions:
        return False
    try:
        for operation, row in zip(request["operations"], rows):
            B.validate_native_path_observations(operation, row)
        B.validate_native_path_counters(request, rows, result.get("native_calls"))
        B.validate_lifecycle_observer_result(request, result)
        if any(method["kind"] == "qt_moc_method" for method in methods):
            B.validate_qt_moc_result(request, result)
    except (KeyError, TypeError, ValueError):
        return False
    for row, actor in zip(rows, methods):
        if actor["kind"] == "native_function":
            images = receipt["native_resources"][actor["target"]["module"]["path"]].get("macho_images")
            if (not isinstance(images, list) or row["function"]["image_identity"] not in images or
                    not _native_section_pin_run(row["function"], receipt["native_resources"]) or
                    not _native_pointer_reads_run(row, actor, receipt["native_resources"]) or
                    not _native_swift_reads_run(row, actor, receipt["native_resources"]) or
                    not _native_callable_run(row, actor, receipt["native_resources"]) or
                    not _native_compiler_completion_run(row, actor)):
                return False
        elif actor["kind"] == "qt_moc_method":
            images = receipt["native_resources"][actor["selection"]["module"]["path"]].get("macho_images")
            if (not isinstance(images, list) or row["function"]["image_identity"] not in images or
                    not _native_section_pin_run(row["function"], receipt["native_resources"])):
                return False
    worker = receipt.get("worker")
    if not isinstance(worker, dict):
        return False
    minimum_calls = B.minimum_native_calls(c["actor"])
    return (receipt.get("status") == "executed" and
            receipt.get("target") == c["target"] and receipt.get("request") == request and
            receipt.get("request_sha256") == R.digest(request) and
            isinstance(identity, dict) and identity == receipt.get("after_identity") and
            type(identity.get("pid")) is int and identity["pid"] == run.get("pid") and
            type(identity.get("launch_time")) in {int, float} and math.isfinite(identity["launch_time"]) and identity["launch_time"] > 0 and
            identity.get("bundle_id") == c["target"]["bundle_id"] and
            identity.get("bundle") == run.get("bundle") == receipt.get("bundle") and
            isinstance(identity.get("bundle"), str) and Path(identity["bundle"]).is_absolute() and
            isinstance(identity.get("executable"), str) and Path(identity["executable"]).is_absolute() and
            type(identity.get("uid")) is int and identity["uid"] >= 0 and identity["uid"] == run.get("uid") and
            result.get("status") == "completed" and
            result.get("request_sha256") == R.digest(request) and
            result.get("attached") is True and result.get("handles_expired") is True and
            isinstance(result.get("process"), dict) and result["process"].get("pid") == identity["pid"] and
            result["process"].get("executable") == identity["executable"] and
            type(result.get("invocations")) is int and result["invocations"] == len(methods) and
            type(result.get("native_calls")) is int and result["native_calls"] >= minimum_calls and
            result.get("cleanup") == {"script_unloaded": True, "detached": True} and
            result.get("preloaded_gadget_remains", False) is (transport["kind"] == "gadget") and
            (transport["kind"] != "gadget" or
             transport["token_file"]["uid"] == transport["config"]["uid"] == identity["uid"]) and
            R.SHA.fullmatch(str(adapter.get("sha256", ""))) and
            adapter.get("source_sha256") == R._file_hash(Path(B.__file__).with_name("rea_runtime_agent.js")) and
            worker.get("returncode") == 0 and worker.get("timed_out") is False and
            run.get("permission") == "runtime_attached" and not receipt.get("resource_review_required"))


def _svg_proof_status(c, proof, current):
    """SVG proofs require retained immutable case oracles/declarations in guest.

    These are local measurements, not signatures. Current code/transport pins,
    historical reference bytes and every independent observation are checked
    before publication/replay; an evidence-only S receipt alone is insufficient.
    """
    try:
        if (current != c["target"] or not isinstance(proof, dict) or proof.get("format") != R.PROOF or
                proof.get("backend") != SVG_BACKEND or proof.get("target") != current or
                proof.get("candidate_sha256") != R.digest(c) or proof.get("status") != "verified"):
            return False, "no matching SVG learned-recipe verification receipt"
        R.assert_guest()  # Before resource/reference/source reads.
        pinned = resources(c)
        if proof.get("resources") != pinned:
            return False, "SVG observer/actor source or exact transport resources changed"
        rows = proof.get("cases")
        if not isinstance(rows, list) or not 3 <= len(rows) <= 32:
            return False, "need three fresh independent SVG checks"
        if any(not _svg_row_valid(c, row, pinned, require_change=True) for row in rows):
            return False, "receipt lacks complete independent SVG before/after/runtime checks"
        semantic = {row["semantic_inputs_sha256"] for row in rows}
        if (len(semantic) < 3 or semantic & set(c["learned_from"]["semantic_inputs"]) or
                len({row["before_state_sha256"] for row in rows}) < 2):
            return False, "SVG proof requires novel substantive task inputs and two initial content states"
        return True, "measured complete SVG DOM text; local evidence, not pixels, persistence or a signature"
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False, "SVG source/transport or retained independent reference evidence is unavailable"


def proof_status(c, proof, current):
    if c.get("backend") == STATE_BACKEND:
        from . import rea_state_campaign as C
        return C.proof_status(c, proof, current)
    if c.get("backend") == SVG_BACKEND:
        return _svg_proof_status(c, proof, current)
    if c.get("actor", {}).get("kind") in NATIVE_RUNTIME_KINDS:
        R.assert_guest()  # Current native resources are guest-only, even for publication.
    if current != c["target"]:
        return False, "app fingerprint changed; relearn and reverify"
    if (not isinstance(proof, dict) or proof.get("format") != R.PROOF or proof.get("backend") != BACKEND or
            proof.get("candidate_sha256") != R.digest(c) or proof.get("target") != current or proof.get("status") != "verified"):
        return False, "no matching learned-recipe verification receipt"
    cases = proof.get("cases", [])
    pinned = proof.get("resources")
    if not isinstance(pinned, dict) or any(
            pinned.get(f["dictionary_path"], {}).get("sha256") != f["dictionary_sha256"] or
            pinned.get(f["dictionary_path"], {}).get("label") != f["dictionary_label"]
            for f in c["reference"]["fields"].values() if f["kind"] == "dictionary_xml"):
        return False, "receipt does not bind the independent reference resources"
    if c["actor"]["kind"] in NATIVE_RUNTIME_KINDS and not isinstance(pinned.get("runtime_bridge"), dict):
        return False, "receipt does not bind the deployed runtime adapter"
    if c["actor"]["kind"] in INSPECTOR_KINDS:
        try:
            _inspector_adapter(pinned.get("inspector_bridge"))
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return False, "receipt does not bind the deployed inspector adapter and explicit transport"
    if c["actor"]["kind"] in NATIVE_RUNTIME_KINDS:
        try:
            if any(not isinstance(pinned.get(path), dict) or pinned[path].get("file") != path or
                   pinned[path].get("sha256") != sha for path, sha in _native_file_claims(c["actor"]).items()):
                return False, "receipt does not bind native modules and attributable ABI contracts"
        except (KeyError, TypeError, ValueError):
            return False, "invalid native file provenance"
    if not isinstance(cases, list) or len(cases) < 3:
        return False, "need at least three fresh independent checks"
    for row in cases:
        try:
            bind(c, row["inputs"], check_files=False)
            valid = (row["inputs_sha256"] == R.digest(row["inputs"]) and row["verdict"] == "verified" and
                     row["observer"] == "learned_ax" and row["before"] != row["expected"] and
                     row["expected"] == row["seen"] and set(row["expected"]) == set(c["reference"]["fields"]) and
                     all(isinstance(v, str) and v for v in row["expected"].values()) and
                     R.SHA.fullmatch(row["before_ax_sha256"]) and R.SHA.fullmatch(row["reference_sha256"]) and
                     row["run"]["returncode"] == 0 and not row["run"]["timed_out"] and
                     (row["run"].get("permission") == 0 if c["actor"]["kind"] == "apple_event" else
                      _runtime_proof_run(c, row["inputs"], row["run"], pinned)
                      if c["actor"]["kind"] in RUNTIME_KINDS else
                      row["run"].get("permission") == "not_required"))
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            return False, "receipt lacks complete independent output checks"
    if len({r["inputs_sha256"] for r in cases}) < 3 or len({r["before_ax_sha256"] for r in cases}) < 2:
        return False, "need three input sets and two observed initial GUI states"
    if c["actor"]["kind"] in NATIVE_RUNTIME_KINDS:
        try:
            current_resources = resources(c)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"pinned native resource changed or unavailable: {exc}"
        if not _resources_equal(current_resources, pinned):
            return False, "pinned reference resource changed"
    return True, "measured declarative recipe; local evidence, not a signature"


def verify(c, cases, bundle, out, timeout):
    R.assert_guest()
    if c.get("backend") == STATE_BACKEND:
        from . import rea_state_campaign as C
        return C.verify(c, cases, bundle, out, timeout)
    c = validate(c)
    root = Path(bundle).resolve(strict=True)
    if R.fingerprint(root) != c["target"]:
        raise ValueError("candidate target changed")
    if (not 1 <= timeout <= 300 or not isinstance(cases, list) or not 3 <= len(cases) <= 32 or
            any(not isinstance(case, dict) or set(case) != {"inputs"} for case in cases) or
            len({R.digest(case["inputs"]) for case in cases}) < 3):
        raise ValueError("need 3–32 distinct input-only verification cases")
    for case in cases:
        _inputs(c["params"], case["inputs"], reference=c["reference"])
    if c["backend"] == SVG_BACKEND:
        semantic = {R.digest(semantic_inputs(c, case["inputs"])) for case in cases}
        if len(semantic) < 3 or semantic & set(c["learned_from"]["semantic_inputs"]):
            raise ValueError("SVG verification needs three fresh substantive task inputs, not new proof-file names")
    _path_roles(c["params"], [case["inputs"] for case in cases], c["reference"])
    pinned = resources(c)
    references = [expected(c, case["inputs"]) for case in cases]
    folder = Path(out).resolve()
    _outside(c, root, folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    proof = {"format": R.PROOF, "backend": c["backend"], "target": c["target"], "candidate_sha256": R.digest(c),
             "resources": pinned, "status": "incomplete", "cases": [], "model_calls": 0}
    try:
        runtime = Runtime(root, c)
        for i, (case, reference) in enumerate(zip(cases, references)):
            if c["actor"]["kind"] in RUNTIME_KINDS and resources(c) != pinned:
                raise ValueError("deployed runtime adapter changed before a verification case")
            work = folder / f"case-{i}"
            work.mkdir(mode=0o700)
            row = _trial(c, case["inputs"], runtime, work, reference, timeout, require_change=True)
            proof["cases"].append(row)
            R.write_json(folder / "proof.json", proof)
            if row["verdict"] != "verified":
                raise ValueError("learned recipe failed; stopped without retry or fallback")
        if R.fingerprint(root) != c["target"] or resources(c) != pinned:
            raise ValueError("app or pinned resources changed during verification")
        proof["status"] = "verified"
        ready, why = proof_status(c, proof, c["target"])
        if not ready:
            proof["status"] = "incomplete"
            raise ValueError(why)
    except Exception as exc:
        proof["error"] = str(exc)
        raise
    finally:
        R.write_json(folder / "proof.json", proof)
    return proof


def replay(c, proof, inputs, bundle, out, timeout):
    R.assert_guest()
    if c.get("backend") == STATE_BACKEND:
        from . import rea_state_campaign as C
        return C.replay(c, proof, inputs, bundle, out, timeout)
    c = validate(c)
    root = Path(bundle).resolve(strict=True)
    ready, why = proof_status(c, proof, R.fingerprint(root))
    if not ready:
        raise ValueError(why)
    if not 1 <= timeout <= 300:
        raise ValueError("timeout must be 1–300 seconds")
    pinned = resources(c)
    if not _resources_equal(pinned, proof["resources"]):
        raise ValueError("pinned reference resource changed")
    reference = expected(c, inputs)
    folder = Path(out).resolve()
    _outside(c, root, folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"verdict": "uncertain", "retry": False, "model_calls": 0,
              "candidate_sha256": R.digest(c), "observer": c["observer"]["kind"] if c["backend"] == SVG_BACKEND else "learned_ax"}
    try:
        result.update(_trial(c, inputs, Runtime(root, c), folder, reference, timeout, require_change=False))
        if R.fingerprint(root) != c["target"] or resources(c) != pinned:
            result.update(verdict="refuted", error="app or reference resource changed during replay")
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        R.write_json(folder / "replay.json", result)
    return result


def run(profile, demos, cases, heldout, bundle, out, timeout=30):
    """One shared learn/verify/held-out driver. Profiles supply data, never code."""
    R.assert_guest()
    if not isinstance(heldout, list) or len(heldout) < 3 or any(not isinstance(x, dict) or set(x) != {"inputs"} for x in heldout):
        raise ValueError("need at least three held-out input sets")
    learning = {R.digest(semantic_inputs(profile, d["inputs"])) for d in demos}
    checking = {R.digest(semantic_inputs(profile, c["inputs"])) for c in cases}
    novel = {R.digest(semantic_inputs(profile, c["inputs"])) for c in heldout}
    if len(novel) != len(heldout) or novel & (learning | checking):
        raise ValueError("held-out inputs must be distinct from learning and verification")
    _params(profile["params"])
    _path_roles(profile["params"], [demo["inputs"] for demo in demos] +
                [case["inputs"] for case in cases] + [case["inputs"] for case in heldout], profile["reference"])
    folder = Path(out).resolve()
    root = Path(bundle).resolve(strict=True)
    if folder.is_relative_to(root):
        raise ValueError("harness receipts must be outside the target app")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"format": "anyuse.rea-harness-run/1", "status": "incomplete", "heldout": [], "model_calls": 0}
    try:
        c = learn(profile, demos, bundle, str(folder / "learn"))
        # Pre-read every held-out oracle before verification begins; GUI results
        # remain unobserved until the recipe is frozen and its proof passes.
        heldout_references = [expected(c, h["inputs"]) for h in heldout]
        proof = R.verify(c, cases, bundle, str(folder / "verify"), timeout=timeout)
        report.update(candidate_sha256=R.digest(c), target=c["target"], verified_cases=len(proof["cases"]))
        for i, (h, reference) in enumerate(zip(heldout, heldout_references)):
            if expected(c, h["inputs"]) != reference:
                raise ValueError("held-out reference changed")
            row = R.replay(c, proof, h["inputs"], bundle, str(folder / f"heldout-{i}"), timeout=timeout)
            report["heldout"].append(row)
            R.write_json(folder / "run.json", report)
            if row["verdict"] != "verified":
                raise ValueError("held-out check failed; stopped without retry")
        report["status"] = "verified"
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        R.write_json(folder / "run.json", report)
    return report
