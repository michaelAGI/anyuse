"""Defects found in the guest VM, each fixed generally; offline with FAKES.

* the frontmost app was read from ``NSWorkspace.frontmostApplication``, which only a run loop refreshes. The
  guest job agent (and any CLI replay) spins none, so after Notes had been in front once, a Finder folder name's
  Return was refused as "keystrokes would go to com.apple.Notes". The tree now reads the system-wide element's
  ``AXFocusedApplication`` live; NSWorkspace is the fallback only.
* the guest job agent found "the ONE running process" of a tool's app in ``NSWorkspace.runningApplications``,
  equally stale: a TextEdit relaunched by the clean start was "0 running processes", so ``ax_elements``,
  ``run_tool`` and ``replay_draft`` could not bind to it. It now asks
  ``NSRunningApplication.runningApplicationsWithBundleIdentifier_`` (LaunchServices, live), as ``running`` does.
* a compiled tool's steps were bound to the record's app ONE BY ONE, right before each ran: a three-step record
  whose second step names another app typed its first step into the document before the refusal. Every step is
  bound first (``bind_steps``), in the guest's ``run_tool`` and in the server's injected path.
* a map-compiled menu hop named its menu-bar item and submenus by TITLE only (the probes and dumps hold no
  identifier for them), while the live AppKit items carry generated ``_NS:<n>`` ids (TextEdit's "Ablage" is
  ``_NS:860``); the identity rule (an identifier on either side is decisive) made every map-compiled menu press "no
  candidate". The hops now carry their ``menu_path``, and an identifier-less selector
  PINNED to a menu place matches the live element at that place with the same name when its id is AppKit-generated;
  a developer-given id, another place, another name, or a selector pinned to no place: refused as before.
* a row's label came from its first cell's AXDescription, which Notes' folder outline fills with a summary
  ("Museum Trip Notes, 4 Notizen") around the name's own text field, so every folder ``select_row`` was not_found.
  A description that begins with the cell's first text and adds to it now yields that text; any other
  description is still the label.
* the map's SYNTHETIC menu-group state (its probed options as required elements, an 8-48 element range, a hash of
  the options) was used as a live drift baseline: the live Finder app (hundreds of elements) "drifted" at every
  map-compiled sort. A synthetic baseline is now judged only on its required elements (the options are
  shown); a recorded baseline is judged as before.
* the Return right after a focused typing step is refused unless the focus is STILL on the typed-into field; the
  check compared element REFERENCES, and Finder re-creates its toolbar search field (``_NS:122``) once its search
  runs AND focuses that field a moment after Ablage > Suchen, so the context read before the typing saw the
  application itself and the Return was refused as "the focus moved" (Notes passed). The context now
  waits (briefly) for the focus to settle into a text role and records the field's identity; the same identified
  field (role, raw role, identifier, app; same window) counts as unmoved. A field without an identifier, another
  field (a Tab) or another window: refused as before.
* the agent harness's Claude CLI policy started ``claude -p`` without ``--safe-mode`` / ``--strict-mcp-config``, so
  each decision carried the person's own configuration (the CLI counted US$0.36 for one desktop decision) and the
  guest's screen text met their loaded connectors; and the per-run budget was a constant US$0.05, so a desktop
  check stopped after its first decision. The argv now isolates the CLI as reviewer R and the
  fill relay do, and ``ANYUSE_AGENT_MAX_USD`` (security-relevant to raise) sets the budget.
* the agent harness on the desktop could not save: the guest offered the WHOLE menu bar (289 TextEdit items,
  the Apple menu and the Services submenu first) and the policy saw the first 60 (Ablage > Sichern was item 109);
  and ``AxRead`` called the guest's ``read_text`` without the bundle id it needs, so every read failed. The guest no
  longer offers the Apple menu (shut down, restart, log out, force quit) or Services, a desktop observation is
  rendered with up to 220 elements, and ``AxRead`` names the app's bundle id.
* the harness-compiled workflow drifted at its first replay step ("required_missing"): its baseline labelled
  each element as the POLICY reads it (AXTitle, else AXDescription: TextEdit's title-bar button "Dokumentaktionen"),
  the drift gate's live capture by AXTitle alone; and its element count and structural hash came from the guest's
  offered elements, not the app tree the gate counts (count_out_of_range: 367 live vs 150-361). The guest now returns
  the recorder's fingerprint of the live app (the gate's own capture) with each screen read, the baseline is that
  fingerprint, and an older guest's rows are labelled by their raw AXTitle.
* a recorded fingerprint required every actionable element, among them TextEdit's recent documents (Ablage >
  Benutzte Dokumente, AppKit identifier ``_recentItemRequested:``): every document opened since the recording made
  them "missing", and the recorded workflow drifted at its first replay step. Such entries never anchor a
  fingerprint now (the snapshot's anchors and the drive harness's fingerprint).

No desktop, no AX, no subprocess, no network, no VM.
"""
import sys
import types

