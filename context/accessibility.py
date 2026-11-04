"""anyuse context.accessibility — macOS Accessibility (AX) tree (Phase 3).

Precise, non-pixel actuation + verification via the macOS Accessibility API
(AXUIElement, pyobjc ApplicationServices). Reading another app's tree or
performing actions needs Accessibility TCC granted to the host process; without
it the API returns errors and everything here degrades to empty snapshots / False
/ empty find — never a crash. The diff / summary / match logic is pure and works
without any permission.

Interface (from execution/executor.py):
  AccessibilityTree(pid=None)
    .snapshot() -> AXSnapshot
    .find(role=None, title=None) -> list[element]   (raw AXUIElementRefs)
    .perform_action(element, action: str) -> bool
    .set_value(element, value) -> bool          True iff the value READS BACK
    .set_value_evidence(element, value) -> SetValueEvidence
    .enter_text(element, text, verify=…) -> TextEntryResult
                                    AXValue → AXSelectedText → keystrokes, each
                                    with evidence; `verify` is the independent
                                    accessor that proves the app kept the text
    .diff(before: AXSnapshot, after: AXSnapshot) -> AXDiff
  AXSnapshot(elements=[AXElementDesc])
  AXDiff: .to_summary() -> str ; .clearly_matches(expected: str) -> bool
"""

from __future__ import annotations

import logging
import re
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_DEPTH = 18
_MAX_ELEMENTS = 4000
_AX_SUCCESS = 0  # kAXErrorSuccess


def _ax():
    import ApplicationServices as A  # pyobjc-framework-ApplicationServices
    return A


def _appkit():
    import AppKit  # pyobjc-framework-Cocoa; only the app switch and the bundle-id lookup need it
    return AppKit


#: how long ``activate_app`` waits for the AX focus to confirm the activated app (seconds). 5 s: under macOS 26's
#: cooperative activation ``AXFocusedApplication`` can lag the activation (Notes visibly in front, the AX focus still
#: TextEdit after 3 s)
ACTIVATE_SETTLE_S = 5.0
#: at most this many polls of an activation are kept in its evidence (the first ones and the latest)
ACTIVATION_POLLS_KEPT = 60


# Whether the macOS Accessibility framework is importable in THIS process. This is
# a static, install-time fact (pyobjc present). The PERMISSION to read other apps'
# trees is a separate runtime TCC grant — see ``ax_trusted()``. Without the grant
# the API degrades to empty snapshots / False, never a crash. Exposed here so the
# recorder/executor have one canonical source instead of importing a name that
# never existed (which silently disabled AX capture entirely).
try:
    import ApplicationServices as _APPSERVICES  # noqa: F401
    AX_AVAILABLE = True
except Exception:  # pragma: no cover - depends on platform/pyobjc presence
    AX_AVAILABLE = False


SECURE_ROLE = "AXSecureTextField"
#: a row of a list, table or outline (an outline's row is an AXRow with the AXOutlineRow
#: subrole) and the containers rows live in (execution/ax_actions.py select_row)
ROW_ROLES = frozenset({"AXRow"})
ROW_CONTAINER_ROLES = frozenset({"AXTable", "AXOutline", "AXList"})
_AX_NO_VALUE = -25212            # kAXErrorNoValue: the attribute has no value (no window)


class AXScanError(RuntimeError):
    """The AX API could not answer (not trusted, the app did not respond, ...)."""


#: an identifier AppKit generates for a nib object (``_NS:<object number>``): it numbers the object in its nib, it is
#: not a name a developer gave it, and it changes with the app's build
AUTO_MENU_ID = re.compile(r"^_NS:\d+$")


def menu_place_identity(selector, live_identifier: str, live_name: str, live_menu_path) -> bool:
    """A selector WITHOUT an identifier that is pinned to a menu place (its
    ``menu_path``: a map-compiled menu hop, which knows the menu titles but no identifier) is the live menu element at
    the SAME menu place with the SAME name even when that element carries an AppKit-generated ``_NS:<n>`` identifier
    (TextEdit's "Ablage" menu-bar item can be ``_NS:860``; the map never sees it). The menu titles and the name
    are the identity there; a developer-given identifier, another place or another name is another element, and a
    namesake at the same place stays an ambiguity. Every other selector keeps the rule that an identifier on either
    side is decisive."""
    mp = getattr(selector, "menu_path", None)
    return (mp is not None and not getattr(selector, "identifier", "") and bool(getattr(selector, "name", ""))
            and bool(live_identifier) and bool(AUTO_MENU_ID.match(str(live_identifier)))
            and live_name == selector.name and live_menu_path is not None and list(live_menu_path) == list(mp))


def frontmost_bundle_live() -> Optional[str]:
    """The bundle id of the app keystrokes go to NOW: the system-wide element's ``AXFocusedApplication`` -> its pid ->
    the running app's bundle id; None when the AX API gives no answer. ``NSWorkspace.frontmostApplication`` is
    refreshed only by a run loop, which a guest job or a CLI replay does not spin: in one long-lived process it keeps
    the app that was in front earlier (a Finder folder name's Return was refused as "keystrokes would go to
    com.apple.Notes" after Notes had been in front once; the guest's ``clean_state.frontmost_bundle`` reads it this
    way too)."""
    try:
        A = _ax()
        err, app = A.AXUIElementCopyAttributeValue(A.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
        if err != _AX_SUCCESS or app is None:
            return None
        err, pid = A.AXUIElementGetPid(app, None)
        if err != _AX_SUCCESS or not pid:
            return None
        running = _appkit().NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        bid = running.bundleIdentifier() if running is not None else None
        return str(bid) if bid else None
    except Exception:  # noqa: BLE001 - no AX answer: the caller falls back
        return None


#: the sources of "which app is in front", in the order they are asked (measured on macOS 26.6.2; the watcher's
#: ``MacAXProbe.frontmost`` asks them in the same order, by these names, recorded as ``front_source``): the system-wide
#: ``AXFocusedApplication`` whenever it answers (it fails in stretches: no value for seconds after a fresh launch,
#: sometimes kAXErrorCannotComplete); else ``NSWorkspace.frontmostApplication`` read right after a run-loop tick
#: (without one it lags one read behind a switch), cross-checked by ``NSWorkspace.menuBarOwningApplication``. The
#: untickled workspace read is never a source on its own. ``menuBarOwningApplication`` is as stale as the untickled
#: workspace read (live: after `open -b Notes` it kept TextEdit for 3.6 s in a worker thread AND in the main thread
#: without a tick, and one main-thread tick showed Notes; in another live check it confirmed or refused by whatever
#: the last main-thread tick had seen), so it too is read only right after a MAIN-thread tick. Off the main thread
#: (the executor runs an AX step in a worker thread) neither workspace source answers; the window server does: the
#: owner of the frontmost on-screen window of the normal layer (``CGWindowListCopyWindowInfo``, a WindowServer query,
#: live in any thread), asked only when no other source answered.
FRONTMOST_SOURCES = ("ax", "nsworkspace_tick", "menubar_owner", "window_server")
#: how long the run-loop tick before the workspace read runs (seconds)
RUNLOOP_TICK_S = 0.02


def _bundle_of(app) -> Optional[str]:
    bid = app.bundleIdentifier() if app is not None else None
    return str(bid) if bid else None


def workspace_frontmost_ticked() -> Optional[str]:
    """``NSWorkspace.frontmostApplication``'s bundle id read RIGHT AFTER a run-loop tick (``NSRunLoop.runUntilDate_``,
    ``RUNLOOP_TICK_S``), which lets the workspace take the app-switch notifications it otherwise lags behind; None
    when the tick cannot run or the read fails (never the untickled read). Only on the MAIN thread: NSWorkspace's
    properties change only when the main run loop runs, so a tick of another thread's run loop (the executor runs
    an AX step in a worker thread) refreshes nothing - there the source gives no answer rather than the stale read."""
    import threading
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        kit = _appkit()
        kit.NSRunLoop.currentRunLoop().runUntilDate_(kit.NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_TICK_S))
    except Exception:  # noqa: BLE001 - no tick: no workspace answer
        return None
    try:
        return _bundle_of(kit.NSWorkspace.sharedWorkspace().frontmostApplication())
    except Exception:  # noqa: BLE001
        return None


def menubar_owner() -> Optional[str]:
    """``NSWorkspace.menuBarOwningApplication``'s bundle id (the app whose menus the menu bar shows) read RIGHT AFTER a
    main-thread run-loop tick, or None: off the main thread, without a tick or on a failed read (it changes only when
    the main run loop runs, like ``frontmostApplication``: never the stale read)."""
    import threading
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        kit = _appkit()
        kit.NSRunLoop.currentRunLoop().runUntilDate_(kit.NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_TICK_S))
    except Exception:  # noqa: BLE001 - no tick: no answer
        return None
    try:
        return _bundle_of(kit.NSWorkspace.sharedWorkspace().menuBarOwningApplication())
    except Exception:  # noqa: BLE001
        return None


def _quartz():
    import Quartz  # pyobjc-framework-Quartz; refused by the host test guard
    return Quartz


def window_server_front_pid(quartz=None) -> Optional[int]:
    """The pid of the app owning the frontmost on-screen window of the normal layer (layer 0) in the window server's
    front-to-back list (``CGWindowListCopyWindowInfo``), or None. A WindowServer query: live in any thread, no run
    loop needed. An app in front with no window of its own is not seen (another app's window is on top)."""
    try:
        Q = quartz if quartz is not None else _quartz()
        rows = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
                                            Q.kCGNullWindowID) or []
        for w in rows:
            if int(w.get("kCGWindowLayer", -1)) != 0 or float(w.get("kCGWindowAlpha", 1.0)) <= 0.0:
                continue
            b = w.get("kCGWindowBounds") or {}
            if float(b.get("Width", 0)) < 50 or float(b.get("Height", 0)) < 50:
                continue
            return int(w["kCGWindowOwnerPID"])
        return None
    except Exception:  # noqa: BLE001 - no answer
        return None


def window_server_front() -> Optional[str]:
    """The bundle id of ``window_server_front_pid``'s app, or None. An app in front with no window of its own is not
    seen: the source then names another app, and an activation check fails closed rather than confirming."""
    pid = window_server_front_pid()
    if pid is None:
        return None
    try:
        return _bundle_of(_appkit().NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))
    except Exception:  # noqa: BLE001 - no answer
        return None


def frontmost_reading() -> Dict[str, Any]:
    """Which app is in front, and which source said so (``FRONTMOST_SOURCES``, in that order):
    ``{"bundle_id", "front_source", "reads": {source: bundle id | None}, "conflict"}``. The AX focus decides whenever it
    answers; else the run-loop-ticked workspace read and the menu-bar owner: one answering, or both agreeing, is the
    answer (``front_source`` the first that answered); both answering differently is a ``conflict`` (no answer); none
    answering is no answer. Reads only."""
    ax = frontmost_bundle_live()
    reads: Dict[str, Optional[str]] = {"ax": ax}
    if ax:
        return {"bundle_id": ax, "front_source": "ax", "reads": reads, "conflict": False}
    ws, mb = workspace_frontmost_ticked(), menubar_owner()
    reads.update(nsworkspace_tick=ws, menubar_owner=mb)
    if ws and mb and ws != mb:
        return {"bundle_id": None, "front_source": None, "reads": reads, "conflict": True}
    if ws or mb:
        return {"bundle_id": ws or mb, "front_source": "nsworkspace_tick" if ws else "menubar_owner", "reads": reads,
                "conflict": False}
    wsrv = window_server_front()            # off the main thread the only live source (FRONTMOST_SOURCES)
    reads["window_server"] = wsrv
    if wsrv:
        return {"bundle_id": wsrv, "front_source": "window_server", "reads": reads, "conflict": False}
    return {"bundle_id": None, "front_source": None, "reads": reads, "conflict": False}


