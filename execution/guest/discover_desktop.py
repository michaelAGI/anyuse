#!/usr/bin/env python3
"""Desktop discovery, guest side. INSIDE THE GUEST VM ONLY.

Two subcommands:

``dump``   For each bundle id: launch the app (TextEdit on a fresh empty
           scratch .txt, Finder on a scratch folder, the rest bare), then
           READ, never actuate: the menu bar through the accessibility tree,
           the front window's controls, the scripting dictionary the bundle
           ships (``.sdef`` with its ``xi:include``s, or the legacy
           ``.scriptSuite``/``.scriptTerminology``), and for Blender the bpy
           operator catalog (``blender --background --python capture.py --
           catalog``). Then ``execution.desktop_discover.discover_app`` turns
           each dump into candidates. Writes ``dump_<bundle>.json`` and
           ``discovery.json``.

``prove``  For every mapped class candidate in ``discovery.json``: run that
           class's EXISTING UI flow (``flows.flow7_calculator``,
           ``flows.flow1_textedit_write_save``,
           ``flows_more.flow3_finder_create_folder``, the Blender
           click driver) on inputs never used before, check that the flow
           resolved the same language-independent trigger discovery found,
           and hand the rows to the existing proof routine
           (``desktop_compile.prove`` / ``blender_compile.prove``).
           ``desktop_discover.attach_proof`` decides ``proved``. Writes
           ``discovery_proved.json``.

No ``sdef`` binary: on this guest it is an Xcode shim that opens the
Command Line Tools installer. The dictionary files are read directly.

usage: discover_desktop.py dump  --out DIR --bundles a,b,c
       discover_desktop.py prove --out DIR [--only class,...]
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flows import refuse_unless_in_a_guest   # noqa: E402

from anyuse.execution import desktop_discover as dd   # noqa: E402  (pure)

SCRATCH = os.path.expanduser("~/anyuse-guest/scratch/discovery")
BLENDER_BIN = "/Applications/Blender.app/Contents/MacOS/Blender"
BLENDER_DIR = os.path.expanduser("~/anyuse-guest/blender")
BASE_BLEND = os.path.join(BLENDER_DIR, "base_empty.blend")
CAPTURE = os.path.expanduser("~/anyuse-guest/src/anyuse/execution/guest/blender/capture.py")
HOOK = os.path.expanduser("~/anyuse-guest/src/anyuse/execution/guest/blender/hook.py")
BLENDER_BID = "org.blenderfoundation.blender"

# ── inputs (fixed in advance) ──────────────────────────────
PROOF_INPUTS = {
    "arithmetic": [(318, "-", 45), (23, "*", 17)],
    "file_write": ["theta", "kappa"],
    ("directory_op", "create"): ["yuzu", "damson"],
    ("app_script", "mesh_add_move"): [(3.0, -2.5, 1.25), (-1.5, 0.5, 2.0)],
}
#: every input already recorded or replayed for these classes
SEEN_INPUTS = {
    "arithmetic": [(12, "+", 30), (7, "*", 6), (144, "/", 12), (99, "-", 57)],
    "file_write": ["alpha", "gamma", "delta", "epsilon", "zeta", "eta"],
    ("directory_op", "create"): ["papaya_rec", "quince", "rhubarb", "sorrel", "tamarind", "ugli"],
    ("app_script", "mesh_add_move"): [(2.5, -1.0, 0.75), (1.0, 2.0, -3.0), (-0.5, 4.25, 0.5)],
}


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000.0, 1)


# ── AX reading (read-only) ─────────────────────────────────────────────────

def _AS():
    import ApplicationServices as A
    return A


def _at(el, name):
    try:
        err, val = _AS().AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:
        return None


def _str(v):
    return None if v is None else str(v)


def _int(v):
    try:
        return int(v)
    except Exception:
        return None


def read_menu_bar(app_el):
    """Flat list of every AXMenuItem under the menu bar, with its index and
    title path. Menus are read closed: Cocoa exposes their items without
    opening them (checked on Preview)."""
    mb = _at(app_el, "AXMenuBar")
    out = []
    if mb is None:
        return out

    def walk(menu, idx_path, title_path, depth):
        if depth > 8:
            return
        for j, item in enumerate(_at(menu, "AXChildren") or []):
            if _str(_at(item, "AXRole")) != "AXMenuItem":
                continue
            title = _str(_at(item, "AXTitle")) or ""
            kids = [k for k in (_at(item, "AXChildren") or []) if _str(_at(k, "AXRole")) == "AXMenu"]
            out.append({
                "index_path": idx_path + [j], "title_path": title_path + [title], "title": title,
                "identifier": _str(_at(item, "AXIdentifier")) or "",
                "cmd_char": _str(_at(item, "AXMenuItemCmdChar")) or "",
                "cmd_mods": _int(_at(item, "AXMenuItemCmdModifiers")),
                "cmd_virtual_key": _int(_at(item, "AXMenuItemCmdVirtualKey")),
                "enabled": bool(_at(item, "AXEnabled")),
                "mark_char": _str(_at(item, "AXMenuItemMarkChar")) or "",
                "has_submenu": bool(kids),
            })
            for sub in kids:
                walk(sub, idx_path + [j], title_path + [title], depth + 1)

    for i, bar_item in enumerate(_at(mb, "AXChildren") or []):
        title = _str(_at(bar_item, "AXTitle")) or ""
        for menu in _at(bar_item, "AXChildren") or []:
            if _str(_at(menu, "AXRole")) == "AXMenu":
                walk(menu, [i], [title], 1)
    return out


def front_window(app_el):
    for attr in ("AXFocusedWindow", "AXMainWindow"):
        w = _at(app_el, attr)
        if w is not None:
            return w
    ws = _at(app_el, "AXWindows") or []
    return ws[0] if ws else None


def read_window_controls(win, limit=4000):
    out = []
    if win is None:
        return out

    def walk(el, depth):
        if depth > 25 or len(out) >= limit:
            return
        role = _str(_at(el, "AXRole")) or ""
        out.append({"role": role, "subrole": _str(_at(el, "AXSubrole")) or "",
                    "identifier": _str(_at(el, "AXIdentifier")) or "",
                    "title": _str(_at(el, "AXTitle")) or "",
                    "description": _str(_at(el, "AXDescription")) or "",
                    "enabled": bool(_at(el, "AXEnabled"))})
        for k in _at(el, "AXChildren") or []:
            walk(k, depth + 1)
    walk(win, 0)
    return out


# ── scripting dictionary (files only) ──────────────────────────────────────

_INCLUDE_RE = re.compile(r'<xi:include[^>]*href="([^"]+)"')
_ALLOWED_INCLUDE_ROOTS = ("/System/Library/ScriptingDefinitions/",)


def _load_plist(path):
    """A property list in any format. Legacy ``.scriptSuite`` files are
    old-style OpenStep text, which ``plistlib`` refuses; the
    system's own ``plutil`` (native, not an Xcode shim) converts them."""
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:  # noqa: BLE001
        xml = subprocess.run(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", path],
                             capture_output=True, timeout=20, check=True).stdout
        return plistlib.loads(xml)


