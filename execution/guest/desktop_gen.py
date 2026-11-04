#!/usr/bin/env python3
"""Generic desktop contracts, guest side. INSIDE THE GUEST VM ONLY.

 The drivers below know CLASSES, not
apps: every trigger is resolved by the identity discovery recorded
(AXIdentifier and/or command character + modifiers, never a title or a
position), every actuation is one ``StateGraphExecutor.execute_checked_step``
through ``execution/ax_actuator.ExecutorActuator`` (identity refusal against the
recorded selector), and every row is judged by
``desktop_contracts.prove_contract`` -> ``desktop_compile.prove``.

Subcommands:
  dump   --out DIR            read-only dump of the six apps (+ menu marks), map to contracts
  record --out DIR [--only]   record step only (recording inputs), for development probes
  prove  --out DIR [--only]   record + prove every measured candidate (fixed set)
  lang   --out DIR            the localisation run (English UI, German-recorded selectors)
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time
import traceback
import urllib.parse
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flows import refuse_unless_in_a_guest   # noqa: E402
import discover_desktop as ddg                 # noqa: E402  (guest-side readers)

from anyuse.execution import desktop_contracts as C   # noqa: E402  (pure)
from anyuse.execution import desktop_discover as dd   # noqa: E402  (pure)
from anyuse.execution import desktop_hygiene as H    # noqa: E402  (pure)

GEN = os.path.expanduser("~/anyuse-guest/scratch/desktop_gen")
TE_TARGET = os.path.join(GEN, "te_target.txt")
TE_SENTINEL = os.path.join(GEN, "te_sentinel.txt")
FINDER_DIR = os.path.join(GEN, "finder")
PREVIEW_PNG = os.path.join(GEN, "preview.png")
SEED = "seed\n"
SENTINEL = "sentinel document\n"
FINDER_SEEDS = ["kiwano_pie.txt", "salak_tart.md", "mangosteen_roll.txt", "blueberry_jam.txt",
                "elderberry_wine.txt", "rambutan_pie.txt", "gen_sentinel.txt"]

APPS = [("TextEdit", "com.apple.TextEdit"), ("Notizen", "com.apple.Notes"),
        ("Finder", "com.apple.finder"), ("Rechner", "com.apple.calculator"),
        ("Vorschau", "com.apple.Preview"), ("Blender", ddg.BLENDER_BID)]
MEASURED = ("doc_save", "object_create", "scoped_find", "toggle_select")
MAX_GROUPS = 2

# ── fixed inputs ────────────────────────────────────────────────────────────
REC_INPUT = {"doc_save": "gen-alpha", "object_create": "genrec", "scoped_find": "tart"}
PROOF_INPUTS = {"doc_save": ["gen-kiwano 17", "gen-salak 42"],
                "object_create": ["genmango", "genlychee"],
                "scoped_find": ["pie", "berry"]}
SEEN = {"doc_save": ["alpha", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "kappa"],
        "object_create": [], "scoped_find": []}
BL_REC = (2.5, -1.0, 0.75)
BL_INPUTS = [(0.75, -3.5, 2.25), (-2.0, 1.5, -0.25)]
BL_SEEN = [(2.5, -1.0, 0.75), (1.0, 2.0, -3.0), (-0.5, 4.25, 0.5), (3.0, -2.5, 1.25), (-1.5, 0.5, 2.0)]

RETURN = 36
TEXT_ROLES = ("AXTextField", "AXTextArea", "AXComboBox")


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000.0, 1)


def _AS():
    import ApplicationServices as A
    return A


def _at(el, name):
    return ddg._at(el, name)


def _s(v):
    return None if v is None else str(v)


def ax_plain(v):
    """An AX attribute value as plain, comparable data.
    An AXValue (point, size, rect, range) becomes ``(kind, numbers...)``
    through AXValueGetValue, else through its text without the wrapper's
    memory address (``desktop_hygiene.ax_value_plain``): ``str()`` of an
    AXValue carries that address, so two reads of one frame compared as
    text differed whenever the allocator did not reuse it. Strings,
    numbers and None come back unchanged."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        A = _AS()
        t = A.AXValueGetType(v)
        kinds = {A.kAXValueCGPointType: "point", A.kAXValueCGSizeType: "size", A.kAXValueCGRectType: "rect",
                 A.kAXValueCFRangeType: "range"}
        if t in kinds:
            ok, x = A.AXValueGetValue(v, t, None)
            if ok:
                k = kinds[t]
                if k == "point":
                    nums = (x.x, x.y)
                elif k == "size":
                    nums = (x.width, x.height)
                elif k == "rect":
                    nums = (x.origin.x, x.origin.y, x.size.width, x.size.height)
                else:
                    nums = (x.location, x.length) if hasattr(x, "location") else tuple(x)
                return tuple([k] + [round(float(n), 3) for n in nums])
    except Exception:  # noqa: BLE001  (fall back to the text, address removed)
        pass
    return H.ax_value_plain(str(v))


def locked():
    import Quartz
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(d.get("CGSSessionScreenIsLocked"))