import pytest

from context import accessibility as acc
from anyuse.context.accessibility import Selector
from anyuse.context import accessibility as acc_pkg  # noqa: F401
from anyuse.execution.executor import StateGraphExecutor

FINDER, NOTES = "com.apple.finder", "com.apple.Notes"


def fakes(focused_pid=777, stale=NOTES, ax_ok=True, tick=True):
    pids = {777: FINDER, 888: NOTES}

    class AX:
        @staticmethod
        def AXUIElementCreateSystemWide():
            return "system-wide"

        @staticmethod
        def AXUIElementCopyAttributeValue(el, attr, _none):
            if el == "system-wide" and attr == "AXFocusedApplication" and ax_ok:
                return 0, ("app", focused_pid)
            return -25204, None

        @staticmethod
        def AXUIElementGetPid(el, _none):
            return 0, el[1]

    class Running:
        def __init__(self, bid):
            self.bid = bid

        def bundleIdentifier(self):
            return self.bid

    ticked = []

    class Workspace:
        def frontmostApplication(self):
            # stale (``stale``) until a run-loop tick let it take the switch, then the focused app
            return Running(pids.get(focused_pid, stale) if ticked else stale)

        def menuBarOwningApplication(self):
            return Running(pids.get(focused_pid, stale))

    class Loop:
        def runUntilDate_(self, _date):
            ticked.append(1)

    kit = types.SimpleNamespace(
        NSRunningApplication=types.SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: Running(pids[pid]) if pid in pids else None),
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: Workspace()),
        NSRunLoop=types.SimpleNamespace(currentRunLoop=lambda: Loop()) if tick else None,
        NSDate=types.SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda s: s))
    return AX, kit


@pytest.fixture
def tree(monkeypatch):
    def install(**kw):
        ax, kit = fakes(**kw)
        monkeypatch.setattr(acc, "_ax", lambda: ax)
        monkeypatch.setattr(acc, "_appkit", lambda: kit)
        return acc.AccessibilityTree.__new__(acc.AccessibilityTree)
    return install


def test_the_frontmost_app_is_read_live_not_from_a_stale_workspace(tree):
    t = tree(focused_pid=777, stale=NOTES)          # Finder focused NOW; the workspace still says Notes
    assert acc.frontmost_bundle_live() == FINDER
    assert t.frontmost_bundle_id() == FINDER


def test_the_workspace_is_only_the_fallback_when_ax_gives_no_answer(tree):
    # the fallback is the workspace read AFTER a run-loop tick,
    # cross-checked by the menu-bar owner; the untickled read (stale: Notes) is never an answer on its own
    t = tree(ax_ok=False, stale=NOTES)
    assert acc.frontmost_bundle_live() is None
    assert t.frontmost_bundle_id() == FINDER and acc.frontmost_reading()["front_source"] == "nsworkspace_tick"
    # no tick: neither workspace read answers (the menu-bar owner is as stale); the window server does
    t = tree(ax_ok=False, stale=NOTES, tick=False)
    acc_ws = acc.window_server_front
    acc.window_server_front = lambda: FINDER
    try:
        r = acc.frontmost_reading()
        assert t.frontmost_bundle_id() == FINDER and r["front_source"] == "window_server"
        assert r["reads"]["menubar_owner"] is None and r["reads"]["nsworkspace_tick"] is None
        acc.window_server_front = lambda: None
        assert acc.frontmost_reading()["bundle_id"] is None
    finally:
        acc.window_server_front = acc_ws
    t = tree(focused_pid=999)                        # a pid no running app has: no live answer
    assert acc.frontmost_bundle_live() is None


def test_the_keystroke_guard_lets_the_recorded_app_type_and_still_refuses_another(tree):
    t = tree(focused_pid=777, stale=NOTES)
    t.bundle_id = lambda: FINDER
    ex = StateGraphExecutor.__new__(StateGraphExecutor)
    ex._ax_tree = t
    assert ex._app_document_refusal(Selector(app=FINDER), element=None, keystrokes=True) is None
    t = tree(focused_pid=888, stale=FINDER)         # Notes really in front: refused, whatever the workspace says
    t.bundle_id = lambda: FINDER
    ex._ax_tree = t
    why = ex._app_document_refusal(Selector(app=FINDER), element=None, keystrokes=True)
    assert why and why[1] == "wrong_app" and NOTES in why[0]


