"""A blind toggle, an unparameterised hop literal and smaller defects, each fixed
generally; offline with FAKES (no desktop, no AX, no network, no model).

* A blind toggle (a WRONG ACCEPT): a map ``flag_toggle`` "off", or a recorded check / uncheck, replayed as a BLIND
  toggle (an AXPress): from a TextEdit with smart dashes already off, the item reported ok and ended with them ON,
  and the proof, gate R and the replay all accepted it. The AX runner now has a STATE-AWARE toggle
  (``ax_actions.set_toggle``): the step carries its target state, the runner reads the state (a menu item's mark, a
  checkbox's value), presses only on a mismatch and reads it back (another state: a deviation, a stop); an
  unreadable state is refused. The map binds the target from the text, a recorded check / uncheck keeps its end
  state, and the proof runs every case from BOTH start states (already on, already off).
* A hop literal: a map_hop typing gap's literal (Finder's new-folder name, taken from the text) was neither a
  parameter nor carried in the hop, so a fill with free text off had nothing it could type and the proof could not
  vary it. The typing gap now carries its value (``${name}``, bound from the text or the caller): the compiler makes
  it a parameter, the gap's hint names it, the fill types the parameter, the proof and the replay vary it.
* Smaller fixes: the CLI's ``from-map --model`` hands the model planner (arm P) its own model function; the
  host's interlock refusal names its entry point (``cli: agent run --app ...`` on the CLI, not "mcp desktop tool").
* Which app is in front (the AX focus can have no value right after a switch, and the untickled workspace fallback
  gives the old app): the AX focus whenever it answers, else the run-loop-ticked workspace read cross-checked by
  the menu-bar owner, the answering source recorded per poll; fail closed only when no source answers or the
  answering ones disagree after the settle polling; never the untickled workspace read.
* A failed MCP desktop call's ``next`` names the record's own local command (``workflow replay`` for a draft
  workflow, none for a routine, ``workflow play`` otherwise); the guest's ``read_text`` labels a named document's
  read by that document, not "the front TextEdit document"; the map lexicon tells going to a folder from creating
  one by its verbs, and a whole-text replace is the write mode (not replace_line).

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_desktop_followups.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
os.environ.setdefault("ANYUSE_DATA_DIR", tempfile.mkdtemp(prefix="anyuse-followups-data-"))
os.environ.setdefault("ANYUSE_VERDICT_STORE", os.path.join(os.environ["ANYUSE_DATA_DIR"], "verdicts.jsonl"))

from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import draft_proof as PR  # noqa: E402
from anyuse.execution import map_compile as M  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402

TURN_OFF = "Turn off smart dashes, then write 'Stand: final' into the document and save."


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── the runner's state-aware toggle ─────────────────────────────────────────────────────────────

class ToggleTree:
    """A toggle's tree surface: its state (None: unreadable), presses, a closed menu; ``sticky``: the press does not
    change the state (the read-back then differs)."""

    def __init__(self, state, sticky=False, refuse=None):
        self.state, self.sticky, self.refuse = state, sticky, refuse
        self.presses, self.closed = 0, 0

    def toggle_state(self, _el):
        return self.state

    def toggle_readback(self, _el, _want):
        return self.state

    def perform_action(self, _el, action):
        assert action == "AXPress"
        self.presses += 1
        if not self.sticky and self.state in ("on", "off"):
            self.state = "off" if self.state == "on" else "on"
        return True

    def close_menu(self, _el):
        self.closed += 1
        return True

    def actuation_refusal(self, _where):
        return self.refuse


CMD = {"role": "AXMenuItem", "title": "Intelligente Bindestriche", "action": "set_toggle"}


def toggle(tree, want):
    return AX.set_toggle(tree, object(), {}, dict(CMD, target_state=want))


def test_already_off_and_turn_off_presses_nothing_and_closes_the_menu():
    t = ToggleTree("off")
    r = toggle(t, "off")
    assert r["success"] and r["pressed"] is False and (r["state_before"], r["state_after"]) == ("off", "off")
    assert t.presses == 0 and t.closed == 1 and t.state == "off"


def test_on_and_turn_off_presses_once_and_reads_off_back():
    t = ToggleTree("on")
    r = toggle(t, "off")
    assert r["success"] and r["pressed"] is True and (r["state_before"], r["state_after"]) == ("on", "off")
    assert t.presses == 1 and t.state == "off"
    t2 = ToggleTree("off")
    assert toggle(t2, "on")["state_after"] == "on" and t2.presses == 1


def test_an_unreadable_state_is_refused_never_a_blind_press():
    t = ToggleTree(None)
    r = toggle(t, "off")
    assert not r["success"] and r["refused"] and r["reason_code"] == "state_unreadable" and r["acted"] is False
    assert t.presses == 0 and t.closed == 1                     # the menu opened to reach it is closed again


def test_a_read_back_that_differs_is_a_deviation_a_stop():
    from anyuse.execution.outcome import DEVIATION, STOP_KINDS
    t = ToggleTree("on", sticky=True)
    r = toggle(t, "off")
    assert not r["success"] and r["acted"] and r["outcome_kind"] == DEVIATION and DEVIATION in STOP_KINDS
    assert t.presses == 1 and "reads on afterwards" in r["error"]          # pressed ONCE, never again


def test_no_target_or_a_gated_tree_presses_nothing():
    t = ToggleTree("on")
    for bad in (None, "", "maybe", "${setting}"):
        r = toggle(t, bad)
        assert r["refused"] and r["reason_code"] == "identity"
    g = ToggleTree("on", refuse="actuation gated (ANYUSE_DRIVE_ENABLED off)")
    assert toggle(g, "off")["reason_code"] == "gated" and g.presses == 0
    assert [AX.target_state(v) for v in ("on", "OFF", True, False, "checked", "uncheck", 1, 0, "x")] == [
        "on", "off", "on", "off", "on", "off", "on", "off", None]


def test_the_executor_runs_set_toggle_through_its_handler_on_both_branches():
    from anyuse.execution import executor as EX
    assert EX.AX_ACTIONS["set_toggle"] == {"verb": None, "native": ""}     # never the platform's blind toggle
    assert "set_toggle" in AX.WRITE_ACTIONS and "set_toggle" in AX.CONTRACT
    t = ToggleTree("off")
    ex = StateGraphExecutor(ax_tree=t)
    r = ex._ax_inline("set_toggle", object(), types_step(), dict(CMD, target_state="off"), "menuitem", None)
    assert r["success"] and t.presses == 0
    # a record with a state-aware toggle needs the contract: a runner without it refuses before anything is sent
    rec = {"steps": [{"command": dict(CMD, target_state="off")}]}
    assert AX.record_version(rec["steps"]) == 2
    assert AX.record_runner_problem(rec, Bare(ax_tree=t)) and AX.record_runner_problem(rec) is None


def types_step():
    from anyuse.execution.plan_types import PlannedStep
    return PlannedStep.from_dict({"intent": "x", "source": "workflow", "target_element": "x", "modality": "ax",
                                  "command": dict(CMD, target_state="off"), "input_data": None,
                                  "needs_visual": False, "expected_after": "", "expected_after_state": None,
                                  "reasoning": ""})


class Bare(StateGraphExecutor):
    AX_CONTRACT = frozenset()


# ── the tree's reads ─────────────────────────────────────────────────────────────────────────────

class FakeAX:
    """``ApplicationServices`` as ``AccessibilityTree`` calls it: attributes per element, AX error codes."""

    def __init__(self, attrs, pressed):
        self.attrs, self.pressed = attrs, pressed

    def AXUIElementCopyAttributeValue(self, el, attr, _none):  # noqa: N802
        v = self.attrs.get(el, {}).get(attr, KeyError)
        if v is KeyError:
            return -25212, None                                 # kAXErrorNoValue
        if v == "ERR":
            return -25200, None                                 # kAXErrorFailure
        return 0, v

    def AXUIElementPerformAction(self, el, action):  # noqa: N802
        self.pressed.append((el, action))
        if action == "AXPress" and el == "bar":
            # AppKit validates the menu when it opens: the item's mark becomes the real state
            self.attrs["item"]["AXMenuItemMarkChar"] = self.attrs["item"].get("_real", "")
        return 0


def tree_with(attrs, monkeypatch):
    from anyuse.context import accessibility as acc
    pressed = []
    fake = FakeAX(attrs, pressed)
    monkeypatch.setattr(acc, "_ax", lambda: fake)
    monkeypatch.setattr(acc, "_drive_enabled", lambda: True)
    monkeypatch.setattr(acc, "_interlock", lambda where: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return acc.AccessibilityTree(), pressed


def test_the_tree_reads_a_menu_mark_and_a_checkbox_value_and_nothing_else(monkeypatch):
    t, _p = tree_with({"on": {"AXRole": "AXMenuItem", "AXMenuItemMarkChar": "✓"},
                       "off": {"AXRole": "AXMenuItem"},
                       "empty": {"AXRole": "AXMenuItem", "AXMenuItemMarkChar": ""},
                       "bad": {"AXRole": "AXMenuItem", "AXMenuItemMarkChar": "ERR"},
                       "box1": {"AXRole": "AXCheckBox", "AXValue": 1}, "box0": {"AXRole": "AXCheckBox", "AXValue": 0},
                       "mixed": {"AXRole": "AXCheckBox", "AXValue": 2}, "sw": {"AXRole": "AXSwitch", "AXValue": True},
                       "button": {"AXRole": "AXButton", "AXValue": 1}, "norole": {}}, monkeypatch)
    got = {k: t.toggle_state(k) for k in ("on", "off", "empty", "bad", "box1", "box0", "mixed", "sw", "button", "norole")}
    assert got == {"on": "on", "off": "off", "empty": "off", "bad": None, "box1": "on", "box0": "off",
                   "mixed": None, "sw": "on", "button": None, "norole": None}


def test_a_menu_items_read_back_waits_for_the_press_to_land_before_it_reopens_the_menu(monkeypatch):
    """Live: AppKit sends a menu item's action after its menu closed; reopened 0.3 s after the press,
    the switch never changed. The read-back gives the press MENU_ACTION_SETTLE_S before it opens the menu, and
    returns at once when the closed item already shows the target."""
    import time as T
    from anyuse.context import accessibility as acc
    attrs = {"item": {"AXRole": "AXMenuItem", "AXMenuItemMarkChar": "✓", "AXParent": "menu", "_real": ""},
             "menu": {"AXRole": "AXMenu", "AXParent": "bar"}, "bar": {"AXRole": "AXMenuBarItem"}}
    t, pressed = tree_with(attrs, monkeypatch)
    clock, log = [0.0], []
    monkeypatch.setattr(T, "monotonic", lambda: clock[0])
    monkeypatch.setattr(T, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    real_press = t.perform_action
    monkeypatch.setattr(t, "perform_action", lambda el, a: (log.append((el, a, clock[0])), real_press(el, a))[1])
    assert t.toggle_readback("item", "off") == "off"
    opened = [c for el, a, c in log if (el, a) == ("bar", "AXPress")]
    assert opened and opened[0] >= acc.AccessibilityTree.MENU_ACTION_SETTLE_S        # never before the settle
    attrs["item"]["AXMenuItemMarkChar"], attrs["item"]["_real"] = "✓", "✓"
    log.clear()
    clock[0] = 0.0
    seq = iter(["✓", "✓", ""])                                   # the closed mark turns within the settle
    monkeypatch.setattr(t, "toggle_state", lambda el: "on" if next(seq, "") else "off")
    assert t.toggle_readback("item", "off") == "off" and log == []        # landed: no menu opened


def test_a_menu_items_read_back_reopens_its_top_menu_when_the_closed_mark_is_stale(monkeypatch):
    attrs = {"item": {"AXRole": "AXMenuItem", "AXMenuItemMarkChar": "✓", "AXParent": "menu", "_real": ""},
             "menu": {"AXRole": "AXMenu", "AXParent": "bar"}, "bar": {"AXRole": "AXMenuBarItem"}}
    t, pressed = tree_with(attrs, monkeypatch)
    # the closed menu still shows the old mark (on); its top menu opened, AppKit shows the real one (off)
    assert t.toggle_readback("item", "off") == "off"
    assert pressed == [("bar", "AXPress"), ("bar", "AXCancel")]           # a read: never the item itself
    pressed.clear()
    assert t.toggle_readback("item", "off") == "off" and pressed == []    # already current: nothing opened
    assert t.close_menu("item") and pressed == [("bar", "AXCancel")]
    t2, p2 = tree_with({"box": {"AXRole": "AXCheckBox", "AXValue": 0, "AXParent": "win"},
                        "win": {"AXRole": "AXWindow"}}, monkeypatch)
    assert t2.close_menu("box") and p2 == []                              # outside the menu bar: nothing to close


# ── the map, the recordings, the compiler ───────────────────────────────────────────────────────

def _te_map():
    return M.build_desktop_map(["Finder", "TextEdit"])


def test_the_map_binds_the_flag_target_from_the_text_and_projects_a_state_aware_toggle():
    amap = _te_map()
    t = next(t for t in amap.transitions if t.effect == "flag_toggle" and "Bindestriche" in t.intent)
    assert t.ax[-1]["action"] == "set_toggle" and t.ax[-1]["target_state"] == "${setting}"
    assert all(h["action"] == "press" for h in t.ax[:-1])                  # the menu path stays plain presses
    for text, want in ((TURN_OFF, "off"), ("Turn on smart dashes and write 'x' into the document and save.", "on")):
        p = M.plan(text, amap)
        draft = M.draft_from_plan(p, amap, "flag", p.task)
        flag = [e["data"] for e in draft["events"] if e["data"].get("action") == "set_toggle"]
        assert len(flag) == 1 and flag[0]["target_state"] == want
    # an older map (a plain press as the flag's item) is projected state-aware too
    old = [dict(h, action="press") if h.get("action") == "set_toggle" else h for h in t.ax]
    for h in old:
        h.pop("target_state", None)
    assert M.state_aware_flag(old)[-1]["action"] == "set_toggle"
    assert M.state_aware_flag(old[:1]) == old[:1]                          # a menu-bar item alone: untouched


def storage():
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-followups-")))


def test_the_turn_off_item_is_ready_and_its_proof_varies_the_start_state():
    amap = _te_map()
    p = M.plan(TURN_OFF, amap)
    st = storage()
    rep = run(DC.draft_to_workflow(M.draft_from_plan(p, amap, "flag_off", p.task),
                                   overrides=M.overrides_from_plan(p, amap), storage=st))
    assert rep["status"] == "ready", rep["reasons"]
    proof = rep["proof"]
    assert proof["passed"] and all(c["toggle_starts"] == ["on", "off"] for c in proof["cases"])
    _wf, rec = DC.find_saved("flag_off", st)
    flag = next(s for s in rec["steps"] if s["command"]["action"] == "set_toggle")
    assert flag["command"]["target_state"] == "off" and flag["command"]["selector"]["identifier"]
    assert rec["compiler"]["version"] == 2
    # the expected effects: the press only from the "on" start, none from "off"
    k = rec["steps"].index(flag)
    vals = proof["cases"][0]["values"]
    on = [e for e in PR.expected_effects(rec, vals, "on") if e["step"] == k]
    off = [e for e in PR.expected_effects(rec, vals, "off") if e["step"] == k]
    assert [e["op"] for e in on] == ["AXPress"] and off == []
    # the toggle is held for approval like any write
    assert k in {g["index"] for g in rec["approvals"]["gated_steps"]}


def test_the_proof_catches_a_blind_press_from_the_already_off_start(monkeypatch):
    """The blind toggle's wrong accept: a runner that presses whatever the state passed the proof, the guest replay
    and R. With the state modelled and both start states run, the blind press fails the proof."""
    amap = _te_map()
    p = M.plan(TURN_OFF, amap)
    draft = M.draft_from_plan(p, amap, "flag_blind", p.task)
    ov = M.overrides_from_plan(p, amap)

    def blind(tree, target, step, cmd):
        tree.perform_action(target, "AXPress")
        return {"success": True, "method": "ax", "element": "blind"}
    monkeypatch.setitem(AX.HANDLERS, "set_toggle", blind)
    rep = run(DC.draft_to_workflow(draft, overrides=ov, save=False))
    assert rep["status"] != "ready" and not rep["proof"]["passed"]
    assert "toggles starting off" in rep["proof"]["reason"] and "effects differ" in rep["proof"]["reason"]


def test_a_recorded_check_or_uncheck_keeps_its_end_state_as_the_target():
    def ev(data):
        return type("E", (), {"data": data, "id": "e1", "reason": "", "target": "", "ax_state_id": "",
                              "anchor_id": None})()
    box = {"canonical_role": "checkbox", "name": "Automatisch sichern", "identifier": "autosave", "path": "",
           "raw_role": "AXCheckBox"}
    got = {}
    for verb, extra in (("check", {}), ("uncheck", {}), ("toggle", {"checked": True}), ("toggle", {"checked": False}),
                        ("toggle", {"target_state": "off"}), ("toggle", {}), ("set_toggle", {"target_state": "on"})):
        st = DC._step_from_event(1, ev(dict({"action": verb, "selector": box}, **extra)), {})
        got[(verb, json.dumps(extra))] = (st.action, st.target_state, st.problem)
    assert got[("check", "{}")] == ("set_toggle", "on", "")
    assert got[("uncheck", "{}")] == ("set_toggle", "off", "")
    assert got[("toggle", '{"checked": true}')] == ("set_toggle", "on", "")
    assert got[("toggle", '{"checked": false}')] == ("set_toggle", "off", "")
    assert got[("toggle", '{"target_state": "off"}')] == ("set_toggle", "off", "")
    assert got[("toggle", "{}")] == ("toggle", "", "")                    # no observed end state: as before
    assert got[("set_toggle", '{"target_state": "on"}')] == ("set_toggle", "on", "")
    # a toggle whose target is not on / off is refused, never replayed blind
    for extra in ({"target_state": "${setting}"}, {}):
        st = DC._step_from_event(1, ev(dict({"action": "set_toggle", "selector": box}, **extra)), {})
        assert st.problem and "never replayed as a blind press" in st.problem
    st = DC._step_from_event(1, ev({"action": "toggle", "selector": box, "target_state": "sometimes"}), {})
    assert st.problem and st.action == "toggle"
    planned = DC._planned(DC._step_from_event(1, ev({"action": "uncheck", "selector": box}), {}), None)
    assert planned["command"]["action"] == "set_toggle" and planned["command"]["target_state"] == "off"


def test_a_proven_record_with_a_state_aware_toggle_re_enters_the_map_with_its_target():
    rec = {"app": "TextEdit", "bundle_id": "com.apple.TextEdit", "status": "ready", "workflow": "w",
           "steps": [{"intent": "x", "command": dict(CMD, target_state="off",
                                                     selector={"canonical_role": "menuitem", "name": "x",
                                                               "identifier": "_NS:668", "path": "",
                                                               "raw_role": "AXMenuItem"})}],
           "parameters": [], "workflow_template": {"steps": []}, "state_graph": {"states": {}}}
    amap = M.from_workflow_record(rec)
    hop = amap.transitions[0].ax[0]
    assert hop["action"] == "set_toggle" and hop["target_state"] == "off"


# ── a map_hop typing gap's literal is a parameter of the hop ────────────────────────────────────

NEW_FOLDER = 'Leg einen neuen Ordner "Belege 2026" an.'
CREATE = "com.apple.finder:object_create:id:_NS:685"


def _finder_plan(amap, bindings):
    p = M.Plan(task="new folder", apps=["Finder"])
    for tid, b in (("com.apple.finder:launch_app", {}), (CREATE, bindings)):
        t = amap.get(tid)
        p.steps.append(M.PlanStep(tid, t.effect, t.app, dict(b), "test", t.sources, "", t.intent, 0))
    return p


def test_a_typing_gaps_literal_rides_in_the_hop_and_becomes_a_parameter():
    amap = M.build_desktop_map(["Finder", "Dictionary"])
    t = amap.get(CREATE)
    assert t.ax[-2]["gap"] == "map_hop" and t.ax[-2]["action"] == "enter_text" and t.ax[-2]["value"] == "${name}"
    assert amap.get("com.apple.Dictionary:scoped_find:structure:search_field").ax[0]["value"] == "${lookup}"
    p = M.plan(NEW_FOLDER, amap)
    assert [(s.effect, s.bindings) for s in p.steps] == [("launch_app", {}), ("object_create", {"name": "Belege 2026"})]
    draft = M.draft_from_plan(p, amap, "new_folder", p.task)
    gap = next(e["data"] for e in draft["events"] if e["data"].get("map_hop") and e["data"]["action"] == "type")
    assert gap["text"] == "Belege 2026"
    ov = M.overrides_from_plan(p, amap)
    assert ov == [{"name": "name", "ref": "Belege 2026", "type": "text"}]
    rep = run(DC.draft_to_workflow(draft, overrides=ov, save=False))
    assert rep["status"] == "needs_fill", rep["reasons"]
    assert [(q["name"], q["type"], q["example"]) for q in rep["parameters"]] == [("name", "text", "Belege 2026")]
    typing = next(u for u in rep["unreplayable"] if u["hint"].get("param"))
    assert typing["gap"] == "map_hop" and typing["hint"]["param"] == "name" and typing["fillable"]
    assert rep["parameters"][0]["occurrences"] == [typing["draft_index"]]
    # an unbound literal stays out (no placeholder becomes a value); the caller's value binds it
    q = _finder_plan(amap, {"name": "${name}"})
    assert M.overrides_from_plan(q, amap) == []
    d = M.draft_from_plan(q, amap, "x", "x")
    assert not any("text" in e["data"] for e in d["events"] if e["data"].get("map_hop"))
    q.unbound.append({"param": "name", "kind": "name", "reference": "the folder", "transition": CREATE, "reason": "x"})
    assert M._bind_caller(q, {"name": "Von Aufrufer"}) == ["name"]
    assert M.overrides_from_plan(q, amap) == [{"name": "name", "ref": "Von Aufrufer", "type": "text"}]


class LiveFinder(PR.SimAXWorld):
    """Finder as the fill sees it: pressing "Neuer Ordner" gives the new folder's name field keyboard focus."""

    def __init__(self, record):
        super().__init__(record)
        self.name_field = PR._Element({"canonical_role": "textfield", "name": "", "identifier": "", "path": "",
                                       "raw_role": "AXTextField"}, "AXTextField")

    def perform_action(self, element, action):
        if element.sel.get("name") == "Neuer Ordner":
            self.focused = self.name_field
        return super().perform_action(element, action)


