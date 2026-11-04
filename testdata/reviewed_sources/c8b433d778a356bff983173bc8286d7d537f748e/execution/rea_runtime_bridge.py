"""VM-only, declarative runtime investigation and typed invocation through Frida.

The installed adapter is fixed source compiled inside the guest. A mapping agent
supplies names, runtime signatures and typed values, never JavaScript or native
addresses. Each transaction attaches anew and expires all object aliases before
detaching. Runtime evidence and a returned method call are not task verification.
Imports do not load Frida/native frameworks, inspect an app or install anything.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import queue
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import threading
import traceback
from types import MappingProxyType
from typing import Any

if __package__:
    from . import rea_mapping as R
    from . import rea_native_path as NP
    from . import rea_native_lifecycle as NL
    from . import rea_qt_moc as Q
    from . import rea_memory_inspection as MI
    from . import rea_runtime_permit as RP
else:  # The bounded worker is launched by its fixed installed file path.
    import rea_mapping as R
    import rea_native_path as NP
    import rea_native_lifecycle as NL
    import rea_qt_moc as Q
    import rea_memory_inspection as MI
    import rea_runtime_permit as RP

LEGACY_RUNTIME = "anyuse.rea-runtime/1"
LEGACY_WORKER = "anyuse.rea-runtime-worker/1"
RUNTIME = "anyuse.rea-runtime/2"
WORKER = "anyuse.rea-runtime-worker/2"
FRIDA_LIFECYCLE = "anyuse.rea-frida-lifecycle/1"
CLEANUP_JOURNAL = "anyuse.rea-worker-cleanup-journal/1"
PHASED = "anyuse.rea-runtime-phased/1"
PHASE_CONTEXT = "anyuse.rea-runtime-phase-context/1"
PHASED2 = "anyuse.rea-phased-runtime/2"
PHASE_CONTEXT2 = "anyuse.rea-native-run-context/2"
PHASED3 = "anyuse.rea-phased-runtime/3"
PHASE_CONTEXT3 = "anyuse.rea-native-run-context/3"
_STRICT_PHASE_OBSERVER = "ax_focused_readonly/1"
HELPER_POLICY = "anyuse.rea-runtime-helper-policy/1"
COMPILER_ADAPTER = "anyuse.rea-compiler-adapter/1"
COMPILER_COMPLETION = "anyuse.rea-compiler-completion/1"
COMPILER_BYTES_COMPLETION = "anyuse.rea-compiler-bytes-completion/1"
MAX_OPERATIONS = 16
MAX_ACTOR_STEPS = 8
MAX_ROWS = 128
MAX_INSTANCES = 16
MAX_FIELDS = 32
MAX_TYPE_DEPTH = 4
MAX_BUFFER_BYTES = 16384
MAX_NATIVE_MODULE_BYTES = 512 * 1024**2
MAX_NATIVE_ARGUMENTS = 16
# This source includes the fixed JS resolver and H receipt verifier together.
# Deployed adapters must still match this exact JS source before attachment.
NATIVE_PATH_RUNTIME_READY = True
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 2 * 1024**2
MAX_AGENT_BYTES = 8 * 1024**2
MAX_GADGET_BYTES = 256 * 1024**2
MIN_TOKEN_BYTES = 32
MAX_TOKEN_BYTES = 4096
CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]{0,255}\Z")
# Swift/KVO runtime class spellings may start with dots. Other identifiers
# retain CLASS; the exact runtime string is never normalized.
OBJC_CLASS = re.compile(r"\.*[A-Za-z_][A-Za-z0-9_.$]*\Z")
SELECTOR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)*:?\Z")
TYPES = {"void", "bool", "int8", "uint8", "int16", "uint16", "int32", "uint32",
         "int64", "uint64", "long", "ulong", "float", "double", "utf8", "object", "nil"}
BINDING_TYPES = {"utf8", "bool", "int8", "uint8", "int16", "uint16", "int32", "uint32",
                 "int64", "uint64", "long", "ulong", "float", "double"}
OBJC_BINDING_TYPES = BINDING_TYPES - {"float", "double"}
NATIVE_TYPES = TYPES - {"object", "nil"} | {"pointer"}
OFFSET = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FLOAT_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,307})(?:\.[0-9]+)?(?:e[+-]?(?:0|[1-9][0-9]{0,3}))?\Z")
DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,19}|-[1-9][0-9]{0,19})\Z")
ENCODINGS = {"void": {"v"}, "bool": {"B", "c"}, "int8": {"c"}, "uint8": {"C"},
             "int16": {"s"}, "uint16": {"S"}, "int32": {"i"}, "uint32": {"I"},
             "int64": {"q"}, "uint64": {"Q"}, "long": {"l"}, "ulong": {"L"},
             "float": {"f"}, "double": {"d"}, "utf8": {"@"}, "object": {"@"}, "nil": {"@"}}
_RESERVED = {"prototype", "constructor", "__proto__", "toJSON", "toString", "valueOf", "hasOwnProperty"}


class Unavailable(ValueError):
    """Existing tooling/permissions do not allow attaching; do not work around it."""


def _file_claim(value):
    _keys(value, {"path", "sha256"})
    if (not _text(value["path"], 4096) or not Path(value["path"]).is_absolute() or
            not isinstance(value["sha256"], str) or SHA256.fullmatch(value["sha256"]) is None):
        raise ValueError("native module needs an absolute file path and exact SHA-256")


def _native_type(kind, *, result=False, depth=0):
    """Only declared natural POD layouts; no guessed C++ or Swift ownership ABI."""
    if depth > MAX_TYPE_DEPTH:
        raise ValueError("native aggregate type exceeds its depth budget")
    if isinstance(kind, str):
        if kind not in NATIVE_TYPES or kind == "void" and not result:
            raise ValueError("unsupported native ABI type")
        return
    if not isinstance(kind, dict):
        raise ValueError("native ABI types must be explicit")
    category = kind.get("kind")
    if category == "struct":
        _keys(kind, {"kind", "fields"})
        if not isinstance(kind["fields"], list) or not 1 <= len(kind["fields"]) <= 16:
            raise ValueError("native POD aggregate needs 1–16 fields")
        names = set()
        for field in kind["fields"]:
            _keys(field, {"name", "type"})
            if not isinstance(field["name"], str) or not R.NAME.fullmatch(field["name"]) or field["name"] in names:
                raise ValueError("native aggregate field names must be unique")
            names.add(field["name"])
            _native_type(field["type"], depth=depth + 1)
            if (isinstance(field["type"], dict) and field["type"].get("kind") == "buffer" or
                    field["type"] == "utf8"):
                raise ValueError("buffers/UTF-8 pointers need top-level arguments, not implicit aggregate ownership")
    elif category == "buffer":
        _keys(kind, {"kind", "bytes", "direction"})
        _integer(kind["bytes"], 1, MAX_BUFFER_BYTES, "native buffer byte limit must be 1–16384")
        if result or not isinstance(kind["direction"], str) or kind["direction"] not in {"in", "out", "inout"}:
            raise ValueError("native buffers are explicit arguments with in/out/inout direction")
    elif category == "swift_type":
        _keys(kind, {"kind", "module", "name", "category", "bytes", "fields"})
        if not _name(kind["module"]) or not _name(kind["name"]) or kind["category"] != "struct":
            raise ValueError("Swift aggregate needs exact module/name and trivial struct metadata")
        _integer(kind["bytes"], 8, 128, "Swift POD stride must be explicitly bounded to 8–128 bytes")
        if kind["bytes"] % 8:
            raise ValueError("current Swift bridge requires a POD stride divisible by eight")
        fields = kind["fields"]
        if not isinstance(fields, list) or not 1 <= len(fields) <= 16:
            raise ValueError("Swift register aggregates require an exact integer/pointer field declaration")
        names = set()
        for field in fields:
            _keys(field, {"name", "type"})
            if (not _name(field["name"]) or field["name"] in names or not isinstance(field["type"], str) or
                    field["type"] not in {"int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64", "long", "ulong", "pointer"}):
                raise ValueError("Swift register aggregate fields must be unique supported integer/pointer types; FP/SIMD is unsupported")
            names.add(field["name"])
    else:
        raise ValueError("packed/union/non-trivial/generic native layouts are unsupported")


def _native_signature(actor):
    _keys(actor["target"], {"module"}, {"symbol", "offset"})
    _file_claim(actor["target"]["module"])
    if ("symbol" in actor["target"]) == ("offset" in actor["target"]):
        raise ValueError("native target must declare one exact symbol or image-relative offset")
    if "symbol" in actor["target"]:
        if not _text(actor["target"]["symbol"], 1024):
            raise ValueError("native symbol must be literal bounded text")
    elif not isinstance(actor["target"]["offset"], str) or OFFSET.fullmatch(actor["target"]["offset"]) is None:
        raise ValueError("native offset must be canonical hexadecimal, never an absolute pointer")
    if not isinstance(actor["abi"], str) or actor["abi"] not in {"default", "unix64", "swift"}:
        raise ValueError("native ABI must be explicit; unsupported conventions cannot be inferred")
    args = actor["arguments"]
    if not isinstance(args, list) or len(args) > MAX_NATIVE_ARGUMENTS:
        raise ValueError("native argument count exceeds its budget")
    _native_type(actor["return_type"], result=True)
    if actor["abi"] != "swift" and isinstance(actor["return_type"], dict) and actor["return_type"]["kind"] == "swift_type":
        raise ValueError("Swift metadata types require actual Swift calling convention")
    _evidence_claim(actor["signature_evidence"], allow_declared=True)


def _evidence_claim(evidence, *, allow_declared=False):
    _keys(evidence, {"kind", "file", "sha256", "pointer"})
    kinds = {"compiler", "dwarf", "header", "reviewed_contract"} | ({"declared_native"} if allow_declared else set())
    if (not isinstance(evidence["kind"], str) or evidence["kind"] not in kinds or
            not _text(evidence["file"], 4096) or not Path(evidence["file"]).is_absolute() or
            not isinstance(evidence["sha256"], str) or SHA256.fullmatch(evidence["sha256"]) is None or
            not _text(evidence["pointer"], 2048, empty=True) or
            evidence["pointer"] and not evidence["pointer"].startswith("/")):
        raise ValueError("native prototype requires a pinned compiler/header/DWARF/reviewed JSON contract")


def _native_argument(argument, *, allow_bindings=False, aliases=None, depth=0, allow_owned_return=False,
                     enclosing_actor=None, argument_index=None, top_level=False):
    _keys(argument, {"type", "value"})
    kind, value = argument["type"], argument["value"]
    _native_type(kind)
    if isinstance(value, dict) and set(value) == {"input"} and not (isinstance(kind, dict) and kind["kind"] == "struct"):
        if (not allow_bindings or not isinstance(value["input"], str) or not R.NAME.fullmatch(value["input"]) or
                not (isinstance(kind, str) and kind in BINDING_TYPES or
                     isinstance(kind, dict) and kind["kind"] == "buffer" and kind["direction"] != "out")):
            raise ValueError("native binding requires a supported scalar or explicit input buffer")
        return
    if isinstance(kind, dict):
        if kind["kind"] == "struct":
            if not isinstance(value, dict) or set(value) != {f["name"] for f in kind["fields"]}:
                raise ValueError("native POD value must match every declared named field")
            for field in kind["fields"]:
                _native_argument({"type": field["type"], "value": value[field["name"]]},
                                 allow_bindings=allow_bindings, aliases=aliases, depth=depth)
        elif kind["kind"] == "buffer":
            if kind["direction"] == "out":
                if value is not None:
                    raise ValueError("out buffer must start as explicit zeroed allocation")
            elif (not isinstance(value, list) or len(value) != kind["bytes"] or
                  any(type(x) is not int or not 0 <= x <= 255 for x in value)):
                raise ValueError("native input buffer must contain exactly its declared bytes")
        else:
            if not isinstance(value, dict) or set(value) != {"getter"}:
                raise ValueError("Swift POD values must come from a freshly typed native getter")
            _native_actor(value["getter"], allow_bindings=allow_bindings, aliases=aliases, depth=depth + 1)
            if value["getter"]["return_type"] != kind:
                raise ValueError("Swift value getter must return the exact declared metadata type")
    elif kind == "pointer":
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("native pointers require an ephemeral alias, typed borrowed getter or Objective-C recipe")
        if "alias" in value:
            if not isinstance(value["alias"], str) or value["alias"] not in (aliases or set()):
                raise ValueError("native alias is unknown, expired or cannot persist in a recipe")
        elif "objc" in value:
            _receiver(value["objc"], aliases or set(), recipe=aliases is None)
        elif "getter" in value:
            _native_actor(value["getter"], allow_bindings=allow_bindings, aliases=aliases, depth=depth + 1)
            if value["getter"]["return_type"] != "pointer":
                raise ValueError("borrowed object getter must return an opaque pointer")
        elif "read" in value:
            _pointer_read(value["read"])
        elif "swift_read" in value:
            _swift_read(value["swift_read"])
        elif "callable" in value:
            _callable(value["callable"], allow_owned_return=allow_owned_return)
        elif "native_path" in value:
            if not top_level or enclosing_actor is None:
                raise ValueError("native paths require a top-level direct native pointer argument")
            _native_path(value["native_path"], depth=depth)
            if NP.is_immediate_root(value["native_path"]) and depth != 0:
                raise ValueError("main-queue borrowed root cannot occur inside a nested getter")
            NP.require_consumer(value["native_path"], enclosing_actor, argument_index)
        else:
            raise ValueError("raw addresses and guessed pointer ownership are unsupported")
    else:
        _value(argument, allow_bindings=allow_bindings)


def _native_actor(actor, *, allow_bindings=False, aliases=None, depth=0):
    if depth > MAX_TYPE_DEPTH:
        raise ValueError("nested native getter depth exceeds its budget")
    _keys(actor, {"kind", "target", "abi", "arguments", "return_type", "signature_evidence"}, {"context", "compiler_adapter"})
    if actor["kind"] != "native_function":
        raise ValueError("expected one typed native_function, never generated code")
    _native_signature(actor)
    adapter = actor.get("compiler_adapter")
    owned_return = isinstance(adapter, dict) and adapter.get("family") == "cxx_owned_string_bytes"
    for index, arg in enumerate(actor["arguments"]):
        _native_argument(arg, allow_bindings=allow_bindings, aliases=aliases, depth=depth,
                         allow_owned_return=owned_return and index == 0,
                         enclosing_actor=actor, argument_index=index, top_level=True)
    if "context" in actor:
        if actor["abi"] != "swift" or not isinstance(actor["context"], dict) or actor["context"].get("type") != "pointer":
            raise ValueError("only native Swift calls accept an explicit borrowed Swift context")
        _native_argument(actor["context"], allow_bindings=allow_bindings, aliases=aliases, depth=depth)
    if actor["abi"] == "swift":
        for kind in [actor["return_type"], *(arg["type"] for arg in actor["arguments"])]:
            if (isinstance(kind, str) and kind in {"float", "double", "bool", "utf8"} or
                    isinstance(kind, dict) and kind["kind"] != "swift_type"):
                raise ValueError("current Swift bridge does not support FP/BOOL lowering or guessed aggregates; use metadata POD/integer types")
    else:
        def swift_in(kind):
            return isinstance(kind, dict) and (kind["kind"] == "swift_type" or
                kind["kind"] == "struct" and any(swift_in(f["type"]) for f in kind["fields"]))
        if any(swift_in(kind) for kind in [actor["return_type"], *(arg["type"] for arg in actor["arguments"])]):
            raise ValueError("Swift metadata arguments require the actual Swift calling convention")
    if "compiler_adapter" in actor:
        _compiler_adapter(actor, allow_bindings=allow_bindings)
    borrowed = _immediate_paths(actor)
    if borrowed and (depth != 0 or len(borrowed) != 1 or "context" in actor or "compiler_adapter" in actor or
            not actor["arguments"] or actor["arguments"][0]["type"] != "pointer" or
            actor["arguments"][0]["value"].get("native_path") is not borrowed[0]):
        raise ValueError("borrowed root requires one top-level direct arg0 receiver and scalar-only terminal call")


def _borrowed_paths(value):
    return [recipe for kind, recipe in NP.walk_recipes(value)
            if kind == "native_path" and NP.is_borrowed_root(recipe)]


def _lifecycle_paths(value):
    return [recipe for kind, recipe in NP.walk_recipes(value)
            if kind == "native_path" and NP.is_lifecycle_root(recipe)]


def _immediate_paths(value):
    return [recipe for kind, recipe in NP.walk_recipes(value)
            if kind == "native_path" and NP.is_immediate_root(recipe)]


def _lifecycle_budget(value, milliseconds):
    for recipe in _lifecycle_paths(value):
        if recipe["root"]["lifecycle"]["observe_ms"] + NL.CLEANUP_RESERVE_MS > milliseconds:
            raise ValueError("lifecycle observation window and cleanup reserve exceed the remaining worker budget")


def _borrowed_terminal_scope(values, *, actors=False):
    """Use-site restriction, in addition to the independently admitted recipe.

    Prior operations finish before acquisition. There is exactly one borrowed
    occurrence, in the final top-level call/read; no lease can outlive that use.
    """
    occurrences = [(index, recipe) for index, value in enumerate(values) for recipe in _immediate_paths(value)]
    if not occurrences:
        return
    if len(occurrences) != 1 or occurrences[0][0] != len(values) - 1:
        raise ValueError("main-queue borrowed root must be the single final top-level use")
    index, recipe = occurrences[0]
    if NP.is_lifecycle_root(recipe) and len(values) != 1:
        raise ValueError("lifecycle observer requires one operation and one immediate root use")
    value = values[index]
    if value.get("kind") == Q.ACTOR or value.get("op") == Q.OPERATION:
        Q.require_consumer(recipe, value, 0)
    elif actors or value["op"] == "native_invoke":
        if ((actors and value["kind"] != "native_function") or "alias" in value or
                "context" in value or "compiler_adapter" in value or not value["arguments"] or
                value["arguments"][0]["type"] != "pointer" or
                value["arguments"][0]["value"].get("native_path") is not recipe):
            raise ValueError("borrowed root invocation needs the actual terminal arg0 receiver without aliases")
    elif value["op"] != "native_read" or value["receiver"].get("native_path") is not recipe or "consumer" in recipe:
        raise ValueError("borrowed root permits only a terminal direct call or immediate scalar native read")
    if not actors and any(value["op"] == "load_helper" for value in values):
        raise ValueError("main-queue borrowed root transactions cannot load helpers")


def _contract(actor):
    return {"abi": actor["abi"], "argument_types": [a["type"] for a in actor["arguments"]],
            "return_type": actor["return_type"], "context": "context" in actor,
            "pointer_policy": "borrowed_no_escape"}


def _native_path(recipe, *, depth=0):
    return NP.validate_recipe(recipe,
        validate_existing_root=lambda root: _native_argument({"type": "pointer", "value": root}, depth=depth),
        validate_existing_callable=_callable)


def _walk_actor(actor):
    return NP.walk_recipes(actor_calls(actor, allow_bindings=True))


def native_path_recipes(actor):
    """Frozen path occurrences in actual getter/argument execution order."""
    return [copy.deepcopy(value) for kind, value in _walk_actor(actor) if kind == "native_path"]


def _receiver_path_calls(receiver):
    return len(receiver.get("path", []))


def _minimum_recipe_calls(value):
    total = 0
    for kind, recipe in NP.walk_recipes(value):
        if kind in {"native_function", "qt_moc"}:
            total += 1
        elif kind == "objc":
            total += _receiver_path_calls(recipe)
        elif kind in {"read", "swift_read"}:
            total += _receiver_path_calls(recipe["receiver"])
    return total


def minimum_native_calls(actor):
    """Pure lower bound for targetCall dispatches in a validated actor graph.

    Includes fixed ObjC receiver-path getters, nested native getters and roots.
    Static image-data roots add no target call.
    Runtime metadata resolution can add calls; ordinary retain/release and native
    calls internal to a target are outside the adapter's targetCall counter.
    """
    total = 0
    for call in actor_calls(actor, allow_bindings=True):
        if call["kind"] == "objc_method":
            total += 1 + _receiver_path_calls(call["receiver"])
        else:
            total += _minimum_recipe_calls(call)
    return total


def minimum_request_native_calls(request, *, completed_operations=None):
    """Pure independent lower bound for a successful request-operation prefix.

    Graph traversal may omit/error individual nodes and fields; only its fixed
    root work is guaranteed here; static image-data roots add no call. Swift
    reflection may perform additional type resolver calls. Neither variable
    work is inferred from a claimed counter.
    """
    operations = validate_request(request)["operations"]
    if completed_operations is not None:
        _integer(completed_operations, 0, len(operations), "completed runtime prefix exceeds the request")
        operations = operations[:completed_operations]
    total = 0
    for operation in operations:
        op = operation["op"]
        if op == "invoke":
            total += 1 + _receiver_path_calls(operation["receiver"])
        elif op in {"native_invoke", Q.OPERATION}:
            total += _minimum_recipe_calls(operation)
        elif op == "read":
            total += _receiver_path_calls(operation["receiver"]) + sum(field["kind"] == "getter" for field in operation["fields"])
        elif op in {"native_read", "graph"}:
            receiver = operation["receiver"] if op == "native_read" else operation["root"]
            total += (_minimum_recipe_calls(operation) if {"native", "native_path", "alias"} & set(receiver)
                      else _receiver_path_calls(receiver))
        elif op == "swift_read":
            total += _receiver_path_calls(operation["read"]["receiver"])
        elif op == "swift_fields":
            total += _receiver_path_calls(operation["receiver"])
    return total


def native_file_claims(actor):
    """Pure complete file/SHA closure used by resources and proof consumers."""
    claims = {}
    def add(path, sha):
        if path in claims and claims[path] != sha:
            raise ValueError("native recipes contain conflicting file identity claims")
        claims[path] = sha
    for kind, value in _walk_actor(actor):
        if kind == "native_function":
            add(value["target"]["module"]["path"], value["target"]["module"]["sha256"])
            evidence = value["signature_evidence"]
            add(evidence["file"], evidence["sha256"])
            if "compiler_adapter" in value:
                evidence = value["compiler_adapter"]["evidence"]
                add(evidence["file"], evidence["sha256"])
        elif kind in {"read", "swift_read", "callable"}:
            add(value["module"]["path"], value["module"]["sha256"])
            evidence = value["signature_evidence"] if kind == "callable" else value["evidence"]
            add(evidence["file"], evidence["sha256"])
        elif kind == "native_path":
            for path, sha in NP.file_claims(value).items():
                add(path, sha)
    return claims


def _callable_type(kind, *, result=False, allow_owned_return=False):
    if isinstance(kind, dict) and kind.get("kind") == "cxx_owned":
        _keys(kind, {"kind", "name", "library", "abi_version"})
        if (not isinstance(kind["name"], str) or kind["name"] not in {"std::string", "std::vector<double>"} or kind["library"] != "libc++" or
                type(kind["abi_version"]) is not int or kind["abi_version"] != 1 or
                result and (not allow_owned_return or kind["name"] != "std::string")):
            raise ValueError("owned returns require the fixed string-return compiler family; only libc++ v1 string/vector arguments are supported")
        return
    _native_type(kind, result=result)


def _callable(entry, *, allow_owned_return=False):
    """Declaration-only entry resolution; never lowers an unsupported native ABI."""
    _keys(entry, {"module", "abi", "argument_types", "return_type", "context", "signature_evidence", "pointer_policy"}, {"symbol", "offset"})
    _file_claim(entry["module"])
    if ("symbol" in entry) == ("offset" in entry):
        raise ValueError("callable entry requires exactly one literal symbol or image-relative offset")
    if "symbol" in entry:
        if not _text(entry["symbol"], 1024):
            raise ValueError("callable symbol must be exact bounded text")
    elif not isinstance(entry["offset"], str) or OFFSET.fullmatch(entry["offset"]) is None:
        raise ValueError("callable entry offset must be canonical image-relative hexadecimal")
    if (not isinstance(entry["abi"], str) or entry["abi"] not in {"default", "unix64", "swift"} or
            not isinstance(entry["context"], str) or entry["context"] not in {"none", "swiftself", "cxx_this"} or
            entry["pointer_policy"] != "borrowed_no_escape" or
            entry["context"] == "swiftself" and entry["abi"] != "swift" or
            entry["context"] == "cxx_this" and entry["abi"] == "swift"):
        raise ValueError("callable declaration needs an explicit ABI and distinct Swift/self or C++/this context")
    if not isinstance(entry["argument_types"], list) or len(entry["argument_types"]) > MAX_NATIVE_ARGUMENTS:
        raise ValueError("callable argument declaration exceeds its budget")
    for kind in entry["argument_types"]:
        _callable_type(kind)
        if entry["abi"] == "swift" and isinstance(kind, dict) and kind.get("kind") == "cxx_owned":
            raise ValueError("C++ ownership cannot be guessed as a Swift argument convention")
    _callable_type(entry["return_type"], result=True, allow_owned_return=allow_owned_return)
    _evidence_claim(entry["signature_evidence"])


def _callable_contract(entry):
    return {"entry": {key: copy.deepcopy(entry[key]) for key in ("module", "symbol", "offset") if key in entry},
            "abi": entry["abi"], "argument_types": entry["argument_types"], "return_type": entry["return_type"],
            "context": entry["context"], "pointer_policy": entry["pointer_policy"]}


def _compiler_adapter(actor, *, allow_bindings=False):
    adapter = actor["compiler_adapter"]
    _keys(adapter, {"family", "evidence", "completion_argument"})
    _evidence_claim(adapter["evidence"])
    if not isinstance(adapter["family"], str) or adapter["family"] not in {"swift_double_bool_context", "cxx_owned_string_vector_double", "cxx_owned_string_bytes"}:
        raise ValueError("unsupported compiler adapter family; generic/async/throws ownership is not inferred")
    args = actor["arguments"]
    cpp = adapter["family"] == "cxx_owned_string_vector_double"
    owned_return = adapter["family"] == "cxx_owned_string_bytes"
    expected = 6 if cpp else 5
    if (actor["abi"] not in {"default", "unix64"} or actor["return_type"] != "uint32" or "context" in actor or
            len(args) != expected or type(adapter["completion_argument"]) is not int or adapter["completion_argument"] != expected - 1 or
            args[0]["type"] != "pointer" or not isinstance(args[0]["value"], dict) or set(args[0]["value"]) != {"callable"} or
            args[1]["type"] != "pointer" or "callable" in args[1]["value"] or
            args[-1] != {"type": {"kind": "buffer", "bytes": 24, "direction": "out"}, "value": None}):
        raise ValueError("compiler adapter requires its fixed C transport and zeroed completion buffer")
    original = args[0]["value"]["callable"]
    if original["module"]["path"] == actor["target"]["module"]["path"]:
        raise ValueError("compiler adapter and original native code require separate pinned module identities")
    if not owned_return and original["return_type"] != "double":
        raise ValueError("current compiler families return exactly Double")
    if owned_return:
        string = {"kind": "cxx_owned", "name": "std::string", "library": "libc++", "abi_version": 1}
        if (original["abi"] != "default" or original["context"] != "cxx_this" or
                original["argument_types"] != [string] or original["return_type"] != string or
                args[2]["type"] != "utf8" or not isinstance(args[3]["type"], dict) or
                args[3]["type"].get("kind") != "buffer" or args[3]["type"].get("direction") != "out" or
                args[3]["value"] is not None):
            raise ValueError("owned string return requires its exact borrowed-this/string declaration and bounded zeroed payload")
    elif cpp:
        owned = [{"kind": "cxx_owned", "name": name, "library": "libc++", "abi_version": 1}
                 for name in ("std::string", "std::vector<double>")]
        if (original["abi"] not in {"default", "unix64"} or original["context"] != "cxx_this" or original["argument_types"] != owned or
                args[2]["type"] != "utf8" or args[3]["type"] != "uint32" or
                not isinstance(args[4]["type"], dict) or args[4]["type"].get("kind") != "buffer" or
                args[4]["type"].get("direction") != "in" or args[4]["type"]["bytes"] % 8):
            raise ValueError("C++ compiler family needs exact owned declarations and a padded Double transport buffer")
        count = args[3]["value"]
        if not (allow_bindings and isinstance(count, dict) and set(count) == {"input"}):
            _integer(count, 0, args[4]["type"]["bytes"] // 8, "C++ vector length exceeds its frozen transport capacity")
    elif (original["abi"] != "swift" or original["context"] != "swiftself" or original["argument_types"] != ["double", "bool"] or
          args[2]["type"] != "double" or args[3]["type"] != "bool"):
        raise ValueError("Swift compiler family requires its corroborated Double/Bool/swiftself declaration")


def _compiler_adapter_contract(actor):
    adapter = actor["compiler_adapter"]
    completion = {"format": COMPILER_COMPLETION, "argument": adapter["completion_argument"], "bytes": 24,
                  "status_return": "uint32", "dispatch_flag": "dispatch_attempted"}
    if adapter["family"] == "cxx_owned_string_bytes":
        completion.update(format=COMPILER_BYTES_COMPLETION, result_type="bytes", payload_argument=3,
                          payload_bytes=actor["arguments"][3]["type"]["bytes"], length_offset=16)
    return {"format": COMPILER_ADAPTER, "family": adapter["family"], "helper": actor["target"], "outer": _contract(actor),
            "original": _callable_contract(actor["arguments"][0]["value"]["callable"]),
            "completion": completion}


def _callables_argument(argument):
    kind, value = argument["type"], argument["value"]
    if isinstance(kind, dict) and kind["kind"] == "struct":
        for field in kind["fields"]:
            yield from _callables_argument({"type": field["type"], "value": value[field["name"]]})
    elif isinstance(value, dict) and set(value) == {"getter"}:
        yield from _callables_call(value["getter"])
    elif isinstance(value, dict) and set(value) == {"callable"}:
        yield value["callable"]


def _callables_call(call):
    if "context" in call:
        yield from _callables_argument(call["context"])
    for argument in call["arguments"]:
        yield from _callables_argument(argument)


def callable_recipes(actor):
    """Original native-entry declarations in actual context/getter/argument order."""
    return [copy.deepcopy(value) for kind, value in _walk_actor(actor) if kind == "callable"]


def callable_contract(entry, *, allow_owned_return=False):
    _callable(entry, allow_owned_return=allow_owned_return)
    return copy.deepcopy(_callable_contract(entry))


def compiler_adapter_contract(actor):
    _native_actor(actor, allow_bindings=True)
    if "compiler_adapter" not in actor:
        raise ValueError("actor has no compiler completion contract")
    return copy.deepcopy(_compiler_adapter_contract(actor))


def _pointer_encoding(encoding):
    """Recognize one observed pointer encoding; its pointee is never dereferenced."""
    if not _text(encoding, 1024):
        raise ValueError("ivar pointer encoding must be exact bounded runtime evidence")
    # Accept opaque/primitive/object pointers and balanced aggregate pointees.
    # We read one pointer slot, never infer or construct its pointee ABI.
    position = 0
    def parse(depth=0):
        nonlocal position
        if depth > 8:
            raise ValueError("ivar pointee encoding exceeds its depth budget")
        while position < len(encoding) and encoding[position] in "rnNoORV":
            position += 1
        if position >= len(encoding):
            raise ValueError("ivar pointer encoding is incomplete")
        token = encoding[position]
        position += 1
        if token == "^":
            parse(depth + 1)
        elif token in "vBcCsSiIlLqQfd@#?:*":
            if token == "@" and position < len(encoding) and encoding[position] == '"':
                end = encoding.find('"', position + 1)
                if end < 0:
                    raise ValueError("ivar object pointee encoding is incomplete")
                position = end + 1
        elif token in "{(":
            closing = "}" if token == "{" else ")"
            start = position
            while position < len(encoding) and encoding[position] not in "=" + closing:
                position += 1
            if position == start or position >= len(encoding):
                raise ValueError("ivar aggregate pointee encoding is incomplete")
            if encoding[position] == "=":
                position += 1
                while position < len(encoding) and encoding[position] != closing:
                    if encoding[position] == '"':
                        end = encoding.find('"', position + 1)
                        if end < 0:
                            raise ValueError("ivar named aggregate field is incomplete")
                        position = end + 1
                    else:
                        parse(depth + 1)
            if position >= len(encoding) or encoding[position] != closing:
                raise ValueError("ivar aggregate pointee encoding is incomplete")
            position += 1
        else:
            raise ValueError("unsupported observed pointer pointee encoding")
    qualified = encoding.lstrip("rnNoORV")
    if not qualified.startswith("^"):
        raise ValueError("borrowed ivar read requires an actual native pointer encoding")
    parse()
    if position != len(encoding):
        raise ValueError("ivar pointer encoding must describe one exact pointer slot")


def _pointer_read(read):
    _keys(read, {"receiver", "receiver_class", "field", "module", "evidence"})
    _receiver(read["receiver"], set(), recipe=True)
    if not _class_name(read["receiver_class"]):
        raise ValueError("ivar pointer recipe requires the exact reached receiver class")
    _keys(read["field"], {"class", "name", "encoding"}, {"offset"})
    if not _class_name(read["field"]["class"]) or not _name(read["field"]["name"]):
        raise ValueError("ivar pointer recipe requires exact owner class and field name")
    _pointer_encoding(read["field"]["encoding"])
    if "offset" in read["field"]:
        _integer(read["field"]["offset"], 0, 1024**2 - 8, "frozen ivar offset exceeds bounded instance layout")
    _file_claim(read["module"])
    _evidence_claim(read["evidence"])


def _pointer_read_contract(read):
    return {"receiver_class": read["receiver_class"], "field": read["field"], "pointer_policy": "borrowed_no_escape"}


def _swift_owner(read):
    _receiver(read["receiver"], set(), recipe=True)
    if not _class_name(read["receiver_class"]):
        raise ValueError("Swift field recipe requires the exact reached Objective-C receiver class")
    _keys(read["swift_class"], {"module", "name"})
    if not _name(read["swift_class"]["module"]) or not _name(read["swift_class"]["name"]):
        raise ValueError("Swift field recipe requires an exact nongeneric module/class")
    _file_claim(read["module"])


def _swift_read(read):
    """A separately typed Swift field route; empty Objective-C encodings stay invalid."""
    _keys(read, {"receiver", "receiver_class", "swift_class", "field", "module", "evidence"}, {"preconditions"})
    _swift_owner(read)
    _keys(read["field"], {"name", "type_name"}, {"offset"})
    if not _name(read["field"]["name"]) or read["field"]["type_name"] != "Swift.Optional<Swift.OpaquePointer>":
        raise ValueError("Swift pointer field route supports only exactly reflected Optional<OpaquePointer>")
    if "offset" in read["field"]:
        _integer(read["field"]["offset"], 0, 1024**2 - 8, "frozen Swift field offset exceeds bounded instance layout")
    preconditions = read.get("preconditions", [])
    if not isinstance(preconditions, list) or len(preconditions) > 8:
        raise ValueError("Swift lifecycle preconditions exceed their field budget")
    names = {read["field"]["name"]}
    for condition in preconditions:
        _keys(condition, {"name", "type_name", "equals"})
        if (not _name(condition["name"]) or condition["name"] in names or
                condition["type_name"] != "Swift.Bool" or type(condition["equals"]) is not bool):
            raise ValueError("Swift preconditions require unique exactly typed Bool fields")
        names.add(condition["name"])
    _evidence_claim(read["evidence"])


def _swift_read_contract(read):
    return {"receiver_class": read["receiver_class"], "swift_class": read["swift_class"],
            "field": read["field"], "preconditions": read.get("preconditions", []),
            "pointer_policy": "borrowed_no_escape", "queue": "main"}


def _pointer_reads_argument(argument):
    kind, value = argument["type"], argument["value"]
    if isinstance(kind, dict) and kind["kind"] == "struct":
        for field in kind["fields"]:
            yield from _pointer_reads_argument({"type": field["type"], "value": value[field["name"]]})
    elif isinstance(value, dict) and set(value) == {"getter"}:
        yield from _pointer_reads_call(value["getter"])
    elif isinstance(value, dict) and set(value) == {"read"}:
        yield value["read"]


def _pointer_reads_call(call):
    if "context" in call:
        yield from _pointer_reads_argument(call["context"])
    for argument in call["arguments"]:
        yield from _pointer_reads_argument(argument)


def pointer_read_recipes(actor):
    """Frozen ivar reads in actual context/getter/argument execution order."""
    return [copy.deepcopy(value) for kind, value in _walk_actor(actor) if kind == "read"]


def _swift_reads_argument(argument):
    kind, value = argument["type"], argument["value"]
    if isinstance(kind, dict) and kind["kind"] == "struct":
        for field in kind["fields"]:
            yield from _swift_reads_argument({"type": field["type"], "value": value[field["name"]]})
    elif isinstance(value, dict) and set(value) == {"getter"}:
        yield from _swift_reads_call(value["getter"])
    elif isinstance(value, dict) and set(value) == {"swift_read"}:
        yield value["swift_read"]


def _swift_reads_call(call):
    if "context" in call:
        yield from _swift_reads_argument(call["context"])
    for argument in call["arguments"]:
        yield from _swift_reads_argument(argument)


def swift_read_recipes(actor):
    """Separately typed Swift reads in actual context/getter/argument order."""
    return [copy.deepcopy(value) for kind, value in _walk_actor(actor) if kind == "swift_read"]


def _json_pointer(value, pointer):
    for token in pointer.split("/")[1:] if pointer else []:
        if re.search(r"~(?![01])", token):
            raise ValueError("native signature JSON pointer escape is invalid")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("native signature JSON pointer index is invalid")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("native signature JSON pointer does not select a contract")
    return value


def _native_getters(argument):
    kind, value = argument["type"], argument["value"]
    if isinstance(kind, dict) and kind["kind"] == "struct":
        for field in kind["fields"]:
            yield from _native_getters({"type": field["type"], "value": value[field["name"]]})
    elif isinstance(value, dict) and set(value) == {"getter"}:
        yield value["getter"]


def _native_calls_recursive(actor):
    for kind, value in _walk_actor(actor):
        if kind == "native_function":
            yield value


def _merge_pins(resources, values):
    for path, pin in values.items():
        previous = resources.get(path)
        if previous is not None and any(not NP.same_data(previous[key], value) for key, value in pin.items()
                                        if key in previous and key != "native_path_contracts"):
            raise ValueError("runtime resources contain conflicting identities for one file")
        merged = {**(previous or {}), **pin}
        if previous is not None and "native_path_contracts" in previous and "native_path_contracts" in pin:
            records = {}
            for row in [*previous["native_path_contracts"], *pin["native_path_contracts"]]:
                if row["pointer"] in records and not NP.same_data(records[row["pointer"]], row):
                    raise ValueError("one native path JSON pointer has conflicting reviewed contracts")
                records[row["pointer"]] = copy.deepcopy(row)
            merged["native_path_contracts"] = [records[key] for key in sorted(records)]
        resources[path] = merged


def _declaration_resources(module, evidence, contract):
    R.assert_guest()
    resources = {}
    for claim, is_module in ((module, True), ({"path": evidence["file"], "sha256": evidence["sha256"]}, False)):
        path = Path(claim["path"])
        pin = _installed_file(str(path), MAX_NATIVE_MODULE_BYTES if is_module else MAX_RESULT_BYTES,
                              "native declaration/module is not a bounded canonical regular file")
        if pin["file"] != str(path) or pin["sha256"] != claim["sha256"]:
            raise ValueError("native declaration/module bytes differ from their pinned evidence")
        if is_module:
            pin.update(macho_images=_macho_images(path), macho_sections=_macho_sections(path), macho_segments=_macho_segments(path))
        resources[pin["file"]] = pin
    try:
        declaration_doc = R.load_json(evidence["file"])
        if isinstance(declaration_doc, dict) and declaration_doc.get("format") == "anyuse.rea-native-model-declaration/1":
            raise ValueError("model-declared native evidence cannot be relabelled as a callable or read contract")
        selected = _json_pointer(declaration_doc, evidence["pointer"])
    except (KeyError, IndexError, TypeError):
        raise ValueError("native declaration evidence does not select a valid contract") from None
    if selected != contract:
        raise ValueError("native declaration differs from exact entry/context/ABI evidence")
    return resources


def _qt_moc_json(claim):
    """Bound and pin actual static evidence before decoding duplicate-free JSON."""
    R.assert_guest()
    pin = _installed_file(claim["file"], MAX_RESULT_BYTES, "Qt artifact is not a bounded installed regular file")
    with Path(claim["file"]).open("rb") as stream:
        raw = stream.read(MAX_RESULT_BYTES + 1)
    if (len(raw) != pin["bytes"] or len(raw) > MAX_RESULT_BYTES or pin["file"] != claim["file"] or
            pin["sha256"] != claim["sha256"] or hashlib.sha256(raw).hexdigest() != claim["sha256"]):
        raise ValueError("Qt static artifact differs from its frozen file pin")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key in Qt static evidence")
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError("nonfinite value in Qt static evidence")
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite numeric literal in Qt static evidence")
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=nonfinite, parse_float=finite_float), pin


def _qt_moc_original_range(raw, pin, item):
    """Read one whole original thin-arm64 section/segment mapping, no target IO."""
    if (len(raw) < 32 or struct.unpack_from("<IIII", raw)[:3] != (0xfeedfacf, 0x0100000c, 0) or
            struct.unpack_from("<I", raw, 12)[0] not in {2, 6}):
        raise ValueError("Qt dispatch ranges require an original thin plain-arm64 image")
    offset, count = int(item["offset"], 16), item["bytes"]
    key = "macho_sections" if item["kind"] == "code" else "native_path_data_sections"
    images = [row for row in pin.get(key, []) if row["image_identity"]["cpu_type"] == 0x0100000c and
              row["image_identity"]["cpu_subtype"] == 0]
    segments = [row for row in pin.get("macho_segments", []) if row["image_identity"]["cpu_type"] == 0x0100000c and
                row["image_identity"]["cpu_subtype"] == 0]
    if len(images) != 1 or len(segments) != 1:
        raise ValueError("Qt original range lost its exact image section/segment authority")
    selected = [row for row in images[0]["sections"] if offset < int(row["offset"], 16) + row["bytes"] and
                int(row["offset"], 16) < offset + count]
    mapped = [row for row in segments[0]["segments"] if int(row["offset"], 16) <= offset and
              offset + count <= int(row["offset"], 16) + row["file_bytes"]]
    if (len(selected) != 1 or int(selected[0]["offset"], 16) > offset or
            offset + count > int(selected[0]["offset"], 16) + selected[0]["bytes"] or len(mapped) != 1 or
            not mapped[0]["initial_protection"] & (4 if item["kind"] == "code" else 1)):
        raise ValueError("Qt original range must fit one exact file-backed section and permitted segment")
    at = mapped[0]["file_offset"] + offset - int(mapped[0]["offset"], 16)
    chunk = raw[at:at + count]
    if len(chunk) != count or chunk.hex() != item["original_hex"] or hashlib.sha256(chunk).hexdigest() != item["sha256"]:
        raise ValueError("Qt catalog/code/table bytes differ from the independently pinned original image")


def _qt_moc_resources(recipe, resources):
    """Add reviewed actual Qt static evidence only after P's exact admissions."""
    R.assert_guest()
    consumer = recipe["consumer"]
    Q.validate_consumer(consumer, root_type=recipe["root_type"])
    selection = consumer["selection"]
    artifacts = {}
    for key in ("catalog", "code", "table", "qt_sources"):
        artifacts[key], pin = _qt_moc_json(selection[key])
        _merge_pins(resources, {pin["file"]: pin})
    for path, digest in Q.source_claims(selection, artifacts["qt_sources"]).items():
        pin = _installed_file(path, 1024 * 1024, "Qt public source is not a bounded installed regular file")
        if pin["file"] != path or pin["sha256"] != digest:
            raise ValueError("actual public Qt source differs from its reviewed exact-commit bytes")
        _merge_pins(resources, {pin["file"]: pin})
    plan = Q.static_plan(consumer, artifacts["catalog"], artifacts["code"], artifacts["table"], artifacts["qt_sources"])
    image = selection["module"]
    pin = copy.deepcopy(resources[image["path"]])
    pin["macho_sections"] = _lifecycle_code_sections(Path(image["path"]))
    _merge_pins(resources, {pin["file"]: pin})
    with Path(image["path"]).open("rb") as stream:
        raw = stream.read(MAX_NATIVE_MODULE_BYTES + 1)
    if len(raw) != pin["bytes"] or hashlib.sha256(raw).hexdigest() != image["sha256"]:
        raise ValueError("original Qt declaring image changed while static resources were prepared")
    for item in plan["original_ranges"]:
        if not Q.same_data(item["module"], image):
            raise ValueError("first Qt dispatch ranges must all belong to the exact declaring image")
        _qt_moc_original_range(raw, pin, item)
    if any(resources.get(path, {}).get("sha256") != digest for path, digest in Q.file_claims(consumer).items()):
        raise ValueError("Qt dispatch resource preparation lost a transitive pinned file")
    catalog_pin = copy.deepcopy(resources[selection["catalog"]["file"]])
    catalog_pin["qt_moc_dispatches"] = [plan["runtime_pin"]]
    _merge_pins(resources, {catalog_pin["file"]: catalog_pin})


