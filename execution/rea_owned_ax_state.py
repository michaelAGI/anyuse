"""Opt-in VM-only observation of one owned, visible complete AX window tree.

Unknown global/local AX focus remains unknown. This capability never authorizes
input, activation or an AX action. Quartz owner/geometry association is not an
AX/Quartz object-identity or pixel-visibility proof. Read queries may execute an
app's normal AX provider. No native getter, screenshot or model supplies state.
Separate context/window/tree reads are not an atomic snapshot; equal bookends
cannot exclude an intervening change and restoration.
The caller supplies the frozen bundle fingerprint; this reader checks the full
current process/kernel/GUI epoch, not an on-disk bundle hash.
"""
from __future__ import annotations

import copy
import json
import math
import time

from . import rea_ax_state as S
from . import rea_mapping as R

CAPABILITY = "ax_owned_visible_readonly/1"
OBSERVATION = "anyuse.rea-owned-ax-observation/1"
STATE = "anyuse.rea-owned-ax-semantic-state/1"
CONTEXT = "anyuse.rea-owned-ax-current-context/1"
CONTRACT = "anyuse.rea-owned-ax-state-contract/1"
SETTLEMENT = "anyuse.rea-owned-ax-settlement/1"
VISIBILITY = "anyuse.rea-owned-ax-visibility/1"
COMPLETENESS_SCOPE = "selected_owned_visible_ax_window_tree"
SELECTOR = S.SELECTOR
MAX_NODES, MAX_DEPTH, MAX_CHILDREN, MAX_TEXT = S.MAX_NODES, S.MAX_DEPTH, S.MAX_CHILDREN, S.MAX_TEXT
MAX_DISPLAYS, MAX_SECONDS, AX_SECONDS, MAX_BYTES = S.MAX_DISPLAYS, S.MAX_SECONDS, S.AX_SECONDS, S.MAX_BYTES
MAX_SAMPLES, MAX_OBSERVATION_BYTES = S.MAX_SAMPLES, S.MAX_OBSERVATION_BYTES
MAX_WINDOWS, MAX_AUTHORIZATION_PROCESSES = 512, 16
AUTHORIZATION_BUNDLE_IDS = ("com.apple.SecurityAgent", "com.apple.authorizationhost")
ATTRS, AVAILABILITY = S.ATTRS, S.AVAILABILITY
validate_target, validate_identity, validate_selector = S.validate_target, S.validate_identity, S.validate_selector
_require, _keys, _same, _text, _vector = S._require, S._keys, S._same, S._text, S._vector
_moment, _settlement_inputs = S._moment, S._settlement_inputs


def _integer(value, low=0, high=2**31 - 1):
    _require(type(value) is int and low <= value <= high, "bounded exact owned-AX integer required")


def _error(value):
    _keys(value, {"type", "message"})
    _require(_text(value["type"], empty=False, maximum=128) and _text(value["message"], maximum=512),
             "bounded actual owned-AX query error required")


def _not_attempted(*, local=False):
    value = {"status": "not_attempted", "code": None, "pid": None, "error": None}
    if local: value["selected_window_match"] = None
    return value


def _focus_probe(value, *, local=False):
    _keys(value, {"status", "code", "pid", "error"} | ({"selected_window_match"} if local else set()))
    _require(type(value["status"]) is str and value["status"] in {"observed", "unavailable", "not_attempted"},
             "explicit focus-query availability required")
    if value["code"] is not None: _integer(value["code"], -(2**31), 2**31 - 1)
    if value["pid"] is not None: _integer(value["pid"], 1)
    if local:
        _require(value["selected_window_match"] is None or type(value["selected_window_match"]) is bool,
                 "actual local-window equality or unknown required")
    if value["status"] == "not_attempted":
        _require(value["code"] is None and value["pid"] is None and value["error"] is None and
                 (not local or value["selected_window_match"] is None), "unattempted focus cannot invent an observation")
    elif value["status"] == "observed":
        _require(value["code"] == 0 and type(value["code"]) is int and value["pid"] is not None and
                 value["error"] is None and (not local or type(value["selected_window_match"]) is bool),
                 "successful focus query needs its actual owner and equality")
    else:
        _error(value["error"])
        if value["code"] != 0:
            _require(value["pid"] is None and (not local or value["selected_window_match"] is None),
                     "failed attribute query cannot invent an element owner/equality")
    return copy.deepcopy(value)