def test_the_guest_finds_a_relaunched_apps_one_process_live(monkeypatch):
    import test_desktop_tools_mcp as TM
    J = TM._job()

    class App:
        def __init__(self, pid, bid="com.apple.TextEdit"):
            self.pid, self.bid = pid, bid

        def bundleIdentifier(self):
            return self.bid

        def processIdentifier(self):
            return self.pid
    live = {"com.apple.TextEdit": [App(16705)]}
    kit = types.SimpleNamespace(
        NSRunningApplication=types.SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda b: live.get(b, [])),
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            runningApplications=lambda: [App(10001)])))            # stale: the pid before the relaunch
    monkeypatch.setitem(sys.modules, "AppKit", kit)
    assert J.app_pid({"bundle_id": "com.apple.TextEdit"}) == (16705, None)
    live["com.apple.TextEdit"] = []
    pid, why = J.app_pid({"bundle_id": "com.apple.TextEdit"})
    assert pid is None and "0 running processes" in why
    live["com.apple.TextEdit"] = [App(1), App(2)]
    pid, why = J.app_pid({"bundle_id": "com.apple.TextEdit"})
    assert pid is None and "2 running processes" in why               # never a guess between two


def _mixed_record():
    from anyuse.mcpserver import desktop_compiled_tools as DT
    area = {"canonical_role": "textarea", "name": "", "identifier": "First Text View", "path": "", "raw_role": "AXTextArea"}
    steps = [{"intent": "type", "source": "workflow", "target_element": "", "modality": "ax", "input_data": "${text}",
              "command": {"action": "enter_text", "selector": dict(area)}},
             {"intent": "menu", "source": "workflow", "target_element": "", "modality": "ax",
              "command": {"action": "press", "app": NOTES, "selector": {"canonical_role": "menubaritem", "name": "Ablage"}}}]
    return DT.compiled_desktop_record(
        workflow="mixed", compile_class="file_write", parameters=[{"name": "text", "type": "string"}], steps=steps,
        workflow_template={"id": "mixed", "name": "mixed", "required_app": "TextEdit"}, state_graph={},
        proof={"proved": True, "fresh_inputs": 0}, extra={"app": {"name": "TextEdit", "bundle_id": "com.apple.TextEdit"}})


def test_a_step_naming_another_app_refuses_the_whole_tool_before_any_step_runs(monkeypatch):
    import asyncio
    import test_desktop_tools_mcp as TM
    from anyuse.mcpserver import desktop_compiled_tools as DT
    rec = _mixed_record()
    steps, why = DT.bind_steps(rec, {"text": "x"})
    assert steps is None and "com.apple.Notes" in why
    ok_steps, ok_why = DT.bind_steps(dict(rec, steps=rec["steps"][:1]), {"text": "x"})
    assert ok_why is None and ok_steps[0]["command"]["selector"]["app"] == "com.apple.TextEdit"
    assert ok_steps[0]["input_data"] == "x"

    class Spy:
        def __init__(self):
            self.ran = []

        async def execute_checked_step(self, step, **kw):
            self.ran.append(step)
            return {"success": True}, {}
    spy = Spy()
    J = TM._job()
    results, ok, why = asyncio.run(J._run_checked(rec, {"text": "x"}, spy))
    assert not ok and results == [] and spy.ran == [] and "com.apple.Notes" in why


def test_map_menu_hops_carry_their_menu_place():
    from anyuse.execution import map_compile as M
    hops = M.menu_hops("Bearbeiten > Ersetzungen > Intelligente Bindestriche", "_NS:668")
    assert [h["selector"]["menu_path"] for h in hops] == [[], ["Bearbeiten"], ["Bearbeiten", "Ersetzungen"]]
    assert [h["selector"]["identifier"] for h in hops] == ["", "", "_NS:668"]
    assert hops[0]["selector"]["raw_role"] == "AXMenuBarItem"


