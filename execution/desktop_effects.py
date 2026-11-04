"""Effect probe: map a discovered menu item to a contract class by the state
delta it causes, never by its title.

``desktop_contracts.map_contracts`` maps by structure alone: a
shortcut convention plus a container role. Two structural fits were wrong in
the guest: Finder's ⌘N opens a *window* (it is not "create object"), and runs
of checkmarked menu items that are *checkbox* groups were mapped as option
(radio) groups. Both are visible in the first second after a harmless press.
So this probe presses each candidate once, on disposable guest documents, and
reads what changed:

    before snapshot ── press (identity-resolved, enabled) ── after snapshot
                                   │
                          classify(before, after)  ->  effects
                                   │
                         map_from_probes  ->  contract candidates

A snapshot is a plain dict the guest fills (``execution/guest/desktop_gen2.py``):

    {"windows":   [{"role", "subrole", "identifier", "document"}],
     "sheets":    int,                       # AXSheet children of all windows
     "focused":   {"role", "subrole", "identifier"} | None,
     "containers": {key: {"rows": int}},     # AXRows (all rows, not the viewport)
     "model":     {"kind": "folder", "items": [...]} | {"kind": "file", "sha": str} | None,
     "text":      str | None,                # digest of the front document's text areas
     "selection": str | None,                # digest of selected rows / text ranges
     "marks":     {"<index path>": bool} | None,  # the pressed item's top-level menu, read open
     "enabled":   bool}                      # the pressed item's AXEnabled at the probe start

Everything here is pure (no pyobjc, no subprocess) and runs under the guarded
test runner. The class a candidate gets depends only on the observed effect,
the element's role/subrole and its enabled state. The one place positions
appear is ``marks``: an index path keys a *reading* inside one snapshot pair,
never a trigger (triggers are resolved by identifier and shortcut).
"""
from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import desktop_contracts as C
from . import desktop_followup as F
from . import desktop_preconditions as P

# ---------------------------------------------------------------------------
# 1. Which items are probed
# ---------------------------------------------------------------------------

#: Key equivalents whose platform meaning is quit / close / hide / minimise /
#: print (``q w h m p`` with Command). Pressing them ends or hides the probe.
CONVENTION_CHARS = frozenset("qwhmp")
#: Virtual keys by platform convention: delete (51), forward delete (117),
#: escape (53, force quit in the Apple menu), tab (48, window cycling).
CONVENTION_VKEYS = frozenset({51, 117, 53, 48})
CONVENTION_CMD_CHARS = frozenset({"\x08", "\x7f", "⎋", "\t"})
#: A mark run is probed member by member, at most this many members.
RUN_PROBES = 6
#: AXMenuItemCmdModifiers bit 8 = "no Command key".
NO_COMMAND = 8
#: bit 16: the globe (fn) key. Globe key equivalents are SYSTEM functions the
#: OS adds to every app's menus (dictation, the emoji palette, full screen,
#: window tiling), not the app's own commands; dictation can hand audio to a
#: server. The probe never presses them.
GLOBE = 16


def _mods_int(it: Dict[str, Any]) -> int:
    m = it.get("cmd_mods")
    try:
        return int(m or 0)
    except (TypeError, ValueError):
        return 0


def identity_string(it: Dict[str, Any]) -> str:
    """The same identity string the guest's mark reader writes for an item
    outside the group (``identifier|cmd|mods``)."""
    return f"{it.get('identifier') or ''}|{(it.get('cmd_char') or '').lower()}|{_mods_int(it)}"


def convention_excluded(it: Dict[str, Any]) -> Optional[str]:
    """Why an item is never pressed by a probe, from its menu-bar position
    class (the application menu) or its key equivalent's platform meaning.
    Never from its title."""
    ip = list(it.get("index_path") or [])
    if ip and ip[0] == 1:
        return "application menu (quit, hide, settings, services by platform convention)"
    ch = it.get("cmd_char") or ""
    vk = it.get("cmd_virtual_key")
    mods = _mods_int(it)
    if ch.lower() in CONVENTION_CHARS and len(ch) == 1 and not (mods & NO_COMMAND):
        return f"key equivalent ⌘{ch.upper()} (quit/close/hide/minimise/print by convention)"
    if (vk in CONVENTION_VKEYS) or ch in CONVENTION_CMD_CHARS:
        return "delete/escape/tab key equivalent (destructive or modal by convention)"
    if mods & GLOBE:
        return "globe (fn) key equivalent: a system function (dictation, emoji, full screen, tiling)"
    return None


def mark_runs(items: Sequence[Dict[str, Any]], marks: Optional[Dict[str, Any]] = None
              ) -> List[List[Dict[str, Any]]]:
    """Runs of contiguous sibling leaf items (a separator or a non-leaf item
    ends a run) of length >= 2 in which at least one item carries a mark.
    ``marks`` (index path string -> mark char) overrides the dump's
    ``mark_char`` (read with the menu closed) when the guest read the menus
    open."""
    def mark(it):
        if marks is not None:
            return bool((marks.get(path_key(it["index_path"])) or "").strip())
        return bool((it.get("mark_char") or "").strip())

    runs, cur, prev = [], [], None
    for it in sorted(items, key=lambda m: m.get("index_path") or []):
        p = tuple(it.get("index_path") or [])
        contiguous = prev is not None and p[:-1] == prev[:-1] and p[-1] == prev[-1] + 1
        if not contiguous:
            if cur:
                runs.append(cur)
            cur = [it]
        else:
            cur.append(it)
        prev = p
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2 and any(mark(m) for m in r)]