def _raw_context(value):
    """Validate actual raw facts, including facts that make admission fail."""
    _keys(value, {"identity", "kernel", "session", "focus"})
    validate_identity(value["identity"])
    kernel = value["kernel"]
    _keys(kernel, {"pid", "ppid", "uid", "started_at", "executable"})
    _integer(kernel["pid"], 1); _integer(kernel["ppid"]); _integer(kernel["uid"], 0, 2**32 - 2)
    _require(_text(kernel["started_at"], empty=False, maximum=128) and
             _text(kernel["executable"], empty=False, maximum=4096), "actual kernel epoch and executable required")
    session = value["session"]
    _keys(session, {"asid", "attribute_bits", "uid", "on_console", "login_done", "locked", "ax_trusted"})
    _integer(session["asid"], 0, 2**32 - 1); _integer(session["attribute_bits"], 0, 2**32 - 1)
    _require(session["uid"] is None or type(session["uid"]) is int and 0 <= session["uid"] < 2**32 - 1,
             "actual console UID or unknown required")
    for key in ("on_console", "login_done", "locked", "ax_trusted"):
        _require(session[key] is None or type(session[key]) is bool, "typed actual GUI availability required")
    focus = value["focus"]
    _keys(focus, {"frontmost_pid", "focused_pid", "global", "local"})
    if focus["frontmost_pid"] is not None: _integer(focus["frontmost_pid"], 1)
    _focus_probe(focus["global"]); _focus_probe(focus["local"], local=True)
    _require(_same(focus["focused_pid"], focus["global"]["pid"]), "global focused_pid must retain the actual query result")
    return copy.deepcopy(value)


def _check_context(value, identity, *, probes=True):
    value = _raw_context(value)
    _require(_same(value["identity"], identity), "owned-AX current process identity changed")
    kernel, session, focus = value["kernel"], value["session"], value["focus"]
    _require(kernel["pid"] == identity["pid"] and kernel["uid"] == identity["uid"] and
             kernel["executable"] == identity["executable"], "owned-AX kernel owner/epoch mismatch")
    _require(session["asid"] > 0 and session["uid"] == identity["uid"] and session["on_console"] is True and
             session["login_done"] is True and session["locked"] is False and session["ax_trusted"] is True and
             session["attribute_bits"] & 0x10 and not session["attribute_bits"] & 0x1000,
             "authorized unlocked same-user local owned-AX context required")
    _require(type(focus["frontmost_pid"]) is int and focus["frontmost_pid"] == identity["pid"],
             "owned-AX target must already be NS frontmost; no activation")
    for name in ("global", "local"):
        query = focus[name]
        if probes: _require(query["status"] != "not_attempted", "complete owned-AX snapshot must attempt both focus queries")
        if query["pid"] is not None:
            _require(query["pid"] == identity["pid"], "known contradictory AX focus owner")
    _require(focus["local"]["selected_window_match"] is not False, "known contradictory local focused AX window")
    return value


def _epoch(value):
    # Focus availability remains in the raw record but is NOT a semantic state
    # or equality claim. Every actual known contradiction is checked separately.
    return {**{key: copy.deepcopy(value[key]) for key in ("identity", "kernel", "session")},
            "frontmost_pid": value["focus"]["frontmost_pid"]}


def _current(backend, identity, window, deadline):
    raw = copy.deepcopy(backend.context(identity, deadline, read_focus=False))
    _keys(raw, {"identity", "kernel", "session", "focus"})
    _keys(raw["focus"], {"frontmost_pid", "focused_pid"})
    _require(raw["focus"]["focused_pid"] is None, "skipped strict focus must stay unknown")
    queries = {"global": _not_attempted(), "local": _not_attempted(local=True)} if window is None else backend.focus(identity, window, deadline)
    _keys(queries, {"global", "local"})
    raw["focus"] = {"frontmost_pid": raw["focus"]["frontmost_pid"], "focused_pid": queries["global"]["pid"], **copy.deepcopy(queries)}
    return raw


def _inventory(value):
    _keys(value, {"windows", "authorization_processes"})
    _require(type(value["windows"]) is list and len(value["windows"]) <= MAX_WINDOWS,
             "bounded complete current Quartz window inventory required")
    numbers = set()
    for row in value["windows"]:
        _keys(row, {"number", "pid", "layer", "alpha", "on_screen", "position", "size", "owner_name", "name"})
        _integer(row["number"], 1); _integer(row["pid"]); _integer(row["layer"], -(2**31))
        _require(row["number"] not in numbers, "unique actual Quartz window numbers required")
        numbers.add(row["number"])
        _require(type(row["alpha"]) is float and math.isfinite(row["alpha"]) and 0 <= row["alpha"] <= 1 and
                 type(row["on_screen"]) is bool and _same(row["position"], _vector(row["position"])) and
                 _same(row["size"], _vector(row["size"], size=True)), "typed current Quartz visibility and geometry required")
        for key in ("owner_name", "name"):
            _require(row[key] is None or _text(row[key]), "actual bounded Quartz text or unavailable required")
    processes = value["authorization_processes"]
    _require(type(processes) is list and len(processes) <= MAX_AUTHORIZATION_PROCESSES,
             "bounded explicit authorization-process inventory required")
    seen = set()
    for process in processes:
        _keys(process, {"bundle_id", "pid"}); _integer(process["pid"], 1)
        _require(process["bundle_id"] in AUTHORIZATION_BUNDLE_IDS and process["pid"] not in seen,
                 "fixed authorization bundle IDs and unique PIDs required")
        seen.add(process["pid"])
    return copy.deepcopy(value)