def fill_model(seen, script):
    def llm(messages, schema):
        turn = json.loads(messages[1]["content"])
        seen.append(turn)
        a = dict({"thought": "-", "element": "", "param": "", "text": "", "secret": "", "key": "", "mode": "",
                  "anchor": ""}, **script.pop(0))
        return json.dumps(a), {"provider": "fake", "model": "fake", "calls": 1,
                               "usage": {"prompt_tokens": 900, "completion_tokens": 60}}
    return llm


def test_the_fill_types_the_parameter_with_free_text_off_and_the_proof_varies_the_name():
    from anyuse.execution import draft_fill as DF
    from anyuse.execution.verdict_store import VerdictStore
    amap = M.build_desktop_map(["Finder"])
    p = M.plan(NEW_FOLDER, amap)
    st = storage()
    vs = VerdictStore(os.path.join(tempfile.mkdtemp(prefix="anyuse-followups-v-"), "verdicts.jsonl"))
    rep = run(DC.draft_to_workflow(M.draft_from_plan(p, amap, "new_folder", p.task),
                                   overrides=M.overrides_from_plan(p, amap), storage=st, verdicts=vs))
    assert rep["status"] == "needs_fill", rep["reasons"]
    _wf, rec = DC.find_saved("new_folder", st)
    tree = LiveFinder(rec)
    seen = []
    script = [{"action": "enter_text", "element": "focused", "param": "name"}, {"action": "done"},
              {"action": "press_key", "element": "focused", "key": "return"}, {"action": "done"}]

    async def approve(_ctx):
        return True
    gates = lambda: {"may_drive": True, "drive_enabled": True, "interlock": {"allowed": True}}  # noqa: E731
    out = run(DF.fill_saved("new_folder", {"name": "Rechnungen Q3"}, approver=approve, llm=fill_model(seen, script),
                            ax_tree=tree, executor=DC.checked_executor(tree), storage=st, preflight=gates,
                            on_step=tree.at_step, free_text=False, verdicts=vs))
    assert out["ok"], out.get("stopped")
    assert out["status"] == "ready", out.get("reasons")
    assert seen[0]["step_to_complete"]["types_parameter"] == "name" and seen[0]["may_type_own_text"] is False
    assert "Rechnungen Q3" not in json.dumps(seen, ensure_ascii=False)           # the run's value is never sent
    assert ("enter_text", "textfield", "Rechnungen Q3") in [(e["op"], e["target"].split(":")[-1], e["value"])
                                                             for e in tree.effects]
    _wf, new = DC.find_saved("new_folder", st)
    typed = next(s for s in new["steps"] if s["command"]["action"] == "enter_text")
    assert typed["input_data"] == "${name}" and typed["command"]["selector"]["kind"] == "focused"
    proof = new["proof"]
    names = [c["values"]["name"] for c in proof["cases"]]
    assert proof["passed"] and len(names) >= 2 and "Belege 2026" not in names and len(set(names)) == len(names)
    # a plain replay types a new name, no model
    world = PR.SimAXWorld(new)
    r = run(DC.replay_saved("new_folder", {"name": "Archiv 2027"}, approver=approve, storage=st,
                            executor=DC.checked_executor(world), preflight=gates, on_step=world.at_step, verdicts=vs))
    assert r["ok"], r["stopped"]
    assert ("enter_text", "Archiv 2027") in [(e["op"], e["value"]) for e in world.effects]


