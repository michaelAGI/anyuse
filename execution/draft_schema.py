"""The shape a draft must have before the seam reads it.

``check_watcher_draft`` and ``check_drive_recording`` walk every field the loaders use and
raise ``SchemaError`` with the field's path and what was wrong (a type, a length cap, an
unknown tag, an unreadable timestamp). The API turns that into a 400 with the reason; nothing
past this point has to guess what a malformed object meant. Unknown extra keys are allowed.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional

#: length caps (characters)
MAX_NAME = 120            # a workflow name
MAX_GOAL = 2000
MAX_LABEL = 500           # a selector name / title, an intent, a target
MAX_TYPED = 20000         # the text typed in one step
MAX_SHORT = 200           # ids, roles, paths, app names, timestamps
MAX_EVENTS = 2000
MAX_ELEMENTS = 5000       # required elements of one fingerprint
MAX_STATES = 2000
_VAULT_MARKER = re.compile(r"\[\[ent:[a-z_]{1,32}:[0-9a-f]{12}\]\]")
TAGS = ("WORKFLOW_START", "WORKFLOW_END", "STEP", "INTENT", "ACTION", "CONTEXT_CHANGE", "OBSERVATION",
        "DECISION", "NEED_CLARIFICATION", "UNDERSTOOD", "TASK_SWITCH", "CONTEXT_LABEL")


class SchemaError(ValueError):
    """A draft field has the wrong type or size; the message names the field."""


def _fail(path: str, what: str) -> None:
    raise SchemaError(f"malformed draft: {path} {what}")


def _str(obj: Dict[str, Any], key: str, path: str, cap: int, required: bool = False,
         nullable: bool = True) -> None:
    if key not in obj or (obj[key] is None and nullable and not required):
        if required:
            _fail(f"{path}.{key}", "is missing")
        return
    v = obj[key]
    if not isinstance(v, str):
        _fail(f"{path}.{key}", f"must be text, not {type(v).__name__}")
    if required and not v.strip():
        _fail(f"{path}.{key}", "is empty")
    if len(v) > cap:
        _fail(f"{path}.{key}", f"is longer than {cap} characters ({len(v)})")


def _int(obj: Dict[str, Any], key: str, path: str, required: bool = False) -> None:
    if key not in obj or obj[key] is None:
        if required:
            _fail(f"{path}.{key}", "is missing")
        return
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, int):
        _fail(f"{path}.{key}", f"must be a whole number, not {type(v).__name__}")


def _dict(v: Any, path: str) -> Dict[str, Any]:
    if not isinstance(v, dict):
        _fail(path, f"must be an object, not {type(v).__name__}")
    return v


def _list(v: Any, path: str, cap: int) -> list:
    if not isinstance(v, list):
        _fail(path, f"must be a list, not {type(v).__name__}")
    if len(v) > cap:
        _fail(path, f"has more than {cap} entries")
    return v


def _str_list(obj: Dict[str, Any], key: str, path: str, cap: int = 200) -> None:
    if obj.get(key) is None:
        return
    for i, v in enumerate(_list(obj[key], f"{path}.{key}", cap)):
        if not isinstance(v, str) or len(v) > MAX_LABEL:
            _fail(f"{path}.{key}[{i}]", "must be text of at most 500 characters")


def _timestamp(obj: Dict[str, Any], key: str, path: str, required: bool) -> None:
    _str(obj, key, path, MAX_SHORT, required=required)
    if obj.get(key):
        try:
            datetime.fromisoformat(obj[key])
        except ValueError:
            _fail(f"{path}.{key}", f"is not an ISO timestamp ({obj[key][:40]!r})")


def check_selector(v: Any, path: str) -> None:
    if v is None:
        return
    sel = _dict(v, path)
    for k in ("canonical_role", "identifier", "path", "raw_role"):
        _str(sel, k, path, MAX_SHORT)
    _str(sel, "name", path, MAX_LABEL)
    # the runner contract: a selector kind (``focused``), a row's
    # container role, the app (bundle id) and the document the element was in
    for k in ("kind", "app"):
        _str(sel, k, path, MAX_SHORT)
    _str(sel, "scope", path, MAX_SHORT)        # the window's container scope, a salted key
    _str(sel, "document", path, MAX_LABEL)
    check_container(sel, path)


def check_start(v: Any, path: str) -> None:
    """A text mode's start shape (``ax_actions.start_shape``): ``{lines, anchors: {<anchor>:
    [line index]}, breaks?}``, no text of the field."""
    if v is None:
        return
    d = _dict(v, path)
    _int(d, "lines", path, required=True)
    _str(d, "breaks", path, MAX_SHORT)
    anchors = _dict(d.get("anchors") if d.get("anchors") is not None else {}, f"{path}.anchors")
    if len(anchors) > 20:
        _fail(f"{path}.anchors", "has more than 20 entries")
    for k, hits in anchors.items():
        if len(k) > MAX_LABEL:
            _fail(f"{path}.anchors", f"has a key longer than {MAX_LABEL} characters")
        for i, h in enumerate(_list(hits, f"{path}.anchors[{k[:40]!r}]", 50)):
            if isinstance(h, bool) or not isinstance(h, int):
                _fail(f"{path}.anchors[{k[:40]!r}][{i}]", "must be a line index")


def check_container(obj: Dict[str, Any], path: str) -> None:
    """A row's list: a role, or ``{role, identifier, description, path}`` (its identity)."""
    v = obj.get("container")
    if v is None or isinstance(v, str):
        _str(obj, "container", path, MAX_SHORT)
        return
    box = _dict(v, f"{path}.container")
    for k in ("role", "identifier", "description", "path"):
        _str(box, k, f"{path}.container", MAX_LABEL)


