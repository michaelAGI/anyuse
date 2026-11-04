#!/usr/bin/env python3
"""The ten fixed desktop flows, recorded and replayed — INSIDE THE GUEST ONLY.

NOT FOR THE HOST. This presses real controls, types into real windows and opens
real menus in a live GUI session; `docs/TEST_POLICY.md` is the incident report
that explains why that must never happen on a machine somebody is using. It
refuses to start outside a VM.

This script runs all ten with one shared frame so the numbers are comparable:

  record     one input per flow, every target stored through the product's own
             vocabulary (`AccessibilityTree.to_selector`), plus an IDENTITY for
             the target that is independent of the matcher (AXIdentifier, a
             menu item's command character, a window's document) so a wrong
             resolution can be recognised before it is actuated;
  replay     inputs never recorded, ≥ 2 held back per flow, each resolved with
             `find_selector`, gated with `evaluate_drift`, actuated with
             `perform_action` / `enter_text`;
  verify     with a postcondition that does not consult the UI: the file on
             disk, Python's arithmetic, the Notes database, `sw_vers`, the
             fixture the page was generated from.

Wrong-target actions are the number this exists to produce: a step whose
resolved element is NOT the recorded one is refused and counted, never pressed.
Model calls are 0 structurally — no model SDK is installed in the guest.

usage: flows.py --out DIR [--runs 5] [--flows 1,2,3,...] [--perturb none|extra_doc|...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

_AX_SUCCESS = 0


def refuse_unless_in_a_guest():
    def sysctl(k):
        try:
            return subprocess.run(["/usr/sbin/sysctl", "-n", k], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    model, hv = sysctl("hw.model"), sysctl("kern.hv_vmm_present")
    if "virtualmac" in model.lower() or hv == "1":
        return
    raise SystemExit(f"REFUSING TO RUN: not a virtual machine "
                     f"(hw.model={model!r}, kern.hv_vmm_present={hv!r}).")


def _AS():
    import ApplicationServices as A
    return A


def _attr(el, name):
    try:
        err, val = _AS().AXUIElementCopyAttributeValue(el, name, None)
        return val if err == _AX_SUCCESS else None
    except Exception:
        return None


def _s(v):
    return None if v is None else str(v)


# ── app plumbing ────────────────────────────────────────────────────────────

class App:
    def __init__(self, bundle_id, acc):
        self.bundle_id, self.acc = bundle_id, acc

    def running(self):
        import AppKit
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(self.bundle_id)
        return apps[0] if apps else None

    def pid(self):
        a = self.running()
        return int(a.processIdentifier()) if a else None

    def quit(self, wait=2.5):
        """Graceful quit (NSRunningApplication.terminate — no Apple event, no
        TCC dialog). A killed TextEdit restores
        every window it had; a quit one does not."""
        a = self.running()
        if a is not None:
            a.terminate()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and self.running() is not None:
                time.sleep(0.3)
            time.sleep(wait)

    def launch(self, path=None, wait=3.5):
        args = ["/usr/bin/open", "-b", self.bundle_id] + ([path] if path else [])
        subprocess.run(args, capture_output=True, timeout=30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.pid():
                time.sleep(wait)
                return self.pid()
            time.sleep(0.4)
        return None

    def activate(self):
        import AppKit
        a = self.running()
        if a is not None:
            a.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.6)

    def tree(self):
        return self.acc.AccessibilityTree(pid=self.pid())


def all_refs(tree):
    descs, refs = [], []
    tree._walk(tree._root(), 0, descs, refs)
    return refs


def subtree(tree, root, skip_roles=("AXToolbar", "AXOutline", "AXScrollBar", "AXMenuBar")):
    """[(element, desc)] below ``root``, skipping whole subtrees of the given
    roles. `_ax_path` keeps only six ancestors, so a path PREFIX cannot say
    which window or list a deep element belongs to (runs 130/131: every
    collector that filtered on 'application/window[0]/…' saw nothing)."""
    out = []

    def rec(el, depth):
        if depth > 18 or len(out) > 3000:
            return
        d = tree.describe_element(el)
        if d.role in skip_roles:
            return
        out.append((el, d))
        for k in tree._children(el):
            rec(k, depth + 1)
    for k in tree._children(root):
        rec(k, 1)
    return out


def ax_dicts(refs):
    return [{"role": d.role, "title": d.title or ""} for _el, d in refs]


def make_state_graph(app, refs, ACTIONABLE_ROLES, sg):
    """A StateGraph holding one recorded state, fingerprinted the way the
    recorder fingerprints one (menu-bar descendants left out of the required
    anchors iff ANYUSE_DRIFT_EXCLUDE_MENUBAR is on, exactly as
    `AXSnapshot.anchor_elements` does)."""
    from anyuse.context.accessibility import drift_excludes_menubar
    live = ax_dicts(refs)
    excl = drift_excludes_menubar()
    # the AX identifier too (draft_compile.choice_domain binds a choice by it)
    actionable = [{"role": d.role, "title": d.title or "", "identifier": d.identifier or ""}
                  for _e, d in refs if d.role in ACTIONABLE_ROLES and not (excl and d.in_menubar)]
    fp = sg.UIFingerprint(
        required_elements=actionable,
        element_count_range=(int(len(live) * 0.6), int(len(live) * 1.4) + 10),
        structural_hash=sg.compute_structural_hash(live),
        role_frequency=sg._role_frequency(live))
    state = sg.UIState(state_id=f"{app}_recorded", app=app, fingerprint=fp)
    graph = sg.StateGraph()
    graph.add_state(state)
    return graph, state.state_id


def menu_bar_item(tree, titles):
    for el, d in all_refs(tree):
        if d.role == "AXMenuBarItem" and (d.title or "") in titles:
            return el, d
    return None, None


def menu_item_by_cmd(tree, cmd_char, modifiers=("", "0")):
    """A menu item by command character + modifier code, after the menu was
    opened. Language-free: 'Sichern' and 'Save' are both ⌘S."""
    for el, d in all_refs(tree):
        if d.role != "AXMenuItem":
            continue
        c = str(_attr(el, "AXMenuItemCmdChar") or "")
        m = str(_attr(el, "AXMenuItemCmdModifiers") or "").strip()
        if c.lower() == cmd_char.lower() and m in modifiers:
            return el, d
    return None, None


def item_identity(el, d):
    """Matcher-independent identity of a menu item / control."""
    return {"role": d.role, "identifier": d.identifier or "",
            "cmd": str(_attr(el, "AXMenuItemCmdChar") or ""),
            "mods": str(_attr(el, "AXMenuItemCmdModifiers") or "").strip(),
            "title": d.title or ""}


def same_identity(a, b):
    if a["role"] != b["role"]:
        return False
    if a["cmd"] or b["cmd"]:
        return a["cmd"].lower() == b["cmd"].lower() and a["mods"] == b["mods"]
    if a["identifier"] and b["identifier"]:
        return a["identifier"] == b["identifier"]
    return a["title"] == b["title"]


class Recorder:
    """Records selectors + identities through the product's vocabulary."""

    def __init__(self, tree):
        self.tree = tree
        self.steps = {}

    def add(self, name, el, d=None):
        d = d or self.tree.describe_element(el)
        self.steps[name] = {"selector": self.tree.to_selector(el).to_dict(),
                            "identity": item_identity(el, d)}
        return self.steps[name]


