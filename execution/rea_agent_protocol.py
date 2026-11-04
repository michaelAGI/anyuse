"""Opt-in, closed JSON transport for the existing Objective-C discovery subset.

This is syntax and pure validation, never a runtime, observer or proof authority.
The driver supplies decoded JSON and its already redacted model view. G retains
every current-session citation, target, process, ABI, effect and publication gate.
"""
from __future__ import annotations

import copy
import json
import math
import re

from . import rea_runtime_bridge as B
from . import rea_symbols as S

FORMAT = "full_native_v1"
SUPPORTED_TOOLS = ("analyze", "symbols", "evidence", "resource", "discover", "runtime", "propose")
MAX_WIRE_BYTES = 64 * 1024
INSTRUCTIONS = (
    "Return exactly action, request and reason as a native JSON object. For step, request is one admitted tool object; for stop, request is null.",
    "For both step and stop, reason must contain 1 to 1200 characters and no NUL. Keep it concise; the action and request have their separate declared fields.",
    "Every schema field is required on the wire. Use null only for an unused nullable field; the harness omits that optional field. A typed nil argument keeps value=null.",
    "This protocol supports the declared static tools and existing native Objective-C metadata, typed reads, invocation and one saved objc_method proposal. It adds no runtime capability.",
    "Use provider=native explicitly. Preserve measured signatures and fresh receiver recipes. Transaction aliases expire after each request and cannot be saved in an actor.",
    "A proposal binds the exact caller-approved observer_profile_sha256 and current-session citations. Do not supply a profile, trial values, setup actions, restart evidence or proof.",
    "Discover exact class names before requesting methods or ivars; a plausible name is not evidence. Inspect matched/truncated before treating a bounded result as exhaustive. Narrow a literal query or increase its declared limit when needed.",
    "Static analyze.query is a REA search pattern, not a natural-language question. To obtain a measured method's body, use analyze.function with its exact procedure name or Objective-C spelling such as -[ObservedClass observedSelector:]. Keep names grounded in current evidence; put investigation explanations in reason, not query.",
    "A negative_metadata_observation retains a failed metadata-only query with confirmed zero native calls and detachment. Read its retained error and partial observations, then choose a different evidence-supported investigation. It establishes no callable boundary or task proof. Execution or cleanup uncertainty still stops the driver.",
    "If a complete method query lacks a task-related selector, investigate another observed class or static evidence. Repeating that complete query without a changed basis supplies no new evidence. Never propose a method absent from the measured method rows, and distinguish class and instance dispatch.",
    "Native discover feedback includes bounded metadata in observation with its raw source artifact/hash. Check omitted rows and operations; read that registered source for full facts and exact citation pointers. A metadata summary is not a callable or task proof.",
)


def _need(value, message):
    if not value:
        raise ValueError(message)


def _object(fields):
    return {"type": "object", "additionalProperties": False,
            "properties": fields, "required": list(fields)}


def _text(maximum=4096, minimum=1, pattern=None):
    value = {"type": "string", "minLength": minimum, "maxLength": maximum}
    if pattern is not None:
        value["pattern"] = pattern
    return value


def _enum(*values):
    return {"type": "string", "enum": list(values),
            "minLength": min(map(len, values)), "maxLength": max(map(len, values))}


def _optional(spec):
    # Internal metadata is removed from the public provider schema.
    return {"anyOf": [spec, {"type": "null"}], "_omit_null": True}


def _array(spec, maximum, minimum=0):
    return {"type": "array", "items": spec, "minItems": minimum, "maxItems": maximum}


def _integer(lo, hi):
    return {"type": "integer", "minimum": lo, "maximum": hi}


def _ref(name):
    return {"$ref": "#/$defs/" + name}


def _admitted(allowed_tools):
    values = SUPPORTED_TOOLS if allowed_tools is None else allowed_tools
    _need(type(values) in {tuple, list} and all(type(v) is str and v in SUPPORTED_TOOLS for v in values)
          and len(set(values)) == len(values), "full_native_v1 requires a unique supported tool subset")
    return tuple(values)


