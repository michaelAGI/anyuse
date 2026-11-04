"""The fresh-input proof of a workflow compiled from a draft.

A workflow is ``ready`` only after it replays correctly with parameter values that were NOT
in the recording, on at least ``MIN_FRESH_CASES`` value sets (the floor the web and desktop
compilers use). A recording with no parameter and no typed text is a fixed routine: one replay,
its effects exactly the recorded ones (``fixed_routine`` in the proof). The replay is the real
one - ``draft_compile.run_steps``: approval holds,
then ``StateGraphExecutor.execute_checked_step`` with the drift gate on - against
``SimAXWorld``, a fake AX tree built from the recording itself:

* its elements are the recorded targets (their recorded selectors), plus, for a ``choice``
  parameter, the other items of the same role the recording saw in that step's state;
* its snapshot before each step is the recorded fingerprint of that step's state (the same
  role distribution and required elements), so the drift gate compares like with like, and
  a removed element makes it fire;
* every press, text entry and key is logged as an effect;
* a state-aware toggle's target (``set_toggle``) has a state the world models, and
  every case runs from BOTH start states (all toggled elements on, then all off): the press is expected
  only where the start state is not the target, so a blind press or a missed one fails the case.

A case passes when the replay completed, its effects are EXACTLY the recorded effects with
the new values at the places the parameters' candidates occurred (so a missed or an extra
binding fails), no recorded parameter value appears in any effect, and every gated step was
held before its effect. What it cannot show is the app's own behaviour (that a save writes
the file): the guest replay does (the measurement harness ``draft-replay``).
Nothing here sends input; the world is a Python object.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.context.accessibility import ACTIONABLE_ROLES
from anyuse.execution import draft_params as DP
from anyuse.execution.desktop_compile import MIN_FRESH_CASES

HARNESS = "sim_ax"
SCOPE = ("binding and the checked executor path (identity check, drift gate, approval holds) on "
         "the recorded AX world; the app's own effect is shown by the guest replay")
#: menu items live in the menu bar, which the r2 drift profile leaves out of fingerprints:
#: their presence cannot be read from a recorded state
_UNCHECKABLE_ROLES = ("AXMenuItem", "AXMenuBarItem", "AXMenu", "AXMenuBar", "")
#: a fingerprint's required elements are the ACTIONABLE ones only (memory/recorder.py): a cell,
#: a row, static text or an image is never listed, so its presence cannot be read either
_FINGERPRINTED_ROLES = ACTIONABLE_ROLES
_OP = {"press": "AXPress", "toggle": "AXPress", "confirm": "AXConfirm", "cancel": "AXCancel",
       "enter_text": "enter_text", "press_key": "key",
       # the runner contract (execution/ax_actions.py): logged with the action's own name
       "select_row": "select_row", "append_text": "append_text", "replace_line": "replace_line",
       "open_app": "open_app", "set_toggle": "AXPress", "press_shortcut": "key"}
#: the start states every case of a record with a ``set_toggle`` runs from (a wrong start state is exercised)
TOGGLE_STARTS = ("on", "off")
#: effects that carry the typed value
_TEXT_OPS = ("enter_text", "append_text", "replace_line")


def describe(sel: Dict[str, Any]) -> str:
    bits = [str(sel.get("canonical_role") or "unknown")]
    if sel.get("kind") == "focused":
        bits.insert(0, "focused")                 # "focused:textfield": the focused element
    if sel.get("identifier"):
        bits.append("#" + str(sel["identifier"]))
    elif sel.get("name"):
        bits.append(f"'{sel['name']}'")
    if sel.get("path"):
        bits.append("@" + str(sel["path"]))
    return ":".join(bits)


class _Element:
    __slots__ = ("sel", "raw_role", "value", "state")

    def __init__(self, sel: Dict[str, Any], raw_role: str):
        self.sel, self.raw_role, self.value = dict(sel), raw_role, ""
        #: a ``set_toggle`` target's state (on / off); None for any other element (unreadable: refused)
        self.state: Optional[str] = None


class _Journal:
    """What every world of one proof case logs, in one sequence: effects, approval holds, the
    current step, which element gets keyboard focus after which step, and the app in front."""

    def __init__(self):
        self.effects: List[Dict[str, Any]] = []
        self.holds: List[Tuple[int, int]] = []      # (sequence, step)
        self.seq = 0
        self.step = -1
        #: step -> (world, element): the element that has focus once that step acted
        self.focus_after: Dict[int, Tuple["SimAXWorld", "_Element"]] = {}
        self.front = ""

    def hold(self) -> None:
        self.seq += 1
        self.holds.append((self.seq, self.step))

    def effect(self, op: str, target: str, value: Optional[str]) -> None:
        self.seq += 1
        self.effects.append({"seq": self.seq, "step": self.step, "op": op, "target": target, "value": value})
        moved = self.focus_after.get(self.step)
        if moved is not None:
            moved[0].focused = moved[1]


class _SimEdit:
    """``AccessibilityTree.edit_text``'s evidence, as far as the handlers read it."""

    def __init__(self, readback: Optional[str], matches: bool, error: str = ""):
        self.readback, self.readback_matches, self.error = readback, matches, error

    def __bool__(self) -> bool:
        return self.readback_matches