def check_states(v: Any, path: str) -> None:
    """``{state_id: UIState}`` - only the fingerprint is read."""
    if v is None:
        return
    states = _dict(v, path)
    if len(states) > MAX_STATES:
        _fail(path, f"has more than {MAX_STATES} states")
    for sid, st in states.items():
        p = f"{path}.{sid}"
        if len(str(sid)) > MAX_SHORT:
            _fail(p, "has a state id longer than 200 characters")
        st = _dict(st, p)
        _str(st, "state_id", p, MAX_SHORT)
        _str(st, "app", p, MAX_SHORT)
        if st.get("fingerprint") is None:
            continue
        fp = _dict(st["fingerprint"], f"{p}.fingerprint")
        fpp = f"{p}.fingerprint"
        for i, el in enumerate(_list(fp.get("required_elements") or [], f"{fpp}.required_elements",
                                     MAX_ELEMENTS)):
            el = _dict(el, f"{fpp}.required_elements[{i}]")
            _str(el, "role", f"{fpp}.required_elements[{i}]", MAX_SHORT)
            _str(el, "title", f"{fpp}.required_elements[{i}]", MAX_LABEL)
            _str(el, "identifier", f"{fpp}.required_elements[{i}]", MAX_SHORT)
        if fp.get("element_count_range") is not None:
            r = _list(fp["element_count_range"], f"{fpp}.element_count_range", 2)
            if len(r) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in r) or r[0] > r[1]:
                _fail(f"{fpp}.element_count_range", "must be two whole numbers, low to high")
        _str(fp, "structural_hash", fpp, MAX_SHORT)
        if fp.get("role_frequency") is not None:
            for role, n in _dict(fp["role_frequency"], f"{fpp}.role_frequency").items():
                if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > 100000:
                    _fail(f"{fpp}.role_frequency.{role}", "must be a count")