def test_one_value_typed_and_chosen_is_told_apart_by_the_parameters_type():
    """The hop literal's consequence (the plans for NT10, NW07): a folder opened by its row AND a new folder named
    the same made the value-matched overrides ambiguous ("matches 2 candidates"), so the record fell to draft. The
    override's type names the candidate: a choice binds the chosen row, a text the typed literal."""
    from anyuse.execution import draft_params as DP
    cands = [DP.Candidate(id="c1", source="chosen", value="Belege", steps=[2]),
             DP.Candidate(id="c2", source="typed", value="Belege", steps=[6])]
    params, probs = DP.apply_overrides(cands, [], [{"name": "file", "ref": "Belege", "type": "choice"},
                                                   {"name": "name", "ref": "Belege", "type": "text"}])
    assert probs == [] and {(p.name, p.candidate) for p in params} == {("file", "c1"), ("name", "c2")}
    _p, probs = DP.apply_overrides(cands, [], [{"name": "x", "ref": "Belege"}])        # no type: still ambiguous
    assert probs and "matches 2 candidates" in probs[0]
    amap = M.build_desktop_map(["Finder"])
    opener = next(t for t in amap.transitions if t.effect == "open_item" and t.app == "Finder")
    p = M.Plan(task="open and create", apps=["Finder"])
    for tid, b in (("com.apple.finder:launch_app", {}), (opener.id, {"file": "Belege"}), (CREATE, {"name": "Belege"})):
        t = amap.get(tid)
        p.steps.append(M.PlanStep(tid, t.effect, t.app, dict(b), "test", t.sources, "", t.intent, 0))
    rep = run(DC.draft_to_workflow(M.draft_from_plan(p, amap, "both", p.task), overrides=M.overrides_from_plan(p, amap),
                                   save=False))
    assert rep["status"] == "needs_fill", rep["reasons"]
    assert sorted((q["name"], q["type"]) for q in rep["parameters"]) == [("file", "choice"), ("name", "text")]