def _splice16(value: str, start: int, length: int, text: str) -> Optional[str]:
    """``value`` with the UTF-16 range ``(start, length)`` replaced by ``text`` (an AX range)."""
    u = value.encode("utf-16-le")
    a, b = 2 * int(start), 2 * (int(start) + int(length))
    if start < 0 or length < 0 or b > len(u):
        return None
    try:
        return (u[:a] + text.encode("utf-16-le") + u[b:]).decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def anchor_texts(record: Dict[str, Any], values: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Each replace_line's recorded anchor (a ``typed_text`` marker, or a literal) -> the text the
    case's field holds for it: the case's value of the step's anchor parameter (``${anker_<n>}``,
    ``draft_compile._anchor_parameters``), else the recorded anchor itself."""
    out: Dict[str, str] = {}
    recorded = record.get("recorded") or []
    for k, s in enumerate(record.get("steps") or []):
        cmd = s.get("command") or {}
        if cmd.get("action") != "replace_line" or k >= len(recorded):
            continue
        rec_anchor = str((recorded[k].get("command") or {}).get("anchor") or "")
        m = re.fullmatch(r"\$\{([^{}\s]{1,40})\}", str(cmd.get("anchor") or ""))
        if rec_anchor:
            out[rec_anchor] = str(values[m.group(1)]) if m and values and m.group(1) in values else rec_anchor
    return out


def start_of(record: Dict[str, Any], k: int, values: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The value of the field recorded step ``k`` edits with a text mode, right before it: the
    recorded start shape rebuilt (``ax_actions.start_value``), empty lines except the anchor's,
    which holds the case's anchor value (``anchor_texts``). None when the recording kept no
    shape. Never invented, and no text of the recorded document is in it."""
    from anyuse.execution import ax_actions as AX
    recorded = record.get("recorded") or []
    r = recorded[k] if k < len(recorded) else {}
    return AX.start_value(r.get("start"), anchor_texts(record, values)) if r.get("start") is not None else None


def start_problems(record: Dict[str, Any]) -> List[str]:
    """Why the record's text modes cannot be proved (``ax_actions.start_problem``): each
    append_text / replace_line step needs the recorded start shape of its field, and a
    replace_line its recorded anchor in a line of it."""
    from anyuse.execution import ax_actions as AX
    out = []
    for k, r in enumerate(record.get("recorded") or []):
        cmd = r.get("command") or {}
        act = cmd.get("action")
        if act not in ("append_text", "replace_line"):
            continue
        why = AX.start_problem(r.get("start"), str(cmd.get("anchor") or ""), act)
        if why:
            out.append(f"step {k}: {why}")
    return out


def _key(sel: Dict[str, Any]) -> Tuple:
    """One element per recorded identity (a row's includes its list's)."""
    box = sel.get("container")
    return (sel.get("canonical_role"), sel.get("identifier"), sel.get("name"), sel.get("path"), sel.get("kind"),
            repr(sorted(box.items())) if isinstance(box, dict) else repr(box))


def _step_key(step: Dict[str, Any]) -> str:
    from anyuse.execution.draft_compile import _app_key
    return _app_key(str(step.get("app") or ""), str(step.get("bundle_id") or ""))


def _occurs_in(record: Dict[str, Any], param: Dict[str, Any], k: int) -> bool:
    recorded = record.get("recorded") or []
    return k < len(recorded) and recorded[k].get("draft_index") in (param.get("occurrences") or [])


class _ListOf:
    """A world row's list, as ``SimAXWorld.attr`` shows it (its recorded identity)."""
    __slots__ = ("box",)

    def __init__(self, box: Dict[str, Any]):
        self.box = box