def _native_path_resources(recipe, *, include_root=True):
    """Admit the exact reviewed source/build closure before any borrowed root.

    A source admission is an independently prepared reviewed attestation. File
    hashes and this check bind it to the selected image/ABI/typed edge; they do
    not recover an unknown native layout or independently prove a task result.
    """
    R.assert_guest()
    _native_path(recipe)
    _native_path_original_sizes([recipe])
    resources = {}

    def pinned(claim, limit):
        path = Path(claim["path"])
        previous = resources.get(str(path))
        if previous is not None:
            if previous["sha256"] != claim["sha256"] or previous["bytes"] > limit:
                raise ValueError("native path closure has conflicting file identities or byte limits")
            return copy.deepcopy(previous)
        pin = _installed_file(str(path), limit, "native path evidence is not a bounded canonical regular file")
        if pin["file"] != str(path) or pin["sha256"] != claim["sha256"]:
            raise ValueError("native path original/module/contract bytes differ from their frozen pins")
        _merge_pins(resources, {pin["file"]: pin})
        return pin

    root_claim = recipe["root_evidence"]["claim"]
    pinned({"path": root_claim["file"], "sha256": root_claim["sha256"]}, MAX_RESULT_BYTES)
    root_document = R.load_json(root_claim["file"])
    from . import rea_native_path_builder as path_builder
    if type(root_document) is dict and root_document.get("format") == path_builder.FORMAT:
        _merge_pins(resources, path_builder.resources(recipe))

    for claim in NP.image_claims(recipe):
        pin = pinned(claim, MAX_NATIVE_MODULE_BYTES)
        path = Path(pin["file"])
        pin.update(macho_images=_macho_images(path), macho_sections=_macho_sections(path),
                   macho_segments=_macho_segments(path), native_path_data_sections=_native_path_data_sections(path))
        if NP.is_lifecycle_root(recipe) and any(image["path"] == claim["path"]
                for image in NL.image_claims(recipe["root"]["lifecycle"])):
            pin["macho_sections"] = _lifecycle_code_sections(path)
        if sum(image["cpu_type"] == 0x0100000C and image["cpu_subtype"] == 0
               for image in pin["macho_images"]) != 1:
            raise ValueError("native pointer paths require one exact plain-arm64 image slice; arm64e/PAC is unsupported")
        _merge_pins(resources, {pin["file"]: pin})

    if NP.is_lifecycle_root(recipe):
        for entry in NL.entries(recipe["root"]["lifecycle"]):
            offset = int(entry["offset"], 16)
            sections = [section for row in resources[entry["module"]["path"]]["macho_sections"]
                        if row["image_identity"]["cpu_type"] == 0x0100000C and row["image_identity"]["cpu_subtype"] == 0
                        for section in row["sections"]]
            overlaps = [section for section in sections if offset < int(section["offset"], 16) + section["bytes"] and
                        int(section["offset"], 16) < offset + 4]
            if (len(overlaps) != 1 or int(overlaps[0]["offset"], 16) > offset or
                    offset + 4 > int(overlaps[0]["offset"], 16) + overlaps[0]["bytes"]):
                raise ValueError("lifecycle entry/return instruction must fit one exact original executable section")

    if "data" in recipe["root"]:
        data = recipe["root"]["data"]
        offset = int(data["offset"], 16)
        extent = NP.path_types(recipe)[0]["bytes"] if data["mode"] == "object" else NP.ABI["pointer_bytes"]
        rows = resources[data["module"]["path"]]["native_path_data_sections"]
        sections = [section for row in rows
                    if row["image_identity"]["cpu_type"] == 0x0100000C and row["image_identity"]["cpu_subtype"] == 0
                    for section in row["sections"]]
        selected = [section for section in sections if offset < int(section["offset"], 16) + section["bytes"] and
                    int(section["offset"], 16) < offset + extent]
        if (len(selected) != 1 or int(selected[0]["offset"], 16) > offset or
                offset + extent > int(selected[0]["offset"], 16) + selected[0]["bytes"]):
            raise ValueError("native data root must fit one exact original file-backed noninstruction section")

    for layout in recipe["types"]:
        if "runtime_identity" in layout:
            identity = layout["runtime_identity"]
            offset = int(identity["data_offset"], 16)
            rows = resources[identity["image"]["path"]]["native_path_data_sections"]
            sections = [section for row in rows
                        if row["image_identity"]["cpu_type"] == 0x0100000C and row["image_identity"]["cpu_subtype"] == 0
                        for section in row["sections"]]
            if not any(int(section["offset"], 16) <= offset and
                       offset + NP.ABI["pointer_bytes"] <= int(section["offset"], 16) + section["bytes"]
                       for section in sections):
                raise ValueError("native path vptr witness is outside independently pinned original data sections")

    for row in NP.evidence_plan(recipe):
        evidence = row["claim"]
        pin = pinned({"path": evidence["file"], "sha256": evidence["sha256"]}, MAX_RESULT_BYTES)
        try:
            selected = _json_pointer(R.load_json(evidence["file"]), evidence["pointer"])
        except (KeyError, IndexError, TypeError):
            raise ValueError("native path evidence does not select its exact reviewed contract") from None
        if not NP.same_data(selected, row["contract"]):
            raise ValueError("native path contract differs from its exact typed root/layout/lifetime/consumer")
        reviews = []
        for original in row["originals"]:
            pinned({"path": original["file"], "sha256": original["sha256"]},
                   MAX_RESULT_BYTES if original["role"] == "independent_review" else NP.MAX_ORIGINAL_BYTES)
            if original["role"] == "independent_review":
                reviews.append(original)
        if not reviews:
            raise ValueError("native path needs a separate source/build correlation review before root code")
        for review in reviews:
            NP.validate_admission(R.load_json(review["file"]), recipe, row)
        # JS receives this selected contract only after the independently
        # attributed originals and their precise correlation were checked.
        pin["native_path_contracts"] = [{"pointer": evidence["pointer"],
                                         "contract": copy.deepcopy(row["contract"]),
                                         "originals": copy.deepcopy(row["originals"])}]
        _merge_pins(resources, {pin["file"]: pin})

    if Q.is_qt_consumer(recipe.get("consumer")):
        _qt_moc_resources(recipe, resources)
    elif "consumer" in recipe:
        entry = recipe["consumer"]["call"]
        _merge_pins(resources, _declaration_resources(entry["module"], entry["signature_evidence"],
                                                       _callable_contract(entry)))
    if include_root:
        root = recipe["root"]
        if "getter" in root:
            _merge_pins(resources, native_resources(root["getter"]))
        for tag, contract_for in (("read", _pointer_read_contract), ("swift_read", _swift_read_contract)):
            if tag in root:
                read = root[tag]
                _merge_pins(resources, _declaration_resources(read["module"], read["evidence"], contract_for(read)))
    _native_path_budget([recipe], resources)
    return resources


