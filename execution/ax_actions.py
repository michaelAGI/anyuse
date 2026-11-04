"""The AX runner's contract for the map's four capabilities.

COMPILE FROM THE MAP needs four things a plain draft runner cannot do: select a row of a list by its
label, type into the field a trigger focused, append to a text or replace one line of it, and run one
workflow across apps. This module is the one place that says what each of them is, in the executor's
own shapes:

* ``AX_ACTIONS``: the rows ``execution/executor.py`` adds to its ``AX_ACTIONS`` table. ``verb``
  is None (no platform verb), so its ``_ax_inline`` runs each of them through
  ``HANDLERS[action](tree, target, step, cmd)``; ``open_app`` acts on the app, not on a
  resolved element (``TARGETLESS``: ``target`` is None);
* the handlers ``select_row``, ``append_text``, ``replace_line`` and ``open_app``: the action
  and its read-back (a mismatch fails the step), each ending in a gated ``AccessibilityTree``
  primitive;
* ``set_toggle``: a STATE-AWARE toggle - the step carries its
  target state (on / off), reads the current one, presses only on a mismatch and reads it back;
  an unreadable state is refused, a read-back that differs is a deviation (``toggle`` stays the
  blind AXPress of the executor's own table, for steps that carry no target);
* ``resolve_kind``: the selector kinds the executor's ``_resolve_target`` resolves by their
  own rule (``AX_SELECTOR_KINDS``): ``focused`` (``resolve_focused``: the element that has
  keyboard focus right after the previous step, of the selector's role, never a password
  field) and ``row`` (``resolve_row``: the tree's ``find_row``, a row of an IDENTIFIED list by
  its first cell's text or its own identifier). A row recorded without the kind resolves
  through ``find_selector`` / ``to_selector`` as before;
* ``CONTRACT``: per action its arguments, identity rule, act, read-back and the tree methods
  it needs. Refusals are ``execution/outcome.refusal``: ``unsupported_verb``, ``ambiguous``,
  ``not_found``, ``identity``, ``wrong_app``.

An executor that does not run one of these (one without the rows, a test double that declares
``AX_CONTRACT``) is refused per step before the step is held or dispatched (``runner_refusal``)
and per record before anything is sent (``record_runner_problem``): an executor without the
table would turn an action it does not know into a press.

Records: a compiled step that needs any of this (or that carries its own app) makes the record
``compiler.version`` 2 (``record_version``); version 1 records are unchanged, and an AnyUse whose
runner knows only version 1 refuses version 2 (``draft_compile.record_version_problem``).
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

#: the refusal for a contract feature an executor or a tree does not run
NOT_IMPLEMENTED = "not implemented in this runner yet"

#: the rows of the executor's AX_ACTIONS table: no platform verb, the inline handler runs them
AX_ACTIONS: Dict[str, Dict[str, Any]] = {
    "select_row": {"verb": None, "native": ""},
    "append_text": {"verb": None, "native": ""},
    "replace_line": {"verb": None, "native": ""},
    "open_app": {"verb": None, "native": ""},
    # a STATE-AWARE toggle: no platform verb, so neither branch can turn it into
    # the blind AXPress the ``toggle`` row is
    "set_toggle": {"verb": None, "native": ""},
    # a recorded shortcut no menu item owned (or whose menu could not be read), sent as the key chord itself
    "press_shortcut": {"verb": None, "native": ""},
}
#: the target states a ``set_toggle`` sets (``command.target_state``)
TOGGLE_STATES: Tuple[str, ...] = ("on", "off")
#: actions on the app itself: no selector, no ``_resolve_target``; the handler gets target None
TARGETLESS: FrozenSet[str] = frozenset({"open_app", "press_shortcut"})
#: the CGEventFlags modifier bits a shortcut carries (``context/keyboard.press_key`` sets them on the event);
#: every other bit the event tap recorded (device bits, non-coalesced) is dropped when it is compiled
SHORTCUT_FLAGS: Dict[str, int] = {"shift": 0x20000, "ctrl": 0x40000, "opt": 0x80000, "cmd": 0x100000}
SHORTCUT_MASK = sum(SHORTCUT_FLAGS.values())
#: shortcuts never sent as a key, in any app: by the key's character - the recorded one (the step's
#: ``char``, else the recorded name's last part) and every one the key it is sent as types on the layout
#: in use (the US positions when no layout can be read) ...
REFUSED_SHORTCUT_CHARS: Dict[str, str] = {"q": "quits the app (or logs out)", "w": "closes a window"}
#: ... and by keycode for keys that type no character (the same keycode on every layout)
REFUSED_SHORTCUT_KEYS: Dict[int, str] = {51: "deletes (Delete)", 117: "deletes (forward delete)",
                                         53: "force quits or cancels (Escape)"}
#: the recorded effects a shortcut's read-back checks (``command.expect``): the app's window / sheet count
#: went up ("more") or down ("fewer"), polled up to ``SHORTCUT_EFFECT_S``
SHORTCUT_EFFECTS: Tuple[str, ...] = ("windows", "sheets")
SHORTCUT_EFFECT_S = 2.0
SHORTCUT_POLL_S = 0.1
#: ``selector["kind"]`` values resolved by their own rule, not by a recorded identity (the
#: executor declares the ones it resolves in a module-level ``AX_SELECTOR_KINDS``): ``focused``
#: (the field with keyboard focus) and ``row`` (a row of an identified list, by its label)
SELECTOR_KINDS: Tuple[str, ...] = ("focused", "row")
#: a typing step's input modes and the AX action each compiles to; ``set`` is today's
#: ``enter_text`` (the whole value replaced)
INPUT_MODES: Tuple[str, ...] = ("set", "append", "replace_line")
MODE_ACTIONS: Dict[str, str] = {"set": "enter_text", "append": "append_text", "replace_line": "replace_line"}
ACTION_MODES: Dict[str, str] = {v: k for k, v in MODE_ACTIONS.items()}
TEXT_ACTIONS: FrozenSet[str] = frozenset(MODE_ACTIONS.values())
#: actions that change what the app holds (the proof requires an approval gate before each)
WRITE_ACTIONS: FrozenSet[str] = frozenset({"select_row", "append_text", "replace_line", "set_toggle"})
#: canonical roles a row click is recorded with; the step always selects the ROW
ROW_ROLES: FrozenSet[str] = frozenset({"row", "cell", "outlinerow"})
ROW_RAW_ROLES: FrozenSet[str] = frozenset({"AXRow", "AXOutlineRow"})
CONTAINER_ROLES: FrozenSet[str] = frozenset({"table", "outline", "list"})
#: the roles a ``focused`` target may have (a text field of some kind)
FOCUS_ROLES: Tuple[str, ...] = ("textfield", "textarea", "searchfield", "combobox")
MAX_ANCHOR = 200
#: the line breaks ``replace_line`` splits on (each line keeps its own); any other break
#: character in the value refuses the edit (it could not tell where a line ends)
LINE_BREAKS = ("\r\n", "\n", "\r", "\u2028", "\u2029")
_OTHER_BREAKS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85")

#: what each action is
CONTRACT: Dict[str, Dict[str, str]] = {
    "select_row": {
        "args": "command.selector = {kind: row, canonical_role: row, raw_role: AXRow, name: <the row's "
                "first-cell text, or ${choice}>, identifier: <only a row's own unique one>, path: '', "
                "container: {role, identifier, description, path, columns?}, scope?: <salted container "
                "scope key>}",
        "identity": "the ONE list in the app matching the recorded list's identity (its identifier, else "
                    "description, else role-index path below its window), then in it the ONE row with "
                    "the identifier, else the label (its first cell's text); none is not_found, two or "
                    "more ambiguous; a list over 500 rows or an app over 4000 elements outside its lists "
                    "is refused (a twin could hide); a list in a window of a DIFFERENT scope is no "
                    "candidate. Then the container policy (scope_refusal, context/container_scope.py), "
                    "in an app with a scope reader: different is wrong_container; unconfirmed acts when "
                    "the recorded scope is a name (or none), and refuses a content row when it is a path "
                    "(container_unconfirmed); an unreadable live scope is no rule",
        "act": "that list's AXSelectedRows = [row], else the row's AXSelected = true (tree.select_row, gated)",
        "read_back": "THAT list's AXSelectedRows, as labels, must be exactly [label]; more than one "
                     "(the row was added to a selection) deselects it again and is a deviation",
        "tree": "find_row(selector), select_row(row), deselect_row(row) (gated), "
                "selected_row_labels(row), row_container_identity(row), window_of(row) (the scope hook "
                "reads the window: _scope_of(tree, window, app) = container_scope)",
    },
    "append_text": {
        "args": "input_data = the text (a ${parameter}, or the fill's own text); command.input_mode = "
                "append",
        "identity": "the field's recorded selector, or the focused kind; as any step",
        "act": "AXSelectedTextRange = (length of the value before, 0), then AXSelectedText = text "
               "(tree.edit_text); lengths in UTF-16 units",
        "read_back": "the field's value afterwards must equal the value before + text; else "
                     "uncertain_completion (acted: never retried, never written twice)",
        "tree": "read_value(el) -> str | None; edit_text(el, text, replace_range, expect_value)",
    },
    "replace_line": {
        "args": "input_data = the new line (no line break); command.anchor = a text that exactly one "
                "line of the value contains; command.input_mode = replace_line",
        "identity": "as append_text; lines end at \\r\\n, \\n, \\r, U+2028 or U+2029 (each keeps its own "
                    "break; another break character refuses); the anchor must be in exactly one line: "
                    "none is not_found, two or more ambiguous",
        "act": "AXSelectedTextRange = that line (without its break), then AXSelectedText = text",
        "read_back": "the field's value afterwards must equal the value before with that line replaced; "
                     "else uncertain_completion (acted)",
        "tree": "read_value(el); edit_text(el, text, replace_range, expect_value)",
    },
    "open_app": {
        "args": "command.bundle_id, command.app_name; no selector (TARGETLESS; not command.app, which "
                "the executor reads as the bundle id a target must be in)",
        "identity": "the bundle id, one of the record's own apps and allowed by the restriction policy "
                    "(the draft runner checks both before the step is held)",
        "act": "activate the app when it runs, else launch it (tree.activate_app, gated)",
        "read_back": "the frontmost app (context.accessibility.frontmost_reading: the AX focus whenever it "
                     "answers, else the run-loop-ticked workspace read cross-checked by the menu-bar owner) must "
                     "be that bundle id within the settle deadline; else uncertain_completion (no source "
                     "answered, the sources disagreed, or another app). Each poll's source and reads (with its "
                     "time) are the step's evidence (tree.last_activation); never the untickled workspace read",
        "tree": "activate_app(bundle_id) -> bool (gated: drive gate + foreground interlock); "
                "frontmost_bundle_id() -> str | None; last_activation (optional: the polls)",
    },
    "set_toggle": {
        "args": "command.target_state = on | off (a map binds it from the text: 'turn off' -> off; a recorded "
                "check / uncheck keeps its observed end state); command.selector = the menu item's or the "
                "checkbox's recorded identity",
        "identity": "the recorded selector, as any step",
        "act": "read the current state (tree.toggle_state: a menu item's AXMenuItemMarkChar, a checkbox's or "
               "switch's AXValue 1 / 0); the target already: NO press (a menu opened to reach the item is "
               "closed again, tree.close_menu); else ONE AXPress (gated). An unreadable state (another role, a "
               "mixed checkbox, a failed read) is refused: never a blind press",
        "read_back": "tree.toggle_readback(el, target): the state after the press must be the target (a menu "
                     "item is read with its top menu opened again: AppKit sets marks when a menu opens); "
                     "another state is a deviation (stop), an unreadable one uncertain_completion (acted)",
        "tree": "toggle_state(el) -> 'on' | 'off' | None (a read); toggle_readback(el, want) -> state | None; "
                "close_menu(el) -> bool (gated AXCancel on the menu-bar item; True outside the menu bar); "
                "perform_action(el, 'AXPress') (gated)",
    },
    "press_shortcut": {
        "args": "command.keys (the recorded name, e.g. cmd+n), command.keycode, command.flags (modifier bits "
                "only: SHORTCUT_MASK; command or control among them), command.char? (the character the chord "
                "stood for on the recording layout: the Command map, with Control alone the control "
                "character's letter, Option ignored, with Shift the shifted symbol or the letter), "
                "command.layout? (the recording's input source id), command.bundle_id (the app it was "
                "recorded in), command.expect? = {windows?: more | fewer, sheets?: more | fewer}; no selector "
                "(TARGETLESS). Compiled only when the recording shows the shortcut's effect (a window or sheet "
                "count that changed, or the next step's recorded screen)",
        "identity": "the app in front: a key goes to it, so the recorded app is brought to the front first "
                    "(tree.activate_app, gated) and the step is refused (wrong_app) when another app stays in "
                    "front; a destructive shortcut (REFUSED_SHORTCUT_CHARS / _KEYS: quit, close, delete, "
                    "force quit) is refused before anything is sent, by the recorded character and by every "
                    "character the key sent types on the layout in use; while the layout cannot be read, also a "
                    "key that types q or w on a common layout (keyboard.UNREAD_QW_KEYS)",
        "act": "one key down + up with the modifier flags on it (tree.press_key, gated): the recorded key and flags "
               "when they still stand for the recorded character on the layout in use (tree.keyboard_layout, "
               "read once the app is in front), else the key that types it there (Shift kept for a letter, "
               "otherwise set by what that layout needs for the character), refused when no key types it; the "
               "recorded keycode when the step has no character or the layout cannot be read, and for an older "
               "recording of an Option chord (said in the result's note)",
        "read_back": "the recorded effect: each expect counter must have moved that way within "
                     "SHORTCUT_EFFECT_S (tree.window_counts), else uncertain_completion (acted; a count "
                     "unreadable afterwards is no effect); a counter unreadable BEFORE the key refuses it "
                     "(nothing sent); the next step's drift gate (settle-polled) checks the recorded screen",
        "tree": "press_key(keycode, flags) -> bool (gated); frontmost_bundle_id(); activate_app(bundle_id) "
                "(gated); window_counts() -> {windows, sheets} (optional: a read); keyboard_layout() -> "
                "context.keyboard.Layout | None (optional: a read)",
    },
    "selector:focused": {
        "args": "command.selector = {kind: focused, canonical_role: textfield | textarea | searchfield "
                "| combobox, raw_role, name: '', identifier: '', path: '', app?, document?} with any text "
                "action",
        "identity": "the AXFocusedUIElement right after the previous step; its canonical role must be "
                    "the selector's and it is never a password field (else identity); no focus is "
                    "not_found; the recorded app and document are checked as for any target",
        "act": "the step's own action on that element",
        "read_back": "the step's own (enter_text's ladder, the modes' value read-back)",
        "tree": "focused_element() -> element | None (a read); describe_element(el).secure",
    },
    "selector:row": {
        "args": "the select_row selector above",
        "identity": "tree.find_row (see select_row); a row recorded WITHOUT this kind (by its path and "
                    "the reuse id every row shares) goes through find_selector / to_selector as before",
        "act": "select_row", "read_back": "select_row's", "tree": "find_row(selector)",
    },
}


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

def refusal(reason: str, *, code: str, method: str = "ax", **extra: Any) -> Dict[str, Any]:
    """"Did not act": ``execution/outcome.refusal`` (kind ``refused``, in STOP_KINDS), with the
    AX method by default."""
    from anyuse.execution.outcome import refusal as _refusal
    return _refusal(reason, code=code, method=method, **extra)


def not_implemented(what: str, **extra: Any) -> Dict[str, Any]:
    return refusal(f"{what}: {NOT_IMPLEMENTED} (execution/ax_actions.py lists the executor's part; "
                   "nothing was sent)", code="unsupported_verb", **extra)


# ---------------------------------------------------------------------------
# what a step needs, and what an executor runs
# ---------------------------------------------------------------------------

def _command(step: Any) -> Dict[str, Any]:
    cmd = step.get("command") if isinstance(step, dict) else getattr(step, "command", None)
    return cmd if isinstance(cmd, dict) else {}


def needs(step: Any) -> List[str]:
    """The contract features a step (a record dict or a ``PlannedStep``) uses: its action when it
    is one of ``AX_ACTIONS``, ``selector:<kind>`` for a selector with a kind."""
    cmd = _command(step)
    out = []
    action = str(cmd.get("action") or "")
    if action in AX_ACTIONS:
        out.append(action)
    sel = cmd.get("selector")
    if isinstance(sel, dict) and sel.get("kind"):
        out.append(f"selector:{sel['kind']}")
    return out


def module_supports(module: Any) -> FrozenSet[str]:
    """The contract features an executor module runs: its ``AX_ACTIONS`` rows that are ours, and
    ``selector:<kind>`` for each of its ``AX_SELECTOR_KINDS``."""
    table = getattr(module, "AX_ACTIONS", None) or {}
    kinds = getattr(module, "AX_SELECTOR_KINDS", None) or ()
    return frozenset([a for a in table if a in AX_ACTIONS] + [f"selector:{k}" for k in kinds])


#: the attribute a wrapping executor (one that delegates to a checked executor, as the guest's
#: ``GuardedCheckedExecutor`` does) sets to the executor it wraps, so ``executor_supports``
#: reads the wrapped executor's table instead of refusing every contract step
WRAPPED_ATTR = "__wrapped_executor__"


def executor_supports(executor: Any) -> FrozenSet[str]:
    """What this executor runs of the contract: an ``AX_CONTRACT`` it declares itself (a test
    double), else its class's module's table (``executor.py``); a wrapper is read through the
    executor it names in ``__wrapped_executor__`` (followed up to 3 levels)."""
    for _ in range(3):
        inner = getattr(executor, WRAPPED_ATTR, None)
        if inner is None or inner is executor:
            break
        executor = inner
    own = getattr(executor, "AX_CONTRACT", None)
    if own is not None:
        return frozenset(own)
    if executor is None:
        from anyuse.execution import executor as EX
        return module_supports(EX)
    for cls in type(executor).__mro__:
        mod = sys.modules.get(cls.__module__)
        if mod is not None and hasattr(mod, "AX_ACTIONS"):
            return module_supports(mod)
    return frozenset()


def capability_probe(executor: Any, executor_factory: Any = None) -> Any:
    """What to ask ``executor_supports`` before anything is built: the executor itself, else a
    factory that declares ``AX_CONTRACT`` (a test double's), else None (the executor
    module, which ``draft_compile.checked_executor`` builds from)."""
    if executor is not None:
        return executor
    if getattr(executor_factory, "AX_CONTRACT", None) is not None:
        return executor_factory
    return None


def _what(feature: str) -> str:
    return (f"the {feature.split(':', 1)[1]!r} selector kind" if feature.startswith("selector:")
            else f"the AX action {feature!r}")


def runner_refusal(step: Any, executor: Any) -> Optional[Dict[str, Any]]:
    """A refusal when ``executor`` cannot run this step's contract features, else None (a step
    with none of them is never refused here)."""
    want = needs(step)
    if not want:
        return None
    have = executor_supports(executor)
    missing = [n for n in want if n not in have]
    if not missing:
        return None
    return not_implemented(", ".join(_what(n) for n in missing), missing=missing)


def record_needs(record: Dict[str, Any]) -> List[str]:
    return sorted({n for s in record.get("steps") or [] for n in needs(s)})


def record_runner_problem(record: Dict[str, Any], executor: Any = None) -> Optional[str]:
    """Before a live run: the record's steps whose contract features this runner (``executor``,
    else the executor module) does not run, as one refusal text; None when it runs them all."""
    have = executor_supports(executor)
    bad: Dict[str, List[int]] = {}
    for i, s in enumerate(record.get("steps") or []):
        for n in needs(s):
            if n not in have:
                bad.setdefault(n, []).append(i)
    if not bad:
        return None
    parts = [f"{_what(n)} (step {', '.join(str(i) for i in idx)})" for n, idx in sorted(bad.items())]
    return (f"{'; '.join(parts)}: {NOT_IMPLEMENTED} (execution/ax_actions.py lists the executor's "
            "part); nothing was sent")


def record_version(steps: Iterable[Dict[str, Any]]) -> int:
    """2 when a step needs the contract or carries its own app, else 1."""
    for s in steps:
        if needs(s) or (isinstance(s, dict) and (s.get("app") or s.get("bundle_id"))):
            return 2
    return 1


# ---------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------

def canonical(raw_role: Optional[str]) -> str:
    """``canonical_role`` with an AXOutlineRow read as a row."""
    from anyuse.context.accessibility import canonical_role
    role = canonical_role(raw_role)
    return "row" if role == "outlinerow" else role


def _parts(path: str) -> List[str]:
    return [p for p in str(path or "").split("/") if p]


def container_from_path(path: str) -> Optional[Dict[str, str]]:
    """A row's list identity from the role-index path of the row or of one of its cells
    (``window[0]/splitgroup[0]/scrollarea[1]/outline[0]/row[3]/cell[0]`` -> role ``outline``,
    path ``splitgroup[0]/scrollarea[1]/outline[0]``, below the window); None without one."""
    from anyuse.context.accessibility import path_within_window
    parts = _parts(path)
    for i in range(len(parts) - 1, -1, -1):
        role = parts[i].split("[", 1)[0]
        if role in CONTAINER_ROLES:
            return {"role": role, "identifier": "", "description": "",
                    "path": path_within_window("/".join(parts[:i + 1]))}
    return None


def cell_column(path: str) -> Optional[int]:
    """The column of a recorded cell (``.../row[3]/cell[k]`` -> k), or None when the path
    does not say."""
    parts = _parts(path)
    if len(parts) >= 2 and parts[-1].startswith("cell[") and parts[-2].split("[", 1)[0] == "row":
        try:
            return int(parts[-1][5:-1])
        except ValueError:
            return None
    return None


def container_identity(raw: Any) -> Optional[Dict[str, str]]:
    """A recorded list identity (``data.container`` / ``selector.container``) in its one shape,
    or None when it names nothing beyond a role (a role alone is not an identity: Finder's
    sidebar and its file list are both outlines)."""
    if not isinstance(raw, dict):
        return None
    out = {"role": str(raw.get("role") or "").strip().lower(),
           "identifier": str(raw.get("identifier") or "").strip(),
           "description": str(raw.get("description") or "").strip(),
           "path": str(raw.get("path") or "").strip()}
    if out["role"] not in CONTAINER_ROLES or not (out["identifier"] or out["description"] or out["path"]):
        return None
    try:
        cols = int(raw.get("columns")) if raw.get("columns") not in (None, "") else None
    except (TypeError, ValueError):
        cols = None
    if cols is not None:
        out["columns"] = cols
    return out


def container_matches(live: Optional[Dict[str, str]], recorded: Optional[Dict[str, str]]) -> bool:
    """Is the live list the recorded one? Same role, then the strongest recorded mark decides:
    the identifier, else the description, else the role-index path below the window (the
    recorded path may be the shorter, from a cell's path: it must end the live one)."""
    rec = container_identity(recorded)
    if rec is None or not isinstance(live, dict) or live.get("role") != rec["role"]:
        return False
    if rec["identifier"]:
        return live.get("identifier") == rec["identifier"]
    if rec["description"]:
        return live.get("description") == rec["description"]
    # a path alone moves with the layout (a hidden sidebar shifts the file list into its place):
    # when the list's column count is known, it must agree too (a sidebar has one column)
    if "columns" in rec and live.get("columns") is not None and int(live["columns"]) != rec["columns"]:
        return False
    lp, rp = _parts(live.get("path") or ""), _parts(rec["path"])
    return bool(rp) and len(lp) >= len(rp) and lp[-len(rp):] == rp


def row_selector(label: str, container: Optional[Dict[str, str]], identifier: str = "",
                 scope: str = "") -> Dict[str, Any]:
    """A ``select_row`` target (kind ``row``): the list's identity and the row's label, or the
    row's own identifier where it has a unique one; ``scope`` the salted key of the folder / note
    list its window showed (``context/container_scope.scope_key``), when recorded."""
    sel = {"kind": "row", "canonical_role": "row", "name": str(label), "identifier": str(identifier or ""),
           "path": "", "raw_role": "AXRow", "container": dict(container or {})}
    if scope:
        sel["scope"] = str(scope)
    return sel


def focused_selector(role: str, raw_role: str = "", document: str = "", app: str = "") -> Dict[str, str]:
    """A ``focused`` target; ``document`` / ``app`` as recorded, so the executor's wrong-document
    and wrong-app checks run on it as on any target."""
    from anyuse.execution.draft_compile import _RAW_ROLE
    sel = {"kind": "focused", "canonical_role": role, "name": "", "identifier": "", "path": "",
           "raw_role": raw_role or _RAW_ROLE.get(role, "")}
    if document:
        sel["document"] = document
    if app:
        sel["app"] = app
    return sel


def expect_for(action: str, selector: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The declared expected effect (``execution/step_effect.py`` format) of a text mode on a
    target with a recorded label or identifier: the target's value afterwards contains the
    typed text. ``only`` is false: a document's other content changes with the edit. None for
    everything else: a focused target has no label to find it by in a snapshot, and a selected
    row has no matcher (``select_row``'s own read-back is its check)."""
    if action not in ("append_text", "replace_line") or not isinstance(selector, dict):
        return None
    if selector.get("kind") or not (selector.get("name") or selector.get("identifier")):
        return None
    return {"changes": [{"op": "value", "target": True, "contains": "${input}"}], "only": False,
            "source": "ax_actions"}


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def _app_refusal(tree: Any, sel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    app = str(sel.get("app") or "")
    getter = getattr(tree, "bundle_id", None)
    if not app or getter is None:
        return None
    live = getter()
    if live != app:
        return refusal(f"wrong app: the tree is {live or 'not bound to an app'}, the recording was in {app}",
                       code="wrong_app")
    return None


def same_focused_field(live: Any, recorded: Optional[Dict[str, Any]]) -> bool:
    """Is the element focused now the SAME field the focused step before typed into,
    although the app handed back another element reference? Only an identified field counts (canonical role, raw role,
    AXIdentifier and app equal; the window is checked by the caller): Finder re-creates its toolbar search field
    (``_NS:122``) as the search runs, and its Return was refused as "the focus moved". A field without an identifier,
    or any other field (a Tab), is not the same."""
    if not isinstance(recorded, dict) or live is None:
        return False
    ident = str(recorded.get("identifier") or "")
    if not ident or str(getattr(live, "identifier", "") or "") != ident:
        return False
    if str(getattr(live, "canonical_role", "") or "") != str(recorded.get("canonical_role") or ""):
        return False
    if str(getattr(live, "raw_role", "") or "") != str(recorded.get("raw_role") or ""):
        return False
    rec_app, live_app = str(recorded.get("app") or ""), str(getattr(live, "app", "") or "")
    return not (rec_app and live_app and rec_app != live_app)


def resolve_focused(tree: Any, sel: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """``(element, None)`` or ``(None, refusal)`` for ``{"kind": "focused", ...}``: the element
    that has keyboard focus now (right after the previous step), of the selector's role."""
    want = str(sel.get("canonical_role") or "")
    getter = getattr(tree, "focused_element", None)
    if getter is None:
        return None, not_implemented("the 'focused' selector kind (the AX tree cannot read the "
                                     "focused element)")
    if want not in FOCUS_ROLES:
        return None, refusal(f"a focused target must be a text field, not {want or 'no role'}", code="identity")
    why = _app_refusal(tree, sel)
    if why is not None:
        return None, why
    try:
        el = getter()
    except Exception as exc:  # noqa: BLE001 - an unreadable focus is no target
        return None, refusal(f"the focused element cannot be read ({exc})", code="not_found")
    if el is None:
        return None, refusal("no element has keyboard focus after the previous step", code="not_found")
    try:
        live = tree.to_selector(el)
    except Exception as exc:  # noqa: BLE001
        return None, refusal(f"could not describe the focused element ({exc})", code="identity")
    if canonical(getattr(live, "raw_role", "") or "") != want and live.canonical_role != want:
        return None, refusal(f"the focused element is a {live.canonical_role}, the step types into a {want}",
                             code="identity")
    if is_secure(tree, el):
        # a web password input is an AXTextField with the secure subrole: its role alone passes
        return None, refusal("the focused element is a password field", code="identity")
    # the trigger's context (``draft_compile.run_steps`` reads it right before the trigger): the
    # focus must have MOVED, and into the trigger's window when the trigger had one
    ctx = sel.get("_focus_ctx") if isinstance(sel.get("_focus_ctx"), dict) else None
    if sel.get("trigger_kind") == "focused" or (ctx is not None and ctx.get("same")):
        # the trigger is itself a focused step: the focus must NOT have moved since it
        if ctx is None or ctx.get("focused") is None:
            return None, refusal("the focus of the focused step before this one was not read, so "
                                 "the focus staying there cannot be checked", code="identity")
        if el != ctx["focused"] and not same_focused_field(live, ctx.get("identity")):
            rec = ctx.get("identity") if isinstance(ctx.get("identity"), dict) else {}
            # roles and identifiers only (never a title or a value): which element had the focus, which has it now
            return None, refusal("the focus moved after the focused step before this one: another "
                                 f"element has it now ({getattr(live, 'raw_role', '') or '?'} "
                                 f"{getattr(live, 'identifier', '') or 'no identifier'}; the step before typed into "
                                 f"{rec.get('raw_role') or '?'} {rec.get('identifier') or 'no identifier'})",
                                 code="identity")
        window_of = getattr(tree, "window_of", None)
        if ctx.get("window") is not None and window_of is not None and window_of(el) != ctx["window"]:
            return None, refusal("the focused element is in another window than the focused step's "
                                 "before this one", code="identity")
        return el, None
    if ctx is not None:
        if ctx.get("focused") is not None and el == ctx["focused"]:
            return None, refusal("the focus did not move after the trigger: the element focused before it "
                                 "still has it", code="identity")
        window_of = getattr(tree, "window_of", None)
        if ctx.get("window") is not None and window_of is not None and window_of(el) != ctx["window"]:
            return None, refusal("the focused element is in another window than the trigger's",
                                 code="identity")
    return el, None


def is_secure(tree: Any, element: Any) -> bool:
    """Is ``element`` a password field (``AXElementDesc.secure``: AXSecureTextField, or an
    AXTextField with that subrole), as far as the tree can describe it?"""
    describe = getattr(tree, "describe_element", None)
    if describe is None:
        return False
    try:
        return bool(getattr(describe(element), "secure", False))
    except Exception:  # noqa: BLE001 - an element that cannot be described is judged by its role
        return False


# ---------------------------------------------------------------------------
# a row's scope: which folder or note list its window shows
# ---------------------------------------------------------------------------
# Finder's and Notes' windows carry no document, and a file list's identity (role, identifier,
# path, columns) is the same for every folder: a same-named file of ANOTHER folder would be
# selected. A row step therefore records its window's container scope (selector field ``scope``,
# a salted key) and is checked against the live one before anything is selected. What a scope
# is, how it is read, stored and compared is ``context/container_scope.py``'s
# (``container_scope``, ``scope_key``, ``same_scope``, ``in_container_content``, ``has_reader``);
# this module only applies its policy to rows, through the plug below.

#: ``same_scope``'s answers (``context/container_scope.py``)
SAME, DIFFERENT, UNCONFIRMED = "same", "different", "unconfirmed"
_SCOPE_API: List[Any] = []
_READ = object()


def _scope_api() -> Any:
    """THE plug: the container scope module (``anyuse.context.container_scope``) when this tree
    has it, else None (no container rule anywhere: rows resolve exactly as before). Imported
    once; a module without the six functions (``scope_strength`` included) counts as absent.
    Tests install a fake by replacing this function."""
    if not _SCOPE_API:
        try:
            from anyuse.context import container_scope as CS
            names = ("container_scope", "scope_key", "same_scope", "in_container_content", "has_reader",
                     "scope_strength")
            _SCOPE_API.append(CS if all(callable(getattr(CS, n, None)) for n in names) else None)
        except ImportError:
            _SCOPE_API.append(None)
    return _SCOPE_API[0]


def _scope_of(tree: Any, window: Any, app: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The scope reader hook: ``container_scope(tree, window, app)`` of the plugged module
    (``{kind, strength, key, title, source}``, plaintext, in memory), or None (no module, an app
    without a reader, nothing readable). Every scope read goes through here (``live_scope``)."""
    api = _scope_api()
    if api is None:
        return None
    try:
        return api.container_scope(tree, window, app)
    except Exception:  # noqa: BLE001 - an unreadable scope is no scope
        return None


def _bundle_of(tree: Any, sel: Optional[Dict[str, Any]] = None) -> str:
    getter = getattr(tree, "bundle_id", None)
    try:
        live = getter() if callable(getter) else None
    except Exception:  # noqa: BLE001
        live = None
    return str(live or (sel or {}).get("app") or "")


def live_scope(tree: Any, row: Any, window: Any = _READ, app: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The scope of the window ``row`` (or a list) is in (``tree.window_of``, unless the caller
    read it already), through the hook; None when it cannot be read. A read; the result is
    plaintext and stays in memory (``scope_key`` salts it)."""
    window_of = getattr(tree, "window_of", None)
    try:
        if window is _READ:
            window = window_of(row) if window_of is not None else None
    except Exception:  # noqa: BLE001
        return None
    return _scope_of(tree, window, app or _bundle_of(tree) or None)


def scope_key(scope: Optional[Dict[str, str]], create: bool = False) -> str:
    """``scope`` as the plugged module's salted key (``scopekey:...``; the document salt), or ""
    (no scope, no module, no salt). ``create`` makes the salt (a recording)."""
    api = _scope_api()
    if api is None or not scope:
        return ""
    try:
        return str(api.scope_key(scope, create=create) or "")
    except Exception:  # noqa: BLE001
        return ""


def _verdict(tree: Any, recorded: str, el: Any, app: str) -> str:
    """``same_scope(recorded, the live scope of el's window)``, or "" when the live scope cannot
    be read (``container_scope`` None: no container rule). A simulated world answers itself
    (``row_scope_verdict``: it holds the recorded keys, no window)."""
    own = getattr(tree, "row_scope_verdict", None)
    if own is not None:
        try:
            return str(own(el, recorded) or "")
        except Exception:  # noqa: BLE001
            return ""
    api = _scope_api()
    live = live_scope(tree, el, app=app) if api is not None else None
    if live is None:
        return ""
    try:
        return str(api.same_scope(recorded, live))
    except Exception:  # noqa: BLE001
        return ""


def scope_candidates(tree: Any, sel: Dict[str, Any], candidates: List[Any]) -> List[Any]:
    """The candidates (lists, or rows) a row step may still mean: one in a window whose scope is
    ``different`` from the recorded one never is (the module's policy). Without a recorded scope,
    a module or a reader for the app, all of them."""
    api = _scope_api()
    rec = str(sel.get("scope") or "")
    app = _bundle_of(tree, sel)
    if api is None or not rec or len(candidates) < 2 or not api.has_reader(app):
        return list(candidates)
    return [c for c in candidates if _verdict(tree, rec, c, app) != DIFFERENT]


def scope_refusal(tree: Any, sel: Dict[str, Any], row: Any) -> Optional[Dict[str, Any]]:
    """The container policy on the row ``find_row`` found, before anything is selected, as the
    resolver applies it (``context/container_scope.py``), in an app with a scope reader:

    * ``different`` (another folder: another full identity, kind or name): never the target,
      ``wrong_container``;
    * ``unconfirmed`` with a RECORDED name only (``scope_strength`` "title", and an old record
      without a scope counts as one): acts - a matching name confirms nothing, but another name
      would already have been ``different``;
    * ``unconfirmed`` with a RECORDED full path (read when recorded, not confirmable now): a row
      of the container's CONTENT (``in_container_content``: a file or note row) is refused,
      ``container_unconfirmed``; chrome (the sidebar) acts;
    * the live scope not readable (``container_scope`` None), no module, or an app without a
      reader: no container rule; ``find_row``'s one-list and one-row rules stay."""
    api = _scope_api()
    app = _bundle_of(tree, sel)
    rec = str(sel.get("scope") or "")
    try:
        if api is None or not rec or not api.has_reader(app):
            return None
    except Exception:  # noqa: BLE001
        return None
    verdict = _verdict(tree, rec, row, app)
    if verdict == DIFFERENT:
        return refusal("the row's window shows another folder or note list than the recorded one",
                       code="wrong_container")
    if verdict != UNCONFIRMED:
        return None                      # same, or no live scope: no container rule
    try:
        strength = str(api.scope_strength(rec) or "")
    except Exception:  # noqa: BLE001
        strength = ""
    if strength != "path":
        return None
    try:
        content = bool(api.in_container_content(tree, row, app))
    except Exception:  # noqa: BLE001 - an unreadable row counts as content (it only refuses)
        content = True
    if content:
        return refusal("the row's folder or note list was recorded by its full path and cannot be confirmed now "
                       "(only its name is readable, or the scope was recorded on another machine); a file or note "
                       "row is selected only in the confirmed one", code="container_unconfirmed")
    return None


def resolve_row(tree: Any, sel: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """``(element, None)`` or ``(None, refusal)`` for ``{"kind": "row", ...}``: the tree's
    ``find_row`` (the identified list, then the one row of that label or identifier), then the
    list's scope (``scope_refusal``)."""
    finder = getattr(tree, "find_row", None)
    if finder is None:
        return None, not_implemented("the 'row' selector kind (the AX tree cannot look for a row)")
    if container_identity(sel.get("container")) is None:
        return None, refusal("a row step needs its list's identity (identifier, description or path), "
                             "not only its role", code="identity")
    if not (sel.get("name") or sel.get("identifier")):
        return None, refusal("a row step needs the row's label or identifier", code="identity")
    why = _app_refusal(tree, sel)
    if why is not None:
        return None, why
    el = finder(sel)
    if el is None:
        problem = getattr(tree, "last_row_problem", None) or {}
        return None, refusal(str(problem.get("why") or "no row matched the recorded one"),
                             code=str(problem.get("code") or "not_found"))
    # the list's scope (which folder / note list), before anything is selected
    why = scope_refusal(tree, sel, el)
    if why is not None:
        return None, why
    return el, None


def resolve_kind(tree: Any, sel: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """The executor's ``_resolve_target`` for a selector with a ``kind``: ``focused``
    (``resolve_focused``), ``row`` (``resolve_row``); any other kind is refused
    (``unsupported_verb``), never resolved as a plain selector without its identity."""
    kind = str(sel.get("kind") or "")
    if kind == "focused":
        return resolve_focused(tree, sel)
    if kind == "row":
        return resolve_row(tree, sel)
    return None, not_implemented(f"the {kind[:40]!r} selector kind")


# ---------------------------------------------------------------------------
# the handlers: (tree, target, step, cmd) -> result dict
# ---------------------------------------------------------------------------

def _gated(tree: Any, where: str) -> Optional[Dict[str, Any]]:
    """A refusal (``acted: False``) when the tree's gates refuse actuation now (the drive gate,
    then the foreground interlock: ``AccessibilityTree.actuation_refusal``); None otherwise.
    Checked before a primitive is called, which checks them again itself."""
    check = getattr(tree, "actuation_refusal", None)
    why = check(where) if check is not None else None
    return refusal(str(why), code="gated", gated=True, acted=False) if why else None


def _acted(kind: str, reason: str, element: str, **extra: Any) -> Dict[str, Any]:
    """The step acted, and its read-back does not show the effect: a STOP kind with
    ``acted`` (``uncertain_completion`` / ``deviation``), so no ladder rung and no retry
    acts a second time."""
    from anyuse.execution.outcome import DEVIATION, UNCERTAIN_COMPLETION
    out = {"success": False, "acted": True, "method": "ax", "element": element, "error": reason,
           "outcome_kind": DEVIATION if kind == "deviation" else UNCERTAIN_COMPLETION,
           "reason_code": "deviation" if kind == "deviation" else "effect_not_observed"}
    out.update(extra)
    return out


def utf16_len(text: str) -> int:
    """A text's length in UTF-16 code units: the unit of an AX text range."""
    return len(text.encode("utf-16-le")) // 2


def split_lines(value: str) -> Optional[List[Tuple[str, str]]]:
    """``[(line, its break)]`` over every break of ``LINE_BREAKS`` (the last line's break is
    ""), or None when the value holds another break character."""
    if any(c in value for c in _OTHER_BREAKS):
        return None
    import re
    pieces = re.split("(" + "|".join(re.escape(b) for b in LINE_BREAKS) + ")", value)
    return [(pieces[i], pieces[i + 1] if i + 1 < len(pieces) else "") for i in range(0, len(pieces), 2)]


def apply_mode(mode: str, before: str, text: str, anchor: str = ""
               ) -> Tuple[Optional[str], Optional[Tuple[int, int]], str, str]:
    """``(value after, (start, length) replaced in UTF-16 units, problem, refusal code)``.
    Pure: the edit a mode makes to ``before``."""
    if mode == "set":
        return text, None, "", ""
    if mode == "append":
        return before + text, (utf16_len(before), 0), "", ""
    if mode != "replace_line":
        return None, None, f"unknown input mode {mode!r}", "unsupported_verb"
    if not anchor:
        return None, None, "replace_line needs an anchor", "identity"
    if any(b in text for b in LINE_BREAKS + _OTHER_BREAKS):
        return None, None, "a line replacement is one line (no line break)", "identity"
    lines = split_lines(before)
    if lines is None:
        return None, None, ("the field holds a line break this edit does not know (vertical tab, form "
                            "feed, a separator, NEL): where a line ends cannot be told"), "identity"
    hits = [i for i, (line, _br) in enumerate(lines) if anchor in line]
    if not hits:
        # the anchor is document text (or a run-time value): a message never quotes it
        return None, None, "no line of the field matches the anchor line", "not_found"
    if len(hits) > 1:
        return None, None, (f"{len(hits)} lines of the field match the anchor line; "
                            "refusing to pick one"), "ambiguous"
    i = hits[0]
    start = utf16_len("".join(line + br for line, br in lines[:i]))
    after = "".join((text if k == i else line) + br for k, (line, br) in enumerate(lines))
    return after, (start, utf16_len(lines[i][0])), "", ""


#: a start shape keeps at most this many line numbers per anchor (one decides, two refuse)
MAX_START_HITS = 3
#: a start shape's line count the proof rebuilds at most
MAX_START_LINES = 1_000_000


def start_shape(value: Optional[str], anchors: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """A field's start value as the proof needs it, without its text: ``{"lines": <count>,
    "anchors": {<anchor>: [the lines that contain it, at most MAX_START_HITS]}}`` over
    ``split_lines``; ``breaks: "other"`` when the value holds a break ``split_lines`` does not
    know. The watcher records it per text mode (``data.start``, keyed by the anchor's MARKER:
    ``step_capture.start_shape``); a live edit reports its unkeyed form (``_edit``). None for no
    value."""
    if not isinstance(value, str):
        return None
    lines = split_lines(value)
    if lines is None:
        return {"lines": 0, "anchors": {}, "breaks": "other"}
    out: Dict[str, Any] = {"lines": len(lines), "anchors": {}}
    for a in anchors:
        if isinstance(a, str) and a and a not in out["anchors"]:
            out["anchors"][a] = [i for i, (line, _br) in enumerate(lines) if a in line][:MAX_START_HITS]
    return out


def valid_start(shape: Any, anchor: str = "") -> Optional[Dict[str, Any]]:
    """A recorded start shape (``start_shape``'s form) checked and reduced to what the step
    needs: its line count, its break kind, and the lines of ITS anchor only (``anchor``: the
    step's recorded anchor, a marker or a literal; "" for an append). None when it is not one."""
    if not isinstance(shape, dict) or isinstance(shape.get("lines"), bool) or not isinstance(shape.get("lines"), int):
        return None
    anchors = shape.get("anchors") if shape.get("anchors") is not None else {}
    if not isinstance(anchors, dict) or not 0 <= shape["lines"] <= MAX_START_LINES:
        return None
    if shape.get("breaks") == "other":
        return {"lines": 0, "anchors": {}, "breaks": "other"}
    out: Dict[str, Any] = {"lines": shape["lines"], "anchors": {}}
    if anchor and anchor in anchors:
        hits = anchors[anchor]
        if not isinstance(hits, list) or any(isinstance(i, bool) or not isinstance(i, int)
                                             or not 0 <= i < shape["lines"] for i in hits):
            return None
        out["anchors"][anchor] = sorted(set(hits))[:MAX_START_HITS]
    return out


def keyed_start(edit_start: Any, anchor: str = "") -> Optional[Dict[str, Any]]:
    """A live edit's unkeyed start (``_edit``: ``{lines, anchor_lines, breaks?}``) as a start
    shape keyed by the step's own recorded anchor (the fill's filled step)."""
    if not isinstance(edit_start, dict) or not isinstance(edit_start.get("lines"), int):
        return None
    if edit_start.get("breaks") == "other":
        return {"lines": 0, "anchors": {}, "breaks": "other"}
    hits = edit_start.get("anchor_lines") if isinstance(edit_start.get("anchor_lines"), list) else []
    return {"lines": edit_start["lines"], "anchors": {anchor: list(hits)} if anchor else {}}


def start_value(shape: Any, place: Optional[Dict[str, str]] = None) -> Optional[str]:
    """A value of ``shape`` (``valid_start``): its lines empty except where an anchor was, which
    hold ``place[anchor]`` (the case's value of an anchor parameter; the anchor itself when
    ``place`` has none). None when the shape is not one, or when the rebuilt value would not
    have the same lines again (two placed texts that overlap)."""
    if isinstance(shape, dict) and shape.get("anchors") is None:
        shape = dict(shape, anchors={})
    if not isinstance(shape, dict) or isinstance(shape.get("lines"), bool) or \
            not isinstance(shape.get("lines"), int) or not isinstance(shape.get("anchors"), dict):
        return None
    if shape.get("breaks") == "other":
        return "\x0b"
    n = shape["lines"]
    if n < 1 or n > MAX_START_LINES:
        return None
    texts = {a: (place or {}).get(a, a) for a in shape["anchors"]}
    parts: List[List[str]] = [[] for _ in range(n)]
    for a, hits in shape["anchors"].items():
        if not isinstance(texts[a], str) or not texts[a] or not isinstance(hits, list) or \
                any(not isinstance(i, int) or not 0 <= i < n for i in hits):
            return None
        for i in hits:
            parts[i].append(texts[a])
    value = "\n".join(" ".join(p) for p in parts)
    got = (start_shape(value, list(texts.values())) or {}).get("anchors") or {}
    if any(got.get(texts[a]) != list(hits)[:MAX_START_HITS] for a, hits in shape["anchors"].items()):
        return None
    return value


def start_problem(shape: Any, anchor: str = "", what: str = "replace_line") -> str:
    """Why a text mode cannot be proved on ``shape`` (the recorded start shape), "" when it can:
    the recording must have kept one, and a replace_line's anchor (its recorded anchor: a marker
    or a literal) must be in a line of it (two lines are left to the run, which refuses them as
    ``ambiguous``)."""
    if shape is None:
        return (f"{what}: the recording kept no start value of the field (its shape when the recording "
                "focused it); the proof does not invent one")
    if not isinstance(shape, dict) or shape.get("invalid") or start_value(shape) is None:
        return f"{what}: the field's recorded start shape is not one the proof can rebuild"
    if what == "replace_line":
        if shape.get("breaks") == "other":
            return ("replace_line: the recorded document holds a line break replace_line does not "
                    "know (where a line ends cannot be told)")
        if not (shape.get("anchors") or {}).get(anchor):
            return "replace_line: the anchor line is not in the recorded document"
    return ""


def _element(cmd: Dict[str, Any]) -> str:
    return f"{cmd.get('role') or ''} '{cmd.get('title') or ''}'".strip()


def _text(step: Any, cmd: Dict[str, Any]) -> Any:
    v = step.get("input_data") if isinstance(step, dict) else getattr(step, "input_data", None)
    return v if v is not None else cmd.get("text")


def _read(tree: Any, element: Any) -> Optional[str]:
    reader = getattr(tree, "read_value", None)
    if reader is not None:
        return reader(element)
    describe = getattr(tree, "describe_element", None)
    return describe(element).value if describe is not None else None


def select_row(tree: Any, target: Any, step: Any, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Select the resolved row; read THAT list's selection back. A selection that holds more
    than this row (the row was added, not made the only one) is undone for this row and is a
    deviation; any other mismatch is uncertain."""
    sel = cmd.get("selector") if isinstance(cmd.get("selector"), dict) else {}
    # the bound label (a legacy role/title step without a selector names it in its title)
    label = str(sel.get("name") or cmd.get("title") or "")
    element = f"row {label!r}"
    if not label:
        return refusal("select_row needs the row's label", code="identity")
    act = getattr(tree, "select_row", None)
    read = getattr(tree, "selected_row_labels", None)
    if act is None or read is None:
        return not_implemented("select_row (the AX tree cannot select a row or read the selection)")
    want = container_identity(sel.get("container"))
    ident = getattr(tree, "row_container_identity", None)
    if want is not None and ident is not None and not container_matches(ident(target), want):
        return refusal("select_row: the row is not in the recorded list", code="identity")
    gated = _gated(tree, "ax.select_row")
    if gated is not None:
        return gated
    # the read-back compares the selected ELEMENTS with the target where the tree reads them
    # (a row of the same label selected instead does not pass); labels only otherwise
    rows = getattr(tree, "selected_rows", None)

    def selection():
        if rows is not None:
            got_rows = rows(target)
            return got_rows, ([str(x) for x in (read(target) or [])] if got_rows is not None else None)
        got_labels = read(target)
        return None, ([str(x) for x in got_labels] if got_labels is not None else None)
    before_rows, before = selection()
    accepted = bool(act(target))
    got_rows, got = selection()
    only = (got_rows is not None and len(got_rows) == 1 and got_rows[0] == target) if rows is not None \
        else got == [label]
    if only:
        return {"success": True, "method": "ax", "element": element, "readback": got}
    added = (target in got_rows and len(got_rows) > 1 and before_rows is not None and target not in before_rows) \
        if rows is not None and got_rows is not None else \
        (got is not None and label in got and len(got) > 1 and before is not None and label not in before)
    if added:
        undo = getattr(tree, "deselect_row", None)
        undone = bool(undo(target)) if undo is not None else False
        return _acted("deviation", f"select_row: the list's selection became {(got or [])[:5]!r}, more than the "
                                   f"one row; the row was {'deselected again' if undone else 'NOT deselected'}",
                      element, readback=got, undone=undone)
    return _acted("uncertain", f"select_row: the list's selection read back as "
                               f"{got[:5] if got is not None else 'unreadable'!r}, not the row {label!r} alone"
                               + ("" if accepted else " (the selection was not accepted)"), element,
                  readback=got)


def _edit(tree: Any, target: Any, step: Any, cmd: Dict[str, Any], mode: str) -> Dict[str, Any]:
    action = MODE_ACTIONS[mode]
    element = _element(cmd)
    text = _text(step, cmd)
    if not isinstance(text, str) or text == "":
        return refusal(f"{action} needs the text", code="identity")
    edit = getattr(tree, "edit_text", None)
    if edit is None:
        return not_implemented(f"{action} (the AX tree has no range edit)")
    before = _read(tree, target)
    if before is None:
        return refusal(f"{action}: the field's value cannot be read, so the edit could not be checked",
                       code="not_found")
    after, rng, why, code = apply_mode(mode, before, text, str(cmd.get("anchor") or ""))
    if why:
        return refusal(f"{action}: {why}", code=code)
    gated = _gated(tree, f"ax.{action}")
    if gated is not None:
        return gated
    ev = edit(target, text, replace_range=rng, expect_value=after)
    if getattr(ev, "refused", False) or str(getattr(ev, "error", "") or "").startswith("gated"):
        # the primitive's own gate refused (it changed since the check above): nothing was written
        return refusal(f"{action}: {getattr(ev, 'error', '') or 'refused'}", code="gated", gated=True, acted=False)
    got = _read(tree, target)
    # the field's start as the proof needs it, without its text and without the anchor: its line
    # count and the anchor's lines (the fill keys it by the filled step's anchor, ``keyed_start``)
    shape = start_shape(before, [str(cmd.get("anchor") or "")] if mode == "replace_line" else []) or {}
    start = {"lines": shape.get("lines", 0), "anchor_lines": next(iter(shape.get("anchors", {}).values()), []),
             **({"breaks": "other"} if shape.get("breaks") else {})}
    if got == after:
        return {"success": True, "method": "ax", "element": element, "rung": "ax_selected_text", "mode": mode,
                "readback_matches": True, "chars_before": len(before), "chars_after": len(got),
                "start": start}
    err = str(getattr(ev, "error", "") or "")
    return _acted("uncertain", f"{action}: the field's value after the edit is not the expected one"
                               + (f" ({err[:120]})" if err else ""), element, rung="ax_selected_text",
                  mode=mode, readback_matches=False, chars_before=len(before), chars_after=len(got or ""),
                  start=start)


def append_text(tree: Any, target: Any, step: Any, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Insert the text at the end of the field's value; read the value back."""
    return _edit(tree, target, step, cmd, "append")


def replace_line(tree: Any, target: Any, step: Any, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the one line that contains ``cmd["anchor"]``; read the value back."""
    return _edit(tree, target, step, cmd, "replace_line")


def open_app(tree: Any, target: Any, step: Any, cmd: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
    """Launch or activate the app by bundle id; read the frontmost app back."""
    bid = str(cmd.get("bundle_id") or "")
    name = str(cmd.get("app_name") or bid)
    element = f"app {name}"
    if not bid:
        return refusal("open_app needs the app's bundle id", code="identity")
    act = getattr(tree, "activate_app", None)
    front = getattr(tree, "frontmost_bundle_id", None)
    if act is None or front is None:
        return not_implemented("open_app (the AX tree cannot launch or activate an app)")
    gated = _gated(tree, f"ax.activate_app({bid})")
    if gated is not None:
        return gated
    ok = bool(act(bid))
    got = front()
    # the activation's evidence where the tree keeps it (AccessibilityTree.last_activation: every poll's reading with
    # its time, the source that answered and each source's read: context.accessibility.FRONTMOST_SOURCES)
    ev = getattr(tree, "last_activation", None)
    extra = {"activation": ev} if isinstance(ev, dict) and ev.get("bundle_id") == bid else {}
    if not ok or got != bid:
        last = (ev.get("polls") or [{}])[-1] if extra else {}
        reads = (f"; {last.get('front_source') or 'no source'} answered {last.get('bundle_id') or 'nothing'} after "
                 f"{last.get('t', 0)} s ({ev.get('verdict')}; reads "
                 + ", ".join(f"{k}={v or 'none'}" for k, v in (last.get("reads") or {}).items()) + ")") if last else ""
        return _acted("uncertain", f"open_app: {bid} is not in front after the switch (front: "
                                   f"{got or 'unknown'}{reads})", element, front=got, **extra)
    if extra:
        extra["front_source"] = ev.get("front_source")         # which source confirmed the switch (ax, nsworkspace_tick, ...)
    return {"success": True, "method": "ax", "element": element, "front": got, **extra}


def target_state(value: Any) -> Optional[str]:
    """``on`` / ``off`` for a target state as a map, a recording or a step names it (on, off, true, false,
    checked, unchecked, 1, 0); None for anything else (a toggle without a target is never pressed)."""
    v = str(value if value is not None else "").strip().lower()
    if v in ("on", "true", "1", "checked", "check"):
        return "on"
    if v in ("off", "false", "0", "unchecked", "uncheck"):
        return "off"
    return None


def set_toggle(tree: Any, target: Any, step: Any, cmd: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
    """A STATE-AWARE toggle: read the state; press once only when it is not the target;
    read it back. The target already: no press (a menu opened to reach the item is closed). An unreadable
    state is refused (never a blind press); a read-back that is not the target is a deviation."""
    want = target_state(cmd.get("target_state"))
    element = _element(cmd)
    if want is None:
        return refusal("set_toggle needs its target state (on or off): a toggle is never pressed blind",
                       code="identity", acted=False)
    read = getattr(tree, "toggle_state", None)
    press = getattr(tree, "perform_action", None)
    if read is None or press is None:
        return not_implemented("set_toggle (the AX tree cannot read a toggle's state)")
    close = getattr(tree, "close_menu", None)
    before = read(target)
    if before not in TOGGLE_STATES:
        closed = bool(close(target)) if close is not None else False
        return refusal(f"set_toggle: the state of {element} cannot be read (a menu item's mark, a checkbox's "
                       f"value); nothing was pressed (never a blind press)", code="state_unreadable",
                       acted=False, target_state=want, menu_closed=closed)
    if before == want:
        gated = _gated(tree, "ax.close_menu")
        if gated is not None:
            return gated
        if close is not None and not close(target):
            return _acted("uncertain", f"set_toggle: {element} is {want} already (nothing pressed), but the menu "
                                       "opened to reach it could not be closed", element, pressed=False,
                          state_before=before, target_state=want)
        return {"success": True, "method": "ax", "element": element, "pressed": False, "state_before": before,
                "state_after": before, "target_state": want}
    gated = _gated(tree, "ax.set_toggle")
    if gated is not None:
        return gated
    accepted = bool(press(target, "AXPress"))
    readback = getattr(tree, "toggle_readback", None)
    after = readback(target, want) if readback is not None else read(target)
    if after == want:
        return {"success": True, "method": "ax", "element": element, "pressed": True, "state_before": before,
                "state_after": after, "target_state": want}
    if after not in TOGGLE_STATES:
        return _acted("uncertain", f"set_toggle: {element} was pressed once ({before} -> {want}); its state "
                                   "cannot be read back", element, pressed=True, state_before=before,
                      state_after=None, target_state=want)
    return _acted("deviation", f"set_toggle: {element} was pressed once to go {before} -> {want}, and reads "
                               f"{after} afterwards" + ("" if accepted else " (the press was not accepted)"),
                  element, pressed=True, state_before=before, state_after=after, target_state=want)


def shortcut_flags(flags: Any) -> int:
    """The modifier bits of recorded CGEventFlags (``SHORTCUT_MASK``); 0 for anything unreadable."""
    try:
        return int(flags) & SHORTCUT_MASK
    except (TypeError, ValueError):
        return 0


def _one_char(c: Any) -> str:
    c = str(c or "")
    return c.lower() if len(c) == 1 and c.isprintable() and not c.isspace() else ""


def shortcut_char(keys: Any, char: Any = None) -> str:
    """The recorded shortcut's character, lower case: the step's ``char`` (what the chord stood for on the
    recording layout: ``step_capture.layout_char``), else the name's last part when it is one character
    (``cmd+n``, ``cmd++``; not ``cmd+key45``); "" when the recording has none."""
    own = _one_char(char)
    if own:
        return own
    name = str(keys or "")
    return _one_char("+" if name.endswith("++") or name == "+" else name.rsplit("+", 1)[-1])


def shortcut_refusal(keys: Any, keycode: Any, flags: Any, char: Any = None, layout: Any = None,
                     by_key: bool = True) -> Optional[str]:
    """Why this chord is never sent as a shortcut (None when it may be): not a command / control chord,
    flags beyond the modifier bits, or a destructive shortcut (quit, close, delete, force quit).

    Destructive is decided by character: the recorded one, and every one ``keycode`` types on
    ``layout`` (the layout the key is sent on; ``context.keyboard.Layout``). Without a layout the
    recorded character decides (the replay checks the key it sends); a recording without a character
    is checked against the US positions. ``by_key`` False checks the recorded character only (the
    replay, before it has read the layout the key goes out on)."""
    try:
        code, f = int(keycode), int(flags)
    except (TypeError, ValueError):
        return "a shortcut replay needs the recorded keycode and modifier flags"
    if f & ~SHORTCUT_MASK:
        return f"the shortcut's flags {f:#x} carry more than the modifier bits"
    if not f & (SHORTCUT_FLAGS["cmd"] | SHORTCUT_FLAGS["ctrl"]):
        return "only a command or control chord is replayed as a shortcut"
    name = str(keys or "")
    ch = shortcut_char(name, char)
    what = REFUSED_SHORTCUT_CHARS.get(ch) or REFUSED_SHORTCUT_KEYS.get(code)
    if what:
        return f"the shortcut {name or code!r} {what}: it is never replayed as a key"
    if not by_key or (layout is None and ch):
        return None
    if layout is None:
        from anyuse.context.keyboard import US_LAYOUT as layout
    for typed in sorted({c.lower() for c in layout.chars_of(code)}):
        what = REFUSED_SHORTCUT_CHARS.get(typed)
        if what:
            where = getattr(layout, "source_id", "") or "the layout in use"
            return (f"the shortcut {name or code!r} would be sent as key {code}, which types {typed!r} on "
                    f"{where}: it {what}, and it is never replayed as a key")
    return None


def shortcut_key(layout: Any, keycode: int, flags: int, char: str, recorded_layout: str = "",
                 from_name: bool = False) -> Dict[str, Any]:
    """The key a recorded shortcut is sent as on ``layout`` (the one in use; None: unreadable):
    ``{"keycode", "flags", "how", "layout", "note"?}``, or ``{"refused": why}``.

    The recorded keycode and flags when they still stand for the recorded character there
    (``Layout.chord_char``), else the key that types the character: Shift stays a modifier of a letter
    (⌘⇧Z), and is otherwise set by what that layout needs for the character (the recorded Shift typed
    it: ⌘⇧= recorded on US is ⌘+, key 30 without Shift on German); none does: refused. No recorded
    character, or no layout: the recorded keycode, with a note.

    ``from_name``: the character is the recorded name's (a recording older than the step's ``char``),
    which held the event's own string: the recorded Shift is kept, the recorded key is kept when it types
    the character with or without Shift, and an Option chord goes by its recorded keycode (its name holds
    the Option glyph the event carried, ``opt+cmd+∂``)."""
    from anyuse.context.keyboard import FLAG_OPTION, FLAG_SHIFT, chord_maps, is_letter
    out = {"keycode": keycode, "flags": flags, "layout": getattr(layout, "source_id", None) or None}
    if not char:
        return dict(out, how="recorded key position",
                    note="no character was recorded for this shortcut: it is sent by its recorded key position")
    if layout is None:
        return dict(out, how="recorded key position",
                    note="the keyboard layout could not be read: the shortcut is sent by its recorded key position")
    kept = {layout.chord_char(keycode, flags)}
    if from_name:
        kept.add(layout.chord_char(keycode, flags & ~FLAG_SHIFT))
    if char in kept:
        return dict(out, how="recorded key")
    if from_name and flags & FLAG_OPTION:
        return dict(out, how="recorded key position",
                    note=(f"an older recording of an Option chord: its name holds the character the event carried "
                          f"({char!r}), not its key's, so it is sent by its recorded key position"))
    maps = chord_maps(flags)
    hit = layout.find(char, maps)
    if hit is None:
        return {"refused": (f"no key types {char!r} on the keyboard layout {layout.source_id or 'in use'}"
                            + (f" (the shortcut was recorded on {recorded_layout})" if recorded_layout else "")
                            + ": nothing was sent")}
    code, shift, table = hit
    shift = shift or bool(flags & FLAG_SHIFT and (from_name or is_letter(char)))
    sent_flags = (flags & ~FLAG_SHIFT) | (FLAG_SHIFT if shift else 0)
    out.update(keycode=code, flags=sent_flags, how="layout")
    if (code, sent_flags) != (keycode, flags):
        moved = sent_flags & FLAG_SHIFT != flags & FLAG_SHIFT

        def key(c: int, f: int) -> str:
            return f"key {c}" + ((" with Shift" if f & FLAG_SHIFT else " without Shift") if moved else "")
        out["note"] = (f"recorded as {key(keycode, flags)}{f' on {recorded_layout}' if recorded_layout else ''}; "
                       f"sent as {key(code, sent_flags)}, which types {char!r} on "
                       f"{layout.source_id or 'the layout in use'}")
        if layout.is_dead(code, shift, table=table):
            out["note"] += " as a dead key (an app that matches characters may see none)"
    return out


def _counts(tree: Any) -> Optional[Dict[str, int]]:
    read = getattr(tree, "window_counts", None)
    if read is None:
        return None
    try:
        got = read()
    except Exception:  # noqa: BLE001 - unreadable: the drift gate of the next step checks the screen
        return None
    return dict(got) if isinstance(got, dict) else None


def _moved(before: Dict[str, int], after: Dict[str, int], want: Dict[str, str]) -> bool:
    # A missing counter is unknown, never evidence that a window or sheet disappeared.
    return all(k in before and k in after
               and ((after[k] > before[k]) if way == "more" else (after[k] < before[k]))
               for k, way in want.items())


def _layout_now(tree: Any) -> Any:
    """The tree's keyboard layout in use (``context.keyboard.Layout``), None when it has none to read."""
    from anyuse.context.keyboard import Layout
    read = getattr(tree, "keyboard_layout", None)
    if read is None:
        return None
    try:
        got = read()
    except Exception:  # noqa: BLE001 - unreadable: the recorded key position, said in the result
        return None
    return got if isinstance(got, Layout) else None


def press_shortcut(tree: Any, target: Any, step: Any, cmd: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
    """Send a recorded shortcut to the recorded app in front; read its recorded effect back.

    The chord is sent by its recorded CHARACTER: once the app is in front, the layout in use is read
    (``tree.keyboard_layout``) and the key that types the character there is sent (``shortcut_key``),
    so a ⌘Z recorded on QWERTZ (key 16) is key 6 on US. Without a recorded character or a readable
    layout the recorded key goes, and the result says so (``note``). The refusal runs on the recorded
    character and again on what the key sent types on that layout (the US positions when unreadable,
    where a key that types q or w on another common layout is refused too)."""
    import time as _time
    keys = str(cmd.get("keys") or "")
    element = f"shortcut {keys or cmd.get('keycode')}"
    why = shortcut_refusal(keys, cmd.get("keycode"), cmd.get("flags"), char=cmd.get("char"), by_key=False)
    if why is not None:
        return refusal(why, code="identity")
    code, flags = int(cmd["keycode"]), int(cmd["flags"])
    bid = str(cmd.get("bundle_id") or "")
    press = getattr(tree, "press_key", None)
    front = getattr(tree, "frontmost_bundle_id", None)
    if not bid:
        return refusal("a shortcut replay needs the app's bundle id (a key goes to the app in front)", code="identity")
    if press is None or front is None:
        return not_implemented("press_shortcut (the AX tree cannot send a key or read the app in front)")
    own = getattr(tree, "bundle_id", None)
    bound = own() if own is not None else None
    if bound and bound != bid:
        return refusal(f"wrong app: the AX tree is bound to {bound}, the shortcut was recorded in {bid}",
                       code="wrong_app", element=element)
    gated = _gated(tree, f"ax.press_shortcut({keys})")
    if gated is not None:
        return gated
    if front() != bid:
        act = getattr(tree, "activate_app", None)
        if act is not None:
            act(bid)
        got = front()
        if got != bid:
            return refusal(f"a shortcut goes to the app in front: {got or 'no app'} is in front, the step was "
                           f"recorded in {bid}", code="wrong_app", element=element)
    # read with the app in front: an app or a document can have its own input source
    layout = _layout_now(tree)
    char = shortcut_char(keys, cmd.get("char"))
    sent = shortcut_key(layout, code, flags, char, str(cmd.get("layout") or ""),
                        from_name=not _one_char(cmd.get("char")))
    if sent.get("refused"):
        return refusal(f"press_shortcut: {sent['refused']}", code="identity", acted=False, element=element)
    unread = layout is None
    if unread:
        from anyuse.context.keyboard import US_LAYOUT as layout
    why = shortcut_refusal(keys, sent["keycode"], sent["flags"], char=cmd.get("char"), layout=layout)
    if why is not None:
        return refusal(why, code="identity", acted=False, element=element)
    if unread:
        # the US positions are a guess: a key that types q or w on another common layout is not sent
        from anyuse.context.keyboard import unread_layout_risk
        risk = unread_layout_risk(sent["keycode"], sent["flags"], char)
        if risk:
            return refusal(f"press_shortcut: {risk}", code="identity", acted=False, element=element)
    code, flags = int(sent["keycode"]), int(sent["flags"])
    sent_as = {k: sent[k] for k in ("keycode", "flags", "how", "layout")}
    noted = {"note": sent["note"]} if sent.get("note") else {}
    want = {k: v for k, v in (cmd.get("expect") or {}).items() if k in SHORTCUT_EFFECTS and v in ("more", "fewer")}
    before = _counts(tree) if want else None
    if want and (before is None or any(k not in before for k in want)):
        # the recorded effect is what checks this replay: a count that cannot be read now could not
        return refusal(f"press_shortcut: the recorded effect of {keys} ({', '.join(f'{k} {v}' for k, v in want.items())}) "
                       "cannot be read here (no window count), so nothing was sent", code="identity",
                       acted=False, element=element)
    if not press(code, flags):
        return {"success": False, "method": "ax", "element": element, "sent_as": sent_as, **noted,
                "error": f"the shortcut {keys} was not sent (the key press failed or was gated)"}
    out = {"success": True, "method": "ax", "element": element, "replayed_as": "shortcut", "keys": keys,
           "sent_as": sent_as, **noted}
    if before is None:
        return out                      # no recorded count: the next step's drift gate checks the screen
    deadline = _time.monotonic() + SHORTCUT_EFFECT_S
    after = _counts(tree)
    while (after is None or not _moved(before, after, want)) and _time.monotonic() < deadline:
        _time.sleep(SHORTCUT_POLL_S)
        after = _counts(tree)
    if after is None or not _moved(before, after, want):
        # an unreadable count afterwards is no evidence of the effect (nor of a window closed)
        shown = ", ".join(f"{k} {before.get(k, 0)} -> {after.get(k, 'unreadable') if after is not None else 'unreadable'} "
                          f"(recorded: {v})" for k, v in want.items())
        return _acted("uncertain", f"press_shortcut: {keys} was sent and its recorded effect did not appear "
                                   f"({shown})", element, counts_before=before, counts_after=after,
                      sent_as=sent_as, **noted)
    out.update(counts_before=before, counts_after=after)
    return out


HANDLERS: Dict[str, Callable[[Any, Any, Any, Dict[str, Any]], Dict[str, Any]]] = {
    "select_row": select_row, "append_text": append_text, "replace_line": replace_line,
    "open_app": open_app, "set_toggle": set_toggle, "press_shortcut": press_shortcut,
}
