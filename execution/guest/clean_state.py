#!/usr/bin/env python3
"""Guest-side observer for the declared clean start state. INSIDE THE GUEST VM ONLY.

``execution/desktop_hygiene.py`` decides; this module only READS the guest's
state for it: the frontmost app, its front window's title, what is selected,
the app's sheets/dialogs, windows of other processes in front of it, and the
disposable folder's listing. It never clicks, never deselects, never closes a
window: a dirty start state is refused, not tidied through the UI.

Every reader returns ``None`` when it cannot read (the hygiene check then
refuses: an unreadable state is never assumed clean).
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

import desktop_gen as G                        # noqa: E402  (guest-side drivers)

SELECTION_ROLES = ("AXOutline", "AXTable", "AXList", "AXBrowser", "AXGroup", "AXScrollArea")


def frontmost_bundle() -> Optional[str]:
    """The focused application, read LIVE through the system-wide AX element.
    ``NSWorkspace.frontmostApplication`` is refreshed only by a run loop,
    which a guest job does not spin, so its value can be stale; it is only the fallback."""
    try:
        import AppKit
        sw = G._AS().AXUIElementCreateSystemWide()
        app = G._at(sw, "AXFocusedApplication")
        if app is not None:
            err, pid = G._AS().AXUIElementGetPid(app, None)
            if not err:
                a = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
                if a is not None:
                    return str(a.bundleIdentifier())
        a = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(a.bundleIdentifier()) if a is not None else None
    except Exception:  # noqa: BLE001
        return None


def window_title(pid) -> Optional[str]:
    if not pid:
        return None
    w = G.front_window(pid)
    return (G._s(G._at(w, "AXTitle")) or "") if w is not None else None


def _ax_selection(pid) -> Optional[List[str]]:
    """Selected rows/children of every container in the front window, and a
    non-empty selected text range in a text area/field."""
    w = G.front_window(pid) if pid else None
    if w is None:
        return None
    out: List[str] = []
    n = [0]

    def walk(el, depth):
        if depth > 14 or n[0] > 4000:
            return
        n[0] += 1
        role = G._s(G._at(el, "AXRole")) or ""
        if role in SELECTION_ROLES:
            for attr in ("AXSelectedRows", "AXSelectedChildren", "AXSelectedCells"):
                for k in G._at(el, attr) or []:
                    out.append(G._label(k) or role)
        if role in G.TEXT_ROLES:
            txt = G._s(G._at(el, "AXSelectedText")) or ""
            if txt:
                out.append(f"text selection: {txt[:40]!r}")
        for k in G._at(el, "AXChildren") or []:
            walk(k, depth + 1)
    walk(w, 0)
    return out


def _finder_selection() -> Optional[List[str]]:
    """Finder's own answer (its scripting model), independent of the view.
    An APPLE EVENT: it needs the Automation grant, which the guest runner does
    not have (the consent prompt it would raise is never answered). Only used
    with ``selection(..., apple_events=True)``; nothing in the desktop rounds
    passes that."""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e",
                            'tell application "Finder" to get name of every item of '
                            '(get selection)'], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    txt = r.stdout.strip()
    return [s.strip() for s in txt.split(",") if s.strip()] if txt else []


def selection(bid: str, pid, apple_events: bool = False) -> Optional[List[str]]:
    """What is selected: the accessibility tree only, unless the caller
    explicitly allows an Apple event to Finder (no job does: osa stays
    out of scope)."""
    ax = _ax_selection(pid)
    if bid == "com.apple.finder" and apple_events:
        fs = _finder_selection()
        if fs is None or ax is None:
            return None
        return sorted(set(ax) | set(fs))
    return ax


def foreign_windows(pid) -> Optional[List[Dict[str, Any]]]:
    """On-screen windows of OTHER processes in front of the app's front window
    (the window list is front to back), plus any other process's window above
    the normal layer (an authorization prompt, an alert)."""
    try:
        import Quartz
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    out: List[Dict[str, Any]] = []
    seen_app = False
    for w in wins:
        owner_pid = int(w.get("kCGWindowOwnerPID") or 0)
        layer = int(w.get("kCGWindowLayer") or 0)
        owner = str(w.get("kCGWindowOwnerName") or "")
        if owner_pid == int(pid or -1):
            if layer == 0:
                seen_app = True
            continue
        if layer < 0 or (layer >= 20 and layer != 1000 and owner != "SecurityAgent"):
            continue                    # menu bar, dock and other session chrome layers
        if not seen_app or layer > 0:
            out.append({"owner": owner, "title": str(w.get("kCGWindowName") or ""),
                        "layer": layer})
    return out


def folder_listing(folder: str) -> Optional[List[str]]:
    try:
        return sorted(os.listdir(folder))
    except OSError:
        return None


def observe(bid: str, pid, folder: str = "", dialogs_fn=None) -> Dict[str, Any]:
    return {"bundle_id": frontmost_bundle(), "front_window": window_title(pid),
            "selection": selection(bid, pid),
            "dialogs": (dialogs_fn(pid) if dialogs_fn is not None else []),
            "foreign_windows": foreign_windows(pid),
            "folder_listing": folder_listing(folder) if folder else []}