def _native_path_budget(recipes, resources):
    originals = {}
    for recipe in recipes:
        for row in NP.evidence_plan(recipe):
            for original in row["originals"]:
                if original["file"] in originals and originals[original["file"]] != original["sha256"]:
                    raise ValueError("native path transaction has conflicting original pins")
                originals[original["file"]] = original["sha256"]
    if (len(originals) > NP.MAX_ORIGINAL_FILES or
            sum(resources[path]["bytes"] for path in originals) > NP.MAX_ORIGINAL_BYTES):
        raise ValueError("native path transaction original evidence exceeds its shared byte/file budget")


def _native_path_original_sizes(recipes):
    """Bound the complete original closure before hashing any large artifact."""
    R.assert_guest()
    originals = {}
    for recipe in recipes:
        for row in NP.evidence_plan(recipe):
            for original in row["originals"]:
                previous = originals.setdefault(original["file"], original["sha256"])
                if previous != original["sha256"]:
                    raise ValueError("native path transaction has conflicting original pins")
    if len(originals) > NP.MAX_ORIGINAL_FILES:
        raise ValueError("native path original file closure exceeds its shared budget")
    total = 0
    for name in originals:
        path = Path(name)
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise ValueError("native path original artifact must be a canonical regular file")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= NP.MAX_ORIGINAL_BYTES:
            raise ValueError("native path original artifact exceeds its bounded regular-file budget")
        total += info.st_size
        if total > NP.MAX_ORIGINAL_BYTES:
            raise ValueError("native path original byte closure exceeds its shared budget")


def _compiler_resources(evidence):
    """Both normal package imports and the fixed-file worker use the same verifier."""
    R.assert_guest()
    if __package__:
        from . import rea_native_adapter as A
    else:
        import importlib
        package_root = str(Path(__file__).resolve().parent.parent)
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        A = importlib.import_module("execution.rea_native_adapter")
    return A.resources(evidence)


def helper_policy_identity():
    """Operator configuration, never an agent-supplied library permission."""
    R.assert_guest()
    name = os.environ.get("ANYUSE_REA_RUNTIME_HELPER_POLICY")
    if name is None:
        return None
    if not _text(name, 4096) or not Path(name).is_absolute():
        raise Unavailable("helper policy needs an absolute protected operator file")
    path = Path(name)
    info = path.lstat()
    if (path.is_symlink() or path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise Unavailable("helper policy must be an owned regular mode0600 operator file")
    pin = _installed_file(str(path), MAX_REQUEST_BYTES, "helper policy is not a bounded installed JSON file")
    policy = R.load_json(path)
    _keys(policy, {"format", "target", "helpers"})
    if policy["format"] != HELPER_POLICY:
        raise ValueError("unsupported explicit helper loading policy")
    target = policy["target"]
    _keys(target, {"bundle", "fingerprint", "scope"})
    if (not _text(target["bundle"], 4096) or not Path(target["bundle"]).is_absolute() or
            Path(target["bundle"]).suffix != ".app" or Path(target["bundle"]).is_relative_to(Path("/System")) or
            target["scope"] != "owned_debug_target"):
        raise ValueError("helper loading policy needs one explicit owned debug app")
    fingerprint = target["fingerprint"]
    _keys(fingerprint, {"bundle_id", "version", "build", "sha256", "files", "bytes"})
    if (not _text(fingerprint["bundle_id"], 256) or not isinstance(fingerprint["sha256"], str) or
            SHA256.fullmatch(fingerprint["sha256"]) is None or type(fingerprint["files"]) is not int or fingerprint["files"] < 1 or
            type(fingerprint["bytes"]) is not int or not 0 < fingerprint["bytes"] <= R.MAX_BUNDLE_BYTES):
        raise ValueError("helper policy requires a whole analysed app fingerprint")
    helpers = policy["helpers"]
    if not isinstance(helpers, list) or not 1 <= len(helpers) <= 8:
        raise ValueError("explicit helper allowlist needs 1–8 pinned libraries")
    paths = set()
    for helper in helpers:
        _keys(helper, {"module", "build_evidence"})
        _file_claim(helper["module"]); _evidence_claim(helper["build_evidence"])
        if helper["module"]["path"] in paths:
            raise ValueError("helper policy cannot alias or redeclare a library")
        paths.add(helper["module"]["path"])
    return {**pin, "uid": info.st_uid, "mode": 0o600, "policy": copy.deepcopy(policy)}


def _helper_authorization(request, policy, root, target, folder):
    operations = [operation for operation in request["operations"] if operation["op"] == "load_helper"]
    if not operations:
        return
    if policy is None:
        raise Unavailable("helper loading needs explicit operator configuration; native invocation never loads a library")
    body = policy["policy"]
    if body["target"] != {"bundle": str(root), "fingerprint": target, "scope": "owned_debug_target"} or root.stat().st_uid != os.getuid():
        raise Unavailable("helper operator policy does not authorize this exact owned debug app")
    if Path(policy["file"]).is_relative_to(root) or Path(policy["file"]).is_relative_to(folder):
        raise Unavailable("helper operator policy must be outside the app and transaction evidence")
    for operation in operations:
        helper = {key: operation[key] for key in ("module", "build_evidence")}
        if helper not in body["helpers"]:
            raise Unavailable("requested helper is not explicitly authorized by the operator policy")
        if any(Path(name).is_relative_to(root) or Path(name).is_relative_to(folder)
               for name in (helper["module"]["path"], helper["build_evidence"]["file"])):
            raise Unavailable("runtime helper/build evidence must be staged outside the app and transaction evidence")


def _helper_resources(operation):
    pins = _compiler_resources(operation["build_evidence"])
    module = operation["module"]
    body = R.load_json(operation["build_evidence"]["file"])
    selected = _json_pointer(body, operation["build_evidence"]["pointer"])
    if not isinstance(selected, dict) or selected.get("helper", {}).get("module") != module:
        raise ValueError("authorized helper module differs from the generated compiler artifact")
    pin = _installed_file(module["path"], MAX_NATIVE_MODULE_BYTES, "helper is not a bounded canonical installed Mach-O file")
    if pin["file"] != module["path"] or pin["sha256"] != module["sha256"]:
        raise ValueError("helper code differs from the approved compiler output")
    path = Path(pin["file"])
    pin.update(macho_images=_macho_images(path), macho_sections=_macho_sections(path), macho_segments=_macho_segments(path))
    _merge_pins(pins, {pin["file"]: pin})
    return pins


def native_resources(actor):
    """VM-guarded module/prototype pins, including every borrowed getter/context."""
    R.assert_guest()
    resources = {}
    paths = native_path_recipes(actor)
    _native_path_original_sizes(paths)
    for call in _native_calls_recursive(actor):
        for claim, limit in ((call["target"]["module"], MAX_NATIVE_MODULE_BYTES),
                             ({"path": call["signature_evidence"]["file"], "sha256": call["signature_evidence"]["sha256"]},
                              MAX_RESULT_BYTES)):
            path = Path(claim["path"])
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError("native module/contract paths must be canonical regular files")
            pin = _installed_file(str(path), limit, "native module or contract is not a bounded installed regular file")
            if claim == call["target"]["module"]:
                pin["macho_images"] = _macho_images(path)
                pin["macho_sections"] = _macho_sections(path)
                pin["macho_segments"] = _macho_segments(path)
            if pin["sha256"] != claim["sha256"]:
                raise ValueError("native module/prototype bytes differ from their pinned evidence")
            _merge_pins(resources, {pin["file"]: pin})
        try:
            declaration_doc = R.load_json(call["signature_evidence"]["file"])
            found = _json_pointer(declaration_doc, call["signature_evidence"]["pointer"])
        except (KeyError, IndexError, TypeError):
            raise ValueError("native prototype evidence does not select a valid contract") from None
        if found != _contract(call):
            raise ValueError("native prototype evidence differs from the exact typed ABI contract")
        declared_document = isinstance(declaration_doc, dict) and declaration_doc.get("format") == "anyuse.rea-native-model-declaration/1"
        if call["signature_evidence"]["kind"] == "declared_native" or declared_document:
            from . import rea_native_contract as declaration
            _merge_pins(resources, declaration.resources(call))
        if "compiler_adapter" in call:
            evidence = call["compiler_adapter"]["evidence"]
            _merge_pins(resources, _compiler_resources(evidence))
            try:
                selected = _json_pointer(R.load_json(evidence["file"]), evidence["pointer"])
            except (KeyError, IndexError, TypeError):
                raise ValueError("compiler adapter evidence does not select a valid contract") from None
            if selected != _compiler_adapter_contract(call):
                raise ValueError("compiler adapter differs from its compiled family/entry/completion contract")
    for entry in callable_recipes(actor):
        _merge_pins(resources, _declaration_resources(entry["module"], entry["signature_evidence"], _callable_contract(entry)))
    for read, contract_for in [*((read, _pointer_read_contract) for read in pointer_read_recipes(actor)),
                               *((read, _swift_read_contract) for read in swift_read_recipes(actor))]:
        for claim, module in ((read["module"], True),
                              ({"path": read["evidence"]["file"], "sha256": read["evidence"]["sha256"]}, False)):
            path = Path(claim["path"])
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError("borrowed pointer module/evidence paths must be canonical regular files")
            pin = _installed_file(str(path), MAX_NATIVE_MODULE_BYTES if module else MAX_RESULT_BYTES,
                                  "borrowed ivar module or evidence is not a bounded installed regular file")
            if module:
                pin["macho_images"] = _macho_images(path)
                pin["macho_sections"] = _macho_sections(path)
                pin["macho_segments"] = _macho_segments(path)
            if pin["sha256"] != claim["sha256"]:
                raise ValueError("borrowed ivar module/evidence bytes differ from the frozen pins")
            _merge_pins(resources, {pin["file"]: pin})
        try:
            found = _json_pointer(R.load_json(read["evidence"]["file"]), read["evidence"]["pointer"])
        except (KeyError, IndexError, TypeError):
            raise ValueError("borrowed ivar evidence does not select a valid contract") from None
        if found != contract_for(read):
            raise ValueError("borrowed ivar evidence differs from exact receiver/owner/type/offset declaration")
    for recipe in paths:
        _merge_pins(resources, _native_path_resources(recipe, include_root=False))
    _native_path_budget(paths, resources)
    # This shared closure also feeds H's replay/publication checks. Never let
    # adding a path hide the root ABI, consumer declaration or nested original.
    if any(resources.get(path, {}).get("sha256") != sha for path, sha in native_file_claims(actor).items()):
        raise ValueError("native resource preparation lost a transitive recipe file claim")
    return resources


def _macho_data(path, *, include_native_path_data=False, include_lifecycle_code=False):
    """Guest-only bounded image UUID and instruction-section provenance."""
    R.assert_guest()
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        header = stream.read(32)
        slices = [(0, size)]
        if header[:4] in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
            endian = ">" if header[:4] in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"} else "<"
            count = struct.unpack(endian + "I", header[4:8])[0]
            if not 1 <= count <= 32:
                raise ValueError("native fat Mach-O slice count exceeds its budget")
            fat64 = header[:4] in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
            width = 32 if fat64 else 20
            stream.seek(8)
            data = stream.read(count * width)
            if len(data) != count * width:
                raise ValueError("native fat Mach-O slice table is incomplete")
            slices = []
            for index in range(count):
                entry = data[index * width:(index + 1) * width]
                offset, length = struct.unpack(endian + ("QQ" if fat64 else "II"), entry[8:24 if fat64 else 16])
                if offset < 8 + count * width or length < 32 or offset + length > size:
                    raise ValueError("native fat Mach-O slice exceeds the file")
                slices.append((offset, length))
        result, instruction_sections, image_segments, native_data_sections = [], [], [], []
        for offset, length in slices:
            stream.seek(offset)
            header = stream.read(32)
            if len(header) != 32 or header[:4] not in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
                raise ValueError("native callable module requires a 64-bit Mach-O image")
            endian = "<" if header[:4] == b"\xcf\xfa\xed\xfe" else ">"
            cpu, subtype, _, commands, byte_count = struct.unpack(endian + "IIIII", header[4:24])
            if not 1 <= commands <= 4096 or not 8 <= byte_count <= 1024**2 or 32 + byte_count > length:
                raise ValueError("native Mach-O load commands exceed bounded image headers")
            data = stream.read(byte_count)
            position, uuid, image_vmaddr = 0, None, None
            sections, segments, data_sections = [], [], []
            for _ in range(commands):
                if position + 8 > len(data):
                    raise ValueError("native Mach-O load command is incomplete")
                kind, width = struct.unpack(endian + "II", data[position:position + 8])
                if width < 8 or position + width > len(data):
                    raise ValueError("native Mach-O load command exceeds its header")
                if kind == 0x1B:
                    if width != 24 or uuid is not None:
                        raise ValueError("native Mach-O UUID is malformed or ambiguous")
                    uuid = data[position + 8:position + 24].hex()
                elif kind == 0x19:
                    if width < 72:
                        raise ValueError("native Mach-O segment command is incomplete")
                    command = data[position:position + width]
                    vmaddr, vmsize, fileoff, filesize = struct.unpack(endian + "QQQQ", command[24:56])
                    maximum, initial = struct.unpack(endian + "II", command[56:64])
                    try:
                        segment_name = command[8:24].split(b"\0", 1)[0].decode("ascii")
                    except UnicodeDecodeError:
                        raise ValueError("native Mach-O segment name is not bounded ASCII") from None
                    count = struct.unpack(endian + "I", command[64:68])[0]
                    if count > 4096 or width != 72 + count * 80 or fileoff + filesize > length:
                        raise ValueError("native Mach-O segment sections exceed bounded file headers")
                    if fileoff == 0 and filesize >= 32 + byte_count:
                        if image_vmaddr is not None:
                            raise ValueError("native Mach-O image-header segment is ambiguous")
                        image_vmaddr = vmaddr
                    if vmsize and not (vmaddr == 0 and filesize == 0 and maximum == 0 and initial == 0):
                        if not segment_name:
                            raise ValueError("native Mach-O mapped segment must have an exact name")
                        segments.append((vmaddr, vmsize, segment_name, fileoff, filesize, maximum, initial))
                    for index in range(count):
                        section = command[72 + index * 80:152 + index * 80]
                        try:
                            name = section[:16].split(b"\0", 1)[0].decode("ascii")
                            segment = section[16:32].split(b"\0", 1)[0].decode("ascii")
                        except UnicodeDecodeError:
                            raise ValueError("native Mach-O section names are not bounded ASCII") from None
                        address, section_bytes = struct.unpack(endian + "QQ", section[32:48])
                        file_offset, flags = struct.unpack(endian + "I", section[48:52])[0], struct.unpack(endian + "I", section[64:68])[0]
                        if address < vmaddr or address + section_bytes > vmaddr + vmsize:
                            raise ValueError("native Mach-O section exceeds its declared segment")
                        if flags & 0xFF not in {1, 0xC, 0x12} and file_offset + section_bytes > length:
                            raise ValueError("native Mach-O file-backed section exceeds its slice")
                        if section_bytes and flags & 0x80000400:
                            if not name or not segment or flags & 0xFF in {1, 0xC, 0x12}:
                                raise ValueError("native Mach-O instruction section must be named file-backed bytes")
                            if include_lifecycle_code and (segment != segment_name or not initial & 4 or not maximum & 4 or
                                    address - vmaddr + section_bytes > filesize or file_offset < fileoff or
                                    file_offset + section_bytes > fileoff + filesize or
                                    file_offset - fileoff != address - vmaddr):
                                raise ValueError("lifecycle instruction section must map exact original executable file-backed bytes")
                            sections.append((address, section_bytes, segment, name, flags))
                        elif include_native_path_data and section_bytes and flags & 0xFF not in {1, 0xC, 0x12}:
                            if not name or not segment:
                                raise ValueError("native path data sections must have exact original names")
                            if (address - vmaddr + section_bytes > filesize or file_offset < fileoff or
                                    file_offset + section_bytes > fileoff + filesize or
                                    file_offset - fileoff != address - vmaddr):
                                raise ValueError("native path file-backed data section does not match its original segment mapping")
                            data_sections.append((address, section_bytes, segment, name, flags))
                position += width
            if uuid is None or position != byte_count:
                raise ValueError("native callable module requires one exact LC_UUID image identity")
            identity = {"cpu_type": cpu, "cpu_subtype": subtype, "uuid": uuid}
            result.append(identity)
            if (sections or segments) and image_vmaddr is None:
                raise ValueError("native Mach-O instruction offsets lack a unique image-header segment")
            ordered = []
            for address, section_bytes, segment, name, flags in sorted(sections):
                if address < image_vmaddr:
                    raise ValueError("native Mach-O instruction section precedes its image header")
                relative = address - image_vmaddr
                if ordered and relative < int(ordered[-1]["offset"], 16) + ordered[-1]["bytes"]:
                    raise ValueError("native Mach-O instruction sections overlap")
                ordered.append({"segment": segment, "name": name, "offset": hex(relative), "bytes": section_bytes, "flags": flags})
            instruction_sections.append({"image_identity": identity, "sections": ordered})
            declared_data = []
            for address, section_bytes, segment, name, flags in sorted(data_sections):
                if image_vmaddr is None or address < image_vmaddr:
                    raise ValueError("native path data section precedes its image header")
                relative = address - image_vmaddr
                if declared_data and relative < int(declared_data[-1]["offset"], 16) + declared_data[-1]["bytes"]:
                    raise ValueError("native path data sections overlap")
                if any(relative < int(section["offset"], 16) + section["bytes"] and
                       int(section["offset"], 16) < relative + section_bytes for section in ordered):
                    raise ValueError("native path data and instruction sections overlap")
                declared_data.append({"segment": segment, "name": name, "offset": hex(relative),
                                      "bytes": section_bytes, "flags": flags})
            native_data_sections.append({"image_identity": identity, "sections": declared_data})
            declared = []
            for vmaddr, vmsize, name, fileoff, filesize, maximum, initial in sorted(segments):
                if vmaddr < image_vmaddr or vmaddr - image_vmaddr + vmsize > MAX_NATIVE_MODULE_BYTES:
                    raise ValueError("native Mach-O positive mapped segment extent exceeds its image budget")
                relative = vmaddr - image_vmaddr
                if declared and relative < int(declared[-1]["offset"], 16) + declared[-1]["bytes"]:
                    raise ValueError("native Mach-O mapped segments overlap")
                declared.append({"segment": name, "offset": hex(relative), "bytes": vmsize,
                                 "file_offset": fileoff, "file_bytes": filesize,
                                 "max_protection": maximum, "initial_protection": initial})
            image_segments.append({"image_identity": identity, "segments": declared})
        return result, instruction_sections, image_segments, native_data_sections


def _macho_images(path):
    """LC_UUID evidence is not a cryptographic hash of all mapped memory."""
    return _macho_data(path)[0]


def _macho_sections(path):
    """Only sections with actual Mach-O instruction attributes are callable."""
    return _macho_data(path)[1]


def _lifecycle_code_sections(path):
    """Stricter code admission only for lifecycle hooks and return PCs."""
    return _macho_data(path, include_lifecycle_code=True)[1]


def _native_path_data_sections(path):
    """Positive original file-backed noninstruction sections for path roots/vptrs."""
    return _macho_data(path, include_native_path_data=True)[3]


def _macho_segments(path):
    """Exact positive segment ranges; a maximum extent alone would include holes."""
    return _macho_data(path)[2]


def _text(value, limit=4096, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and "\0" not in value and
            len(value.encode("utf-8")) <= limit)


def _name(value, expression=CLASS):
    return isinstance(value, str) and expression.fullmatch(value) and value not in _RESERVED


def _class_name(value):
    return (isinstance(value, str) and 1 <= len(value) <= 256 and
            OBJC_CLASS.fullmatch(value) is not None and value.lstrip(".") not in _RESERVED)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("runtime request has missing or unknown fields")


def _integer(value, minimum, maximum, message):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(message)


def parse_encoding(signature: str) -> list[str]:
    """Parse only supported scalar/object Objective-C ABI types, not guessed ABIs."""
    if not _text(signature, 1024):
        raise ValueError("supply a bounded observed Objective-C encoding")
    result, i = [], 0
    while i < len(signature):
        while i < len(signature) and (signature[i].isdigit() or signature[i] in "rnNoORV"):
            i += 1
        if i >= len(signature):
            break
        token = signature[i]
        if token not in "vBcCsSiIlLqQfd@:#":
            raise ValueError("unsupported Objective-C ABI; pointers, blocks and aggregates need another adapter")
        i += 1
        if token == "@" and i < len(signature) and signature[i] == "?":
            raise ValueError("Objective-C blocks are not supported")
        if token == "@" and i < len(signature) and signature[i] == '"':
            end = signature.find('"', i + 1)
            if end < 0:
                raise ValueError("incomplete Objective-C object encoding")
            i = end + 1
        result.append(token)
    if not result:
        raise ValueError("empty Objective-C encoding")
    return result


def _signature(selector, signature, arguments, return_type):
    if not _name(selector, SELECTOR) or not isinstance(return_type, str) or return_type not in TYPES:
        raise ValueError("use an exact selector and a supported return type")
    tokens = parse_encoding(signature)
    if len(tokens) < 3 or tokens[1:3] != ["@", ":"]:
        raise ValueError("submitted method encoding needs implicit Objective-C self and _cmd fields")
    if len(tokens) != 3 + len(arguments):
        raise ValueError(f"submitted encoding declares {len(tokens) - 3} explicit arguments; "
                         f"arguments supplies {len(arguments)}; selector colon count is {selector.count(':')}. "
                         "self/_cmd are implicit. These counts describe submitted syntax, not native evidence.")
    if selector.count(":") != len(arguments) or tokens[0] not in ENCODINGS[return_type]:
        raise ValueError("declared arguments/return do not match the observed method encoding")
    for argument, token in zip(arguments, tokens[3:]):
        if argument["type"] not in TYPES - {"void"} or token not in ENCODINGS[argument["type"]]:
            raise ValueError("typed argument does not match the observed Objective-C encoding")


def _value(argument, *, allow_bindings=False, aliases=None):
    _keys(argument, {"type", "value"})
    kind, value = argument["type"], argument["value"]
    if not isinstance(kind, str) or kind not in TYPES - {"void"}:
        raise ValueError("unsupported runtime argument type")
    if allow_bindings and isinstance(value, dict) and set(value) == {"input"}:
        if kind not in OBJC_BINDING_TYPES or not isinstance(value["input"], str) or not R.NAME.fullmatch(value["input"]):
            raise ValueError("runtime input binding needs a supported scalar type and exact input name")
        return
    if kind == "utf8":
        valid = _text(value, empty=True)
    elif kind == "nil":
        valid = value is None
    elif kind == "bool":
        valid = type(value) is bool
    elif kind == "object":
        valid = (isinstance(value, dict) and set(value) == {"alias"} and isinstance(value["alias"], str) and
                 value["alias"] in (aliases or set()))
    elif kind in {"float", "double"}:
        try:
            valid = type(value) in {int, float} and math.isfinite(value) and abs(value) <= (3.402823466e38 if kind == "float" else 1.7976931348623157e308)
        except OverflowError:
            valid = False
    elif kind in {"int64", "uint64", "long", "ulong"}:
        signed = kind in {"int64", "long"}
        valid = (isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]{0,19})", value) is not None and
                 (-(2**63) if signed else 0) <= int(value) < (2**63 if signed else 2**64))
    else:
        signed, bits = kind.startswith("int"), int(re.search(r"\d+", kind)[0])
        valid = type(value) is int and (-(2**(bits - 1)) if signed else 0) <= value < (2**(bits - 1) if signed else 2**bits)
    if not valid:
        raise ValueError("runtime argument value does not fit its declared type")


