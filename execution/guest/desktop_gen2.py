#!/usr/bin/env python3
"""Desktop generalisation, guest side. INSIDE THE GUEST VM ONLY.

 Builds on ``desktop_gen.py``
and adds the effect probe, the stable model reader, the scoped-find
semantics calibration and declared coupled settings. Subcommands:

  inspect --out DIR   read-only: container structure (all rows, not the
                      viewport) of a few apps' start windows
  probe   --out DIR   dump + effect probe of every app on DISPOSABLE probe
                      documents, map by effect, calibrate scoped_find
  prove   --out DIR   record + prove the measured set (fixed rule)
  lang    --out DIR   the localisation run (English UI, German recording)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop_gen as G                        # noqa: E402  (guest helpers)
from flows import refuse_unless_in_a_guest     # noqa: E402

_at, _s, _AS = G._at, G._s, G._AS

REMINDERS = "com.apple.reminders"


def _texts(el, depth=0, out=None, limit=40):
    """Every text value in an element's subtree (role:value), depth-first."""
    out = [] if out is None else out
    if depth > 6 or len(out) >= limit:
        return out
    role = _s(_at(el, "AXRole")) or ""
    for a in ("AXTitle", "AXValue", "AXDescription"):
        v = _at(el, a)
        if isinstance(v, str):
            v = v.strip()
            if v:
                out.append(f"{role}.{a[2:]}={v.strip()[:60]}")
    for k in _at(el, "AXChildren") or []:
        _texts(k, depth + 1, out, limit)
    return out


def inspect_containers(pid):
    win = G.front_window(pid)
    res = []
    if win is None:
        return res

    def walk(el, depth):
        if depth > 14:
            return
        role = _s(_at(el, "AXRole")) or ""
        if role in ("AXTable", "AXOutline", "AXList", "AXBrowser"):
            rows = _at(el, "AXRows")
            vis = _at(el, "AXVisibleRows")
            kids = list(_at(el, "AXChildren") or [])
            sel = _at(el, "AXSelectedRows")
            item = {"role": role, "subrole": _s(_at(el, "AXSubrole")) or "",
                    "identifier": _s(_at(el, "AXIdentifier")) or "",
                    "description": _s(_at(el, "AXDescription")) or "",
                    "rows": None if rows is None else len(rows),
                    "visible_rows": None if vis is None else len(vis),
                    "children": len(kids), "selected_rows": None if sel is None else len(sel),
                    "child_roles": sorted({_s(_at(k, "AXRole")) or "" for k in kids}),
                    "row_texts": [_texts(r) for r in list(rows or kids)[:14]],
                    "selected_texts": [_texts(r) for r in list(sel or [])[:3]]}
            res.append(item)
            return
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    walk(win, 0)
    return res