def read_scripting(app_path):
    res = os.path.join(app_path, "Contents", "Resources")
    with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as fh:
        info = plistlib.load(fh)
    meta = {"NSAppleScriptEnabled": info.get("NSAppleScriptEnabled"),
            "OSAScriptingDefinition": info.get("OSAScriptingDefinition")}
    name = info.get("OSAScriptingDefinition")
    if name:
        path = os.path.join(res, name)
        xml = open(path, encoding="utf-8").read()
        includes = {}
        for href in _INCLUDE_RE.findall(xml):
            local = href.replace("file://localhost", "", 1)
            if local.startswith(_ALLOWED_INCLUDE_ROOTS) or local.startswith(app_path):
                try:
                    includes[href] = open(local, encoding="utf-8").read()
                except OSError:
                    pass
        return meta, {"xml": xml, "includes": includes, "path": path}, None
    suites = [f for f in os.listdir(res) if f.endswith(".scriptSuite")] if os.path.isdir(res) else []
    if suites:
        base = suites[0][:-len(".scriptSuite")]
        suite = _load_plist(os.path.join(res, suites[0]))
        term = {}
        for d in ("", "en.lproj", "English.lproj", "Base.lproj"):
            p = os.path.join(res, d, base + ".scriptTerminology")
            if os.path.exists(p):
                term = _load_plist(p)
                break
        return meta, None, {"suite": suite, "terminology": term, "path": suites[0]}
    return meta, None, None


# ── Blender catalog (headless, the existing capture mechanism) ─────────────