def focused_window_info(pid: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The focused window of ``pid`` (or of the system-wide focused app when None):
    ``{"pid", "title", "frame", "focus"}`` - ``title`` "" when the app has no focused window,
    ``frame`` its (position, size), ``focus`` the focused element's (role, subrole). None when
    the AX API could not answer: the caller treats the window as unknown. A few attribute
    reads, no tree walk."""
    try:
        A = _ax()
        if pid is None:
            err, app = A.AXUIElementCopyAttributeValue(A.AXUIElementCreateSystemWide(),
                                                       "AXFocusedApplication", None)
            if err != _AX_SUCCESS or app is None:
                return None
            err, pid = A.AXUIElementGetPid(app, None)
            if err != _AX_SUCCESS:
                return None
        else:
            app = A.AXUIElementCreateApplication(int(pid))
        err, win = A.AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
    except Exception:  # noqa: BLE001
        return None
    if err == _AX_NO_VALUE or (err == _AX_SUCCESS and win is None):
        return {"pid": int(pid), "title": "", "frame": None, "focus": None}
    if err != _AX_SUCCESS:
        return None
    focused = _attr(app, "AXFocusedUIElement")
    focus = ((_s(_attr(focused, "AXRole")), _s(_attr(focused, "AXSubrole")))
             if focused is not None else None)
    return {"pid": int(pid), "title": _s(_attr(win, "AXTitle")) or "",
            "frame": (_ax_pair(_attr(win, "AXPosition"), "point"), _ax_pair(_attr(win, "AXSize"), "size")),
            "focus": focus}


def focused_window_has_secure_field(pid: Optional[int], max_elements: int = 2500,
                                    max_depth: int = 30) -> bool:
    """Does the app's focused window show a secure field (role or subrole
    ``AXSecureTextField``)? Reads roles, subroles and children only, at most
    ``max_elements`` elements (a larger window is judged by those). No focused window:
    False. An unknown app (``pid`` None): True. Raises ``AXScanError`` when the AX API does
    not answer - the callers withhold then."""
    if pid is None:
        return True
    A = _ax()
    try:
        err, win = A.AXUIElementCopyAttributeValue(A.AXUIElementCreateApplication(int(pid)),
                                                   "AXFocusedWindow", None)
    except Exception as exc:  # noqa: BLE001
        raise AXScanError(f"{type(exc).__name__}") from exc
    if err == _AX_NO_VALUE or (err == _AX_SUCCESS and win is None):
        return False
    if err != _AX_SUCCESS:
        raise AXScanError(f"AX error {err}")
    stack, seen = [(win, 0)], 0
    while stack and seen < max_elements:
        el, depth = stack.pop()
        seen += 1
        if SECURE_ROLE in (_s(_attr(el, "AXRole")), _s(_attr(el, "AXSubrole"))):
            return True
        if depth >= max_depth:
            continue
        kids = _attr(el, "AXChildren")
        try:
            stack.extend((k, depth + 1) for k in (list(kids) if kids is not None else []))
        except Exception:  # noqa: BLE001
            continue
    return False


def ax_trusted() -> bool:
    """True iff this process has macOS Accessibility (TCC) trust granted.

    Dynamic — re-checks every call, since the user can grant the permission while
    the process runs (e.g. after the AnyUse Driver bundle is enabled in System
    Settings). Returns False (never raises) when the framework is absent.
    """
    if not AX_AVAILABLE:
        return False
    try:
        return bool(_ax().AXIsProcessTrusted())
    except Exception:
        return False


_EXCLUDE_MENUBAR_ENV = "ANYUSE_DRIFT_EXCLUDE_MENUBAR"


#: the identifier AppKit gives the entries of a recent-documents menu (Ablage > Benutzte Dokumente, the Apple menu's
#: recent items): the list changes whenever ANY document is opened
VOLATILE_MENU_IDENTIFIERS = ("_recentItemRequested:",)


def volatile_anchor(desc: Any) -> bool:
    """A menu entry that never anchors a recorded fingerprint: TextEdit's recent
    documents were 30 of a recording's 325 required elements, and every document opened since the recording made
    them "missing", so the replay drifted at its first step. Matched by AppKit's identifier, in the menu bar only."""
    return bool(getattr(desc, "in_menubar", False)) and str(getattr(desc, "identifier", "") or "") in VOLATILE_MENU_IDENTIFIERS


def drift_excludes_menubar() -> bool:
    """OPT-IN (default off): leave menu-bar descendants out of a fingerprint's
    required anchors."""
    from anyuse.settings import get as get_setting
    return bool(get_setting(_EXCLUDE_MENUBAR_ENV))


def _drive_enabled() -> bool:
    """The keystone actuation gate (env ANYUSE_DRIVE_ENABLED). Enforced inside the
    AX actuation primitives below so NO real AX click/type fires when off — covering
    every caller (executor legacy path, MacOSPlatform.perform_verb, …). Test spies
    replace AccessibilityTree, so they bypass this and stay unaffected."""
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_DRIVE_ENABLED"))


def _interlock(where: str) -> Optional[str]:
    """The foreground interlock (execution/foreground_interlock.py): None when
    input may be sent now, else the refusal message. Checked after the drive
    gate in every AX actuation primitive below."""
    from anyuse.execution.foreground_interlock import refusal
    return refusal(where)


def _attr(element, attr: str):
    """AXUIElementCopyAttributeValue(element, attr) -> value, or None on error."""
    try:
        err, val = _ax().AXUIElementCopyAttributeValue(element, attr, None)
        return val if err == _AX_SUCCESS else None
    except Exception:
        return None


def _s(v) -> Optional[str]:
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return None


def _take(counter: Counter, value) -> bool:
    """Consume one ``value`` from ``counter`` (a value→count multiset).

    Returns True if ``value`` had remaining count (and decrements it), else
    False. Used to select, in original snapshot order, the elements whose value
    is part of a key bucket's surplus (added/removed/changed) rather than its
    stable matched pairs.
    """
    if counter.get(value, 0) > 0:
        counter[value] -= 1
        return True
    return False


# ── Canonical roles (cross-platform vocabulary) ──────────────────────────────
# Selectors store a CANONICAL role, not the raw 'AXButton', so a Windows UIA
# resolver can map its ControlTypes into the same vocabulary. raw_role keeps the
# platform original for the native resolver.
_CANONICAL_ROLE = {
    "AXButton": "button", "AXMenuButton": "button", "AXPopUpButton": "popupbutton",
    "AXMenuItem": "menuitem", "AXMenuBarItem": "menuitem", "AXMenu": "menu",
    "AXTextField": "textfield", "AXTextArea": "textarea", "AXSecureTextField": "passwordfield",
    "AXComboBox": "combobox", "AXSearchField": "searchfield",
    "AXCheckBox": "checkbox", "AXRadioButton": "radiobutton", "AXSwitch": "switch",
    "AXSlider": "slider", "AXStepper": "stepper", "AXIncrementor": "stepper",
    "AXLink": "link", "AXStaticText": "text", "AXHeading": "heading",
    "AXImage": "image", "AXTab": "tab", "AXTabGroup": "tabgroup",
    "AXRow": "row", "AXCell": "cell", "AXColumn": "column",
    "AXList": "list", "AXTable": "table", "AXOutline": "outline",
    "AXGroup": "group", "AXScrollArea": "scrollarea", "AXSplitGroup": "splitgroup",
    "AXToolbar": "toolbar", "AXWindow": "window", "AXSheet": "dialog", "AXDialog": "dialog",
    "AXDisclosureTriangle": "disclosure",
}


def canonical_role(ax_role: Optional[str]) -> str:
    """Map a raw AX role to the portable canonical vocabulary."""
    if not ax_role:
        return "unknown"
    if ax_role in _CANONICAL_ROLE:
        return _CANONICAL_ROLE[ax_role]
    r = ax_role[2:] if ax_role.startswith("AX") else ax_role  # AXFooBar -> FooBar
    return r.lower() or "unknown"


@dataclass
class Selector:
    """A portable, position-independent way to find a UI element again.

    The MODEL (canonical_role + name + identifier + path) is cross-platform; only
    ``raw_role`` is platform-specific. ``is_durable`` is the abstain signal: a
    role+path with no name/identifier is fragile, so the caller should persist an
    OCR-anchor or coordinate instead of a fabricated selector (esp. Electron/web).

    ``app`` and ``document`` say WHERE the element
    was: the app's bundle id and the full ``AXDocument`` path or URL of the
    window it sat in (never the basename: two documents of the same name in two
    folders are two targets). Both are optional and only serialized when set, so
    a selector recorded before them is read and written unchanged. The executor
    checks both immediately before acting.

    A recording also knows
    about the element's PLACE, each optional and serialized only when set:

    * ``scope``: the container a document-less window shows (``context/container_scope.py``:
      a Finder folder, a Notes folder), a dict or its salted ``scope_key``;
    * ``menu_path``: for an element of the menu bar, the titles from its menu-bar item down to
      its parent menu item (``[]`` for a menu-bar item itself); a namesake in another menu is
      another element;
    * ``namesakes``: how many OTHER elements of its window (or of its menu) had the same role,
      identifier and name when it was recorded. Such twins are told apart by position only, and
      position is not identity: a recording with namesakes is refused at replay;
    * ``shared_id``: how many other elements of its window (or menu) carried its identifier.
      An identifier several elements share is not an identity; the name must match too.
    """
    canonical_role: str = "unknown"
    name: str = ""           # human-visible label (AXTitle / AXValue)
    identifier: str = ""     # AXIdentifier — most stable when present
    path: str = ""           # role-index path from an ancestor; disambiguates duplicates
    raw_role: str = ""       # original platform role (AXButton / UIA ControlType)
    app: str = ""            # bundle id of the app the element belonged to
    document: str = ""       # full AXDocument path/URL of the element's window
    scope: Any = None        # the window's container scope (dict or scope_key), else None
    menu_path: Optional[List[str]] = None   # menu titles above a menu-bar element
    namesakes: Optional[int] = None         # identical others in its window / menu
    shared_id: Optional[int] = None         # others in its window / menu with its identifier

    def is_durable(self) -> bool:
        return bool(self.identifier or self.name)

    def describe(self) -> str:
        bits = [self.canonical_role]
        if self.identifier:
            bits.append(f"#{self.identifier}")
        elif self.name:
            bits.append(f"'{self.name}'")
        if self.path:
            bits.append(f"@{self.path}")
        out = ":".join(bits)
        if self.app:
            out += f" in {self.app}"
        if self.document:
            out += f" doc={normalize_document(self.document)}"
        return out

    def to_dict(self) -> dict:
        d = {"canonical_role": self.canonical_role, "name": self.name,
             "identifier": self.identifier, "path": self.path, "raw_role": self.raw_role}
        if self.app:
            d["app"] = self.app
        if self.document:
            d["document"] = self.document
        if self.scope:
            d["scope"] = dict(self.scope) if isinstance(self.scope, dict) else self.scope
        if self.menu_path is not None:
            d["menu_path"] = list(self.menu_path)
        if self.namesakes is not None:
            d["namesakes"] = int(self.namesakes)
        if self.shared_id is not None:
            d["shared_id"] = int(self.shared_id)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Selector":
        mp = d.get("menu_path")

        def _int(v):
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return cls(canonical_role=d.get("canonical_role", "unknown"), name=d.get("name", ""),
                   identifier=d.get("identifier", ""), path=d.get("path", ""), raw_role=d.get("raw_role", ""),
                   app=d.get("app", "") or "", document=d.get("document", "") or "",
                   scope=d.get("scope") or None,
                   menu_path=[str(t) for t in mp] if isinstance(mp, (list, tuple)) else None,
                   namesakes=_int(d.get("namesakes")), shared_id=_int(d.get("shared_id")))


def normalize_document(doc: Optional[str]) -> str:
    """One comparable form of a window's document: a ``file://`` URL becomes its
    (unquoted) absolute path, a trailing slash is dropped; other URLs are kept
    whole. ``""`` for none. Never a basename."""
    if not doc:
        return ""
    text = str(doc).strip()
    if text.lower().startswith("file://"):
        from urllib.parse import unquote, urlparse
        text = unquote(urlparse(text).path) or text
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _is_document_key(doc: Optional[str]) -> bool:
    """A full document key (a path or a URL), as opposed to a bare file name or
    window title, which only ever identifies a window by its basename."""
    return bool(doc) and ("/" in str(doc))


#: a document recorded as a salted key instead of its path or URL (the watcher's structured
#: capture never stores an AXDocument): ``dockey:<salt id>:<HMAC-SHA256 of the normalized
#: document>``. The salt lives under the secrets dir; a key from another machine's salt
#: matches nothing here
DOCUMENT_KEY_PREFIX = "dockey:"
DOCUMENT_SALT_FILE = "document_key.salt"


def _document_salt(create: bool = False) -> Optional[bytes]:
    from anyuse.paths import anyuse_secret_path
    path = anyuse_secret_path(DOCUMENT_SALT_FILE)
    try:
        data = path.read_bytes()
        return data if len(data) >= 16 else None
    except FileNotFoundError:
        pass
    except OSError:
        return None
    if not create:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(os.urandom(32))
    except FileExistsError:
        pass
    except OSError:
        return None
    return _document_salt(create=False)


def _salt_id(salt: bytes) -> str:
    import hashlib
    return hashlib.sha256(b"anyuse document salt id\0" + salt).hexdigest()[:8]