def cmd_inspect(args):
    G.prepare_start_documents()
    out = []
    targets = [("Finder", "com.apple.finder", [G.FINDER_DIR]), ("Notizen", "com.apple.Notes", []),
               ("Erinnerungen", REMINDERS, []), ("Kalender", "com.apple.iCal", [])]
    only = {x for x in (args.only or "").split(",") if x}
    for name, bid, docs in [t for t in targets if not only or t[0] in only]:
        if G.locked():
            out.append({"app": name, "stopped": "lock screen"})
            break
        try:
            ctl = G.AppCtl(bid, quittable=bid != "com.apple.finder")
            pid = ctl.restart(docs)
            app = _AS().AXUIElementCreateApplication(pid)
            wins = [{"role": _s(_at(w, "AXRole")), "subrole": _s(_at(w, "AXSubrole")),
                     "identifier": _s(_at(w, "AXIdentifier")), "document": _s(_at(w, "AXDocument"))}
                    for w in (_at(app, "AXWindows") or [])]
            foc = _at(app, "AXFocusedUIElement")
            out.append({"app": name, "bundle_id": bid, "windows": wins,
                        "focused": None if foc is None else {"role": _s(_at(foc, "AXRole")),
                                                             "subrole": _s(_at(foc, "AXSubrole"))},
                        "containers": inspect_containers(pid),
                        "controls": [dict(c, texts=None) for c in
                                     G.ddg.read_window_controls(G.front_window(pid), limit=600)],
                        "window_texts": _texts(G.front_window(pid), limit=200)})
            if bid != "com.apple.finder":
                G.ddg._quit(bid)
        except Exception as exc:  # noqa: BLE001
            out.append({"app": name, "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()})
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "inspect.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps(out, default=str)[:4000])
    return 0


# ════════════════════════════════════════════════════════════════════════════
# Effect probe, stable model reader, semantics calibration, proof
# ════════════════════════════════════════════════════════════════════════════

import hashlib        # noqa: E402
import shutil         # noqa: E402
import subprocess     # noqa: E402

from anyuse.execution import desktop_contracts as C   # noqa: E402  (pure)
from anyuse.execution import desktop_effects as E     # noqa: E402  (pure)

GEN2 = os.path.expanduser("~/anyuse-guest/scratch/desktop_gen2")
PROBE_DIR = os.path.join(GEN2, "probe")
PROBE_TE = os.path.join(PROBE_DIR, "te_probe.txt")
PROBE_FINDER = os.path.join(PROBE_DIR, "finder")
PROBE_PNG = os.path.join(PROBE_DIR, "probe.png")
CALIB = os.path.join(GEN2, "calib")
ICAL = "com.apple.iCal"
APPS2 = [("TextEdit", "com.apple.TextEdit"), ("Notizen", "com.apple.Notes"),
         ("Finder", "com.apple.finder"), ("Rechner", "com.apple.calculator"),
         ("Vorschau", "com.apple.Preview"), ("Kalender", ICAL), ("Notizzettel", "com.apple.Stickies"),
         ("Schach", "com.apple.Chess"),
         # harmless apps with settings menus
         ("Farbmessung", "com.apple.DigitalColorMeter"), ("Systeminfo", "com.apple.SystemProfiler"),
         ("Konsole", "com.apple.Console"), ("Woerterbuch", "com.apple.Dictionary"),
         ("Blender", G.ddg.BLENDER_BID)]
MEASURED2 = ("doc_save", "object_create", "scoped_find", "toggle_select", "flag_toggle")
MAX_SETTING_GROUPS = 3
PROBE_BUDGET_S = 1500          # per app

# ── fixed inputs ───────────────────────────────
REC_INPUT2 = {"doc_save": "gen2-beta", "object_create": "gen2rec", "scoped_find": "jam"}
PROOF_INPUTS2 = {"doc_save": ["gen-papaya 58", "gen-guava 73"],
                 "object_create": ["genpapaya", "genguava"],
                 "scoped_find": ["wine", "steen"]}
SEEN2 = {"doc_save": G.SEEN["doc_save"] + ["gen-alpha", "gen-kiwano 17", "gen-salak 42"],
         "object_create": ["genrec", "genmango", "genlychee"],
         "scoped_find": ["tart", "pie", "berry", "fig", "roll"]}
BL_INPUTS2 = [(1.25, -2.75, 0.5), (-3.0, 0.25, 1.75)]
BL_SEEN2 = list(G.BL_SEEN) + list(G.BL_INPUTS)

WRONG_TARGET_MARKS2 = G.WRONG_TARGET_MARKS + ("undeclared side effect", "scopes lost", "scopes missing",
                                              "missing from the model", "not satisfy the")


def probe_docs(bid):
    return {"com.apple.TextEdit": [PROBE_TE], "com.apple.finder": [PROBE_FINDER],
            "com.apple.Preview": [PROBE_PNG]}.get(bid, [])


def reset_probe_docs():
    os.makedirs(PROBE_FINDER, exist_ok=True)
    for n in os.listdir(PROBE_FINDER):
        if n not in G.FINDER_SEEDS:
            p = os.path.join(PROBE_FINDER, n)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    for n in G.FINDER_SEEDS:
        with open(os.path.join(PROBE_FINDER, n), "w") as fh:
            fh.write(n + "\n")
    with open(PROBE_TE, "w") as fh:
        fh.write("probe seed\n")
    G._png(PROBE_PNG)


class AppCtl2(G.AppCtl):
    """Probe-phase app control: launches ignore saved window state, so a
    force-quit after a stuck dialog does not bring old windows back."""

    def launch(self, docs=(), wait=3.0):
        lang = self.lang
        try:
            import AppKit
            docs = list(docs)
            args = ["/usr/bin/open", "-b", self.bid] + docs[:1] + ["--args", "-ApplePersistenceIgnoreState", "YES"]
            if lang:
                args += ["-AppleLanguages", f"({lang})"]
            subprocess.run(args, capture_output=True, timeout=30)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and self.running() is None:
                time.sleep(0.4)
            time.sleep(wait)
            for d in docs[1:]:
                subprocess.run(["/usr/bin/open", "-b", self.bid, d], capture_output=True, timeout=30)
                time.sleep(1.8)
            a = self.running()
            if a is not None:
                a.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
                time.sleep(0.8)
            return self.pid()
        except Exception:  # noqa: BLE001
            return None

    def quit(self):
        super().quit()
        if self.quittable and self.running() is not None:
            a = self.running()
            if a is not None:
                a.forceTerminate()
                time.sleep(1.5)


# ── stable readers ──────────────────────────────────────────────────────────

def _values(el, depth=0, out=None):
    """AXValue/AXTitle strings in a subtree (no descriptions), depth-first."""
    out = [] if out is None else out
    if depth > 5 or len(out) > 12:
        return out
    for a in ("AXValue", "AXTitle"):
        v = _at(el, a)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    for k in _at(el, "AXChildren") or []:
        _values(k, depth + 1, out)
    return out


def _first_label(el, depth=0):
    """An item's primary label: its first value/title in the subtree; else
    its accessibility description up to the first '. ' (VoiceOver puts the
    name first: 'Holiday. 3 October 2026, all day')."""
    vals = _values(el)
    if vals:
        return vals[0]
    stack = [(el, 0)]
    while stack:
        e, d = stack.pop(0)
        v = _at(e, "AXDescription")
        if isinstance(v, str) and v.strip() and (_s(_at(e, "AXRole")) or "") in ("AXStaticText", "AXImage",
                                                                              "AXButton", "AXGroup"):
            return v.strip().split(". ")[0]
        if d < 4:
            stack.extend((k, d + 1) for k in (_at(e, "AXChildren") or []))
    return None


ITEM_ROLES = ("AXImage", "AXStaticText", "AXTextField", "AXButton", "AXCell", "AXGroup")


def item_labels(container):
    """Every item's primary label: AXRows (all rows, not the visible ones)
    when the container has rows, else its item children, descending through
    nested lists (an icon/gallery view)."""
    rows = _at(container, "AXRows")
    if rows is not None:
        return [v for v in (_first_label(r) for r in rows) if v]
    out = []

    def walk(el, depth):
        for k in _at(el, "AXChildren") or []:
            role = _s(_at(k, "AXRole")) or ""
            if role in ("AXList", "AXScrollArea") and depth < 3:
                walk(k, depth + 1)
                continue
            if role in ITEM_ROLES:
                v = _first_label(k)
                if v:
                    out.append(v)
    walk(container, 0)
    return out


def containers(pid):
    """[(key 'role:identifier', element)] for every table/outline/list/
    browser in the front window (a list nested in a list is part of its
    parent)."""
    win = G.front_window(pid)
    out = []
    if win is None:
        return out
    n = [0]

    def walk(el, depth):
        if depth > 16 or n[0] > 6000:
            return
        n[0] += 1
        role = _s(_at(el, "AXRole")) or ""
        if role in C.CONTAINER_ROLES:
            out.append((f"{role}:{_s(_at(el, 'AXIdentifier')) or ''}", el))
            return
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    walk(win, 0)
    return out


def container_rows(el):
    rows = _at(el, "AXRows")
    if rows is not None:
        return len(rows)
    kids = list(_at(el, "AXChildren") or [])
    if len(kids) == 1 and (_s(_at(kids[0], "AXRole")) or "") == "AXList":
        return len(_at(kids[0], "AXChildren") or [])
    return len(kids)


def model_state(pid, docs):
    """The app's model, read independently of the viewport: the backend
    (folder listing / file digest) when the start document is on disk, plus
    every navigation container's scopes with their object counts (all rows)."""
    m = {}
    if docs:
        d = docs[0]
        if os.path.isdir(d):
            m = {"kind": "folder", "items": sorted(n for n in os.listdir(d) if not n.startswith("."))}
        elif os.path.isfile(d):
            with open(d, "rb") as fh:
                m = {"kind": "file", "sha": hashlib.sha1(fh.read()).hexdigest()[:16]}
    scopes = {}
    for key, el in containers(pid):
        rows = _at(el, "AXRows")
        if rows is None:
            continue
        sc = E.scopes_from_rows([_values(r) for r in list(rows)[:300]])
        if sc:
            for k, v in sc.items():
                scopes[f"{k}"] = v
    if scopes:
        m["scopes"] = scopes
    return m or None


# ── authentication prompts: detect, refuse, cancel; never answered ─────────

AUTH_AGENTS = ("com.apple.SecurityAgent",)


def ax_tree(el, depth=0, budget=None):
    """A role-only copy of an AX subtree for desktop_effects.is_auth_prompt
    (no titles, no values: a prompt's text is never read)."""
    budget = budget if budget is not None else [1500]
    budget[0] -= 1
    node = {"role": _s(_at(el, "AXRole")) or "", "subrole": _s(_at(el, "AXSubrole")) or "", "children": []}
    if depth < 10 and budget[0] > 0:
        node["children"] = [ax_tree(k, depth + 1, budget) for k in (_at(el, "AXChildren") or [])[:60]]
    return node


def auth_prompts(pid):
    """[(pid, window element)] of authentication prompts belonging to the app
    or to the system's authentication agent."""
    import AppKit
    pids = [pid] if pid else []
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if str(app.bundleIdentifier() or "") in AUTH_AGENTS or "SecurityAgent" in str(app.executableURL() or ""):
            pids.append(int(app.processIdentifier()))
    out = []
    for p in pids:
        for w in _at(_AS().AXUIElementCreateApplication(p), "AXWindows") or []:
            if E.is_auth_prompt(ax_tree(w)):
                out.append((p, w))
    return out


def cancel_auth(prompts):
    """Cancel each prompt: its AXCancelButton, else Escape posted to its
    process. Nothing is ever typed into it."""
    import Quartz
    log = []
    for p, w in prompts:
        cb = _at(w, "AXCancelButton")
        if cb is not None:
            log.append({"pid": p, "cancel": "AXCancelButton", "err": int(_AS().AXUIElementPerformAction(cb, "AXPress"))})
        else:
            for down in (True, False):
                Quartz.CGEventPostToPid(p, Quartz.CGEventCreateKeyboardEvent(None, 53, down))
                time.sleep(0.1)
            log.append({"pid": p, "cancel": "escape"})
        time.sleep(1.5)
    return log


AUTH_REFUSAL = "requires authentication: refused, prompt cancelled, no credentials entered"


class AuthGuard:
    """Mixin for the proof drivers: after every actuation, an authentication
    prompt refuses the step (a refusal, not a wrong target)."""

    def _auth_check(self, pid, res):
        prompts = auth_prompts(pid)
        if prompts:
            self.ev.setdefault("auth_prompts", []).append(cancel_auth(prompts))
            if auth_prompts(pid):
                print("AUTH PROMPT COULD NOT BE CANCELLED: stopping", flush=True)
                raise SystemExit(5)
            return {"error": AUTH_REFUSAL, "refused": True}
        return res

    def press_trigger(self, pid, trig, name):
        return self._auth_check(pid, super().press_trigger(pid, trig, name))

    def enter(self, pid, el, text, name, commit=False):
        return self._auth_check(pid, super().enter(pid, el, text, name, commit))


def _digest(x):
    return hashlib.sha1(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]


def read_top_marks(pid, top):
    """{index path: marked} for every item of one top-level menu, read with
    the menu OPEN (a read: AXPress on the menu-bar item, then AXCancel)."""
    app = _AS().AXUIElementCreateApplication(pid)
    mb = _at(app, "AXMenuBar")
    kids = list(_at(mb, "AXChildren") or []) if mb is not None else []
    if top is None or top >= len(kids):
        return None
    bar = kids[top]
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.7)
    out = {}

    def walk(menu, path, depth):
        if depth > 5:
            return
        for j, it in enumerate(_at(menu, "AXChildren") or []):
            if (_s(_at(it, "AXRole")) or "") != "AXMenuItem":
                continue
            out[E.path_key(path + [j])] = bool((_s(_at(it, "AXMenuItemMarkChar")) or "").strip())
            for sub in _at(it, "AXChildren") or []:
                if (_s(_at(sub, "AXRole")) or "") == "AXMenu":
                    walk(sub, path + [j], depth + 1)
    for menu in _at(bar, "AXChildren") or []:
        if (_s(_at(menu, "AXRole")) or "") == "AXMenu":
            walk(menu, [top], 1)
    _AS().AXUIElementPerformAction(bar, "AXCancel")
    time.sleep(0.3)
    return out


def all_open_marks(pid):
    app = _AS().AXUIElementCreateApplication(pid)
    mb = _at(app, "AXMenuBar")
    n = len(list(_at(mb, "AXChildren") or [])) if mb is not None else 0
    out = {}
    for top in range(1, n):
        got = read_top_marks(pid, top) or {}
        out.update({k: ("✓" if v else "") for k, v in got.items()})
    return out


def snapshot(pid, docs, top=None):
    app = _AS().AXUIElementCreateApplication(pid)
    wins = list(_at(app, "AXWindows") or [])
    windows = [{"role": _s(_at(w, "AXRole")), "subrole": _s(_at(w, "AXSubrole")),
                "identifier": _s(_at(w, "AXIdentifier")), "document": G._url_path(_at(w, "AXDocument"))}
               for w in wins]
    sheets = sum(1 for w in wins for k in (_at(w, "AXChildren") or []) if _s(_at(k, "AXRole")) == "AXSheet")
    foc = _at(app, "AXFocusedUIElement")
    focused = None if foc is None else {"role": _s(_at(foc, "AXRole")), "subrole": _s(_at(foc, "AXSubrole")),
                                        "identifier": _s(_at(foc, "AXIdentifier"))}
    conts, sel = {}, []
    for key, el in containers(pid):
        conts.setdefault(key, {"rows": 0})["rows"] += container_rows(el)
        srows = _at(el, "AXSelectedRows")
        if srows is not None:
            sel.append((key, [_first_label(r) for r in list(srows)[:5]]))
    texts = []
    win = G.front_window(pid)

    def walk(el, depth):
        if depth > 12 or len(texts) > 5:
            return
        if _s(_at(el, "AXRole")) == "AXTextArea":
            v = _at(el, "AXValue")
            texts.append(str(v) if v is not None else "")
            return
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    if win is not None:
        walk(win, 0)
    snap = {"windows": windows, "sheets": sheets, "focused": focused, "containers": conts,
            "model": model_state(pid, docs), "text": _digest(texts) if texts else None,
            "selection": _digest(sel), "marks": None, "bundle_id": G.app_identity(pid)["bundle_id"],
            "auth_prompt": bool(auth_prompts(pid))}
    if top is not None:
        snap["marks"] = read_top_marks(pid, top)
    return snap


def press_item(pid, trig):
    """Open the item's top-level menu, resolve the item by its recorded
    identity (identifier and/or key equivalent; 0 or >1 matches refuse),
    read its AXEnabled, press it. Returns (enabled, error)."""
    app_el = _AS().AXUIElementCreateApplication(pid)
    mb = _at(app_el, "AXMenuBar")
    kids = list(_at(mb, "AXChildren") or []) if mb is not None else []
    ip = trig.get("index_path") or []
    bar = kids[ip[0]] if ip and ip[0] < len(kids) else None
    if bar is not None:
        _AS().AXUIElementPerformAction(bar, "AXPress")
        time.sleep(0.6)
    el, _ident, why = G.resolve_trigger(pid, trig)
    if el is None:
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
        return None, why
    enabled = bool(_at(el, "AXEnabled"))
    if not enabled:
        if bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
            time.sleep(0.3)
        return False, None
    err = _AS().AXUIElementPerformAction(el, "AXPress")
    # sample which app is frontmost while the press takes effect: an item
    # that hands focus to another process (dictation, a system palette)
    # makes a document app autosave on deactivation, which is not a save
    fronts = set()
    for _ in range(6):
        time.sleep(0.25)
        fronts.add(_frontmost())
    PRESS_FRONTS[:] = sorted(f for f in fronts if f)
    if err and bar is not None:
        _AS().AXUIElementPerformAction(bar, "AXCancel")
    return True, None


PRESS_FRONTS = []


def _frontmost():
    """The focused application, read LIVE through the system-wide AX element
    (NSWorkspace.frontmostApplication is only refreshed by a run loop, which
    this job does not spin, so it gives a stale value for every press)."""
    try:
        import AppKit
        sw = _AS().AXUIElementCreateSystemWide()
        app = _at(sw, "AXFocusedApplication")
        if app is None:
            return None
        err, pid = _AS().AXUIElementGetPid(app, None)
        a = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid)) if not err else None
        return str(a.bundleIdentifier()) if a is not None else None
    except Exception:  # noqa: BLE001
        return None