def test_a_map_compiled_record_keeps_the_menu_place_of_its_steps():
    import asyncio
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import map_compile as M
    amap = M.AppMap(app="TextEdit")
    amap.merge(M.from_effect_map(app="TextEdit"))
    p = M.plan("Turn off smart dashes, then write 'Stand: final' into the document and save.", amap)
    draft = M.draft_from_plan(p, amap, "vc_d4", p.task, app="TextEdit")
    import tempfile
    from anyuse.memory.storage import WorkflowStorage
    storage = WorkflowStorage(base_dir=tempfile.mkdtemp(prefix="anyuse-vc-d4-"))
    rep = asyncio.run(DC.draft_to_workflow(draft, overrides=M.overrides_from_plan(p, amap, "TextEdit"), save=True,
                                           storage=storage, name="vc_d4"))
    sels = [s["command"].get("selector") or {} for s in DC.find_saved("vc_d4", storage)[1]["steps"]]
    menus = [s for s in sels if s.get("canonical_role") == "menuitem"]
    assert [s.get("menu_path") for s in menus] == [[], ["Bearbeiten"], ["Bearbeiten", "Ersetzungen"], [], ["Ablage"]]
    assert rep["status"] == "ready", rep["reasons"]                      # the offline proof still passes
    assert DC._selector_dict({"canonical_role": "menuitem", "name": "x", "menu_path": "Ablage"}).get("menu_path") is None


def test_the_identity_rule_accepts_an_appkit_id_only_at_the_pinned_menu_place():
    from anyuse.execution.executor import _same_identity
    rec = Selector(canonical_role="menuitem", name="Ablage", identifier="", raw_role="AXMenuBarItem", menu_path=[])
    live = lambda **kw: Selector(**dict(dict(canonical_role="menuitem", name="Ablage", identifier="_NS:860",  # noqa: E731
                                             raw_role="AXMenuBarItem", menu_path=[]), **kw))
    assert _same_identity(rec, live())
    assert not _same_identity(rec, live(identifier="newDocument:"))      # a developer-given id: another element
    assert not _same_identity(rec, live(menu_path=["Ablage"]))           # another place
    assert not _same_identity(rec, live(name="Bearbeiten"))              # another name
    unpinned = Selector(canonical_role="menuitem", name="Ablage", identifier="", raw_role="AXMenuBarItem")
    assert not _same_identity(unpinned, live())                          # pinned to no place: refused as before
    assert _same_identity(Selector(canonical_role="menuitem", name="Sichern", identifier="_NS:358", menu_path=["Ablage"]),
                          live(name="Sichern", identifier="_NS:358", menu_path=["Ablage"]))


def test_a_synthetic_menu_baseline_is_judged_on_its_options_only():
    from anyuse.execution import map_compile as M
    from anyuse.execution.drift_gate import evaluate_drift
    from anyuse.guides.state_graph import StateGraph, UIState
    opts = [{"label": x} for x in ("Name", "Art", "Zuletzt geöffnet", "Hinzugefügt am", "Änderungsdatum",
                                   "Erstellungsdatum", "Größe", "Tags")]
    raw = M._menu_state("Finder", "sort", opts)
    g = StateGraph()
    g.add_state(UIState.from_dict(raw))
    sid = raw["state_id"]
    others = [{"role": "AXButton", "title": f"b{i}"} for i in range(300)]
    shown = [{"role": "AXMenuItem", "title": o["label"]} for o in opts] + others
    rep = evaluate_drift(g, sid, shown)
    assert not rep.drifted and rep.signals.get("synthetic") is True, (rep.reason, rep.signals)
    rep = evaluate_drift(g, sid, others)                                  # the options are not shown: drift
    assert rep.drifted and rep.reason == "required_missing"
    recorded = dict(raw, state_id="rec", custom_render_notes=[])           # the same fingerprint, recorded
    g.add_state(UIState.from_dict(recorded))
    rep = evaluate_drift(g, "rec", shown)
    assert rep.drifted and rep.reason in ("count_out_of_range", "low_structural_similarity")


