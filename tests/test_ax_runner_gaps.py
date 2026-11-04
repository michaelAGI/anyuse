"""The runner contract for the map's four capabilities (execution/ax_actions.py), offline with fixtures and fakes.

Pinned here, per capability (a row selected by its label; the field focused after a trigger; the
text modes append and replace_line; one workflow across apps): how a recording or a map-style
draft compiles (the step, the gap, the compiler version), the fresh-input proof on the simulated
world, the fill (a fake model picks a row, the focused field, an append), and that a live replay
of an action the executor does not run yet refuses before anything is sent, never a wrong action.
An old record (compiled by the base, ``testdata/drafts/old_record_v1.json``) compiles, proves and
replays exactly as before.

No desktop, no AX, no network, no model: every model answer is a fake.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_ax_runner_gaps.py
"""
import asyncio
import copy
import json
import os
import pathlib
import sys
import tempfile
import types

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.context.accessibility import AXElementDesc, AXSnapshot  # noqa: E402
from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import draft_fill as DF  # noqa: E402
from anyuse.execution import draft_proof as PR  # noqa: E402
from anyuse.execution import executor as EX  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.memory.storage import WorkflowStorage  # noqa: E402

FIX = _HERE / "testdata" / "drafts"
TE = FIX / "textedit_draft.json"
OLD = FIX / "old_record_v1.json"
TE_PARAMS = [{"name": "text", "ref": "@c2", "type": ""},
             {"name": "dateiname", "ref": "@c4", "type": "file_name"}]
GATES = lambda: {"may_drive": True, "drive_enabled": True, "interlock": {"allowed": True}}  # noqa: E731
#: what the executor runs of the contract: all of it
RUNS = AX.module_supports(EX)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def storage():
    return WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-axgaps-")))


def vstore():
    from anyuse.execution.verdict_store import VerdictStore
    return VerdictStore(os.path.join(tempfile.mkdtemp(prefix="anyuse-axgaps-v-"), "verdicts.jsonl"))


def step(data, reason, **extra):
    return dict({"data": data, "reason": reason}, **extra)


def draft(name, steps, app="Notizen", bundle="com.example.notes", goal="a fixture"):
    events = [{"id": "e0", "tag_type": "WORKFLOW_START", "timestamp": "2026-10-09T10:00:00",
               "data": {"goal": goal}, "workflow_name": name}]
    for i, s in enumerate(steps, 1):
        ev = {"id": f"e{i}", "tag_type": "STEP", "timestamp": f"2026-10-09T10:00:{i:02d}",
              "workflow_name": name}
        ev.update(s)
        events.append(ev)
    events.append({"id": f"e{len(steps) + 1}", "tag_type": "WORKFLOW_END",
                   "timestamp": f"2026-10-09T10:00:{len(steps) + 1:02d}", "data": {}, "workflow_name": name})
    return {"name": name, "goal": goal, "session_id": "s-ax-gaps", "apps_involved": [app], "urls_visited": [],
            "app": app, "bundle_id": bundle, "taught_by": "fixture", "events": events, "anchors": [],
            "ax_states": {}}


def sel(role, name="", ident="", path="", raw=""):
    return {"canonical_role": role, "name": name, "identifier": ident, "path": path, "raw_role": raw}


def typed(text, s, **data):
    return dict({"action": "type", "text": text, "selector": s}, **data)


def click(s, **data):
    return dict({"action": "click", "selector": s}, **data)


def compile_(d, overrides, st=None, save=True):
    return run(DC.draft_to_workflow(d, overrides=overrides, storage=st or storage(), save=save,
                                    verdicts=vstore()))


async def yes(_ctx):
    return True


#: the list a recorded cell path names (ROW_PATH below): its role and its path below the window
BOX = {"role": "outline", "identifier": "", "description": "", "path": "splitgroup[0]/scrollarea[0]/outline[0]"}


class Bare(StateGraphExecutor):
    """An executor that runs none of the contract (what the executor is until the rows land)."""
    AX_CONTRACT = frozenset()


# ── 1. the contract itself ─────────────────────────────────────────────────────────────────────

def test_the_contract_is_one_table_row_and_one_handler_per_action():
    assert set(AX.AX_ACTIONS) == set(AX.HANDLERS) == {"select_row", "append_text", "replace_line", "open_app", "set_toggle",
                                                      "press_shortcut"}
    for row in AX.AX_ACTIONS.values():
        assert row == {"verb": None, "native": ""}          # no platform verb: the inline handler
    assert set(AX.CONTRACT) == set(AX.AX_ACTIONS) | {"selector:focused", "selector:row"}
    for spec in AX.CONTRACT.values():
        assert set(spec) == {"args", "identity", "act", "read_back", "tree"}
    r = AX.refusal("x", code="ambiguous")
    assert (r["success"], r["refused"], r["outcome_kind"], r["reason_code"], r["error"]) == (
        False, True, "refused", "ambiguous", "x")
    # what an executor runs is read from its own module's table, or what it declares
    fake = types.ModuleType("fake_exec")
    fake.AX_ACTIONS = {"press": {"verb": "invoke", "native": "AXPress"}, "select_row": {"verb": None, "native": ""}}
    fake.AX_SELECTOR_KINDS = ("focused",)
    assert AX.module_supports(fake) == {"select_row", "selector:focused"}
    assert RUNS == set(AX.CONTRACT) and AX.executor_supports(None) == RUNS
    assert AX.executor_supports(DC.checked_executor(object())) == RUNS
    assert AX.executor_supports(Bare(ax_tree=object())) == frozenset()
    assert set(AX.AX_ACTIONS) <= set(EX.AX_ACTIONS) and EX.AX_SELECTOR_KINDS == AX.SELECTOR_KINDS
    # a step with none of it is never refused, whatever the executor
    assert AX.runner_refusal({"command": {"action": "press", "selector": sel("button", "OK")}}, Bare()) is None
    why = AX.runner_refusal({"command": {"action": "select_row", "selector": AX.row_selector("a", BOX)}}, Bare())
    assert why["reason_code"] == "unsupported_verb" and AX.NOT_IMPLEMENTED in why["error"]
    why = AX.runner_refusal({"command": {"action": "enter_text", "selector": AX.focused_selector("textfield")}},
                            Bare())
    assert why["missing"] == ["selector:focused"]
    # versions: plain steps say 1, a contract step or a per-step app says 2
    assert AX.record_version([{"command": {"action": "press"}}]) == 1
    assert AX.record_version([{"command": {"action": "open_app"}}]) == 2
    assert AX.record_version([{"command": {"action": "press"}, "app": "Notes"}]) == 2


def test_the_modes_edit_by_utf16_ranges_and_refuse_ambiguity():
    after, rng, why, _ = AX.apply_mode("append", "ab\U0001F600", "x")
    assert (after, rng, why) == ("ab\U0001F600x", (4, 0), "")      # the emoji is two UTF-16 units
    before = "eins\nzwei \U0001F600 Stand: alt\ndrei"
    after, rng, why, _ = AX.apply_mode("replace_line", before, "Stand: neu", "Stand:")
    assert after == "eins\nStand: neu\ndrei" and rng == (5, AX.utf16_len("zwei \U0001F600 Stand: alt"))
    after, rng, _w, _c = AX.apply_mode("replace_line", "a\r\nStand: x\r\nb", "Stand: y", "Stand:")
    assert after == "a\r\nStand: y\r\nb" and rng == (3, 8)          # the line break stays
    assert AX.apply_mode("replace_line", "a\nb", "c", "zz")[3] == "not_found"
    assert AX.apply_mode("replace_line", "Stand 1\nStand 2", "c", "Stand")[3] == "ambiguous"
    assert AX.apply_mode("replace_line", "Stand 1", "c\nd", "Stand")[3] == "identity"
    assert AX.apply_mode("replace_line", "Stand 1", "c", "")[3] == "identity"


class _Tree:
    """A hand-made tree for the handlers' read-back paths."""

    def __init__(self, value="", selected=None, accept=True, front="", sel_ok=True):
        self.value, self.selected, self.accept, self.front, self.sel_ok = value, selected, accept, front, sel_ok

    def read_value(self, _el):
        return self.value

    def edit_text(self, _el, text, replace_range=None, expect_value=None):
        if self.accept:
            self.value = PR._splice16(self.value, replace_range[0], replace_range[1], text)
        return PR._SimEdit(self.value, self.value == expect_value)

    def select_row(self, _el):
        return self.sel_ok

    def selected_row_labels(self, _el):
        return self.selected

    def activate_app(self, bid):
        return True

    def frontmost_bundle_id(self):
        return self.front


