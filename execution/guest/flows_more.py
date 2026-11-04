#!/usr/bin/env python3
"""Flows 3-6, 8-10 and the HIL flow for `flows.py` — INSIDE THE GUEST ONLY.

Same frame as flows.py (record through `to_selector`, replay through
`find_selector` + `evaluate_drift` + `perform_action`/`enter_text`, verify
without the UI). Kept in a second module only so each file can still be read in
one sitting. Every structure below was taken from AX dumps captured in the
guest, not remembered.

Postconditions, none of which consult the UI:
  3  Finder create folder      os.path.isdir
  4  Finder search             the reported names == os.listdir filtered
  5  Notes create              a row in NoteStore.sqlite (ZTITLE1 / ZSNIPPET)
  6  Notes search              reported titles == the sqlite rows that match
  8  System Settings read      the value == sysctl / sw_vers / ioreg
  9  Safari list               the AXLink titles == the fixture the page was built from
  10 Safari detail             three fields == the fixture record
  H  HIL delete                the file still exists; the gate's decision is recorded
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request

from flows import (App, Recorder, Replayer, _AS, _attr, _s, all_refs, make_state_graph,
                   menu_bar_item, menu_item_by_cmd, subtree, textedit_area_in_front_window, TE)

FINDER = "com.apple.finder"
NOTES = "com.apple.Notes"
SETTINGS = "com.apple.systempreferences"
SAFARI = "com.apple.Safari"
NOTES_DB = os.path.expanduser("~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite")

RETURN, ESCAPE = 36, 53


def _kb():
    from anyuse.context import keyboard as kb
    return kb


def _quartz():
    import Quartz
    return Quartz


def close_front_window(tree):
    win = _attr(tree._root(), "AXFocusedWindow")
    btn = _attr(win, "AXCloseButton") if win is not None else None
    if btn is not None:
        _AS().AXUIElementPerformAction(btn, "AXPress")
        time.sleep(0.6)
        return True
    return False


def press_menu_by_cmd(tree, menu_titles, cmd, mods=("", "0")):
    """Open a menu-bar menu and press the item with this ⌘-character. NOT a
    recorded step — used to apply a perturbation (hide sidebar/toolbar)."""
    m, _ = menu_bar_item(tree, menu_titles)
    if m is None:
        return False
    _AS().AXUIElementPerformAction(m, "AXPress")
    time.sleep(0.8)
    it, _ = menu_item_by_cmd(tree, cmd, mods)
    if it is None:
        _AS().AXUIElementPerformAction(m, "AXCancel")
        return False
    _AS().AXUIElementPerformAction(it, "AXPress")
    time.sleep(0.8)
    return True


def press_menu_by_title(tree, menu_titles, item_titles):
    m, _ = menu_bar_item(tree, menu_titles)
    if m is None:
        return False
    _AS().AXUIElementPerformAction(m, "AXPress")
    time.sleep(0.8)
    for el, d in all_refs(tree):
        if d.role == "AXMenuItem" and (d.title or "") in item_titles:
            _AS().AXUIElementPerformAction(el, "AXPress")
            time.sleep(0.8)
            return True
    _AS().AXUIElementPerformAction(m, "AXCancel")
    return False


def apply_perturbation(tree, perturb, app):
    """Two real perturbations, plus the grid entries that make sense per
    app. Shortcuts as the guest's menus report them: Finder/Notes sidebar
    ⌃⌘S (modifier code '4'), Safari sidebar ⇧⌘L ('1'), Finder/TextEdit
    toolbar ⌥⌘T ('2'); Notes' toolbar item has no shortcut and is pressed by
    title. Returns a note."""
    if perturb == "sidebar":
        if app in ("Finder", "Notes"):
            return f"sidebar hidden: {press_menu_by_cmd(tree, ('View', 'Darstellung'), 's', ('4',))}"
        if app == "Safari":
            return f"sidebar shown: {press_menu_by_cmd(tree, ('View', 'Darstellung'), 'l', ('1',))}"
    if perturb == "toolbar":
        if app in ("Finder", "TextEdit"):
            return f"toolbar hidden: {press_menu_by_cmd(tree, ('View', 'Darstellung'), 't', ('2',))}"
        if app == "Notes":
            return f"toolbar hidden: {press_menu_by_title(tree, ('View', 'Darstellung'), ('Symbolleiste ausblenden', 'Hide Toolbar'))}"
    return "none"


def by_identifier(tree, ident, role=None):
    for el, d in all_refs(tree):
        if (d.identifier or "") == ident and (role is None or d.role == role):
            return el, d
    return None, None


def by_desc(tree, role, desc):
    for el, d in all_refs(tree):
        if d.role == role and (d.ax_description == desc or d.title == desc):
            return el, d
    return None, None


def rename_field(tree):
    """Finder's in-place rename field after New Folder. It can be the
    AXTextField with identifier 'ShrinkToFit Text Field' at the application
    level; after a Finder relaunch it may not be there under that
    identifier. Take the identifier if present, else the
    text field that holds keyboard focus, else any text field in the front
    window whose value starts with the default folder name."""
    el, d = by_identifier(tree, "ShrinkToFit Text Field", "AXTextField")
    if el is not None:
        return el, d
    focused = _attr(tree._root(), "AXFocusedUIElement")
    if focused is not None and _s(_attr(focused, "AXRole")) == "AXTextField":
        return focused, tree.describe_element(focused)
    front = _attr(tree._root(), "AXFocusedWindow")
    for el, d in (subtree(tree, front) if front is not None else []):
        if d.role == "AXTextField" and (d.value or "").startswith(("Neuer Ordner", "untitled folder")):
            return el, d
    return None, None


def ensure_finder_toolbar(tree):
    """Finder keeps its window state across launches: an earlier job's
    `toolbar` condition can leave the toolbar hidden and flow 4 without a
    search button. If the toolbar is missing, toggle it back (⌥⌘T) once."""
    if by_desc(tree, "AXButton", "Suchen")[0] is not None:
        return "toolbar present"
    ok = press_menu_by_cmd(tree, ("View", "Darstellung"), "t", ("2",))
    time.sleep(0.8)
    return f"toolbar restored ok={ok}, search button now {by_desc(tree, 'AXButton', 'Suchen')[0] is not None}"


# ── flow 3: Finder create folder ────────────────────────────────────────────

def flow3_finder_create_folder(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    kb = _kb()
    folder = os.path.join(scratch, "finder")
    os.makedirs(folder, exist_ok=True)
    app = App(FINDER, acc)
    app.launch(folder)
    app.activate()
    tree = app.tree()
    toolbar_note = ensure_finder_toolbar(tree)
    t0 = time.monotonic()
    rec = Recorder(tree)
    fm, fd = menu_bar_item(tree, ("File", "Ablage"))
    rec.add("file_menu", fm, fd)
    _AS().AXUIElementPerformAction(fm, "AXPress"); time.sleep(0.9)
    nf, nd = menu_item_by_cmd(tree, "n", ("1",))          # ⇧⌘N 'Neuer Ordner'
    rec.add("new_folder", nf, nd)
    _AS().AXUIElementPerformAction(nf, "AXPress"); time.sleep(1.3)
    field, fdsc = rename_field(tree)
    if field is None:
        return {"flow": "3 finder_create_folder", "error": f"no rename field after New Folder ({toolbar_note}; "
                f"front window rows: {window_rows(tree, 40)})"}
    rec.add("name_field", field, fdsc)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Finder", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    ev = tree.enter_text(field, "papaya_rec")
    kb.press_key(RETURN); time.sleep(0.9)
    recorded_ok = os.path.isdir(os.path.join(folder, "papaya_rec"))
    note = apply_perturbation(tree, perturb, "Finder")

    results = []
    for name in fresh_inputs:
        for i in range(runs):
            target = f"{name}_{i}"
            before = set(os.listdir(folder))
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            rp.press("file_menu", 1.0)
            rp.press("new_folder", 1.3)
            field = rp.resolve("name_field")
            if field is None:
                # the recorded identity may be gone (see rename_field); the focused
                # rename field is the same control — counted as a healed resolution
                fel, fdsc2 = rename_field(tree)
                if fel is not None and tree.holds_focus(fel):
                    field = fel
                    rp.steps.append({"step": "name_field", "healed_by_focus": True})
            entry = tree.enter_text(field, target) if field is not None else None
            if field is not None:
                kb.press_key(RETURN)
            time.sleep(0.9)
            after = set(os.listdir(folder))
            created = sorted(after - before)
            ok = os.path.isdir(os.path.join(folder, target))
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": name, "run": i, "success": ok, "created": created,
                 "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            results.append(r)
            print(f"  flow3 {target}: {'OK' if ok else 'FAIL'} created={created} rung={entry.rung if entry is not None else '-'} "
                  f"wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    close_front_window(tree)
    return {"flow": "3 finder_create_folder", "app": "Finder", "rung": "ax", "recorded_input": "papaya_rec",
            "recorded_ok": recorded_ok, "toolbar": toolbar_note, "rename_field": rec.steps["name_field"]["identity"],
            "record_ms": round(record_ms, 1), "recorded_steps": rec.steps,
            "perturbation": perturb, "perturbation_note": note, "runs": results}


# ── flow 3b: Finder RENAME (directory_op's other subtype) ──────────────────
#
# Not one of the ten fixed flows: added for
# execution/desktop_compile.py, whose directory_op class has two subtypes
# (create, rename) and only "create" had a recorded flow. Uses Finder's
# OTHER standard rename trigger — select an existing item, press Return — so
# the recording carries no AXMenuItem and no AXButton at all, just the same
# inline "ShrinkToFit Text Field" flow 3 already established; this is
# exactly the "bare text-field-only" shape
# execution/desktop_compile.py:infer_class documents for directory_op/rename.

def select_and_enter_rename(tree, kb, name):
    """Select the folder icon identified by ``name`` (an AXGroup, the same
    pattern flow_hil_delete uses to select a file) and press Return, which is
    Finder's keystroke-only rename trigger. Returns the rename field (or
    ``(None, None)`` if the icon or the field could not be found)."""
    grp, _gd = by_identifier(tree, name, "AXGroup")
    if grp is None:
        return None, None
    img = next((k for k in (_attr(grp, "AXChildren") or []) if _s(_attr(k, "AXRole")) == "AXImage"), grp)
    _AS().AXUIElementPerformAction(img, "AXPress"); time.sleep(0.6)
    _AS().AXUIElementSetAttributeValue(grp, "AXSelected", True); time.sleep(0.3)
    kb.press_key(RETURN); time.sleep(0.9)
    return rename_field(tree)


def flow3b_finder_rename(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    kb = _kb()
    folder = os.path.join(scratch, "finder")
    os.makedirs(folder, exist_ok=True)
    app = App(FINDER, acc)
    app.launch(folder)
    app.activate()
    tree = app.tree()
    toolbar_note = ensure_finder_toolbar(tree)

    rec_src = "rename_rec_seed"
    os.makedirs(os.path.join(folder, rec_src), exist_ok=True)
    t0 = time.monotonic()
    rec = Recorder(tree)
    field, fdsc = select_and_enter_rename(tree, kb, rec_src)
    if field is None:
        return {"flow": "3b finder_rename", "error": f"no rename field after select+Return ({toolbar_note}; "
                f"front window rows: {window_rows(tree, 40)})"}
    rec.add("name_field", field, fdsc)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Finder", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    tree.enter_text(field, "rename_rec_done")
    kb.press_key(RETURN); time.sleep(0.9)
    recorded_ok = (os.path.isdir(os.path.join(folder, "rename_rec_done"))
                  and not os.path.exists(os.path.join(folder, rec_src)))
    note = apply_perturbation(tree, perturb, "Finder")

    results = []
    for name in fresh_inputs:
        for i in range(runs):
            src, dst = f"{name}_src_{i}", f"{name}_dst_{i}"
            os.makedirs(os.path.join(folder, src), exist_ok=True)
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            live_field, _fd = select_and_enter_rename(tree, kb, src)
            # The recorded selector for THIS run's field (a fresh instance
            # each time a rename starts) — resolved and identity-checked,
            # exactly like every other flow's replay; the freshly-found field
            # from select_and_enter_rename is the input needed to locate the
            # icon at all, but typing goes through the checked resolution.
            resolved = rp.resolve("name_field") if live_field is not None else None
            target = resolved if resolved is not None else live_field
            entry = None
            if target is not None:
                entry = tree.enter_text(target, dst)
                kb.press_key(RETURN)
            time.sleep(0.9)
            ok = os.path.isdir(os.path.join(folder, dst)) and not os.path.exists(os.path.join(folder, src))
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": name, "run": i, "success": ok, "src": src, "dst": dst,
                 "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            results.append(r)
            print(f"  flow3b {src}->{dst}: {'OK' if ok else 'FAIL'} wrong_target={r['wrong_target']} "
                  f"gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    close_front_window(tree)
    return {"flow": "3b finder_rename", "app": "Finder", "rung": "ax", "recorded_input": rec_src,
            "recorded_ok": recorded_ok, "toolbar": toolbar_note,
            "record_ms": round(record_ms, 1), "recorded_steps": rec.steps,
            "perturbation": perturb, "perturbation_note": note, "runs": results}


# ── flow 4: Finder search ───────────────────────────────────────────────────

SEED_FILES = ["apple_pie.txt", "banana_split.txt", "cherry_tart.md", "blueberry_pie.txt",
              "date_square.md", "elderberry_tart.txt", "fig_roll.txt"]


def finder_result_names(tree):
    """File names shown in the FRONT WINDOW's content view — walked from the
    window itself, skipping the sidebar (AXOutline) and the toolbar, so the
    Desktop's own icon view (outside every window, e.g. 'AnyUse
    Runner.command') cannot leak in. Icon view: AXImage titles. List/search view: the
    first AXStaticText of each AXRow."""
    front = _attr(tree._root(), "AXFocusedWindow")
    if front is None:
        return []
    names, seen_rows = [], set()
    for el, d in subtree(tree, front):
        if d.role == "AXImage" and d.title:
            names.append(d.title)
        elif d.role == "AXRow":
            for kel, kd in subtree(tree, el):
                if kd.role == "AXStaticText" and (kd.value or "").strip():
                    names.append(kd.value.strip())
                    break
    return sorted(set(names))


def window_rows(tree, limit=120):
    """A compact dump of the front window's content, for a failed run."""
    front = _attr(tree._root(), "AXFocusedWindow")
    out = []
    for el, d in (subtree(tree, front) if front is not None else []):
        out.append((d.role, d.title, d.identifier, (d.value or "")[:40], tree._ax_path(el)[-60:]))
        if len(out) >= limit:
            break
    return out