def _receiver(receiver, aliases, *, recipe=False):
    if not isinstance(receiver, dict):
        raise ValueError("runtime receiver must be declarative")
    if set(receiver) == {"alias"}:
        if recipe or not isinstance(receiver["alias"], str) or receiver["alias"] not in aliases:
            raise ValueError("unknown or expired transaction object alias")
        return
    _keys(receiver, {"class", "kind"}, {"subclasses", "path"})
    if not _class_name(receiver["class"]) or not isinstance(receiver["kind"], str) or receiver["kind"] not in {"class", "unique"}:
        raise ValueError("resolve an exact class receiver or one unique live instance")
    if "subclasses" in receiver and (receiver["kind"] != "unique" or type(receiver["subclasses"]) is not bool):
        raise ValueError("subclasses applies only to live instance resolution")
    path = receiver.get("path", [])
    if not isinstance(path, list) or len(path) > 8:
        raise ValueError("receiver path exceeds the getter budget")
    for step in path:
        _keys(step, {"selector", "signature"})
        _signature(step["selector"], step["signature"], [], "object")


def _method_actor(actor, *, allow_bindings=False):
    _keys(actor, {"kind", "receiver", "selector", "signature", "arguments", "return_type"})
    if actor["kind"] != "objc_method":
        raise ValueError("expected objc_method actor")
    _receiver(actor["receiver"], set(), recipe=True)
    args = actor["arguments"]
    if not isinstance(args, list) or len(args) > 8:
        raise ValueError("method arguments exceed the runtime budget")
    for arg in args:
        _value(arg, allow_bindings=allow_bindings)
    _signature(actor["selector"], actor["signature"], args, actor["return_type"])


def validate_actor(actor, *, allow_bindings=False):
    """Validate typed call(s); ordered sequences do not imply native atomicity."""
    if isinstance(actor, dict) and actor.get("kind") in {"objc_sequence", "runtime_sequence"}:
        _keys(actor, {"kind", "steps"})
        steps = actor["steps"]
        if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_ACTOR_STEPS:
            raise ValueError("runtime actor sequence needs 2–8 method steps")
        for step in steps:
            if actor["kind"] == "runtime_sequence" and isinstance(step, dict) and step.get("kind") == "native_function":
                _native_actor(step, allow_bindings=allow_bindings)
            else:
                _method_actor(step, allow_bindings=allow_bindings)
        _borrowed_terminal_scope(steps, actors=True)
    elif isinstance(actor, dict) and actor.get("kind") == Q.ACTOR:
        Q.validate_actor(actor, validate_path=_native_path)
    elif isinstance(actor, dict) and actor.get("kind") == "native_function":
        _native_actor(actor, allow_bindings=allow_bindings)
    else:
        _method_actor(actor, allow_bindings=allow_bindings)
    return copy.deepcopy(actor)


def actor_methods(actor, *, allow_bindings=False):
    """Return validated method copies in exact recipe order, never flatten nesting."""
    methods = actor_calls(actor, allow_bindings=allow_bindings)
    if any(method["kind"] != "objc_method" for method in methods):
        raise ValueError("actor_methods only accepts Objective-C calls; use actor_calls for native recipes")
    return methods


def actor_calls(actor, *, allow_bindings=False):
    value = validate_actor(actor, allow_bindings=allow_bindings)
    return value["steps"] if value["kind"] in {"objc_sequence", "runtime_sequence"} else [value]


def argument_leaves(argument):
    """Typed binding leaves, including nested POD values and borrowed getters."""
    rows = []
    # Walk actual value paths rather than synthetic struct argument wrappers.
    def descend(kind, value, path):
        if isinstance(kind, dict) and kind.get("kind") == "struct":
            for field in kind["fields"]:
                descend(field["type"], value[field["name"]], path + [field["name"]])
        elif isinstance(value, dict) and set(value) == {"getter"}:
            getter = value["getter"]
            for index, child in enumerate(getter["arguments"]):
                descend(child["type"], child["value"], path + ["getter", "arguments", index, "value"])
            if "context" in getter:
                descend(getter["context"]["type"], getter["context"]["value"], path + ["getter", "context", "value"])
        elif (isinstance(kind, str) and kind in BINDING_TYPES or
              isinstance(kind, dict) and kind.get("kind") == "buffer" and kind["direction"] != "out"):
            rows.append({"path": path, "type": copy.deepcopy(kind), "value": copy.deepcopy(value)})
    descend(argument["type"], argument["value"], ["value"])
    return rows


def replace_argument_leaf(argument, path, value):
    if path not in [row["path"] for row in argument_leaves(argument)]:
        raise ValueError("runtime binding path is not an explicit typed leaf")
    result = copy.deepcopy(argument)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = copy.deepcopy(value)
    return result


def convert_binding(kind, value):
    """Convert frozen text explicitly and validate its declared scalar ABI range."""
    buffer = isinstance(kind, dict) and kind.get("kind") == "buffer" and kind.get("direction") in {"in", "inout"}
    if not buffer and (not isinstance(kind, str) or kind not in BINDING_TYPES):
        raise ValueError("runtime input binding needs a supported scalar type")
    if not isinstance(value, str):
        raise ValueError("runtime input must be frozen text for its declared type")
    if buffer:
        _native_type(kind)
        if not re.fullmatch(r"[0-9a-f]{" + str(kind["bytes"] * 2) + r"}", value):
            raise ValueError("buffer input must be exact canonical lowercase hexadecimal bytes")
        converted = list(bytes.fromhex(value))
        _native_argument({"type": kind, "value": converted})
        return converted
    elif kind == "utf8":
        converted = value
    elif kind == "bool":
        if value not in {"true", "false"}:
            raise ValueError("runtime boolean input must be literal true or false")
        converted = value == "true"
    elif kind in {"float", "double"}:
        if FLOAT_DECIMAL.fullmatch(value) is None or len(value) > 1024:
            raise ValueError("native float input must be finite explicit decimal text")
        converted = float(value)
    else:
        if DECIMAL.fullmatch(value) is None:
            raise ValueError("runtime integer input must be a canonical bounded decimal")
        converted = value if kind in {"int64", "uint64", "long", "ulong"} else int(value)
    _value({"type": kind, "value": converted})
    return converted


def bind_actor(actor, inputs):
    result = validate_actor(actor, allow_bindings=True)
    if not isinstance(inputs, dict):
        raise ValueError("runtime recipe inputs must be an object")
    methods = result["steps"] if result["kind"] in {"objc_sequence", "runtime_sequence"} else [result]
    for method in methods:
        for field in ("arguments", "context"):
            if field not in method:
                continue
            arguments = method[field] if field == "arguments" else [method[field]]
            for index, argument in enumerate(arguments):
                for leaf in argument_leaves(argument):
                    value = leaf["value"]
                    if isinstance(value, dict) and set(value) == {"input"}:
                        if value["input"] not in inputs:
                            raise ValueError("runtime recipe input is missing")
                        argument = replace_argument_leaf(argument, leaf["path"], convert_binding(leaf["type"], inputs[value["input"]]))
                if field == "arguments":
                    method[field][index] = argument
                else:
                    method[field] = argument
    # Validate every bound value before creating a worker request. A bad later
    # input cannot cause an earlier method to be invoked during binding.
    return validate_actor(result)


def invocation_request(actor):
    operations = []
    for method in actor_calls(actor):
        kind = method.pop("kind")
        operations.append({"op": Q.OPERATION if kind == Q.ACTOR else "native_invoke" if kind == "native_function" else "invoke", **method})
    return validate_request({"operations": operations})


def _native_fields(fields, size, *, graph=False):
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        raise ValueError("native read needs 1–32 declared fields")
    names = set()
    widths = {"bool": 1, "int8": 1, "uint8": 1, "int16": 2, "uint16": 2, "int32": 4,
              "uint32": 4, "int64": 8, "uint64": 8, "long": 8, "ulong": 8,
              "float": 4, "double": 8, "pointer": 8}
    for field in fields:
        _keys(field, {"name", "offset", "type"}, {"target"} if graph else {"alias"})
        if (not isinstance(field["name"], str) or not R.NAME.fullmatch(field["name"]) or field["name"] in names or
                not isinstance(field["type"], str) or field["type"] not in widths):
            raise ValueError("native memory fields require unique names and exact primitive widths")
        names.add(field["name"])
        _integer(field["offset"], 0, size - widths[field["type"]], "native field must fit declared object bytes")
        if "target" in field and (field["type"] != "pointer" or not _name(field["target"])):
            raise ValueError("native graph edges need an exact declared pointer target layout")
        if "alias" in field and (field["type"] != "pointer" or not isinstance(field["alias"], str) or
                                 not R.NAME.fullmatch(field["alias"])):
            raise ValueError("only native pointer fields may introduce a transaction alias")


def _native_receiver(receiver, aliases):
    if not isinstance(receiver, dict) or len(receiver) != 1:
        raise ValueError("native read receiver requires a declared borrowed getter or transaction alias")
    if "alias" in receiver:
        if receiver["alias"] not in aliases:
            raise ValueError("native read alias is unknown or expired")
    elif "native" in receiver:
        _native_actor(receiver["native"])
        if receiver["native"]["return_type"] != "pointer":
            raise ValueError("native read root getter must return a borrowed pointer")
        if native_path_recipes(receiver["native"]):
            raise ValueError("native path call arguments cannot be nested inside an investigation root getter")
    elif "native_path" in receiver:
        _native_path(receiver["native_path"])
    else:
        raise ValueError("native reads never accept persisted addresses")


def _graph(operation):
    native = isinstance(operation.get("root"), dict) and bool({"native", "native_path"} & set(operation["root"]))
    _keys(operation, {"op", "root", "types", "max_depth", "max_nodes", "methods_limit"}, {"root_type"} if native else ())
    _integer(operation["max_depth"], 0, 6, "graph depth must be 0–6")
    _integer(operation["max_nodes"], 1, 64, "graph node budget must be 1–64")
    _integer(operation["methods_limit"], 0, MAX_ROWS, "graph method row budget must be 0–128")
    types = operation["types"]
    if not isinstance(types, list) or not 1 <= len(types) <= 16:
        raise ValueError("graph requires 1–16 explicitly declared type layouts")
    names = set()
    if native:
        _native_receiver(operation["root"], set())
        if _immediate_paths(operation):
            raise ValueError("main-queue borrowed root is not a graph exploration lifetime")
        if operation["methods_limit"] != 0:
            raise ValueError("native declared-layout graph has no inferred method prototypes")
        for layout in types:
            _keys(layout, {"name", "bytes", "fields", "layout_evidence"})
            if not _name(layout["name"]) or layout["name"] in names:
                raise ValueError("graph native layout names must be exact and unique")
            names.add(layout["name"])
            _integer(layout["bytes"], 1, MAX_BUFFER_BYTES, "native object bytes exceed bounded read budget")
            _native_fields(layout["fields"], layout["bytes"], graph=True)
            _evidence_claim(layout["layout_evidence"])
        if operation.get("root_type") not in names:
            raise ValueError("native graph root needs a declared root layout")
        if "native_path" in operation["root"]:
            path = operation["root"]["native_path"]
            result = NP.path_types(path)[-1]
            layout = next(item for item in types if item["name"] == operation["root_type"])
            if (operation["root_type"] != path["result_type"] or layout["bytes"] != result["bytes"] or
                    not NP.same_data(layout["fields"], result["fields"])):
                raise ValueError("native path graph root must retain the exact admitted result layout")
    else:
        _receiver(operation["root"], set(), recipe=True)
        for layout in types:
            _keys(layout, {"class", "fields"})
            if not _class_name(layout["class"]) or layout["class"] in names:
                raise ValueError("graph Objective-C class names must be exact and unique")
            names.add(layout["class"])
            fields = layout["fields"]
            if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
                raise ValueError("graph fields exceed the per-node budget")
            base_fields = []
            for field in fields:
                if not isinstance(field, dict) or "alias" in field:
                    raise ValueError("graph does not persist or export pointer aliases")
                if field.get("kind") == "getter" and "signature" not in field:
                    raise ValueError("graph getter edges need their exact observed method signature")
                if field.get("kind") == "ivar" and "encoding" not in field:
                    raise ValueError("graph ivar edges need their exact observed encoding")
                if "target" in field and (field.get("type") != "object" or not _class_name(field["target"])):
                    raise ValueError("graph object edges require an exact declared target class")
                base_fields.append({key: value for key, value in field.items() if key != "target"})
            validate_request({"operations": [{"op": "read", "receiver": {"class": layout["class"], "kind": "unique"},
                                              "fields": base_fields}]})
    if any(field.get("target") not in names for layout in types for field in layout["fields"] if "target" in field):
        raise ValueError("graph edge target must have an explicit bounded layout")


def _native_path_read(operation):
    """A path receiver grants only its admitted result layout, never aliases."""
    recipe = operation["receiver"]["native_path"]
    result = NP.path_types(recipe)[-1]
    fields = {field["name"]: field for field in result["fields"]}
    if operation["bytes"] != result["bytes"]:
        raise ValueError("native path read extent differs from the admitted result layout")
    borrowed = NP.is_immediate_root(recipe)
    if borrowed and "consumer" in recipe:
        raise ValueError("borrowed root read requires its independent immediate-read lifetime admission")
    for field in operation["fields"]:
        declared = fields.get(field["name"])
        if ("alias" in field or declared is None or
                not NP.same_data(field, {key: declared[key] for key in ("name", "type", "offset")})):
            raise ValueError("native path reads require declared result fields and cannot export aliases")
    if borrowed:
        allowed = NP.borrowed_terminal_contract(recipe)["fields"]
        positions = []
        for field in operation["fields"]:
            if field["type"] not in NP.BORROWED_SCALAR_TYPES:
                raise ValueError("borrowed root read cannot expose pointers or non-scalar values")
            positions.append(allowed.index(field))
        if positions != sorted(positions):
            raise ValueError("borrowed root read projection must follow its admitted scalar declaration order")


def request_native_path_recipes(request):
    """Validated path rows in fixed operation/root/getter/argument order."""
    return [copy.deepcopy(value) for kind, value in NP.walk_recipes(validate_request(request))
            if kind == "native_path"]


def validate_native_path_observations(operation, row):
    """Validate a successful operation's exact path receipts, including none."""
    recipes = [value for kind, value in NP.walk_recipes(operation) if kind == "native_path"]
    return NP.validate_observations(row.get("native_paths", []), recipes,
                                    for_call=operation["op"] in {"native_invoke", Q.OPERATION})


def validate_native_path_counters(request, successful_rows, native_calls):
    """Pure terminal borrowed-scope check shared by collection and proof users.

    Existing lifetime modes retain their previous counter rules. A request
    containing the new mode needs the complete successful row sequence; callers
    preserving an incomplete prefix should call only after its borrowed terminal
    row completed. The selected mode receipt is validated here as well, so this
    boundary does not rely on an unvalidated epoch or a caller's Boolean proof.
    """
    operations = validate_request(request)["operations"]
    if not any(_borrowed_paths(operation) for operation in operations):
        return
    if type(successful_rows) is not list or len(successful_rows) != len(operations):
        raise ValueError("borrowed root counters require the complete successful operation sequence")
    for index, (operation, row) in enumerate(zip(operations, successful_rows)):
        if (type(row) is not dict or type(row.get("index")) is not int or row["index"] != index or
                row.get("op") != operation["op"] or row.get("status") != "ok"):
            raise ValueError("borrowed root counter rows differ from the exact request order/status")
    if type(native_calls) is not int or not 0 <= native_calls <= 4096:
        raise ValueError("borrowed root counter requires an exact bounded integer")
    rows = validate_native_path_observations(operations[-1], successful_rows[-1])
    scope = rows[0]["borrowed_scope"]
    expected = scope["getter_epoch"] + (operations[-1]["op"] in {"native_invoke", "qt_moc_invoke"})
    if native_calls != expected:
        raise ValueError("borrowed root terminal epoch differs from the final native call counter")
    if native_calls < minimum_request_native_calls(request):
        raise ValueError("borrowed root terminal epoch omits declared prior native calls")


def validate_lifecycle_observer_result(request, result):
    """Pure complete observer/use/counter binding, shared by live and proof users.

    Call on completed transactions only. Incomplete raw observer records remain
    failure evidence and cannot authorize a retained or subsequent use.
    """
    operations = validate_request(request)["operations"]
    paths = [recipe for operation in operations for recipe in _lifecycle_paths(operation)]
    if not isinstance(result, dict):
        raise ValueError("lifecycle result envelope must be data")
    if not paths:
        if "lifecycle_observer" in result:
            raise ValueError("runtime result carries an unrequested lifecycle observer")
        return None
    if result.get("format") != WORKER:
        raise ValueError("lifecycle observer requires the current worker cleanup envelope")
    rows = result.get("operations")
    if len(operations) != 1 or len(paths) != 1 or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("lifecycle observer requires its one complete terminal operation")
    operation, row = operations[0], rows[0]
    if (not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != 0 or
            row.get("op") != operation["op"] or row.get("status") != "ok"):
        raise ValueError("lifecycle observer row differs from its exact operation")
    observed = validate_native_path_observations(operation, row)
    return NL.validate_result(result.get("lifecycle_observer"), paths[0]["root"]["lifecycle"],
        scope=observed[0]["lifecycle_scope"], for_call=operation["op"] in {"native_invoke", Q.OPERATION},
        native_calls=result.get("native_calls"), invocations=result.get("invocations"))


def _require_native_path_backend(request):
    if not NATIVE_PATH_RUNTIME_READY and any(kind == "native_path" for kind, _ in NP.walk_recipes(request)):
        raise Unavailable("native pointer path runtime and receipt integration are not enabled in this source snapshot")


def _qt_moc_entry_pin(function, target, resources):
    """Pure loaded entry/section binding to the already checked worker resources."""
    pin = resources.get(target["module"]["path"])
    if not isinstance(pin, dict) or pin.get("file") != target["module"]["path"] or pin.get("sha256") != target["module"]["sha256"]:
        return False
    identity = function["image_identity"]
    images = pin.get("macho_images", [])
    sections = [row for row in pin.get("macho_sections", []) if NP.same_data(row.get("image_identity"), identity)]
    return (any(NP.same_data(image, identity) for image in images) and len(sections) == 1 and
            any(NP.same_data(section, function["instruction_section"]) for section in sections[0]["sections"]))