def path_key(ip: Iterable[int]) -> str:
    return "/".join(str(i) for i in ip)


@dataclass
class ProbeItem:
    key: str                         # discovery key
    trigger: Dict[str, Any]          # identifier / cmd_char / cmd_mods / index_path
    kind: str                        # "shortcut" | "run"
    run: Optional[str] = None        # the run's first index path (runs only)
    run_members: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"key": self.key, "trigger": self.trigger, "kind": self.kind, "run": self.run,
                "run_members": list(self.run_members)}


def probe_plan(dump: Dict[str, Any], marks: Optional[Dict[str, Any]] = None,
               refused: Optional[Dict[str, str]] = None) -> Tuple[List[ProbeItem], Dict[str, str]]:
    """(items to probe, {discovery key: why not probed}). Probed: every leaf
    item with a key equivalent, and up to RUN_PROBES members of every mark
    run. An item must carry an identity (identifier or key equivalent) to be
    pressed at all: a title or a position never resolves a trigger."""
    leaf = C._leaf_menu(dump)
    plan: List[ProbeItem] = []
    skipped: Dict[str, str] = {}
    in_run: Dict[str, Tuple[str, List[str]]] = {}
    ident_n = Counter(identity_string(m) for m in dump.get("menu_items") or [])

    def unique(m):
        return ident_n[identity_string(m)] == 1

    runs = mark_runs(leaf, marks)
    for r in runs:
        members = [m for m in r if not convention_excluded(m) and _has_identity(m) and unique(m)]
        rid = path_key(r[0]["index_path"])
        keys = [m["key"] for m in members[:RUN_PROBES]]
        for m in r:
            if m["key"] in keys:
                in_run[m["key"]] = (rid, keys)
    refused = refused or {}
    for it in leaf:
        why = convention_excluded(it)
        if not why and identity_string(it) in refused:
            why = f"raised an authentication prompt in an earlier probe; never pressed again ({refused[identity_string(it)]})"
        if why:
            skipped[it["key"]] = why
            continue
        if not _has_identity(it):
            skipped[it["key"]] = "no identity to resolve by (no identifier, no key equivalent)"
            continue
        if not unique(it):
            skipped[it["key"]] = (f"identity shared by {ident_n[identity_string(it)]} menu items "
                                  "(identifier + key equivalent do not pick one)")
            continue
        trig = C._trigger_recipe(it)
        if it["key"] in in_run:
            rid, keys = in_run[it["key"]]
            plan.append(ProbeItem(it["key"], trig, "run", rid, keys))
        elif (it.get("cmd_char") or "").strip() or it.get("cmd_virtual_key"):
            plan.append(ProbeItem(it["key"], trig, "shortcut"))
        else:
            skipped[it["key"]] = "not probed: no key equivalent and not in a marked group"
    return plan, skipped


def _has_identity(it: Dict[str, Any]) -> bool:
    return bool((it.get("identifier") or "").strip() or (it.get("cmd_char") or "").strip())


# ---------------------------------------------------------------------------
# 2. Classifying one probe
# ---------------------------------------------------------------------------

EFFECTS = ("auth_required", "object_removed", "object_created_on_commit", "object_named", "app_deactivated", "dialog_opened", "window_opened", "window_closed", "navigated", "view_changed", "layout_changed", "object_created", "entry_focused",
           "search_focused", "document_saved", "document_changed", "mark_exclusive", "mark_flag",
           "mark_already", "mark_from_none", "mark_other", "selection_changed", "focus_changed")

TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
DIALOG_SUBROLES = frozenset({"AXDialog", "AXSystemDialog", "AXFloatingWindow", "AXSystemFloatingWindow"})


SECURE_FIELD = "AXSecureTextField"
DIALOG_ROLES = frozenset({"AXSheet"})


def is_auth_prompt(tree: Optional[Dict[str, Any]], _in_dialog: bool = False, _depth: int = 0) -> bool:
    """True if an accessibility tree (``{"role", "subrole", "children"}``
    dicts, one per element) holds an authentication prompt: a dialog or
    sheet (role ``AXSheet``, or a window whose subrole is a dialog subrole)
    containing a secure text field. Generic: roles only, no titles. Such a
    prompt is never answered; the step is refused and the prompt cancelled."""
    if not isinstance(tree, dict) or _depth > 20:
        return False
    role, sub = tree.get("role") or "", tree.get("subrole") or ""
    dialog = _in_dialog or role in DIALOG_ROLES or (role == "AXWindow" and sub in DIALOG_SUBROLES)
    if dialog and (role == SECURE_FIELD or sub == SECURE_FIELD):
        return True
    return any(is_auth_prompt(k, dialog, _depth + 1) for k in tree.get("children") or [])


def _wkey(w):
    return (w.get("role"), w.get("subrole"), w.get("identifier"))