def test_the_return_after_typing_accepts_the_same_identified_field_re_created():
    from anyuse.execution import ax_actions as AX
    from anyuse.execution import draft_compile as DC
    from anyuse.execution.plan_types import PlannedStep
    search = dict(canonical_role="textfield", name="", identifier="_NS:122", raw_role="AXTextField", app=FINDER)

    class Tree:
        def __init__(self):
            self.focused, self.sels, self.wins = "old", {"old": search, "new": search,
                                                          "app": dict(canonical_role="application", name="", identifier="",
                                                                      raw_role="AXApplication"),
                                                          "other": dict(search, identifier="_NS:200"),
                                                          "anon_old": dict(search, identifier=""),
                                                          "anon_new": dict(search, identifier="")}, {}

        def focused_element(self):
            if isinstance(self.focused, list):                 # a focus that settles: one element per read
                return self.focused.pop(0) if len(self.focused) > 1 else self.focused[0]
            return self.focused

        def to_selector(self, el):
            return Selector(**self.sels[el])

        def window_of(self, el):
            return self.wins.get(el, "win")

        def bundle_id(self):
            return FINDER

    def steps():
        typing = PlannedStep(intent="type", source="workflow", target_element="", modality="ax",
                             command={"action": "enter_text", "selector": {"kind": "focused", "canonical_role": "textfield"}})
        ret = PlannedStep(intent="return", source="workflow", target_element="", modality="ax",
                          command={"action": "press_key", "keycode": 36, "selector": {
                              "kind": "focused", "canonical_role": "textfield", "trigger_kind": "focused", "app": FINDER}})
        return [typing, ret]

    def outcome(before, after, window_after="win"):
        t = Tree()
        t.focused = before
        st = steps()
        DC.prepare_focus(st, 0, t)                                 # read right before the typing step
        t.focused = after
        t.wins[after] = window_after
        return AX.resolve_focused(t, st[1].command["selector"])

    el, why = outcome("old", "new")                               # Finder: the same field, a new reference
    assert why is None and el == "new"
    el, why = outcome("old", "other")                             # another field (a Tab): refused
    assert el is None and "focus moved" in why["error"]
    el, why = outcome("anon_old", "anon_new")                     # no identifier: the reference check alone
    assert el is None and "focus moved" in why["error"]
    el, why = outcome("old", "new", window_after="another window")
    assert el is None and "another window" in why["error"]
    el, why = outcome("old", "old")                               # the same reference: as before
    assert why is None
    import anyuse.execution.draft_compile as DCmod
    saved = DCmod.FOCUS_SETTLE_S
    DCmod.FOCUS_SETTLE_S = 0.0
    try:
        el, why = outcome(["app", "app", "old"], "new")          # Finder: the field takes the focus a moment later
        assert why is None and el == "new"
    finally:
        DCmod.FOCUS_SETTLE_S = saved


def test_the_policy_cli_is_isolated_and_the_run_budget_is_a_setting(monkeypatch):
    import math
    from anyuse import settings as S
    from anyuse.execution import agent_harness as AH
    from anyuse.execution import harness_policies as HP
    argv = HP.claude_cli_argv("sonnet", "SYS")
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv and argv[argv.index("--tools") + 1] == ""
    monkeypatch.delenv("ANYUSE_AGENT_MAX_USD", raising=False)
    assert AH.HarnessConfig.from_settings().max_usd == AH.DEFAULT_MAX_USD == S.DEFAULT_AGENT_MAX_USD
    monkeypatch.setenv("ANYUSE_AGENT_MAX_USD", "0.6")
    assert AH.HarnessConfig.from_settings().max_usd == 0.6
    monkeypatch.setenv("ANYUSE_AGENT_MAX_USD", "unlimited")
    assert AH.HarnessConfig.from_settings().max_usd == math.inf
    assert AH.HarnessConfig.from_settings(max_usd=0.01).max_usd == 0.01        # an explicit override wins
    spec = S.SETTINGS["ANYUSE_AGENT_MAX_USD"]
    assert spec.security_relevant and spec.confirm_when(1.5) and not spec.confirm_when(0.5)    # raising asks