def _argument(saved):
    variants = []
    for kind in sorted(B.TYPES - {"void"} - ({"object"} if saved else set())):
        if kind == "nil":
            value = {"type": "null"}
        elif kind == "object":
            value = _ref("alias")
        elif kind == "utf8":
            value = _text(4096, 0)
        elif kind == "bool":
            value = {"type": "boolean"}
        elif kind in {"float", "double"}:
            bound = 3.402823466e38 if kind == "float" else 1.7976931348623157e308
            value = {"type": "number", "minimum": -bound, "maximum": bound}
        elif kind in {"int64", "uint64", "long", "ulong"}:
            value = _text(21, 1, r"^(?:0|[1-9][0-9]{0,19}|-[1-9][0-9]{0,18})$")
        else:
            bits, signed = int(re.search(r"\d+", kind)[0]), kind.startswith("int")
            value = _integer(-(2 ** (bits - 1)) if signed else 0,
                             (2 ** (bits - (1 if signed else 0))) - 1)
        if saved and kind in B.OBJC_BINDING_TYPES:
            value = {"anyOf": [value, _ref("binding")]}
        variants.append(_object({"type": _enum(kind), "value": value}))
    return {"anyOf": variants}


def _spec(allowed_tools):
    admitted = _admitted(allowed_tools)
    name = _text(64, 1, r"^[a-z][a-z0-9_]{0,63}$")
    identifier = _text(256, 1, r"^[A-Za-z_][A-Za-z0-9_.$]{0,255}$")
    klass = _text(256, 1, r"^[.]*[A-Za-z_][A-Za-z0-9_.$]*$")
    selector, signature = _text(1024), _text(1024)
    definitions = {
        "alias": _object({"alias": name}), "binding": _object({"input": name}),
        "getter_path": _array(_object({"selector": selector, "signature": signature}), 8),
    }
    recipes = [_object({"class": klass, "kind": _enum("class"), "path": _optional(_ref("getter_path"))}),
               _object({"class": klass, "kind": _enum("unique"),
                        "subclasses": _optional({"type": "boolean"}), "path": _optional(_ref("getter_path"))})]
    definitions["saved_receiver"] = {"anyOf": recipes}
    definitions["receiver"] = {"anyOf": [*recipes, _ref("alias")]}
    definitions["argument"] = _argument(False)
    definitions["actor_argument"] = _argument(True)
    field_types = _enum(*sorted(B.TYPES - {"void", "nil"}))
    definitions["field"] = {"anyOf": [
        _object({"name": name, "kind": _enum("getter"), "type": field_types,
                 "selector": selector, "signature": signature, "alias": _optional(name)}),
        _object({"name": name, "kind": _enum("ivar"), "type": field_types,
                 "ivar": identifier, "encoding": signature, "alias": _optional(name)}),
    ]}
    metadata = [
        _object({"op": _enum("classes"), "query": _optional(_text(256, 0)),
                 "owner": _optional(_enum("app", "all")), "limit": _optional(_integer(1, 128))}),
        _object({"op": _enum("methods", "ivars"), "class": klass,
                 "query": _optional(_text(256, 0)), "own": _optional({"type": "boolean"}),
                 "limit": _optional(_integer(1, 128))}),
        _object({"op": _enum("instances"), "class": klass, "alias": name,
                 "subclasses": _optional({"type": "boolean"})}),
    ]
    definitions["metadata"] = {"anyOf": metadata}
    definitions["runtime_operation"] = {"anyOf": [
        _ref("metadata"),
        _object({"op": _enum("read"), "receiver": _ref("receiver"),
                 "fields": _array(_ref("field"), 32, 1)}),
        _object({"op": _enum("invoke"), "receiver": _ref("receiver"), "selector": selector,
                 "signature": signature, "arguments": _array(_ref("argument"), 8),
                 "return_type": _enum(*sorted(B.TYPES)), "alias": _optional(name)}),
    ]}
    definitions["actor"] = _object({"kind": _enum("objc_method"), "receiver": _ref("saved_receiver"),
        "selector": selector, "signature": signature, "arguments": _array(_ref("actor_argument"), 8),
        "return_type": _enum(*sorted(B.TYPES))})
    tools = {
        "analyze": _object({"op": _enum("analyze"), "query": _optional(_text()),
            "function": _optional(_text()), "timeout": _optional(_integer(1, 900))}),
        "symbols": _object({"op": _enum("symbols"), "query": _optional(_text(S.MAX_QUERY_BYTES, 0)),
            "limit": _optional(_integer(1, S.MAX_ROWS)), "timeout": _optional(_integer(1, 30))}),
        "evidence": _object({"op": _enum("evidence"), "artifact": _text(),
            "offset": _optional(_integer(0, 16 * 1024**2)), "limit": _optional(_integer(1, 16384))}),
        "resource": _object({"op": _enum("resource"), "relative": _text()}),
        "propose": _object({"op": _enum("propose"), "rationale": _text(),
            "citations": _array(_object({"artifact": _text(), "pointer": _text(1024, 1, r"^/.*$")}), 16, 1),
            "actor": _ref("actor"), "observer_profile_sha256": _text(64, 64, r"^[0-9a-f]{64}$")}),
    }
    for op, definition in (("discover", "metadata"), ("runtime", "runtime_operation")):
        tools[op] = _object({"op": _enum(op), "provider": _enum("native"),
            "request": _object({"operations": _array(_ref(definition), 16, 1)}),
            "timeout": _optional(_integer(1, 60))})
    result = _object({"action": _enum(*(["step", "stop"] if admitted else ["stop"])),
        "request": {"anyOf": [{"type": "null"}, *[tools[op] for op in admitted]]} if admitted else {"type": "null"},
        "reason": _text(1200)})
    result["$defs"] = definitions
    return result