def classify(before: Dict[str, Any], after: Dict[str, Any], pressed: Optional[str] = None,
             run_paths: Sequence[str] = (), name: Optional[str] = None) -> Dict[str, Any]:
    """The effects one press caused. ``pressed`` is the pressed item's index
    path string (a reading key), ``run_paths`` its run's members. ``name``:
    a value the probe committed into a focused entry; if it now
    names a new object (a new folder item or scope, or a new static label of
    the main window, ``labels``) the effect ``object_named`` is added.
    Returns {"effects": [...], "coupled": [paths of marks that changed
    outside the run], "grew": [container keys whose row count grew]}."""
    eff: List[str] = []
    if after.get("auth_prompt") and not before.get("auth_prompt"):
        eff.append("auth_required")
    # AXWindows can hold non-window elements (Finder lists its inline rename
    # field and the desktop layer there): only AXWindow entries are windows
    bw = Counter(_wkey(w) for w in before.get("windows") or [] if w.get("role") == "AXWindow")
    aw = Counter(_wkey(w) for w in after.get("windows") or [] if w.get("role") == "AXWindow")
    new = aw - bw
    gone = bw - aw
    if any(k[1] in DIALOG_SUBROLES for k in new) or (after.get("sheets") or 0) > (before.get("sheets") or 0):
        eff.append("dialog_opened")
    if any(k[1] not in DIALOG_SUBROLES for k in new):
        eff.append("window_opened")
    if gone and not new:
        eff.append("window_closed")
    if not new and not gone and sorted(str(w.get("document")) for w in before.get("windows") or []
                                       if w.get("role") == "AXWindow") != \
            sorted(str(w.get("document")) for w in after.get("windows") or [] if w.get("role") == "AXWindow"):
        eff.append("navigated")
    if not new and not gone and set(before.get("containers") or {}) != set(after.get("containers") or {}):
        eff.append("layout_changed")
    grew = sorted(k for k, v in (after.get("containers") or {}).items()
                  if (v or {}).get("rows", 0) > ((before.get("containers") or {}).get(k) or {}).get("rows", 0)
                  and k in (before.get("containers") or {}))
    bm, am = before.get("model") or {}, after.get("model") or {}
    model_grew = bm.get("kind") == "folder" and am.get("kind") == "folder" and \
        len(set(am.get("items") or []) - set(bm.get("items") or [])) > 0
    scope_grew = _scopes_grew(bm, am)
    folder_model = bm.get("kind") == "folder" and am.get("kind") == "folder"
    if folder_model and not model_grew and not scope_grew:
        # the backend is authoritative: rows that grew without the folder
        # growing are a view change (another folder shown), not an object
        if grew:
            eff.append("view_changed")
        grew = []
    if grew or model_grew or scope_grew:
        eff.append("object_created")
    if (folder_model and set(bm.get("items") or []) - set(am.get("items") or [])) or \
            model_preserved_scopes_lost(bm, am):
        eff.append("object_removed")
    f0, f1 = before.get("focused") or {}, after.get("focused") or {}
    if f1 and f1 != f0:
        if f1.get("subrole") == "AXSearchField":
            eff.append("search_focused")
        elif f1.get("role") in TEXT_ROLES:
            eff.append("entry_focused")
        else:
            eff.append("focus_changed")
    fronts = after.get("frontmost_during_press")
    if after.get("frontmost_source") != "ax_system_wide":
        fronts = None          # samples from another source can be a stale NSWorkspace value
    if isinstance(fronts, list) and fronts and before.get("bundle_id") and \
            any(f != before.get("bundle_id") for f in fronts):
        eff.append("app_deactivated")
    if bm.get("kind") == "file" and am.get("kind") == "file" and bm.get("sha") != am.get("sha"):
        eff.append("document_saved")
    if before.get("text") is not None and after.get("text") is not None \
            and before.get("text") != after.get("text"):
        eff.append("document_changed")
    coupled: List[str] = []
    b, a = before.get("marks"), after.get("marks")
    if pressed and isinstance(b, dict) and isinstance(a, dict):
        run = set(run_paths) | {pressed}
        changed = sorted(p for p in set(b) | set(a) if bool(b.get(p)) != bool(a.get(p)))
        in_run = [p for p in changed if p in run]
        coupled = [p for p in changed if p not in run]
        if pressed in in_run:
            others = [p for p in in_run if p != pressed]
            if a.get(pressed) and len(others) == 1 and b.get(others[0]) and not a.get(others[0]):
                eff.append("mark_exclusive")
            elif not others and a.get(pressed) and not any(b.get(p) for p in run):
                # the group had no mark and now the pressed item has one:
                # consistent with an option group and a checkbox group alike
                eff.append("mark_from_none")
            elif not others:
                eff.append("mark_flag")
            else:
                eff.append("mark_other")
        elif b.get(pressed) and a.get(pressed) and not in_run:
            eff.append("mark_already")
        elif in_run:
            eff.append("mark_other")
    if before.get("selection") != after.get("selection") and after.get("selection") is not None:
        eff.append("selection_changed")
    if name and named_object(before, after, name):
        eff.append("object_named")
    return {"effects": eff, "coupled": coupled, "grew": grew}


def named_object(before: Dict[str, Any], after: Dict[str, Any], name: str) -> bool:
    """Is ``name`` the name of something that exists only after: a new item
    of the folder model, a new scope of the scope model, or a new static
    label of the main window (``labels``: never an editable field's value)?"""
    bm, am = before.get("model") or {}, after.get("model") or {}
    if name in set(am.get("items") or []) - set(bm.get("items") or []):
        return True
    if name in set((am.get("scopes") or {})) - set((bm.get("scopes") or {})):
        return True
    bl, al = before.get("labels"), after.get("labels")
    return isinstance(bl, list) and isinstance(al, list) and name in set(al) - set(bl)