def _visible(row):
    return row["on_screen"] and row["alpha"] > 0 and all(value > 0 for value in row["size"])


def _overlap(left, right):
    return all(max(left["position"][axis], right["position"][axis]) <
               min(left["position"][axis] + left["size"][axis], right["position"][axis] + right["size"][axis])
               for axis in (0, 1))


def _visibility_record(inventory, identity, geometry):
    value = {"format": VISIBILITY, "association": "unique_owner_geometry", "inventory_materialization_bounded": False,
             **copy.deepcopy(inventory), "selected_index": None, "selected_window_number": None}
    _inventory(inventory)
    matches = [index for index, row in enumerate(inventory["windows"]) if _visible(row) and row["pid"] == identity["pid"] and
               _same(row["position"], geometry["position"]) and _same(row["size"], geometry["size"])]
    if len(matches) == 1:
        value["selected_index"] = matches[0]
        value["selected_window_number"] = inventory["windows"][matches[0]]["number"]
    return value


def _visibility_raw(value):
    _keys(value, {"format", "association", "inventory_materialization_bounded", "windows", "authorization_processes",
                  "selected_index", "selected_window_number"})
    _require(value["format"] == VISIBILITY and value["association"] == "unique_owner_geometry" and
             value["inventory_materialization_bounded"] is False, "explicit scoped Quartz association required")
    _inventory({key: value[key] for key in ("windows", "authorization_processes")})
    if value["selected_index"] is None:
        _require(value["selected_window_number"] is None, "unselected Quartz inventory cannot invent a window number")
    else:
        _integer(value["selected_index"], 0, len(value["windows"]) - 1)
        row = value["windows"][value["selected_index"]]
        _require(_same(row["number"], value["selected_window_number"]), "Quartz selected index and number differ")
    return copy.deepcopy(value)


def _visibility_gate(value, identity, geometry):
    value = _visibility_raw(value)
    expected = _visibility_record({key: value[key] for key in ("windows", "authorization_processes")}, identity, geometry)
    _require(_same(expected, value) and value["selected_index"] is not None,
             "exactly one owned visible Quartz window must match the selected AX geometry")
    auth = {row["pid"] for row in value["authorization_processes"]}
    _require(not any(_visible(row) and row["pid"] in auth for row in value["windows"]), "visible authorization window")
    index = value["selected_index"]
    selected = value["windows"][index]
    _require(not any(_visible(row) and _overlap(row, selected) for row in value["windows"][:index]),
             "selected owned window is covered by a preceding visible Quartz window")
    return copy.deepcopy(selected)


def _window_candidates(reader, backend, identity, selector, displays):
    app = backend.application(identity["pid"])
    values, status = reader.attr(app, "AXWindows")
    if status != "present" or values is None: raise S._Fault("app_windows_unavailable", availability=status)
    if not backend.is_array(values): raise S._Fault("windows_type")
    if len(values) > MAX_CHILDREN: raise S._Fault("child_bound", reported_children=len(values))
    windows = []
    for offset, window in enumerate(values):
        role, _ = reader.scalar(window, "role")
        if role != selector["role"]: continue
        subrole, _ = reader.scalar(window, "subrole")
        title, _ = reader.scalar(window, "title")
        if not _same(subrole, selector["subrole"]) or not _same(title, selector["title"]): continue
        position, _ = reader.vector(window, "position")
        size, _ = reader.vector(window, "size")
        if position is None or size is None: continue
        expected = [selector["geometry"]] if selector["geometry"]["kind"] == "exact" else displays
        count = sum(_same(position, row["position"]) and _same(size, row["size"]) for row in expected)
        if count > 1: raise S._Fault("display_selector_ambiguous")
        if count == 1: windows.append((offset, window, {"position": position, "size": size}))
    if len(windows) != 1: raise S._Fault("window_selector_ambiguous_or_absent", matches=len(windows))
    return windows[0]