def document_key(doc: Optional[str], create: bool = False) -> str:
    """``doc`` (an AXDocument path or URL) as a salted key, or "" (no document, no salt).
    ``create`` makes this machine's salt when there is none yet."""
    import hashlib
    import hmac
    norm = normalize_document(doc)
    if not norm or not _is_document_key(norm):
        return ""
    salt = _document_salt(create=create)
    if salt is None:
        return ""
    digest = hmac.new(salt, norm.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{DOCUMENT_KEY_PREFIX}{_salt_id(salt)}:{digest}"


def is_document_key(doc: Optional[str]) -> bool:
    return bool(doc) and str(doc).startswith(DOCUMENT_KEY_PREFIX)


def document_key_is_local(doc: Optional[str]) -> bool:
    """Was this key made with this machine's salt?"""
    salt = _document_salt(create=False)
    parts = str(doc or "").split(":")
    return salt is not None and is_document_key(doc) and len(parts) == 3 and parts[1] == _salt_id(salt)


def same_document(want: Optional[str], have: Optional[str], window_title: Optional[str] = None) -> bool:
    """Does a window showing ``have`` (its ``AXDocument``) with ``window_title``
    show the document ``want``? A full path/URL must match the full path/URL
    (a basename would make ``/a/notes.txt`` and ``/b/notes.txt`` the same
    document). A bare name keeps the old, weaker rule (basename or title). A
    salted key (``document_key``) matches the live document's key."""
    if not want:
        return False
    if is_document_key(want):
        return bool(have) and document_key(have) == want
    if _is_document_key(want):
        return bool(have) and normalize_document(want) == normalize_document(have)
    base = os.path.basename(str(want).rstrip("/"))
    got = os.path.basename(normalize_document(have)) if have else ""
    return bool(base) and base in (got, window_title or "")


def path_within_window(path: str) -> str:
    """The part of a role-index path below its window (``window[k]/``): the
    window's index is its place in the app's z-order, which says nothing about
    which window it is, so it never decides between two candidates."""
    if not path:
        return ""
    parts = path.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "window" or parts[i].startswith("window["):
            return "/".join(parts[i + 1:])
    return path


#: canonical roles whose AXValue is content a step edits, never their identity
EDITABLE_TEXT_ROLES = frozenset({"textfield", "textarea", "searchfield", "combobox", "passwordfield"})


def selector_from_attrs(role: Optional[str] = None, title: Optional[str] = None,
                        value: Optional[str] = None, identifier: Optional[str] = None,
                        path: str = "", description: Optional[str] = None,
                        app: Optional[str] = None, document: Optional[str] = None) -> Selector:
    """Build a Selector from raw AX attribute values (pure — unit-testable).

    ``description`` is ``AXDescription`` and ranks between the title and the
    value, because on macOS it is very often the ONLY human label a control has.
    Measured: every one of Calculator's 25 buttons
    has ``AXTitle = None`` and carries its label solely in ``AXDescription``
    (``'Addieren'``, ``'Ist gleich'``, …). Without it, such a control produced a
    selector whose ``name`` was empty, so ``is_durable()`` was False whenever the
    app also lacked an identifier, and ``find_selector`` had nothing but the role
    to match on — it would return the FIRST button of that role, i.e. the wrong
    one.
    """
    canon = canonical_role(role)
    # an editable text element's VALUE is what a step writes into it, not which element it
    # is. A Finder rename field recorded empty was "another element" once the replay had
    # typed into it, so the Return that commits the name was refused. Such an element is
    # identified by its label, identifier and path only.
    name = (title or description or ("" if canon in EDITABLE_TEXT_ROLES else value) or "").strip()
    return Selector(canonical_role=canon, name=name,
                    identifier=(identifier or "").strip(), path=path, raw_role=role or "",
                    app=(app or "").strip(), document=(document or "").strip())


def _ax_pair(axvalue, kind: str) -> Optional[Tuple[float, float]]:
    """Extract (a,b) from an AXValue holding a CGPoint/CGSize, best-effort.

    kind ∈ {"point","size"}. Tries the proper AXValueGetValue API, then falls back
    to parsing the repr (``x:.. y:..`` / ``w:.. h:..``). Returns None on failure —
    position/size are disambiguators, never load-bearing.
    """
    if axvalue is None:
        return None
    try:
        import ApplicationServices as A
        # pyobjc REQUIRES the out-arg be None (passing a CGPoint()/CGSize() raises
        # "valuePtr should be None"); the typed value comes back in the tuple.
        if kind == "point":
            ok, v = A.AXValueGetValue(axvalue, A.kAXValueCGPointType, None)
            if ok and v is not None:
                return (float(v.x), float(v.y))
        else:
            ok, v = A.AXValueGetValue(axvalue, A.kAXValueCGSizeType, None)
            if ok and v is not None:
                return (float(v.width), float(v.height))
    except Exception:
        pass
    try:
        import re as _re
        keys = (r"x:([\d.\-]+)\s+y:([\d.\-]+)" if kind == "point"
                else r"w:([\d.\-]+)\s+h:([\d.\-]+)")
        m = _re.search(keys, str(axvalue))
        if m:
            return (float(m.group(1)), float(m.group(2)))
    except Exception:
        pass
    return None


@dataclass
class AXElementDesc:
    """A serializable description of one AX element (for snapshots/diffs)."""
    role: Optional[str] = None
    title: Optional[str] = None
    value: Optional[str] = None
    role_desc: Optional[str] = None
    identifier: Optional[str] = None   # AXIdentifier — stable dev-assigned id when present
    #: AXDescription — on macOS frequently the ONLY human label a control has.
    #: Distinct from ``role_desc`` (AXRoleDescription), which is the *kind* of
    #: control ("button" / "Taste") and says nothing about which one.
    ax_description: Optional[str] = None
    position: Optional[Tuple[float, float]] = None  # (x, y) logical points, top-left
    size: Optional[Tuple[float, float]] = None      # (w, h) logical points
    #: True for every descendant of an AXMenuBar. Measured: 141-309 of an
    #: app's AXMenuItems live in the menu bar, i.e.
    #: most of every "actionable" set, and a menu's items only exist in the tree
    #: after that menu was opened once — so a fingerprint recorded right after a
    #: menu was used fires the drift gate on every later replay
    #: (`required_missing`). See ANYUSE_DRIFT_EXCLUDE_MENUBAR.
    in_menubar: bool = False
    #: a password field: AXSecureTextField, or (a web password input) an AXTextField whose
    #: subrole is AXSecureTextField. Its value is already empty (``_describe``).
    secure: bool = False

    def key(self) -> Tuple:
        return (self.role, self.title or self.ax_description, self.role_desc)

    def label(self) -> str:
        """The human-visible label: AXTitle, else AXDescription, else AXValue."""
        return (self.title or self.ax_description or self.value or "")

    def text(self) -> str:
        return " ".join(t for t in (self.role_desc or self.role, self.title,
                                    self.ax_description, self.value) if t)

    def to_selector(self, path: str = "") -> "Selector":
        """The portable selector for this element (pure; no AX calls)."""
        return selector_from_attrs(role=self.role, title=self.title, value=self.value,
                                   identifier=self.identifier, path=path,
                                   description=self.ax_description)

    @property
    def description(self) -> Optional[str]:
        """The element's own description (AXDescription), falling back to the role
        description. Callers (the recorder) use this as a LABEL; the role
        description alone is "Taste" for every button in the app, never
        "Addieren"."""
        return self.ax_description or self.role_desc

    def center(self) -> Optional[Tuple[float, float]]:
        """Center point (logical points) from position+size, or None if unknown."""
        if self.position and self.size:
            return (self.position[0] + self.size[0] / 2.0, self.position[1] + self.size[1] / 2.0)
        return None


# ---------------------------------------------------------------------------
# Role sets — ONE public source of truth for the whole codebase
# ---------------------------------------------------------------------------
# ONE "actionable" set: guides/generator.py and execution/planner.py import it.
# The set feeds the generator's state-id hash, so a second, disagreeing set
# gives the same screen different ids in the recorder and the generator, and
# the graph nodes never merge.
#
# Membership decision, recorded so the next reader does not have to guess:
# this set was kept and the generator's local set discarded. The generator's
# set additionally contained AXTabGroup (a container, not a control) and
# AXCell (table content, not a control) — both deliberately dropped, because an
# AXCell anchor turns every row of a list into a "required element", which on a
# busy screen is exactly what makes the drift gate misfire. The
# roles this set gains over the generator's are AXMenuButton, AXSecureTextField,
# AXSearchField, AXStepper and AXDisclosureTriangle — all genuine controls.
ACTIONABLE_ROLES = frozenset({
    "AXButton", "AXMenuButton", "AXPopUpButton", "AXMenuItem", "AXMenuBarItem",
    "AXTextField", "AXTextArea", "AXSecureTextField", "AXSearchField", "AXComboBox",
    "AXCheckBox", "AXRadioButton", "AXSwitch", "AXSlider", "AXStepper", "AXIncrementor",
    "AXLink", "AXTab", "AXDisclosureTriangle",
})

# Structural container roles that carry no semantics of their own. The only
# consumer is planner.build_perception_context's ``ax_generic_ratio``, whose
# purpose is to notice "this AX tree tells us nothing" — which is how an
# Electron app or an unlabelled web view looks. The membership below is an
# inference from that single counter, not a recovered intention: if the ratio
# ever drives a decision rather than describing one, re-derive it from data.
GENERIC_ROLES = frozenset({
    "AXGroup", "AXUnknown", "AXSplitGroup", "AXScrollArea",
    "AXLayoutArea", "AXLayoutItem",
})

# Back-compat alias for the old private name. Do not use it in new code.
_ACTIONABLE_ROLES = ACTIONABLE_ROLES


@dataclass
class AXSnapshot:
    """A flattened snapshot of an app's AX element descriptors at a point in time."""
    elements: List[AXElementDesc] = field(default_factory=list)
    app_name: str = ""

    def texts(self) -> List[str]:
        return [e.text() for e in self.elements]

    @property
    def root_elements(self) -> List[AXElementDesc]:
        """All captured elements (used for counts + role-frequency)."""
        return self.elements

    def to_json(self, max_elements: int = 300) -> str:
        """A compact JSON list for a model prompt: role (without ``AX``), title, description,
        identifier and a value of at most 80 characters per element; containers without a
        label are left out. Secure-field values are already empty (``_describe``)."""
        import json as _json
        out = []
        for e in self.elements:
            if len(out) >= max_elements:
                break
            role = (e.role or "")[2:] if (e.role or "").startswith("AX") else (e.role or "")
            if role in ("Group", "SplitGroup", "ScrollArea", "LayoutArea", "Unknown", "") \
                    and not (e.title or e.ax_description):
                continue
            item = {"r": role}
            for key, val in (("t", e.title), ("d", e.ax_description), ("id", e.identifier),
                             ("v", (e.value or "")[:80])):
                if val:
                    item[key] = val
            out.append(item)
        return _json.dumps({"app": self.app_name, "elements": out}, ensure_ascii=False,
                           separators=(",", ":"))

    @property
    def anchor_elements(self) -> List[AXElementDesc]:
        """The elements a recorded fingerprint REQUIRES: the actionable ones, never a volatile menu
        entry (``volatile_anchor``), minus the menu bar when ``ANYUSE_DRIFT_EXCLUDE_MENUBAR`` is on."""
        els = [e for e in self.actionable_elements if not volatile_anchor(e)]
        if drift_excludes_menubar():
            els = [e for e in els if not e.in_menubar]
        return els

    @property
    def actionable_elements(self) -> List[AXElementDesc]:
        """The interactive subset (buttons/fields/links/…) used as required anchors."""
        return [e for e in self.elements if (e.role or "") in _ACTIONABLE_ROLES]


@dataclass
class AXDiff:
    """The difference between two AX snapshots (what appeared / vanished / changed)."""
    added: List[AXElementDesc] = field(default_factory=list)
    removed: List[AXElementDesc] = field(default_factory=list)
    changed: List[Tuple[AXElementDesc, AXElementDesc]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_summary(self) -> str:
        if self.is_empty():
            return "no AX changes"
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)} added: "
                         + "; ".join(e.text()[:60] for e in self.added[:5]))
        if self.removed:
            parts.append(f"-{len(self.removed)} removed: "
                         + "; ".join(e.text()[:60] for e in self.removed[:5]))
        if self.changed:
            parts.append(f"~{len(self.changed)} changed: "
                         + "; ".join(f"{b.title!r}:{b.value!r}->{a.value!r}"
                                     for b, a in self.changed[:5]))
        return " | ".join(parts)

    def clearly_matches(self, expected: Optional[str]) -> bool:
        """Heuristic: does the post-change evidence clearly contain ``expected``?"""
        if not expected or not expected.strip():
            return False
        needle = expected.strip().lower()
        for e in self.added:
            if needle in e.text().lower():
                return True
        for _before, after in self.changed:
            if needle in after.text().lower():
                return True
        return False


@dataclass
class SetValueEvidence:
    """What could be OBSERVED after one attempt to put text into an element.

    ``accepted`` is the old return value (the AX server took the call). The rest
    is evidence: the value read back, the character count read back, and which
    window/document the element belongs to. ``independent`` is filled by
    :meth:`AccessibilityTree.enter_text` when the caller supplied an accessor
    that does not go through the UI at all (a file on disk, a database row),
    and is the only field that proves the application kept the text.
    ``bool(evidence)`` is the read-back check — deliberately NOT the independent
    one, so a caller who wants proof has to ask for it by name.
    """
    method: str = "ax_value"           # ax_value | ax_selected_text | keystrokes
    requested: str = ""
    accepted: bool = False
    readback: Optional[str] = None
    readback_matches: bool = False
    nchars: Optional[int] = None
    nchars_matches: Optional[bool] = None
    window_title: Optional[str] = None
    document: Optional[str] = None
    independent: Optional[bool] = None
    stuck_modifiers: List[str] = field(default_factory=list)
    refused: bool = False              # the rung declined to act (focus not held)
    error: str = ""

    @property
    def verified(self) -> bool:
        """Accepted, reads back equal, and the character count (where the
        element reports one) agrees."""
        return bool(self.accepted and self.readback_matches
                    and self.nchars_matches is not False)

    def __bool__(self) -> bool:
        return self.verified

    def to_dict(self) -> dict:
        return {"method": self.method, "accepted": self.accepted,
                "readback_matches": self.readback_matches, "nchars": self.nchars,
                "nchars_matches": self.nchars_matches, "verified": self.verified,
                "independent": self.independent, "window_title": self.window_title,
                "document": self.document, "stuck_modifiers": list(self.stuck_modifiers),
                "refused": self.refused, "error": self.error}


@dataclass
class TextEntryResult:
    """The outcome of :meth:`AccessibilityTree.enter_text`: which rung served,
    the evidence of every rung tried, and whether an independent accessor
    confirmed the text. ``wrong_target`` means the element belonged to a
    different document than the caller named and NOTHING was written."""
    requested: str = ""
    rung: str = ""                     # the rung that served, "" if none did
    rungs: List[SetValueEvidence] = field(default_factory=list)
    verified_independently: bool = False
    wrong_target: bool = False
    window_raised: Optional[bool] = None   # the element's window was (made) the focused one
    window_title: Optional[str] = None
    document: Optional[str] = None
    error: str = ""

    @property
    def success(self) -> bool:
        return bool(self.rung) and not self.wrong_target

    @property
    def refused_rungs(self) -> int:
        """Rungs that declined to act because the element did not hold focus."""
        return sum(1 for r in self.rungs if r.refused)

    def __bool__(self) -> bool:
        return self.success

    @property
    def fall_throughs(self) -> int:
        """Rungs that were tried and did not serve."""
        return max(0, len(self.rungs) - (1 if self.rung else 0))

    def to_dict(self) -> dict:
        return {"rung": self.rung, "success": self.success,
                "verified_independently": self.verified_independently,
                "wrong_target": self.wrong_target, "fall_throughs": self.fall_throughs,
                "refused_rungs": self.refused_rungs, "window_raised": self.window_raised,
                "window_title": self.window_title, "document": self.document,
                "rungs": [r.to_dict() for r in self.rungs], "error": self.error}