def test_each_handler_reads_back_and_fails_on_a_mismatch():
    cmd = {"action": "select_row", "selector": AX.row_selector("Rechnung", BOX)}
    assert AX.select_row(_Tree(selected=["Rechnung"]), "el", {"input_data": None}, cmd)["success"]
    out = AX.select_row(_Tree(selected=["Angebot"]), "el", {}, cmd)
    # a read-back mismatch after acting: a STOP kind with ``acted``, never retried by a ladder
    assert not out["success"] and "read back" in out["error"] and out["acted"]
    assert out["outcome_kind"] == "uncertain_completion"
    out = AX.select_row(_Tree(sel_ok=False, selected=[]), "el", {}, cmd)
    assert not out["success"] and "not accepted" in out["error"] and out["outcome_kind"] == "uncertain_completion"
    assert AX.select_row(object(), "el", {}, cmd)["reason_code"] == "unsupported_verb"
    t = _Tree(value="Notizen")
    out = AX.append_text(t, "el", {"input_data": " und mehr"}, {"action": "append_text"})
    assert out["success"] and t.value == "Notizen und mehr" and out["mode"] == "append"
    out = AX.append_text(_Tree(value="Notizen", accept=False), "el", {"input_data": "x"}, {"action": "append_text"})
    assert not out["success"] and "not the expected one" in out["error"]
    assert out["acted"] and out["outcome_kind"] == "uncertain_completion"
    t = _Tree(value="a\nStatus: offen\nb")
    out = AX.replace_line(t, "el", {"input_data": "Status: erledigt"}, {"action": "replace_line", "anchor": "Status:"})
    assert out["success"] and t.value == "a\nStatus: erledigt\nb"
    out = AX.replace_line(_Tree(value="Status 1\nStatus 2"), "el", {"input_data": "x"},
                          {"action": "replace_line", "anchor": "Status"})
    assert out["refused"] and out["reason_code"] == "ambiguous"
    out = AX.open_app(_Tree(front="com.apple.Notes"), None, {}, {"bundle_id": "com.apple.Notes", "app_name": "Notes"})
    assert out["success"]
    out = AX.open_app(_Tree(front="com.apple.TextEdit"), None, {}, {"bundle_id": "com.apple.Notes"})
    assert not out["success"] and "not in front" in out["error"]


# ── 2. rows: select_row by label ──────────────────────────────────────────────────────────────

ROW_PATH = "window[0]/splitgroup[0]/scrollarea[0]/outline[0]/row[3]/cell[0]"


def row_draft(extra=()):
    return draft("open_file", [
        step(typed("Rechnung", sel("searchfield", ident="searchField", raw="AXSearchField")), "search"),
        step(click(sel("cell", "Rechnung 2026.pdf", path=ROW_PATH, raw="AXCell")), "select the file"),
        step(click(sel("menuitem", "Öffnen", ident="openDocument:", raw="AXMenuItem")), "open it"),
        *extra])


ROW_PARAMS = [{"name": "suche", "ref": "@c1"}, {"name": "datei", "ref": "@c2", "type": "choice"}]


def test_a_labelled_row_click_compiles_to_select_row_and_a_choice_binds_its_label():
    src = DC.load_source(row_draft([step(click(sel("row", path="outline[0]/row[1]", raw="AXRow")), "a row")]))
    s1, s3 = src.steps[1], src.steps[3]
    assert s1.action == "select_row" and not s1.problem
    assert s1.selector == {"kind": "row", "canonical_role": "row", "name": "Rechnung 2026.pdf", "identifier": "",
                           "path": "", "raw_role": "AXRow", "container": BOX}
    # a row click without a label (as the recorder made it before the row kind: its path and the
    # reuse id) compiles as it did: a press on that selector, which find_selector resolves
    assert s3.action == "press" and not s3.problem and s3.selector["path"] == "outline[0]/row[1]"
    # a cell of another column names another text than the row's label (its first cell's): a gap;
    # so is a cell of an unknown column and a row whose list has no identity beyond its role
    src = DC.load_source(row_draft([
        step(click(sel("cell", "München", path=ROW_PATH.replace("cell[0]", "cell[1]"), raw="AXCell")), "column 2"),
        step(click(sel("cell", "München", raw="AXCell")), "no path"),
        step(click(sel("row", "Rechnung 2026.pdf", raw="AXRow"), container="outline"), "a role only")]))
    for st_ in src.steps[3:]:
        assert st_.action == "press" and st_.gap == "no_identity", (st_.intent, st_.problem)
    assert "first column" in src.steps[3].problem and "list's identity" in src.steps[5].problem
    # a list identity given by the recording (a description) is used as it is
    src = DC.load_source(row_draft([step(click(sel("row", "Downloads", raw="AXRow"),
                                               container={"role": "outline", "description": "Seitenleiste"}),
                                         "sidebar")]))
    assert src.steps[3].selector["container"] == {"role": "outline", "identifier": "", "description": "Seitenleiste",
                                                  "path": ""}
    st = storage()
    rep = compile_(row_draft(), ROW_PARAMS, st)
    assert rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("open_file", st)
    assert rec["compiler"] == {"name": "draft_compile", "version": 2}
    row = rec["steps"][1]["command"]
    assert row["action"] == "select_row" and row["role"] == "AXRow"
    assert row["selector"]["name"] == "${datei}" and row["selector"]["identifier"] == ""
    assert rec["recorded"][1]["command"]["selector"]["name"] == "Rechnung 2026.pdf"
    assert {p["name"]: p["type"] for p in rec["parameters"]} == {"suche": "text", "datei": "choice"}
    # bound like any choice: the label, nowhere else
    bound = DC.bind_step(rec["steps"][1], {"suche": "x", "datei": "Angebot.pdf"})
    assert bound["command"]["selector"]["name"] == "Angebot.pdf" and bound["command"]["title"] == "Angebot.pdf"
    assert rec["approvals"]["gated_steps"][1]["action"] == "select_row"


def test_a_row_choice_is_proved_on_fresh_labels_and_twins_are_refused():
    st = storage()
    rep = compile_(row_draft(), ROW_PARAMS, st)
    proof = rep["proof"]
    assert proof["passed"], proof["reason"]
    assert "emulated" not in proof                       # the executor's own handler ran
    for case in proof["cases"]:
        assert case["values"]["datei"].startswith("AnyUse probe row ") and case["holds"] == 3
    _wf, rec = DC.find_saved("open_file", st)
    values = {"suche": "Angebot", "datei": "Angebot 7.pdf"}
    world = PR.SimAXWorld(rec)
    world.add_row_values(values)
    out = run(DC.run_steps(rec, values, executor=DC.checked_executor(world), approver=yes,
                           decide_as_driving=True, on_step=world.at_step))
    assert out["ok"], out["stopped"]
    assert [e["op"] for e in world.effects] == ["enter_text", "select_row", "AXPress"]
    assert world.effects[1]["target"] == "row:'Angebot 7.pdf'"
    row_el = next(e for e in world.elements if e.sel.get("name") == "Angebot 7.pdf")
    assert world.selected_row_labels(row_el) == ["Angebot 7.pdf"]
    # two rows of that label in the list: ambiguous, refused before anything is selected
    twin = PR.SimAXWorld(rec)
    twin.add_row_values(values)
    twin.elements.append(PR._Element(AX.row_selector("Angebot 7.pdf", BOX), "AXRow"))
    out = run(DC.run_steps(rec, values, executor=DC.checked_executor(twin), approver=yes,
                           decide_as_driving=True, on_step=twin.at_step))
    assert not out["ok"] and out["steps"][-1]["reason_code"] == "ambiguous"
    assert out["steps"][-1]["refused"] and [e["op"] for e in twin.effects] == ["enter_text"]
    # a row of that label in ANOTHER list (a sidebar) is not it: not found, nothing selected
    side = PR.SimAXWorld(rec)
    side.elements.append(PR._Element(AX.row_selector("Angebot 7.pdf", {"role": "outline", "description": "Seitenleiste"}),
                                     "AXRow"))
    out = run(DC.run_steps(rec, values, executor=DC.checked_executor(side), approver=yes,
                           decide_as_driving=True, on_step=side.at_step))
    assert not out["ok"] and out["steps"][-1]["reason_code"] == "not_found"
    assert [e["op"] for e in side.effects] == ["enter_text"]