class SimAXWorld:
    """The recorded app as a fake ``AccessibilityTree`` (the methods the executor calls).

    ``app`` (a multi-app record, ``SimMultiAppWorld``): only the steps recorded in that app make
    its elements, and its ``bundle_id()`` is that app's. Rows are elements of role row named by
    their label (``select_row`` selects one; a twin label is ambiguous); the element a
    ``focused`` step types into gets keyboard focus once the step before it acted; a field a
    text mode edits starts with the recorded start shape (``start_of``), its anchor line holding
    the case's anchor value, never with an invented line or the recorded document's text."""

    def __init__(self, record: Dict[str, Any], *, drop_titles: Sequence[str] = (), app: Optional[str] = None,
                 journal: Optional[_Journal] = None, multi: Optional["SimMultiAppWorld"] = None,
                 bundle: str = "", app_name: str = ""):
        self.record = record
        self.states = ((record.get("state_graph") or {}).get("states") or {})
        self.before = [s.get("expected_before_state") or ""
                       for s in ((record.get("workflow_template") or {}).get("steps") or [])]
        self.drop = set(drop_titles)
        self.elements: List[_Element] = []
        self.absent: List[Dict[str, Any]] = []
        self.journal = journal or _Journal()
        self.multi = multi
        self.app = app
        self.bundle = bundle or str(record.get("bundle_id") or "")
        self.app_name = app_name or str(record.get("app") or "")
        self.focused: Optional[_Element] = None
        self.selected: Dict[str, List[_Element]] = {}
        #: the last ``find_selector`` that found two or more candidates (the executor reports it)
        self.last_ambiguity: Optional[Dict[str, Any]] = None
        self.step = -1
        steps = record.get("steps") or []
        recorded = record.get("recorded") or []
        mine = [k for k in range(len(recorded))
                if app is None or (k < len(steps) and _step_key(steps[k]) == app
                                   and (steps[k].get("command") or {}).get("action") != "open_app")]
        keys = set()
        #: recorded step -> the element a focused step types into
        own: Dict[int, _Element] = {}
        #: recorded step -> the field its text mode edits; the case's values (``add_row_values``)
        self.text_steps: Dict[int, _Element] = {}
        self.values: Dict[str, str] = {}
        #: the elements a ``set_toggle`` targets (their state is modelled: ``set_toggle_start``)
        self.toggled: List[_Element] = []
        edited: Dict[int, Tuple[_Element, int]] = {}
        for k in mine:
            r = recorded[k]
            cmd = r.get("command") or {}
            sel = cmd.get("selector")
            if not sel:
                continue
            if sel.get("kind") == "focused":
                prev = ((recorded[k - 1].get("command") or {}).get("selector") if k > 0 else None) or {}
                if prev.get("kind") == "focused" and k - 1 in own:
                    # the trigger is itself a focused step (a Return after the focused typing):
                    # the same element keeps the focus (``ax_actions.resolve_focused``)
                    el = own[k - 1]
                else:
                    el = _Element(sel, str(cmd.get("role") or sel.get("raw_role") or ""))
                    self.journal.focus_after[k - 1] = (self, el)
                    if k == 0:
                        self.focused = el
                own[k] = el
            else:
                key = _key(sel)
                if key in keys:
                    el = next(e for e in self.elements if _key(e.sel) == key)
                else:
                    keys.add(key)
                    el = _Element(sel, str(cmd.get("role") or ""))
                    self.elements.append(el)
            if cmd.get("action") in ("append_text", "replace_line"):
                edited.setdefault(id(el), (el, k))
                self.text_steps[k] = el
            if cmd.get("action") == "set_toggle" and el not in self.toggled:
                self.toggled.append(el)
        # a field a text mode edits starts as the recording saw it before its first edit (the
        # recorded shape, ``start_of``), and again before each later one (``at_step``)
        for el, k in edited.values():
            el.value = start_of(record, k) or ""
        #: the fields a text mode edits: their value shows on the recorded screen too
        self.edited: List[_Element] = [el for el, _k in edited.values()]
        self._first_edit = {k: el for el, k in edited.values()}
        options = choice_options(record)
        for p in record.get("parameters") or []:
            if app is not None and not any(
                    _occurs_in(record, p, k) for k in mine):
                continue
            for role, raw, title, ident in options.get(p["name"], []):
                if any(e.sel.get("canonical_role") == role and e.sel.get("name") == title
                       for e in self.elements):
                    continue
                self.elements.append(_Element({"canonical_role": role, "name": title,
                                               "identifier": ident or "", "path": "", "raw_role": raw},
                                              raw))

    @property
    def effects(self) -> List[Dict[str, Any]]:
        return self.journal.effects

    @property
    def holds(self) -> List[Tuple[int, int]]:
        return self.journal.holds

    def add_row_values(self, values: Dict[str, str]) -> None:
        """The case's values the world must show: one row per value of a parameter that names a
        row (``select_row`` with ``${name}`` as its label), unless a row of that label exists, and
        a replace_line's anchor parameter in the line it replaces."""
        steps = self.record.get("steps") or []
        for k, s in enumerate(steps):
            if self.app is not None and _step_key(s) != self.app:
                continue
            cmd = s.get("command") or {}
            sel = cmd.get("selector") or {}
            m = re.fullmatch(r"\$\{([^{}\s]{1,40})\}", str(sel.get("name") or ""))
            if cmd.get("action") != "select_row" or not m or m.group(1) not in values:
                continue
            label = str(values[m.group(1)])
            if any(e.sel.get("kind") == "row" and e.sel.get("name") == label
                   and e.sel.get("container") == sel.get("container") for e in self.elements):
                continue
            self.elements.append(_Element(dict(sel, name=label, identifier="", path=""), "AXRow"))
        # a replace_line whose anchor is a parameter (``draft_compile._anchor_parameters``): the
        # field it edits holds this case's anchor value in the line the recording's anchor was in
        self.values = dict(values)
        for k, el in self._first_edit.items():
            el.value = start_of(self.record, k, self.values) or ""

    def set_toggle_start(self, start: str) -> None:
        """Every element a ``set_toggle`` targets starts in ``start`` (on / off)."""
        for el in self.toggled:
            el.state = start

    # -- the runner's hook -----------------------------------------------------------------
    def at_step(self, k: int) -> None:
        self.step = k
        self.journal.step = k
        el = self.text_steps.get(k)
        if el is not None and k not in self._first_edit:
            # a later edit of the field: it starts as the recording saw it right before this step
            got = start_of(self.record, k, self.values)
            if got is not None:
                el.value = got

    def note_hold(self) -> None:
        self.journal.hold()

    def _effect(self, op: str, target: str, value: Optional[str]) -> None:
        self.journal.effect(op, target, value)

    def _present(self, e: "_Element") -> bool:
        """Is the target in the RECORDED state before this step? Read from that state's fingerprint
        (its required elements: same role, and the same title unless the recorded one has none);
        True when there is no fingerprint, it lists nothing, or the role is a menu's or one a
        fingerprint never lists (not actionable: a cell, a row, static text)."""
        sid = self.before[self.step] if 0 <= self.step < len(self.before) else ""
        state = self.states.get(sid) if sid else None
        if state is None or e.raw_role in _UNCHECKABLE_ROLES or e.raw_role not in _FINGERPRINTED_ROLES:
            return True
        req = (state.get("fingerprint") or {}).get("required_elements") or []
        if not req:
            return True
        name = e.sel.get("name") or ""
        return any(r.get("role") == e.raw_role and (not r.get("title") or r.get("title") == name) for r in req)

    # -- the AccessibilityTree surface the executor uses --------------------------------------
    def find_selector(self, selector, document=None):
        self.last_ambiguity = None
        container = str(getattr(selector, "container", "") or "")
        hits = []
        for e in self.elements:
            if e.sel.get("name") in self.drop:
                continue
            if e.sel.get("canonical_role") != selector.canonical_role:
                continue
            if container and e.sel.get("container") not in (None, "", container):
                continue
            if selector.identifier:
                ok = e.sel.get("identifier") == selector.identifier
            elif selector.name:
                ok = e.sel.get("name") == selector.name
            else:
                ok = bool(selector.path) and e.sel.get("path") == selector.path
            if ok:
                hits.append(e)
        if len(hits) > 1:
            # two or more candidates are an ambiguity, refused (the tree's _unique rule)
            self.last_ambiguity = {"n": len(hits), "why": f"{len(hits)} candidates match {describe(hits[0].sel)}"}
        if len(hits) != 1:
            return None
        if not self._present(hits[0]):
            miss = {"step": self.step, "target": describe(hits[0].sel)}
            if miss not in self.absent:
                self.absent.append(miss)
            return None
        return hits[0]

    def to_selector(self, element):
        from anyuse.context.accessibility import Selector
        return Selector.from_dict(element.sel)

    def find(self, role=None, title=None):
        return [e for e in self.elements if (role is None or e.raw_role == role)
                and (title is None or e.sel.get("name") == title)]

    def perform_action(self, element, action: str) -> bool:
        self._effect(action, describe(element.sel), None)
        if action == "AXPress" and getattr(element, "state", None) in TOGGLE_STARTS:
            element.state = "off" if element.state == "on" else "on"
        return True

    # a state-aware toggle's surface (``ax_actions.set_toggle``): the modelled state; closing a menu is no effect
    def toggle_state(self, element) -> Optional[str]:
        return getattr(element, "state", None)

    def toggle_readback(self, element, want: str) -> Optional[str]:  # noqa: ARG002
        return getattr(element, "state", None)

    def close_menu(self, element) -> bool:  # noqa: ARG002
        return True

    def set_value(self, element, value) -> bool:
        element.value = str(value)
        self._effect("enter_text", describe(element.sel), str(value))
        return True

    def enter_text(self, element, text: str, **_kw):
        from anyuse.context.accessibility import TextEntryResult
        element.value = str(text)
        self._effect("enter_text", describe(element.sel), str(text))
        return TextEntryResult(requested=str(text), rung="sim_ax_value")

    def press_key(self, keycode: int, flags: int = 0, presser=None) -> bool:
        self._effect("key", "", str(int(keycode)))
        self._keyed = self.step          # its recorded effect: the screen before the next step
        return True

    def window_counts(self) -> Optional[Dict[str, int]]:
        """``{windows, sheets}`` of the recorded screen in effect: the one before the current step, or
        before the next once this step pressed a key (a shortcut's recorded effect,
        ``ax_actions.press_shortcut``); None when that fingerprint kept no role frequencies
        (unknown, never zero)."""
        k = max(self.step, 0)
        if self.step >= 0 and getattr(self, "_keyed", None) == self.step:
            k += 1
        sid = self.before[k] if k < len(self.before) else ""
        freq = ((self.states.get(sid) or {}).get("fingerprint") or {}).get("role_frequency") if sid else None
        if not isinstance(freq, dict) or not freq:
            return None
        return {"windows": int(freq.get("AXWindow") or 0), "sheets": int(freq.get("AXSheet") or 0)}

    # -- the contract's tree surface (execution/ax_actions.py) ---------------------------------
    def bundle_id(self) -> str:
        return self.bundle

    def focused_element(self):
        return self.focused

    def read_value(self, element) -> Optional[str]:
        return element.value

    def edit_text(self, element, text: str, replace_range=None, expect_value=None):
        """A range edit, in UTF-16 units, logged with its mode: an insertion at the end of the
        value is ``append_text``, any other range ``replace_line`` (a replaced line is never
        empty: it holds its anchor)."""
        end = len(element.value.encode("utf-16-le")) // 2
        rng = tuple(replace_range) if replace_range is not None else (end, 0)
        after = _splice16(element.value, rng[0], rng[1], str(text))
        if after is None:
            return _SimEdit(element.value, False, f"invalid range {replace_range!r}")
        element.value = after
        self._effect("append_text" if rng == (end, 0) else "replace_line", describe(element.sel), str(text))
        return _SimEdit(after, expect_value is None or after == expect_value)

    @staticmethod
    def _box(element) -> str:
        return repr(sorted((element.sel.get("container") or {}).items()))

    def find_row(self, sel: Dict[str, Any]):
        """The row a ``row`` selector names (``AccessibilityTree.find_row``'s rule on the
        world's rows): its list by identity, then exactly one row of the identifier, else the
        label; ``last_row_problem`` says why not."""
        from anyuse.execution.ax_actions import container_matches, scope_candidates
        self.last_row_problem = None
        rows = [e for e in self.elements if e.sel.get("kind") == "row" and e.sel.get("name") not in self.drop
                and container_matches(e.sel.get("container"), sel.get("container"))]
        ident, name = sel.get("identifier") or "", sel.get("name") or ""
        hits = [e for e in rows if ident and e.sel.get("identifier") == ident]
        if len(hits) != 1 and name and (not ident or len(hits) > 1):
            hits = [e for e in rows if e.sel.get("name") == name]
        # a twin in a window of a DIFFERENT scope is never the row (the module's policy)
        if len(hits) > 1:
            hits = scope_candidates(self, sel, hits)
            if not hits:
                self.last_row_problem = {"code": "wrong_container",
                                         "why": f"every row {name!r} is in another folder or note list"}
                return None
        if len(hits) != 1:
            self.last_row_problem = {"code": "ambiguous" if hits else "not_found",
                                     "why": f"{len(hits)} rows {name!r} in the recorded list"}
            return None
        return hits[0]

    def row_container_identity(self, element) -> Optional[Dict[str, str]]:
        return element.sel.get("container")

    def row_scope_verdict(self, element, recorded: str) -> str:
        """``same_scope`` for the world's row (``ax_actions._verdict``): the world shows each row
        in the scope its step was recorded with (its selector's ``scope``, the salted key). The
        same key is ``same`` when it holds a full path (``scope_strength``) and ``unconfirmed``
        when it holds a name only (the recorded worlds hold no paths); another key is
        ``different``; a row without one has no readable scope ("": no container rule)."""
        from anyuse.execution.ax_actions import DIFFERENT, SAME, UNCONFIRMED, _scope_api
        own = str(element.sel.get("scope") or "") if isinstance(getattr(element, "sel", None), dict) else ""
        api = _scope_api()
        if not own or not recorded or api is None:
            return ""
        if own != recorded:
            return DIFFERENT
        return SAME if api.scope_strength(own) == "path" else UNCONFIRMED

    # the module's ``in_container_content`` reads a row through ``attr`` / ``children``: a world row
    # is an AXRow whose parent is its list (role and description as recorded)
    def attr(self, element, name: str):
        if isinstance(element, _ListOf):
            box = element.box
            if name == "AXRole":
                return {"table": "AXTable", "outline": "AXOutline", "list": "AXList"}.get(box.get("role"), "AXList")
            return box.get("description") if name == "AXDescription" else None
        sel = getattr(element, "sel", None)
        if not isinstance(sel, dict):
            return None
        if name == "AXRole":
            return element.raw_role or None
        if name == "AXParent" and sel.get("kind") == "row" and isinstance(sel.get("container"), dict):
            return _ListOf(sel["container"])
        return None

    def children(self, element) -> List[Any]:
        return []

    def select_row(self, element) -> bool:
        if element.sel.get("kind") != "row":
            return False
        self.selected[self._box(element)] = [element]
        self._effect("select_row", describe(element.sel), None)
        return True

    def deselect_row(self, element) -> bool:
        box = self.selected.get(self._box(element), [])
        if element in box:
            box.remove(element)
        return True

    def selected_rows(self, element) -> Optional[List["_Element"]]:
        return list(self.selected.get(self._box(element), []))

    def selected_row_labels(self, element) -> Optional[List[str]]:
        return [str(e.sel.get("name") or "") for e in self.selected.get(self._box(element), [])]

    def focus_context(self, trigger=None) -> Dict[str, Any]:
        """The element focused now (the world has no windows: no window check)."""
        return {"focused": self.focused, "window": None}

    def window_of(self, element):
        return None

    def activate_app(self, bundle_id: str) -> bool:
        if self.multi is not None:
            return self.multi.activate_app(bundle_id)
        self.journal.front = bundle_id
        self._effect("open_app", bundle_id, None)
        return True

    def frontmost_bundle_id(self) -> str:
        return self.journal.front or self.bundle

    def snapshot(self, row_labels: bool = False, **_kw):
        from anyuse.context.accessibility import AXElementDesc, AXSnapshot
        sid = self.before[self.step] if 0 <= self.step < len(self.before) else ""
        state = self.states.get(sid) if sid else None
        if state is None:
            els = []
            for e in self.elements:
                row = e.sel.get("kind") == "row"
                # a real row has no title of its own: its label is read only on request
                d = AXElementDesc(role=e.raw_role or None, title=None if row else (e.sel.get("name") or None),
                                  value=e.value or None,
                                  identifier=(e.sel.get("identifier") or None))
                if row and row_labels:
                    d.row_label, d.row_container = e.sel.get("name") or None, e.sel.get("container")
                    if e.sel.get("scope"):
                        d.row_scope = e.sel["scope"]
                els.append(d)
            return AXSnapshot(elements=els, app_name=self.app_name)
        fp = state.get("fingerprint") or {}
        req = [r for r in fp.get("required_elements") or [] if (r.get("title") or "") not in self.drop]
        els = [AXElementDesc(role=r.get("role") or None, title=r.get("title") or None) for r in req]
        have = Counter(r.get("role") or "unknown" for r in req)
        freq = fp.get("role_frequency") or {}
        for role, n in freq.items():
            els += [AXElementDesc(role=None if role == "unknown" else role)
                    for _ in range(max(0, int(n) - have.get(role, 0)))]
        # a field a text mode edits carries its current value and identifier on the recorded
        # screen (the declared effect of append_text / replace_line is read there): the first
        # element of its role (and title) gets them; roles, titles and counts are unchanged
        used = set()
        for e in getattr(self, "edited", ()):
            for i, d in enumerate(els):
                if i in used or d.role != e.raw_role or (d.title and d.title != (e.sel.get("name") or "")):
                    continue
                d.value, d.identifier = e.value or None, e.sel.get("identifier") or None
                used.add(i)
                break
        return AXSnapshot(elements=els, app_name=self.app_name)

    def diff(self, before, after):
        from anyuse.context.accessibility import AccessibilityTree
        return AccessibilityTree.diff(self, before, after)