def blender_catalog(out_dir):
    out = os.path.join(out_dir, "bpy_catalog.json")
    proc = subprocess.run([BLENDER_BIN, "--background", "--factory-startup", "--python", CAPTURE,
                           "--", "catalog", out], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"catalog failed rc={proc.returncode}: {proc.stderr[-800:]}")
    with open(out) as fh:
        return json.load(fh)


# ── dump ───────────────────────────────────────────────────────────────────

def _running(bid):
    import AppKit
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid)
    return apps[0] if apps else None


def _quit(bid):
    a = _running(bid)
    if a is None:
        return
    a.terminate()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _running(bid) is not None:
        time.sleep(0.3)
    time.sleep(1.0)


def _launch(bid, doc=None, wait=3.0):
    import AppKit
    subprocess.run(["/usr/bin/open", "-b", bid] + ([doc] if doc else []), capture_output=True, timeout=30)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        a = _running(bid)
        if a is not None:
            time.sleep(wait)
            a.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.8)
            return int(a.processIdentifier())
        time.sleep(0.4)
    return None


def dump_app(bid, out_dir):
    import AppKit
    url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
    if url is None:
        return {"bundle_id": bid, "error": "no app for bundle id"}
    app_path = str(url.path())
    app_name = os.path.basename(app_path)[:-4]
    notes, timing = [], {}
    was_running = _running(bid) is not None
    doc = None
    if bid == "com.apple.TextEdit":
        _quit(bid)
        doc = os.path.join(SCRATCH, "discover_seed.txt")
        open(doc, "w").close()
        notes.append("launched on a fresh empty scratch .txt (default seed document)")
    elif bid == "com.apple.finder":
        doc = os.path.join(SCRATCH, "finder")
        os.makedirs(doc, exist_ok=True)
        notes.append("Finder window opened on a scratch folder")
    elif bid != BLENDER_BID:
        _quit(bid)
    t0 = time.perf_counter()
    pid = _launch(bid, doc)
    timing["launch_ms"] = _ms(t0)
    if pid is None:
        return {"bundle_id": bid, "app": app_name, "error": "did not launch"}
    app_el = _AS().AXUIElementCreateApplication(pid)
    t0 = time.perf_counter()
    menu = read_menu_bar(app_el)
    timing["menu_ms"] = _ms(t0)
    t0 = time.perf_counter()
    win = front_window(app_el)
    controls = read_window_controls(win)
    timing["window_ms"] = _ms(t0)
    notes.append(f"front window: {_str(_at(win, 'AXTitle')) if win is not None else None!r}, "
                 f"{len(controls)} elements read")
    t0 = time.perf_counter()
    meta, sdef, suite = read_scripting(app_path)
    timing["dictionary_ms"] = _ms(t0)
    dump = {"bundle_id": bid, "app": app_name, "app_path": app_path, "pid": pid,
            "scripting_meta": meta, "menu_items": menu, "window_controls": controls,
            "sdef": sdef, "script_suite": suite, "bpy_catalog": None, "notes": notes}
    if bid == BLENDER_BID:
        t0 = time.perf_counter()
        dump["bpy_catalog"] = blender_catalog(out_dir)
        timing["bpy_catalog_ms"] = _ms(t0)
    timing["read_ms"] = round(sum(v for k, v in timing.items() if k != "launch_ms"), 1)
    dump["timing_ms"] = timing
    if not was_running and bid not in ("com.apple.finder", BLENDER_BID):
        _quit(bid)
    if bid == "com.apple.TextEdit" or bid in ("com.apple.calculator", "com.apple.Notes"):
        _quit(bid)
    return dump