# ── the app switch: AX first, the ticked workspace and the menu-bar owner behind it ──────────────────────────

NOTES, TEXTEDIT = "com.apple.Notes", "com.apple.TextEdit"


def scripted(seq):
    """A read that answers ``seq`` one by one (the last one repeats)."""
    seq = list(seq)
    return lambda: seq.pop(0) if len(seq) > 1 else seq[0]


def activation(monkeypatch, ax, ws, mb, settle=1.0, wsrv=(None,)):
    """An ``AccessibilityTree.activate_app`` against scripted reads per poll: the AX focus, the run-loop-ticked
    workspace and the menu-bar owner; a fake clock advances 0.1 s per sleep."""
    from anyuse.context import accessibility as acc
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(acc, "frontmost_bundle_live", scripted(ax))
    monkeypatch.setattr(acc, "workspace_frontmost_ticked", scripted(ws))
    monkeypatch.setattr(acc, "menubar_owner", scripted(mb))
    monkeypatch.setattr(acc, "window_server_front", scripted(wsrv))
    monkeypatch.setattr(acc, "_drive_enabled", lambda: True)
    monkeypatch.setattr(acc, "_interlock", lambda where: None)
    activated = []

    class App:
        def activateWithOptions_(self, _o):
            activated.append(1)
            return True
    kit = type("Kit", (), {"NSApplicationActivateIgnoringOtherApps": 2,
                           "NSRunningApplication": type("R", (), {"runningApplicationsWithBundleIdentifier_":
                                                                  staticmethod(lambda bid: [App()])})})
    monkeypatch.setattr(acc, "_appkit", lambda: kit)
    t = acc.AccessibilityTree()
    ok = t.activate_app(NOTES, settle=settle)
    return ok, t, activated