class SimMultiAppWorld:
    """A multi-app record's world: one ``SimAXWorld`` per app,
    one journal (effects and holds in one sequence), and the app in front, which an
    ``open_app`` changes (logged as an effect). ``tree_for(app)`` is the runner's
    ``tree_factory``: an app's name or bundle id -> its world."""

    def __init__(self, record: Dict[str, Any], *, drop_titles: Sequence[str] = ()):
        from anyuse.execution.draft_compile import _app_key, record_apps
        self.record = record
        self.journal = _Journal()
        apps = record.get("apps") or record_apps(record.get("steps") or [], str(record.get("app") or ""),
                                                 str(record.get("bundle_id") or ""))
        self.worlds: Dict[str, SimAXWorld] = {}
        self._names: Dict[str, str] = {}
        for a in apps:
            key = _app_key(str(a.get("app") or ""), str(a.get("bundle_id") or ""))
            if not key or key in self.worlds:
                continue
            self.worlds[key] = SimAXWorld(record, drop_titles=drop_titles, app=key, journal=self.journal,
                                          multi=self, bundle=str(a.get("bundle_id") or ""),
                                          app_name=str(a.get("app") or ""))
            for n in (a.get("app"), a.get("bundle_id")):
                if n:
                    self._names[str(n).lower()] = key
        first = apps[0] if apps else {}
        self.journal.front = str(first.get("bundle_id") or "")
        self.step = -1

    def tree_for(self, app: str) -> Optional[SimAXWorld]:
        return self.worlds.get(self._names.get(str(app or "").lower(), ""))

    @property
    def effects(self) -> List[Dict[str, Any]]:
        return self.journal.effects

    @property
    def holds(self) -> List[Tuple[int, int]]:
        return self.journal.holds

    @property
    def absent(self) -> List[Dict[str, Any]]:
        return [m for w in self.worlds.values() for m in w.absent]

    def add_row_values(self, values: Dict[str, str]) -> None:
        for w in self.worlds.values():
            w.add_row_values(values)

    def set_toggle_start(self, start: str) -> None:
        for w in self.worlds.values():
            w.set_toggle_start(start)

    def at_step(self, k: int) -> None:
        self.step = k
        for w in self.worlds.values():
            w.at_step(k)

    def note_hold(self) -> None:
        self.journal.hold()

    def activate_app(self, bundle_id: str) -> bool:
        """Bring an app of the record to the front (an app it never saw cannot be opened here)."""
        if self.tree_for(bundle_id) is None:
            return False
        self.journal.front = bundle_id
        self.journal.effect("open_app", bundle_id, None)
        return True