# ── 3. the field focused after a trigger ──────────────────────────────────────────────────────

def focus_draft(how="focuses"):
    trigger = click(sel("button", "Neuer Ordner", raw="AXButton"))
    field = {"action": "type", "text": "Archiv 2025"}
    if how == "focuses":
        trigger["focuses"] = "AXTextField"
    elif how == "focused":
        field.update(focused=True, selector={"canonical_role": "textfield"})
    elif how == "button":
        field.update(focused=True, role="AXButton")
    return draft("new_folder", [step(trigger, "create a folder"), step(field, "name it"),
                                step(click(sel("button", "Fertig", raw="AXButton")), "done")])


def test_a_field_recorded_as_focused_compiles_to_the_focused_kind():
    for how in ("focuses", "focused"):
        src = DC.load_source(focus_draft(how))
        s = src.steps[1]
        assert s.action == "enter_text" and not s.problem, (how, s.problem)
        assert s.selector == {"kind": "focused", "canonical_role": "textfield", "name": "", "identifier": "",
                              "path": "", "raw_role": "AXTextField", "trigger": 0}   # the step before it
    s = DC.load_source(focus_draft("button")).steps[1]
    assert s.gap == "no_identity" and "not a text field" in s.problem
    # a map-style step can say it in its selector; a Return into that field targets it too; a
    # selector kind the runner does not have is no step; a recorded document is kept
    d = draft("kinds", [
        step(click(sel("button", "Neu", raw="AXButton")), "trigger"),
        step({"action": "type", "text": "x", "selector": {"kind": "focused", "canonical_role": "searchfield"}}, "a"),
        step({"action": "key", "keys": "return", "focused": True, "selector": {"canonical_role": "searchfield"}}, "b"),
        step({"action": "type", "text": "y", "selector": {"kind": "hovered", "canonical_role": "textfield"}}, "c"),
        step(click(dict(sel("button", "OK", raw="AXButton"), document="/Users/x/a.txt")), "d"),
        step({"action": "click", "selector": {"kind": "focused", "canonical_role": "button"}}, "e"),
        step({"action": "open_app", "app": "Mail", "bundle_id": "com.apple.mail", "withheld": "meeting_surface"},
             "f")])
    a, b, c, d4, e5, f6 = DC.load_source(d).steps[1:]
    assert a.selector["kind"] == "focused" and a.selector["canonical_role"] == "searchfield" and not a.problem
    assert b.action == "press_key" and b.keycode == 36 and b.selector["kind"] == "focused" and not b.problem
    assert "'hovered'" in c.problem and not c.gap
    assert "document" not in d4.selector                  # kept on focused selectors only
    assert e5.gap == "no_identity" and "typing and keys" in e5.problem       # never a click on "the focus"
    # the recorded document and app stay on the focused selector: the executor checks them
    d = draft("doc", [step(click(sel("button", "Neu", raw="AXButton")), "trigger"),
                      step({"action": "type", "text": "x", "focused": True,
                            "selector": {"canonical_role": "textarea", "document": "file:///Users/x/a.txt",
                                         "app": "com.apple.TextEdit"}}, "type")])
    f = DC.load_source(d).steps[1].selector
    assert (f["kind"], f["document"], f["app"]) == ("focused", "file:///Users/x/a.txt", "com.apple.TextEdit")
    assert f6.gap == "sensitive"                                              # withheld wins over the switch
    st = storage()
    rep = compile_(focus_draft(), [{"name": "name", "ref": "@c1"}], st)
    assert rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("new_folder", st)
    assert rec["compiler"]["version"] == 2 and rec["steps"][1]["input_data"] == "${name}"
    assert "emulated" not in rep["proof"]


def test_the_focused_kind_checks_focus_and_role():
    st = storage()
    compile_(focus_draft(), [{"name": "name", "ref": "@c1"}], st)
    _wf, rec = DC.find_saved("new_folder", st)
    world = PR.SimAXWorld(rec)
    out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(world), approver=yes,
                           decide_as_driving=True, on_step=world.at_step))
    assert out["ok"], out["stopped"]
    assert world.effects[1] == dict(world.effects[1], op="enter_text", target="focused:textfield", value="Neu")
    # the trigger did not focus anything: no focus, refused
    nofocus = PR.SimAXWorld(rec)
    nofocus.journal.focus_after.clear()
    out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(nofocus), approver=yes,
                           decide_as_driving=True, on_step=nofocus.at_step))
    assert not out["ok"] and "no element has keyboard focus" in out["stopped"]
    assert out["steps"][-1]["reason_code"] == "not_found" and len(nofocus.effects) == 1
    # focus on a button: the role check refuses
    wrong = PR.SimAXWorld(rec)
    btn = PR._Element(sel("button", "Löschen", raw="AXButton"), "AXButton")
    wrong.journal.focus_after[0] = (wrong, btn)
    out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(wrong), approver=yes,
                           decide_as_driving=True, on_step=wrong.at_step))
    assert not out["ok"] and "the focused element is a button" in out["stopped"]
    assert out["steps"][-1]["reason_code"] == "identity"

    class SecureFocus:
        """A web password input has focus: an AXTextField with the secure subrole."""

        def focused_element(self):
            return "el"

        def to_selector(self, _el):
            from anyuse.context.accessibility import Selector
            return Selector(canonical_role="textfield", raw_role="AXTextField")

        def describe_element(self, _el):
            return AXElementDesc(role="AXTextField", secure=True)
    el, why = AX.resolve_focused(SecureFocus(), AX.focused_selector("textfield"))
    assert el is None and "password field" in why["error"] and why["reason_code"] == "identity"
    assert DF.focused_candidate(SecureFocus(), []) is None


def test_a_map_hop_is_a_gap_the_fill_grounds():
    d = draft("hop", [step(typed("Projekt", sel("textfield", ident="projectField", raw="AXTextField")), "project"),
                      step({"action": "type", "map_hop": True}, "type the name into the field the trigger focused"),
                      step(click(sel("button", "Fertig", raw="AXButton")), "done")])
    rep = compile_(d, [{"name": "projekt", "ref": "@c1"}])
    assert rep["status"] == "needs_fill", rep["reasons"]
    (g,) = rep["unreplayable"]
    assert g["gap"] == "map_hop" and g["fillable"] and g["position"] == 1


# ── 4. text modes: append and replace_line ────────────────────────────────────────────────────

AREA = sel("textarea", ident="First Text View", raw="AXTextArea")


def mode_draft(extra=()):
    # ``start``: the field's shape when the recording focused it (watcher data; no text)
    return draft("edit_note", [
        step(typed("Stand: final", AREA, input_mode="append", start={"lines": 3, "anchors": {}}), "append a line"),
        step(typed("Status: erledigt", AREA, input_mode="replace_line", anchor="Status:",
                   start={"lines": 3, "anchors": {"Status:": [1]}}), "edit the status line"),
        step(click(sel("menuitem", "Sichern", ident="saveDocument:", raw="AXMenuItem")), "save"),
        *extra], app="TextEdit", bundle="com.apple.TextEdit")


def test_the_modes_compile_with_their_action_anchor_and_declared_effect():
    st = storage()
    rep = compile_(mode_draft(), [{"name": "zeile", "ref": "@c1"}, {"name": "status", "ref": "@c2"}], st)
    assert rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("edit_note", st)
    a, r = rec["steps"][0]["command"], rec["steps"][1]["command"]
    assert (a["action"], a["input_mode"], r["action"], r["input_mode"], r["anchor"]) == (
        "append_text", "append", "replace_line", "replace_line", "Status:")
    # the target has an identifier: its value afterwards must contain the typed text (step_effect format)
    assert a["expect"] == {"changes": [{"op": "value", "target": True, "contains": "${input}"}], "only": False,
                           "source": "ax_actions"}
    assert rec["compiler"]["version"] == 2
    # value_problems know the modes: a replaced line is one line; an appended text area may have breaks
    assert any("replace_line" in p for p in DC.value_problems(rec, {"zeile": "a", "status": "x\ny"}))
    assert DC.value_problems(rec, {"zeile": "a\nb", "status": "x"}) == []
    # without the anchor, or without the text: a no_text gap that keeps the mode for the fill
    src = DC.load_source(mode_draft([step({"action": "type", "selector": AREA, "input_mode": "append"}, "append"),
                                     step(typed("x", AREA, input_mode="replace_line"), "no anchor"),
                                     step(typed("x", AREA, input_mode="prepend"), "unknown")]))
    g1, g2, bad = src.steps[3], src.steps[4], src.steps[5]
    assert (g1.gap, g1.input_mode, g2.gap, bad.gap) == ("no_text", "append", "no_text", "")
    assert "anchor" in g2.problem and "prepend" in bad.problem
    assert DC._gap_hint(g1)["mode"] == "append"