def test_the_ax_focus_decides_whenever_it_answers_even_after_a_lag(monkeypatch):
    ok, t, activated = activation(monkeypatch, [TEXTEDIT] * 3 + [NOTES], [NOTES], [NOTES])
    ev = t.last_activation
    assert ok and activated == [1] and ev["confirmed_by"] == "ax" and ev["verdict"] == "confirmed"
    assert [p["bundle_id"] for p in ev["polls"]] == [TEXTEDIT] * 3 + [NOTES]
    assert ev["polls"][0]["reads"] == {"ax": TEXTEDIT}       # AX answered: nothing else read


def test_ax_without_an_answer_falls_to_the_ticked_workspace_cross_checked_by_the_menu_bar_owner(monkeypatch):
    # AX has no value right after the switch; the ticked workspace and the menu-bar owner agree
    ok, t, _a = activation(monkeypatch, [None], [NOTES], [NOTES])
    ev = t.last_activation
    assert ok and ev["confirmed_by"] == "nsworkspace_tick"
    assert ev["polls"] == [{"t": 0.0, "front_source": "nsworkspace_tick", "bundle_id": NOTES,
                            "reads": {"ax": None, "nsworkspace_tick": NOTES,
                                      "menubar_owner": NOTES}}]
    ok, t, _a = activation(monkeypatch, [None], [None], [NOTES])               # one source answering is enough
    assert ok and t.last_activation["confirmed_by"] == "menubar_owner"
    # AX fails for a stretch (no value, then kAXErrorCannotComplete -> None) while the fallbacks still show the old
    # app, then the switch lands: confirmed on the poll that shows it, whichever source answered
    ok, t, _a = activation(monkeypatch, [None, None, None], [TEXTEDIT, TEXTEDIT, NOTES], [TEXTEDIT, TEXTEDIT, NOTES])
    assert ok and len(t.last_activation["polls"]) == 3 and t.last_activation["confirmed_by"] == "nsworkspace_tick"