def choice_options(record: Dict[str, Any]) -> Dict[str, List[Tuple[str, str, str, Optional[str]]]]:
    """For each ``choice`` parameter: (canonical role, raw role, title, AX identifier) of every
    item of the chosen item's role in the recorded state of the step it was chosen in. The
    identifier is None when the fingerprint did not record one way or the other."""
    from anyuse.context.accessibility import canonical_role
    states = ((record.get("state_graph") or {}).get("states") or {})
    tsteps = ((record.get("workflow_template") or {}).get("steps") or [])
    rec = record.get("recorded") or []
    out: Dict[str, List[Tuple[str, str, str, Optional[str]]]] = {}
    for p in record.get("parameters") or []:
        if p.get("type") != "choice":
            continue
        opts: List[Tuple[str, str, str, Optional[str]]] = []
        for k, r in enumerate(rec):
            if r.get("draft_index") not in (p.get("occurrences") or []):
                continue
            cmd = r.get("command") or {}
            raw = str(cmd.get("role") or "")
            sid = (tsteps[k] if k < len(tsteps) else {}).get("expected_before_state") or ""
            fp = (states.get(sid) or {}).get("fingerprint") or {}
            for el in fp.get("required_elements") or []:
                title = el.get("title") or ""
                if el.get("role") == raw and title:
                    ident = str(el["identifier"]) if "identifier" in el and el["identifier"] is not None else None
                    item = (canonical_role(raw), raw, title, ident)
                    if item not in opts:
                        opts.append(item)
        out[p["name"]] = opts
    return out