AUTH_REFUSED = "auth_refused.json"


def load_auth_refused(out):
    try:
        with open(os.path.join(out, AUTH_REFUSED)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def record_auth_refused(out, bid, trig, why):
    d = load_auth_refused(out)
    d.setdefault(bid, {})[E.identity_string(trig)] = why
    with open(os.path.join(out, AUTH_REFUSED), "w") as fh:
        json.dump(d, fh, indent=1)


def clear_selection(pid):
    """The declared clean start: nothing selected in any container (an item
    selected by default would enable selection-dependent commands)."""
    if not pid:
        return
    for _k, el in containers(pid):
        if (_s(_at(el, "AXRole")) or "") == "AXOutline":
            # a navigation outline's selection IS the shown scope (Notes'
            # folder list: clearing it disabled New Note)
            continue
        for attr in ("AXSelectedRows", "AXSelectedChildren"):
            if _at(el, attr) is not None:
                _AS().AXUIElementSetAttributeValue(el, attr, [])
        for kid in _at(el, "AXChildren") or []:
            if (_s(_at(kid, "AXRole")) or "") == "AXList" and _at(kid, "AXSelectedChildren") is not None:
                _AS().AXUIElementSetAttributeValue(kid, "AXSelectedChildren", [])
    time.sleep(0.4)


def clean_disposable(log):
    """Reset the disposable test folders to their declared seeds. Anything
    extra is MOVED to gen2/archive/<time>/ (never deleted)."""
    arch = os.path.join(GEN2, "archive", time.strftime("%Y%m%d-%H%M%S"))
    for folder, seeds in ((G.FINDER_DIR, G.FINDER_SEEDS), (PROBE_FINDER, G.FINDER_SEEDS)):
        os.makedirs(folder, exist_ok=True)
        shared = folder == G.FINDER_DIR
        for n in sorted(os.listdir(folder)):
            if n.startswith(".") or n in seeds:
                continue
            if shared and not n.startswith("gen"):
                # gen/finder is shared with another agent's jobs (rep*): only
                # this project's own objects (gen*) are moved
                continue
            dst = os.path.join(arch, os.path.basename(os.path.dirname(folder)) + "_" + os.path.basename(folder))
            os.makedirs(dst, exist_ok=True)
            shutil.move(os.path.join(folder, n), os.path.join(dst, n))
            log.append(f"moved {folder}/{n} -> {dst}/")
        for n in seeds:
            with open(os.path.join(folder, n), "w") as fh:
                fh.write(n + "\n")
            os.chmod(os.path.join(folder, n), 0o644)
    G.prepare_start_documents()
    reset_probe_docs()


def cmd_check(args):
    """Read-only gate before measuring: screen unlocked, no auth prompt and
    no dialog/sheet in any regular app (roles only; nothing is pressed or
    typed), then the disposable folders are reset (files only)."""
    rep_ = _session_state()
    quit_log = []
    if not rep_["locked"] and rep_["auth_prompts"] == 0:
        # known windows: every measured app (never Terminal, never an auth
        # agent) is quit, force-quit only if it does not go; Finder's windows
        # are closed. A sheet or dialog left by an earlier (interrupted) job
        # goes with its app. Nothing is pressed inside any dialog.
        import AppKit
        measured_bids = {b for _n, b in APPS2 if b not in ("com.apple.finder", G.ddg.BLENDER_BID)}
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            bid = str(app.bundleIdentifier() or "")
            if bid in measured_bids:
                app.terminate()
                t0 = time.monotonic()
                while time.monotonic() - t0 < 8 and not app.isTerminated():
                    time.sleep(0.3)
                forced = False
                if not app.isTerminated():
                    app.forceTerminate()
                    forced = True
                    time.sleep(1.5)
                quit_log.append({"bundle_id": bid, "forced": forced})
        G.AppCtl("com.apple.finder", False)._close_windows()
        time.sleep(1.0)
        rep_ = _session_state()
    rep_["quit"] = quit_log
    # windows above the normal layer that would dirty every
    # proof row (a helper's panel left by an earlier job); a helper process
    # (no Dock presence) is ended, an auth agent never touched, a regular
    # app's panel reported. Nothing is pressed or typed in any of them.
    rep_["leftover_windows"] = _tidy_leftover_windows()
    rep_["clean"] = not rep_["locked"] and rep_["auth_prompts"] == 0 and \
        not any(a["dialogs"] or a["sheets"] for a in rep_["apps"]) and \
        not rep_["leftover_windows"]["remaining"]
    moved = []
    if rep_["clean"]:
        clean_disposable(moved)
    rep_["moved"] = moved
    rep_["finder_dir"] = sorted(os.listdir(G.FINDER_DIR))
    rep_["probe_finder"] = sorted(os.listdir(PROBE_FINDER))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "check.json"), "w") as fh:
        json.dump(rep_, fh, indent=1)
    print(json.dumps(rep_, indent=1))
    return 0 if rep_["clean"] else 4