def test_no_source_or_disagreeing_sources_fail_closed_after_the_settle_polling(monkeypatch):
    ok, t, _a = activation(monkeypatch, [None], [None], [None], settle=0.5)
    assert not ok and t.last_activation["verdict"] == "no_source_answered" and t.last_activation["polls"][-1]["t"] >= 0.5
    ok, t, _a = activation(monkeypatch, [None], [NOTES], [TEXTEDIT], settle=0.5)
    ev = t.last_activation
    assert not ok and ev["verdict"] == "sources_disagree" and all(p.get("conflict") for p in ev["polls"])
    ok, t, _a = activation(monkeypatch, [TEXTEDIT], [NOTES], [NOTES], settle=0.3)  # AX answers another app: it decides
    assert not ok and t.last_activation["verdict"] == "not_in_front"
    # a disagreement that resolves within the deadline is no failure
    ok, t, _a = activation(monkeypatch, [None], [NOTES], [TEXTEDIT, TEXTEDIT, NOTES], settle=1.0)
    assert ok and t.last_activation["polls"][-1]["front_source"] == "nsworkspace_tick"
    from anyuse.context import accessibility as acc
    ok, t, _a = activation(monkeypatch, [None], [None], [None], settle=20.0)
    assert len(t.last_activation["polls"]) == acc.ACTIVATION_POLLS_KEPT and t.last_activation["polls"][-1]["t"] >= 20.0
    assert acc.ACTIVATE_SETTLE_S >= 5.0 and acc.FRONTMOST_SOURCES == (
        "ax", "nsworkspace_tick", "menubar_owner", "window_server")


def test_the_workspace_is_read_only_after_a_run_loop_tick_never_untickled(monkeypatch):
    """Without a tick the workspace lags one read behind a switch; no tick, no workspace answer."""
    from anyuse.context import accessibility as acc
    calls = []

    class App:
        def __init__(self, bid):
            self.bid = bid

        def bundleIdentifier(self):
            return self.bid

    class WS:
        def frontmostApplication(self):
            calls.append("front")
            return App(NOTES if "tick" in calls else TEXTEDIT)          # stale until the run loop has turned

        def menuBarOwningApplication(self):
            return App(NOTES)

    class Loop:
        def runUntilDate_(self, _d):
            calls.append("tick")
    kit = type("Kit", (), {"NSWorkspace": type("W", (), {"sharedWorkspace": staticmethod(lambda: WS())}),
                           "NSRunLoop": type("L", (), {"currentRunLoop": staticmethod(lambda: Loop())}),
                           "NSDate": type("D", (), {"dateWithTimeIntervalSinceNow_": staticmethod(lambda s: s)})})
    monkeypatch.setattr(acc, "_appkit", lambda: kit)
    assert acc.workspace_frontmost_ticked() == NOTES and calls == ["tick", "front"]
    assert acc.menubar_owner() == NOTES
    kit.NSRunLoop = None                                                 # no tick possible: no workspace answer
    calls.clear()
    assert acc.workspace_frontmost_ticked() is None and calls == []
    # off the main thread a tick refreshes nothing (NSWorkspace changes only when the MAIN run loop runs): no answer
    import threading
    kit.NSRunLoop = type("L", (), {"currentRunLoop": staticmethod(lambda: Loop())})
    got = []
    th = threading.Thread(target=lambda: got.append(acc.workspace_frontmost_ticked()))
    th.start()
    th.join()
    assert got == [None] and calls == []
    assert acc.workspace_frontmost_ticked() == NOTES                    # on the main thread again: ticked, read