def validate_qt_moc_result(request, result):
    """Complete modern Qt dispatch/observer binding shared by live and retained H.

    Partial/error data is preserved by callers but never a successful Qt result.
    This verifies a receipt's contract, not unsigned-local evidence authenticity.
    """
    operations = validate_request(request)["operations"]
    selected = [operation for operation in operations if operation["op"] == Q.OPERATION]
    if not isinstance(result, dict):
        raise ValueError("Qt runtime result must be data")
    rows = result.get("operations", [])
    if not selected:
        if isinstance(rows, list) and any(isinstance(row, dict) and "qt_moc" in row for row in rows):
            raise ValueError("runtime result carries an unrequested Qt dispatch")
        return None
    if (len(operations) != 1 or len(selected) != 1 or result.get("format") != WORKER or
            result.get("status") != "completed" or result.get("error") is not None or result.get("attached") is not True or
            result.get("request_sha256") != R.digest(request) or
            result.get("handles_expired") is not True or result.get("native_state_uncertain") is not False or
            result.get("queue") != "main" or not isinstance(rows, list) or len(rows) != 1 or
            type(result.get("native_calls")) is not int or result["native_calls"] != minimum_request_native_calls(request) or
            type(result.get("invocations")) is not int or result["invocations"] != 1 or
            type(result.get("helper_load_attempts")) is not int or result["helper_load_attempts"] != 0 or
            type(result.get("callable_resolutions")) is not int or result["callable_resolutions"] != 0 or
            not NP.same_data(result.get("helper_residency"), []) or
            not NP.same_data(result.get("compiler_provenance"), {}) or
            not NP.same_data(result.get("cleanup"), {"script_unloaded": True, "detached": True})):
        raise ValueError("Qt dispatch requires its complete current-worker receiver and single-invocation clean disposition")
    transport = result.get("transport")
    if (not isinstance(transport, dict) or not isinstance(transport.get("kind"), str) or
            transport["kind"] not in {"local", "gadget"} or
            transport["kind"] == "local" and set(transport) != {"kind"} or
            result.get("preloaded_gadget_remains") is not (transport["kind"] == "gadget")):
        raise ValueError("Qt dispatch lost its retained transport/preloaded-agent disposition")
    _frida_lifecycle(result)
    operation = selected[0]
    recipe = operation["receiver"]["native_path"]
    resources = result.get("native_resources")
    if not isinstance(resources, dict):
        raise ValueError("Qt result lost its complete pinned native resource closure")
    consumer = recipe["consumer"]
    if any(not isinstance(resources.get(path), dict) or resources[path].get("sha256") != digest
           for path, digest in Q.file_claims(consumer).items()):
        raise ValueError("Qt result lost a selected artifact/image/admission file pin")
    records = resources[operation["selection"]["catalog"]["file"]].get("qt_moc_dispatches")
    if (not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict) or
            records[0].get("format") != Q.RUNTIME_PIN or
            not NP.same_data(records[0].get("selection"), operation["selection"]) or
            not NP.same_data(records[0].get("consumer"), Q.consumer_contract(consumer))):
        raise ValueError("Qt result lost the checked catalog/static-runtime enrichment")
    observation = Q.validate_row(rows[0], operation,
        validate_entry=lambda function, target: _qt_moc_entry_pin(function, target, resources))
    validate_native_path_observations(operation, rows[0])
    validate_lifecycle_observer_result(request, result)
    return observation


def request_resources(request):
    """Pin every requested native callable/module before fixed JS is loaded."""
    R.assert_guest()
    resources = {}
    paths = request_native_path_recipes(request)
    _native_path_original_sizes(paths)
    def merge(values):
        _merge_pins(resources, values)
    def layout_pin(layout):
        evidence = layout["layout_evidence"]
        path = Path(evidence["file"])
        pin = _installed_file(str(path), MAX_RESULT_BYTES, "native layout contract is not an installed bounded file")
        if pin["file"] != str(path) or pin["sha256"] != evidence["sha256"]:
            raise ValueError("native layout bytes differ from their pinned contract")
        try:
            found = _json_pointer(R.load_json(path), evidence["pointer"])
        except (KeyError, IndexError, TypeError):
            raise ValueError("native layout evidence does not select a contract") from None
        contract = {"bytes": layout["bytes"], "fields": layout["fields"], "pointer_policy": "borrowed_no_escape"}
        if found != contract:
            raise ValueError("native layout evidence differs from exact declared offsets/types")
        merge({pin["file"]: pin})
    for operation in validate_request(request)["operations"]:
        if operation["op"] == Q.OPERATION:
            merge(native_resources({"kind": Q.ACTOR, **{k: v for k, v in operation.items() if k != "op"}}))
        elif operation["op"] in MI.OPERATIONS:
            for claim in MI.module_claims(operation):
                pin = _installed_file(claim["path"], MAX_NATIVE_MODULE_BYTES, "memory provenance module is not an installed bounded file")
                pin["macho_images"] = _macho_images(Path(pin["file"]))
                pin["macho_sections"] = _macho_sections(Path(pin["file"]))
                pin["macho_segments"] = _macho_segments(Path(pin["file"]))
                if pin["file"] != claim["path"] or pin["sha256"] != claim["sha256"]:
                    raise ValueError("memory module file provenance differs from the pinned file")
                merge({pin["file"]: pin})
        elif operation["op"] == "native_invoke":
            merge(native_resources({"kind": "native_function", **{k: v for k, v in operation.items() if k not in {"op", "alias"}}}))
        elif operation["op"] in {"graph", "native_read"}:
            receiver = operation["root"] if operation["op"] == "graph" else operation["receiver"]
            if "native" in receiver:
                merge(native_resources(receiver["native"]))
            elif "native_path" in receiver:
                merge(_native_path_resources(receiver["native_path"]))
            if operation["op"] == "graph" and {"native", "native_path"} & set(receiver):
                for layout in operation["types"]:
                    layout_pin(layout)
            if operation["op"] == "native_read":
                layout_pin(operation)
        elif operation["op"] in {"symbols", "swift_fields"} or operation["op"] == "swift_types" and "image" in operation:
            claim = operation["image"] if operation["op"] == "swift_types" else operation["module"]
            pin = _installed_file(claim["path"], MAX_NATIVE_MODULE_BYTES, "symbol module is not an installed bounded file")
            pin["macho_images"] = _macho_images(Path(pin["file"]))
            pin["macho_sections"] = _macho_sections(Path(pin["file"]))
            pin["macho_segments"] = _macho_segments(Path(pin["file"]))
            if pin["file"] != claim["path"] or pin["sha256"] != claim["sha256"]:
                raise ValueError("symbol module bytes differ from the pinned image")
            merge({pin["file"]: pin})
        elif operation["op"] == "swift_read":
            read = operation["read"]
            for claim, module in ((read["module"], True),
                                  ({"path": read["evidence"]["file"], "sha256": read["evidence"]["sha256"]}, False)):
                pin = _installed_file(claim["path"], MAX_NATIVE_MODULE_BYTES if module else MAX_RESULT_BYTES,
                                      "Swift field module/evidence is not a bounded installed regular file")
                if pin["file"] != claim["path"] or pin["sha256"] != claim["sha256"]:
                    raise ValueError("Swift field bytes differ from their pinned contract")
                if module:
                    pin["macho_images"] = _macho_images(Path(pin["file"]))
                    pin["macho_sections"] = _macho_sections(Path(pin["file"]))
                    pin["macho_segments"] = _macho_segments(Path(pin["file"]))
                merge({pin["file"]: pin})
            try:
                found = _json_pointer(R.load_json(read["evidence"]["file"]), read["evidence"]["pointer"])
            except (KeyError, IndexError, TypeError):
                raise ValueError("Swift field evidence does not select a valid contract") from None
            if found != _swift_read_contract(read):
                raise ValueError("Swift field evidence differs from exact reflected owner/type/lifecycle contract")
        elif operation["op"] == "resolve_callable":
            entry = operation["entry"]
            merge(_declaration_resources(entry["module"], entry["signature_evidence"], _callable_contract(entry)))
        elif operation["op"] == "load_helper":
            merge(_helper_resources(operation))
    _native_path_budget(paths, resources)
    return resources


def request_compiler_provenance(request):
    """Public compiler artifacts, derived only from revalidated fixed-family builds."""
    R.assert_guest()
    claims = []
    for operation in validate_request(request)["operations"]:
        if operation["op"] == "load_helper":
            claims.append(operation["build_evidence"])
            continue
        root = None
        if operation["op"] == "native_invoke":
            root = {"kind": "native_function", **{key: value for key, value in operation.items() if key not in {"op", "alias"}}}
        elif operation["op"] in {"graph", "native_read"}:
            receiver = operation["root"] if operation["op"] == "graph" else operation["receiver"]
            root = receiver.get("native")
        if root is not None:
            claims.extend(call["compiler_adapter"]["evidence"] for call in _native_calls_recursive(root)
                          if "compiler_adapter" in call)
    provenance = {}
    for evidence in claims:
        previous = provenance.get(evidence["file"])
        if previous is not None:
            if previous["evidence"] != evidence:
                raise ValueError("one compiler manifest cannot have conflicting evidence claims")
            continue
        pins = _compiler_resources(evidence)
        manifest = pins.get(evidence["file"])
        if manifest is None or manifest["sha256"] != evidence["sha256"]:
            raise ValueError("compiler verifier did not pin its exact manifest")
        body = R.load_json(evidence["file"])
        if R._file_hash(Path(evidence["file"])) != evidence["sha256"]:
            raise ValueError("compiler manifest changed while its provenance was retained")
        artifacts = body["artifacts"]
        if any({key: row[key] for key in ("file", "sha256", "bytes")} != pins.get(row["file"])
               for row in artifacts):
            raise ValueError("compiler provenance artifacts differ from the verified transitive pins")
        provenance[evidence["file"]] = {"evidence": copy.deepcopy(evidence), "artifacts": copy.deepcopy(artifacts),
                                        "manifest_sha256": manifest["sha256"]}
    return provenance


def validate_request(request):
    """Pure validation for the agent loop; caller VM-guards before any native IO."""
    _keys(request, {"operations"})
    operations = request["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ValueError("runtime transaction needs 1–16 bounded operations")
    if MI.is_request(request):
        return MI.validate_request(request)
    aliases = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("runtime operation must be an object")
        op = operation.get("op")
        if not isinstance(op, str):
            raise ValueError("runtime operation needs an exact supported name")
        if op == "classes":
            _keys(operation, {"op"}, {"query", "owner", "limit"})
            if (not _text(operation.get("query", ""), 256, empty=True) or
                    not isinstance(operation.get("owner", "app"), str) or operation.get("owner", "app") not in {"app", "all"}):
                raise ValueError("class discovery takes a literal query and app/all module scope")
        elif op in {"methods", "ivars"}:
            _keys(operation, {"op", "class"}, {"query", "own", "limit"})
            if (not _class_name(operation["class"]) or not _text(operation.get("query", ""), 256, empty=True) or
                    type(operation.get("own", True)) is not bool):
                raise ValueError("metadata discovery needs exact class, literal query and own flag")
        elif op == "instances":
            _keys(operation, {"op", "class", "alias"}, {"subclasses"})
            if (not _class_name(operation["class"]) or not R.NAME.fullmatch(str(operation["alias"])) or
                    operation["alias"] in aliases or type(operation.get("subclasses", False)) is not bool):
                raise ValueError("live instance selection needs an unused alias and an exact class")
            aliases.add(operation["alias"])
        elif op == "read":
            _keys(operation, {"op", "receiver", "fields"}, {"expected_class"})
            if "expected_class" in operation and not _class_name(operation["expected_class"]):
                raise ValueError("read expected_class needs an exact Objective-C class")
            _receiver(operation["receiver"], aliases)
            fields = operation["fields"]
            if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
                raise ValueError("state read needs 1–32 explicitly declared fields")
            names = set()
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("kind"), str) or field.get("kind") not in {"getter", "ivar"}:
                    raise ValueError("state field must declare getter or ivar")
                _keys(field, {"name", "kind", "type", "selector" if field["kind"] == "getter" else "ivar"},
                      {"signature", "encoding", "alias"})
                if (not R.NAME.fullmatch(str(field["name"])) or field["name"] in names or
                        not isinstance(field["type"], str) or field["type"] not in TYPES - {"void", "nil"}):
                    raise ValueError("state fields need unique names and supported scalar/object types")
                names.add(field["name"])
                if field["kind"] == "getter":
                    if "encoding" in field:
                        raise ValueError("getter fields use method signatures, not ivar encodings")
                    if "signature" in field:
                        _signature(field["selector"], field["signature"], [], field["type"])
                    elif not _name(field["selector"], SELECTOR) or ":" in field["selector"]:
                        raise ValueError("read getters must have no arguments")
                else:
                    if "signature" in field or not _name(field["ivar"]):
                        raise ValueError("ivar field must use an exact name and optional encoding")
                    if "encoding" in field and parse_encoding(field["encoding"]) not in [[x] for x in ENCODINGS[field["type"]]]:
                        raise ValueError("ivar encoding does not match its declared type")
                if "alias" in field:
                    if (field["type"] != "object" or not R.NAME.fullmatch(str(field["alias"])) or field["alias"] in aliases):
                        raise ValueError("only object fields may introduce an unused transaction alias")
                    aliases.add(field["alias"])
        elif op == "invoke":
            _keys(operation, {"op", "receiver", "selector", "signature", "arguments", "return_type"}, {"alias"})
            _receiver(operation["receiver"], aliases)
            args = operation["arguments"]
            if not isinstance(args, list) or len(args) > 8:
                raise ValueError("method arguments exceed the runtime budget")
            for arg in args:
                _value(arg, aliases=aliases)
            _signature(operation["selector"], operation["signature"], args, operation["return_type"])
            if "alias" in operation:
                if (operation["return_type"] != "object" or not R.NAME.fullmatch(str(operation["alias"])) or operation["alias"] in aliases):
                    raise ValueError("only object returns may introduce an unused transaction alias")
                aliases.add(operation["alias"])
        elif op == Q.OPERATION:
            _keys(operation, {"op", "receiver", "selection"})
            Q.validate_actor({"kind": Q.ACTOR, "receiver": operation["receiver"], "selection": operation["selection"]},
                             validate_path=_native_path)
        elif op == "native_invoke":
            _keys(operation, {"op", "target", "abi", "arguments", "return_type", "signature_evidence"}, {"context", "alias", "compiler_adapter"})
            _native_actor({"kind": "native_function", **{k: v for k, v in operation.items() if k not in {"op", "alias"}}}, aliases=aliases)
            if "alias" in operation:
                if operation["return_type"] != "pointer" or not isinstance(operation["alias"], str) or not R.NAME.fullmatch(operation["alias"]) or operation["alias"] in aliases:
                    raise ValueError("only borrowed native pointer returns may introduce a transaction alias")
                aliases.add(operation["alias"])
        elif op == "modules":
            _keys(operation, {"op"}, {"query", "owner", "limit"})
            if not _text(operation.get("query", ""), 256, empty=True) or operation.get("owner", "app") not in {"app", "all"}:
                raise ValueError("module discovery needs a literal query and app/all scope")
        elif op == "symbols":
            _keys(operation, {"op", "module"}, {"query", "kind", "limit"})
            _file_claim(operation["module"])
            if not _text(operation.get("query", ""), 256, empty=True) or operation.get("kind", "symbols") not in {"symbols", "exports"}:
                raise ValueError("symbol discovery does not infer prototypes or ABIs")
        elif op == "swift_types":
            _keys(operation, {"op"}, {"module", "image", "query", "limit"})
            if not _text(operation.get("query", ""), 256, empty=True) or "module" in operation and not _name(operation["module"]):
                raise ValueError("Swift metadata discovery needs exact module and literal query")
            if "image" in operation:
                _file_claim(operation["image"])
        elif op == "swift_fields":
            _keys(operation, {"op", "receiver", "receiver_class", "swift_class", "module"}, {"query", "limit"})
            _swift_owner(operation)
            if not _text(operation.get("query", ""), 256, empty=True):
                raise ValueError("Swift field discovery requires a bounded literal query")
        elif op == "swift_read":
            _keys(operation, {"op", "read"})
            _swift_read(operation["read"])
        elif op == "resolve_callable":
            _keys(operation, {"op", "entry"})
            _callable(operation["entry"])
        elif op == "load_helper":
            _keys(operation, {"op", "module", "build_evidence"})
            _file_claim(operation["module"]); _evidence_claim(operation["build_evidence"])
        elif op == "native_read":
            _keys(operation, {"op", "receiver", "bytes", "fields", "layout_evidence"})
            _native_receiver(operation["receiver"], aliases)
            _integer(operation["bytes"], 1, MAX_BUFFER_BYTES, "native object bytes exceed bounded read budget")
            _native_fields(operation["fields"], operation["bytes"])
            _evidence_claim(operation["layout_evidence"])
            if "native_path" in operation["receiver"]:
                _native_path_read(operation)
            for field in operation["fields"]:
                if "alias" in field:
                    if field["alias"] in aliases:
                        raise ValueError("native pointer alias already exists")
                    aliases.add(field["alias"])
        elif op == "graph":
            _graph(operation)
        else:
            raise ValueError("unsupported runtime operation; source and addresses are not request fields")
        if op in {"classes", "methods", "ivars", "modules", "symbols", "swift_types", "swift_fields"}:
            _integer(operation.get("limit", MAX_ROWS), 1, MAX_ROWS, "metadata row limit must be 1–128")
    _borrowed_terminal_scope(operations)
    if any(operation["op"] == Q.OPERATION for operation in operations) and len(operations) != 1:
        raise ValueError("Qt metacall requires one operation and one observed receiver")
    value = copy.deepcopy(request)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
        raise ValueError("runtime request exceeds its byte budget")
    return value


def _expected_identity(value):
    """Freeze a complete ephemeral NS identity without filesystem or target IO."""
    if value is None:
        return None
    message = "expected runtime process identity needs exact bounded same-user NS fields"
    keys = {"bundle", "bundle_id", "executable", "launch_time", "pid", "uid"}
    if type(value) is not dict:
        raise ValueError(message)
    pin = value.copy()
    if set(pin) != keys:
        raise ValueError(message)
    for key in ("bundle", "bundle_id", "executable"):
        text = pin[key]
        try:
            valid = (type(text) is str and _text(text, 512 if key == "bundle_id" else 4096) and
                     all(32 <= ord(char) != 127 for char in text))
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError(message)
        if key != "bundle_id":
            path = Path(text)
            if not path.is_absolute() or text.startswith("//") or str(path) != text or ".." in path.parts:
                raise ValueError(message)
    launched = pin["launch_time"]
    # Limit timestamps to the positive Unix range through year 9999. Checking
    # the range first also rejects huge Python integers without float overflow.
    if (type(launched) not in {int, float} or not 0 < launched < 253402300800 or not math.isfinite(launched) or
            type(pin["pid"]) is not int or not 1 <= pin["pid"] <= 2**31 - 1 or
            type(pin["uid"]) is not int or not 0 <= pin["uid"] < 2**32 - 1 or pin["uid"] != os.getuid()):
        raise ValueError(message)
    return MappingProxyType(pin)