def test_the_desktop_policy_sees_the_apps_menus_and_never_the_apple_menu(monkeypatch):
    import test_desktop_view_probe as T13
    from anyuse.execution import harness_policies as HPol
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import harness_types as T
    J = T13._job()
    El = T13.El
    monkeypatch.setattr(J, "_at", lambda el, name: el.a.get(name) if isinstance(el, El) else None)
    monkeypatch.setattr(J, "_s", lambda v: None if v is None else str(v))
    item = lambda t, kids=(): El("AXMenuItem", ([El("AXMenu", list(kids))] if kids else []), AXTitle=t)  # noqa: E731
    menubar = El("AXMenuBar", [
        El("AXMenuBarItem", [El("AXMenu", [item("Ausschalten …"), item("Neustart …"), item("Sofort beenden …")])],
           AXTitle="Apple"),
        El("AXMenuBarItem", [El("AXMenu", [item("Über TextEdit"), item("Dienste", [item("Mail senden"), item("X")]),
                                           item("Einstellungen …")])], AXTitle="TextEdit"),
        El("AXMenuBarItem", [El("AXMenu", [item("Neu"), item("Sichern")])], AXTitle="Ablage")])
    titles = [e.a.get("AXTitle") for e in J.offered_menu_elements(menubar)]
    assert titles == ["TextEdit", "Über TextEdit", "Einstellungen …", "Ablage", "Neu", "Sichern"]
    els = [T.Element(index=i, kind="menuitem", role="AXMenuItem", text=f"item{i}") for i in range(150)]
    _sys, user = HPol.default_prompt("t", T.Observation(kind="ax", app="TextEdit", elements=els), [], ["ax_press"])
    assert "item149" in user and "more" not in user.split("STEPS SO FAR")[0].splitlines()[-2]
    _sys, user = HPol.default_prompt("t", T.Observation(kind="web", url="https://x.example", elements=els), [], ["click"])
    assert "item59" in user and "item60" not in user and "90 more" in user           # a web page keeps its 60
    calls = []
    dt = HT.DesktopTools(lambda op, **a: calls.append((op, a)) or {"status": "done", "text": "Brief"}, app="TextEdit")
    obs = T.Observation(kind="ax", app="TextEdit", elements=[])
    out = dt.dispatch(T.AxRead(), obs)
    assert calls == [("read_text", {"bundle": "com.apple.TextEdit", "what": "document"})] and out.ok


def test_a_harness_baseline_labels_elements_as_the_drift_gate_reads_them():
    from anyuse.execution import harness_tools as HT
    from anyuse.execution.drift_gate import evaluate_drift
    from anyuse.guides.state_graph import StateGraph, UIState
    rows = [{"role": "AXTextArea", "title": "", "ax_title": "", "identifier": "First Text View"},
            {"role": "AXButton", "title": "", "ax_title": ""},
            {"role": "AXMenuButton", "title": "Dokumentaktionen", "ax_title": ""},   # labelled by its description
            {"role": "AXMenuItem", "title": "Sichern", "ax_title": "Sichern", "in_menubar": True}]
    obs = HT.DesktopTools(lambda op, **a: {"status": "done", "ok": True, "elements": rows}, app="TextEdit").observe()
    assert [e.text for e in obs.elements][2] == "Dokumentaktionen"                     # the policy's label stays
    sid, state = obs.state_id, dict(obs.fingerprint, state_id=obs.state_id)
    assert {"role": "AXMenuButton", "title": ""} in state["fingerprint"]["required_elements"]
    g = StateGraph()
    g.add_state(UIState.from_dict(state))
    live = [{"role": "AXTextArea", "title": ""}, {"role": "AXButton", "title": ""}, {"role": "AXMenuButton", "title": ""},
            {"role": "AXMenuItem", "title": "Sichern"}]                               # AXElementDesc.title: AXTitle only
    rep = evaluate_drift(g, sid, live)
    assert rep.reason != "required_missing", rep.signals
    old = [dict(r) for r in rows]                                                      # an older guest: no ax_title
    for r in old:
        r.pop("ax_title")
    obs_old = HT.DesktopTools(lambda op, **a: {"status": "done", "ok": True, "elements": old}, app="TextEdit").observe()
    assert {"role": "AXMenuButton", "title": "Dokumentaktionen"} in obs_old.fingerprint["fingerprint"]["required_elements"]
    # the guest's recorder fingerprint (the gate's own capture) is the baseline when the screen read carries it
    rec_fp = {"required_elements": [{"role": "AXTextArea"}], "element_count_range": [300, 420], "structural_hash": "h",
              "role_frequency": {"AXTextArea": 1}, "scoped_counts": {"window": 9}}
    obs_fp = HT.DesktopTools(lambda op, **a: {"status": "done", "ok": True, "elements": rows, "fingerprint": rec_fp},
                             app="TextEdit").observe()
    fp = obs_fp.fingerprint["fingerprint"]
    assert fp["element_count_range"] == [300, 420] and fp["scoped_counts"] == {"window": 9}
    assert obs_fp.state_id == obs.state_id                                                # the harness's own state id
    empty = {"required_elements": [], "element_count_range": [0, 10], "structural_hash": "", "role_frequency": {}}
    obs_empty = HT.DesktopTools(lambda op, **a: {"status": "done", "ok": True, "elements": rows, "fingerprint": empty},
                                app="TextEdit").observe()
    assert obs_empty.fingerprint["fingerprint"]["structural_hash"]                       # a placeholder is not used


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