def _onscreen_windows():
    """[{owner, pid, layer, title}] front to back, and the pids of regular
    (Dock-present) applications."""
    import AppKit
    import Quartz
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    wins = [{"owner": str(w.get("kCGWindowOwnerName") or ""), "pid": int(w.get("kCGWindowOwnerPID") or 0),
             "layer": int(w.get("kCGWindowLayer") or 0), "title": str(w.get("kCGWindowName") or "")}
            for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []]
    regular = [int(a.processIdentifier()) for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
               if a.activationPolicy() == 0]
    return wins, regular


def _tidy_leftover_windows():
    """desktop_hygiene.leftover_windows on the live window list; helpers
    ('end') are terminated (force after 5 s), everything is reported."""
    import AppKit
    from anyuse.execution import desktop_hygiene as H
    wins, regular = _onscreen_windows()
    found = H.leftover_windows(wins, regular)
    ended = []
    for w in found:
        if w["tidy"] != "end":
            continue
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(w["pid"])
        if app is None:
            continue
        rec = dict(w, bundle_id=str(app.bundleIdentifier() or ""),
                   exe=str(app.executableURL().path()) if app.executableURL() else None)
        app.terminate()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5 and not app.isTerminated():
            time.sleep(0.3)
        rec["forced"] = not app.isTerminated()
        if rec["forced"]:
            app.forceTerminate()
            time.sleep(1.5)
        ended.append(rec)
    time.sleep(1.0)
    wins, regular = _onscreen_windows()
    return {"found": found, "ended": ended, "remaining": H.leftover_windows(wins, regular)}


def _session_state():
    import AppKit
    import Quartz
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    rep_ = {"locked": bool(d.get("CGSSessionScreenIsLocked")), "apps": [], "auth_prompts": 0}
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        pid = int(app.processIdentifier())
        bid = str(app.bundleIdentifier() or "")
        if app.activationPolicy() != 0 and "SecurityAgent" not in str(app.executableURL() or ""):
            continue
        wins = list(_at(_AS().AXUIElementCreateApplication(pid), "AXWindows") or [])
        dialogs = sum(1 for w in wins if (_s(_at(w, "AXSubrole")) or "") in E.DIALOG_SUBROLES)
        sheets = sum(1 for w in wins for k in (_at(w, "AXChildren") or []) if _s(_at(k, "AXRole")) == "AXSheet")
        auth = sum(1 for w in wins if E.is_auth_prompt(ax_tree(w)))
        rep_["auth_prompts"] += auth
        rep_["apps"].append({"bundle_id": bid, "windows": len(wins), "dialogs": dialogs, "sheets": sheets,
                             "auth_prompts": auth})
    return rep_


def dirty_document(pid):
    """Probe start for a document app: the front document's text area gets
    new text (an unsaved edit), so a Save-class item has something to save."""
    win = G.front_window(pid)
    found = []

    def walk(el, depth):
        if depth > 12 or found:
            return
        if _s(_at(el, "AXRole")) == "AXTextArea":
            found.append(el)
            return
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    if win is not None:
        walk(win, 0)
    if found:
        _AS().AXUIElementSetAttributeValue(found[0], "AXValue", "probe edit\n")
        time.sleep(0.5)
        return True
    return False


# ── dump + effect probe ─────────────────────────────────────────────────────

def dump_one2(bid, docs, ctl):
    import AppKit
    url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
    if url is None:
        return {"bundle_id": bid, "error": "no app for bundle id"}
    app_path = str(url.path())
    pid = ctl.restart(docs)
    if pid is None:
        return {"bundle_id": bid, "error": "did not launch"}
    app_el = _AS().AXUIElementCreateApplication(pid)
    menu = G.ddg.read_menu_bar(app_el)
    for _ in range(4):
        # right after a login the menu bar of a just-launched app can read
        # empty: activate and read again
        if menu:
            break
        import AppKit as _AK
        a = ctl.running()
        if a is not None:
            a.activateWithOptions_(_AK.NSApplicationActivateIgnoringOtherApps)
        time.sleep(3.0)
        menu = G.ddg.read_menu_bar(app_el)
    win = G.ddg.front_window(app_el)
    controls = G.ddg.read_window_controls(win)
    meta, sdef, suite = G.ddg.read_scripting(app_path)
    return {"bundle_id": bid, "app": os.path.basename(app_path)[:-4], "app_path": app_path, "pid": pid,
            "scripting_meta": meta, "menu_items": menu, "window_controls": controls, "sdef": sdef,
            "script_suite": suite, "bpy_catalog": None, "open_marks": all_open_marks(pid),
            "notes": [f"probe documents: {docs}"]}


def layout_keys(pid):
    return sorted({k for k, _el in containers(pid)})


def norm_layout_key(k):
    """A container key up to a trailing alphabetic variant suffix of its
    identifier: Notes' note list is ``ICMNoteGridCollectionView_asList`` or
    ``…_asGallery`` by view mode, one container."""
    role, _, ident = str(k).partition(":")
    stem, sep, tail = ident.rpartition("_")
    if sep and stem and tail.isalpha():
        ident = stem
    return f"{role}:{ident}"


def same_layout(a, b):
    return {norm_layout_key(k) for k in a or []} == {norm_layout_key(k) for k in b or []}


#: a probe row with any of these effects is not a pure pane toggle and is
#: never pressed to restore a layout (Notes' ⌘N changes the layout
#: AND creates a note)
NOT_A_TOGGLE = ("object_created", "object_removed", "object_created_on_commit", "document_changed", "doc_saved",
                "window_opened", "dialog_opened", "auth_required", "entry_focused", "search_focused")