def _wire_spec(allowed_tools, nullable_omissions=False):
    _need(type(nullable_omissions) is bool, "nullable omission mode must be a boolean")
    def adapt(value):
        if type(value) is dict:
            result = {key: adapt(child) for key, child in value.items()}
            if nullable_omissions and result.get("type") == "object":
                result["required"] = [key for key, field in result["properties"].items()
                                      if not field.get("_omit_null")]
            return result
        if type(value) is list:
            return [adapt(child) for child in value]
        return value
    return adapt(_spec(allowed_tools))


def schema(allowed_tools=None, *, nullable_omissions=False):
    """Strict provider schema: closed objects, explicit nulls, bounded native data."""
    def public(value):
        if type(value) is dict:
            return {k: public(v) for k, v in value.items() if k != "_omit_null"}
        if type(value) is list:
            return [public(v) for v in value]
        return value
    return public(_wire_spec(allowed_tools, nullable_omissions))


def _normalize(value, spec, definitions, location="$"):
    if "$ref" in spec:
        return _normalize(value, definitions[spec["$ref"].removeprefix("#/$defs/")], definitions, location)
    if "anyOf" in spec:
        errors = []
        for branch in spec["anyOf"]:
            try:
                return _normalize(value, branch, definitions, location)
            except ValueError as error:
                errors.append((branch, error))
        # Report a matching declared operation/kind's real validation error;
        # this improves feedback without accepting or repairing any value.
        if type(value) is dict:
            for branch, error in errors:
                while "$ref" in branch:
                    branch = definitions[branch["$ref"].removeprefix("#/$defs/")]
                fields = branch.get("properties", {})
                if any(key in value and value[key] in fields.get(key, {}).get("enum", [])
                       for key in ("op", "kind")):
                    raise ValueError(str(error)) from None
        for branch, error in errors:
            while "$ref" in branch:
                branch = definitions[branch["$ref"].removeprefix("#/$defs/")]
            exact_types = {"null": type(None), "object": dict, "array": list,
                           "string": str, "boolean": bool, "integer": int}
            if "enum" in branch and (type(value) is exact_types.get(branch.get("type")) or
                    branch.get("type") == "number" and type(value) in {int, float}):
                raise ValueError(str(error)) from None
        raise ValueError(location + ": value does not match a declared full-native alternative")
    kind = spec["type"]
    valid = {"null": value is None, "object": type(value) is dict, "array": type(value) is list,
             "string": type(value) is str, "boolean": type(value) is bool,
             "integer": type(value) is int, "number": type(value) in {int, float}}[kind]
    _need(valid, "invalid full-native JSON type")
    if "enum" in spec:
        _need(value in spec["enum"], location + ": value is outside the full-native enum; allowed=" + repr(spec["enum"]))
    if kind == "object":
        expected = set(spec["properties"])
        required = set(spec["required"])
        _need(required <= set(value) <= expected, location + ": full-native object needs its declared wire fields; " +
              "missing=" + repr(sorted(required - set(value))) + "; unexpected=" + repr(sorted(set(value) - expected)))
        result = {}
        for key, field in spec["properties"].items():
            if key not in value:
                continue  # Only declared _omit_null fields can be absent.
            normalized = _normalize(value[key], field, definitions, location + "." + key)
            if normalized is not None or not field.get("_omit_null"):
                result[key] = normalized
        return result
    if kind == "array":
        _need(spec["minItems"] <= len(value) <= spec["maxItems"], "full-native list exceeds its declared bound")
        return [_normalize(v, spec["items"], definitions, location + "[" + str(index) + "]") for index, v in enumerate(value)]
    if kind == "string":
        _need("\0" not in value and spec["minLength"] <= len(value) <= spec["maxLength"],
              location + ": invalid bounded full-native text; expected " + str(spec["minLength"]) + ".." +
              str(spec["maxLength"]) + " characters without NUL; received " + str(len(value)) +
              " characters; contains_NUL=" + str("\0" in value))
        _need("pattern" not in spec or re.fullmatch(spec["pattern"], value) is not None, "invalid full-native text pattern")
    if kind in {"integer", "number"}:
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        _need(finite and spec["minimum"] <= value <= spec["maximum"], "invalid bounded full-native number")
    return copy.deepcopy(value)


