#!/usr/bin/env python3
"""``osa_command`` in the guest. INSIDE THE GUEST VM ONLY.

``analyse``  Read-only. For each bundle id: read the scripting dictionary from
             the bundle's own files (``discover_desktop.read_scripting``; never
             the ``sdef`` tool) plus the guest's ``CocoaStandard.sdef``, run
             ``execution.osa_compile.analyse_app``, and compile-check every
             emitted script with ``osacompile`` (compile only: nothing runs,
             no Apple event is sent). Then read each target's Automation status
             WITHOUT asking (``AEDeterminePermissionToAutomateTarget``,
             ``askUserIfNeeded=false``). Writes ``osa_analysis.json``.

``prove`` The fixed GUI-vs-OSA proofs.
             It reads the Automation status first; if a target is not granted,
             that proof is refused before any GUI step and no event is sent.
             Writes ``osa_proved.json``.

usage: osa_desktop.py analyse --out DIR --bundles a,b,c
       osa_desktop.py prove   --out DIR
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flows import refuse_unless_in_a_guest   # noqa: E402

from anyuse.execution import desktop_discover as dd   # noqa: E402
from anyuse.execution import osa_compile as oc        # noqa: E402

SCRATCH = os.path.expanduser("~/anyuse-guest/scratch/osa")
COCOA_STANDARD = "/System/Library/ScriptingDefinitions/CocoaStandard.sdef"

#: existing desktop classes per app (recorded and proved earlier). Notes has
#: none; its candidate is discovered.
CLASSES = {
    "com.apple.finder": [("directory_op", "create")],
    "com.apple.TextEdit": [("file_write", None)],
    "com.apple.calculator": [("arithmetic", None)],
    "org.blenderfoundation.blender": [("app_script", "mesh_add_move")],
    "com.apple.Notes": [],
}

# ── inputs (fixed in advance) ────────────────────────────────────
PROOF_INPUTS = {
    "com.apple.finder": ["mirabelle", "sloe"],
    "com.apple.TextEdit": ["iota", "lambda"],
    "com.apple.Notes": ["mispel", "holunder"],        # flows_more.NOTE_TITLES keys
}
SEEN_INPUTS = {
    "com.apple.finder": ["papaya_rec", "quince", "rhubarb", "sorrel", "tamarind", "ugli", "yuzu", "damson"],
    "com.apple.TextEdit": ["alpha", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "kappa"],
    "com.apple.Notes": ["rec", "quince", "harz", "lamp"],
}


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000.0, 1)


# ── Automation status, never asking ────────────────────────────────────────

class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


def _fcc(s):
    return int.from_bytes(s.encode("mac_roman"), "big")


def automation_status(bundle_id: str) -> int:
    """``AEDeterminePermissionToAutomateTarget(target, '****', '****',
    askUserIfNeeded=false)``. Never shows a dialog."""
    cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
    cs.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(_AEDesc), ctypes.c_uint32,
                                                         ctypes.c_uint32, ctypes.c_ubyte]
    cs.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
    cs.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
    desc, raw = _AEDesc(), bundle_id.encode()
    cs.AECreateDesc(_fcc("bund"), raw, len(raw), ctypes.byref(desc))
    try:
        return int(cs.AEDeterminePermissionToAutomateTarget(ctypes.byref(desc), _fcc("****"),
                                                             _fcc("****"), 0))
    finally:
        cs.AEDisposeDesc(ctypes.byref(desc))


def _running(bid):
    import AppKit
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid)
    return apps[0] if apps else None


def status_with_app_running(bid):
    """The status needs a running target (-600 otherwise): launch it in the
    background if needed, read, and quit it again if we launched it."""
    launched = False
    if _running(bid) is None:
        subprocess.run(["/usr/bin/open", "-g", "-b", bid], capture_output=True, timeout=30)
        launched = True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _running(bid) is None:
            time.sleep(0.4)
        time.sleep(1.5)
    st = automation_status(bid)
    if launched and _running(bid) is not None:
        _running(bid).terminate()
        time.sleep(1.5)
    return st


def on_screen_owners():
    import Quartz
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                             Quartz.kCGNullWindowID) or []
    return sorted({str(w.get("kCGWindowOwnerName")) for w in wins})


def locked():
    import Quartz
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(d.get("CGSSessionScreenIsLocked"))


# ── analyse ────────────────────────────────────────────────────────────────

def osacompile(script: str, name: str):
    src = os.path.join(SCRATCH, "compile", name + ".applescript")
    out = os.path.join(SCRATCH, "compile", name + ".scpt")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(script)
    t0 = time.perf_counter()
    try:
        p = subprocess.run(["/usr/bin/osacompile", "-o", out, src], capture_output=True, text=True, timeout=30)
        return {"rc": p.returncode, "ms": _ms(t0), "stderr": p.stderr.strip()[-400:]}
    except subprocess.TimeoutExpired:
        return {"rc": None, "ms": _ms(t0), "stderr": "timeout 30 s"}


def cmd_analyse(args):
    from discover_desktop import read_scripting
    import AppKit
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(SCRATCH, exist_ok=True)
    cocoa = open(COCOA_STANDARD, encoding="utf-8").read() if os.path.exists(COCOA_STANDARD) else None
    owners_before = on_screen_owners()
    apps = []
    for bid in [b.strip() for b in args.bundles.split(",") if b.strip()]:
        print(f"== {bid}")
        url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
        if url is None:
            apps.append({"bundle_id": bid, "error": "no app for bundle id"})
            continue
        app_path = str(url.path())
        t0 = time.perf_counter()
        meta, sdef, suite = read_scripting(app_path)
        read_ms = _ms(t0)
        t0 = time.perf_counter()
        d = oc.dictionary_from_dump({"bundle_id": bid, "sdef": sdef, "script_suite": suite}, cocoa)
        r = oc.analyse_app(d, CLASSES.get(bid, []))
        analyse_ms = _ms(t0)
        checks = []
        for c in r["mapped"] + r["app_property_commands"]:
            name = c["id"].split("osa_command/")[-1].replace("/", "_").replace(" ", "_")
            chk = osacompile(c["script"], f"{bid}_{name}")
            c["osacompile"] = chk
            checks.append(chk)
            print(f"   {c['id']}: osacompile rc={chk['rc']} {chk['ms']} ms {chk['stderr'][:120]}")
        r["counts"]["osacompile_ok_mapped"] = sum(1 for c in r["mapped"] if c["osacompile"]["rc"] == 0)
        r["counts"]["osacompile_ok_app_property"] = sum(1 for c in r["app_property_commands"]
                                                        if c["osacompile"]["rc"] == 0)
        r.update({"app_path": app_path, "scripting_meta": meta,
                  "timing_ms": {"dictionary_read_ms": read_ms, "analyse_ms": analyse_ms,
                                "osacompile_ms": [c["ms"] for c in checks]}})
        r["automation_status"] = status_with_app_running(bid) if r["counts"]["commands"] else None
        print(f"   counts={json.dumps(r['counts'])} automation={r['automation_status']}")
        apps.append(r)
    owners_after = on_screen_owners()
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cocoa_standard_read": cocoa is not None,
           "on_screen_owners_before": owners_before, "on_screen_owners_after": owners_after,
           "apps": apps}
    with open(os.path.join(args.out, "osa_analysis.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("new on-screen owners:", sorted(set(owners_after) - set(owners_before)))
    return 0


# ── prove ──────────────────────────────────────────────────────────────────

def _osascript_runner(argv, script):
    p = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _gate():
    from anyuse.execution import hil_gate
    return hil_gate


def _prove_one(bid, cmd, status, mods, out):
    """GUI path = the class's existing flow; OSA path = ``run_osa``. Only
    reached with a granted status (``prove`` refuses first otherwise)."""
    from anyuse.settings import get as get_setting
    hil = _gate()
    root = os.path.join(SCRATCH, "prove")
    os.makedirs(root, exist_ok=True)
    gate = hil.HILGate(mode="all", approver=oc.disposable_approver(root, [cmd.id]))
    drive = bool(get_setting("ANYUSE_DRIVE_ENABLED"))
    inputs = PROOF_INPUTS[bid]

    def run(values):
        return oc.run_osa(cmd, values, permission=automation_status, runner=_osascript_runner, gate=gate,
                          drive_enabled=drive, scratch_root=root)

    timings = {}
    if bid == "com.apple.finder":
        from flows_more import flow3_finder_create_folder
        gui_dir, osa_dir = os.path.join(root, "finder"), os.path.join(root, "osa_finder")
        os.makedirs(osa_dir, exist_ok=True)
        start = {"gui": sorted(os.listdir(gui_dir)) if os.path.isdir(gui_dir) else [],
                 "osa": sorted(os.listdir(osa_dir))}
        ui = flow3_finder_create_folder(mods, root, 1, inputs, "none", out)
        by = {r["input"]: r for r in ui.get("runs", [])}

        def gui_fn(name):
            timings.setdefault("gui_ms", []).append((by.get(name) or {}).get("latency_ms"))
            return {"added": sorted((set(os.listdir(gui_dir)) - set(start["gui"])) & {f"{name}_0"})}

        def osa_fn(name):
            res = run([osa_dir, f"{name}_0"])
            timings.setdefault("osa_ms", []).append(res.ms)
            if not res.ok:
                return {"error": res.refused or res.stderr[-300:]}
            return {"added": sorted((set(os.listdir(osa_dir)) - set(start["osa"])) & {f"{name}_0"})}

        def expect(_s, name, obs):
            return None if obs.get("added") == [f"{name}_0"] else f"expected {name}_0 created, got {obs}"
        start_state = {"declared": "scratch folders as listed", **start}
    elif bid == "com.apple.TextEdit":
        from flows import flow1_textedit_write_save
        ui = flow1_textedit_write_save(mods, root, 1, inputs, "none", out)
        by = {r["input"]: r for r in ui.get("runs", [])}

        def gui_fn(w):
            timings.setdefault("gui_ms", []).append((by.get(w) or {}).get("latency_ms"))
            p = os.path.join(root, f"flow1_{w}_0.txt")
            return {"has_line": f"anyuse desktop validation {w} 0" in open(p).read()} if os.path.exists(p) else None

        def osa_fn(w):
            p = os.path.join(root, f"osa_{w}_0.txt")
            with open(p, "w") as fh:
                fh.write("seed\n")
            res = run([p, f"anyuse desktop validation {w} 0"])
            timings.setdefault("osa_ms", []).append(res.ms)
            if not res.ok:
                return {"error": res.refused or res.stderr[-300:]}
            return {"has_line": f"anyuse desktop validation {w} 0" in open(p).read()}

        def expect(_s, w, obs):
            return None if obs.get("has_line") is True else f"line for {w} not on disk: {obs}"
        start_state = {"declared": "a fresh file containing 'seed\\n' per run, TextEdit quit"}
    elif bid == "com.apple.Notes":
        from flows_more import NOTE_TITLES, flow5_notes_create, notes_rows
        start_rows = notes_rows()
        ui = flow5_notes_create(mods, root, 1, inputs, "none", out)
        by = {r["input"]: r for r in ui.get("runs", [])}

        def gui_fn(k):
            timings.setdefault("gui_ms", []).append((by.get(k) or {}).get("latency_ms"))
            t = f"{NOTE_TITLES[k]} 0"
            return {"rows": sorted({(a, "line" if f"validation {k} 0" in b else b) for a, b in notes_rows()
                                    if a == t})} if (by.get(k) or {}).get("success") else None

        def osa_fn(k):
            t = f"{NOTE_TITLES[k]} OSA 0"
            res = run([t, f"anyuse desktop validation {k} 0"])
            timings.setdefault("osa_ms", []).append(res.ms)
            if not res.ok:
                return {"error": res.refused or res.stderr[-300:]}
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                rows = {(a, "line" if f"validation {k} 0" in b else b) for a, b in notes_rows() if a == t}
                if rows:
                    return {"rows": sorted((a.replace(" OSA 0", " 0"), b) for a, b in rows)}
                time.sleep(2.0)
            return None

        def expect(_s, k, obs):
            want = [(f"{NOTE_TITLES[k]} 0", "line")]
            return None if obs.get("rows") == want else f"expected {want}, got {obs}"
        start_state = {"declared": "NoteStore rows before the flow", "rows": len(start_rows) or None}
    else:
        return None, f"no proof bound for {bid}"
    report = oc.prove(cmd, inputs, gui_fn, osa_fn, recorded_inputs=SEEN_INPUTS[bid], start_state=start_state,
                      expect=expect, automation_status=status)
    d = report.to_dict()
    d["timings"] = timings
    return d, None


def cmd_prove(args):
    os.makedirs(SCRATCH, exist_ok=True)
    print("LOCKED", locked())
    if locked():
        raise SystemExit("screen is locked: stop")
    with open(os.path.join(args.out, "osa_analysis.json")) as fh:
        analysis = json.load(fh)
    results = []
    mods = None
    for app in analysis["apps"]:
        bid = app.get("bundle_id")
        if bid not in PROOF_INPUTS:
            continue
        for c in app.get("mapped") or []:
            cmd = oc.OsaCommand(c["id"], c["bundle_id"], c["shape"], c["source"], c["effect"],
                                tuple(oc.ArgSpec(**a) for a in c["args"]), c["script"], tuple(c["uses"]),
                                c["observer"], c["reverse"], c["step_class"])
            t0 = time.perf_counter()
            status = status_with_app_running(bid)
            print(f"== prove {cmd.id}: automation status {status} {oc.AUTOMATION_STATUS.get(status, '')}")
            if status != oc.AUTOMATION_GRANTED:
                rep = oc.prove(cmd, PROOF_INPUTS[bid], None, None, recorded_inputs=SEEN_INPUTS[bid],
                               start_state=None, expect=lambda *a: None, automation_status=status).to_dict()
                why = None
            else:
                if mods is None:
                    from discover_desktop import _mods
                    mods = _mods()
                rep, why = _prove_one(bid, cmd, status, mods, args.out)
            cc = dd.ClassCandidate(id=cmd.id, bundle_id=bid, op_class="osa_command", subtype=cmd.shape,
                                   triggers=[f"aevt:{u}" for u in cmd.uses if len(u) == 8])
            if rep is None:
                cc.status, cc.proof_reason = "mapped", f"not proved: {why}"
            else:
                dd.attach_proof(cc, rep, PROOF_INPUTS[bid], SEEN_INPUTS[bid])
            row = cc.to_dict()
            row["proof_ms"] = _ms(t0)
            row["automation_status"] = status
            results.append(row)
            print(f"   status={cc.status} ({cc.proof_reason}) {row['proof_ms']:.0f} ms")
    with open(os.path.join(args.out, "osa_proved.json"), "w") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results}, fh,
                  indent=1, default=str)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyse")
    a.add_argument("--out", required=True)
    a.add_argument("--bundles", required=True)
    p = sub.add_parser("prove")
    p.add_argument("--out", required=True)
    args = ap.parse_args()
    refuse_unless_in_a_guest()
    return cmd_analyse(args) if args.cmd == "analyse" else cmd_prove(args)


if __name__ == "__main__":
    raise SystemExit(main())