def test_the_modes_are_proved_on_fresh_values_with_their_effects():
    st = storage()
    rep = compile_(mode_draft(), [{"name": "zeile", "ref": "@c1"}, {"name": "status", "ref": "@c2"}], st)
    assert rep["proof"]["passed"], rep["proof"]["reason"]
    _wf, rec = DC.find_saved("edit_note", st)
    world = PR.SimAXWorld(rec)
    vals = {"zeile": " und neu", "status": "Status: offen"}
    out = run(DC.run_steps(rec, vals, executor=DC.checked_executor(world), approver=yes,
                           decide_as_driving=True, on_step=world.at_step))
    assert out["ok"], out["stopped"]
    assert [(e["op"], e["value"]) for e in world.effects[:2]] == [("append_text", " und neu"),
                                                                    ("replace_line", "Status: offen")]
    area = next(e for e in world.elements if e.sel.get("identifier") == "First Text View")
    # each edit started from its recorded shape (3 lines; for the replace_line, the anchor in line 2)
    assert area.value == "\nStatus: offen\n"
    assert rec["recorded"][0]["start"] == {"lines": 3, "anchors": {}}
    assert rec["recorded"][1]["start"] == {"lines": 3, "anchors": {"Status:": [1]}}
    assert [e for e in PR.expected_effects(rec, vals)][:2] == [
        {"step": 0, "op": "append_text", "target": "textarea:#First Text View", "value": " und neu"},
        {"step": 1, "op": "replace_line", "target": "textarea:#First Text View", "value": "Status: offen"}]


# ── 5. several apps ──────────────────────────────────────────────────────────────────────────

def multi_draft():
    te, notes = {"app": "TextEdit", "bundle_id": "com.apple.TextEdit"}, {"app": "Notes", "bundle_id": "com.apple.Notes"}
    return draft("copy_list", [
        step(typed("Liste: Milch", AREA, **te), "write the list"),
        step(click(sel("button", "Neue Notiz", raw="AXButton"), **notes), "a new note"),
        step(typed("Einkauf", sel("textfield", "Titel", raw="AXTextField"), **notes), "the title"),
    ], app="TextEdit", bundle="com.apple.TextEdit")


MULTI_PARAMS = [{"name": "liste", "ref": "@c1"}, {"name": "titel", "ref": "@c2"}]


def test_a_multi_app_draft_keeps_every_app_and_switches_with_open_app():
    src = DC.load_source(multi_draft())
    assert src.multi_app and [s.action for s in src.steps] == ["enter_text", "open_app", "press", "enter_text"]
    sw = src.steps[1]
    assert (sw.app, sw.bundle_id, sw.via, sw.index) == ("Notes", "com.apple.Notes", "app switch", 1)
    assert [s.bundle_id for s in src.steps] == ["com.apple.TextEdit"] + ["com.apple.Notes"] * 3
    st = storage()
    rep = compile_(multi_draft(), MULTI_PARAMS, st)
    assert rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("copy_list", st)
    assert rec["apps"] == [{"app": "TextEdit", "bundle_id": "com.apple.TextEdit"},
                           {"app": "Notes", "bundle_id": "com.apple.Notes"}]
    assert rec["steps"][1]["command"] == {"role": "", "title": "Notes", "action": "open_app", "app_name": "Notes",
                                          "bundle_id": "com.apple.Notes"}
    assert rec["steps"][2]["command"]["selector"]["app"] == "com.apple.Notes"
    assert [s["bundle_id"] for s in rec["steps"]] == ["com.apple.TextEdit"] + ["com.apple.Notes"] * 3
    assert DC.is_multi_app(rec) and rec["compiler"]["version"] == 2
    # the open_app is held like any step under ``all``
    assert [g["index"] for g in rec["approvals"]["gated_steps"]] == [0, 1, 2, 3]
    # a one-app draft whose steps carry their app is not multi-app and compiles as before
    one = DC.load_source(draft("one", [step(click(sel("button", "OK", raw="AXButton"), app="Notizen",
                                                  bundle_id="com.example.notes"), "ok")]))
    assert not one.multi_app and [s.action for s in one.steps] == ["press"]


def test_two_worlds_prove_a_multi_app_record_and_a_wrong_tree_is_refused():
    st = storage()
    rep = compile_(multi_draft(), MULTI_PARAMS, st)
    proof = rep["proof"]
    assert proof["passed"], proof["reason"]
    assert "emulated" not in proof
    _wf, rec = DC.find_saved("copy_list", st)
    world = PR.SimMultiAppWorld(rec)
    te, notes = world.tree_for("TextEdit"), world.tree_for("com.apple.Notes")
    assert te is not world.tree_for("Notes") and notes is world.tree_for("Notes")
    assert [e.sel.get("name") or e.sel.get("identifier") for e in te.elements] == ["First Text View"]
    assert sorted(e.sel.get("name") for e in notes.elements) == ["Neue Notiz", "Titel"]
    vals = {"liste": "Liste: Brot", "titel": "Wocheneinkauf"}
    out = run(DC.run_steps(rec, vals, executor=DC.checked_executor(te), approver=yes, decide_as_driving=True,
                           on_step=world.at_step, tree_factory=world.tree_for,
                           executor_factory=DC.checked_executor, default_tree=te))
    assert out["ok"], out["stopped"]
    assert [(e["op"], e["target"]) for e in world.effects] == [
        ("enter_text", "textarea:#First Text View"), ("open_app", "com.apple.Notes"),
        ("AXPress", "button:'Neue Notiz'"), ("enter_text", "textfield:'Titel'")]
    assert world.journal.front == "com.apple.Notes"
    # the tree the factory hands out for Notes is TextEdit's: wrong_app, before the step is held
    w2 = PR.SimMultiAppWorld(rec)
    held = []

    async def note(_ctx):
        held.append(1)
        return True
    out = run(DC.run_steps(rec, vals, executor=DC.checked_executor(w2.tree_for("TextEdit")), approver=note,
                           decide_as_driving=True, on_step=w2.at_step,
                           tree_factory=lambda app: w2.tree_for("TextEdit"),
                           executor_factory=DC.checked_executor))
    assert not out["ok"] and "wrong app" in out["stopped"] and out["steps"][-1]["reason_code"] == "wrong_app"
    assert [e["op"] for e in w2.effects] == ["enter_text", "open_app"] and len(held) == 2
    # no tree for an app: refused, nothing launched
    w3 = PR.SimMultiAppWorld(rec)
    out = run(DC.run_steps(rec, vals, executor=DC.checked_executor(w3.tree_for("TextEdit")), approver=yes,
                           decide_as_driving=True, on_step=w3.at_step, tree_factory=lambda app: None,
                           executor_factory=DC.checked_executor))
    assert not out["ok"] and "no AX tree for 'Notes'" in out["stopped"]