def _capture(target, identity, selector, *, deadline, backend, clock=time.monotonic):
    """Private inert-backend seam. Public capture always constructs fixed _Mac."""
    target, identity, selector = validate_target(target), validate_identity(identity), validate_selector(selector)
    _require(identity["bundle_id"] == target["bundle_id"], "owned-AX target and identity differ")
    _require(type(deadline) in (int, float) and math.isfinite(deadline) and 0 < deadline - clock() <= MAX_SECONDS,
             "owned-AX deadline must be absolute monotonic time within10s")
    reader = S._Reader(backend, identity["pid"], deadline, clock)
    result = {"format": OBSERVATION, "complete": False, "issues": [], "read_only": True, "proof_created": False,
              "selector": selector, "context": {"format": CONTEXT, "completeness_scope": COMPLETENESS_SCOPE,
                  "target": target, "expected_identity": identity},
              "state": {"format": STATE, "displays": [], "root": None, "node_count": 0, "nodes": reader.nodes}}
    context = result["context"]
    try:
        reader.bounded()
        displays = S._displays(backend.displays(deadline))
        context["displays"] = displays
        result["state"]["displays"] = [{key: row[key] for key in ("position", "size")} for row in displays]
        context["preflight"] = _current(backend, identity, None, deadline)
        _check_context(context["preflight"], identity, probes=False)
        reader.bounded()
        index, window, geometry = _window_candidates(reader, backend, identity, selector, displays)
        context["window_ax_index"] = index
        context["before"] = _current(backend, identity, window, deadline)
        _check_context(context["before"], identity)
        _require(_same(_epoch(context["preflight"]), _epoch(context["before"])), "owned-AX epoch changed before reading")
        reader.bounded()
        context["visibility_before"] = _visibility_record(backend.visible_windows(deadline), identity, geometry)
        selected = _visibility_gate(context["visibility_before"], identity, geometry)
        result["state"]["root"] = reader.walk(window, [])
        reader.bounded()
        context["after"] = _current(backend, identity, window, deadline)
        _check_context(context["after"], identity)
        after_index, after_window, after_geometry = _window_candidates(reader, backend, identity, selector, displays)
        context["window_after"] = {"ax_index": after_index, "same_window": backend.equal(window, after_window), **after_geometry}
        _require(index == after_index and context["window_after"]["same_window"] is True and _same(geometry, after_geometry),
                 "selected owned AX window changed during snapshot")
        context["visibility_after"] = _visibility_record(backend.visible_windows(deadline), identity, geometry)
        _require(_same(selected, _visibility_gate(context["visibility_after"], identity, geometry)), "owned visible Quartz window changed")
        _require(_same(_epoch(context["before"]), _epoch(context["after"])), "owned-AX current epoch changed during snapshot")
        _require(_same(displays, S._displays(backend.displays(deadline))), "owned-AX current displays changed during snapshot")
        reader.bounded()
        result["state"]["node_count"] = len(reader.nodes)
        result["complete"] = True
        validate_observation(result)
        reader.bounded()
    except S._Fault as failure:
        result["complete"] = False
        result["issues"].append(failure.issue)
    except Exception as failure:
        result["complete"] = False
        result["issues"].append({"kind": "snapshot_incomplete", "error_type": type(failure).__name__})
    finally:
        result["state"]["node_count"] = len(reader.nodes)
        reader.handles.clear()  # AX element references are private to this one capture.
    return result


def _node(row, index, *, partial=False):
    required = {"id", "parent", "path", "children", "availability"}
    _require(type(row) is dict and required <= set(row) and set(row) <= required | set(ATTRS), "closed owned-AX node prefix required")
    if not partial: _keys(row, required | set(ATTRS))
    _require(type(row["id"]) is int and row["id"] == index and
             (row["parent"] is None and index == 0 or type(row["parent"]) is int and 0 <= row["parent"] < index),
             "owned-AX node index/parent required")
    _require(type(row["path"]) is list and len(row["path"]) <= MAX_DEPTH and
             all(type(part) is int and 0 <= part < MAX_CHILDREN for part in row["path"]), "bounded owned-AX node path required")
    _require(type(row["availability"]) is dict and set(row["availability"]) <= set(ATTRS) and
             all(type(value) is str and value in AVAILABILITY for value in row["availability"].values()),
             "typed owned-AX attribute availability required")
    _require(type(row["children"]) is list and len(row["children"]) <= MAX_CHILDREN and
             all(type(child) is int and index < child < MAX_NODES for child in row["children"]) and
             (not row["children"] or row["availability"].get("children") == "present"), "bounded raw child indices required")
    for key in ATTRS:
        if key == "children":
            if key in row["availability"] and row["availability"][key] != "present":
                _require(row[key] == [], "unavailable children cannot carry indices")
            continue
        _require((key in row) == (key in row["availability"]), "raw attribute and availability must be retained together")
        if key not in row: continue
        value = row[key]
        if key in {"position", "size"}:
            _require(value is None or _same(value, _vector(value, size=key == "size")), "typed owned-AX geometry required")
        else:
            _require(_same(value, S._scalar(value)), "exact typed owned-AX scalar required")
            if key in {"role", "subrole", "title", "description"}: _require(value is None or type(value) is str, "typed AX text required")
            if key == "enabled": _require(value is None or type(value) is bool, "typed AX enabled flag required")
        if row["availability"][key] != "present": _require(value is None, "unavailable AX attribute cannot carry a value")
    if not partial:
        _keys(row["availability"], set(ATTRS))
        _require(_text(row["role"], empty=False) and row["availability"]["role"] == "present", "complete owned-AX role required")