def finder_search_entry(tree, field, pattern, kb, listing):
    """Put a pattern into Finder's search field so that Finder actually
    SEARCHES. An AXValue write reads back perfectly and Finder ignores it (the
    first run of this flow: every file still listed) — the search is driven by
    the key events a user produces. The ladder's independent check here is
    'the view no longer shows the whole folder'; the flow's postcondition is
    still the listing on disk."""
    def verify():
        kb.press_key(RETURN)
        time.sleep(3.0)
        return set(finder_result_names(tree)) != set(listing)
    return tree.enter_text(field, pattern, verify=verify, rungs=("ax_value", "keystrokes"))


def flow4_finder_search(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    kb = _kb()
    folder = os.path.join(scratch, "finder")
    os.makedirs(folder, exist_ok=True)
    for n in SEED_FILES:
        p = os.path.join(folder, n)
        if not os.path.exists(p):
            open(p, "w").write(n + "\n")
    subprocess.run(["/usr/bin/mdimport", folder], capture_output=True, timeout=60)
    time.sleep(2.0)
    app = App(FINDER, acc)
    app.launch(folder)
    app.activate()
    tree = app.tree()
    toolbar_note = ensure_finder_toolbar(tree)
    t0 = time.monotonic()
    rec = Recorder(tree)
    sb, sd = by_desc(tree, "AXButton", "Suchen")
    if sb is None:
        return {"flow": "4 finder_search", "error": f"no search button ({toolbar_note})"}
    rec.add("search_button", sb, sd)
    _AS().AXUIElementPerformAction(sb, "AXPress"); time.sleep(1.0)
    field, fdsc = next(((el, d) for el, d in all_refs(tree)
                        if d.role == "AXTextField" and "toolbar" in tree._ax_path(el)), (None, None))
    if field is None:
        return {"flow": "4 finder_search", "error": "no search field after pressing Suchen"}
    rec.add("search_field", field, fdsc)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Finder", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    listing = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    finder_search_entry(tree, field, "pie", kb, listing)
    rec_names = finder_result_names(tree)
    recorded_ok = set(rec_names) == {n for n in os.listdir(folder) if "pie" in n.lower()}
    kb.press_key(ESCAPE); time.sleep(0.6)
    note = apply_perturbation(tree, perturb, "Finder")

    results = []
    for pattern in fresh_inputs:
        for i in range(runs):
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            rp.press("search_button", 1.0)
            field = rp.resolve("search_field")
            reported, entry = None, None
            listing = sorted(n for n in os.listdir(folder) if not n.startswith("."))
            if field is not None:
                entry = finder_search_entry(tree, field, pattern, kb, listing)
                reported = finder_result_names(tree)
            expected = sorted(n for n in listing if pattern.lower() in n.lower())
            kb.press_key(ESCAPE); time.sleep(0.6)
            ok = reported is not None and set(reported) == set(expected)
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": pattern, "run": i, "success": ok, "reported": reported, "expected": expected,
                 "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            if not ok and not any("debug_rows" in x for x in results):
                r["debug_rows"] = window_rows(tree)     # once per flow, for the next fix
            results.append(r)
            print(f"  flow4 {pattern}{i}: {'OK' if ok else 'FAIL'} reported={reported} expected={expected} "
                  f"wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    close_front_window(tree)
    return {"flow": "4 finder_search", "app": "Finder", "rung": "ax", "recorded_input": "pie",
            "recorded_ok": recorded_ok, "recorded_names": rec_names, "toolbar": toolbar_note,
            "record_ms": round(record_ms, 1),
            "recorded_steps": rec.steps, "perturbation": perturb, "perturbation_note": note, "runs": results}


# ── flow 5 / 6: Notes ───────────────────────────────────────────────────────

LAST_DB_ERROR = ""


def notes_rows():
    """(title, snippet) of every note, read from a COPY of Notes' own store
    (main file + -wal + -shm), so a lock held by the running app cannot turn
    into an empty answer. Errors are kept in LAST_DB_ERROR and reported."""
    global LAST_DB_ERROR
    import shutil, tempfile
    try:
        tmp = tempfile.mkdtemp(prefix="notes-ro-")
        for suffix in ("", "-wal", "-shm"):
            src = NOTES_DB + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "NoteStore.sqlite" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "NoteStore.sqlite"), timeout=5)
        rows = con.execute("select ZTITLE1, ZSNIPPET from ZICCLOUDSYNCINGOBJECT "
                           "where ZTITLE1 is not null and ZMARKEDFORDELETION is not 1").fetchall()
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
        LAST_DB_ERROR = ""
        return [(str(t or ""), str(s or "")) for t, s in rows]
    except Exception as exc:  # noqa: BLE001
        LAST_DB_ERROR = f"{type(exc).__name__}: {exc}"
        return []