# ---------------------------------------------------------------------------
# fresh values and the expected effects
# ---------------------------------------------------------------------------

def row_params(record: Dict[str, Any]) -> List[str]:
    """``choice`` parameters that name a row (every occurrence is a ``select_row``): a row is
    content (a file, a note), not a closed set of options, so the proof makes rows of the fresh
    values instead of picking other recorded options."""
    action_at = {s.get("draft_index"): (s.get("command") or {}).get("action")
                 for s in record.get("steps") or [] if isinstance(s, dict)}
    return [p["name"] for p in record.get("parameters") or []
            if p.get("type") == "choice" and p.get("occurrences")
            and all(action_at.get(o) == "select_row" for o in p["occurrences"])]


def _number_like(example: str, delta: int) -> str:
    sep = "," if "," in example else "."
    decimals = len(example.split(sep, 1)[1]) if sep in example else 0
    x = DP.parse_number(example) + delta
    text = f"{x:.{decimals}f}"
    return text.replace(".", sep) if sep == "," else text


def fresh_values(record: Dict[str, Any], n: int = MIN_FRESH_CASES,
                 given: Optional[Sequence[Dict[str, str]]] = None) -> Tuple[List[Dict[str, str]], List[str]]:
    """``n`` value sets none of whose values was recorded (or the given ones, checked)."""
    params = record.get("parameters") or []
    examples = {p["name"]: str(p.get("example") or "") for p in params}
    if given:
        sets, probs = [], []
        for i, vals in enumerate(given):
            vals = {k: str(v) for k, v in dict(vals).items()}
            if set(vals) != set(examples):
                probs.append(f"fresh set {i}: needs exactly {sorted(examples)}")
                continue
            if vals in sets:
                probs.append(f"fresh set {i} repeats an earlier set (the proof needs {n} different ones)")
                continue
            for name, v in vals.items():
                p = next(q for q in params if q["name"] == name)
                ex = examples[name]
                if v == ex or (len(ex) >= 3 and p["type"] != "choice" and ex in v):
                    probs.append(f"fresh set {i}: {name}={v[:60]!r} is or contains the recorded value")
                why = DP.type_problem(p["type"], v, "chosen" if p["type"] == "choice" else "typed")
                if why:
                    probs.append(f"fresh set {i}: {name}: {why}")
            sets.append(vals)
        if len(sets) < n:
            probs.append(f"{len(sets)} fresh value set(s); the proof needs at least {n}")
        return sets, probs
    tag = str((record.get("verdict_keys") or {}).get("draft_digest") or "")[:6] or "x"
    options = choice_options(record)
    rows = set(row_params(record))
    sets, probs = [], []
    for k in range(1, n + 1):
        vals: Dict[str, str] = {}
        for p in params:
            ex, name, t = examples[p["name"]], p["name"], p["type"]
            if t == "number":
                vals[name] = _number_like(ex, 17 * k)
            elif t == "date":
                fmt = DP.date_format(ex)
                vals[name] = (datetime.strptime(ex.strip(), fmt) + timedelta(days=3 * k)).strftime(fmt)
            elif t == "file_name":
                m = re.match(r"^(.*?)(\.[A-Za-z0-9]{1,8})?$", ex.strip())
                vals[name] = f"anyuse-probe-{k}-{tag}{(m.group(2) if m else '') or ''}"
            elif t == "choice" and name in rows:
                vals[name] = f"AnyUse probe row {k} {tag}"
            elif p.get("anchor"):
                vals[name] = f"AnyUse anchor {k} {tag}"      # a line's anchor: in no other line
            elif t == "choice":
                others = sorted(t3 for _r, _raw, t3, _i in options.get(name, []) if t3 != ex)
                if len(others) < k:
                    probs.append(f"{name}: the recording saw {len(others)} other option(s) of this "
                                 f"choice; {n} are needed for the proof")
                    continue
                vals[name] = others[k - 1]
            else:
                vals[name] = f"AnyUse probe {k} {tag}"
        sets.append(vals)
    return sets, probs


def typed_steps(record: Dict[str, Any]) -> List[int]:
    """The record's steps that type text (``_TEXT_OPS``)."""
    return [k for k, s in enumerate(record.get("steps") or [])
            if isinstance(s, dict) and (s.get("command") or {}).get("action") in _TEXT_OPS]


def toggle_steps(record: Dict[str, Any]) -> List[int]:
    """The record's ``set_toggle`` steps (their start state is varied: ``TOGGLE_STARTS``)."""
    return [k for k, s in enumerate(record.get("steps") or [])
            if isinstance(s, dict) and (s.get("command") or {}).get("action") == "set_toggle"]