def validate_state(state):
    """Full topology/typed state validation without any semantic focus claim."""
    _keys(state, {"format", "displays", "root", "node_count", "nodes"})
    _require(state["format"] == STATE and type(state["displays"]) is list and 1 <= len(state["displays"]) <= MAX_DISPLAYS,
             "versioned complete owned-AX state required")
    for display in state["displays"]:
        _keys(display, {"position", "size"})
        _require(_same(display["position"], _vector(display["position"])) and
                 _same(display["size"], _vector(display["size"], size=True)) and all(v > 0 for v in display["size"]),
                 "full typed owned-AX display geometry required")
    _require(_same(state["displays"], sorted(state["displays"], key=lambda row: (*row["position"], *row["size"]))),
             "canonical semantic display order required")
    nodes = state["nodes"]
    _require(type(nodes) is list and 1 <= len(nodes) <= MAX_NODES and type(state["node_count"]) is int and
             state["node_count"] == len(nodes) and type(state["root"]) is int and state["root"] == 0,
             "complete bounded owned-AX topology required")
    visited = []
    def walk(index, parent, path):
        _require(type(index) is int and index == len(visited) and index < len(nodes) and len(path) <= MAX_DEPTH,
                 "canonical noncyclic owned-AX preorder required")
        visited.append(index)
        row = nodes[index]; _node(row, index)
        _require(_same(row["parent"], parent) and _same(row["path"], path), "owned-AX topology differs from full path")
        for offset, child in enumerate(row["children"]): walk(child, index, path + [offset])
    walk(0, None, [])
    _require(len(visited) == len(nodes) and nodes[0]["role"] == "AXWindow", "unreachable owned-AX nodes or wrong window root")
    _require(len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode()) <= MAX_BYTES, "owned-AX semantic state exceeds byte bound")
    return copy.deepcopy(state)


def validate_contract(contract):
    _keys(contract, {"format", "target", "selector", "state"})
    _require(contract["format"] == CONTRACT, "versioned complete owned-AX contract required")
    validate_target(contract["target"])
    selector, state = validate_selector(contract["selector"]), validate_state(contract["state"])
    root = state["nodes"][0]
    _require(all(_same(root[key], selector[key]) for key in ("role", "subrole", "title")), "owned-AX contract root differs from selector")
    expected = [selector["geometry"]] if selector["geometry"]["kind"] == "exact" else state["displays"]
    _require(sum(_same(root["position"], row["position"]) and _same(root["size"], row["size"]) for row in expected) == 1,
             "owned-AX contract has absent or ambiguous geometry")
    return copy.deepcopy(contract)


def _issues(issues):
    _require(type(issues) is list and len(issues) <= 16, "bounded actual owned-AX issues required")
    allowed = {"kind", "error_type", "attribute", "code", "path", "reported_children", "availability", "matches"}
    for issue in issues:
        _require(type(issue) is dict and "kind" in issue and not set(issue) - allowed and
                 _text(issue["kind"], empty=False, maximum=128), "closed owned-AX issue required")
        for key in ("error_type", "attribute", "availability"):
            if key in issue: _require(_text(issue[key], empty=False, maximum=128), "typed owned-AX issue detail required")
        if "code" in issue and issue["code"] is not None: _integer(issue["code"], -(2**31))
        for key in ("reported_children", "matches"):
            if key in issue: _integer(issue[key], 0, 2**63 - 1)
        if "path" in issue:
            _require(type(issue["path"]) is list and len(issue["path"]) <= MAX_DEPTH + 1 and
                     all(type(part) is int and 0 <= part < MAX_CHILDREN for part in issue["path"]), "bounded issue path required")