def test_a_multi_app_replay_runs_end_to_end_on_fakes():
    st = storage()
    compile_(multi_draft(), MULTI_PARAMS, st)
    _wf, rec = DC.find_saved("copy_list", st)
    world = PR.SimMultiAppWorld(rec)
    asked = []
    factory_calls = []

    def factory(app):
        factory_calls.append(app)
        return world.tree_for(app)

    async def approve(ctx):
        asked.append(ctx["args"].get("intent"))
        return True
    vals = {"liste": "Liste: Brot", "titel": "Wocheneinkauf"}
    # the CLI's path: no executor, the tree of each app from the factory (one executor per app)
    out = run(DC.replay_saved("copy_list", vals, approver=approve, storage=st, tree_factory=factory,
                              preflight=GATES, on_step=world.at_step, verdicts=vstore()))
    assert out["ok"], out["stopped"]
    assert [(e["op"], e["target"], e["value"]) for e in world.effects] == [
        ("enter_text", "textarea:#First Text View", "Liste: Brot"), ("open_app", "com.apple.Notes", None),
        ("AXPress", "button:'Neue Notiz'", None), ("enter_text", "textfield:'Titel'", "Wocheneinkauf")]
    # each app's tree by its bundle id (a localized name is no identity)
    assert factory_calls == ["com.apple.TextEdit", "com.apple.Notes"] and world.journal.front == "com.apple.Notes"
    assert asked == [] and sum(s["llm_calls"] for s in out["steps"]) == 0     # level auto: no hold, no model
    # a level that holds every step holds the app switch too
    DC.set_approval_level("copy_list", "all", storage=st)
    world = PR.SimMultiAppWorld(rec)
    out = run(DC.replay_saved("copy_list", vals, approver=approve, storage=st, tree_factory=world.tree_for,
                              preflight=GATES, on_step=world.at_step, verdicts=vstore()))
    assert out["ok"] and len(asked) == 4 and any("switch to Notes" in (a or "") for a in asked), asked


def test_the_router_puts_an_open_app_on_the_front_app_and_the_fill_sees_the_opened_one():
    st = storage()
    compile_(multi_draft(), MULTI_PARAMS, st)
    _wf, rec = DC.find_saved("copy_list", st)
    world = PR.SimMultiAppWorld(rec)
    te = world.tree_for("TextEdit")
    first = DC.checked_executor(te)
    router = DC.AppRouter(rec, first, tree_factory=world.tree_for, executor_factory=DC.checked_executor,
                          default_tree=te)
    assert router.front_slot() == (te, first)
    ex, why = router.for_step(rec["steps"][1])           # the open_app runs on the app in front
    assert why is None and ex is first
    router.done(rec["steps"][1])
    tree, ex2 = router.front_slot()
    assert tree is world.tree_for("Notes") and ex2 is not first and router.front_app() == ("Notes", "com.apple.Notes")
    assert router.for_step(rec["steps"][2]) == (ex2, None)   # one executor per app, built once
    # a one-app record: the given executor, always
    one = DC.AppRouter({"steps": [{"command": {"action": "press"}}]}, first)
    assert one.for_step({"command": {"action": "press"}}) == (first, None) and not one.multi


# ── 6. the fill: a row, the focused field, an append ───────────────────────────────────────────

def fill_draft():
    return draft("rename_note", [
        step(typed("Projekt Alpha", sel("textfield", ident="projectField", raw="AXTextField")), "the project"),
        step(typed("Rechnung", sel("searchfield", ident="searchField", raw="AXSearchField")), "search"),
        step({"action": "click", "map_hop": True}, "select the row of the note"),
        step(click(sel("button", "Umbenennen", raw="AXButton")), "rename"),
        step({"action": "type", "map_hop": True}, "type the new name into the field focused after the trigger"),
        step({"action": "type", "selector": sel("textarea", ident="noteBody", raw="AXTextArea"),
              "input_mode": "append"}, "append the project to the note"),
        step(click(sel("button", "Fertig", raw="AXButton")), "done"),
    ])


#: the notes list of the fill's app, by its identifier
NOTES = {"role": "table", "identifier": "notesTable", "description": "", "path": ""}


class LiveNotes(PR.SimAXWorld):
    """The app as the fill run sees it: two rows, a notes area, and a name field that gets keyboard
    focus when "Umbenennen" is pressed."""

    def __init__(self, record):
        super().__init__(record)
        for label in ("Angebot 7", "Angebot 8"):
            self.elements.append(PR._Element(AX.row_selector(label, NOTES), "AXRow"))
        area = PR._Element(sel("textarea", ident="noteBody", raw="AXTextArea"), "AXTextArea")
        area.value = "Notizen"
        self.elements.append(area)
        self.name_field = PR._Element(sel("textfield", raw="AXTextField"), "AXTextField")

    def perform_action(self, element, action):
        if element.sel.get("name") == "Umbenennen":
            self.focused = self.name_field
        return super().perform_action(element, action)

    def snapshot(self, row_labels=False):
        els = []
        for e in self.elements:
            row = e.sel.get("kind") == "row"
            d = AXElementDesc(role=e.raw_role or None, title=None if row else (e.sel.get("name") or None),
                              value=e.value or None, identifier=e.sel.get("identifier") or None)
            if row and row_labels:                 # as AccessibilityTree.snapshot(row_labels=True)
                d.row_label, d.row_container = e.sel["name"], e.sel["container"]
            els.append(d)
        return AXSnapshot(elements=els, app_name="Notizen")


def fill_model(seen):
    def by_label(turn, label):
        return next(e["id"] for e in turn["elements"] if e.get("label") == label)

    def by_ident(turn, ident):
        return next(e["id"] for e in turn["elements"] if e.get("identifier") == ident)
    script = [lambda t: {"action": "select_row", "element": by_label(t, "Angebot 7"), "param": "suche"},
              lambda t: {"action": "done"},
              lambda t: {"action": "enter_text", "element": "focused", "text": "Archiv"},
              lambda t: {"action": "done"},
              lambda t: {"action": "enter_text", "element": by_ident(t, "noteBody"), "param": "projekt",
                         "mode": "append"},
              lambda t: {"action": "done"}]

    def llm(messages, schema):
        turn = json.loads(messages[1]["content"])
        seen.append({"turn": turn, "system": messages[0]["content"]})
        a = dict({"thought": "-", "element": "", "param": "", "text": "", "secret": "", "key": "", "mode": "",
                  "anchor": ""}, **script.pop(0)(turn))
        return json.dumps(a), {"provider": "fake", "model": "fake", "calls": 1,
                               "usage": {"prompt_tokens": 900, "completion_tokens": 60}}
    return llm


def with_data_dir(fn):
    def wrapped():
        prev = os.environ.get("ANYUSE_DATA_DIR")
        os.environ["ANYUSE_DATA_DIR"] = tempfile.mkdtemp(prefix="anyuse-axgaps-data-")
        try:
            fn()
        finally:
            if prev is None:
                os.environ.pop("ANYUSE_DATA_DIR", None)
            else:
                os.environ["ANYUSE_DATA_DIR"] = prev
    wrapped.__name__ = fn.__name__
    return wrapped


