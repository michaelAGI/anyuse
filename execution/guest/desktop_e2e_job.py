#!/usr/bin/env python3
"""The desktop end-to-end test, GUEST half: a closed-command executor/observer.
INSIDE THE GUEST VM ONLY (refuses elsewhere). The design notes.

    desktop_e2e_job.py serve --session DIR [--max-min 240] [--idle-min 15]

Started by the GUI-session job runner (Accessibility granted there). The HOST
orchestrator writes one JSON command per
file into ``DIR/cmd/`` over ssh and reads ``DIR/resp/<id>.json``; every model
call happens on the host, no key ever enters the guest. This process only
reads the screen through AX and acts through AnyUse's checked executor.

Commands are a CLOSED set (``OPS``); nothing here evaluates code or runs a
shell command from a message. Guards on every actuation, in order:

* the screen is not locked (else the server STOPS; unlock.py is never run,
  nothing is ever typed into a lock screen);
* no authentication prompt / consent window before the action (else the
  action is not taken: status ``prompt``); after OUR action a prompt is
  cancelled through its AXCancelButton only, never typed into; one that
  stays STOPS the server;
* the LIVE element's own label must not name an irreversible verb
  (``desktop_e2e.irreversible_reason``): such an action is HELD (status
  ``gated``) and never executed — the harness declines every gate;
* typing only into a non-secure field outside any authentication dialog;
  keys only Return / Escape;
* only this test's apps (``desktop_e2e.MANAGED``) and this test's files
  (under ``~/anyuse-guest/scratch/e2e/gen``).

Before ALL of that, every input this process sends asks the foreground
interlock (``interlock``): AX actions and value writes (``_AS``),
the checked executor's presses, text and keys (``EX``), the one posted click,
app launch, activation, quit and folder opens. Host: always refused. Guest:
only while this process's own audit session is the one on the console,
its screen not locked, with no screen-sharing viewer. A refused command answers a TYPED
refusal (status ``refused``, ``refusal.kind`` ``foreground_interlock``), never
a silent no-op; reads that send nothing still answer.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
import unicodedata
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flows import refuse_unless_in_a_guest   # noqa: E402
import desktop_gen as G                        # noqa: E402
import desktop_gen2 as G2                      # noqa: E402
import clean_state as CS                       # noqa: E402

from anyuse.execution import desktop_e2e as X      # noqa: E402  (pure)
from anyuse.execution import desktop_effects as E  # noqa: E402  (pure)
from anyuse.execution import desktop_hygiene as H  # noqa: E402  (pure)
from anyuse.execution import desktop_e2e6 as U6  # noqa: E402  (pure; the UI language)
from anyuse.execution import foreground_interlock as FI  # noqa: E402  (every input asks it first)
from anyuse.execution import choice_identity as CI  # noqa: E402  (pure; a choice option found by identity)

_at, _s = G._at, G._s
_AS_BASE = G._AS        # the real ApplicationServices accessor; ``_AS`` below is the guarded one
FINDER, NOTES, TEXTEDIT, DICT, DCM = ("com.apple.finder", "com.apple.Notes", "com.apple.TextEdit",
                                     "com.apple.Dictionary", "com.apple.DigitalColorMeter")
GEN = os.path.realpath(os.path.expanduser(X.GEN))
PROMPT_OWNERS = ("SecurityAgent", "UserNotificationCenter", "coreautha", "tccd", "universalAccessAuthWarn")
TEXT_ROLES = ("AXTextField", "AXTextArea", "AXComboBox")
#: Lexikon sources that reach the web; never pressed (any arm, any setup, the
#: restore), and nothing is typed into Lexikon while one of them is marked
WEB_SOURCES = ("Wikipedia", "Alle Lexika")
T0 = time.monotonic()
EX = None               # one checked executor (desktop_gen.Exec) for the job
STATE = {"finder_folder": None, "events": []}
_PROMPT_CLOCK_ID = None


class Stop(Exception):
    """The whole server stops: lock screen, a prompt that would not cancel, over time."""


class Prompt(Exception):
    """An authentication prompt was seen: the action is not taken, the task stops."""


def log(*a):
    print(f"[{time.monotonic() - T0:7.1f}s]", *a, flush=True)


# ── the foreground interlock: every input asks it first ─────────────────────

INTERLOCK_WHERE = "desktop_e2e_job"


class InterlockRefused(BaseException):
    """The foreground interlock refused an input. A ``BaseException`` on purpose: the ops' and helpers'
    ``except Exception`` fallbacks (a frame that could not be set, a menu that did not read) must not turn a
    refusal into a silent no-op; ``serve`` answers it as a typed refusal."""

    def __init__(self, verdict):
        super().__init__(f"refused by foreground interlock: {verdict.reason}")
        self.verdict = verdict


def guest_console_refusal(v):
    """The brief's guest rule as a SECOND check after ``foreground_interlock.evaluate`` (which refuses a guest
    session that is not the console session itself since 4e172bc): input goes only into this process's own
    audit session while it IS the console session, and no other session is on the console. A verdict that
    is not a VM guest's is refused here too (host: always)."""
    if v.environment != "vm-guest":
        return "host: this job agent sends input only inside a VM guest"
    d = v.detail or {}
    sessions, own = d.get("sessions"), d.get("own_asid")
    if sessions is None or own is None:
        return "vm guest: the console sessions or the own audit session cannot be read"
    on = [s for s in sessions if s.get("on_console")]
    if not on:
        return "vm guest: no session is on the console (the login window is showing)"
    if [s.get("audit_id") for s in on] != [own]:
        return (f"vm guest: the console session is not only this process's own audit session ({own}): "
                f"{[(s.get('user'), s.get('audit_id')) for s in on]}")
    return None


def interlock(where):
    """Called immediately before every input this job sends. ``foreground_interlock.require_clear`` (no
    screen-sharing viewer, the own GUI session's screen not locked; refused on a host in every state), then
    ``guest_console_refusal``. Raises ``InterlockRefused``; returns the verdict when input may be sent."""
    try:
        v = FI.require_clear(f"{INTERLOCK_WHERE}: {where}")
    except FI.ForegroundInterlockRefused as exc:
        raise InterlockRefused(exc.verdict) from None
    if not v.bypassed:
        why = guest_console_refusal(v)
        if why:
            FI.logger.error("FOREGROUND INTERLOCK refused %s: %s", v.where, why)
            raise InterlockRefused(FI.Verdict(False, v.where, why, v.environment, detail=v.detail))
    STATE["inputs"] = STATE.get("inputs", 0) + 1
    return v


def interlock_status():
    """The verdict as data, nothing sent and nothing raised (``hello``)."""
    v = FI.check(f"{INTERLOCK_WHERE}: status")
    why = None if not v.allowed or v.bypassed else guest_console_refusal(v)
    return {"allowed": bool(v.allowed and not why), "reason": why or v.reason, "environment": v.environment,
            "bypassed": v.bypassed}


def interlock_refusal(verdict):
    """The typed answer to a refused command (``inputs_sent_before``: inputs this command sent before it)."""
    return {"status": "refused", "refused_by": "foreground_interlock",
            "refusal": {"kind": "foreground_interlock", "where": verdict.where, "reason": verdict.reason,
                        "environment": verdict.environment, "inputs_sent_before": STATE.get("inputs", 0)},
            "detail": f"refused by foreground interlock: {verdict.reason}"}


#: the ApplicationServices calls that send input; every other attribute (reads) passes through
_GATED_AX = {"AXUIElementPerformAction": "AX action", "AXUIElementSetAttributeValue": "AX value write",
             "AXUIElementPostKeyboardEvent": "AX key event"}
#: a menu press: opening a menu bar item, pressing a menu item,
#: closing a menu go through the CHECKED executor (StateGraphExecutor.execute_checked_step: the element's recorded
#: identity and app, the verb table, the effect check), behind the interlock -- never as a raw AX action
MENU_ROLES = ("AXMenuBarItem", "AXMenuItem", "AXMenu", "AXMenuBar")
MENU_VERBS = {"AXPress": "press", "AXCancel": "cancel"}
AX_FAILURE = -25200                  # kAXErrorFailure: what a refused or failed checked press answers the caller


def _is_menu(el):
    try:
        return _s(_at(el, "AXRole")) in MENU_ROLES
    except Exception:  # noqa: BLE001 - an element whose role cannot be read is not treated as a menu
        return False


def _element_pid(el):
    try:
        err, pid = _AS_BASE().AXUIElementGetPid(el, None)
        if not err and pid:
            return int(pid)
    except Exception:  # noqa: BLE001
        pass
    return STATE.get("last_pid")


def checked_menu_action(el, action):
    """One menu press through the checked executor (``EX``: ``GuardedExec`` asks the interlock first). Answers like
    the raw call: 0 when the checked step succeeded, ``AX_FAILURE`` when it was refused or failed (the caller then
    finds the menu closed or its item missing and refuses with its own reason). Without a checked executor nothing
    is pressed."""
    if EX is None:
        raise RuntimeError("no checked executor: a menu press is never sent raw")
    pid = _element_pid(el)
    r = EX.run(el, pid, G.record_selector(pid, el), MENU_VERBS[action])
    STATE.setdefault("menu_checked", []).append({"action": MENU_VERBS[action], "success": bool(r.get("success")),
                                                 "refused": bool(r.get("refused"))})
    return 0 if r.get("success") else AX_FAILURE


class GuardedAS:
    """ApplicationServices for this job: an AX action, a value write or a posted key asks the interlock
    first. ``base`` returns the module (or a fake). The value written never goes into a refusal. A menu press
    (``MENU_ROLES`` x ``MENU_VERBS``) is not sent raw: it runs as one checked executor step (``checked_menu_action``)."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        attr = getattr(self._base(), name)
        if name not in _GATED_AX:
            return attr

        def gated(*a, **kw):
            what = a[1] if len(a) > 1 and isinstance(a[1], str) else ""
            if name == "AXUIElementPerformAction" and what in MENU_VERBS and a and _is_menu(a[0]):
                return checked_menu_action(a[0], what)
            interlock(f"{_GATED_AX[name]} {what}".strip())
            return attr(*a, **kw)
        return gated


_GUARDED_AS = GuardedAS(lambda: _AS_BASE())


def _AS():
    return _GUARDED_AS


class GuardedExec:
    """The checked executor (``desktop_gen.Exec``) behind the interlock: presses, text entry and keys."""

    def __init__(self, ex):
        self.ex = ex

    def run(self, el, pid, selector, action, **kw):
        interlock(f"executor {action}")
        return self.ex.run(el, pid, selector, action, **kw)

    def __getattr__(self, name):
        return getattr(self.ex, name)


def install_guard():
    """The guest helper modules this job calls act through their own ``_AS`` (desktop_gen's
    ``canonical_frame``, ``AppCtl._close_windows``; desktop_gen2's ``read_group_marks2``): route it through
    the same guard. Called by ``serve`` (the job process only)."""
    G._AS = _AS
    G2._AS = _AS


def _open_path(path):
    """A folder opened in Finder (``open``): an app activation."""
    interlock("open in Finder")
    subprocess.run(["/usr/bin/open", exp(path)], capture_output=True, timeout=30)


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def exp(path):
    return os.path.realpath(os.path.expanduser(path))


def ours(path):
    p = exp(path)
    return p == GEN or p.startswith(GEN + os.sep)


# ── guards ──────────────────────────────────────────────────────────────────

def prompt_windows():
    import Quartz
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        owner = str(w.get("kCGWindowOwnerName") or "")
        layer = int(w.get("kCGWindowLayer") or 0)
        if any(o.lower() in owner.lower() for o in PROMPT_OWNERS) or layer == H.AUTH_LAYER:
            out.append({"owner": owner, "layer": layer, "pid": int(w.get("kCGWindowOwnerPID") or 0)})
    return out


def prompts(pid):
    try:
        ax = [p for p, _w in G2.auth_prompts(pid)]
    except Exception as exc:  # noqa: BLE001  (unreadable = treat as a prompt)
        ax = [f"unreadable: {exc!r}"[:80]]
    return ax, prompt_windows()


def guard_before(pid, where):
    if G.locked():
        raise Stop(f"screen locked ({where})")
    ax, wins = prompts(pid)
    if ax or wins:
        STATE["events"].append({"prompt_seen": where, "own": False, "ax": len(ax), "windows": wins})
        raise Prompt(f"prompt on screen before {where}: nothing done")


def guard_after(pid, where):
    if G.locked():
        raise Stop(f"screen locked ({where})")
    ax, wins = prompts(pid)
    if not ax and not wins:
        return
    STATE["events"].append({"prompt_seen": where, "own": True, "ax": len(ax), "windows": wins})
    for p, w in G2.auth_prompts(pid):
        cb = _at(w, "AXCancelButton")
        if cb is not None:
            _AS().AXUIElementPerformAction(cb, "AXPress")
    for w in wins:
        app = _AS().AXUIElementCreateApplication(w["pid"])
        for win in _at(app, "AXWindows") or []:
            cb = _at(win, "AXCancelButton")
            if cb is not None:
                _AS().AXUIElementPerformAction(cb, "AXPress")
    time.sleep(1.5)
    ax2, wins2 = prompts(pid)
    if ax2 or wins2:
        raise Stop(f"prompt after {where} not closed by its Cancel button")
    raise Prompt(f"prompt after {where}: cancelled through its Cancel button")


def in_auth_dialog(el):
    cur, n = el, 0
    while cur is not None and n < 30:
        role, sub = _s(_at(cur, "AXRole")) or "", _s(_at(cur, "AXSubrole")) or ""
        if role in E.DIALOG_ROLES or (role == "AXWindow" and sub in E.DIALOG_SUBROLES):
            return E.is_auth_prompt(G2.ax_tree(cur))
        cur, n = _at(cur, "AXParent"), n + 1
    return False


def secure(el):
    return E.SECURE_FIELD in ((_s(_at(el, "AXRole")) or ""), (_s(_at(el, "AXSubrole")) or "")) or in_auth_dialog(el)


# ── apps ────────────────────────────────────────────────────────────────────

def running(bid):
    return G.ddg._running(bid)


def pid_of(bid):
    a = running(bid)
    return int(a.processIdentifier()) if a is not None else None


def activate(bid):
    import AppKit
    a = running(bid)
    if a is None:
        return None
    interlock(f"activate {bid}")
    a.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.6)
    STATE["last_pid"] = int(a.processIdentifier())
    return STATE["last_pid"]


def open_app(bid, doc=None, fresh=True, wait=3.0):
    interlock(f"launch {bid}")
    args = ["/usr/bin/open"] + (["-F"] if fresh else []) + ["-b", bid] + ([exp(doc)] if doc else [])
    subprocess.run(args, capture_output=True, timeout=30)
    t = time.monotonic()
    while time.monotonic() - t < 25 and running(bid) is None:
        time.sleep(0.4)
    time.sleep(wait)
    pid = activate(bid)
    if pid:
        G.canonical_frame(pid)
    return pid


def clear_find_pasteboard(setup):
    """Lexikon comes up with a word in its search field (e.g. "Zeitgeist" after
    fresh launches) and looks it up in the chosen source at once; on Wikipedia or
    "Alle Lexika" that lookup can reach the web from the guest. The word is
    NOT the find pasteboard's (it came back after this); Lexikon keeps the field's
    last value itself, and an emptied field stays empty at the next launch. Kept as
    a harmless extra; the clean start and the restore empty the field."""
    try:
        import AppKit
        name = getattr(AppKit, "NSPasteboardNameFind", None) or getattr(AppKit, "NSFindPboard")
        AppKit.NSPasteboard.pasteboardWithName_(name).clearContents()
        setup.append("find pasteboard cleared")
    except Exception as exc:  # noqa: BLE001
        setup.append(f"find pasteboard not cleared: {exc!r}"[:120])


def open_dict(setup):
    clear_find_pasteboard(setup)
    pid = open_app(DICT, fresh=True)
    sf = search_field_els(pid) if pid else []
    setup.append(f"lexikon search field at launch: {(nfc(_s(_at(sf[0], 'AXValue')) or '') if sf else None)!r}")
    return pid


def web_source_item(bid, *labels):
    """A Lexikon menu item that selects a web source (never pressed)."""
    return bid == DICT and any(nfc(str(x or "")).split(" > ")[-1].strip() in WEB_SOURCES for x in labels)


def dict_on_web(pid):
    """Is a web source marked in Lexikon's Suchen menu (read with the menu open)?"""
    ti = top_index(pid, "Suchen")
    items = read_menu(pid, ti) if ti is not None else None
    if items is None:
        return True                      # unreadable: treated as on the web
    return any(it["mark"] and it["title"] in WEB_SOURCES for it in items)


def clear_dict_field(pid, setup):
    """Lexikon keeps its search field's word and looks it up at the next
    launch; the field is emptied before Lexikon quits, so every launch comes up empty."""
    sf = search_field_els(pid) if pid else []
    if sf and (_s(_at(sf[0], "AXValue")) or ""):
        _AS().AXUIElementSetAttributeValue(sf[0], "AXValue", "")
        time.sleep(0.5)
        setup.append("lexikon: search field emptied before quitting")


def quit_app(bid, setup):
    """TextEdit: close its (saved) windows first so that nothing is restored;
    then terminate; force-quit only if it does not go (reported)."""
    a = running(bid)
    if a is None:
        return
    interlock(f"quit {bid}")
    if bid == DICT:
        clear_dict_field(int(a.processIdentifier()), setup)
    if bid == TEXTEDIT:
        pid = int(a.processIdentifier())
        for w in list(_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []):
            b = _at(w, "AXCloseButton")
            if b is not None:
                _AS().AXUIElementPerformAction(b, "AXPress")
                time.sleep(0.5)
        time.sleep(0.8)
        if app_sheets(pid):
            setup.append("textedit: a sheet after closing its windows (nothing pressed in it)")
    a.terminate()
    t = time.monotonic()
    # NSRunningApplication.isTerminated is updated by a run loop this process does not
    # spin (the survey read it stale): ask LaunchServices afresh instead
    while time.monotonic() - t < 10 and running(bid) is not None:
        time.sleep(0.3)
    left = running(bid)
    if left is not None:
        left.forceTerminate()
        time.sleep(1.5)
        setup.append(f"{bid}: force-quit (did not quit in 10 s)")
    else:
        setup.append(f"{bid}: quit")
    time.sleep(0.8)


def front_window(pid):
    return G.front_window(pid) if pid else None


def doc_path(w):
    """A window's document path in NFC (AXDocument comes back
    decomposed, "Café" / "Entwürfe", while every name this harness compares is NFC)."""
    p = G._url_path(_at(w, "AXDocument")) if w is not None else None
    return nfc(p) if p else p


def find_all(el, pred, depth=0, out=None, budget=None, max_depth=16):
    out = [] if out is None else out
    budget = budget if budget is not None else [6000]
    budget[0] -= 1
    if el is None or depth > max_depth or budget[0] < 0:
        return out
    if pred(el):
        out.append(el)
    for k in _at(el, "AXChildren") or []:
        find_all(k, pred, depth + 1, out, budget, max_depth)
    return out


def role(el):
    return _s(_at(el, "AXRole")) or ""


def texts_of(el):
    return [nfc(_s(_at(t, "AXValue")) or "").strip() for t in
            find_all(el, lambda e: role(e) in ("AXTextField", "AXStaticText"))
            if (_s(_at(t, "AXValue")) or "").strip()]


def app_sheets(pid):
    out = []
    for w in _at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []:
        if (_s(_at(w, "AXSubrole")) or "") in E.DIALOG_SUBROLES:
            out.append(w)
        for k in _at(w, "AXChildren") or []:
            if role(k) == "AXSheet":
                out.append(k)
    return out


# ── menus ───────────────────────────────────────────────────────────────────

def menu_bar_items(pid):
    mb = _at(_AS().AXUIElementCreateApplication(pid), "AXMenuBar")
    return list(_at(mb, "AXChildren") or []) if mb is not None else []


def read_menu(pid, top_index):
    """[{path, title, id, cmd, mods, mark, enabled, ip}] of one top-level
    menu, read with the menu OPEN (AXPress on its bar item, then AXCancel)."""
    kids = menu_bar_items(pid)
    if top_index >= len(kids):
        return None
    bar = kids[top_index]
    top = nfc(_s(_at(bar, "AXTitle")) or "")
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.7)
    out = []

    def walk(menu, path, ip, depth):
        if depth > 5:
            return
        for j, it in enumerate(_at(menu, "AXChildren") or []):
            if role(it) != "AXMenuItem":
                continue
            t = nfc(_s(_at(it, "AXTitle")) or "")
            ident = G.item_identity(it)
            rec = {"path": " > ".join(path + [t]), "title": t, "id": ident["identifier"], "cmd": ident["cmd"],
                   "mods": ident["mods"], "mark": bool(ident["mark"].strip()), "enabled": bool(_at(it, "AXEnabled")),
                   "ip": ip + [j]}
            if t:
                out.append(rec)
            for sub in _at(it, "AXChildren") or []:
                if role(sub) == "AXMenu":
                    walk(sub, path + [t], ip + [j], depth + 1)
    for m in _at(bar, "AXChildren") or []:
        if role(m) == "AXMenu":
            walk(m, [top], [top_index], 1)
    _AS().AXUIElementPerformAction(bar, "AXCancel")
    time.sleep(0.3)
    return out


def top_index(pid, title):
    for i, k in enumerate(menu_bar_items(pid)):
        if nfc(_s(_at(k, "AXTitle")) or "") == title:
            return i
    return None


def menu_item_by_path(pid, path):
    """The live item under an OPEN top menu with this title path (harness setup only)."""
    parts = path.split(" > ")
    ti = top_index(pid, parts[0])
    if ti is None:
        return None, None
    bar = menu_bar_items(pid)[ti]
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.7)
    cur = bar
    for t in parts[1:]:
        menu = next((m for m in (_at(cur, "AXChildren") or []) if role(m) == "AXMenu"), None)
        hit = [k for k in (_at(menu, "AXChildren") or []) if role(k) == "AXMenuItem"
               and nfc(_s(_at(k, "AXTitle")) or "") == t] if menu is not None else []
        if len(hit) != 1:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
            return None, None
        cur = hit[0]
    return cur, bar


def setup_press_path(pid, path, want_marked, setup):
    """Harness setup: bring one menu option/flag to the declared state through a
    checked press (only if it differs). Reported, never counted as a step."""
    if path.split(" > ")[-1] in WEB_SOURCES and path.startswith("Suchen > "):
        setup.append(f"setup: {path!r} NOT pressed (a web source: never selected)")
        return False
    el, bar = menu_item_by_path(pid, path)
    if el is None:
        setup.append(f"setup: no item {path!r}")
        return False
    marked = bool((_s(_at(el, "AXMenuItemMarkChar")) or "").strip())
    if marked == want_marked:
        _AS().AXUIElementPerformAction(bar, "AXCancel")
        time.sleep(0.3)
        return True
    if X.irreversible_reason(_s(_at(el, "AXTitle"))):
        _AS().AXUIElementPerformAction(bar, "AXCancel")
        setup.append(f"setup: refused to press {path!r} (guard)")
        return False
    r = EX.run(el, pid, G.record_selector(pid, el), "press")
    time.sleep(1.0)
    setup.append(f"setup: pressed {path!r} ({'ok' if r['success'] else r['error']})")
    return r["success"]


# ── Finder / Notes readers (validated helpers) ────────────────────

def list_outline(pid, w=None):
    o = [e for e in find_all(w if w is not None else front_window(pid), lambda e: role(e) == "AXOutline")
         if _s(_at(e, "AXIdentifier")) == "ListView"]
    return o[0] if o else None


def finder_rows(pid, w=None):
    o = list_outline(pid, w)
    out = []
    for r in (_at(o, "AXRows") or []) if o is not None else []:
        tf = find_all(r, lambda e: role(e) == "AXTextField")
        if not tf:
            continue
        cells = [" ".join(texts_of(c)) for c in (_at(r, "AXChildren") or []) if role(c) == "AXCell"]
        out.append({"el": r, "label": nfc(_s(_at(tf[0], "AXValue")) or ""), "text": " | ".join(cells),
                    "selected": bool(_at(r, "AXSelected"))})
    return o, out


def finder_sidebar(pid):
    w = front_window(pid)
    outs = [e for e in find_all(w, lambda e: role(e) == "AXOutline")
            if _s(_at(e, "AXIdentifier")) != "ListView"]
    for o in outs:
        rows = []
        for r in _at(o, "AXRows") or []:
            tx = texts_of(r)
            if tx:
                rows.append((tx[0], r))
        if rows:
            return o, rows
    return None, []


def notes_sidebar(pid):
    w = front_window(pid)
    o = [e for e in find_all(w, lambda e: role(e) == "AXOutline") if (_s(_at(e, "AXDescription")) or "") == "Ordner"]
    if not o:
        return None, []
    rows = []
    for r in _at(o[0], "AXRows") or []:
        tf = find_all(r, lambda e: role(e) in ("AXTextField", "AXStaticText"))
        vals = [nfc(_s(_at(t, "AXValue")) or "").strip() for t in tf if (_s(_at(t, "AXValue")) or "").strip()]
        # the count badge lives in the cell's description: "AnyUse-Pick, 8 Notizen"
        count = None
        for c in _at(r, "AXChildren") or []:
            m = re.search(r",\s*(\d+)\s+Notiz", nfc(_s(_at(c, "AXDescription")) or ""))
            if m:
                count = int(m.group(1))
                break
        rows.append({"label": vals[0] if vals else "", "count": count, "el": r,
                     "selected": bool(_at(r, "AXSelected"))})
    return o[0], rows


def note_table(pid):
    t = [e for e in find_all(front_window(pid), lambda e: role(e) == "AXTable")
         if (_s(_at(e, "AXIdentifier")) or "").startswith("ICMNoteListTableView")]
    return t[0] if t else None


GALLERY_ID = "ICMNoteGridCollectionView_asGallery"
GALLERY_PREFIX = "ICMNoteGridCollectionView"


def gallery_cells(pid):
    """In the gallery view the notes are cells of an AX
    collection whose description reads "<title>, geändert: <time>, <preview>"."""
    # after a relaunch the same gallery carries the identifier "…_asList"
    g = find_all(front_window(pid), lambda e: (_s(_at(e, "AXIdentifier")) or "").startswith(GALLERY_PREFIX),
                 max_depth=12)
    if not g:
        return None
    out = []
    for c in find_all(g[0], lambda e: role(e) == "AXCell", max_depth=4):
        d = nfc(_s(_at(c, "AXDescription")) or "")
        m = re.match(r"^(.*?), geändert: [^,]*(?:, (.*))?$", d, re.S)
        title, prev = (m.group(1), (m.group(2) or "")) if m else (d, "")
        out.append({"el": c, "label": title.strip(), "text": f"{title.strip()} | {prev.strip()}".strip(" |"),
                    "line": prev.strip(), "selected": bool(_at(c, "AXSelected")), "gallery": True})
    return out


def _gallery_back(pid):
    """A note opened from the gallery hides the grid: Escape (an allowed key) to the
    note's window returns to the grid. True when the grid is listed again."""
    if gallery_cells(pid) is not None or note_table(pid) is not None:
        return True
    w = front_window(pid)
    if w is None:
        return False
    EX.run(w, pid, G.record_selector(pid, w), "press_key", keycode=53)
    for _ in range(6):
        time.sleep(0.5)
        if gallery_cells(pid) is not None:
            return True
    # else the toolbar's own "Zurück" button (not an irreversible label)
    back = [e for e in find_all(front_window(pid), lambda e: role(e) == "AXButton", max_depth=4)
            if nfc(_s(_at(e, "AXDescription")) or "") == "Zurück"]
    if len(back) == 1 and not X.irreversible_reason("Zurück"):
        EX.run(back[0], pid, G.record_selector(pid, back[0]), "press")
        for _ in range(6):
            time.sleep(0.5)
            if gallery_cells(pid) is not None:
                return True
    return False


def _gallery_open(pid, cell, label):
    """A gallery cell's selection is not settable; the cell's
    own AXPress (through the checked executor, the guard on its label) selects /
    opens it; a second press, then Return (the one key besides Escape) if the
    note is still not shown. Read back through the note's text area."""
    tried = []
    for how in ("press", "press", "return"):
        cells = [c for c in (gallery_cells(pid) or []) if c["label"] == label]
        el = cells[0]["el"] if cells else cell["el"]
        if X.irreversible_reason(label, _s(_at(el, "AXDescription"))):
            return {"status": "gated", "detail": "irreversible-action guard on the note's label"}
        if how == "press":
            r = EX.run(el, pid, G.record_selector(pid, el), "press")
        else:
            r = EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=36)
        time.sleep(1.5)
        body = (note_body(pid) or "").split("\n")[0]
        tried.append(f"{how}:{r.get('success')}:{body[:30]!r}")
        if body == label:
            return {"status": "done", "detail": "gallery " + " ".join(tried)}
    return {"status": "failed", "detail": "gallery " + " ".join(tried)}


def notes_searching(pid):
    """Is a Notes search active (its search field holds text)?"""
    sf = search_field_els(pid)
    return bool(sf and (_s(_at(sf[0], "AXValue")) or "").strip())