def validate_observation(value):
    """Pure validation of a complete snapshot or its actual incomplete prefix."""
    _keys(value, {"format", "complete", "issues", "read_only", "proof_created", "selector", "context", "state"})
    _require(value["format"] == OBSERVATION and type(value["complete"]) is bool and value["read_only"] is True and
             value["proof_created"] is False, "closed read-only owned-AX observation required")
    _issues(value["issues"])
    context = value["context"]
    required = {"format", "completeness_scope", "target", "expected_identity"}
    optional = {"displays", "preflight", "window_ax_index", "before", "visibility_before", "after", "window_after", "visibility_after"}
    _require(type(context) is dict and required <= set(context) and not set(context) - required - optional and
             context["format"] == CONTEXT and context["completeness_scope"] == COMPLETENESS_SCOPE,
             "closed owned-AX current-context prefix required")
    target, identity, selector = validate_target(context["target"]), validate_identity(context["expected_identity"]), validate_selector(value["selector"])
    _require(identity["bundle_id"] == target["bundle_id"], "owned-AX sample target and identity differ")
    if "displays" in context: _require(_same(context["displays"], S._displays(context["displays"])), "canonical complete display inventory required")
    order = ("displays", "preflight", "window_ax_index", "before", "visibility_before", "after", "window_after", "visibility_after")
    acquired = [key for key in order if key in context]
    _require(acquired == list(order[:len(acquired)]), "ordered actual owned-AX acquisition prefix required")
    for key in ("preflight", "before", "after"):
        if key in context: _raw_context(context[key])
    if "window_ax_index" in context: _integer(context["window_ax_index"], 0, MAX_CHILDREN - 1)
    if "window_after" in context:
        row = context["window_after"]
        _keys(row, {"ax_index", "same_window", "position", "size"}); _integer(row["ax_index"], 0, MAX_CHILDREN - 1)
        _require(type(row["same_window"]) is bool and _same(row["position"], _vector(row["position"])) and
                 _same(row["size"], _vector(row["size"], size=True)), "actual typed selected-window recheck required")
    for key in ("visibility_before", "visibility_after"):
        if key in context: _visibility_raw(context[key])
    state = value["state"]
    _keys(state, {"format", "displays", "root", "node_count", "nodes"})
    _require(state["format"] == STATE and type(state["nodes"]) is list and len(state["nodes"]) <= MAX_NODES and
             type(state["node_count"]) is int and state["node_count"] == len(state["nodes"]) and
             (state["root"] is None or type(state["root"]) is int and state["root"] == 0), "bounded actual owned-AX tree prefix required")
    projected = [{key: row[key] for key in ("position", "size")} for row in context.get("displays", [])]
    _require(_same(state["displays"], projected), "owned-AX display context lost its semantic projection")
    for index, row in enumerate(state["nodes"]): _node(row, index, partial=True)
    if value["complete"]:
        _require(value["issues"] == [] and set(context) == required | optional, "complete owned-AX capture lacks its full context")
        validate_contract({"format": CONTRACT, "target": target, "selector": selector, "state": state})
        current = [_check_context(context[key], identity, probes=key != "preflight") for key in ("preflight", "before", "after")]
        _require(all(_same(_epoch(current[0]), _epoch(row)) for row in current[1:]), "retained owned-AX epoch changed")
        _require(_same(context["window_ax_index"], context["window_after"]["ax_index"]) and
                 context["window_after"]["same_window"] is True, "retained selected AX window changed")
        geometry = {key: state["nodes"][0][key] for key in ("position", "size")}
        _require(all(_same(context["window_after"][key], geometry[key]) for key in geometry), "retained selected AX geometry changed")
        before, after = [_visibility_gate(context[key], identity, geometry) for key in ("visibility_before", "visibility_after")]
        _require(_same(before, after), "retained selected Quartz window changed")
    else:
        _require(bool(value["issues"]), "incomplete owned-AX capture needs its actual issue")
    _require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= MAX_OBSERVATION_BYTES,
             "owned-AX raw observation exceeds byte bound")
    return copy.deepcopy(value)


def matches(observation, contract):
    contract, observation = validate_contract(contract), validate_observation(observation)
    if not observation["complete"]: return False
    return (_same(observation["context"]["target"], contract["target"]) and
            _same(observation["selector"], contract["selector"]) and _same(observation["state"], contract["state"]))


def capture(target, expected_identity, window_selector, *, deadline):
    R.assert_guest()
    target, identity, selector = validate_target(target), validate_identity(expected_identity), validate_selector(window_selector)
    _require(type(deadline) in (int, float) and math.isfinite(deadline) and 0 < deadline - time.monotonic() <= MAX_SECONDS,
             "owned-AX deadline must be absolute monotonic time within10s")
    return _capture(target, identity, selector, deadline=deadline, backend=_Mac())


def _observation_claims(observation):
    observation = validate_observation(observation)
    context, claims = observation["context"], []
    for key in ("preflight", "before", "after"):
        if key in context: claims.append(("current", _epoch(_check_context(context[key], context["expected_identity"], probes=key != "preflight"))))
    for key in ("displays", "window_ax_index"):
        if key in context: claims.append((key, context[key]))
    for key in ("visibility_before", "visibility_after"):
        if key in context and context[key]["selected_index"] is not None:
            claims.append(("selected_quartz_window", context[key]["windows"][context[key]["selected_index"]]))
    return context["target"], context["expected_identity"], observation["selector"], claims