def model_preserved_scopes_lost(bm, am) -> bool:
    bs, as_ = (bm or {}).get("scopes"), (am or {}).get("scopes")
    return isinstance(bs, dict) and isinstance(as_, dict) and \
        any(k not in as_ or as_[k] < n for k, n in bs.items())


def _scopes_grew(bm, am) -> bool:
    """A new scope, or a scope whose object-count badge grew (a new
    note counts in its folder's badge before any label shows its name)."""
    bs, as_ = (bm or {}).get("scopes"), (am or {}).get("scopes")
    return isinstance(bs, dict) and isinstance(as_, dict) and \
        (bool(set(as_) - set(bs)) or any(as_[k] > n for k, n in bs.items() if k in as_))


# ---------------------------------------------------------------------------
# 2b. The element a trigger focused
# ---------------------------------------------------------------------------

#: the watcher's settle time after a trigger (``watcher/step_capture.py`` ``FOCUS_SETTLE_S``)
FOCUS_SETTLE_S = 0.3
#: how long the read waits at most while the app still has input queued
FOCUS_WAIT_S = 2.0
FOCUS_POLL_S = 0.05
#: the raw roles of a text entry; the runner's ``focused`` kind types into one of them
FOCUS_ENTRY_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"})
#: the canonical roles ``ax_actions.FOCUS_ROLES`` takes (kept in step with it)
FOCUS_CANONICAL = {"AXTextField": "textfield", "AXTextArea": "textarea", "AXComboBox": "combobox",
                   "AXSearchField": "searchfield"}