def note_rows(pid):
    t = note_table(pid)
    if t is None:
        cells = gallery_cells(pid)
        if cells is not None:
            return cells
    out = []
    searching = notes_searching(pid) if t is not None else False
    for r in (_at(t, "AXRows") or []) if t is not None else []:
        cell = find_all(r, lambda e: _s(_at(e, "AXIdentifier")) == "ICMNoteListCell")
        if not cell:
            continue
        tx = texts_of(cell[0])
        if searching and len(tx) >= 4:
            # during a search a cell reads [match snippet, time, folder, title]; the TITLE is the row's label
            # (a snippet such as "…Kollegin, Umzug ins 3. OG" can come from another note naming the searched
            # word), the snippet and the folder are its text
            out.append({"el": r, "label": tx[-1], "text": f"{tx[-1]} | {tx[0]} | in {tx[-2]}", "line": tx[0],
                        "folder": tx[-2], "selected": bool(_at(r, "AXSelected")), "search_row": True})
            continue
        out.append({"el": r, "label": tx[0] if tx else "", "text": " | ".join(tx[:3]),
                    "line": tx[1] if len(tx) > 1 else "", "selected": bool(_at(r, "AXSelected"))})
    return out


def note_body(pid):
    b = find_all(front_window(pid), lambda e: role(e) == "AXTextArea"
                 and _s(_at(e, "AXIdentifier")) == "Note Body Text View")
    return nfc(_s(_at(b[0], "AXValue")) or "") if b else None


def notes_shown_folder(pid):
    _o, rows = notes_sidebar(pid)
    sel = [r["label"] for r in rows if r["selected"]]
    return sel[0] if len(sel) == 1 else None


def search_field_els(pid):
    return find_all(front_window(pid), lambda e: (_s(_at(e, "AXSubrole")) or "") == "AXSearchField"
                    or role(e) == "AXSearchField")


# ── ops ─────────────────────────────────────────────────────────────────────

def need_managed(bid):
    if bid not in X.MANAGED:
        raise ValueError(f"{bid!r} is not one of this test's apps")


def op_hello(a):
    st = G2._session_state()
    wins, regular = G2._onscreen_windows()
    return {"status": "done", "vm": True, "locked": st["locked"], "auth_prompts": st["auth_prompts"],
            "prompt_windows": prompt_windows(), "leftover": H.leftover_windows(wins, regular),
            "apps": st["apps"], "boot": subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                                                       text=True).stdout.strip(),
            "src": os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "interlock": interlock_status(), "scope_reader": dict(SCOPE_READER)}


def op_front(a):
    bid = CS.frontmost_bundle()
    pid = pid_of(bid) if bid else None
    w = front_window(pid)
    return {"status": "done", "front_bundle": bid, "title": nfc(_s(_at(w, "AXTitle")) or "") if w else None,
            "document": doc_path(w) if w is not None else None}


def op_launch(a):
    bid = a["bundle"]
    need_managed(bid)
    pid = pid_of(bid)
    if pid:
        guard_before(pid, "launch")
        activate(bid)
    else:
        guard_before(None, "launch")
        pid = open_app(bid, fresh=False)
    guard_after(pid, "launch")
    return {"status": "done" if pid else "failed", "pid": pid}


def op_rows(a):
    bid, kind = a["bundle"], a["kind"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "failed", "detail": "not running"}
    w = front_window(pid)
    title = nfc(_s(_at(w, "AXTitle")) or "") if w is not None else ""
    if kind == "finder_files":
        view, rows = finder_items(pid)
        path = STATE.get("finder_folder")
        return {"status": "done", "where": title, "view": view,
                "path": path if path and os.path.basename(path) == title else None,
                "rows": [{"label": r["label"], "text": r["text"], "selected": r["selected"], "kind": r["kind"]}
                         for r in rows]}
    if kind == "finder_locations":
        # the subfolders of the folder shown and the folder above it (inside this
        # test's data) come first; sidebar places that reach a network or cloud never appear
        _o, side = finder_sidebar(pid)
        _v, items = finder_items(pid)
        path = STATE.get("finder_folder")
        if not title:
            # with no Finder window the front "window" is the desktop (no title): no places
            return {"status": "done", "where": "", "path": None, "rows": []}
        rows = [{"label": title, "text": f"{title} (the folder shown now)", "shown": True, "via": "shown"}]
        rows += [{"label": r["label"], "text": f"{r['label']} (subfolder of the folder shown)", "shown": False,
                  "via": "sub"} for r in items if r["kind"] == "folder"]
        if path and os.path.basename(path) == title and ours(os.path.dirname(path)) \
                and os.path.dirname(path) != GEN:
            up = nfc(os.path.basename(os.path.dirname(path)))
            rows.append({"label": up, "text": f"{up} (the folder above the shown one)", "shown": False, "via": "up"})
        rows += [{"label": n, "text": f"{n} (sidebar)", "shown": False, "via": "sidebar"} for n, _r in side
                 if n != title and not any(x in X.norm(n) for x in NO_NET_PLACES)]
        return {"status": "done", "where": title, "path": path if path and os.path.basename(path) == title else None,
                "rows": rows}
    if kind == "notes_notes":
        rows = note_rows(pid)
        body = note_body(pid)
        went_back, before = False, None
        if a.get("back") and not rows and note_table(pid) is None and body is not None:
            before = (body or "").split("\n")[0]
            if _gallery_back(pid):
                went_back = True
                rows = note_rows(pid)        # asked for the candidates: back to the grid (a note opened
                body = note_body(pid)        # from the gallery hides it); a plain read never goes back
        return {"status": "done", "where": notes_shown_folder(pid), "shown_note": (body or "").split("\n")[0],
                "shown_body": (body or "")[:600], "went_back": went_back, "shown_before": before,
                "rows": [{"label": r["label"], "text": r["text"], "selected": r["selected"]} for r in rows]}
    if kind == "notes_folders":
        _o, rows = notes_sidebar(pid)
        where = notes_shown_folder(pid)
        return {"status": "done", "where": where,
                "rows": [{"label": r["label"], "text": r["label"], "shown": r["label"] == where}
                         for r in rows if r["label"]]}
    raise ValueError(f"unknown rows kind {kind!r}")


def _select(pid, kind, label, via=None):
    """Select the ONE row carrying exactly this label (identity check right
    before the write), through the settable selection attribute; read back."""
    label = nfc(label)
    if kind == "finder_files":
        o, rows = finder_rows(pid)
        hit = [r for r in rows if r["label"] == label]
        if len(hit) != 1:
            return {"status": "refused", "detail": f"{len(hit)} rows labelled {label!r}"}
        err = _AS().AXUIElementSetAttributeValue(o, "AXSelectedRows", [hit[0]["el"]])
        time.sleep(0.7)
        sel = [r["label"] for r in finder_rows(pid)[1] if r["selected"]]
        return {"status": "done" if sel == [label] else "failed", "detail": f"err={int(err)} selected={sel}"}
    if kind == "notes_notes":
        rows = note_rows(pid)
        if not any(r["label"] == label for r in rows) and note_table(pid) is None and _gallery_back(pid):
            rows = note_rows(pid)
        hit = [r for r in rows if r["label"] == label]
        if len(hit) != 1:
            return {"status": "refused", "detail": f"{len(hit)} notes titled {label!r}"}
        if hit[0].get("gallery"):
            return _gallery_open(pid, hit[0], label)
        err = _AS().AXUIElementSetAttributeValue(hit[0]["el"], "AXSelected", True)
        time.sleep(1.0)
        full = note_body(pid) or ""
        body = full.split("\n")[0]
        # during a notes search a row shows a match snippet ("…Gast-Passwort: …",
        # "Verworfen: Zelt, …"), not the title: the note shown must then contain the snippet
        snip = label.lstrip("…").strip()
        ok = body == label or bool(snip and snip.rstrip("…").strip() in full)
        return {"status": "done" if ok else "failed", "detail": f"err={int(err)} shown={body!r}",
                "shown_note": body}
    if kind == "notes_folders":
        o, rows = notes_sidebar(pid)
        hit = [r for r in rows if r["label"] == label]
        if len(hit) != 1:
            return {"status": "refused", "detail": f"{len(hit)} folders named {label!r}"}
        for _ in range(2):
            err = _AS().AXUIElementSetAttributeValue(o, "AXSelectedRows", [hit[0]["el"]])
            time.sleep(1.5)
            if notes_shown_folder(pid) == label:
                break
            o, rows = notes_sidebar(pid)
            hit = [r for r in rows if r["label"] == label] or hit
        now = notes_shown_folder(pid)
        return {"status": "done" if now == label else "failed", "detail": f"err={int(err)} shown={now!r}"}
    if kind == "finder_locations":
        w = front_window(pid)
        if nfc(_s(_at(w, "AXTitle")) or "") == label:
            return {"status": "done", "detail": "already shown"}
        if via in ("sub", "up"):
            return _enter_folder(pid, label) if via == "sub" else _up_folder(pid)
        if via is None:
            _v, items = finder_items(pid)
            if any(r["label"] == label and r["kind"] == "folder" for r in items):
                return _enter_folder(pid, label)
        if any(x in X.norm(label) for x in NO_NET_PLACES):
            return {"status": "refused", "detail": f"{label!r} reaches a network or cloud place: never entered"}
        o, side = finder_sidebar(pid)
        hit = [r for n, r in side if n == label]
        if len(hit) != 1:
            return {"status": "refused", "detail": f"{len(hit)} sidebar rows named {label!r}"}
        err = _AS().AXUIElementSetAttributeValue(o, "AXSelectedRows", [hit[0]])
        time.sleep(1.2)
        now = nfc(_s(_at(front_window(pid), "AXTitle")) or "")
        return {"status": "done" if now == label else "failed", "detail": f"err={int(err)} shown={now!r}"}
    raise ValueError(f"unknown select kind {kind!r}")


def op_select(a):
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "failed", "detail": "not running"}
    guard_before(pid, f"select {a['label']!r}")
    r = _select(pid, a["kind"], a["label"], a.get("via"))
    guard_after(pid, f"select {a['label']!r}")
    return r


def op_marks(a):
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "done", "ok": False, "detail": "not running"}
    if any(isinstance(o, dict) and o.get("identity") for o in a["options"]):
        t, others = marks_by_identity(pid, a["parent"], a["options"])
    else:
        t, others = G2.read_group_marks2(pid, a["parent"], a["options"])
    return {"status": "done", "ok": t is not None, "target": t, "sentinel": others}


# ── a choice option found by its identity (execution/choice_identity.py) ─────────────

def _menu_item_identities(pid, top=None):
    """[(element, item identity)] of every live menu item (of the top-level menu ``top`` when given)."""
    return [(el, G.item_identity(el)) for el, path in G.menu_items(pid)
            if top is None or (path and path[0] == top)]


def resolve_menu_trigger(pid, trig):
    """(element, identity, why). A trigger that carries ``identity`` (a parameterized routine's choice option) is
    found by that identity among the live items of its top-level menu, so another UI language or a reordered
    list does not move it (``choice_identity.resolve_live``); any other trigger exactly as before
    (``desktop_gen.resolve_trigger``: its identifier and shortcut, exactly one live item)."""
    ident = trig.get("identity")
    if not ident:
        return G.resolve_trigger(pid, trig)
    top = (trig.get("index_path") or [None])[0]
    items = [(el, i) for el, i in _menu_item_identities(pid, top) if i["role"] == "AXMenuItem"]
    k, why = CI.resolve_live(ident, [i for _e, i in items])
    if k is None:
        return None, None, f"option {ident.get('key')!r} not found by its identity: {why}"
    return items[k][0], items[k][1], None


def marks_by_identity(pid, parent_path, options):
    """``desktop_gen2.read_group_marks2`` for options that carry their identity: the group's top-level menu is
    opened (an AX action, behind the interlock), every item read with its title, the menu closed; each option is
    found by ``choice_identity.resolve_live`` (an option without identity by its trigger, exactly once).
    Returns (target, sentinel) in read_group_marks2's shape."""
    app = _AS().AXUIElementCreateApplication(pid)
    mb = _at(app, "AXMenuBar")
    bar = (_at(mb, "AXChildren") or [])[parent_path[0]] if mb is not None else None
    if bar is None:
        return None, None
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.8)
    items = []

    def walk(el, depth, parent):
        if depth > 6:
            return
        for k in _at(el, "AXChildren") or []:
            r = _s(_at(k, "AXRole"))
            if r == "AXMenuItem":
                it = G.item_identity(k)
                it["parent"] = parent
                items.append(it)
                for m in [m for m in (_at(k, "AXChildren") or []) if _s(_at(m, "AXRole")) == "AXMenu"]:
                    walk(m, depth + 1, f"{it['identifier']}|{it['cmd']}|{it['mods']}")
            elif r == "AXMenu":
                walk(k, depth + 1, parent)
    walk(bar, 0, None)
    _AS().AXUIElementPerformAction(bar, "AXCancel")
    time.sleep(0.4)
    opt_keys, selected, used = [], [], set()
    for o in options:
        if o.get("identity"):
            k, _why = CI.resolve_live(o["identity"], items)
            hit = [] if k is None else [k]
        else:
            hit = [i for i, it in enumerate(items) if G.trigger_matches(it, o)]
        if len(hit) == 1:
            opt_keys.append(o["key"])
            used.add(hit[0])
            if items[hit[0]]["mark"].strip():
                selected.append(o["key"])
    others = sorted(((it["parent"] + ">") if it["parent"] else "") + f"{it['identifier']}|{it['cmd']}|{it['mods']}"
                    for i, it in enumerate(items) if i not in used and it["mark"].strip())
    return {"options": opt_keys, "selected": selected}, others


def op_press(a):
    bid, trig = a["bundle"], a["trigger"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "failed", "detail": "not running"}
    guard_before(pid, f"press {a.get('label')!r}")
    kids = menu_bar_items(pid)
    ip = trig.get("index_path") or []
    bar = kids[ip[0]] if ip and ip[0] < len(kids) else None
    if bar is not None:
        _AS().AXUIElementPerformAction(bar, "AXPress")
        time.sleep(0.8)
    el, ident, why = resolve_menu_trigger(pid, trig)
    if el is None:
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
        return {"status": "refused", "detail": why}
    gate = X.irreversible_reason(ident.get("title"), _s(_at(el, "AXDescription")))
    if gate:
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
        return {"status": "gated", "detail": f"irreversible-action guard: {gate}"}
    if web_source_item(bid, ident.get("title")):
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
        return {"status": "refused", "detail": f"{ident.get('title')!r} reaches the web: never pressed"}
    if not bool(_at(el, "AXEnabled")):
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
        return {"status": "failed", "detail": f"{ident.get('title')!r} is disabled"}
    r = EX.run(el, pid, G.record_selector(pid, el), "press")
    time.sleep(1.2)
    if not r["success"] and bar is not None:
        _AS().AXUIElementPerformAction(bar, "AXCancel")
        time.sleep(0.3)
    guard_after(pid, f"press {ident.get('title')!r}")
    return {"status": "done" if r["success"] else ("refused" if r["refused"] else "failed"),
            "detail": f"{ident.get('title')!r} {r.get('outcome') or ''} {r.get('error') or ''}".strip()}


def doc_window(pid, document):
    """(the ONE window of the app that shows ``document`` (its AXDocument), None) or (None, why). TextEdit's text
    area ("First Text View") is in every document window: a lookup scoped by the document never reads or writes
    another document's."""
    wins = [w for w in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []) if doc_path(w) == exp(document)]
    if len(wins) != 1:
        return None, f"{len(wins)} windows show {document!r}"
    return wins[0], None


def _target_el(pid, t):
    if t.get("focused"):
        el = _at(_AS().AXUIElementCreateApplication(pid), "AXFocusedUIElement")
        if el is None:
            return None, "nothing focused"
        if role(el) not in TEXT_ROLES:
            return None, f"the focused element is a {role(el)}, not a text entry"
        if t.get("search") and (_s(_at(el, "AXSubrole")) or "") != "AXSearchField":
            return None, "the focused entry is not a search field"
        return el, None
    if "search_field" in t:
        els = search_field_els(pid)
        i = int(t["search_field"])
        return (els[i], None) if 0 <= i < len(els) else (None, f"no search field {i}")
    if t.get("document"):
        scope, why = doc_window(pid, t["document"])
        if scope is None:
            return None, why
    else:
        scope = front_window(pid)
    els = find_all(scope, lambda e: role(e) == t["role"] and (_s(_at(e, "AXIdentifier")) or "") == t["identifier"])
    if len(els) != 1:
        return None, f"{len(els)} {t['role']} carry the identifier {t['identifier']!r}"
    return els[0], None


def op_enter(a):
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "failed", "detail": "not running"}
    guard_before(pid, "enter")
    if bid == DICT and dict_on_web(pid):
        return {"status": "refused", "detail": "Lexikon is on a web source: nothing typed"}
    el, why = _target_el(pid, a["target"])
    if el is None:
        return {"status": "refused", "detail": why}
    if secure(el):
        return {"status": "refused", "detail": "secure field or authentication dialog: never typed into"}
    sel = G.record_selector(pid, el)
    kw = {"text": a["text"]}
    if a.get("keys") and (_s(_at(el, "AXSubrole")) or "") != "AXSearchField":
        kw["keys"] = True
    r = EX.run(el, pid, sel, "enter_text", **kw)
    if r["refused"] or not r["success"]:
        guard_after(pid, "enter")
        return {"status": "refused" if r["refused"] else "failed", "detail": r.get("error")}
    if a.get("commit"):
        # the commit targets the element just typed into; a field without a title or
        # description is named by its value, which the entry has just changed (Lexikon's
        # search field): its selector is recorded again from the same element, and the
        # executor's identity check then compares the live element with that
        r2 = EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=36)
        time.sleep(0.8)
        if r2["refused"]:
            guard_after(pid, "enter commit")
            return {"status": "refused", "detail": f"commit: {r2.get('error')}"}
    time.sleep(0.8)
    guard_after(pid, "enter")
    return {"status": "done", "detail": "entered" + (" + Return" if a.get("commit") else "")}


def op_field(a):
    """A field's value: in the front window, or -- ``document`` given -- in the window that shows that document."""
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    scope = front_window(pid) if pid else None
    if pid and a.get("document"):
        scope, why = doc_window(pid, a["document"])
        if scope is None:
            return {"status": "done", "ok": False, "count": 0, "value": None, "detail": why}
    els = find_all(scope, lambda e: role(e) == a["role"] and
                   (_s(_at(e, "AXIdentifier")) or "") == a["identifier"]) if scope is not None else []
    return {"status": "done", "ok": len(els) == 1, "count": len(els),
            "value": nfc(_s(_at(els[0], "AXValue")) or "") if len(els) == 1 else None}


def op_search_fields(a):
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    out = []
    for i, el in enumerate(search_field_els(pid) if pid else []):
        d = nfc(_s(_at(el, "AXDescription")) or _s(_at(el, "AXPlaceholderValue")) or
                _s(_at(el, "AXIdentifier")) or "")
        out.append({"index": i, "text": f"search field {d}".strip(), "value": nfc(_s(_at(el, "AXValue")) or "")})
    return {"status": "done", "fields": out}


def op_shown_folder(a):
    pid = activate(FINDER)
    w = front_window(pid)
    doc = doc_path(w) if w is not None else None
    if doc and ours(doc):
        return {"status": "done", "path": doc}
    f = STATE.get("finder_folder")
    title = nfc(_s(_at(w, "AXTitle")) or "") if w is not None else ""
    if f and os.path.basename(f) == title:
        return {"status": "done", "path": f, "via": "window title = the folder the clean start opened"}
    return {"status": "done", "path": None, "detail": f"window {title!r}"}


def op_listing(a):
    p = exp(a["path"])
    if not ours(p):
        raise ValueError("not this test's folder")
    # names in NFC: Finder lists a folder it created decomposed ('Ru\u0308ckfragen')
    return {"status": "done", "names": sorted(nfc(n) for n in os.listdir(p) if not n.startswith("."))}


def file_info(p):
    try:
        b = open(p, "rb").read()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "text": b.decode("utf-8", "replace") if len(b) < 65536 else None,
            "sha256": hashlib.sha256(b).hexdigest(), "mtime": os.path.getmtime(p), "size": len(b)}


def op_file(a):
    p = exp(a["path"])
    if not ours(p):
        raise ValueError("not this test's file")
    return dict(file_info(p), status="done")


# ── containers, content, the Open panel ──
# Finder's windows carry no AXDocument in this guest: the folder a
# window shows is TRACKED from the clean start's folder through every verified
# move (``STATE["finder_folder"]``), and every move is verified twice: the
# window's title is the folder's name AND the names the window lists (AX) are
# exactly the folder's entries on disk. Quick Look (Ablage > Übersicht) shows a
# text file's content as an AXTextArea; TextEdit's Open panel is a
# window of TextEdit itself (identifier "open-panel") with a column browser.

#: sidebar places never offered or entered (network, cloud, sharing: no web contact)
NO_NET_PLACES = ("icloud", "geteilt", "netzwerk", "airdrop", "my shared files", "server", "shared")


def _listing(path):
    try:
        return sorted(nfc(n) for n in os.listdir(path) if not n.startswith("."))
    except OSError:
        return None


def finder_view(pid, w=None):
    w = w if w is not None else front_window(pid)
    if w is None:
        return "none", None
    if list_outline(pid, w) is not None:
        return "list", None
    br = find_all(w, lambda e: role(e) == "AXBrowser")
    return ("columns", br[0]) if br else ("other", None)


def _col_lists(browser):
    return find_all(browser, lambda e: role(e) == "AXList") if browser is not None else []


def _col_items(li):
    out = []
    for g in _at(li, "AXChildren") or []:
        tf = find_all(g, lambda e: role(e) == "AXTextField", max_depth=2)
        if tf:
            out.append({"el": g, "label": nfc(_s(_at(tf[0], "AXValue")) or "")})
    return out


def finder_items(pid, w=None):
    """(view, rows): the items of the folder the front window (or ``w``) shows,
    each with its label, text, kind (folder | file) and selection. List view:
    the rows (kind from the Art column, "Ordner"); column view: the last column
    (kind from the tracked folder on disk)."""
    view, br = finder_view(pid, w)
    if view == "list":
        _o, rows = finder_rows(pid, w)
        for r in rows:
            r["kind"] = "folder" if r["text"].split(" | ")[-1].strip() == "Ordner" else "file"
        return view, rows
    if view == "columns":
        lists = _col_lists(br)
        if not lists:
            return view, []
        base = STATE.get("finder_folder") or ""
        sel = set()
        for ch in _at(lists[-1], "AXSelectedChildren") or []:
            tf = find_all(ch, lambda e: role(e) == "AXTextField", max_depth=2)
            if tf:
                sel.add(nfc(_s(_at(tf[0], "AXValue")) or ""))
        rows = []
        for it in _col_items(lists[-1]):
            kind = "folder" if base and os.path.isdir(os.path.join(base, it["label"])) else "file"
            rows.append({"el": it["el"], "label": it["label"], "text": f"{it['label']} | {kind}", "kind": kind,
                         "selected": it["label"] in sel, "list": lists[-1]})
        return view, rows
    return view, []


def _verify_shown(pid, path, view=None):
    """The front window shows ``path``: its title is the folder's name (list
    view) and the names it lists are exactly the folder's entries on disk."""
    want = _listing(path)
    if want is None:
        return False, f"{path!r} not readable"
    for _ in range(8):
        v, rows = finder_items(pid)
        title = nfc(_s(_at(front_window(pid), "AXTitle")) or "")
        names = sorted(r["label"] for r in rows)
        if names == want and (v != "list" or title == nfc(os.path.basename(path))):
            return True, f"title {title!r}, {len(names)} names = the folder's entries"
        time.sleep(0.6)
    return False, f"title {title!r}, names {names[:8]} != entries {want[:8]}"


def _enter_folder(pid, label):
    """Go INTO the subfolder ``label`` of the folder shown (list view: select
    its row, Ablage > Öffnen; column view: select it in the last column)."""
    cur = STATE.get("finder_folder")
    if not cur:
        return {"status": "refused", "detail": "the folder shown is not known (no verified start or move)"}
    view, rows = finder_items(pid)
    title = nfc(_s(_at(front_window(pid), "AXTitle")) or "")
    if view == "list" and title != nfc(os.path.basename(cur)):
        return {"status": "refused", "detail": f"the window shows {title!r}, not the tracked folder {cur!r}"}
    hit = [r for r in rows if r["label"] == nfc(label) and r["kind"] == "folder"]
    if len(hit) != 1:
        return {"status": "refused", "detail": f"{len(hit)} subfolders named {label!r} in the folder shown"}
    new = os.path.join(cur, hit[0]["label"])
    if not ours(new):
        return {"status": "refused", "detail": "not this test's folder"}
    if view == "list":
        r = _select(pid, "finder_files", label)
        if r["status"] != "done":
            return r
        trig = X.EffectMap().open_trigger("Finder")
        r = op_press({"bundle": FINDER, "trigger": trig["trigger"], "label": trig["path"]})
        if r["status"] != "done":
            return r
    else:
        err = _AS().AXUIElementSetAttributeValue(hit[0]["list"], "AXSelectedChildren", [hit[0]["el"]])
        time.sleep(1.2)
        if int(err):
            return {"status": "failed", "detail": f"column selection err={int(err)}"}
        STATE["finder_folder"] = new           # the column view lists the selected folder's items next
    ok, why = _verify_shown(pid, new, view)
    if ok:
        STATE["finder_folder"] = new
    elif view != "list":
        STATE["finder_folder"] = cur
    return {"status": "done" if ok else "failed", "detail": f"entered {label!r}: {why}", "path": new if ok else cur}


def _menu_first(pid, path):
    """The first ENABLED live item with this title path (Finder lists some
    items twice, e.g. an alternate with other modifiers)."""
    parts = path.split(" > ")
    ti = top_index(pid, parts[0])
    if ti is None:
        return None, None
    bar = menu_bar_items(pid)[ti]
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.7)
    cur = bar
    for t in parts[1:]:
        menu = next((m for m in (_at(cur, "AXChildren") or []) if role(m) == "AXMenu"), None)
        hit = [k for k in (_at(menu, "AXChildren") or []) if role(k) == "AXMenuItem"
               and nfc(_s(_at(k, "AXTitle")) or "") == t and bool(_at(k, "AXEnabled"))] if menu is not None else []
        if not hit:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
            return None, None
        cur = hit[0]
    return cur, bar


def _up_folder(pid):
    cur = STATE.get("finder_folder")
    if not cur or not ours(os.path.dirname(cur)):
        return {"status": "refused", "detail": "no folder above the shown one inside this test's data"}
    el, bar = _menu_first(pid, "Gehe zu > Übergeordneter Ordner")
    if el is None:
        return {"status": "refused", "detail": "no enabled 'Übergeordneter Ordner'"}
    guard_before(pid, "up")
    r = EX.run(el, pid, G.record_selector(pid, el), "press")
    time.sleep(1.2)
    guard_after(pid, "up")
    if not r["success"]:
        return {"status": "failed", "detail": r.get("error")}
    ok, why = _verify_shown(pid, os.path.dirname(cur))
    if ok:
        STATE["finder_folder"] = os.path.dirname(cur)
    return {"status": "done" if ok else "failed", "detail": f"up: {why}", "path": STATE["finder_folder"]}


def _finder_main(pid):
    """The Finder window that shows the tracked folder (not Quick Look)."""
    path = STATE.get("finder_folder") or ""
    wins = [x for x in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
            if (_s(_at(x, "AXSubrole")) or "") == "AXStandardWindow"]
    hit = [x for x in wins if nfc(_s(_at(x, "AXTitle")) or "") == nfc(os.path.basename(path))] if path else []
    return hit[0] if hit else (wins[0] if wins else front_window(pid))


def _finder_select_label(pid, label, w=None):
    """Select the ONE item with this label in the folder shown (list or column
    view) of the front window or ``w``; read back."""
    view, rows = finder_items(pid, w)
    hit = [r for r in rows if r["label"] == nfc(label)]
    if len(hit) != 1:
        return {"status": "refused", "detail": f"{len(hit)} items labelled {label!r}"}
    if view == "list":
        o = list_outline(pid, w)
        err = _AS().AXUIElementSetAttributeValue(o, "AXSelectedRows", [hit[0]["el"]])
        time.sleep(0.7)
        sel = [r["label"] for r in finder_rows(pid, w)[1] if r["selected"]]
        return {"status": "done" if sel == [nfc(label)] else "failed", "detail": f"err={int(err)} selected={sel}"}
    err = _AS().AXUIElementSetAttributeValue(hit[0]["list"], "AXSelectedChildren", [hit[0]["el"]])
    time.sleep(0.8)
    return {"status": "done" if not int(err) else "failed", "detail": f"err={int(err)}"}


# Quick Look: the text of a selected text file (read only; left with Escape)

def _ql_window(pid):
    for x in _at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []:
        if (_s(_at(x, "AXSubrole")) or "") == "Quick Look":
            return x
    return None


def _ql_read(w):
    areas = find_all(w, lambda e: role(e) == "AXTextArea", max_depth=6)
    names = [nfc(_s(_at(e, "AXValue")) or "") for e in find_all(w, lambda e: role(e) == "AXStaticText", max_depth=3)]
    return (nfc(_s(_at(areas[0], "AXValue")) or "") if len(areas) == 1 else None), names