def _png(path, w=64, h=48):
    raw = b"".join(b"\x00" + bytes(v for x in range(w) for v in (x * 4 % 256, y * 5 % 256, 160))
                   for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(data)


def prepare_start_documents():
    os.makedirs(FINDER_DIR, exist_ok=True)
    for n in FINDER_SEEDS:
        p = os.path.join(FINDER_DIR, n)
        if not os.path.exists(p):
            with open(p, "w") as fh:
                fh.write(n + "\n")
    with open(TE_TARGET, "w") as fh:
        fh.write(SEED)
    with open(TE_SENTINEL, "w") as fh:
        fh.write(SENTINEL)
    if not os.path.exists(PREVIEW_PNG):
        _png(PREVIEW_PNG)


def start_docs(bid):
    return {"com.apple.TextEdit": [TE_TARGET, TE_SENTINEL], "com.apple.finder": [FINDER_DIR],
            "com.apple.Preview": [PREVIEW_PNG]}.get(bid, [])


# ── app control ─────────────────────────────────────────────────────────────

#: when set, every launched app's main window gets the screen's
#: visible frame (a settable AX attribute; no key, no click). Notes shows
#: its note list again only in a wide window.
CANONICAL_FRAME = [False]


def canonical_frame(pid):
    """The main window at the screen's visible frame through AXPosition /
    AXSize; a fixed-size window keeps its size. Returns the size read back."""
    try:
        import AppKit
        import Quartz
        win = front_window(pid)
        if win is None:
            return None
        sc = AppKit.NSScreen.mainScreen()
        vf, fr = sc.visibleFrame(), sc.frame()
        top = fr.size.height - (vf.origin.y + vf.size.height)        # AX y runs from the top
        AS = _AS()
        AS.AXUIElementSetAttributeValue(win, "AXPosition", AS.AXValueCreate(
            AS.kAXValueCGPointType, Quartz.CGPointMake(vf.origin.x, top)))
        AS.AXUIElementSetAttributeValue(win, "AXSize", AS.AXValueCreate(
            AS.kAXValueCGSizeType, Quartz.CGSizeMake(vf.size.width, vf.size.height)))
        time.sleep(0.8)
        return ax_plain(_at(win, "AXSize"))
    except Exception:  # noqa: BLE001
        return None


class AppCtl:
    """Launch/quit/reset one app. ``quittable`` comes from the app's own menu
    (a ⌘Q item), never from its bundle id."""

    def __init__(self, bid, quittable, lang=None):
        self.bid, self.quittable, self.lang = bid, quittable, lang

    def running(self):
        return ddg._running(self.bid)

    def pid(self):
        a = self.running()
        return int(a.processIdentifier()) if a is not None else None

    def _close_windows(self):
        pid = self.pid()
        if pid is None:
            return
        app = _AS().AXUIElementCreateApplication(pid)
        for w in list(_at(app, "AXWindows") or []):
            b = _at(w, "AXCloseButton")
            if b is not None:
                _AS().AXUIElementPerformAction(b, "AXPress")
                time.sleep(0.4)

    def quit(self):
        if self.quittable:
            ddg._quit(self.bid)
        else:
            self._close_windows()
            time.sleep(0.8)

    def launch(self, docs=(), wait=3.0):
        import AppKit
        docs = list(docs)
        args = ["/usr/bin/open", "-b", self.bid] + docs[:1]
        if self.lang:
            args += ["--args", "-AppleLanguages", f"({self.lang})"]
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
            if CANONICAL_FRAME[0]:
                canonical_frame(self.pid())
        return self.pid()

    def restart(self, docs=()):
        self.quit()
        return self.launch(docs)


def app_identity(pid):
    import AppKit
    a = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid)) if pid else None
    return {"bundle_id": str(a.bundleIdentifier()) if a is not None else "", "window": None, "document": None}


def _url_path(u):
    if not u:
        return None
    u = str(u)
    if u.startswith("file://"):
        u = urllib.parse.unquote(u[len("file://"):])
    return os.path.realpath(u.rstrip("/")) if u else None


# ── identity-based resolution (never a title, never a position) ─────────────

def item_identity(el):
    return {"role": _s(_at(el, "AXRole")) or "", "identifier": _s(_at(el, "AXIdentifier")) or "",
            "cmd": (_s(_at(el, "AXMenuItemCmdChar")) or "").lower(),
            "mods": str(ddg._int(_at(el, "AXMenuItemCmdModifiers")) or 0),
            "title": _s(_at(el, "AXTitle")) or "", "mark": _s(_at(el, "AXMenuItemMarkChar")) or ""}


def menu_items(pid):
    app = _AS().AXUIElementCreateApplication(pid)
    mb = _at(app, "AXMenuBar")
    out = []

    def walk(el, depth, path):
        if depth > 8:
            return
        for j, k in enumerate(_at(el, "AXChildren") or []):
            role = _s(_at(k, "AXRole"))
            if role == "AXMenuItem":
                out.append((k, path + [j]))
            if role in ("AXMenuItem", "AXMenu", "AXMenuBarItem"):
                walk(k, depth + 1, path + [j] if role != "AXMenu" else path)
    if mb is not None:
        walk(mb, 0, [])
    return out