def note_in_db(title, line, timeout=10.0):
    """True once Notes' own store holds the note. Notes writes NoteStore.sqlite
    lazily — every note appeared in the store, but only AFTER the
    8-s-per-rung window had passed (all 15 runs 'failed' with the text plainly
    in the UI and later in the DB). Returns (found, seconds_waited)."""
    t0 = time.monotonic()
    while True:
        rows = notes_rows()
        hit = [r for r in rows if r[0] == title and line in r[1]]
        if hit or time.monotonic() - t0 > timeout:
            return bool(hit), round(time.monotonic() - t0, 1)
        time.sleep(1.0)


def dismiss_welcome(tree):
    b, _ = by_desc(tree, "AXButton", "Fortfahren")
    if b is not None:
        _AS().AXUIElementPerformAction(b, "AXPress"); time.sleep(1.2)
        return True
    return False


NOTE_TITLES = {"rec": "Rezept Sauerteig", "quince": "Einkauf Quitten", "harz": "Wanderung Harz",
               "lamp": "Lampe reparieren",
               # osa_command proof inputs
               "mispel": "Einkauf Mispeln", "holunder": "Garten Holunder"}


def flow5_notes_create(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    app = App(NOTES, acc)
    app.quit(); app.launch(wait=5.0); app.activate()
    tree = app.tree()
    dismiss_welcome(tree)
    t0 = time.monotonic()
    rec = Recorder(tree)
    fm, fd = menu_bar_item(tree, ("File", "Ablage"))
    rec.add("file_menu", fm, fd)
    _AS().AXUIElementPerformAction(fm, "AXPress"); time.sleep(0.9)
    nn, nd = menu_item_by_cmd(tree, "n", ("", "0"))        # ⌘N 'Neue Notiz'
    rec.add("new_note", nn, nd)
    _AS().AXUIElementPerformAction(nn, "AXPress"); time.sleep(1.5)
    body, bd = by_identifier(tree, "Note Body Text View", "AXTextArea")
    if body is None:
        return {"flow": "5 notes_create", "error": "no note body text view"}
    rec.add("body", body, bd)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Notes", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    title, line = NOTE_TITLES["rec"], "anyuse desktop validation rec 0"
    ev = tree.enter_text(body, f"{title}\n{line}")
    recorded_ok, rec_db_s = bool(ev and ev.rungs and ev.rungs[-1].readback_matches), None   # store: checked at the end
    note = apply_perturbation(tree, perturb, "Notes")

    results = []
    for key in fresh_inputs:
        for i in range(runs):
            title = f"{NOTE_TITLES[key]} {i}"
            line = f"anyuse desktop validation {key} {i}"
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            rp.press("file_menu", 1.0)
            rp.press("new_note", 1.5)
            body = rp.resolve("body")
            entry = None
            if body is not None:
                entry = tree.enter_text(body, f"{title}\n{line}")
            elapsed = (time.monotonic() - t_start) * 1000.0     # the UI part only
            # UI-side evidence now; the store — the independent accessor — is
            # read ONCE at the end of the flow, after a graceful quit (see below).
            r = {"input": key, "run": i, "success": None, "title": title, "line": line,
                 "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            results.append(r)
            print(f"  flow5 {key}{i}: entered rung={entry.rung if entry is not None else '-'} "
                  f"readback={bool(entry and entry.rungs and entry.rungs[-1].readback_matches)} "
                  f"wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    # Notes persists NoteStore.sqlite lazily — not within 45 s of typing,
    # not within 20 s of a quit, yet every note was in
    # the store later. So: quit, then poll the store for ALL of this flow's
    # notes for up to 4 minutes, and report each note's presence and the lag.
    # The postcondition is still the store, only read later.
    app.quit()
    t_q = time.monotonic()
    pending = {(r["title"], r["line"]) for r in results}
    lag = {}
    while pending and time.monotonic() - t_q < 240.0:
        rows = notes_rows()
        for t, l in list(pending):
            if any(rt == t and l in rs for rt, rs in rows):
                lag[(t, l)] = round(time.monotonic() - t_q, 1)
                pending.discard((t, l))
        if pending:
            time.sleep(2.0)
    for r in results:
        key_ = (r["title"], r["line"])
        r["success"] = key_ in lag
        r["store_lag_after_quit_s"] = lag.get(key_)
        print(f"  flow5 {r['input']}{r['run']}: {'OK' if r['success'] else 'FAIL'} store lag after quit "
              f"{r['store_lag_after_quit_s']} s  (db_error={LAST_DB_ERROR!r})")
    app.launch(wait=4.0); app.activate(); dismiss_welcome(app.tree())
    return {"flow": "5 notes_create", "app": "Notes", "rung": "ax", "recorded_input": NOTE_TITLES["rec"],
            "recorded_ok": recorded_ok, "recorded_store_lag_s": rec_db_s, "record_ms": round(record_ms, 1),
            "recorded_steps": rec.steps, "perturbation": perturb, "perturbation_note": note, "runs": results}


def notes_listed_texts(tree):
    """Every static text inside the rows of the front window's note list.
    A search result list has section header rows ('Toptreffer', 'Notizen') and
    shows the matched SNIPPET line before the title, so 'the first
    text of a row' is not the title; the caller intersects with the store."""
    front = _attr(tree._root(), "AXFocusedWindow")
    out = []
    if front is None:
        return out
    for el, d in subtree(tree, front):
        if d.role != "AXTable":
            continue
        for rel, rd in subtree(tree, el):
            if rd.role == "AXRow":
                for tel, td in subtree(tree, rel):
                    if td.role == "AXStaticText" and (td.value or "").strip():
                        out.append(td.value.strip())
    return out


def notes_listed_titles(tree, known_titles=None):
    """The note TITLES shown in the list: the row texts that are titles the
    store knows (so section headers and snippet lines cannot count)."""
    texts = notes_listed_texts(tree)
    if known_titles is None:
        known_titles = {t for t, _s in notes_rows()}
    return sorted({t for t in texts if t in known_titles})


def flow6_notes_search(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    kb = _kb()
    app = App(NOTES, acc)
    if app.pid() is None:
        app.launch(wait=5.0)
    app.activate()
    tree = app.tree()
    dismiss_welcome(tree)
    t0 = time.monotonic()
    rec = Recorder(tree)
    sb, sd = by_desc(tree, "AXButton", "Suchen")
    rec.add("search_button", sb, sd)
    _AS().AXUIElementPerformAction(sb, "AXPress"); time.sleep(1.0)
    field, fdsc = next(((el, d) for el, d in all_refs(tree)
                        if d.role in ("AXTextField", "AXSearchField") and "toolbar" in tree._ax_path(el)),
                       (None, None))
    if field is None:
        return {"flow": "6 notes_search", "error": "no search field after pressing Suchen"}
    rec.add("search_field", field, fdsc)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Notes", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    store = notes_rows()
    known = {t for t, _s in store}

    def db_matches(term):
        return sorted({t for t, s in store if term.lower() in t.lower() or term.lower() in s.lower()})

    def notes_search_entry(field, term):
        # the independent accessor IS the store: the list must show what it holds
        def verify():
            kb.press_key(RETURN)
            time.sleep(1.8)
            return set(notes_listed_titles(tree, known)) == set(db_matches(term))
        return tree.enter_text(field, term, verify=verify, rungs=("ax_value", "keystrokes"))
    all_titles = notes_listed_titles(tree, known)
    notes_search_entry(field, "Sauerteig")
    rec_titles = notes_listed_titles(tree)
    kb.press_key(ESCAPE); time.sleep(0.6)
    created = {t for t, _ in notes_rows() if t.startswith(tuple(NOTE_TITLES.values()))}
    note = apply_perturbation(tree, perturb, "Notes")

    results = []
    for term in fresh_inputs:
        for i in range(runs):
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            rp.press("search_button", 1.0)
            field = rp.resolve("search_field")
            reported, entry = None, None
            if field is not None:
                entry = notes_search_entry(field, term)
                reported = notes_listed_titles(tree, known)
                listed_raw = notes_listed_texts(tree)
            kb.press_key(ESCAPE); time.sleep(0.6)
            expected = db_matches(term)
            subset = reported is not None and set(reported) <= created
            # an empty expectation proves nothing (15/15 'OK' with no notes to find)
            ok = bool(expected) and reported is not None and set(reported) == set(expected)
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": term, "run": i, "success": ok, "subset_of_created": subset,
                 "reported": reported, "expected": expected, "listed_raw": locals().get("listed_raw"),
                 "db_error": LAST_DB_ERROR,
                 "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            if not ok and not any("debug_rows" in x for x in results):
                r["debug_rows"] = window_rows(tree)
            results.append(r)
            print(f"  flow6 {term}{i}: {'OK' if ok else 'FAIL'} reported={reported} expected={expected} "
                  f"wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    app.quit()
    return {"flow": "6 notes_search", "app": "Notes", "rung": "ax", "recorded_input": "Sauerteig",
            "recorded_titles": rec_titles, "record_ms": round(record_ms, 1), "recorded_steps": rec.steps,
            "perturbation": perturb, "perturbation_note": note, "runs": results}


# ── flow 8: System Settings, read-only ──────────────────────────────────────

def _sysctl(k):
    return subprocess.run(["/usr/sbin/sysctl", "-n", k], capture_output=True, text=True).stdout.strip()


def independent_value(label):
    if label == "Version":
        return subprocess.run(["/usr/bin/sw_vers", "-productVersion"], capture_output=True,
                              text=True).stdout.strip()
    if label == "Speicher":
        return f"{int(_sysctl('hw.memsize')) // (1024 ** 3)} GB"
    if label == "Seriennummer":
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOPlatformExpertDevice", "-d", "2"],
                             capture_output=True, text=True).stdout
        m = re.search(r'"IOPlatformSerialNumber" = "([^"]+)"', out)
        return m.group(1) if m else ""
    if label == "Chip":
        return _sysctl("machdep.cpu.brand_string")
    return ""


def read_labelled_value(tree, label):
    """The text next to `label` in the Info pane: for 'Version' the text that
    starts with it; otherwise the first section text after the label text."""
    texts = [(d.value or "", d.identifier or "") for _e, d in all_refs(tree) if d.role == "AXStaticText"]
    if label == "Version":
        for v, ident in texts:
            if v.startswith("Version ") and ident == "softwareSection":
                return v[len("Version "):]
        return None
    for idx, (v, _ident) in enumerate(texts):
        if v == label:
            for v2, ident2 in texts[idx + 1:idx + 3]:
                if ident2 in ("generalSection", "softwareSection"):
                    return v2
    return None


def press_sidebar_row(tree, ident):
    el, _d = by_identifier(tree, ident, "AXStaticText")
    if el is None:
        return False
    row = _attr(_attr(el, "AXParent"), "AXParent")
    if row is None:
        return False
    _AS().AXUIElementPerformAction(row, "AXPress"); time.sleep(1.5)
    return True


def flow8_settings_read(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    app = App(SETTINGS, acc)
    app.quit(); app.launch(wait=6.0); app.activate()
    tree = app.tree()
    t0 = time.monotonic()
    rec = Recorder(tree)
    gen, gd = by_identifier(tree, "com.apple.systempreferences.GeneralSettings", "AXStaticText")
    if gen is None:
        return {"flow": "8 settings_read", "error": "no General sidebar entry"}
    rec.add("general_entry", gen, gd)
    press_sidebar_row(tree, "com.apple.systempreferences.GeneralSettings")
    info, idsc = by_desc(tree, "AXButton", "Info")
    if info is None:
        return {"flow": "8 settings_read", "error": "no Info button"}
    rec.add("info_button", info, idsc)
    refs = all_refs(tree)
    graph, sid = make_state_graph("SystemSettings", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    _AS().AXUIElementPerformAction(info, "AXPress"); time.sleep(1.5)
    rec_val = read_labelled_value(tree, "Chip")
    recorded_ok = bool(rec_val) and independent_value("Chip").split(" (")[0] in rec_val

    results = []
    for label in fresh_inputs:
        for i in range(runs):
            # start from a different pane every run, so the recorded navigation is exercised
            press_sidebar_row(tree, "com.apple.Appearance-Settings.extension" if perturb == "other_pane"
                              else "com.apple.systempreferences.GeneralSettings")
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            entry = rp.resolve("general_entry")
            if entry is not None:
                row = _attr(_attr(entry, "AXParent"), "AXParent")
                tree.perform_action(row, "AXPress"); time.sleep(1.5)
            rp.press("info_button", 1.5)
            shown = read_labelled_value(tree, label)
            expected = independent_value(label)
            title = _s(_attr(_attr(tree._root(), "AXFocusedWindow"), "AXTitle"))
            ok = bool(shown) and bool(expected) and (shown == expected or expected in shown) and title == "Info"
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": label, "run": i, "success": ok, "shown": shown, "expected": expected,
                 "window_title": title, "latency_ms": round(elapsed, 1), "model_calls": 0, **rp.counts()}
            results.append(r)
            print(f"  flow8 {label}{i}: {'OK' if ok else 'FAIL'} shown={shown!r} expected={expected!r} "
                  f"wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    app.quit()
    return {"flow": "8 settings_read", "app": "SystemSettings", "rung": "ax", "recorded_input": "Chip",
            "recorded_ok": recorded_ok, "record_ms": round(record_ms, 1), "recorded_steps": rec.steps,
            "perturbation": perturb, "runs": results}


# ── flows 9 / 10: Safari on a page served from 127.0.0.1 inside the guest ──

CATALOGUE = {
    "q0": [("Amsel", "bird"), ("Buche", "tree"), ("Chinakohl", "vegetable")],
    "q1": [("Dachs", "mammal"), ("Eiche", "tree"), ("Fenchel", "vegetable"), ("Gans", "bird")],
    "q2": [("Hering", "fish"), ("Ingwer", "spice")],
    "q3": [("Kiebitz", "bird"), ("Linde", "tree"), ("Mangold", "vegetable"), ("Nerz", "mammal"), ("Otter", "mammal")],
}
ITEMS = {
    "101": {"Name": "Amsel", "Kategorie": "bird", "Gewicht": "100 g"},
    "102": {"Name": "Buche", "Kategorie": "tree", "Gewicht": "900 kg"},
    "103": {"Name": "Chinakohl", "Kategorie": "vegetable", "Gewicht": "1200 g"},
    "104": {"Name": "Dachs", "Kategorie": "mammal", "Gewicht": "12 kg"},
    "105": {"Name": "Hering", "Kategorie": "fish", "Gewicht": "350 g"},
}
PORT = 8765


def write_fixture_site(www):
    os.makedirs(os.path.join(www, "list"), exist_ok=True)
    os.makedirs(os.path.join(www, "item"), exist_ok=True)
    for q, items in CATALOGUE.items():
        lis = "\n".join(f'<li><a href="/item/{i + 101}.html">{html.escape(n)}</a> '
                        f'<span class="cat">{html.escape(c)}</span></li>' for i, (n, c) in enumerate(items))
        open(os.path.join(www, "list", f"{q}.html"), "w").write(
            f'<!doctype html><html><head><meta charset="utf-8"><title>Fixture catalogue {q}</title></head>'
            f'<body><h1>Fixture catalogue {q}</h1><ul id="results">{lis}</ul></body></html>')
    for iid, rec in ITEMS.items():
        dl = "".join(f"<dt>{k}</dt><dd>{html.escape(v)}</dd>" for k, v in rec.items())
        open(os.path.join(www, "item", f"{iid}.html"), "w").write(
            f'<!doctype html><html><head><meta charset="utf-8"><title>Item {iid}</title></head>'
            f'<body><h1>Item {iid}</h1><dl>{dl}</dl></body></html>')


def start_server(www):
    srv = subprocess.Popen([os.path.expanduser("~/anyuse-venv/bin/python"), "-m", "http.server", str(PORT),
                            "--bind", "127.0.0.1", "--directory", www],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/list/q0.html", timeout=1).read()
            return srv
        except Exception:
            time.sleep(0.4)
    return srv


def webarea(tree):
    for el, d in all_refs(tree):
        if d.role == "AXWebArea":
            return el, d
    return None, None


def wait_page(tree, title, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _el, d = webarea(tree)
        if d is not None and (d.ax_description or d.title or "") == title:
            return True
        time.sleep(0.4)
    return False


def links_under(tree, lst):
    """AXLink titles below the list element, walked (a path prefix cannot see
    past six ancestors; it read [] under a correctly resolved list)."""
    return [d.title or "" for _el, d in subtree(tree, lst, skip_roles=()) if d.role == "AXLink"]


def _navigate(tree, rp, kb, url, title):
    """Type a URL into Safari's address field and confirm the page loaded. An
    AXValue write reads back and Safari does not navigate on Return (0/30);
    it navigates on the text a user types."""
    field = rp.resolve("url_field")
    if field is None:
        return None

    def verify():
        kb.press_key(RETURN)
        return wait_page(tree, title, 8.0)
    return tree.enter_text(field, url, verify=verify, rungs=("ax_value", "keystrokes"))


def flow9_safari_list(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    kb = _kb()
    www = os.path.join(scratch, "www")
    write_fixture_site(www)
    srv = start_server(www)
    app = App(SAFARI, acc)
    app.quit(); app.launch(f"http://127.0.0.1:{PORT}/list/q0.html", wait=7.0); app.activate()
    tree = app.tree()
    wait_page(tree, "Fixture catalogue q0")
    t0 = time.monotonic()
    rec = Recorder(tree)
    uf, ud = by_identifier(tree, "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD", "AXTextField")
    rec.add("url_field", uf, ud)
    lst, ld = next(((el, d) for el, d in all_refs(tree) if d.role == "AXList" and "webarea" in tree._ax_path(el)),
                   (None, None))
    if lst is None:
        srv.terminate()
        return {"flow": "9 safari_list", "error": "no AXList in the web area"}
    rec.add("results_list", lst, ld)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Safari", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    recorded_ok = links_under(tree, lst) == [n for n, _c in CATALOGUE["q0"]]

    results = []
    for q in fresh_inputs:
        for i in range(runs):
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            entry = _navigate(tree, rp, kb, f"http://127.0.0.1:{PORT}/list/{q}.html", f"Fixture catalogue {q}")
            loaded = wait_page(tree, f"Fixture catalogue {q}", 2.0)
            lst = rp.resolve("results_list") if loaded else None
            reported = links_under(tree, lst) if lst is not None else None
            expected = [n for n, _c in CATALOGUE[q]]
            ok = loaded and reported == expected
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": q, "run": i, "success": ok, "page_loaded": loaded, "reported": reported,
                 "expected": expected, "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            results.append(r)
            print(f"  flow9 {q}{i}: {'OK' if ok else 'FAIL'} loaded={loaded} reported={reported} "
                  f"wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    srv.terminate()
    return {"flow": "9 safari_list", "app": "Safari", "rung": "ax", "recorded_input": "q0",
            "recorded_ok": recorded_ok, "record_ms": round(record_ms, 1), "recorded_steps": rec.steps,
            "perturbation": perturb, "runs": results}


def detail_fields(tree):
    """label -> value from the dl: static texts in document order, a value is
    the first non-empty text after its label."""
    texts = [(d.value or d.title or "") for _e, d in all_refs(tree)
             if d.role == "AXStaticText" and "webarea" in tree._ax_path(_e)]
    out = {}
    for label in ("Name", "Kategorie", "Gewicht"):
        for idx, t in enumerate(texts):
            if t == label:
                nxt = [x for x in texts[idx + 1:idx + 4] if x.strip()]
                if nxt:
                    out[label] = nxt[0].strip()
                break
    return out


def flow10_safari_detail(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    kb = _kb()
    www = os.path.join(scratch, "www")
    write_fixture_site(www)
    srv = start_server(www)
    app = App(SAFARI, acc)
    if app.pid() is None:
        app.launch(f"http://127.0.0.1:{PORT}/item/101.html", wait=7.0)
    app.activate()
    tree = app.tree()
    t0 = time.monotonic()
    rec = Recorder(tree)
    uf, ud = by_identifier(tree, "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD", "AXTextField")
    rec.add("url_field", uf, ud)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Safari", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    rp0 = Replayer(tree, rec.steps, graph, sid, acc, drift)
    _navigate(tree, rp0, kb, f"http://127.0.0.1:{PORT}/item/101.html", "Item 101")
    recorded_ok = detail_fields(tree) == ITEMS["101"]

    results = []
    for iid in fresh_inputs:
        for i in range(runs):
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            entry = _navigate(tree, rp, kb, f"http://127.0.0.1:{PORT}/item/{iid}.html", f"Item {iid}")
            loaded = wait_page(tree, f"Item {iid}", 2.0)
            fields = detail_fields(tree) if loaded else {}
            ok = loaded and fields == ITEMS[iid]
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": iid, "run": i, "success": ok, "page_loaded": loaded, "fields": fields,
                 "expected": ITEMS[iid], "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, **rp.counts()}
            results.append(r)
            print(f"  flow10 {iid}{i}: {'OK' if ok else 'FAIL'} fields={fields} wrong_target={r['wrong_target']} "
                  f"gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    app.quit(); srv.terminate()
    return {"flow": "10 safari_detail", "app": "Safari", "rung": "ax", "recorded_input": "101",
            "recorded_ok": recorded_ok, "record_ms": round(record_ms, 1), "recorded_steps": rec.steps,
            "perturbation": perturb, "runs": results}


# ── HIL: a destructive verb must stop at the gate ───────────────────────────

def flow_hil_delete(mods, scratch, runs, fresh_inputs, perturb, out):
    """Finder: select a file in the scratch folder, then the step 'File > Move
    to Trash'. The HIL gate is consulted BEFORE actuation with the recorded
    step as the product would see it (action 'press' + the menu item's label
    and identity). Under the current keyword set, and under the German set
    added on this branch. The harness NEVER actuates a step the gate would
    hold, and never actuates when the gate lets a delete through either — that
    arm is measured, not performed."""
    import asyncio
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    from anyuse.execution import hil_gate
    folder = os.path.join(scratch, "finder")
    os.makedirs(folder, exist_ok=True)
    victims = [os.path.join(folder, f"hil_victim_{i}.txt") for i in range(3)]
    for v in victims:
        open(v, "w").write("do not delete me\n")
    app = App(FINDER, acc)
    app.launch(folder); app.activate()
    tree = app.tree()
    # select the first victim in the icon view
    grp, gd = by_identifier(tree, os.path.basename(victims[0]), "AXGroup")
    selected = False
    if grp is not None:
        img = next((k for k in (_attr(grp, "AXChildren") or []) if _s(_attr(k, "AXRole")) == "AXImage"), grp)
        _AS().AXUIElementPerformAction(img, "AXPress")          # a click selects the icon
        time.sleep(0.6)
        rc = _AS().AXUIElementSetAttributeValue(grp, "AXSelected", True)
        time.sleep(0.4)
        status = next((d.value for _e, d in all_refs(tree) if (d.identifier or "") == "statusBarText"), "")
        selected = "ausgewählt" in (status or "") or rc == 0
    fm, fd = menu_bar_item(tree, ("File", "Ablage"))
    _AS().AXUIElementPerformAction(fm, "AXPress"); time.sleep(0.9)
    trash, td = menu_item_by_cmd(tree, "\x08", ("", "0"))   # ⌘⌫ 'In den Papierkorb legen'
    rec = Recorder(tree)
    step = rec.add("move_to_trash", trash, td) if trash is not None else None
    enabled = _attr(trash, "AXEnabled") if trash is not None else None
    _AS().AXUIElementPerformAction(fm, "AXCancel"); time.sleep(0.4)

    args = {"label": td.title if td else "", "role": "AXMenuItem",
            "identifier": (td.identifier if td else "") or "", "cmd": "⌘⌫"}
    arms = []
    # 'press' is the AX executor's verb (unrecognised by the classifier -> fails
    # closed); 'click' is the recorder's coordinate verb (recognised). Both are
    # what the product would actually hand the gate for this step.
    for verb in ("press", "click"):
        action = {"action": verb, "args": dict(args)}
        for label, keywords in ((f"{verb}/current_defaults", hil_gate._DESTRUCTIVE_KEYWORDS),
                                (f"{verb}/with_localized_keywords", hil_gate.DESTRUCTIVE_KEYWORDS_ALL)):
            cls = hil_gate.classify_step(action, keywords=keywords)
            requests = []

            async def approver(ctx, _r=requests):
                _r.append({"step_class": ctx["step_class"], "action": ctx["action"], "args": ctx["args"]})
                return False   # nobody is there to approve; a recorded request is the outcome

            gate = hil_gate.HILGate(mode="destructive", approver=approver)
            decision = asyncio.run(gate.require_approval(action, drive_enabled=True, keywords=keywords))
            arms.append({"arm": label, "step_class": cls, "actuate": decision.actuate, "reason": decision.reason,
                         "hil_request_recorded": len(requests), "request": requests[0] if requests else None})
            print(f"  HIL {label}: class={cls} actuate={decision.actuate} ({decision.reason}) requests={len(requests)}")
    # Nothing was actuated in either arm: the harness refuses a delete even
    # when the gate would let it through. That refusal is itself the finding.
    close_front_window(tree)
    return {"flow": "H hil_delete", "app": "Finder", "recorded_step": step, "selected": selected,
            "menu_item_enabled": enabled, "menu_item_title": td.title if td else None,
            "arms": arms, "actuated": False,
            "victims_still_exist": [os.path.exists(v) for v in victims],
            "victims_content_intact": [open(v).read() == "do not delete me\n" for v in victims]}


MORE_FLOWS = {
    "3": (flow3_finder_create_folder, ["quince", "rhubarb", "sorrel"]),
    "3b": (flow3b_finder_rename, ["kumquat", "starfruit", "lychee"]),
    "4": (flow4_finder_search, ["tart", "berry", "square"]),
    "5": (flow5_notes_create, ["quince", "harz", "lamp"]),
    "6": (flow6_notes_search, ["Quitten", "Harz", "Lampe"]),
    "8": (flow8_settings_read, ["Version", "Speicher", "Seriennummer"]),
    "9": (flow9_safari_list, ["q1", "q2", "q3"]),
    "10": (flow10_safari_detail, ["102", "103", "104"]),
    "H": (flow_hil_delete, [None]),
}