def document_class(window: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """``(document, window)`` of the window a focused element is in, without its text: the
    window's ``AXDocument`` path as ``sha256:<16 hex>`` ("" when it has none) and its class
    (``sheet``, else the window's subrole, "" for no window)."""
    if not isinstance(window, dict):
        return "", ""
    doc = window.get("document")
    digest = ("sha256:" + hashlib.sha256(str(doc).encode("utf-8")).hexdigest()[:16]) if doc else ""
    return digest, ("sheet" if window.get("sheet") else str(window.get("subrole") or ""))


def read_trigger_focus(pid: int, pre: Any, *, bundle_id: str,
                       front_pid: Callable[[], Optional[int]], focused: Callable[[int], Any],
                       pid_of: Callable[[Any], Optional[int]], describe: Callable[[Any], Dict[str, Any]],
                       window_of: Callable[[Any], Optional[Dict[str, Any]]], same: Callable[[Any, Any], bool],
                       input_pending: Optional[Callable[[], bool]] = None,
                       sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                       settle: float = FOCUS_SETTLE_S, wait: float = FOCUS_WAIT_S) -> Dict[str, Any]:
    """The element focused after the probe pressed a trigger, the watcher's way
    (``step_capture._read_trigger_focus``): read ``settle`` seconds after the press, later while
    the app still has input queued (``input_pending``, at most ``wait`` seconds), from the app
    with the probed ``pid`` only: the front app must be that pid (``front_pid``) and the focused
    element must belong to it (``pid_of``). ``pre``: the element focused before the press (an
    in-memory element, compared by ``same``).

    Reads only: the element's role and subrole (``describe``), its window's document and class
    (``window_of``; the document hashed, ``document_class``). Never a value or a title.

    Returns ``{"read": True, "role", "subrole", "canonical_role", "app", "document", "window",
    "moved", "text_entry", "secure"}`` or ``{"read": False, "why"}``."""
    sleep(settle)
    deadline = clock() + wait
    while input_pending is not None:
        try:
            pending = bool(input_pending())
        except Exception:  # noqa: BLE001 - an unreadable queue: treated as still busy
            pending = True
        if not pending:
            break
        if clock() >= deadline:
            return {"read": False, "why": f"input still queued {wait:.1f} s after the trigger"}
        sleep(FOCUS_POLL_S)
    try:
        front = front_pid()
    except Exception:  # noqa: BLE001
        front = None
    if front != pid:
        return {"read": False, "why": "another app is in front after the trigger"}
    try:
        el = focused(pid)
    except Exception:  # noqa: BLE001
        el = None
    if el is None:
        return {"read": False, "why": "no element of the app has keyboard focus"}
    try:
        owner = pid_of(el)
    except Exception:  # noqa: BLE001
        owner = None
    if owner != pid:
        return {"read": False, "why": "the focused element belongs to another process"}
    try:
        d = describe(el) or {}
        win = window_of(el)
    except Exception as exc:  # noqa: BLE001
        return {"read": False, "why": f"the focused element cannot be described ({type(exc).__name__})"}
    role, sub = str(d.get("role") or ""), str(d.get("subrole") or "")
    secure = SECURE_FIELD in (role, sub)
    try:
        moved = pre is None or not same(el, pre)
    except Exception:  # noqa: BLE001
        moved = True
    doc, window = document_class(win)
    return {"read": True, "role": role, "subrole": sub,
            "canonical_role": "" if secure else FOCUS_CANONICAL.get(role, ""),
            "app": bundle_id, "document": doc, "window": window, "moved": bool(moved),
            "text_entry": not secure and role in FOCUS_ENTRY_ROLES, "secure": secure}


def focused_target(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The map projection's ``focused`` target for one probe row: ``{kind: "focused",
    canonical_role, raw_role, app, document, window, trigger}`` when the row's ``focus`` was read,
    moved and is a text entry of the probed app (not a password field); None otherwise. The
    canonical role is the element's ROLE's (a search field is an AXTextField with the subrole
    AXSearchField: ``textfield``), as ``ax_actions.resolve_focused`` compares it."""
    f = row.get("focus") if isinstance(row, dict) else None
    if not (isinstance(f, dict) and f.get("read") and f.get("moved") and f.get("text_entry")
            and not f.get("secure") and f.get("canonical_role")):
        return None
    return {"kind": "focused", "canonical_role": f["canonical_role"], "raw_role": f.get("role") or "",
            "app": f.get("app") or "", "document": f.get("document") or "", "window": f.get("window") or "",
            "trigger": row.get("trigger")}


# ---------------------------------------------------------------------------
# 3. Mapping probes to contract candidates
# ---------------------------------------------------------------------------

#: Why a probed item gets no class, by its effects (first match wins).
NO_CLASS = (
    ("auth_required", "requires authentication: refused, prompt cancelled, no credentials entered"),
    ("dialog_opened", "opens a dialog or sheet"),
    ("window_opened", "opens a window"),
    ("window_closed", "closes a window"),
    ("navigated", "shows another location in the same window"),
    ("view_changed", "changes what the window shows, the model is unchanged"),
    ("layout_changed", "shows or hides a pane (the window's containers change)"),
    ("document_changed", "changes the document without saving it (an edit command)"),
    ("selection_changed", "changes the selection only"),
    ("focus_changed", "moves focus only"),
)


def declare_coupled(dump: Dict[str, Any], by_path: Dict[str, Dict[str, Any]],
                    coupled_paths: Sequence[str]) -> List[str]:
    """Side-effect declarations for coupled marks: a mark inside a
    submenu declares the whole submenu (``parent identity>*``: its items can
    differ between states, e.g. a sort submenu per view mode); a mark in a
    top-level menu declares its run between separators (item identities)."""
    parents = {path_key(m.get("index_path") or []): m for m in dump.get("menu_items") or []}
    out = []
    for c in coupled_paths:
        ip = [int(x) for x in c.split("/")]
        if len(ip) >= 3 and path_key(ip[:-1]) in parents:
            out.append(identity_string(parents[path_key(ip[:-1])]) + ">*")
        else:
            out.extend(identity_string(by_path[k]) for k in sibling_run(by_path, c) if k in by_path)
    return out


def sibling_run(by_path: Dict[str, Dict[str, Any]], path: str) -> List[str]:
    """Index-path keys of the settings group that contains ``path``: inside
    a submenu (index path of length >= 3), every leaf item of that submenu
    (a submenu is one named group, even across its separators); in a
    top-level menu, the contiguous run of leaf items between separators."""
    if path not in by_path:
        return [path]
    ip = [int(x) for x in path.split("/")]
    if len(ip) >= 3:
        pre = path_key(ip[:-1]) + "/"
        return sorted((k for k in by_path if k.startswith(pre) and k.count("/") == path.count("/")),
                      key=lambda k: [int(x) for x in k.split("/")])
    out = [path]
    for step in (-1, 1):
        j = ip[-1] + step
        while True:
            k = path_key(ip[:-1] + [j])
            if k not in by_path:
                break
            out.append(k)
            j += step
    return sorted(out, key=lambda k: [int(x) for x in k.split("/")])


def reason_for(effects: Sequence[str]) -> str:
    for e, why in NO_CLASS:
        if e in effects:
            return why
    return "no observable effect in the probe start" if not effects else \
        "effects fit no class: " + ", ".join(effects)


def map_from_probes(dump: Dict[str, Any], probes: Sequence[Dict[str, Any]]
                    ) -> Tuple[List[C.ContractCandidate], Dict[str, Any]]:
    """Contract candidates from one app's probe results. ``probes`` are the
    guest's rows: {"key", "trigger", "kind", "run", "run_members", "enabled",
    "pressed_path", "before", "after", "error", "control_saved"}.
    ``control_saved`` is the guest's idle control for a row that saved: the
    same probe start, the same wait, no press; True if the file changed
    anyway (autosave), and then the save is not attributed to the item. Returns (candidates,
    counts) with per-item outcomes in counts["outcomes"]."""
    bid = dump.get("bundle_id") or ""
    leaf = {m["key"]: m for m in C._leaf_menu(dump)}
    by_path = {path_key(m["index_path"]): m for m in leaf.values()}
    ctrls = C._controls(dump)
    container = C._has_container(dump)
    area = next((c for c in ctrls if c.get("role") == "AXTextArea"), None)
    out: List[C.ContractCandidate] = []
    outcome: Dict[str, str] = {}
    runs: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}

    def add(contract, triggers, recipe, provable=True, note=""):
        cid = f"{bid}:{contract}:{'+'.join(triggers)}"
        out.append(C.ContractCandidate(cid, bid, contract, list(triggers), recipe, provable, note))

    # a run member enabled only under a precondition is classified alone when
    # other members of its run were probed in the plain start (one start
    # state per run; desktop_preconditions.merge_rows)
    plain_runs = {p.get("run") for p in probes if p.get("kind") == "run" and p.get("enabled")
                  and not p.get("precondition")}
    for p in probes:
        k = p["key"]
        if p.get("error"):
            outcome[k] = f"probe error: {p['error']}"
            continue
        if not p.get("enabled"):
            outcome[k] = "disabled in the probe start" + (
                "; enabled by no derived precondition" if p.get("precondition_tried") else "")
            continue
        cl = classify(p["before"], p["after"], p.get("pressed_path"),
                      [path_key(leaf[m]["index_path"]) for m in p.get("run_members") or [] if m in leaf])
        if isinstance(p.get("after_commit"), dict):
            # a naming step: the guest committed a probe name into the
            # focused entry; an object that exists only now was created by
            # the trigger + naming (a sheet that asks for the name first, or
            # an inline entry whose object appears on commit: seen
            # through the model or as a new label named by the value)
            c2 = classify(p["before"], p["after_commit"], name=p.get("commit_value"))
            if ("object_created" in c2["effects"] or "object_named" in c2["effects"]) \
                    and "object_created" not in cl["effects"]:
                cl["effects"] = cl["effects"] + ["object_created_on_commit"]
                cl["grew"] = c2["grew"]
                if "object_named" in c2["effects"]:
                    cl["effects"].append("object_named")
        p["classified"] = cl
        eff = cl["effects"]
        pc = p.get("precondition")
        under = f" (under precondition {pc['key']})" if pc else ""
        if p.get("kind") == "run" and not (pc and p.get("run") in plain_runs):
            runs.setdefault(p["run"], []).append((p, cl))
            continue
        trig = p["trigger"]
        fu = p.get("followup") if isinstance(p.get("followup"), dict) else None
        # the field the trigger focused, as the probe read it (``read_trigger_focus``): the map
        # projection's ``focused`` target; rows from older probes have none
        focus = focused_target(p)
        if "auth_required" in eff:
            outcome[k] = reason_for(eff)
        elif "object_removed" in eff:
            outcome[k] = "removes or moves existing objects (not a plain create)" + under
        elif fu is not None and ("window_opened" in eff or "dialog_opened" in eff):
            fcls, fwhy = F.map_followup(fu.get("action"), fu.get("effects") or [], fu.get("why") or "")
            base = reason_for(eff)
            if fcls == "scoped_find" and container is not None:
                add("scoped_find", [k], {"trigger": trig, "container_role": container.get("role"),
                                         "window": "new", "entry": "focused search field in the window "
                                         "the trigger opened", "commit": "return", "effects": eff,
                                         "followup": fu, "semantics": None, "precondition": pc,
                                         **({"focus": focus} if focus else {})})
                outcome[k] = "scoped_find (window follow-up)" + under
            elif fcls == "object_create":
                # the follow-up's ``grew`` are object names, not container
                # keys (read as keys, they show no object):
                # the proof observes the object where the follow-up saw it
                add("object_create", [k], {"trigger": trig, "container_role": (container or {}).get("role"),
                                           "naming": "dialog", "containers": [],
                                           "observe": fu.get("seen_through") or _seen_through(fu, p["after"].get("model")),
                                           "entry": "focused text field in the window/sheet the trigger opened",
                                           "commit": "default_button" if (fu.get("facts") or {}).get("default_button")
                                           else "return", "effects": eff, "followup": fu,
                                           "model": (p["after"].get("model") or {}).get("kind"),
                                           "precondition": pc, **({"focus": focus} if focus else {})})
                outcome[k] = "object_create (window follow-up)" + under
            elif fcls == "field_rw":
                f0 = next((f for f in (fu.get("facts") or {}).get("fields") or [] if f.get("focused")), {})
                add("field_rw", [k], {"role": f0.get("role"), "identifier": f0.get("identifier") or "",
                                      "window_trigger": trig, "followup": fu, "precondition": pc})
                outcome[k] = "field_rw (window follow-up)" + under
            else:
                outcome[k] = f"{base}; {fwhy}" + under
        elif ("object_created" in eff and ("entry_focused" in eff or "search_focused" in eff)
                and "window_opened" not in eff and "dialog_opened" not in eff) or \
                ("object_created_on_commit" in eff and "window_opened" not in eff):
            add("object_create", [k], {"trigger": trig, "container_role": (container or {}).get("role"),
                                       "naming": "sheet" if "object_created_on_commit" in eff else "inline",
                                       "containers": cl["grew"],
                                       "observe": "labels" if ("object_named" in eff and not cl["grew"]
                                                               and not _model_grew(p)) else "model",
                                       "entry": "focused text entry after the trigger",
                                       "commit": "return", "effects": eff,
                                       "model": (p["after"].get("model") or {}).get("kind"),
                                       "precondition": pc, **({"focus": focus} if focus else {})})
            outcome[k] = "object_create" + under
        elif "document_saved" in eff and "app_deactivated" in eff:
            outcome[k] = "hands focus to another app; the document autosaved on deactivation: not a save"
        elif "document_saved" in eff and p.get("control_saved"):
            outcome[k] = "the document changed on disk without the press too (autosave): not attributable"
        elif "document_saved" in eff and area is not None and "dialog_opened" not in eff:
            add("doc_save", [k], {"trigger": trig, "entry": {"role": "AXTextArea",
                                                             "identifier": area.get("identifier") or ""},
                                  "effects": eff, "precondition": pc})
            outcome[k] = "doc_save" + under
        elif "search_focused" in eff and container is not None and "window_opened" not in eff:
            add("scoped_find", [k], {"trigger": trig, "container_role": container.get("role"),
                                     "entry": "focused search field after the trigger",
                                     "commit": "return", "effects": eff, "semantics": None,
                                     "precondition": pc, **({"focus": focus} if focus else {})})
            outcome[k] = "scoped_find" + under
        else:
            outcome[k] = reason_for(eff) + under
    for rid, rows in sorted(runs.items()):
        kinds = Counter()
        for p, cl in rows:
            e = cl["effects"]
            if "mark_exclusive" in e:
                kinds["exclusive"] += 1
            elif "mark_flag" in e:
                kinds["flag"] += 1
            elif "mark_already" in e or "mark_from_none" in e:
                kinds["already"] += 1
            else:
                kinds["other"] += 1
        members = [p for p, _cl in rows]
        if any("auth_required" in cl["effects"] for _p, cl in rows):
            for p in members:
                outcome[p["key"]] = reason_for(["auth_required"])
            continue
        pcd, pc_bad = P.candidate_precondition([p for p, _cl in rows])
        coupled_paths = sorted({c for _p, cl in rows for c in cl["coupled"]})
        # the declaration is per coupled SETTINGS GROUP: every member
        # of a sibling run in which a coupled mark moved, not only the
        # members whose mark the probes happened to see move
        side = sorted(set(declare_coupled(dump, by_path, coupled_paths)))
        if kinds["exclusive"] >= 2 and kinds["flag"] == 0 and kinds["other"] == 0:
            keys = [p["key"] for p, cl in rows
                    if {"mark_exclusive", "mark_already", "mark_from_none"} & set(cl["effects"])]
            opts = [p["trigger"] for p, _ in rows if p["key"] in keys]
            add("toggle_select", keys, {"options": opts, "parent_index_path": opts[0]["index_path"][:-1],
                                        "side_effects": side, "coupled_paths": coupled_paths,
                                        "precondition": pcd},
                provable=len(keys) >= C.MIN_OPTIONS and not pc_bad,
                note=pc_bad or ("" if len(keys) >= C.MIN_OPTIONS else f"{len(keys)} options"))
            for p in members:
                outcome[p["key"]] = "toggle_select" if p["key"] in keys else "run member, not an option"
        elif kinds["flag"] >= 2 and kinds["exclusive"] == 0:
            keys = [p["key"] for p, cl in rows if "mark_flag" in cl["effects"]]
            opts = [p["trigger"] for p, _ in rows if p["key"] in keys]
            add("flag_toggle", keys, {"options": opts, "parent_index_path": opts[0]["index_path"][:-1],
                                      "side_effects": side, "coupled_paths": coupled_paths,
                                      "precondition": pcd},
                provable=len(keys) >= C.MIN_OPTIONS and not pc_bad,
                note=pc_bad or ("" if len(keys) >= C.MIN_OPTIONS else f"{len(keys)} flags"))
            for p in members:
                outcome[p["key"]] = "flag_toggle" if p["key"] in keys else "run member, not a flag"
        else:
            why = f"marked group with mixed or no mark behaviour {dict(kinds)}"
            for p in members:
                outcome[p["key"]] = why
    counts = _counts(dump, out, outcome, probes)
    return out, counts


def _model_grew(p: Dict[str, Any]) -> bool:
    b = (p.get("before") or {}).get("model") or {}
    a = (p.get("after_commit") or p.get("after") or {}).get("model") or {}
    return bool(set(a.get("items") or []) - set(b.get("items") or [])) or _scopes_grew(b, a)


def _seen_through(fu: Dict[str, Any], after_model: Any = None) -> str:
    """Where a follow-up's committed name was seen (saved rows without
    ``seen_through``, re-mapped): the backend listing (``model``), the model's
    scopes (``scopes``) or a container label (``labels``). The follow-up's
    ``grew`` are object names; they are looked up in the model read after
    the press when the follow-up carries no pre/post readings."""
    pre, post = fu.get("pre") or {}, fu.get("post") or {}
    names = {fu.get("value")} | set(fu.get("grew") or [])
    bm, am = pre.get("model") or {}, post.get("model") or (after_model if isinstance(after_model, dict) else {})
    if names & (set(am.get("items") or []) - set(bm.get("items") or [])):
        return "model"
    if names & (set(am.get("scopes") or {}) - set(bm.get("scopes") or {})):
        return "scopes"
    if fu.get("grew"):
        # grown in the model (that is what ``grew`` means), readings not
        # kept: by the model's shape
        return "model" if isinstance(am.get("items"), list) else "scopes" if isinstance(am.get("scopes"), dict) \
            else "labels"
    return "labels"


def _counts(dump, cands, outcome, probes):
    from . import desktop_discover as dd
    disc = dd.discover_app(dump)
    all_keys = {c.key for c in disc.candidates}
    used = set()
    for c in cands:
        used.update(k for k in c.triggers if k in all_keys)
    reasons = Counter(v for v in outcome.values())
    return {"discovered": len(disc.candidates), "probed": len(probes),
            "probed_enabled": sum(1 for p in probes if p.get("enabled") and not p.get("error")),
            "source_candidates_mapped": len(used),
            "mapping_rate": round(len(used) / len(disc.candidates), 4) if disc.candidates else 0.0,
            "mapping_rate_of_probed": round(len(used) / len(probes), 4) if probes else 0.0,
            "contract_candidates": len(cands),
            "provable_candidates": sum(1 for c in cands if c.provable),
            "by_contract": {n: sum(1 for c in cands if c.contract == n) for n in C.CONTRACTS},
            "outcomes": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "triggers_keyed_by_position": sum(1 for c in cands for k in c.triggers if k.startswith("pos:")),
            # state-conditioned probing and window follow-ups
            "disabled_in_plain_start": sum(1 for p in probes if p.get("enabled") is False
                                           or (p.get("precondition") and p.get("disabled_plain"))),
            "probed_under_precondition": sum(1 for p in probes if p.get("precondition")),
            "enabled_by_precondition": {p["key"]: p["precondition"]["key"] for p in probes
                                        if p.get("precondition")},
            "still_disabled": sum(1 for p in probes if p.get("enabled") is False),
            "followups": sum(1 for p in probes if isinstance(p.get("followup"), dict)),
            "followup_filled": sum(1 for p in probes if isinstance(p.get("followup"), dict)
                                   and (p["followup"].get("action"))),
            "candidates_via_followup": sum(1 for c in cands if isinstance(c.recipe.get("followup"), dict)),
            "candidates_under_precondition": sum(1 for c in cands if c.recipe.get("precondition")),
            "per_item": outcome}


# ---------------------------------------------------------------------------
# 4. Scoped-find completeness semantics
# ---------------------------------------------------------------------------
#
# A search result is "complete" only relative to a matching rule. Three rules
# are distinguished, and the app's own rule is MEASURED on calibration files
# (never assumed from the app's name):
#
#   substring      case-insensitive literal substring of the item name
#   token_prefix   case-insensitive prefix of a name token (tokens split at
#                  anything that is not a letter or digit)
#   spotlight      the system metadata index (name tokens AND content), as
#                  the system's own index answers it at query time
#
# The comparison backend must have the SAME rule. substring and token_prefix
# have pure backends below; spotlight's backend is the system index itself
# (the guest's ``mdfind -onlyin``). If the observed rule matches none of the
# three, or its backend is unavailable, scoped_find is refused with that
# reason. The comparison is never loosened.

SEMANTICS = ("substring", "token_prefix", "spotlight")

#: calibration files: name -> content. Designed so the three rules disagree.
CALIBRATION_FILES = {
    "calib_amber.txt": "calibration\n",       # token after '_'
    "amberly.txt": "calibration\n",           # name prefix
    "xamberx.txt": "calibration\n",           # mid-word only
    "plain_one.txt": "amber inside the content only\n",
    "quartz_box.md": "calibration\n",
    "quartzite.md": "calibration\n",
    "rosequartz.md": "calibration\n",
    "plain_two.txt": "quartz inside the content only\n",
    "zz_unrelated.txt": "calibration\n",
}
CALIBRATION_PATTERNS = ("amber", "quartz")


def _tokens(name: str) -> List[str]:
    out, cur = [], []
    for ch in name.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def match_substring(names: Iterable[str], pattern: str) -> List[str]:
    p = pattern.lower()
    return sorted(n for n in names if p in n.lower())


def match_token_prefix(names: Iterable[str], pattern: str) -> List[str]:
    p = pattern.lower()
    return sorted(n for n in names if any(t.startswith(p) for t in _tokens(n)))


def expected_calibration(semantics: str, pattern: str) -> List[str]:
    names = list(CALIBRATION_FILES)
    if semantics == "substring":
        return match_substring(names, pattern)
    if semantics == "token_prefix":
        return match_token_prefix(names, pattern)
    if semantics == "spotlight":
        content = [n for n, body in CALIBRATION_FILES.items() if pattern in body]
        return sorted(set(match_token_prefix(names, pattern)) | set(content))
    raise ValueError(semantics)


def classify_semantics(observed: Dict[str, Sequence[str]]) -> Tuple[Optional[str], str]:
    """(semantics, why) from the app's own results on the calibration
    patterns. Exactly one rule must reproduce every pattern's result set."""
    fits = [s for s in SEMANTICS
            if all(sorted(observed.get(p) or []) == expected_calibration(s, p) for p in CALIBRATION_PATTERNS)]
    if len(fits) == 1:
        return fits[0], f"calibration results match the {fits[0]} rule on every pattern"
    got = {p: sorted(observed.get(p) or []) for p in CALIBRATION_PATTERNS}
    return None, f"calibration results {got} match {'no' if not fits else 'several'} known rule(s) {fits}"


def pure_backend(semantics: str):
    """The pure comparison backend for a rule, or None (spotlight: the
    system index is the only backend with the same rule)."""
    return {"substring": match_substring, "token_prefix": match_token_prefix}.get(semantics)


# ---------------------------------------------------------------------------
# 5. Stable model reading (preserved state independent of the viewport)
# ---------------------------------------------------------------------------

def scopes_from_rows(rows: Sequence[Sequence[str]]) -> Optional[Dict[str, int]]:
    """{scope label: object count} from a navigation container's rows, when
    every counted row carries exactly one label and one integer badge (a
    source list with counts, e.g. a folder list). Rows without a badge
    (section headers) are skipped; None if no row has one."""
    out: Dict[str, int] = {}
    for texts in rows:
        labels = [t for t in texts if not t.strip().isdigit()]
        nums = [int(t) for t in texts if t.strip().isdigit()]
        if len(nums) == 1 and labels:
            out[labels[0]] = nums[0]
    return out or None