def _ql_close(pid):
    """Escape (the one key besides Return) to Quick Look, then wait until it is gone."""
    for _ in range(2):
        w = _ql_window(pid)
        if w is None:
            return True
        EX.run(w, pid, G.record_selector(pid, w), "press_key", keycode=53)
        for _ in range(8):
            time.sleep(0.5)
            if _ql_window(pid) is None:
                return True
    return _ql_window(pid) is None


def _ql_open(pid):
    ti = top_index(pid, "Ablage")
    items = [it for it in (read_menu(pid, ti) or []) if it["path"].split(" > ")[-1].startswith("Übersicht")
             and it["enabled"]] if ti is not None else []
    if len(items) != 1:
        raise RuntimeError(f"{len(items)} 'Übersicht' items")
    el, bar = menu_item_by_path(pid, items[0]["path"])
    if el is None or X.irreversible_reason(_s(_at(el, "AXTitle"))):
        raise RuntimeError("no Quick Look item (or held by the guard)")
    guard_before(pid, "quick look")
    EX.run(el, pid, G.record_selector(pid, el), "press")
    guard_after(pid, "quick look")


def _ql_wait_text(pid, lab, tries=6):
    for _ in range(tries):
        time.sleep(0.7)
        w = _ql_window(pid)
        if w is None:
            continue
        t, names = _ql_read(w)
        if t is not None and nfc(lab) in names:
            return t
    return None


def ql_texts(pid, labels):
    """{label: text} for these file items of the folder shown, read from Quick
    Look (Ablage > Übersicht; it follows the selection, else it is closed and
    opened again for the item; closed with Escape at the end); the folder
    window's selection is put back as it was."""
    mw = _finder_main(pid)
    view, rows = finder_items(pid, mw)
    prev = [r["label"] for r in rows if r["selected"]]
    out = {}
    try:
        for lab in labels:
            r = _finder_select_label(pid, lab, mw)
            if r["status"] != "done":
                out[lab] = None
                continue
            txt = _ql_wait_text(pid, lab, tries=3) if _ql_window(pid) is not None else None
            if txt is None:
                _ql_close(pid)
                _finder_select_label(pid, lab, _finder_main(pid))
                _ql_open(pid)
                txt = _ql_wait_text(pid, lab)
            out[lab] = txt
    finally:
        _ql_close(pid)
        mw = _finder_main(pid)
        if view == "list":
            o = list_outline(pid, mw)
            keep = [r["el"] for r in finder_rows(pid, mw)[1] if r["label"] in prev]
            if o is not None:
                _AS().AXUIElementSetAttributeValue(o, "AXSelectedRows", keep)
                time.sleep(0.5)
    return out, _ql_window(pid) is None


def op_previews(a):
    """Content snippets for picker candidates: the Quick Look text of each named
    file item of the folder Finder shows."""
    pid = activate(FINDER)
    if not pid:
        return {"status": "failed", "detail": "Finder not running"}
    guard_before(pid, "previews")
    texts, closed = ql_texts(pid, [nfc(x) for x in a.get("labels") or []][:12])
    guard_after(pid, "previews")
    return {"status": "done", "previews": {k: (v[:400] if isinstance(v, str) else None) for k, v in texts.items()},
            "ql_closed": closed}


def te_read_source(named, path):
    """A TextEdit read's ``source`` label: the NAMED document (front or behind: the read is scoped by it) by its file
    name, else the front one (a named back document is never labelled "the front ...")."""
    if named:
        return f"the TextEdit document {os.path.basename(path or named)!r}"
    return "the front TextEdit document"


def op_read_text(a):
    """read_content: the text an app shows, through AX. TextEdit: the front
    document's text area (AXValue); Notes: the note shown (its body), or the
    note list (title and first line of each note); Finder: the selected file's
    Quick Look text; Lexikon: the texts of its window."""
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "done", "ok": False, "detail": "not running"}
    what = X.norm(a.get("what") or "")
    if bid == TEXTEDIT:
        w = front_window(pid)
        if a.get("document"):
            w, why = doc_window(pid, a["document"])          # scoped by the document, never another window's area
            if w is None:
                return {"status": "done", "ok": False, "detail": why}
        areas = find_all(w, lambda e: role(e) == "AXTextArea" and _s(_at(e, "AXIdentifier")) == "First Text View")
        if len(areas) != 1:
            return {"status": "done", "ok": False, "detail": f"{len(areas)} document text areas"}
        return {"status": "done", "ok": True, "text": nfc(_s(_at(areas[0], "AXValue")) or ""),
                "source": te_read_source(a.get("document"), doc_path(w)), "document": doc_path(w)}
    if bid == NOTES:
        body = note_body(pid)
        rows = note_rows(pid)
        if body is None or "list" in what or "liste" in what:
            return {"status": "done", "ok": bool(rows), "text": "\n".join(r["text"] for r in rows),
                    "source": f"the note list of {notes_shown_folder(pid)!r}"}
        return {"status": "done", "ok": True, "text": body, "source": "the note shown",
                "note": body.split("\n")[0], "folder": notes_shown_folder(pid)}
    if bid == FINDER:
        _v, rows = finder_items(pid)
        sel = [r["label"] for r in rows if r["selected"] and r["kind"] == "file"]
        if len(sel) != 1:
            return {"status": "done", "ok": False, "detail": f"{len(sel)} files selected (read needs exactly one)"}
        guard_before(pid, "read file")
        texts, closed = ql_texts(pid, sel)
        guard_after(pid, "read file")
        t = texts.get(sel[0])
        return {"status": "done", "ok": t is not None, "text": t, "source": "Quick Look", "file": sel[0],
                "ql_closed": closed}
    if bid == DICT:
        return {"status": "done", "ok": True, "text": "\n".join(texts_of(front_window(pid))[:80]),
                "source": "the Lexikon window"}
    return {"status": "done", "ok": False, "detail": f"no text reader for {bid}"}


def op_screen(a):
    """Read-only: what is on the screen, for the planner: the app in
    front; Finder's front window (the folder's name); the Notes folder and the
    note shown; TextEdit's open documents (names); Lexikon's marked source.
    No app is launched or activated; no menu of an app in the background is
    opened (Lexikon's source is read only when Lexikon is in front)."""
    front = CS.frontmost_bundle()
    out = {"front": X.UI_NAMES.get(front, front)}
    fpid = pid_of(front) if front else None
    if fpid:
        # the language bridge: the UI language from the front app's menu-bar titles (read, never opened)
        titles = [nfc(_s(_at(k, "AXTitle")) or "") for k in menu_bar_items(fpid)]
        out["ui_language"] = U6.ui_language_of(titles)
        out["menu_bar"] = titles[:12]
    pid = pid_of(FINDER)
    if pid:
        ws = [x for x in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
              if (_s(_at(x, "AXSubrole")) or "") == "AXStandardWindow"]
        if ws:
            out["finder_window"] = nfc(_s(_at(ws[0], "AXTitle")) or "")
    pid = pid_of(NOTES)
    if pid:
        out["notes_folder"] = notes_shown_folder(pid)
        body = note_body(pid)
        out["notes_note"] = (body or "").split("\n")[0] if body is not None else None
    pid = pid_of(TEXTEDIT)
    if pid:
        out["textedit_documents"] = sorted(os.path.basename(d) for d in (
            doc_path(w) for w in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []))
            if d)
    pid = pid_of(DICT)
    if pid:
        out["lexikon_running"] = True
        if front == DICT:
            ti = top_index(pid, "Suchen")
            marked = [it["title"] for it in (read_menu(pid, ti) or []) if it["mark"]] if ti is not None else []
            out["lexikon_source"] = marked[0] if len(marked) == 1 else None
    return {"status": "done", "screen": out}


def op_notes_verify(a):
    """A task's Notes folders hold exactly the author's notes
    (title + first line), read in list view. With ``repair``: a note whose text an
    earlier run rewrote (one declared note missing, one unknown note in its place)
    gets its declared text back through its own text area, as the seeding wrote it
    (this test's own objects only). Read back."""
    spec, repair, setup = a["spec"], bool(a.get("repair")), []
    # bounded; past the deadline the check stops and REPORTS (never hangs)
    deadline = time.monotonic() + float(a.get("deadline_s") or 1e9)
    pid = pid_of(NOTES) or open_app(NOTES, fresh=False, wait=4.0)
    activate(NOTES)
    guard_before(pid, "notes verify")
    if notes_searching(pid):
        # a search the last run left (a task's expected end state) filters every
        # folder's list; Notes clears it at a fresh launch (as the clean start relies on): quit, relaunch
        quit_app(NOTES, setup)
        pid = open_app(NOTES, fresh=False, wait=4.0)
        activate(NOTES)
        guard_before(pid, "notes verify")
        setup.append(f"notes verify: an active search was cleared by a relaunch (empty now: {not notes_searching(pid)})")
    report = {}
    for folder, notes in spec.items():
        if time.monotonic() > deadline:
            report[folder] = {"ok": False, "why": "deadline reached before this folder was checked"}
            continue
        err = notes_show(pid, folder, None, setup)
        if err:
            report[folder] = {"ok": False, "why": err}
            continue
        if note_table(pid) is None and note_body(pid) is not None and _gallery_back(pid):
            # a note opened from the gallery hides the grid; after "Als Liste" the list
            # would then read empty (every note "missing"): back to the grid first
            setup.append("notes verify: back to the gallery grid first")
            time.sleep(0.8)
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        time.sleep(0.8)
        want = [(nfc(t), nfc(l)) for t, l in notes]
        have = []
        for _k in range(6):                     # the list may appear late after a view switch
            have = [(r["label"], r.get("line", "")) for r in note_rows(pid)]
            if have:
                break
            time.sleep(0.8)
        missing = [w for w in want if w not in have]
        extra = [h for h in have if h not in want]
        fixed = []
        if repair and len(missing) == len(extra) == 1:
            (mt, ml), (xt, _xl) = missing[0], extra[0]
            r = _select(pid, "notes_notes", xt)
            areas = find_all(front_window(pid), lambda e: role(e) == "AXTextArea"
                             and _s(_at(e, "AXIdentifier")) == "Note Body Text View")
            if r["status"] == "done" and len(areas) == 1 and not secure(areas[0]):
                _AS().AXUIElementSetAttributeValue(areas[0], "AXValue", f"{mt}\n{ml}")
                time.sleep(1.2)
                fixed.append({"note": xt, "restored_as": mt, "body_now": (note_body(pid) or "")[:80]})
            have = [(r["label"], r["line"]) for r in note_rows(pid)]
            missing = [w for w in want if w not in have]
            extra = [h for h in have if h not in want]
        # every note's FULL text (title + line, as seeded): a change below the preview line shows
        # neither in the list nor in the note count (e.g. the baseline typing into note bodies)
        body_diff = []
        if not missing and not extra:
            for t, l in want:
                if time.monotonic() > deadline:
                    body_diff.append({"note": t, "why": "deadline reached: not checked"})
                    continue
                if _select(pid, "notes_notes", t)["status"] != "done":
                    body_diff.append({"note": t, "why": "not selectable"})
                    continue
                body = (note_body(pid) or "").rstrip("\n")
                if body == f"{t}\n{l}":
                    continue
                entry = {"note": t, "was": body[:120]}
                areas = find_all(front_window(pid), lambda e: role(e) == "AXTextArea"
                                 and _s(_at(e, "AXIdentifier")) == "Note Body Text View")
                if repair and len(areas) == 1 and not secure(areas[0]):
                    _AS().AXUIElementSetAttributeValue(areas[0], "AXValue", f"{t}\n{l}")
                    time.sleep(1.0)
                    entry["now"] = (note_body(pid) or "")[:120]
                    fixed.append(entry)
                    if (note_body(pid) or "").rstrip("\n") == f"{t}\n{l}":
                        continue
                body_diff.append(entry)
        report[folder] = {"ok": not missing and not extra and not body_diff, "missing": missing, "extra": extra,
                          "body_diff": body_diff, "fixed": fixed}
    guard_after(pid, "notes verify")
    return {"status": "done", "ok": all(v.get("ok") for v in report.values()), "report": report, "setup": setup,
            "deadline_reached": time.monotonic() > deadline}


# TextEdit's Open panel (AX navigation in the panel OUR press opened; never in
# an authentication prompt: the guards run before and after every action)

def _panel(pid):
    ws = [x for x in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
          if _s(_at(x, "AXIdentifier")) == "open-panel"]
    return ws[0] if len(ws) == 1 else None


def _panel_button(p, title):
    hits = [e for e in find_all(p, lambda e: role(e) == "AXButton", max_depth=10)
            if nfc(_s(_at(e, "AXTitle")) or "") == title]
    return hits[0] if len(hits) == 1 else None


def _up(d, j):
    for _ in range(j):
        d = os.path.dirname(d)
    return d


def _gen_dirs():
    out = []
    for root, dirs, _files in os.walk(GEN):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        out.append(root)
    return out


def _panel_path(p):
    """(path, last-column items, lists): the folder the panel's last column
    lists, found among THIS TEST's folders on disk (the panel shows
    localized names for the home folder's places, so only our own tree is
    matched): the one folder whose entries are exactly the last column's names,
    disambiguated by the previous columns (each the entries of the parent)."""
    br = find_all(p, lambda e: role(e) == "AXBrowser")
    lists = _col_lists(br[0]) if br else []
    cols = [_col_items(li) for li in lists]
    if not cols:
        return None, [], lists
    names = [sorted(it["label"] for it in c) for c in cols]
    cand = [d for d in _gen_dirs() if _listing(d) == names[-1]]
    k = 2
    while len(cand) > 1 and k <= len(names):
        up = k - 1
        cand = [d for d in cand if _listing(_up(d, up)) == names[-k]]
        k += 1
    return (cand[0] if len(cand) == 1 else None), cols[-1], lists


def op_panel(a):
    """TextEdit's Open panel: show (press TextEdit's own Ablage > Öffnen …),
    home (the home folder in its sidebar), enter (a subfolder of the folder
    listed last), open (a file listed last: select it, press Öffnen), cancel."""
    act = a["action"]
    pid = activate(TEXTEDIT)
    if not pid:
        return {"status": "failed", "detail": "TextEdit not running"}
    p = _panel(pid)
    if act == "show":
        if p is None:
            r = op_press({"bundle": TEXTEDIT, "trigger": a["trigger"], "label": a.get("label")})
            if r["status"] != "done":
                return r
            for _ in range(10):
                time.sleep(0.5)
                p = _panel(pid)
                if p is not None:
                    break
        if p is None:
            return {"status": "failed", "detail": "no Open panel"}
    elif p is None:
        return {"status": "failed", "detail": "no Open panel open"}
    guard_before(pid, f"panel {act}")
    if act == "home":
        side = [e for e in find_all(p, lambda e: role(e) == "AXOutline")
                if nfc(_s(_at(e, "AXDescription")) or "") == "Seitenleiste"]
        home = nfc(os.path.basename(os.path.expanduser("~")))
        rows = [r for r in (_at(side[0], "AXRows") or []) if home in texts_of(r)] if side else []
        if len(rows) != 1:
            return {"status": "refused", "detail": f"{len(rows)} sidebar rows for the home folder"}
        _AS().AXUIElementSetAttributeValue(side[0], "AXSelectedRows", [rows[0]])
        time.sleep(1.5)
    elif act == "goto_finder":
        # move the panel to the folder the Finder window shows (tracked, verified): the home
        # folder in the panel's sidebar, then one column selection per path component
        target = STATE.get("finder_folder")
        if not target or not ours(target):
            return {"status": "refused", "detail": "no verified Finder folder of this test"}
        side = [e for e in find_all(p, lambda e: role(e) == "AXOutline")
                if nfc(_s(_at(e, "AXDescription")) or "") == "Seitenleiste"]
        home = os.path.realpath(os.path.expanduser("~"))
        hrows = [r for r in (_at(side[0], "AXRows") or []) if nfc(os.path.basename(home)) in texts_of(r)] if side else []
        if len(hrows) != 1:
            return {"status": "refused", "detail": f"{len(hrows)} sidebar rows for the home folder"}
        _AS().AXUIElementSetAttributeValue(side[0], "AXSelectedRows", [hrows[0]])
        time.sleep(1.5)
        for k, name in enumerate(os.path.relpath(os.path.realpath(target), home).split(os.sep)):
            br = find_all(p, lambda e: role(e) == "AXBrowser")
            lists = _col_lists(br[0]) if br else []
            hit = [it for it in (_col_items(lists[k]) if k < len(lists) else []) if it["label"] == nfc(name)]
            if len(hit) != 1:
                guard_after(pid, "panel goto")
                return {"status": "failed", "detail": f"column {k}: {len(hit)} items {name!r}"}
            _AS().AXUIElementSetAttributeValue(lists[k], "AXSelectedChildren", [hit[0]["el"]])
            time.sleep(1.2)
    elif act in ("enter", "open"):
        path, items, lists = _panel_path(p)
        lab = nfc(a["label"])
        hit = [it for it in items if it["label"] == lab]
        if path is None or len(hit) != 1:
            return {"status": "refused", "detail": f"{len(hit)} items {lab!r} in the folder listed ({path!r})"}
        full = os.path.join(path, lab)
        if act == "enter" and not os.path.isdir(full):
            return {"status": "refused", "detail": f"{lab!r} is not a folder"}
        if act == "open" and (os.path.isdir(full) or not ours(full)):
            return {"status": "refused", "detail": f"{lab!r} is not one of this test's files"}
        _AS().AXUIElementSetAttributeValue(lists[-1], "AXSelectedChildren", [hit[0]["el"]])
        time.sleep(1.2)
        if act == "enter":
            p2, _i2, _l2 = _panel_path(p)
            guard_after(pid, "panel enter")
            return {"status": "done" if p2 == full else "failed", "detail": f"now {p2!r}", "path": p2,
                    "items": [x["label"] for x in _i2]}
        b = _panel_button(p, "Öffnen")
        if b is None or not bool(_at(b, "AXEnabled")) or X.irreversible_reason(_s(_at(b, "AXTitle"))):
            return {"status": "failed", "detail": "the panel's Öffnen button is not available"}
        r = EX.run(b, pid, G.record_selector(pid, b), "press")
        doc = None
        for _ in range(12):
            time.sleep(0.5)
            w = front_window(pid)
            doc = doc_path(w) if w is not None else None
            if _panel(pid) is None and doc == nfc(os.path.realpath(full)):
                break
        guard_after(pid, "panel open")
        return {"status": "done" if doc == nfc(os.path.realpath(full)) else "failed",
                "detail": f"press {r['success']}; front document {doc!r}", "document": doc}
    elif act == "cancel":
        b = _panel_button(p, "Abbrechen")
        if b is None:
            return {"status": "failed", "detail": "no Abbrechen button"}
        EX.run(b, pid, G.record_selector(pid, b), "press")
        time.sleep(1.0)
        guard_after(pid, "panel cancel")
        return {"status": "done" if _panel(pid) is None else "failed", "detail": "cancelled"}
    guard_after(pid, f"panel {act}")
    p = _panel(pid)
    path, items, _l = _panel_path(p) if p is not None else (None, [], [])
    return {"status": "done", "path": path, "where": os.path.basename(path) if path else None,
            "rows": [{"label": it["label"], "kind": "folder" if path and os.path.isdir(os.path.join(path, it["label"]))
                      else "file", "text": it["label"]} for it in items]}


# ── the baseline's executor step ────────────────────────────────────────────

def _menu_bar_ancestor(el):
    cur, n = el, 0
    while cur is not None and n < 12:
        if role(cur) == "AXMenuBarItem":
            return cur
        cur, n = _at(cur, "AXParent"), n + 1
    return None


ROW_KINDS = {FINDER: ("finder_files", "finder_locations"), NOTES: ("notes_notes", "notes_folders")}


def _row_kind(pid, bid, label):
    """Which row list carries this exact label (content rows, then the folder list
    / sidebar): the one kind in which exactly one row has it, else None."""
    hits = []
    for kind in ROW_KINDS.get(bid, ()):
        # the locations also list subfolder / folder-above rows: the baseline's
        # row lookup keeps the plain lists (the shown folder and the sidebar only)
        rows = [r for r in op_rows({"bundle": bid, "kind": kind}).get("rows") or []
                if r.get("via") not in ("sub", "up")]
        n = sum(1 for r in rows if r["label"] == nfc(label))
        if n == 1:
            hits.append(kind)
    return hits[0] if len(hits) == 1 else None


def op_ax_step(a):
    """One step of the baseline's plan through the same checked executor
    (``StateGraphExecutor.execute_checked_step``) and the same guards.
    Harness adaptations, identical for both arms: menu items get their menu
    opened first; a set_value on a text role runs the executor's enter_text
    ladder (as the arch's writes); 'select'/'open' on a content row use the
    arch's selection primitive (and ⌘O); only the 'ax' modality runs."""
    from anyuse.context.accessibility import AccessibilityTree
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.plan_types import PlannedStep
    st = PlannedStep.from_dict(a["step"] or {})
    bid = CS.frontmost_bundle()
    if bid not in X.MANAGED:
        return {"status": "refused", "detail": f"front app {bid!r} is not one of this test's apps"}
    pid = pid_of(bid)
    if st.modality != "ax":
        return {"status": "refused", "detail": f"modality {st.modality!r} is not allowed in this test"}
    cmd = dict(st.command or {})
    rl, title, action = cmd.get("role") or "", cmd.get("title") or "", (cmd.get("action") or "press")
    guard_before(pid, f"ax_step {action} {rl} {title!r}")
    if action in ("press_key", "key"):
        if not X.key_allowed(cmd.get("keycode"), cmd.get("flags")):
            return {"status": "refused", "detail": f"key {cmd.get('keycode')!r}/{cmd.get('flags')!r} not allowed"}
    if bid in ROW_KINDS and (action.lower() in ("select", "axselect", "open", "axopen") or
                             (action == "press" and rl in ("AXRow", "AXCell"))):
        gate = X.irreversible_reason(title)
        if gate:
            return {"status": "gated", "detail": f"irreversible-action guard: {gate}"}
        kind = _row_kind(pid, bid, title)
        if kind is None:
            return {"status": "refused", "detail": f"no single row labelled {title!r}"}
        r = _select(pid, kind, title)
        if r["status"] == "done" and action.lower() in ("open", "axopen") and kind == "finder_files":
            trig = X.EffectMap().open_trigger("Finder")
            r = op_press({"bundle": FINDER, "trigger": trig["trigger"], "label": trig["path"]})
        guard_after(pid, "ax_step select")
        return r
    tree = AccessibilityTree(pid=pid)
    el = None
    scoped = None
    if action not in ("press_key", "key"):
        els = tree.find(role=rl or None, title=title or None)
        if len(els) > 1:
            # same-named elements (two TextEdit documents, each with a text area
            # "First Text View") are told apart by their window: the named document's or window's, else the FRONT
            # window's (the one the model saw); exactly one, else refused as before (never guessed)
            k, how = _by_window(pid, els, cmd)
            if k is not None:
                wtree = _window_tree(pid, _at(els[k], "AXWindow"))
                one = wtree.find(role=rl or None, title=title or None)
                if len(one) == 1:
                    els, tree, scoped = one, wtree, how
        if len(els) != 1:
            return {"status": "refused", "detail": f"{len(els)} candidates for role={rl!r} title={title!r}"
                                                   " (no recorded identity: refused, never guessed)"}
        el = els[0]
        gate = X.irreversible_reason(_s(_at(el, "AXTitle")), _s(_at(el, "AXDescription")), title)
        if gate:
            return {"status": "gated", "detail": f"irreversible-action guard: {gate}"}
        if web_source_item(bid, _s(_at(el, "AXTitle")), title):
            return {"status": "refused", "detail": f"{title!r} reaches the web: never pressed"}
        if bid == DICT and role(el) in TEXT_ROLES + ("AXSearchField",) and dict_on_web(pid):
            return {"status": "refused", "detail": "Lexikon is on a web source: nothing typed"}
        if role(el) in TEXT_ROLES and action in ("set_value", "enter_text"):
            if secure(el):
                return {"status": "refused", "detail": "secure field: never typed into"}
            cmd["action"] = "enter_text"
        if role(el) == "AXMenuItem":
            bar = _menu_bar_ancestor(el)
            if bar is not None:
                _AS().AXUIElementPerformAction(bar, "AXPress")
                time.sleep(0.8)
    step = PlannedStep(intent=st.intent, source=st.source, target_element=st.target_element, modality="ax",
                       command=cmd, input_data=st.input_data)
    ex = StateGraphExecutor(ax_tree=tree if scoped else AccessibilityTree(pid=pid))
    res, _ver = asyncio.run(ex.execute_checked_step(step))
    time.sleep(1.0)
    guard_after(pid, "ax_step")
    ok = bool(res.get("success"))
    return {"status": "done" if ok else ("refused" if res.get("refused") or res.get("outcome") == "uncertain"
                                         else "failed"),
            "detail": f"{res.get('outcome') or ''} {res.get('error') or ''} {res.get('rung') or ''}".strip()
            + (f" [window scope: the element {scoped}]" if scoped else "")}


def _by_window(pid, els, cmd):
    """(index, how) of the one element among same-named ones by its window
    (``desktop_e2e8.choose_by_window``: the named document / window, else the front
    window), or (None, why)."""
    from anyuse.execution import desktop_e2e8 as R8
    front = front_window(pid)
    fk = _win_key(front) if front is not None else None
    info = []
    for e in els:
        w = _at(e, "AXWindow")
        info.append({"window": nfc(_s(_at(w, "AXTitle")) or "") if w is not None else None,
                     "document": doc_path(w) if w is not None else None,
                     "front": w is not None and fk is not None and _win_key(w) == fk})
    return R8.choose_by_window(info, cmd.get("document"), cmd.get("window"))


def _window_tree(pid, window):
    """The app's AX tree rooted at one of its windows (the checked executor then
    resolves the element inside that window only)."""
    from anyuse.context.accessibility import AccessibilityTree

    class WindowTree(AccessibilityTree):
        def _root(self):
            return window
    return WindowTree(pid=pid)


# ── the baseline's view: the front app's AX tree as compact text ────────────

SKIP_MENUS = ("Apple", "Fenster", "Hilfe", "Window", "Help")


def op_ax_text(a):
    # the frontier-agent arm reads longer field values (value_chars); the baseline's default is unchanged
    vc = max(1, min(int(a.get("value_chars") or 60), 2000))
    bid = CS.frontmost_bundle()
    if bid not in X.MANAGED:
        return {"status": "done", "bundle": bid, "text": "[]"}
    pid = pid_of(bid)
    w = front_window(pid)
    head = {"app": bid, "window": nfc(_s(_at(w, "AXTitle")) or "") if w is not None else "",
            "document": doc_path(w) if w is not None else None}
    elems = []
    n = [0]

    def walk(el, depth):
        if depth > 14 or n[0] > 450:
            return
        n[0] += 1
        r = role(el)
        if r == "AXRow":
            tx = texts_of(el)
            if tx:
                elems.append({"role": "AXRow", "title": tx[0], "cells": " | ".join(tx[1:4])[:120],
                              **({"selected": True} if _at(el, "AXSelected") else {})})
            return
        t = nfc(_s(_at(el, "AXTitle")) or "")
        d = nfc(_s(_at(el, "AXDescription")) or "")
        v = _s(_at(el, "AXValue"))
        ident = _s(_at(el, "AXIdentifier")) or ""
        if r in ("AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXTextField",
                 "AXTextArea", "AXSearchField", "AXComboBox", "AXSlider", "AXLink", "AXTab") or (t or d):
            if r not in ("AXGroup", "AXScrollArea", "AXSplitGroup", "AXToolbar", "AXLayoutArea", "AXWindow"):
                e = {"role": r}
                if t:
                    e["title"] = t
                if d:
                    e["ax_description"] = d
                if ident and not ident.startswith("_NS:"):
                    e["identifier"] = ident
                if v is not None and r in TEXT_ROLES + ("AXSearchField", "AXStaticText", "AXCheckBox",
                                                        "AXPopUpButton", "AXSlider"):
                    e["value"] = nfc(str(v))[:vc]
                if _at(el, "AXEnabled") is False:
                    e["enabled"] = False
                if (_s(_at(el, "AXSubrole")) or "") == "AXSearchField":
                    e["subrole"] = "AXSearchField"
                elems.append(e)
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    if w is not None:
        walk(w, 0)
    menus = []
    kids = menu_bar_items(pid)
    for i, k in enumerate(kids):
        t = nfc(_s(_at(k, "AXTitle")) or "")
        if i < 2 or t in SKIP_MENUS:
            continue
        for it in read_menu(pid, i) or []:
            if not it["enabled"]:
                continue
            e = {"role": "AXMenuItem", "title": it["title"], "menu": " > ".join(it["path"].split(" > ")[:-1])}
            if it["mark"]:
                e["mark"] = "✓"
            menus.append(e)
    out = [head]
    for e in elems + menus:
        trial = json.dumps(out + [e], ensure_ascii=False)
        if len(trial) > 15000:
            break
        out.append(e)
    return {"status": "done", "bundle": bid, "text": json.dumps(out, ensure_ascii=False),
            "n_window": len(elems), "n_menu": len(menus)}


# ── the independent reader ──────────────────────────────────────────────────

