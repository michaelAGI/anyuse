"""The watcher -> automation seam: a recorded draft becomes a saved, parameterized workflow.

Contract and format: the design notes. Headless (CLI ``anyuse workflow from-draft``,
``anyuse workflow drafts`` and ``anyuse workflow replay``).

What it reads (never edits) - ``load_source``:

* a watcher draft: ``<session_id>/<workflow_name>`` (``watcher.storage.SessionStorage`` +
  ``RawWorkflowDraft.from_session``) or a draft JSON file (``RawWorkflowDraft.to_dict()``
  shape, plus the optional seam keys ``taught_by``, ``app``, ``bundle_id``, ``ax_states``);
* the guest drive harness's recording (the measurement harness record`` writes
  ``workflow.json``: the planned steps with their recorded AX selectors, the per-step
  fingerprints and the template);
* a web recording directory in ``anyuse workflow compile``'s layout (``NAME.rec.json`` and
  ``NAME.fresh*.json``): compiled and proved by the unchanged web compiler
  (``api_verify.compile_and_verify``), persisted as ``compiled_api.json``.

What it writes - ``save_workflow``: an ordinary ``WorkflowStorage`` entry
(``workflow_<id>/workflow.json``, listed by ``anyuse workflow list``) with no legacy steps,
and beside it ``draft_workflow.json`` (``FORMAT``): the steps as ``PlannedStep`` dicts with
their recorded AX selectors and ``${name}`` placeholders, the ``WorkflowTemplate`` and the
``StateGraph`` the drift gate reads, the approval gates, the drift profile, the rungs, the
proof, the attribution and the compiler version. ``core/agent.py`` refuses to play such a
workflow without its parameters; ``replay_saved`` runs it.

How it runs - ``run_steps`` (the one runner for the proof and the live replay): every step
is bound, every gated step is held for approval (``HILGate(mode="all")``) BEFORE it is
dispatched, and every step goes through ``StateGraphExecutor.execute_checked_step`` (the
recorded identity check, the drift gate, the after-step verification). Real input only ever
leaves through ``context/accessibility.py``'s primitives, which check the drive gate and the
foreground interlock themselves; ``replay_saved`` checks both before it builds anything and
refuses on the host.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import getpass
import glob
import hashlib
import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import draft_params as DP

FORMAT = "anyuse.draft-workflow/1"
#: the newest record version this runner runs. A record says the LOWEST version that runs it
#: (``record_compiler``): 1 for press / toggle / enter_text / press_key on recorded selectors in
#: one app; 2 when a step needs ``execution/ax_actions.py``'s contract (select_row, append_text,
#: replace_line, open_app, the ``focused`` selector kind) or carries its own app. An AnyUse whose
#: runner knows version 1 only refuses a version-2 record (``record_version_problem``): its
#: executor would turn an action it does not know into a press.
COMPILER = {"name": "draft_compile", "version": 2}
RECORD_FILE = "draft_workflow.json"
HIL_MODE = "all"
#: the drift settings a record states: what the settings resolve to when it is compiled (a replay reads the
#: replaying machine's settings).
DRIFT_SETTING_NAMES = ("ANYUSE_DRIFT_EXCLUDE_MENUBAR", "ANYUSE_DRIFT_MISSING_TOLERANCE")


def drift_profile_settings() -> Dict[str, Any]:
    """The drift gate's settings in force now (``settings.get``): what replay applies on this machine."""
    from anyuse.settings import get as get_setting
    return {"ANYUSE_DRIFT_EXCLUDE_MENUBAR": bool(get_setting("ANYUSE_DRIFT_EXCLUDE_MENUBAR")),
            "ANYUSE_DRIFT_MISSING_TOLERANCE": float(get_setting("ANYUSE_DRIFT_MISSING_TOLERANCE"))}

#: the closed vocabulary of recorded verbs -> the executor's AX action. Anything else is
#: refused with its reason; "skip" verbs carry no effect an AX replay must reproduce.
VERBS = {"click": "press", "press": "press", "tap": "press", "choose": "press", "select": "press",
         "pick": "press", "type": "enter_text", "enter_text": "enter_text", "input": "enter_text",
         "set_value": "enter_text", "key": "press_key", "press_key": "press_key",
         "keypress": "press_key", "toggle": "toggle", "check": "set_toggle", "uncheck": "set_toggle",
         # a STATE-AWARE toggle: the step carries its target state; a recorded check /
         # uncheck keeps its observed end state (on / off), a toggle with an observed end state becomes one too
         "set_toggle": "set_toggle",
         "scroll": "skip", "hover": "skip", "move": "skip", "wait": "skip", "read": "skip",
         "screenshot": "skip",
         # an app switch (launch or activate an app by bundle id; ax_actions.open_app)
         "open_app": "open_app", "launch_app": "open_app", "activate_app": "open_app",
         "switch_app": "open_app"}
#: keys an AX replay may press: the guest guard's own list (``desktop_e2e.key_allowed``)
KEYS = {"return": 36, "enter": 36, "escape": 53, "esc": 53}
#: how the compile report names a recorded shortcut no menu item was found for, compiled as the chord itself
#: (``ax_actions.press_shortcut``)
SHORTCUT_VIA = "replayed as a shortcut"
#: verbs the structured step capture records that the AX runner does not reproduce
REFUSED_VERBS = {
    "right_click": "a right click opens a context menu, which the AX runner does not open",
    "double_click": "a double click is not replayed by the AX runner (AXPress is a single press)",
    "drag": "a drag is not replayed by the AX runner",
}
#: a typed value the watcher kept in the entity vault: ``[[ent:<class>:<12 hex>]]``
VAULT_MARKER = re.compile(r"\[\[ent:[a-z_]{1,32}:[0-9a-f]{12}\]\]")
#: a list row's label or list name the watcher keeps as presence-tier content (step_capture)
_CONTENT_MARKER = re.compile(r"\[\[ent:content_label:[0-9a-f]{12}\]\]")
_RAW_ROLE = {"button": "AXButton", "popupbutton": "AXPopUpButton", "menuitem": "AXMenuItem",
             "menu": "AXMenu", "textfield": "AXTextField", "textarea": "AXTextArea",
             "passwordfield": "AXSecureTextField", "combobox": "AXComboBox",
             "searchfield": "AXSearchField", "checkbox": "AXCheckBox", "radiobutton": "AXRadioButton",
             "switch": "AXSwitch", "slider": "AXSlider", "stepper": "AXStepper", "link": "AXLink",
             "text": "AXStaticText", "tab": "AXTab", "row": "AXRow", "cell": "AXCell",
             "disclosure": "AXDisclosureTriangle", "window": "AXWindow", "dialog": "AXSheet",
             "group": "AXGroup", "image": "AXImage", "heading": "AXHeading", "list": "AXList",
             "table": "AXTable", "outline": "AXOutline", "toolbar": "AXToolbar"}
BROWSER_BUNDLES = ("com.apple.Safari", "com.google.Chrome", "org.mozilla.firefox",
                   "company.thebrowser.Browser", "com.google.Chrome.for.Testing")
_DESC_RE = re.compile(r"""^(?P<role>[a-z]+)
    (?:\ (?P<name>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"))?
    (?:\ id=(?P<id>\S+))?
    (?:\ path=(?P<path>\S+))?$""", re.X)


class DraftError(ValueError):
    """The reference names no draft (the API answers 404)."""


class MalformedDraft(DraftError):
    """The draft was found but cannot be read as one: a wrong type, a size cap, bad JSON (400)."""


def _check_shape(data: Any, kind: str) -> None:
    from anyuse.execution import draft_schema
    try:
        draft_schema.check(data, kind)
    except draft_schema.SchemaError as exc:
        raise MalformedDraft(str(exc)) from exc


# ---------------------------------------------------------------------------
# 1. The normalized draft
# ---------------------------------------------------------------------------

@dataclass
class DraftStep:
    index: int
    action: str                              # press | enter_text | press_key | toggle | set_toggle | select_row | open_app | skip | ""
    intent: str = ""
    selector: Optional[Dict[str, str]] = None
    raw_role: str = ""
    value: Optional[str] = None              # the text typed
    keycode: Optional[int] = None
    flags: int = 0
    state_id: str = ""                       # the recorded state before this step
    problem: str = ""                        # why this step cannot be replayed
    event_id: str = ""
    value_ref: bool = False                  # value is an entity-vault marker, not the text
    verb: str = ""                           # the recorded verb, as captured
    #: a gap the agent bridge may fill once: "no_identity",
    #: "prose", "gesture", "key", "no_text", "map_hop" or "sensitive"; "" for a replayable step
    gap: str = ""
    #: a password field, or a step withheld at capture (payment data, a terminal): filled only
    #: when the run allows sensitive steps (``ANYUSE_FILL_SENSITIVE`` / ``fill --sensitive``)
    sensitive: bool = False
    #: the app the step was recorded in (watcher data ``app`` / ``bundle_id``)
    app: str = ""
    bundle_id: str = ""
    #: a typing step's input mode: "" (set: the whole value), "append" or "replace_line"
    input_mode: str = ""
    anchor: str = ""                         # replace_line: a text exactly one line contains
    #: a text mode's recorded start shape (watcher data ``start``: line count, the lines its anchor
    #: is in; no text): the proof rebuilds the field from it (``ax_actions.valid_start``)
    start: Optional[Dict[str, Any]] = None
    #: why the compiler added this step ("app switch": an open_app between two apps' steps)
    via: str = ""
    #: why the capture kept only this step's kind (``data.withheld``), "" for a step it kept
    withheld: str = ""
    #: a ``set_toggle``'s target state (on / off): a recorded check / uncheck's observed end state, a map flag's
    #: setting from the text; the runner presses only when the live state differs
    target_state: str = ""
    #: a ``press_shortcut``: the recorded chord's name (cmd+n) and the recorded effect its read-back checks
    #: (``{windows|sheets: more|fewer}``, from the fingerprints before and after it)
    keys: str = ""
    expect: Optional[Dict[str, str]] = None
    #: a ``press_shortcut``: the character the chord stood for on the recording layout (``layout_char``) and
    #: that layout's input source id; the replay sends the key that types it on the layout in use
    char: str = ""
    layout: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class DraftSource:
    ref: str
    kind: str                                # watcher_session | draft_file | drive_recording | web_recording
    name: str
    goal: str = ""
    app: str = ""
    bundle_id: str = ""
    apps: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    steps: List[DraftStep] = field(default_factory=list)
    states: Dict[str, dict] = field(default_factory=dict)   # state_id -> UIState dict
    partial: bool = False
    attribution: Dict[str, Any] = field(default_factory=dict)
    web: Optional[Dict[str, Any]] = None
    digest: str = ""
    #: the steps run in more than one app (each step carries its own ``app`` / ``bundle_id``, an
    #: ``open_app`` step switches between them). A one-app draft compiles as before.
    multi_app: bool = False

    @property
    def is_web(self) -> bool:
        return bool(self.urls) or self.bundle_id in BROWSER_BUNDLES or self.kind == "web_recording"


class BundleId(str):
    """A bundle id handed to a ``tree_factory`` (a multi-app record's app): the CLI looks it up
    among the running apps; a plain string stays a process name (``pgrep -x``), as before."""


def _app_key(app: str, bundle_id: str) -> str:
    return (bundle_id or app or "").strip().lower()


def _step_app_pairs(steps: Sequence[DraftStep], app: str, bundle_id: str
                    ) -> Tuple[Tuple[str, str], List[Tuple[str, str]]]:
    """``(the draft's own pair, the pair each step runs in)``: a step's own app, else the previous
    step's, else the draft's; a missing name or bundle id is completed from a pair seen elsewhere
    in the draft."""
    pairs = [(app, bundle_id)] + [(s.app, s.bundle_id) for s in steps]
    by_bid = {b: a for a, b in pairs if a and b}
    by_name = {a.lower(): b for a, b in pairs if a and b}

    def complete(a: str, b: str) -> Tuple[str, str]:
        return a or by_bid.get(b, ""), b or by_name.get(a.lower(), "")
    start = cur = complete(app, bundle_id)
    out = []
    for s in steps:
        if s.app or s.bundle_id:
            cur = complete(s.app, s.bundle_id)
        out.append(cur)
    return start, out


def _multi_app(steps: Sequence[DraftStep], app: str, bundle_id: str) -> bool:
    if any(s.action == "open_app" for s in steps):
        return True
    start, pairs = _step_app_pairs(steps, app, bundle_id)
    keys = {_app_key(*p) for p in [start] + pairs}
    keys.discard("")
    return len(keys) > 1


#: why the watcher withheld a step for privacy (a guarded surface: a secure field, payment data, a
#: terminal, a flagged page, a meeting, an unreadable window); ``not_a_text_field``, an unreadable
#: value and the like are no guarded surface
PRIVACY_WITHHELD = frozenset({"secure_field", "terminal", "phishing_suspected", "meeting_surface",
                              "surface_unknown", "denylist_app", "denylist_title"})


def _privacy_withheld(s: DraftStep) -> bool:
    return s.withheld in PRIVACY_WITHHELD or s.withheld.startswith("never_captured")


def _with_app_switches(steps: List[DraftStep], app: str, bundle_id: str) -> List[DraftStep]:
    """A multi-app draft: every step gets its app, and where the app changes without a recorded
    ``open_app``, one is inserted (``via: app switch``); the steps are numbered again. An app
    whose steps were withheld at capture (a guarded surface) is never an ``open_app`` target: a
    withheld step switches nothing, and a switch into that app is refused (not a gap)."""
    start, pairs = _step_app_pairs(steps, app, bundle_id)
    current = _app_key(*start) or (_app_key(*pairs[0]) if pairs else "")
    guarded = {_app_key(a, b) for s, (a, b) in zip(steps, pairs) if _privacy_withheld(s)}
    out: List[DraftStep] = []
    for s, (a, b) in zip(steps, pairs):
        s.app, s.bundle_id = a, b
        key = _app_key(a, b)
        if s.action == "open_app":
            current = key
        elif _privacy_withheld(s):
            pass                         # stays a sensitive gap in place; the app in front is unchanged
        elif key and key != current:
            sw = DraftStep(index=0, action="open_app", intent=f"switch to {a or b}"[:200], verb="open_app",
                           app=a, bundle_id=b, via="app switch")
            if not b:
                sw.problem = "an app switch without the app's bundle id cannot be replayed"
            elif key in guarded:
                sw.problem = (f"an app switch into {a or b}, whose steps were withheld at capture (a guarded "
                              "surface), is not replayed")
            out.append(sw)
            current = key
        out.append(s)
    for k, s in enumerate(out):
        s.index = k
    return out


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def parse_selector_description(desc: str) -> Optional[Dict[str, str]]:
    """The AX identity ``watcher/anchors.py`` writes into ``ClickAnchor.target_description``
    (``role 'name' id=… path=…``), back to a selector dict; None for any other text (a model's
    prose description is not an identity)."""
    m = _DESC_RE.match((desc or "").strip())
    if not m or m.group("role") not in _RAW_ROLE:
        return None
    name = ""
    if m.group("name"):
        try:
            name = str(ast.literal_eval(m.group("name")))
        except (ValueError, SyntaxError):
            return None
    sel = {"canonical_role": m.group("role"), "name": name, "identifier": m.group("id") or "",
           "path": m.group("path") or "", "raw_role": ""}
    return sel if (sel["name"] or sel["identifier"] or sel["path"]) else None


def _selector_dict(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    sel = {"canonical_role": str(raw.get("canonical_role") or "unknown"),
           "name": str(raw.get("name") or ""), "identifier": str(raw.get("identifier") or ""),
           "path": str(raw.get("path") or ""), "raw_role": str(raw.get("raw_role") or "")}
    if sel["raw_role"]:
        from anyuse.context.accessibility import canonical_role
        from_raw = canonical_role(sel["raw_role"])
        # a secure raw role is a password field whatever canonical role the recording claims
        if sel["canonical_role"] == "unknown" or from_raw in DP.SECRET_ROLES:
            sel["canonical_role"] = from_raw
    mp = raw.get("menu_path")
    if isinstance(mp, (list, tuple)) and all(isinstance(t, str) for t in mp):
        # a menu element's place (the menu titles above it), kept: the replay presses it only there (dropped
        # here, a map hop would lose the place its identity rests on)
        sel["menu_path"] = list(mp)
    return sel


def _key_ok(code: Any, flags: Any) -> bool:
    from anyuse.execution.desktop_e2e import key_allowed
    try:
        return code is not None and key_allowed(int(code), int(flags or 0))
    except (TypeError, ValueError):
        return False


def _usable_states(raw: Any) -> Dict[str, dict]:
    """Recorded fingerprints that can serve as a drift baseline. A placeholder (no required
    elements and no structural hash) is dropped: the drift gate would always fire on it."""
    from anyuse.guides.state_graph import UIState
    out: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for sid, sd in raw.items():
        if not isinstance(sd, dict):
            continue
        try:
            fp = UIState.from_dict(dict(sd, state_id=str(sid))).fingerprint
        except Exception:  # noqa: BLE001 - an unreadable fingerprint is no baseline
            continue
        if fp.required_elements or (fp.structural_hash or "").strip():
            out[str(sid)] = dict(sd, state_id=str(sid))
    return out


def _taught_by(explicit: Any) -> Tuple[str, str]:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:120], "draft"
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}", "os_user"
    except Exception:  # noqa: BLE001
        return "unknown", "unknown"