def process_identity(bundle: Path, bundle_id: str) -> dict:
    """Find exactly one existing same-user app; never launch or elevate it."""
    R.assert_guest()
    import AppKit
    apps = list(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id))
    if len(apps) != 1:
        raise Unavailable("need one unambiguous running target app")
    app = apps[0]
    url, executable, launch = app.bundleURL(), app.executableURL(), app.launchDate()
    if (not url or Path(str(url.path())).resolve(strict=True) != bundle or not executable or
            not launch or app.isTerminated()):
        raise Unavailable("running app identity does not match the exact target bundle")
    pid, launched = int(app.processIdentifier()), float(launch.timeIntervalSince1970())
    if pid <= 0 or not math.isfinite(launched) or launched <= 0:
        raise Unavailable("running app has incomplete process identity")
    # NSRunningApplication does not identify ownership. ps queries only this PID.
    result = R.subprocess.run(["/bin/ps", "-o", "uid=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5, check=False)
    if result.returncode or result.stdout.strip() != str(os.getuid()):
        raise Unavailable("target is not an existing app owned by this guest user")
    return {"bundle_id": bundle_id, "bundle": str(bundle), "executable": str(Path(str(executable.path())).resolve(strict=True)),
            "pid": pid, "launch_time": launched, "uid": os.getuid()}


def _installed_file(value, limit, message):
    if not value:
        raise Unavailable(message)
    path = Path(value)
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= limit:
        raise Unavailable(message)
    path = path.resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size}


def _adapter(value):
    adapter = _installed_file(value, MAX_AGENT_BYTES, "configure an already installed, compiled guest runtime adapter")
    manifest = _installed_file(adapter["file"] + ".manifest.json", MAX_REQUEST_BYTES,
                               "compiled guest adapter needs its source/build hash manifest")
    build = R.load_json(manifest["file"])
    source_hash = R._file_hash(Path(__file__).with_name("rea_runtime_agent.js"))
    if (not isinstance(build, dict) or build.get("format") != "anyuse.rea-runtime-adapter/1" or
            build.get("source_sha256") != source_hash or build.get("bundle_sha256") != adapter["sha256"]):
        raise Unavailable("compiled guest adapter does not match this fixed source and build manifest")
    return {**adapter, "source_sha256": source_hash, "manifest": manifest, "build": build}


def _private_file(value, limit, message):
    """Read bounded private configuration without following a file symlink."""
    try:
        path = Path(value) if value else None
        if path is None or not path.is_absolute():
            raise Unavailable(message)
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                stat.S_IMODE(before.st_mode) != 0o600 or not 1 <= before.st_size <= limit):
            raise Unavailable(message)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode, opened.st_size) != (
                    before.st_dev, before.st_ino, before.st_uid, before.st_mode, before.st_size):
                raise Unavailable(message)
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (len(data) != opened.st_size or (after.st_size, after.st_mtime_ns) !=
                    (opened.st_size, opened.st_mtime_ns)):
                raise Unavailable(message)
        identity = {"file": str(path.resolve(strict=True)), "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "uid": opened.st_uid, "mode": "0600"}
        return identity, data
    except (OSError, TypeError, ValueError):
        # Invalid JSON/UTF-8 or OS diagnostics must never include file contents.
        raise Unavailable(message) from None


def _transport(*, secrets=None, backend=None):
    """Load explicit guest configuration; never select a fallback after denial."""
    if backend is not None and (not isinstance(backend, str) or backend not in {"local", "gadget"}):
        raise ValueError("runtime backend override must explicitly be local or gadget")
    kind = backend if backend is not None else os.environ.get("ANYUSE_REA_RUNTIME_TRANSPORT", "local")
    if kind == "local":
        return {"kind": "local"}, None
    if kind != "gadget":
        raise Unavailable("runtime transport must explicitly be local or gadget")
    token_file, token_bytes = _private_file(os.environ.get("ANYUSE_REA_RUNTIME_GADGET_TOKEN_FILE"),
                                          MAX_TOKEN_BYTES, "Gadget token needs an owned regular 0600 guest file")
    try:
        token = token_bytes.decode("utf-8")
    except UnicodeError:
        raise Unavailable("Gadget token must be bounded UTF-8 text") from None
    if (not _text(token, MAX_TOKEN_BYTES) or len(token_bytes) < MIN_TOKEN_BYTES or
            "\n" in token or "\r" in token):
        raise Unavailable("Gadget token must be 32–4096 UTF-8 bytes without line endings")
    if secrets is not None:
        # Keep known authentication material available for failure redaction
        # even when a later library/config validation raises before return.
        secrets.append(token)
    endpoint = os.environ.get("ANYUSE_REA_RUNTIME_GADGET_ENDPOINT", "")
    match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", endpoint)
    if match is None or not 1 <= int(match[1]) <= 65535:
        raise Unavailable("Gadget requires a literal 127.0.0.1 endpoint and port")
    library_value = os.environ.get("ANYUSE_REA_RUNTIME_GADGET_LIBRARY")
    if not library_value or not Path(library_value).is_absolute():
        raise Unavailable("configure an already installed guest Gadget library")
    library = _installed_file(library_value, MAX_GADGET_BYTES, "configure an already installed guest Gadget library")
    config_file, config_bytes = _private_file(str(Path(library["file"]).with_suffix(".config")), MAX_REQUEST_BYTES,
                                            "Gadget config needs an owned regular 0600 guest file")
    try:
        config = json.loads(config_bytes)
        interaction = config["interaction"]
        if (set(config) - {"interaction", "teardown", "runtime", "code_signing"} or
                not isinstance(interaction, dict) or set(interaction) != {
                    "type", "address", "port", "token", "on_port_conflict", "on_load"} or
                interaction["type"] != "listen" or interaction["address"] != "127.0.0.1" or
                type(interaction["port"]) is not int or interaction["port"] != int(match[1]) or
                interaction["token"] != token or interaction["on_port_conflict"] != "fail" or
                interaction["on_load"] != "resume" or config.get("teardown", "minimal") not in {"minimal", "full"} or
                config.get("runtime", "default") not in {"default", "qjs", "v8"} or
                config.get("code_signing", "optional") != "optional"):
            raise ValueError("invalid Gadget listener configuration")
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise Unavailable("Gadget config must match the authenticated loopback listener, fail on conflict and resume") from None
    return {"kind": "gadget", "endpoint": endpoint, "token_file": token_file,
            "library": library, "config": config_file}, token


def _transport_outside(transport, bundle, folder):
    if transport["kind"] == "gadget":
        for field in ("token_file", "library", "config"):
            path = Path(transport[field]["file"])
            if path.is_relative_to(bundle) or path.is_relative_to(folder):
                raise Unavailable("Gadget configuration, token and library must be outside the app and evidence folder")


def _redact(value, token):
    """Remove authentication material before diagnostics or receipts are saved."""
    if not token:
        return value
    if isinstance(token, (tuple, list)):
        for secret in token:
            value = _redact(value, secret)
        return value
    if isinstance(value, str):
        return value.replace(token, "<redacted-runtime-secret>")
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {_redact(key, token): _redact(item, token) for key, item in value.items()}
    return value


def _error(error, token, *, stack=False):
    detail = {"type": type(error).__name__, "message": _redact(str(error), token)[:4096]}
    if stack:
        detail["stack"] = _redact(traceback.format_exc(), token)[-8192:]
    return detail


def _redact_artifacts(folder, token):
    if not token:
        return
    tokens = token if isinstance(token, (tuple, list)) else [token]
    patterns = {pattern for secret in tokens for pattern in (
        secret.encode("utf-8"), json.dumps(secret, ensure_ascii=True)[1:-1].encode("utf-8"))}
    for name in ("worker-request.json", "worker-output.json", "worker.stderr", "trace.jsonl", "worker-cleanup.json"):
        path = folder / name
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > MAX_RESULT_BYTES:
            path.write_bytes(b"diagnostic omitted: exceeded redaction byte budget\n")
            continue
        data = path.read_bytes()
        redacted = data
        for pattern in patterns:
            redacted = redacted.replace(pattern, b"<redacted-runtime-secret>")
        if redacted != data:
            path.write_bytes(redacted)


def adapter_identity(*, backend=None):
    """Pin the deployed fixed-source adapter before a saved recipe can actuate."""
    R.assert_guest()
    transport, _ = _transport(backend=backend)
    identity = {**_adapter(os.environ.get("ANYUSE_REA_RUNTIME_AGENT")), "transport": transport}
    policy = helper_policy_identity()
    if policy is not None:
        identity["helper_policy"] = policy
    return identity


def _memory_process_identity(request, result, expected_identity=None):
    """Bind returned raw observations to the independently captured process.

    Process.arch remains a declared supported runtime fact; the existing NS
    identity has no architecture field and supplies no independent arch proof.
    """
    if not MI.is_request(request) or result.get("memory_inspection") is None:
        return
    identity = _expected_identity(result.get("identity"))
    process = result.get("process")
    if (identity is None or not isinstance(process, dict) or type(process.get("pid")) is not int or
            process["pid"] != identity["pid"] or process.get("executable") != identity["executable"]):
        raise ValueError("raw process observation differs from the captured worker identity")
    after = result.get("after_identity")
    if after is not None:
        _expected_identity(after)
    if result.get("status") == "completed" and not NP.same_data(after, dict(identity)):
        raise ValueError("completed raw observation lost its unchanged worker process identity")
    if expected_identity is not None and (not NP.same_data(dict(identity), dict(expected_identity)) or
            after is not None and not NP.same_data(after, dict(expected_identity))):
        raise ValueError("raw worker process differs from the independently frozen caller identity")


def _worker_result(result, request_hash, request, transport, *, expected_permit=None):
    if (not isinstance(result, dict) or result.get("format") not in (WORKER, LEGACY_WORKER) or
            result.get("transport") != transport or
            result.get("preloaded_gadget_remains") is not (transport["kind"] == "gadget") or
            result.get("request_sha256") != request_hash or type(result.get("attached")) is not bool or
            type(result.get("native_state_uncertain")) is not bool or
            result.get("status") not in {"completed", "incomplete", "unavailable"} or
            not isinstance(result.get("operations"), list) or len(result["operations"]) > len(request["operations"]) or
            not (type(result.get("native_calls")) is int and 0 <= result["native_calls"] <= 4096 or
                 result.get("native_calls") is None and result["status"] == "incomplete") or
            not (type(result.get("invocations")) is int and 0 <= result["invocations"] <= sum(op["op"] in {"invoke", "native_invoke", Q.OPERATION} for op in request["operations"]) or
                 result.get("invocations") is None and result["status"] == "incomplete")):
        raise ValueError("runtime worker returned a malformed or unbound result")
    if any(operation["op"] == Q.OPERATION for operation in request["operations"]) and result["format"] != WORKER:
        raise ValueError("Qt dispatch never existed in historical worker envelopes")
    if result["format"] == WORKER:
        _frida_lifecycle(result)
    elif "frida_lifecycle" in result or "cleanup_errors" in result:
        raise ValueError("legacy runtime worker cannot contain a modern lifecycle disposition")
    attempts = result.get("helper_load_attempts", 0 if not any(op["op"] == "load_helper" for op in request["operations"]) else None)
    if not (type(attempts) is int and 0 <= attempts <= sum(op["op"] == "load_helper" for op in request["operations"]) or
            attempts is None and result["status"] == "incomplete"):
        raise ValueError("runtime worker lost explicit helper load disposition")
    resolutions = result.get("callable_resolutions", 0)
    if type(resolutions) is not int or not 0 <= resolutions <= 128:
        raise ValueError("runtime worker returned invalid callable resolution counters")
    observed_invocations = 0
    borrowed_terminal_completed = False
    for index, row in enumerate(result["operations"]):
        if (not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index or
                row.get("op") != request["operations"][index]["op"] or row.get("status") != "ok"):
            raise ValueError("runtime worker operation evidence does not match the request")
        operation = request["operations"][index]
        if operation["op"] != Q.OPERATION and "qt_moc" in row:
            raise ValueError("runtime operation carries an unrequested Qt dispatch observation")
        validate_native_path_observations(operation, row)
        if _borrowed_paths(operation):
            borrowed_terminal_completed = True
        observed_invocations += operation["op"] in {"invoke", "native_invoke", Q.OPERATION}
    if borrowed_terminal_completed:
        validate_native_path_counters(request, result["operations"], result["native_calls"])
    minimum_calls = minimum_request_native_calls(request, completed_operations=len(result["operations"]))
    if type(result["native_calls"]) is int and result["native_calls"] < minimum_calls:
        raise ValueError("runtime worker native call counter omitted a declared dispatch or receiver-path getter")
    if type(result["invocations"]) is int and result["invocations"] < observed_invocations:
        raise ValueError("runtime worker invocation counter omitted a completed declared dispatch")
    if result["status"] == "completed" and (not result["attached"] or result.get("handles_expired") is not True or
            len(result["operations"]) != len(request["operations"]) or result.get("native_state_uncertain") is True):
        raise ValueError("completed runtime result has missing operations or live object handles")
    if result["status"] == "completed":
        validate_lifecycle_observer_result(request, result)
        validate_qt_moc_result(request, result)
    MI.validate_result(request, result, worker=True)
    _memory_process_identity(request, result)
    _permit_result(result, expected_permit)
    return result


def _permit_result(result, expected):
    observed = result.get("pre_actuation_ack")
    if expected is None:
        if "pre_actuation_ack" in result: raise ValueError("unrequested pre-actuation permit evidence")
        return
    binding = RP.validate_binding(expected)
    if result.get("format") != WORKER or result.get("request_sha256") != binding["request_sha256"]:
        raise ValueError("current permit evidence must bind this actual worker request")
    if observed is None:
        if (result.get("status") == "completed" or result.get("operations") or
                result.get("native_calls") not in (None, 0) or result.get("invocations") not in (None, 0)):
            raise ValueError("missing permit journal cannot create a native entry")
        return
    RP.validate_agent(observed)
    if observed["binding_sha256"] != RP.digest(binding): raise ValueError("agent permit binding differs from frozen request")
    identity = _expected_identity(result.get("identity"))
    if identity is None or RP.digest(dict(identity)) != binding["identity_sha256"]:
        raise ValueError("actual permit worker identity differs from frozen process")
    if result.get("native_calls") is not None and result["native_calls"] != int(observed["native_entry"]):
        raise ValueError("permit journal and actual native entry counter disagree")
    if result.get("invocations") is not None and result["invocations"] != int(observed["native_entry"]):
        raise ValueError("permit journal and actual invocation counter disagree")
    if result.get("status") == "completed" and (observed["closed"] is not True or
            observed["state"] != "consumed" or observed["error"] is not None or not observed["native_entry"]):
        raise ValueError("completed native call lacks actual closed one-use permit consumption")


def _frida_lifecycle(result):
    """Validate current-worker cleanup without relabeling historical receipts.

    A closed manager is a cooperative API result, not an observed helper exit.
    Direct _worker_result users may still inspect historical worker evidence;
    every new collect transaction requires this additional disposition.
    """
    lifecycle = result.get("frida_lifecycle")
    if (not isinstance(lifecycle, dict) or set(lifecycle) != {
            "format", "manager_state", "attachment_attempted", "helper_exit_confirmed", "error"} or
            lifecycle.get("format") != FRIDA_LIFECYCLE or
            not isinstance(lifecycle.get("manager_state"), str) or
            lifecycle["manager_state"] not in {"not_acquired", "pending", "closed", "failed"} or
            type(lifecycle.get("attachment_attempted")) is not bool or
            lifecycle.get("helper_exit_confirmed") is not False or
            result.get("attached") and not lifecycle["attachment_attempted"] or
            lifecycle["manager_state"] == "not_acquired" and lifecycle["attachment_attempted"] or
            result.get("status") == "completed" and lifecycle["manager_state"] != "closed"):
        raise ValueError("runtime worker returned missing or malformed Frida lifecycle disposition")
    errors = result.get("cleanup_errors")
    if not isinstance(errors, list) or len(errors) > 4:
        raise ValueError("runtime worker lost its bounded cleanup error disposition")
    phases = set()
    for row in errors:
        if (not isinstance(row, dict) or set(row) != {"phase", "error"} or
                not isinstance(row.get("phase"), str) or row["phase"] not in {
                    "script_unload", "session_detach", "manager_close", "journal"} or row["phase"] in phases):
            raise ValueError("runtime worker returned a malformed cleanup error phase")
        phases.add(row["phase"])
        detail = row["error"]
        if (not isinstance(detail, dict) or set(detail) != {"type", "message"} or
                not _text(detail.get("type"), 256) or not _text(detail.get("message"), 4096, empty=True)):
            raise ValueError("runtime worker returned a malformed cleanup error")
    manager_errors = [row["error"] for row in errors if row["phase"] == "manager_close"]
    if (lifecycle["manager_state"] == "failed" and manager_errors != [lifecycle["error"]] or
            lifecycle["manager_state"] != "failed" and (lifecycle["error"] is not None or manager_errors)):
        raise ValueError("runtime worker lost its manager close error disposition")
    if errors and result.get("status") == "completed":
        raise ValueError("runtime worker claimed completion despite a cleanup error")
    return lifecycle


def _artifacts(folder):
    result = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and not path.is_symlink():
            size = path.stat().st_size
            result.append({"file": path.name, "bytes": size,
                           "sha256": R._file_hash(path) if size <= R.MAX_JSON_BYTES else None,
                           "oversized": size > R.MAX_JSON_BYTES})
    return result


def _phase_limits(value):
    _keys(value, {"setup_seconds", "before_seconds", "after_seconds", "margin_seconds"})
    result = copy.deepcopy(value)
    for key, number in result.items():
        maximum = 5 if key == "margin_seconds" else 10
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 < number <= maximum:
            raise ValueError("phase limits require positive finite seconds within the fixed phase bounds")
    return result


class _PhaseTrace:
    """Bounded prefix of one owned worker's append-only trace, not a task oracle."""

    def __init__(self, path):
        self.path, self.identity, self.prefix = path, None, b""

    def read(self):
        before = self.path.lstat()
        key = (before.st_dev, before.st_ino, before.st_uid, before.st_mode, before.st_nlink)
        maximum = 128 * (8192 + 1)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1 or
                before.st_mode & 0o077 or before.st_size > maximum or
                self.identity is not None and self.identity != key):
            raise ValueError("runtime phase trace is not the original bounded private owned spool")
        with os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != key[:2]:
                raise ValueError("runtime phase trace changed while opening")
            raw = stream.read(maximum + 1)
        after = self.path.lstat()
        if (len(raw) > maximum or not raw.startswith(self.prefix) or after.st_size < len(raw) or
                (after.st_dev, after.st_ino, after.st_uid, after.st_mode, after.st_nlink) != key):
            raise ValueError("runtime phase trace was replaced, rewritten or truncated")
        self.identity, self.prefix = key, raw
        def pairs(items):
            result = {}
            for name, value in items:
                if name in result: raise ValueError("duplicate runtime phase trace key")
                result[name] = value
            return result
        def constant(_):
            raise ValueError("nonfinite runtime phase trace value")
        events = []
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"): break
            if len(line) > 8193: raise ValueError("runtime phase trace row exceeds its byte bound")
            event = json.loads(line, object_pairs_hook=pairs, parse_constant=constant)
            if type(event) is not dict: raise ValueError("runtime phase trace requires object rows")
            events.append(event)
        if len(events) > 128: raise ValueError("runtime phase trace exceeds its row bound")
        return events