def read_probe(p):
    k = p["kind"]
    if k == "folder":
        path = exp(p["path"])
        if not ours(path) or not os.path.isdir(path):
            return {"ok": False}
        ents = []
        for n in sorted(os.listdir(path)):
            if n.startswith("."):
                continue
            q = os.path.join(path, n)
            if os.path.isdir(q):
                ents.append({"name": nfc(n), "type": "dir"})
            else:
                fi = file_info(q)
                ents.append({"name": nfc(n), "type": "file", "sha256": fi.get("sha256"), "text": fi.get("text"),
                             "mtime": fi.get("mtime")})
        return {"ok": True, "entries": ents}
    if k == "marks":
        pid = pid_of(p["bundle"])
        if not pid:
            return {"ok": False, "why": "not running"}
        # a background app's menu marks read stale (Lexikon behind
        # Notes showed the source of its launch, not the one just set): the reader
        # brings the app to the front for the read and gives the front back after
        prev = CS.frontmost_bundle()
        moved = prev != p["bundle"]
        if moved:
            activate(p["bundle"])
        ti = top_index(pid, p["top"])
        items = read_menu(pid, ti) if ti is not None else None
        if moved and prev and running(prev) is not None:
            activate(prev)
        if items is None:
            return {"ok": False, "why": f"no menu {p['top']!r}"}
        return {"ok": True, "items": [{"path": it["path"], "mark": it["mark"]} for it in items],
                **({"read_in_front": True} if moved else {})}
    if k == "notes":
        pid = pid_of(NOTES)
        if not pid:
            return {"ok": False, "why": "not running"}
        _o, side = notes_sidebar(pid)
        rows = note_rows(pid)
        sf = search_field_els(pid)
        body = note_body(pid)
        return {"ok": bool(side), "folder": notes_shown_folder(pid),
                "counts": {r["label"]: r["count"] for r in side if r["label"] in p.get("folders", [])},
                "shown_note": (body or "").split("\n")[0] if body is not None else None,
                # judge: the text of the note shown, to compare with the author's note
                "shown_text": nfc(body)[:1000] if body is not None else None,
                "selected": [r["label"] for r in rows if r["selected"]],
                "list": [[r["label"], r["line"]] for r in rows] or None,
                "search": nfc(_s(_at(sf[0], "AXValue")) or "") if sf else None}
    if k == "te_prefs":
        # TextEdit's settings for NEW documents (its Einstellungen window: opened, read, closed;
        # nothing pressed). A task that starts TextEdit with no document names a setting only there.
        try:
            return {"ok": True, "prefs": te_prefs(None, [])["after"]}
        except RuntimeError as exc:
            return {"ok": False, "why": str(exc)[:200]}
    if k == "dictionary":
        pid = pid_of(DICT)
        if not pid:
            return {"ok": False, "why": "not running"}
        sf = search_field_els(pid)
        return {"ok": True, "search": nfc(_s(_at(sf[0], "AXValue")) or "") if sf else None,
                "headwords": [t for t in texts_of(front_window(pid))[:40]][:40]}
    if k == "front":
        docs = []
        for b in p.get("bundles") or []:
            pid = pid_of(b)
            for w in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []) if pid else []:
                d = doc_path(w)
                if d:
                    docs.append(d)
        return {"ok": True, "front_bundle": CS.frontmost_bundle(), "documents": sorted(docs)}
    return {"ok": False, "why": f"unknown probe {k!r}"}


def op_read_state(a):
    return {"status": "done", "read": [read_probe(p) for p in a["read"]]}


# ── seeding and the clean start ─────────────────────────────────────────────