def trigger_matches(ident, trig):
    if not trig.get("identifier") and not trig.get("cmd_char"):
        return False
    if trig.get("identifier") and ident["identifier"] != trig["identifier"]:
        return False
    if trig.get("cmd_char") and (ident["cmd"] != trig["cmd_char"] or ident["mods"] != trig["cmd_mods"]):
        return False
    if not trig.get("cmd_char") and trig.get("cmd_mods", "0") != "0" and ident["mods"] != trig["cmd_mods"]:
        return False
    return True


def resolve_trigger(pid, trig):
    """(element, identity, note). Exactly one live menu item must carry the
    recorded identity; 0 or >1 refuses."""
    hits = [(el, item_identity(el)) for el, _p in menu_items(pid)]
    hits = [(el, i) for el, i in hits if i["role"] == "AXMenuItem" and trigger_matches(i, trig)]
    if len(hits) != 1:
        return None, None, f"{len(hits)} live menu items carry the recorded identity {trig}"
    return hits[0][0], hits[0][1], None


def focused_entry(pid):
    app = _AS().AXUIElementCreateApplication(pid)
    el = _at(app, "AXFocusedUIElement")
    if el is None:
        return None
    role = _s(_at(el, "AXRole")) or ""
    return el if role in TEXT_ROLES else None


def front_window(pid):
    app = _AS().AXUIElementCreateApplication(pid)
    for a in ("AXFocusedWindow", "AXMainWindow"):
        w = _at(app, a)
        if w is not None:
            return w
    ws = _at(app, "AXWindows") or []
    return ws[0] if ws else None


def _label(el, depth=0):
    for a in ("AXTitle", "AXValue", "AXDescription"):
        v = _s(_at(el, a))
        role = _s(_at(el, "AXRole"))
        if v and v.strip() and role in ("AXStaticText", "AXTextField", "AXImage", "AXCell", "AXRow",
                                         "AXGroup", "AXButton") and (a != "AXDescription" or role == "AXImage"):
            return v.strip()
    if depth >= 4:
        return None
    for k in _at(el, "AXChildren") or []:
        v = _label(k, depth + 1)
        if v:
            return v
    return None


def container_labels(pid):
    """{container key: sorted primary labels} for every table/outline/list/
    browser in the front window. A row's primary label is its first
    non-empty title/value, depth-first."""
    win = front_window(pid)
    out = {}
    if win is None:
        return out
    n = [0]

    def walk(el, depth):
        if depth > 14 or n[0] > 4000:
            return
        n[0] += 1
        role = _s(_at(el, "AXRole")) or ""
        if role in C.CONTAINER_ROLES:
            key = f"{role}:{_s(_at(el, 'AXIdentifier')) or ''}:{len(out)}"
            labels = []
            for k in _at(el, "AXChildren") or []:
                kr = _s(_at(k, "AXRole")) or ""
                if kr in ("AXColumn", "AXScrollBar", "AXGroup") and kr != "AXGroup":
                    continue
                if kr == "AXGroup" and role != "AXList":
                    continue
                v = _label(k)
                if v:
                    labels.append(v)
            out[key] = sorted(set(labels))
            return
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    walk(win, 0)
    return out


def all_labels(pid):
    return sorted({v for vs in container_labels(pid).values() for v in vs})


def read_group_marks(pid, parent_path, options):
    """Open the group's top-level menu (a read: AXPress on its menu-bar item,
    then AXCancel), read every item's mark, close it. Returns (target,
    sentinel): the group's options/selected by identity, and every other
    marked item of that top-level menu (identity tuples)."""
    app = _AS().AXUIElementCreateApplication(pid)
    mb = _at(app, "AXMenuBar")
    bar = (_at(mb, "AXChildren") or [])[parent_path[0]] if mb is not None else None
    if bar is None:
        return None, None
    _AS().AXUIElementPerformAction(bar, "AXPress")
    time.sleep(0.8)
    items = []

    def walk(el, depth):
        if depth > 6:
            return
        for k in _at(el, "AXChildren") or []:
            r = _s(_at(k, "AXRole"))
            if r == "AXMenuItem":
                items.append(item_identity(k))
            if r in ("AXMenu", "AXMenuItem"):
                walk(k, depth + 1)
    walk(bar, 0)
    _AS().AXUIElementPerformAction(bar, "AXCancel")
    time.sleep(0.4)
    opt_keys, selected, used = [], [], set()
    for o in options:
        hit = [i for i, it in enumerate(items) if trigger_matches(it, o)]
        if len(hit) == 1:
            opt_keys.append(o["key"])
            used.add(hit[0])
            if items[hit[0]]["mark"].strip():
                selected.append(o["key"])
    others = sorted(f"{it['identifier']}|{it['cmd']}|{it['mods']}" for i, it in enumerate(items)
                    if i not in used and it["mark"].strip())
    return {"options": opt_keys, "selected": selected}, others


# ── the checked executor ────────────────────────────────────────────────────