@with_data_dir
def test_a_fake_model_fills_a_row_the_focused_field_and_an_append_then_it_replays():
    st = storage()
    rep = compile_(fill_draft(), [{"name": "projekt", "ref": "@c1"}, {"name": "suche", "ref": "@c2"}], st)
    assert rep["status"] == "needs_fill", rep["reasons"]
    assert [u["gap"] for u in rep["unreplayable"]] == ["map_hop", "map_hop", "no_text"]
    _wf, rec = DC.find_saved("rename_note", st)
    tree = LiveNotes(rec)
    seen, asked = [], []

    async def approve(ctx):
        asked.append(ctx["args"].get("intent") or "")
        return True
    values = {"projekt": "Projekt Beta", "suche": "Angebot 7"}
    out = run(DF.fill_saved("rename_note", values, approver=approve, llm=fill_model(seen), ax_tree=tree,
                            executor=DC.checked_executor(tree), storage=st, preflight=GATES,
                            on_step=tree.at_step, free_text=True, verdicts=vstore()))
    assert out["ok"], out["stopped"]
    assert out["status"] == "ready", out["reasons"]
    # what the model was offered: rows by label; the focused field once the trigger focused it
    rows = [e for e in seen[0]["turn"]["elements"] if e["role"] == "AXRow"]
    assert [r["label"] for r in rows] == ["Angebot 7", "Angebot 8"] and "select_row" in seen[0]["system"]
    assert not any(e["id"] == "focused" for e in seen[0]["turn"]["elements"])
    assert {"id": "focused", "role": "AXTextField", "label": "(the field that has keyboard focus now)",
            "focused": True} in seen[2]["turn"]["elements"]
    assert seen[4]["turn"]["step_to_complete"]["recorded_mode"] == "append"
    # the parameters by name only (the row's label is on the screen; the run's values are not sent)
    assert all(set(p) == {"name", "type", "description"} for p in seen[0]["turn"]["parameters"])
    assert values["projekt"] not in json.dumps([s["turn"] for s in seen], ensure_ascii=False)
    assert sum("[AI-filled gap]" in a for a in asked) == 3
    _wf, new = DC.find_saved("rename_note", st)
    acts = [(s["command"]["action"], s["command"].get("selector", {}).get("kind", ""), s.get("input_data"))
            for s in new["steps"]]
    assert acts == [("enter_text", "", "${projekt}"), ("enter_text", "", "${suche}"), ("select_row", "row", None),
                    ("press", "", None), ("enter_text", "focused", "Archiv"), ("append_text", "", "${projekt}"),
                    ("press", "", None)]
    assert new["steps"][2]["command"]["selector"]["name"] == "${suche}"
    assert new["steps"][2]["command"]["selector"]["container"] == NOTES      # the listed row's own list
    assert new["recorded"][2]["command"]["selector"]["name"] == "Rechnung"
    assert next(p for p in new["parameters"] if p["name"] == "suche")["occurrences"] == sorted([1, DF.filled_index(2, 0)])
    assert new["compiler"]["version"] == 2 and new["unreplayable"] == []
    # the filled append recorded its field's start value as the live edit read it ("Notizen"), as a
    # shape only and on the recorded copy only: the proof's field starts from it
    assert new["recorded"][5]["start"] == {"lines": 1, "anchors": {}} and "start" not in new["steps"][5]
    assert "Notizen" not in json.dumps(new["recorded"][5])
    # the live tree saw exactly what was approved
    assert [e["op"] for e in tree.effects] == ["enter_text", "enter_text", "select_row", "AXPress", "enter_text",
                                               "append_text", "AXPress"]
    assert next(e for e in tree.elements if e.sel.get("identifier") == "noteBody").value == "NotizenProjekt Beta"
    # from now on a plain replay: no model; on the contract executor it runs, on one without it it refuses
    world = PR.SimAXWorld(new)
    vals = {"projekt": "Projekt Gamma", "suche": "Angebot 9"}
    world.add_row_values(vals)
    r = run(DC.replay_saved("rename_note", vals, approver=yes, storage=st, executor=DC.checked_executor(world),
                            preflight=GATES, on_step=world.at_step, verdicts=vstore()))
    assert r["ok"], r["stopped"]
    assert ("select_row", "row:'Angebot 9'") in [(e["op"], e["target"]) for e in world.effects]
    r = run(DC.replay_saved("rename_note", vals, approver=yes, storage=st, executor=Bare(ax_tree=world),
                            preflight=GATES, verdicts=vstore()))
    assert not r["ok"] and AX.NOT_IMPLEMENTED in r["stopped"]


def test_the_fill_offers_and_accepts_only_what_the_runner_runs():
    rec = {"parameters": [{"name": "suche", "type": "text"}]}
    row = {"id": "e1", "role": "AXRow", "label": "Angebot 7", "identifier": "",
           "selector": AX.row_selector("Angebot 7", NOTES)}
    field = {"id": "e2", "role": "AXTextArea", "label": "Notiz", "identifier": "", "selector": sel("textarea", "Notiz")}
    focus = {"id": "focused", "role": "AXTextField", "label": "", "focused": True,
             "selector": AX.focused_selector("textfield")}
    cands = [row, field, focus]
    every = frozenset(AX.CONTRACT)

    def parse(answer, features=every, values=None):
        a = dict({"thought": "", "element": "", "param": "", "text": "", "secret": "", "key": "", "mode": "",
                  "anchor": ""}, **answer)
        return DF.parse_answer(json.dumps(a), cands, rec, DF.FillOptions(False, True, False),
                               features=features, values=values)
    assert parse({"action": "select_row", "element": "e1"}, features=frozenset())[1].endswith(AX.NOT_IMPLEMENTED)
    assert "select_row" in parse({"action": "press", "element": "e1"})[1]
    assert "not this run's value" in parse({"action": "select_row", "element": "e1", "param": "suche"},
                                           values={"suche": "Angebot 8"})[1]
    ok, err = parse({"action": "select_row", "element": "e1", "param": "suche"}, values={"suche": "Angebot 7"})
    assert not err and ok["param"] == "suche"
    assert "focused field takes" in parse({"action": "press", "element": "focused"})[1]
    assert parse({"action": "enter_text", "element": "e2", "text": "x", "mode": "append"},
                 features=frozenset())[1].endswith(AX.NOT_IMPLEMENTED)
    assert "anchor" in parse({"action": "enter_text", "element": "e2", "text": "x", "mode": "replace_line"})[1]
    assert "anchor goes with" in parse({"action": "enter_text", "element": "e2", "text": "x", "anchor": "a"})[1]
    ok, err = parse({"action": "enter_text", "element": "e2", "text": "neu", "mode": "replace_line",
                     "anchor": "Status:"})
    assert not err and (ok["mode"], ok["anchor"]) == ("replace_line", "Status:")
    st = DF.planned_step(ok, {"draft_index": 3, "intent": "edit"})
    assert st["command"]["action"] == "replace_line" and st["command"]["anchor"] == "Status:"
    # an anchor that carries this run's value would pin it
    assert DF.value_dependence(dict(st, command=dict(st["command"], anchor="Angebot 7")), {"suche": "Angebot 7"})
    # a row bound to a parameter is not pinned by it
    row_step = DF.planned_step({"action": "select_row", "element": row, "param": "suche", "thought": ""},
                               {"draft_index": 2, "intent": "row"})
    assert row_step["command"]["selector"]["name"] == "${suche}"
    assert DF.value_dependence(row_step, {"suche": "suche"}) is None
    # without the contract the prompt is the one before it; rows are not listed
    assert DF.system_prompt(DF.FillOptions(False, False, False)) == DF.system_prompt(
        DF.FillOptions(False, False, False), frozenset())
    assert "select_row" not in DF.system_prompt(DF.FillOptions(False, False, False))
    side = {"role": "outline", "identifier": "", "description": "Seitenleiste", "path": ""}

    def real_row(label, box):
        # a real row: no title; its label and its list come only as row_label / row_container
        d = AXElementDesc(role="AXRow")
        d.row_label, d.row_container = label, box
        return d
    snap = AXSnapshot(elements=[real_row("Angebot 7", NOTES), real_row("Angebot 7", NOTES),
                                real_row("Angebot 8", NOTES), real_row("Angebot 7", side),
                                AXElementDesc(role="AXRow", title="Titel ohne Liste"),
                                AXElementDesc(role="AXButton", title="OK")])
    assert [c["label"] for c in DF.candidates(snap)] == ["OK"]
    # twins (two rows of one label in ONE list) are left out; the same label in another list is
    # another row; a row without its list's identity is not offered
    got = DF.candidates(snap, rows=True)
    assert [(c["label"], c.get("list")) for c in got] == [("Angebot 8", "notesTable"), ("Angebot 7", "Seitenleiste"),
                                                          ("OK", None)]
    assert got[1]["selector"] == AX.row_selector("Angebot 7", side)


# ── 7. live replays: the executor runs the contract; one without it refuses cleanly ─────────