def expected_effects(record: Dict[str, Any], values: Dict[str, str],
                     toggle_start: Optional[str] = None) -> List[Dict[str, Any]]:
    """The recorded effects with the new values where the parameters' candidates occurred -
    computed from the recording and the candidate occurrences, not from the bound steps. A
    ``set_toggle`` presses once only where its element's state (``toggle_start`` at first, then
    what the steps before set) is not the step's target."""
    from anyuse.execution.ax_actions import target_state
    states: Dict[Tuple, Optional[str]] = {}
    at: Dict[int, Dict[str, Any]] = {}
    for p in record.get("parameters") or []:
        if p.get("anchor"):
            continue                     # a line's anchor names where the value goes, not the value
        for di in p.get("occurrences") or []:
            at[di] = p
    out = []
    for k, r in enumerate(record.get("recorded") or []):
        cmd = r.get("command") or {}
        action = str(cmd.get("action") or "")
        sel = dict(cmd.get("selector") or {})
        value = r.get("input_data")
        p = at.get(r.get("draft_index"))
        bound = _bound_step(record, r.get("draft_index"))
        if p is not None and p["type"] == "choice" and (bound.get("choice") or {}).get("by") == "identifier":
            from anyuse.execution.draft_compile import choice_identifier
            sel = {"canonical_role": sel.get("canonical_role"), "name": "",
                   "identifier": choice_identifier(bound, values[p["name"]]) or "", "path": ""}
        elif p is not None and (p["type"] == "choice" or action == "select_row"):
            # a choice, or any parameter a filled select_row names its row by, binds the label
            sel = {"canonical_role": sel.get("canonical_role"), "name": values[p["name"]],
                   "identifier": "", "path": ""}
        elif p is not None:
            value = values[p["name"]]
        op = _OP.get(action, action)
        if action == "set_toggle":
            key = (_step_key(r), _key(sel))
            have, want = states.get(key, toggle_start), target_state(cmd.get("target_state"))
            if have != want:
                out.append({"step": k, "op": op, "target": describe(sel), "value": None})
                states[key] = want
            continue
        if op == "key":
            out.append({"step": k, "op": "key", "target": "", "value": str(cmd.get("keycode"))})
        elif op == "open_app":
            out.append({"step": k, "op": "open_app", "target": str(cmd.get("bundle_id") or ""), "value": None})
        else:
            out.append({"step": k, "op": op, "target": describe(sel),
                        "value": value if op in _TEXT_OPS else None})
    return out


def _bound_step(record: Dict[str, Any], draft_index: Any) -> Dict[str, Any]:
    return next((s for s in record.get("steps") or [] if s.get("draft_index") == draft_index), {})


def _choice_precheck(record: Dict[str, Any], sets: Optional[Sequence[Dict[str, str]]] = None) -> List[str]:
    """Without ``sets``: a chosen item recorded WITH an AX identifier (no other option can be
    confirmed live) -- unless the choice is bound by identity (``draft_compile.choice_domain``: every
    option of the recorded state carries its own identifier). With ``sets``: an option whose identifier
    field the recording did not keep, or (by identity) a value that names no recorded option."""
    probs = []
    options = choice_options(record)
    rows = set(row_params(record))
    for p in record.get("parameters") or []:
        if p.get("type") != "choice" or p["name"] in rows:
            # a row is identified by its label alone (select_row): no identifier to confirm
            continue
        by_id = [s for s in record.get("steps") or [] if (s.get("choice") or {}).get("param") == p["name"]]
        if by_id:
            from anyuse.execution.draft_compile import choice_identifier
            for vals in sets or []:
                if p["name"] in vals and choice_identifier(by_id[0], vals[p["name"]]) is None:
                    probs.append(f"{p['name']}: {vals[p['name']]!r} names no recorded option")
            continue
        if sets is None:
            for r in record.get("recorded") or []:
                sel = ((r.get("command") or {}).get("selector") or {})
                if r.get("draft_index") in (p.get("occurrences") or []) and sel.get("identifier"):
                    probs.append(f"{p['name']}: the recorded item carries the AX identifier "
                                 f"{sel['identifier']!r}; the executor's identity check is decisive on "
                                 "identifiers, so another option could not be confirmed (a choice "
                                 "parameter needs items without one)")
            continue
        by_title = {t: i for _r, _raw, t, i in options.get(p["name"], [])}
        for vals in sets:
            if p["name"] in vals and by_title.get(vals[p["name"]]) is None:
                probs.append(f"{p['name']}: the recording kept no AX identifier field for the option "
                             f"{vals[p['name']]!r}; the executor's identity check decides on it, so "
                             "the option cannot be proved offline (prove it with a guest replay)")
    return probs