class Exec:
    """One ExecutorActuator for the job; every actuation is a checked step."""

    def __init__(self):
        from anyuse.execution.ax_actuator import ExecutorActuator
        self.act = ExecutorActuator(view=None)
        self.log = []

    def _info(self, el, pid, selector):
        from anyuse.execution.ax_actuator import ElementInfo
        return ElementInfo(ref=el, role=_s(_at(el, "AXRole")) or "", label=_s(_at(el, "AXTitle")) or "",
                           selector=dict(selector), pid=pid)

    def run(self, el, pid, selector, action, **kw):
        t0 = time.perf_counter()
        info = self._info(el, pid, selector)
        tree, _ex = self.act._for(pid)
        if action == "enter_text" and (_s(_at(el, "AXSubrole")) == "AXSearchField" or kw.pop("keys", False)):
            # search fields act on typed keys only: the ax_value
            # rung would read back perfectly and do nothing
            orig = type(tree).enter_text
            tree.enter_text = lambda e, v, **k: orig(tree, e, v, rungs=("keystrokes",))
        try:
            res = self.act.run(info, action, **kw)
        finally:
            tree.__dict__.pop("enter_text", None)
        row = {"action": action, "success": bool(res.get("success")), "refused": bool(res.get("refused")),
               "outcome": res.get("outcome"), "error": res.get("error"), "ms": _ms(t0)}
        self.log.append(row)
        return row


def record_selector(pid, el):
    from anyuse.context import accessibility as acc
    return acc.AccessibilityTree(pid=pid).to_selector(el).to_dict()


# ── class drivers ───────────────────────────────────────────────────────────

class Driver:
    """Shared frame: reset -> observe start -> act -> observe."""

    def __init__(self, cand, app, ex, evidence):
        self.c, self.app, self.ex, self.ev = cand, app, ex, evidence
        self.rec = {}
        self.docs = start_docs(app.bid)

    def reset(self):
        if self.app.bid == "com.apple.TextEdit":
            # quit FIRST: a document the last run edited
            # without saving is autosaved on quit; replacing its file while
            # the app still holds it made TextEdit answer the quit with
            # "could not be autosaved, the file was changed by another
            # application" (a sheet without Cancel), the app never quit,
            # and the sheet refused every later row (18 rows). An app that
            # is still running after the quit keeps its files as they are
            # (the start then differs and the row is refused).
            self.app.quit()
            if self.app.running() is None:
                with open(TE_TARGET, "w") as fh:
                    fh.write(SEED)
                with open(TE_SENTINEL, "w") as fh:
                    fh.write(SENTINEL)
        return self.app.restart(self.docs)

    def press_trigger(self, pid, trig, name):
        """Open the item's top-level menu (navigation, as every recorded flow
        does: a raw AXPress on the menu-bar item at the discovered index —
        a position used only to OPEN a menu, never to identify what gets
        actuated), resolve the item by its recorded identity, press it as a
        checked executor step."""
        bar = None
        app_el = _AS().AXUIElementCreateApplication(pid)
        mb = _at(app_el, "AXMenuBar")
        kids = list(_at(mb, "AXChildren") or []) if mb is not None else []
        ip = trig.get("index_path") or []
        if ip and ip[0] < len(kids):
            bar = kids[ip[0]]
            _AS().AXUIElementPerformAction(bar, "AXPress")
            time.sleep(0.8)
        el, ident, why = resolve_trigger(pid, trig)
        if el is None:
            if bar is not None:
                _AS().AXUIElementPerformAction(bar, "AXCancel")
            return {"error": f"unresolved {name}: {why}"}
        if name not in self.rec:
            self.rec[name] = {"selector": record_selector(pid, el), "identity": ident}
        r = self.ex.run(el, pid, self.rec[name]["selector"], "press")
        time.sleep(1.2)
        if not r["success"] and bar is not None:
            _AS().AXUIElementPerformAction(bar, "AXCancel")
            time.sleep(0.3)
        if r["refused"]:
            return {"error": f"executor refused {name}: {r['error']}", "refused": True}
        return None

    def enter(self, pid, el, text, name, commit=False):
        if name not in self.rec:
            self.rec[name] = {"selector": record_selector(pid, el)}
        r = self.ex.run(el, pid, self.rec[name]["selector"], "enter_text", text=text)
        if r["refused"]:
            return {"error": f"executor refused {name}: {r['error']}", "refused": True}
        if commit:
            r2 = self.ex.run(el, pid, self.rec[name]["selector"], "press_key", keycode=RETURN)
            if r2["refused"]:
                return {"error": f"executor refused commit: {r2['error']}", "refused": True}
        time.sleep(0.8)
        return None