def test_off_the_main_thread_only_the_window_server_answers(monkeypatch):
    """Live: menuBarOwningApplication kept the old app for 3.6 s after a switch in a worker
    thread and in the main thread without a tick; one main-thread tick showed the new app. So the menu-bar owner is
    read only right after a main-thread tick, like the workspace; in a worker thread (the executor's AX step) the
    window server's frontmost normal-layer window decides, and a switch is confirmed by it."""
    import threading
    from anyuse.context import accessibility as acc

    class App:
        def __init__(self, bid):
            self.bid = bid

        def bundleIdentifier(self):
            return self.bid
    ticks = []

    class WS:
        def frontmostApplication(self):
            return App(NOTES if ticks else TEXTEDIT)

        def menuBarOwningApplication(self):
            return App(NOTES if ticks else TEXTEDIT)             # stale until the MAIN run loop turned

    class Loop:
        def runUntilDate_(self, _d):
            ticks.append(threading.current_thread() is threading.main_thread())
    pids = {11: TEXTEDIT, 12: NOTES, 13: "com.apple.Terminal"}
    kit = type("Kit", (), {"NSWorkspace": type("W", (), {"sharedWorkspace": staticmethod(lambda: WS())}),
                           "NSRunLoop": type("L", (), {"currentRunLoop": staticmethod(lambda: Loop())}),
                           "NSDate": type("D", (), {"dateWithTimeIntervalSinceNow_": staticmethod(lambda s: s)}),
                           "NSRunningApplication": type("R", (), {"runningApplicationWithProcessIdentifier_":
                                                                  staticmethod(lambda pid: App(pids[pid]))})})
    rows = [{"kCGWindowLayer": 25, "kCGWindowOwnerPID": 13, "kCGWindowBounds": {"Width": 400, "Height": 30}},  # menu bar
            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 11, "kCGWindowAlpha": 0.0,
             "kCGWindowBounds": {"Width": 800, "Height": 600}},                                               # invisible
            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 11, "kCGWindowBounds": {"Width": 20, "Height": 20}},   # a sliver
            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 12, "kCGWindowBounds": {"Width": 900, "Height": 700}},
            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 11, "kCGWindowBounds": {"Width": 900, "Height": 700}}]
    Q = type("Q", (), {"kCGWindowListOptionOnScreenOnly": 1, "kCGWindowListExcludeDesktopElements": 16,
                       "kCGNullWindowID": 0, "CGWindowListCopyWindowInfo": staticmethod(lambda o, w: rows)})
    monkeypatch.setattr(acc, "_appkit", lambda: kit)
    monkeypatch.setattr(acc, "_quartz", lambda: Q)
    monkeypatch.setattr(acc, "frontmost_bundle_live", lambda: None)          # AX: kAXErrorCannotComplete
    assert acc.window_server_front() == NOTES
    got = []
    th = threading.Thread(target=lambda: got.append((acc.menubar_owner(), acc.frontmost_reading())))
    th.start()
    th.join()
    mb, r = got[0]
    assert mb is None and ticks == []                                        # no stale menu-bar owner, no tick
    assert r["front_source"] == "window_server" and r["bundle_id"] == NOTES
    assert r["reads"] == {"ax": None, "nsworkspace_tick": None, "menubar_owner": None, "window_server": NOTES}
    assert acc.menubar_owner() == NOTES and ticks == [True]                 # main thread: ticked, then read
    rows[:] = []
    assert acc.window_server_front() is None
    monkeypatch.setattr(acc, "_quartz", lambda: (_ for _ in ()).throw(ImportError("guard")))
    assert acc.window_server_front() is None
    # the activation: in the worker thread a stale owner never confirms; the window server does
    ok, t, _a = activation(monkeypatch, [None], [None], [None], wsrv=[TEXTEDIT, NOTES])
    assert ok and t.last_activation["confirmed_by"] == "window_server"
    assert t.last_activation["polls"][-1]["reads"]["window_server"] == NOTES


def test_open_app_reports_the_source_and_every_read_in_its_evidence(monkeypatch):
    ok, t, _a = activation(monkeypatch, [None], [NOTES], [TEXTEDIT], settle=0.4)
    t.activate_app = lambda bid: False          # the activation above already filled last_activation
    t.actuation_refusal = lambda where: None
    t.frontmost_bundle_id = lambda: None
    r = AX.open_app(t, None, {}, {"bundle_id": NOTES, "app_name": "Notes"})
    assert not r["success"] and r["acted"] and r["outcome_kind"] == "uncertain_completion"
    assert "sources_disagree" in r["error"] and "nsworkspace_tick=com.apple.Notes" in r["error"]
    assert "menubar_owner=com.apple.TextEdit" in r["error"] and r["activation"]["polls"]
    ok, t, _a = activation(monkeypatch, [None], [NOTES], [NOTES])
    t.activate_app = lambda bid: True
    t.actuation_refusal = lambda where: None
    t.frontmost_bundle_id = lambda: NOTES
    r = AX.open_app(t, None, {}, {"bundle_id": NOTES, "app_name": "Notes"})
    assert r["success"] and r["activation"]["confirmed_by"] == "nsworkspace_tick" and r["front_source"] == "nsworkspace_tick"


# ── smaller fixes ────────────────────────────────────────────────────────────────────────────────

def test_the_cli_from_map_model_planner_gets_arm_ps_model_function(monkeypatch):
    """``from-map --model`` runs the model planner (arm P, ``plan_by_model``) but passed arm M's ``default_llm``
    (another schema name, fewer tokens); it now passes ``default_llm_p``. No model is called here."""
    from click.testing import CliRunner
    from anyuse.cli import cli
    seen = {}

    async def fake_from_map(app, text, amap, **kw):
        seen.update(kw, app=app)
        return {"status": "draft", "reasons": ["fake"], "plan": {}, "workflow": "x"}
    monkeypatch.setattr(M, "from_map", fake_from_map)
    res = CliRunner().invoke(cli, ["workflow", "from-map", "TextEdit", "Write 'x' into the document and save.",
                                   "--model", "--no-review", "--dry-run", "--json"])
    assert res.exit_code == 1, res.output                       # draft: exit 1, the JSON report printed
    assert seen["planner"] == "model" and seen["llm"] is M.default_llm_p and seen["llm"] is not M.default_llm
    seen.clear()
    CliRunner().invoke(cli, ["workflow", "from-map", "TextEdit", "Write 'x' into the document and save.",
                             "--no-review", "--dry-run", "--json"])
    assert seen["planner"] == "lexicon" and seen["llm"] is None


def test_the_host_refusal_names_its_entry_point_not_always_mcp(monkeypatch, tmp_path):
    """``desktop_tools.host_refusal``'s ``where`` read "mcp desktop tool: ..." on the CLI's ``agent run --app`` too."""
    from click.testing import CliRunner
    from anyuse.cli import cli
    from anyuse.execution import desktop_tools as DTools
    from anyuse.execution import harness_product as HP
    monkeypatch.delenv("ANYUSE_DESKTOP_GUEST_SESSION", raising=False)
    assert DTools.host_refusal("desktop.x")["refusal"]["where"].startswith("mcp desktop tool: desktop.x")
    out = HP.run_agent_task("t", app="TextEdit", policy="scripted", script=[{"action": "done"}])
    # the run's refusal carries the host refusal (its own ``refusal.where``)
    assert out["outcome"] == "refused" and out["refusal"]["kind"] == "foreground_interlock"
    assert out["refusal"]["refusal"]["where"].startswith("agent harness: agent run --app TextEdit")
    script = tmp_path / "s.json"
    script.write_text('[{"action": "done"}]')
    res = CliRunner().invoke(cli, ["agent", "run", "x", "--app", "TextEdit", "--policy", "scripted", "--script",
                                   str(script), "--json"], env={"ANYUSE_DATA_DIR": str(tmp_path)})
    rep = json.loads(res.output[res.output.index("{"):])
    where = rep["refusal"]["refusal"]["where"]
    assert where.startswith("cli: agent run --app TextEdit") and "mcp" not in where
    assert rep["steps"] == 0 and rep["model_calls"] == 0