def seed_folder(folder, seeds):
    path = exp(folder)
    if not ours(path):
        raise ValueError("not this test's folder")
    H.mark_disposable(path)
    decl = H.CleanState(bundle_id=FINDER, window=os.path.basename(path), folder=path,
                        entries=tuple((n, "file") for n, _d, _s in seeds))
    done = H.reset_folder(decl)
    for n, date, size in seeds:
        p = os.path.join(path, n)
        unit = (f"e2e seed {n}\n").encode()
        with open(p, "wb") as fh:
            fh.write((unit * (size // len(unit) + 1))[:size])
        ts = time.mktime(time.strptime(date, "%Y-%m-%d %H:%M"))
        os.utime(p, (ts, ts))
    return decl, done


def _write_dated(p, text, date):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    ts = time.mktime(time.strptime(date, "%Y-%m-%d %H:%M"))
    os.utime(p, (ts, ts))
    return ts


def seed_tree(folder, files, subfolders):
    """A task folder with the author's
    files (name, modification date, full text) and subfolders, rebuilt from
    scratch inside this test's marked disposable folder before every run."""
    import shutil
    path = exp(folder)
    if not ours(path):
        raise ValueError("not this test's folder")
    H.mark_disposable(path)
    entries = tuple((f["name"], "file") for f in files) + tuple((d["name"], "dir") for d in subfolders)
    decl = H.CleanState(bundle_id=FINDER, window=os.path.basename(path), folder=path, entries=entries)
    done = H.reset_folder(decl)
    for f in files:
        _write_dated(os.path.join(path, f["name"]), f["text"], f["modified"])
    for d in subfolders:
        sp = os.path.join(path, d["name"])
        if os.path.lexists(sp):
            if os.path.islink(sp) or not os.path.isdir(sp):
                os.unlink(sp)
            else:
                shutil.rmtree(sp)
        os.makedirs(sp)
        newest = [_write_dated(os.path.join(sp, f["name"]), f["text"], f["modified"]) for f in d["files"]]
        if newest:
            os.utime(sp, (max(newest), max(newest)))
        done.append(f"rebuilt subfolder {d['name']} ({len(d['files'])} files)")
    return decl, done


# ── TextEdit's settings for NEW documents (its Einstellungen window) ─────────
# The Bearbeiten-menu substitutions/spelling marks belong to the open document
# (a mark set in one run is gone after a relaunch); a document the
# task opens later takes TextEdit's settings for new documents. The harness sets
# those, when a task declares them while TextEdit is not open at its start,
# through the settings window's own checkboxes, and restores them at the end.

TE_SETUP_DOC = X.GEN + "/e3/_setup/Einstellungen.txt"


def _te_prefs_window(pid):
    for w in _at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []:
        boxes = find_all(w, lambda e: role(e) == "AXCheckBox", max_depth=12)
        if boxes and doc_path(w) is None:
            return w, boxes
    return None, []


def te_prefs(want=None, setup=None):
    """Open TextEdit's Einstellungen window (TextEdit launched on this test's
    setup document if it is not running), read its checkboxes, set those in
    ``want`` ({title: bool}) through a checked press, read them back, close
    the window. Returns {title: bool} after, and what was done."""
    setup = setup if setup is not None else []
    launched = False
    pid = pid_of(TEXTEDIT)
    if not pid:
        write_doc(TE_SETUP_DOC, "Einstellungen (Einrichtung des Tests)\n")
        pid = open_app(TEXTEDIT, doc=TE_SETUP_DOC, fresh=True)
        launched = True
    guard_before(pid, "textedit settings")
    ti = 1
    kids = menu_bar_items(pid)
    bar = kids[ti] if len(kids) > ti else None
    item = None
    if bar is not None:
        _AS().AXUIElementPerformAction(bar, "AXPress")
        time.sleep(0.7)
        menu = next((m for m in (_at(bar, "AXChildren") or []) if role(m) == "AXMenu"), None)
        for k in (_at(menu, "AXChildren") or []) if menu is not None else []:
            if role(k) == "AXMenuItem" and nfc(_s(_at(k, "AXTitle")) or "").startswith("Einstellungen"):
                item = k
                break
    if item is None:
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
        raise RuntimeError("no 'Einstellungen' item in TextEdit's app menu")
    r = EX.run(item, pid, G.record_selector(pid, item), "press")
    time.sleep(1.5)
    guard_after(pid, "textedit settings open")
    w, boxes = _te_prefs_window(pid)
    if w is None:
        raise RuntimeError(f"settings window not found (press: {r.get('success')})")
    before = {nfc(_s(_at(b, "AXTitle")) or ""): bool(_at(b, "AXValue")) for b in boxes}
    for title, val in (want or {}).items():
        hit = [b for b in boxes if nfc(_s(_at(b, "AXTitle")) or "") == nfc(title)]
        if len(hit) != 1:
            setup.append(f"te_prefs: {len(hit)} checkboxes titled {title!r}")
            continue
        if bool(_at(hit[0], "AXValue")) != bool(val):
            rr = EX.run(hit[0], pid, G.record_selector(pid, hit[0]), "press")
            time.sleep(0.6)
            setup.append(f"te_prefs: pressed {title!r} ({'ok' if rr['success'] else rr.get('error')})")
    w, boxes = _te_prefs_window(pid)
    after = {nfc(_s(_at(b, "AXTitle")) or ""): bool(_at(b, "AXValue")) for b in boxes}
    cb = _at(w, "AXCloseButton") if w is not None else None
    if cb is not None:
        _AS().AXUIElementPerformAction(cb, "AXPress")
        time.sleep(0.8)
    guard_after(pid, "textedit settings close")
    if launched:
        quit_app(TEXTEDIT, setup)
    return {"before": before, "after": after, "setup": setup}


def op_te_prefs(a):
    """Read (and with ``set``, change) TextEdit's settings for new documents."""
    out = te_prefs(a.get("set"), [])
    return dict(out, status="done")


def op_seed3(a):
    """The one-time Notes seeding: the task folders and their notes,
    created through Notes' own menu items (never deleted: a UI delete is
    irreversible; the snapshot reset removes them)."""
    setup = []
    quit_app(NOTES, setup)
    pid = open_app(NOTES, fresh=False, wait=4.0)
    guard_before(pid, "seed3 notes")
    setup_press_path(pid, "Darstellung > Als Liste", True, setup)
    got = {}
    for folder, notes in a["notes"].items():
        _o, rows = notes_sidebar(pid)
        if folder not in [r["label"] for r in rows]:
            notes_create_folder(pid, folder, setup)
        err = notes_show(pid, folder, None, setup)
        if err:
            return {"status": "failed", "detail": err, "setup": setup}
        have = [r["label"] for r in note_rows(pid)]
        want = [t for t, _l in notes]
        if not have:
            for title, line in reversed(notes):
                notes_create_note(pid, title, line, setup)
            # the list can lag behind the last note created: read it again for up to 10 s
            for _k in range(10):
                time.sleep(1.5 if _k == 0 else 1.0)
                have = [r["label"] for r in note_rows(pid)]
                if sorted(have) == sorted(want):
                    break
        got[folder] = have
        if sorted(have) != sorted(want):
            return {"status": "failed", "detail": f"{folder} holds {have}, declared {want}", "setup": setup,
                    "got": got}
    quit_app(NOTES, setup)
    return {"status": "done", "setup": setup, "got": got}


def write_doc(path, text):
    p = exp(path)
    if not ours(p):
        raise ValueError("not this test's document")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def notes_create_folder(pid, name, setup):
    it, bar = menu_item_by_path(pid, "Ablage > Neuer Ordner")
    if it is None:
        raise RuntimeError("no 'Neuer Ordner'")
    guard_before(pid, "seed: new notes folder")
    EX.run(it, pid, G.record_selector(pid, it), "press")
    time.sleep(1.8)
    sh = app_sheets(pid)
    if len(sh) != 1 or E.is_auth_prompt(G2.ax_tree(sh[0])):
        raise RuntimeError(f"new-folder sheet not as expected ({len(sh)})")
    fields = find_all(sh[0], lambda e: role(e) == "AXTextField")
    if len(fields) != 1 or secure(fields[0]):
        raise RuntimeError("new-folder sheet has no single plain name field")
    _AS().AXUIElementSetAttributeValue(fields[0], "AXValue", name)
    time.sleep(0.5)
    ok = _at(sh[0], "AXDefaultButton")
    if nfc(_s(_at(fields[0], "AXValue")) or "") != name or ok is None:
        cb = _at(sh[0], "AXCancelButton")
        if cb is not None:
            _AS().AXUIElementPerformAction(cb, "AXPress")
        raise RuntimeError("folder name did not take")
    _AS().AXUIElementPerformAction(ok, "AXPress")
    time.sleep(1.8)
    guard_after(pid, "seed: new notes folder")
    setup.append(f"seed: notes folder {name!r} created")


def notes_create_note(pid, title, line, setup):
    it, bar = menu_item_by_path(pid, "Ablage > Neue Notiz")
    if it is None:
        raise RuntimeError("no 'Neue Notiz'")
    guard_before(pid, "seed: new note")
    EX.run(it, pid, G.record_selector(pid, it), "press")
    time.sleep(1.4)
    guard_after(pid, "seed: new note")
    foc = _at(_AS().AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    if foc is None or role(foc) != "AXTextArea" or _s(_at(foc, "AXIdentifier")) != "Note Body Text View" \
            or secure(foc):
        raise RuntimeError("focus is not the new note's body")
    txt = f"{title}\n{line}"
    _AS().AXUIElementSetAttributeValue(foc, "AXValue", txt)
    time.sleep(0.8)
    if nfc(_s(_at(foc, "AXValue")) or "") != nfc(txt):
        raise RuntimeError("note body did not take")
    setup.append(f"seed: note {title!r}")


def notes_show(pid, folder, rest, setup):
    r = _select(pid, "notes_folders", folder)
    if r["status"] != "done":
        return f"folder {folder!r} not shown: {r['detail']}"
    return None


def op_seed(a):
    spec, setup = a["spec"], []
    seed_folder(spec["list"]["folder"], spec["list"]["seeds"])
    setup.append(f"seed: {spec['list']['folder']}")
    d = spec["docs"]
    write_doc(d["doc"], d["seed"])
    write_doc(d["sentinel"], d["sentinel_text"])
    quit_app(NOTES, setup)
    pid = open_app(NOTES, fresh=False, wait=4.0)
    guard_before(pid, "seed notes")
    setup_press_path(pid, "Darstellung > Als Liste", True, setup)
    for folder, notes in spec["notes"].items():
        _o, rows = notes_sidebar(pid)
        if folder not in [r["label"] for r in rows]:
            notes_create_folder(pid, folder, setup)
        err = notes_show(pid, folder, None, setup)
        if err:
            raise RuntimeError(err)
        have = [r["label"] for r in note_rows(pid)]
        if not have:
            for title, line in reversed(notes):
                notes_create_note(pid, title, line, setup)
            time.sleep(1.5)
            have = [r["label"] for r in note_rows(pid)]
        want = [t for t, _l in notes]
        setup.append(f"notes {folder}: {have}")
        if sorted(have) != sorted(want):
            return {"status": "failed", "detail": f"{folder} holds {have}, declared {want}", "setup": setup}
    return {"status": "done", "setup": setup}


def op_clean_start(a):
    start, setup, problems = a["start"], [], []
    bid = start["app"]
    st = G2._session_state()
    if st["locked"]:
        raise Stop("screen locked at a clean start")
    if st["auth_prompts"] or prompt_windows():
        return {"status": "done", "clean": False, "problems": ["an authentication prompt is on screen"],
                "setup": setup}
    for b in X.MANAGED:
        if b != FINDER:
            quit_app(b, setup)
    G.AppCtl(FINDER, False)._close_windows()
    time.sleep(0.8)
    interlock("end leftover helper windows")
    tidy = G2._tidy_leftover_windows()
    if tidy["remaining"]:
        problems.append(f"leftover windows: {tidy['remaining'][:3]}")
    decl = None
    if "te_prefs" in start:
        tp = te_prefs(start["te_prefs"], [])
        setup += tp["setup"]
        wrong = {k: v for k, v in start["te_prefs"].items() if tp["after"].get(k) != bool(v)}
        if wrong:
            problems.append(f"textedit settings not as declared: {wrong}")
        quit_app(TEXTEDIT, setup)
    if "finder" in start:
        f = start["finder"]
        if "files" in f:
            decl, done = seed_tree(f["folder"], f["files"], f.get("subfolders") or [])
        else:
            decl, done = seed_folder(f["folder"], f["seeds"])
        setup += done
        STATE["finder_folder"] = exp(f["folder"])
        _open_path(f["folder"])
        pid = None
        for _ in range(20):
            time.sleep(0.5)
            pid = activate(FINDER)
            w = front_window(pid) if pid else None
            if w is not None and nfc(_s(_at(w, "AXTitle")) or "") == os.path.basename(exp(f["folder"])):
                break
        guard_before(pid, "clean start finder")
        setup_press_path(pid, "Darstellung > " + f["view"], True, setup)
        G.canonical_frame(pid)
        if f.get("sort"):
            setup_press_path(pid, "Darstellung > Sortieren nach > " + f["sort"], True, setup)
        o = list_outline(pid)
        if o is not None and (_at(o, "AXSelectedRows") or []):
            _AS().AXUIElementSetAttributeValue(o, "AXSelectedRows", [])
            time.sleep(0.4)
            setup.append("setup: list selection cleared")
    if "textedit" in start:
        t = start["textedit"]
        if t.get("seed") is not None:
            write_doc(t["doc"], t["seed"])
        if t.get("sentinel"):
            write_doc(t["sentinel"], t["sentinel_text"])
        pid = open_app(TEXTEDIT, doc=t["doc"], fresh=True)
        guard_before(pid, "clean start textedit")
        for title, want in (t.get("flags") or {}).items():
            path = title if " > " in title else "Bearbeiten > Ersetzungen > " + title
            if not setup_press_path(pid, path, bool(want), setup):
                problems.append(f"textedit flag {path!r} not set")
    if bid == TEXTEDIT and "textedit" not in start:
        # the author starts TextEdit in front with NO document. TextEdit is
        # launched fresh; the Open panel it shows at a launch without a document is cancelled
        # through its own "Abbrechen" (nothing opened, nothing typed)
        pid = open_app(TEXTEDIT, fresh=True)
        guard_before(pid, "clean start textedit without a document")
        p = _panel(pid) if pid else None
        if p is not None:
            b = _panel_button(p, "Abbrechen")
            if b is not None:
                EX.run(b, pid, G.record_selector(pid, b), "press")
                time.sleep(1.0)
            guard_after(pid, "clean start textedit panel cancel")
            setup.append("setup: TextEdit's Open panel at launch cancelled")
            if _panel(pid) is not None:
                problems.append("TextEdit's Open panel stays open")
    if "notes" in start:
        n = start["notes"]
        pid = open_app(NOTES, fresh=False, wait=4.0)
        guard_before(pid, "clean start notes")
        # the folder first, then its view (Notes keeps the view per folder; a view pressed
        # before the folder does not survive)
        err = notes_show(pid, n["folder"], n["rest"], setup)
        if err:
            problems.append(err)
        setup_press_path(pid, "Darstellung > " + n["view"], True, setup)
        time.sleep(0.8)
        sf = search_field_els(pid)
        if n.get("clear_search") and sf and (_s(_at(sf[0], "AXValue")) or ""):
            problems.append("the notes search field is not empty after a fresh launch")
        if n.get("rest"):
            r = _select(pid, "notes_notes", n["rest"])
            if r["status"] != "done":
                problems.append(f"rest note {n['rest']!r}: {r['detail']}")
    if "dictionary" in start:
        pid = open_dict(setup)
        guard_before(pid, "clean start dictionary")
        setup_press_path(pid, "Suchen > " + start["dictionary"]["source"], True, setup)
        # Lexikon restores its last search at launch (e.g. "Zeitgeist"); a declared start
        # names no search word: the field starts empty
        sf = search_field_els(pid)
        if sf and (_s(_at(sf[0], "AXValue")) or ""):
            _AS().AXUIElementSetAttributeValue(sf[0], "AXValue", "")
            time.sleep(0.5)
            setup.append("setup: dictionary search field cleared")
            if (_s(_at(search_field_els(pid)[0], "AXValue")) or ""):
                problems.append("the dictionary search field is not empty")
    if "dcm" in start:
        c = start["dcm"]
        pid = open_app(DCM, fresh=True)
        guard_before(pid, "clean start dcm")
        setup_press_path(pid, "Darstellung > Vergrößerung > " + c["magnification"], True, setup)
        setup_press_path(pid, "Darstellung > Position fixieren", bool(c["lock"]), setup)
    pid = activate(bid)
    time.sleep(0.5)
    guard_before(pid, "clean start check")
    w = front_window(pid)
    obs = {"bundle_id": CS.frontmost_bundle(), "front_window": nfc(_s(_at(w, "AXTitle")) or "") if w else None,
           "selection": CS.selection(bid, pid) if bid == FINDER else [],
           "dialogs": [role(x) for x in app_sheets(pid)] if pid else None,
           "foreign_windows": CS.foreign_windows(pid),
           "folder_listing": CS.folder_listing(decl.folder) if decl else []}
    # the declared front window: the Finder folder when Finder is in front; TextEdit's
    # document when TextEdit is (a task can start in TextEdit with a Finder folder)
    win = (decl.window if decl else "") if bid == FINDER else \
        (os.path.basename(exp(start["textedit"]["doc"])) if bid == TEXTEDIT and "textedit" in start else "")
    columns = bid == FINDER and (start.get("finder") or {}).get("view") == "Als Spalten"
    cdecl = H.CleanState(bundle_id=bid, window=win, folder=decl.folder if decl else "",
                         entries=decl.entries if decl else (), no_selection=(bid == FINDER and not columns))
    problems += H.check(cdecl, obs)
    if columns:
        # the column view marks every path component as selected; only the last
        # column (the folder's own items) must have no selection
        _v, items = finder_items(pid)
        sel = [r["label"] for r in items if r["selected"]]
        if sel:
            problems.append(f"{len(sel)} item(s) selected in the folder's column: {sel[:3]}")
    read = [read_probe(p) for p in a.get("read") or []]
    return {"status": "done", "clean": not problems, "problems": problems, "setup": setup, "observed": obs,
            "read": read}


def op_survey(a):
    """Read-only (apps are launched, menus opened and closed, nothing pressed
    inside): the states the final restore returns to."""
    out, setup = {}, []
    pid = open_app(NOTES, fresh=False, wait=4.0)
    _o, rows = notes_sidebar(pid)
    ti = top_index(pid, "Darstellung")
    out["notes"] = {"shown": notes_shown_folder(pid), "folders": [(r["label"], r["count"]) for r in rows],
                    "view": [(i["path"], i["mark"]) for i in (read_menu(pid, ti) or [])
                             if i["path"] in ("Darstellung > Als Liste", "Darstellung > Als Galerie")]}
    for b, top in ((DICT, "Suchen"), (DCM, "Darstellung")):
        pid = open_dict(setup) if b == DICT else open_app(b, fresh=True)
        ti = top_index(pid, top)
        out[b] = [(i["path"], i["mark"]) for i in (read_menu(pid, ti) or []) if i["mark"]]
    doc = exp(X.DOC)
    if os.path.exists(doc):
        pid = open_app(TEXTEDIT, doc=doc, fresh=True)
        ti = top_index(pid, "Bearbeiten")
        out[TEXTEDIT] = [(i["path"], i["mark"]) for i in (read_menu(pid, ti) or []) if i["mark"]]
    for b in (NOTES, DICT, DCM, TEXTEDIT):
        quit_app(b, setup)
    if a.get("te_prefs"):
        out["te_prefs"] = te_prefs(None, setup)["after"]
    out["setup"] = setup
    return {"status": "done", "survey": out}


def op_restore(a):
    """End of the run: the app settings the tasks touched go back to the survey's."""
    sv, setup = a["survey"], []
    if sv.get(DICT):
        pid = open_dict(setup)
        # a web source (Wikipedia, "Alle Lexika") goes back only with an EMPTY search
        # field: a word there would be looked up on the web now and at the next launch
        sf = search_field_els(pid)
        if sf and (_s(_at(sf[0], "AXValue")) or ""):
            _AS().AXUIElementSetAttributeValue(sf[0], "AXValue", "")
            time.sleep(0.5)
        sf = search_field_els(pid)
        empty = bool(sf) and not (_s(_at(sf[0], "AXValue")) or "")
        for path, _m in sv[DICT]:
            if not path.startswith("Suchen > "):
                continue
            if path.split(" > ")[-1] in WEB_SOURCES:
                # a web source is never selected, not even to restore the survey's state
                setup.append(f"restore: the survey's {path!r} NOT restored (a web source; field empty={empty}): "
                             "left on the local source")
                continue
            setup_press_path(pid, path, True, setup)
    if sv.get(DCM):
        pid = open_app(DCM, fresh=True)
        for path, _m in sv[DCM]:
            if path.startswith("Darstellung > Vergrößerung > "):
                setup_press_path(pid, path, True, setup)
        marked = {p for p, _m in sv[DCM]}
        setup_press_path(pid, "Darstellung > Position fixieren", "Darstellung > Position fixieren" in marked, setup)
    if sv.get(TEXTEDIT) is not None and os.path.exists(exp(X.DOC)):
        pid = open_app(TEXTEDIT, doc=X.DOC, fresh=True)
        marked = {p for p, _m in sv[TEXTEDIT]}
        for p in ["Bearbeiten > Ersetzungen > " + t for t in (
                "Intelligentes Kopieren/Einsetzen", "Intelligente Anführungszeichen", "Intelligente Bindestriche",
                "Intelligente Links")] + ["Bearbeiten > Rechtschreibung und Grammatik > " + t for t in (
                "Während der Texteingabe prüfen", "Rechtschreibung autom. korrigieren")]:
            setup_press_path(pid, p, p in marked, setup)
    nv = (sv.get("notes") or {})
    if nv:
        pid = open_app(NOTES, fresh=False, wait=4.0)
        # the folder first, then its view (a view pressed before selecting
        # the folder does not survive; Notes keeps the view per folder)
        if nv.get("shown"):
            _select(pid, "notes_folders", nv["shown"])
        for path, m in nv.get("view") or []:
            if m:
                setup_press_path(pid, path, True, setup)
    for b in (NOTES, DICT, DCM, TEXTEDIT):
        quit_app(b, setup)
    if sv.get("te_prefs"):
        tp = te_prefs(sv["te_prefs"], setup)
        setup.append(f"te_prefs restored: {tp['after'] == sv['te_prefs']}")
    clear_find_pasteboard(setup)
    G.AppCtl(FINDER, False)._close_windows()
    return {"status": "done", "setup": setup}


def ax_dump(el, depth=0, budget=None, max_depth=8):
    budget = budget if budget is not None else [800]
    budget[0] -= 1
    node = {"role": role(el), "subrole": _s(_at(el, "AXSubrole")) or ""}
    for k in ("AXIdentifier", "AXTitle", "AXValue", "AXDescription", "AXHelp"):
        v = _at(el, k)
        if isinstance(v, (str, int, float, bool)) and str(v).strip():
            node[k[2:].lower()] = str(v).strip()[:100]
    for k in ("AXSelected", "AXEnabled", "AXFocused"):
        v = _at(el, k)
        if isinstance(v, bool):
            node[k[2:].lower()] = v
    kids = _at(el, "AXChildren") or []
    if kids and depth < max_depth and budget[0] > 0:
        node["children"] = [ax_dump(k, depth + 1, budget, max_depth) for k in list(kids)[:60]]
    elif kids:
        node["n_children"] = len(kids)
    return node


def op_axdump(a):
    """Read-only development view of the front window (or one of its parts)."""
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    w = front_window(pid)
    what = a.get("what") or "window"
    if what == "notes_sidebar":
        o, _rows = notes_sidebar(pid)
        el = o
    elif what == "search_fields":
        return {"status": "done", "dump": [ax_dump(x, max_depth=3) for x in search_field_els(pid)]}
    elif what == "focused":
        el = _at(_AS().AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    else:
        el = w
    return {"status": "done", "dump": ax_dump(el, max_depth=int(a.get("depth") or 8)) if el is not None else None}


# ── development probe (never measured) ──
# Learns the AX shapes the desktop_e2e4 capabilities build on, in this test's own
# probe folder only: Finder subfolder rows and entering one, the column view,
# Quick Look's text, TextEdit's Open panel. Every press goes through the guards
# and the irreversible-action check; the panel is left through its Cancel button,
# Quick Look through Escape.

PROBE4 = X.GEN + "/e4/_probe/Oberordner"


def _actions(el):
    try:
        err, names = _AS().AXUIElementCopyActionNames(el, None)
        return [str(n) for n in names or []]
    except Exception:  # noqa: BLE001
        return []


def press_menu_path(pid, path, setup):
    """Press one menu item by its title path through the checked executor
    (development probe and harness only; the guard reads the live title)."""
    el, bar = menu_item_by_path(pid, path)
    if el is None:
        setup.append(f"no item {path!r}")
        return False
    gate = X.irreversible_reason(_s(_at(el, "AXTitle")))
    if gate or not bool(_at(el, "AXEnabled")):
        _AS().AXUIElementPerformAction(bar, "AXCancel")
        setup.append(f"not pressed {path!r}: {'guard ' + gate if gate else 'disabled'}")
        return False
    guard_before(pid, f"probe press {path!r}")
    r = EX.run(el, pid, G.record_selector(pid, el), "press")
    time.sleep(1.5)
    guard_after(pid, f"probe press {path!r}")
    setup.append(f"pressed {path!r}: {r['success']}")
    return r["success"]


def _menu_titles(pid, top):
    ti = top_index(pid, top)
    return [(it["path"], it["enabled"]) for it in (read_menu(pid, ti) or [])] if ti is not None else []


def op_probe4(a):
    """Development probe (read, plus presses on this test's probe folder)."""
    out, setup = {}, []
    part = a.get("part") or "finder"
    if part == "finder":
        seed_tree(PROBE4, [{"name": "Alpha.txt", "modified": "2026-09-01 10:00", "text": "Inhalt Alpha: Lampe defekt."},
                           {"name": "Beta.txt", "modified": "2026-09-02 10:00", "text": "Inhalt Beta."}],
                  [{"name": "Unter1", "files": [{"name": "Gamma.txt", "modified": "2026-09-03 10:00",
                                                 "text": "Gamma"}]},
                   {"name": "Unter2", "files": []}])
        for b in X.MANAGED:
            if b != FINDER:
                quit_app(b, setup)
        G.AppCtl(FINDER, False)._close_windows()
        _open_path(PROBE4)
        time.sleep(2.0)
        pid = activate(FINDER)
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        w = front_window(pid)
        out["title0"], out["doc0"] = nfc(_s(_at(w, "AXTitle")) or ""), doc_path(w)
        o, rows = finder_rows(pid)
        out["rows"] = [{"label": r["label"], "text": r["text"], "actions": _actions(r["el"]),
                        "cells": [ax_dump(c, max_depth=3) for c in (_at(r["el"], "AXChildren") or [])][:5]}
                       for r in rows]
        out["menu_ablage"] = _menu_titles(pid, "Ablage")
        out["menu_gehe_zu"] = _menu_titles(pid, "Gehe zu")
        out["menu_darstellung"] = _menu_titles(pid, "Darstellung")
        # enter a subfolder: select its row, press Ablage > Öffnen (the mapped open trigger)
        r = _select(pid, "finder_files", "Unter1")
        out["select_unter1"] = r
        trig = X.EffectMap().open_trigger("Finder")
        out["open_unter1"] = op_press({"bundle": FINDER, "trigger": trig["trigger"], "label": trig["path"]})
        time.sleep(1.0)
        w = front_window(pid)
        out["title1"], out["doc1"] = nfc(_s(_at(w, "AXTitle")) or ""), doc_path(w)
        out["n_windows1"] = len(_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
        out["rows1"] = [x["label"] for x in finder_rows(pid)[1]]
        # back up: Gehe zu > Übergeordneter Ordner
        up = next((p for p, _e in out["menu_gehe_zu"] if "bergeordnet" in p), None)
        if up:
            press_menu_path(pid, up, setup)
            w = front_window(pid)
            out["title2"], out["doc2"] = nfc(_s(_at(w, "AXTitle")) or ""), doc_path(w)
        # the column view
        if press_menu_path(pid, "Darstellung > Als Spalten", setup):
            time.sleep(1.0)
            out["columns"] = ax_dump(front_window(pid), max_depth=11)
            w = front_window(pid)
            out["title_col"], out["doc_col"] = nfc(_s(_at(w, "AXTitle")) or ""), doc_path(w)
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        time.sleep(0.8)
        # Quick Look on Alpha.txt
        r = _select(pid, "finder_files", "Alpha.txt")
        out["select_alpha"] = r
        ql = next((p for p, _e in out["menu_ablage"] if p.split(" > ")[-1].startswith("Übersicht")
                   or "Quick Look" in p), None)
        out["ql_item"] = ql
        if ql and press_menu_path(pid, ql, setup):
            time.sleep(2.0)
            wins, _reg = G2._onscreen_windows()
            out["ql_onscreen"] = wins[:12]
            out["ql_windows"] = [ax_dump(x, max_depth=12) for x in
                                 (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])][:4]
            qlw = [x for x in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
                   if nfc(_s(_at(x, "AXTitle")) or "") != out.get("title2", "")]
            el = qlw[0] if qlw else front_window(pid)
            rr = EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=53)
            time.sleep(1.0)
            out["ql_escape"] = rr
            out["n_windows_after_ql"] = len(_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
    if part == "panel":
        doc = PROBE4 + "/Alpha.txt"
        quit_app(TEXTEDIT, setup)
        pid = open_app(TEXTEDIT, doc=doc, fresh=True)
        trig = X.EffectMap().open_trigger("TextEdit")
        out["press_open"] = op_press({"bundle": TEXTEDIT, "trigger": trig["trigger"], "label": trig["path"]})
        time.sleep(2.5)
        wins, _reg = G2._onscreen_windows()
        out["onscreen"] = wins[:12]
        app = _AS().AXUIElementCreateApplication(pid)
        out["windows"] = [{"title": nfc(_s(_at(x, "AXTitle")) or ""), "subrole": _s(_at(x, "AXSubrole")) or "",
                           "doc": doc_path(x)} for x in (_at(app, "AXWindows") or [])]
        out["sheets"] = len(app_sheets(pid))
        out["auth"] = len(G2.auth_prompts(pid))
        out["focused_window"] = ax_dump(_at(app, "AXFocusedWindow"), max_depth=14, budget=[2500]) \
            if _at(app, "AXFocusedWindow") is not None else None
        for x in _at(app, "AXWindows") or []:
            if doc_path(x):
                continue
            cb = _at(x, "AXCancelButton") or next(iter(find_all(x, lambda e: role(e) == "AXButton" and nfc(
                _s(_at(e, "AXTitle")) or "") == "Abbrechen")), None)
            if cb is not None:
                _AS().AXUIElementPerformAction(cb, "AXPress")
                time.sleep(1.0)
                out["cancelled"] = True
        out["windows_after"] = [nfc(_s(_at(x, "AXTitle")) or "") for x in (_at(app, "AXWindows") or [])]
        quit_app(TEXTEDIT, setup)
    if part == "finder2":
        for b in X.MANAGED:
            if b != FINDER:
                quit_app(b, setup)
        G.AppCtl(FINDER, False)._close_windows()
        _open_path(PROBE4)
        time.sleep(2.0)
        pid = activate(FINDER)
        app = _AS().AXUIElementCreateApplication(pid)
        wt = lambda: [nfc(_s(_at(x, "AXTitle")) or "") for x in (_at(app, "AXWindows") or [])]  # noqa: E731
        out["windows0"] = wt()
        _select(pid, "finder_files", "Unter1")
        trig = X.EffectMap().open_trigger("Finder")
        out["open_unter1"] = op_press({"bundle": FINDER, "trigger": trig["trigger"], "label": trig["path"]})
        time.sleep(1.0)
        out["windows1"] = wt()
        out["front1"] = nfc(_s(_at(front_window(pid), "AXTitle")) or "")
        # the column view through the window's own toolbar control
        w = front_window(pid)
        rb = [e for e in find_all(w, lambda e: role(e) == "AXRadioButton") if
              nfc(_s(_at(e, "AXDescription")) or "") == "Spaltendarstellung"]
        if len(rb) == 1:
            r = EX.run(rb[0], pid, G.record_selector(pid, rb[0]), "press")
            time.sleep(1.5)
            out["col_press"] = r
            out["col_dump"] = ax_dump(front_window(pid), max_depth=10, budget=[1500])
            br = find_all(front_window(pid), lambda e: role(e) == "AXBrowser")
            out["n_browser"] = len(br)
            if br:
                lists = find_all(br[0], lambda e: role(e) == "AXList")
                out["col_lists"] = [[nfc(_s(_at(t, "AXValue")) or "") for g in (_at(li, "AXChildren") or [])
                                     for t in find_all(g, lambda e: role(e) == "AXTextField", max_depth=2)]
                                    for li in lists]
            rb2 = [e for e in find_all(front_window(pid), lambda e: role(e) == "AXRadioButton") if
                   nfc(_s(_at(e, "AXDescription")) or "") == "Listendarstellung"]
            if len(rb2) == 1:
                EX.run(rb2[0], pid, G.record_selector(pid, rb2[0]), "press")
                time.sleep(1.2)
        # Quick Look: back in the probe folder, Alpha.txt selected, the Ablage item whose title starts "Übersicht"
        G.AppCtl(FINDER, False)._close_windows()
        _open_path(PROBE4)
        time.sleep(2.0)
        pid = activate(FINDER)
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        out["select_alpha"] = _select(pid, "finder_files", "Alpha.txt")
        ab = [p for p, _e in _menu_titles(pid, "Ablage") if p.split(" > ")[-1].startswith("Übersicht")]
        out["ql_titles"] = ab
        if len(ab) == 1 and press_menu_path(pid, ab[0], setup):
            time.sleep(2.5)
            wins, _reg = G2._onscreen_windows()
            out["ql_onscreen"] = [(x["owner"], x["title"], x["layer"]) for x in wins[:14]]
            out["ql_windows"] = [{"title": nfc(_s(_at(x, "AXTitle")) or ""), "subrole": _s(_at(x, "AXSubrole")) or "",
                                  "dump": ax_dump(x, max_depth=12, budget=[600])} for x in (_at(app, "AXWindows") or [])]
            ql = [x for x in (_at(app, "AXWindows") or []) if nfc(_s(_at(x, "AXTitle")) or "") != "Oberordner"]
            out["ql_texts"] = [texts_of(x) for x in ql]
            out["ql_areas"] = [[nfc(_s(_at(e, "AXValue")) or "")[:300] for e in
                                find_all(x, lambda e: role(e) in ("AXTextArea", "AXStaticText", "AXWebArea"))]
                               for x in ql]
            el = ql[0] if ql else front_window(pid)
            out["ql_escape"] = EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=53)
            time.sleep(1.0)
            out["windows_after_ql"] = [nfc(_s(_at(x, "AXTitle")) or "") for x in (_at(app, "AXWindows") or [])]
    if part == "gallery":
        # this test's own seeded folder only: its view is set to the gallery and back to the list
        folder = a["folder"]
        quit_app(NOTES, setup)
        pid = open_app(NOTES, fresh=False, wait=4.0)
        guard_before(pid, "probe gallery")
        out["show"] = notes_show(pid, folder, None, setup)
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        out["list_rows"] = [r["text"] for r in note_rows(pid)]
        setup_press_path(pid, "Darstellung > Als Galerie", True, setup)
        time.sleep(1.5)
        w = front_window(pid)
        out["gallery_dump"] = ax_dump(w, max_depth=14, budget=[3000])
        out["gallery_note_rows"] = [r["text"] for r in note_rows(pid)]
        out["gallery_body"] = note_body(pid)
        out["gallery_folder"] = notes_shown_folder(pid)
        cand = find_all(w, lambda e: role(e) in ("AXList", "AXCollection", "AXGrid", "AXTable", "AXOutline",
                                                 "AXScrollArea"), max_depth=12)
        out["containers"] = [{"role": role(c), "id": _s(_at(c, "AXIdentifier")) or "",
                              "desc": nfc(_s(_at(c, "AXDescription")) or ""), "n": len(_at(c, "AXChildren") or []),
                              "actions": _actions(c)} for c in cand][:30]
        items = find_all(w, lambda e: "Note" in (_s(_at(e, "AXIdentifier")) or "") or
                         "Gallery" in (_s(_at(e, "AXIdentifier")) or ""), max_depth=14)
        out["items"] = [{"role": role(e), "id": _s(_at(e, "AXIdentifier")) or "", "texts": texts_of(e)[:3],
                         "selected": _at(e, "AXSelected"), "actions": _actions(e)} for e in items][:40]
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        time.sleep(1.0)
        out["back_to_list_rows"] = [r["text"] for r in note_rows(pid)]
        quit_app(NOTES, setup)
    if part == "gallery2":
        # this test's own seeded folder only: select and open a note in the gallery, read back
        folder = a["folder"]
        quit_app(NOTES, setup)
        pid = open_app(NOTES, fresh=False, wait=4.0)
        guard_before(pid, "probe gallery2")
        out["show"] = notes_show(pid, folder, None, setup)
        setup_press_path(pid, "Darstellung > Als Galerie", True, setup)
        time.sleep(1.5)
        out["cells"] = [{k: v for k, v in c.items() if k != "el"} for c in (gallery_cells(pid) or [])]
        c0 = (gallery_cells(pid) or [None])[0]
        out["cell_actions"] = _actions(c0["el"]) if c0 else None
        out["select"] = _select(pid, "notes_notes", a.get("note") or (c0 or {}).get("label"))
        out["body_after"] = (note_body(pid) or "")[:120]
        out["shown_folder_after"] = notes_shown_folder(pid)
        out["rows_after"] = [{k: v for k, v in r.items() if k != "el"} for r in note_rows(pid)]
        out["marks_after"] = [(i["path"], i["mark"]) for i in (read_menu(pid, top_index(pid, "Darstellung")) or [])
                              if i["mark"]]
        out["window_after"] = ax_dump(front_window(pid), max_depth=7, budget=[500])
        # a clean start of the same state again (the harness's own path), then the list view back
        out["reselect_other"] = _select(pid, "notes_notes", (out["cells"][-1] or {}).get("label")) if out["cells"] else None
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        time.sleep(1.0)
        out["list_rows_end"] = [r["text"] for r in note_rows(pid)]
        quit_app(NOTES, setup)
    if part == "gallery3":
        folder = a["folder"]
        quit_app(NOTES, setup)
        pid = open_app(NOTES, fresh=False, wait=4.0)
        guard_before(pid, "probe gallery3")
        out["show"] = notes_show(pid, folder, None, setup)
        setup_press_path(pid, "Darstellung > Als Galerie", True, setup)
        time.sleep(1.5)
        labels = [c["label"] for c in (gallery_cells(pid) or [])]
        out["labels"] = labels
        out["open1"] = _select(pid, "notes_notes", labels[0]) if labels else None
        out["after_open1"] = {"cells": gallery_cells(pid) is not None, "body": (note_body(pid) or "")[:60],
                              "window": ax_dump(front_window(pid), max_depth=6, budget=[300])}
        out["rows_via_op"] = op_rows({"bundle": NOTES, "kind": "notes_notes"})
        out["open2"] = _select(pid, "notes_notes", labels[1]) if len(labels) > 1 else None
        out["body2"] = (note_body(pid) or "")[:60]
        out["marks"] = [(i["path"], i["mark"]) for i in (read_menu(pid, top_index(pid, "Darstellung")) or []) if i["mark"]]
        out["read_probe"] = read_probe({"kind": "notes", "folders": [folder]})
        setup_press_path(pid, "Darstellung > Als Liste", True, setup)
        time.sleep(1.0)
        out["list_rows_end"] = [r["text"] for r in note_rows(pid)]
        quit_app(NOTES, setup)
    if part == "panel2":
        doc = PROBE4 + "/Alpha.txt"
        quit_app(TEXTEDIT, setup)
        pid = open_app(TEXTEDIT, doc=doc, fresh=True)
        trig = X.EffectMap().open_trigger("TextEdit")
        out["press_open"] = op_press({"bundle": TEXTEDIT, "trigger": trig["trigger"], "label": trig["path"]})
        time.sleep(2.5)
        app = _AS().AXUIElementCreateApplication(pid)
        pan = [x for x in (_at(app, "AXWindows") or []) if _s(_at(x, "AXIdentifier")) == "open-panel"]
        out["n_panel"] = len(pan)
        if pan:
            p0 = pan[0]
            db, cb = _at(p0, "AXDefaultButton"), _at(p0, "AXCancelButton")
            out["default"] = nfc(_s(_at(db, "AXTitle")) or "") if db is not None else None
            out["cancel"] = nfc(_s(_at(cb, "AXTitle")) or "") if cb is not None else None
            out["buttons"] = [(nfc(_s(_at(e, "AXTitle")) or ""), nfc(_s(_at(e, "AXDescription")) or ""),
                               bool(_at(e, "AXEnabled"))) for e in find_all(p0, lambda e: role(e) in (
                                   "AXButton", "AXPopUpButton", "AXMenuButton"), max_depth=8)][:30]

            def cols():
                br = find_all(p0, lambda e: role(e) == "AXBrowser")
                if not br:
                    return None
                return [[(nfc(_s(_at(t, "AXValue")) or ""), bool(_at(g, "AXSelected")))
                         for g in (_at(li, "AXChildren") or [])
                         for t in find_all(g, lambda e: role(e) == "AXTextField", max_depth=2)][:40]
                        for li in find_all(br[0], lambda e: role(e) == "AXList")]
            out["cols0"] = cols()
            side = [e for e in find_all(p0, lambda e: role(e) == "AXOutline")
                    if nfc(_s(_at(e, "AXDescription")) or "") == "Seitenleiste"]
            home = os.path.basename(os.path.expanduser("~"))
            if side:
                rows = [r for r in (_at(side[0], "AXRows") or []) if home in texts_of(r)]
                if len(rows) == 1:
                    out["side_err"] = int(_AS().AXUIElementSetAttributeValue(side[0], "AXSelectedRows", [rows[0]]))
                    time.sleep(1.5)
                    out["cols_home"] = cols()
            steps = [x for x in os.path.relpath(exp(PROBE4), os.path.expanduser("~")).split(os.sep)] + ["Beta.txt"]
            out["steps"] = []
            for k, name in enumerate(steps):
                br = find_all(p0, lambda e: role(e) == "AXBrowser")
                lists = find_all(br[0], lambda e: role(e) == "AXList") if br else []
                if k >= len(lists):
                    out["steps"].append({"name": name, "why": f"only {len(lists)} columns"})
                    break
                li = lists[k]
                grp = [g for g in (_at(li, "AXChildren") or []) if any(
                    nfc(_s(_at(t, "AXValue")) or "") == name for t in find_all(g, lambda e: role(e) == "AXTextField",
                                                                                max_depth=2))]
                if len(grp) != 1:
                    out["steps"].append({"name": name, "why": f"{len(grp)} items"})
                    break
                e1 = int(_AS().AXUIElementSetAttributeValue(li, "AXSelectedChildren", [grp[0]]))
                time.sleep(1.2)
                sel = bool(_at(grp[0], "AXSelected"))
                e2 = None
                if not sel:
                    e2 = int(_AS().AXUIElementSetAttributeValue(grp[0], "AXSelected", True))
                    time.sleep(1.2)
                    sel = bool(_at(grp[0], "AXSelected"))
                out["steps"].append({"name": name, "err_list": e1, "err_group": e2, "selected": sel,
                                     "n_cols": len(find_all(br[0], lambda e: role(e) == "AXList"))})
                if not sel:
                    break
            out["cols_end"] = cols()
            db = _at(p0, "AXDefaultButton")
            out["default_enabled"] = bool(_at(db, "AXEnabled")) if db is not None else None
            if out["steps"] and out["steps"][-1].get("selected") and out["steps"][-1]["name"] == "Beta.txt" \
                    and db is not None and bool(_at(db, "AXEnabled")) and \
                    not X.irreversible_reason(_s(_at(db, "AXTitle"))):
                guard_before(pid, "probe panel open")
                r = EX.run(db, pid, G.record_selector(pid, db), "press")
                time.sleep(2.0)
                guard_after(pid, "probe panel open")
                out["open_press"] = r
            out["windows_after"] = [(nfc(_s(_at(x, "AXTitle")) or ""), doc_path(x))
                                    for x in (_at(app, "AXWindows") or [])]
            for x in _at(app, "AXWindows") or []:
                if _s(_at(x, "AXIdentifier")) == "open-panel":
                    c = _at(x, "AXCancelButton")
                    if c is not None:
                        _AS().AXUIElementPerformAction(c, "AXPress")
                        time.sleep(1.0)
                        out["cancelled"] = True
        quit_app(TEXTEDIT, setup)
    return {"status": "done", "probe": out, "setup": setup}


# ── absolute dates, read-only ─────────
# Finder lists recent dates as "Vorgestern, 08:30"; a picker can take
# the older absolute date for the newer one. Candidates now carry the absolute
# modification and creation dates, read from the file system (os.stat, nothing is
# changed) for the items of a folder of THIS test's data, and the guest's local
# date, so relative labels (Finder's or Notes') can be turned into dates.

def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else None


# ── journalled recovery, the live fault gate ──

def op_restore_file(a):
    """The journalled recovery of a document's bytes on disk. Only this test's files; only
    when the file is STILL what the host observed (compare-and-swap on its sha256: a change nobody observed is
    never overwritten); written in place (the same file: the app's open document keeps it); read back."""
    p = exp(a["path"])
    if not ours(p) or not os.path.isfile(p):
        raise ValueError("not this test's file")
    cur = file_info(p)
    if cur.get("sha256") != a.get("expect_sha256"):
        return {"status": "refused", "detail": "the file changed since it was observed: nothing written",
                "sha256": cur.get("sha256")}
    data = str(a.get("text") or "").encode("utf-8")
    with open(p, "r+b") as fh:
        fh.seek(0)
        fh.write(data)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())
    back = file_info(p)
    return dict(back, status="done" if back.get("sha256") == hashlib.sha256(data).hexdigest() else "failed")


FAULT_KINDS = ("edit_file", "truncate_file", "open_doc", "kill_app")


def op_fault(a):
    """The live fault gate (test harness only): ONE fault injected between the engine's own steps by a
    guest-side action; the host's fault channel sends it at a frozen location and the recovery code never sees
    it. Only this test's files and apps: ``edit_file`` (another process appends a line to the document on
    disk), ``truncate_file`` (a torn save: the file cut to its first bytes), ``open_doc`` (another of this
    test's documents opened in TextEdit, in front), ``kill_app`` (the app terminated; unsaved text is lost)."""
    k = a.get("kind")
    if k not in FAULT_KINDS:
        raise ValueError(f"unknown fault {k!r}")
    if k in ("edit_file", "truncate_file", "open_doc"):
        p = exp(a["path"])
        if not ours(p) or not os.path.isfile(p):
            raise ValueError("not this test's file")
    if k == "edit_file":
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(str(a.get("append") or ""))
            fh.flush()
            os.fsync(fh.fileno())
        return dict(file_info(p), status="done", fault=k)
    if k == "truncate_file":
        b = open(p, "rb").read()
        n = int(a.get("keep") if a.get("keep") is not None else len(b) // 2)
        with open(p, "r+b") as fh:
            fh.seek(0)
            fh.write(b[:n])
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
        return dict(file_info(p), status="done", fault=k)
    if k == "open_doc":
        pid = open_app(TEXTEDIT, doc=p, fresh=False, wait=1.5)
        return {"status": "done" if pid else "failed", "fault": k}
    bid = a.get("bundle")
    need_managed(bid)
    r = running(bid)
    if r is not None:
        interlock(f"force-quit {bid}")
        r.forceTerminate()
        t = time.monotonic()
        while time.monotonic() - t < 8 and running(bid) is not None:
            time.sleep(0.3)
    return {"status": "done", "fault": k, "running": running(bid) is not None}


def op_item_dates(a):
    """{now, weekday, path, items: {label: {modified, created, folder} | None}}:
    the named items of ``path`` (a folder of this test's data) or of the folder
    Finder shows (tracked); ``os.stat`` only (st_mtime, st_birthtime)."""
    now = time.time()
    out = {"status": "done", "now": _fmt_ts(now), "weekday": time.strftime("%A", time.localtime(now)),
           "path": None, "items": {}}
    base = a.get("path") or STATE.get("finder_folder")
    if not base or not ours(base) or not os.path.isdir(exp(base)):
        return out
    base = exp(base)
    out["path"] = base
    try:
        names = {nfc(n): n for n in os.listdir(base) if not n.startswith(".")}
    except OSError:
        return out
    for lab in [nfc(x) for x in a.get("labels") or []][:40]:
        n = names.get(lab)
        if n is None:
            out["items"][lab] = None
            continue
        st = os.stat(os.path.join(base, n))
        out["items"][lab] = {"modified": _fmt_ts(st.st_mtime),
                             "created": _fmt_ts(getattr(st, "st_birthtime", 0) or st.st_mtime),
                             "folder": os.path.isdir(os.path.join(base, n))}
    return out


# ── an app's own settings window (settings_set) ──
# Generic for any of this test's apps: the app menu's settings item (Einstellungen / Settings /
# Preferences) opens the window; its tabs (a tab group's choices or toolbar buttons) and its
# controls (checkboxes, choices, pop-ups) are read by their labels; ONE control is set through the
# checked executor, read back, the tab shown at the opening is shown again and the window closed.
# Never: Lexikon's settings (they choose its sources, some reach the web), a text field, a key
# press, a label that names an irreversible verb or a web source.

SETTINGS_ITEMS = ("Einstellungen", "Settings", "Preferences", "Voreinstellungen")
SETTINGS_REFUSED = (DICT,)
CONTROL_KINDS = {"AXCheckBox": "checkbox", "AXRadioButton": "choice", "AXPopUpButton": "popup"}


def _win_key(w):
    return (nfc(_s(_at(w, "AXTitle")) or ""), G.ax_plain(_at(w, "AXPosition")), G.ax_plain(_at(w, "AXSize")))


def _frame(el):
    p, z = G.ax_plain(_at(el, "AXPosition")), G.ax_plain(_at(el, "AXSize"))
    if not (isinstance(p, tuple) and isinstance(z, tuple) and len(p) == 3 and len(z) == 3):
        return None
    return p[1], p[2], z[1], z[2]


def _app_windows(pid):
    return list(_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])


def _settings_item(pid):
    """(the app menu's settings item, its bar item): the app menu is the menu bar's
    item 1 (item 0 is the Apple menu); the item's title starts with a settings word."""
    kids = menu_bar_items(pid)
    bar = kids[1] if len(kids) > 1 else None
    if bar is None:
        return None, None
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.7)
    menu = next((m for m in (_at(bar, "AXChildren") or []) if role(m) == "AXMenu"), None)
    for k in (_at(menu, "AXChildren") or []) if menu is not None else []:
        t = nfc(_s(_at(k, "AXTitle")) or "")
        if role(k) == "AXMenuItem" and t.startswith(SETTINGS_ITEMS):
            return k, bar
    _AS().AXUIElementPerformAction(bar, "AXCancel")
    time.sleep(0.3)
    return None, None


def _in(el, roles, n=6):
    cur, k = _at(el, "AXParent"), 0
    while cur is not None and k < n:
        if role(cur) in roles:
            return True
        cur, k = _at(cur, "AXParent"), k + 1
    return False


def _label_of(el):
    """A control's label: its title, else its title element's text, else the static
    text to its left on its row (the nearest; geometry, not element identity), else
    its description."""
    t = nfc(_s(_at(el, "AXTitle")) or "").strip()
    if t:
        return t
    te = _at(el, "AXTitleUIElement")
    if te is not None:
        t = nfc(_s(_at(te, "AXValue")) or _s(_at(te, "AXTitle")) or "").strip()
        if t:
            return t.rstrip(":").strip()
    f = _frame(el)
    par = _at(el, "AXParent")
    if f is not None and par is not None:
        cy = f[1] + f[3] / 2
        best = None
        for scope in (par, _at(par, "AXParent")):
            for k in find_all(scope, lambda e: role(e) == "AXStaticText", max_depth=2) if scope is not None else []:
                v = nfc(_s(_at(k, "AXValue")) or "").strip()
                g = _frame(k)
                if not v or g is None or abs((g[1] + g[3] / 2) - cy) > max(f[3], g[3]) / 2 + 2 or g[0] + g[2] > f[0] + 4:
                    continue
                d = f[0] - (g[0] + g[2])
                if best is None or d < best[0]:
                    best = (d, v)
            if best is not None:
                return best[1].rstrip(":").strip()
    return nfc(_s(_at(el, "AXDescription")) or "").strip()


def _group_label(el):
    """The label of a choice's group (its radio group's title element or the static
    text before the group), "" when none."""
    par = _at(el, "AXParent")
    if par is None or role(par) != "AXRadioGroup":
        return ""
    return _label_of(par)


def settings_tabs(w):
    """[(title, element, selected)] of the window's tabs (a tab group's choices, or
    the toolbar's buttons)."""
    out = []
    for tg in find_all(w, lambda e: role(e) == "AXTabGroup", max_depth=8):
        for k in _at(tg, "AXTabs") or [x for x in (_at(tg, "AXChildren") or []) if role(x) == "AXRadioButton"]:
            out.append((nfc(_s(_at(k, "AXTitle")) or "").strip(), k, bool(_at(k, "AXValue"))))
    if not out:
        for tb in find_all(w, lambda e: role(e) == "AXToolbar", max_depth=4):
            for k in find_all(tb, lambda e: role(e) in ("AXButton", "AXRadioButton"), max_depth=4):
                t = nfc(_s(_at(k, "AXTitle")) or _s(_at(k, "AXDescription")) or "").strip()
                if t:
                    out.append((t, k, bool(_at(k, "AXSelected")) or bool(_at(k, "AXValue"))))
    return out


def settings_controls(w):
    """[{label, kind, value, enabled, group, el}] of the tab shown (tabs and toolbar
    buttons aside)."""
    out = []
    for el in find_all(w, lambda e: role(e) in CONTROL_KINDS, max_depth=16):
        if role(el) == "AXRadioButton" and (role(_at(el, "AXParent")) == "AXTabGroup" or _in(el, ("AXToolbar",))):
            continue
        if _in(el, ("AXToolbar",)):
            continue
        kind = CONTROL_KINDS[role(el)]
        v = _at(el, "AXValue")
        value = (bool(v) if kind in ("checkbox", "choice") else nfc(_s(v) or _s(_at(el, "AXTitle")) or "").strip())
        out.append({"label": _label_of(el), "kind": kind, "value": value, "enabled": bool(_at(el, "AXEnabled")),
                    "group": _group_label(el) if kind == "choice" else "", "el": el})
    return out


def _public(cs):
    return [{k: v for k, v in c.items() if k != "el"} for c in cs]


def _open_settings(bid):
    """(pid, window, opened_by_us): the app must be running (the arm launches it)."""
    pid = activate(bid)
    if not pid:
        raise RuntimeError("not running")
    guard_before(pid, "settings")
    before = {_win_key(w) for w in _app_windows(pid)}
    item, bar = _settings_item(pid)
    if item is None:
        raise RuntimeError("no settings item (Einstellungen / Settings) in the app menu")
    title = nfc(_s(_at(item, "AXTitle")) or "")
    if X.irreversible_reason(title):
        _AS().AXUIElementPerformAction(bar, "AXCancel")
        raise RuntimeError(f"guard: {title!r}")
    r = EX.run(item, pid, G.record_selector(pid, item), "press")
    time.sleep(1.5)
    guard_after(pid, "settings open")
    new = [w for w in _app_windows(pid) if _win_key(w) not in before and doc_path(w) is None]
    if len(new) != 1:
        # never guessed: a window already open before the press, or several new ones, is not operated
        for w in new:
            cb = _at(w, "AXCloseButton")
            if cb is not None:
                _AS().AXUIElementPerformAction(cb, "AXPress")
        raise RuntimeError(f"{len(new)} new windows after {title!r} (press: {r.get('success')}): not operated")
    return pid, new[0], True, title


def _close_settings(pid, w, opened):
    if not opened or w is None:
        return
    cb = _at(w, "AXCloseButton")
    if cb is not None:
        _AS().AXUIElementPerformAction(cb, "AXPress")
        time.sleep(0.8)
    guard_after(pid, "settings close")


def _press_checked(pid, el, what):
    lab = _label_of(el)
    if X.irreversible_reason(lab):
        return {"success": False, "gated": True, "error": f"irreversible-action guard: {lab!r}"}
    if any(s in lab for s in WEB_SOURCES):
        return {"success": False, "refused": True, "error": f"{lab!r} names a web source: never pressed"}
    r = EX.run(el, pid, G.record_selector(pid, el), "press")
    time.sleep(0.7)
    guard_after(pid, what)
    return r


def _show_tab(pid, w, title):
    tabs = settings_tabs(w)
    hit = [t for t in tabs if X.norm(t[0]) == X.norm(title)]
    if len(hit) != 1:
        return False, f"{len(hit)} tabs titled {title!r} (tabs: {[t[0] for t in tabs]})"
    if hit[0][2]:
        return True, "shown"
    r = _press_checked(pid, hit[0][1], f"settings tab {title!r}")
    return bool(r.get("success")), "pressed" if r.get("success") else str(r.get("error"))


def op_settings(a):
    """Read or set ONE control of an app's own settings window (settings_set).
    action "read": every tab's controls; "set": tab, label, kind, value -> the control
    set, read back; "dump": the window's AX tree (development, read-only)."""
    bid = a["bundle"]
    need_managed(bid)
    if bid in SETTINGS_REFUSED:
        return {"status": "refused", "detail": "Lexikon's settings choose its sources (some reach the web): never operated"}
    action = a.get("action") or "read"
    try:
        pid, w, opened, item = _open_settings(bid)
    except RuntimeError as exc:
        return {"status": "failed", "detail": str(exc)[:200]}
    out = {"status": "done", "item": item, "window": nfc(_s(_at(w, "AXTitle")) or "")}
    tabs0 = settings_tabs(w)
    first = next((t[0] for t in tabs0 if t[2]), tabs0[0][0] if tabs0 else "")
    out["tabs"], out["tab_shown"] = [t[0] for t in tabs0], first
    try:
        if action == "dump":
            out["dump"] = ax_dump(w, max_depth=int(a.get("depth") or 12), budget=[1500])
            return out
        if action == "read":
            per = {}
            for t in [x[0] for x in tabs0] or [""]:
                if t:
                    ok, why = _show_tab(pid, w, t)
                    if not ok:
                        per[t] = {"error": why}
                        continue
                per[t] = _public(settings_controls(w))
            out["controls"] = per
            return out
        # set
        tab, label, kind, value = a.get("tab") or "", a.get("label") or "", a.get("kind") or "", a.get("value")
        if tab:
            ok, why = _show_tab(pid, w, tab)
            if not ok:
                return dict(out, status="failed", detail=f"tab: {why}")
        cs = [c for c in settings_controls(w) if X.norm(c["label"]) == X.norm(label) and (not kind or c["kind"] == kind)]
        if len(cs) != 1:
            return dict(out, status="failed", detail=f"{len(cs)} controls labelled {label!r} ({kind or 'any kind'})")
        c = cs[0]
        out["before"] = c["value"]
        if not c["enabled"]:
            return dict(out, status="failed", detail=f"{label!r} is disabled")
        if c["kind"] == "checkbox":
            want = bool(value)
            if c["value"] != want:
                r = _press_checked(pid, c["el"], f"setting {label!r}")
                if r.get("gated"):
                    return dict(out, status="gated", detail=r["error"])
                if not r.get("success"):
                    return dict(out, status="refused" if r.get("refused") else "failed", detail=str(r.get("error")))
        elif c["kind"] == "choice":
            want = True
            if not c["value"]:
                r = _press_checked(pid, c["el"], f"setting {label!r}")
                if not r.get("success"):
                    return dict(out, status="gated" if r.get("gated") else "failed", detail=str(r.get("error")))
        else:
            want = nfc(str(value or "")).strip()
            if X.norm(c["value"]) != X.norm(want):
                r = _press_checked(pid, c["el"], f"pop-up {label!r}")
                if not r.get("success"):
                    return dict(out, status="gated" if r.get("gated") else "failed", detail=str(r.get("error")))
                menu = next((m for m in (_at(c["el"], "AXChildren") or []) if role(m) == "AXMenu"), None)
                items = [k for k in (_at(menu, "AXChildren") or []) if role(k) == "AXMenuItem"] if menu is not None else []
                hit = [k for k in items if X.norm(_s(_at(k, "AXTitle"))) == X.norm(want)]
                if len(hit) != 1:
                    if menu is not None:
                        _AS().AXUIElementPerformAction(menu, "AXCancel")
                    return dict(out, status="failed", detail=f"{len(hit)} pop-up items titled {want!r}")
                r = _press_checked(pid, hit[0], f"pop-up item {want!r}")
                if not r.get("success"):
                    return dict(out, status="gated" if r.get("gated") else "failed", detail=str(r.get("error")))
        after = [x for x in settings_controls(w) if X.norm(x["label"]) == X.norm(label) and x["kind"] == c["kind"]]
        got = after[0]["value"] if len(after) == 1 else None
        out["after"] = got
        out["verified"] = (got == want) if c["kind"] != "popup" else X.norm(got) == X.norm(want)
        if not out["verified"]:
            out["status"] = "failed"
            out["detail"] = f"{label!r} reads {got!r} after the change, not {want!r}"
        return out
    finally:
        try:
            if first and action in ("read", "set"):
                _show_tab(pid, w, first)        # the tab shown at the opening again (the next opening shows it)
        finally:
            _close_settings(pid, w, opened)


def _leave_search(pid):
    """End an active Notes search, least invasive first, every action through the checked
    executor: Escape in the search field (an allowed key), else the field's own clear button (its label passes the
    irreversible-action guard), else the field emptied through its value and committed with Return (the other
    allowed key). (True, how) when no search is active any more."""
    sf = search_field_els(pid)
    if not sf:
        return False, "no search field"
    el, tried = sf[0], []
    EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=53)
    time.sleep(1.0)
    tried.append("escape")
    if not notes_searching(pid):
        return True, "escape"
    el = (search_field_els(pid) or [el])[0]
    btn = _at(el, "AXClearButton") or _at(el, "AXCancelButton")
    if btn is not None:
        lab = nfc(_s(_at(btn, "AXDescription")) or _s(_at(btn, "AXTitle")) or "")
        if not X.irreversible_reason(lab):
            EX.run(btn, pid, G.record_selector(pid, btn), "press")
            time.sleep(1.0)
            tried.append(f"clear button {lab!r}")
            if not notes_searching(pid):
                return True, "the clear button"
    el = (search_field_els(pid) or [el])[0]
    r = EX.run(el, pid, G.record_selector(pid, el), "enter_text", text="")
    if not r.get("refused"):
        EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=36)
        time.sleep(1.0)
        tried.append("emptied + Return")
    return (not notes_searching(pid)), ", ".join(tried)