class DocSave(Driver):
    def observe(self, pid, doc=None, complete=True):
        ident = app_identity(pid)
        ident["document"] = doc
        return {"identity": ident, "complete": complete,
                "target": {"text": open(TE_TARGET).read() if doc else open(TE_TARGET).read()},
                "sentinel": open(TE_SENTINEL).read()}

    def start(self, inp=None, run=None):
        pid = self.reset()
        return self.observe(pid, os.path.realpath(TE_TARGET))

    def act(self, inp, run):
        pid = self.app.pid()
        from anyuse.context import accessibility as acc
        tree = acc.AccessibilityTree(pid=pid)
        area = None
        if "area" in self.rec:
            area = tree.find_selector(acc.Selector.from_dict(self.rec["area"]["selector"]),
                                      document=TE_TARGET)
        else:
            want = self.c["recipe"]["entry"]
            for el, d in [(e, tree._describe(e)) for e, _ in self._walk(tree)]:
                if d.role == want["role"] and (d.identifier or "") == want["identifier"] \
                        and _url_path(tree.element_window(el)[1]) == os.path.realpath(TE_TARGET):
                    area = el
                    break
        if area is None:
            return {"error": "no text area in the target document's window"}
        _title, docurl = tree.element_window(area)
        doc = _url_path(docurl)
        if doc != os.path.realpath(TE_TARGET):
            return {"error": f"text area belongs to {doc}"}
        tree.raise_window(area)
        mtime = os.path.getmtime(TE_TARGET)
        err = self.enter(pid, area, str(inp), "area")
        if err:
            return err
        err = self.press_trigger(pid, self.c["recipe"]["trigger"], "save")
        if err:
            return err
        time.sleep(1.0)
        return self.observe(pid, doc, complete=os.path.getmtime(TE_TARGET) > mtime)

    @staticmethod
    def _walk(tree):
        descs, refs = [], []
        tree._walk(tree._root(), 0, descs, refs)
        return refs


class ObjectCreate(Driver):
    def observe(self, pid, complete=True):
        names = all_labels(pid)
        return {"identity": app_identity(pid), "complete": complete and bool(names),
                "target": {"names": names}, "sentinel": None}

    def start(self, inp=None, run=None):
        pid = self.reset()
        return self.observe(pid)

    def act(self, inp, run):
        pid = self.app.pid()
        err = self.press_trigger(pid, self.c["recipe"]["trigger"], "trigger")
        if err:
            return err
        entry = focused_entry(pid)
        if entry is None:
            return {"error": "no focused text entry after the trigger (the trigger did not start "
                             "naming a new object)"}
        err = self.enter(pid, entry, C.object_name(inp, run), "entry", commit=True)
        if err:
            return err
        pid = self.app.restart(self.docs)
        time.sleep(1.0)
        return self.observe(pid)


class ScopedFind(Driver):
    """The scope is what the start state shows: the start document's own
    listing when it is a folder (read from the file system, an accessor
    independent of the UI), else every container label at the start. The
    results are the labels of the containers the search changed."""

    def _declared_scope(self, before):
        folder = next((d for d in self.docs if os.path.isdir(d)), None)
        if folder:
            return sorted(n for n in os.listdir(folder) if not n.startswith("."))
        return sorted({v for vs in before.values() for v in vs})

    def start(self, inp=None, run=None):
        pid = self.reset()
        self._before = container_labels(pid)
        return {"identity": app_identity(pid), "complete": True,
                "target": {"names": [], "scope": self._declared_scope(self._before)}, "sentinel": None}

    def act(self, inp, run):
        pid = self.app.pid()
        before = self._before
        err = self.press_trigger(pid, self.c["recipe"]["trigger"], "trigger")
        if err:
            return err
        entry = focused_entry(pid)
        if entry is None:
            return {"error": "no focused search entry after the trigger"}
        err = self.enter(pid, entry, str(inp), "entry", commit=True)
        if err:
            return err
        time.sleep(2.5)
        after = container_labels(pid)
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        names = sorted({v for vs in changed.values() for v in vs})
        self.ev.setdefault("search_views", []).append({"input": inp, "run": run,
                                                        "changed_containers": list(changed),
                                                        "names": names})
        return {"identity": app_identity(pid), "complete": bool(changed),
                "target": {"names": names, "scope": self._declared_scope(before)}, "sentinel": None}


class ToggleSelect(Driver):
    def __init__(self, *a):
        super().__init__(*a)
        opts = self.c["recipe"]["options"]
        self.options = opts
        self.parent = self.c["recipe"]["parent_index_path"]

    def marks(self, pid):
        t, others = read_group_marks(pid, self.parent, self.options)
        if t is None:
            return {"error": "menu not readable"}
        return {"identity": app_identity(pid), "complete": True, "target": t, "sentinel": others}

    def choose_inputs(self, pid):
        t = self.marks(pid)
        keys = [o["key"] for o in self.options]
        sel = (t.get("target") or {}).get("selected") or []
        if len(sel) != 1:
            return None, None, f"initial marks {sel!r}: not exactly one"
        i = keys.index(sel[0])
        rec = keys[(i + 1) % len(keys)]
        rest = [keys[(keys.index(rec) + k) % len(keys)] for k in range(1, len(keys))]
        return rec, rest[:2], None

    def select(self, pid, key, name):
        opt = next(o for o in self.options if o["key"] == key)
        return self.press_trigger(pid, opt, name)

    def start(self, inp=None, run=None):
        pid = self.reset()
        err = self.select(pid, self.rec_key, "option:" + self.rec_key)
        if err:
            return err
        return self.marks(pid)

    def act(self, inp, run):
        pid = self.app.pid()
        err = self.select(pid, inp, "option:" + inp)
        if err:
            return err
        obs = self.marks(pid)
        pid = self.app.restart(self.docs)
        after = self.marks(pid)
        self.ev.setdefault("persistence", []).append({
            "input": inp, "run": run, "selected_before_restart": (obs.get("target") or {}).get("selected"),
            "selected_after_restart": (after.get("target") or {}).get("selected")})
        return obs