def _focuses_role(prev: Optional[Dict[str, Any]]) -> str:
    """The text-field role the previous step's effect focused (its data ``focuses``: a raw or
    canonical role), else ""."""
    from anyuse.execution import ax_actions as AX
    raw = (prev or {}).get("focuses")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    role = AX.canonical(raw.strip()) if raw.strip().startswith("AX") else raw.strip().lower()
    return role if role in AX.FOCUS_ROLES else ""


def _is_chord(data: Dict[str, Any]) -> bool:
    """A recorded key step that is a command / control chord with its keycode: a shortcut the capture found
    no menu item for (one it found is a ``choose`` of that item)."""
    from anyuse.execution import ax_actions as AX
    try:
        code, flags = data.get("keycode"), int(data.get("flags") or 0)
    except (TypeError, ValueError):
        return False
    return (code is not None and not isinstance(code, bool)
            and bool(flags & (AX.SHORTCUT_FLAGS["cmd"] | AX.SHORTCUT_FLAGS["ctrl"])))


def _shortcut_step(st: DraftStep, data: Dict[str, Any]) -> DraftStep:
    """A shortcut no menu item was found for: compiled as the chord itself (``press_shortcut``, modifier
    bits only, with the recorded character and layout the replay sends it by), unless it is one never sent
    as a key (quit, close, delete, force quit: a gap). Whether the recording shows an effect its replay can
    be checked against is decided once every step is read (``_shortcut_effects``)."""
    from anyuse.execution import ax_actions as AX
    keys = data.get("keys") or data.get("key") or ""
    name = str(keys[0] if isinstance(keys, list) and len(keys) == 1 else keys)[:40]
    flags = AX.shortcut_flags(data.get("flags"))
    char = AX.shortcut_char("", data.get("char"))
    layout = data.get("layout")
    why = AX.shortcut_refusal(name, data.get("keycode"), flags, char=char or None)
    if why is not None:
        st.problem = f"{why} (a shortcut is replayed as its menu item, or as itself when it is safe to send)"
        st.gap = "key"
        return st
    st.action, st.keycode, st.flags, st.keys = "press_shortcut", int(data["keycode"]), flags, name
    st.char, st.layout = char, str(layout)[:120] if isinstance(layout, str) else ""
    st.via, st.selector = SHORTCUT_VIA, None
    st.intent = f"press the shortcut {name} ({SHORTCUT_VIA}: no menu item was found for it)"
    return st


def _shortcut_effects(steps: Sequence[DraftStep], states: Dict[str, dict], bundle_id: str = "") -> None:
    """A ``press_shortcut`` is kept only with a recorded effect its replay is checked against: the window /
    sheet count of the recorded screen before it against the one before the next step (``expect``, read back
    by the handler), or a next step whose recorded screen differs (its drift gate checks it). Without one, or
    without the app's bundle id (a key goes to the app in front), it is a ``key`` gap again."""
    for i, st in enumerate(steps):
        if st.action != "press_shortcut" or st.problem:
            continue
        st.bundle_id = st.bundle_id or bundle_id
        nxt = next((s for s in steps[i + 1:] if s.action not in ("skip", "")), None)
        before = (states.get(st.state_id) or {}).get("fingerprint") or {} if st.state_id else {}
        after = ((states.get(nxt.state_id) or {}).get("fingerprint") or {}
                 if nxt is not None and nxt.state_id else {})
        expect: Dict[str, str] = {}
        fb, fa = before.get("role_frequency"), after.get("role_frequency")
        if isinstance(fb, dict) and isinstance(fa, dict):
            # only fingerprints that kept their role counts: a missing count is unknown, never zero
            for key, role in (("windows", "AXWindow"), ("sheets", "AXSheet")):
                b, a = int(fb.get(role) or 0), int(fa.get(role) or 0)
                if a != b:
                    expect[key] = "more" if a > b else "fewer"
        screen_after = bool(after) and nxt is not None and not nxt.problem and nxt.state_id != st.state_id
        why = ""
        if not st.bundle_id:
            why = "a shortcut replay needs the app's bundle id (a key goes to the app in front)"
        elif not (expect or screen_after):
            why = ("the recording shows no effect of it to check a replay against (a shortcut is replayed as "
                   "its menu item, or as itself when its recorded effect can be checked)")
        if why:
            st.action, st.via, st.expect = "press_key", "", None
            st.problem, st.gap = f"the shortcut {st.keys!r} matches no menu item, and {why}", "key"
            continue
        st.expect = expect or None


def _step_from_event(i: int, ev: Any, anchors: Dict[str, Any],
                     prev: Optional[Dict[str, Any]] = None) -> DraftStep:
    """One STEP tag as a normalized step. ``prev`` is the previous STEP's data (a trigger whose
    effect focused a field says so in ``focuses``)."""
    from anyuse.execution import ax_actions as AX
    data = dict(getattr(ev, "data", None) or {})
    verb = str(data.get("action") or "").strip().lower()
    intent = (getattr(ev, "reason", "") or getattr(ev, "target", "") or verb)[:200]
    st = DraftStep(index=i, action="", intent=intent, event_id=getattr(ev, "id", ""),
                   state_id=getattr(ev, "ax_state_id", None) or "", verb=verb[:40],
                   app=str(data.get("app") or "")[:200], bundle_id=str(data.get("bundle_id") or "")[:200])
    # a step captured in a password field or withheld at capture is a sensitive gap, whatever its verb
    st.sensitive = data.get("secure") is True or bool(data.get("withheld"))
    st.withheld = str(data.get("withheld") or "")[:80]
    if verb in REFUSED_VERBS:
        st.problem, st.gap = REFUSED_VERBS[verb], "gesture"
        st.selector = _selector_dict(data.get("selector"))
        return st
    action = VERBS.get(verb)
    if action is None:
        st.problem = (f"the recorded action {verb[:60]!r} is not one of the replayable verbs "
                      f"({', '.join(sorted(set(VERBS)))}); the watcher described it in prose")
        # a step with no description at all gives the model nothing to complete: no gap
        st.gap = "prose" if st.intent.strip() else ""
        return st
    st.action = action
    if action == "skip":
        return st
    if action in ("toggle", "set_toggle"):
        want = toggle_target(verb, data)
        if want is not None:
            st.action, st.target_state = "set_toggle", want
        elif action == "set_toggle" or any(data.get(k) is not None for k in ("target_state", "end_state")):
            # a target named but not on / off is refused too, never replayed as the blind toggle
            st.selector = _selector_dict(data.get("selector"))
            st.problem = ("a state-aware toggle without its target state (on / off): a toggle is never "
                          "replayed as a blind press")
            return st
    if data.get("map_hop") is True and data.get("secure") is not True and not data.get("withheld"):
        # a plan hop the map holds no AX identity for (a row, the field a trigger focused, an edit):
        # the fill grounds it on the live screen, to a listed element or the focused one
        st.selector = _selector_dict(data.get("selector"))
        if action == "enter_text":
            st.input_mode, st.anchor = _input_mode(data)
            val = data.get("text")
            if isinstance(val, str) and val and not _PLACEHOLDER.search(val):
                # the literal this typing gap types (from the task text or the caller) stays with the gap, so it
                # is a candidate the overrides make a parameter: the fill types the parameter, the proof varies it
                st.value = val
        st.problem = "a plan hop the map could not ground (it holds no AX identity for it)"
        st.gap = "map_hop"
        return st
    if data.get("secure") is True:
        st.problem = (("typed into" if action == "enter_text" else "a key pressed in")
                      + " a password field: credentials are never stored (the fill types one from "
                        "the credential vault)")
        st.gap = "sensitive"
        return st
    withheld = str(data.get("withheld") or "")
    if withheld:
        st.problem = (f"withheld at capture ({withheld[:60]}): the watcher kept only this step's "
                      "kind, not its target or value")
        st.gap = "sensitive"
        return st
    if action == "open_app":
        # an app switch is identified by the bundle id alone; it has no element to fill in
        if not st.bundle_id:
            st.problem = "an app switch without the app's bundle id cannot be replayed"
        elif not getattr(ev, "reason", ""):
            st.intent = f"switch to {st.app or st.bundle_id}"[:200]
        return st
    sel = _selector_dict(data.get("selector"))
    anchor = anchors.get(getattr(ev, "anchor_id", None) or "")
    if sel is None and anchor is not None:
        sel = parse_selector_description(getattr(anchor, "target_description", ""))
    unreleased = False
    if sel is not None:
        from anyuse.execution import ax_actions as _AX
        for k in ("name", "identifier"):
            # a list row's label (``content_label``) stays a marker: a choice parameter takes its
            # place (``_resolve_content``); any other marker left here (content of another
            # element, a label this machine did not release) is no identity to replay by
            keep = (k == "name" and sel["canonical_role"] in _AX.ROW_ROLES
                    and _CONTENT_MARKER.fullmatch(sel.get(k) or ""))
            if VAULT_MARKER.fullmatch(sel.get(k) or "") and not keep:
                sel[k], unreleased = "", True
        if data.get("source") == "ax_capture" and not getattr(ev, "reason", ""):
            st.intent = " ".join(x for x in (verb, sel.get("raw_role") or sel["canonical_role"],
                                             repr(sel["name"]) if sel["name"] else "",
                                             f"#{sel['identifier']}" if sel["identifier"] else "") if x)[:200]
        if unreleased and not (sel["name"] or sel["identifier"]):
            st.problem = ("the target's label is in the entity vault of the machine that recorded it and "
                          "was not released here (compile it there, or export it with --release-labels)")
            st.gap = "no_identity"
            return st
    kind = str((data.get("selector") or {}).get("kind") or "") if isinstance(data.get("selector"), dict) else ""
    if kind and kind not in AX.SELECTOR_KINDS:
        st.problem = f"the selector kind {kind[:40]!r} is not one the AX runner has ({', '.join(AX.SELECTOR_KINDS)})"
        return st
    if kind == "focused" and action not in ("enter_text", "press_key"):
        st.problem = "the focused element is a target for typing and keys only"
        st.gap = "no_identity"
        return st
    if action == "press_key" and _is_chord(data):
        return _shortcut_step(st, data)
    if (action in ("enter_text", "press_key")
            and (data.get("focused") is True or kind == "focused" or _focuses_role(prev))
            and (sel is None or not (sel["name"] or sel["identifier"] or sel["path"]))):
        # no recorded identity, but recorded as the field that had focus right after the previous
        # step: the ``focused`` selector kind (ax_actions.resolve_focused), with a role check
        role = ""
        if sel is not None and sel["canonical_role"] not in ("", "unknown"):
            role = sel["canonical_role"]
        elif isinstance(data.get("role"), str) and data["role"].strip():
            r = data["role"].strip()
            role = AX.canonical(r) if r.startswith("AX") else r.lower()
        else:
            role = _focuses_role(prev)
        if role in DP.SECRET_ROLES and action == "press_key":
            st.problem = ("a key pressed in a password field: credentials are never stored (the fill types "
                          "one from the credential vault)")
            st.gap, st.sensitive = "sensitive", True
            return st
        if role not in AX.FOCUS_ROLES and role not in DP.SECRET_ROLES:
            st.problem = (f"the field focused after the previous step is not a text field "
                          f"({role or 'no role recorded'})")
            st.gap = "no_identity"
            st.selector = sel
            return st
        # the recorded document and app stay: the executor's wrong-document / wrong-app checks
        # run on a focused target as on any other
        raw_sel = data.get("selector") if isinstance(data.get("selector"), dict) else {}
        document = str((sel or {}).get("document") or raw_sel.get("document") or "")
        from anyuse.context.accessibility import document_key_is_local, is_document_key
        if VAULT_MARKER.search(document) or (is_document_key(document) and not document_key_is_local(document)):
            # the watcher keeps a document as a key salted on the machine that recorded it
            st.problem = ("the focused field's document was recorded on another machine (a salted key "
                          "that matches no document here): compile it there")
            st.gap = "no_identity"
            return st
        sel = AX.focused_selector(role, (sel or {}).get("raw_role") or "", document=document,
                                  app=str(raw_sel.get("app") or ""))
    if action == "press_key":
        keys = data.get("keys") or data.get("key") or ""
        name = (keys[0] if isinstance(keys, list) and len(keys) == 1 else keys)
        code = data.get("keycode")
        if code is None and isinstance(name, str):
            code = KEYS.get(name.strip().lower())
        if not _key_ok(code, data.get("flags")):
            st.problem = (f"the key {str(keys)[:40]!r} is not one an AX replay presses "
                          "(Return, Escape; a shortcut is replayed as its menu item, or as itself)")
            st.gap = "key"
            return st
        st.keycode, st.flags, st.selector = int(code), 0, sel
        if sel is None:
            st.problem = "a key press needs the recorded identity of the element that had focus"
            st.gap = "no_identity"
        else:
            st.raw_role = sel.get("raw_role") or _RAW_ROLE.get(sel["canonical_role"], "")
        return st
    if action == "press" and sel is not None and sel["canonical_role"] in AX.ROW_ROLES and sel["name"]:
        # a row of a list, table or outline: selected by its label - the text of its FIRST
        # cell - inside its list, identified (ax_actions.select_row). A click on a cell of
        # another column names another text: a gap, never a row picked by that text. A row or
        # cell click WITHOUT a label is compiled as before this kind (a press on its recorded
        # path and identifier, which find_selector resolves as it always did).
        st.selector = sel
        if sel["canonical_role"] == "cell" and AX.cell_column(sel.get("path") or "") != 0:
            st.problem = ("a click on a cell that is not in the first column (or of an unknown column): a row "
                          "is selected by its first cell's text")
            st.gap = "no_identity"
            return st
        raw_sel = data.get("selector") if isinstance(data.get("selector"), dict) else {}
        container = (AX.container_identity(data.get("container")) or AX.container_identity(raw_sel.get("container"))
                     or AX.container_from_path(sel.get("path") or ""))
        if container is None:
            st.problem = ("a row click without its list's identity (its identifier, description or path): "
                          "the row could be found in another list")
            st.gap = "no_identity"
            return st
        if VAULT_MARKER.search(_CONTENT_MARKER.sub("", container["identifier"] + container["description"])):
            st.problem = ("the row's list is named by a label in the entity vault of the machine that recorded it "
                          "and was not released here (compile it there, or export it with --release-labels)")
            st.gap = "no_identity"
            return st
        # a row's own identifier is kept (a cell's is its column's); find_row uses it only when
        # it names one row, else the label
        ident = sel.get("identifier") or "" if sel["canonical_role"] != "cell" else ""
        # the folder / note list the row's window showed: the salted key, as recorded
        scope = raw_sel.get("scope") if isinstance(raw_sel.get("scope"), str) else ""
        scope = scope if scope.startswith("scopekey:") and len(scope) <= 200 else ""
        st.action, st.selector, st.raw_role = ("select_row", AX.row_selector(sel["name"], container, ident, scope),
                                               "AXRow")
        return st
    if sel is None:
        st.problem = "no recorded AX identity for this step (no selector, no AX anchor)"
        st.gap = "no_identity"
        return st
    st.selector = sel
    st.raw_role = sel.get("raw_role") or _RAW_ROLE.get(sel["canonical_role"], "")
    if action == "enter_text":
        val = data.get("text", data.get("value"))
        ref = data.get("value_ref")
        if val is None and isinstance(ref, str) and VAULT_MARKER.fullmatch(ref):
            # the watcher's structured capture keeps the text in the entity vault; the
            # marker is the candidate's value (the text never enters the compile)
            val, st.value_ref = ref, True
        st.input_mode, st.anchor = _input_mode(data)
        if sel["canonical_role"] in DP.SECRET_ROLES:
            st.problem = ("typed into a password field: credentials are never stored (the fill types "
                          "one from the credential vault)")
            st.gap, st.sensitive = "sensitive", True
        elif st.input_mode not in ("", "append", "replace_line"):
            st.problem = (f"the input mode {st.input_mode[:40]!r} is not one the AX runner has "
                          "(set, append, replace_line)")
        elif (st.input_mode == "replace_line" and not VAULT_MARKER.fullmatch(st.anchor)
              and _anchor_problem(st.anchor)):
            # a marker anchor (the watcher keeps the line's anchor as a typed value) becomes a
            # parameter of its own (``_anchor_parameters``); a literal one must be one line
            # the fill supplies the text and the anchor
            st.problem = f"replace_line: {_anchor_problem(st.anchor)}"
            st.gap = "no_text"
        elif not isinstance(val, str) or val == "":
            st.problem = "a typing step without the typed text"
            st.gap = "no_text"
        elif st.input_mode == "replace_line" and ("\n" in val or "\r" in val):
            st.problem = "replace_line: a line replacement is one line (the recorded text has a line break)"
        else:
            st.value = val
    return st