def _settlement_history(samples, target, identity, selector):
    claims, previous = {}, None
    for index, sample in enumerate(samples):
        if sample["error"] is not None: return "capture_error", index, None
        try: observed_target, observed_identity, observed_selector, acquired = _observation_claims(sample["observation"])
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError): return "observation_invalid", index, None
        if not (_same(observed_target, target) and _same(observed_identity, identity) and _same(observed_selector, selector)):
            return "context_changed", index, None
        for key, value in acquired:
            if key in claims and not _same(claims[key], value): return "context_changed", index, None
            claims[key] = value
        if sample["observation"]["complete"]:
            if previous is not None and _same(previous["state"], sample["observation"]["state"]): return "stable", index, [index - 1, index]
            previous = sample["observation"]
        else: previous = None
    return None, None, None


def validate_settlement(value):
    _keys(value, {"format", "complete", "issues", "read_only", "proof_created", "target", "expected_identity", "selector",
                  "interval", "max_samples", "started_monotonic", "deadline_monotonic", "finished_monotonic", "samples", "stable_pair"})
    _require(value["format"] == SETTLEMENT and type(value["complete"]) is bool and value["read_only"] is True and
             value["proof_created"] is False, "closed read-only owned-AX settlement required")
    target, identity, selector, deadline, interval, maximum, started = _settlement_inputs(
        value["target"], value["expected_identity"], value["selector"], value["deadline_monotonic"], value["interval"], value["max_samples"], value["started_monotonic"])
    finished = _moment(value["finished_monotonic"])
    _require(finished >= started and type(value["samples"]) is list and len(value["samples"]) <= maximum,
             "bounded monotonic owned-AX settlement required")
    previous_end = None
    for index, sample in enumerate(value["samples"]):
        _keys(sample, {"index", "started_monotonic", "finished_monotonic", "observation", "error"})
        begin, end = _moment(sample["started_monotonic"]), _moment(sample["finished_monotonic"])
        _require(type(sample["index"]) is int and sample["index"] == index and started <= begin < deadline and
                 begin <= end <= finished and (previous_end is None or begin >= previous_end + interval), "owned-AX sample cadence/deadline changed")
        if sample["error"] is not None:
            _keys(sample["error"], {"type"})
            _require(sample["observation"] is None and _text(sample["error"]["type"], empty=False, maximum=128), "actual capture exception required")
        else:
            _require(type(sample["observation"]) is dict and
                     len(json.dumps(sample["observation"], ensure_ascii=False, allow_nan=False).encode()) <= MAX_OBSERVATION_BYTES,
                     "bounded retained owned-AX observation required")
        previous_end = end
    reason, stop, pair = _settlement_history(value["samples"], target, identity, selector)
    _require(stop is None or stop == len(value["samples"]) - 1, "owned-AX settlement passed its FIRST stopping event")
    if value["complete"]:
        _require(value["issues"] == [] and reason == "stable" and _same(value["stable_pair"], pair) and finished < deadline,
                 "owned-AX settlement needs its first consecutive complete whole-state pair")
    else:
        _require(value["stable_pair"] is None and type(value["issues"]) is list and len(value["issues"]) == 1, "incomplete settlement cannot expose a pair")
        issue = value["issues"][0]; _keys(issue, {"kind"})
        if issue["kind"] == "deadline": _require(finished >= deadline, "owned-AX deadline was not reached")
        elif issue["kind"] == "sample_limit": _require(reason is None and len(value["samples"]) == maximum, "owned-AX sample limit was not reached")
        else: _require(reason in {"capture_error", "observation_invalid", "context_changed"} and issue["kind"] == reason,
                       "owned-AX failure differs from retained observations")
    return copy.deepcopy(value)


