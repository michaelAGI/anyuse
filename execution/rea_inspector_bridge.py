"""VM-only access to an already enabled local Chromium/V8 app inspector.

Recipes describe internal object paths and typed method calls. They never carry
JavaScript, live CDP object IDs, launch switches, or an app-specific driver.
This provider supplies runtime evidence, not independent task proof. Descriptor
traps and explicitly requested getters may execute app code; faults/timeouts
after any remote evaluation leave effects unknown and prohibit automatic retry.
Imports perform no native, network or filesystem operation.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit
import uuid

from . import rea_mapping as R
from . import rea_runtime_bridge as B

FORMAT = "anyuse.rea-inspector/1"
TRANSPORT = "anyuse.rea-inspector-transport/1"
CONTRACT = "anyuse.rea-inspector-call-contract/1"
MAX_MESSAGE = 2 * 1024**2
MAX_WIRE_MESSAGE = 8 * 1024**2
MAX_TEXT = 65536
MAX_OPERATIONS = 8
MAX_STEPS = 16
MAX_RPC = 256
MAX_RETAINED_EVENTS = 128
MAX_TOTAL_EVENTS = 4096
MAX_TOTAL_EVENT_BYTES = 16 * 1024**2
MAX_EVENT_METHOD_COUNTERS = 128
MAX_EVENT_METHOD_BYTES = 128
MAX_CALL_NONSTREAM_FRAMES = 256
MAX_DOM_MATCHES = 4096
MAX_DOM_ATTRIBUTES = 256
MAX_DOM_ATTRIBUTE_OUTPUT = 32
MAX_SCOPE_CHAIN = 32
MAX_SCOPE_BINDINGS = 16384
MAX_SCOPE_PREVIEW = 32
MAX_SCOPE_STEPS = 2
MAX_SCOPE_OPERATIONS = 2
MAX_INSTANCE_CANDIDATES = 64
MAX_INSTANCE_PREVIEW = 32
MAX_INSTANCE_INVARIANTS = 8
MAX_HEAP_QUERIES = 4
MAX_DESCRIPTOR_OFFSET = 8192
MAX_PROPERTY_QUERY_BYTES = 256
TYPES = {"utf8", "bool", "number", "int32", "uint32", "null", "json"}
SAFE_INTEGER = 2**53 - 1
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_SCOPE_KINDS = {"Closure", "Module", "Block", "Catch", "Script", "Local", "Eval"}


class Unavailable(ValueError):
    pass


class DOMRootNotUnique(Unavailable):
    """A completed DOM-only query rejected the root before app evaluation."""
    def __init__(self, selector, match_count):
        self.selector, self.match_count = selector, match_count
        super().__init__("DOM root requires exactly one match; observed " + str(match_count))


class ProtocolError(ValueError):
    def __init__(self, message, *, remote_exception=None, command_error=None):
        super().__init__(message)
        self.remote_exception = remote_exception
        self.command_error = command_error


# Only these reviewed functions cross the inspector. All recipe strings arrive
# as CDP CallArgument JSON values; none are interpolated into executable source.
_FIND = """
function descriptor(value, name) {
  let owner = value;
  for (let i = 0; owner !== null && i < 16; ++i) {
    const found = Object.getOwnPropertyDescriptor(owner, name);
    if (found) return found;
    owner = Object.getPrototypeOf(owner);
  }
  throw new Error('property_not_found');
}
function source(fn) {
  if (typeof fn !== 'function') throw new Error('not_function');
  const text = Function.prototype.toString.call(fn);
  if (text.length > 65536) throw new Error('function_source_budget');
  return text;
}
"""
GLOBAL = "globalThis"
DOM_ROOT = """function(selector) {
  const matches = document.querySelectorAll(selector);
  if (matches.length !== 1) throw new Error('ambiguous_dom_root');
  return matches[0];
}"""
PROPERTY = "function(name) {" + _FIND + """
  const found = descriptor(this, name);
  if (!Object.prototype.hasOwnProperty.call(found, 'value'))
    throw new Error('accessor_requires_explicit_getter');
  return found.value;
}"""
GETTER_SOURCE = "function(name) {" + _FIND + "return source(descriptor(this, name).get); }"
GETTER = "function(name, expectedSource) {" + _FIND + """
  const found = descriptor(this, name);
  if (source(found.get) !== expectedSource) throw new Error('getter_changed');
  return Reflect.apply(found.get, this, []);
}"""
MAP_KEY = """function(key) {
  if (!Map.prototype.has.call(this, key)) throw new Error('map_key_not_found');
  return Map.prototype.get.call(this, key);
}"""
MAP_KEYS = """function(maxKeys) {
  const iterator = Map.prototype.keys.call(this), keys = [];
  let unsupported = 0;
  for (let i = 0; i < maxKeys; ++i) {
    const next = iterator.next();
    if (next.done) return {keys, unsupported_key_count: unsupported, truncated: false};
    const value = next.value, type = value === null ? 'null' : typeof value;
    if (type === 'null' || type === 'boolean' || (type === 'number' && Number.isFinite(value) &&
      (!Number.isInteger(value) || Number.isSafeInteger(value))) || (type === 'string' && value.length <= 1024))
      keys.push({type, value});
    else ++unsupported;
  }
  return {keys, unsupported_key_count: unsupported, truncated: !iterator.next().done};
}"""
METHOD_SOURCE = "function(name) {" + _FIND + """
  const found = descriptor(this, name);
  if (!Object.prototype.hasOwnProperty.call(found, 'value'))
    throw new Error('method_accessor_unsupported');
  return source(found.value);
}"""
METHOD_DISCOVERY = "function(name) {" + _FIND + """
  let owner = this, depth = 0;
  for (; owner !== null && depth < 16; ++depth) {
    const found = Object.getOwnPropertyDescriptor(owner, name);
    if (found) {
      if (!Object.prototype.hasOwnProperty.call(found, 'value'))
        throw new Error('method_accessor_unsupported');
      return {status: 'found', function_source: source(found.value)};
    }
    owner = Object.getPrototypeOf(owner);
  }
  if (owner !== null) throw new Error('method_discovery_prototype_budget');
  return {status: 'not_found', prototype_depth: depth};
}"""
_JSON = """
function bounded(value, depth, seen, count) {
  if (++count.n > 256 || depth > 6) throw new Error('json_budget');
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || Object.is(value, -0) ||
        (Number.isInteger(value) && !Number.isSafeInteger(value)))
      throw new Error('unsupported_number');
    return value;
  }
  if (typeof value === 'string') {
    if (value.length > 65536) throw new Error('text_budget');
    return value;
  }
  if (typeof value !== 'object' || seen.has(value)) throw new Error('unsupported_json');
  seen.add(value);
  if (Object.getOwnPropertySymbols(value).length !== 0) throw new Error('json_symbol_unsupported');
  const array = Array.isArray(value);
  if (!array && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)
    throw new Error('json_nonplain_object_unsupported');
  const names = Object.getOwnPropertyNames(value);
  if (names.length > 65) throw new Error('json_property_budget');
  if (array && (value.length > 64 || names.length !== value.length + 1 ||
      names.some(name => name !== 'length' &&
        (!/^(0|[1-9][0-9]*)$/.test(name) || Number(name) >= value.length))))
    throw new Error('json_sparse_or_extended_array_unsupported');
  const output = array ? [] : Object.create(null);
  for (const name of names) {
    if (array && name === 'length') continue;
    if (name.length > 256) throw new Error('property_name_budget');
    const found = Object.getOwnPropertyDescriptor(value, name);
    if (!found || !Object.prototype.hasOwnProperty.call(found, 'value'))
      throw new Error('json_accessor_unsupported');
    Object.defineProperty(output, name, {value: bounded(found.value, depth + 1, seen, count),
      enumerable: true, configurable: true, writable: true});
  }
  seen.delete(value);
  return output;
}
function typed(value, kind) {
  if (kind === 'void') {
    if (value !== undefined) throw new Error('return_type_mismatch');
    return null;
  }
  if (kind === 'null' && value === null) return null;
  if (kind === 'utf8' && typeof value === 'string' && value.length <= 65536) return value;
  if (kind === 'bool' && typeof value === 'boolean') return value;
  if (kind === 'number' && typeof value === 'number' && Number.isFinite(value) &&
      !Object.is(value, -0) && (!Number.isInteger(value) || Number.isSafeInteger(value))) return value;
  if (kind === 'int32' && Number.isInteger(value) && value >= -2147483648 && value <= 2147483647) return value;
  if (kind === 'uint32' && Number.isInteger(value) && value >= 0 && value <= 4294967295) return value;
  if (kind === 'json') return bounded(value, 0, new Set(), {n: 0});
  throw new Error('return_type_mismatch');
}
"""
READ = "function(kind) {" + _JSON + "return {value: typed(this, kind)}; }"
FUNCTION_SOURCE = "function() {" + _FIND + "return source(this); }"
OWN_PROPERTY = """function(name) {
  const found = Object.getOwnPropertyDescriptor(this, name);
  if (!found || !Object.prototype.hasOwnProperty.call(found, 'value'))
    throw new Error('required_own_data_property_absent_or_accessor');
  return found.value;
}"""
ARRAY_COUNT = """function() {
  const found = Object.getOwnPropertyDescriptor(this, 'length');
  if (!Array.isArray(this) || !found || !Object.prototype.hasOwnProperty.call(found, 'value') ||
      !Number.isInteger(found.value) || found.value < 0 || found.value > 4294967295)
    throw new Error('query_result_is_not_an_ordinary_counted_array');
  return found.value;
}"""
_INSTANCE_CHECK = """
function prototypeRelation(ctor, prototype) {
  if (typeof ctor !== 'function' || ctor === Object || ctor === Function ||
      prototype === null || typeof prototype !== 'object' ||
      prototype === Object.prototype || prototype === Function.prototype) return false;
  const owner = Object.getOwnPropertyDescriptor(prototype, 'constructor');
  const ownPrototype = Object.getOwnPropertyDescriptor(ctor, 'prototype');
  return !!owner && !!ownPrototype && Object.prototype.hasOwnProperty.call(owner, 'value') &&
    Object.prototype.hasOwnProperty.call(ownPrototype, 'value') && owner.value === ctor && ownPrototype.value === prototype;
}
function invariantMatch(candidate, invariants) {
  // Constructor prototype objects can share the same direct prototype as
  // instances. Conservatively refuse any own constructor descriptor, without
  // following a possibly accessor/proxy-valued constructor.
  if (Object.getOwnPropertyDescriptor(candidate, 'constructor')) return false;
  for (const invariant of invariants) {
    const found = Object.getOwnPropertyDescriptor(candidate, invariant.name);
    if (!found || !Object.prototype.hasOwnProperty.call(found, 'value') || !Object.is(found.value, invariant.equals)) return false;
  }
  return true;
}
"""
PROTOTYPE_RELATION = "function(prototype) {" + _INSTANCE_CHECK + "return prototypeRelation(this, prototype); }"
DIRECT_PROTOTYPE = "function(prototype) { return Object.getPrototypeOf(this) === prototype; }"
OWN_INVARIANTS = "function(invariants) {" + _INSTANCE_CHECK + "return invariantMatch(this, invariants); }"
CALL = "async function(receiver, values, kinds, expectedSource, returnKind, selectionGuards = [], ...guardObjects) {" + _FIND + _JSON + _INSTANCE_CHECK + """
  const currentSource = source(this);
  if (currentSource !== expectedSource || currentSource.includes('[native code]'))
    throw new Error('method_changed_or_unattributable_native_function');
  for (let i = 0; i < kinds.length; ++i) typed(values[i], kinds[i]);
  if (guardObjects.length !== selectionGuards.length * 3) throw new Error('invalid_instance_guard_envelope');
  for (let i = 0; i < selectionGuards.length; ++i) {
    const candidate = guardObjects[i * 3], ctor = guardObjects[i * 3 + 1], prototype = guardObjects[i * 3 + 2];
    const guard = selectionGuards[i];
    if (!prototypeRelation(ctor, prototype) || source(ctor) !== guard.constructor_source ||
        Object.getPrototypeOf(candidate) !== prototype || !invariantMatch(candidate, guard.own_invariants))
      throw new Error('selected_instance_preconditions_changed_before_dispatch');
  }
  const value = await Reflect.apply(this, receiver, values);
  return {completed: true, value: typed(value, returnKind)};
}"""
_METADATA_PRIMITIVE = """
function primitiveMetadata(value, row) {
  const type = value === null ? 'null' : typeof value;
  if (type === 'null' || type === 'boolean') { row.value = value; return; }
  if (type === 'string') {
    if (value.length <= 1024) row.value = value;
    else { row.value_truncated = true; row.unsupported_reason = 'text_budget'; }
    return;
  }
  if (type === 'number') {
    if (!Number.isFinite(value)) row.unsupported_reason = 'nonfinite_number';
    else if (Object.is(value, -0)) row.unsupported_reason = 'negative_zero';
    else if (Number.isInteger(value) && !Number.isSafeInteger(value)) row.unsupported_reason = 'unsafe_integer';
    else { row.value = value; return; }
  } else row.unsupported_reason = type;
  row.value_unsupported = true;
}
"""
DESCRIBE = "function(maxProperties, prototypeDepth, offset, propertyQuery) {" + _FIND + _METADATA_PRIMITIVE + """
  if (offset !== undefined || propertyQuery !== undefined) {
    offset = offset === undefined ? 0 : offset;
    propertyQuery = propertyQuery === undefined ? '' : propertyQuery;
    if (!Number.isInteger(offset) || offset < 0 || offset > 8192 ||
        typeof propertyQuery !== 'string' || propertyQuery.length > 256)
      throw new Error('invalid_descriptor_page');
    const query = propertyQuery.toLowerCase();
    const selected = []; let owner = this, propertyCount = 0, symbolKeyCount = 0, omittedNames = 0, filteredCount = 0;
    for (let level = 0; owner !== null && level <= prototypeDepth; ++level) {
      const names = Object.getOwnPropertyNames(owner);
      propertyCount += names.length;
      symbolKeyCount += Object.getOwnPropertySymbols(owner).length;
      for (const name of names) {
        if (name.length > 256) { omittedNames++; continue; }
        if (!query || name.toLowerCase().includes(query)) {
          const index = filteredCount++;
          if (index >= offset && selected.length < maxProperties) selected.push({name, owner, level});
        }
      }
      owner = Object.getPrototypeOf(owner);
    }
    const rows = [];
    for (const item of selected) {
      const found = Object.getOwnPropertyDescriptor(item.owner, item.name);
      if (!found) throw new Error('descriptor_disappeared');
      const data = Object.prototype.hasOwnProperty.call(found, 'value');
      const row = {name: item.name, owner_depth: item.level, kind: data ? 'data' : 'accessor',
        enumerable: !!found.enumerable, configurable: !!found.configurable};
      if (data) {
        row.type = found.value === null ? 'null' : typeof found.value;
        row.writable = !!found.writable;
        if (typeof found.value === 'function') {
          const text = Function.prototype.toString.call(found.value);
          if (text.length <= 65536) row.function_source = text;
          else { row.function_source_omitted = true; row.function_source_characters = text.length; }
        }
        else if (found.value === null || typeof found.value !== 'object') primitiveMetadata(found.value, row);
      } else {
        row.getter = typeof found.get === 'function'; row.setter = typeof found.set === 'function';
        if (row.getter) {
          const text = Function.prototype.toString.call(found.get);
          if (text.length <= 65536) row.getter_source = text;
          else { row.getter_source_omitted = true; row.getter_source_characters = text.length; }
        }
      }
      rows.push(row);
    }
    const after = offset + rows.length, more = after < filteredCount;
    return {properties: rows, property_count: propertyCount, total: propertyCount,
      symbol_key_count: symbolKeyCount, symbol_keys_omitted: symbolKeyCount > 0,
      omitted_name_count: omittedNames, offset, property_query: propertyQuery,
      filtered_count: filteredCount, next_offset: more && after <= 8192 ? after : null,
      pagination_limited: more && after > 8192, paging_scope: 'selected_prototype_chain',
      truncated: offset > 0 || more || omittedNames > 0, whole_graph_coverage: false,
      getters_invoked: 0, prototype_traversed: prototypeDepth > 0};
  }
  const rows = []; let owner = this, propertyCount = 0, symbolKeyCount = 0, truncated = false;
  for (let level = 0; owner !== null && level <= prototypeDepth; ++level) {
  const names = Object.getOwnPropertyNames(owner);
  symbolKeyCount += Object.getOwnPropertySymbols(owner).length;
  propertyCount += names.length;
  if (names.length > maxProperties) truncated = true;
  for (const name of names.slice(0, maxProperties)) {
    if (name.length > 256) continue;
    const found = Object.getOwnPropertyDescriptor(owner, name);
    if (!found) throw new Error('descriptor_disappeared');
    const data = Object.prototype.hasOwnProperty.call(found, 'value');
    const row = {name, owner_depth: level, kind: data ? 'data' : 'accessor', enumerable: !!found.enumerable,
      configurable: !!found.configurable};
    if (data) {
      row.type = found.value === null ? 'null' : typeof found.value;
      row.writable = !!found.writable;
      if (typeof found.value === 'function') row.function_source = source(found.value);
      else if (found.value === null || typeof found.value !== 'object') primitiveMetadata(found.value, row);
    } else {
      row.getter = typeof found.get === 'function';
      row.setter = typeof found.set === 'function';
      if (row.getter) row.getter_source = source(found.get);
    }
    rows.push(row);
  }
  owner = Object.getPrototypeOf(owner);
  }
  return {properties: rows, property_count: propertyCount, symbol_key_count: symbolKeyCount,
    symbol_keys_omitted: symbolKeyCount > 0, truncated, whole_graph_coverage: false,
    getters_invoked: 0, prototype_traversed: prototypeDepth > 0};
}"""
GRAPH = "function(maxNodes, maxDepth, maxProperties) {" + _METADATA_PRIMITIVE + """
  const queue = [{value: this, depth: 0, id: 'n0', path: []}];
  const seen = new Map([[this, 'n0']]);
  const nodes = []; let truncated = false;
  for (let index = 0; index < queue.length && index < maxNodes; ++index) {
    const item = queue[index], edges = [];
    const names = Object.getOwnPropertyNames(item.value);
    const symbolKeyCount = Object.getOwnPropertySymbols(item.value).length;
    if (names.length > maxProperties) truncated = true;
    for (const name of names.slice(0, maxProperties)) {
      if (name.length > 256) { truncated = true; continue; }
      const found = Object.getOwnPropertyDescriptor(item.value, name);
      if (!found) throw new Error('descriptor_disappeared');
      const data = Object.prototype.hasOwnProperty.call(found, 'value');
      const edge = {name, kind: data ? 'data' : 'accessor'};
      if (!data) { edge.getter = typeof found.get === 'function'; edges.push(edge); continue; }
      const value = found.value;
      edge.type = value === null ? 'null' : typeof value;
      if (value !== null && (typeof value === 'object' || typeof value === 'function')) {
        if (seen.has(value)) { edge.ref = seen.get(value); edge.cycle_or_reuse = true; }
        else if (item.depth < maxDepth && queue.length < maxNodes) {
          const id = 'n' + queue.length; seen.set(value, id); edge.ref = id;
          queue.push({value, id, depth: item.depth + 1,
            path: item.path.concat([{kind: 'property', name, access: 'data'}])});
        } else { edge.unexpanded = true; truncated = true; }
      } else primitiveMetadata(value, edge);
      edges.push(edge);
    }
    nodes.push({id: item.id, depth: item.depth, path: item.path, edges,
      property_count: names.length, symbol_key_count: symbolKeyCount, symbol_keys_omitted: symbolKeyCount > 0});
  }
  return {nodes, truncated, getters_invoked: 0, whole_graph_coverage: false,
    traversal_scope: 'own_string_data_descriptors', symbol_keys_exported: false};
}"""


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("invalid closed inspector schema")


def _text(value, maximum=MAX_TEXT, *, empty=False):
    if not isinstance(value, str) or not (empty or bool(value)) or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _number(value):
    """Numbers whose exact semantics survive this JSON/CDP value envelope."""
    if type(value) is int:
        return abs(value) <= SAFE_INTEGER
    return (type(value) is float and math.isfinite(value) and
            not (value == 0 and math.copysign(1, value) < 0) and
            not (value.is_integer() and abs(value) > SAFE_INTEGER))


def _json_value(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 6 or count[0] > 256:
        raise ValueError("inspector JSON depth/node budget exceeded")
    if value is None or type(value) is bool:
        return
    if type(value) in {int, float}:
        if not _number(value):
            raise ValueError("inspector number cannot lose precision or be nonfinite")
        return
    if isinstance(value, str):
        if not _text(value, empty=True):
            raise ValueError("inspector text exceeds its byte budget")
        return
    if isinstance(value, (dict, list)) and len(value) <= 64:
        for key, child in value.items() if isinstance(value, dict) else enumerate(value):
            if isinstance(value, dict) and not _text(key, 256, empty=True):
                raise ValueError("invalid inspector JSON property name")
            _json_value(child, depth + 1, count)
        return
    raise ValueError("inspector arguments/returns require bounded JSON values")


def _typed(kind, value, *, result=False):
    if result and kind == "void" and value is None:
        return
    if kind == "json":
        _json_value(value)
        return
    valid = ((kind == "null" and value is None) or
             (kind == "utf8" and _text(value, empty=True)) or
             (kind == "bool" and type(value) is bool) or
             (kind == "number" and _number(value)) or
             (kind == "int32" and type(value) is int and -(2**31) <= value < 2**31) or
             (kind == "uint32" and type(value) is int and 0 <= value < 2**32))
    if not valid:
        raise ValueError("inspector value does not fit its explicit declared type")


def validate_recipe(recipe):
    _keys(recipe, {"root", "steps"})
    root = recipe["root"]
    if not isinstance(root, dict):
        raise ValueError("inspector root must be data")
    if root.get("kind") == "global":
        _keys(root, {"kind", "name"})
        if not _text(root["name"], 256):
            raise ValueError("global root needs one literal name")
    elif root.get("kind") == "dom":
        _keys(root, {"kind", "selector"})
        if not _text(root["selector"], 1024):
            raise ValueError("DOM root needs one literal unambiguous selector")
    else:
        raise ValueError("inspector root must be global or DOM data, never source/handles")
    if not isinstance(recipe["steps"], list) or len(recipe["steps"]) > MAX_STEPS:
        raise ValueError("inspector path exceeds its step budget")
    scope_steps = instance_steps = 0
    for step in recipe["steps"]:
        if not isinstance(step, dict):
            raise ValueError("inspector path steps must be data")
        if step.get("kind") == "property":
            _keys(step, {"kind", "name", "access"}, {"getter_sha256"})
            if not _text(step["name"], 256) or not isinstance(step["access"], str) or step["access"] not in {"data", "getter"}:
                raise ValueError("property traversal needs explicit data/getter semantics")
            if step["access"] == "getter":
                if not R.SHA.fullmatch(str(step.get("getter_sha256", ""))):
                    raise ValueError("explicit getter requires attributable function-source hash")
            elif "getter_sha256" in step:
                raise ValueError("data access cannot carry a getter assertion")
        elif step.get("kind") == "index":
            _keys(step, {"kind", "index"})
            if type(step["index"]) is not int or not 0 <= step["index"] <= 65535:
                raise ValueError("index must be an explicit bounded nonnegative integer")
        elif step.get("kind") == "private_property":
            _keys(step, {"kind", "name"})
            if not _text(step["name"], 256):
                raise ValueError("private property needs its actual inspected literal name")
        elif step.get("kind") == "map_key":
            _keys(step, {"kind", "key"})
            _keys(step["key"], {"type", "value"})
            if not isinstance(step["key"]["type"], str) or step["key"]["type"] not in TYPES - {"json"}:
                raise ValueError("map keys are typed primitives, never live object identities")
            _typed(step["key"]["type"], step["key"]["value"])
        elif step.get("kind") == "scope_binding":
            _keys(step, {"kind", "function_sha256", "scope", "name"})
            _validate_scope_pin(step["scope"])
            if not R.SHA.fullmatch(str(step["function_sha256"])) or not _text(step["name"], 512):
                raise ValueError("scope binding needs a measured function hash and literal bounded binding name")
            scope_steps += 1
            if scope_steps > MAX_SCOPE_STEPS:
                raise ValueError("inspector recipe exceeds its separate scope traversal budget")
        elif step.get("kind") == "unique_instance":
            _validate_instance_selection(step)
            instance_steps += 1
            if instance_steps > 1:
                raise ValueError("a recipe supports at most one exact instance selection anchor")
        else:
            raise ValueError("unsupported inspector path step or persisted live handle")
    return copy.deepcopy(recipe)


def _scope_kind(description):
    return description.split(" (", 1)[0]


def _validate_scope_pin(scope):
    _keys(scope, {"index", "description", "chain_sha256", "bindings_sha256"})
    if (type(scope["index"]) is not int or not 0 <= scope["index"] < MAX_SCOPE_CHAIN or
            not _text(scope["description"], 512) or _scope_kind(scope["description"]) not in _SCOPE_KINDS or
            any(not R.SHA.fullmatch(str(scope[key])) for key in ("chain_sha256", "bindings_sha256"))):
        raise ValueError("scope selection requires an exact supported bounded index, description and structural pins")
    return copy.deepcopy(scope)


def _protocol_limits():
    return {"wire_message_bytes": MAX_WIRE_MESSAGE, "retained_message_bytes": MAX_MESSAGE,
            "retained_event_count": MAX_RETAINED_EVENTS, "retained_event_bytes": MAX_MESSAGE,
            "total_event_count": MAX_TOTAL_EVENTS, "total_event_bytes": MAX_TOTAL_EVENT_BYTES,
            "event_method_counters": MAX_EVENT_METHOD_COUNTERS, "event_method_name_bytes": MAX_EVENT_METHOD_BYTES,
            "call_nonstream_frames": MAX_CALL_NONSTREAM_FRAMES,
            "operation_output_bytes": MAX_MESSAGE, "scope_chain": MAX_SCOPE_CHAIN,
            "scope_bindings": MAX_SCOPE_BINDINGS, "scope_preview": MAX_SCOPE_PREVIEW,
            "scope_steps_per_recipe": MAX_SCOPE_STEPS, "scope_discovery_operations": MAX_SCOPE_OPERATIONS,
            "instance_candidates": MAX_INSTANCE_CANDIDATES, "instance_preview": MAX_INSTANCE_PREVIEW,
            "instance_invariants": MAX_INSTANCE_INVARIANTS, "heap_queries": MAX_HEAP_QUERIES,
            "descriptor_page_offset": MAX_DESCRIPTOR_OFFSET, "descriptor_property_query_bytes": MAX_PROPERTY_QUERY_BYTES}


def _validate_instance_selection(step):
    _keys(step, {"kind", "constructor_sha256", "max_candidates", "own_invariants"})
    if (not R.SHA.fullmatch(str(step["constructor_sha256"])) or type(step["max_candidates"]) is not int or
            not 1 <= step["max_candidates"] <= MAX_INSTANCE_CANDIDATES or
            not isinstance(step["own_invariants"], list) or len(step["own_invariants"]) > MAX_INSTANCE_INVARIANTS):
        raise ValueError("instance selection needs a measured constructor, complete candidate budget and literal invariants")
    names = set()
    for invariant in step["own_invariants"]:
        _keys(invariant, {"name", "type", "equals"})
        if (not _text(invariant["name"], 256) or invariant["name"] in names or
                not isinstance(invariant["type"], str) or invariant["type"] not in TYPES - {"json"}):
            raise ValueError("instance invariants need unique literal own names and primitive types")
        names.add(invariant["name"])
        _typed(invariant["type"], invariant["equals"])
        if invariant["type"] == "utf8" and not _text(invariant["equals"], 1024, empty=True):
            raise ValueError("instance invariant literal text exceeds its bounded UTF-8 budget")
    return copy.deepcopy(step)


def _heap_effects(count):
    return {"queries_attempted": count, "heap_scan_possible": bool(count), "forced_gc_possible": bool(count),
            "objects_retained_until_group_release": bool(count), "server_side_candidate_bound": False}


def call_contract(actor):
    return {"format": CONTRACT, "method": actor["method"], "function_sha256": actor["function_sha256"],
            "argument_types": [argument["type"] for argument in actor["arguments"]],
            "return_type": actor["return_type"], "await_promise": True}


def validate_actor(actor, *, allow_bindings=False):
    _keys(actor, {"kind", "receiver", "method", "function_sha256", "arguments", "return_type", "signature_evidence"})
    if actor["kind"] != "inspector_method" or not _text(actor["method"], 256) or not R.SHA.fullmatch(str(actor["function_sha256"])):
        raise ValueError("inspector method needs a measured function and attributable source hash")
    validate_recipe(actor["receiver"])
    if (not isinstance(actor["arguments"], list) or len(actor["arguments"]) > 16 or
            not isinstance(actor["return_type"], str) or actor["return_type"] not in TYPES | {"void"}):
        raise ValueError("inspector call requires bounded typed arguments/return")
    for argument in actor["arguments"]:
        _keys(argument, {"type", "value"})
        if not isinstance(argument["type"], str) or argument["type"] not in TYPES:
            raise ValueError("unsupported inspector argument type")
        value = argument["value"]
        if allow_bindings and isinstance(value, dict) and set(value) == {"input"}:
            if not R.NAME.fullmatch(str(value["input"])):
                raise ValueError("invalid inspector input binding")
        else:
            _typed(argument["type"], value)
    evidence = actor["signature_evidence"]
    _keys(evidence, {"kind", "file", "sha256", "pointer"})
    if (evidence["kind"] not in {"reviewed_js", "declared_js"} or not _text(evidence["file"], 4096) or
            not Path(evidence["file"]).is_absolute() or not R.SHA.fullmatch(str(evidence["sha256"])) or
            not _text(evidence["pointer"], 1024) or not evidence["pointer"].startswith("/")):
        raise ValueError("inspector call needs a pinned reviewed or explicitly model-declared JSON declaration")
    return copy.deepcopy(actor)


def convert_binding(kind, value):
    if not _text(value, MAX_MESSAGE // 2, empty=True):
        raise ValueError("inspector binding must be explicit frozen text")
    if kind == "utf8":
        converted = value
    elif kind == "bool" and value in {"true", "false"}:
        converted = value == "true"
    elif kind == "null" and value == "null":
        converted = None
    elif kind in {"number", "int32", "uint32"} and len(value) <= 1024 and _DECIMAL.fullmatch(value):
        if kind != "number" and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
            raise ValueError("integer input must be literal decimal integer text")
        converted = float(value) if kind == "number" else int(value)
    elif kind == "json":
        converted = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    else:
        raise ValueError("unsupported or noncanonical inspector binding")
    _typed(kind, converted)
    return converted


def bind_actor(actor, inputs):
    actor = validate_actor(actor, allow_bindings=True)
    for argument in actor["arguments"]:
        value = argument["value"]
        if isinstance(value, dict) and set(value) == {"input"}:
            if value["input"] not in inputs:
                raise ValueError("inspector input missing")
            argument["value"] = convert_binding(argument["type"], inputs[value["input"]])
    return validate_actor(actor)


def resources(actor):
    R.assert_guest()
    actor = validate_actor(actor, allow_bindings=True)
    if actor["signature_evidence"]["kind"] == "declared_js":
        from . import rea_inspector_contract as C
        return C.resources(actor)
    claim = actor["signature_evidence"]
    file = Path(claim["file"])
    if (file.is_symlink() or not file.is_file() or file.resolve(strict=True) != file or
            not 1 <= file.stat().st_size <= MAX_MESSAGE // 2 or R._file_hash(file) != claim["sha256"]):
        raise ValueError("inspector declaration changed or is not a canonical regular file")
    selected = B._json_pointer(R.load_json(file), claim["pointer"])
    if selected != call_contract(actor):
        raise ValueError("reviewed inspector declaration differs from the requested typed operation")
    return {str(file): {"file": str(file), "sha256": claim["sha256"], "bytes": file.stat().st_size}}


def _invocation_resources(actor):
    # A literal JSON argument may itself contain {"input": ...}. Invocation
    # values have already been bound and validated, so never reinterpret that
    # data as a saved-recipe input name while checking its declared types.
    if actor["signature_evidence"]["kind"] == "declared_js":
        from . import rea_inspector_contract as C
        return C.resources(actor, bound=True)
    return resources(actor)


def validate_request(request):
    _keys(request, {"operations"})
    operations = request["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ValueError("inspector request needs 1–8 bounded operations")
    if sum(isinstance(operation, dict) and operation.get("op") == "function_scopes" for operation in operations) > MAX_SCOPE_OPERATIONS:
        raise ValueError("inspector request exceeds its separate scope discovery operation budget")
    for operation in operations:
        op = operation.get("op") if isinstance(operation, dict) else None
        if op == "call":
            _keys(operation, {"op", "actor"})
            validate_actor(operation["actor"])
        elif op == "dom_query":
            _keys(operation, {"op", "selector", "max_nodes"})
            if not _text(operation["selector"], 1024):
                raise ValueError("DOM metadata query needs one bounded literal CSS selector")
            if type(operation["max_nodes"]) is not int or not 1 <= operation["max_nodes"] <= 32:
                raise ValueError("DOM metadata node budget must be1–32")
        elif op == "function_scopes":
            _keys(operation, {"op", "recipe", "function_sha256", "max_scopes", "max_bindings"})
            recipe = validate_recipe(operation["recipe"])
            if (not R.SHA.fullmatch(str(operation["function_sha256"])) or
                    any(type(operation[key]) is not int or not 1 <= operation[key] <= MAX_SCOPE_PREVIEW
                        for key in ("max_scopes", "max_bindings"))):
                raise ValueError("function scope discovery requires a measured body hash and preview budgets1–32")
            if any(step["kind"] == "property" and step["access"] == "getter" for step in recipe["steps"]):
                raise ValueError("scope metadata discovery cannot invoke an app getter")
        elif op == "prototype_instances":
            _keys(operation, {"op", "recipe", "constructor_sha256", "max_instances", "max_properties"})
            recipe = validate_recipe(operation["recipe"])
            if (not R.SHA.fullmatch(str(operation["constructor_sha256"])) or
                    any(type(operation[key]) is not int or not 1 <= operation[key] <= MAX_INSTANCE_PREVIEW
                        for key in ("max_instances", "max_properties"))):
                raise ValueError("prototype instance discovery needs a pinned constructor and preview budgets1–32")
            if any(step["kind"] == "property" and step["access"] == "getter" for step in recipe["steps"]):
                raise ValueError("instance metadata discovery cannot invoke an app getter")
        elif op == "read":
            _keys(operation, {"op", "recipe", "return_type"})
            validate_recipe(operation["recipe"])
            if not isinstance(operation["return_type"], str) or operation["return_type"] not in TYPES:
                raise ValueError("read needs one bounded return type")
        elif op == "map_keys":
            _keys(operation, {"op", "recipe", "max_keys"})
            validate_recipe(operation["recipe"])
            if type(operation["max_keys"]) is not int or not 1 <= operation["max_keys"] <= 32:
                raise ValueError("map key budget must be 1–32")
        elif op in {"describe", "graph", "method"}:
            _keys(operation, {"op", "recipe"} | ({"method"} if op == "method" else {"max_properties"}),
                  {"max_nodes", "max_depth"} if op == "graph" else {"prototype_depth", "offset", "property_query"} if op == "describe" else set())
            validate_recipe(operation["recipe"])
            if op == "method" and not _text(operation["method"], 256):
                raise ValueError("method discovery needs a literal name")
            if op != "method" and (type(operation["max_properties"]) is not int or not 1 <= operation["max_properties"] <= 32):
                raise ValueError("descriptor property budget must be 1–32")
            if op == "describe" and "offset" in operation and (type(operation["offset"]) is not int or
                    not 0 <= operation["offset"] <= MAX_DESCRIPTOR_OFFSET):
                raise ValueError("descriptor page offset must be 0–8192")
            if op == "describe" and "property_query" in operation and not _text(operation["property_query"], MAX_PROPERTY_QUERY_BYTES, empty=True):
                raise ValueError("descriptor property query must be bounded literal text")
            if op == "graph" and (type(operation.get("max_nodes", 16)) is not int or
                    not 1 <= operation.get("max_nodes", 16) <= 64 or type(operation.get("max_depth", 2)) is not int or
                    not 0 <= operation.get("max_depth", 2) <= 6):
                raise ValueError("graph needs bounded node and depth budgets")
            if op == "describe" and (type(operation.get("prototype_depth", 1)) is not int or
                    not 0 <= operation.get("prototype_depth", 1) <= 4):
                raise ValueError("descriptor prototype depth must be 0–4")
        else:
            raise ValueError("unsupported inspector operation; source/launch/handles are not tool inputs")
    queries = sum(operation["op"] == "prototype_instances" for operation in operations)
    for operation in operations:
        recipe = operation["actor"]["receiver"] if operation["op"] == "call" else operation.get("recipe")
        if recipe:
            queries += sum(step["kind"] == "unique_instance" for step in recipe["steps"])
    if queries > MAX_HEAP_QUERIES:
        raise ValueError("inspector request exceeds its separate heap-query budget")
    if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) > MAX_MESSAGE // 2:
        raise ValueError("inspector request exceeds its byte budget")
    return copy.deepcopy(request)


def adapter_identity():
    R.assert_guest()
    file = Path(__file__).resolve(strict=True)
    templates = {name: globals()[name] for name in
                 ("GLOBAL", "DOM_ROOT", "PROPERTY", "GETTER_SOURCE", "GETTER", "MAP_KEY", "MAP_KEYS", "METHOD_SOURCE", "METHOD_DISCOVERY", "FUNCTION_SOURCE", "OWN_PROPERTY", "ARRAY_COUNT", "PROTOTYPE_RELATION", "DIRECT_PROTOTYPE", "OWN_INVARIANTS", "READ", "CALL", "DESCRIBE", "GRAPH")}
    return {"file": str(file), "sha256": R._file_hash(file), "fixed_templates_sha256": R.digest(templates),
            "protocol": "chromium_renderer_cdp", "caller_javascript": False,
            "protocol_limits": _protocol_limits()}


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("inspector deadline exceeded")
    return value


def _endpoint(endpoint):
    if not _text(endpoint, 256):
        raise Unavailable("inspector endpoint must be bounded literal text")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise Unavailable("inspector requires an explicit numeric guest-loopback HTTP endpoint")
    try:
        port = parsed.port
    except ValueError:
        raise Unavailable("invalid inspector port") from None
    if type(port) is not int or not 1 <= port <= 65535:
        raise Unavailable("explicit inspector port required")
    return parsed.hostname, port


def _ws_url(value, endpoint, kind):
    host, port = _endpoint(endpoint)
    parsed = urlsplit(value)
    if (parsed.scheme != "ws" or parsed.hostname != host or parsed.port != port or parsed.query or parsed.fragment or
            parsed.username or parsed.password or re.fullmatch(r"/devtools/" + kind + r"/[A-Za-z0-9_-]{1,128}", parsed.path) is None):
        raise Unavailable("inspector WebSocket is not this exact guest-loopback endpoint/target kind")
    return value


def _http_json(endpoint, path, deadline):
    host, port = _endpoint(endpoint)
    connection = http.client.HTTPConnection(host, port, timeout=_remaining(deadline))
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise Unavailable("inspector HTTP metadata refused; redirects/fallback are not followed")
        raw = response.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise Unavailable("inspector HTTP metadata exceeds its byte budget")
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    finally:
        connection.close()


def _socket(url, deadline):
    try:
        from websockets.sync.client import connect
        import inspect
    except ImportError:
        raise Unavailable("an already installed guest websockets sync client is required; no installation attempted") from None
    required = {"proxy", "compression", "open_timeout", "close_timeout", "max_size", "max_queue", "ping_interval"}
    if not required <= set(inspect.signature(connect).parameters):
        raise Unavailable("installed inspector client lacks explicit proxy/deadline/buffer controls")
    return connect(url, proxy=None, compression=None, open_timeout=_remaining(deadline), close_timeout=2,
                   max_size=MAX_WIRE_MESSAGE, max_queue=8, ping_interval=None)


class _CDP:
    def __init__(self, socket, deadline):
        self.socket, self.deadline = socket, deadline
        self.next_id, self.events, self.pending, self.closed = 0, [], set(), False
        self.invalid_context = False
        self.bound_context = None
        self.retained_event_bytes = 0
        self.received_event_count = self.received_event_bytes = 0
        self.streamed_event_count = self.streamed_event_bytes = 0
        self.event_method_counts = {}
        self.unindexed_event_count = self.unindexed_event_bytes = 0
        self.total_event_budget_exceeded = self.retained_event_budget_exceeded = False

    def _count_event(self, name, raw_bytes):
        self.received_event_count += 1
        self.received_event_bytes += raw_bytes
        if self.received_event_count > MAX_TOTAL_EVENTS or self.received_event_bytes > MAX_TOTAL_EVENT_BYTES:
            self.total_event_budget_exceeded = True
        try:
            bounded = isinstance(name, str) and 0 < len(name.encode('utf-8')) <= MAX_EVENT_METHOD_BYTES and '\0' not in name
        except UnicodeError:
            bounded = False
        if bounded and (name in self.event_method_counts or len(self.event_method_counts) < MAX_EVENT_METHOD_COUNTERS):
            row = self.event_method_counts.setdefault(name, {"received": 0, "utf8_bytes": 0})
            row["received"] += 1
            row["utf8_bytes"] += raw_bytes
        else:
            self.unindexed_event_count += 1
            self.unindexed_event_bytes += raw_bytes

    def event_summary(self):
        """Consumed notifications only; no DOM nodes or event payloads copied."""
        return {"received_count": self.received_event_count, "received_utf8_bytes": self.received_event_bytes,
                "retained_count": len(self.events), "retained_utf8_bytes": self.retained_event_bytes,
                "streamed": {"method": "DOM.setChildNodes", "count": self.streamed_event_count,
                             "utf8_bytes": self.streamed_event_bytes, "payload_retained": False},
                "by_method": copy.deepcopy(self.event_method_counts),
                "unindexed_methods": {"received": self.unindexed_event_count, "utf8_bytes": self.unindexed_event_bytes},
                "total_budget_exceeded": self.total_event_budget_exceeded,
                "retained_budget_exceeded": self.retained_event_budget_exceeded,
                "context_invalidated": self.invalid_context, "pending_rpc_count": len(self.pending),
                "received_events_only": True, "raw_payloads_in_summary": False}

    def call(self, method, params=None, *, cleanup=False):
        if self.next_id >= MAX_RPC and not cleanup:
            raise ProtocolError("inspector RPC budget exhausted")
        if self.invalid_context and not cleanup:
            raise ProtocolError("inspector context changed; live IDs are expired")
        deadline = time.monotonic() + 2 if cleanup else self.deadline
        _remaining(deadline)
        self.next_id += 1
        requested = self.next_id
        self.pending.add(requested)
        self.socket.send(json.dumps({"id": requested, "method": method, "params": params or {}},
                                    ensure_ascii=False, allow_nan=False))
        nonstream_frames = 0
        while nonstream_frames < MAX_CALL_NONSTREAM_FRAMES:
            raw = self.socket.recv(timeout=_remaining(deadline))
            if not isinstance(raw, str):
                raise ProtocolError("inspector response is not bounded UTF-8 JSON")
            raw_bytes = len(raw.encode())
            if raw_bytes > MAX_WIRE_MESSAGE:
                raise ProtocolError("inspector response is not bounded UTF-8 JSON")
            message = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(message, dict):
                raise ProtocolError("malformed inspector response")
            if "method" in message:
                self._count_event(message["method"], raw_bytes)
                if message["method"] in {"Runtime.executionContextDestroyed", "Runtime.executionContextsCleared", "Inspector.detached"}:
                    self.invalid_context = True
                    if not cleanup:
                        raise ProtocolError("inspector context was destroyed; no stale-ID retry")
                if message["method"] == "Runtime.executionContextCreated" and self.bound_context:
                    context = message.get("params", {}).get("context", {})
                    auxiliary = context.get("auxData", {})
                    if (auxiliary.get("isDefault") is True and
                            auxiliary.get("frameId") == self.bound_context["frame_id"] and
                            context.get("uniqueId") != self.bound_context["unique_id"]):
                        self.invalid_context = True
                        if not cleanup:
                            raise ProtocolError("app default execution context was replaced; no stale-ID retry")
                if self.total_event_budget_exceeded:
                    raise ProtocolError("inspector total event budget exceeded")
                # DOM.getDocument implicitly enables DOM notifications. Node-ID
                # requests can emit this exact unused notification repeatedly;
                # account for its wire bytes without retaining the node tree.
                # Context invalidation above always runs before this filter.
                parameters = message.get("params")
                if (message["method"] == "DOM.setChildNodes" and set(message) == {"method", "params"} and
                        type(parameters) is dict and set(parameters) == {"parentId", "nodes"} and
                        type(parameters["parentId"]) is int and 0 < parameters["parentId"] < 2**31 and
                        type(parameters["nodes"]) is list):
                    self.streamed_event_count += 1
                    self.streamed_event_bytes += raw_bytes
                    continue
                nonstream_frames += 1
                if len(self.events) >= MAX_RETAINED_EVENTS or self.retained_event_bytes + raw_bytes > MAX_MESSAGE:
                    self.retained_event_budget_exceeded = True
                    raise ProtocolError("inspector event budget exceeded")
                self.retained_event_bytes += raw_bytes
                self.events.append(message)
                continue
            nonstream_frames += 1
            response_id = message.get("id")
            if type(response_id) is not int or response_id not in self.pending:
                raise ProtocolError("unsolicited/stale inspector RPC response")
            self.pending.remove(response_id)
            if response_id != requested:
                # A timed-out request may finish while handles are released.
                # Its late result does not make the interrupted task successful.
                # It is discarded within the wire bound, not retained as output.
                continue
            if raw_bytes > MAX_MESSAGE and method != "Runtime.getProperties":
                raise ProtocolError("inspector response exceeds its retained message budget")
            if "error" in message:
                raise ProtocolError("inspector command refused; effects may be partial",
                                    command_error=_command_error_diagnostic(message["error"], method, params or {}))
            if not isinstance(message.get("result"), dict):
                raise ProtocolError("inspector command returned a malformed result")
            return message["result"]
        raise ProtocolError("inspector response/event budget exhausted")

    def close(self):
        self.socket.close()
        self.closed = True


def _config_for_target(target, value):
    _keys(target, {"bundle_id", "version", "build", "sha256", "files", "bytes"})
    if (not _text(target["bundle_id"], 1024) or not _text(target["version"], 1024, empty=True) or
            not _text(target["build"], 1024, empty=True) or not R.SHA.fullmatch(str(target["sha256"])) or
            type(target["files"]) is not int or target["files"] <= 0 or
            type(target["bytes"]) is not int or not 1 <= target["bytes"] <= R.MAX_BUNDLE_BYTES):
        raise ValueError("inspector transport requires the complete verified app fingerprint")
    value = value or os.environ.get("ANYUSE_REA_INSPECTOR_CONFIG")
    pin, raw = B._private_file(value, 16384, "inspector configuration requires an owned canonical 0600 guest file")
    config = json.loads(raw)
    _keys(config, {"format", "endpoint", "relative_url", "target"}, {"document_suffix"})
    if config["format"] != TRANSPORT or config["target"] != target:
        raise Unavailable("inspector operator configuration differs from this exact app fingerprint")
    _endpoint(config["endpoint"])
    if not _text(config["relative_url"], 4096):
        raise Unavailable("inspector target URL must be bounded app-relative text")
    path = Path(config["relative_url"])
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise Unavailable("inspector target URL must name an exact app-relative renderer document")
    if "document_suffix" in config:
        _document_suffix(config["document_suffix"])
    return config, pin


def _config(bundle, target, value):
    config, pin = _config_for_target(target, value)
    if not (bundle / config["relative_url"]).resolve().is_relative_to(bundle):
        raise Unavailable("renderer URL path escapes the exact app bundle")
    return config, pin


def _transport(config, pin):
    value = {"kind": "chromium_renderer_cdp", "endpoint": config["endpoint"],
             "relative_url": config["relative_url"], "config": pin}
    if "document_suffix" in config:
        value["document_suffix"] = _document_suffix(config["document_suffix"])
    return value


def _document_suffix(value):
    """Validate exact raw URI components without decoding or normalizing them.

    An absent configuration field still requires both components to be empty.
    Explicit suffixes are data pins, never a query/fragment wildcard or source.
    """
    _keys(value, {"query", "fragment"})
    for key in ("query", "fragment"):
        component = value[key]
        if (not _text(component, 4096, empty=True) or
                any(not 0x21 <= ord(character) <= 0x7E for character in component) or
                key == "query" and "#" in component):
            raise ValueError("document suffix must contain bounded exact printable raw URI query/fragment text")
    return copy.deepcopy(value)


def _effective_frame_url(frame):
    """Join CDP Page.Frame's separately supplied raw fragment exactly once.

    The primary Page.Frame schema defines url without its fragment and the
    optional urlFragment including '#'. Legacy full URLs remain literal when
    the separate field is absent; supplying both forms is contradictory.
    https://chromedevtools.github.io/devtools-protocol/tot/Page/#type-Frame
    """
    if not isinstance(frame, dict) or not _text(frame.get("url"), 16384):
        raise ValueError("inspector frame omitted a bounded actual document URL")
    url = frame["url"]
    if "urlFragment" not in frame:
        return url
    fragment = frame["urlFragment"]
    if "#" in url or not _text(fragment, 4097, empty=True):
        raise ValueError("inspector frame contradicts the separate fragment representation")
    if fragment:
        if not fragment.startswith("#"):
            raise ValueError("CDP frame fragment must explicitly include its leading '#'")
        _document_suffix({"query": "", "fragment": fragment[1:]})
    effective = url + fragment
    if not _text(effective, 16384):
        raise ValueError("effective inspector frame URL exceeds its byte budget")
    return effective


def transport_identity_for_target(target_fingerprint, *, config_file=None):
    """Pin an owned configuration against an already verified fingerprint.

    This metadata-only step requires no bundle path, resolution, connection or
    process. Relative path containment is checked against the actual bundle at
    collect/invoke; metadata preparation alone never establishes app access.
    """
    R.assert_guest()
    config, pin = _config_for_target(target_fingerprint, config_file)
    return _transport(config, pin)


def transport_identity(bundle, *, config_file=None):
    """Pin operator-owned configuration; this neither connects nor starts an app."""
    R.assert_guest()
    bundle = Path(bundle).resolve(strict=True)
    config, pin = _config(bundle, R.fingerprint(bundle), config_file)
    return _transport(config, pin)


def _select_target(bundle, config, deadline):
    version = _http_json(config["endpoint"], "/json/version", deadline)
    browser = _ws_url(version.get("webSocketDebuggerUrl", ""), config["endpoint"], "browser")
    targets = _http_json(config["endpoint"], "/json/list", deadline)
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise Unavailable("inspector target list is missing or exceeds its budget")
    expected = (bundle / config["relative_url"]).resolve()
    suffix = _document_suffix(config.get("document_suffix", {"query": "", "fragment": ""}))
    selected = []
    for row in targets:
        if not isinstance(row, dict) or row.get("type") != "page" or not _text(row.get("url"), 16384):
            continue
        parsed = urlsplit(row["url"])
        if (parsed.scheme == "file" and parsed.netloc in {"", "localhost"} and
                parsed.query == suffix["query"] and parsed.fragment == suffix["fragment"] and
                Path(unquote(parsed.path)).resolve() == expected):
            selected.append(row)
    if len(selected) != 1:
        raise Unavailable("need one exact owned app renderer target; no first-target fallback")
    selected = selected[0]
    _ws_url(selected.get("webSocketDebuggerUrl", ""), config["endpoint"], "page")
    if not _text(selected.get("id"), 128):
        raise Unavailable("inspector renderer target has no bounded identity")
    return browser, {key: selected[key] for key in ("id", "type", "url", "webSocketDebuggerUrl")}


def _listener(endpoint, main, deadline):
    _, port = _endpoint(endpoint)
    run = R.subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fpun"],
                           capture_output=True, text=True, timeout=min(5, _remaining(deadline)), check=False)
    if run.returncode or len(run.stdout) > 65536:
        raise Unavailable("cannot independently attribute the inspector listener")
    records, row = [], {}
    for line in run.stdout.splitlines():
        if line.startswith("p"):
            if row:
                records.append(row)
            row = {"pid": int(line[1:]), "names": []}
        elif line.startswith("u"):
            row["uid"] = int(line[1:])
        elif line.startswith("n"):
            row["names"].append(line[1:])
    if row:
        records.append(row)
    if not records or any(row.get("pid") != main["pid"] or row.get("uid") != main["uid"] or
                          not row["names"] or any(name not in {f"127.0.0.1:{port}", f"[::1]:{port}"}
                                                  for name in row["names"]) for row in records):
        raise Unavailable("inspector listener is foreign or not restricted to guest loopback")
    return {"pid": main["pid"], "uid": main["uid"], "listen_addresses": sorted({name for row in records for name in row["names"]})}


def _ps_environment():
    """Persist kernel start strings in one locale without changing the caller."""
    return dict(os.environ, LC_ALL="C", LANG="C")


def _process(pid, deadline):
    run = R.subprocess.run(["/bin/ps", "-o", "ppid=,uid=,lstart=,comm=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=min(5, _remaining(deadline)),
                           env=_ps_environment(), check=False)
    fields = run.stdout.strip().split(None, 7)
    if run.returncode or len(fields) != 8 or not fields[0].isdigit() or not fields[1].isdigit():
        raise Unavailable("renderer process identity unavailable")
    return {"pid": pid, "ppid": int(fields[0]), "uid": int(fields[1]), "started_at": " ".join(fields[2:7]),
            "executable": str(Path(fields[7]).resolve(strict=True))}


def _renderer_processes(browser, bundle, main, deadline):
    rows = browser.call("SystemInfo.getProcessInfo").get("processInfo")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise Unavailable("browser cannot identify its bounded process set")
    browsers = [row for row in rows if row.get("type") == "browser"]
    if len(browsers) != 1 or browsers[0].get("id") != main["pid"]:
        raise Unavailable("inspector browser PID differs from the exact owned app")
    renderer_rows = [row for row in rows if row.get("type") == "renderer"]
    if not 1 <= len(renderer_rows) <= 16:
        raise Unavailable("inspector needs a bounded attributable renderer set")
    pids = [row.get("id") for row in renderer_rows]
    if any(type(pid) is not int or pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
        raise Unavailable("renderer process set must have unique exact positive PIDs")
    result = []
    for renderer in renderer_rows:
        pid = renderer.get("id")
        if type(pid) is not int or pid <= 0:
            raise Unavailable("renderer has no exact OS process identity")
        original = current = _process(pid, deadline)
        seen = set()
        for _ in range(16):
            if (current["uid"] != main["uid"] or not Path(current["executable"]).is_relative_to(bundle) or
                    current["pid"] in seen):
                raise Unavailable("renderer is foreign, reused or outside the exact app bundle")
            seen.add(current["pid"])
            if current["ppid"] == main["pid"]:
                break
            current = _process(current["ppid"], deadline)
        else:
            raise Unavailable("renderer ancestry exceeds its budget")
        result.append(original)
    return sorted(result, key=lambda row: row["pid"])


def _exception_diagnostic(details):
    """Copy bounded scalar exception metadata, never live handles or previews."""
    diagnostic = {"malformed": False, "truncated": False}
    if not isinstance(details, dict):
        diagnostic["malformed"] = True
        return diagnostic

    def text_field(source, name, output, budget):
        if name not in source:
            return
        value = source[name]
        if not isinstance(value, str):
            diagnostic["malformed"] = True
            return
        try:
            raw = value.encode("utf-8")
        except UnicodeError:
            raw = value.encode("utf-8", errors="replace")
            diagnostic["malformed"] = True
        if b"\0" in raw:
            raw = raw.replace(b"\0", b"?")
            diagnostic["malformed"] = True
        if len(raw) > budget:
            raw = raw[:budget]
            diagnostic["truncated"] = True
        diagnostic[output] = raw.decode("utf-8", errors="ignore")

    text_field(details, "text", "text", 1024)
    text_field(details, "url", "source_url", 1024)
    for name, output in (("lineNumber", "line"), ("columnNumber", "column")):
        if name in details:
            value = details[name]
            if type(value) is int and 0 <= value < 2**31:
                diagnostic[output] = value
            else:
                diagnostic["malformed"] = True
    if "exception" in details:
        exception = details["exception"]
        if not isinstance(exception, dict):
            diagnostic["malformed"] = True
        else:
            text_field(exception, "type", "type", 32)
            text_field(exception, "subtype", "subtype", 64)
            text_field(exception, "description", "description", 2048)
    return diagnostic


def _fixed_template_name(source):
    return next((name for name in ("DOM_ROOT", "PROPERTY", "GETTER_SOURCE", "GETTER", "MAP_KEY", "MAP_KEYS",
                                   "METHOD_SOURCE", "METHOD_DISCOVERY", "FUNCTION_SOURCE", "OWN_PROPERTY", "ARRAY_COUNT", "PROTOTYPE_RELATION",
                                   "DIRECT_PROTOTYPE", "OWN_INVARIANTS", "READ", "CALL", "DESCRIBE", "GRAPH")
                 if globals()[name] == source), "UNKNOWN_REVIEWED_FUNCTION")


def _command_error_diagnostic(error, method, params):
    """Bounded protocol diagnostics; metadata is never executed or dereferenced."""
    diagnostic = {"command": method, "malformed": False, "truncated": False}
    if method == "Runtime.callFunctionOn":
        diagnostic["fixed_template"] = _fixed_template_name(params.get("functionDeclaration"))
    elif method == "Runtime.evaluate" and params.get("expression") == GLOBAL:
        diagnostic["fixed_template"] = "GLOBAL"
    if not isinstance(error, dict):
        diagnostic["malformed"] = True
        return diagnostic
    if type(error.get("code")) is int and -(2**31) <= error["code"] < 2**31:
        diagnostic["code"] = error["code"]
    else:
        diagnostic["malformed"] = True
    for key, budget in (("message", 1024), ("data", 2048)):
        if key not in error:
            if key == "message":
                diagnostic["malformed"] = True
            continue
        value = error[key]
        if not isinstance(value, str):
            diagnostic["malformed"] = True
            continue
        try:
            raw = value.encode("utf-8")
        except UnicodeError:
            raw = value.encode("utf-8", errors="replace")
            diagnostic["malformed"] = True
        if b"\0" in raw:
            raw = raw.replace(b"\0", b"?")
            diagnostic["malformed"] = True
        if len(raw) > budget:
            raw = raw[:budget]
            diagnostic["truncated"] = True
        diagnostic[key] = raw.decode("utf-8", errors="ignore")
    return diagnostic


def _remote(result, *, fixed_template=None):
    if "exceptionDetails" in result:
        diagnostic = _exception_diagnostic(result["exceptionDetails"])
        if fixed_template is not None:
            diagnostic["fixed_template"] = fixed_template
        raise ProtocolError("fixed inspector evaluation failed; effects may be partial", remote_exception=diagnostic)
    if not isinstance(result.get("result"), dict):
        raise ProtocolError("fixed inspector evaluation returned malformed metadata; effects may be partial")
    return result["result"]


def _scope_remote_metadata(remote):
    """Validate only bounded scalar metadata; never copy previews or live IDs."""
    if not isinstance(remote, dict) or remote.get("type") not in {
            "undefined", "object", "function", "string", "number", "boolean", "symbol", "bigint"}:
        raise ProtocolError("scope descriptor has malformed remote-value metadata")
    kind = remote["type"]
    output = {"type": kind}
    for key, label, limit in (("subtype", "subtype", 64), ("className", "class_name", 128)):
        if key in remote:
            if not _text(remote[key], limit):
                raise ProtocolError("scope descriptor has oversized or malformed scalar type metadata")
            output[label] = remote[key]
    if "objectId" in remote and not _text(remote["objectId"], 1024):
        raise ProtocolError("scope descriptor has an invalid fresh object identity")
    if kind == "function" or kind == "object" and remote.get("subtype") != "null":
        if "objectId" not in remote or "unserializableValue" in remote:
            raise ProtocolError("scope object descriptor omitted a usable fresh object identity")
    elif kind == "object" and (remote.get("value", object()) is not None or "objectId" in remote):
        raise ProtocolError("scope null descriptor does not contain an actual null primitive")
    elif kind == "boolean" and type(remote.get("value")) is not bool:
        raise ProtocolError("scope boolean descriptor omitted its actual primitive")
    elif kind == "string" and not isinstance(remote.get("value"), str):
        raise ProtocolError("scope string descriptor omitted its actual primitive")
    elif kind == "number" and "unserializableValue" not in remote and type(remote.get("value")) not in {int, float}:
        raise ProtocolError("scope number descriptor omitted its actual primitive")
    reason = None
    if "unserializableValue" in remote:
        if not _text(remote["unserializableValue"], 256):
            raise ProtocolError("scope descriptor has malformed unserializable primitive metadata")
        reason = "unserializable_primitive"
    elif kind in {"undefined", "symbol", "bigint"}:
        reason = kind
    elif kind == "number" and not _number(remote["value"]):
        reason = "unsupported_number"
    elif kind == "string":
        try:
            length = len(remote["value"].encode("utf-8"))
        except UnicodeEncodeError:
            reason = "non_utf8_string"
        else:
            if "\0" in remote["value"]:
                reason = "nul_in_string"
            elif length > MAX_TEXT:
                reason = "text_budget"
    output["value_supported"] = reason is None
    if reason:
        output["unsupported_reason"] = reason
    return output


def _scope_binding_rows(rows):
    # This digest pins structural membership, not mutable captured values or
    # closure-instance identity. The anchored recipe and independent task
    # observer still determine whether an operation reached the intended state.
    if not isinstance(rows, list) or len(rows) > MAX_SCOPE_BINDINGS:
        raise ProtocolError("complete scope binding descriptors exceed their bounded cardinality")
    names, output = set(), []
    for row in rows:
        if not isinstance(row, dict) or not _text(row.get("name"), 512) or row["name"] in names:
            raise ProtocolError("scope binding names are malformed or ambiguous")
        names.add(row["name"])
        for key in ("writable", "configurable", "enumerable", "isOwn", "wasThrown"):
            if key in row and type(row[key]) is not bool:
                raise ProtocolError("scope binding descriptor flags must be exact booleans")
        if row.get("isOwn") is False:
            raise ProtocolError("scope binding descriptor unexpectedly belongs to another scope")
        for key in ("get", "set"):
            if key in row:
                metadata = _scope_remote_metadata(row[key])
                if metadata["type"] not in {"function", "undefined"}:
                    raise ProtocolError("scope accessor descriptor has no function/undefined metadata")
        value = _scope_remote_metadata(row["value"]) if "value" in row else None
        if "symbol" in row:
            if _scope_remote_metadata(row["symbol"])["type"] != "symbol":
                raise ProtocolError("scope symbol descriptor has an invalid symbol identity")
            kind = "symbol"
        elif row.get("wasThrown") is True or value is None and "get" not in row and "set" not in row:
            kind = "unavailable"
        elif "get" in row or "set" in row:
            kind = "accessor"
        else:
            kind = "data"
        output.append((row, {"name": row["name"], "kind": kind, **(value or {})}))
    shape = sorted(({"name": metadata["name"], "kind": metadata["kind"]} for _, metadata in output),
                   key=lambda item: item["name"])
    return output, R.digest({"format": "anyuse.inspector-scope-bindings/1", "bindings": shape})


class _Transaction:
    def __init__(self, page, context, group):
        self.page, self.context, self.group = page, context, group
        self.evaluation_started = False
        self.runtime_evaluation_attempts = 0
        self.dom_root_started = False
        self.method_attempts = self.getter_attempts = 0
        self.live_ids = set()
        self.heap_queries = 0
        self.instance_guards = []
        self.instance_selection_modes = []

    def remember(self, remote):
        if "objectId" in remote:
            if not _text(remote["objectId"], 1024):
                raise ProtocolError("invalid transaction-local inspector object identity")
            self.live_ids.add(remote["objectId"])
        return remote

    def function(self, receiver, function, values=(), *, by_value=False, object_argument=None, extra_object_arguments=()):
        if not isinstance(receiver, dict) or receiver.get("objectId") not in self.live_ids:
            raise ProtocolError("internal receiver has no fresh transaction-local object identity")
        arguments = [{"value": value} for value in values]
        if object_argument is not None:
            if object_argument.get("objectId") not in self.live_ids:
                raise ProtocolError("object argument is not from this fresh inspector transaction")
            arguments.insert(0, {"objectId": object_argument["objectId"]})
        for argument in extra_object_arguments:
            if not isinstance(argument, dict) or argument.get("objectId") not in self.live_ids:
                raise ProtocolError("instance guard needs a fresh transaction-local object identity")
            arguments.append({"objectId": argument["objectId"]})
        self.evaluation_started = True
        self.runtime_evaluation_attempts += 1
        result = self.page.call("Runtime.callFunctionOn", {"objectId": receiver["objectId"],
            "functionDeclaration": function, "arguments": arguments,
            "objectGroup": self.group, "returnByValue": by_value, "awaitPromise": True, "silent": True})
        remote = _remote(result, fixed_template=_fixed_template_name(function))
        if by_value and "value" not in remote:
            raise ProtocolError("inspector omitted the bounded by-value result")
        return self.remember(remote)

    def private_properties(self, receiver):
        if receiver.get("objectId") not in self.live_ids:
            raise ProtocolError("private inspection needs a fresh receiver")
        result = self.page.call("Runtime.getProperties", {"objectId": receiver["objectId"],
            "ownProperties": True, "accessorPropertiesOnly": False, "generatePreview": False})
        if "exceptionDetails" in result:
            raise ProtocolError("private descriptor inspection failed")
        rows = result.get("privateProperties", [])
        if not isinstance(rows, list) or len(rows) > 128:
            raise ProtocolError("private descriptor budget exceeded")
        return rows

    def private_value(self, receiver, name):
        found = [row for row in self.private_properties(receiver) if row.get("name") == name]
        if len(found) != 1 or not isinstance(found[0].get("value"), dict) or "get" in found[0]:
            raise ProtocolError("private field/method absent, ambiguous or an unsupported accessor")
        return self.remember(found[0]["value"])

    def scope_properties(self, remote):
        if remote.get("objectId") not in self.live_ids:
            raise ProtocolError("scope inspection requires a fresh transaction-local receiver")
        result = self.page.call("Runtime.getProperties", {"objectId": remote["objectId"],
            "ownProperties": True, "accessorPropertiesOnly": False, "generatePreview": False})
        if "exceptionDetails" in result:
            raise ProtocolError("scope descriptor inspection failed; effects may be partial",
                                remote_exception=_exception_diagnostic(result["exceptionDetails"]))
        if not isinstance(result.get("result"), list) or len(result["result"]) > MAX_SCOPE_BINDINGS:
            raise ProtocolError("scope descriptor inspection omitted or exceeded its complete bounded result")
        return result

    def function_scope_chain(self, function, expected_sha):
        if function.get("type") != "function" or function.get("subtype") == "proxy":
            raise ProtocolError("scope anchor must be an attributable actual JavaScript function")
        source = self.function(function, FUNCTION_SOURCE, by_value=True).get("value")
        if (not _text(source) or "[native code]" in source or
                hashlib.sha256(source.encode()).hexdigest() != expected_sha):
            raise ProtocolError("scope anchor source changed or is native/bound/unattributable")
        internal = self.scope_properties(function).get("internalProperties", [])
        if (not isinstance(internal, list) or len(internal) > 128 or
                any(not isinstance(row, dict) or not _text(row.get("name"), 512) for row in internal)):
            raise ProtocolError("function internal descriptors are malformed or oversized")
        found = [row for row in internal if row["name"] == "[[Scopes]]"]
        if len(found) != 1 or not isinstance(found[0].get("value"), dict):
            raise ProtocolError("function has no unique inspector-provided scope list")
        value = found[0]["value"]
        if value.get("type") != "object" or value.get("subtype") != "internal#scopeList":
            raise ProtocolError("function scopes have an unsupported inspector wrapper type")
        _scope_remote_metadata(value)
        rows = self.scope_properties(self.remember(value))["result"]
        if len(rows) > MAX_SCOPE_CHAIN:
            raise ProtocolError("complete inspector scope chain exceeds32 entries")
        scopes, seen = [], set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str) or
                    re.fullmatch(r"0|[1-9][0-9]*", row["name"]) is None or
                    len(row["name"]) > 2 or int(row["name"]) >= MAX_SCOPE_CHAIN or
                    row["name"] in seen or "get" in row or "set" in row or "symbol" in row or
                    row.get("wasThrown", False) is not False or row.get("isOwn", True) is not True or
                    not isinstance(row.get("value"), dict)):
                raise ProtocolError("scope indices are malformed, ambiguous or not direct own data descriptors")
            seen.add(row["name"])
            wrapper = row["value"]
            if (wrapper.get("type") != "object" or wrapper.get("subtype") != "internal#scope" or
                    not _text(wrapper.get("description"), 512)):
                raise ProtocolError("scope entry has no exact supported bounded wrapper description")
            _scope_remote_metadata(wrapper)
            scopes.append({"index": int(row["name"]), "description": wrapper["description"],
                           "remote": self.remember(wrapper)})
        scopes.sort(key=lambda scope: scope["index"])
        if [scope["index"] for scope in scopes] != list(range(len(scopes))):
            raise ProtocolError("inspector scope chain has missing/noncontiguous indices")
        chain = [{"index": scope["index"], "description": scope["description"]} for scope in scopes]
        return scopes, R.digest({"format": "anyuse.inspector-scope-chain/1", "scopes": chain})

    def function_scopes(self, function, operation):
        scopes, chain_sha = self.function_scope_chain(function, operation["function_sha256"])
        output = []
        for scope in scopes[:operation["max_scopes"]]:
            item = {"index": scope["index"], "description": scope["description"],
                    "kind": _scope_kind(scope["description"])}
            if item["kind"] not in _SCOPE_KINDS:
                item.update(traversed=False, unsupported_reason="unsupported_scope_kind")
            else:
                rows, shape = _scope_binding_rows(self.scope_properties(scope["remote"])["result"])
                item.update(traversed=True, binding_count=len(rows), bindings_sha256=shape,
                            bindings=[metadata for _, metadata in rows[:operation["max_bindings"]]],
                            bindings_truncated=len(rows) > operation["max_bindings"])
            output.append(item)
        return {"op": "function_scopes", "function_sha256": operation["function_sha256"],
                "scope_count": len(scopes), "chain_sha256": chain_sha, "scopes": output,
                "scopes_truncated": len(scopes) > operation["max_scopes"], "whole_scope_coverage": False,
                "primitive_values_exported": False, "live_ids_exported": False,
                "debugger_pause": False, "getters_invoked": 0}

    def scope_binding(self, function, step):
        scopes, chain_sha = self.function_scope_chain(function, step["function_sha256"])
        pin = step["scope"]
        if chain_sha != pin["chain_sha256"] or pin["index"] >= len(scopes):
            raise ProtocolError("scope chain changed; no alternate-scope search or retry")
        scope = scopes[pin["index"]]
        if scope["description"] != pin["description"] or _scope_kind(scope["description"]) not in _SCOPE_KINDS:
            raise ProtocolError("exact selected scope description changed or is unsupported")
        rows, shape = _scope_binding_rows(self.scope_properties(scope["remote"])["result"])
        if shape != pin["bindings_sha256"]:
            raise ProtocolError("complete selected scope binding shape changed")
        selected = [(row, metadata) for row, metadata in rows if metadata["name"] == step["name"]]
        if (len(selected) != 1 or selected[0][1]["kind"] != "data" or
                selected[0][1].get("value_supported") is not True):
            raise ProtocolError("selected scope binding is absent, ambiguous or not a supported actual data value")
        # The inspector unwraps scope wrappers for getProperties. Calling the
        # ordinary PROPERTY template on a synthetic wrapper would be incorrect.
        return self.remember(selected[0][0]["value"])

    def inspect_prototype(self, prototype, expected_sha):
        if prototype.get("type") != "object" or prototype.get("subtype") in {"null", "proxy"}:
            raise ProtocolError("instance query needs a specific ordinary object prototype, never a proxy")
        _scope_remote_metadata(prototype)
        ctor = self.function(prototype, OWN_PROPERTY, ["constructor"])
        if ctor.get("type") != "function" or ctor.get("subtype") == "proxy":
            raise ProtocolError("instance prototype has no actual own nonproxy constructor function")
        source = self.function(ctor, FUNCTION_SOURCE, by_value=True).get("value")
        if (not _text(source) or "[native code]" in source or
                hashlib.sha256(source.encode()).hexdigest() != expected_sha):
            raise ProtocolError("instance constructor source changed or is native/bound/unattributable")
        relation = self.function(ctor, PROTOTYPE_RELATION, by_value=True, object_argument=prototype).get("value")
        if relation is not True:
            raise ProtocolError("instance constructor/prototype own relation is absent, changed or too broad")
        return ctor, source

    def query_instances(self, prototype):
        if self.heap_queries >= MAX_HEAP_QUERIES:
            raise ProtocolError("instance query exceeds its separate heap-query budget")
        self.heap_queries += 1
        result = self.page.call("Runtime.queryObjects", {"prototypeObjectId": prototype["objectId"], "objectGroup": self.group})
        array = result.get("objects")
        if not isinstance(array, dict) or array.get("type") != "object" or array.get("subtype") != "array":
            raise ProtocolError("heap query omitted its fresh ordinary result array")
        _scope_remote_metadata(array)
        array = self.remember(array)
        count = self.function(array, ARRAY_COUNT, by_value=True).get("value")
        if type(count) is not int or not 0 <= count <= 4294967295:
            raise ProtocolError("heap query omitted the complete exact candidate count")
        return array, count

    def instance_candidates(self, array, count):
        rows = self.scope_properties(array)["result"]
        if len(rows) != count + 1:
            raise ProtocolError("complete heap query descriptors do not fit the exact bounded array count")
        found, length = {}, False
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str) or
                    "get" in row or "set" in row or "symbol" in row or row.get("wasThrown", False) is not False or
                    row.get("isOwn", True) is not True or not isinstance(row.get("value"), dict)):
                raise ProtocolError("heap query result contains an unsupported array descriptor")
            name = row["name"]
            if name == "length":
                if length or row["value"].get("type") != "number" or type(row["value"].get("value")) is not int or row["value"]["value"] != count:
                    raise ProtocolError("heap query array length descriptor changed")
                length = True
                continue
            if (len(name) > 2 or re.fullmatch(r"0|[1-9][0-9]*", name) is None or
                    int(name) >= count or int(name) in found):
                raise ProtocolError("heap query candidate descriptors are ambiguous or not canonical")
            value = row["value"]
            _scope_remote_metadata(value)
            if value.get("type") not in {"object", "function"} or value.get("subtype") in {"proxy", "null"}:
                raise ProtocolError("heap query candidate is a proxy or not an actual object")
            found[int(name)] = self.remember(value)
        if not length or sorted(found) != list(range(count)):
            raise ProtocolError("heap query omitted part of its complete bounded candidate set")
        return [found[index] for index in range(count)]

    def direct_instance(self, candidate, prototype):
        actual = self.function(candidate, DIRECT_PROTOTYPE, by_value=True, object_argument=prototype).get("value")
        if type(actual) is not bool:
            raise ProtocolError("direct prototype observation did not return an exact boolean")
        return actual

    def prototype_instances(self, prototype, operation):
        self.inspect_prototype(prototype, operation["constructor_sha256"])
        array, count = self.query_instances(prototype)
        output = []
        for index in range(min(count, operation["max_instances"])):
            candidate = self.function(array, OWN_PROPERTY, [str(index)])
            metadata = _scope_remote_metadata(candidate)
            if candidate.get("type") not in {"object", "function"} or candidate.get("subtype") in {"proxy", "null"}:
                output.append({"metadata": metadata, "supported": False, "unsupported_reason": "not_an_ordinary_nonproxy_object"})
                continue
            direct = self.direct_instance(candidate, prototype)
            item = {"metadata": metadata, "supported": True, "direct_prototype": direct}
            if direct:
                rows, _ = _scope_binding_rows(self.scope_properties(candidate)["result"])
                instance_candidate = not any(metadata["name"] == "constructor" for _, metadata in rows)
                item["instance_candidate"] = instance_candidate
                if not instance_candidate:
                    item.update(supported=False, unsupported_reason="own_constructor_descriptor")
                properties = []
                for row, value in rows[:operation["max_properties"]]:
                    preview = copy.deepcopy(value)
                    if value["kind"] == "data" and value.get("value_supported"):
                        remote = row["value"]
                        kind = {"string": "utf8", "number": "number", "boolean": "bool"}.get(remote["type"])
                        if remote["type"] == "object" and remote.get("subtype") == "null":
                            kind = "null"
                        if kind and (kind != "utf8" or _text(remote["value"], 1024, empty=True)):
                            preview["literal_invariant"] = {"name": row["name"], "type": kind, "equals": remote["value"]}
                    properties.append(preview)
                item.update(property_count=len(rows), properties=properties,
                            properties_truncated=len(rows) > operation["max_properties"])
            output.append(item)
        return {"op": "prototype_instances", "constructor_sha256": operation["constructor_sha256"],
                "complete_candidate_count": count, "instances": output, "instances_truncated": count > len(output),
                "selection_indices_exported": False, "live_ids_exported": False, "subclasses_selected": False,
                "heap_scan": True, "forced_gc_possible": True, "objects_retained_until_group_release": True,
                "server_side_candidate_bound": False, "task_result_verified": False}

    def unique_instance(self, prototype, step):
        ctor, source = self.inspect_prototype(prototype, step["constructor_sha256"])
        array, count = self.query_instances(prototype)
        if count > step["max_candidates"] or not step["own_invariants"] and count != 1:
            raise ProtocolError("complete instance candidate count exceeds its budget or needs literal disambiguation")
        matches = []
        for candidate in self.instance_candidates(array, count):
            if not self.direct_instance(candidate, prototype):
                continue
            match = self.function(candidate, OWN_INVARIANTS, [step["own_invariants"]], by_value=True).get("value")
            if type(match) is not bool:
                raise ProtocolError("own literal invariant observation did not return an exact boolean")
            if match:
                matches.append(candidate)
        if len(matches) != 1:
            raise ProtocolError("literal own invariants did not identify exactly one direct-prototype instance")
        self.instance_guards.append({"candidate": matches[0], "constructor": ctor, "prototype": prototype,
                                     "constructor_source": source, "own_invariants": copy.deepcopy(step["own_invariants"])})
        self.instance_selection_modes.append("literal_own_invariants" if step["own_invariants"] else "snapshot_singleton")
        return matches[0]

    def dom_node_ids(self, selector):
        """Fresh protocol-owned nodes only, never persisted as a recipe."""
        document = self.page.call("DOM.getDocument", {"depth": 0, "pierce": False}).get("root")
        if (not isinstance(document, dict) or type(document.get("nodeId")) is not int or
                not 0 < document["nodeId"] < 2**31 or type(document.get("nodeType")) is not int or document["nodeType"] != 9):
            raise ProtocolError("DOM metadata omitted its exact bounded document node")
        node_ids = self.page.call("DOM.querySelectorAll", {"nodeId": document["nodeId"],
                                  "selector": selector}).get("nodeIds")
        if (not isinstance(node_ids, list) or len(node_ids) > MAX_DOM_MATCHES or
                any(type(node_id) is not int or not 0 < node_id < 2**31 for node_id in node_ids) or
                len(set(node_ids)) != len(node_ids)):
            raise ProtocolError("DOM metadata query returned malformed, duplicated or oversized node identities")
        return node_ids

    def dom_query(self, operation):
        """Standard DOM metadata only: no global resolution or app evaluation."""
        node_ids = self.dom_node_ids(operation["selector"])
        rows, truncated = [], len(node_ids) > operation["max_nodes"]
        for node_id in node_ids[:operation["max_nodes"]]:
            node = self.page.call("DOM.describeNode", {"nodeId": node_id, "depth": 0, "pierce": False}).get("node")
            if (not isinstance(node, dict) or type(node.get("nodeType")) is not int or node["nodeType"] != 1 or
                    not _text(node.get("nodeName"), 128) or
                    "nodeId" in node and (type(node["nodeId"]) is not int or node["nodeId"] not in {0, node_id})):
                raise ProtocolError("DOM metadata returned an invalid selected element description")
            attributes = node.get("attributes", [])
            if (not isinstance(attributes, list) or len(attributes) % 2 or len(attributes) > MAX_DOM_ATTRIBUTES * 2 or
                    any(not isinstance(item, str) for item in attributes)):
                raise ProtocolError("DOM metadata attributes must be bounded literal name/value pairs")
            names, pairs = set(), []
            attribute_truncated = len(attributes) > MAX_DOM_ATTRIBUTE_OUTPUT * 2
            for index in range(0, len(attributes), 2):
                name, value = attributes[index:index + 2]
                if not _text(name, 256) or name in names or "\0" in value:
                    raise ProtocolError("DOM metadata attribute name/value is malformed or duplicated")
                names.add(name)
                # Refuse malformed UTF-8 rather than putting unsafe text into a
                # receipt. Truncated values are preview metadata, not selectors.
                raw = value.encode("utf-8")
                if index >= MAX_DOM_ATTRIBUTE_OUTPUT * 2:
                    continue
                if len(raw) > 1024:
                    raw = raw[:1024]
                    attribute_truncated = True
                pairs.append([name, raw.decode("utf-8", errors="ignore")])
            count = node.get("childNodeCount")
            if "childNodeCount" in node and (type(count) is not int or not 0 <= count < 2**31):
                raise ProtocolError("DOM metadata child count is not a bounded integer")
            rows.append({"tag": node["nodeName"], "attributes": pairs, "attribute_count": len(attributes) // 2,
                         "attributes_truncated": attribute_truncated, "child_count": count})
            truncated = truncated or attribute_truncated
        return {"op": "dom_query", "selector": operation["selector"], "total_match_count": len(node_ids),
                "nodes": rows, "truncated": truncated, "live_node_ids_exported": False,
                "app_evaluations": 0, "getters_invoked": 0}

    def resolve(self, recipe):
        root = recipe["root"]
        if root["kind"] == "dom":
            self.dom_root_started = True
            nodes = self.dom_node_ids(root["selector"])
            if len(nodes) != 1:
                raise DOMRootNotUnique(root["selector"], len(nodes))
            # Keep all wrapper-resolution/later Runtime failures conservative.
            # The sticky flag is never reset, including between operations.
            self.evaluation_started = True
            context = self.page.bound_context
            if (not isinstance(context, dict) or context.get("unique_id") != self.context or
                    type(context.get("numeric_id")) is not int or context["numeric_id"] <= 0):
                raise ProtocolError("DOM resolution needs the exact bound execution context")
            remote = self.page.call("DOM.resolveNode", {"nodeId": nodes[0], "executionContextId": context["numeric_id"],
                                                       "objectGroup": self.group}).get("object")
            if not isinstance(remote, dict) or remote.get("type") != "object" or remote.get("subtype") != "node":
                raise ProtocolError("DOM resolution omitted an actual node wrapper")
            _scope_remote_metadata(remote)
            value = self.remember(remote)
        else:
            self.evaluation_started = True
            self.runtime_evaluation_attempts += 1
            value = self.remember(_remote(self.page.call("Runtime.evaluate", {"expression": GLOBAL, "uniqueContextId": self.context,
                "objectGroup": self.group, "returnByValue": False, "silent": True}), fixed_template="GLOBAL"))
            if root["name"] != "globalThis":
                value = self.function(value, PROPERTY, [root["name"]])
        for step in recipe["steps"]:
            if step["kind"] == "unique_instance":
                value = self.unique_instance(value, step)
            elif step["kind"] == "scope_binding":
                value = self.scope_binding(value, step)
            elif step["kind"] == "private_property":
                value = self.private_value(value, step["name"])
            elif step["kind"] == "map_key":
                value = self.function(value, MAP_KEY, [step["key"]["value"]])
            elif step["kind"] == "property" and step["access"] == "getter":
                actual = self.function(value, GETTER_SOURCE, [step["name"]], by_value=True).get("value")
                if not _text(actual) or hashlib.sha256(actual.encode()).hexdigest() != step["getter_sha256"]:
                    raise ProtocolError("getter source changed before invocation")
                self.getter_attempts += 1
                value = self.function(value, GETTER, [step["name"], actual])
            else:
                name = str(step["index"]) if step["kind"] == "index" else step["name"]
                value = self.function(value, PROPERTY, [name])
        return value

    def operation(self, operation):
        self.instance_guards = []
        op = operation["op"]
        if op == "dom_query":
            return self.dom_query(operation)
        actor = operation.get("actor")
        value = self.resolve(actor["receiver"] if actor else operation["recipe"])
        if op == "function_scopes":
            return self.function_scopes(value, operation)
        if op == "prototype_instances":
            return self.prototype_instances(value, operation)
        if op == "map_keys":
            returned = self.function(value, MAP_KEYS, [operation["max_keys"]], by_value=True)["value"]
            if (not isinstance(returned, dict) or set(returned) != {"keys", "unsupported_key_count", "truncated"} or
                    not isinstance(returned["keys"], list) or len(returned["keys"]) > operation["max_keys"] or
                    type(returned["unsupported_key_count"]) is not int or returned["unsupported_key_count"] < 0 or
                    type(returned["truncated"]) is not bool):
                raise ProtocolError("malformed bounded map-key observation")
            for key in returned["keys"]:
                _keys(key, {"type", "value"})
                key["type"] = {"string": "utf8", "boolean": "bool", "number": "number", "null": "null"}.get(key["type"])
                _typed(key["type"], key["value"])
            return {"op": op, **returned, "live_object_keys_exported": False}
        if op == "read":
            if "objectId" in value:
                result = self.function(value, READ, [operation["return_type"]], by_value=True)["value"]
                actual = result["value"]
            else:
                if "value" not in value or value.get("type") == "undefined":
                    raise ProtocolError("inspector read omitted an actual primitive value")
                actual = value["value"]
            _typed(operation["return_type"], actual)
            return {"op": op, "value": actual, "type": operation["return_type"]}
        if op in {"method", "call"}:
            name = actor["method"] if actor else operation["method"]
            if op == "method" and not name.startswith("#"):
                if value.get("subtype") == "proxy":
                    raise ProtocolError("method metadata cannot resolve an inspector proxy wrapper")
                measured = self.function(value, METHOD_DISCOVERY, [name], by_value=True)["value"]
                if type(measured) is not dict:
                    raise ProtocolError("malformed fixed method-discovery result")
                if measured.get("status") == "not_found":
                    if (set(measured) != {"status", "prototype_depth"} or
                            type(measured["prototype_depth"]) is not int or not 1 <= measured["prototype_depth"] <= 16):
                        raise ProtocolError("malformed bounded method-absence result")
                    return {"op": "method", "method": name, "status": "not_found",
                            "lookup_scope": "own_and_prototype_data_descriptors",
                            "prototype_depth": measured["prototype_depth"], "complete_prototype_chain": True,
                            "callable": False, "getters_invoked": 0, "types_recovered_automatically": False}
                if set(measured) != {"status", "function_source"} or measured["status"] != "found":
                    raise ProtocolError("malformed fixed method-discovery result")
                actual = measured["function_source"]
            else:
                method = self.private_value(value, name) if name.startswith("#") else self.function(value, PROPERTY, [name])
                actual = self.function(method, FUNCTION_SOURCE, by_value=True)["value"]
            if not _text(actual):
                raise ProtocolError("measured function source is unavailable or oversized")
            sha = hashlib.sha256(actual.encode()).hexdigest()
            if op == "method":
                return {"op": op, "method": name, "function_source": actual, "function_sha256": sha,
                        "types_recovered_automatically": False}
            if sha != actor["function_sha256"]:
                raise ProtocolError("method source changed before invocation")
            if "[native code]" in actual:
                raise ProtocolError("native/bound/proxy functions lack attributable JavaScript body; no invocation")
            self.method_attempts += 1
            guards = [{"constructor_source": guard["constructor_source"], "own_invariants": guard["own_invariants"]}
                      for guard in self.instance_guards]
            guard_objects = [guard[key] for guard in self.instance_guards for key in ("candidate", "constructor", "prototype")]
            returned = self.function(method, CALL, [[argument["value"] for argument in actor["arguments"]],
                [argument["type"] for argument in actor["arguments"]], actual, actor["return_type"], guards],
                by_value=True, object_argument=value, extra_object_arguments=guard_objects)["value"]
            if not isinstance(returned, dict) or returned.get("completed") is not True or set(returned) != {"completed", "value"}:
                raise ProtocolError("method did not return its fixed completion envelope")
            _typed(actor["return_type"], returned["value"], result=True)
            return {"op": op, "method": name, "function_sha256": sha, "value": returned["value"],
                    "return_type": actor["return_type"], "completed": True, "body_entry_independently_proved": False}
        if op == "describe":
            arguments = [operation["max_properties"], operation.get("prototype_depth", 1)]
            if "offset" in operation or "property_query" in operation:
                arguments.extend([operation.get("offset", 0), operation.get("property_query", "")])
            result = self.function(value, DESCRIBE, arguments, by_value=True)["value"]
            for row in result.get("properties", []):
                for name in ("function_source", "getter_source"):
                    if name in row:
                        row[name + "_sha256"] = hashlib.sha256(row[name].encode()).hexdigest()
            private = self.private_properties(value)
            result["private_properties"] = [{"name": row.get("name"),
                "type": row.get("value", {}).get("type"), "getter": "get" in row,
                "setter": "set" in row} for row in private[:operation["max_properties"]]]
            result["private_properties_truncated"] = len(private) > operation["max_properties"]
            return {"op": op, **result}
        result = self.function(value, GRAPH, [operation.get("max_nodes", 16), operation.get("max_depth", 2),
                                           operation["max_properties"]], by_value=True)["value"]
        return {"op": op, **result}


def collect(bundle, out, request, *, timeout=30, config_file=None, expected_identity=None):
    R.assert_guest()
    expected = B._expected_identity(expected_identity)
    request = validate_request(request)
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("inspector timeout must be 1–60 seconds")
    app = Path(bundle).resolve(strict=True)
    target = R.fingerprint(app)
    if expected is not None and (expected["bundle"] != str(app) or expected["bundle_id"] != target["bundle_id"]):
        raise ValueError("expected inspector identity must name this exact app bundle")
    pinned = {}
    for operation in request["operations"]:
        if operation["op"] == "call":
            pinned.update(_invocation_resources(operation["actor"]))
    config, config_pin = _config(app, target, config_file)
    folder = Path(out).resolve()
    if folder.is_relative_to(app) or any(folder.is_relative_to(Path(path)) for path in [*pinned, config_pin["file"]]):
        raise ValueError("inspector evidence must be outside app/config/declaration files")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    deadline = time.monotonic() + timeout
    receipt = {"format": FORMAT, "status": "incomplete", "bundle": str(app), "target": target,
               "request": request, "request_sha256": R.digest(request), "adapter": adapter_identity(),
               "transport": _transport(config, config_pin),
               "protocol_limits": _protocol_limits(),
               "declaration_resources": pinned, "operations": [], "actuated": False,
               "state_uncertain": False, "resource_review_required": False, "retry": False,
               "proof_created": False, "task_result_verified": False, "model_calls": 0,
               "cleanup": {"object_group_released": False, "page_socket_closed": False, "browser_socket_closed": False,
                           "requested_evaluations_completed": False, "remote_execution_cancelled": False,
                           "local_live_ids_discarded": True}}
    if expected is not None:
        receipt["expected_identity"] = dict(expected)
    page = browser = transaction = None
    known_preflight = False
    group = "anyuse-rea-" + uuid.uuid4().hex
    try:
        main = B.process_identity(app, target["bundle_id"])
        receipt["identity"] = main
        if expected is not None and main != expected:
            raise Unavailable("inspector process differs from the caller's expected identity; no successor adoption")
        listener = _listener(config["endpoint"], main, deadline)
        browser_url, selected = _select_target(app, config, deadline)
        browser = _CDP(_socket(browser_url, deadline), deadline)
        renderers = _renderer_processes(browser, app, main, deadline)
        receipt["endpoint_identity"] = {"listener": listener, "target": {key: selected[key] for key in ("id", "type", "url")},
                                        "renderer_processes": renderers,
                                        "renderer_binding_scope": "selected exact app URL plus owned app descendant renderer set"}
        page = _CDP(_socket(selected["webSocketDebuggerUrl"], deadline), deadline)
        frame = page.call("Page.getFrameTree").get("frameTree", {}).get("frame", {})
        if not _text(frame.get("id"), 128) or _effective_frame_url(frame) != selected["url"]:
            raise Unavailable("inspector frame differs from the exact selected app renderer")
        receipt["endpoint_identity"]["frame"] = copy.deepcopy(frame)
        page.call("Runtime.enable")
        if page.call("Page.getFrameTree").get("frameTree", {}).get("frame") != frame:
            raise Unavailable("app frame changed before context selection")
        contexts = [event.get("params", {}).get("context", {}) for event in page.events
                    if event.get("method") == "Runtime.executionContextCreated"]
        contexts = [context for context in contexts if context.get("auxData", {}).get("isDefault") is True and
                    context.get("auxData", {}).get("frameId") == frame["id"] and
                    type(context.get("id")) is int and context["id"] > 0]
        if len(contexts) != 1 or not _text(contexts[0].get("uniqueId"), 256):
            raise Unavailable("inspector needs one exact default app execution context")
        page.bound_context = {"unique_id": contexts[0]["uniqueId"], "numeric_id": contexts[0]["id"],
                              "frame_id": frame["id"], "frame_url": _effective_frame_url(frame), "is_default": True}
        receipt["endpoint_identity"]["execution_context"] = copy.deepcopy(page.bound_context)
        transaction = _Transaction(page, contexts[0]["uniqueId"], group)
        def unchanged_context():
            after = B.process_identity(app, target["bundle_id"])
            if (after != main or R.fingerprint(app) != target or
                    _config(app, target, config_pin["file"]) != (config, config_pin) or
                    adapter_identity() != receipt["adapter"] or
                    _listener(config["endpoint"], main, deadline) != listener or
                    _select_target(app, config, deadline) != (browser_url, selected) or
                    page.call("Page.getFrameTree").get("frameTree", {}).get("frame") != frame or
                    _renderer_processes(browser, app, main, deadline) != renderers):
                raise Unavailable("app bytes, endpoint or owned renderer process set changed during evaluation")
            return after
        for operation in request["operations"]:
            if (_config(app, target, config_pin["file"]) != (config, config_pin) or
                    adapter_identity() != receipt["adapter"] or R.fingerprint(app) != target):
                raise Unavailable("app bytes, operator configuration or fixed adapter changed before operation")
            if B.process_identity(app, target["bundle_id"]) != main or _listener(config["endpoint"], main, deadline) != listener:
                raise Unavailable("owned app/listener changed before inspector operation")
            if _select_target(app, config, deadline) != (browser_url, selected):
                raise Unavailable("inspector target changed before operation; no stale-ID retry")
            if page.call("Page.getFrameTree").get("frameTree", {}).get("frame") != frame:
                raise Unavailable("full app frame changed before operation; no stale-ID retry")
            if operation["op"] == "call":
                current_resources = _invocation_resources(operation["actor"])
                if any(pinned.get(path) != entry for path, entry in current_resources.items()):
                    raise ValueError("reviewed inspector declaration changed before invocation")
                if expected is not None and B.process_identity(app, target["bundle_id"]) != expected:
                    raise Unavailable("expected inspector process changed immediately before the task call")
            output = transaction.operation(operation)
            if len(json.dumps([*receipt["operations"], output], ensure_ascii=False, allow_nan=False).encode()) > MAX_MESSAGE:
                raise ProtocolError("inspector operation output exceeds its retained evidence budget")
            receipt["operations"].append(output)
        receipt["after_identity"] = unchanged_context()
        for operation in request["operations"]:
            if operation["op"] == "call" and any(pinned.get(path) != entry
                    for path, entry in _invocation_resources(operation["actor"]).items()):
                raise Unavailable("reviewed inspector declaration changed during evaluation")
        receipt["status"] = "executed" if transaction.method_attempts or transaction.getter_attempts else "observed"
        receipt["cleanup"]["requested_evaluations_completed"] = not page.pending
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[:1024]}
        if isinstance(error, ProtocolError) and error.remote_exception is not None:
            receipt["error"]["remote_exception"] = copy.deepcopy(error.remote_exception)
        if isinstance(error, ProtocolError) and error.command_error is not None:
            receipt["error"]["command_error"] = copy.deepcopy(error.command_error)
        if isinstance(error, DOMRootNotUnique):
            receipt["preflight_failure"] = {"format": "anyuse.rea-inspector-preflight-failure/1",
                "kind": "dom_root_not_unique", "operation_index": len(receipt["operations"]),
                "selector": error.selector, "match_count": error.match_count,
                "context_revalidated": False, "live_node_ids_exported": False}
            if (transaction is not None and not transaction.evaluation_started and
                    transaction.runtime_evaluation_attempts == transaction.method_attempts == transaction.getter_attempts == 0):
                try:
                    receipt["after_identity"] = unchanged_context()
                    if page.pending or page.invalid_context:
                        raise Unavailable("preflight context has pending or expired protocol work")
                    receipt["preflight_failure"]["context_revalidated"] = True
                    # No Runtime evaluations were requested; all protocol
                    # requests used for this typed rejection have completed.
                    receipt["cleanup"]["requested_evaluations_completed"] = True
                    known_preflight = True
                except Exception as revalidation:
                    receipt["preflight_revalidation_error"] = {"type": type(revalidation).__name__, "message": str(revalidation)[:1024]}
        receipt["state_uncertain"] = bool(transaction and (transaction.evaluation_started or
                                                          transaction.dom_root_started and not known_preflight))
        receipt["resource_review_required"] = receipt["state_uncertain"]
    finally:
        receipt["heap_query_effects"] = _heap_effects(transaction.heap_queries if transaction else 0)
        receipt["instance_selection_modes"] = copy.deepcopy(transaction.instance_selection_modes) if transaction else []
        receipt["method_dispatch_attempts"] = transaction.method_attempts if transaction else 0
        receipt["getter_dispatch_attempts"] = transaction.getter_attempts if transaction else 0
        receipt["runtime_evaluation_attempts"] = transaction.runtime_evaluation_attempts if transaction else 0
        receipt["actuated"] = bool(receipt["method_dispatch_attempts"] or receipt["getter_dispatch_attempts"])
        if receipt["state_uncertain"]:
            receipt["actuated"] = None
        receipt["descriptor_traps_may_have_effects"] = bool(transaction and transaction.evaluation_started)
        if page:
            try:
                page.call("Runtime.releaseObjectGroup", {"objectGroup": group}, cleanup=True)
                receipt["cleanup"]["object_group_released"] = True
            except Exception:
                receipt["resource_review_required"] = True
            try:
                page.close()
                receipt["cleanup"]["page_socket_closed"] = True
            except Exception:
                receipt["resource_review_required"] = True
        if browser:
            try:
                browser.close()
                receipt["cleanup"]["browser_socket_closed"] = True
            except Exception:
                receipt["resource_review_required"] = True
        if page and page.invalid_context and transaction and transaction.evaluation_started:
            receipt["state_uncertain"] = True
            receipt["actuated"] = None
            receipt["resource_review_required"] = True
        if known_preflight and (page is None or page.invalid_context or page.pending or
                receipt["cleanup"] != {"object_group_released": True, "page_socket_closed": True,
                    "browser_socket_closed": True, "requested_evaluations_completed": True,
                    "remote_execution_cancelled": False, "local_live_ids_discarded": True}):
            receipt["state_uncertain"] = True
            receipt["actuated"] = None
            receipt["resource_review_required"] = True
        if receipt["resource_review_required"]:
            receipt["status"] = "incomplete"
        if transaction:
            transaction.live_ids.clear()
            transaction.instance_guards.clear()
        receipt["rpc_count"] = sum(channel.next_id for channel in (page, browser) if channel)
        receipt["event_summary"] = {name: channel.event_summary() for name, channel in
                                    (("page", page), ("browser", browser)) if channel is not None}
        receipt["elapsed_s"] = round(timeout - max(0, deadline - time.monotonic()), 4)
        R.write_json(folder / "inspector.json", receipt)
    return receipt


def invocation_request(actor):
    return validate_request({"operations": [{"op": "call", "actor": validate_actor(actor)}]})


def invoke(bundle, actor, out, *, timeout=30, config_file=None, expected_identity=None):
    options = {"expected_identity": expected_identity} if expected_identity is not None else {}
    return collect(bundle, out, invocation_request(actor), timeout=timeout, config_file=config_file, **options)


def receipt_valid(receipt, actor, target, adapter, transport, *, check_files=True):
    """Validate this provider's invocation envelope, separately from task output.

    A saved proof passes its own pinned adapter/transport identities. The
    caller still needs independent complete result observations and immutable
    receipt hashes; this local evidence is not a signature or a task verdict.
    """
    try:
        actor = validate_actor(actor)
        if (not isinstance(receipt, dict) or receipt.get("format") != FORMAT or receipt.get("status") != "executed" or
                receipt.get("target") != target or receipt.get("adapter") != adapter or
                receipt.get("transport") != transport or receipt.get("request") != invocation_request(actor) or
                not isinstance(adapter, dict) or receipt.get("protocol_limits") != adapter.get("protocol_limits") or
                receipt.get("request_sha256") != R.digest(invocation_request(actor)) or
                receipt.get("state_uncertain") is not False or receipt.get("resource_review_required") is not False or
                receipt.get("proof_created") is not False or receipt.get("task_result_verified") is not False or
                receipt.get("retry") is not False or receipt.get("actuated") is not True or receipt.get("model_calls") != 0 or
                type(receipt.get("model_calls")) is not int or
                type(receipt.get("method_dispatch_attempts")) is not int or receipt["method_dispatch_attempts"] != 1):
            return False
        expected_getters = sum(step["kind"] == "property" and step["access"] == "getter"
                               for step in actor["receiver"]["steps"])
        selections = [step for step in actor["receiver"]["steps"] if step["kind"] == "unique_instance"]
        modes = ["literal_own_invariants" if step["own_invariants"] else "snapshot_singleton" for step in selections]
        if (receipt.get("heap_query_effects") != _heap_effects(len(selections)) or
                receipt.get("instance_selection_modes") != modes):
            return False
        if type(receipt.get("getter_dispatch_attempts")) is not int or receipt["getter_dispatch_attempts"] != expected_getters:
            return False
        expected_cleanup = {"object_group_released": True, "page_socket_closed": True,
            "browser_socket_closed": True, "requested_evaluations_completed": True,
            "remote_execution_cancelled": False, "local_live_ids_discarded": True}
        if receipt.get("cleanup") != expected_cleanup:
            return False
        main = receipt.get("identity")
        if (not isinstance(main, dict) or main != receipt.get("after_identity") or
                set(main) != {"bundle_id", "bundle", "executable", "pid", "launch_time", "uid"} or
                main["bundle_id"] != target["bundle_id"] or main["bundle"] != receipt.get("bundle") or
                type(main["pid"]) is not int or main["pid"] <= 0 or type(main["uid"]) is not int or main["uid"] < 0 or
                type(main["launch_time"]) not in {int, float} or not math.isfinite(main["launch_time"]) or main["launch_time"] <= 0 or
                not Path(main["bundle"]).is_absolute() or not Path(main["executable"]).is_relative_to(Path(main["bundle"]))):
            return False
        if "expected_identity" in receipt and dict(B._expected_identity(receipt["expected_identity"])) != main:
            return False
        if transport.get("kind") != "chromium_renderer_cdp":
            return False
        _keys(transport, {"kind", "endpoint", "relative_url", "config"}, {"document_suffix"})
        suffix = _document_suffix(transport.get("document_suffix", {"query": "", "fragment": ""}))
        host, port = _endpoint(transport["endpoint"])
        endpoint = receipt.get("endpoint_identity", {})
        listener = endpoint.get("listener", {})
        if (listener.get("pid") != main["pid"] or listener.get("uid") != main["uid"] or
                not isinstance(listener.get("listen_addresses"), list) or not listener["listen_addresses"] or
                any(name not in {f"127.0.0.1:{port}", f"[::1]:{port}"} for name in listener["listen_addresses"])):
            return False
        selected = endpoint.get("target", {})
        parsed = urlsplit(selected.get("url", ""))
        if (selected.get("type") != "page" or not _text(selected.get("id"), 128) or parsed.scheme != "file" or
                parsed.netloc not in {"", "localhost"} or
                parsed.query != suffix["query"] or parsed.fragment != suffix["fragment"] or
                Path(unquote(parsed.path)).resolve() != (Path(main["bundle"]) / transport["relative_url"]).resolve() or
                endpoint.get("renderer_binding_scope") != "selected exact app URL plus owned app descendant renderer set"):
            return False
        context = endpoint.get("execution_context")
        if (not isinstance(context, dict) or
                set(context) != {"unique_id", "numeric_id", "frame_id", "frame_url", "is_default"} or
                not _text(context["unique_id"], 256) or not _text(context["frame_id"], 128) or
                type(context["numeric_id"]) is not int or context["numeric_id"] <= 0 or
                context["frame_url"] != selected["url"] or context["is_default"] is not True):
            return False
        if "frame" in endpoint:
            raw_frame = endpoint["frame"]
            if (not isinstance(raw_frame, dict) or raw_frame.get("id") != context["frame_id"] or
                    _effective_frame_url(raw_frame) != context["frame_url"]):
                return False
        renderers = endpoint.get("renderer_processes")
        if not isinstance(renderers, list) or not 1 <= len(renderers) <= 16:
            return False
        if any(not isinstance(renderer, dict) for renderer in renderers):
            return False
        if len({renderer.get("pid") for renderer in renderers}) != len(renderers):
            return False
        for renderer in renderers:
            if (set(renderer) != {"pid", "ppid", "uid", "started_at", "executable"} or
                    type(renderer["pid"]) is not int or renderer["pid"] <= 0 or type(renderer["ppid"]) is not int or
                    renderer["ppid"] <= 0 or renderer["uid"] != main["uid"] or not _text(renderer["started_at"], 128) or
                    not Path(renderer["executable"]).is_relative_to(Path(main["bundle"]))):
                return False
        rows = receipt.get("operations")
        if not isinstance(rows, list) or len(rows) != 1:
            return False
        row = rows[0]
        if (set(row) != {"op", "method", "function_sha256", "value", "return_type", "completed", "body_entry_independently_proved"} or
                row["op"] != "call" or row["method"] != actor["method"] or row["function_sha256"] != actor["function_sha256"] or
                row["return_type"] != actor["return_type"] or row["completed"] is not True or
                row["body_entry_independently_proved"] is not False):
            return False
        _typed(actor["return_type"], row["value"], result=True)
        if check_files:
            R.assert_guest()
            if (adapter_identity() != adapter or transport_identity(main["bundle"], config_file=transport["config"]["file"]) != transport or
                    _invocation_resources(actor) != receipt.get("declaration_resources")):
                return False
        else:
            evidence = actor["signature_evidence"]
            entries = receipt.get("declaration_resources", {})
            declared = evidence["kind"] == "declared_js"
            if (not isinstance(entries, dict) or evidence["file"] not in entries or
                    (not declared and set(entries) != {evidence["file"]}) or
                    (declared and not 1 <= len(entries) <= 64)):
                return False
            if declared and any(type(pin) is not dict or set(pin) != {"file", "sha256", "bytes"} or
                    pin["file"] != path or not Path(path).is_absolute() or not R.SHA.fullmatch(str(pin["sha256"])) or
                    type(pin["bytes"]) is not int or not 0 < pin["bytes"] <= R.MAX_JSON_BYTES
                    for path, pin in entries.items()):
                return False
            entry = entries[evidence["file"]]
            if (set(entry) != {"file", "sha256", "bytes"} or entry["file"] != evidence["file"] or
                    entry["sha256"] != evidence["sha256"] or type(entry["bytes"]) is not int or entry["bytes"] <= 0):
                return False
        return True
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False