def restore_layout(ctl, bid, docs, out, restart=True):
    """Bring the app's pane layout (its set of containers) back to the
    reference (``layout_ref.json``: the first snapshot of the first
    probe job), using only items that earlier probe rows saw change the
    layout and nothing else, each pressed at most once and pressed back if
    it did not help. Returns a log. Generic: no item is chosen by title."""
    log = {"reference": None, "before": None, "pressed": [], "after": None}
    ref_p = os.path.join(out, "layout_ref.json")
    refs = {}
    if os.path.exists(ref_p):
        with open(ref_p) as fh:
            refs = json.load(fh)
    pid = ctl.restart(docs) if restart else ctl.pid()
    cur = layout_keys(pid)
    log["before"] = cur
    if bid not in refs:
        refs[bid] = cur
        with open(ref_p, "w") as fh:
            json.dump(refs, fh, indent=1)
    ref = refs[bid]
    log["reference"] = ref
    if same_layout(cur, ref):
        log["after"] = cur
        return log
    toggles = []
    for f in sorted(os.listdir(out)):
        if f.startswith("probes") and bid in f:
            with open(os.path.join(out, f)) as fh:
                for r in json.load(fh).get("probes") or []:
                    b, a = (r.get("before") or {}).get("containers"), (r.get("after") or {}).get("containers")
                    if b is not None and a is not None and set(b) != set(a) and r["trigger"] not in toggles \
                            and len((r.get("before") or {}).get("windows") or []) == \
                            len((r.get("after") or {}).get("windows") or []) \
                            and not any(e in NOT_A_TOGGLE for e in (r.get("effects") or [])):
                        toggles.append(r["trigger"])

    def diff(x):
        return len({norm_layout_key(k) for k in ref} ^ {norm_layout_key(k) for k in x})
    for trig in toggles:
        missing = diff(cur)
        press_item(ctl.pid(), trig)
        time.sleep(0.8)
        now = layout_keys(ctl.pid())
        if diff(now) < missing:
            log["pressed"].append(trig["key"])
            cur = now
        else:
            press_item(ctl.pid(), trig)
            time.sleep(0.8)
            cur = layout_keys(ctl.pid())
        if same_layout(cur, ref):
            break
    log["after"] = cur
    return log