def _settle(target, identity, selector, *, deadline, interval, max_samples, backend, clock=time.monotonic, sleep=time.sleep, started=None):
    started = _moment(clock()) if started is None else started
    target, identity, selector, deadline, interval, maximum, started = _settlement_inputs(target, identity, selector, deadline, interval, max_samples, started)
    result = {"format": SETTLEMENT, "complete": False, "issues": [], "read_only": True, "proof_created": False,
              "target": target, "expected_identity": identity, "selector": selector, "interval": interval, "max_samples": maximum,
              "started_monotonic": started, "deadline_monotonic": deadline, "finished_monotonic": started, "samples": [], "stable_pair": None}
    reason, pair = None, None
    while len(result["samples"]) < maximum:
        if clock() >= deadline: reason = "deadline"; break
        if result["samples"]:
            remaining = deadline - clock()
            if remaining <= 0: reason = "deadline"; break
            sleep(min(interval, remaining))
            if clock() >= deadline: reason = "deadline"; break
        begin = _moment(clock())
        if begin >= deadline: reason = "deadline"; break
        observation, error = None, None
        try: observation = copy.deepcopy(_capture(target, identity, selector, deadline=deadline, backend=backend, clock=clock))
        except Exception as failure: error = {"type": type(failure).__name__}
        result["samples"].append({"index": len(result["samples"]), "started_monotonic": begin,
                                  "finished_monotonic": _moment(clock()), "observation": observation, "error": error})
        reason, _, pair = _settlement_history(result["samples"], target, identity, selector)
        if clock() >= deadline: reason, pair = "deadline", None
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
    """No expected state is supplied. Unknown focus is retained, never input permission."""
    R.assert_guest()
    target, identity, selector, deadline, interval, maximum, started = _settlement_inputs(
        target, expected_identity, window_selector, deadline, interval, max_samples, time.monotonic())
    return _settle(target, identity, selector, deadline=deadline, interval=interval, max_samples=maximum,
                   backend=_Mac(), clock=time.monotonic, sleep=time.sleep, started=started)


class _Mac(S._Mac):
    """Fixed read APIs. Construction also guards direct internal callers."""
    def __init__(self):
        R.assert_guest()
        super().__init__()

    def focus(self, identity, window, deadline):
        def query(*, local):
            result = _not_attempted(local=local)
            try:
                _require(time.monotonic() < deadline, "owned-AX focus deadline")
                root = self.application(identity["pid"]) if local else self.A.AXUIElementCreateSystemWide()
                self.timeout(root, min(AX_SECONDS, deadline - time.monotonic()))
                code, element = self.attribute(root, "AXFocusedWindow" if local else "AXFocusedApplication")
                result["code"] = code if type(code) is int else None
                _require(type(code) is int and code == 0 and element is not None, "AX focus attribute unavailable")
                _require(time.monotonic() < deadline, "owned-AX focus owner deadline")
                self.timeout(element, min(AX_SECONDS, deadline - time.monotonic()))
                result["pid"] = self.owner(element)
                if local: result["selected_window_match"] = self.equal(element, window)
                result.update(status="observed", error=None)
            except Exception as failure:
                result.update(status="unavailable", error={"type": type(failure).__name__,
                    "message": "local AXFocusedWindow query unavailable" if local else "global AXFocusedApplication query unavailable"})
            return result
        return {"global": query(local=False), "local": query(local=True)}

    def visible_windows(self, deadline):
        _require(time.monotonic() < deadline, "owned-AX Quartz inventory deadline")
        Q = self.Q
        rows = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID)
        # The SDK materializes the inventory before the retained-row cap.
        _require(rows is not None and len(rows) <= MAX_WINDOWS, "bounded complete onscreen Quartz inventory required")
        windows = []
        for row in rows:
            _require(time.monotonic() < deadline, "owned-AX Quartz row deadline")
            bounds = row.get(Q.kCGWindowBounds)
            _require(bounds is not None and self.F.CFGetTypeID(bounds) == self.F.CFDictionaryGetTypeID(), "typed Quartz bounds required")
            def scalar(key): return S._scalar(row[key], self.native_scalar)
            def bound(key): return S._scalar(bounds[key], self.native_scalar)
            windows.append({"number": scalar(Q.kCGWindowNumber), "pid": scalar(Q.kCGWindowOwnerPID),
                "layer": scalar(Q.kCGWindowLayer), "alpha": float(scalar(Q.kCGWindowAlpha)),
                "on_screen": scalar(Q.kCGWindowIsOnscreen), "position": _vector([bound("X"), bound("Y")]),
                "size": _vector([bound("Width"), bound("Height")], size=True),
                "owner_name": scalar(Q.kCGWindowOwnerName) if Q.kCGWindowOwnerName in row else None,
                "name": scalar(Q.kCGWindowName) if Q.kCGWindowName in row else None})
        processes = []
        for bundle in AUTHORIZATION_BUNDLE_IDS:
            _require(time.monotonic() < deadline, "owned-AX authorization inventory deadline")
            apps = self.K.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
            _require(apps is not None and len(apps) + len(processes) <= MAX_AUTHORIZATION_PROCESSES, "bounded authorization-process inventory required")
            processes.extend({"bundle_id": bundle, "pid": int(app.processIdentifier())} for app in apps)
        _require(time.monotonic() < deadline, "owned-AX inventory deadline")
        return _inventory({"windows": windows, "authorization_processes": processes})