def normalize_request(decision, view):
    """Validate decoded wire data and return a canonical G request or None.

    The driver must reject duplicate JSON keys while decoding. This function
    never parses/repairs a nested JSON string and never reads evidence files.
    """
    _need(type(view) is dict and type(view.get("tools")) is dict, "full-native protocol needs the effective model view")
    nullable_omissions = view.get("decision_protocol", {}).get("nullable_omissions", False)
    spec = _wire_spec(list(view["tools"]), nullable_omissions)
    try:
        raw = json.dumps(decision, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("full-native decision must be finite JSON data") from error
    _need(len(raw) <= MAX_WIRE_BYTES, "full-native decision exceeds byte bound")
    normalized = _normalize(decision, spec, spec["$defs"])
    _need(bool(normalized["reason"].strip()), "full-native reason must be nonempty")
    request = normalized["request"]
    if normalized["action"] == "stop":
        _need(request is None, "full-native STOP requires a null request")
        return None
    _need(type(request) is dict, "full-native step requires a native JSON request object")
    op = request["op"]
    if op in {"discover", "runtime"}:
        request["request"] = B.validate_request(request["request"])
    elif op == "propose":
        _need(request["observer_profile_sha256"] == view.get("observer_profile_sha256"),
              "proposal must bind the caller-approved observer hash")
        request["actor"] = B.validate_actor(request["actor"], allow_bindings=True)
        names = {p["name"] for p in view.get("params", [])}
        for argument in request["actor"]["arguments"]:
            if type(argument["value"]) is dict:
                _need(argument["value"]["input"] in names, "actor input binding is outside frozen parameters")
        for citation in request["citations"]:
            _need(citation["artifact"] in view.get("artifacts", {}) and
                  re.search(r"~(?![01])", citation["pointer"]) is None,
                  "proposal citations require visible current-session artifacts and JSON pointers")
    elif op == "evidence":
        _need(request["artifact"] in view.get("artifacts", {}), "evidence is outside visible investigation artifacts")
    elif op == "symbols" and "query" in request:
        query = request["query"]
        _need(len(query.encode()) <= S.MAX_QUERY_BYTES and all(c.isprintable() for c in query),
              "symbol query must be bounded literal text")
    elif op == "analyze":
        _need(all(len(request[k].encode()) <= 4096 and request[k].strip()
                  for k in ("query", "function") if k in request), "analysis argument exceeds bounded text")
    return request


# Separate opt-in wire transport. The native protocol above keeps its schema,
# admitted tools and canonical requests unchanged.
INSPECTOR_FORMAT = "inspector_structured_v1"
INSPECTOR_SUPPORTED_TOOLS = ("resource", "evidence", "discover", "declare_inspector", "propose")
INSPECTOR_INSTRUCTIONS = (
    "Return exactly action, request and reason as a JSON object. For step, request is one admitted tool object; for stop, request is null. Never send request_json or a nested JSON string.",
    "Every schema field is required on the wire. Use null for an unused nullable optional field; the harness omits it before dispatch. A typed null argument keeps value=null.",
    "Use provider=inspector explicitly. Recipes are data: literal global or DOM roots and typed property, index, map-key, private-property, measured scope or unique-instance steps. Never supply JavaScript, evaluation expressions, live object IDs or generated code.",
    "This closed subset admits resource, evidence, inspector metadata discovery, declare_inspector and propose only. Runtime read/call, analyze, symbols, task trials, setup, restart and publication are not wire tools here.",
    "Discover the exact current method and its function-source hash. Cite its completed raw sNNNN/runtime/inspector.json operation at /operations/N; summaries, resource text and old sessions are not method citations.",
    "declare_inspector takes the typed actor without signature_evidence. Its returned declared_js actor is a model-declared hypothesis, not automatically recovered argument types, independent review or task proof. For propose, use that returned actor and its exact registered declaration pin with pointer /contract.",
    "A proposal binds the caller-approved observer_profile_sha256 and current raw method citations. Supply no profile, task values, trials, setup actions, restart evidence or proof. G still validates the current declaration, target, process, receiver, method body and evidence provenance.",
    "Arguments admit explicit primitive values or frozen input bindings. The json argument type accepts a frozen input binding only in this closed wire subset; literal JSON containers are unsupported. Input names must come from the visible frozen params.",
    "Inspect truncation and page offsets before claiming complete metadata. Function scopes and prototype-instance queries require their measured hashes and existing budgets; getters are refused for these two discovery operations. Explicit getter traversal elsewhere can execute app code. Heap queries may force GC or retain objects until cleanup and do not establish task success.",
)


def _inspector_admitted(allowed_tools):
    values = INSPECTOR_SUPPORTED_TOOLS if allowed_tools is None else allowed_tools
    _need(type(values) in {tuple, list} and all(type(v) is str and v in INSPECTOR_SUPPORTED_TOOLS for v in values)
          and len(set(values)) == len(values), "inspector_structured_v1 requires a unique supported tool subset")
    return tuple(values)


def _inspector_primitive(kind, *, maximum=65536):
    if kind == "null":
        return {"type": "null"}
    if kind == "utf8":
        return _text(maximum, 0)
    if kind == "bool":
        return {"type": "boolean"}
    if kind == "number":
        return {"type": "number", "minimum": -(2**53 - 1), "maximum": 2**53 - 1}
    return _integer(-(2**31) if kind == "int32" else 0, 2**31 - 1 if kind == "int32" else 2**32 - 1)


def _inspector_spec(allowed_tools):
    from . import rea_inspector_bridge as I
    admitted = _inspector_admitted(allowed_tools)
    name = _text(64, 1, r"^[a-z][a-z0-9_]{0,63}$")
    sha = _text(64, 64, r"^[0-9a-f]{64}$")
    primitive_types = sorted(I.TYPES - {"json"})
    definitions = {"binding": _object({"input": name})}
    definitions["primitive"] = {"anyOf": [
        _object({"type": _enum(kind), "value": _inspector_primitive(kind)}) for kind in primitive_types]}
    definitions["invariant"] = {"anyOf": [
        _object({"name": _text(256), "type": _enum(kind),
                 "equals": _inspector_primitive(kind, maximum=1024)}) for kind in primitive_types]}
    definitions["scope"] = _object({"index": _integer(0, I.MAX_SCOPE_CHAIN - 1),
        "description": _text(512), "chain_sha256": sha, "bindings_sha256": sha})
    definitions["root"] = {"anyOf": [
        _object({"kind": _enum("global"), "name": _text(256)}),
        _object({"kind": _enum("dom"), "selector": _text(1024)}),
    ]}
    definitions["step"] = {"anyOf": [
        _object({"kind": _enum("property"), "name": _text(256), "access": _enum("data")}),
        _object({"kind": _enum("property"), "name": _text(256), "access": _enum("getter"), "getter_sha256": sha}),
        _object({"kind": _enum("index"), "index": _integer(0, 65535)}),
        _object({"kind": _enum("private_property"), "name": _text(256)}),
        _object({"kind": _enum("map_key"), "key": _ref("primitive")}),
        _object({"kind": _enum("scope_binding"), "function_sha256": sha,
                 "scope": _ref("scope"), "name": _text(512)}),
        _object({"kind": _enum("unique_instance"), "constructor_sha256": sha,
                 "max_candidates": _integer(1, I.MAX_INSTANCE_CANDIDATES),
                 "own_invariants": _array(_ref("invariant"), I.MAX_INSTANCE_INVARIANTS)}),
    ]}
    definitions["recipe"] = _object({"root": _ref("root"), "steps": _array(_ref("step"), I.MAX_STEPS)})
    arguments = []
    for kind in sorted(I.TYPES):
        value = _ref("binding") if kind == "json" else {"anyOf": [_inspector_primitive(kind), _ref("binding")]}
        arguments.append(_object({"type": _enum(kind), "value": value}))
    definitions["argument"] = {"anyOf": arguments}
    actor = {"kind": _enum("inspector_method"), "receiver": _ref("recipe"),
             "method": _text(256), "function_sha256": sha,
             "arguments": _array(_ref("argument"), 16), "return_type": _enum(*sorted(I.TYPES | {"void"}))}
    definitions["unsigned_actor"] = _object(actor)
    definitions["actor"] = _object({**actor, "signature_evidence": _object({
        "kind": _enum("declared_js"), "file": _text(4096, 1, r"^/.*$"),
        "sha256": sha, "pointer": _enum("/contract")})})
    definitions["citation"] = _object({"artifact": _text(4096, 1, r"^s[0-9]{4}/runtime/inspector[.]json$"),
        "pointer": _text(1024, 1, r"^/operations/(?:0|[1-9][0-9]*)$")})
    definitions["discovery"] = {"anyOf": [
        _object({"op": _enum("dom_query"), "selector": _text(1024), "max_nodes": _integer(1, 32)}),
        _object({"op": _enum("describe"), "recipe": _ref("recipe"), "max_properties": _integer(1, 32),
                 "prototype_depth": _optional(_integer(0, 4)), "offset": _optional(_integer(0, I.MAX_DESCRIPTOR_OFFSET)),
                 "property_query": _optional(_text(I.MAX_PROPERTY_QUERY_BYTES, 0))}),
        _object({"op": _enum("method"), "recipe": _ref("recipe"), "method": _text(256)}),
        _object({"op": _enum("function_scopes"), "recipe": _ref("recipe"), "function_sha256": sha,
                 "max_scopes": _integer(1, I.MAX_SCOPE_PREVIEW), "max_bindings": _integer(1, I.MAX_SCOPE_PREVIEW)}),
        _object({"op": _enum("prototype_instances"), "recipe": _ref("recipe"), "constructor_sha256": sha,
                 "max_instances": _integer(1, I.MAX_INSTANCE_PREVIEW), "max_properties": _integer(1, I.MAX_INSTANCE_PREVIEW)}),
        _object({"op": _enum("graph"), "recipe": _ref("recipe"), "max_properties": _integer(1, 32),
                 "max_nodes": _optional(_integer(1, 64)), "max_depth": _optional(_integer(0, 6))}),
        _object({"op": _enum("map_keys"), "recipe": _ref("recipe"), "max_keys": _integer(1, 32)}),
    ]}
    tools = {
        "resource": _object({"op": _enum("resource"), "relative": _text()}),
        "evidence": _object({"op": _enum("evidence"), "artifact": _text(),
            "offset": _optional(_integer(0, 16 * 1024**2)), "limit": _optional(_integer(1, 16384))}),
        "discover": _object({"op": _enum("discover"), "provider": _enum("inspector"),
            "request": _object({"operations": _array(_ref("discovery"), I.MAX_OPERATIONS, 1)}),
            "timeout": _optional(_integer(1, 60))}),
        "declare_inspector": _object({"op": _enum("declare_inspector"), "actor": _ref("unsigned_actor"),
            "citations": _array(_ref("citation"), 16, 1)}),
        "propose": _object({"op": _enum("propose"), "rationale": _text(), "actor": _ref("actor"),
            "citations": _array(_ref("citation"), 16, 1), "observer_profile_sha256": sha}),
    }
    result = _object({"action": _enum(*(["step", "stop"] if admitted else ["stop"])),
        "request": {"anyOf": [{"type": "null"}, *[tools[op] for op in admitted]]} if admitted else {"type": "null"},
        "reason": _text(1200)})
    result["$defs"] = definitions
    return result


def _inspector_wire_spec(allowed_tools, nullable_omissions=False):
    _need(type(nullable_omissions) is bool, "nullable omission mode must be a boolean")
    def adapt(value):
        if type(value) is dict:
            result = {key: adapt(child) for key, child in value.items()}
            if nullable_omissions and result.get("type") == "object":
                result["required"] = [key for key, field in result["properties"].items() if not field.get("_omit_null")]
            return result
        return [adapt(child) for child in value] if type(value) is list else value
    return adapt(_inspector_spec(allowed_tools))


def inspector_schema(allowed_tools=None, *, nullable_omissions=False):
    """Closed provider schema for the explicitly selected inspector subset."""
    def public(value):
        if type(value) is dict:
            return {key: public(child) for key, child in value.items() if key != "_omit_null"}
        return [public(child) for child in value] if type(value) is list else value
    return public(_inspector_wire_spec(allowed_tools, nullable_omissions))


def inspector_instructions(allowed_tools=None):
    admitted = _inspector_admitted(allowed_tools)
    result = [*INSPECTOR_INSTRUCTIONS, "Caller-admitted wire tools: " + ", ".join(admitted) + "."]
    if "discover" in admitted:
        example = {"action": "step", "request": {"op": "discover", "provider": "inspector",
            "request": {"operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 8}]},
            "timeout": None}, "reason": "Inspect bounded DOM metadata."}
        result.append("JSON syntax example only; choose an actual evidence-supported selector: " + json.dumps(example))
    return result


def normalize_inspector_request(decision, view):
    """Pure wire admission; authoritative declaration/effect checks stay in G.

    No nested string is parsed or repaired, no evidence file is opened, and no
    inspector operation is collected or invoked by this function.
    """
    _need(type(view) is dict and type(view.get("tools")) is dict, "inspector protocol needs the effective model view")
    spec = _inspector_wire_spec(list(view["tools"]), view.get("decision_protocol", {}).get("nullable_omissions", False))
    try:
        raw = json.dumps(decision, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("inspector decision must be finite JSON data") from error
    _need(len(raw) <= MAX_WIRE_BYTES, "inspector decision exceeds byte bound")
    normalized = _normalize(decision, spec, spec["$defs"])
    _need(bool(normalized["reason"].strip()), "inspector reason must be nonempty")
    request = normalized["request"]
    if normalized["action"] == "stop":
        _need(request is None, "inspector STOP requires a null request")
        return None
    _need(type(request) is dict, "inspector step requires a native JSON request object")
    op = request["op"]
    if op == "discover":
        from . import rea_runtime_providers as providers
        request["request"] = providers.for_name("inspector").validate_discovery(request["request"])
    elif op in {"declare_inspector", "propose"}:
        from . import rea_inspector_bridge as I
        if op == "declare_inspector":
            from . import rea_inspector_contract as C
            request["actor"] = C._unsigned(request["actor"], view.get("params", []))
        else:
            _need(request["observer_profile_sha256"] == view.get("observer_profile_sha256"),
                  "proposal must bind the caller-approved observer hash")
            request["actor"] = I.validate_actor(request["actor"], allow_bindings=True)
            names = {parameter["name"] for parameter in view.get("params", [])}
            for argument in request["actor"]["arguments"]:
                if type(argument["value"]) is dict:
                    _need(argument["value"]["input"] in names, "inspector actor input binding is outside frozen parameters")
            claim = request["actor"]["signature_evidence"]
            _need(any(re.fullmatch(r"s[0-9]{4}/inspector-declaration[.]json", name) and type(pin) is dict
                      and pin.get("file") == claim["file"] and pin.get("sha256") == claim["sha256"]
                      for name, pin in view.get("artifacts", {}).items()),
                  "proposal requires the visible current-session declared_js result pin")
        citations = request["citations"]
        _need(len({(citation["artifact"], citation["pointer"]) for citation in citations}) == len(citations),
              "inspector method citations must be distinct")
        _need(all(citation["artifact"] in view.get("artifacts", {}) for citation in citations),
              "inspector citations require visible current-session raw method artifacts")
    elif op == "evidence":
        _need(request["artifact"] in view.get("artifacts", {}), "evidence is outside visible investigation artifacts")
    return request