class Replayer:
    """Resolves recorded steps against the live tree with the gate and the
    wrong-target check in front of every actuation."""

    def __init__(self, tree, recorded, graph, state_id, acc, drift):
        self.tree, self.rec, self.graph, self.state_id = tree, recorded, graph, state_id
        self.acc, self.drift = acc, drift
        self.wrong_target = self.unresolved = self.gate_fired = 0
        self.gate_reasons = []
        self.steps = []

    def gate(self):
        report = self.drift.evaluate_drift(self.graph, self.state_id, ax_dicts(all_refs(self.tree)))
        if report.drifted:
            self.gate_fired += 1
            self.gate_reasons.append(str(report.reason))
        return report

    def _outcome(self, el, want):
        """right / wrong / none for a resolution, against the recorded identity."""
        if el is None:
            return "none", None
        got = item_identity(el, self.tree.describe_element(el))
        return ("right" if same_identity(got, want) else "wrong"), got

    def resolve(self, name):
        """The live element for a recorded step, or None; a wrong identity is
        refused (and counted) before anything can be pressed.

        Both matchers are always run and scored against the recorded identity:
        the cascade (today's default) and the weighted scorer (the proposed
        default). Which one's answer is ACTUATED follows ANYUSE_ELEMENT_MATCHER.
        Scoring the other one costs a second tree walk and nothing else."""
        step = self.rec[name]
        sel = self.acc.Selector.from_dict(step["selector"])
        from anyuse.context.element_scorer import matcher_mode
        active = matcher_mode()
        el_c = self.tree.find_selector(sel, mode="cascade")
        oc, got_c = self._outcome(el_c, step["identity"])
        el_w = self.tree.find_selector(sel, mode="weighted")
        ow, got_w = self._outcome(el_w, step["identity"])
        match = self.tree.last_match.to_dict() if self.tree.last_match is not None else None
        rec = {"step": name, "matcher": active, "cascade": oc, "weighted": ow, "weighted_match": match}
        el, outcome = (el_w, ow) if active == "weighted" else (el_c, oc)
        if outcome == "none":
            self.unresolved += 1
            rec["resolved"] = False
            self.steps.append(rec)
            return None
        if outcome == "wrong":
            self.wrong_target += 1
            rec.update({"resolved": True, "wrong_target": True,
                        "got": got_w if active == "weighted" else got_c, "want": step["identity"]})
            self.steps.append(rec)
            return None
        rec.update({"resolved": True, "wrong_target": False})
        self.steps.append(rec)
        return el

    def press(self, name, settle=0.5):
        el = self.resolve(name)
        ok = self.tree.perform_action(el, "AXPress") if el is not None else False
        time.sleep(settle)
        return ok

    def counts(self):
        heal = {"cascade": {"right": 0, "wrong": 0, "none": 0},
                "weighted": {"right": 0, "wrong": 0, "none": 0}}
        for st in self.steps:
            for k in ("cascade", "weighted"):
                if k in st:
                    heal[k][st[k]] += 1
        return {"wrong_target": self.wrong_target, "unresolved": self.unresolved,
                "drift_gate_fired": self.gate_fired, "gate_reasons": sorted(set(self.gate_reasons)),
                "resolutions": heal, "steps": list(self.steps)}