def test_a_live_replay_runs_a_contract_step_and_an_executor_without_it_refuses_before_anything_is_sent():
    st = storage()
    compile_(row_draft(), ROW_PARAMS, st)
    _wf, rec = DC.find_saved("open_file", st)
    values = {"suche": "Angebot", "datei": "Angebot 7.pdf"}

    async def never(_ctx):
        raise AssertionError("nothing is held on a refused run")

    def no_preflight():
        raise AssertionError("refused before the drive gate is read")
    # an executor without the row: refused up front, nothing held, nothing sent
    world = PR.SimAXWorld(rec)
    out = run(DC.replay_saved("open_file", values, approver=never, storage=st, executor=Bare(ax_tree=world),
                              preflight=no_preflight, verdicts=vstore()))
    assert not out["ok"] and AX.NOT_IMPLEMENTED in out["stopped"] and "step 1" in out["stopped"]
    assert out["runner"] == ["select_row", "selector:row"] and world.effects == []
    # the CLI's path (no executor, a tree factory): the executor module runs select_row, so the
    # replay goes on to the gates and the tree (none here: refused, nothing launched)
    out = run(DC.replay_saved("open_file", values, approver=never, storage=st, tree_factory=lambda app: None,
                              preflight=GATES, verdicts=vstore()))
    assert not out["ok"] and "no AX tree" in out["stopped"] and AX.NOT_IMPLEMENTED not in out["stopped"]
    # the checked executor: the replay runs, the row is selected and read back, no model
    world = PR.SimAXWorld(rec)
    world.add_row_values(values)
    out = run(DC.replay_saved("open_file", values, approver=yes, storage=st, executor=DC.checked_executor(world),
                              preflight=GATES, on_step=world.at_step, verdicts=vstore()))
    assert out["ok"], out["stopped"]
    assert [(e["op"], e["target"]) for e in world.effects][1] == ("select_row", "row:'Angebot 7.pdf'")
    # the runner itself, step by step: the plain step runs, the contract step is refused unheld, never pressed
    world = PR.SimAXWorld(rec)
    world.add_row_values(values)
    held = []

    async def note(_ctx):
        held.append(_ctx["args"].get("intent"))
        return True
    out = run(DC.run_steps(rec, values, executor=Bare(ax_tree=world), approver=note, decide_as_driving=True,
                           on_step=world.at_step))
    assert not out["ok"] and AX.NOT_IMPLEMENTED in out["stopped"]
    assert out["steps"][-1]["reason_code"] == "unsupported_verb" and out["steps"][-1]["refused"]
    assert [e["op"] for e in world.effects] == ["enter_text"] and len(held) == 1
    # the fill refuses a record whose recorded steps it could not run
    st2 = storage()
    compile_(row_draft([step({"action": "click", "map_hop": True}, "a hop")]), ROW_PARAMS, st2)
    out = run(DF.fill_saved("open_file", values, approver=never, llm=lambda *a: None, storage=st2,
                            executor=Bare(ax_tree=world), preflight=no_preflight, verdicts=vstore()))
    assert not out["ok"] and AX.NOT_IMPLEMENTED in out["stopped"]


def test_an_older_runner_refuses_a_version_2_record():
    st = storage()
    compile_(multi_draft(), MULTI_PARAMS, st)
    _wf, rec = DC.find_saved("copy_list", st)
    assert DC.record_version_problem(rec) is None
    real = DC.COMPILER
    DC.COMPILER = {"name": "draft_compile", "version": 1}       # the runner before the contract
    try:
        assert "Update AnyUse" in DC.record_version_problem(rec)
    finally:
        DC.COMPILER = real


# ── 8. an old record: compiled, proved and replayed exactly as before ─────────────────────────

def _without_owned_app(steps, bundle_id):
    out = copy.deepcopy(steps)
    for s in out:
        sel = (s.get("command") or {}).get("selector")
        if isinstance(sel, dict) and sel.get("app") == bundle_id and "app" not in s:
            sel.pop("app")
    return out


def test_an_old_record_compiles_proves_and_replays_as_before():
    old = json.loads(OLD.read_text())
    assert old["compiler"] == {"name": "draft_compile", "version": 1} and DC.record_version_problem(old) is None
    rep = compile_(str(TE), TE_PARAMS, save=False)
    st = storage()
    compile_(str(TE), TE_PARAMS, st)
    _wf, now = DC.find_saved("write_note", st)
    # a one-app record now names its app in every selector (the old record does not); the comparison
    # leaves exactly that out
    for key in ("steps", "recorded"):
        now[key] = _without_owned_app(now[key], old["bundle_id"])
    # a record states the drift settings in force when compiled, not a constant profile
    # that no replay applied; the comparison leaves those out too
    assert now["drift_profile"]["settings"] == DC.drift_profile_settings()
    assert "replaying machine" in now["drift_profile"]["settings_note"]
    now["drift_profile"] = {k: v for k, v in now["drift_profile"].items() if k not in ("settings", "settings_note")}
    old_cmp = dict(old, drift_profile={k: v for k, v in old["drift_profile"].items() if k != "settings"})
    for key in ("steps", "recorded", "approvals", "state_graph", "parameters", "candidates", "unreplayable",
                "drift_profile", "compiler", "format", "kind"):
        assert now[key] == old_cmp[key], key
    assert DC.artifact_digest(now) == old["verdict_keys"]["artifact_digest"]    # the artifact, unchanged
    assert DC.artifact_digest(old) == old["verdict_keys"]["artifact_digest"]
    assert "apps" not in now and not DC.is_multi_app(now) and AX.record_needs(now) == []
    assert rep["status"] == "ready"
    # proved again on the same fresh values, with the same effects
    proof = run(PR.prove(old))
    assert proof["passed"] and "emulated" not in proof
    assert [c["values"] for c in proof["cases"]] == [c["values"] for c in old["proof"]["cases"]]
    assert [(c["holds"], c["steps_run"]) for c in proof["cases"]] == [(c["holds"], c["steps_run"])
                                                                      for c in old["proof"]["cases"]]
    # replayed through the plain checked executor: no runner refusal, the recorded effects with the new values
    st2 = storage()
    DC.save_workflow(copy.deepcopy(old), storage=st2)
    world = PR.SimAXWorld(old)
    values = {"text": "Brief an Anna", "dateiname": "brief.txt"}
    out = run(DC.replay_saved("write_note", values, approver=yes, storage=st2,
                              executor=DC.checked_executor(world), preflight=GATES, on_step=world.at_step,
                              verdicts=vstore()))
    assert out["ok"], out["stopped"]
    assert [{k: e[k] for k in ("step", "op", "target", "value")} for e in world.effects] == PR.expected_effects(
        old, values)


# ── 9. edge cases ──────────────────────────────────────────────────────────────────────────

OLD_ROWS = FIX / "old_record_v1_rows.json"


def test_an_old_record_with_a_row_and_a_document_compiles_proves_and_replays_as_before():
    """Compiled by ``draft_compile`` version 1: a nameless row click (its path and the reuse id) and a
    button whose recorded selector named its document. Both compile exactly as then."""
    old = json.loads(OLD_ROWS.read_text())
    assert old["compiler"] == {"name": "draft_compile", "version": 1}
    st = storage()
    compile_(str(FIX / "old_rows_draft.json"), TE_PARAMS, st)
    _wf, now = DC.find_saved("write_note_rows", st)
    for key in ("steps", "recorded"):
        now[key] = _without_owned_app(now[key], old["bundle_id"])
    for key in ("steps", "recorded", "approvals", "parameters", "candidates", "unreplayable", "compiler"):
        assert now[key] == old[key], key
    assert DC.artifact_digest(now) == old["verdict_keys"]["artifact_digest"]
    row = next(s for s in now["steps"] if s["command"]["selector"]["canonical_role"] == "row")
    assert row["command"]["action"] == "press" and "kind" not in row["command"]["selector"]
    assert not any("document" in (s["command"].get("selector") or {}) for s in now["steps"])
    proof = run(PR.prove(old))
    assert proof["passed"], proof["reason"]
    st2 = storage()
    DC.save_workflow(copy.deepcopy(old), storage=st2)
    world = PR.SimAXWorld(old)
    out = run(DC.replay_saved("write_note_rows", {"text": "Brief", "dateiname": "brief.txt"}, approver=yes,
                              storage=st2, executor=DC.checked_executor(world), preflight=GATES,
                              on_step=world.at_step, verdicts=vstore()))
    assert out["ok"], out["stopped"]


def test_document_stays_only_on_focused_selectors():
    d = draft("docs", [step(click(dict(sel("button", "OK", raw="AXButton"), document="/Users/x/a.txt")), "ok"),
                       step({"action": "type", "text": "x", "focused": True,
                             "selector": {"canonical_role": "textfield", "document": "/Users/x/a.txt"}}, "type")])
    a, b = DC.load_source(d).steps
    assert "document" not in a.selector and b.selector["document"] == "/Users/x/a.txt"


class MangledArea(LiveNotes):
    """The notes area changes what was typed (an autocorrect): the append's read-back fails."""

    def __init__(self, record):
        super().__init__(record)
        self.writes = 0

    def edit_text(self, element, text, replace_range=None, expect_value=None):
        self.writes += 1
        return super().edit_text(element, text + "!", replace_range, expect_value)


def append_gap_draft():
    return draft("append_note", [
        step(typed("Projekt Alpha", sel("textfield", ident="projectField", raw="AXTextField")), "the project"),
        step({"action": "type", "selector": sel("textarea", ident="noteBody", raw="AXTextArea"),
              "input_mode": "append"}, "append the project to the note"),
        step(click(sel("button", "Fertig", raw="AXButton")), "done")])