class _PhasedWait:
    """Trusted host hook; never constructed from a mapping-agent callable name.

    Failures in setup/observation do not cancel or retry the armed native request.
    Only the already owned worker's deadline can terminate that worker group.
    """

    def __init__(self, expected, hook, limits, folder, *, observer_capability, observer):
        self.expected, self.hook, self.limits = dict(expected), hook, copy.deepcopy(limits)
        # Selected through the closed trusted registry before any phase IO.
        # A returned settlement's schema never chooses its validating module.
        self.observer_capability, self.observer = observer_capability, observer
        self.owner_bound = observer_capability != _STRICT_PHASE_OBSERVER
        self.folder, self.config, self.trace = folder, None, None
        self.secrets, self.handled = [], False
        self.selector = None
        self.data = {"format": PHASE_CONTEXT2 if self.owner_bound else PHASE_CONTEXT, "evidence_only": True, "proof_created": False,
                     "limits": copy.deepcopy(limits), "expected_identity": dict(expected),
                     "request_sha256": None, "target": None, "adapter": None, "native_resources": None,
                     "worker_anchor_monotonic": None, "earliest_terminal_monotonic": None,
                     "pre_terminal_deadline_monotonic": None, "ready": None,
                     "phases": {"setup": None, "before": None, "after": None},
                     "errors": [], "owned_worker": None, "native_receipt": None}
        if self.owner_bound:
            self.data["observer_capability"] = observer_capability

    def save(self):
        if self.folder is not None:
            R.write_json(self.folder / "run-context.json", _redact(self.data, self.secrets))

    def fail(self, phase, error):
        self.data["errors"].append({"phase": phase, "error": _error(error, self.secrets)})
        self.save()

    def prepare(self, config, folder, secrets):
        self.config, self.secrets = copy.deepcopy(config), list(secrets)
        # Precreate the ordinary append spool with private mode; the unchanged
        # worker still appends its genuine send events to this same file.
        fd = os.open(config["trace"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self.trace = _PhaseTrace(Path(config["trace"]))
        self.spec = _lifecycle_paths(config["request"])[0]["root"]["lifecycle"]
        self.data.update(request_sha256=R.digest(config["request"]), target=copy.deepcopy(config["target"]),
                         adapter=copy.deepcopy(config["adapter"]), native_resources=copy.deepcopy(config["native_resources"]))
        self.save()

    def current(self):
        if not NP.same_data(process_identity(Path(self.config["bundle"]), self.config["target"]["bundle_id"]), self.expected):
            raise ValueError("runtime phase current identity differs from the frozen process; no replacement adoption")

    def pre_terminal(self, proc, deadline):
        events = self.trace.read()
        if any(row.get("event") in {"native-invoke-start", "native-invoke-returned", "operation-returned"} for row in events):
            raise ValueError("native terminal marker preceded completion of the baseline phase")
        self.current()
        if proc.poll() is not None or time.monotonic() >= min(deadline, self.data["pre_terminal_deadline_monotonic"]):
            raise TimeoutError("setup/baseline exceeded the conservative earliest-terminal bound")

    def validate_setup_return(self, returned):
        # Legacy/33 intentionally keeps the original opaque trusted return.
        pass

    def call(self, phase, deadline, proc=None):
        row = self.data["phases"][phase] = {"status": "incomplete", "started_monotonic": time.monotonic(),
            "deadline_monotonic": deadline, "returned_monotonic": None, "finished_monotonic": None,
            "return": None, "error": None, "pre_terminal_gate_checked": False}
        self.save()
        try:
            if proc is not None: self.pre_terminal(proc, deadline)
            else: self.current()
            if time.monotonic() >= deadline: raise TimeoutError("runtime host phase has no remaining time")
            context = {"phase": phase, "target": self.config["target"], "expected_identity": self.expected,
                "request_sha256": self.data["request_sha256"], "worker_pid": self.data["owned_worker"]["pid"],
                "deadline_monotonic": deadline, "earliest_terminal_monotonic": self.data["earliest_terminal_monotonic"],
                "ready": self.data["ready"]}
            if self.owner_bound or getattr(self, "require_ack", False):
                context["observer_capability"] = self.observer_capability
            returned = self.hook(phase, copy.deepcopy(context))
            row["returned_monotonic"] = time.monotonic()
            # Retain raw JSON BEFORE semantic validation. A valid failure
            # settlement remains failure evidence, never a usable baseline.
            raw = json.dumps(returned, ensure_ascii=False, allow_nan=False).encode()
            maximum = 64 * 1024 if phase == "setup" else 129 * 1024**2
            if len(raw) > maximum: raise ValueError("runtime host phase return exceeds its byte budget")
            file = self.folder / ("setup-return.json" if phase == "setup" else phase + "-settlement.json")
            R.write_json(file, _redact(returned, self.secrets))
            row["return"] = {"file": str(file), "sha256": R._file_hash(file), "bytes": file.stat().st_size}
            self.save()
            if phase == "setup":
                self.validate_setup_return(returned)
            else:
                settlement = self.observer.validate_settlement(returned)
                if (settlement["complete"] is not True or not NP.same_data(settlement["target"], self.config["target"]) or
                        not NP.same_data(settlement["expected_identity"], self.expected) or
                        not row["started_monotonic"] <= settlement["started_monotonic"] <=
                            settlement["finished_monotonic"] <= row["returned_monotonic"] or
                        settlement["deadline_monotonic"] > deadline or
                        self.selector is not None and not NP.same_data(settlement["selector"], self.selector)):
                    raise ValueError("runtime phase requires a fresh complete settlement for this exact target/process/selector/deadline")
                if self.selector is None: self.selector = copy.deepcopy(settlement["selector"])
            if proc is not None:
                self.pre_terminal(proc, deadline)
                row["pre_terminal_gate_checked"] = True
            else:
                self.current()
                if time.monotonic() >= deadline: raise TimeoutError("runtime after phase returned past its deadline")
            row["status"] = "returned"
        except Exception as error:
            row["error"] = _error(error, self.secrets)
        finally:
            row["finished_monotonic"] = time.monotonic()
            if row["status"] == "returned" and row["finished_monotonic"] >= deadline:
                row["status"] = "incomplete"
                row["error"] = _error(TimeoutError("runtime phase completion exceeded its deadline"), self.secrets)
            self.save()
        return row["status"] == "returned"

    def tick(self, proc):
        if self.handled: return
        try:
            events = self.trace.read()
            ready = [row for row in events if row.get("event") == "lifecycle-observer-ready"]
            if not ready:
                if time.monotonic() >= self.data["pre_terminal_deadline_monotonic"]:
                    raise TimeoutError("native observer was not ready within the conservative baseline window")
                return
            if (len(ready) != 1 or set(ready[0]) != {"event", "observe_ms", "hooks"} or
                    type(ready[0]["observe_ms"]) is not int or ready[0]["observe_ms"] != self.spec["observe_ms"] or
                    type(ready[0]["hooks"]) is not int or ready[0]["hooks"] != 1 + len(self.spec["destructors"])):
                raise ValueError("native observer readiness differs from the sole validated observation spec")
            self.data["ready"] = copy.deepcopy(ready[0])
            self.handled = True
            bound = self.data["pre_terminal_deadline_monotonic"]
            if self.call("setup", min(bound, time.monotonic() + self.limits["setup_seconds"]), proc):
                self.call("before", min(bound, time.monotonic() + self.limits["before_seconds"]), proc)
        except Exception as error:
            self.handled = True
            self.fail("ready", error)

    def spawn(self, argv, stdout, stderr, cwd):
        return subprocess.Popen(argv, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                cwd=cwd, start_new_session=True)

    def run(self, argv, output, error, *, timeout, cwd):
        started = time.monotonic()  # BEFORE Popen: a conservative lower bound.
        self.data["worker_anchor_monotonic"] = started
        self.data["earliest_terminal_monotonic"] = started + self.spec["observe_ms"] / 1000
        self.data["pre_terminal_deadline_monotonic"] = self.data["earliest_terminal_monotonic"] - self.limits["margin_seconds"]
        deadline, timed_out, code = started + timeout, False, None
        with output.open("xb") as stdout, error.open("xb") as stderr:
            proc = self.spawn(argv, stdout, stderr, cwd)
            owned = self.data["owned_worker"] = {"pid": proc.pid, "returncode": None, "reaped": False,
                                                "term_attempted": False, "kill_attempted": False, "errors": []}
            def stop_owned():
                for name, signum in (("term", signal.SIGTERM), ("kill", signal.SIGKILL)):
                    owned[name + "_attempted"] = True
                    try: os.killpg(proc.pid, signum)
                    except ProcessLookupError: pass
                    except Exception as failure: owned["errors"].append(_error(failure, self.secrets))
                    try:
                        proc.wait(timeout=5)
                        return
                    except subprocess.TimeoutExpired: pass
                    except Exception as failure: owned["errors"].append(_error(failure, self.secrets))
            try:
                self.save()
                while proc.poll() is None:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        stop_owned()
                        break
                    self.tick(proc)
                    if proc.poll() is None:
                        time.sleep(max(0, min(.05, deadline - time.monotonic())))
                code = proc.wait(timeout=0)
                owned["reaped"], owned["returncode"] = True, code
            except BaseException as failure:
                # Preserve hook/interruption failure, then audit the actual owned
                # child. Killing a worker does not prove its native work stopped.
                timed_out = True
                # Do not let a receipt-write error prevent owned-child cleanup.
                self.data["errors"].append({"phase": "worker_wait", "error": _error(failure, self.secrets)})
                stop_owned()
                try:
                    code = proc.wait(timeout=0)
                    owned["reaped"], owned["returncode"] = True, code
                except Exception as cleanup:
                    owned["errors"].append(_error(cleanup, self.secrets))
            finally:
                self.save()
        return {"returncode": code, "timed_out": timed_out, "elapsed_s": round(time.monotonic() - started, 3),
                "output": output.name, "sha256": R._file_hash(output), "stderr": error.name}

    def finish(self, runtime):
        if self.folder is None:
            return None
        self.data["native_receipt"] = {"file": runtime["receipt"], "sha256": R._file_hash(Path(runtime["receipt"])),
                                       "status": runtime["status"], "resource_review_required": runtime["resource_review_required"],
                                       "native_calls": None if runtime["result"] is None else runtime["result"]["native_calls"],
                                       "invocations": None if runtime["result"] is None else runtime["result"]["invocations"]}
        pre = all(self.data["phases"][phase] is not None and self.data["phases"][phase]["status"] == "returned"
                  for phase in ("setup", "before"))
        owned = self.data["owned_worker"]
        if (pre and not self.data["errors"] and runtime["status"] in {"observed", "executed"} and
                runtime["resource_review_required"] is False and owned is not None and owned["reaped"] and
                owned["returncode"] == 0 and not owned["errors"]):
            self.call("after", time.monotonic() + self.limits["after_seconds"])
            # S reads current OS/AX context but does not rehash app/resources.
            # Recheck that independent closure after the external observation.
            self.current()
            if (R.fingerprint(Path(self.config["bundle"])) != self.config["target"] or
                    not NP.same_data(adapter_identity(backend=self.config["backend"]), self.config["adapter"]) or
                    not NP.same_data(request_resources(self.config["request"]), self.config["native_resources"]) or
                    not NP.same_data(request_compiler_provenance(self.config["request"]), self.config["compiler_provenance"])):
                raise ValueError("target/runtime resources changed during external after observation")
        self.save()
        return copy.deepcopy(self.data)


def _permit_write(path, value, *, fresh=False):
    """Owned private write-ahead journal; a failed fsync never grants a permit."""
    path = Path(path)
    raw = (RP.canonical(value) + "\n").encode("utf-8")
    if fresh:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    else:
        fd, temporary = tempfile.mkstemp(prefix=".permit-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)
    return _permit_pin(path)


def _permit_pin(path):
    path = Path(path)
    info = path.lstat()
    if (str(path.resolve(strict=True)) != str(path) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError("permit journal is not a canonical private owned file")
    return RP.pin({"file": str(path), "sha256": R._file_hash(path), "bytes": info.st_size})


def _permit_read(expected):
    expected = RP.pin(expected)
    if not RP.same(_permit_pin(expected["file"]), expected):
        raise ValueError("frozen permit phase bytes changed")
    value = R.load_json(expected["file"])
    if not RP.same(_permit_pin(expected["file"]), expected):
        raise ValueError("frozen permit phase changed while reading")
    return value


def _permit_event(journal, kind):
    if len(journal["events"]) >= RP.MAX_EVENTS:
        raise ValueError("permit event ledger exhausted")
    journal["events"].append({"seq": len(journal["events"]) + 1, "kind": kind, "at": time.monotonic()})


def _permit_error(error, secrets):
    row = _error(error, secrets)
    return {"type": row["type"][:128] or "Error", "message": row["message"][:2048]}


class _PermitChannel:
    """One bounded inherited stream. No listener, reconnect or address inputs."""

    def __init__(self, endpoint):
        if endpoint.family != socket.AF_UNIX or endpoint.type & 0xf != socket.SOCK_STREAM:
            raise ValueError("anonymous local stream required for the owned permit channel")
        # socketpair ends have no named filesystem/abstract endpoint.
        if endpoint.getsockname() not in ("", b"") or endpoint.getpeername() not in ("", b""):
            raise ValueError("permit channel must be an anonymous socketpair")
        endpoint.setblocking(False)
        self.socket, self.pending, self.received, self.sent, self.eof = endpoint, b"", 0, 0, False

    def receive(self):
        rows = []
        while not self.eof:
            try: raw = self.socket.recv(RP.MAX_FRAME + 4)
            except BlockingIOError: break
            if not raw:
                self.eof = True
                if self.pending: raise ValueError("permit channel ended in a partial frame")
                break
            self.pending += raw
            decoded, self.pending = RP.decode_frames(self.pending)
            self.received += len(decoded)
            if self.received > RP.MAX_FRAMES: raise ValueError("permit channel frame count exhausted")
            rows.extend(decoded)
        return rows

    def send(self, frame, deadline):
        if self.eof or self.sent >= RP.MAX_FRAMES: raise ValueError("permit channel cannot send another frame")
        raw = RP.encode_frame(frame)
        cursor = 0
        while cursor < len(raw):
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError("permit frame issuance crossed its deadline")
            _, writable, _ = select.select([], [self.socket], [], min(.05, remaining))
            if not writable: continue
            try: count = self.socket.send(raw[cursor:])
            except BlockingIOError: continue
            if count <= 0: raise EOFError("owned permit channel closed during send")
            cursor += count
        self.sent += 1

    def close(self):
        self.socket.close()


class _AcknowledgedWait(_PhasedWait):
    """True pre-actuation protocol; only this trusted opt-in creates a channel."""
    require_ack = True

    def __init__(self, *args, observer_selector, runtime_timeout, **kwargs):
        super().__init__(*args, **kwargs)
        self.selector = copy.deepcopy(observer_selector)
        self.timeout = runtime_timeout
        self.channel = self.proc = self.permit = self.binding = None
        self.config_file = None
        self.denial = None
        self.data.update(format=PHASE_CONTEXT3, observer_capability=self.observer_capability,
                         pre_actuation_ack=None, runtime_timeout_seconds=runtime_timeout,
                         permit_deadline_monotonic=None)

    def prepare(self, config, folder, secrets):
        # Check the actual fixed compiled capability before trace creation/Popen.
        import secrets as permit_secrets
        spec = _lifecycle_paths(config["request"])[0]["root"]["lifecycle"]
        self.binding = RP.make_binding(config, run_nonce=permit_secrets.token_hex(32),
            observer_capability=self.observer_capability, observer_selector=self.selector, observer_spec=spec)
        self.config_file = folder / "worker-request.json"
        super().prepare(config, folder, secrets)
        self.permit = {"format": RP.PARENT, "binding": copy.deepcopy(self.binding), "worker_pid": None,
            "worker_nonce": None, "selection": None, "decision": None, "sent": False, "events": [], "error": None}
        _permit_event(self.permit, "prepared")
        self.permit_save()

    def permit_save(self):
        RP.validate_parent(self.permit)
        self.data["pre_actuation_ack"] = _permit_write(self.folder / "pre-actuation-parent.json", self.permit)
        self.save()

    def validate_setup_return(self, returned):
        RP.validate_setup(returned)

    def call(self, phase, deadline, proc=None):
        returned = super().call(phase, deadline, proc)
        if phase in {"setup", "before"}:
            if returned: _permit_event(self.permit, phase + "_complete")
            else: self.denial = "trusted " + phase + " phase did not complete successfully"
            self.permit_save()
        return returned

    def spawn(self, argv, stdout, stderr, cwd):
        self.data["permit_deadline_monotonic"] = self.data["worker_anchor_monotonic"] + self.timeout - 3
        parent_end, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.channel = _PermitChannel(parent_end)
            self.config["pre_actuation_ack"] = {"format": RP.PROTOCOL, "binding": copy.deepcopy(self.binding),
                "observer_selector": copy.deepcopy(self.selector), "observer_spec": copy.deepcopy(self.spec),
                "control_fd": child_end.fileno(), "parent_pid": os.getpid()}
            RP.validate_config(self.config["pre_actuation_ack"])
            R.write_json(self.config_file, self.config)
            self.proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                cwd=cwd, start_new_session=True, pass_fds=(child_end.fileno(),))
        except BaseException:
            parent_end.close()
            raise
        finally:
            child_end.close()
        self.permit["worker_pid"] = self.proc.pid
        _permit_event(self.permit, "worker_started")
        # run() has now acquired an owned child; receipt failure must unwind it.
        return self.proc

    def _incoming(self):
        for row in self.channel.receive():
            if (not RP.same(row["binding"], self.binding) or row["worker_pid"] != self.proc.pid or
                    row["kind"] not in {"ready", "need"}):
                raise ValueError("parent received an unrelated or wrong-direction permit frame")
            if row["kind"] == "ready":
                if self.permit["worker_nonce"] is not None: raise ValueError("duplicate permit readiness")
                self.permit["worker_nonce"] = row["worker_nonce"]
                _permit_event(self.permit, "ready_received")
            else:
                if self.permit["worker_nonce"] != row["worker_nonce"] or self.permit["selection"] is not None:
                    raise ValueError("permit NEED preceded readiness or repeated a selection")
                self.permit["selection"] = copy.deepcopy(row["selection"])
                _permit_event(self.permit, "need_received")
            self.permit_save()
        if self.channel.eof and not self.permit["sent"]:
            raise EOFError("owned worker permit channel closed before a complete decision send")
        # W may finish its sole RPC/control thread before stdout/SDK cleanup and
        # natural process exit. After a full send, EOF does not revoke the commit
        # or replace the independent W/J receipts and owned-process exit audit.

    def _checkpoint(self, proc):
        if proc.poll() is not None: raise ValueError("permit worker exited before positive commit")
        setup, before = self.data["phases"]["setup"], self.data["phases"]["before"]
        RP.validate_setup(_permit_read(setup["return"]))
        settlement = self.observer.validate_settlement(_permit_read(before["return"]))
        if (settlement["complete"] is not True or not RP.same(settlement["selector"], self.selector) or
                not RP.same(settlement["target"], self.config["target"]) or
                not RP.same(settlement["expected_identity"], self.expected)):
            raise ValueError("positive commit lost its complete frozen BEFORE observation")
        current = {"identity": process_identity(Path(self.config["bundle"]), self.config["target"]["bundle_id"]),
            "target": R.fingerprint(Path(self.config["bundle"])),
            "adapter": adapter_identity(backend=self.config["backend"]),
            "native_resources": request_resources(self.config["request"]),
            "compiler_provenance": request_compiler_provenance(self.config["request"])}
        checkpoint = {"format": RP.CHECKPOINT, "binding": copy.deepcopy(self.binding),
            "worker_pid": proc.pid, "worker_nonce": self.permit["worker_nonce"],
            "selection": copy.deepcopy(self.permit["selection"]), "setup": copy.deepcopy(setup["return"]),
            "before": copy.deepcopy(before["return"]), "setup_finished_monotonic": setup["finished_monotonic"],
            "before_finished_monotonic": before["finished_monotonic"], "checked_monotonic": time.monotonic(),
            "deadline_monotonic": self.data["permit_deadline_monotonic"], "current": current}
        RP.validate_checkpoint(checkpoint)
        return _permit_write(self.folder / "pre-actuation-checkpoint.json", checkpoint, fresh=True)

    def _decide(self, proc):
        if self.permit["decision"] is not None or self.permit["worker_nonce"] is None: return
        pre = all(self.data["phases"][name] is not None and self.data["phases"][name]["status"] == "returned"
                  for name in ("setup", "before"))
        if self.denial is None and (not pre or self.permit["selection"] is None): return
        frame = {"format": RP.FRAME, "kind": "decision", "binding": copy.deepcopy(self.binding),
            "worker_pid": proc.pid, "worker_nonce": self.permit["worker_nonce"],
            "selection": copy.deepcopy(self.permit["selection"]), "decision": "deny", "ordinal": 1,
            "setup": None, "before": None, "checkpoint": None,
            "reason": self.denial or "positive pre-actuation checks did not complete"}
        if self.denial is None:
            try:
                checkpoint_pin = self._checkpoint(proc)
                frame.update(decision="allow", setup=copy.deepcopy(self.data["phases"]["setup"]["return"]),
                    before=copy.deepcopy(self.data["phases"]["before"]["return"]), checkpoint=checkpoint_pin, reason=None)
            except Exception as failure:
                self.denial = "positive pre-actuation checkpoint failed"
                frame["reason"] = self.denial
                self.permit["error"] = _permit_error(failure, self.secrets)
                _permit_event(self.permit, "error")
        self.permit["decision"] = RP.validate_frame(frame)
        _permit_event(self.permit, "decision_intent")
        self.permit_save()  # durable intent BEFORE the first channel byte
        self.channel.send(frame, self.data["permit_deadline_monotonic"])
        self.permit["sent"] = True
        _permit_event(self.permit, "decision_sent")
        self.permit_save()
        if time.monotonic() >= self.data["permit_deadline_monotonic"]:
            raise TimeoutError("permit send returned after its issuance deadline; cancellation is not inferred")

    def tick(self, proc):
        if self.permit["error"] is not None: return
        try:
            self._incoming()
            # Preserve the real trace check; a channel message cannot fake J readiness.
            if self.permit["worker_nonce"] is not None: super().tick(proc)
            if self.handled and not all(self.data["phases"][name] is not None and
                    self.data["phases"][name]["status"] == "returned" for name in ("setup", "before")):
                self.denial = self.denial or "trusted phases did not complete"
            if time.monotonic() >= self.data["permit_deadline_monotonic"]:
                if not self.permit["sent"]: raise TimeoutError("pre-actuation permit deadline expired")
            self._decide(proc)
        except Exception as failure:
            self.denial = "private pre-actuation protocol failed"
            self.permit["error"] = _permit_error(failure, self.secrets)
            _permit_event(self.permit, "error")
            self.permit_save()
            # Closing before any positive commit prevents an unissued permit.
            # After a partial/positive commit, cancellation is NOT claimed.
            self.channel.close()

    def _audit_spawn(self):
        """Cover the interval after Popen returns but before base ownership exists."""
        if self.proc is None: return
        owned = self.data["owned_worker"]
        if owned is None:
            owned = self.data["owned_worker"] = {"pid": self.proc.pid, "returncode": None, "reaped": False,
                "term_attempted": False, "kill_attempted": False, "errors": []}
        if owned["reaped"]: return
        for name, signum in (("term", signal.SIGTERM), ("kill", signal.SIGKILL)):
            try:
                if self.proc.poll() is not None:
                    owned["returncode"], owned["reaped"] = self.proc.wait(timeout=0), True
                    break
                if owned[name + "_attempted"]: continue
                owned[name + "_attempted"] = True
                try: os.killpg(self.proc.pid, signum)
                except ProcessLookupError: pass
                owned["returncode"], owned["reaped"] = self.proc.wait(timeout=5), True
                break
            except subprocess.TimeoutExpired: pass
            except Exception as failure: owned["errors"].append(_error(failure, self.secrets))

    def run(self, argv, output, error, *, timeout, cwd):
        try:
            return super().run(argv, output, error, timeout=timeout, cwd=cwd)
        except BaseException as failure:
            if self.proc is None: raise
            self.data["errors"].append({"phase": "worker_wait", "error": _error(failure, self.secrets)})
            self._audit_spawn()
            return {"returncode": self.data["owned_worker"]["returncode"], "timed_out": True,
                "elapsed_s": round(time.monotonic() - self.data["worker_anchor_monotonic"], 3),
                "output": output.name, "sha256": R._file_hash(output), "stderr": error.name}
        finally:
            self._audit_spawn()
            if self.channel is not None: self.channel.close()
            self.save()

    def finish(self, runtime):
        try:
            super().finish(runtime)
        finally:
            if self.permit is not None:
                _permit_event(self.permit, "closed")
                self.permit_save()
        return copy.deepcopy(self.data)


class _PermitWorker:
    """One owned forwarding thread; the worker main thread owns the sole RPC."""

    def __init__(self, config, path, secrets):
        import secrets as permit_secrets
        declaration = RP.validate_config(config["pre_actuation_ack"])
        self.config, self.declaration, self.secrets = copy.deepcopy(config), declaration, list(secrets)
        self.binding = declaration["binding"]
        specs = _lifecycle_paths(config["request"])
        if (len(config["request"]["operations"]) != 1 or len(specs) != 1 or
                config["request"]["operations"][0]["op"] not in {"native_invoke", Q.OPERATION} or
                minimum_request_native_calls(config["request"]) != 1 or
                not RP.same(specs[0]["root"]["lifecycle"], declaration["observer_spec"])):
            raise ValueError("private permit requires the one validated observed native/Qt call")
        actual = RP.make_binding(config, run_nonce=self.binding["run_nonce"],
            observer_capability=self.binding["observer_capability"],
            observer_selector=declaration["observer_selector"], observer_spec=declaration["observer_spec"])
        if not RP.same(actual, self.binding) or os.getppid() != declaration["parent_pid"]:
            raise ValueError("owned permit worker configuration or actual parent changed")
        self.path = Path(path)
        self.queue, self.stop_requested, self.started = queue.Queue(maxsize=16), threading.Event(), threading.Event()
        self.thread = self.script = None
        self.notification_failure = None
        self.channel = _PermitChannel(socket.socket(fileno=declaration["control_fd"]))
        self.deadline = time.monotonic() + max(0, (config["deadline_ms"] - time.time() * 1000) / 1000)
        self.data = {"format": RP.WORKER, "binding": copy.deepcopy(self.binding), "worker_pid": os.getpid(),
            "worker_nonce": permit_secrets.token_hex(32), "selection": None, "decision": None,
            "post_started": False, "post_returned": False, "parent_eof": False,
            "thread_started": False, "thread_stopped": False, "events": [], "errors": []}
        self.ready = False
        self.save()

    def save(self):
        RP.validate_worker(self.data)
        _permit_write(self.path, self.data)

    def error(self, failure):
        if len(self.data["errors"]) < RP.MAX_ERRORS:
            self.data["errors"].append(_permit_error(failure, self.secrets))
        _permit_event(self.data, "error")

    def notify(self, event):
        if type(event) is dict and event.get("event") in {"lifecycle-observer-ready", "phase-permit-needed"}:
            # The SDK callback never blocks, forwards a decision or invokes RPC.
            try: self.queue.put_nowait(copy.deepcopy(event))
            except queue.Full:
                self.notification_failure = ValueError("bounded permit SDK notification queue exhausted")

    def _frame(self, kind, **extra):
        return {"format": RP.FRAME, "kind": kind, "binding": copy.deepcopy(self.binding),
            "worker_pid": self.data["worker_pid"], "worker_nonce": self.data["worker_nonce"], **extra}

    def _agent_event(self, event):
        if event["event"] == "lifecycle-observer-ready":
            spec = self.declaration["observer_spec"]
            if (self.ready or set(event) != {"event", "observe_ms", "hooks"} or
                    type(event["observe_ms"]) is not int or event["observe_ms"] != spec["observe_ms"] or
                    type(event["hooks"]) is not int or event["hooks"] != 1 + len(spec["destructors"])):
                raise ValueError("private permit readiness differs from the one actual lifecycle observer")
            self.channel.send(self._frame("ready"), self.deadline)
            self.ready = True
            _permit_event(self.data, "ready_sent")
        else:
            if (not self.ready or self.data["selection"] is not None or
                    set(event) != {"event", "binding_sha256", "worker_nonce", "selection"} or
                    event["binding_sha256"] != RP.digest(self.binding) or
                    event["worker_nonce"] != self.data["worker_nonce"]):
                raise ValueError("private permit NEED is repeated, premature or belongs to another run")
            self.data["selection"] = RP.selection(event["selection"])
            self.channel.send(self._frame("need", selection=copy.deepcopy(self.data["selection"])), self.deadline)
            _permit_event(self.data, "need_sent")
        self.save()

    def _recheck(self):
        if os.getppid() != self.declaration["parent_pid"] or time.time() * 1000 >= self.config["deadline_ms"]:
            raise ValueError("permit parent disappeared or native wall deadline expired before forwarding")
        current = copy.deepcopy(self.config)
        current.update(identity=process_identity(Path(current["bundle"]), current["target"]["bundle_id"]),
            target=R.fingerprint(Path(current["bundle"])), adapter=adapter_identity(backend=current["backend"]),
            native_resources=request_resources(current["request"]),
            compiler_provenance=request_compiler_provenance(current["request"]))
        binding = RP.make_binding(current, run_nonce=self.binding["run_nonce"],
            observer_capability=self.binding["observer_capability"],
            observer_selector=self.declaration["observer_selector"], observer_spec=self.declaration["observer_spec"])
        if not RP.same(binding, self.binding): raise ValueError("current permit target/source/resources changed before forwarding")

    def _decision(self, frame):
        if (frame["kind"] != "decision" or self.data["decision"] is not None or not self.ready or
                not RP.same(frame["binding"], self.binding) or frame["worker_pid"] != self.data["worker_pid"] or
                frame["worker_nonce"] != self.data["worker_nonce"] or
                not (RP.same(frame["selection"], self.data["selection"]) or
                     frame["decision"] == "deny" and frame["selection"] is None)):
            raise ValueError("worker refused an unrelated, replayed or out-of-order parent decision")
        self.data["decision"] = copy.deepcopy(frame)
        _permit_event(self.data, "decision_received")
        if self.stop_requested.is_set(): raise InterruptedError("permit forwarding stopped before independent checks")
        if frame["decision"] == "allow":
            if self.data["selection"] is None: raise ValueError("positive permit preceded actual J selection")
            self._recheck()
        self.data["post_started"] = True
        _permit_event(self.data, "post_started")
        self.save()  # Actual attempted-forward disposition before script.post.
        if self.stop_requested.is_set(): raise InterruptedError("permit forwarding stopped before script.post; no post issued")
        if time.monotonic() >= self.deadline: raise TimeoutError("private permit expired before script.post")
        self.script.post({"type": "anyuse.rea-phase-permit", "payload": copy.deepcopy(frame)})
        self.data["post_returned"] = True
        _permit_event(self.data, "post_returned")
        self.save()

    def _abort(self, reason):
        # This is never represented as a parent-issued DENY or proof of cancellation.
        self.script.post({"type": "anyuse.rea-phase-permit-abort", "payload": {
            "binding_sha256": RP.digest(self.binding), "worker_nonce": self.data["worker_nonce"], "reason": reason[:256]}})
        _permit_event(self.data, "abort_posted")

    def _run(self):
        self.data["thread_started"] = True
        _permit_event(self.data, "started")
        self.started.set()
        try:
            self.save()
            while not self.stop_requested.is_set():
                if self.notification_failure is not None: raise self.notification_failure
                try:
                    while True: self._agent_event(self.queue.get_nowait())
                except queue.Empty: pass
                for frame in self.channel.receive(): self._decision(frame)
                if self.channel.eof:
                    self.data["parent_eof"] = True
                    _permit_event(self.data, "parent_eof")
                    self._abort("owned parent channel reached EOF")
                    break
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("private permit forwarding exceeded native wall deadline")
                select.select([self.channel.socket], [], [], .025)
        except Exception as failure:
            self.error(failure)
            if not self.stop_requested.is_set():
                try: self._abort("owned permit forwarding failed")
                except Exception as cleanup: self.error(cleanup)
        finally:
            self.data["thread_stopped"] = True
            _permit_event(self.data, "stopped")
            self.save()

    def start(self, script):
        self.script = script
        self.thread = threading.Thread(target=self._run, name="anyuse-owned-permit", daemon=True)
        self.thread.start()
        if not self.started.wait(timeout=1): raise TimeoutError("owned permit control thread did not enter")

    def payload(self):
        return {"format": RP.PROTOCOL, "binding": copy.deepcopy(self.binding), "binding_sha256": RP.digest(self.binding),
            "worker_pid": self.data["worker_pid"], "worker_nonce": self.data["worker_nonce"],
            "observer_selector": copy.deepcopy(self.declaration["observer_selector"]),
            "observer_spec": copy.deepcopy(self.declaration["observer_spec"]),
            "request_canonical_json": RP.canonical(self.config["request"]),
            "observer_canonical_json": RP.canonical(RP.observer_descriptor(self.config,
                observer_capability=self.binding["observer_capability"],
                observer_selector=self.declaration["observer_selector"], observer_spec=self.declaration["observer_spec"])),
            "permit_wait_ms": 5000}

    def stop(self):
        self.stop_requested.set()
        if self.thread is None:
            self.save()
            return
        if self.thread is not None:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                # No concurrent mutation/write of its ledger while still alive.
                raise TimeoutError("owned permit control thread has not exited; forwarding/native outcome remains uncertain")
        if not self.data["thread_stopped"] or self.data["errors"]:
            raise ValueError("owned permit control thread did not close cleanly")
        retained = RP.validate_worker(R.load_json(self.path))
        if not RP.same(retained, self.data):
            raise ValueError("owned control-thread final journal was not retained exactly")
        # Keep inherited socket FD open until this worker exits naturally. Closing
        # it before script/session/manager cleanup would falsely resemble parent EOF.


def collect_phased(bundle, out, request, *, expected_identity, phase_hook, limits, timeout=35, backend=None,
                   observer_capability=_STRICT_PHASE_OBSERVER, require_pre_actuation_ack=False, observer_selector=None):
    """Internal TRUSTED host coordinator; not a model/JSON callable surface.

    The fixed hook owns separately authorized ordinary setup and settlements
    from the selected fixed read-only observer. Strict focused S15 is the
    unchanged default; explicit owner-bound observation uses version2 records.
    No expected state is accepted. Failure drains/audits this sole
    worker; an already armed request can still dispatch. Native counts remain
    actual measurements. This wrapper never produces a successful task proof.
    """
    R.assert_guest()  # Before path IO, callbacks, backend acquisition or writes.
    if __package__:
        from . import rea_state_reader as SR
    else:
        import rea_state_reader as SR
    # Unknown capabilities fail before bundle IO, phase writes or worker setup.
    observer = SR.for_capability(observer_capability)
    if type(require_pre_actuation_ack) is not bool or not require_pre_actuation_ack and observer_selector is not None:
        raise ValueError("pre-actuation acknowledgement is a trusted exact boolean; a selector requires true opt-in")
    selector = observer.validate_selector(observer_selector) if require_pre_actuation_ack else None
    expected = _expected_identity(expected_identity)
    if expected is None or not callable(phase_hook):
        raise ValueError("phased runtime requires a full current identity and a fixed trusted host hook")
    value, bounds = validate_request(request), _phase_limits(limits)
    _require_native_path_backend(value)
    _integer(timeout, 1, 60, "runtime timeout must be 1–60 seconds")
    if require_pre_actuation_ack and timeout <= 3:
        raise ValueError("acknowledged runtime requires time beyond the fixed three-second cleanup reserve")
    _lifecycle_budget(value, timeout * 1000 - 100)
    if len(value["operations"]) != 1 or len(_lifecycle_paths(value)) != 1:
        raise ValueError("phased runtime requires one operation with one existing lifecycle-observed root")
    if require_pre_actuation_ack and value["operations"][0]["op"] not in {"native_invoke", Q.OPERATION}:
        raise ValueError("pre-actuation acknowledgement requires one native/Qt invocation")
    if require_pre_actuation_ack and minimum_request_native_calls(value) != 1:
        raise ValueError("one-use acknowledgement excludes nested target-call getters")
    if bounds["margin_seconds"] >= _lifecycle_paths(value)[0]["root"]["lifecycle"]["observe_ms"] / 1000:
        raise ValueError("phase margin leaves no conservative pre-terminal window")
    root, folder = Path(bundle).resolve(strict=True), Path(out).resolve()
    if folder.is_relative_to(root): raise ValueError("runtime phase evidence must be outside the target app bundle")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    phase_type = _AcknowledgedWait if require_pre_actuation_ack else _PhasedWait
    phase_options = {"observer_capability": observer_capability, "observer": observer}
    if require_pre_actuation_ack:
        phase_options.update(observer_selector=selector, runtime_timeout=timeout)
    phases = phase_type(expected, phase_hook, bounds, folder, **phase_options)
    # Keep mutable phase journals OUTSIDE the native receipt's artifact closure.
    # Its runtime.json/worker bytes and historical validation remain immutable.
    runtime = _collect(root, folder / "runtime", value, timeout=timeout, backend=backend,
                       expected_identity=dict(expected), _phases=phases)
    try:
        context = phases.finish(runtime)
    except Exception as failure:
        phases.fail("finish", failure)
        context = copy.deepcopy(phases.data) if phases.folder is not None else None
    complete = (context is not None and not context["errors"] and
                all(row is not None and row["status"] == "returned" for row in context["phases"].values()))
    if require_pre_actuation_ack:
        complete = (complete and phases.permit is not None and phases.permit["error"] is None and
                    phases.permit["sent"] is True and phases.permit["decision"]["decision"] == "allow")
    result = {"format": PHASED3 if require_pre_actuation_ack else PHASED2 if phases.owner_bound else PHASED,
              "evidence_only": True, "proof_created": False, "phases_complete": complete,
              "runtime": runtime, "run_context": context}
    if phases.owner_bound or require_pre_actuation_ack:
        result["observer_capability"] = phases.observer_capability
    if require_pre_actuation_ack:
        result["pre_actuation_ack"] = copy.deepcopy(phases.data["pre_actuation_ack"])
    R.write_json(folder / "phased.json", result)
    return result


def collect(bundle, out, request, *, timeout=15, backend=None, expected_identity=None) -> dict:
    """Run one fixed-asset transaction, preserving denial/timeout and partial output.

    The optional full NS pin is checked before worker preparation; the worker
    still rechecks before attachment and fixed-script loading. PID attachment
    is not atomic. Ordinary calls retain the existing bounded runner unchanged.
    """
    return _collect(bundle, out, request, timeout=timeout, backend=backend, expected_identity=expected_identity)


def _collect(bundle, out, request, *, timeout=15, backend=None, expected_identity=None, _phases=None) -> dict:
    """Run one fixed-asset transaction, preserving denial/timeout and partial output.

    An optional complete NS identity is an ephemeral process precondition,
    retained as receipt provenance and checked before preparing the worker.
    It does not make attachment by PID atomic; the worker still rechecks the
    captured identity before attachment and before loading the fixed script.
    """
    R.assert_guest()  # Must precede validation IO, writes, imports and attachment.
    expected = _expected_identity(expected_identity)
    value = validate_request(request)
    _require_native_path_backend(value)
    _integer(timeout, 1, 60, "runtime timeout must be 1–60 seconds")
    _lifecycle_budget(value, timeout * 1000 - 100)
    root, folder = Path(bundle).resolve(strict=True), Path(out).resolve()
    if folder.is_relative_to(root):
        raise ValueError("runtime evidence must be outside the target app bundle")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic()
    receipt = {"format": RUNTIME, "status": "incomplete", "evidence_only": True, "verified": False,
               "proof_created": False, "actuated": False, "native_calls": False, "model_calls": 0,
               "bundle": str(root), "target": None, "identity": None, "after_identity": None,
               "request": value, "request_sha256": R.digest(value), "adapter": None, "transport": None,
               "result": None, "worker": None, "error": None, "resource_review_required": False, "native_resources": {},
               "compiler_provenance": {},
               "observed_at": datetime.now(timezone.utc).isoformat(), "receipt": str(folder / "runtime.json")}
    if expected is not None:
        receipt["expected_identity"] = dict(expected)
    token = None
    secrets = []
    try:
        # Capture usable authentication material before any endpoint or target
        # denial so request text is redacted even on these early exits.
        transport, token = _transport(secrets=secrets, backend=backend)
        _transport_outside(transport, root, folder)
        target = receipt["target"] = R.fingerprint(root)
        identity = receipt["identity"] = process_identity(root, target["bundle_id"])
        if expected is not None and identity != expected:
            raise Unavailable("running app differs from the expected runtime process identity; no replacement process adoption")
        adapter = receipt["adapter"] = adapter_identity(backend=backend)
        if adapter["transport"] != transport:
            raise Unavailable("runtime transport configuration changed before the worker was prepared")
        receipt["transport"] = transport
        _helper_authorization(value, adapter.get("helper_policy"), root, target, folder)
        if token and _redact(value, token) != value:
            raise Unavailable("runtime request contains protected authentication material")
        resources = receipt["native_resources"] = request_resources(value)
        provenance = receipt["compiler_provenance"] = request_compiler_provenance(value)
        # A venv's executable is commonly a symlink. Resolving it for argv loses
        # pyvenv.cfg/site-packages and silently launches the underlying Python.
        interpreter = Path(os.environ.get("ANYUSE_REA_RUNTIME_PYTHON", sys.executable)).absolute()
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise Unavailable("configure an installed guest Python with Frida")
        receipt["python"] = {"file": str(interpreter), "resolved": str(interpreter.resolve(strict=True))}
        config = {"format": WORKER, "bundle": str(root), "target": target, "identity": identity,
                  "request": value, "adapter": adapter, "transport": transport, "native_resources": resources,
                  "compiler_provenance": provenance, "backend": backend,
                  "deadline_ms": int(time.time() * 1000 + timeout * 1000 - 100),
                  "trace": str(folder / "trace.jsonl")}
        R.write_json(folder / "worker-request.json", config)
        if _phases is not None:
            _phases.prepare(config, folder, secrets)
        receipt.update(actuated=None, native_calls=None, resource_review_required=True)
        runner = R._bounded_run if _phases is None else _phases.run
        run = receipt["worker"] = runner([str(interpreter), str(Path(__file__).resolve()), "--worker",
                                          str(folder / "worker-request.json")],
                                         folder / "worker-output.json", folder / "worker.stderr",
                                         timeout=timeout, cwd=folder)
        _redact_artifacts(folder, secrets)
        if run["timed_out"]:
            receipt.update(actuated=None, native_calls=None, resource_review_required=True)
            raise TimeoutError("runtime worker timed out; queued/running native work and detachment require resource review")
        file = folder / "worker-output.json"
        if file.stat().st_size > MAX_RESULT_BYTES:
            raise ValueError("runtime worker output exceeds its byte budget")
        result_options = {}
        if isinstance(_phases, _AcknowledgedWait): result_options["expected_permit"] = _phases.binding
        result = receipt["result"] = _worker_result(R.load_json(file), receipt["request_sha256"], value, transport, **result_options)
        if result_options and (folder / "worker-permit.json").exists():
            permit_worker = RP.validate_worker(R.load_json(folder / "worker-permit.json"))
            if (not RP.same(permit_worker["binding"], _phases.binding) or
                    permit_worker["worker_pid"] != _phases.data["owned_worker"]["pid"] or
                    result.get("status") == "completed" and (
                        not permit_worker["thread_stopped"] or permit_worker["errors"] or
                        permit_worker["parent_eof"] or not permit_worker["post_returned"])):
                raise ValueError("runtime lost its actual owned permit-forwarding disposition")
        elif result_options and (result.get("attached") or result.get("status") == "completed"):
            raise ValueError("attached permit worker lost its actual forwarding journal")
        _memory_process_identity(value, result, identity)
        if result["format"] != WORKER:
            raise ValueError("current runtime transaction requires a current worker lifecycle envelope")
        lifecycle = _frida_lifecycle(result)
        if not NP.same_data(result.get("native_resources", {}), resources):
            raise ValueError("runtime worker lost its pinned native resources or fault disposition")
        if result.get("compiler_provenance", {}) != provenance:
            raise ValueError("runtime worker lost its verified compiler artifact provenance")
        attempts = result.get("helper_load_attempts", 0)
        receipt["actuated"] = None if result["invocations"] is None or attempts is None else bool(result["invocations"] or attempts)
        receipt["native_calls"] = None if result["native_calls"] is None else bool(result["native_calls"])
        receipt["after_identity"] = process_identity(root, target["bundle_id"])
        if receipt["after_identity"] != identity or R.fingerprint(root) != target:
            raise ValueError("target identity or app bytes changed during the runtime transaction")
        if adapter_identity(backend=backend) != adapter:
            raise ValueError("compiled runtime adapter or transport changed during the transaction")
        if not NP.same_data(request_resources(value), resources):
            raise ValueError("native module/prototype evidence changed during the transaction")
        if request_compiler_provenance(value) != provenance:
            raise ValueError("verified compiler artifact provenance changed during the transaction")
        cleanup = result.get("cleanup")
        if (result.get("attached") and cleanup != {"script_unloaded": True, "detached": True}):
            receipt["resource_review_required"] = True
            raise ValueError("runtime detachment is not confirmed; resource review required")
        receipt["resource_review_required"] = (result["invocations"] is None or result["native_calls"] is None or attempts is None or
                                               result.get("native_state_uncertain") is True or
                                               lifecycle["manager_state"] in {"pending", "failed"} or
                                               lifecycle["attachment_attempted"] and not result["attached"] or
                                               bool(result["cleanup_errors"]) or
                                               bool(_lifecycle_paths(value)) and result.get("status") != "completed")
        if run["returncode"] != 0 or result.get("status") != "completed":
            if result.get("status") == "unavailable" and result.get("attached") is False:
                receipt["status"] = "unavailable"
            error = result.get("error") or {"type": "RuntimeError", "message": "runtime transaction failed"}
            receipt["error"] = {"type": str(error.get("type", "RuntimeError")), "message": str(error.get("message", "failed"))[:4096]}
        else:
            receipt["status"] = "executed" if result.get("invocations", 0) or attempts else "observed"
    except Exception as error:
        if isinstance(error, Unavailable) and receipt["worker"] is None:
            receipt["status"] = "unavailable"
        receipt["error"] = _error(error, secrets)
    _redact_artifacts(folder, secrets)
    if receipt["worker"]:
        # Logs may have been sanitized after capture; bind the retained bytes.
        file = folder / "worker-output.json"
        if file.is_file() and not file.is_symlink() and file.stat().st_size <= MAX_RESULT_BYTES:
            receipt["worker"]["sha256"] = R._file_hash(file)
    receipt["elapsed_s"] = round(time.monotonic() - started, 3)
    receipt["artifacts"] = _artifacts(folder)
    receipt = _redact(receipt, secrets)
    R.write_json(folder / "runtime.json", receipt)
    return receipt


def invoke(bundle, actor, out, *, timeout=15, expected_identity=None):
    """Invoke once, optionally bound to an ephemeral complete NS process pin."""
    R.assert_guest()
    expected = _expected_identity(expected_identity)
    request = invocation_request(actor)
    if expected is None:
        return collect(bundle, out, request, timeout=timeout)
    return collect(bundle, out, request, timeout=timeout, expected_identity=dict(expected))


def _worker(config_path) -> dict:
    """Private subprocess boundary. Only the fixed bundled adapter enters the app."""
    R.assert_guest()
    config = R.load_json(config_path)
    if not isinstance(config, dict) or config.get("format") != WORKER:
        raise ValueError("invalid runtime worker configuration")
    value = validate_request(config["request"])
    result = {"format": WORKER, "status": "incomplete", "attached": False,
              "request_sha256": R.digest(value), "operations": [], "native_calls": 0, "invocations": 0,
              "cleanup": {"script_unloaded": False, "detached": False}, "cleanup_errors": [], "error": None,
              "frida_lifecycle": {"format": FRIDA_LIFECYCLE, "manager_state": "not_acquired",
                                  "attachment_attempted": False, "helper_exit_confirmed": False, "error": None},
              "frida_version": None, "identity": None, "after_identity": None,
              "transport": None, "preloaded_gadget_remains": False, "native_state_uncertain": False,
              "native_resources": {}, "compiler_provenance": {}, "helper_load_attempts": 0,
              "callable_resolutions": 0, "helper_residency": []}
    session = script = permit = None
    frida = None
    manager_requested = False
    token = None
    secrets = []
    trace_path = Path(config["trace"])
    trace_count = 0
    cleanup_path = Path(config_path).parent / "worker-cleanup.json"

    def journal():
        # Separate from the capped agent trace and stdout, which may stay empty
        # if the parent hard timeout interrupts an unfinished cleanup call.
        R.write_json(cleanup_path, _redact({
            "format": CLEANUP_JOURNAL, "request_sha256": result["request_sha256"],
            "status": result["status"], "attached": result["attached"],
            "frida_lifecycle": result["frida_lifecycle"], "cleanup": result["cleanup"],
            "cleanup_errors": result["cleanup_errors"], "error": result["error"],
            "native_state_uncertain": result["native_state_uncertain"],
            "native_calls": result["native_calls"], "invocations": result["invocations"],
            "helper_load_attempts": result["helper_load_attempts"],
            "completed_operations": len(result["operations"]),
        }, secrets))

    def cleanup_error(phase, error):
        # Never replace a primary task/transport failure with a secondary
        # cleanup error. Keep one bounded, redacted error per cleanup phase.
        if not any(row["phase"] == phase for row in result["cleanup_errors"]):
            result["cleanup_errors"].append({"phase": phase, "error": _error(error, secrets)})
        if result["error"] is None:
            result["error"] = _error(error, secrets, stack=True)
        result["status"] = "incomplete"

    def checkpoint():
        # Receipt IO failure must not skip the remaining cleanup phases.
        try:
            journal()
        except Exception as error:
            cleanup_error("journal", error)

    try:
        _require_native_path_backend(value)
        if time.time() * 1000 >= config["deadline_ms"]:
            raise TimeoutError("runtime transaction deadline expired before attachment")
        root = Path(config["bundle"]).resolve(strict=True)
        result["identity"] = process_identity(root, config["target"]["bundle_id"])
        if result["identity"] != config["identity"] or R.fingerprint(root) != config["target"]:
            raise ValueError("runtime process/app changed before attachment")
        transport, token = _transport(secrets=secrets, backend=config.get("backend"))
        result["transport"] = transport
        result["preloaded_gadget_remains"] = transport["kind"] == "gadget"
        _transport_outside(transport, root, Path(config_path).resolve(strict=True).parent)
        adapter = adapter_identity(backend=config.get("backend"))
        if (adapter != config["adapter"] or transport != config.get("transport") or
                adapter["transport"] != transport):
            raise ValueError("compiled guest adapter or transport changed before attachment")
        _helper_authorization(value, adapter.get("helper_policy"), root, config["target"], Path(config_path).resolve(strict=True).parent)
        if token and _redact(value, token) != value:
            raise ValueError("runtime request contains protected authentication material")
        resources = result["native_resources"] = request_resources(value)
        if not NP.same_data(resources, config.get("native_resources", {})):
            raise ValueError("native module/prototype resources changed before attachment")
        provenance = result["compiler_provenance"] = request_compiler_provenance(value)
        if provenance != config.get("compiler_provenance", {}):
            raise ValueError("verified compiler artifact provenance changed before attachment")
        if "pre_actuation_ack" in config:
            permit = _PermitWorker(config, Path(config_path).parent / "worker-permit.json", secrets)
        _lifecycle_budget(value, config["deadline_ms"] - int(time.time() * 1000))
        import frida
        result["frida_version"] = frida.__version__
        # This is one dedicated Python worker per transaction. Its default
        # manager owns only this worker's devices and helper connection.
        manager_requested = True
        result["frida_lifecycle"]["manager_state"] = "pending"
        manager = frida.get_device_manager()
        journal()
        if transport["kind"] == "local":
            device = manager.get_local_device()
        else:
            # This is an explicitly configured, preloaded Gadget, not a retry
            # of local injection. Gadget's public name is not target identity.
            device = manager.add_remote_device(transport["endpoint"], token=token)
            processes = device.enumerate_processes()
            if (len(processes) != 1 or type(processes[0].pid) is not int or
                    processes[0].pid != result["identity"]["pid"] or processes[0].name != "Gadget"):
                raise ValueError("Gadget listener does not identify the independently verified target PID")
        result["frida_lifecycle"]["attachment_attempted"] = True
        journal()
        session = device.attach(result["identity"]["pid"])
        result["attached"] = True
        # Recheck OS identity after the attach-by-PID race, before any JS is loaded.
        if process_identity(root, config["target"]["bundle_id"]) != config["identity"]:
            raise ValueError("runtime process identity changed during attachment")
        compiled_bytes = Path(adapter["file"]).read_bytes()
        if hashlib.sha256(compiled_bytes).hexdigest() != adapter["sha256"]:
            raise ValueError("compiled guest adapter changed before its bytes were loaded")
        source = compiled_bytes.decode("utf-8")
        script = session.create_script(source, name="anyuse-fixed-rea-runtime", runtime="qjs")
        def message(message, data):
            nonlocal trace_count
            if permit is not None and message.get("type") == "send":
                permit.notify(message.get("payload"))
            if trace_count >= 128:
                return
            event = message.get("payload") if message.get("type") == "send" else {
                "event": "agent-error", "message": _redact(str(message), secrets)[:4096]}
            event = _redact(event, secrets)
            line = json.dumps(event, ensure_ascii=False, allow_nan=False)
            if len(line.encode()) <= 8192:
                with trace_path.open("a") as stream:
                    stream.write(line + "\n")
                trace_count += 1
        script.on("message", message)
        if permit is not None: permit.start(script)
        script.load()
        payload = {"request": value, "deadline_ms": config["deadline_ms"],
                   "pid": config["identity"]["pid"], "bundle": config["bundle"],
                   "target": config["target"],
                   "executable": config["identity"]["executable"], "native_resources": resources,
                   "helper_policy": adapter.get("helper_policy")}
        if permit is not None:
            payload["pre_actuation_ack"] = permit.payload()
            payload["compiler_provenance"] = copy.deepcopy(provenance)
        # An RPC transport error can occur after a method entered the app. Until
        # the adapter returns its receipt, its execution outcome stays unknown.
        result["native_calls"] = None
        result["invocations"] = None if any(op["op"] in {"invoke", "native_invoke", Q.OPERATION} for op in value["operations"]) else 0
        result["helper_load_attempts"] = None if any(op["op"] == "load_helper" for op in value["operations"]) else 0
        observed = script.exports_sync.run(payload)
        if not isinstance(observed, dict) or type(observed.get("native_state_uncertain")) is not bool:
            raise ValueError("runtime adapter returned missing or malformed native fault disposition")
        if any(op["op"] == "load_helper" for op in value["operations"]) and type(observed.get("helper_load_attempts")) is not int:
            raise ValueError("runtime adapter lost explicit helper load outcome")
        for key in ("status", "operations", "native_calls", "invocations", "error", "process", "handles_expired", "queue", "native_state_uncertain",
                    "helper_load_attempts", "callable_resolutions", "helper_residency", "lifecycle_observer", "memory_inspection",
                    "pre_actuation_ack"):
            if key in observed:
                result[key] = observed[key]
        result["after_identity"] = process_identity(root, config["target"]["bundle_id"])
        if result["after_identity"] != config["identity"] or R.fingerprint(root) != config["target"]:
            raise ValueError("runtime target identity/app changed during execution")
        if adapter_identity(backend=config.get("backend")) != adapter:
            raise ValueError("compiled guest adapter or transport changed during execution")
        if not NP.same_data(request_resources(value), resources):
            raise ValueError("native module/prototype resources changed during execution")
        if request_compiler_provenance(value) != provenance:
            raise ValueError("verified compiler artifact provenance changed during execution")
    except Exception as error:
        result["status"] = "unavailable" if not result["attached"] else "incomplete"
        result["error"] = _error(error, secrets, stack=True)
    finally:
        if permit is not None:
            try:
                permit.stop()
            except Exception as failure:
                if result["error"] is None: result["error"] = _error(failure, secrets, stack=True)
                result["status"] = "incomplete" if result["attached"] else "unavailable"
                if permit.thread is not None and permit.thread.is_alive(): result["native_state_uncertain"] = True
        checkpoint()
        if script is not None:
            try:
                script.unload()
                result["cleanup"]["script_unloaded"] = True
            except Exception as error:
                cleanup_error("script_unload", error)
            checkpoint()
        if session is not None:
            try:
                session.detach()
                result["cleanup"]["detached"] = True
            except Exception as error:
                cleanup_error("session_detach", error)
            checkpoint()
        if manager_requested:
            # Public cooperative close of this process-local manager, also if
            # attach failed without returning a Session. A blocked close stays
            # pending in the atomic journal; the parent's existing hard worker
            # bound retains resource-review uncertainty. Do not retry or kill
            # helper PIDs, target apps, or another worker's resources here.
            checkpoint()
            try:
                frida.shutdown()
                result["frida_lifecycle"]["manager_state"] = "closed"
            except Exception as error:
                result["frida_lifecycle"]["manager_state"] = "failed"
                result["frida_lifecycle"]["error"] = _error(error, secrets)
                cleanup_error("manager_close", error)
            checkpoint()
    # Detachment confirms only our session/script cleanup. A preloaded Gadget
    # and its loopback listener remain resident until the target app exits.
    # Manager close confirms no OS helper exit and repairs no native outcome.
    return _redact(result, secrets)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("use the VM-only REA runtime bridge entry point")
    response = _worker(sys.argv[2])
    print(json.dumps(response, ensure_ascii=False, sort_keys=True, allow_nan=False))
    raise SystemExit(0 if response["status"] == "completed" else 1)