async def prove(record: Dict[str, Any], candidates: Sequence[DP.Candidate] = (), *,
                fresh: Optional[Sequence[Dict[str, str]]] = None, n: int = MIN_FRESH_CASES,
                world_factory: Optional[Callable[[Dict[str, Any]], SimAXWorld]] = None) -> Dict[str, Any]:
    """The proof (module docstring). A multi-app record runs in a ``SimMultiAppWorld`` (one
    world per app, the runner's ``tree_factory``, one checked executor per app). The runner
    contract's steps (``execution/ax_actions.py``) run through the executor's own handlers,
    against the world's rows, focus, field values and front app. A text mode's field starts
    from the recorded start shape, its anchor line holding the case's anchor value; without the
    shape, or with the recorded anchor in no line of it, the proof fails (``start_problems``)."""
    from anyuse.execution import ax_actions as AX
    from anyuse.execution.draft_compile import checked_executor, is_multi_app, run_steps
    # a recording with no parameter and nothing typed is a fixed routine (start a new game, empty the
    # trash): there is no fresh input to bind, so its proof is one replay that reproduces the recorded
    # effects. Typed text with no parameter stays unproved: that value was meant to change
    routine = not record.get("parameters") and not typed_steps(record)
    if routine:
        n = 1
    base = {"harness": HARNESS, "scope": SCOPE, "min_cases": n, "cases": []}
    if routine:
        base["fixed_routine"] = True
    elif not record.get("parameters"):
        return {**base, "passed": False, "reason": "no parameter to bind: nothing to prove on fresh input"}
    if record.get("unreplayable"):
        return {**base, "passed": False,
                "reason": f"{len(record['unreplayable'])} recorded step(s) cannot be replayed"}
    if not record.get("steps"):
        return {**base, "passed": False, "reason": "no replayable step"}
    pre = _choice_precheck(record) + start_problems(record)
    if pre:
        return {**base, "passed": False, "reason": "; ".join(pre)[:400]}
    sets, probs = ([{}], []) if routine else fresh_values(record, n, fresh)
    if probs:
        return {**base, "passed": False, "reason": "fresh values: " + "; ".join(probs)[:400]}
    pre = _choice_precheck(record, sets)
    if pre:
        return {**base, "passed": False, "reason": "; ".join(pre)[:400]}
    gated = {g["index"] for g in (record.get("approvals") or {}).get("gated_steps") or []}
    recorded_values = [str(p.get("example") or "") for p in record.get("parameters") or []
                       if p.get("type") != "choice"]
    # occurrences a person split off and kept fixed (``@c2#4`` makes step 4 a parameter of its
    # own; c2's other steps keep the recorded text on purpose) are not leaks
    draft_to_step = {r.get("draft_index"): k for k, r in enumerate(record.get("recorded") or [])}
    param_cands = {p.get("candidate") for p in record.get("parameters") or []}
    cands = [c for c in record.get("candidates") or [] if isinstance(c, dict)]
    split_bases = {str(c.get("id")).split("#")[0] for c in cands if "#" in str(c.get("id"))}
    kept_fixed = {draft_to_step.get(st) for c in cands
                  if c.get("id") not in param_cands and str(c.get("id")).split("#")[0] in split_bases
                  for st in c.get("steps") or []}
    cases = []
    multi = is_multi_app(record)
    make_executor = checked_executor
    ungated_writes = [i for i, s in enumerate(record.get("steps") or [])
                      if s["command"].get("action") in ("enter_text", "toggle", "confirm",
                                                        *sorted(AX.WRITE_ACTIONS))
                      and i not in gated]
    # a record with a state-aware toggle runs every case from both start states (already on, already off)
    starts: Sequence[Optional[str]] = TOGGLE_STARTS if toggle_steps(record) else (None,)

    async def replay(values: Dict[str, str], start: Optional[str]):
        world = (world_factory or (SimMultiAppWorld if multi else SimAXWorld))(record)
        add_rows = getattr(world, "add_row_values", None)
        if add_rows is not None:
            add_rows(values)
        if start is not None and getattr(world, "set_toggle_start", None) is not None:
            world.set_toggle_start(start)
        first = world.tree_for(record.get("bundle_id") or record.get("app") or "") if multi else world
        executor = make_executor(first)

        async def approver(_ctx, _w=world):
            _w.note_hold()
            return True
        # a recorded vault step types its reference, never a credential: the proof reads no vault
        out = await run_steps(record, values, executor=executor, approver=approver,
                              decide_as_driving=True, on_step=world.at_step,
                              secret_resolver=lambda alias: "${vault:%s}" % alias,
                              tree_factory=world.tree_for if multi else None,
                              executor_factory=make_executor, default_tree=first)
        problems = [] if out["ok"] else [str(out["stopped"])]
        for miss in world.absent:
            problems.insert(0, f"step {miss['step']}: the recording's own state before this step does not "
                               f"show its target {miss['target']}")
        got = [{k: e[k] for k in ("step", "op", "target", "value")} for e in world.effects]
        want = expected_effects(record, values, start)
        if out["ok"] and got != want:
            first = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
            problems.append(f"effects differ from the recording at effect {first}: got "
                            f"{got[first] if first < len(got) else None}, expected "
                            f"{want[first] if first < len(want) else None}")
        for e in world.effects:
            # an open_app's target is the recorded bundle id: nothing is bound there
            if e["step"] in kept_fixed or e["op"] == "open_app":
                continue
            for rv in recorded_values:
                if len(rv) >= 3 and (rv in (e["value"] or "") or rv in e["target"]):
                    problems.append(f"the recorded value {rv[:40]!r} reached step {e['step']} "
                                    "(bind it there too, or make that text a parameter of its own)")
        for e in world.effects:
            if e["step"] in gated and not any(s < e["seq"] and st == e["step"] for s, st in world.holds):
                problems.append(f"step {e['step']} acted before its approval hold")
        if ungated_writes:
            problems.append(f"write step(s) {ungated_writes} have no approval gate")
        if start is not None:
            problems = [f"toggles starting {start}: {p}" for p in problems]
        return problems, out, world

    for values in sets:
        problems: List[str] = []
        runs = []
        for start in starts:
            probs, out, world = await replay(values, start)
            problems += probs
            runs.append((out, world))
        out, world = runs[0]
        case = {"values": values, "passed": not problems, "problems": problems[:5],
                "steps_run": len(out["steps"]), "holds": len(world.holds),
                "drift_fired": sum(1 for o, _w in runs for s in o["steps"] if s.get("drift_fired")),
                "llm_calls": sum(s.get("llm_calls", 0) for o, _w in runs for s in o["steps"])}
        if starts != (None,):
            case["toggle_starts"] = list(starts)
        cases.append(case)
    passed = len(cases) >= n and all(c["passed"] for c in cases)
    failed = next((c for c in cases if not c["passed"]), None)
    if routine:
        reason = ("fixed routine (no parameter): the replay reproduced the recorded effects"
                  if passed else "the replay: " + "; ".join(failed["problems"])[:400])
    else:
        reason = (f"{len(cases)}/{len(cases)} fresh value sets replayed with the expected effects"
                  if passed else f"case {cases.index(failed)}: " + "; ".join(failed["problems"])[:400])
    return {**base, "passed": passed, "reason": reason, "cases": cases,
            "fresh_inputs": len(cases), "passed_cases": sum(1 for c in cases if c["passed"])}