@with_data_dir
def test_a_filled_action_that_acted_and_failed_stops_the_fill_with_one_write():
    st = storage()
    rep = compile_(append_gap_draft(), [{"name": "projekt", "ref": "@c1"}], st)
    assert rep["status"] == "needs_fill", rep["reasons"]
    _wf, rec = DC.find_saved("append_note", st)
    tree = MangledArea(rec)
    asked = []

    def llm(messages, schema):
        turn = json.loads(messages[1]["content"])
        asked.append(turn)
        area = next(e["id"] for e in turn["elements"] if e.get("identifier") == "noteBody")
        a = {"thought": "-", "action": "enter_text", "element": area, "param": "projekt", "text": "", "secret": "",
             "key": "", "mode": "append", "anchor": ""}
        return json.dumps(a), {"provider": "fake", "model": "fake", "calls": 1, "usage": {}}
    out = run(DF.fill_saved("append_note", {"projekt": "Projekt Beta"}, approver=yes, llm=llm, ax_tree=tree,
                            executor=DC.checked_executor(tree), storage=st, preflight=GATES, on_step=tree.at_step,
                            verdicts=vstore()))
    assert not out["ok"] and "stopped the fill" in out["stopped"] and "uncertain_completion" in out["stopped"]
    assert tree.writes == 1 and len(asked) == 1                 # never asked again, never written twice
    assert DC.find_saved("append_note", st)[1]["status"] == "needs_fill"     # nothing saved


def test_the_cli_replay_path_takes_the_configured_app_blocklist():
    """The CLI calls replay_saved without ``restriction=``: the configured blocklist applies."""
    st = storage()
    compile_(multi_draft(), MULTI_PARAMS, st)
    prev = os.environ.get("ANYUSE_APP_BLOCKLIST")
    for blocked in ("com.apple.Notes", "Notes"):
        os.environ["ANYUSE_APP_BLOCKLIST"] = blocked
        try:
            world = PR.SimMultiAppWorld(rec := DC.find_saved("copy_list", st)[1])
            out = run(DC.replay_saved("copy_list", {"liste": "Liste: Brot", "titel": "Woche"}, approver=yes,
                                      storage=st, tree_factory=world.tree_for, preflight=GATES,
                                      on_step=world.at_step, verdicts=vstore(), supervised=True))
        finally:
            if prev is None:
                os.environ.pop("ANYUSE_APP_BLOCKLIST", None)
            else:
                os.environ["ANYUSE_APP_BLOCKLIST"] = prev
        assert not out["ok"] and out["steps"][-1]["reason_code"] == "restricted", (blocked, out["stopped"])
        assert [e["op"] for e in world.effects] == ["enter_text"] and world.journal.front == "com.apple.TextEdit"
    assert rec["apps"][1]["bundle_id"] == "com.apple.Notes"


def test_the_policy_reads_the_bundle_id_first_and_the_name_as_an_extra():
    from anyuse.core.restriction import RestrictionPolicy
    block = RestrictionPolicy.from_boundary({"blocked_apps": ["com.apple.Notes"]})
    assert not block.is_app_target_allowed("com.apple.Notes", "Notizen")[0]
    assert not RestrictionPolicy.from_boundary({"blocked_apps": ["Notizen"]}).is_app_target_allowed(
        "com.apple.Notes", "Notizen")[0]
    allow = RestrictionPolicy.from_boundary({"list_mode": "allowlist", "allowed_apps": ["Notes"]})
    assert allow.is_app_target_allowed("com.apple.Notes", "Notes")[0]
    assert not allow.is_app_target_allowed("com.apple.Mail", "Mail")[0]
    assert RestrictionPolicy.from_settings(env={}).active is False
    pol = RestrictionPolicy.from_settings(env={"ANYUSE_APP_BLOCKLIST": "com.apple.Notes, Terminal"})
    assert pol.blocked_apps == ["com.apple.Notes", "Terminal"]


def test_a_wrapper_names_the_executor_it_wraps():
    class Guarded:
        """The guest's GuardedCheckedExecutor shape: it delegates every attribute."""

        def __init__(self, ex, named=True):
            self.ex = ex
            if named:
                self.__wrapped_executor__ = ex

        def __getattr__(self, name):
            return getattr(self.ex, name)
    inner = DC.checked_executor(object())
    assert AX.executor_supports(Guarded(inner)) == set(AX.CONTRACT)
    assert AX.executor_supports(Guarded(inner, named=False)) == frozenset()       # fails closed


def test_the_cli_tree_factory_looks_up_only_marked_bundle_ids():
    import subprocess
    from anyuse import cli as CLI
    from anyuse.context.accessibility import AccessibilityTree
    calls = []
    real_run, real_pid = subprocess.run, AccessibilityTree.running_pid

    def fake_run(argv, **_kw):
        calls.append(("pgrep", argv[-1]))
        return types.SimpleNamespace(stdout="4242\n")

    def fake_pid(bid):
        calls.append(("bundle", str(bid)))
        return 77
    subprocess.run, AccessibilityTree.running_pid = fake_run, staticmethod(fake_pid)
    try:
        for name in ("zoom.us", "draw.io", "TextEdit"):
            assert CLI._pid_tree(name)._pid == 4242
        assert CLI._pid_tree(DC.BundleId("com.apple.Notes"))._pid == 77
    finally:
        subprocess.run, AccessibilityTree.running_pid = real_run, staticmethod(real_pid)
    assert calls == [("pgrep", "zoom.us"), ("pgrep", "draw.io"), ("pgrep", "TextEdit"), ("bundle", "com.apple.Notes")]


def test_a_one_app_replay_finds_its_app_by_bundle_id_not_by_its_localized_name():
    """On a German system the record names "Schach", the process is "Chess": ``pgrep -x Schach``
    finds nothing and every replay would be refused. The record's bundle id finds it; a record without
    one, or whose bundle id finds no app, is looked up by its name."""
    asked = []

    def factory(found):
        def make(app):
            asked.append((type(app) is DC.BundleId, str(app)))
            return found.get((type(app) is DC.BundleId, str(app)))
        return make
    chess = object()
    tree, why = DC.start_tree({"app": "Schach", "bundle_id": "com.apple.Chess"}, None,
                              factory({(True, "com.apple.Chess"): chess}))
    assert tree is chess and why == "" and asked == [(True, "com.apple.Chess")]
    asked.clear()
    tree, why = DC.start_tree({"app": "TextEdit", "bundle_id": ""}, None, factory({(False, "TextEdit"): "te"}))
    assert tree == "te" and asked == [(False, "TextEdit")]
    asked.clear()
    tree, why = DC.start_tree({"app": "TextEdit", "bundle_id": "com.apple.TextEdit"}, None,
                              factory({(False, "TextEdit"): "by-name"}))
    assert tree == "by-name" and asked == [(True, "com.apple.TextEdit"), (False, "TextEdit")]
    tree, why = DC.start_tree({"app": "Schach", "bundle_id": "com.apple.Chess"}, None, factory({}))
    assert tree is None and "'Schach' (com.apple.Chess)" in why and "start the app first" in why
    # the CLI's factory asks the running apps for a bundle id, never ``pgrep -x`` (the localized name)
    import subprocess
    from anyuse import cli as CLI
    from anyuse.context.accessibility import AccessibilityTree
    calls = []
    real_run, real_pid = subprocess.run, AccessibilityTree.running_pid
    subprocess.run = lambda argv, **_kw: calls.append(("pgrep", argv[-1])) or types.SimpleNamespace(stdout="")
    AccessibilityTree.running_pid = staticmethod(lambda bid: calls.append(("bundle", str(bid))) or 404)
    try:
        tree, why = DC.start_tree({"app": "Schach", "bundle_id": "com.apple.Chess"}, None, CLI._pid_tree)
    finally:
        subprocess.run, AccessibilityTree.running_pid = real_run, staticmethod(real_pid)
    assert tree._pid == 404 and why == "" and calls == [("bundle", "com.apple.Chess")]


def test_the_approval_line_names_the_deselect():
    st = storage()
    compile_(row_draft(), ROW_PARAMS, st)
    _wf, rec = DC.find_saved("open_file", st)
    action = DC._gate_action(DC.bound_steps(rec, {"suche": "a", "datei": "b"})[1], "all")
    assert "may deselect the row again if the selection grows" in action["args"]["target"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