def test_a_failed_desktop_call_names_the_local_command_of_its_records_class():
    """meta_tools' ``next`` named ``anyuse workflow play`` for a draft-compiled tool, which ``play`` refuses
    (vc_i11 over real MCP): a draft workflow replays with ``anyuse workflow replay``; a routine runs only in the guest."""
    from anyuse.mcpserver import meta_tools as MT

    def op(name, cls):
        spec = {"record": {"workflow": name, "compile_class": cls, "parameters": [],
                           "workflow_template": {"required_app": "Notes"}},
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "proof": {"fresh_inputs": 2, "artifact_digest": "abc"}, "compile_class": cls, "holds": False,
                "route": "guest", "ready": True, "name": f"desktop.{name}"}
        return MT._desktop_operation(spec, True, ())

    async def failing(_target, _args):
        return {"ok": False, "error": "a desktop step failed", "steps": []}
    ops = [op("vc_i11", "draft_workflow"), op("sort_r", "routine"), op("rec_x", "ax_flow")]
    nxt = {o.name: run(MT.call_operation("Notes", o.name, {}, operations=ops, run_web=None,
                                         run_desktop=failing))["next"] for o in ops}
    assert "`anyuse workflow replay vc_i11`" in nxt["vc_i11"] and "workflow play" not in nxt["vc_i11"]
    assert "guest VM" in nxt["sort_r"] and "anyuse workflow" not in nxt["sort_r"]
    assert "`anyuse workflow play rec_x`" in nxt["rec_x"]


def test_a_read_of_a_named_back_document_is_labelled_by_that_document(monkeypatch):
    """The guest's ``read_text`` labelled its source "the front TextEdit document" even for a NAMED document behind the
    front one: the label names the document the read was scoped by."""
    import importlib
    vm = str(_HERE / "execution" / "guest")
    if vm not in sys.path:
        sys.path.insert(0, vm)
    J = importlib.import_module("desktop_e2e_job")
    front, back = {"doc": "/u/B/Brief.txt", "text": "B"}, {"doc": "/u/A/Brief.txt", "text": "A"}
    monkeypatch.setattr(J, "need_managed", lambda bid: None)
    monkeypatch.setattr(J, "activate", lambda bid: 4242)
    monkeypatch.setattr(J, "front_window", lambda pid: front)
    monkeypatch.setattr(J, "doc_window", lambda pid, name: (back if "A/" in name else front, ""))
    monkeypatch.setattr(J, "find_all", lambda w, pred: [w])
    monkeypatch.setattr(J, "_at", lambda el, attr: el["text"])
    monkeypatch.setattr(J, "doc_path", lambda w: w["doc"])
    named = J.op_read_text({"bundle": J.TEXTEDIT, "document": "/u/A/Brief.txt"})
    assert named["text"] == "A" and named["source"] == "the TextEdit document 'Brief.txt'"
    assert named["document"] == "/u/A/Brief.txt"
    plain = J.op_read_text({"bundle": J.TEXTEDIT})
    assert plain["text"] == "B" and plain["source"] == "the front TextEdit document"


def test_the_lexicon_tells_going_to_a_folder_from_creating_one_by_its_verbs():
    """"in den Ordner X" / "go to the folder X" planned object_create (a NEW folder X); "Leg einen neuen Ordner an."
    named a folder "an". A new folder needs a creation verb in its clause and no place preposition before it."""
    sys.path.insert(0, str(_HERE))
    import test_map_compile as TMC
    amap = TMC._map((TMC._routine_create(), TMC.T15.GOAL_NEW), (TMC._routine_edit(), TMC.T15.GOAL_EDIT))

    def steps(text):
        p = M.plan(text, amap)
        return [(s.effect, s.bindings) for s in p.steps if s.effect != "launch_app"], p
    for text, folder in (('Geh in den Ordner "Archiv" und sortier nach Größe.', "Archiv"),
                         ('Go to the folder "Archive" and sort it by size.', "Archive"),
                         ('Wechsle in den Ordner "Belege 2026" und sortier nach Größe.', "Belege 2026")):
        got, p = steps(text)
        assert got == [("navigate", {"folder": folder}), ("toggle_select", {"choice": "Größe"})], (text, got)
        assert p.complete and not any(s.effect == "object_create" for s in p.steps)
    got, _p = steps('Leg im Ordner "Archiv" einen neuen Ordner "Q3" an.')
    assert got == [("navigate", {"folder": "Archiv"}), ("object_create", {"name": "Q3"})]
    got, _p = steps('Leg einen neuen Ordner "Belege 2026" an.')
    assert got == [("object_create", {"name": "Belege 2026"})]
    assert M._text_bindings("Leg einen neuen Ordner an.")["new_name"] is None          # "an" is no name
    assert M._text_bindings('Geh in den Ordner "Archiv" und leg dort einen neuen Ordner "Q3" an.')["new_name"] == "Q3"


def test_a_whole_text_replace_is_the_write_mode_not_a_line_replace():
    """"replace the whole text ... with Y" planned replace_line with an unbound anchor; "Ersetze den Text im offenen
    TextEdit-Dokument durch Y" (check 1's task) one with the anchor "Text im offenen TextEdit-Dokument". A whole-text
    replace sets the field's whole value (the write mode: enter_text, read back); a quoted line stays an edit."""
    amap = M.build_desktop_map(["TextEdit"])
    for text in ('Replace the whole text of the document with "Neu" and save.',
                 'Replace the text in the open document with "Neu" and save it.',
                 'Ersetze den ganzen Text durch "Neu" und sichere.',
                 'Ersetze den Text im offenen TextEdit-Dokument durch "Neu" und sichere es.',
                 'Den ganzen Text durch "Neu" ersetzen und sichern.'):
        p = M.plan(text, amap)
        doc = [s for s in p.steps if s.effect == "doc_save"]
        assert len(doc) == 1 and doc[0].bindings == {"text": "Neu"}, (text, [(s.effect, s.bindings) for s in p.steps])
        assert amap.get(doc[0].transition).mode == "write" and not p.unbound
    p = M.plan('Replace the line "Status: offen" with "Status: erledigt" and save.', amap)
    doc = [s for s in p.steps if s.effect == "doc_save"]
    assert amap.get(doc[0].transition).mode == "edit" and doc[0].bindings["anchor"] == "Status: offen"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