def probe_app(name, bid, out, ex):
    reset_probe_docs()
    docs = probe_docs(bid)
    quittable = bid != "com.apple.finder"
    ctl = AppCtl2(bid, quittable)
    layout_log = restore_layout(ctl, bid, docs, out)
    print(f"   layout: {layout_log}", flush=True)
    dump = dump_one2(bid, docs, ctl)
    dump["layout_restore"] = layout_log
    if "error" in dump:
        return dump, [], {}
    plan, skipped = E.probe_plan(dump, dump.get("open_marks"), refused=load_auth_refused(out).get(bid, {}))
    leaf = {m["key"]: m for m in C._leaf_menu(dump)}
    probes = []
    t_app = time.perf_counter()
    need_reset = True
    pid = ctl.pid()
    chain_start = {}
    for it in plan:
        if G.locked():
            probes.append({"key": it.key, "error": "lock screen: stopped"})
            break
        if time.perf_counter() - t_app > PROBE_BUDGET_S:
            skipped[it.key] = "not probed: per-app probe budget used up"
            continue
        row = dict(it.to_dict(), pressed_path=E.path_key(it.trigger["index_path"]))
        t0 = time.perf_counter()
        try:
            if need_reset or ctl.pid() is None:
                reset_probe_docs()
                pid = ctl.restart(docs)
                if docs and os.path.isfile(docs[0]):
                    dirty_document(pid)
                clear_selection(ctl.pid())
                need_reset = False
            pid = ctl.pid()
            top = it.trigger["index_path"][0]
            before = snapshot(pid, docs, top)
            tp = time.perf_counter()
            enabled, why = press_item(pid, it.trigger)
            row["ms_press"] = round(time.perf_counter() - tp + 2.0, 1)
            if why:
                row.update(enabled=None, error=why)
            else:
                row["enabled"] = bool(enabled)
                if enabled:
                    after = snapshot(ctl.pid() or pid, docs, top)
                    if after.get("auth_prompt"):
                        row["auth_cancel"] = cancel_auth(auth_prompts(ctl.pid() or pid))
                        record_auth_refused(out, bid, it.trigger, AUTH_REFUSAL)
                        need_reset = True
                        if auth_prompts(ctl.pid() or pid):
                            row["auth_left_open"] = True
                            print("AUTH PROMPT COULD NOT BE CANCELLED: stopping", flush=True)
                            probes.append(row)
                            raise SystemExit(5)
                    after["frontmost_during_press"] = list(PRESS_FRONTS)
                    after["frontmost_source"] = "ax_system_wide"
                    row.update(before=before, after=after)
                    members = [E.path_key(leaf[k]["index_path"]) for k in it.run_members if k in leaf]
                    cl = E.classify(before, after, row["pressed_path"], members)
                    if "entry_focused" in cl["effects"] and "object_created" not in cl["effects"] \
                            and "window_opened" not in cl["effects"] and "auth_required" not in cl["effects"] \
                            and not auth_prompts(ctl.pid() or pid):
                        # a naming step: commit a probe name, then look again
                        entry = G.focused_entry(ctl.pid() or pid)
                        if entry is not None:
                            drv = G.Driver({"recipe": {}}, ctl, ex, {})
                            drv.enter(ctl.pid() or pid, entry, f"gen2probe-{len(probes)}", "entry", commit=True)
                            time.sleep(1.0)
                            committed = snapshot(ctl.pid() or pid, docs, None)
                            row["after_commit"] = committed
                            if "object_created" in E.classify(before, committed)["effects"]:
                                cl["effects"].append("object_created_on_commit")
                    row["effects"] = cl["effects"]
                    if "document_saved" in cl["effects"] and "auth_required" not in cl["effects"]:
                        row["control_saved"] = idle_control(ctl, docs, bid, row["ms_press"] if "ms_press" in row
                                                            else 3.0)
                    non_mark = [e for e in cl["effects"] if not e.startswith("mark_")]
                    if "layout_changed" in cl["effects"] and ctl.pid() is not None:
                        # a pane toggle persists across restarts: press it again
                        press_item(ctl.pid(), it.trigger)
                        row["layout_restored"] = set(snapshot(ctl.pid(), docs)["containers"]) == \
                            set(before["containers"])
                    if non_mark or ctl.pid() is None or row.get("control_saved") is not None:
                        need_reset = True
                    if cl["effects"] and not non_mark and it.kind == "run":
                        # chain through the group: the next member's probe
                        # starts where this one ended (more transitions, so
                        # more coupled settings seen); restore after the last
                        chain_start.setdefault(it.run, (before["marks"], members,
                                                        {E.path_key(leaf[k]["index_path"]): leaf[k]
                                                         for k in it.run_members if k in leaf}))
                    if it.kind == "run" and it.key == it.run_members[-1] and it.run in chain_start:
                        b0, mem0, byp0 = chain_start.pop(it.run)
                        cur = read_top_marks(ctl.pid(), it.trigger["index_path"][0]) or {}
                        restore = _restore_to(b0, cur, mem0, byp0)
                        for trig in restore:
                            press_item(ctl.pid(), trig)
                        row["restored"] = [t["key"] for t in restore]
        except Exception as exc:  # noqa: BLE001
            row.update(error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            need_reset = True
        row["ms"] = G._ms(t0)
        probes.append(row)
        print(f"   {it.key:40s} {row.get('effects', row.get('error', 'disabled' if row.get('enabled') is False else ''))}",
              flush=True)
    if quittable:
        G.ddg._quit(bid)
    else:
        ctl.quit()
    return dump, probes, skipped


def idle_control(ctl, docs, bid, wait):
    """The same probe start and the same wait, without the press: did the
    document change on disk anyway (autosave)?"""
    reset_probe_docs()
    pid = ctl.restart(docs)
    if docs and os.path.isfile(docs[0]):
        dirty_document(pid)
    before = model_state(pid, docs)
    time.sleep(max(3.0, wait))
    after = model_state(pid, docs)
    return (before or {}).get("sha") != (after or {}).get("sha")


def _restore_to(b0, a, members, by_path):
    """Presses that bring a group's marks back to ``b0``."""
    if not isinstance(b0, dict) or not isinstance(a, dict):
        return []
    was = [p for p in members if b0.get(p)]
    now = [p for p in members if a.get(p)]
    if set(was) == set(now):
        return []
    if len(was) == 1 and len(now) == 1 and was[0] in by_path:
        return [C._trigger_recipe(by_path[was[0]])]
    return [C._trigger_recipe(by_path[p]) for p in members
            if bool(b0.get(p)) != bool(a.get(p)) and p in by_path]


def _restore_marks(b, a, pressed, members, by_path):
    if not isinstance(b, dict) or not isinstance(a, dict):
        return []
    was = [p for p in members if b.get(p)]
    if a.get(pressed) and not b.get(pressed) and len(was) == 1 and was[0] != pressed and was[0] in by_path:
        return [C._trigger_recipe(by_path[was[0]])]
    if bool(a.get(pressed)) != bool(b.get(pressed)) and pressed in by_path:
        return [C._trigger_recipe(by_path[pressed])]
    return []


def calibrate_semantics(bid, cand, ex):
    """Measure the app's own search rule on calibration files (never the
    proof inputs) in a disposable folder: E.CALIBRATION_FILES, each pattern
    of E.CALIBRATION_PATTERNS searched once through the candidate's trigger."""
    docs = [CALIB]
    if bid != "com.apple.finder" and not probe_docs(bid)[:1]:
        return None, "no backend reachable: the search scope is not a file-system folder", {}
    if os.path.isdir(CALIB):
        shutil.rmtree(CALIB)
    os.makedirs(CALIB)
    for n, body in E.CALIBRATION_FILES.items():
        with open(os.path.join(CALIB, n), "w") as fh:
            fh.write(body)
    subprocess.run(["/usr/bin/mdimport", CALIB], capture_output=True, timeout=60)
    time.sleep(5)
    ctl = G.AppCtl(bid, bid != "com.apple.finder")
    drv = G.Driver(cand, ctl, ex, {})
    drv.docs = docs
    seen = {}
    for pat in E.CALIBRATION_PATTERNS:
        pid = ctl.restart(docs)
        before = {k: sorted(set(item_labels(el))) for k, el in containers(pid)}
        err = drv.press_trigger(pid, cand["recipe"]["trigger"], "trigger")
        if err:
            return None, f"calibration: {err['error']}", seen
        entry = G.focused_entry(pid)
        if entry is None:
            return None, "calibration: no focused search entry after the trigger", seen
        err = drv.enter(pid, entry, pat, "entry", commit=True)
        if err:
            return None, f"calibration: {err['error']}", seen
        time.sleep(3.0)
        after = {k: sorted(set(item_labels(el))) for k, el in containers(pid)}
        names = sorted({v for k, vs in after.items() if before.get(k) != vs for v in vs})
        seen[pat] = names
    if bid == "com.apple.finder":
        ctl.quit()
    else:
        G.ddg._quit(bid)
    sem, why = E.classify_semantics({p: [n for n in v] for p, v in seen.items()})
    if sem == "spotlight":
        ok = subprocess.run(["/usr/bin/mdfind", "-onlyin", CALIB, E.CALIBRATION_PATTERNS[0]],
                            capture_output=True, text=True, timeout=60)
        idx = sorted(os.path.basename(x) for x in ok.stdout.split("\n") if x.strip())
        if idx != E.expected_calibration("spotlight", E.CALIBRATION_PATTERNS[0]):
            return None, f"{why}; but the system index backend answers {idx}: no backend with the same rule", seen
    return sem, why, seen


def cmd_probe(args):
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(GEN2, exist_ok=True)
    reset_probe_docs()
    G.prepare_start_documents()
    subprocess.run(["/usr/bin/mdimport", G.FINDER_DIR], capture_output=True, timeout=60)
    only = {x for x in (args.only or "").split(",") if x}
    remap = {x for x in (args.remap or "").split(",") if x}
    only |= remap
    apps = []
    ex = G.Exec()
    for name, bid in APPS2:
        if only and name not in only:
            continue
        if G.locked():
            print("SCREEN LOCKED: stopping")
            break
        print(f"== {name} {bid}", flush=True)
        t0 = time.perf_counter()
        if bid == G.ddg.BLENDER_BID:
            # Blender: the desktop_gen dump (bpy catalog) and its structural bpy
            # mapping; menus of a GUI Blender are not AX menus
            with open(os.path.expanduser("~/anyuse-guest/desktop_gen/dump_org.blenderfoundation.blender.json")) as fh:
                dump = json.load(fh)
            cands, _c = C.map_contracts(dump)
            cands = [c for c in cands if c.recipe.get("backend") == "bpy"]
            counts = E._counts(dump, cands, {}, [])
            apps.append({"app": name, "bundle_id": bid, "quittable": False, "counts": counts,
                         "candidates": [c.to_dict() for c in cands]})
            continue
        try:
            if name in remap:
                # re-map an earlier probe's saved rows with the current
                # (pure) classification; nothing is pressed again
                with open(os.path.join(args.out, f"dump2_{bid}.json")) as fh:
                    dump = json.load(fh)
                with open(os.path.join(args.out, f"probes_{bid}.json")) as fh:
                    pj = json.load(fh)
                probes, skipped = pj["probes"], pj["not_probed"]
            else:
                dump, probes, skipped = probe_app(name, bid, args.out, ex)
        except Exception as exc:  # noqa: BLE001
            dump, probes, skipped = {"bundle_id": bid, "error": f"{type(exc).__name__}: {exc}",
                                     "traceback": traceback.format_exc()}, [], {}
        if name not in remap:
            with open(os.path.join(args.out, f"dump2_{bid}.json"), "w") as fh:
                json.dump(dump, fh, indent=1, default=str)
            with open(os.path.join(args.out, f"probes_{bid}.json"), "w") as fh:
                json.dump({"probes": probes, "not_probed": skipped}, fh, indent=1, default=str)
        if "error" in dump:
            print("  ERROR", dump["error"])
            apps.append({"app": name, "bundle_id": bid, "error": dump["error"]})
            continue
        cands, counts = E.map_from_probes(dump, [p for p in probes if "before" in p or p.get("enabled") is False
                                                 or p.get("error")])
        # role-mapped controls (field_rw / property_update), as in desktop_gen: mapped, not probed, not measured
        struct, _c = C.map_contracts(dump)
        cands += [c for c in struct if c.contract in ("field_rw", "property_update")]
        counts = E._counts(dump, cands, counts["per_item"], probes)
        for c in cands:
            if c.contract == "scoped_find":
                sem, why, seen = calibrate_semantics(bid, c.to_dict(), ex)
                c.recipe.update(semantics=sem, semantics_why=why, calibration=seen)
                c.provable = sem is not None
                c.note = why
        apps.append({"app": name, "bundle_id": bid, "quittable": bid != "com.apple.finder", "counts": counts,
                     "not_probed": skipped, "candidates": [c.to_dict() for c in cands],
                     "probe_s": round(time.perf_counter() - t0, 1)})
        print(f"  {({k: v for k, v in counts.items() if k not in ('per_item',)})}", flush=True)
    path = os.path.join(args.out, "contracts2.json")
    if only and os.path.exists(path):
        # a partial re-probe replaces only its apps, in the original order
        with open(path) as fh:
            prev = json.load(fh)["apps"]
        mine = {a["app"]: a for a in apps}
        apps = [mine.pop(a["app"], a) for a in prev] + list(mine.values())
    with open(path, "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "apps": apps}, fh, indent=1,
                  default=str)
    return 0


# ── proof drivers ───────────────────────────────────────────────────────────

class ObjectCreate2(G.Driver):
    """object_create observed through the model: the folder on disk when the
    scope is a folder, else every row (AXRows, not the viewport) of the
    containers the probe saw grow; preserved state from the model (folder
    listing and/or scope counts), never from the visible rows."""

    def observe(self, pid, complete=True):
        model = model_state(pid, self.docs)
        if model and model.get("kind") == "folder":
            names = list(model["items"])
        else:
            keys = set(self.c["recipe"].get("containers") or [])
            names = sorted({v for k, el in containers(pid) if not keys or k in keys for v in item_labels(el)})
        return {"identity": G.app_identity(pid), "complete": complete and bool(names),
                "target": {"names": sorted(set(names))}, "sentinel": None, "model": model}

    def start(self, inp=None, run=None):
        pid = self.reset()
        return self.observe(pid)

    def act(self, inp, run):
        pid = self.app.pid()
        err = self.press_trigger(pid, self.c["recipe"]["trigger"], "trigger")
        if err:
            return err
        entry = G.focused_entry(pid)
        if entry is None:
            return {"error": "no focused text entry after the trigger"}
        err = self.enter(pid, entry, C.object_name(inp, run), "entry", commit=True)
        if err:
            return err
        time.sleep(1.0)
        pid = self.app.restart(self.docs)
        time.sleep(1.0)
        return self.observe(pid)


class ScopedFind2(G.ScopedFind):
    """scoped_find with the measured semantics; the result names are every
    item (all rows) of the containers the search changed."""

    def start(self, inp=None, run=None):
        pid = self.reset()
        self._before = {k: sorted(set(item_labels(el))) for k, el in containers(pid)}
        return {"identity": G.app_identity(pid), "complete": True,
                "target": {"names": [], "scope": self._declared_scope(self._before)}, "sentinel": None}

    def act(self, inp, run):
        pid = self.app.pid()
        before = self._before
        err = self.press_trigger(pid, self.c["recipe"]["trigger"], "trigger")
        if err:
            return err
        entry = G.focused_entry(pid)
        if entry is None:
            return {"error": "no focused search entry after the trigger"}
        err = self.enter(pid, entry, str(inp), "entry", commit=True)
        if err:
            return err
        time.sleep(3.0)
        after = {k: sorted(set(item_labels(el))) for k, el in containers(pid)}
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        names = sorted({v for vs in changed.values() for v in vs})
        self.ev.setdefault("search_views", []).append({"input": inp, "run": run, "changed": list(changed),
                                                        "names": names})
        return {"identity": G.app_identity(pid), "complete": bool(changed),
                "target": {"names": names, "scope": self._declared_scope(before)}, "sentinel": None}

    def backend(self, inp, run):
        """The direct side: a backend with the SAME rule (pure rule over the
        folder listing, or the system index for spotlight), read right after
        the UI run."""
        sem = self.c["recipe"]["semantics"]
        folder = next((d for d in self.docs if os.path.isdir(d)), None)
        scope = sorted(n for n in os.listdir(folder) if not n.startswith("."))
        if sem == "spotlight":
            r = subprocess.run(["/usr/bin/mdfind", "-onlyin", folder, str(inp)], capture_output=True, text=True,
                               timeout=60)
            names = sorted({os.path.basename(x) for x in r.stdout.split("\n") if x.strip()
                            and os.path.dirname(x.rstrip("/")) == os.path.realpath(folder)})
        else:
            names = E.pure_backend(sem)(scope, str(inp))
        return {"identity": {"bundle_id": self.app.bid, "window": None, "document": None}, "complete": True,
                "target": {"names": names, "scope": scope}, "sentinel": None}


def read_group_marks2(pid, parent_path, options):
    """G.read_group_marks, with the sentinel items of a submenu written as
    ``parent identity>item identity`` (so a submenu can be declared as one
    coupled settings group)."""
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
                sub = [m for m in (_at(k, "AXChildren") or []) if _s(_at(m, "AXRole")) == "AXMenu"]
                pid_ = f"{it['identifier']}|{it['cmd']}|{it['mods']}"
                for m in sub:
                    walk(m, depth + 1, pid_)
            elif r == "AXMenu":
                walk(k, depth + 1, parent)
    walk(bar, 0, None)
    _AS().AXUIElementPerformAction(bar, "AXCancel")
    time.sleep(0.4)
    opt_keys, selected, used = [], [], set()
    for o in options:
        hit = [i for i, it in enumerate(items) if G.trigger_matches(it, o)]
        if len(hit) == 1:
            opt_keys.append(o["key"])
            used.add(hit[0])
            if items[hit[0]]["mark"].strip():
                selected.append(o["key"])
    others = sorted(((it["parent"] + ">") if it["parent"] else "") + f"{it['identifier']}|{it['cmd']}|{it['mods']}"
                    for i, it in enumerate(items) if i not in used and it["mark"].strip())
    return {"options": opt_keys, "selected": selected}, others


class ToggleSelect2(G.ToggleSelect):
    def marks(self, pid):
        t, others = read_group_marks2(pid, self.parent, self.options)
        if t is None:
            return {"error": "menu not readable"}
        return {"identity": G.app_identity(pid), "complete": True, "target": t, "sentinel": others}


class FlagToggle(G.Driver):
    def __init__(self, *a):
        super().__init__(*a)
        self.options = self.c["recipe"]["options"]
        self.parent = self.c["recipe"]["parent_index_path"]
        self.declared = None

    def marks(self, pid):
        t, others = read_group_marks2(pid, self.parent, self.options)
        if t is None:
            return {"error": "menu not readable"}
        return {"identity": G.app_identity(pid), "complete": True,
                "target": {"options": t["options"], "marked": sorted(t["selected"])}, "sentinel": others}

    def start(self, inp=None, run=None):
        pid = self.reset()
        obs = self.marks(pid)
        if "error" in obs:
            return obs
        if self.declared is None:
            self.declared = list(obs["target"]["marked"])
            return obs
        for k in sorted(set(obs["target"]["marked"]) ^ set(self.declared)):
            err = self.press_trigger(pid, next(o for o in self.options if o["key"] == k), "setup:" + k)
            if err:
                return err
        return self.marks(pid)

    def act(self, inp, run):
        pid = self.app.pid()
        err = self.press_trigger(pid, next(o for o in self.options if o["key"] == inp), "option:" + inp)
        if err:
            return err
        obs = self.marks(pid)
        pid = self.app.restart(self.docs)
        after = self.marks(pid)
        self.ev.setdefault("persistence", []).append({
            "input": inp, "run": run, "marked_before_restart": (obs.get("target") or {}).get("marked"),
            "marked_after_restart": (after.get("target") or {}).get("marked")})
        return obs


DRIVERS2 = {k: type(v.__name__ + "Guarded", (AuthGuard, v), {}) for k, v in {
    "doc_save": G.DocSave, "object_create": ObjectCreate2, "scoped_find": ScopedFind2,
    "toggle_select": ToggleSelect2, "flag_toggle": FlagToggle}.items()}


def contract_for(c):
    if c["contract"] == "scoped_find":
        sem = c["recipe"].get("semantics")
        return C.scoped_find_contract(sem, E.pure_backend(sem))
    return C.CONTRACTS[c["contract"]]


def measured2(apps):
    """The fixed measured set: every provable candidate of the five
    measured classes (settings groups: the first MAX_SETTING_GROUPS per app
    in menu order), plus Blender's bpy object_create. scoped_find whose
    semantics could not be matched to a backend is REFUSED (listed apart)."""
    out, refused = [], []
    for a in apps:
        if "candidates" not in a:
            continue
        groups = 0
        for c in sorted(a["candidates"], key=lambda c: (c["recipe"].get("trigger") or
                                                        (c["recipe"].get("options") or [{}])[0]).get("index_path") or []):
            if c["contract"] not in MEASURED2:
                continue
            if c["recipe"].get("backend") == "bpy":
                out.append((a, c))
                continue
            if c["contract"] == "doc_save" and not any(os.path.isfile(d) for d in G.start_docs(a["bundle_id"])):
                refused.append({"id": c["id"], "reason": "no disposable document of this app to save into"})
                continue
            if c["contract"] == "scoped_find" and not c["recipe"].get("semantics"):
                refused.append({"id": c["id"], "reason": c.get("note")})
                continue
            if not c["provable"]:
                continue
            if c["contract"] in ("toggle_select", "flag_toggle"):
                groups += 1
                if groups > MAX_SETTING_GROUPS:
                    continue
            out.append((a, c))
    return out, refused


def start_docs2(bid):
    return G.start_docs(bid)


def prove_ax2(a, c, ex, lang=None, recorded=None):
    ev = {}
    app = G.AppCtl(a["bundle_id"], a["quittable"], lang)
    drv = DRIVERS2[c["contract"]](c, app, ex, ev)
    contract = contract_for(c)
    side = list(c["recipe"].get("side_effects") or [])
    t0 = time.perf_counter()
    if c["contract"] in ("toggle_select", "flag_toggle"):
        pid = app.restart(drv.docs)
        keys = [o["key"] for o in c["recipe"]["options"]]
        if c["contract"] == "toggle_select":
            rec, unseen, why = drv.choose_inputs(pid)
            if rec is None:
                return {"id": c["id"], "status": "not proved", "reason": why, "evidence": ev}
            drv.rec_key = rec
            err = drv.select(app.pid(), rec, "option:" + rec)
            rec_obs = drv.marks(app.pid()) if not err else err
        else:
            rec, unseen = keys[0], keys[1:3]
            drv.start()                                   # declares the start marks
            rec_obs = drv.act(rec, 0)
        rec_input, inputs = rec, unseen
    else:
        rec_input, inputs = REC_INPUT2[c["contract"]], PROOF_INPUTS2[c["contract"]]
        drv.start()
        rec_obs = drv.act(rec_input, 0)
    if c["contract"] == "flag_toggle":
        drv.declared = None
    start = drv.start()
    result = {"id": c["id"], "app": a["app"], "contract": c["contract"], "triggers": c["triggers"],
              "side_effects": side, "semantics": c["recipe"].get("semantics"),
              "recording_input": rec_input, "recording_observation": rec_obs, "recorded": drv.rec,
              "declared_start": start, "inputs": inputs, "record_ms": G._ms(t0)}
    if not isinstance(start, dict) or contract.validate(start):
        result.update(status="not proved", reason=f"declared start not observable: "
                                                  f"{contract.validate(start) if isinstance(start, dict) else start}")
        return result
    n0 = len(ex.log)
    t0 = time.perf_counter()
    direct = drv.backend if c["contract"] == "scoped_find" else None
    seen = [rec_input] + SEEN2.get(c["contract"], [])
    rep = C.prove_contract(contract, c["id"], inputs, 3, lambda i, r: drv.act(i, r), recorded_inputs=seen,
                           start_state=start, observe_start=lambda i, r: drv.start(i, r), direct_fn=direct,
                           side_effects=side)
    d = rep.to_dict()
    rows = d["rows"]
    result.update(status="proved" if rep.proved else "not proved", proof=d, prove_ms=G._ms(t0),
                  rows=len(rows), rows_agree=sum(1 for r in rows if r["agree"]),
                  wrong_target_rows=sum(1 for r in rows if r["mismatch"] and
                                        any(m in r["mismatch"] for m in WRONG_TARGET_MARKS2)),
                  refused_rows=sum(1 for r in rows if isinstance(r["ui_value"], dict)
                                   and r["ui_value"].get("refused")),
                  executor_steps=len(ex.log) - n0,
                  executor_refusals=sum(1 for x in ex.log[n0:] if x["refused"]), evidence=ev)
    return result


def cmd_prove(args):
    with open(os.path.join(args.out, "contracts2.json")) as fh:
        apps = json.load(fh)["apps"]
    only = {x.strip() for x in (args.only or "").split(",") if x.strip()}
    G.prepare_start_documents()
    subprocess.run(["/usr/bin/mdimport", G.FINDER_DIR], capture_output=True, timeout=60)
    ex = G.Exec()
    todo, refused = measured2(apps)
    results = [{"id": r["id"], "status": "refused", "reason": r["reason"]} for r in refused]
    G.BL_INPUTS[:] = BL_INPUTS2
    G.BL_SEEN[:] = BL_SEEN2
    restored = {}
    for a, c in todo:
        if only and not any(o in c["id"] for o in only):
            continue
        if a["bundle_id"] not in restored and a["bundle_id"] != G.ddg.BLENDER_BID:
            ctl = G.AppCtl(a["bundle_id"], a["quittable"])
            restored[a["bundle_id"]] = restore_layout(ctl, a["bundle_id"], G.start_docs(a["bundle_id"]), args.out)
            print(f"   layout {a['bundle_id']}: {restored[a['bundle_id']]}", flush=True)
            results.append({"id": f"layout:{a['bundle_id']}", "status": "setup", "layout": restored[a["bundle_id"]]})
        if G.locked():
            print("SCREEN LOCKED: stopping")
            results.append({"id": c["id"], "status": "stopped", "reason": "lock screen"})
            break
        print(f"== {c['id']}", flush=True)
        t0 = time.perf_counter()
        try:
            if c["recipe"].get("backend") == "bpy":
                r = G.prove_blender(a, c, args.out)
            else:
                r = prove_ax2(a, c, ex)
        except Exception as exc:  # noqa: BLE001
            r = {"id": c["id"], "status": "error", "reason": f"{type(exc).__name__}: {exc}",
                 "traceback": traceback.format_exc()}
        r["total_ms"] = G._ms(t0)
        results.append(r)
        print(f"   {r.get('status')} rows={r.get('rows_agree')}/{r.get('rows')} "
              f"wrong_target={r.get('wrong_target_rows')} {r.get('reason', '')}", flush=True)
        if a["bundle_id"] not in ("com.apple.finder", G.ddg.BLENDER_BID):
            G.ddg._quit(a["bundle_id"])
        with open(os.path.join(args.out, f"proved2{'_' + args.tag if args.tag else ''}.json"), "w") as fh:
            json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results}, fh,
                      indent=1, default=str)
    return 0