def cmd_dump(args):
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(SCRATCH, exist_ok=True)
    apps = []
    for bid in [b.strip() for b in args.bundles.split(",") if b.strip()]:
        print(f"== {bid}")
        try:
            dump = dump_app(bid, args.out)
        except Exception as exc:  # noqa: BLE001
            import traceback
            dump = {"bundle_id": bid, "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()}
        with open(os.path.join(args.out, f"dump_{bid}.json"), "w") as fh:
            json.dump(dump, fh, indent=1, default=str)
        if "error" in dump:
            print("  ERROR", dump["error"])
            continue
        disc = dd.discover_app(dump)
        apps.append(disc)
        c = disc.counts()
        print(f"  discovered={c['discovered']} by_source={c['by_source']} classes={c['classes']} "
              f"menu_lang_free={c['menu_language_independent']} timing={disc.timing_ms}")
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "summary": dd.summary_table(apps), "apps": [a.to_dict() for a in apps]}
    with open(os.path.join(args.out, "discovery.json"), "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print("report ->", os.path.join(args.out, "discovery.json"))
    return 0


# ── prove ──────────────────────────────────────────────────────────────────

def _mods():
    from anyuse.context import accessibility as acc
    from anyuse.guides import state_graph as sg
    from anyuse.execution import drift_gate as drift
    if not acc.ax_trusted():
        raise SystemExit("REFUSING TO RUN: Accessibility is not granted.")
    return {"acc": acc, "sg": sg, "drift": drift}


def _identity_matches(trigger_keys, recorded_steps, step):
    ident = (recorded_steps.get(step) or {}).get("identity") or {}
    cmd, mods = (ident.get("cmd") or "").lower(), (ident.get("mods") or "0").strip() or "0"
    return any(k == f"key:{mods}:{cmd}" or k.startswith(f"key:{mods}:{cmd}@")
               or (ident.get("identifier") and k.startswith(f"id:{ident['identifier']}"))
               for k in trigger_keys)


def prove_arithmetic(k, mods, runs, out):
    from anyuse.execution import desktop_compile as dc
    from flows import CALC_IDS, flow7_calculator, _num
    ids = {t[len("id:"):] for t in k["triggers"]}
    if not set(CALC_IDS.values()) <= ids:
        return None, f"flow7 presses {sorted(set(CALC_IDS.values()) - ids)} that discovery did not find"
    inputs = PROOF_INPUTS["arithmetic"]
    ui = flow7_calculator(mods, SCRATCH, runs, inputs, "none", out)
    by = {(r["input"], r["run"]): r for r in ui.get("runs", [])}
    op = dc.CLASS_OPERATIONS["arithmetic"]

    def ui_fn(inp, i):
        r = by.get((f"{inp[0]}{inp[1]}{inp[2]}", i)) or {}
        return {"display": [n for n in (_num(s) for s in r.get("display") or []) if n is not None]}

    # flow7 presses AC first, so every run starts from a cleared display
    report = dc.prove(op, inputs, runs, ui_fn, lambda inp, i: {"display": [op.direct(*inp)]},
                      equal_fn=lambda shown, want: any(abs(x - want["display"][0]) < 1e-6
                                                       for x in shown["display"]),
                      recorded_inputs=SEEN_INPUTS["arithmetic"], start_state={"display": [0.0]})
    return report.to_dict(), None


def prove_file_write(k, mods, runs, out):
    from anyuse.execution import desktop_compile as dc
    from flows import flow1_textedit_write_save
    inputs = PROOF_INPUTS["file_write"]
    ui = flow1_textedit_write_save(mods, SCRATCH, runs, inputs, "none", out)
    if not _identity_matches(k["triggers"], ui.get("recorded_steps") or {}, "save"):
        return None, f"flow1's Save item {ui.get('recorded_steps', {}).get('save')} is not the discovered trigger"
    op = dc.CLASS_OPERATIONS["file_write"]

    sentinel = os.path.join(SCRATCH, "file_write_sentinel.txt")
    with open(sentinel, "w") as fh:
        fh.write("the untouched document\n")

    def _read(p):
        return {"text": open(p).read() if os.path.exists(p) else None,
                "sentinel": open(sentinel).read()}

    def ui_fn(w, i):
        return _read(os.path.join(SCRATCH, f"flow1_{w}_{i}.txt"))

    def direct_fn(w, i):
        p = os.path.join(SCRATCH, f"direct_{w}_{i}.txt")
        op.direct(p, f"anyuse desktop validation {w} {i}")
        return _read(p)

    def expect(start, w, i, obs):
        want = f"anyuse desktop validation {w} {i}"
        return None if obs["text"].rstrip("\n") == want else f"file holds {obs['text']!r}, want {want!r}"

    # flow1 seeds every run's document with "seed\n" before it opens it
    return dc.prove(op, inputs, runs, ui_fn, direct_fn, recorded_inputs=SEEN_INPUTS["file_write"],
                    start_state={"text": "seed\n", "sentinel": "the untouched document\n"},
                    expect=expect).to_dict(), None


def prove_directory_create(k, mods, runs, out):
    from anyuse.execution import desktop_compile as dc
    from flows_more import flow3_finder_create_folder
    inputs = PROOF_INPUTS[("directory_op", "create")]
    folder = os.path.join(SCRATCH, "finder")
    os.makedirs(folder, exist_ok=True)
    ui = flow3_finder_create_folder(mods, SCRATCH, runs, inputs, "none", out)
    if "runs" not in ui:
        return None, f"flow3 did not run: {ui.get('error')}"
    if not _identity_matches(k["triggers"], ui.get("recorded_steps") or {}, "new_folder"):
        return None, f"flow3's New Folder item {ui['recorded_steps'].get('new_folder')} is not the discovered trigger"
    op = dc.CLASS_OPERATIONS[("directory_op", "create")]

    by = {(r["input"], r["run"]): r for r in ui["runs"]}
    direct_root = os.path.join(SCRATCH, "finder_direct")

    def ui_fn(name, i):
        # the flow's own per-run listing delta (after - before), i.e. the
        # listing relative to that run's start
        r = by.get((name, i))
        return {"entries": list(r["created"])} if r is not None else {"error": "run missing"}

    def direct_fn(name, i):
        root = os.path.join(direct_root, f"{name}_{i}")
        os.makedirs(root, exist_ok=True)
        before = set(os.listdir(root))
        op.direct(os.path.join(root, f"{name}_{i}"))
        return {"entries": sorted(set(os.listdir(root)) - before)}

    expect, preserved = dc.expect_new_entry(lambda n, i: f"{n}_{i}")
    return dc.prove(op, inputs, runs, ui_fn, direct_fn,
                    recorded_inputs=SEEN_INPUTS[("directory_op", "create")],
                    start_state={"entries": []},     # each observation is relative to its run's start
                    expect=expect, preserved=preserved).to_dict(), None


def _hook(cmd, timeout=20.0):
    cmd_path, res_path = os.path.join(BLENDER_DIR, "cmd.json"), os.path.join(BLENDER_DIR, "result.json")
    if os.path.exists(res_path):
        os.remove(res_path)
    with open(cmd_path + ".tmp", "w") as fh:
        json.dump(cmd, fh)
    os.replace(cmd_path + ".tmp", cmd_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(res_path):
            time.sleep(0.2)
            with open(res_path) as fh:
                return json.load(fh)
        time.sleep(0.2)
    return {"ok": False, "error": "hook timeout"}


def _relaunch_blender_gui():
    subprocess.run(["/usr/bin/pkill", "-x", "Blender"], capture_output=True)
    time.sleep(2.0)
    for p in ("cmd.json", "result.json"):
        try:
            os.remove(os.path.join(BLENDER_DIR, p))
        except OSError:
            pass
    log = open(os.path.join(BLENDER_DIR, "blender_stdout.log"), "w")
    subprocess.Popen([BLENDER_BIN, BASE_BLEND, "--python", HOOK], stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)
    time.sleep(5.0)
    for _ in range(30):
        if _hook({"action": "ping"}, timeout=2.0).get("ok"):
            return True
        time.sleep(0.5)
    return False


def prove_app_script(k, mods, runs, out):
    """UI side: the click driver (Add > Mesh > Cube,
    Object > Transform > Move, keycode numeric entry) in a freshly launched
    GUI Blender on base_empty.blend, read back through the hook. Script
    side: blender_compile.generate_script over the DISCOVERED operators
    (only the parameter kwarg; no recorded constants), run headless."""
    from anyuse.execution import blender_compile as bc
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender"))
    import drive
    key = ("app_script", k["subtype"])
    inputs = PROOF_INPUTS[key]
    cc = dd.ClassCandidate(id=k["id"], bundle_id=k["bundle_id"], op_class=k["op_class"],
                           subtype=k["subtype"], triggers=k["triggers"])
    rec = dd.synthetic_blender_recording(cc)
    inf = bc.infer_class(rec)
    src = bc.generate_script(rec, inf)
    script = os.path.join(out, "discovered_mesh_add_move.py")
    with open(script, "w") as fh:
        fh.write(src)
    if not _relaunch_blender_gui():
        return None, "GUI Blender with hook did not come up"
    start = _hook({"action": "dump_state"}).get("state")
    evidence = {"start_state": start, "ui": {}, "script": {}}

    def ui_fn(inp):
        if not _relaunch_blender_gui():
            return {"error": "relaunch failed"}
        since = 0
        t0 = time.perf_counter()
        for x, y, pause in ((262, 101, 0.3), (292, 124, 0.3), (456, 144, 0.4),
                            (305, 101, 0.4), (342, 124, 0.4), (512, 124, 0.4)):
            drive.click(x, y)
            time.sleep(pause)
        drive.numeric_modal(list(inp), (600, 420))
        gui_ms = _ms(t0)
        time.sleep(0.6)
        res = _hook({"action": "dump_both", "since": since})
        evidence["ui"][repr(inp)] = {"gui_ms": gui_ms, "ops": res.get("ops"), "ok": res.get("ok")}
        return res.get("state") if res.get("ok") else {"error": res.get("error")}

    def script_fn(inp):
        o = os.path.join(out, f"discovered_script_{'_'.join(str(v) for v in inp)}.json")
        ms, _log = bc.run_headless(BLENDER_BIN, BASE_BLEND, script, [*map(str, inp), o])
        evidence["script"][repr(inp)] = {"headless_ms": round(ms, 1)}
        with open(o) as fh:
            return json.load(fh)

    report = bc.prove(k["id"], k["subtype"], inputs, ui_fn, script_fn,
                      recorded_inputs=SEEN_INPUTS[key], start_state=start or {})
    d = report.to_dict()
    d["evidence"] = evidence
    d["generated_script"] = src
    _relaunch_blender_gui()   # leave one GUI Blender with the hook, as found
    return d, None


PROVERS = {"arithmetic": prove_arithmetic, "file_write": prove_file_write,
           ("directory_op", "create"): prove_directory_create,
           ("app_script", "mesh_add_move"): prove_app_script}


def cmd_prove(args):
    os.makedirs(SCRATCH, exist_ok=True)
    from anyuse.settings import get as get_setting
    if not get_setting("ANYUSE_DRIVE_ENABLED"):
        raise SystemExit("ANYUSE_DRIVE_ENABLED is off — nothing would actuate.")
    mods = _mods()
    try:
        from anyuse.context import keyboard as kb
        kb.release_modifiers()
    except Exception:
        pass
    with open(os.path.join(args.out, "discovery.json")) as fh:
        report = json.load(fh)
    only = set(x.strip() for x in (args.only or "").split(",") if x.strip())
    results = []
    for app in report["apps"]:
        for k in app["classes"]:
            key = (k["op_class"], k["subtype"]) if k["subtype"] else k["op_class"]
            label = k["op_class"] + (f"/{k['subtype']}" if k["subtype"] else "")
            if only and label not in only:
                continue
            fn = PROVERS.get(key)
            print(f"== prove {k['id']}")
            t0 = time.perf_counter()
            if fn is None:
                proof, why = None, "no prover bound for this class"
            else:
                try:
                    proof, why = fn(k, mods, dd_runs(), args.out)
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    proof, why = None, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            elapsed = _ms(t0)
            cc = dd.ClassCandidate(id=k["id"], bundle_id=k["bundle_id"], op_class=k["op_class"],
                                   subtype=k["subtype"], triggers=k["triggers"])
            if proof is None:
                cc.status, cc.proof_reason = "mapped", f"not proved: {why}"
            else:
                dd.attach_proof(cc, proof, PROOF_INPUTS[key], SEEN_INPUTS[key])
            row = cc.to_dict()
            row["proof_ms"] = elapsed
            results.append(row)
            print(f"   status={cc.status} ({cc.proof_reason.splitlines()[0] if cc.proof_reason else ''}) "
                  f"{elapsed:.0f} ms")
    with open(os.path.join(args.out, "discovery_proved.json"), "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results},
                  fh, indent=1, default=str)
    return 0


def dd_runs():
    from anyuse.execution import desktop_compile as dc
    return dc.MIN_RUNS_PER_INPUT


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--out", required=True)
    d.add_argument("--bundles", required=True)
    p = sub.add_parser("prove")
    p.add_argument("--out", required=True)
    p.add_argument("--only", default="")
    args = ap.parse_args()
    refuse_unless_in_a_guest()
    return cmd_dump(args) if args.cmd == "dump" else cmd_prove(args)


if __name__ == "__main__":
    raise SystemExit(main())