def op_precondition(a):
    """Leave a DECLARED UI state that makes the app
    disable menu items (``desktop_e2e8.PRECONDITIONS``), so that the host can retry
    the press once: Notes — a note opened from the gallery hides the grid (back to
    the grid: Escape, else the toolbar's Zurück; the note stays selected); Finder —
    Quick Look open (Escape); TextEdit — the Open panel open (Abbrechen); Notes (desktop_e2e12) —
    a search is active (its view items are disabled; the search is left, its pattern reported
    as ``shown_before``). Status "done" only when a state was left; "unchanged" when none of
    them is present."""
    bid = a["bundle"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "unchanged", "restored": None, "detail": "not running"}
    guard_before(pid, "precondition")
    out = {"status": "unchanged", "restored": None, "detail": "no declared state found"}
    if bid == NOTES and a.get("leave_search") is False and notes_searching(pid):
        # the host of the frozen Arch8 arm passes leave_search=False: a search is not one of
        # desktop_e2e8's declared states and stays as it is (as that arm's guest did, byte for byte)
        out = {"status": "unchanged", "restored": None, "detail": "a Notes search is active: left as it is"}
    elif bid == NOTES and notes_searching(pid):
        # Notes disables its view items while a search is active: a DECLARED state
        # now (desktop_e2e12.PRECONDITIONS12). The search is left (its field emptied); the pattern is reported so
        # that the host enters it again after the one retried press
        sf = search_field_els(pid)
        pattern = nfc(_s(_at(sf[0], "AXValue")) or "") if sf else ""
        ok, how = _leave_search(pid)
        out = {"status": "done" if ok else "failed", "restored": "notes_search_active" if ok else None,
               "shown_before": pattern or None, "reshow": "notes_search",
               "detail": f"the search {pattern!r} left ({how})" if ok else f"the search did not end ({how})"}
    elif bid == NOTES and gallery_cells(pid) is None and note_table(pid) is None and note_body(pid) is not None:
        shown = (note_body(pid) or "").split("\n")[0].strip()
        ok = _gallery_back(pid)
        out = {"status": "done" if ok else "failed", "restored": "gallery_item_open" if ok else None,
               "shown_before": shown or None, "reshow": "notes_notes",
               "detail": "back to the gallery grid" if ok else "the grid did not come back"}
    elif bid == FINDER and _ql_window(pid) is not None:
        ok = _ql_close(pid)
        out = {"status": "done" if ok else "failed", "restored": "quick_look_open" if ok else None,
               "detail": "Quick Look closed" if ok else "Quick Look did not close"}
    elif bid == TEXTEDIT and _panel(pid) is not None:
        b = _panel_button(_panel(pid), "Abbrechen")
        if b is not None:
            EX.run(b, pid, G.record_selector(pid, b), "press")
            time.sleep(1.0)
        ok = _panel(pid) is None
        out = {"status": "done" if ok else "failed", "restored": "open_panel" if ok else None,
               "detail": "the Open panel cancelled" if ok else "the Open panel did not close"}
    guard_after(pid, "precondition")
    return out


OPS = {"axdump": op_axdump, "hello": op_hello, "probe4": op_probe4, "front": op_front, "launch": op_launch, "rows": op_rows, "select": op_select,
       "marks": op_marks, "press": op_press, "enter": op_enter, "field": op_field,
       "search_fields": op_search_fields, "shown_folder": op_shown_folder, "listing": op_listing,
       "file": op_file, "ax_text": op_ax_text, "ax_step": op_ax_step, "read_state": op_read_state,
       "clean_start": op_clean_start, "seed": op_seed, "survey": op_survey, "restore": op_restore,
       "seed3": op_seed3, "te_prefs": op_te_prefs,
       "read_text": op_read_text, "previews": op_previews, "panel": op_panel, "screen": op_screen,
       "notes_verify": op_notes_verify, "item_dates": op_item_dates,
       "settings": op_settings, "precondition": op_precondition,
       "restore_file": op_restore_file, "fault": op_fault}


# ── development probe (never a measured run) ──────────────
# Why does Notes keep "Als Liste" disabled after a search was left? ``view_probe`` is
# READ-ONLY: the View menu is opened and cancelled to read its VALIDATED items (as every press does); nothing is
# pressed, nothing typed. ``probe_act`` is a CLOSED set of single generic actions, each behind the prompt guards:
# keys and presses through the checked executor; the harness's own selection / focus attributes (as ``_select``
# sets a selection); one real click, only on a hit-tested sidebar row of Notes' own window.

#: Notes' sidebar rows that are not this test's folders (the account, the search scope, smart folders)
NOTES_SMART = ("Auf meinem Mac", "Alle auf meinem Mac", "Notizen", "Geteilt", "Tags", "Alle Tags", "Schlagwörter",
               "Schnelle Notizen", "Zuletzt gelöscht", "Shared", "All on My Mac", "Quick Notes", "Recently Deleted")
PROBE_ACTS = ("leave_escape", "leave_clear", "leave_empty", "select_folder", "click_folder", "focus_list",
              "escape_twice", "cmd_1", "cmd_2", "precondition", "relaunch")
CMD_FLAG = 1 << 20              # kCGEventFlagMaskCommand
KEY_1, KEY_2 = 18, 19           # the "1" / "2" keys (the same keycodes on the German layout)


def _brief(el):
    """One element as plain data (role, subrole, identifier, title, description, value, focused / enabled /
    selected)."""
    if el is None:
        return None
    d = {"role": role(el)}
    for k in ("AXSubrole", "AXIdentifier", "AXTitle", "AXDescription", "AXPlaceholderValue"):
        v = _s(_at(el, k))
        if v and v.strip():
            d[k[2:].lower()] = nfc(v).strip()[:80]
    v = _at(el, "AXValue")
    if isinstance(v, str) and v.strip():
        d["value"] = nfc(v).strip()[:60]
    for k in ("AXFocused", "AXEnabled", "AXSelected"):
        b = _at(el, k)
        if isinstance(b, bool):
            d[k[2:].lower()] = b
    return d


def _chain(el, n=16):
    """The ancestors of ``el`` up to its window, each as role[/subrole][#identifier][(description)]."""
    out, cur = [], el
    for _ in range(n):
        cur = _at(cur, "AXParent")
        if cur is None:
            break
        b = _brief(cur)
        out.append(b["role"] + (f"/{b['subrole']}" if b.get("subrole") else "") +
                   (f"#{b['identifier']}" if b.get("identifier") else "") +
                   (f"({b['description']})" if b.get("description") else ""))
        if b["role"] in ("AXWindow", "AXApplication"):
            break
    return out


def focus_in(el_brief, chain):
    """Where the keyboard focus is, in Notes' terms: search_field / note_body / note_list / gallery / sidebar /
    other / nothing (pure: reads the focused element's brief and its ancestor chain)."""
    if not el_brief:
        return "nothing"
    me = f"{el_brief.get('role', '')}/{el_brief.get('subrole', '')}#{el_brief.get('identifier', '')}" \
         f"({el_brief.get('description', '')})"
    for where, here in (("search_field", "AXSearchField"), ("note_body", "Note Body Text View"),
                        ("note_list", "ICMNoteListTableView"), ("gallery", GALLERY_PREFIX), ("sidebar", "(Ordner)")):
        if here in me or any(here in c for c in chain):
            return where
    return "other"


def _gallery_view_el(w):
    g = find_all(w, lambda e: (_s(_at(e, "AXIdentifier")) or "").startswith(GALLERY_PREFIX), max_depth=12) \
        if w is not None else []
    return g[0] if g else None


def _menu_closed(pid, ti, titles):
    """The items' AXEnabled as the CLOSED menu reports it (no validation pass since the last opening)."""
    kids = menu_bar_items(pid)
    out = {}
    if ti is None or ti >= len(kids):
        return out
    for m in _at(kids[ti], "AXChildren") or []:
        if role(m) == "AXMenu":
            for it in _at(m, "AXChildren") or []:
                t = nfc(_s(_at(it, "AXTitle")) or "")
                if t in titles:
                    out[t] = bool(_at(it, "AXEnabled"))
    return out


def _toolbar(w):
    tbs = [k for k in (_at(w, "AXChildren") or []) if role(k) == "AXToolbar"] if w is not None else []
    if not tbs:
        return {"present": False}
    items = []
    for el in find_all(tbs[0], lambda e: role(e) in ("AXButton", "AXRadioButton", "AXCheckBox", "AXMenuButton",
                                                       "AXPopUpButton", "AXSegmentedControl", "AXRadioGroup"),
                       max_depth=7):
        b = _brief(el)
        if b.get("description") or b.get("title") or b.get("identifier"):
            items.append(b)
    return {"present": True, "items": items[:30]}


def view_state(pid, top="Darstellung", titles=("Als Liste", "Als Galerie")):
    """The candidate causes of the disabled view items, read in one pass (READ-ONLY), then the View menu opened and
    cancelled to read its validated items."""
    app = _AS().AXUIElementCreateApplication(pid)
    w = front_window(pid)
    fw = _at(app, "AXFocusedWindow")
    foc = _at(app, "AXFocusedUIElement")
    fb, chain = _brief(foc), (_chain(foc) if foc is not None else [])
    o, side = notes_sidebar(pid)
    tab = note_table(pid)
    gl = _gallery_view_el(w)
    cells = gallery_cells(pid)
    rows = note_rows(pid)
    bodies = find_all(w, lambda e: role(e) == "AXTextArea" and _s(_at(e, "AXIdentifier")) == "Note Body Text View") \
        if w is not None else []
    body = note_body(pid)
    sfs = search_field_els(pid)
    search = []
    for el in sfs:
        search.append({"value": nfc(_s(_at(el, "AXValue")) or ""), "focused": _at(el, "AXFocused"),
                       "clear_button": _brief(_at(el, "AXClearButton")),
                       "cancel_button": _brief(_at(el, "AXCancelButton")),
                       "children": [_brief(k) for k in (_at(el, "AXChildren") or [])][:6]})
    ti = top_index(pid, top)
    closed = _menu_closed(pid, ti, titles)
    items = read_menu(pid, ti) if ti is not None else None          # the menu OPEN: validated items
    foc2 = _at(_AS().AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    top_items = [it for it in (items or []) if it["path"].count(" > ") == 1]
    view = {}
    for t in titles:
        it = next((x for x in top_items if x["title"] == t), None)
        view[t] = {"enabled": it["enabled"] if it else None, "enabled_closed": closed.get(t),
                   "mark": it["mark"] if it else None}
    return {
        "view_items": view,
        "menu": [{"title": it["title"], "enabled": it["enabled"], "mark": it["mark"]} for it in top_items],
        "window": {"title": nfc(_s(_at(w, "AXTitle")) or "") if w is not None else None,
                   "main": _at(w, "AXMain") if w is not None else None,
                   "focused": _at(w, "AXFocused") if w is not None else None,
                   "fullscreen": _at(w, "AXFullScreen") if w is not None else None,
                   "focused_window": nfc(_s(_at(fw, "AXTitle")) or "") if fw is not None else None,
                   "n_windows": len(_app_windows(pid))},
        "focus": {"in": focus_in(fb, chain), "el": fb, "chain": chain,
                  "after_menu": focus_in(_brief(foc2), _chain(foc2) if foc2 is not None else [])},
        "sidebar": {"focused": _at(o, "AXFocused") if o is not None else None,
                    "selected": [r["label"] for r in side if r["selected"]],
                    "special": [{"label": r["label"], "selected": r["selected"]} for r in side
                                if r["label"] in NOTES_SMART],
                    "n_rows": len(side)},
        "note_list": {"present": tab is not None, "identifier": _s(_at(tab, "AXIdentifier")) if tab is not None else None,
                      "focused": _at(tab, "AXFocused") if tab is not None else None,
                      "n_rows": len(_at(tab, "AXRows") or []) if tab is not None else 0},
        "gallery": {"present": gl is not None, "identifier": _s(_at(gl, "AXIdentifier")) if gl is not None else None,
                    "focused": _at(gl, "AXFocused") if gl is not None else None,
                    "n_cells": len(cells) if cells is not None else None},
        "rows": [{"label": r["label"], "selected": r["selected"]} for r in rows][:12],
        "body": {"present": body is not None, "first_line": (body or "").split("\n")[0] if body is not None else None,
                 "focused": _at(bodies[0], "AXFocused") if bodies else None, "n": len(bodies)},
        # desktop_e2e8's declared state, tested exactly as ``op_precondition`` tests it
        "gallery_note_open": cells is None and tab is None and body is not None,
        "searching": notes_searching(pid),
        "search": search,
        "toolbar": _toolbar(w),
        "shown_folder": notes_shown_folder(pid),
    }


def op_view_probe(a):
    """Development only: READ-ONLY dump of the View menu's "Als Liste" / "Als Galerie"
    (AXEnabled read with the menu closed and open, their marks) with the candidate causes: the selected
    sidebar row and the smart folders, the first responder (the focused element and its ancestors), the note
    list / gallery grid / note body and their focus, a gallery note open, the search field, the toolbar."""
    bid = a.get("bundle") or NOTES
    if bid != NOTES:
        raise ValueError("view_probe reads Notes only")
    pid = activate(bid)
    if not pid:
        return {"status": "failed", "detail": "not running"}
    guard_before(pid, "view probe")
    st = view_state(pid)
    guard_after(pid, "view probe")
    return {"status": "done", **st}


def _click_sidebar_row(pid, label):
    """A real left click on the ONE sidebar row carrying ``label``: the point is hit-tested first (the element
    there must be that row of Notes' own sidebar, in Notes' process) and its label passes the
    irreversible-action guard; the click is posted only then (drive gate on)."""
    from anyuse.execution.actions import drive_enabled
    if not drive_enabled():
        return {"status": "refused", "detail": "drive gate off"}
    o, rows = notes_sidebar(pid)
    hit = [r for r in rows if r["label"] == nfc(label)]
    if len(hit) != 1:
        return {"status": "refused", "detail": f"{len(hit)} folders named {label!r}"}
    if X.irreversible_reason(label):
        return {"status": "gated", "detail": "irreversible-action guard on the folder's label"}
    fr = _frame(hit[0]["el"])
    if fr is None:
        return {"status": "failed", "detail": "the row has no frame"}
    x, y = fr[0] + min(fr[2] / 2.0, 70.0), fr[1] + fr[3] / 2.0
    A = _AS()
    err, at = A.AXUIElementCopyElementAtPosition(A.AXUIElementCreateApplication(pid), float(x), float(y), None)
    cur, row_el, n = at, None, 0
    while cur is not None and n < 12:
        if role(cur) == "AXRow":
            row_el = cur
            break
        cur, n = _at(cur, "AXParent"), n + 1
    try:
        same_pid = int(A.AXUIElementGetPid(at, None)[1]) == int(pid) if at is not None else False
    except Exception:  # noqa: BLE001
        same_pid = False
    tx = texts_of(row_el) if row_el is not None else []
    if int(err) != 0 or row_el is None or not same_pid or not tx or tx[0] != nfc(label):
        return {"status": "refused", "detail": f"hit-test at ({x:.0f},{y:.0f}): err={int(err)} row={tx[:1]} "
                                               f"same_pid={same_pid}: not clicked"}
    import Quartz as Q
    interlock("posted click")
    pt = Q.CGPointMake(float(x), float(y))
    for t in (Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
        Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, t, pt, Q.kCGMouseButtonLeft))
        time.sleep(0.08)
    time.sleep(1.5)
    return {"status": "done", "detail": f"clicked {label!r} at ({x:.0f},{y:.0f}); shown={notes_shown_folder(pid)!r}"}


def op_probe_act(a):
    """Development only: ONE generic action from a closed set (``PROBE_ACTS``) on Notes, behind
    the prompt guards. leave_escape / leave_clear / leave_empty: the three ways ``_leave_search``
    leaves a search, each alone; select_folder: the folder through its selection attribute (``desktop_e2e12``'s
    re-show); click_folder: a real click on its hit-tested sidebar row; focus_list: the note list (or gallery grid)
    given keyboard focus; escape_twice: Escape twice to the front window; cmd_1 / cmd_2: the view items' own key
    equivalents (Command-1 / Command-2), modifiers released after; precondition: the declared-state op
    once more; relaunch: Notes quit and opened again (the known fix)."""
    act = a["act"]
    if act not in PROBE_ACTS:
        raise ValueError(f"unknown probe act {act!r}")
    pid = activate(NOTES)
    if not pid:
        return {"status": "failed", "detail": "not running"}
    if act == "precondition":
        return {"status": "done", "precondition": op_precondition({"bundle": NOTES})}
    guard_before(pid, f"probe act {act}")
    out = {"status": "done", "detail": ""}
    if act.startswith("leave_"):
        sf = search_field_els(pid)
        if not sf:
            out = {"status": "failed", "detail": "no search field"}
        elif act == "leave_escape":
            r = EX.run(sf[0], pid, G.record_selector(pid, sf[0]), "press_key", keycode=53)
            out["detail"] = f"escape in the field: {r['success']}"
        elif act == "leave_clear":
            btn = _at(sf[0], "AXClearButton") or _at(sf[0], "AXCancelButton")
            lab = nfc(_s(_at(btn, "AXDescription")) or _s(_at(btn, "AXTitle")) or "") if btn is not None else ""
            if btn is None:
                out = {"status": "failed", "detail": "the field has no clear button"}
            elif X.irreversible_reason(lab):
                out = {"status": "gated", "detail": f"irreversible-action guard on {lab!r}"}
            else:
                r = EX.run(btn, pid, G.record_selector(pid, btn), "press")
                out["detail"] = f"clear button {lab!r}: {r['success']}"
        else:
            r = EX.run(sf[0], pid, G.record_selector(pid, sf[0]), "enter_text", text="")
            if not r.get("refused"):
                el = (search_field_els(pid) or sf)[0]
                EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=36)
            out["detail"] = f"field emptied + Return: {r['success']}"
        time.sleep(1.0)
        out["searching"] = notes_searching(pid)
    elif act == "select_folder":
        out = _select(pid, "notes_folders", a["label"])
    elif act == "click_folder":
        out = _click_sidebar_row(pid, a["label"])
    elif act == "focus_list":
        w = front_window(pid)
        el = note_table(pid)
        el = el if el is not None else _gallery_view_el(w)
        if el is None:
            out = {"status": "failed", "detail": "no note list or gallery grid shown"}
        else:
            err = _AS().AXUIElementSetAttributeValue(el, "AXFocused", True)
            time.sleep(1.0)
            out["detail"] = f"err={int(err)} focused={_at(el, 'AXFocused')}"
    elif act == "escape_twice":
        for _ in range(2):
            w = front_window(pid)
            EX.run(w, pid, G.record_selector(pid, w), "press_key", keycode=53)
            time.sleep(1.0)
        out["detail"] = "escape x2 to the front window"
    elif act in ("cmd_1", "cmd_2"):
        w = front_window(pid)
        r = EX.run(w, pid, G.record_selector(pid, w), "press_key", keycode=KEY_1 if act == "cmd_1" else KEY_2,
                   flags=CMD_FLAG)
        time.sleep(1.2)
        out["detail"] = f"command-{act[-1]}: {r['success']}"
    elif act == "relaunch":
        setup = []
        quit_app(NOTES, setup)
        pid = open_app(NOTES, fresh=False, wait=4.0)
        out["detail"] = "; ".join(setup) + f"; relaunched pid={pid}"
    guard_after(pid, f"probe act {act}")
    out["act"] = act
    return out


OPS.update({"view_probe": op_view_probe, "probe_act": op_probe_act})     # development probe


# ── a compiled desktop tool run in the guest (execution/desktop_tools.py) ────────────
# The MCP server never acts on its own host: it sends the proof-pinned record here. The guest re-checks the pin,
# binds an AX tree to the recorded app's ONE running process (nothing is launched; the rule of cli._pid_tree), and
# runs the checked steps (StateGraphExecutor.execute_checked_step, drift gate on, no vision fallback), each step
# behind the interlock; a draft workflow through draft_compile.run_steps (its value checks; its approval level:
# auto/off holds nothing, a held step finds no person here and stops). A routine is not run here: the host replays it
# op by op through this channel.

def app_pid(app):
    """(pid, why not): the ONE running process of the recorded app, by bundle id, else by name (``pgrep -x``)."""
    bid, name = str(app.get("bundle_id") or ""), str(app.get("name") or "")
    if bid:
        import AppKit
        # LIVE: NSRunningApplication's class lookup asks LaunchServices afresh, as
        # ``running`` does; NSWorkspace.runningApplications is refreshed only by a run loop, which this job does not
        # spin -- a TextEdit relaunched by the clean start would be "0 running processes", so no tool, replay or
        # screen read could bind to it
        apps = [x for x in (AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid) or [])
                if str(x.bundleIdentifier() or "") == bid]
        if len(apps) != 1:
            return None, f"{len(apps)} running processes of {bid!r}; exactly one is needed (nothing is launched)"
        return int(apps[0].processIdentifier()), None
    if name:
        out = subprocess.run(["/usr/bin/pgrep", "-x", name], capture_output=True, text=True, timeout=10).stdout.split()
        if len(out) != 1:
            return None, f"{len(out)} running processes named {name!r}; exactly one is needed (nothing is launched)"
        return int(out[0]), None
    return None, "the record names no app"