class _PlaceFilter:
    """Does a live candidate sit where the recording's element sat?

    * a recorded ``menu_path``: only a menu-bar element under the same menu titles (a namesake
      in another submenu, or the menu-bar item named like a menu item, is another element);
    * recorded in a window (a ``scope`` or, for a recording that read its place, no menu path):
      no menu-bar element;
    * a recorded container ``scope``: not a candidate whose window shows a DIFFERENT container
      (``container_scope.same_scope``); an unconfirmed one stays a candidate, and the executor
      decides before acting whether it may act on it (only outside the container's content).
    A selector recorded before these fields (none set) keeps every candidate, as before."""

    def __init__(self, tree: "AccessibilityTree", selector: Selector):
        self.tree = tree
        self.sel = selector
        self.read_place = (selector.namesakes is not None or selector.menu_path is not None
                           or bool(selector.scope))
        self._verdicts: List[Tuple[Any, str]] = []

    def _verdict(self, win) -> str:
        from anyuse.context import container_scope as CS
        for w, v in self._verdicts:
            if w == win:
                return v
        live = CS.container_scope(self.tree, win, self.sel.app or self.tree.bundle_id())
        v = CS.same_scope(self.sel.scope, live)
        self._verdicts.append((win, v))
        return v

    def keeps(self, el, desc: "AXElementDesc") -> bool:
        if not self.read_place:
            return True
        in_menu = bool(getattr(desc, "in_menubar", False))
        if self.sel.menu_path is not None:
            return in_menu and self.tree._menu_path(el) == list(self.sel.menu_path)
        if in_menu:
            return False
        if self.sel.scope:
            from anyuse.context import container_scope as CS
            win = self.tree._window_element(el)
            return win is not None and self._verdict(win) != CS.DIFFERENT
        return True