def cmd_lang(args):
    """Every proved AX candidate once, English UI, German-recorded selectors,
    first unseen input, one run (run index 7)."""
    latest = {}
    for name in ["proved2.json"] + sorted(f for f in os.listdir(args.out)
                                          if f.startswith("proved2_") and f.endswith(".json")):
        with open(os.path.join(args.out, name)) as fh:
            for r in json.load(fh)["results"]:
                if r.get("status") != "setup":
                    latest[r["id"]] = r          # a later tagged re-proof replaces the earlier row
    proved = [r for r in latest.values() if r.get("status") == "proved" and "recorded" in r]
    with open(os.path.join(args.out, "contracts2.json")) as fh:
        apps = {a["bundle_id"]: a for a in json.load(fh)["apps"]}
    ex = G.Exec()
    out = []
    for r in proved:
        bid = r["id"].split(":")[0]
        a = apps[bid]
        c = next(x for x in a["candidates"] if x["id"] == r["id"])
        if not a.get("quittable"):
            out.append({"id": r["id"], "status": "n/a", "reason": "not relaunchable with launch arguments"})
            continue
        if G.locked():
            out.append({"id": r["id"], "status": "stopped", "reason": "lock screen"})
            break
        ev = {}
        app = G.AppCtl(bid, True, lang="en")
        drv = DRIVERS2[c["contract"]](c, app, ex, ev)
        drv.rec = r["recorded"]
        contract = contract_for(c)
        side = list(c["recipe"].get("side_effects") or [])
        if c["contract"] == "toggle_select":
            drv.rec_key = r["recording_input"]
        inp = r["inputs"][0]
        try:
            start = drv.start()
            pid = app.pid()
            bar = [_s(_at(k, "AXTitle")) for k in (_at(_at(_AS().AXUIElementCreateApplication(pid), "AXMenuBar"),
                                                       "AXChildren") or [])][:4] if pid else []
            n0 = len(ex.log)
            obs = drv.act(inp, 7) if not (isinstance(start, dict) and start.get("error")) else start
            why = contract.validate(obs) or contract.validate(start) or \
                contract.expect(start, inp, 7, obs) or contract.preserve(start, inp, 7, obs, side)
            if why is None and c["contract"] == "scoped_find":
                d = drv.backend(inp, 7)
                why = None if contract.equal_fn(obs, d) else f"differs from the backend: {d['target']['names']}"
        except Exception as exc:  # noqa: BLE001
            obs, bar, why, n0 = {"error": str(exc)}, [], f"{type(exc).__name__}: {exc}", len(ex.log)
        out.append({"id": r["id"], "input": inp, "menu_bar": bar, "met_contract": why is None, "why": why,
                    "observation": obs, "executor": ex.log[n0:], "evidence": ev})
        print(f"== {r['id']} en: {'OK' if why is None else why} bar={bar}", flush=True)
        G.ddg._quit(bid)
    with open(os.path.join(args.out, "localisation2.json"), "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": out}, fh, indent=1,
                  default=str)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("inspect", "check", "probe", "prove", "lang"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--remap", default="")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    refuse_unless_in_a_guest()
    if G.locked():
        print("SCREEN LOCKED: stopping before anything runs")
        return 3
    from anyuse.context import accessibility as acc
    if not acc.ax_trusted():
        raise SystemExit("REFUSING TO RUN: Accessibility is not granted.")
    os.makedirs(G.GEN, exist_ok=True)
    if args.cmd not in ("inspect", "check"):
        from anyuse.settings import get as get_setting
        if not get_setting("ANYUSE_DRIVE_ENABLED"):
            raise SystemExit("ANYUSE_DRIVE_ENABLED is off")
        try:
            from anyuse.context import keyboard as kb
            kb.release_modifiers()
        except Exception:  # noqa: BLE001
            pass
    return {"inspect": cmd_inspect, "check": cmd_check, "probe": cmd_probe, "prove": cmd_prove,
            "lang": cmd_lang}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