def app_tree(pid):
    """The AX tree bound to that process."""
    from anyuse.context.accessibility import AccessibilityTree
    return AccessibilityTree(pid=pid)


#: the canonical roles of a document window's own controls (TextEdit's "First Text View" in every window)
DOCUMENT_ROLES = ("textarea",)


def front_document_scope(step, pid):
    """A text-area step that names no document -- neither its recorded selector nor its command (a map-compiled
    "write in the open document" step; a draft recorded before selectors carried documents) -- acts on the FRONT
    document: the guest names it in the command, so the lookup is scoped by that document and the executor checks
    the document right before acting; with two windows open it is never refused as ambiguous, never another
    window's. Every other step unchanged."""
    cmd = dict(step.command or {})
    sel = cmd.get("selector") or {}
    if step.modality != "ax" or cmd.get("document") or not isinstance(sel, dict) or sel.get("document") \
            or sel.get("canonical_role") not in DOCUMENT_ROLES:
        return step
    w = front_window(pid) if pid else None
    doc = doc_path(w) if w is not None else None
    if not doc:
        return step
    cmd["document"] = doc
    step.command = cmd
    return step


class GuardedCheckedExecutor:
    """A checked executor whose every step asks the interlock first; a text-area step that names no document is
    scoped by the front document (``front_document_scope``)."""

    def __init__(self, ex, pid=None):
        self.ex = ex
        self.pid = pid
        # ax_actions.executor_supports reads the WRAPPED executor's table through this
        # attribute; without it every version-2 step (select_row, focused, append_text, replace_line, open_app) is
        # refused on the guest MCP path (fail-closed)
        self.__wrapped_executor__ = ex

    async def execute_checked_step(self, step, **kw):
        interlock(f"desktop tool step {kw.get('step_idx', '')}".strip())
        if self.pid:
            step = front_document_scope(step, self.pid)
        return await self.ex.execute_checked_step(step, **kw)

    def __getattr__(self, name):
        return getattr(self.ex, name)


def guest_tree_factory(draft, why=None):
    """``tree_factory(app)`` for a multi-app tool in the guest (``draft_compile.AppRouter``): the AX tree of the named
    app's ONE running process -- a ``BundleId`` by bundle id, a plain name by name -- never launched, the interlock
    asked first. An app the record does not name, or one the restriction policy blocks (``ANYUSE_APP_BLOCKLIST`` /
    ``_ALLOWLIST``), gets no tree: AppRouter refuses that step (``no_tree``) and nothing is sent to it. ``why`` (a
    list) collects the reason of every app that got no tree."""
    from anyuse.core.restriction import RestrictionPolicy
    from anyuse.execution.draft_compile import BundleId
    apps = {str(a.get("bundle_id") or "") for a in draft.get("apps") or [] if isinstance(a, dict)}
    apps.add(str(draft.get("bundle_id") or ""))
    apps.discard("")
    policy = RestrictionPolicy.from_settings()
    why = why if why is not None else []

    def factory(app):
        interlock(f"desktop tool tree {app}")
        key = str(app or "")
        by_bid = isinstance(app, BundleId)
        if by_bid and key not in apps:
            why.append(f"{key}: not one of the record's apps")
            return None
        ok, reason = policy.is_app_target_allowed(key) if by_bid else policy.is_app_allowed(key)
        if not ok:
            why.append(f"{key}: {reason}")
            return None
        pid, w = app_pid({"bundle_id": key} if by_bid else {"name": key})
        if pid is None:
            why.append(f"{key}: {w}")
            return None
        return app_tree(pid)
    factory.why = why
    return factory


def guest_executor_factory(tree):
    """One checked executor per app of a multi-app tool, behind the interlock like the first app's."""
    from anyuse.execution import draft_compile as DC
    return GuardedCheckedExecutor(DC.checked_executor(tree), pid=getattr(tree, "pid", None))


async def _run_checked(rec, args, ex, on_step=None):
    from anyuse.execution.plan_types import PlannedStep
    from anyuse.guides.state_graph import StateGraph
    from anyuse.mcpserver.desktop_compiled_tools import bind_steps
    from anyuse.memory.models import WorkflowTemplate
    workflow = WorkflowTemplate.from_dict(rec.get("workflow_template") or {
        "id": rec.get("workflow", ""), "name": rec.get("workflow", "")})
    graph = StateGraph.from_dict(rec.get("state_graph") or {})
    results = []
    # every step bound first (each AX step's selector carries the record's app): a step naming another app refuses
    # the whole call before ANY step runs
    bound_steps, why = bind_steps(rec, args)
    if why:
        return results, False, why
    for index, bound in enumerate(bound_steps):
        if on_step is not None:
            on_step(index)
        step = PlannedStep.from_dict(bound)
        executed, verification = await ex.execute_checked_step(step, step_idx=index, workflow=workflow,
                                                                state_graph=graph)
        results.append({"execution": executed, "verification": verification})
        if not executed.get("success"):
            return results, False, executed.get("error") or f"step {index} failed"
    return results, True, None


def op_run_tool(a):
    from anyuse.mcpserver import desktop_compiled_tools as DT
    rec, args = a.get("record") or {}, dict(a.get("arguments") or {})
    if not DT.verify_record(rec):
        return {"status": "refused", "detail": "the record's proof pin does not verify in the guest"}
    cls = rec.get("compile_class")
    if cls == "routine":
        return {"status": "refused", "detail": "a routine is replayed op by op from the host, not as one tool"}
    app = DT.record_app(rec)
    pid, why = app_pid(app)
    if pid is None:
        return {"status": "done", "ok": False, "stopped": why, "steps": [], "app": app}
    interlock(f"desktop tool {rec.get('workflow')!r}")
    from anyuse.execution import draft_compile as DC
    tree = app_tree(pid)
    ex = GuardedCheckedExecutor(DC.checked_executor(tree), pid=pid)
    on_step = getattr(tree, "at_step", None)          # a tree that wants the step index is told it
    if cls == "draft_workflow":
        from anyuse.execution.actions import drive_enabled
        draft = (rec.get("payload") or {}).get("record") or {}

        async def no_person(_ctx):
            return False
        no_tree = []
        out = asyncio.run(DC.run_steps(draft, args, executor=ex, approver=no_person, on_step=on_step,
                                       decide_as_driving=drive_enabled(), hil_mode=DC.effective_hil_mode(draft),
                                       tree_factory=guest_tree_factory(draft, no_tree),
                                       executor_factory=guest_executor_factory, default_tree=tree))
        steps, ok, stopped = out.get("steps") or [], bool(out.get("ok")), out.get("stopped")
        if no_tree and not ok:
            stopped = f"{stopped or 'a step was refused'} ({'; '.join(no_tree)[:300]})"
    else:
        steps, ok, stopped = asyncio.run(_run_checked(rec, args, ex, on_step))
    return {"status": "done", "ok": ok, "stopped": stopped, "steps": steps, "app": dict(app, pid=pid),
            "served_by": "guest:execute_checked_step"}


OPS["run_tool"] = op_run_tool       # a compiled desktop tool from the MCP server, run in the guest


# ── the agent harness's screen read (execution/harness_tools.DesktopTools.observe) ──────────────
# ``ax_elements``: the numbered elements of ONE app's front window and menu bar, each with its role, title,
# identifier, path (the recorder's selector, ``record_selector``), value (never a secure field's), enabled and
# in_menubar, plus the window's static text. A READ, but behind the foreground interlock like every other guest
# path: a visible guest answers the typed refusal and nothing is read.

AX_ELEMENTS_CAP = 300
AX_ELEMENTS_TEXT_CHARS = 8000


def ax_element_row(pid, el, in_menubar):
    """One element as the harness lists it (None for a role the harness never acts on)."""
    from anyuse.context.accessibility import ACTIONABLE_ROLES
    r = role(el)
    if r not in ACTIONABLE_ROLES:
        return None
    try:
        sel = G.record_selector(pid, el) or {}
    except Exception:  # noqa: BLE001 - an element the tree cannot address keeps its raw attributes
        sel = {}
    secure = r == "AXSecureTextField" or _s(_at(el, "AXSubrole")) == "AXSecureTextField"
    val = None if secure else _at(el, "AXValue")
    enabled = _at(el, "AXEnabled")
    return {"role": r, "title": (_s(_at(el, "AXTitle")) or _s(_at(el, "AXDescription")) or sel.get("name") or "")[:200],
            # the raw AXTitle too: the drift gate's live capture labels an element by it alone, so a drift baseline
            # built from this read must too ("title" above is the label a policy reads)
            "ax_title": (_s(_at(el, "AXTitle")) or "")[:200],
            "identifier": (sel.get("identifier") or _s(_at(el, "AXIdentifier")) or "")[:120],
            "path": str(sel.get("path") or "")[:200],
            "value": (str(val)[:200] if isinstance(val, (str, int, float)) and not isinstance(val, bool) else ""),
            "enabled": enabled is not False, "in_menubar": bool(in_menubar), "secure": secure}


#: the app menu's submenu the agent harness is never offered: other apps' commands (Dienste / Services)
NOT_OFFERED_SUBMENUS = ("Dienste", "Services")
#: the Apple menu's menu-bar item as the AX tree names it
APPLE_MENU_TITLES = ("Apple",)


def offered_menu_elements(menubar, max_depth=6):
    """The menu bar as the agent harness may use it: every menu-bar item and menu
    item in document order EXCEPT the Apple menu (menu-bar item "Apple": shut down, restart, log out, force quit,
    recent items -- never part of an app's task) and the app menu's Services submenu. TextEdit's menu bar listed 289
    items with those first, so a policy shown the first 60 never saw Ablage > Sichern (item 109)."""
    out = []
    tops = [k for k in (_at(menubar, "AXChildren") or []) if role(k) == "AXMenuBarItem"]
    for top in tops:
        if nfc(_s(_at(top, "AXTitle")) or "") in APPLE_MENU_TITLES:
            continue                                          # the Apple menu, by its name (never by position)
        out.append(top)
        stack = [(k, 1) for k in reversed(_at(top, "AXChildren") or [])]
        while stack and len(out) < 4 * AX_ELEMENTS_CAP:
            el, depth = stack.pop()
            if role(el) == "AXMenuItem":
                if nfc(_s(_at(el, "AXTitle")) or "") in NOT_OFFERED_SUBMENUS:
                    continue                                  # the item and its whole submenu
                out.append(el)
            if depth < max_depth:
                stack.extend((k, depth + 1) for k in reversed(_at(el, "AXChildren") or []))
    return out


def op_ax_elements(a):
    """The screen for the agent harness: ``{"app" | "bundle"}`` names the ONE running process (never launched)."""
    interlock("ax_elements read")
    app = {"bundle_id": str(a.get("bundle") or ""), "name": str(a.get("app") or "")}
    pid, why = app_pid(app)
    if pid is None:
        return {"status": "done", "ok": False, "detail": why, "elements": [], "app": app["name"] or app["bundle_id"]}
    guard_before(pid, "ax_elements")
    root = _AS().AXUIElementCreateApplication(pid)
    w = front_window(pid)
    rows = []
    from anyuse.context.accessibility import ACTIONABLE_ROLES
    if w is not None:
        for el in find_all(w, lambda e: role(e) in ACTIONABLE_ROLES, max_depth=24)[:AX_ELEMENTS_CAP]:
            row = ax_element_row(pid, el, False)
            if row:
                rows.append(row)
    mb = _at(root, "AXMenuBar") if root is not None else None
    if mb is not None:
        for el in offered_menu_elements(mb)[:AX_ELEMENTS_CAP]:
            row = ax_element_row(pid, el, True)
            if row:
                rows.append(row)
    texts = []
    if w is not None:
        for el in find_all(w, lambda e: role(e) == "AXStaticText", max_depth=24)[:200]:
            t = _s(_at(el, "AXValue")) or _s(_at(el, "AXTitle"))
            if t:
                texts.append(t)
    fingerprint = None
    try:
        # the recorder's fingerprint of the live app (``tree.snapshot()``: the capture the drift gate reads before a
        # replayed step), so the harness compiles its drift baseline from the same view
        from anyuse.memory.recorder import _build_fingerprint_from_snapshot
        snap = app_tree(pid).snapshot()
        fp = _build_fingerprint_from_snapshot(snap)
        if fp is not None:
            # the harness's anchors: the actionable elements OUTSIDE the menu bar (a menu's items exist only once it
            # was opened); the count, the hash and the scoped counts stay the whole capture's
            fp.required_elements = [dict({"role": e.role}, **({"title": e.title} if e.title else {}))
                                    for e in snap.anchor_elements if not e.in_menubar]
        fingerprint = fp.to_dict() if fp is not None else None
    except Exception as exc:  # noqa: BLE001 - no fingerprint: the harness builds its own, as before
        log("ax_elements: no recorder fingerprint:", type(exc).__name__)
    return {"status": "done", "ok": True, "app": app["name"] or app["bundle_id"], "bundle": app["bundle_id"],
            "pid": pid, "window": (_s(_at(w, "AXTitle")) if w is not None else "") or "", "elements": rows,
            "text": "\n".join(texts)[:AX_ELEMENTS_TEXT_CHARS], "fingerprint": fingerprint,
            "interlock": interlock_status()}


OPS["ax_elements"] = op_ax_elements   # the agent harness's screen read


# ── guest replays of draft workflows ───────────────────────────────────────────────
# ``replay_draft``: a READY draft workflow -- the host's ``from-draft``, the agent harness's compile, a ``from-map``
# record, each proved on its recorded world -- replayed HERE with new values through the seam ``anyuse workflow
# replay`` runs (``draft_compile.replay_saved``: the record's version, ready, a known-wrong replay, the runner's
# capabilities, the drive gate and the interlock, the value check, then the checked steps at the workflow's OWN
# approval level). A started replay asks nothing; a level that opts into holds finds no person
# here and stops. This is how a draft workflow gets the guest replays its MCP tool needs
# (``desktop_tools.guest_proof``): ``run_tool`` runs only a record whose guest proof pin already verifies.
# Nothing is launched: the record's app must run with exactly one process (the host's clean start opens it).

REPLAY_STORE = "~/anyuse-guest/data/vc_replay"      # one fresh WorkflowStorage per replay (kept as evidence)
STEP_BRIEF_KEYS = ("idx", "intent", "success", "error", "refused", "blocked", "method", "llm_calls", "drift_fired",
                   "drift_note", "reason_code", "held", "ai_filled",
                   # the step's own evidence (draft_compile's replay entry): a state-aware toggle's states, an app
                   # switch's polls and the source that answered
                   "outcome_kind", "acted", "pressed", "state_before", "state_after", "target_state", "activation",
                   "front", "front_source")


def _step_brief(s):
    s = s if isinstance(s, dict) else {}
    return {k: s.get(k) for k in STEP_BRIEF_KEYS if k in s}


def op_replay_draft(a):
    import tempfile
    from anyuse.execution import draft_compile as DC
    from anyuse.memory.storage import WorkflowStorage
    rec, values = a.get("record"), a.get("values") or {}
    if not isinstance(rec, dict) or not rec.get("workflow") or not isinstance(values, dict):
        return {"status": "refused", "detail": "replay_draft needs a draft workflow record and its values"}
    if rec.get("status") != "ready":
        return {"status": "refused", "detail": "the record is not ready: "
                + "; ".join(str(r) for r in rec.get("status_reasons") or [])[:300]}
    values = {str(k): str(v) for k, v in values.items()}
    app = {"bundle_id": str(rec.get("bundle_id") or ""), "name": str(rec.get("app") or "")}
    pid, why = app_pid(app)
    if pid is None:
        return {"status": "done", "ok": False, "stopped": why, "steps": [], "app": app, "llm_calls": 0}
    interlock(f"replay draft {str(rec.get('workflow'))[:60]!r}")
    base = exp(REPLAY_STORE)
    os.makedirs(base, exist_ok=True)
    storage = WorkflowStorage(base_dir=tempfile.mkdtemp(prefix="replay-", dir=base))
    saved = DC.save_workflow(json.loads(json.dumps(rec)), storage=storage)
    if saved.get("status") != "ready":
        return {"status": "refused", "detail": f"the record did not save as ready ({saved.get('status')})"}
    tree = app_tree(pid)
    ex = GuardedCheckedExecutor(DC.checked_executor(tree), pid=pid)
    no_tree = []

    async def no_person(_ctx):
        return False
    out = asyncio.run(DC.replay_saved(saved["name"], values, approver=no_person, ax_tree=tree, storage=storage,
                                      executor=ex, on_step=getattr(tree, "at_step", None),
                                      tree_factory=guest_tree_factory(rec, no_tree),
                                      executor_factory=guest_executor_factory))
    steps = [_step_brief(s) for s in out.get("steps") or []]
    stopped = out.get("stopped")
    if no_tree and not out.get("ok"):
        stopped = f"{stopped or 'a step was refused'} ({'; '.join(no_tree)[:300]})"
    pre = out.get("preflight") or {}
    return {"status": "done", "ok": bool(out.get("ok")), "stopped": stopped, "steps": steps,
            "llm_calls": sum(int(s.get("llm_calls") or 0) for s in steps), "holds": len(out.get("holds") or []),
            "approval": out.get("approval"), "app": dict(app, pid=pid), "storage": saved.get("dir"),
            "preflight": {"may_drive": pre.get("may_drive"), "drive_enabled": pre.get("drive_enabled")},
            "served_by": "guest:replay_saved"}


OPS["replay_draft"] = op_replay_draft     # a ready draft's guest replays (its proof, then the tool)


def op_menu_mark(a):
    """Read one menu item's mark with its WHOLE menu chain open: the top menu, then every submenu on the path opened
    (AXPress on its parent item, as a person hovers it), the item's ``AXMenuItemMarkChar`` read, the menu closed
    (AXCancel). A read (menus opened and closed, nothing chosen); the reference for a submenu flag, whose mark AppKit
    validates only when ITS menu opens."""
    bid, path = a["bundle"], a["path"]
    need_managed(bid)
    pid = activate(bid)
    if not pid:
        return {"status": "done", "ok": False, "detail": "not running"}
    parts = path.split(" > ")
    ti = top_index(pid, parts[0])
    if ti is None or len(parts) < 2:
        return {"status": "done", "ok": False, "detail": f"no top menu {parts[0]!r}"}
    guard_before(pid, f"menu_mark {path!r}")
    bar = menu_bar_items(pid)[ti]
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.7)
    cur, opened, mark_top = bar, [parts[0]], None
    try:
        for k, t in enumerate(parts[1:]):
            menu = next((m for m in (_at(cur, "AXChildren") or []) if role(m) == "AXMenu"), None)
            hit = [x for x in (_at(menu, "AXChildren") or []) if role(x) == "AXMenuItem"
                   and nfc(_s(_at(x, "AXTitle")) or "") == t] if menu is not None else []
            if len(hit) != 1:
                return {"status": "done", "ok": False, "detail": f"{len(hit)} items {t!r} under {opened}"}
            cur = hit[0]
            if k < len(parts) - 2:
                _AS().AXUIElementPerformAction(cur, "AXPress")      # opens the submenu (validates its items)
                time.sleep(0.5)
                opened.append(t)
        mark_top = bool((_s(_at(cur, "AXMenuItemMarkChar")) or "").strip())
    finally:
        _AS().AXUIElementPerformAction(bar, "AXCancel")
        time.sleep(0.3)
        guard_after(pid, "menu_mark")
    return {"status": "done", "ok": True, "path": path, "marked": mark_top, "opened": opened}


OPS["menu_mark"] = op_menu_mark           # a submenu flag's validated mark

# ``open_docs``: this test's own files and folders (under GEN) opened in TextEdit or Finder -- an ``open``, not a
# click -- for the checks that need several windows (two documents of one title; a Finder window per folder).


def _windows_of(pid):
    out = []
    for w in (_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or []) if pid else []:
        out.append({"title": nfc(_s(_at(w, "AXTitle")) or ""), "document": doc_path(w)})
    return out


def op_open_docs(a):
    bid = a.get("bundle") or TEXTEDIT
    if bid not in (TEXTEDIT, FINDER):
        raise ValueError("open_docs opens TextEdit documents or Finder folders only")
    paths = [exp(p) for p in a.get("paths") or []]
    if not paths or not all(ours(p) and os.path.exists(p) for p in paths):
        raise ValueError("not this test's files")
    for i, p in enumerate(paths):
        if bid == FINDER:
            interlock("open a finder window")
            _open_path(p)
            time.sleep(1.5)
        else:
            open_app(TEXTEDIT, doc=p, fresh=bool(a.get("fresh")) and i == 0, wait=2.0)
    pid = activate(bid)
    time.sleep(0.6)
    guard_after(pid, "open_docs")
    return {"status": "done", "opened": paths, "pid": pid, "windows": _windows_of(pid),
            "front_bundle": CS.frontmost_bundle()}


OPS["open_docs"] = op_open_docs

# ``launch_read`` / ``quit_read``: the read checks' setups. An app of this short list is launched (optionally with the
# launch argument ``-AppleLanguages (en)``: nothing is written to the session) or quit; System Settings may be opened
# on one pane by its URL. The reads that follow are ``ax_elements`` / ``read_state`` (read-only).

READ_APPS = (FINDER, NOTES, TEXTEDIT, "com.apple.calculator", "com.apple.systempreferences")
SETTINGS_PANES = {"general": "x-apple.systempreferences:com.apple.systempreferences.GeneralSettings"}


def op_launch_read(a):
    bid = a.get("bundle") or ""
    if bid not in READ_APPS:
        raise ValueError(f"launch_read: {bid!r} is not one of the read setups' apps")
    lang = a.get("lang") or ""
    if lang not in ("", "en", "de"):
        raise ValueError("launch_read: lang is en or de")
    interlock(f"launch {bid}")
    args = ["/usr/bin/open", "-b", bid]
    if a.get("pane"):
        url = SETTINGS_PANES.get(a["pane"])
        if bid != "com.apple.systempreferences" or not url:
            raise ValueError("launch_read: an unknown settings pane")
        args = ["/usr/bin/open", url]
    elif lang:
        args += ["--args", "-AppleLanguages", f"({lang})"]
    subprocess.run(args, capture_output=True, timeout=30)
    t = time.monotonic()
    while time.monotonic() - t < 20 and running(bid) is None:
        time.sleep(0.4)
    time.sleep(float(a.get("wait") or 3.0))
    pid = activate(bid)
    guard_after(pid, "launch_read")
    return {"status": "done" if pid else "failed", "pid": pid, "windows": _windows_of(pid), "lang": lang or None}


def op_quit_read(a):
    bid = a.get("bundle") or ""
    if bid not in READ_APPS:
        raise ValueError(f"quit_read: {bid!r} is not one of the read setups' apps")
    setup = []
    if bid in X.MANAGED:
        quit_app(bid, setup)
    else:
        app = running(bid)
        if app is not None:
            interlock(f"quit {bid}")
            app.terminate()
            t = time.monotonic()
            while time.monotonic() - t < 10 and running(bid) is not None:
                time.sleep(0.3)
            setup.append(f"{bid}: {'quit' if running(bid) is None else 'still running'}")
    return {"status": "done", "setup": setup, "running": running(bid) is not None}


OPS.update({"launch_read": op_launch_read, "quit_read": op_quit_read})

# ``record_te``: a short TextEdit task recorded by the product executor on the document the clean start opened (a
# GEN file, never ~/Documents): the front text area's text replaced by ``text``, then Ablage > Sichern (the file is
# saved in place; no save sheet). Each target is resolved live with the drive harness's fixed rules
# (``interlock_drive_e2e``: no model) and run as a checked step behind the interlock; the answer is the drive
# recording (``workflow.json``'s shape) the host compiles with ``anyuse workflow from-draft``.