DRIVERS = {"doc_save": DocSave, "object_create": ObjectCreate, "scoped_find": ScopedFind,
           "toggle_select": ToggleSelect}


# ── dump / map ──────────────────────────────────────────────────────────────

def quittable(dump):
    return any((m.get("cmd_char") or "").lower() == "q" and ddg._int(m.get("cmd_mods")) in (0, None)
               and not m.get("has_submenu") for m in dump.get("menu_items") or [])


def dump_one(bid, out):
    if bid == ddg.BLENDER_BID:
        return ddg.dump_app(bid, out)
    import AppKit
    url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
    if url is None:
        return {"bundle_id": bid, "error": "no app for bundle id"}
    app_path = str(url.path())
    timing = {}
    ctl = AppCtl(bid, quittable=bid != "com.apple.finder")    # before the dump: only Finder is kept running
    if bid != "com.apple.finder":
        ddg._quit(bid)
    t0 = time.perf_counter()
    pid = ctl.launch(start_docs(bid))
    timing["launch_ms"] = _ms(t0)
    if pid is None:
        return {"bundle_id": bid, "error": "did not launch"}
    app_el = _AS().AXUIElementCreateApplication(pid)
    t0 = time.perf_counter()
    menu = ddg.read_menu_bar(app_el)
    timing["menu_ms"] = _ms(t0)
    t0 = time.perf_counter()
    win = ddg.front_window(app_el)
    controls = ddg.read_window_controls(win)
    timing["window_ms"] = _ms(t0)
    meta, sdef, suite = ddg.read_scripting(app_path)
    dump = {"bundle_id": bid, "app": os.path.basename(app_path)[:-4], "app_path": app_path, "pid": pid,
            "scripting_meta": meta, "menu_items": menu, "window_controls": controls, "sdef": sdef,
            "script_suite": suite, "bpy_catalog": None,
            "window_document": _url_path(_at(win, "AXDocument")) if win is not None else None,
            "container_labels": container_labels(pid),
            "notes": [f"start documents: {start_docs(bid)}"], "timing_ms": timing}
    return dump