def check_watcher_draft(d: Any) -> None:
    d = _dict(d, "draft")
    _str(d, "name", "draft", MAX_NAME, required=True)
    _str(d, "goal", "draft", MAX_GOAL)
    for k in ("session_id", "app", "bundle_id", "taught_by", "workflow_id"):
        _str(d, k, "draft", MAX_SHORT)
    _str_list(d, "apps_involved", "draft")
    _str_list(d, "urls_visited", "draft")
    if d.get("is_partial") is not None and not isinstance(d["is_partial"], bool):
        _fail("draft.is_partial", "must be true or false")
    for i, ev in enumerate(_list(d.get("events") if "events" in d else None, "draft.events", MAX_EVENTS)):
        p = f"draft.events[{i}]"
        ev = _dict(ev, p)
        _str(ev, "id", p, MAX_SHORT, required=True)
        _str(ev, "tag_type", p, MAX_SHORT, required=True)
        if ev["tag_type"] not in TAGS:
            _fail(f"{p}.tag_type", f"is not a watcher tag ({ev['tag_type'][:40]!r})")
        _timestamp(ev, "timestamp", p, required=True)
        for k in ("reason", "context", "expected_result", "target"):
            _str(ev, k, p, MAX_LABEL)
        for k in ("workflow_name", "anchor_id", "ax_state_id"):
            _str(ev, k, p, MAX_SHORT)
        for k in ("x", "y"):
            _int(ev, k, p)
        if ev.get("confidence") is not None and (isinstance(ev["confidence"], bool)
                                                 or not isinstance(ev["confidence"], (int, float))):
            _fail(f"{p}.confidence", "must be a number")
        if ev.get("data") is None:
            continue
        data = _dict(ev["data"], f"{p}.data")
        dp = f"{p}.data"
        for k in ("action", "goal", "description", "name"):
            _str(data, k, dp, MAX_LABEL)
        for k in ("text", "value"):
            _str(data, k, dp, MAX_TYPED)
        # the structured step capture: a vault marker instead of the text, a secure-field
        # flag, and why a step's identity and value were withheld
        _str(data, "value_ref", dp, MAX_SHORT)
        if data.get("value_ref") and not _VAULT_MARKER.fullmatch(data["value_ref"]):
            _fail(f"{dp}.value_ref", "is not an entity-vault marker [[ent:<class>:<12 hex>]]")
        for k in ("withheld", "via", "source", "app", "bundle_id", "window_title_ref"):
            _str(data, k, dp, MAX_SHORT)
        if data.get("secure") is not None and not isinstance(data["secure"], bool):
            _fail(f"{dp}.secure", "must be true or false")
        # the runner contract: the field focused after the previous step, the role a trigger
        # focused, a hop the map could not ground, a typing step's input mode and anchor
        for k in ("focused", "map_hop"):
            if data.get(k) is not None and not isinstance(data[k], bool):
                _fail(f"{dp}.{k}", "must be true or false")
        for k in ("focuses", "input_mode", "role", "target_state", "end_state"):
            _str(data, k, dp, MAX_SHORT)
        # a state-aware toggle's observed end state
        if data.get("checked") is not None and not isinstance(data["checked"], bool):
            _fail(f"{dp}.checked", "must be true or false")
        check_container(data, dp)
        _str(data, "anchor", dp, MAX_LABEL)
        check_start(data.get("start"), f"{dp}.start")
        for k in ("keycode", "flags"):
            _int(data, k, dp)
        keys = data.get("keys", data.get("key"))
        if keys is not None and not (isinstance(keys, str) and len(keys) <= MAX_SHORT) and not (
                isinstance(keys, list) and len(keys) <= 8 and all(isinstance(k, str) and len(k) <= 40 for k in keys)):
            _fail(f"{dp}.keys", "must be a key name or a short list of key names")
        check_selector(data.get("selector"), f"{dp}.selector")
    for i, a in enumerate(_list(d.get("anchors") or [], "draft.anchors", MAX_EVENTS)):
        p = f"draft.anchors[{i}]"
        a = _dict(a, p)
        _str(a, "id", p, MAX_SHORT, required=True)
        _timestamp(a, "timestamp", p, required=True)
        _int(a, "x", p, required=True)
        _int(a, "y", p, required=True)
        _str(a, "filename", p, MAX_SHORT, required=True)
        for k in ("app", "tag_event_id"):
            _str(a, k, p, MAX_SHORT)
        _str(a, "target_description", p, MAX_LABEL)
        if a.get("size") is not None:
            sz = _list(a["size"], f"{p}.size", 2)
            if len(sz) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in sz):
                _fail(f"{p}.size", "must be two whole numbers")
    check_states(d.get("ax_states"), "draft.ax_states")


def check_drive_recording(d: Any) -> None:
    d = _dict(d, "recording")
    wf = _dict(d.get("workflow"), "recording.workflow")
    _str(wf, "name", "recording.workflow", MAX_NAME)
    _str(wf, "description", "recording.workflow", MAX_GOAL)
    _str(wf, "required_app", "recording.workflow", MAX_SHORT)
    for i, st in enumerate(_list(wf.get("steps") or [], "recording.workflow.steps", MAX_EVENTS)):
        st = _dict(st, f"recording.workflow.steps[{i}]")
        _str(st, "expected_before_state", f"recording.workflow.steps[{i}]", MAX_SHORT)
    for i, p in enumerate(_list(d.get("plan"), "recording.plan", MAX_EVENTS)):
        pp = f"recording.plan[{i}]"
        p = _dict(p, pp)
        _str(p, "intent", pp, MAX_LABEL)
        _str(p, "input_data", pp, MAX_TYPED)
        cmd = _dict(p.get("command"), f"{pp}.command")
        for k in ("role", "action"):
            _str(cmd, k, f"{pp}.command", MAX_SHORT)
        _str(cmd, "title", f"{pp}.command", MAX_LABEL)
        for k in ("keycode", "flags"):
            _int(cmd, k, f"{pp}.command")
        check_selector(cmd.get("selector"), f"{pp}.command.selector")
    g = _dict(d.get("graph"), "recording.graph")
    check_states(g.get("states"), "recording.graph.states")
    for k in ("text", "name"):
        _str(d, k, "recording", MAX_TYPED)
    for k in ("recorded_at", "taught_by"):
        _str(d, k, "recording", MAX_SHORT)


def check(data: Any, kind: str) -> None:
    (check_drive_recording if kind == "drive_recording" else check_watcher_draft)(data)


__all__ = ("SchemaError", "check", "check_watcher_draft", "check_drive_recording", "MAX_NAME")