class AccessibilityTree:
    """Walk / query / act on the macOS AX tree of an app (or system-wide)."""

    def __init__(self, pid: Optional[int] = None):
        self._pid = pid

    def _root(self):
        A = _ax()
        if self._pid is not None:
            return A.AXUIElementCreateApplication(int(self._pid))
        return A.AXUIElementCreateSystemWide()

    def _children(self, element) -> List:
        kids = _attr(element, "AXChildren")
        try:
            return list(kids) if kids is not None else []
        except Exception:
            return []

    def attr(self, element, name: str):
        """One AX attribute of ``element`` (None when unreadable). A read; the public reader
        ``context/container_scope.py`` uses."""
        return _attr(element, name)

    def children(self, element) -> List:
        """``element``'s AXChildren (empty when unreadable). A read."""
        return self._children(element)

    def _describe(self, element) -> AXElementDesc:
        role = _s(_attr(element, "AXRole"))
        # Never surface a secure field's VALUE (a typed password / token) — it would
        # otherwise flow verbatim into the planner/intervention LLM prompts. Redact
        # at the source so the snapshot can never leak it. A web password input is an
        # AXTextField whose SUBROLE is AXSecureTextField; the subrole is read for text
        # fields only (one more attribute read, not one per element).
        secure = role == SECURE_ROLE or (
            role == "AXTextField" and _s(_attr(element, "AXSubrole")) == SECURE_ROLE)
        value = "" if secure else _s(_attr(element, "AXValue"))
        return AXElementDesc(
            role=role,
            title=_s(_attr(element, "AXTitle")),
            value=value,
            role_desc=_s(_attr(element, "AXRoleDescription")),
            identifier=_s(_attr(element, "AXIdentifier")),
            ax_description=_s(_attr(element, "AXDescription")),
            secure=secure,
        )

    # ── rows of a list, table or outline (execution/ax_actions.py select_row) ─────────────
    # A row target is a selector of kind ``row``: its list's identity and its label (or its
    # own identifier). It is resolved here (``find_row``), never through ``find_selector`` /
    # ``to_selector``, so a row recorded before (by its path and the reuse id every row of
    # an AppKit table shares) resolves exactly as it did. Row attributes are read only when a
    # row is looked for or listed (``snapshot(row_labels=True)``), not on every walk.

    #: rows read from one list at most; a longer list is refused (a twin could hide in the rest)
    ROW_SCAN_CAP = 500
    #: elements read while looking for lists (their rows are not walked); more is refused
    CONTAINER_SCAN_CAP = 4000
    #: why the last ``find_row`` returned None: ``{"code", "why"}``
    last_row_problem = None

    def _first_text(self, element, max_depth: int) -> Optional[str]:
        """The first text below ``element`` in document order: an AXStaticText's or a
        non-secure AXTextField's value, or a cell's title or description."""
        stack = [(c, 1) for c in reversed(self._children(element))]
        while stack:
            el, depth = stack.pop()
            role = _s(_attr(el, "AXRole"))
            text = None
            if role in ("AXStaticText", "AXTextField") and _s(_attr(el, "AXSubrole")) != SECURE_ROLE:
                text = _s(_attr(el, "AXValue"))
            elif role == "AXCell":
                text = _s(_attr(el, "AXTitle")) or _s(_attr(el, "AXDescription"))
            if text and text.strip():
                return text.strip()
            if depth < max_depth:
                stack.extend((c, depth + 1) for c in reversed(self._children(el)))
        return None

    def _row_label(self, element) -> Optional[str]:
        """A row's label: its own AXTitle / AXDescription, else the text of its FIRST cell
        (Finder's name column, Notes' title cell); a row without cells: its first text. A
        read; None when that shows no text."""
        own = _s(_attr(element, "AXTitle")) or _s(_attr(element, "AXDescription"))
        if own and own.strip():
            return own.strip()
        cells = [c for c in self._children(element) if _s(_attr(c, "AXRole")) == "AXCell"]
        if cells:
            own = _s(_attr(cells[0], "AXTitle")) or _s(_attr(cells[0], "AXDescription"))
            if own and own.strip():
                own = own.strip()
                text = self._first_text(cells[0], 3)
                # a cell whose description SUMMARISES its own texts is labelled by its first text -- Notes' folder
                # cell describes itself "Museum Trip Notes, 4 Notizen" around the name's text field, so a folder
                # select_row would be not_found; a description that does not begin
                # with the cell's first text is the label itself, as before
                if text and text != own and own.startswith(text) and own[len(text):len(text) + 1] in (",", ";", " ", "("):
                    return text
                return own
            return self._first_text(cells[0], 3)
        return self._first_text(element, 3)

    def _row_container_element(self, element, max_up: int = 6):
        """The nearest AXTable / AXOutline / AXList above a row, or None."""
        cur = _attr(element, "AXParent")
        for _ in range(max_up):
            if cur is None:
                return None
            if _s(_attr(cur, "AXRole")) in ROW_CONTAINER_ROLES:
                return cur
            cur = _attr(cur, "AXParent")
        return None

    def container_identity(self, box) -> Dict[str, Any]:
        """A list's identity: its canonical role, AXIdentifier, AXDescription, role-index path
        below its window, and its column count (ax_actions.container_matches decides by the
        strongest mark; with only a path, the column count must agree too)."""
        cols = _attr(box, "AXColumns")
        try:
            columns = len(list(cols)) if cols is not None else sum(
                1 for c in self._children(box) if _s(_attr(c, "AXRole")) == "AXColumn")
        except Exception:  # noqa: BLE001 - an unreadable column list is no column count
            columns = None
        out: Dict[str, Any] = {"role": canonical_role(_s(_attr(box, "AXRole"))),
                               "identifier": (_s(_attr(box, "AXIdentifier")) or "").strip(),
                               "description": (_s(_attr(box, "AXDescription")) or "").strip(),
                               "path": path_within_window(self._ax_path(box, max_up=12))}
        if columns is not None:
            out["columns"] = columns
        return out

    def row_container_identity(self, element) -> Optional[Dict[str, str]]:
        box = self._row_container_element(element)
        return self.container_identity(box) if box is not None else None

    def _row_containers(self) -> Tuple[List[Any], bool]:
        """Every list, table and outline of the app (not the menu bar), without walking their
        rows; ``truncated`` when more than ``CONTAINER_SCAN_CAP`` elements had to be read."""
        found: List[Any] = []
        stack = [self._root()]
        seen = 0
        while stack:
            el = stack.pop()
            seen += 1
            if seen > self.CONTAINER_SCAN_CAP:
                return found, True
            role = _s(_attr(el, "AXRole"))
            if role in ROW_CONTAINER_ROLES:
                found.append(el)
                continue
            if role == "AXMenuBar":
                continue
            stack.extend(reversed(self._children(el)))
        return found, False

    def _container_rows(self, box) -> Optional[List[Any]]:
        rows = _attr(box, "AXRows")
        if rows is None:
            return [c for c in self._children(box) if _s(_attr(c, "AXRole")) in ROW_ROLES]
        try:
            return list(rows)
        except Exception:  # noqa: BLE001
            return None

    def find_row(self, selector: Dict[str, Any]):
        """The ONE row a ``row`` selector names, or None (``last_row_problem`` says why): the
        list matching the recorded list's identity (exactly one in the app), then in it the row
        with the recorded identifier, else the recorded label. None, two or more lists, a list
        longer than ``ROW_SCAN_CAP``, an app larger than ``CONTAINER_SCAN_CAP`` (a twin could
        hide), and no or several rows all refuse."""
        from anyuse.execution.ax_actions import container_matches
        self.last_row_problem = None

        def fail(code: str, why: str):
            self.last_row_problem = {"code": code, "why": why}
            return None
        want = selector.get("container") if isinstance(selector.get("container"), dict) else None
        name, ident = str(selector.get("name") or ""), str(selector.get("identifier") or "")
        if not want or not (name or ident):
            return fail("identity", "a row needs its list's identity and its label or identifier")
        if not AX_AVAILABLE:
            return fail("no_tree", "the Accessibility framework is not available")
        try:
            boxes, truncated = self._row_containers()
            if truncated:
                return fail("ambiguous", f"the app has more than {self.CONTAINER_SCAN_CAP} elements outside its "
                                         "lists; another list like the recorded one cannot be ruled out")
            hits = [b for b in boxes if container_matches(self.container_identity(b), want)]
            if len(hits) > 1:
                # a list in a window of a DIFFERENT container scope is never the recorded one
                from anyuse.execution.ax_actions import scope_candidates
                hits = scope_candidates(self, selector, hits)
                if not hits:
                    return fail("wrong_container", "every list like the recorded one shows another folder or note "
                                                   "list than the recorded one")
            if not hits:
                return fail("not_found", f"no list matches the recorded one ({want})")
            if len(hits) > 1:
                return fail("ambiguous", f"{len(hits)} lists match the recorded one ({want})")
            rows = self._container_rows(hits[0])
            if rows is None:
                return fail("not_found", "the list's rows cannot be read")
            if len(rows) > self.ROW_SCAN_CAP:
                return fail("ambiguous", f"the list has {len(rows)} rows, more than the {self.ROW_SCAN_CAP} "
                                         "read; a twin could hide in the rest")
            match = [r for r in rows if (_s(_attr(r, "AXIdentifier")) or "") == ident] if ident else []
            what = f"#{ident}"
            if len(match) != 1 and name and (not ident or len(match) > 1):
                # no identifier, or one every row shares (a table's reuse id): the label decides
                match = [r for r in rows if self._row_label(r) == name]
                what = repr(name)
            if not match:
                return fail("not_found", f"no row {what} in the list")
            if len(match) > 1:
                return fail("ambiguous", f"{len(match)} rows {what} in the list")
            return match[0]
        except Exception as e:  # noqa: BLE001 - an unreadable tree finds nothing
            return fail("not_found", f"the rows cannot be read ({e})")

    def select_row(self, element) -> bool:
        """Make ``element`` (a row) the selected row of its list: the list's
        ``AXSelectedRows`` = [row], else the row's ``AXSelected``. Gated like every
        actuator (the drive gate, then the foreground interlock); the caller reads the
        selection back."""
        if not _drive_enabled():
            return False
        if _interlock("ax.select_row"):
            return False
        try:
            A = _ax()
            box = self._row_container_element(element)
            if box is not None and A.AXUIElementSetAttributeValue(box, "AXSelectedRows", [element]) == _AX_SUCCESS:
                return True
            return A.AXUIElementSetAttributeValue(element, "AXSelected", True) == _AX_SUCCESS
        except Exception as e:  # noqa: BLE001
            logger.debug("select_row failed: %s", e)
            return False

    def deselect_row(self, element) -> bool:
        """Take ``element`` out of its list's selection (``AXSelected`` = false). Gated."""
        if not _drive_enabled():
            return False
        if _interlock("ax.deselect_row"):
            return False
        try:
            return _ax().AXUIElementSetAttributeValue(element, "AXSelected", False) == _AX_SUCCESS
        except Exception as e:  # noqa: BLE001
            logger.debug("deselect_row failed: %s", e)
            return False

    def selected_rows(self, element) -> Optional[List[Any]]:
        """The row elements selected in ``element``'s list (``AXSelectedRows``); None when the
        list or its selection cannot be read. A read; compared with ``==`` (CFEqual)."""
        try:
            box = self._row_container_element(element)
            rows = _attr(box, "AXSelectedRows") if box is not None else None
            return list(rows) if rows is not None else None
        except Exception:  # noqa: BLE001
            return None

    def actuation_refusal(self, where: str) -> Optional[str]:
        """Why an actuation would be refused now (the drive gate, then the foreground
        interlock), or None: the same checks every actuating primitive makes, asked first."""
        if not _drive_enabled():
            return "actuation gated (ANYUSE_DRIVE_ENABLED off)"
        return _interlock(where)

    def window_of(self, element):
        """The window ``element`` sits in (``AXWindow``), or None. A read."""
        return _attr(element, "AXWindow") if element is not None else None

    def window_counts(self) -> Optional[Dict[str, int]]:
        """``{windows, sheets}``: the app's ``AXWindows`` and the sheets attached to them; None for a tree
        not bound to an app or an app that answers no window list. A read (a shortcut's read-back:
        ``execution/ax_actions.press_shortcut``)."""
        if self._pid is None:
            return None
        root = self._root()
        wins = _attr(root, "AXWindows")
        if wins is None:
            # no window list (an app without windows may answer none): its children, which always hold the
            # menu bar when the app answers at all
            kids = self._children(root)
            if not kids:
                return None
            wins = [k for k in kids if _s(_attr(k, "AXRole")) == "AXWindow"]
        wins = list(wins)
        sheets = sum(1 for w in wins for k in self._children(w) if _s(_attr(k, "AXRole")) == "AXSheet")
        return {"windows": len(wins), "sheets": sheets}

    def focus_context(self, trigger: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Right before a trigger whose effect focuses a field: the element focused now and the
        window of the trigger's target (resolved read-only by its recorded selector; None for a
        menu item or a target that cannot be resolved). ``ax_actions.resolve_focused`` then
        requires the focus to have moved, into that window."""
        window = None
        if isinstance(trigger, dict) and trigger and not trigger.get("kind"):
            try:
                el = self.find_selector(Selector.from_dict(trigger))
                window = self.window_of(el) if el is not None else None
            except Exception:  # noqa: BLE001 - no window: the window check is skipped
                window = None
        return {"focused": self.focused_element(), "window": window}

    def selected_row_labels(self, element) -> Optional[List[str]]:
        """The labels of the rows selected in ``element``'s list (``AXSelectedRows``), in
        their order; None when the list or its selection cannot be read. A read."""
        try:
            box = self._row_container_element(element)
            rows = _attr(box, "AXSelectedRows") if box is not None else None
            if rows is None:
                return None
            return [self._row_label(r) or "" for r in list(rows)]
        except Exception:  # noqa: BLE001
            return None

    # ── a state-aware toggle's reads (execution/ax_actions.set_toggle) ──

    #: seconds a menu item's press is given to land before its menu is opened again to read the mark
    MENU_ACTION_SETTLE_S = 1.2

    def toggle_state(self, element) -> Optional[str]:
        """``"on"`` / ``"off"`` for a menu item (its ``AXMenuItemMarkChar``: a mark is on, no
        value or an empty one is off) or a checkbox / switch (``AXValue`` 1 / 0); None when it
        cannot be read: another role, a role that cannot be read, a mixed checkbox (2), a failed
        read. A read; ``set_toggle`` refuses on None (never a blind press)."""
        try:
            role = _s(_attr(element, "AXRole"))
            if role == "AXMenuItem":
                err, val = _ax().AXUIElementCopyAttributeValue(element, "AXMenuItemMarkChar", None)
                if err == _AX_NO_VALUE or (err == _AX_SUCCESS and not (_s(val) or "").strip()):
                    return "off"
                return "on" if err == _AX_SUCCESS else None
            if role in ("AXCheckBox", "AXSwitch"):
                err, val = _ax().AXUIElementCopyAttributeValue(element, "AXValue", None)
                if err != _AX_SUCCESS or val is None or isinstance(val, str):
                    return None
                return {1: "on", 0: "off"}.get(int(val))
        except Exception:  # noqa: BLE001 - unreadable is None, refused by the caller
            return None
        return None

    def _menu_bar_item(self, element):
        """The menu-bar item ``element`` sits under (itself for one), or None outside the menu bar."""
        cur = element
        for _ in range(_MAX_DEPTH):
            if cur is None:
                return None
            role = _s(_attr(cur, "AXRole"))
            if role == "AXMenuBarItem":
                return cur
            if role in ("AXMenuBar", "AXWindow", "AXApplication"):
                return None
            cur = _attr(cur, "AXParent")
        return None

    #: how long a menu opened to read it is given to build its items (AppKit's menuNeedsUpdate runs as it
    #: opens), polled this often
    MENU_BUILD_S = 0.8
    MENU_POLL_S = 0.05

    def _menu_has_items(self, bar_item) -> bool:
        return any(_s(_attr(m, "AXRole")) == "AXMenu"
                   and any(_s(_attr(k, "AXRole")) == "AXMenuItem" for k in self._children(m))
                   for m in self._children(bar_item))

    def with_menu_open(self, bar_item, read: Callable[[Any], Any]) -> Tuple[bool, Any]:
        """Open ``bar_item``'s menu (``AXPress`` on the menu-bar item, gated like every actuator), wait until
        it shows items (at most ``MENU_BUILD_S``), call ``read(bar_item)`` while it is open, and close it again
        whatever ``read`` does (``AXCancel`` on the menu-bar item, else on its menu). For an app that builds a
        menu only when it opens (Chess's "Spiel": no AX items before). ``(False, None)`` when it was not
        opened (the gates, or AX refused the press), else ``(True, read's result)``."""
        import time as _time
        if not self.perform_action(bar_item, "AXPress"):
            return False, None
        try:
            deadline = _time.monotonic() + self.MENU_BUILD_S
            while not self._menu_has_items(bar_item) and _time.monotonic() < deadline:
                _time.sleep(self.MENU_POLL_S)
            return True, read(bar_item)
        finally:
            if not self.perform_action(bar_item, "AXCancel"):
                menus = [m for m in self._children(bar_item) if _s(_attr(m, "AXRole")) == "AXMenu"]
                if not any(self.perform_action(m, "AXCancel") for m in menus):
                    logger.warning("AX: a menu opened to read it did not confirm closing")

    def close_menu(self, element) -> bool:
        """Close the menu ``element`` was reached through (``AXCancel`` on its menu-bar item, as the
        guest harness closes a menu it opened to read); True for an element outside the menu bar
        (nothing to close). Gated like every actuator (``perform_action``)."""
        import time as _time
        bar = self._menu_bar_item(element)
        if bar is None:
            return True
        ok = self.perform_action(bar, "AXCancel")
        _time.sleep(0.2)
        return ok

    def toggle_readback(self, element, want: str, timeout: float = 1.5) -> Optional[str]:
        """The state of a toggle right after its press. A checkbox / switch: ``toggle_state``,
        polled until it reads ``want`` or ``timeout`` passes. A menu item: AppKit sets its mark
        when the menu is VALIDATED (when it opens), so a closed menu may still show the old mark:
        read once; when that is not ``want``, its top menu is opened (gated ``AXPress`` on the
        menu-bar item, as the guest harness reads marks), the mark read, the menu closed. Never a
        press of the toggle itself."""
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout)
        got = self.toggle_state(element)
        bar = self._menu_bar_item(element) if _s(_attr(element, "AXRole")) == "AXMenuItem" else None
        if bar is None:
            while got != want and _time.monotonic() < deadline:
                _time.sleep(0.1)
                got = self.toggle_state(element)
            return got
        if got == want:
            return got
        # AppKit sends a menu item's action AFTER its menu has closed; a menu opened again before that drops it
        # (live: reopened 0.3 s after the press, the switch never changed, from either
        # state, while the harness's press with 1.2 s before its next read toggled every time). The press is
        # given MENU_ACTION_SETTLE_S to land (the closed item's mark polled meanwhile) before its menu is opened.
        settle = _time.monotonic() + self.MENU_ACTION_SETTLE_S
        while _time.monotonic() < settle:
            _time.sleep(0.1)
            got = self.toggle_state(element)
            if got == want:
                return got
        if not self.perform_action(bar, "AXPress"):
            return got
        try:
            _time.sleep(0.5)
            got = self.toggle_state(element)
        finally:
            self.perform_action(bar, "AXCancel")
            _time.sleep(0.2)
        return got

    def focused_element(self):
        """The element that has keyboard focus (``AXFocusedUIElement`` of the tree's app,
        or the system-wide one for a tree without a pid), or None. A read."""
        try:
            return _attr(self._root(), "AXFocusedUIElement")
        except Exception:  # noqa: BLE001
            return None

    def activate_app(self, bundle_id: str, settle: Optional[float] = None) -> bool:
        """Bring the app ``bundle_id`` to the front: activate it when it runs, else launch it
        (NSWorkspace). Gated like every actuator (the drive gate, then the foreground
        interlock); True only once the LIVE AX focus (``AXFocusedApplication``) reports that app,
        polled up to ``ACTIVATE_SETTLE_S`` seconds: each poll is a ``frontmost_reading`` (the AX
        focus whenever it answers, else the run-loop-ticked workspace read cross-checked by the
        menu-bar owner), kept with its time since the activation, the source that answered and
        every read in ``self.last_activation`` (the step's evidence). True as soon as the answering
        source reports the app; at the deadline False (fail closed) when no source answered, the
        sources disagreed, or they agreed on another app. Never the untickled workspace read."""
        self.last_activation = {"bundle_id": bundle_id, "polls": [], "confirmed_by": None, "verdict": "not_sent"}
        if not bundle_id or not _drive_enabled():
            return False
        if _interlock(f"ax.activate_app({bundle_id})"):
            return False
        try:
            AppKit = _appkit()
            running = list(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id) or [])
            if running:
                running[0].activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            else:
                ws = AppKit.NSWorkspace.sharedWorkspace()
                res = ws.launchAppWithBundleIdentifier_options_additionalEventParamDescriptor_launchIdentifier_(
                    bundle_id, 0, None, None)
                # pyobjc returns (ok, launch identifier) for the out-parameter
                if not (res[0] if isinstance(res, tuple) else res):
                    return False
        except Exception as e:  # noqa: BLE001
            logger.debug("activate_app(%s) failed: %s", bundle_id, e)
            return False
        import time as _time
        t0 = _time.monotonic()
        limit = max(0.0, float(ACTIVATE_SETTLE_S if settle is None else settle))
        deadline = t0 + limit
        ev = self.last_activation
        ev.update(verdict="polling", settle_s=limit)
        polls = ev["polls"]
        while True:
            r = frontmost_reading()
            row = {"t": round(_time.monotonic() - t0, 3), "front_source": r["front_source"], "bundle_id": r["bundle_id"],
                   "reads": r["reads"]}
            if r["conflict"]:
                row["conflict"] = True
            if len(polls) < ACTIVATION_POLLS_KEPT:
                polls.append(row)
            else:
                polls[-1] = row                       # the first ones and the latest
            ev["polled"] = ev.get("polled", 0) + 1
            if r["bundle_id"] == bundle_id:
                ev.update(confirmed_by=r["front_source"], front_source=r["front_source"], verdict="confirmed")
                return True
            if _time.monotonic() >= deadline:
                ev["verdict"] = ("sources_disagree" if r["conflict"] else
                                 "no_source_answered" if r["bundle_id"] is None else "not_in_front")
                return False
            _time.sleep(0.1)

    @staticmethod
    def running_pid(bundle_id: str) -> Optional[int]:
        """The pid of the ONE running app with ``bundle_id`` (a read), else None."""
        try:
            apps = list(_appkit().NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id) or [])
            return int(apps[0].processIdentifier()) if len(apps) == 1 else None
        except Exception:  # noqa: BLE001
            return None

    def _walk(self, element, depth, descs, refs=None, in_menubar: bool = False, tag: Optional[str] = None):
        if depth > _MAX_DEPTH or len(descs) >= _MAX_ELEMENTS:
            return
        desc = self._describe(element)
        desc.in_menubar = in_menubar
        # which top-level part of the app the element belongs to (``top_level_tag``):
        # an attribute, not a dataclass field, so keys, diffs and dumps are unchanged
        desc.top_tag = tag
        descs.append(desc)
        if refs is not None:
            refs.append((element, desc))
        below_menubar = in_menubar or desc.role == "AXMenuBar"
        for i, child in enumerate(self._children(element)):
            if len(descs) >= _MAX_ELEMENTS:
                break
            ctag = tag
            if depth == 0 and desc.role == "AXApplication":
                ctag = self._top_tag(child, i)
            elif desc.role == "AXMenuBar":
                ctag = "menu:" + (_s(_attr(child, "AXTitle")) or "")
            self._walk(child, depth + 1, descs, refs, below_menubar, ctag)

    @staticmethod
    def _top_tag(child, index: int) -> str:
        role = _s(_attr(child, "AXRole"))
        if role == "AXWindow":
            return f"window:{index}"
        if role == "AXMenuBar":
            return "menubar"
        return f"app:{index}"

    def top_level_tag(self, element) -> Optional[str]:
        """The top-level part of the app ``element`` sits in, as ``snapshot`` tags it:
        ``window:<k>`` (its window, by its place among the app's children), ``menu:<title>``
        (the menu-bar item whose menu holds it), ``menubar`` or ``app:<k>``; None when it cannot
        be read. A read (the drift gate counts the target's window or menu)."""
        cur = element
        try:
            for _ in range(_MAX_DEPTH + 2):
                if cur is None:
                    return None
                parent = _attr(cur, "AXParent")
                if parent is None:
                    return None
                prole = _s(_attr(parent, "AXRole"))
                if prole == "AXMenuBar":
                    return "menu:" + (_s(_attr(cur, "AXTitle")) or "")
                if prole == "AXApplication":
                    for i, k in enumerate(self._children(parent)):
                        if k == cur:
                            return self._top_tag(cur, i)
                    return None
                cur = parent
        except Exception:  # noqa: BLE001 - an unreadable chain has no tag
            return None
        return None

    def bundle_id(self) -> Optional[str]:
        """Bundle id of the app this tree is bound to (``None`` when it is not
        bound to one pid, or the app cannot be asked). The executor checks
        it against the recorded ``Selector.app`` right before acting."""
        if self._pid is None:
            return None
        try:
            AppKit = _appkit()
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(self._pid))
            bid = app.bundleIdentifier() if app is not None else None
            return str(bid) if bid else None
        except Exception:
            return None

    def element_bundle_id(self, element) -> Optional[str]:
        """Bundle id of the app that OWNS ``element`` (``AXUIElementGetPid``), else the
        tree's own app. A system-wide hit-test (``find_at``) can return another app's
        element; its selector must name that app, not the tree's."""
        try:
            err, pid = _ax().AXUIElementGetPid(element, None)
            if err == _AX_SUCCESS and pid and (self._pid is None or int(pid) != int(self._pid)):
                AppKit = _appkit()
                app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
                bid = app.bundleIdentifier() if app is not None else None
                return str(bid) if bid else None
        except Exception:  # noqa: BLE001 - no pid lookup: the tree's own app
            pass
        return self.bundle_id()

    def frontmost_bundle_id(self) -> Optional[str]:
        """Bundle id of the frontmost app (keystrokes go there), or ``None``: ``frontmost_reading`` (the live AX focus
        whenever it answers; else the run-loop-ticked workspace read cross-checked by the menu-bar owner; None when
        no source answers or they disagree - never the untickled workspace read)."""
        return frontmost_reading()["bundle_id"]

    def element_document(self, element) -> Optional[str]:
        """The full ``AXDocument`` of the window ``element`` sits in, or None."""
        return self.element_window(element)[1]

    def _app_name(self) -> str:
        """Localized name of the app this tree is bound to (best-effort, "" on fail)."""
        if self._pid is None:
            return ""
        try:
            AppKit = _appkit()
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(self._pid))
            return str(app.localizedName()) if app is not None else ""
        except Exception:
            return ""

    def snapshot(self, focused_window_only: bool = False, row_labels: bool = False) -> AXSnapshot:
        """The app's elements, flattened (bounded by ``_MAX_DEPTH`` / ``_MAX_ELEMENTS``).

        ``focused_window_only``: only the app's focused window (for a tree without a pid, the
        focused window of the system-wide focused app) - no menu bar, no other window. No
        focused window gives an empty snapshot. The watcher's secure-field exclusion
        and AX prompt context depend on it.

        ``row_labels`` (the gap fill lists rows): each row (at most ``ROW_SCAN_CAP``) also
        carries the attributes ``row_label``, ``row_container`` (its list's identity) and, when
        the container scope module is plugged in and the scope can be read, ``row_scope`` (the
        salted key of the folder / note list its window shows, read once per window through
        ``ax_actions.live_scope``); attributes, not dataclass fields, so keys, diffs and dumps
        are unchanged."""
        try:
            descs: List[AXElementDesc] = []
            refs: Optional[List[Tuple[Any, AXElementDesc]]] = [] if row_labels else None
            root = self._focused_window() if focused_window_only else self._root()
            if root is not None:
                self._walk(root, 0, descs, refs)
            if row_labels:
                from anyuse.execution.ax_actions import _scope_api, live_scope, scope_key
                rows = [(el, d) for el, d in refs or [] if d.role in ROW_ROLES][:self.ROW_SCAN_CAP]
                scopes: List[Tuple[Any, str]] = []      # (window, salted scope key): read once per window
                api = _scope_api()
                scoped = False
                if api is not None and rows:
                    try:
                        scoped = bool(api.has_reader(self.bundle_id()))
                    except Exception:  # noqa: BLE001
                        scoped = False
                for el, d in rows:
                    d.row_label = self._row_label(el)
                    d.row_container = self.row_container_identity(el)
                    if d.row_container is not None and scoped:
                        win = self.window_of(el)
                        key = next((k for w, k in scopes if w is not None and w == win), None)
                        if key is None:
                            key = scope_key(live_scope(self, el, window=win), create=True)
                            scopes.append((win, key))
                        if key:
                            d.row_scope = key
            snap = AXSnapshot(elements=descs, app_name=self._app_name())
            # the focused window's tag (each element carries ``top_tag``), so a recorded
            # fingerprint can count that window instead of the whole app
            snap.focused_tag = None
            if not focused_window_only and root is not None:
                fw = self._focused_window()
                snap.focused_tag = self.top_level_tag(fw) if fw is not None else None
            return snap
        except Exception as e:  # noqa: BLE001
            logger.debug("AX snapshot failed (Accessibility TCC?): %s", e)
            return AXSnapshot(elements=[])

    def _focused_window(self):
        app = self._root()
        if self._pid is None:
            app = _attr(app, "AXFocusedApplication")
            if app is None:
                return None
        return _attr(app, "AXFocusedWindow")

    def find(self, role: Optional[str] = None, title: Optional[str] = None) -> List[Any]:
        """Elements matching ``role`` and/or ``title``.

        ``title`` matches AXTitle OR the composite label (AXTitle, else
        AXDescription, else AXValue — ``AXElementDesc.label()``), not AXTitle
        alone. Many controls' only human-visible label is AXDescription, not
        AXTitle — macOS Calculator's buttons are the example ("Sieben", not an
        AXTitle at all). The recorded-selector matcher (``find_selector``) reads
        it; this bare, no-recorded-identity lookup (used when a live planner names
        a target with no prior recording to match against) reads it too.
        Widening a MATCH criterion cannot turn a correct unique resolution into a
        wrong one: the caller still requires exactly one candidate
        (``execution/executor.py``'s ``_resolve_target``) before actuating, so a
        broader search can only turn "0 found" into either a legitimate unique
        match or a still-safely-refused ambiguous one, never a guess.

        ``title`` also matches ``identifier``
        (AXIdentifier) exactly. TextEdit's document ``AXTextArea`` has no
        AXTitle, no AXDescription and no AXValue worth reading — its only
        human-readable attribute is ``identifier`` ("First Text View"). A live
        planner given an AX dump whose only string for that element is
        ``identifier`` reasonably puts it in the plan step's ``title`` (there is
        no separate ``identifier`` field in the plan schema), and the label
        widening does not cover it — ``label()`` never falls back to
        ``identifier``. An exact identifier match is at least as decisive as an
        exact title match (it is the more durable of the two — see
        ``Selector.is_durable()``), so this is the same widening, one attribute
        further, with the identical safety property: it can only turn "0 found"
        into a legitimate unique match or a still-safely-refused ambiguous one,
        never a guess.
        """
        try:
            descs: List[AXElementDesc] = []
            refs: List[Tuple[Any, AXElementDesc]] = []
            self._walk(self._root(), 0, descs, refs)
            out = []
            for el, desc in refs:
                if role is not None and desc.role != role:
                    continue
                if (title is not None and desc.title != title and desc.label() != title
                        and (desc.identifier or "") != title):
                    continue
                out.append(el)
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug("AX find failed (Accessibility TCC?): %s", e)
            return []

    # ── Selector upgrade: hit-test a coordinate → a durable selector ──────────
    def find_at(self, x: float, y: float):
        """Hit-test the AX tree at a LOGICAL-POINT coordinate → the element ref there
        (or None). The core of selector-upgrade: turn a computer-use *click
        coordinate* into a stable role:name selector. Needs Accessibility TCC;
        returns None (never raises) without it. Coordinates MUST be logical points
        (use context.coordinates.px_to_pt on vision-model pixel coords first)."""
        if not AX_AVAILABLE:
            return None
        try:
            A = _ax()
            system = A.AXUIElementCreateSystemWide()
            err, el = A.AXUIElementCopyElementAtPosition(system, float(x), float(y), None)
            return el if err == _AX_SUCCESS else None
        except Exception as e:  # noqa: BLE001
            logger.debug("find_at(%.1f,%.1f) failed (Accessibility TCC?): %s", x, y, e)
            return None

    def describe_element(self, element) -> AXElementDesc:
        """Full descriptor for one element ref, incl. position/size (best-effort)."""
        desc = self._describe(element)
        desc.position = _ax_pair(_attr(element, "AXPosition"), "point")
        desc.size = _ax_pair(_attr(element, "AXSize"), "size")
        return desc

    def to_selector(self, element) -> Selector:
        """Build a durable Selector from a live element ref (AX attrs + role-path),
        with the app's bundle id and the full document of the element's window,
        so a recording knows WHERE its target was; also its place
        (``_add_place``): the window's container scope, the menu path, and how
        many identical namesakes / identifier sharers the window or menu holds."""
        app = self.element_bundle_id(element)
        sel = selector_from_attrs(
            role=_s(_attr(element, "AXRole")),
            title=_s(_attr(element, "AXTitle")),
            value=_s(_attr(element, "AXValue")),
            identifier=_s(_attr(element, "AXIdentifier")),
            path=self._ax_path(element),
            description=_s(_attr(element, "AXDescription")),
            app=app,
            document=self.element_document(element),
        )
        try:
            self._add_place(element, sel, app)
        except Exception as e:  # noqa: BLE001 - a place that cannot be read is not recorded
            logger.debug("to_selector: place not read (%s)", e)
        return sel

    # ── where an element is (its window's container, its menu) and its twins there ──────

    def _identity(self, element) -> Tuple[str, str, str]:
        """(canonical role, identifier, name) exactly as ``selector_from_attrs`` builds them:
        what a selector compares."""
        s = selector_from_attrs(role=_s(_attr(element, "AXRole")), title=_s(_attr(element, "AXTitle")),
                                value=_s(_attr(element, "AXValue")),
                                identifier=_s(_attr(element, "AXIdentifier")),
                                description=_s(_attr(element, "AXDescription")))
        return s.canonical_role, s.identifier, s.name

    def _window_element(self, element):
        """The AXWindow ``element`` sits in, or None (menu-bar elements have none)."""
        win = _attr(element, "AXWindow")
        if win is None:
            win = _attr(element, "AXTopLevelUIElement")
        if win is not None and _s(_attr(win, "AXRole")) == "AXWindow":
            return win
        return None

    def _menu_path(self, element) -> Optional[List[str]]:
        """The titles of the menu-bar item and menu items above ``element``, top first (``[]``
        for a menu-bar item); None for an element outside the menu bar."""
        titles: List[str] = []
        cur = _attr(element, "AXParent")
        for _ in range(_MAX_DEPTH):
            if cur is None:
                return None
            role = _s(_attr(cur, "AXRole"))
            if role == "AXMenuBar":
                return list(reversed(titles))
            if role in ("AXWindow", "AXApplication"):
                return None
            if role in ("AXMenuItem", "AXMenuBarItem"):
                titles.append(_s(_attr(cur, "AXTitle")) or "")
            cur = _attr(cur, "AXParent")
        return None

    def _menu_place(self, element, menu_path: List[str]) -> List[Any]:
        """Every element of the menu bar whose menu path is ``menu_path`` (the element's menu)."""
        bar = _attr(element, "AXParent")
        for _ in range(_MAX_DEPTH):
            if bar is None or _s(_attr(bar, "AXRole")) == "AXMenuBar":
                break
            bar = _attr(bar, "AXParent")
        if bar is None:
            return []
        out: List[Any] = []
        stack = [(c, []) for c in reversed(self._children(bar))]
        while stack and len(out) < _MAX_ELEMENTS:
            el, path = stack.pop()
            if path == menu_path:
                out.append(el)
            role = _s(_attr(el, "AXRole"))
            below = path + [_s(_attr(el, "AXTitle")) or ""] if role in ("AXMenuItem", "AXMenuBarItem") else path
            if len(below) <= len(menu_path) and below == menu_path[:len(below)]:
                stack.extend((c, below) for c in reversed(self._children(el)))
        return out

    def _subtree(self, root) -> List[Any]:
        out: List[Any] = []
        stack = [(c, 1) for c in reversed(self._children(root))]
        while stack and len(out) < _MAX_ELEMENTS:
            el, depth = stack.pop()
            out.append(el)
            if depth < _MAX_DEPTH:
                stack.extend((c, depth + 1) for c in reversed(self._children(el)))
        return out

    def _add_place(self, element, sel: Selector, app: Optional[str]) -> None:
        """Record ``element``'s place on ``sel``: its menu path, or its window's container
        scope; and the identical namesakes and identifier sharers in that window or menu."""
        from anyuse.context import container_scope as CS
        mp = self._menu_path(element)
        if mp is not None:
            sel.menu_path = mp
            place = self._menu_place(element, mp)
        else:
            win = self._window_element(element)
            if win is None:
                return
            sel.scope = CS.container_scope(self, win, app) if CS.has_reader(app) else None
            place = self._subtree(win)
        mine = (sel.canonical_role, sel.identifier, sel.name)
        twins = sharers = 0
        for el in place:
            if el == element or canonical_role(_s(_attr(el, "AXRole"))) != sel.canonical_role:
                continue
            ident = self._identity(el)
            if ident == mine:
                twins += 1
            if sel.identifier and ident[1] == sel.identifier:
                sharers += 1
        sel.namesakes = twins
        sel.shared_id = sharers if sel.identifier else None

    def element_scope(self, element) -> Optional[Dict[str, str]]:
        """The container scope of the window ``element`` sits in (``container_scope``), or
        None. A read; the executor's check right before acting."""
        from anyuse.context import container_scope as CS
        return CS.container_scope(self, self._window_element(element), self.element_bundle_id(element))

    def in_container_content(self, element) -> bool:
        """Is ``element`` a row of its container's content (not the sidebar)? A read."""
        from anyuse.context import container_scope as CS
        return CS.in_container_content(self, element, self.element_bundle_id(element))

    def selector_at(self, x: float, y: float) -> Optional[Selector]:
        """Hit-test a point and return its durable selector (None if nothing there)."""
        el = self.find_at(x, y)
        return self.to_selector(el) if el is not None else None

    def _sibling_index(self, parent, element) -> int:
        """Index of ``element`` among its parent's same-role children (-1 unknown)."""
        if parent is None:
            return -1
        try:
            role = _s(_attr(element, "AXRole"))
            same = [c for c in self._children(parent) if _s(_attr(c, "AXRole")) == role]
            for i, c in enumerate(same):
                if c == element:
                    return i
        except Exception:
            pass
        return -1

    def _ax_path(self, element, max_up: int = 6) -> str:
        """A role-index path over up to ``max_up`` ancestors — disambiguates
        same-role/same-name siblings (e.g. 'window/group[0]/button[2]')."""
        try:
            chain = []
            cur = element
            for _ in range(max_up):
                parent = _attr(cur, "AXParent")
                role = canonical_role(_s(_attr(cur, "AXRole")))
                idx = self._sibling_index(parent, cur)
                chain.append(role if idx < 0 else f"{role}[{idx}]")
                if parent is None:
                    break
                cur = parent
            return "/".join(reversed(chain))
        except Exception:
            return ""

    def _in_document(self, element, document: str) -> bool:
        """Does ``element`` sit in the window showing ``document``? A full path or
        URL must match the window's full ``AXDocument`` (by basename, two
        documents of the same name would be the same target); a
        bare file name or window title keeps the old basename/title rule."""
        title, url = self.element_window(element)
        return same_document(document, url, title)

    #: The scorer's verdict for the last weighted resolution (None in cascade
    #: mode): score, runner-up, margin, abstained. Read by the replay harness to
    #: count healed-right / healed-wrong / abstained without touching the UI.
    last_match = None
    #: Why the last ``find_selector`` returned None although candidates existed
    #: (``{"n": <candidates left>, "why": ...}``), else None. The executor
    #: reports it as an ``ambiguous`` refusal.
    last_ambiguity = None

    def _resolve_weighted(self, selector: Selector, cands, total: int):
        """Pick among same-role candidates with the weighted scorer (opt-in,
        ``ANYUSE_ELEMENT_MATCHER=weighted``). An exact, UNIQUE identifier still
        wins outright — AXIdentifier is the most durable handle macOS offers
        and the scorer has no component for it. Otherwise the best-scoring
        candidate is returned only if it clears the score floor AND the
        top-two margin; a tie is an abstention, never a guess."""
        from anyuse.context import element_scorer as es
        mine = (selector.canonical_role, selector.identifier, selector.name)
        same = [c for c in cands if self._identity_of_desc(c[1]) == mine]
        if self._recorded_twins_refusal(selector, [c[0] for c in same]):
            return None
        if len(same) > 1 and len({self._window_key(c[0]) for c in same}) == 1:
            # identical twins in one window are told apart by position only
            self.last_ambiguity = {"n": len(same), "why": f"{len(same)} identical elements in one window match "
                                   f"{selector.describe()}; their positions cannot prove which one was recorded"}
            return None
        if selector.identifier:
            exact = [c for c in cands if (c[1].identifier or "") == selector.identifier]
            if selector.name and (selector.shared_id or (
                    len(exact) > 1 and len({self._window_key(c[0]) for c in exact}) == 1)):
                # an identifier several elements share is not an identity; the name decides
                exact = [c for c in exact if self._identity_of_desc(c[1])[2] == selector.name]
                if not exact:
                    return None
            if len(exact) == 1:
                self.last_match = es.MatchResult(index=0, element={"identifier": selector.identifier},
                                                 score=1.0, abstained=False, reason="exact identifier",
                                                 mode="weighted")
                return exact[0][0]
            if exact:
                if len({self._window_key(c[0]) for c in exact}) > 1:
                    # the same identifier in two windows (every document
                    # window's text view) is an ambiguity, never a z-order pick
                    self.last_ambiguity = {"n": len(exact), "why": f"{len(exact)} candidates in "
                                           f"different windows carry #{selector.identifier}"}
                    return None
                cands = exact
        # the window's index is its z-order: paths are compared below the window
        target = {"role": selector.raw_role or "", "label": selector.name, "value": "",
                  "ax_path": path_within_window(selector.path), "identifier": selector.identifier}
        # cap the path computations: a rough label pre-rank keeps the top 60
        if len(cands) > 60:
            cands = sorted(cands, key=lambda c: -es._seqsim(selector.name, c[1].label()))[:60]
        feats = []
        for el, d, idx in cands:
            feats.append({"role": d.role or "", "label": d.label(), "value": (d.value or "")[:80],
                          "ax_path": path_within_window(self._ax_path(el)), "identifier": d.identifier or "",
                          "order_frac": idx / max(1, total)})
        weights = dict(es.ax_weights())
        weights["pos"] = 0.0          # the stored selector carries no document position
        res = es.resolve_weighted(target, feats, weights=weights)
        self.last_match = res
        if res.abstained or res.element is None:
            return None
        return cands[res.index][0]

    def find_selector(self, selector: Selector, document: Optional[str] = None,
                      mode: Optional[str] = None):
        """Resolve a stored Selector → a live element ref (or None) — the replay side
        of to_selector. Matches canonical_role + (identifier or name); on ties,
        prefers the candidate whose role-path matches the stored path.

        ``document`` restricts the candidates to the window showing that
        document. Every document window of an app carries the same controls
        with the same identifiers and role-paths (TextEdit's text area is
        ``First Text View`` at ``window[0]/scrollarea[0]/textarea[0]`` in every
        document), so with two documents open an unscoped selector resolves to
        whichever is in front — a wrong-target write that no read-back can see.
        """
        if not AX_AVAILABLE or selector is None:
            return None
        self.last_match = None
        self.last_ambiguity = None
        # a selector that recorded its window's full document is scoped to
        # that document without the caller having to pass it again.
        document = document or (selector.document or None)
        try:
            descs: List[AXElementDesc] = []
            refs: List[Tuple[Any, AXElementDesc]] = []
            self._walk(self._root(), 0, descs, refs)
            from anyuse.context.element_scorer import matcher_mode
            place = _PlaceFilter(self, selector)
            if matcher_mode(mode) == "weighted":
                same_role = [(el, desc, i) for i, (el, desc) in enumerate(refs)
                             if canonical_role(desc.role) == selector.canonical_role
                             and not (document and not self._in_document(el, document))
                             and place.keeps(el, desc)]
                return self._resolve_weighted(selector, same_role, len(refs))
            by_id = []
            for el, desc in refs:
                if canonical_role(desc.role) != selector.canonical_role:
                    continue
                # The stored name may have come from AXTitle, AXDescription or AXValue
                # (selector_from_attrs prefers them in that order; Calculator's buttons carry
                # their label in AXDescription only). The live element's name is built by the
                # SAME rule and must equal it, and an element with an identifier is not one recorded
                # without (``_same_identity`` refuses both anyway), so neither makes a match look
                # ambiguous; a selector with neither name nor identifier matches only elements with
                # neither (it used to match every element of its role).
                ident = self._identity_of_desc(desc)
                if selector.identifier:
                    if ident[1] != selector.identifier:
                        continue
                elif ident[2] != selector.name:
                    continue
                elif ident[1] and not (selector.menu_path is not None and getattr(desc, "in_menubar", False)
                                       and AUTO_MENU_ID.match(ident[1])):
                    # an identified element is not one recorded without -- except an AppKit-generated id at the
                    # menu place the selector is pinned to (the place filter below checks the place)
                    continue
                if document and not self._in_document(el, document):
                    continue
                if not place.keeps(el, desc):
                    continue
                by_id.append((el, ident[2]))
            if selector.identifier and selector.name and (
                    selector.shared_id or (len(by_id) > 1 and len({self._window_key(el) for el, _n in by_id}) == 1)):
                # an identifier several elements of one window or menu share (Lexikon's
                # dictionaries all carry ``chooseDictionary:``) is not an identity: the name must
                # match too, or a reorder or a removal hands the step a sibling with the same
                # identifier. (The same identifier in two windows stays an ambiguity.)
                by_id = [(el, nm) for el, nm in by_id if nm == selector.name]
            return self._unique(selector, [el for el, _nm in by_id])
        except Exception as e:  # noqa: BLE001
            logger.debug("find_selector(%s) failed: %s", selector.describe(), e)
            return None

    @staticmethod
    def _identity_of_desc(desc: "AXElementDesc") -> Tuple[str, str, str]:
        s = selector_from_attrs(role=desc.role, title=desc.title, value=desc.value,
                                identifier=desc.identifier, description=desc.ax_description)
        return s.canonical_role, s.identifier, s.name

    def _recorded_twins_refusal(self, selector: Selector, candidates: List[Any]) -> bool:
        """A recording whose element had identical namesakes in its window or menu
        (``Selector.namesakes``) is identified by position only, and position is not identity:
        when twins trade places, or one goes and the other moves up, the window looks exactly
        as recorded (8 measured wrong actions are byte-identical to their recorded
        worlds). Refused whenever a candidate is left (none left: not found)."""
        n = selector.namesakes or 0
        if n <= 0 or not candidates:
            return False
        self.last_ambiguity = {"n": max(2, len(candidates)),
                               "why": f"the recorded element had {n} identical namesake(s) in its "
                                      f"{'menu' if selector.menu_path is not None else 'window'}, "
                                      f"{len(candidates)} element(s) match {selector.describe()} now; "
                                      f"position does not prove which one was recorded"}
        return True

    def _unique(self, selector: Selector, candidates: List[Any]):
        """The one candidate that is the recorded element, or None.

        Taking the FIRST identifier match outright (TextEdit's ``First Text View``
        exists in every document window), or ``candidates[0]`` when several matched
        and the path did not decide, is a wrong-target write that ``_same_identity``
        cannot see, because the identifier and the name agree. So two or more
        candidates are an ambiguity, and ambiguity is refused.

        The recorded role-path does not break a tie between
        candidates in one window. Every candidate left here is identical in all a
        selector compares (role, identifier, name: ``find_selector`` filters by
        exactly that), so only their places differ, and a place proves nothing once
        twins can trade places or one of them goes: the window then looks exactly as
        recorded, and the index points at the twin (16 measured wrong
        actions). A tie may be broken only among candidates whose position
        relation to the recorded anchors is unchanged; identical twins share every
        relation an AX read can observe, so there is none to choose from. A
        recording that knows its element had twins refuses even one candidate
        (``_recorded_twins_refusal``: the others may be the ones gone)."""
        if self._recorded_twins_refusal(selector, candidates):
            return None
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return None
        windows = {self._window_key(el) for el in candidates}
        where = "windows" if len(windows) > 1 else "places in one window"
        self.last_ambiguity = {"n": len(candidates),
                               "why": f"{len(candidates)} candidates in "
                                      f"{len(windows) if len(windows) > 1 else 'different'} {where} match "
                                      f"{selector.describe()} identically; their positions cannot prove which "
                                      f"one was recorded"}
        return None

    def _window_key(self, element):
        """A hashable key for the window ``element`` sits in (its document, else
        its title; ``None`` for the menu bar and other windowless elements)."""
        title, url = self.element_window(element)
        return normalize_document(url) or title

    def read_value(self, element) -> Optional[str]:
        """The element's ``AXValue`` as text (a read: no actuation, no gate)."""
        return _s(_attr(element, "AXValue"))

    def perform_action(self, element, action: str) -> bool:
        if not _drive_enabled():
            return False  # keystone gate: no AX actuation when ANYUSE_DRIVE_ENABLED off
        if _interlock(f"ax.perform_action({action})"):
            return False
        try:
            return _ax().AXUIElementPerformAction(element, action) == _AX_SUCCESS
        except Exception as e:  # noqa: BLE001
            logger.debug("AX perform_action(%s) failed: %s", action, e)
            return False

    # ── text entry: evidence, not return codes ────────────────────

    def element_window(self, element) -> Tuple[Optional[str], Optional[str]]:
        """``(window title, document URL)`` of the window ``element`` sits in.

        This is the identity a text write needs and a return code cannot give:
        every TextEdit document window carries a text area with the SAME
        identifier (``First Text View``) and the same role-path, so a stored
        selector resolves to whichever document is in front. The window title
        and ``AXDocument`` say which one that was.
        """
        try:
            win = _attr(element, "AXWindow")
            if win is None:
                win = _attr(element, "AXTopLevelUIElement")
            if win is None:
                return None, None
            return _s(_attr(win, "AXTitle")), _s(_attr(win, "AXDocument"))
        except Exception:
            return None, None

    def raise_window(self, element) -> bool:
        """Bring the window ``element`` sits in to the front (AXMain + AXRaise),
        if it is not already the app's focused window. Gated like every actuator.

        Needed because AX attribute writes reach a BACKGROUND window's text view
        perfectly well, while keystrokes and ⌘S go to the FRONT one: a
        document-scoped repair wrote AXValue into the intended
        (background) document and then typed the same line into the other
        document in front and saved it — a wrong-target write."""
        if not _drive_enabled():
            return False
        if _interlock("ax.raise_window"):
            return False
        try:
            win = _attr(element, "AXWindow")
            if win is None:
                return False
            app = self._root()
            if _attr(app, "AXFocusedWindow") == win:
                return True
            A = _ax()
            A.AXUIElementSetAttributeValue(win, "AXMain", True)
            A.AXUIElementPerformAction(win, "AXRaise")
            import time as _time
            _time.sleep(0.4)
            return _attr(app, "AXFocusedWindow") == win
        except Exception as e:  # noqa: BLE001
            logger.debug("raise_window failed: %s", e)
            return False

    def holds_focus(self, element, max_up: int = 4) -> bool:
        """Does ``element`` hold keyboard focus — itself, or through a focused
        descendant (a field editor inside a search field)? Keystrokes go to
        whatever holds focus, so this is the wrong-target guard for typing: in
        the guest it refused 15/15 attempts to type into a find-bar field whose
        recorded twin was the Help menu's search field (same identifier)."""
        try:
            focused = _attr(self._root(), "AXFocusedUIElement")
            cur = focused
            for _ in range(max_up + 1):
                if cur is None:
                    return False
                if cur == element:
                    return True
                cur = _attr(cur, "AXParent")
            return False
        except Exception:
            return False

    def set_value_evidence(self, element, value) -> "SetValueEvidence":
        """Write ``AXValue`` and return what can be OBSERVED afterwards.

        ``AXUIElementSetAttributeValue(...) == kAXErrorSuccess`` says only whether
        the AX server accepted the write, nothing about whether the application
        took the value. The evidence
        returned here is the value re-read through ``AXValue``, the character
        count re-read through ``AXNumberOfCharacters`` (text elements only), and
        the window/document the element belongs to. Read-back is NOT proof that
        a document-backed app will persist the text; for that the caller
        supplies an independent accessor to :meth:`enter_text`.
        """
        ev = SetValueEvidence(method="ax_value", requested=_s(value) or "")
        if not _drive_enabled():
            ev.error = "gated: ANYUSE_DRIVE_ENABLED off"
            return ev  # keystone gate: no AX value-set when ANYUSE_DRIVE_ENABLED off
        refused = _interlock("ax.set_value")
        if refused:
            ev.error, ev.refused = refused, True
            return ev
        try:
            rc = _ax().AXUIElementSetAttributeValue(element, "AXValue", value)
            ev.accepted = rc == _AX_SUCCESS
            if not ev.accepted:
                ev.error = f"AXUIElementSetAttributeValue rc={rc}"
        except Exception as e:  # noqa: BLE001
            logger.debug("AX set_value failed: %s", e)
            ev.error = f"{type(e).__name__}: {e}"
            return ev
        return self._observe_text(element, ev)

    def _observe_text(self, element, ev: "SetValueEvidence") -> "SetValueEvidence":
        """Fill the read-back fields of ``ev`` from the live element."""
        ev.readback = _s(_attr(element, "AXValue"))
        ev.readback_matches = ev.readback is not None and ev.readback == ev.requested
        n = _attr(element, "AXNumberOfCharacters")
        if n is not None:
            try:
                ev.nchars = int(n)
                ev.nchars_matches = ev.nchars == len(ev.requested)
            except (TypeError, ValueError):
                ev.nchars = None
        ev.window_title, ev.document = self.element_window(element)
        return ev

    def set_value(self, element, value) -> bool:
        """``True`` iff the write was accepted AND reads back as the value set.

        Strictly stronger than the old return-code semantics. See
        :meth:`set_value_evidence` for the full picture and :meth:`enter_text`
        for text that must actually reach an application's document model.
        """
        return bool(self.set_value_evidence(element, value))

    def type_text(self, text: str, typer=None) -> dict:
        """Type ``text`` into the focused element with layout-independent key
        events (``context.keyboard.type_unicode``). Gated like every actuator."""
        if not _drive_enabled():
            return {"typed": False, "chars": 0, "stuck_modifiers": [],
                    "error": "gated: ANYUSE_DRIVE_ENABLED off"}
        refused = _interlock("ax.type_text")
        if refused:
            return {"typed": False, "chars": 0, "stuck_modifiers": [], "refused": True,
                    "error": refused}
        if typer is None:
            from anyuse.context.keyboard import type_unicode as typer  # noqa: F811
        try:
            return dict(typer(str(text)))
        except Exception as e:  # noqa: BLE001
            return {"typed": False, "chars": 0, "stuck_modifiers": [],
                    "error": f"{type(e).__name__}: {e}"}

    def press_key(self, keycode: int, flags: int = 0, presser=None) -> bool:
        """One raw key press (down+up), gated like every actuator.

        ``context.keyboard.press_key`` is
        deliberately NOT self-gating — its own module docstring says so, "checked
        by the caller" — because it is a bare hardware-event primitive with no
        AX element to reason about (unlike ``set_value``/``enter_text``, which
        gate on the element they would write to). This is that caller: the one
        gated entry point the executor's ``_execute_ax`` reaches for a raw key
        (Finder's rename/search fields commit on a literal Return, and those
        exact fields ignore an ``AXValue`` write).
        """
        if not _drive_enabled():
            return False
        if _interlock(f"ax.press_key({int(keycode)})"):
            return False
        if presser is None:
            from anyuse.context.keyboard import press_key as presser  # noqa: F811
        try:
            return bool(presser(int(keycode), int(flags)))
        except Exception as e:  # noqa: BLE001
            logger.debug("AX press_key failed: %s", e)
            return False

    def keyboard_layout(self):
        """The keyboard layout in use now (``context.keyboard.current_layout``, read only), or None
        when it cannot be read: a recorded shortcut is sent by the key that types its character on it."""
        from anyuse.context.keyboard import current_layout
        return current_layout()

    def edit_text(self, element, text: str, replace_range: Optional[Tuple[int, int]] = None,
                  expect_value: Optional[str] = None) -> "SetValueEvidence":
        """Insert ``text`` at the element's insertion point, or replace the
        character range ``replace_range = (start, length)`` with it, through
        ``AXSelectedText`` — a document-model edit, not a whole-value write and
        not keystrokes. Voice control's fast path: measured at
        ~6 ms, against ~0.7 s for ``enter_text``'s
        focus + select-all + settle ladder, which also REPLACES the whole text.

        Evidence, as everywhere: the value is read back. With
        ``expect_value`` the read-back must equal it exactly (a range edit's
        caller knows the full resulting text); otherwise it must contain
        ``text``. Gated like every actuator. No focus change, no keystroke, so
        no keyboard-focus wrong-target risk; the caller resolves ``element``
        through the identity check first.
        """
        ev = SetValueEvidence(method="ax_selected_text", requested=text)
        if not _drive_enabled():
            ev.error = "gated: ANYUSE_DRIVE_ENABLED off"
            return ev
        refused = _interlock("ax.edit_text")
        if refused:
            ev.error, ev.refused = refused, True
            return ev
        try:
            A = _ax()
            if replace_range is not None:
                start, length = int(replace_range[0]), int(replace_range[1])
                if start < 0 or length < 0:
                    ev.error = f"invalid range {replace_range!r}"
                    return ev
                rng = A.AXValueCreate(A.kAXValueCFRangeType, (start, length))
                rc = A.AXUIElementSetAttributeValue(element, "AXSelectedTextRange", rng)
                if rc != _AX_SUCCESS:
                    ev.error = f"AXSelectedTextRange rc={rc}"
                    return ev
            rc = A.AXUIElementSetAttributeValue(element, "AXSelectedText", text)
            ev.accepted = rc == _AX_SUCCESS
            if not ev.accepted:
                ev.error = f"AXSelectedText rc={rc}"
        except Exception as e:  # noqa: BLE001
            ev.error = f"{type(e).__name__}: {e}"
            return ev
        ev.readback = _s(_attr(element, "AXValue"))
        if expect_value is not None:
            ev.readback_matches = ev.readback == expect_value
            n = _attr(element, "AXNumberOfCharacters")
            if n is not None:
                try:
                    ev.nchars = int(n)
                    ev.nchars_matches = ev.nchars == len(expect_value)
                except (TypeError, ValueError):
                    ev.nchars = None
        else:
            ev.readback_matches = ev.readback is not None and text in ev.readback
        ev.window_title, ev.document = self.element_window(element)
        return ev

    def enter_text(self, element, text: str, verify=None, typer=None,
                   settle: float = 0.3, expect_document: Optional[str] = None,
                   rungs: Tuple[str, ...] = ("ax_value", "ax_selected_text", "keystrokes"),
                   ) -> "TextEntryResult":
        """Put ``text`` into a text element and prove it, one rung at a time.

        Rungs, cheapest first: an ``AXValue`` write; a focus + select-all +
        ``AXSelectedText`` write (a document-model insert); focus + select-all +
        real key events. Each rung's read-back is recorded. If ``verify`` is
        given — a callable returning True when an INDEPENDENT accessor (the
        saved file, a database row, the app's own export) shows the text — the
        ladder stops at the first rung whose verify passes and reports
        ``verified_independently``. Without ``verify`` it stops at the first
        rung that reads back, and says so.

        ``expect_document`` refuses to write at all when the element's window
        belongs to a different document (``AXDocument`` URL or window title not
        matching): that is a wrong-target write, and it is counted rather than
        performed.
        """
        import time as _time
        res = TextEntryResult(requested=text)
        window_title, document = self.element_window(element)
        res.window_title, res.document = window_title, document
        if expect_document and not same_document(expect_document, document, window_title):
            # a full path is compared as a full path (same_document); a
            # bare name keeps the basename/title rule.
            res.wrong_target = True
            # neither document is quoted: a path or a title is the person's data
            res.error = "element belongs to another document; refusing to write"
            return res
        if not _drive_enabled():
            res.error = "gated: ANYUSE_DRIVE_ENABLED off"
            return res
        refused = _interlock("ax.enter_text")
        if refused:
            res.error = refused
            return res
        res.window_raised = self.raise_window(element)

        def _independent(ev):
            if verify is None:
                return None
            try:
                return bool(verify())
            except Exception as e:  # noqa: BLE001
                ev.error = (ev.error + "; " if ev.error else "") + f"verify raised {type(e).__name__}: {e}"
                return False

        for rung in rungs:
            ev = SetValueEvidence(method=rung, requested=text)
            if rung == "ax_value":
                ev = self.set_value_evidence(element, text)
            elif rung == "ax_selected_text":
                if self._focus_select_all(element):
                    try:
                        rc = _ax().AXUIElementSetAttributeValue(element, "AXSelectedText", text)
                        ev.accepted = rc == _AX_SUCCESS
                    except Exception as e:  # noqa: BLE001
                        ev.error = f"{type(e).__name__}: {e}"
                else:
                    ev.error = "could not focus/select"
            elif rung == "keystrokes":
                if not self._focus_select_all(element):
                    ev.error = "could not focus/select"
                elif not self.holds_focus(element):
                    # Key events go to whatever holds focus. If that is not the
                    # element we were asked to fill, typing would be a
                    # wrong-target write into some other control or document.
                    ev.error = "element does not hold keyboard focus; refusing to type"
                    ev.refused = True
                else:
                    typed = self.type_text(text, typer=typer)
                    ev.accepted = bool(typed.get("typed"))
                    ev.stuck_modifiers = list(typed.get("stuck_modifiers") or [])
                    if not ev.accepted:
                        ev.error = str(typed.get("error", "typing failed"))
            else:
                ev.error = f"unknown rung {rung!r}"
            _time.sleep(settle)
            self._observe_text(element, ev)   # observe once, after the app settled
            ev.independent = _independent(ev) if ev.accepted else None
            res.rungs.append(ev)
            if ev.independent is True:
                res.rung, res.verified_independently = rung, True
                return res
            if verify is None and ev.readback_matches:
                res.rung = rung
                return res
        res.error = res.error or "no rung verified"
        return res

    def _focus_select_all(self, element) -> bool:
        """Give ``element`` keyboard focus and select its whole text."""
        if _interlock("ax.focus_select_all"):
            return False
        try:
            A = _ax()
            A.AXUIElementSetAttributeValue(element, "AXFocused", True)
            import time as _time
            _time.sleep(0.25)
            n = _attr(element, "AXNumberOfCharacters")
            try:
                n = int(n) if n is not None else len(_s(_attr(element, "AXValue")) or "")
            except (TypeError, ValueError):
                n = 0
            rng = A.AXValueCreate(A.kAXValueCFRangeType, (0, n))
            A.AXUIElementSetAttributeValue(element, "AXSelectedTextRange", rng)
            _time.sleep(0.15)
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("focus/select-all failed: %s", e)
            return False

    def diff(self, before: Optional[AXSnapshot], after: Optional[AXSnapshot]) -> AXDiff:
        before = before or AXSnapshot()
        after = after or AXSnapshot()
        b_by_key: Dict[Tuple, List[AXElementDesc]] = {}
        for e in before.elements:
            b_by_key.setdefault(e.key(), []).append(e)
        a_by_key: Dict[Tuple, List[AXElementDesc]] = {}
        for e in after.elements:
            a_by_key.setdefault(e.key(), []).append(e)

        added: List[AXElementDesc] = []
        removed: List[AXElementDesc] = []
        changed: List[Tuple[AXElementDesc, AXElementDesc]] = []
        for k in set(b_by_key) | set(a_by_key):
            bs = b_by_key.get(k, [])
            as_ = a_by_key.get(k, [])
            # Pair within a key bucket order-insensitively by comparing the
            # MULTISET of values, so a reordered same-key snapshot doesn't get
            # mis-paired positionally and report spurious changes. Values present
            # in `after` but not `before` are added/changed evidence; values in
            # `before` but not `after` are removed.
            b_vals = Counter((e.value or "") for e in bs)
            a_vals = Counter((e.value or "") for e in as_)
            # Elements whose value is unchanged across the two snapshots match
            # 1:1 and contribute nothing to the diff.
            matched = b_vals & a_vals          # min counts per value: the stable pairs
            new_vals = a_vals - matched        # surplus values only in `after`
            gone_vals = b_vals - matched       # surplus values only in `before`
            new_descs = [e for e in as_ if _take(new_vals, e.value or "")]
            gone_descs = [e for e in bs if _take(gone_vals, e.value or "")]
            # Pair surplus before/after as value-changes (same key, diff value);
            # leftovers are genuine added (after-only) / removed (before-only).
            for b, a in zip(gone_descs, new_descs):
                changed.append((b, a))
            n = min(len(gone_descs), len(new_descs))
            added.extend(new_descs[n:])
            removed.extend(gone_descs[n:])
        return AXDiff(added=added, removed=removed, changed=changed)