def cmd_dump(args):
    os.makedirs(args.out, exist_ok=True)
    prepare_start_documents()
    subprocess.run(["/usr/bin/mdimport", FINDER_DIR], capture_output=True, timeout=60)
    apps = []
    for name, bid in APPS:
        print(f"== {name} {bid}", flush=True)
        try:
            dump = dump_one(bid, args.out)
        except Exception as exc:  # noqa: BLE001
            dump = {"bundle_id": bid, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        with open(os.path.join(args.out, f"dump_{bid}.json"), "w") as fh:
            json.dump(dump, fh, indent=1, default=str)
        if "error" in dump:
            print("  ERROR", dump["error"])
            continue
        cands, counts = C.map_contracts(dump)
        apps.append({"app": name, "bundle_id": bid, "quittable": quittable(dump), "counts": counts,
                     "candidates": [c.to_dict() for c in cands]})
        print(f"  {counts}")
        if bid in ("com.apple.TextEdit", "com.apple.Notes", "com.apple.calculator", "com.apple.Preview"):
            ddg._quit(bid)
    with open(os.path.join(args.out, "contracts.json"), "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "apps": apps}, fh, indent=1)
    return 0


def measured(apps):
    out = []
    for a in apps:
        groups = 0
        for c in a["candidates"]:
            if c["contract"] not in MEASURED or not c["provable"]:
                continue
            if c["recipe"].get("backend") == "bpy":
                out.append((a, c))
                continue
            if c["contract"] == "toggle_select":
                groups += 1
                if groups > MAX_GROUPS:
                    continue
            out.append((a, c))
    return out


# ── prove ───────────────────────────────────────────────────────────────────

WRONG_TARGET_MARKS = ("sentinel changed", "observed document", "observed app", "unexpected additional",
                      "missing afterwards", "option group changed", "outside the scope")


def prove_ax(a, c, ex, record_only=False, lang=None, prior=None):
    ev = {}
    app = AppCtl(a["bundle_id"], a["quittable"], lang)
    drv = DRIVERS[c["contract"]](c, app, ex, ev)
    t0 = time.perf_counter()
    contract = C.CONTRACTS[c["contract"]]
    if prior is not None and prior.get("recorded"):
        # the recording already made (record step, development probe): reuse
        # its selectors instead of recording again, so a candidate is
        # recorded exactly once and the recording input never runs twice
        drv.rec = prior["recorded"]
        rec_input, inputs = prior["recording_input"], prior["inputs"]
        rec_obs = {"reused_from": "recorded.json", "observation": prior.get("recording_observation")}
        if c["contract"] == "toggle_select":
            drv.rec_key = rec_input
    elif c["contract"] == "toggle_select":
        pid = app.restart(drv.docs)
        rec, unseen, why = drv.choose_inputs(pid)
        if rec is None:
            return {"id": c["id"], "status": "not proved", "reason": why, "evidence": ev}
        drv.rec_key = rec
        rec_input, inputs = rec, unseen
        # the recording: select the recording option from the initial state
        err = drv.select(app.pid(), rec, "option:" + rec)
        rec_obs = drv.marks(app.pid()) if not err else err
    else:
        rec_input, inputs = REC_INPUT[c["contract"]], PROOF_INPUTS[c["contract"]]
        drv.start()
        rec_obs = drv.act(rec_input, 0)
    start = drv.start()
    rec_ms = _ms(t0)
    result = {"id": c["id"], "app": a["app"], "contract": c["contract"], "triggers": c["triggers"],
              "recording_input": rec_input, "recording_observation": rec_obs,
              "recorded": drv.rec, "declared_start": start, "inputs": inputs, "record_ms": rec_ms}
    if record_only:
        result["status"] = "recorded only (development probe)"
        result["executor_log"] = ex.log[-20:]
        return result
    if contract.validate(start):
        result.update(status="not proved", reason=f"declared start not observable: {contract.validate(start)}")
        return result
    n0 = len(ex.log)
    t0 = time.perf_counter()
    rep = C.prove_contract(contract, c["id"], inputs, 3, lambda i, r: drv.act(i, r),
                           recorded_inputs=[rec_input] + SEEN.get(c["contract"], []),
                           start_state=start, observe_start=lambda i, r: drv.start(i, r))
    d = rep.to_dict()
    rows = d["rows"]
    result.update(status="proved" if rep.proved else "not proved", proof=d, prove_ms=_ms(t0),
                  rows=len(rows), rows_agree=sum(1 for r in rows if r["agree"]),
                  wrong_target_rows=sum(1 for r in rows if r["mismatch"] and
                                        any(m in r["mismatch"] for m in WRONG_TARGET_MARKS)),
                  refused_rows=sum(1 for r in rows if isinstance(r["ui_value"], dict)
                                   and r["ui_value"].get("refused")),
                  executor_steps=len(ex.log) - n0,
                  executor_refusals=sum(1 for x in ex.log[n0:] if x["refused"]),
                  evidence=ev)
    return result


def prove_blender(a, c, out):
    """object_create through bpy: the click driver vs the headless
    script, hardened blender_compile.prove, each unseen input x 3 relaunches."""
    from anyuse.execution import blender_compile as bc
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender"))
    import drive
    cc = dd.ClassCandidate(id=c["id"], bundle_id=c["bundle_id"], op_class="app_script",
                           subtype=c["recipe"]["shape"], triggers=c["triggers"])
    rec = dd.synthetic_blender_recording(cc)
    src = bc.generate_script(rec, bc.infer_class(rec))
    script = os.path.join(out, "gen_mesh_add_move.py")
    with open(script, "w") as fh:
        fh.write(src)
    if not ddg._relaunch_blender_gui():
        return {"id": c["id"], "status": "not proved", "reason": "GUI Blender did not come up"}
    start = ddg._hook({"action": "dump_state"}).get("state")
    evidence = {"ui": [], "script": []}

    def ui_fn(inp):
        if not ddg._relaunch_blender_gui():
            return {"error": "relaunch failed"}
        t0 = time.perf_counter()
        for x, y, pause in ((262, 101, 0.3), (292, 124, 0.3), (456, 144, 0.4),
                            (305, 101, 0.4), (342, 124, 0.4), (512, 124, 0.4)):
            drive.click(x, y)
            time.sleep(pause)
        drive.numeric_modal(list(inp), (600, 420))
        gui_ms = _ms(t0)
        time.sleep(0.6)
        res = ddg._hook({"action": "dump_both", "since": 0})
        evidence["ui"].append({"input": list(inp), "gui_ms": gui_ms, "ops": res.get("ops")})
        return res.get("state") if res.get("ok") else {"error": res.get("error")}

    def script_fn(inp):
        o = os.path.join(out, f"gen_script_{'_'.join(str(v) for v in inp)}_{len(evidence['script'])}.json")
        ms, _log = bc.run_headless(ddg.BLENDER_BIN, ddg.BASE_BLEND, script, [*map(str, inp), o])
        evidence["script"].append({"input": list(inp), "headless_ms": round(ms, 1)})
        with open(o) as fh:
            return json.load(fh)

    inputs = [i for i in BL_INPUTS for _ in range(3)]
    t0 = time.perf_counter()
    rep = bc.prove(c["id"], c["recipe"]["shape"], inputs, ui_fn, script_fn,
                   recorded_inputs=BL_SEEN, start_state=start or {})
    d = rep.to_dict()
    ddg._relaunch_blender_gui()
    rows = d["rows"]
    return {"id": c["id"], "app": "Blender", "contract": "object_create", "backend": "bpy",
            "status": "proved" if rep.proved else "not proved", "proof": d, "prove_ms": _ms(t0),
            "rows": len(rows), "rows_agree": sum(1 for r in rows if r["agree"]),
            "wrong_target_rows": sum(1 for r in rows if r.get("mismatch") and
                                     ("missing afterwards" in r["mismatch"] or "exactly one new" in r["mismatch"])),
            "declared_start": start, "generated_script": src, "evidence": evidence}


def _load(out):
    with open(os.path.join(out, "contracts.json")) as fh:
        return json.load(fh)["apps"]


def cmd_prove(args, record_only=False):
    only = {x.strip() for x in (args.only or "").split(",") if x.strip()}
    apps = _load(args.out)
    prepare_start_documents()
    ex = Exec()
    results = []
    prior = {}
    rp = os.path.join(args.out, "recorded.json")
    if not record_only and os.path.exists(rp):
        with open(rp) as fh:
            prior = {r["id"]: r for r in json.load(fh)["results"]}
    for a, c in measured(apps):
        if only and not any(o in c["id"] for o in only):
            continue
        if locked():
            print("SCREEN LOCKED: stopping")
            results.append({"id": c["id"], "status": "stopped", "reason": "lock screen"})
            break
        print(f"== {c['id']}", flush=True)
        t0 = time.perf_counter()
        try:
            if c["recipe"].get("backend") == "bpy":
                r = {"id": c["id"], "status": "skipped in record mode"} if record_only else \
                    prove_blender(a, c, args.out)
            else:
                r = prove_ax(a, c, ex, record_only=record_only, prior=prior.get(c["id"]))
        except Exception as exc:  # noqa: BLE001
            r = {"id": c["id"], "status": "error", "reason": f"{type(exc).__name__}: {exc}",
                 "traceback": traceback.format_exc()}
        r["total_ms"] = _ms(t0)
        results.append(r)
        print(f"   {r.get('status')} rows={r.get('rows_agree')}/{r.get('rows')} "
              f"wrong_target={r.get('wrong_target_rows')} {r.get('reason', '')}", flush=True)
        for bid in (a["bundle_id"],):
            if bid not in ("com.apple.finder", ddg.BLENDER_BID):
                ddg._quit(bid)
    name = "recorded.json" if record_only else f"proved{('_' + args.tag) if args.tag else ''}.json"
    with open(os.path.join(args.out, name), "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results}, fh,
                  indent=1, default=str)
    return 0


def cmd_lang(args):
    """Each proved AX candidate once, English UI, German-recorded selectors,
    first unseen input, one run (run index 7: a fresh object name)."""
    with open(os.path.join(args.out, "proved.json")) as fh:
        proved = [r for r in json.load(fh)["results"] if r.get("status") == "proved" and "recorded" in r]
    apps = {a["bundle_id"]: a for a in _load(args.out)}
    ex = Exec()
    out = []
    for r in proved:
        bid = r["id"].split(":")[0]
        a = apps[bid]
        c = next(x for x in a["candidates"] if x["id"] == r["id"])
        if not a["quittable"]:
            out.append({"id": r["id"], "status": "n/a", "reason": "not relaunchable with launch arguments"})
            continue
        ev = {}
        app = AppCtl(bid, True, lang="en")
        drv = DRIVERS[c["contract"]](c, app, ex, ev)
        drv.rec = r["recorded"]
        contract = C.CONTRACTS[c["contract"]]
        if c["contract"] == "toggle_select":
            drv.rec_key = r["recording_input"]
        inp = r["inputs"][0]
        start = drv.start()
        pid = app.pid()
        bar = [_s(_at(k, "AXTitle")) for k in (_at(_at(_AS().AXUIElementCreateApplication(pid), "AXMenuBar"),
                                                   "AXChildren") or [])][:4] if pid else []
        n0 = len(ex.log)
        obs = drv.act(inp, 7) if not (isinstance(start, dict) and start.get("error")) else start
        why = contract.validate(obs) or contract.validate(start) or \
            contract.expect(start, inp, 7, obs) or contract.preserve(start, inp, 7, obs)
        out.append({"id": r["id"], "input": inp, "menu_bar": bar, "met_contract": why is None,
                    "why": why, "observation": obs, "executor": ex.log[n0:], "evidence": ev})
        print(f"== {r['id']} en: {'OK' if why is None else why} bar={bar}", flush=True)
        ddg._quit(bid)
    with open(os.path.join(args.out, "localisation.json"), "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": out}, fh, indent=1,
                  default=str)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("dump", "record", "prove", "lang"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    refuse_unless_in_a_guest()
    if locked():
        print("SCREEN LOCKED: stopping before anything runs")
        return 3
    from anyuse.context import accessibility as acc
    if not acc.ax_trusted():
        raise SystemExit("REFUSING TO RUN: Accessibility is not granted.")
    if args.cmd != "dump":
        from anyuse.settings import get as get_setting
        if not get_setting("ANYUSE_DRIVE_ENABLED"):
            raise SystemExit("ANYUSE_DRIVE_ENABLED is off")
        try:
            from anyuse.context import keyboard as kb
            kb.release_modifiers()
        except Exception:  # noqa: BLE001
            pass
    os.makedirs(GEN, exist_ok=True)
    if args.cmd == "dump":
        return cmd_dump(args)
    if args.cmd == "record":
        return cmd_prove(args, record_only=True)
    if args.cmd == "prove":
        return cmd_prove(args)
    return cmd_lang(args)


if __name__ == "__main__":
    raise SystemExit(main())