# ── flow 1: TextEdit write + save ───────────────────────────────────────────

TE = "com.apple.TextEdit"


def textedit_area_in_front_window(tree):
    """The text area of the FRONT document window (AXFocusedWindow), not the
    first AXTextArea in the tree — those are the same only when one document
    is open."""
    app = tree._root()
    win = _attr(app, "AXFocusedWindow")
    for el, d in all_refs(tree):
        if d.role != "AXTextArea":
            continue
        if win is None or _attr(el, "AXWindow") == win:
            return el, d
    return None, None


def flow1_textedit_write_save(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    app = App(TE, acc)
    app.quit()
    path = os.path.join(scratch, "flow1_rec.txt")
    open(path, "w").write("seed\n")
    app.launch(path)
    tree = app.tree()
    t0 = time.monotonic()
    rec = Recorder(tree)
    area, ad = textedit_area_in_front_window(tree)
    rec.add("area", area, ad)
    fm, fd = menu_bar_item(tree, ("File", "Ablage"))
    rec.add("file_menu", fm, fd)
    _AS().AXUIElementPerformAction(fm, "AXPress"); time.sleep(1.0)
    sv, sd = menu_item_by_cmd(tree, "s")
    rec.add("save", sv, sd)
    _AS().AXUIElementPerformAction(fm, "AXCancel"); time.sleep(0.4)
    refs = all_refs(tree)
    graph, sid = make_state_graph("TextEdit", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    # the recorded input itself, so the recording is a real one
    line_rec = "anyuse desktop validation alpha"
    ev = tree.enter_text(area, line_rec)
    _AS().AXUIElementPerformAction(fm, "AXPress"); time.sleep(1.0)
    sv, _ = menu_item_by_cmd(tree, "s"); _AS().AXUIElementPerformAction(sv, "AXPress"); time.sleep(1.5)
    recorded_ok = line_rec in open(path).read()
    app.quit()

    results = []
    for word in fresh_inputs:
        for i in range(runs):
            line = f"anyuse desktop validation {word} {i}"
            doc = os.path.join(scratch, f"flow1_{word}_{i}.txt")
            open(doc, "w").write("seed\n")
            other = None
            if perturb == "extra_doc":
                # a SECOND document, opened AFTER the target so it is in front
                other = os.path.join(scratch, f"flow1_other_{word}_{i}.txt")
                open(other, "w").write("the other document\n")
            app.launch(doc)
            if other:
                app.launch(other, wait=2.5)
            tree = app.tree()
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            area = rp.resolve("area")
            entry, healed = None, False
            if area is not None:
                def verify(_area=area):
                    # the app's own save through the recorded menu steps, then the disk
                    if not rp.press("file_menu", settle=1.0):
                        return False
                    if not rp.press("save", settle=1.5):
                        _AS().AXUIElementPerformAction(tree.find_selector(
                            acc.Selector.from_dict(rec.steps["file_menu"]["selector"])), "AXCancel")
                        return False
                    return line in open(doc).read()
                entry = tree.enter_text(area, line, verify=verify, expect_document=doc)
                if entry.wrong_target:
                    # refused (counted, nothing written); recover by resolving the
                    # same selector INSIDE the intended document's window
                    rp.wrong_target += 1
                    scoped = tree.find_selector(
                        acc.Selector.from_dict(rec.steps["area"]["selector"]), document=doc)
                    if scoped is not None:
                        entry = tree.enter_text(scoped, line, verify=verify, expect_document=doc)
                        healed = entry.success
            on_disk = open(doc).read()
            other_disk = open(other).read() if other else None
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": word, "run": i, "success": line in on_disk,
                 "latency_ms": round(elapsed, 1), "model_calls": 0,
                 "entry": entry.to_dict() if entry is not None else None, "healed_by_document": healed,
                 "other_document_untouched": (other_disk == "the other document\n") if other else None,
                 **rp.counts()}
            results.append(r)
            print(f"  flow1 {word}{i}: rung={entry.rung if entry is not None else '-'} indep={entry.verified_independently if entry is not None else '-'} "
                  f"disk_ok={r['success']} wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
            app.quit()
    return {"flow": "1 textedit_write_save", "app": "TextEdit", "rung": "ax",
            "recorded_input": "alpha", "recorded_ok": recorded_ok, "record_ms": round(record_ms, 1),
            "recorded_steps": rec.steps, "perturbation": perturb, "runs": results}


# ── flow 2: TextEdit find a word, report the paragraph ─────────────────────

PARAS = ["Der schnelle braune Fuchs springt über den faulen Hund.",
         "Ein Wolkenbruch über dem Hafen ließ alle Kräne stillstehen.",
         "Die Bibliothek öffnet montags erst um elf Uhr.",
         "Am Ende des Ganges brannte noch eine einzige Lampe.",
         "Niemand hatte mit dem Zug um sechs gerechnet."]


def flow2_textedit_find(mods, scratch, runs, fresh_inputs, perturb, out):
    """Open a document, find <word> with the find bar, report the paragraph.
    Postcondition: the reported paragraph equals the one Python finds in the file."""
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    from anyuse.context import keyboard as kb
    app = App(TE, acc)
    app.quit()
    path = os.path.join(scratch, "flow2_doc.txt")
    open(path, "w").write("\n\n".join(PARAS) + "\n")
    app.launch(path)
    tree = app.tree()
    t0 = time.monotonic()
    rec = Recorder(tree)
    area, ad = textedit_area_in_front_window(tree)
    rec.add("area", area, ad)
    em, ed = menu_bar_item(tree, ("Edit", "Bearbeiten"))
    rec.add("edit_menu", em, ed)
    _AS().AXUIElementPerformAction(em, "AXPress"); time.sleep(1.0)
    # 'Suchen' is a submenu; its first item 'Suchen…' is ⌘F
    fi, fd = menu_item_by_cmd(tree, "f")
    rec.add("find_item", fi, fd)
    _AS().AXUIElementPerformAction(em, "AXCancel"); time.sleep(0.4)
    # open the find bar once to record its search field
    kb.press_key(3, _quartz().kCGEventFlagMaskCommand)  # ⌘F
    time.sleep(1.2)
    # The find bar's field, INSIDE the document window. The Help menu carries a
    # search field with the same identifier (_SC_SEARCH_FIELD) under the menu
    # bar; recording that one made every replay a refused
    # wrong-target write, which is the guard doing its job.
    front = _attr(tree._root(), "AXFocusedWindow")
    sf, sd = next(((el, d) for el, d in all_refs(tree)
                   if d.role in ("AXTextField", "AXSearchField") and not d.in_menubar
                   and _attr(el, "AXWindow") == front), (None, None))
    if sf is None:
        return {"flow": "2 textedit_find", "error": "no search field in the document window after ⌘F"}
    rec.add("search_field", sf, sd)
    refs = all_refs(tree)
    graph, sid = make_state_graph("TextEdit", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    kb.press_key(53)  # escape closes the find bar
    time.sleep(0.5)

    def paragraph_from_file(word):
        text = open(path).read()
        for para in text.split("\n\n"):
            if word in para:
                return para.strip()
        return None

    results = []
    for word in fresh_inputs:
        for i in range(runs):
            tree = app.tree()
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            rp.gate()
            area = rp.resolve("area")
            reported, entry_d = None, None
            if area is not None:
                _AS().AXUIElementSetAttributeValue(area, "AXFocused", True); time.sleep(0.2)
                kb.press_key(3, _quartz().kCGEventFlagMaskCommand); time.sleep(1.0)
                sf = rp.resolve("search_field")
                loc = -1
                if sf is not None:
                    # The find bar's field takes an AXValue write and reads it back,
                    # and the search never runs — TextEdit searches on the text the
                    # user TYPES. Same class as Finder's search field and Safari's
                    # address field. Independent check for the rung: the
                    # selection moved onto the word.
                    def _found():
                        kb.press_key(36)   # return: find next
                        deadline = time.monotonic() + 2.5
                        while time.monotonic() < deadline:   # polled: a single read came too early in 3/15 replays
                            l = _cfrange_location(_attr(area, "AXSelectedTextRange"))
                            full = _s(_attr(area, "AXValue")) or ""
                            if l >= 0 and full[l:l + len(word)] == word:
                                return True
                            time.sleep(0.3)
                        return False
                    ev = tree.enter_text(sf, word, verify=_found, rungs=("ax_value", "keystrokes"))
                    entry_d = ev.to_dict()
                    rng = _attr(area, "AXSelectedTextRange")
                    loc = _cfrange_location(rng)
                    full = _s(_attr(area, "AXValue")) or ""
                    if loc >= 0:
                        start = full.rfind("\n\n", 0, loc)
                        start = 0 if start < 0 else start + 2
                        end = full.find("\n\n", loc)
                        end = len(full) if end < 0 else end
                        reported = full[start:end].strip()
                kb.press_key(53); time.sleep(0.3)
            expected = paragraph_from_file(word)
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": word, "run": i, "success": reported is not None and reported == expected,
                 "reported": reported, "expected": expected, "latency_ms": round(elapsed, 1),
                 "model_calls": 0, "entry": locals().get("entry_d"), **rp.counts()}
            results.append(r)
            print(f"  flow2 {word}{i}: {'OK' if r['success'] else 'FAIL'} reported={reported!r} wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    app.quit()
    return {"flow": "2 textedit_find", "app": "TextEdit", "rung": "ax", "recorded_input": "Fuchs",
            "record_ms": round(record_ms, 1), "recorded_steps": rec.steps, "perturbation": perturb, "runs": results}


def _quartz():
    import Quartz
    return Quartz


def _cfrange_location(axvalue):
    """location of an AXValue holding a CFRange; pyobjc hands it back as a
    struct with .location, as a (loc, len) tuple, or only via repr."""
    if axvalue is None:
        return -1
    try:
        ok, cf = _AS().AXValueGetValue(axvalue, _AS().kAXValueCFRangeType, None)
        if ok and cf is not None:
            if hasattr(cf, "location"):
                return int(cf.location)
            return int(cf[0])
    except Exception:
        pass
    m = re.search(r"(?:loc|location)[=:]\s*(\d+)", repr(axvalue))
    return int(m.group(1)) if m else -1


# ── flow 7: Calculator (same harness) ──────────────────────────────────────

CALC = "com.apple.calculator"
CALC_IDS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five",
            "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine", "+": "Add", "-": "Subtract",
            "*": "Multiply", "/": "Divide", "=": "Equals", "AC": "AllClear"}


def _num(text):
    t = (text or "").replace("‎", "").replace(" ", "").replace(" ", "")
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        return None


def flow7_calculator(mods, scratch, runs, fresh_inputs, perturb, out):
    acc, sg, drift = mods["acc"], mods["sg"], mods["drift"]
    app = App(CALC, acc)
    app.quit(); app.launch(); app.activate()
    tree = app.tree()
    t0 = time.monotonic()
    rec = Recorder(tree)
    want = {v: k for k, v in CALC_IDS.items()}
    for el, d in all_refs(tree):
        if d.role == "AXButton" and want.get((d.identifier or "").strip()) and want[d.identifier.strip()] not in rec.steps:
            rec.add(want[d.identifier.strip()], el, d)
    refs = all_refs(tree)
    graph, sid = make_state_graph("Calculator", refs, acc.ACTIONABLE_ROLES, sg)
    record_ms = (time.monotonic() - t0) * 1000.0
    results = []
    for a, op, b in fresh_inputs:
        for i in range(runs):
            expected = {"+": a + b, "-": a - b, "*": a * b, "/": a / b}[op]
            seq = ["AC"] + list(str(a)) + [op] + list(str(b)) + ["="]
            rp = Replayer(tree, rec.steps, graph, sid, acc, drift)
            t_start = time.monotonic()
            for key in seq:
                rp.gate()
                rp.press(key, settle=0.12)
            time.sleep(0.5)
            shown = [(_s(d.value) or "").strip() for _e, d in all_refs(tree)
                     if d.role in ("AXStaticText", "AXTextField") and (_s(d.value) or "").strip()]
            hit = next((s for s in shown if _num(s) is not None and abs(_num(s) - expected) < 1e-6), None)
            elapsed = (time.monotonic() - t_start) * 1000.0
            r = {"input": f"{a}{op}{b}", "run": i, "success": hit is not None, "expected": expected,
                 "display": shown, "latency_ms": round(elapsed, 1), "model_calls": 0, **rp.counts()}
            results.append(r)
            print(f"  flow7 {a}{op}{b} r{i}: {'OK' if r['success'] else 'FAIL'} shown={shown} wrong_target={r['wrong_target']} gate={r['drift_gate_fired']} {elapsed:.0f} ms")
    app.quit()
    return {"flow": "7 calculator_arithmetic", "app": "Calculator", "rung": "ax", "recorded_input": "12+30",
            "record_ms": round(record_ms, 1), "selectors": len(rec.steps), "perturbation": perturb, "runs": results}


# ── main ────────────────────────────────────────────────────────────────────

FLOWS = {
    "1": (flow1_textedit_write_save, ["gamma", "delta", "epsilon"]),
    "2": (flow2_textedit_find, ["Wolkenbruch", "Bibliothek", "Lampe"]),
    "7": (flow7_calculator, [(7, "*", 6), (144, "/", 12), (99, "-", 57)]),
}


def _all_flows():
    """flows 3-6, 8-10 and H live in flows_more.py (same frame, second file)."""
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from flows_more import MORE_FLOWS
    return {**FLOWS, **MORE_FLOWS}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--flows", default="1,2,7")
    ap.add_argument("--perturb", default="none")
    ap.add_argument("--scratch", default=os.path.expanduser("~/anyuse-guest/scratch/flows"))
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    refuse_unless_in_a_guest()
    from anyuse.context import accessibility as acc
    from anyuse.guides import state_graph as sg
    from anyuse.execution import drift_gate as drift
    if not acc.ax_trusted():
        raise SystemExit("REFUSING TO RUN: Accessibility is not granted.")
    from anyuse.settings import get as get_setting
    if not get_setting("ANYUSE_DRIVE_ENABLED"):
        raise SystemExit("ANYUSE_DRIVE_ENABLED is off — nothing would actuate.")
    mods = {"acc": acc, "sg": sg, "drift": drift}
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.scratch, exist_ok=True)
    # third-party modules this needs beyond pyobjc: none
    try:
        from anyuse.context import keyboard as kb
        kb.release_modifiers()
    except Exception:
        pass
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "runs_per_input": args.runs,
              "perturbation": args.perturb, "flows": []}
    flows = _all_flows()
    for key in [f.strip() for f in args.flows.split(",") if f.strip()]:
        fn, inputs = flows[key]
        print(f"flow {key}: {fn.__name__} fresh inputs {inputs!r}")
        try:
            report["flows"].append(fn(mods, args.scratch, 1 if key == "H" else args.runs, inputs,
                                      args.perturb, args.out))
        except Exception as exc:  # noqa: BLE001
            import traceback
            report["flows"].append({"flow": key, "error": f"{type(exc).__name__}: {exc}",
                                    "traceback": traceback.format_exc()})
            print(f"  flow {key} ERROR {type(exc).__name__}: {exc}")
    name = f"flows{('_' + args.tag) if args.tag else ''}{('_' + args.perturb) if args.perturb != 'none' else ''}.json"
    path = os.path.join(args.out, name)
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"\nreport -> {path}")
    for f in report["flows"]:
        if "error" in f:
            print(f"  {f['flow']}: ERROR {f['error']}")
            continue
        rs = f.get("runs")
        if rs is None:
            print(f"  {f['flow']}: arms={[(a['arm'], a['step_class'], a['actuate']) for a in f.get('arms', [])]} "
                  f"victims_still_exist={f.get('victims_still_exist')}")
            continue
        ok = sum(1 for r in rs if r.get("success"))
        wt = sum(r.get("wrong_target", 0) for r in rs)
        print(f"  {f['flow']}: {ok}/{len(rs)} success, wrong-target {wt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