def toggle_target(verb: str, data: Dict[str, Any]) -> Optional[str]:
    """The target state a recorded toggle keeps: ``check`` -> on, ``uncheck`` -> off, else
    the step's own ``target_state`` (the map's flag setting) or its observed end state (``checked``: true /
    false); None when the recording observed none (a ``toggle`` stays the executor's plain toggle)."""
    from anyuse.execution import ax_actions as AX
    if verb == "check":
        return "on"
    if verb == "uncheck":
        return "off"
    for k in ("target_state", "end_state"):
        if data.get(k) is not None:
            return AX.target_state(data.get(k))
    if isinstance(data.get("checked"), bool):
        return "on" if data["checked"] else "off"
    return None


def _record_starts(starts: Sequence[Tuple[DraftStep, Any]]) -> None:
    """Each text mode's recorded start shape (watcher data ``start``: the field's line count and
    the lines of its anchor, keyed by the anchor as recorded, a ``typed_text`` marker or a
    literal; never the field's text), taken as it is and reduced to the step's own anchor
    (``ax_actions.valid_start``). One that is not a shape is ``{"invalid": True}``; a step
    without one keeps none (the proof then refuses its text mode)."""
    from anyuse.execution import ax_actions as AX
    for st, shape in starts:
        got = AX.valid_start(shape, st.anchor if st.input_mode == "replace_line" else "")
        st.start = got if got is not None else {"invalid": True}


def _mark_triggers(steps: Sequence[DraftStep]) -> None:
    """A ``focused`` step names the step whose effect focused its field (``trigger``: the
    previous step's index); the runner checks that this is the step right before it and that
    the focus moved (``run_steps``, ``ax_actions.resolve_focused``). When that trigger is itself
    a ``focused`` step (a Return right after the focused typing), ``trigger_kind: "focused"``
    says so: the focus must then still be on the element the trigger typed into."""
    for k, s in enumerate(steps):
        if isinstance(s.selector, dict) and s.selector.get("kind") == "focused":
            prev = steps[k - 1].selector if k > 0 else None
            if k > 0:
                s.selector["trigger"] = steps[k - 1].index
            else:
                s.selector.pop("trigger", None)
            if isinstance(prev, dict) and prev.get("kind") == "focused":
                s.selector["trigger_kind"] = "focused"
            else:
                s.selector.pop("trigger_kind", None)


def _input_mode(data: Dict[str, Any]) -> Tuple[str, str]:
    """A typing step's ``(input_mode, anchor)``: "" for set (today's whole-value entry)."""
    mode = str(data.get("input_mode") or "").strip().lower()
    return ("" if mode == "set" else mode), str(data.get("anchor") or "")


def _anchor_problem(anchor: str) -> str:
    from anyuse.execution import ax_actions as AX
    if not anchor.strip():
        return "it needs an anchor (a text exactly one line of the field contains)"
    if VAULT_MARKER.search(anchor):
        return ("its anchor is in the entity vault of the machine that recorded it and was not released here "
                "(compile it there, or export it with --release-labels)")
    if len(anchor) > AX.MAX_ANCHOR or _CONTROL.search(anchor) or "\n" in anchor or "${" in anchor:
        return (f"the anchor must be one line of at most {AX.MAX_ANCHOR} characters, with no control "
                "character and no ${...}")
    return ""


def _states_from_guides(app: str, state_ids: Sequence[str]) -> Dict[str, dict]:
    """The fingerprints the watcher fed into the app's guide (``ax_state_id``), best effort."""
    if not app or not state_ids:
        return {}
    try:
        from anyuse.guides.storage import GuideStorage
        guide = GuideStorage().get_by_app(app)
        graph = guide.get_state_graph() if guide else None
    except Exception:  # noqa: BLE001 - no guide is "no baseline", not an error
        return {}
    out = {}
    for sid in state_ids:
        st = graph.get_state(sid) if graph else None
        if st is not None:
            out[sid] = st.to_dict()
    return out


def _released_labels(data: Dict[str, Any]) -> Dict[str, Any]:
    """The structured step capture stores UI labels (selector names, fingerprint titles) as
    ``ui_label`` vault markers. They are resolved here, in-process, from this machine's
    entity vault (the ``draft_compile`` standing grant; audited). A marker this machine
    cannot resolve (another machine's recording, no vault) is dropped from a fingerprint
    (its role is still required; an identifier becomes unknown) and from a selector
    (``_step_from_event`` then refuses a step left without a name or identifier, and one whose
    row list, focused document or anchor is still a marker)."""
    from anyuse.watcher import step_capture as SC
    text = json.dumps(data, ensure_ascii=False)
    if not (SC.LABEL_MARKER.search(text) or _CONTENT_MARKER.search(text)):
        return data
    from anyuse.watcher.entity_vault import get_default_vault
    resolve = SC.vault_label_resolver(get_default_vault())
    out, _n = SC.release_labels(data, resolve) if resolve is not None else (copy.deepcopy(data), 0)
    for st in (out.get("ax_states") or {}).values():
        for el in ((st or {}).get("fingerprint") or {}).get("required_elements") or []:
            # a marker left (a label not released here, content: presence-tier, never resolved into
            # a fingerprint) is no title to compare: the role stays required
            if isinstance(el.get("title"), str) and VAULT_MARKER.fullmatch(el["title"]):
                el["title"] = ""
            if isinstance(el.get("identifier"), str) and VAULT_MARKER.fullmatch(el["identifier"]):
                del el["identifier"]     # not released here: unknown, not "has none"
    return out


def source_from_raw_draft(data: Dict[str, Any], ref: str, kind: str = "draft_file",
                          guide_states: bool = False) -> DraftSource:
    from anyuse.watcher.models import RawWorkflowDraft, TagType
    _check_shape(data, "watcher_draft")
    digest = _digest(data)
    data = _released_labels(data)
    try:
        draft = RawWorkflowDraft.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        raise MalformedDraft(f"not a watcher draft: {exc}") from exc
    anchors = {a.id: a for a in draft.anchors}
    steps: List[DraftStep] = []
    prev: Optional[Dict[str, Any]] = None
    starts: List[Tuple[DraftStep, Any]] = []
    for ev in draft.events:
        # STEP only, as the composer builds steps: an ACTION tag details a STEP, it is not another one
        if ev.tag_type == TagType.STEP:
            steps.append(_step_from_event(len(steps), ev, anchors, prev))
            prev = dict(ev.data or {})
            if steps[-1].input_mode and prev.get("start") is not None:
                starts.append((steps[-1], prev["start"]))
    _record_starts(starts)
    app = str(data.get("app") or (draft.apps_involved[0] if draft.apps_involved else ""))
    bundle_id = str(data.get("bundle_id") or "")
    _mark_triggers(steps)
    multi = _multi_app(steps, app, bundle_id)
    if multi:
        start, pairs = _step_app_pairs(steps, app, bundle_id)
        if not _app_key(*start) and pairs:
            start = pairs[0]                     # the record starts in the first step's app
        steps = _with_app_switches(steps, app, bundle_id)
        app, bundle_id = start[0] or app, start[1] or bundle_id
        _mark_triggers(steps)                    # the indices moved
    states = _usable_states(data.get("ax_states"))
    if guide_states and not states:
        # each step's fingerprint is in the guide of the app it was recorded in
        by_app: Dict[str, List[str]] = {}
        for s in steps:
            if s.state_id:
                by_app.setdefault((s.app if multi and s.app else app), []).append(s.state_id)
        for app_name, ids in by_app.items():
            states.update(_states_from_guides(app_name, ids))
    for s in steps:
        if s.state_id and s.state_id not in states:
            s.state_id = ""          # an id with no recorded fingerprint is no baseline
    _shortcut_effects(steps, states, bundle_id)
    stamps = sorted(e.timestamp.isoformat() for e in draft.events)
    who, how = _taught_by(data.get("taught_by"))
    partial = bool(data.get("is_partial")) or not any(
        e.tag_type == TagType.WORKFLOW_END for e in draft.events)
    apps = list(draft.apps_involved)
    for s in steps:
        if s.app and s.app not in apps:
            apps.append(s.app)
    return DraftSource(
        ref=ref, kind=kind, name=draft.name, goal=draft.goal, app=app,
        bundle_id=bundle_id, apps=apps,
        urls=list(draft.urls_visited), steps=steps, states=states, partial=partial,
        attribution={"taught_by": who, "taught_by_source": how,
                     "taught_at": stamps[0] if stamps else "", "taught_until": stamps[-1] if stamps else "",
                     "session_id": draft.session_id, "workflow_name": draft.name},
        digest=digest, multi_app=multi)


def source_from_drive_recording(data: Dict[str, Any], ref: str) -> DraftSource:
    """``interlock_drive_e2e.py record``'s ``workflow.json``."""
    _check_shape(data, "drive_recording")
    wf, plan, graph = data.get("workflow") or {}, data.get("plan") or [], data.get("graph") or {}
    if not isinstance(wf, dict) or not isinstance(plan, list) or not all(isinstance(p, dict) for p in plan):
        raise MalformedDraft("a drive recording needs a workflow object and a list of planned steps")
    intents = [x if isinstance(x, dict) else {} for x in (wf.get("steps") or [])]
    steps: List[DraftStep] = []
    for i, p in enumerate(plan):
        cmd = p.get("command") if isinstance(p.get("command"), dict) else {}
        action = str(cmd.get("action") or "")
        st = DraftStep(index=i, action=action if action in ("press", "enter_text", "press_key",
                                                              "toggle", "set_toggle", "cancel", "confirm") else "",
                       intent=str(p.get("intent") or "")[:200], raw_role=str(cmd.get("role") or ""),
                       state_id=str((intents[i] if i < len(intents) else {}).get("expected_before_state")
                                    or ""))
        if not st.action:
            st.problem = f"the recorded action {action!r} is not replayable here"
        sel = _selector_dict(cmd.get("selector"))
        if sel is None:
            st.problem = st.problem or "no recorded AX selector"
        st.selector = sel
        if action == "enter_text":
            val = p.get("input_data")
            if sel and sel["canonical_role"] in DP.SECRET_ROLES:
                st.problem = "typed into a password field: never stored"
            elif not isinstance(val, str) or val == "":
                st.problem = st.problem or "a typing step without the typed text"
            else:
                st.value = val
        if action == "press_key":
            if not _key_ok(cmd.get("keycode"), cmd.get("flags")):
                st.problem = st.problem or (f"the key {cmd.get('keycode')!r} with flags "
                                            f"{cmd.get('flags')!r} is not one an AX replay presses")
            else:
                st.keycode, st.flags = int(cmd["keycode"]), int(cmd.get("flags") or 0)
        if action == "set_toggle":
            from anyuse.execution import ax_actions as AX
            st.target_state = AX.target_state(cmd.get("target_state")) or ""
            if not st.target_state:
                st.problem = st.problem or "a state-aware toggle without its target state (on / off)"
        steps.append(st)
    states = _usable_states(graph.get("states") if isinstance(graph, dict) else None)
    for s in steps:
        if s.state_id and s.state_id not in states:
            s.state_id = ""
    who, how = _taught_by(data.get("taught_by") or "interlock_drive_e2e record (guest harness)")
    req_app = str(wf.get("required_app") or "")
    bundle_id = str(data.get("bundle_id") or wf.get("bundle_id") or (req_app if "." in req_app and " " not in req_app else ""))
    return DraftSource(
        ref=ref, kind="drive_recording", name=str(wf.get("name") or "recorded workflow"),
        goal=str(wf.get("description") or wf.get("name") or ""), app=req_app,
        bundle_id=bundle_id,
        apps=[req_app], steps=steps, states=states,
        attribution={"taught_by": who, "taught_by_source": how,
                     "taught_at": str(data.get("recorded_at") or ""), "session_id": "",
                     "workflow_name": str(wf.get("name") or "")},
        digest=_digest(data))


def source_from_web_dir(path: str, ref: str, name: str = "") -> DraftSource:
    recs = sorted(glob.glob(os.path.join(path, f"{name or '*'}.rec.json")))
    if len(recs) != 1:
        raise DraftError(f"{path}: expected exactly one NAME.rec.json, found {len(recs)} "
                         "(pass the name to pick one)")
    stem = os.path.basename(recs[0])[:-len(".rec.json")]
    try:
        with open(recs[0], encoding="utf-8") as fh:
            recording = json.load(fh)
    except (OSError, ValueError) as exc:
        raise MalformedDraft(f"{ref}: {stem}.rec.json is not readable JSON ({exc})") from exc
    if not isinstance(recording, dict):
        raise MalformedDraft(f"malformed draft: {stem}.rec.json must be an object, not {type(recording).__name__}")
    who, how = _taught_by(recording.get("taught_by"))
    files = {role: sorted(glob.glob(os.path.join(path, f"{stem}.{role}*.json")))
             for role in ("fresh", "fit", "sel")}
    return DraftSource(
        ref=ref, kind="web_recording", name=stem, goal=str(recording.get("goal") or stem),
        app=str(recording.get("site") or ""), urls=[str(recording.get("site") or "")],
        attribution={"taught_by": who, "taught_by_source": how,
                     "taught_at": str(recording.get("recorded_at") or ""), "session_id": "",
                     "workflow_name": stem},
        web={"dir": path, "name": stem, "rec": recs[0], **files},
        digest=_digest({"rec": recording, "files": {k: [os.path.basename(f) for f in v]
                                                    for k, v in files.items()}}))


def drafts_inbox() -> str:
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("drafts"))


_SESSION_REF = re.compile(r"^(?P<sid>[A-Za-z0-9_-]{1,64})/(?P<wf>[^/\\]{1,200})$")


def _load_path(path: str, ref: str, web_name: str) -> DraftSource:
    if os.path.isdir(path):
        return source_from_web_dir(path, ref, web_name)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise MalformedDraft(f"{ref}: not readable JSON ({exc})") from exc
    if isinstance(data, dict) and "plan" in data and "graph" in data:
        return source_from_drive_recording(data, ref)
    if isinstance(data, dict) and "events" in data:
        return source_from_raw_draft(data, ref)
    raise MalformedDraft(f"{ref}: neither a watcher draft nor a drive recording")


def load_source(ref: Any, *, session_storage=None, inbox: Optional[str] = None,
                web_name: str = "", allow_paths: bool = True) -> DraftSource:
    """A draft by reference: an inline dict, a draft in the inbox (a file or a web recording
    directory), ``<session_id>/<workflow_name>``, or - with ``allow_paths`` (the CLI; never the
    API) - any path."""
    if isinstance(ref, dict):
        if "plan" in ref and "graph" in ref:
            return source_from_drive_recording(ref, "inline")
        return source_from_raw_draft(ref, "inline")
    if not isinstance(ref, str):
        raise MalformedDraft(f"a draft is a reference or an object, not {type(ref).__name__}")
    ref = ref.strip()
    if not ref:
        raise MalformedDraft("no draft reference given")
    if len(ref) > 1000:
        raise MalformedDraft("the draft reference is longer than 1000 characters")
    if allow_paths:
        path = os.path.expanduser(ref)
        if os.path.exists(path):
            return _load_path(path, ref, web_name)
    box = os.path.realpath(inbox or drafts_inbox())
    for cand in (os.path.join(box, ref), os.path.join(box, ref + ".json")):
        real = os.path.realpath(cand)
        if real.startswith(box + os.sep) and os.path.exists(real):
            return _load_path(real, ref, web_name)
    m = _SESSION_REF.match(ref)
    if m and m.group("sid") not in (".", ".."):
        sid, wname = m.group("sid"), m.group("wf")
        from anyuse.watcher.models import RawWorkflowDraft
        storage = session_storage
        if storage is None:
            from anyuse.watcher.storage import SessionStorage
            storage = SessionStorage()
        session = storage.load(sid)
        if session is None:
            raise DraftError(f"no watcher session {sid!r}")
        structured = list(getattr(session, "structured_workflow_names", []) or [])
        if wname in structured:
            # the structured step capture (watcher/step_capture.py): verbs, AX identity, the
            # fingerprints before each step, typed values as vault markers
            src = source_from_raw_draft(session.structured_draft(wname), ref, kind="watcher_session")
            if not src.attribution.get("taught_at"):
                src.attribution["taught_at"] = session.started_at.isoformat()
            return src
        if wname not in session.workflow_names:
            raise DraftError(f"session {sid} has no workflow {wname!r} "
                             f"(it has {', '.join(session.workflow_names + structured) or 'none'})")
        draft = RawWorkflowDraft.from_session(session, wname)
        data = draft.to_dict()
        data["is_partial"] = not any(e.tag_type.value == "WORKFLOW_END" for e in draft.events)
        src = source_from_raw_draft(data, ref, kind="watcher_session", guide_states=True)
        if not src.attribution.get("taught_at"):
            src.attribution["taught_at"] = session.started_at.isoformat()
        return src
    raise DraftError(f"{ref!r} is not a draft in the inbox or SESSION/WORKFLOW"
                     + (" or a path" if allow_paths else " (paths are not accepted here)"))


