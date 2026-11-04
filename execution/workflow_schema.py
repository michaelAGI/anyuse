"""Versioned, tagged values for compiled workflow dataflow.

The wire template still owns *where* a value is structurally encoded.  This
module owns *where the value is allowed to come from*.  That distinction is
important: a ``derived`` value is response data, not another caller-supplied
string, and a ``const`` is part of the proof rather than a default an agent may
override.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


WORKFLOW_SCHEMA_VERSION = 2
PARAMETER_KINDS = frozenset({"const", "input", "derived"})
VALUE_TYPES = frozenset({"string", "integer", "number", "boolean"})
DEFAULT_INPUT_MAX_LENGTH = 2048
DEFAULT_DERIVED_MAX_LENGTH = 4096


def value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def json_schema_for_value(value: Any) -> dict:
    """Return a value-free JSON Schema for an observed extracted value.

    Proof records may retain this shape because it contains only field names and
    primitive types, never response values.  ``bool`` is checked before ``int``
    because Python's booleans are integers.
    """
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        item_schemas = [json_schema_for_value(item) for item in value if item is not None]
        return {"type": "array", "items": merge_json_schemas(item_schemas)}
    if isinstance(value, dict):
        properties = {str(name): json_schema_for_value(child)
                      for name, child in value.items() if child is not None}
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}
    return {"type": ["string", "number", "boolean", "array"]}


def merge_json_schemas(schemas: Iterable[Mapping[str, Any]]) -> dict:
    """Merge observed schemas without manufacturing a more specific type."""
    rows = [dict(schema) for schema in schemas if schema]
    if not rows:
        return {"type": ["string", "number", "boolean"]}
    first = rows[0]
    if all(row == first for row in rows[1:]):
        return first
    types: List[str] = []
    for row in rows:
        raw = row.get("type")
        for kind in (raw if isinstance(raw, list) else [raw]):
            if kind and kind not in types:
                types.append(str(kind))
    return {"type": types or ["string", "number", "boolean", "array"]}


@dataclass(frozen=True)
class ParameterSpec:
    """One tagged value and its enforceable dataflow contract."""

    name: str
    kind: str
    type: str = "string"
    value: Any = None
    min_length: int = 0
    max_length: int = DEFAULT_INPUT_MAX_LENGTH
    enum: Tuple[Any, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    source: Dict[str, str] = field(default_factory=dict)
    destinations: Tuple[str, ...] = ()
    destination_origins: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"name": self.name, "kind": self.kind, "type": self.type}
        if self.kind == "const":
            out["value"] = self.value
        if self.min_length:
            out["min_length"] = self.min_length
        if self.max_length:
            out["max_length"] = self.max_length
        if self.enum:
            out["enum"] = list(self.enum)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.source:
            out["source"] = dict(self.source)
        if self.destinations:
            out["destinations"] = list(self.destinations)
        if self.destination_origins:
            out["destination_origins"] = list(self.destination_origins)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParameterSpec":
        kind = str(data.get("kind") or "")
        default_max = DEFAULT_DERIVED_MAX_LENGTH if kind == "derived" else DEFAULT_INPUT_MAX_LENGTH
        return cls(
            name=str(data.get("name") or ""), kind=kind,
            type=str(data.get("type") or "string"), value=data.get("value"),
            min_length=int(data.get("min_length") or 0),
            max_length=int(data.get("max_length") or default_max),
            enum=tuple(data.get("enum") or ()),
            minimum=data.get("minimum"), maximum=data.get("maximum"),
            source={str(k): str(v) for k, v in (data.get("source") or {}).items()},
            destinations=tuple(str(v) for v in (data.get("destinations") or ())),
            destination_origins=tuple(str(v) for v in
                                      (data.get("destination_origins") or ())),
        )

    def problems(self) -> List[str]:
        out: List[str] = []
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", self.name or ""):
            out.append(f"invalid parameter name {self.name!r}")
        if self.kind not in PARAMETER_KINDS:
            out.append(f"parameter {self.name!r} has unsupported kind {self.kind!r}")
        if self.type not in VALUE_TYPES:
            out.append(f"parameter {self.name!r} has unsupported type {self.type!r}")
        if self.min_length < 0 or self.max_length < self.min_length:
            out.append(f"parameter {self.name!r} has invalid length bounds")
        if self.kind == "derived":
            if not self.source.get("step"):
                out.append(f"derived parameter {self.name!r} has no earlier source step")
            if not (self.source.get("output") or self.source.get("path")):
                out.append(f"derived parameter {self.name!r} has no named output/response field")
            if not self.destinations:
                out.append(f"derived parameter {self.name!r} has no destination scope")
        return out

    def validate(self, raw: Any) -> Tuple[bool, str]:
        if self.type == "string":
            if not isinstance(raw, str):
                return False, f"{self.name!r} must be a string"
            if len(raw) < self.min_length or len(raw) > self.max_length:
                return False, (f"{self.name!r} length {len(raw)} is outside "
                               f"[{self.min_length}, {self.max_length}]")
        elif self.type == "boolean":
            if not isinstance(raw, bool):
                return False, f"{self.name!r} must be a boolean"
        elif self.type == "integer":
            if isinstance(raw, bool) or not isinstance(raw, int):
                return False, f"{self.name!r} must be an integer"
        elif self.type == "number":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return False, f"{self.name!r} must be a number"
        if self.enum and raw not in self.enum:
            return False, f"{self.name!r} is outside its allowed domain"
        if self.minimum is not None and raw < self.minimum:
            return False, f"{self.name!r} is below {self.minimum}"
        if self.maximum is not None and raw > self.maximum:
            return False, f"{self.name!r} is above {self.maximum}"
        return True, ""


def input_spec(name: str, destinations: Iterable[str] = ()) -> ParameterSpec:
    return ParameterSpec(name=name, kind="input", type="string",
                         min_length=0, max_length=DEFAULT_INPUT_MAX_LENGTH,
                         destinations=tuple(destinations))


def specs_from_dicts(rows: Iterable[Mapping[str, Any]]) -> List[ParameterSpec]:
    return [ParameterSpec.from_dict(row) for row in rows]


def input_names(specs: Iterable[ParameterSpec]) -> List[str]:
    return [spec.name for spec in specs if spec.kind == "input"]


def spec_map(specs: Iterable[ParameterSpec]) -> Dict[str, ParameterSpec]:
    return {spec.name: spec for spec in specs}


def validate_specs(specs: Iterable[ParameterSpec]) -> List[str]:
    rows = list(specs)
    out: List[str] = []
    names: set[str] = set()
    for spec in rows:
        if spec.name in names:
            out.append(f"parameter {spec.name!r} is declared more than once")
        names.add(spec.name)
        out.extend(spec.problems())
    return out


class DataflowValues(dict):
    """Internal values carrying provenance for untrusted response bindings."""

    def __init__(self, values: Optional[Mapping[str, Any]] = None):
        super().__init__(values or {})
        self.derived_sources: Dict[str, Dict[str, str]] = {}
        self.destination_step = ""

    def for_step(self, step: str) -> "DataflowValues":
        out = DataflowValues(self)
        out.derived_sources = dict(self.derived_sources)
        out.destination_step = step
        return out

    def add_derived(self, name: str, value: Any, source: Mapping[str, str]) -> None:
        self[name] = value
        self.derived_sources[name] = {str(k): str(v) for k, v in source.items()}


def resolve_values(specs: Iterable[ParameterSpec], supplied: Mapping[str, Any], *,
                   destination_step: str = "", destination_url: str = ""
                   ) -> Tuple[Dict[str, Any], List[str]]:
    """Resolve tagged values, refusing caller-forged derived/const values."""
    rows = list(specs)
    known = {s.name for s in rows}
    errors = ([] if isinstance(supplied, DataflowValues) else
              [f"undeclared input {name!r}" for name in supplied if name not in known])
    out: Dict[str, Any] = {}
    flow = supplied if isinstance(supplied, DataflowValues) else None
    target_origin = ""
    if destination_url:
        parts = urllib.parse.urlsplit(destination_url)
        if parts.scheme and parts.hostname:
            port = parts.port
            default = 443 if parts.scheme == "https" else 80
            target_origin = f"{parts.scheme}://{parts.hostname}" + (
                f":{port}" if port not in (None, default) else "")
    for spec in rows:
        if spec.kind == "const":
            if spec.name in supplied and supplied[spec.name] != spec.value:
                errors.append(f"const {spec.name!r} is immutable in the proved template")
            value = spec.value
        elif spec.kind == "input":
            if spec.name not in supplied:
                errors.append(f"missing input {spec.name!r}")
                continue
            value = supplied[spec.name]
        else:
            if flow is None or spec.name not in flow.derived_sources:
                errors.append(f"derived value {spec.name!r} may only come from an earlier step")
                continue
            actual_source = flow.derived_sources[spec.name]
            if any(actual_source.get(k) != v for k, v in spec.source.items()):
                errors.append(f"derived value {spec.name!r} has the wrong source")
                continue
            step = destination_step or flow.destination_step
            if spec.destinations and step not in spec.destinations:
                errors.append(f"derived value {spec.name!r} is outside destination step {step!r}")
                continue
            if spec.destination_origins and target_origin not in spec.destination_origins:
                errors.append(f"derived value {spec.name!r} is outside destination origin scope")
                continue
            value = supplied[spec.name]
        ok, why = spec.validate(value)
        if not ok:
            errors.append(why)
        else:
            out[spec.name] = value
    return out, errors


def template_digest(template_dict: Mapping[str, Any]) -> str:
    """Pin a proof to the exact versioned template, including every const."""
    blob = json.dumps(template_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def json_schema_for_inputs(specs: Iterable[ParameterSpec]) -> dict:
    props: Dict[str, dict] = {}
    required: List[str] = []
    for spec in specs:
        if spec.kind != "input":
            continue
        row: Dict[str, Any] = {"type": spec.type}
        if spec.type == "string":
            row.update({"minLength": spec.min_length, "maxLength": spec.max_length})
        if spec.enum:
            row["enum"] = list(spec.enum)
        if spec.minimum is not None:
            row["minimum"] = spec.minimum
        if spec.maximum is not None:
            row["maximum"] = spec.maximum
        props[spec.name] = row
        required.append(spec.name)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}