def _drive_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "interlock_drive_e2e", os.path.join(os.path.dirname(os.path.abspath(__file__)), "interlock_drive_e2e.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def op_record_te(a):
    D = _drive_module()
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.hil_gate import HILGate
    from anyuse.guides import state_graph as sg
    from anyuse.memory.models import WorkflowIntent, WorkflowTemplate
    doc, text = exp(a.get("doc") or ""), str(a.get("text") or "")
    if not ours(doc) or not text:
        raise ValueError("record_te needs this test's document and a text")
    pid = pid_of(TEXTEDIT)
    w = front_window(pid) if pid else None
    if w is None or doc_path(w) != nfc(doc):
        return {"status": "done", "ok": False, "stopped": "the document is not TextEdit's front window",
                "front": doc_path(w) if w is not None else None}
    guard_before(pid, "record_te")
    tree = app_tree(pid)
    holds = []

    async def approve(ctx):
        holds.append({"intent": (ctx.get("args") or {}).get("intent", ""), "class": ctx.get("step_class")})
        return True
    ex = GuardedCheckedExecutor(StateGraphExecutor(ax_tree=tree, drift_gate_enabled=True,
                                                   hil_gate=HILGate(mode=None, approver=approve)), pid=pid)
    graph = sg.StateGraph()
    wf = WorkflowTemplate(id="vm-checks-textedit", name="TextEdit: replace the open document's text, save",
                          required_app="TextEdit", steps=[])
    plan, results = [], []

    def do(intent, resolve, action, input_data=None, settle=1.2):
        el, d = resolve(tree)
        if el is None:
            results.append({"intent": intent, "success": False, "error": "unresolved"})
            return False
        step = D.step_for(tree, el, d, intent, action, input_data)
        sid = f"s{len(plan)}"
        graph.add_state(D.fingerprint_state(tree, sid, "TextEdit"))
        wf.steps.append(WorkflowIntent(intent=intent, target_element=step.target_element,
                                       expected_before_state=sid, input_type=action, input_data=input_data))
        plan.append(step)
        res, _ver = asyncio.run(ex.execute_checked_step(step, step_idx=len(plan) - 1, workflow=wf,
                                                        state_graph=graph))
        time.sleep(settle)
        results.append({"intent": intent, "success": bool(res.get("success")), "error": res.get("error"),
                        "method": res.get("method"), "llm_calls": int(res.get("llm_calls") or 0)})
        return bool(res.get("success"))

    ok = (do("type the text", D.resolve_front_text_area, "enter_text", input_data=text)
          and do("open the Ablage menu", lambda t: D.resolve_menu_bar_item(t, D.FILE_MENU), "press", settle=0.8)
          and do("save the document", lambda t: D.resolve_menu_item(t, "s"), "press", settle=2.0))
    guard_after(pid, "record_te")
    recording = {"workflow": wf.to_dict(), "plan": [s.to_dict() for s in plan], "graph": graph.to_dict(),
                 "text": text, "name": os.path.basename(doc), "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    return {"status": "done", "ok": ok, "results": results, "holds": holds, "recording": recording,
            "file": file_info(doc)}


OPS["record_te"] = op_record_te

# ``fill_draft``: the agent bridge (``draft_fill.fill_saved``) on a ``needs_fill`` draft HERE, its model on the HOST
# through the drive harness's file relay (``interlock_drive_e2e.RelayModel``: ``req-<n>.json`` / ``resp-<n>.json`` in a
# relay folder under ~/anyuse-guest/relay; the guest holds no key and makes no network call). The fill holds every
# step (the learning phase); this test harness approves each hold and counts it (as run-fill's approver). Every step
# runs behind the interlock on the record's app (one process, never launched). The answer carries the filled record
# (status ready or draft with the proof's reasons) for the host to save.

RELAY_ROOT = "~/anyuse-guest/relay"


def op_fill_draft(a):
    import tempfile
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import draft_fill as DF
    from anyuse.memory.storage import WorkflowStorage
    rec, values, relay = a.get("record"), a.get("values") or {}, str(a.get("relay") or "")
    if not isinstance(rec, dict) or rec.get("status") != "needs_fill" or not isinstance(values, dict):
        return {"status": "refused", "detail": "fill_draft needs a needs_fill draft workflow record and its values"}
    root = exp(RELAY_ROOT)
    rdir = exp(relay)
    if not rdir.startswith(root + os.sep) or os.path.exists(rdir) or "/../" in rdir:
        return {"status": "refused", "detail": "the relay folder is a new folder under ~/anyuse-guest/relay"}
    app = {"bundle_id": str(rec.get("bundle_id") or ""), "name": str(rec.get("app") or "")}
    pid, why = app_pid(app)
    if pid is None:
        return {"status": "done", "ok": False, "stopped": why, "app": app}
    interlock(f"fill draft {str(rec.get('workflow'))[:60]!r}")
    base = exp(REPLAY_STORE)
    os.makedirs(base, exist_ok=True)
    storage = WorkflowStorage(base_dir=tempfile.mkdtemp(prefix="fill-", dir=base))
    saved = DC.save_workflow(json.loads(json.dumps(rec)), storage=storage)
    holds = []

    async def approve(ctx):
        holds.append({"intent": str((ctx.get("args") or {}).get("intent", ""))[:120], "class": ctx.get("step_class")})
        return True
    tree = app_tree(pid)
    no_tree = []
    llm = _drive_module().RelayModel(rdir)
    res = asyncio.run(DF.fill_saved(saved["name"], {str(k): str(v) for k, v in values.items()}, approver=approve,
                                    llm=llm, ax_tree=tree, storage=storage,
                                    executor=GuardedCheckedExecutor(DC.checked_executor(tree), pid=pid),
                                    tree_factory=guest_tree_factory(rec, no_tree),
                                    executor_factory=guest_executor_factory,
                                    show_values=False, free_text=False, sensitive=False))
    _wf, filled = DC.find_saved(saved["name"], storage)
    model = res.get("model") or {}
    return {"status": "done", "ok": bool(res.get("ok")), "stopped": res.get("stopped"),
            "fill_status": res.get("status"), "reasons": res.get("reasons"),
            "proof": {k: (res.get("proof") or {}).get(k) for k in ("passed", "reason", "harness")},
            "model_calls": model.get("calls"), "steps": [_step_brief(s) for s in res.get("steps") or []],
            "holds": len(holds), "record": filled if res.get("ok") else None, "relay": rdir,
            "served_by": "guest:fill_saved"}


OPS["fill_draft"] = op_fill_draft         # the agent bridge's fill of a needs_fill draft, model relayed

# ``drift_probe``: READ-ONLY. The drift gate's verdict for one step of a draft record on the live app, exactly as the
# checked executor computes it before acting (``tree.snapshot()`` -> role/title dicts -> ``evaluate_drift``), with the
# missing elements and the live elements of the required roles: why a replay "drifted".


def op_drift_probe(a):
    from anyuse.execution import drift_gate
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.guides.state_graph import StateGraph
    rec, idx = a.get("record") or {}, int(a.get("step") or 0)
    app = {"bundle_id": str(rec.get("bundle_id") or ""), "name": str(rec.get("app") or "")}
    pid, why = app_pid(app)
    if pid is None:
        return {"status": "done", "ok": False, "detail": why}
    interlock("drift probe read")
    steps = (rec.get("workflow_template") or {}).get("steps") or []
    sid = (steps[idx] if idx < len(steps) else {}).get("expected_before_state") or ""
    graph = StateGraph.from_dict(rec.get("state_graph") or {})
    tree = app_tree(pid)
    snap = tree.snapshot()
    els = StateGraphExecutor._ax_snapshot_to_element_dicts(snap)
    rep = drift_gate.evaluate_drift(graph, sid, els)
    want = graph.get_state(sid).fingerprint.required_elements if sid and graph.get_state(sid) else []
    roles = {str(w.get("role")) for w in want}
    sig = rep.signals or {}
    return {"status": "done", "ok": True, "state": sid, "drifted": rep.drifted, "reason": rep.reason,
            "required_missing": sig.get("required_missing"), "required_found": len(sig.get("required_found") or []),
            "count_in_range": sig.get("count_in_range"), "structural_similarity": sig.get("structural_similarity"),
            "live_count": len(els), "live_of_required_roles": [[e["role"], e["title"]] for e in els
                                                               if e["role"] in roles][:40]}


OPS["drift_probe"] = op_drift_probe       # read-only, why a step drifts

# ── the AppleScript route ────────────────────────────────────────────
# ``scope_read``: per window of ONE app (the front one, or all), the container identity from Apple Events FIRST
# (``applescript_route.ScriptScopeReader``: Finder's ``POSIX path of (target of window as alias)``, Notes' selected
# note's folder with its account and parents), then AX (``container_scope``); next to it the AX-only reading and
# the raw AX sources. READ-ONLY, behind the interlock like every op. Without the
# Automation grant (-1743 / -1744) the answer is the AX reading with ``automation_not_granted``: the permission is
# read with askUserIfNeeded=false, so no op here ever raises the prompt (only the measurement harness
# does). The Apple Events go through execution/guest/applescript_guest.py (the route's constant scripts only); this job
# agent starts no such process itself. Paths and titles go into the answer only, never into a log line.

def guest_scope_reader():
    """The Apple-Events source for ``container_scope`` (``applescript_guest``: the route's constant scripts only;
    frames from the window's own AX position and size)."""
    import applescript_guest as AG
    return AG.scope_reader(frame_of=lambda q: _frame(q["window"]))


def op_scope_read(a):
    """``{"bundle", "window": "front" | "all"}`` -> per window ``scope`` (Apple Events first), ``ax_scope`` (AX
    only), ``script`` (the Apple-Events answer: ok, automation_not_granted, not_running, no_answer), ``frame``,
    ``raw`` (``container_scope.raw_sources``); for Finder also every Finder window as Apple Events list it."""
    interlock("scope_read read")
    bid = str(a.get("bundle") or "")
    need_managed(bid)
    from anyuse.context import container_scope as CSC
    pid = pid_of(bid)
    if not pid:
        return {"status": "done", "ok": False, "detail": "not running", "bundle": bid, "windows": []}
    guard_before(pid, "scope_read")
    tree = app_tree(pid)
    which = "all" if a.get("window") == "all" else "front"
    wins = [w for w in ([front_window(pid)] if which == "front" else _app_windows(pid)) if w is not None]
    reader = guest_scope_reader()
    out = []
    for k, w in enumerate(wins):
        scope, script = CSC.read_scope(tree, w, bid, reader=reader)
        ax_scope, _none = CSC.read_scope(tree, w, bid, reader=None)
        out.append({"index": k, "scope": scope, "ax_scope": ax_scope, "script": script, "frame": _frame(w),
                    "raw": CSC.raw_sources(tree, w, bid)})
    resp = {"status": "done", "ok": True, "bundle": bid, "window": which, "windows": out,
            "interlock": interlock_status()}
    if bid == FINDER:
        resp["finder_windows"], resp["finder_windows_why"] = reader.finder_windows()
    return resp


OPS["scope_read"] = op_scope_read     # the AppleScript route's container read


# ── the Apple-Events container read, WIRED ─────────
# Every container read of the product in this process -- the recorder's selector ``scope``, the resolver's place
# filter, the executor's check right before acting, the row lane's ``scope_refusal`` -- asks Apple Events FIRST:
# ``serve`` plugs the guest's reader into ``container_scope`` once, before any op runs (``plug_scope_reader``). The
# permission is read without asking (no op here raises the prompt); an app not granted answers
# ``automation_not_granted`` and its reads stay AX, as before. ``hello`` reports whether it is plugged in.

SCOPE_READER = {"plugged": False, "why": "not plugged in"}


def plug_scope_reader():
    """``guest_scope_reader()`` plugged into ``container_scope`` for this whole process. A failure leaves every read on
    AX (the behaviour before the wiring) and is reported by ``hello``, never hidden."""
    from anyuse.context import container_scope as CSC
    try:
        reader = guest_scope_reader()
    except Exception as exc:  # noqa: BLE001 - no Apple-Events source: the AX reading, as before
        SCOPE_READER.update(plugged=False, why=type(exc).__name__)
        log("container read: the Apple-Events source is NOT plugged in:", type(exc).__name__)
        return None
    CSC.set_script_reader(reader)
    SCOPE_READER.update(plugged=True, why="")
    log("container read: the Apple-Events source is plugged in (the permission read without asking)")
    return reader


# ``scope_step``: ONE product step on a row of a Finder folder of this test, RECORDED by the product's recorder and
# REPLAYED by the product's checked executor, with whatever container source this process has plugged in (the live
# check of the wiring above, VM job f16a-wire). Only Finder windows that show a folder under GEN (the front window's
# path read; when same-named windows cannot be told apart, every Finder window Apple Events list is under GEN).
#   ``{"mode": "record", "bundle", "label"}`` (a read): the ONE row labelled ``label`` in the front window's content
#     list. ``resolver``: ``AccessibilityTree.to_selector`` of the row's name text (the recorder's selector with the
#     window's container ``scope``); ``resolver_list``: the same for the row's list (Finder names a file with an
#     editable AXTextField, which a selector identifies by its place only); ``row``: the row lane's selector (``ax_actions.row_selector``: the list's
#     identity, the label, the window's scope as this machine's salted key, as the watcher records it).
#   ``{"mode": "replay", "bundle", "lane": "resolver" | "resolver_list" | "row", "selector", "label"}``: one
#     ``execute_checked_step`` behind the interlock: the resolver lanes a ``read`` (the recorded element resolved and its container checked
#     before the read; nothing changes), the row lane a ``select_row`` (the row selected, the list's selection read
#     back). Answers the outcome and its reason code, the front window's live scope (Apple Events first) and
#     ``same_scope`` against the recorded one, and the list's selection after the step.
#   ``"reader": "off"`` (either mode): the product's reads in THIS op are AX only (the source unplugged for the op and
#     plugged back after it): the behaviour before the wiring, for the before/after comparison. The check that the
#     front window shows a folder of this test still reads its path with the plugged source.
# Paths and titles go into the answer only, never into a log line.

SCOPE_STEP_APPS = (FINDER,)
SCOPE_STEP_ACTIONS = {"resolver": "read", "resolver_list": "read", "row": "select_row"}


def _scope_brief(scope):
    return {k: scope.get(k) for k in ("kind", "strength", "source", "key", "title")} if isinstance(scope, dict) else None


def _content_rows(tree, window, label, bid):
    """The rows labelled ``label`` in ``window``'s content lists (not the sidebar), as the product reads a row's
    label (``AccessibilityTree._row_label``) and a list's place (``container_scope.in_container_content``)."""
    from anyuse.context import container_scope as CSC
    boxes, _truncated = tree._row_containers()
    out = []
    for box in boxes:
        if tree.window_of(box) != window:
            continue
        for row in tree._container_rows(box) or []:
            if nfc(tree._row_label(row) or "") == nfc(label) and CSC.in_container_content(tree, row, bid):
                out.append(row)
    return out


def _name_text(tree, row, label):
    """The row's ONE text element showing ``label`` (Finder's name column: an AXStaticText), or None."""
    hits, stack = [], list(tree.children(row))
    while stack:
        el = stack.pop()
        if _s(tree.attr(el, "AXRole")) in ("AXStaticText", "AXTextField") and nfc(_s(tree.attr(el, "AXValue"))
                                                                              or "") == nfc(label):
            hits.append(el)
        stack.extend(tree.children(el))
    return hits[0] if len(hits) == 1 else None


def _ours_by_path(live, reader, tree, win):
    """Does the front window show a folder of this test? Its own path read (strength ``path``), or, when two same-named
    windows cannot be told apart (no answer), EVERY Finder window as Apple Events list it is under GEN and one of them
    has the front window's name."""
    if isinstance(live, dict) and live.get("strength") == "path":
        return ours(live.get("key") or "")
    listing = getattr(reader, "finder_windows", None)
    if listing is None:
        return False
    rows, _why = listing()
    title = nfc(_s(tree.attr(win, "AXTitle")) or "")
    paths = [str(r[2] or "") for r in rows if isinstance(r, (list, tuple)) and len(r) == 3]
    return bool(paths) and all(p.startswith("/") and ours(p) for p in paths) and \
        any(nfc(str(r[0] or "")) == title for r in rows if isinstance(r, (list, tuple)) and len(r) == 3)


def _front_scope(tree, bid, pid, reader):
    from anyuse.context import container_scope as CSC
    w = front_window(pid)
    scope, script = CSC.read_scope(tree, w, bid, reader=reader) if w is not None else (None, None)
    return w, scope, script


def op_scope_step(a):
    interlock("scope_step " + str(a.get("mode") or ""))
    from anyuse.context import container_scope as CSC
    bid, mode, label = str(a.get("bundle") or ""), str(a.get("mode") or ""), nfc(str(a.get("label") or ""))
    need_managed(bid)
    if bid not in SCOPE_STEP_APPS or mode not in ("record", "replay") or not label:
        raise ValueError("scope_step: Finder, mode record or replay, and a row label")
    if a.get("reader") not in (None, "", "on", "off"):
        raise ValueError("scope_step: reader on or off")
    pid = pid_of(bid)
    if not pid:
        return {"status": "done", "ok": False, "detail": "not running", "bundle": bid}
    guard_before(pid, "scope_step")
    tree = app_tree(pid)
    plugged = CSC.script_reader()
    win, live, script = _front_scope(tree, bid, pid, plugged)
    off = a.get("reader") == "off"
    base = {"status": "done", "bundle": bid, "mode": mode, "scope_reader": dict(SCOPE_READER), "reader": "off" if off
            else "on", "front_scope": _scope_brief(live), "front_script": script}
    if win is None or not _ours_by_path(live, plugged, tree, win):
        return dict(base, ok=False, detail="the front window does not show a folder of this test (by its path)")
    if off:
        CSC.set_script_reader(None)
    try:
        return _scope_step(a, base, tree, bid, pid, win, live if not off else _front_scope(tree, bid, pid, None)[1],
                           mode, label)
    finally:
        if off:
            CSC.set_script_reader(plugged)


def _scope_step(a, base, tree, bid, pid, win, live, mode, label):
    from anyuse.context import container_scope as CSC
    from anyuse.execution import ax_actions as AXC
    base = dict(base, product_scope=_scope_brief(live))
    if mode == "record":
        rows = _content_rows(tree, win, label, bid)
        if len(rows) != 1:
            return dict(base, ok=False, detail=f"{len(rows)} rows labelled like that in the front window")
        row = rows[0]
        text = _name_text(tree, row, label)
        resolver = resolver_list = None
        if text is not None:
            sel = tree.to_selector(text)
            resolver = {"selector": sel.to_dict(), "scope": _scope_brief(sel.scope)}
        box = tree._row_container_element(row)
        if box is not None:
            sel = tree.to_selector(box)
            resolver_list = {"selector": sel.to_dict(), "scope": _scope_brief(sel.scope)}
        rscope = AXC.live_scope(tree, row, app=bid)
        row_sel = AXC.row_selector(label, tree.row_container_identity(row),
                                   scope=AXC.scope_key(rscope, create=True))
        return dict(base, ok=True, resolver=resolver, resolver_list=resolver_list,
                    row={"selector": row_sel, "scope": _scope_brief(rscope),
                         "strength": CSC.scope_strength(row_sel.get("scope"))})
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.outcome import classify_outcome
    from anyuse.execution.plan_types import PlannedStep
    lane, sel = str(a.get("lane") or ""), a.get("selector")
    if lane not in SCOPE_STEP_ACTIONS or not isinstance(sel, dict):
        raise ValueError("scope_step replay: lane resolver or row, and the recorded selector")
    recorded = sel.get("scope")
    step = PlannedStep(intent=f"scope_step {lane}", source="workflow", target_element="", modality="ax",
                       command={"role": "AXRow" if lane == "row" else str(sel.get("raw_role") or ""),
                                "title": label, "action": SCOPE_STEP_ACTIONS[lane], "selector": dict(sel)})
    ex = GuardedCheckedExecutor(StateGraphExecutor(ax_tree=tree), pid=pid)
    res, _ver = asyncio.run(ex.execute_checked_step(step))
    time.sleep(0.6)
    guard_after(pid, "scope_step")
    rows = _content_rows(tree, win, label, bid)
    selected = tree.selected_row_labels(rows[0]) if rows else None
    return dict(base, ok=True, lane=lane, action=SCOPE_STEP_ACTIONS[lane], outcome=classify_outcome(res),
                success=bool(res.get("success")), reason_code=res.get("reason_code") or "",
                error=str(res.get("error") or "")[:300], value=res.get("value") if lane != "row" else None,
                recorded_strength=CSC.scope_strength(recorded),
                same_scope=CSC.same_scope(recorded, live) if recorded else None,
                selected_after=selected)


OPS["scope_step"] = op_scope_step     # the wired container read through record and replay


# ``script_step``: ONE recorded step on the AppleScript route's script rung (``applescript_route.try_step``): the
# step mapped to a dictionary shape with object identity (a POSIX path under this test's scratch root, a Notes id
# resolved by name and required unique), run through osa_compile.run_osa's guards (the permission read without
# asking, the drive gate, the disposable root, the human gate with the standing approval below, the interlock), its
# declared effect read back. Answers ``rung``: ``script`` (ran; ``ok`` and the effect), ``ax`` (NOT run here: the
# host runs today's AX step; the reason is recorded, e.g. ``automation_not_granted``) or ``none``. The host carries
# the context (folder entered, note shown, document open) from step to step.

#: the standing approval for the script rung in this test:
#: these action ids, every path argument under the test's scratch root (``GEN``). Nothing else is approved. The ids
#: are the general planner's: ``<bundle>:script_rung/<op>/<the dictionary codes>``.
SCRIPT_RUNG_APPROVED = ("com.apple.finder:script_rung/make/cfol", "com.apple.finder:script_rung/set/brow.fvtg",
                        "com.apple.finder:script_rung/set/capp.sele",
                        "com.apple.TextEdit:script_rung/command/aevtodoc",
                        "com.apple.TextEdit:script_rung/set_save/docu.ctxt",
                        "com.apple.Notes:script_rung/command/noteshow/cfol",
                        "com.apple.Notes:script_rung/command/noteshow/note", "com.apple.Notes:script_rung/make/note")


def route_dictionaries():
    """The rung's dictionaries, read once from the apps' own bundles (``applescript_guest.dictionaries``)."""
    import applescript_guest as AG
    return AG.dictionaries()


def script_rung_caller():
    """The rung's caller (``applescript_guest.rung_caller``): run_osa's guards, the standing approval below."""
    import applescript_guest as AG
    from anyuse.execution.actions import drive_enabled
    return AG.rung_caller(GEN, SCRIPT_RUNG_APPROVED, drive_enabled())


def _front_document():
    bid = CS.frontmost_bundle()
    pid = pid_of(bid) if bid else None
    w = front_window(pid) if pid else None
    return doc_path(w) if w is not None else None


def _read_ours(path):
    """A file's text, only under this test's scratch root (the rung's observer of a save)."""
    if not ours(path) or not os.path.isfile(exp(path)):
        return None
    with open(exp(path), encoding="utf-8") as fh:
        return fh.read()


def _exists_ours(path):
    """Does it exist, only under this test's scratch root (the rung's observer of a new folder)."""
    return ours(path) and os.path.exists(exp(path))


def op_script_step(a):
    """``{"step": {"step", "resolved", "check"?}, "context": {finder_folder, notes_folder, notes_folder_id,
    note_name, note_id, document}}`` -> ``{rung, plan, result, context}``."""
    interlock("script_step")
    from anyuse.execution import applescript_route as AR
    rec = a.get("step") or {}
    ctx = AR.RungContext(**{k: str(v or "") for k, v in (a.get("context") or {}).items()
                            if k in AR.RungContext.__dataclass_fields__})
    bundle = AR.APP_BUNDLES.get(str((rec.get("step") or {}).get("app") or ""), "")
    if bundle:
        need_managed(bundle)
        pid = pid_of(bundle)
        if pid:
            guard_before(pid, "script_step")
    dicts = route_dictionaries()
    plan = AR.plan_step(rec, ctx, dicts)
    res = AR.run_plan(plan, script_rung_caller(), dicts, read_file=_read_ours, front_document=_front_document,
                      exists=_exists_ours)
    AR.advance(ctx, rec, res)
    return {"status": "done", "rung": res.rung, "ok": res.ok, "plan": plan.to_dict(), "result": res.to_dict(),
            "context": dict(ctx.__dict__)}


OPS["script_step"] = op_script_step   # the AppleScript route's script rung


# ``script_intent``: ONE held-out task of the general script rung (Reminders, Calendar, Contacts, Safari, Mail) planned from the app's OWN dictionary
# in the guest (``applescript_guest.dictionaries``), with the host's recorded chooser answers when the task needed one
# (``answers``: slot -> candidate id; an answer that is not one of the guest dictionary's candidates rejects the plan),
# run through run_osa's guards with the standing approval below. Only task ids from the fixed list: the host
# sends no intent, value or script text of its own. Answers ``rung`` as ``script_step`` does.

#: the standing approval for the held-out live run: the action ids the offline held-out plans compiled;
#: every path argument under the test's scratch root. Nothing else.


# ── the server loop ─────────────────────────────────────────────────────────

def op_resolve_system_prompt(a, *, _decision_deadline_monotonic=None):
    """One explicit prompt boundary; policy storage and authority stay here.

    This calls the separate resolver, whose own pre-press checks permit only
    supported choices. Ordinary foreground guards remain unchanged.
    """
    from anyuse.execution import system_prompt_policy as SP
    if type(a) is not dict or set(a) - {"where", "mode", "policy_path", "task_context", "decision_limits"}:
        raise ValueError("resolve_system_prompt accepts only where, mode, guest policy_path, task_context and decision_limits")
    where, mode = a.get("where"), a.get("mode")
    if type(where) is not str or not where or len(where) > 200 or "\0" in where:
        raise ValueError("resolve_system_prompt requires a bounded explicit action boundary")
    if mode not in ("manual", "configured_denials", "rules", "llm", "allow_all"):
        raise ValueError("resolve_system_prompt requires a supported explicit mode")
    task_context = a.get("task_context", "")
    if type(task_context) is not str or "\0" in task_context or \
            len(task_context.encode("utf-8")) > SP.MAX_TASK_CONTEXT_BYTES:
        raise ValueError("resolve_system_prompt requires bounded UTF-8 task_context text")
    limits = a.get("decision_limits")
    if limits is not None:
        if type(limits) is not dict or set(limits) != {"remaining_usd", "remaining_calls", "timeout_s"}:
            raise ValueError("resolve_system_prompt requires exact decision_limits fields")
        dollars, calls, seconds = limits["remaining_usd"], limits["remaining_calls"], limits["timeout_s"]
        if (dollars is not None and (type(dollars) not in (int, float) or not math.isfinite(dollars) or dollars < 0)) or \
                type(calls) is not int or not 0 <= calls <= 100000 or \
                type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
            raise ValueError("resolve_system_prompt decision_limits must be finite bounded data")
    environment = dict(os.environ)
    environment[SP.MODE_ENV] = mode
    if "policy_path" in a:
        path = a["policy_path"]
        if type(path) is not str or not path.startswith("/") or len(path) > 4096 or "\0" in path:
            raise ValueError("resolve_system_prompt policy_path must be an absolute guest path")
        environment[SP.POLICY_ENV] = path
    resolution = SP.resolve_configured("desktop_e2e_job: " + where, environment=environment,
                                       task_context=task_context, decision_limits=limits,
                                       decision_deadline_monotonic=_decision_deadline_monotonic)
    return {"status": "done", "resolution": resolution}


OPS["resolve_system_prompt"] = op_resolve_system_prompt


def handle(op, args, *, _prompt_deadline=None):
    """One command: (answer, stop the server). A refusal of the foreground interlock is a TYPED answer
    (``interlock_refusal``); the server keeps serving (the next command asks again)."""
    STATE["events"], STATE["inputs"], STATE["menu_checked"] = [], 0, []
    stop = False
    try:
        if op == "quit":
            resp, stop = {"status": "done", "detail": "bye"}, True
        elif op not in OPS:
            resp = {"status": "error", "detail": f"unknown op {op!r}"}
        elif op == "resolve_system_prompt":
            resp = OPS[op](args or {}, _decision_deadline_monotonic=_prompt_deadline)
        else:
            resp = OPS[op](args or {})
    except InterlockRefused as exc:
        resp = interlock_refusal(exc.verdict)
    except Prompt as exc:
        resp = {"status": "prompt", "detail": str(exc)}
    except Stop as exc:
        resp, stop = {"status": "stop", "detail": str(exc)}, True
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "error", "detail": f"{type(exc).__name__}: {exc}",
                "tb": traceback.format_exc()[-1500:]}
    if STATE["events"]:
        resp["events"] = STATE["events"]
    if STATE["menu_checked"]:
        # every menu press of this command ran as a checked executor step (``checked_menu_action``): the host sees
        # each one's verdict
        resp["menu_checked"] = STATE["menu_checked"]
    return resp, stop


def _dispatch_prompt_job(message):
    """Apply only the prompt job's read-only clock handshake and expiry."""
    global _PROMPT_CLOCK_ID
    try:
        if set(message) - {"op", "args", "clock_probe", "decision_deadline"}:
            raise ValueError("unexpected prompt-job envelope fields")
        args = message.get("args") or {}
        if type(args) is not dict:
            raise ValueError("prompt-job arguments must be an object")
        if message.get("clock_probe") is True:
            if args != {"where": "transport.clock", "mode": "manual"} or "decision_deadline" in message:
                raise ValueError("clock probe is a fixed read-only prompt operation")
            if _PROMPT_CLOCK_ID is None:
                _PROMPT_CLOCK_ID = uuid.uuid4().hex
            return {"status": "done", "clock": {"server_id": _PROMPT_CLOCK_ID, "monotonic_s": time.monotonic()}}, False
        deadline = None
        if args.get("mode") == "llm" and args.get("decision_limits") is not None:
            bound = message.get("decision_deadline")
            if type(bound) is not dict or set(bound) != {"server_id", "monotonic_s"} or \
                    _PROMPT_CLOCK_ID is None or bound["server_id"] != _PROMPT_CLOCK_ID or \
                    type(bound["monotonic_s"]) not in (int, float) or not math.isfinite(bound["monotonic_s"]):
                raise ValueError("guest prompt deadline has no matching server clock sample")
            deadline = bound["monotonic_s"]
            remaining = deadline - time.monotonic()
            limits = args["decision_limits"]
            if type(limits) is not dict or type(limits.get("timeout_s")) not in (int, float) or \
                    not 0 < remaining <= min(86400.0, limits["timeout_s"]):
                raise ValueError("guest prompt deadline expired or exceeds the caller limit")
            args = dict(args, decision_limits=dict(limits, timeout_s=remaining))
        return handle("resolve_system_prompt", args, _prompt_deadline=deadline)
    except (ValueError, TypeError, KeyError) as exc:
        return {"status": "done", "resolution": {"format": "anyuse.system-prompt-resolution/1", "status": "manual",
                "reason": str(exc), "attempted": False, "reobserve_required": False, "next_action": "stop",
                "decision": {"model_calls": 0, "cost_usd": 0.0, "budget_charged_usd": 0.0, "budget_exhausted": True}}}, False


def _serve_prompt_job(command, cid, message, done_d, resp_d):
    """Claim one prompt job durably before dispatch and preserve its first receipt.

    A duplicate command never dispatches, even when the first process crashed
    after its claim and left no receipt. The client then has an unknown outcome.
    Other operations retain the existing command protocol.
    """
    done, response = os.path.join(done_d, cid + ".json"), os.path.join(resp_d, cid + ".json")
    if os.path.lexists(response) or message.get("op") != "resolve_system_prompt":
        os.unlink(command)
        return False
    try:
        os.link(command, done)
    except FileExistsError:
        os.unlink(command)
        return False
    with open(done, "rb") as claimed:
        os.fsync(claimed.fileno())
    directory = os.open(done_d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    os.unlink(command)
    started = time.monotonic()
    receipt, stop = _dispatch_prompt_job(message)
    receipt.update(id=cid, op="resolve_system_prompt", ms=round((time.monotonic() - started) * 1000))
    temporary = os.path.join(resp_d, "." + cid + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(receipt, output, ensure_ascii=False, default=str)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.link(temporary, response)
    finally:
        os.unlink(temporary)
    return stop


def serve(session, max_min, idle_min):
    global EX
    refuse_unless_in_a_guest()
    from anyuse.context import accessibility as acc
    if not acc.ax_trusted():
        raise SystemExit("REFUSING: Accessibility is not granted")
    install_guard()
    try:
        interlock("release modifier keys")
        from anyuse.context.keyboard import release_modifiers
        release_modifiers()
    except InterlockRefused as exc:
        log("modifier keys not released:", exc)
    except Exception:  # noqa: BLE001
        pass
    G.CANONICAL_FRAME[0] = True
    EX = GuardedExec(G.Exec())
    plug_scope_reader()      # f16a-wire: every container read of this process asks Apple Events first (no-ask)
    cmd_d, resp_d, done_d = (os.path.join(session, d) for d in ("cmd", "resp", "done"))
    for d in (cmd_d, resp_d, done_d):
        os.makedirs(d, exist_ok=True)
    last = time.monotonic()
    log("serving", session)
    while True:
        if time.monotonic() - T0 > max_min * 60:
            log("max lifetime reached")
            return 0
        if time.monotonic() - last > idle_min * 60:
            log("idle timeout")
            return 0
        files = sorted(glob.glob(os.path.join(cmd_d, "*.json")))
        if not files:
            time.sleep(0.15)
            continue
        f = files[0]
        cid = os.path.basename(f)[:-5]
        try:
            msg = json.load(open(f, encoding="utf-8"))
        except ValueError:
            time.sleep(0.2)
            continue
        if msg.get("op") == "resolve_system_prompt" or re.fullmatch(r"p[0-9a-f]{32}", cid):
            last = time.monotonic()
            if _serve_prompt_job(f, cid, msg, done_d, resp_d):
                return 3
            continue
        os.replace(f, os.path.join(done_d, os.path.basename(f)))
        last = time.monotonic()
        op = msg.get("op")
        t = time.monotonic()
        resp, stop = handle(op, msg.get("args") or {})
        resp["id"], resp["op"], resp["ms"] = cid, op, round((time.monotonic() - t) * 1000)
        tmp = os.path.join(resp_d, f".{cid}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(resp, fh, ensure_ascii=False, default=str)
        os.replace(tmp, os.path.join(resp_d, f"{cid}.json"))
        log(cid, op, resp.get("status"), resp["ms"], "ms", str(resp.get("detail") or "")[:120])
        if stop:
            return 3 if resp["status"] == "stop" else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--session", required=True)
    s.add_argument("--max-min", type=float, default=240)
    s.add_argument("--idle-min", type=float, default=15)
    a = ap.parse_args()
    return serve(os.path.expanduser(a.session), a.max_min, a.idle_min)


if __name__ == "__main__":
    sys.exit(main())