def list_drafts(*, session_storage=None, inbox: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every draft a ``from-draft`` can read: watcher sessions' workflows and inbox files."""
    out: List[Dict[str, Any]] = []
    storage = session_storage
    if storage is None:
        try:
            from anyuse.watcher.storage import SessionStorage
            storage = SessionStorage()
        except Exception:  # noqa: BLE001 - no session store is an empty list
            storage = None
    if storage is not None:
        for meta in storage.list_sessions():
            session = storage.load(meta["id"])
            if session is None:
                continue
            for wname in session.workflow_names:
                evs = session.get_events_for_workflow(wname)
                out.append({"id": f"{session.id}/{wname}", "source": "watcher_session",
                            "workflow": wname, "session_id": session.id,
                            "started_at": meta.get("started_at", ""),
                            "steps": sum(1 for e in evs if e.tag_type.value in ("STEP", "ACTION")),
                            "complete": any(e.tag_type.value == "WORKFLOW_END" for e in evs),
                            "apps": meta.get("apps_used", [])})
            for wname in getattr(session, "structured_workflow_names", []) or []:
                evs = [e for e in session.step_events if e.workflow_name == wname]
                start = next((e for e in evs if e.tag_type.value == "WORKFLOW_START"), None)
                out.append({"id": f"{session.id}/{wname}", "source": "watcher_session",
                            "structured": True, "workflow": wname, "session_id": session.id,
                            "started_at": meta.get("started_at", ""),
                            "steps": sum(1 for e in evs if e.tag_type.value == "STEP"),
                            "complete": any(e.tag_type.value == "WORKFLOW_END" for e in evs),
                            "apps": [str((start.data if start else {}).get("app") or "")]})
    box = os.path.realpath(inbox or drafts_inbox())
    for path in sorted(glob.glob(os.path.join(box, "*.json"))):
        if not os.path.realpath(path).startswith(box + os.sep):
            continue                                  # a link out of the inbox is not followed
        try:
            src = load_source(path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the others
            out.append({"id": os.path.basename(path), "source": "inbox", "error": str(exc)[:200]})
            continue
        out.append({"id": os.path.basename(path)[:-5], "source": "inbox", "workflow": src.name,
                    "kind": src.kind, "steps": len(src.steps), "complete": not src.partial,
                    "apps": src.apps})
    return out


# ---------------------------------------------------------------------------
# 2. Compile: steps, template, graph, gates, drift, rungs
# ---------------------------------------------------------------------------

def _planned(st: DraftStep, value: Optional[str], multi_app: bool = False) -> Dict[str, Any]:
    """A normalized step as a ``PlannedStep`` dict. A one-app step is exactly what it was before
    the runner contract; ``multi_app`` adds the step's ``app`` / ``bundle_id`` (and the selector's
    ``app``, the bundle id the executor compares the tree with)."""
    from anyuse.execution import ax_actions as AX
    sel = dict(st.selector or {})
    action = st.action
    if action == "enter_text" and st.input_mode:
        action = AX.MODE_ACTIONS.get(st.input_mode, action)
    cmd: Dict[str, Any] = {"role": st.raw_role, "title": sel.get("name", ""), "action": action}
    if action == "open_app":
        # ``app_name``, not ``app``: the executor reads a command's ``app`` as the bundle id the
        # target must be in, and an open_app has no target
        cmd.update(title=st.app or st.bundle_id, app_name=st.app, bundle_id=st.bundle_id)
    if sel:
        if multi_app and st.bundle_id:
            sel["app"] = st.bundle_id
        cmd["selector"] = sel
    if st.action == "press_key":
        cmd.update(keycode=st.keycode, flags=st.flags)
    if action == "press_shortcut":
        # a key goes to the app in front: the handler checks it is the recorded one (``bundle_id``)
        cmd.update(title=st.keys, keys=st.keys, keycode=st.keycode, flags=st.flags, bundle_id=st.bundle_id)
        if st.char:
            cmd["char"] = st.char
        if st.layout:
            cmd["layout"] = st.layout
        if st.expect:
            cmd["expect"] = dict(st.expect)
    if action == "set_toggle":
        cmd["target_state"] = st.target_state
    if action in ("append_text", "replace_line"):
        cmd["input_mode"] = st.input_mode
        if action == "replace_line":
            cmd["anchor"] = st.anchor
        expect = AX.expect_for(action, sel)
        if expect:
            cmd["expect"] = expect
    target = f"{st.raw_role or sel.get('canonical_role', '')} {sel.get('name', '')}".strip()
    if action == "open_app":
        target = f"app {st.app or st.bundle_id}"
    if action == "press_shortcut":
        target = f"shortcut {st.keys}"
    out = {"intent": st.intent or f"{action} {target}", "source": "workflow",
           "target_element": target, "modality": "ax", "command": cmd, "input_data": value,
           "needs_visual": False, "expected_after": "", "expected_after_state": None,
           "reasoning": ""}
    if multi_app:
        out.update(app=st.app, bundle_id=st.bundle_id)
    return out


def choice_domain(source: DraftSource, st: DraftStep) -> Optional[List[Dict[str, str]]]:
    """The options a chosen item can be bound among BY IDENTITY (execution/choice_identity.py):
    every element of the item's role in the recorded state before the step, with its title and AX identifier,
    when each carries a distinct non-empty identifier and the recorded item is one of them. None otherwise
    (the choice is bound by its title, as before): a fingerprint that kept no identifier field (the watcher's,
    an older guest harness's), two options with one identifier, an item without one."""
    ident = str((st.selector or {}).get("identifier") or "")
    fp = (source.states.get(st.state_id) or {}).get("fingerprint") or {} if st.state_id else {}
    opts = [{"title": str(el.get("title") or ""), "identifier": str(el.get("identifier") or "")}
            for el in fp.get("required_elements") or [] if el.get("role") == st.raw_role]
    ids = [o["identifier"] for o in opts]
    if not ident or len(opts) < 2 or "" in ids or len(set(ids)) != len(ids) or ident not in ids:
        return None
    return opts


def choice_identifier(step: Dict[str, Any], value: Any) -> Optional[str]:
    """The AX identifier a by-identity choice value names: an option's identifier, ``id:<identifier>``, or the
    option's RECORDED title (resolved against the recorded options, never the live titles). None: no option."""
    opts = (step.get("choice") or {}).get("options") or []
    v = str(value or "").strip()
    v = v[3:] if v.startswith("id:") else v
    hit = [o["identifier"] for o in opts if o["identifier"] == v] or \
        [o["identifier"] for o in opts if o["title"] == str(value or "").strip()]
    return hit[0] if len(hit) == 1 else None


def bind_steps(source: DraftSource, params: Sequence[DP.Parameter],
               candidates: Sequence[DP.Candidate]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(steps, recorded)``: the replayable steps with ``${name}`` at every place a parameter's
    candidate occurs, and the same steps with the recorded literals (kept for the proof). A
    choice binds the chosen item's label: a menu item's, a radio button's, a row's
    (``select_row``)."""
    by_c = {c.id: c for c in candidates}
    at: Dict[int, DP.Parameter] = {}
    anchor_at: Dict[int, DP.Parameter] = {}
    for p in params:
        for i in by_c[p.candidate].steps:
            (anchor_at if by_c[p.candidate].source == ANCHOR_SOURCE else at)[i] = p
    steps, recorded = [], []
    # a single-app desktop draft names its app in every
    # selector, so the executor checks the bundle id immediately before each action
    # (the replay's tree is bound to that one app; a step in another app is refused).
    app_key = (source.bundle_id if source.bundle_id and not source.is_web
               and len(set(a for a in source.apps if a)) <= 1 else "")
    for st in source.steps:
        if st.action in ("skip", "") or st.problem:
            continue
        rec = _planned(st, st.value, source.multi_app)
        if app_key and isinstance(rec["command"].get("selector"), dict):
            rec["command"]["selector"].setdefault("app", app_key)
        # the recorded copy keeps a text mode's start shape (the proof world's field starts from it)
        recorded.append(dict(rec, draft_index=st.index, **({"start": st.start} if st.start is not None else {})))
        step = copy.deepcopy(rec)
        p = at.get(st.index)
        dom = choice_domain(source, st) if p is not None and p.type == "choice" else None
        if dom is not None:
            # bound by the option's IDENTITY: the AX identifier the recorded state lists for it, so
            # another UI language or a reordered list does not move it; the value names the option by
            # its identifier or its recorded title (``choice_identifier``)
            step["command"]["selector"].update(name="", identifier="${%s}" % p.name, path="")
            step["command"]["title"] = ""
            step["target_element"] = f"{st.raw_role} #${{{p.name}}}"
            step["choice"] = {"param": p.name, "by": "identifier", "options": dom}
        elif p is not None and p.type == "choice":
            # a chosen item is found again by its label; the recorded item's own id/path would
            # pin the recorded option
            step["command"]["selector"].update(name="${%s}" % p.name, identifier="", path="")
            step["command"]["title"] = "${%s}" % p.name
            step["target_element"] = f"{st.raw_role} ${{{p.name}}}"
        elif p is not None:
            step["input_data"] = "${%s}" % p.name
        if st.index in anchor_at:
            # the line's anchor is a parameter of its own (the recorded one stays a vault marker)
            step["command"]["anchor"] = "${%s}" % anchor_at[st.index].name
        steps.append(dict(step, draft_index=st.index))
    return steps, recorded


def _template(source: DraftSource, steps: Sequence[Dict[str, Any]],
              params: Sequence[DP.Parameter], wid: str) -> Dict[str, Any]:
    from anyuse.memory.models import WorkflowIntent, WorkflowParam, WorkflowTemplate
    by_i = {s.index: s for s in source.steps}
    tpl = WorkflowTemplate.create_new(source.name, source.goal)
    tpl.id, tpl.required_app = wid, source.app
    tpl.parameters = [WorkflowParam(name=p.name, param_type="number" if p.type == "number" else "string",
                                    required=True, description=p.description) for p in params]
    tpl.steps = [WorkflowIntent(intent=s["intent"], target_element=s["target_element"],
                                expected_before_state=by_i[s["draft_index"]].state_id,
                                input_data=s["input_data"], input_type=s["command"]["action"])
                 for s in steps]
    tpl.state_path = [by_i[s["draft_index"]].state_id for s in steps if by_i[s["draft_index"]].state_id]
    return tpl.to_dict()


#: the executor's AX verbs as the classifier's own action names, to read a step's LABELS: the
#: classifier fails closed on a verb it does not know, so every AX step is "destructive" to it
_LABEL_VERB = {"press": "click", "toggle": "click", "set_toggle": "click", "confirm": "click", "cancel": "click",
               "enter_text": "type", "set_value": "type", "press_key": "key",
               "select_row": "click", "append_text": "type", "replace_line": "type",
               "open_app": "open_app", "press_shortcut": "key"}


def approval_plan(steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Which steps are held for a person before dispatch: everything the HIL classifier does not
    call read-only on the executor's own projection of the step (``HILGate(mode="all")``). That
    holds every write, submit, send and save, as the guest's ``ANYUSE_HIL_MODE = "all"`` profile
    does. ``class`` is what the step's labels say (a destructive keyword or not);
    ``executor_class`` is what the gate itself sees."""
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.hil_gate import StepClass, classify_step
    from anyuse.execution.plan_types import PlannedStep
    gated = []
    for i, s in enumerate(steps):
        action = StateGraphExecutor._step_to_hil_action(PlannedStep.from_dict(s))
        held = classify_step(action)
        if held == StepClass.READ_ONLY:
            continue
        by_label = classify_step({"action": _LABEL_VERB.get(action["action"], action["action"]),
                                  "args": action["args"]})
        gated.append({"index": i, "class": by_label, "executor_class": held,
                      "intent": s["intent"][:120], "action": s["command"].get("action")})
    return {"mode": HIL_MODE, "gated_steps": gated,
            "rule": "every step the HIL classifier does not call read-only is held before dispatch"}


def _gap_hint(st: DraftStep) -> Dict[str, str]:
    """What the recording still says about a gap's target, for the agent bridge: role, label and
    id when it kept them; a label still in the entity vault is left out."""
    sel = st.selector or {}
    name = str(sel.get("name") or "")
    ident = str(sel.get("identifier") or "")
    out = {"verb": st.verb,
           "role": str(sel.get("raw_role") or sel.get("canonical_role") or ""),
           "name": "" if VAULT_MARKER.search(name) else name[:120],
           "identifier": "" if VAULT_MARKER.search(ident) else ident[:120]}
    # a typing gap recorded with an input mode keeps it (the fill appends or replaces a line)
    if st.input_mode:
        out["mode"] = st.input_mode[:40]
    if st.anchor and not VAULT_MARKER.search(st.anchor):
        out["anchor"] = st.anchor[:200]
    return out


def _unreplayable(st: DraftStep, steps: Sequence[Dict[str, Any]], param: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"draft_index": st.index, "intent": st.intent[:120], "reason": st.problem}
    if st.gap:
        hint = _gap_hint(st)
        if param and st.action == "enter_text":
            hint["param"] = param        # the typing gap's literal is this parameter (the fill types it)
        # ``position``: how many replayable steps run before this gap
        out.update(fillable=True, gap=st.gap, sensitive=st.sensitive, state_before=st.state_id,
                   hint=hint,
                   intent_full=VAULT_MARKER.sub("[vault]", st.intent)[:200],
                   position=sum(1 for s in steps if s["draft_index"] < st.index))
    return out


#: the candidate source of a replace_line anchor the watcher kept as a typed value
ANCHOR_SOURCE = "anchor"


def _anchor_parameters(source: DraftSource, params: Sequence[DP.Parameter],
                       candidates: Sequence[DP.Candidate]) -> Tuple[List[DP.Parameter], List[DP.Candidate]]:
    """A replace_line step whose anchor is a vault marker (the watcher keeps a line's anchor as
    a typed value, never as text): the anchor becomes a text parameter of its own
    (``anker_<n>``, the marker its example), given at run time like any typed value and bound by
    the proof like one. The record holds the marker and ``${anker_<n>}``, never the text."""
    params, candidates = list(params), list(candidates)
    have = {c.id for c in candidates}
    names = {p.name for p in params}
    n = 0
    for st in source.steps:
        if st.problem or st.input_mode != "replace_line" or not VAULT_MARKER.fullmatch(st.anchor or ""):
            continue
        n += 1
        cid = f"anchor{n}"
        if cid in have:
            continue
        name, k = f"anker_{n}", n
        while name in names:
            k += 1
            name = f"anker_{k}"
        names.add(name)
        sel = st.selector or {}
        candidates.append(DP.Candidate(id=cid, source=ANCHOR_SOURCE, value=st.anchor, steps=[st.index],
                                       field_info={"role": str(sel.get("canonical_role") or ""), "title": "",
                                                   "identifier": ""}))
        params.append(DP.Parameter(name=name, candidate=cid, type="text", chosen_by="anchor",
                                   description="a text that exactly one line of the field contains (the "
                                               "line this step replaces)"))
    return params, candidates


def _resolve_content(source: DraftSource, params: Sequence[DP.Parameter],
                     candidates: Sequence[DP.Candidate]) -> None:
    """A list row's label and its list's identifier / description that the watcher keeps as
    ``content_label`` markers (presence-tier). The list's identity is resolved here, on this
    machine, by a deref under the presence-gated grant (each read asks, audited): the replay
    finds the list by it. A row's label is never resolved: like a typed value it is a choice
    parameter's example (the record names ``${name}`` and keeps the marker), and a row label no
    parameter takes is refused (the replay would look for a row named by a vault reference).
    A list identity that does not resolve (no vault, presence not given, another machine)
    makes the step a ``no_identity`` gap; its markers stay."""
    marked = [st for st in source.steps if st.action == "select_row" and not st.problem
              and _CONTENT_MARKER.search(json.dumps(st.selector or {}, ensure_ascii=False))]
    if not marked:
        return
    by_c = {c.id: c for c in candidates}
    chosen = {i for p in params if p.type == "choice" and p.candidate in by_c for i in by_c[p.candidate].steps}
    resolve = None
    for st in marked:
        if st.index not in chosen and _CONTENT_MARKER.fullmatch(str((st.selector or {}).get("name") or "")):
            st.problem = ("the row is named by its label, which the watcher keeps in the entity vault: make "
                          "the label a choice parameter (its value is given at run time)")
            continue
        sel = copy.deepcopy(st.selector)
        box = sel.get("container") or {}
        need = [k for k in ("identifier", "description") if _CONTENT_MARKER.fullmatch(box.get(k) or "")]
        if need and resolve is None:
            from anyuse.watcher import step_capture as SC
            from anyuse.watcher.entity_vault import get_default_vault
            resolve = SC.vault_content_resolver(get_default_vault()) or (lambda m: None)
        got = {k: resolve(box[k]) for k in need}
        if any(v is None for v in got.values()):
            st.problem = ("the row's list is named by content in the entity vault: it is read only on the "
                          "machine that recorded it, with presence, and was not released here (compile it "
                          "there, or export it with --release-content)")
            st.gap = "no_identity"
            continue
        box.update(got)
        st.selector = sel


def compile_record(source: DraftSource, params: Sequence[DP.Parameter],
                   candidates: Sequence[DP.Candidate], wid: str = "") -> Dict[str, Any]:
    """The draft-workflow record, without proof and status (``draft_to_workflow`` adds both)."""
    from anyuse.guides.state_graph import StateGraph, UIState
    _resolve_content(source, params, candidates)
    params, candidates = _anchor_parameters(source, params, candidates)
    wid = wid or hashlib.sha256((source.digest + DP.parameters_digest(params, candidates)).encode()
                                ).hexdigest()[:8]
    steps, recorded = bind_steps(source, params, candidates)
    graph = StateGraph()
    for sid, sd in source.states.items():
        graph.add_state(UIState.from_dict(sd))
    by_c = {c.id: c for c in candidates}
    # a typing gap whose literal is a parameter: its hint names the parameter the fill types
    typed_at = {i: p.name for p in params if p.type != "choice" and p.candidate in by_c for i in by_c[p.candidate].steps}
    unreplayable = [_unreplayable(s, steps, typed_at.get(s.index)) for s in source.steps if s.problem]
    baseline = sum(1 for s in source.steps if s.state_id and not s.problem and s.action != "skip")
    rec = {
        "format": FORMAT, "compiler": record_compiler(steps), "workflow": source.name, "workflow_id": wid,
        "kind": "web" if source.is_web else "desktop", "source_kind": source.kind,
        "app": source.app, "bundle_id": source.bundle_id, "goal": source.goal,
        "parameters": [dict(p.to_dict(), example=by_c[p.candidate].value,
                            occurrences=list(by_c[p.candidate].steps),
                            **({"anchor": True} if by_c[p.candidate].source == ANCHOR_SOURCE else {}))
                       for p in params],
        "candidates": [c.to_dict() for c in candidates],
        "steps": steps, "recorded": recorded,
        "workflow_template": _template(source, steps, params, wid),
        "state_graph": graph.to_dict(),
        "approvals": approval_plan(steps),
        "drift_profile": {"gate": "on", "settings": drift_profile_settings(),
                          "settings_note": ("the settings in force when compiled; replay applies the replaying "
                                            "machine's own settings"),
                          "baseline_steps": baseline, "steps": len(steps),
                          "note": ("steps without a recorded fingerprint are checked by the recorded "
                                   "identity only; the drift gate has no baseline for them")
                          if baseline < len(steps) else ""},
        "unreplayable": unreplayable,
        "shortcuts": shortcut_notes(source.steps),
        "rungs": [{"rung": "ax", "status": "compiled" if steps else "empty",
                   "steps": len(steps)}],
        "attribution": dict(source.attribution, draft_ref=source.ref, draft_kind=source.kind,
                            draft_digest=source.digest, partial=source.partial),
    }
    if source.multi_app:
        rec["apps"] = record_apps(steps, source.app, source.bundle_id)
    rec["verdict_keys"] = {"draft_digest": source.digest,
                           "parameters_digest": DP.parameters_digest(params, candidates),
                           "artifact_digest": artifact_digest(rec)}
    return rec


def shortcut_notes(steps: Sequence[DraftStep]) -> List[Dict[str, Any]]:
    """The compile report's line per shortcut compiled as the chord itself (``SHORTCUT_VIA``)."""
    out = []
    for s in steps:
        if s.action != "press_shortcut" or s.problem:
            continue
        checks = [f"the {k} count goes {'up' if v == 'more' else 'down'}" for k, v in (s.expect or {}).items()]
        checks.append("the next step's recorded screen (its drift gate)")
        out.append({"draft_index": s.index, "keys": s.keys, "expect": dict(s.expect or {}),
                    "note": (f"{SHORTCUT_VIA} ({s.keys}): no menu item was found for it when it was recorded; the "
                             f"replay sends it to {s.app or s.bundle_id} in front and checks "
                             + " and ".join(checks))})
    return out


def record_compiler(steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The lowest compiler version that runs these steps (``COMPILER`` is the newest)."""
    from anyuse.execution import ax_actions as AX
    return {"name": COMPILER["name"], "version": AX.record_version(steps)}


def record_apps(steps: Sequence[Dict[str, Any]], app: str = "", bundle_id: str = "") -> List[Dict[str, str]]:
    """The apps of a multi-app record, the starting app first: ``[{app, bundle_id}]``."""
    out: List[Dict[str, str]] = []
    for a, b in [(app, bundle_id)] + [(str(s.get("app") or ""), str(s.get("bundle_id") or ""))
                                      for s in steps]:
        row = {"app": a, "bundle_id": b}
        if (a or b) and row not in out:
            out.append(row)
    return out


def is_multi_app(rec: Dict[str, Any]) -> bool:
    """Do the record's steps carry their own apps (compiled from a multi-app draft)?"""
    return any(isinstance(s, dict) and ("app" in s or "bundle_id" in s) for s in rec.get("steps") or [])


def artifact_digest(rec: Dict[str, Any]) -> str:
    """Stable over saves and runs: no ids, names, timestamps or who chose a parameter."""
    tpl = {k: v for k, v in (rec.get("workflow_template") or {}).items()
           if k not in ("id", "name", "created_at", "updated_at")}
    params = [{k: p.get(k) for k in ("name", "type", "candidate", "example", "occurrences")}
              for p in rec.get("parameters") or []]
    return _digest({"format": rec.get("format"), "compiler": rec.get("compiler"),
                    "steps": rec.get("steps"), "parameters": params, "template": tpl,
                    "state_graph": rec.get("state_graph"), "approvals": rec.get("approvals")})


def _api_rung_note(source: DraftSource, rec: Dict[str, Any]) -> Dict[str, Any]:
    destructive = [g for g in rec["approvals"]["gated_steps"] if g["class"] == "destructive"]
    if destructive:
        why = ("a step's label is destructive (submit / send / confirm / delete ...); the api rung "
               "sends GET/HEAD only, so this flow stays on the UI path")
    else:
        why = ("no network recording was given; the api rung needs one recording plus at least two "
               "of other input values (`anyuse attach` sessions, a HAR, or a recording directory)")
    return {"rung": "api", "status": "not_applicable", "reason": why}


# ---------------------------------------------------------------------------
# 3. The runner (proof and live replay share it)
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\$\{([^{}\s]{1,40})\}")


def bind_values(value: Any, values: Dict[str, Any]) -> Any:
    """Replace every string that is exactly ``${name}`` (parameter names may be German) by its
    value; ``desktop_compiled_tools._bind``'s pattern is ASCII-only."""
    if isinstance(value, str):
        m = _PLACEHOLDER.fullmatch(value)
        return values[m.group(1)] if m and m.group(1) in values else value
    if isinstance(value, list):
        return [bind_values(v, values) for v in value]
    if isinstance(value, dict):
        return {k: bind_values(v, values) for k, v in value.items()}
    return value


def bind_step(step: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """One record step with its values bound where ``bind_steps`` puts placeholders, and only
    there: ``input_data`` (typed), the selector's name, the command's title and the target
    (choice); the selector's identifier only on a step ``bind_steps`` marked as a by-identity
    choice (``choice``), with the identifier its value names. A recorded literal elsewhere (a
    role, an id, a path) is never a placeholder."""
    out = copy.deepcopy({k: v for k, v in step.items() if k not in ("draft_index", "choice")})
    out["input_data"] = bind_values(out.get("input_data"), values)
    out["target_element"] = bind_values(out.get("target_element"), values)
    cmd = out.get("command") or {}
    if "title" in cmd:
        cmd["title"] = bind_values(cmd["title"], values)
    if "anchor" in cmd:
        cmd["anchor"] = bind_values(cmd["anchor"], values)     # a replace_line anchor parameter
    if isinstance(cmd.get("selector"), dict):
        cmd["selector"]["name"] = bind_values(cmd["selector"].get("name"), values)
        ch = step.get("choice") or {}
        if (ch.get("by") == "identifier" and ch.get("param") in values
                and cmd["selector"].get("identifier") == "${%s}" % ch["param"]):
            # a by-identity choice step (``bind_steps`` marked it): ITS placeholder takes the identifier
            # the value names; a recorded identifier is never a placeholder anywhere else. A value that
            # names no option keeps the placeholder (fail closed): with name and identifier both empty
            # the lookup would take the first element of the role, a wrong-target press
            cmd["selector"]["identifier"] = (choice_identifier(step, values[ch["param"]])
                                             or cmd["selector"]["identifier"])
    return out


def bound_steps(record: Dict[str, Any], values: Dict[str, Any]) -> List[Any]:
    from anyuse.execution.plan_types import PlannedStep
    return [PlannedStep.from_dict(bind_step(s, values)) for s in record.get("steps") or []]


class NoVision:
    """The executor's visual resolver for draft-compiled workflows: a step whose screen drifted
    from the recording is never handed to a vision model (no screenshot, no call, no spend);
    the run stops there and the workflow is re-recorded."""

    async def resolve(self, step, ax_json):  # noqa: ARG002 - the resolver interface
        return {"ok": False, "llm_calls": 0,
                "error": ("the screen drifted from the recording; a draft-compiled workflow does "
                          "not fall back to a vision model (re-record it)")}


def checked_executor(ax_tree):
    """The executor every draft-compiled run uses: the drift gate on, no vision fallback."""
    from anyuse.execution.executor import StateGraphExecutor
    return StateGraphExecutor(ax_tree=ax_tree, drift_gate_enabled=True, visual_resolver=NoVision())


#: control characters a value may not carry: typed by the keystroke rung, a tab moves the focus
#: and a return submits - an un-gated key inside a text step. A line break is let through only
#: into a multi-line text area.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def value_problems(record: Dict[str, Any], values: Dict[str, Any]) -> List[str]:
    """New values a run refuses before acting: unknown or missing names, wrong types, control
    characters (a line break only into a text area), a choice that names an irreversible option
    (the guest guard's list)."""
    from anyuse.execution.desktop_e2e import irreversible_reason
    params = {p["name"]: p for p in record.get("parameters") or []}
    field_role = {c.get("id"): (c.get("field") or {}).get("role", "")
                  for c in record.get("candidates") or [] if isinstance(c, dict)}
    # the input modes a parameter is typed with: a replace_line value is one line
    action_at = {s.get("draft_index"): (s.get("command") or {}).get("action")
                 for s in record.get("steps") or [] if isinstance(s, dict)}
    probs = [f"unknown parameter {k!r}" for k in values if k not in params]
    for name, p in params.items():
        if name not in values:
            probs.append(f"missing parameter {name!r}")
            continue
        v = values[name]
        if not isinstance(v, str) or v == "":
            probs.append(f"{name}: an empty or non-text value")
            continue
        if VAULT_REF.search(v) or "${vault:" in v:
            probs.append(f"{name}: a value may not name a vault credential (only a recorded password step does)")
            continue
        if p.get("anchor") and _anchor_problem(v):
            probs.append(f"{name}: {_anchor_problem(v)}")
            continue
        multiline = p["type"] == "text" and field_role.get(p.get("candidate")) == "textarea"
        if _CONTROL.search(v) or ("\n" in v and not multiline):
            probs.append(f"{name}: a control character (a tab or a return would move the focus or "
                         "submit; a line break goes only into a multi-line text area)")
            continue
        if "\n" in v and any(action_at.get(i) == "replace_line" for i in p.get("occurrences") or []):
            probs.append(f"{name}: a line break in a value that replaces one line (replace_line)")
            continue
        why = DP.type_problem(p["type"], v, "chosen" if p["type"] == "choice" else "typed")
        if why:
            probs.append(f"{name}: {why}")
        by_id = [s for s in record.get("steps") or [] if (s.get("choice") or {}).get("param") == name]
        if by_id:
            ident = choice_identifier(by_id[0], v)
            opt = next((o for o in by_id[0]["choice"]["options"] if o["identifier"] == ident), None)
            if opt is None:
                probs.append(f"{name}: {v!r} names no recorded option (by identifier or recorded title)")
                continue
            v = opt["title"]
        if p["type"] == "choice" and irreversible_reason(v):
            probs.append(f"{name}: the option {v!r} is irreversible ({irreversible_reason(v)})")
    return probs


Approver = Callable[[Dict[str, Any]], Awaitable[bool]]

#: per-workflow approval levels (``anyuse workflow approvals``). ``auto`` (the default):
#: a replay started WITH its values holds no step - starting it is the approval.
#: Prompts belong to the learning phase only (the fill and the proof hold every step). A replay
#: that deviates from what was recorded and proved still stops: the identity check refuses
#: another target, the drift gate (no vision fallback) stops on an unexpected screen or dialog,
#: ``value_problems`` and the typed-text readback refuse a value mismatch. ``all`` and
#: ``destructive`` are opt-in holds; ``off`` is the same as ``auto``.
APPROVAL_LEVELS = ("auto", "all", "destructive", "off")
APPROVAL_DEFAULT = "auto"


def approval_level(rec: Dict[str, Any]) -> str:
    level = str((rec.get("approval") or {}).get("level") or APPROVAL_DEFAULT)
    return level if level in APPROVAL_LEVELS else "all"


def effective_hil_mode(rec: Dict[str, Any]) -> str:
    """The HIL mode a live replay of ``rec`` runs under: ``auto`` holds nothing."""
    level = approval_level(rec)
    return "off" if level == "auto" else level


def workflow_hil_mode(workflow: Any) -> str:
    """The same rule for any saved workflow: a
    ``WorkflowTemplate``'s own ``approval`` record, read by ``approval_level``.
    A non-draft replay started with its values therefore holds nothing unless
    its workflow opted in (``anyuse workflow approvals NAME --level ...``)."""
    appr = workflow.get("approval") if isinstance(workflow, dict) else getattr(workflow, "approval", None)
    return effective_hil_mode({"approval": appr if isinstance(appr, dict) else {}})


def gate_keywords(hil_mode: str) -> Tuple[str, ...]:
    """The destructive keywords the gate matches under ``hil_mode``."""
    from anyuse.execution.hil_gate import DESTRUCTIVE_KEYWORDS_ALL
    return DESTRUCTIVE_KEYWORDS_ALL + (EXTRA_HOLD_KEYWORDS if hil_mode == "destructive" else ())


def _gate_action(step, hil_mode: str, first_app: str = "") -> Dict[str, Any]:
    """The step as the approval gate sees it. Under ``all`` it is the executor's projection
    (every AX verb is unknown to the classifier, so every AX step is held). Under
    ``destructive`` the verb is mapped to the classifier's own name (``_LABEL_VERB``), so a
    step is held when its LABELS are destructive (send, delete, save ...), not merely because
    the classifier does not know the verb ``press``. An ``open_app`` shows its bundle id, and
    under ``destructive`` one that leaves the record's first app (``first_app``) is held."""
    from anyuse.execution.executor import StateGraphExecutor
    action = StateGraphExecutor._step_to_hil_action(step)
    cmd = step.command or {}
    sel = cmd.get("selector") or {}
    action["args"]["target"] = " ".join(str(x) for x in (sel.get("canonical_role") or cmd.get("role"),
                                                         sel.get("name"), sel.get("identifier")) if x)
    if cmd.get("action") == "select_row":
        # the step may undo its own selection (ax_actions.select_row): the person sees that
        action["args"]["target"] += " (may deselect the row again if the selection grows)"
    if cmd.get("action") == "open_app":
        bid = str(cmd.get("bundle_id") or "")
        action["args"]["target"] = f"app {cmd.get('app_name') or bid} ({bid})"
        action["args"]["bundle_id"] = bid
        if hil_mode == "destructive" and bid != first_app:
            action["args"]["hold"] = f"confirm (a switch to another app: {bid})"
    if cmd.get("keycode") is not None:
        action["args"]["keycode"] = f"{cmd.get('keycode')} flags {cmd.get('flags') or 0}"
    if hil_mode == "destructive":
        action["action"] = _LABEL_VERB.get(action["action"], action["action"])
        # held as a confirmation: a key or AXConfirm presses whatever the default button is (a
        # sheet's "Löschen", a chat's send), and a dialog's OK / Yes confirms what it asked
        if cmd.get("action") in ("press_key", "confirm"):
            action["args"]["hold"] = "confirm (the default button)"
        elif cmd.get("action") == "press_shortcut":
            # a chord has no label the classifier could read: what it does is the app's
            action["args"]["hold"] = f"confirm (the keyboard shortcut {cmd.get('keys') or ''}: {SHORTCUT_VIA})"
        elif str(sel.get("name") or cmd.get("title") or "").strip().lower() in ("ok", "okay", "ja", "yes"):
            action["args"]["hold"] = "confirm (a dialog's answer)"
    return action


#: under ``destructive``, these labels are held too (the classifier matches substrings): what
#: commits an order, a message, a transfer or a sign-up without one of its own keywords
EXTRA_HOLD_KEYWORDS = (
    "bestellen", "kaufen", "abschicken", "absenden", "überweisen", "ueberweisen", "kündigen",
    "kuendigen", "veröffentlichen", "veroeffentlichen", "teilen", "unterschreiben", "abonnieren",
    "zahlungspflichtig", "kostenpflichtig", "place order", "publish", "share", "subscribe",
    "buchen", "reservieren",
    # a dialog's destructive answers: replace a file, close without saving, throw changes away
    "ersetzen", "replace", "nicht sichern", "don't save", "don’t save", "verwerfen", "discard",
)


#: a password typed from the credential vault (``credentials/vault.py``): ``${vault:<alias>}`` in
#: a RECORD step's ``input_data``. The approval shows the reference; only after it is the alias
#: resolved, into a copy of the step that is dispatched and dropped. No record, log or prompt
#: holds the value. A parameter VALUE never carries a reference (``value_problems``).
VAULT_REF = re.compile(r"\$\{vault:([A-Za-z0-9._@+-]{1,80})\}")
_PASSWORD_ROLES = ("passwordfield",)
_PASSWORD_RAW = ("AXSecureTextField",)


def vault_secret(alias: str) -> Optional[str]:
    from anyuse.credentials.vault import CredentialVault
    return CredentialVault().get_credential(alias)


def vault_target_problem(command: Optional[Dict[str, Any]]) -> str:
    """Why this command's target may not receive a vault credential ("" when it may): the
    RECORDED selector must name a password field."""
    sel = (command or {}).get("selector") or {}
    # ``secure``: the fill recorded a web password input (an AXTextField with the secure subrole)
    if (sel.get("canonical_role") in _PASSWORD_ROLES or sel.get("raw_role") in _PASSWORD_RAW
            or sel.get("secure") is True):
        return ""
    return "a vault credential is typed only into a password field"


def is_vault_step(record_step: Dict[str, Any]) -> bool:
    """An UNBOUND record step that types a vault credential into a recorded password field."""
    data = record_step.get("input_data")
    return (isinstance(data, str) and bool(VAULT_REF.fullmatch(data))
            and not vault_target_problem(record_step.get("command")))


class _Suppression:
    """The watcher's typing-suppression flag (``paths.typing_suppression_flag``), raised for one
    credential and removed only if it still holds this call's own token."""

    def __init__(self):
        import secrets
        from anyuse.paths import typing_suppression_flag
        self.path = typing_suppression_flag()
        self.token = f"{datetime.now().isoformat()} {secrets.token_hex(8)}"

    def raise_(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.token)
            return True
        except OSError:
            return False

    def drop(self) -> None:
        try:
            if self.path.read_text() == self.token:
                self.path.unlink()
        except OSError:
            pass


async def dispatch_step(i: Any, step, *, gate, executor, decide_as_driving: bool, hil_mode: str,
                        template=None, graph=None, label: str = "",
                        secret_resolver: Optional[Callable[[str], Optional[str]]] = None,
                        vault_step: bool = False, first_app: str = ""
                        ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """Hold, then dispatch one bound step. Returns ``(log entry, hold entry, stop reason)``;
    the drift gate is evaluated only when ``template`` and ``graph`` are given (a step the
    agent bridge filled has no recorded state to compare with). A vault reference is resolved
    only when ``vault_step`` (the caller saw it in the UNBOUND record step), the target is a
    password field and ``secret_resolver`` is given; any other reference fails the step."""
    from anyuse.execution import ax_actions as AX
    from anyuse.execution.hil_gate import ApprovalRequired
    # a contract step this executor does not run is refused before it is held or dispatched: an
    # executor before the contract would turn the action into a press (ax_actions)
    unsupported = AX.runner_refusal(step, executor)
    if unsupported is not None:
        return ({"index": i, "intent": step.intent, "success": False, "error": unsupported["error"],
                 "refused": True, "blocked": None, "method": "ax", "drift_fired": False,
                 "drift_note": None, "llm_calls": 0, "reason_code": unsupported["reason_code"]},
                None, f"step {i}: {unsupported['error']}")
    action = _gate_action(step, hil_mode, first_app)
    if label:
        action["args"]["intent"] = f"{label} {action['args'].get('intent') or step.intent}"[:300]
    keywords = gate_keywords(hil_mode)
    try:
        decision = await gate.require_approval(action, drive_enabled=decide_as_driving, keywords=keywords)
    except ApprovalRequired as exc:
        return None, None, f"step {i}: held, no approver ({exc.step_class})"
    hold = None
    if decision.step_class and gate._holds(decision.step_class):
        hold = {"index": i, "approved": decision.actuate, "class": decision.step_class,
                "reason": decision.reason}
    if not decision.actuate:
        return None, hold, f"step {i}: not approved ({decision.reason})"
    ref = VAULT_REF.search(step.input_data) if isinstance(step.input_data, str) else None
    suppress = None
    if ref is not None:
        why = ("a vault reference outside a recorded vault step" if not vault_step
               or not VAULT_REF.fullmatch(step.input_data) else vault_target_problem(step.command)
               or ("" if secret_resolver is not None else "no credential resolver for this run"))
        # the watcher drops input capture while this flag exists (credentials/vault.py does the
        # same); a credential is never typed while capture could be on
        suppress = _Suppression() if not why else None
        if suppress is not None and not suppress.raise_():
            why, suppress = "the watcher's typing-suppression flag could not be written", None
        secret = secret_resolver(ref.group(1)) if not why else None
        if not why and not secret:
            why = f"no credential {ref.group(1)!r} in the credential vault"
        if why:
            if suppress is not None:
                suppress.drop()
            return ({"index": i, "intent": step.intent, "success": False, "error": why, "refused": True,
                     "blocked": None, "method": "ax", "drift_fired": False, "drift_note": None,
                     "llm_calls": 0}, hold, f"step {i}: {why}")
        step = copy.copy(step)
        step.input_data = secret
    try:
        res, ver = await _execute(executor, step, i, template, graph)
    finally:
        if suppress is not None:
            suppress.drop()
    drift = (ver or {}).get("drift_gate") or {}
    entry = {"index": i, "intent": step.intent, "success": bool(res.get("success")),
             "error": res.get("error"), "refused": res.get("refused"),
             "blocked": res.get("blocked") or res.get("gated"), "method": res.get("method"),
             "drift_fired": bool(drift.get("fired")), "drift_note": drift.get("note"),
             "llm_calls": int(res.get("llm_calls", 0) or 0)}
    # a typed outcome (outcome.refusal, ax_actions): the fill stops on a STOP kind or an action
    # that acted and failed, instead of asking the model again
    for k in ("reason_code", "outcome_kind", "acted"):
        if res.get(k) is not None:
            entry[k] = res[k]
    # a text mode's start (``ax_actions._edit``: line count and the anchor's lines; no text, no
    # anchor): the fill records it for the proof
    if isinstance(res.get("start"), dict):
        entry["start"] = res["start"]
    # the step's own evidence: an app switch's polls (AX focus and workspace per poll, ``ax_actions.open_app``), a
    # state-aware toggle's states (``ax_actions.set_toggle``)
    for k in ("activation", "front", "front_source", "pressed", "state_before", "state_after", "target_state"):
        if res.get(k) is not None:
            entry[k] = res[k]
    return entry, hold, None if res.get("success") else f"step {i}: {res.get('error') or 'failed'}"


async def _execute(executor, step, i, template, graph):
    if template is not None and graph is not None:
        return await executor.execute_checked_step(step, step_idx=i, workflow=template, state_graph=graph)
    return await executor.execute_checked_step(step)


class AppRouter:
    """The executor each step runs on.

    A one-app record (no step carries an app): always the given executor, as before. A multi-app
    record: a step runs on its own app's tree, ``tree_factory(bundle id)`` (the CLI finds the one
    running app of that bundle id; nothing is launched here), with one checked executor per app
    built once by ``executor_factory``; a step that names its app only by its localized name is
    refused (a name is no identity: ``pgrep -x`` misses a localized one). An ``open_app`` runs on
    the app in front before it, only to an app of the record's own ``apps`` that the restriction
    policy (``core/restriction.py``) allows, and after it succeeded the opened app is in front. A
    tree whose ``bundle_id()`` is not the step's recorded bundle id refuses the step
    (``wrong_app``), so the drift gate and the identity check of each step run against its own
    app. All of it before the step is held or sent."""

    def __init__(self, record: Dict[str, Any], executor, *, tree_factory=None, executor_factory=None,
                 default_tree=None, restriction=None):
        self.multi = is_multi_app(record)
        self.default = executor
        self.tree_factory = tree_factory
        self.executor_factory = executor_factory or checked_executor
        # the caller's policy, else the configured one (ANYUSE_APP_BLOCKLIST / _ALLOWLIST), read
        # when an app switch is checked
        self._policy = restriction
        self.apps = {str(a.get("bundle_id") or "") for a in record.get("apps") or [] if isinstance(a, dict)}
        self.apps.discard("")
        start = (str(record.get("app") or ""), str(record.get("bundle_id") or ""))
        self.front = _app_key(*start)
        self.names: Dict[str, Tuple[str, str]] = {self.front: start}
        self.slots: Dict[str, Tuple[Any, Any]] = {}
        if executor is not None:
            self.slots[self.front] = (default_tree, executor)

    def _slot(self, name: str, bid: str) -> Tuple[Optional[Tuple[Any, Any]], Optional[Dict[str, Any]]]:
        from anyuse.execution import ax_actions as AX
        key = _app_key(name, bid)
        self.names.setdefault(key, (name, bid))
        if key not in self.slots:
            if not bid:
                return None, AX.refusal(f"the step names its app only by the name {name!r}; a multi-app "
                                        "replay finds an app by its bundle id", code="identity")
            tree = self.tree_factory(BundleId(bid)) if self.tree_factory is not None else None
            if tree is None:
                return None, AX.refusal(f"no AX tree for {name or bid!r} ({bid}; start the app first; nothing "
                                        "here launches one)", code="no_tree")
            self.slots[key] = (tree, self.executor_factory(tree))
        tree, ex = self.slots[key]
        getter = getattr(tree, "bundle_id", None) if tree is not None else None
        if bid and getter is not None:
            live = getter()
            if live != bid:
                return None, AX.refusal(f"wrong app: the AX tree for {name or bid!r} is "
                                        f"{live or 'not bound to an app'}, the step was recorded in {bid}",
                                        code="wrong_app")
        return (tree, ex), None

    def for_step(self, rec_step: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """``(executor, None)`` or ``(None, refusal)`` for one UNBOUND record step."""
        if not self.multi:
            return self.default, None
        cmd = rec_step.get("command") or {}
        if cmd.get("action") == "open_app":
            why = self.switch_problem(cmd)
            if why is not None:
                return None, why
            slot, why = self._slot(*self.names.get(self.front, ("", "")))
        else:
            slot, why = self._slot(str(rec_step.get("app") or ""), str(rec_step.get("bundle_id") or ""))
        return (None, why) if why is not None else (slot[1], None)

    @property
    def policy(self):
        if self._policy is None:
            from anyuse.core.restriction import RestrictionPolicy
            self._policy = RestrictionPolicy.from_settings()
        return self._policy

    def switch_problem(self, cmd: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Why this ``open_app`` may not run (None when it may): its app is not one of the
        record's own, or the restriction policy refuses it (by name or by bundle id)."""
        from anyuse.execution import ax_actions as AX
        bid, name = str(cmd.get("bundle_id") or ""), str(cmd.get("app_name") or "")
        if bid not in self.apps:
            return AX.refusal(f"the app switch names {bid or 'no bundle id'}, which is not one of the recording's "
                              f"apps ({', '.join(sorted(self.apps)) or 'none'})", code="wrong_app")
        allowed, reason = self.policy.is_app_target_allowed(bid, name)
        if not allowed:
            return AX.refusal(f"the app switch to {bid} is refused by the restriction policy: {reason}",
                              code="restricted")
        return None

    def front_slot(self) -> Tuple[Any, Any]:
        """``(tree, executor)`` of the app in front (what a gap is filled on), or (None, None)."""
        if not self.multi:
            return None, self.default
        slot, _why = self._slot(*self.names.get(self.front, ("", "")))
        return slot if slot is not None else (None, None)

    def tree_of(self, executor) -> Any:
        """The tree ``executor`` (one of this router's) acts on, for reads around a step."""
        for tree, ex in self.slots.values():
            if ex is executor and tree is not None:
                return tree
        return getattr(executor, "_ax_tree", None)

    def front_app(self) -> Tuple[str, str]:
        return self.names.get(self.front, ("", ""))

    def done(self, rec_step: Dict[str, Any]) -> None:
        """A step succeeded: an ``open_app`` brought its app to the front; any other step ran in
        its own app, which is in front now."""
        if not self.multi:
            return
        cmd = rec_step.get("command") or {}
        name, bid = ((str(cmd.get("app_name") or ""), str(cmd.get("bundle_id") or "")) if cmd.get("action") == "open_app"
                     else (str(rec_step.get("app") or ""), str(rec_step.get("bundle_id") or "")))
        if name or bid:
            self.front = _app_key(name, bid)
            self.names.setdefault(self.front, (name, bid))


def trigger_problem(record_steps: Sequence[Dict[str, Any]], i: int) -> Optional[Dict[str, Any]]:
    """A refusal when record step ``i`` targets the field a trigger focused (selector kind
    ``focused`` with its ``trigger``) and the step right before it is not that trigger (nor a
    step the fill made of it): the focus it relies on would come from another step."""
    from anyuse.execution import ax_actions as AX
    sel = ((record_steps[i] if i < len(record_steps) else {}).get("command") or {}).get("selector")
    if not (isinstance(sel, dict) and sel.get("kind") == "focused" and "trigger" in sel):
        return None
    prev = record_steps[i - 1] if i > 0 else None
    if prev is not None and sel["trigger"] in (prev.get("draft_index"), prev.get("gap_index")):
        return None
    return AX.refusal("the step before this one is not the trigger whose effect focused its field",
                      code="identity")


#: how long the focus of a focused step may take to settle into a text field: reads, and seconds between them
FOCUS_SETTLE_READS, FOCUS_SETTLE_S = 8, 0.25


def _settled_focus(tree: Any, getter: Callable[[], Any], sleep: Optional[Callable[[float], None]] = None) -> Any:
    """The focused element once the app has moved the focus into a text role:
    Finder focuses its toolbar search field a moment after Ablage > Suchen, so a read right away sees the application
    itself, and the Return after the typing would be refused as "the focus moved". Up to ``FOCUS_SETTLE_READS`` reads;
    the last one stands (resolve_focused then judges it)."""
    import time
    from anyuse.execution import ax_actions as AX
    sleep = sleep or time.sleep
    el = getter()
    for _ in range(FOCUS_SETTLE_READS - 1):
        try:
            sel = tree.to_selector(el) if el is not None and hasattr(tree, "to_selector") else None
        except Exception:  # noqa: BLE001 - an element that cannot be described: read again
            sel = None
        if sel is not None and (getattr(sel, "canonical_role", "") in AX.FOCUS_ROLES
                                or AX.canonical(getattr(sel, "raw_role", "") or "") in AX.FOCUS_ROLES):
            return el
        sleep(FOCUS_SETTLE_S)
        el = getter()
    return el


def prepare_focus(steps: Sequence[Any], i: int, tree: Any) -> None:
    """Right before bound step ``i``: when step ``i + 1`` targets the focused field, read the
    focus context on ``i``'s tree (the element focused now, the trigger target's window) into
    that step's selector (``_focus_ctx``, in memory only): ``ax_actions.resolve_focused`` then
    requires the focus to have moved, into the trigger's window.

    When step ``i`` is itself a ``focused`` step (``trigger_kind: "focused"``, or a filled
    focused step right before another), the element focused now is the one step ``i`` resolves
    to; the context then names it and its window (``same: True``), and step ``i + 1`` must find
    the focus still there."""
    if i + 1 >= len(steps) or tree is None:
        return
    nxt = (steps[i + 1].command or {}).get("selector")
    if not isinstance(nxt, dict) or nxt.get("kind") != "focused":
        return
    trigger = (steps[i].command or {}).get("selector")
    if nxt.get("trigger_kind") == "focused" or (isinstance(trigger, dict) and trigger.get("kind") == "focused"):
        getter, window_of = getattr(tree, "focused_element", None), getattr(tree, "window_of", None)
        try:
            el = _settled_focus(tree, getter) if getter is not None else None
            win = window_of(el) if el is not None and window_of is not None else None
        except Exception:  # noqa: BLE001 - an unreadable focus: resolve_focused refuses the step
            el, win = None, None
        ident = None
        if el is not None and hasattr(tree, "to_selector"):
            try:
                # the focused field's identity too: an app that re-creates the field it keeps the focus in (Finder's
                # toolbar search field once a search runs) hands back another element reference of the SAME field
                ident = tree.to_selector(el).to_dict()
            except Exception:  # noqa: BLE001 - no identity read: the reference check alone, as before
                ident = None
        nxt["_focus_ctx"] = {"focused": el, "window": win, "same": True, "identity": ident}
        return
    reader = getattr(tree, "focus_context", None)
    if reader is None:
        return
    try:
        nxt["_focus_ctx"] = reader(trigger if isinstance(trigger, dict) else None)
    except Exception:  # noqa: BLE001 - an unreadable focus: the check runs without it
        pass


def _refused_entry(i: Any, step, why: Dict[str, Any]) -> Dict[str, Any]:
    return {"index": i, "intent": getattr(step, "intent", ""), "success": False, "error": why.get("error"),
            "refused": True, "blocked": None, "method": "ax", "drift_fired": False, "drift_note": None,
            "llm_calls": 0, "reason_code": why.get("reason_code")}


async def run_steps(record: Dict[str, Any], values: Dict[str, Any], *, executor, approver: Approver,
                    decide_as_driving: bool, on_step: Optional[Callable[[int], None]] = None,
                    after_step: Optional[Callable[[int, Dict[str, Any]], Awaitable[None]]] = None,
                    hil_mode: str = HIL_MODE,
                    secret_resolver: Optional[Callable[[str], Optional[str]]] = None,
                    tree_factory: Optional[Callable[[str], Any]] = None,
                    executor_factory: Optional[Callable[[Any], Any]] = None,
                    default_tree: Any = None, restriction=None) -> Dict[str, Any]:
    """Bind, hold, dispatch - in that order, one step at a time; stop at the first failure.

    ``decide_as_driving`` is what the approval gate is told about the master gate: the live
    replay passes the real drive gate (it has already refused when that is off); the proof
    passes True so that every gated step's question reaches its (recording) approver.
    ``hil_mode`` is ``all`` for the proof and the first replays (``effective_hil_mode``).
    ``secret_resolver``: see ``dispatch_step`` (the proof passes one that keeps the reference).
    ``tree_factory`` / ``executor_factory`` / ``default_tree``: a multi-app record's other apps
    (``AppRouter``); ``executor`` runs the record's first app. A one-app record ignores them."""
    from anyuse.execution.hil_gate import HILGate
    from anyuse.guides.state_graph import StateGraph
    from anyuse.memory.models import WorkflowTemplate
    probs = value_problems(record, values)
    if probs:
        return {"ok": False, "stopped": "refused (parameter check): " + "; ".join(probs)[:400],
                "steps": [], "holds": []}
    steps = bound_steps(record, values)
    template = WorkflowTemplate.from_dict(record.get("workflow_template") or {})
    graph = StateGraph.from_dict(record.get("state_graph") or {})
    # an explicit mode: the global ANYUSE_HIL_MODE never lowers a draft workflow's holds
    gate = HILGate(mode=hil_mode, approver=approver)
    router = AppRouter(record, executor, tree_factory=tree_factory, executor_factory=executor_factory,
                       default_tree=default_tree, restriction=restriction)
    log: List[Dict[str, Any]] = []
    holds: List[Dict[str, Any]] = []
    for i, step in enumerate(steps):
        rec_step = (record.get("steps") or [])[i]
        if on_step is not None:
            on_step(i)
        ex, why = router.for_step(rec_step)
        why = why or trigger_problem(record.get("steps") or [], i)
        if why is not None:
            log.append(_refused_entry(i, step, why))
            return {"ok": False, "stopped": f"step {i}: {why['error']}", "steps": log, "holds": holds}
        prepare_focus(steps, i, router.tree_of(ex))
        # every step is put to the gate, never only the indices a saved record lists; the
        # person sees the target and any key
        # whether this step types a credential is read from the UNBOUND record step: its
        # reference and its password-field target, never from a run's value
        entry, hold, stop = await dispatch_step(i, step, gate=gate, executor=ex,
                                                decide_as_driving=decide_as_driving,
                                                hil_mode=hil_mode, template=template, graph=graph,
                                                secret_resolver=secret_resolver,
                                                vault_step=is_vault_step(rec_step),
                                                first_app=str(record.get("bundle_id") or ""))
        if hold is not None:
            holds.append(hold)
        if entry is not None:
            log.append(entry)
            if after_step is not None:
                await after_step(i, entry)
        if stop is not None:
            return {"ok": False, "stopped": stop, "steps": log, "holds": holds}
        router.done(rec_step)
    return {"ok": True, "stopped": None, "steps": log, "holds": holds}


# ---------------------------------------------------------------------------
# 4. The whole seam: draft -> parameters -> record -> proof -> status -> save
# ---------------------------------------------------------------------------

def choose_parameters(source: DraftSource, candidates: Sequence[DP.Candidate], *,
                      parameters: Optional[Sequence[Dict[str, Any]]] = None,
                      overrides: Sequence[Dict[str, str]] = (), fixed: Sequence[str] = (),
                      propose: bool = False, llm=None) -> Dict[str, Any]:
    """Confirmed parameters (no model) > overrides (no model unless ``propose``) > the model
    (only when ``propose``). An override wins over the model on the same candidate or name.
    References of the form ``@c2#4`` split a candidate by step first (``split_candidates``);
    ``candidates`` in the answer is the list after the split."""
    refs = [str(o.get("ref") or o.get("candidate") or "") for o in overrides] + [str(f) for f in fixed]
    refs += [str(p.get("candidate") or "") for p in (parameters or []) if isinstance(p, dict)]
    split, split_problems = DP.split_candidates(candidates, refs)
    if parameters is not None:
        data = {"parameters": [{k: p.get(k, "") for k in ("candidate", "name", "type", "description")}
                               for p in parameters]}
        params, problems = DP.validate_proposal(data, split, chosen_by="confirmed")
        return {"parameters": params if not split_problems else [], "problems": split_problems + problems,
                "how": "confirmed", "proposal": None, "candidates": split}
    proposal = None
    proposed: List[DP.Parameter] = []
    notes: List[str] = []
    if propose:
        proposal = DP.propose(source.goal, candidates, llm=llm)
        proposed = [DP.Parameter.from_dict(p, "model") for p in proposal["parameters"]]
        if proposal["problems"] and not overrides:
            return {"parameters": [], "problems": ["the model's answer was rejected: "
                                                   + "; ".join(proposal["problems"])[:600]],
                    "how": "model", "proposal": proposal, "candidates": split}
        left = {c.id for c in split}
        gone = [p for p in proposed if p.candidate not in left]
        if gone:
            notes.append("the model's choice of " + ", ".join(sorted({p.candidate for p in gone}))
                         + " is not used: every step of it was split off by an override")
        proposed = [p for p in proposed if p.candidate in left]
    if overrides or fixed:
        rejected = bool(proposal and proposal["problems"])
        params, problems = DP.apply_overrides(split, [] if rejected else proposed, overrides, fixed)
        if rejected:
            notes.append("the model's answer was rejected and is not used: "
                         + "; ".join(proposal["problems"])[:400])
        return {"parameters": params if not split_problems else [], "problems": split_problems + problems,
                "notes": notes, "how": "override+model" if propose else "override", "proposal": proposal,
                "candidates": split}
    return {"parameters": proposed, "problems": [], "notes": notes, "how": "model" if propose else "none",
            "proposal": proposal, "candidates": split}


def _web_rung(source: DraftSource, api_executor=None, permission_checker=None) -> Dict[str, Any]:
    """The unchanged web compile on a recording directory: extract, then prove on fresh input
    (``api_verify.compile_and_verify``) behind the same permission preflight the CLI uses."""
    from anyuse.execution.api_verify import compile_and_verify, permission_preflight
    web = source.web or {}

    def read(paths):
        out = []
        for p in paths:
            with open(p, encoding="utf-8") as fh:
                out.append(json.load(fh))
        return out
    recording = read([web["rec"]])[0]
    if permission_checker is None:
        from anyuse.execution.api_permission import check_permission as permission_checker

    def preflight(compiled):
        return permission_preflight(compiled, recording, checker=permission_checker)
    compiled, proof = compile_and_verify(recording, read(web.get("fresh") or []), executor=api_executor,
                                         preflight=preflight, fitting_recordings=read(web.get("fit") or []),
                                         selection_recordings=read(web.get("sel") or []))
    param = recording.get("param") or {}
    return {"recording": recording, "compiled": compiled, "proof": proof,
            "param": {"name": str(param.get("name") or "q"), "value": str(param.get("value") or "")}}


async def draft_to_workflow(ref: Any, *, parameters=None, overrides: Sequence[Dict[str, str]] = (),
                            fixed: Sequence[str] = (), propose: bool = False, llm=None,
                            fresh: Optional[Sequence[Dict[str, str]]] = None, name: str = "",
                            save: bool = True, storage=None, session_storage=None,
                            inbox: Optional[str] = None, web_name: str = "", allow_paths: bool = True,
                            verdict_check: Optional[Callable[[Dict[str, str]], Optional[str]]] = None,
                            api_executor=None, permission_checker=None, verdicts=None) -> Dict[str, Any]:
    """The seam end to end. Returns a report; with ``save`` the workflow is written as
    ``ready`` (its fresh-input proof passed) or ``draft`` (with the reasons)."""
    from anyuse.execution import draft_verdicts as DV
    from anyuse.execution.draft_schema import MAX_NAME
    if name and (not isinstance(name, str) or len(name) > MAX_NAME or re.search(r"[\x00-\x1f\x7f]", name)):
        raise MalformedDraft(f"a workflow name is at most {MAX_NAME} characters with no control character")
    source = load_source(ref, session_storage=session_storage, inbox=inbox, web_name=web_name,
                         allow_paths=allow_paths)
    if name:
        source.name = name
    store = verdicts if verdicts is not None else DV.default_store()
    if source.kind == "web_recording":
        return await _web_to_workflow(source, save=save, storage=storage, verdict_check=verdict_check,
                                      api_executor=api_executor, permission_checker=permission_checker,
                                      store=store)
    from anyuse.execution import draft_proof
    candidates = DP.collect_candidates(source.steps)
    choice = choose_parameters(source, candidates, parameters=parameters, overrides=overrides,
                               fixed=fixed, propose=propose, llm=llm)
    params = choice["parameters"]
    candidates = choice["candidates"]
    rec = compile_record(source, params, candidates)
    rec["rungs"].append(_api_rung_note(source, rec))
    rec["parameter_choice"] = {"how": choice["how"], "problems": choice["problems"],
                               "notes": choice.get("notes", []),
                               "proposal": _proposal_summary(choice.get("proposal"))}
    proof = await draft_proof.prove(rec, candidates, fresh=fresh)
    rec["proof"] = proof
    rec["verdict"] = await DV.check_compile(rec, proof, store)
    _settle_status(rec, source, choice, proof, verdict_check)
    report = {"workflow": rec["workflow"], "status": rec["status"], "reasons": rec["status_reasons"],
              "parameters": rec["parameters"], "candidates": rec["candidates"],
              "parameter_choice": rec["parameter_choice"], "proof": proof, "verdict": rec["verdict"],
              "approvals": rec["approvals"], "unreplayable": rec["unreplayable"],
              "shortcuts": rec["shortcuts"],
              "rungs": rec["rungs"], "drift_profile": rec["drift_profile"],
              "attribution": rec["attribution"], "saved": None}
    if save:
        report["saved"] = save_workflow(rec, storage=storage)
        report["status"], report["reasons"] = rec["status"], rec["status_reasons"]
    return report


def _proposal_summary(proposal: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not proposal:
        return None
    return {k: proposal.get(k) for k in ("parameters", "problems", "model", "estimate", "note", "cost_usd",
                                         "cost_basis", "budget")}


def _settle_status(rec, source, choice, proof, verdict_check) -> None:
    """``ready``, ``needs_fill`` (every reason is a gap the agent bridge may fill, and the proof
    failed only because of them) or ``draft``."""
    reasons: List[str] = []
    gap_reasons: List[str] = []
    if source.partial:
        reasons.append("the draft is partial (no WORKFLOW_END was recorded)")
    for u in rec["unreplayable"]:
        (gap_reasons if u.get("fillable") else reasons).append(
            f"draft step {u['draft_index']} cannot be replayed: {u['reason']}"
            + ((" (a sensitive gap: `anyuse workflow fill` needs the sensitive switch)" if u.get("sensitive")
                else " (a gap the agent bridge can fill: `anyuse workflow fill`)") if u.get("fillable") else ""))
    if not rec["steps"]:
        (gap_reasons if gap_reasons else reasons).append("no replayable step")
    reasons += choice["problems"]
    for s in rec["steps"]:
        if isinstance(s.get("input_data"), str) and VAULT_MARKER.fullmatch(s["input_data"]):
            reasons.append(f"draft step {s.get('draft_index')} would type a vault reference, not its "
                           "text: make that typed value a parameter (the recorded text stays in the "
                           "entity vault)")
    if not rec["parameters"] and not proof.get("fixed_routine"):
        reasons.append("no parameter: a fresh-input proof needs at least one "
                       "(choose with --param / overrides, or ask the model with --propose)")
    if not proof.get("passed"):
        why = (("routine replay not passed: " if proof.get("fixed_routine") else "fresh-input proof not passed: ")
               + str(proof.get("reason") or "")[:300])
        # with gaps the proof stops before any case; it runs again after the fill
        (gap_reasons if gap_reasons and not proof.get("cases") else reasons).append(why)
    if verdict_check is not None:
        why = verdict_check(dict(rec["verdict_keys"]))
        if why:
            reasons.append(f"a verdict marked this compile wrong: {why}")
    v = rec.get("verdict") or {}
    if v.get("source") == "verdict store" and v.get("verdict") != "agrees":
        reasons.append(v.get("reason") or "a stored verdict disagrees with this compile")
    rec["status"] = "draft" if reasons else ("needs_fill" if gap_reasons else "ready")
    rec["status_reasons"] = reasons + gap_reasons


async def _web_to_workflow(source: DraftSource, *, save, storage, verdict_check, api_executor,
                           permission_checker, store=None) -> Dict[str, Any]:
    web = _web_rung(source, api_executor=api_executor, permission_checker=permission_checker)
    compiled, proof = web["compiled"], web["proof"]
    wid = hashlib.sha256(source.digest.encode()).hexdigest()[:8]
    rec = {"format": FORMAT, "compiler": record_compiler([]), "workflow": source.name, "workflow_id": wid,
           "kind": "web", "source_kind": source.kind, "app": source.app, "bundle_id": "",
           "goal": source.goal,
           "parameters": [{"name": web["param"]["name"], "candidate": "", "type": "text",
                           "description": "the value the recorded request carries", "chosen_by": "web_compile",
                           "example": web["param"]["value"], "occurrences": []}],
           "candidates": [], "steps": [], "recorded": [], "workflow_template": {}, "state_graph": {},
           "approvals": {"mode": HIL_MODE, "gated_steps": [],
                         "rule": "the api rung sends GET/HEAD only; a write is refused by the api executor"},
           "drift_profile": {"gate": "n/a (api rung)"}, "unreplayable": [],
           "rungs": [{"rung": "api", "status": proof.effective_status if compiled.template else "failed",
                      "reason": compiled.reason or proof.reason, "file": None},
                     {"rung": "ax", "status": "absent", "reason": "a network recording has no UI steps"}],
           "attribution": dict(source.attribution, draft_ref=source.ref, draft_kind=source.kind,
                               draft_digest=source.digest, partial=False),
           "proof": {"passed": bool(proof.passed), "harness": "api_verify.compile_and_verify",
                     "reason": proof.reason,
                     "cases": [{"values": {web["param"]["name"]: c.value}, "passed": bool(c.passed),
                                "problems": [] if c.passed else [c.reason]} for c in proof.cases],
                     "fresh_inputs": len(proof.cases),
                     "passed_cases": proof.passed_count, "status": proof.effective_status,
                     "scope": proof.scope},
           "parameter_choice": {"how": "web_compile", "problems": [], "notes": [], "proposal": None}}
    rec["verdict_keys"] = {"draft_digest": source.digest, "parameters_digest": _digest(rec["parameters"]),
                           "artifact_digest": _digest(compiled.template.to_dict() if compiled.template else {})}
    reasons = []
    if compiled.template is None:
        reasons.append(f"extraction failed: {compiled.reason}")
    elif not proof.passed:
        reasons.append(f"fresh-input proof not passed: {proof.reason}")
    if verdict_check is not None:
        why = verdict_check(dict(rec["verdict_keys"]))
        if why:
            reasons.append(f"a verdict marked this compile wrong: {why}")
    from anyuse.execution import draft_verdicts as DV
    rec["verdict"] = await DV.check_compile(rec, rec["proof"], store)
    if rec["verdict"].get("source") == "verdict store" and rec["verdict"].get("verdict") != "agrees":
        reasons.append(rec["verdict"].get("reason") or "a stored verdict disagrees with this compile")
    rec["status"], rec["status_reasons"] = ("draft" if reasons else "ready"), reasons
    report = {"workflow": rec["workflow"], "status": rec["status"], "reasons": reasons,
              "verdict": rec["verdict"],
              "parameters": rec["parameters"], "candidates": [], "parameter_choice": rec["parameter_choice"],
              "proof": rec["proof"], "approvals": rec["approvals"], "unreplayable": [],
              "rungs": rec["rungs"], "drift_profile": rec["drift_profile"],
              "attribution": rec["attribution"], "saved": None}
    if save:
        saved = save_workflow(rec, storage=storage, web=web)
        report.update(saved=saved, status=rec["status"], reasons=rec["status_reasons"],
                      rungs=rec["rungs"])
    return report


# ---------------------------------------------------------------------------
# 5. Storage
# ---------------------------------------------------------------------------

def _storage(storage=None):
    if storage is not None:
        return storage
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage()


def load_record(storage, workflow) -> Optional[Dict[str, Any]]:
    path = os.path.join(str(storage.get_workflow_dir(workflow.id)), RECORD_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_workflow(rec: Dict[str, Any], *, storage=None, web: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    """Write the ``Workflow`` entry and ``draft_workflow.json``. A workflow of the same name that
    is ``ready`` (or was not compiled from a draft) is never replaced: the new compile becomes a
    shadow draft beside it, to be promoted after a shadow run."""
    from anyuse.memory.models import VerificationIssue, Workflow
    storage = _storage(storage)
    name = rec["workflow"]
    existing = storage.get(name)
    if existing is not None:
        old = load_record(storage, existing)
        if old is None or old.get("status") == "ready":
            rec["shadow_of"] = {"workflow_id": existing.id,
                                "compiler": (old or {}).get("compiler") or "not a draft compile"}
            rec["status"] = "draft"
            rec.setdefault("status_reasons", []).append(
                f"shadow: a working workflow {name!r} exists; this compile is kept beside it and is "
                "promoted only after a shadow run confirms it (never an automatic upgrade)")
            existing = None
    wf = Workflow.create_new(name, rec.get("goal") or "")
    if existing is not None:                     # a draft of the same name: replaced in place
        wf.id, wf.created_at = existing.id, existing.created_at
    elif "shadow_of" in rec:
        name = wf.name = f"{name}.shadow-{wf.id}"
    rec["workflow"], rec["workflow_id"] = name, wf.id
    if rec.get("workflow_template"):
        rec["workflow_template"]["id"], rec["workflow_template"]["name"] = wf.id, name
    wf.updated_at = datetime.now().isoformat()
    wf.variables = [p["name"] for p in rec.get("parameters") or []]
    wf.tags = ["from-draft", rec.get("kind") or ""] + ([rec["app"].lower().replace(" ", "_")]
                                                        if rec.get("app") else [])
    wf.verified = rec["status"] == "ready"
    wf.verify_after_play = False
    wf.last_verification = datetime.now().isoformat() if wf.verified else ""
    wf.verification_issues = [VerificationIssue(step_index=-1, issue_type="not_proven",
                                                description=r[:300], severity="error")
                              for r in rec.get("status_reasons") or []]
    storage.save(wf)
    rec["compiled_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    out = {"workflow_id": wf.id, "name": name, "status": rec["status"],
           "dir": str(storage.get_workflow_dir(wf.id))}
    from anyuse.execution.api_compile import COMPILED_FILE
    stale = os.path.join(out["dir"], COMPILED_FILE)
    if rec["status"] != "ready" and os.path.isfile(stale):
        os.remove(stale)                         # a draft never leaves a callable rung behind
    if (web is not None and rec["status"] == "ready" and web["compiled"].template is not None
            and web["proof"].passed):
        from anyuse.execution.api_compile import compiled_record, persist_compiled
        api = compiled_record(web["compiled"].template, web["proof"].to_dict(), workflow=name,
                              chrome_filtered=list(web["compiled"].chrome_dropped),
                              source={"recording": os.path.basename((rec["attribution"].get("draft_ref") or "")),
                                      "origin": "draft_compile"})
        out["compiled_api"] = persist_compiled(storage, wf.id, api)
        rec["rungs"][0]["file"] = "compiled_api.json"
    path = os.path.join(out["dir"], RECORD_FILE)
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    out["record"] = path
    return out


def play_refusal(storage, workflow) -> Optional[str]:
    """Why ``Agent.play`` must not run this workflow (it has no values for its parameters and
    its legacy step list is empty on purpose), or None for any other workflow."""
    if workflow is None or storage is None:
        return None
    try:
        path = os.path.join(str(storage.get_workflow_dir(workflow.id)), RECORD_FILE)
    except Exception:  # noqa: BLE001 - a storage without directories holds no draft compiles
        return None
    if not os.path.isfile(path):
        return None
    return (f"'{workflow.name}' was compiled from a recorded draft; run it with "
            f"`anyuse workflow replay {workflow.name} --param NAME=VALUE ...` (it needs its "
            "parameters, the drive gate, the foreground interlock and its approvals: "
            "`anyuse workflow approvals NAME`)")


def find_saved(name: str, storage=None) -> Tuple[Any, Optional[Dict[str, Any]]]:
    storage = _storage(storage)
    wf = storage.get(name) or storage.get_by_id(name)
    return wf, (load_record(storage, wf) if wf is not None else None)


# ---------------------------------------------------------------------------
# 6. The live replay (refuses on the host)
# ---------------------------------------------------------------------------

def record_version_problem(rec: Dict[str, Any]) -> Optional[str]:
    """Why this AnyUse must not run a saved record: an unknown format, another compiler, or a
    NEWER compiler version than this one (its steps may mean something this runner does not do)."""
    if rec.get("format") != FORMAT:
        return (f"the record's format {str(rec.get('format'))[:60]!r} is not {FORMAT!r}; this AnyUse "
                "cannot run it")
    comp = rec.get("compiler") if isinstance(rec.get("compiler"), dict) else {}
    ver = comp.get("version")
    if comp.get("name") != COMPILER["name"] or isinstance(ver, bool) or not isinstance(ver, int):
        return f"the record was made by an unknown compiler ({str(comp)[:80]}); recompile it with this AnyUse"
    if ver > COMPILER["version"]:
        return (f"the record was made by {COMPILER['name']} version {ver}; this AnyUse runs version "
                f"{COMPILER['version']}. Update AnyUse (or recompile the draft here) before replaying")
    return None


def start_tree(rec: Dict[str, Any], ax_tree=None, tree_factory=None) -> Tuple[Any, str]:
    """``(tree, "")`` for the app a run starts in, or ``(None, why)``: the given tree, else
    ``tree_factory`` by the record's bundle id; a one-app record without one (or whose app the
    bundle id does not find) by the app's name, as before. A localized name is no process name
    (on a German system "Schach" is the process "Chess", so ``pgrep -x Schach`` finds nothing); a
    multi-app record needs its bundle id."""
    if ax_tree is not None or tree_factory is None:
        return ax_tree, ("" if ax_tree is not None else
                         f"no AX tree for {rec.get('app')!r} (start the app first; nothing here launches one)")
    if is_multi_app(rec):
        bid = str(rec.get("bundle_id") or "")
        if not bid:
            return None, (f"the record names its first app only by the name {rec.get('app')!r}; a multi-app "
                          "replay finds an app by its bundle id")
        tree = tree_factory(BundleId(bid))
        return tree, ("" if tree is not None else
                      f"no AX tree for {rec.get('app')!r} ({bid}; start the app first; nothing here launches one)")
    bid = str(rec.get("bundle_id") or "")
    tree = tree_factory(BundleId(bid)) if bid else None
    if tree is None and rec.get("app"):
        tree = tree_factory(rec.get("app") or "")
    where = f" ({bid})" if bid else ""
    return tree, ("" if tree is not None else
                  f"no AX tree for {rec.get('app')!r}{where} (start the app first; nothing here launches one)")


def live_preflight() -> Dict[str, Any]:
    """The drive gate and the foreground interlock, read before anything is built."""
    from anyuse.execution.actions import drive_enabled
    if not drive_enabled():
        return {"drive_enabled": False, "may_drive": False,
                "interlock": {"allowed": False, "reason": "not probed: the drive gate is off"}}
    from anyuse.execution.foreground_interlock import check
    verdict = check("draft workflow replay")
    return {"drive_enabled": True, "interlock": verdict.to_dict(),
            "may_drive": bool(verdict.allowed)}


async def replay_saved(name: str, values: Dict[str, str], *, approver: Approver, ax_tree=None,
                       storage=None, preflight=None, executor=None,
                       on_step: Optional[Callable[[int], None]] = None,
                       tree_factory: Optional[Callable[[str], Any]] = None,
                       after_step: Optional[Callable[[int, Dict[str, Any]], Awaitable[None]]] = None,
                       verdicts=None, supervised: bool = False,
                       executor_factory: Optional[Callable[[Any], Any]] = None,
                       restriction=None) -> Dict[str, Any]:
    """Run a saved ``ready`` workflow with new values through the checked executor.

    Refuses before anything is built when the drive gate is off, the interlock refuses, the
    workflow is not ``ready``, a value fails its check, or a step needs a runner action this
    executor does not run yet (``ax_actions.record_runner_problem``). ``ax_tree`` is the target
    app's ``AccessibilityTree`` (the guest harness passes it; nothing here launches an app); a
    multi-app record's other apps come from ``tree_factory`` (one ``executor_factory`` executor
    per app, ``checked_executor`` by default).
    ``supervised``: the approver is a person (the CLI's typed-back code); a successful run under
    ``all`` is counted in the record (``approval.supervised_runs``); a scripted approver never is."""
    storage = _storage(storage)
    wf, rec = find_saved(name, storage)
    if wf is None or rec is None:
        return {"ok": False, "stopped": f"no draft-compiled workflow named {name!r}"}
    why = record_version_problem(rec)
    if why:
        return {"ok": False, "stopped": f"refused: {why}"}
    if rec.get("status") != "ready":
        return {"ok": False, "stopped": "refused: the workflow is a draft ("
                + "; ".join(rec.get("status_reasons") or [])[:300] + ")"}
    from anyuse.execution import draft_verdicts as DV
    known = DV.replay_refusal(rec, values, verdicts if verdicts is not None else DV.default_store())
    if known:
        return {"ok": False, "stopped": f"refused: {known['why']}", "known_wrong": known}
    if not rec.get("steps"):
        return {"ok": False, "stopped": "refused: no UI steps (an api-rung workflow runs with "
                                        "`anyuse workflow play NAME --param ...`)"}
    from anyuse.execution import ax_actions as AX
    missing = AX.record_runner_problem(rec, AX.capability_probe(executor, executor_factory))
    if missing:
        return {"ok": False, "stopped": f"refused: {missing}", "runner": AX.record_needs(rec)}
    pre = (preflight or live_preflight)()
    if not pre.get("may_drive"):
        why = ("ANYUSE_DRIVE_ENABLED is off" if not pre.get("drive_enabled") else
               "the foreground interlock refuses: " + str((pre.get("interlock") or {}).get("reason")))
        return {"ok": False, "stopped": f"refused before anything was sent: {why}", "preflight": pre}
    probs = value_problems(rec, values)
    if probs:
        return {"ok": False, "stopped": "refused (parameter check): " + "; ".join(probs)[:400],
                "preflight": pre}
    if executor is None:
        ax_tree, why = start_tree(rec, ax_tree, tree_factory)
        if ax_tree is None:
            return {"ok": False, "stopped": f"refused: {why}"}
        executor = (executor_factory or checked_executor)(ax_tree)
    mode = effective_hil_mode(rec)
    out = await run_steps(rec, values, executor=executor, approver=approver,
                          decide_as_driving=bool(pre.get("drive_enabled")), on_step=on_step,
                          after_step=after_step, hil_mode=mode, secret_resolver=vault_secret,
                          tree_factory=tree_factory, executor_factory=executor_factory,
                          default_tree=ax_tree, restriction=restriction)
    out["preflight"] = pre
    out["workflow"] = rec["workflow"]
    out["approval"] = {"level": approval_level(rec), "mode": mode}
    if out.get("ok") and supervised and mode == HIL_MODE:
        # read again: a level set while the run waited on its approvals wins, and a record
        # recompiled meanwhile (another artifact) is not counted
        _wf, fresh = find_saved(name, storage)
        if (fresh is not None and approval_level(fresh) == approval_level(rec)
                and (fresh.get("verdict_keys") or {}).get("artifact_digest")
                == (rec.get("verdict_keys") or {}).get("artifact_digest")):
            appr = dict(fresh.get("approval") or {})
            appr["supervised_runs"] = int(appr.get("supervised_runs") or 0) + 1
            appr["last_supervised_run"] = datetime.now().astimezone().isoformat(timespec="seconds")
            fresh["approval"] = appr
            write_record(storage, wf, fresh)
            out["approval"]["supervised_runs"] = appr["supervised_runs"]
    return out


_MODE_RANK = {"off": 0, "destructive": 1, HIL_MODE: 2}


def lowers_holds(rec: Dict[str, Any], level: str) -> bool:
    """Would ``level`` hold fewer steps than the workflow holds now? (The CLI asks for the
    typed-back code then: holds are lowered by a person, never through a pipe.)"""
    new = dict(rec, approval=dict(rec.get("approval") or {}, level=level))
    return _MODE_RANK.get(effective_hil_mode(new), 0) < _MODE_RANK.get(effective_hil_mode(rec), 0)


def write_record(storage, wf, rec: Dict[str, Any], *, keep_previous: str = "") -> str:
    """Rewrite ``draft_workflow.json`` atomically; ``keep_previous`` names a copy of the old one."""
    d = str(storage.get_workflow_dir(wf.id))
    path = os.path.join(d, RECORD_FILE)
    if keep_previous and os.path.isfile(path):
        with open(path, "rb") as src, open(os.path.join(d, keep_previous), "wb") as dst:
            dst.write(src.read())
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def set_approval_level(name: str, level: str, *, storage=None) -> Dict[str, Any]:
    """Set a saved draft workflow's approval level (the CLI asks for the typed code before
    ``off``). Returns ``{workflow, level, mode}``."""
    if level not in APPROVAL_LEVELS:
        raise ValueError(f"approval level must be one of {', '.join(APPROVAL_LEVELS)}")
    storage = _storage(storage)
    wf, rec = find_saved(name, storage)
    if wf is not None and rec is None:
        # any saved workflow with a replayable template takes the same levels
        tpl = storage.get_template(wf.id) if hasattr(storage, "get_template") else None
        if tpl is None:
            raise ValueError(f"no replayable workflow named {name!r}")
        appr = dict(getattr(tpl, "approval", None) or {})
        appr.update(level=level, set_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        tpl.approval = appr
        storage.save_template(wf.id, tpl)
        return {"workflow": wf.name, "level": level, "mode": workflow_hil_mode(tpl),
                "supervised_runs": int(appr.get("supervised_runs") or 0)}
    if wf is None or rec is None:
        raise ValueError(f"no draft-compiled workflow named {name!r}")
    appr = dict(rec.get("approval") or {})
    appr.update(level=level, set_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    rec["approval"] = appr
    write_record(storage, wf, rec)
    return {"workflow": rec["workflow"], "level": level, "mode": effective_hil_mode(rec),
            "supervised_runs": int(appr.get("supervised_runs") or 0)}


def run_sync(coro):
    """Run a coroutine from synchronous code (the CLI)."""
    return asyncio.run(coro)
