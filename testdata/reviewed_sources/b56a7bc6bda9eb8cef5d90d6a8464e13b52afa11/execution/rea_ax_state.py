"""VM-only, read-only complete AX window states; no actor or task proof.

Observation context keeps the complete current process identity, kernel epoch,
session, actual focus PIDs, display IDs and window-list index. Semantic state
keeps every selected-window node/attribute/availability/geometry, every display
rectangle and owner-relative focus. Only the versioned semantic schema, not an
ad hoc filter over a receipt, permits exact comparisons across app restarts.

The supplied target fingerprint is the caller's frozen resource identity;
this observer checks the current NS/kernel process before and after reading,
but does not re-hash an app bundle. AX exposes a tree, not arbitrary internal
or pixel state. Read messages can make the app execute its ordinary AX code.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import PurePosixPath
import re
import time

from . import rea_mapping as R

OBSERVATION = "anyuse.rea-ax-observation/1"
STATE = "anyuse.rea-ax-semantic-state/1"
CONTEXT = "anyuse.rea-ax-current-context/1"
SELECTOR = "anyuse.rea-ax-window-selector/1"
CONTRACT = "anyuse.rea-ax-state-contract/1"
SETTLEMENT = "anyuse.rea-ax-settlement/1"
MAX_NODES, MAX_DEPTH, MAX_CHILDREN, MAX_TEXT = 256, 8, 64, 1024
MAX_DISPLAYS, MAX_SECONDS, AX_SECONDS, MAX_BYTES = 16, 10, 0.5, 2 * 1024**2
MAX_SAMPLES, MAX_OBSERVATION_BYTES = 16, 8 * 1024**2
ATTRS = {"role": "AXRole", "subrole": "AXSubrole", "title": "AXTitle",
         "description": "AXDescription", "enabled": "AXEnabled", "value": "AXValue",
         "position": "AXPosition", "size": "AXSize", "children": "AXChildren"}
AVAILABILITY = {"present", "unsupported", "no_value"}


def _require(value, message):
    if not value: raise ValueError(message)


def _keys(value, keys):
    _require(type(value) is dict and set(value) == set(keys), "closed AX data schema required")


def _same(left, right):
    if type(left) is not type(right): return False
    if type(left) is dict:
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _text(value, *, empty=True, maximum=MAX_TEXT):
    return type(value) is str and (empty or bool(value)) and "\0" not in value and len(value) <= maximum


def _vector(value, *, size=False):
    _require(type(value) in (list, tuple) and len(value) == 2 and
             all(type(v) in (int, float) and math.isfinite(v) and (not size or v >= 0) for v in value),
             "finite typed AX geometry required")
    return [float(v) for v in value]


def validate_target(value):
    _keys(value, {"bundle_id", "version", "build", "sha256", "files", "bytes"})
    _require(_text(value["bundle_id"], empty=False, maximum=512) and
             all(_text(value[key], maximum=512) for key in ("version", "build")) and
             type(value["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) and
             all(type(value[key]) is int and value[key] >= 0 for key in ("files", "bytes")),
             "complete frozen AX target fingerprint required")
    return copy.deepcopy(value)


def validate_identity(value):
    """Pure validation of all current process fields; never resolve a new PID."""
    _keys(value, {"bundle", "bundle_id", "executable", "launch_time", "pid", "uid"})
    for key in ("bundle", "executable"):
        text = value[key]
        _require(_text(text, empty=False, maximum=4096) and text.startswith("/") and not text.startswith("//") and
                 str(PurePosixPath(text)) == text and ".." not in PurePosixPath(text).parts and
                 all(ord(char) >= 32 and ord(char) != 127 for char in text), "canonical AX process path required")
    _require(_text(value["bundle_id"], empty=False, maximum=512) and
             type(value["pid"]) is int and 1 <= value["pid"] <= 2**31 - 1 and
             type(value["uid"]) is int and 0 <= value["uid"] < 2**32 - 1 and
             type(value["launch_time"]) in (int, float) and 0 < value["launch_time"] < 253402300800 and
             math.isfinite(value["launch_time"]), "complete typed current AX process identity required")
    _require(PurePosixPath(value["executable"]).is_relative_to(PurePosixPath(value["bundle"])),
             "AX executable must be within the declared bundle")
    return copy.deepcopy(value)


def validate_selector(value):
    _keys(value, {"format", "role", "subrole", "title", "geometry"})
    _require(value["format"] == SELECTOR and value["role"] == "AXWindow" and
             (value["subrole"] is None or _text(value["subrole"], empty=False)) and
             (value["title"] is None or _text(value["title"])), "exact owned AX window selector required")
    geometry = value["geometry"]
    _require(type(geometry) is dict, "explicit AX window geometry selector required")
    if geometry.get("kind") == "full_display":
        _keys(geometry, {"kind"})
    else:
        _keys(geometry, {"kind", "position", "size"})
        _require(geometry["kind"] == "exact", "unsupported AX geometry selection")
        _require(_same(geometry["position"], _vector(geometry["position"])) and
                 _same(geometry["size"], _vector(geometry["size"], size=True)) and
                 all(v > 0 for v in geometry["size"]), "selector geometry uses explicit finite float pairs")
    return copy.deepcopy(value)


def _native_scalar(value, F, NSNumber):
    kind = F.CFGetTypeID(value)
    if kind == F.CFBooleanGetTypeID():
        _require(isinstance(value, (bool, int)) and value in (0, 1), "known CFBoolean bridge required")
        return bool(int(value))
    _require(kind == F.CFNumberGetTypeID() and isinstance(value, NSNumber), "known CFNumber/NSNumber bridge required")
    if isinstance(value, float):
        result = float(value)
        _require(math.isfinite(result), "finite native AX number required")
        return result
    _require(isinstance(value, int) and not isinstance(value, bool), "integer native AX number required")
    result = int(value)
    _require(value == result and -(2**63) <= result <= 2**63 - 1, "exact signed64 native AX number required")
    return result


def _scalar(value, native=None):
    if value is None or type(value) is bool: return value
    if type(value) is int:
        _require(-(2**63) <= value <= 2**63 - 1, "signed64 AX integer required")
        return value
    if type(value) is float:
        _require(math.isfinite(value), "finite AX float required")
        return value
    if isinstance(value, str):
        result = str(value)
        _require(_text(result), "bounded AX string required")
        return result
    _require(native is not None, "unsupported AX scalar")
    return _scalar(native(value))  # Returned value must be a built-in scalar.


class _Fault(Exception):
    def __init__(self, kind, **detail):
        self.issue = {"kind": kind, **detail}


class _Reader:
    def __init__(self, backend, pid, deadline, clock):
        self.backend, self.pid, self.deadline, self.clock = backend, pid, deadline, clock
        self.nodes, self.handles = [], []

    def bounded(self):
        if self.clock() >= self.deadline: raise _Fault("phase_timeout")

    def own(self, element):
        self.bounded()
        self.backend.timeout(element, min(AX_SECONDS, self.deadline - self.clock()))
        self.bounded()
        if self.backend.owner(element) != self.pid: raise _Fault("element_owner_changed")
        self.bounded()

    def attr(self, element, name):
        self.own(element)
        code, value = self.backend.attribute(element, name)
        self.bounded()
        if code == -25205: return None, "unsupported"
        if code == -25212: return None, "no_value"
        if type(code) is not int or code != 0: raise _Fault("attribute_error", attribute=name, code=code if type(code) is int else None)
        return value, "present"

    def scalar(self, element, key):
        value, status = self.attr(element, ATTRS[key])
        try: value = _scalar(value, self.backend.native_scalar)
        except Exception as error: raise _Fault("unsupported_scalar", attribute=ATTRS[key], error_type=type(error).__name__) from None
        if key in {"role", "subrole", "title", "description"}:
            if value is not None and type(value) is not str: raise _Fault("attribute_type", attribute=ATTRS[key])
        if key == "role" and (status != "present" or not _text(value, empty=False)):
            raise _Fault("role_unavailable")
        if key == "enabled" and value is not None and type(value) is not bool:
            raise _Fault("attribute_type", attribute=ATTRS[key])
        return value, status

    def vector(self, element, key):
        value, status = self.attr(element, ATTRS[key])
        if value is None: return None, status
        try: value = _vector(self.backend.vector(value, key), size=key == "size")
        except Exception as error: raise _Fault("geometry_type", attribute=ATTRS[key], error_type=type(error).__name__) from None
        return value, status

    def children(self, element):
        values, status = self.attr(element, "AXChildren")
        if values is None:
            if status == "present": raise _Fault("children_type")
            return [], status
        if not self.backend.is_array(values): raise _Fault("children_type")
        try: count = len(values)
        except Exception: raise _Fault("children_type") from None
        if count > MAX_CHILDREN: raise _Fault("child_bound", reported_children=count)
        return list(values), status

    def walk(self, element, path, parent=None):
        self.own(element)
        if len(path) > MAX_DEPTH: raise _Fault("depth_bound", path=path)
        if len(self.nodes) >= MAX_NODES: raise _Fault("node_bound")
        for old in self.handles:
            self.bounded()
            if self.backend.equal(element, old): raise _Fault("cycle_or_duplicate", path=path)
        index = len(self.nodes)
        row = {"id": index, "parent": parent, "path": path, "children": [], "availability": {}}
        self.nodes.append(row)
        self.handles.append(element)
        for key in ("role", "subrole", "title", "description", "enabled", "value"):
            row[key], row["availability"][key] = self.scalar(element, key)
        for key in ("position", "size"):
            row[key], row["availability"][key] = self.vector(element, key)
        children, row["availability"]["children"] = self.children(element)
        for offset, child in enumerate(children):
            row["children"].append(self.walk(child, path + [offset], index))
        return index


def _check_context(value, identity):
    _keys(value, {"identity", "kernel", "session", "focus"})
    _require(_same(validate_identity(value["identity"]), identity), "current AX process identity changed")
    kernel = value["kernel"]
    _keys(kernel, {"pid", "ppid", "uid", "started_at", "executable"})
    _require(all(type(kernel[key]) is int for key in ("pid", "ppid", "uid")) and kernel["pid"] == identity["pid"] and
             kernel["uid"] == identity["uid"] and kernel["executable"] == identity["executable"] and
             _text(kernel["started_at"], empty=False, maximum=128), "current AX kernel owner/epoch mismatch")
    session = value["session"]
    _keys(session, {"asid", "attribute_bits", "uid", "on_console", "login_done", "locked", "ax_trusted"})
    _require(type(session["asid"]) is int and session["asid"] > 0 and type(session["attribute_bits"]) is int and
             type(session["uid"]) is int and session["uid"] == identity["uid"] and session["on_console"] is True and
             session["login_done"] is True and session["locked"] is False and session["ax_trusted"] is True and
             session["attribute_bits"] & 0x10 and not session["attribute_bits"] & 0x1000,
             "authorized unlocked same-user local AX context required")
    focus = value["focus"]
    _keys(focus, {"frontmost_pid", "focused_pid"})
    _require(all(type(focus[key]) is int and focus[key] == identity["pid"] for key in focus),
             "target must already be frontmost and focused; no activation")
    return copy.deepcopy(value)


def _displays(value):
    _require(type(value) is list and 1 <= len(value) <= MAX_DISPLAYS, "bounded full current display list required")
    rows, identifiers = [], set()
    for row in value:
        _keys(row, {"id", "position", "size"})
        _require(type(row["id"]) is int and row["id"] > 0 and row["id"] not in identifiers, "unique current display IDs required")
        identifiers.add(row["id"])
        position, size = _vector(row["position"]), _vector(row["size"], size=True)
        _require(all(v > 0 for v in size), "positive current display extent required")
        rows.append({"id": row["id"], "position": position, "size": size})
    return sorted(rows, key=lambda row: (*row["position"], *row["size"], row["id"]))


def _capture(target, identity, selector, *, deadline, backend, clock=time.monotonic):
    """Private inert-backend seam; production capture always uses _Mac below."""
    target, identity, selector = validate_target(target), validate_identity(identity), validate_selector(selector)
    _require(identity["bundle_id"] == target["bundle_id"], "AX identity and frozen target disagree")
    _require(type(deadline) in (int, float) and math.isfinite(deadline) and 0 < deadline - clock() <= MAX_SECONDS,
             "AX deadline must be an absolute monotonic time within10s")
    reader = _Reader(backend, identity["pid"], deadline, clock)
    result = {"format": OBSERVATION, "complete": False, "issues": [], "read_only": True, "proof_created": False,
              "selector": selector, "context": {"format": CONTEXT, "target": target, "expected_identity": identity},
              "state": {"format": STATE, "displays": [], "focus": {"frontmost_owner": False, "focused_owner": False},
                        "root": None, "node_count": 0, "nodes": reader.nodes}}
    try:
        reader.bounded()
        # The required screen API may initialize this ordinary GUI client's
        # WindowServer connection. Retain its full display read before asking
        # for focus; there is no extra warm-up or activation.
        displays = _displays(backend.displays(deadline))
        result["context"]["displays"] = displays
        result["state"]["displays"] = [{key: row[key] for key in ("position", "size")} for row in displays]
        reader.bounded()
        before = _check_context(backend.context(identity, deadline), identity)
        result["context"]["before"] = before
        result["state"]["focus"] = {"frontmost_owner": True, "focused_owner": True}
        windows, app = [], backend.application(identity["pid"])
        children, availability = reader.children(app)
        if availability != "present": raise _Fault("app_children_unavailable", availability=availability)
        for offset, window in enumerate(children):
            role, _ = reader.scalar(window, "role")
            if role != selector["role"]: continue
            subrole, _ = reader.scalar(window, "subrole")
            title, _ = reader.scalar(window, "title")
            if subrole != selector["subrole"] or title != selector["title"]: continue
            position, _ = reader.vector(window, "position")
            size, _ = reader.vector(window, "size")
            if position is None or size is None: continue
            geometry = selector["geometry"]
            if geometry["kind"] == "exact":
                matched = _same(position, geometry["position"]) and _same(size, geometry["size"])
            else:
                matches = [row for row in displays if _same(position, row["position"]) and _same(size, row["size"])]
                if len(matches) > 1: raise _Fault("display_selector_ambiguous")
                matched = len(matches) == 1
            if matched: windows.append((offset, window))
        if len(windows) != 1: raise _Fault("window_selector_ambiguous_or_absent", matches=len(windows))
        index, window = windows[0]
        result["context"]["window_app_child_index"] = index
        result["state"]["root"] = reader.walk(window, [])
        reader.bounded()
        after = _check_context(backend.context(identity, deadline), identity)
        result["context"]["after"] = after
        _require(_same(before, after), "AX process/kernel/session/focus changed during snapshot")
        _require(_same(displays, _displays(backend.displays(deadline))), "current AX displays changed during snapshot")
        reader.bounded()
        result["state"]["node_count"] = len(reader.nodes)
        validate_contract({"format": CONTRACT, "target": target, "selector": selector, "state": result["state"]})
        reader.bounded()  # Validation itself cannot carry a sample beyond its deadline.
        result["complete"] = True
    except _Fault as error:
        result["issues"].append(error.issue)
    except Exception as error:
        result["issues"].append({"kind": "snapshot_incomplete", "error_type": type(error).__name__})
    result["state"]["node_count"] = len(reader.nodes)
    return result


def validate_state(state):
    """Pure full semantic-state validation; no tree field may be omitted."""
    _keys(state, {"format", "displays", "focus", "root", "node_count", "nodes"})
    _require(state["format"] == STATE and type(state["displays"]) is list and 1 <= len(state["displays"]) <= MAX_DISPLAYS,
             "complete versioned AX semantic state required")
    for display in state["displays"]:
        _keys(display, {"position", "size"})
        _require(_same(display["position"], _vector(display["position"])) and
                 _same(display["size"], _vector(display["size"], size=True)) and all(v > 0 for v in display["size"]),
                 "complete finite semantic display geometry required")
    _require(state["displays"] == sorted(state["displays"], key=lambda row: (*row["position"], *row["size"])),
             "canonical full semantic display order required")
    _keys(state["focus"], {"frontmost_owner", "focused_owner"})
    _require(all(state["focus"][key] is True for key in state["focus"]), "semantic owner focus required")
    nodes = state["nodes"]
    _require(type(nodes) is list and 1 <= len(nodes) <= MAX_NODES and type(state["node_count"]) is int and
             state["node_count"] == len(nodes) and type(state["root"]) is int and state["root"] == 0,
             "complete bounded AX topology required")
    visited = []
    def walk(index, parent, path):
        _require(type(index) is int and index == len(visited) and index < len(nodes) and len(path) <= MAX_DEPTH,
                 "canonical noncyclic complete AX preorder required")
        visited.append(index)
        row = nodes[index]
        _keys(row, {"id", "parent", "path", "availability"} | set(ATTRS))
        _require(type(row["id"]) is int and row["id"] == index and _same(row["parent"], parent) and _same(row["path"], path),
                 "AX node topology differs from its complete path")
        _keys(row["availability"], set(ATTRS))
        _require(all(type(value) is str and value in AVAILABILITY for value in row["availability"].values()),
                 "explicit AX attribute availability required")
        for key in ("role", "subrole", "title", "description", "enabled", "value"):
            _require(_same(row[key], _scalar(row[key])), "exact typed AX scalar required")
            if key in {"role", "subrole", "title", "description"}:
                _require(row[key] is None or type(row[key]) is str, "typed AX text field required")
            if key == "enabled": _require(row[key] is None or type(row[key]) is bool, "typed AX enabled field required")
            if row["availability"][key] != "present": _require(row[key] is None, "unavailable AX attribute cannot carry a value")
        _require(_text(row["role"], empty=False) and row["availability"]["role"] == "present", "complete AX role required")
        for key in ("position", "size"):
            _require(row[key] is None or _same(row[key], _vector(row[key], size=key == "size")), "typed AX geometry required")
            if row["availability"][key] != "present": _require(row[key] is None, "unavailable geometry cannot carry a value")
        _require(type(row["children"]) is list and len(row["children"]) <= MAX_CHILDREN and
                 (row["availability"]["children"] == "present" or row["children"] == []), "complete bounded AX child list required")
        for offset, child in enumerate(row["children"]): walk(child, index, path + [offset])
    walk(0, None, [])
    _require(len(visited) == len(nodes) and nodes[0]["role"] == "AXWindow", "AX state has unreachable nodes or wrong window root")
    _require(len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode()) <= MAX_BYTES, "AX semantic state exceeds byte bound")
    return copy.deepcopy(state)


def validate_contract(contract):
    _keys(contract, {"format", "target", "selector", "state"})
    _require(contract["format"] == CONTRACT, "versioned complete AX state contract required")
    validate_target(contract["target"])
    selector, state = validate_selector(contract["selector"]), validate_state(contract["state"])
    root = state["nodes"][0]
    _require(all(_same(root[key], selector[key]) for key in ("role", "subrole", "title")), "AX contract root differs from selector")
    geometry = selector["geometry"]
    expected = [geometry] if geometry["kind"] == "exact" else state["displays"]
    _require(sum(_same(root["position"], row["position"]) and _same(root["size"], row["size"]) for row in expected) == 1,
             "AX contract has absent or ambiguous declared window geometry")
    return copy.deepcopy(contract)


def matches(observation, contract):
    """Compare all semantic fields; never edit a historical observation/context."""
    contract = validate_contract(contract)
    _keys(observation, {"format", "complete", "issues", "read_only", "proof_created", "selector", "context", "state"})
    if (observation["format"] != OBSERVATION or observation["complete"] is not True or observation["issues"] != [] or
            observation["read_only"] is not True or observation["proof_created"] is not False): return False
    context = observation["context"]
    _keys(context, {"format", "target", "expected_identity", "before", "after", "displays", "window_app_child_index"})
    _require(context["format"] == CONTEXT and type(context["window_app_child_index"]) is int and
             0 <= context["window_app_child_index"] < MAX_CHILDREN, "complete current AX context required")
    identity = validate_identity(context["expected_identity"])
    before, after = _check_context(context["before"], identity), _check_context(context["after"], identity)
    _require(_same(before, after), "retained AX process context changed")
    displays = _displays(context["displays"])
    state = validate_state(observation["state"])
    _require(_same(state["displays"], [{key: row[key] for key in ("position", "size")} for row in displays]),
             "retained AX display context differs from semantic state")
    return (_same(validate_target(context["target"]), contract["target"]) and identity["bundle_id"] == contract["target"]["bundle_id"] and
            _same(validate_selector(observation["selector"]), contract["selector"]) and _same(state, contract["state"]))


def capture(target, expected_identity, window_selector, *, deadline):
    """Read one complete bounded window state in an already authorized VM GUI.

    The caller supplies the current complete identity on every run. Restart
    comparisons supply a newly verified identity, never adopt a new PID from an
    old receipt. No actions, activation, screenshots, attachment or prompts.
    """
    R.assert_guest()
    # Validate before framework construction or target reads.
    target = validate_target(target)
    expected_identity = validate_identity(expected_identity)
    window_selector = validate_selector(window_selector)
    _require(type(deadline) in (int, float) and math.isfinite(deadline) and 0 < deadline - time.monotonic() <= MAX_SECONDS,
             "AX deadline must be an absolute monotonic time within10s")
    return _capture(target, expected_identity, window_selector, deadline=deadline, backend=_Mac())


def _moment(value):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
             "finite nonnegative monotonic AX time required")
    return float(value)


def _settlement_inputs(target, identity, selector, deadline, interval, max_samples, started):
    target, identity, selector = validate_target(target), validate_identity(identity), validate_selector(selector)
    _require(identity["bundle_id"] == target["bundle_id"], "AX identity and frozen target disagree")
    started, deadline = _moment(started), _moment(deadline)
    _require(0 < deadline - started <= MAX_SECONDS, "AX settlement phase must be within10s")
    _require(type(interval) in (int, float) and math.isfinite(interval) and 0 < interval <= MAX_SECONDS,
             "positive bounded AX sample interval required")
    _require(type(max_samples) is int and 1 <= max_samples <= MAX_SAMPLES,
             "AX settlement requires1–16 bounded samples")
    return target, identity, selector, deadline, float(interval), max_samples, started


def _settlement_observation(value):
    """Validate a raw complete observation or its bounded incomplete prefix."""
    _keys(value, {"format", "complete", "issues", "read_only", "proof_created", "selector", "context", "state"})
    _require(value["format"] == OBSERVATION and type(value["complete"]) is bool and value["read_only"] is True and
             value["proof_created"] is False and type(value["issues"]) is list and len(value["issues"]) <= 16,
             "closed read-only AX sample required")
    context = value["context"]
    required = {"format", "target", "expected_identity"}
    optional = {"before", "after", "displays", "window_app_child_index"}
    _require(type(context) is dict and required <= set(context) and not set(context) - required - optional and
             context["format"] == CONTEXT, "closed AX sample context prefix required")
    target, identity, selector = (validate_target(context["target"]),
                                  validate_identity(context["expected_identity"]), validate_selector(value["selector"]))
    _require(identity["bundle_id"] == target["bundle_id"], "sample target and process disagree")
    claims = []
    for key in ("before", "after"):
        if key in context: claims.append(("current", _check_context(context[key], identity)))
    if "displays" in context:
        _require(_same(context["displays"], _displays(context["displays"])),
                 "canonical acquired AX display context required")
        claims.append(("displays", context["displays"]))
    if "window_app_child_index" in context:
        _require("before" in context and "displays" in context and type(context["window_app_child_index"]) is int and
                 0 <= context["window_app_child_index"] < MAX_CHILDREN, "acquired AX window index required")
        claims.append(("window_app_child_index", context["window_app_child_index"]))
    if "after" in context: _require("window_app_child_index" in context, "ordered AX context acquisition required")
    _require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= MAX_OBSERVATION_BYTES,
             "retained AX observation exceeds byte bound")
    if value["complete"]:
        _require(matches(value, {"format": CONTRACT, "target": target, "selector": selector, "state": value["state"]}),
                 "complete AX sample lost its whole state/context")
    else:
        _require(bool(value["issues"]), "incomplete AX sample requires its actual issue")
        _keys(value["state"], {"format", "displays", "focus", "root", "node_count", "nodes"})
        state = value["state"]
        _require(state["format"] == STATE and type(state["nodes"]) is list and len(state["nodes"]) <= MAX_NODES and
                 type(state["node_count"]) is int and state["node_count"] == len(state["nodes"]) and
                 (state["root"] is None or type(state["root"]) is int and state["root"] == 0),
                 "bounded raw AX partial state required")
    # An early display-only prefix is real retained evidence even if the focus
    # query fails. Its entire ID/geometry claim stays fixed across samples;
    # the semantic projection cannot be stripped or changed independently.
    projected = [{key: row[key] for key in ("position", "size")} for row in context.get("displays", [])]
    _require(_same(value["state"]["displays"], projected), "acquired AX displays lost their semantic projection")
    return target, identity, selector, claims


def _settlement_history(samples, target, identity, selector):
    """Derive the FIRST stopping event; no independent expected state is read."""
    claims, previous = {}, None
    for index, sample in enumerate(samples):
        if sample["error"] is not None: return "capture_error", index, None
        observation = sample["observation"]
        try: sampled_target, sampled_identity, sampled_selector, current = _settlement_observation(observation)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
            return "observation_invalid", index, None
        if (not _same(sampled_target, target) or not _same(sampled_identity, identity) or not _same(sampled_selector, selector)):
            return "context_changed", index, None
        for key, value in current:
            if key in claims and not _same(claims[key], value): return "context_changed", index, None
            claims[key] = value
        if observation["complete"]:
            if previous is not None and _same(previous["state"], observation["state"]):
                return "stable", index, [index - 1, index]
            previous = observation
        else:
            previous = None
    return None, None, None


def validate_settlement(value):
    """Pure validation of retained success/failure and the first-pair rule.

    A valid incomplete record is retained failure evidence, never a state proof.
    Callers must additionally require complete is True before using its pair.
    """
    _keys(value, {"format", "complete", "issues", "read_only", "proof_created", "target", "expected_identity", "selector",
                  "interval", "max_samples", "started_monotonic", "deadline_monotonic", "finished_monotonic", "samples", "stable_pair"})
    _require(value["format"] == SETTLEMENT and type(value["complete"]) is bool and value["read_only"] is True and
             value["proof_created"] is False, "closed read-only AX settlement required")
    target, identity, selector, deadline, interval, maximum, started = _settlement_inputs(
        value["target"], value["expected_identity"], value["selector"], value["deadline_monotonic"], value["interval"],
        value["max_samples"], value["started_monotonic"])
    finished = _moment(value["finished_monotonic"])
    _require(finished >= started and type(value["samples"]) is list and len(value["samples"]) <= maximum,
             "bounded monotonic AX settlement sequence required")
    previous_end = None
    for index, sample in enumerate(value["samples"]):
        _keys(sample, {"index", "started_monotonic", "finished_monotonic", "observation", "error"})
        begin, end = _moment(sample["started_monotonic"]), _moment(sample["finished_monotonic"])
        _require(type(sample["index"]) is int and sample["index"] == index and started <= begin < deadline and
                 begin <= end <= finished and (previous_end is None or begin >= previous_end + interval),
                 "AX sample order/cadence/deadline changed")
        if sample["error"] is not None:
            _keys(sample["error"], {"type"})
            _require(sample["observation"] is None and _text(sample["error"]["type"], empty=False, maximum=128),
                     "actual capture exception type required")
        else:
            _require(type(sample["observation"]) is dict, "raw AX observation required")
            _require(len(json.dumps(sample["observation"], ensure_ascii=False, allow_nan=False).encode()) <= MAX_OBSERVATION_BYTES,
                     "retained raw AX sample is not bounded JSON data")
        previous_end = end
    reason, stop, pair = _settlement_history(value["samples"], target, identity, selector)
    _require(stop is None or stop == len(value["samples"]) - 1, "settlement continued beyond its FIRST stopping event")
    if value["complete"]:
        _require(value["issues"] == [] and reason == "stable" and _same(value["stable_pair"], pair) and
                 finished < deadline, "complete settlement lacks its first consecutive complete whole-state pair")
    else:
        _require(value["stable_pair"] is None and type(value["issues"]) is list and len(value["issues"]) == 1,
                 "incomplete settlement cannot expose a usable pair")
        issue = value["issues"][0]
        _keys(issue, {"kind"})
        if issue["kind"] == "deadline":
            _require(finished >= deadline, "settlement claimed a deadline it never reached")
        elif issue["kind"] == "sample_limit":
            _require(reason is None and len(value["samples"]) == maximum, "settlement did not reach its sample limit")
        else:
            _require(reason in {"capture_error", "observation_invalid", "context_changed"} and issue["kind"] == reason,
                     "settlement failure differs from its retained observations")
    return copy.deepcopy(value)


def _settle(target, identity, selector, *, deadline, interval, max_samples, backend,
            clock=time.monotonic, sleep=time.sleep, started=None):
    """Fixed-backend/clock seam for inert tests; public settle constructs _Mac."""
    started = _moment(clock()) if started is None else started
    target, identity, selector, deadline, interval, maximum, started = _settlement_inputs(
        target, identity, selector, deadline, interval, max_samples, started)
    result = {"format": SETTLEMENT, "complete": False, "issues": [], "read_only": True, "proof_created": False,
              "target": target, "expected_identity": identity, "selector": selector, "interval": interval, "max_samples": maximum,
              "started_monotonic": started, "deadline_monotonic": deadline, "finished_monotonic": started,
              "samples": [], "stable_pair": None}
    reason, pair = None, None
    while len(result["samples"]) < maximum:
        if clock() >= deadline:
            reason = "deadline"
            break
        if result["samples"]:
            remaining = deadline - clock()
            if remaining <= 0:
                reason = "deadline"
                break
            sleep(min(interval, remaining))  # Fixed read-only cadence; never another actor/input.
            if clock() >= deadline:
                reason = "deadline"
                break
        begin = _moment(clock())
        if begin >= deadline:
            reason = "deadline"
            break
        observation, error = None, None
        try:
            observation = copy.deepcopy(_capture(target, identity, selector, deadline=deadline, backend=backend, clock=clock))
        except Exception as failure:
            error = {"type": type(failure).__name__}
        result["samples"].append({"index": len(result["samples"]), "started_monotonic": begin,
                                  "finished_monotonic": _moment(clock()), "observation": observation, "error": error})
        reason, _, pair = _settlement_history(result["samples"], target, identity, selector)
        if clock() >= deadline:
            reason, pair = "deadline", None
        if reason is not None: break
    if reason is None: reason = "sample_limit"
    result.update(complete=reason == "stable", issues=[] if reason == "stable" else [{"kind": reason}],
                  stable_pair=pair if reason == "stable" else None, finished_monotonic=_moment(clock()))
    validated = validate_settlement(result)
    validated["finished_monotonic"] = _moment(clock())
    if validated["complete"] and validated["finished_monotonic"] >= deadline:
        validated.update(complete=False, stable_pair=None, issues=[{"kind": "deadline"}])
    return validated


def settle(target, expected_identity, window_selector, *, deadline, interval=.4, max_samples=16):
    """Retain bounded samples and stop at the first complete equal full pair.

    No expected contract is accepted or consulted. All acquired current-context
    fields must stay fixed within this phase; a restart needs a new phase/input.
    """
    R.assert_guest()
    target, identity, selector, deadline, interval, maximum, started = _settlement_inputs(
        target, expected_identity, window_selector, deadline, interval, max_samples, time.monotonic())
    return _settle(target, identity, selector, deadline=deadline, interval=interval, max_samples=maximum,
                   backend=_Mac(), clock=time.monotonic, sleep=time.sleep, started=started)


class _Mac:
    """Fixed public read-only macOS APIs; loaded only after the VM guard."""
    def __init__(self):
        import AppKit
        import ApplicationServices
        import CoreFoundation
        import Foundation
        import Quartz
        self.K, self.A, self.F, self.N, self.Q = AppKit, ApplicationServices, CoreFoundation, Foundation, Quartz

    def timeout(self, element, seconds):
        _require(seconds > 0 and self.A.AXUIElementSetMessagingTimeout(element, seconds) == 0, "cannot bound AX message")

    def owner(self, element):
        code, pid = self.A.AXUIElementGetPid(element, None)
        _require(code == 0 and type(pid) is int, "AX element owner unavailable")
        return pid

    def attribute(self, element, name): return self.A.AXUIElementCopyAttributeValue(element, name, None)
    def equal(self, left, right): return bool(self.F.CFEqual(left, right))
    def is_array(self, value): return self.F.CFGetTypeID(value) == self.F.CFArrayGetTypeID()
    def native_scalar(self, value): return _native_scalar(value, self.F, self.N.NSNumber)
    def application(self, pid): return self.A.AXUIElementCreateApplication(pid)

    def vector(self, value, key):
        _require(key in {"position", "size"}, "known typed AX geometry required")
        kind = self.A.kAXValueTypeCGPoint if key == "position" else self.A.kAXValueTypeCGSize
        valid, pair = self.A.AXValueGetValue(value, kind, None)
        _require(valid and pair is not None and len(pair) == 2, "typed AX geometry unavailable")
        # The actual bridge returns CoreFoundation.CGPoint/CGSize structs, not
        # list/tuple. Only this successful typed API result is normalized here;
        # the shared semantic-vector validator still requires finite numbers.
        return [pair[0], pair[1]]

    def displays(self, deadline):
        _require(time.monotonic() < deadline, "AX display phase expired")
        screens = self.K.NSScreen.screens()
        _require(screens is not None and 1 <= len(screens) <= MAX_DISPLAYS, "bounded current displays required")
        rows = []
        for screen in screens:
            _require(time.monotonic() < deadline, "AX display phase expired")
            identifier = int(screen.deviceDescription()["NSScreenNumber"])
            rect = self.Q.CGDisplayBounds(identifier)
            rows.append({"id": identifier, "position": [rect.origin.x, rect.origin.y], "size": [rect.size.width, rect.size.height]})
        return rows

    def context(self, identity, deadline, *, read_focus=True):
        """Read raw context; skipped AX focus stays unknown, never proof-ready."""
        _require(type(read_focus) is bool, "read_focus must be an exact boolean")
        import ctypes
        from pathlib import Path
        import subprocess
        _require(time.monotonic() < deadline and identity["uid"] == os.getuid() == os.geteuid(), "same-user AX phase required")
        app = self.K.NSRunningApplication.runningApplicationWithProcessIdentifier_(identity["pid"])
        _require(app is not None and not app.isTerminated(), "existing AX process required; no launch/adoption")
        bundle, executable, launched = app.bundleURL(), app.executableURL(), app.launchDate()
        _require(bundle is not None and executable is not None and launched is not None, "complete current AX NS identity required")
        current = {"bundle": str(Path(str(bundle.path())).resolve(strict=True)), "bundle_id": str(app.bundleIdentifier()),
                   "executable": str(Path(str(executable.path())).resolve(strict=True)), "pid": int(app.processIdentifier()),
                   "launch_time": float(launched.timeIntervalSince1970()), "uid": os.getuid()}
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "AX kernel identity phase expired")
        query = subprocess.run(["/bin/ps", "-o", "pid=,ppid=,uid=,lstart=,comm=", "-p", str(identity["pid"])],
            capture_output=True, timeout=min(5, remaining), check=False, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
        _require(query.returncode == 0 and len(query.stdout) <= 8192 and len(query.stderr) <= 1024, "bounded current AX kernel record required")
        lines = query.stdout.decode("utf-8").splitlines()
        fields = lines[0].split(None, 8) if len(lines) == 1 else []
        _require(len(fields) == 9 and all(value.isdecimal() for value in fields[:3]), "complete AX kernel record required")
        kernel = {"pid": int(fields[0]), "ppid": int(fields[1]), "uid": int(fields[2]),
                  "started_at": " ".join(fields[3:8]), "executable": fields[8]}
        gui = self.Q.CGSessionCopyCurrentDictionary() or {}
        def gui_scalar(key, *, absent=None):
            return _scalar(gui[key], self.native_scalar) if key in gui else absent
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        function = security.SessionGetInfo
        function.argtypes, function.restype = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int32
        asid, bits = ctypes.c_uint32(), ctypes.c_uint32()
        _require(function(0xffffffff, ctypes.byref(asid), ctypes.byref(bits)) == 0, "current AX security session unavailable")
        front = self.K.NSWorkspace.sharedWorkspace().frontmostApplication()
        focused_pid = None
        if read_focus:
            system = self.A.AXUIElementCreateSystemWide()
            _require(deadline > time.monotonic(), "AX focus phase expired")
            self.timeout(system, min(AX_SECONDS, deadline - time.monotonic()))
            code, focused = self.attribute(system, "AXFocusedApplication")
            _require(code == 0 and focused is not None, "current AX focused application unavailable")
            self.timeout(focused, min(AX_SECONDS, deadline - time.monotonic()))
            focused_pid = self.owner(focused)
        else:
            _require(deadline > time.monotonic(), "AX focus phase expired")
        return {"identity": current, "kernel": kernel,
                "session": {"asid": asid.value, "attribute_bits": bits.value, "uid": gui_scalar(self.Q.kCGSessionUserIDKey),
                    "on_console": gui_scalar(self.Q.kCGSessionOnConsoleKey),
                    "login_done": gui_scalar("kCGSessionLoginDoneKey"),
                    "locked": gui_scalar("CGSSessionScreenIsLocked", absent=False), "ax_trusted": bool(self.A.AXIsProcessTrusted())},
                "focus": {"frontmost_pid": int(front.processIdentifier()) if front is not None else None, "focused_pid": focused_pid}}

