"""Compiled desktop routines with input binding, replayed on NEW inputs with runtime checks
(``execution/desktop_routine.py``), offline with fakes.

Pinned here:

* the parameters are read off a successful run's trace (Arch12 on the fake guest): the folder entered, the
  file / note opened, the detail read, the text written, the old text an edit replaces, the term looked up, a new
  object's name, a flag's value, a menu group's option; each bound to every place it occurs, equal values of one
  family are one parameter, the request's verbatim spans are bound too;
* today's compiled format is untouched: a plain list or a replay without new values gives the steps byte for byte;
* a replay on new values does the task on the NEW inputs with 0 model calls;
* the checks: bad values are refused before acting; the target's identity (exists, unique, kind, the folder the
  replay entered, a new name not taken) before acting; the item before a human gate; the exact permitted diff, the
  journal and its compare-and-swap restore on a parameterized replay (a restore is an error); the guest's
  irreversible-action guard; an unmet check is an honest failure with nothing done.

No desktop, no AX, no subprocess, no network.
"""
import json
import sys

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z
from anyuse.execution import desktop_routine as R

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
ROOT = T4.ROOT
TREE = {"P/Final/approved.txt": "Approved", "P/Drafts/draft.txt": "Draft",
        "P/preise.txt": "Preise\nPreis: 5 Euro\nRest", "P/alt.txt": "Preise alt\nPreis: 4 Euro"}
NOTES = {"Werkstatt": [("Türschloss", "Türschloss\nÜbersetzung fehlt für: Falle."),
                       ("Scharnier", "Scharnier\nÜbersetzung fehlt für: Angel.")],
         "Finanzen": [("Budget", "Budget\nWort: Haushalt.")]}


def arch12(g, planner):
    return Z.Arch12(g, EMAP, planner=planner, spend=None, inbox=None, sleep=lambda s: None, precondition13=True)


def replayer(g):
    return R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)


def trace(goal, *plans, tree=TREE, shown="P", notes=None):
    g = T9.FakeGuest9(tree, shown=shown, notes=notes)
    rec = X.Rec("T", "arch12", 1)
    arch12(g, FakePlanner4(*plans)).run(X.Task("T", goal, "single-app", {}, [], {}), rec)
    assert rec.status == "completed", (rec.stopped, rec.steps[-1])
    return rec.compiled


def replay(routine, values, tree=TREE, shown="P", notes=None, guest=None):
    g = guest or T9.FakeGuest9(tree, shown=shown, notes=notes)
    a = replayer(g)
    steps, goal, probs = a.prepare(routine, values)
    rec = X.Rec("T", "replay", 1, mode="compiled")
    a.run(X.Task("T", goal, "single-app", {}, [], {}), rec, compiled=steps)
    return g, rec, goal, probs


GOAL_NEW = "Inside the Final subfolder, make an empty folder called Client copy."
PLAN_NEW = plan4(S("launch_app", "Finder"), S("navigate", "Finder", "Final"),
                 S("object_create", "Finder", "folder", "Client copy"))
GOAL_EDIT = "Open preise.txt and change Preis: 5 Euro to Preis: 7 Euro, then save."
PLAN_EDIT = (plan4(S("launch_app", "Finder"), S("open_item", "Finder", "preise.txt"),
                   S("read_content", "TextEdit", "document", "text")),
             plan4(S("extract", "none", "the old price line", "old", "", find="Preis: 5 Euro"),
                   S("doc_save", "TextEdit", "", "Preis: 7 Euro", mode="edit", find="{{old}}")))
P = lambda name: f"{ROOT}/P/{name}"                                                     # noqa: E731


# ── 1. input binding ───────────────────────────────────────────────────────────────────────────────

def test_the_parameters_of_a_create_run_are_the_folder_and_the_name_bound_to_steps_and_request():
    r = R.derive_routine(trace(GOAL_NEW, PLAN_NEW), GOAL_NEW, task="T", arm="arch12")
    assert r["format"] == R.ROUTINE_FORMAT and r["task"] == "T"
    ps = {p["name"]: p for p in r["params"]}
    assert set(ps) == {"folder", "name"}
    assert ps["folder"]["value"] == "Final" and [1, "resolved", "label"] in ps["folder"]["bind"]
    assert [1, "step", "target"] in ps["folder"]["bind"] and ps["folder"]["goal_spans"] == 1
    assert ps["name"]["value"] == "Client copy" and ps["name"]["bind"] == [[2, "step", "value"]]


def test_an_edit_run_yields_file_detail_and_text_and_one_value_is_one_parameter():
    comp = trace(GOAL_EDIT, *PLAN_EDIT)
    r = R.derive_routine(comp, GOAL_EDIT)
    ps = {p["name"]: p for p in r["params"]}
    assert set(ps) == {"file", "detail", "text"}
    assert ps["file"]["value"] == "preise.txt" and ps["detail"]["value"] == "Preis: 5 Euro"
    assert ps["text"]["value"] == "Preis: 7 Euro" and ps["text"]["bind"] == [[4, "step", "value"]]
    # the edit's find is the plan variable {{old}}: bound by the detail at replay time, no parameter of its own
    comp2 = json.loads(json.dumps(comp))
    comp2[4]["step"]["find"] = "Preis: 5 Euro"                      # a literal anchor equal to the detail
    ps2 = {p["name"]: p for p in R.derive_routine(comp2, GOAL_EDIT)["params"]}
    assert set(ps2) == {"file", "detail", "text"} and [4, "step", "find"] in ps2["detail"]["bind"]


def test_settings_lookups_and_stops_flags_choices_terms_and_no_input_for_a_gate():
    sort = T4.SORT
    comp = [{"step": S("launch_app", "Finder"), "resolved": {"op": "launch", "bundle": T4.FINDER}},
            {"step": S("toggle_select", "Finder", "Sortieren nach", "Art"),
             "resolved": {"op": "toggle", "entry": sort, "option": sort["options"][1]}},
            {"step": S("flag_toggle", "TextEdit", "Intelligente Links", "off"),
             "resolved": {"op": "flag", "entry": None, "option": {"key": "k", "label": "Intelligente Links"}}},
            {"step": S("scoped_find", "Dictionary", "", "Zeitgeist"), "resolved": {"op": "scoped_find", "entry": {}}},
            {"step": S("human_gate", "Finder", "Papierkorb", "move"), "resolved": {"op": "gate"}}]
    ps = {p["name"]: p for p in R.derive_routine(comp, "Schlag Zeitgeist nach.")["params"]}
    assert set(ps) == {"choice", "setting", "lookup"}
    assert ps["lookup"]["goal_spans"] == 1 and ps["setting"]["goal_spans"] == 0


def test_no_parameters_or_no_new_values_is_todays_compiled_format_byte_for_byte():
    comp = trace(GOAL_EDIT, *PLAN_EDIT)
    before = json.dumps(comp, ensure_ascii=False)
    r = R.derive_routine(comp, GOAL_EDIT)
    assert json.dumps(r["steps"], ensure_ascii=False) == before
    steps, goal, probs = R.instantiate(r, {})
    assert steps is r["steps"] and goal == GOAL_EDIT and probs == []
    same = {p["name"]: p["value"] for p in r["params"]}
    assert R.instantiate(r, same)[0] is r["steps"]                 # the recorded values: nothing changes
    assert R.instantiate(comp)[0] is comp and R.instantiate(comp, {})[0] is comp
    assert R.instantiate(comp, {"text": "x"})[2]                     # a plain list takes no values
    new = R.instantiate(r, {"text": "Preis: 9 Euro"})[0]
    assert json.dumps(r["steps"], ensure_ascii=False) == before and new[4]["step"]["value"] == "Preis: 9 Euro"


# ── replays on new inputs ──────────────────────────────────────────────────────────────────────────

def test_a_create_routine_replays_on_a_new_folder_and_name_with_no_model_call():
    r = R.derive_routine(trace(GOAL_NEW, PLAN_NEW), GOAL_NEW)
    g, rec, goal, probs = replay(r, {"folder": "Drafts", "name": "Print proof"})
    assert probs == [] and rec.status == "completed", rec.stopped
    assert goal == "Inside the Drafts subfolder, make an empty folder called Print proof."
    assert g.isdir(P("Drafts/Print proof")) and not g.isdir(P("Final/Print proof"))
    assert sum(rec.calls.values()) == 0 and rec.identity15 == []


def test_the_plans_scope_is_bound_with_the_folder_and_the_location_check_reads_the_new_one():
    """GG10: the compiled object_create carried the plan's scope "Final"; unbound, Arch4's location
    check refused the replay into Drafts (nothing created). Bound, the replay creates the folder in Drafts."""
    plan = plan4(S("launch_app", "Finder"), S("navigate", "Finder", "Final"),
                 S("object_create", "Finder", "folder", "Client copy", sc="Final"))
    r = R.derive_routine(trace(GOAL_NEW, plan), GOAL_NEW)
    ps = {p["name"]: p for p in r["params"]}
    assert set(ps) == {"folder", "name"} and [2, "step", "scope"] in ps["folder"]["bind"]
    g, rec, _, _ = replay(r, {"folder": "Drafts", "name": "Print proof"})
    assert rec.status == "completed", rec.stopped
    assert g.isdir(P("Drafts/Print proof")) and "the plan names" in rec.steps[2]["location"]
    unbound = json.loads(json.dumps(r))
    unbound["params"][0]["bind"].remove([2, "step", "scope"])
    g, rec, _, _ = replay(unbound, {"folder": "Drafts", "name": "Print proof"})
    assert rec.stopped == "refused (location check)" and not g.isdir(P("Drafts/Print proof"))


def test_an_edit_routine_replays_on_a_new_file_detail_and_text():
    r = R.derive_routine(trace(GOAL_EDIT, *PLAN_EDIT), GOAL_EDIT)
    g, rec, goal, _ = replay(r, {"text": "Preis: 9 Euro"})
    assert rec.status == "completed" and g.fs[P("preise.txt")] == "Preise\nPreis: 9 Euro\nRest"
    g, rec, goal, _ = replay(r, {"file": "alt.txt", "detail": "Preis: 4 Euro", "text": "Preis: 6 Euro"})
    assert rec.status == "completed", rec.stopped
    assert g.fs[P("alt.txt")] == "Preise alt\nPreis: 6 Euro" and g.fs[P("preise.txt")] == TREE["P/preise.txt"]
    assert goal == "Open alt.txt and change Preis: 4 Euro to Preis: 6 Euro, then save."


# ── 2. the checks ──────────────────────────────────────────────────────────────────────────────────

def test_bad_values_are_refused_before_anything_is_done():
    r = R.derive_routine(trace(GOAL_NEW, PLAN_NEW), GOAL_NEW)
    for vals, want in (({"nope": "x"}, "unknown parameter"), ({"name": ""}, "empty"),
                       ({"folder": "a/b"}, "path separator"), ({"name": 3}, "not text")):
        g, rec, _goal, probs = replay(r, vals)
        assert probs and want in probs[0] and rec.stopped.startswith("refused (parameter check)")
        assert g.log[:1] == [] or all(op not in ("press", "select", "enter") for op, _a in g.log)
    comp = [{"step": S("toggle_select", "Finder", "Sortieren nach", "Art"),
             "resolved": {"op": "toggle", "entry": T4.SORT, "option": T4.SORT["options"][1]}},
            {"step": S("flag_toggle", "TextEdit", "Intelligente Links", "off"),
             "resolved": {"op": "flag", "entry": None, "option": {"key": "k", "label": "Intelligente Links"}}}]
    rt = R.derive_routine(comp)
    assert "options of the recorded group" in R.parameter_problems(rt, {"choice": "Größe"})[0]
    assert "not on/off" in R.parameter_problems(rt, {"setting": "vielleicht"})[0]
    assert R.parameter_problems(rt, {"choice": "Änderungsdatum", "setting": "on"}) == []
    steps = R.instantiate(rt, {"choice": "Änderungsdatum"})[0]
    assert steps[0]["resolved"]["option"]["label"] == "Änderungsdatum" and steps[0]["step"]["value"] == "Änderungsdatum"
    ext = json.loads(json.dumps(comp[:1]))
    ext[0]["resolved"]["entry"]["options"].append(dict(T4.SORT["options"][0], label="Wikipedia", key="w"))
    ext[0]["resolved"]["entry"]["options"].append(dict(T4.SORT["options"][0], label="In den Papierkorb legen",
                                                       key="t"))
    rx = R.derive_routine(ext)
    assert "outside the Mac" in R.parameter_problems(rx, {"choice": "Wikipedia"})[0]
    assert "irreversible" in R.parameter_problems(rx, {"choice": "In den Papierkorb legen"})[0]


def test_the_target_must_exist_be_unique_of_its_kind_and_in_the_folder_entered():
    r = R.derive_routine(trace(GOAL_NEW, PLAN_NEW), GOAL_NEW)
    g, rec, _, _ = replay(r, {"folder": "Archiv"})
    assert rec.stopped.startswith("failed step: identity check: 'Archiv' is not in") and rec.identity15
    assert not any(op in ("press", "enter") for op, _a in g.log)
    g, rec, _, _ = replay(r, {"name": "Fresh"})
    assert rec.status == "completed" and g.isdir(P("Final/Fresh"))                     # a free name: fine
    g, rec, _, _ = replay(r, {"name": "approved.txt"})                                 # taken in Final
    assert "already exists" in rec.stopped and not any(op == "press" for op, _a in g.log)
    e = R.derive_routine(trace(GOAL_EDIT, *PLAN_EDIT), GOAL_EDIT)
    g, rec, _, _ = replay(e, {"file": "Final"})
    assert "is a folder, the routine opens a file" in rec.stopped and g.fs[P("preise.txt")] == TREE["P/preise.txt"]


def test_two_notes_with_one_title_are_never_guessed_and_the_folder_entered_is_checked():
    goal = "Öffne im Ordner Werkstatt die Notiz Türschloss."
    plan = plan4(S("launch_app", "Notes"), S("navigate", "Notes", "Werkstatt"), S("open_item", "Notes", "Türschloss"))
    r = R.derive_routine(trace(goal, plan, notes=NOTES), goal)
    assert {p["name"] for p in r["params"]} == {"folder", "note"}
    g, rec, _, _ = replay(r, {"note": "Scharnier"}, notes=NOTES)
    assert rec.status == "completed" and g.shown_note == "Scharnier"
    dup = {"Werkstatt": NOTES["Werkstatt"] + [("Scharnier", "Scharnier\nzweite")], "Finanzen": NOTES["Finanzen"]}
    g, rec, _, _ = replay(r, {"note": "Scharnier"}, notes=dup)
    assert "2 items are named 'Scharnier'" in rec.stopped and g.shown_note is None
    g, rec, _, _ = replay(r, {"folder": "Finanzen"}, notes=NOTES)                      # Türschloss is not there
    assert "'Türschloss' is not in 'Finanzen'" in rec.stopped and g.shown_note is None


def test_before_a_gate_the_item_must_be_the_one_the_replay_bound():
    g = T9.FakeGuest9(TREE, shown="P")
    a = replayer(g)
    a._bound15 = {f"item:{T4.FINDER}": "preise.txt"}
    g.sel = ["alt.txt"]
    assert "the replay bound 'preise.txt'" in a._gate15(S("human_gate", "Finder", "preise.txt", "delete"))
    g.sel = ["preise.txt"]
    assert a._gate15(S("human_gate", "Finder", "preise.txt", "delete")) is None
    comp = [{"step": S("launch_app", "Finder"), "resolved": {"op": "launch", "bundle": T4.FINDER}},
            {"step": S("select_item", "Finder", "preise.txt"), "resolved": {"op": "select", "label": "preise.txt"}},
            {"step": S("human_gate", "Finder", "preise.txt", "delete"), "resolved": {"op": "gate"}}]
    r = R.derive_routine(comp, "Lösch preise.txt.")
    g, rec, _, _ = replay(r, {"file": "alt.txt"})
    assert rec.stopped == "human gate (declined)" and g.sel == ["alt.txt"] and rec.gate_stops == 1
    assert X.irreversible_reason("delete") and rec.irreversible_executed == []


def test_a_parameterized_write_keeps_the_exact_diff_and_a_torn_save_is_restored_and_recorded_as_an_error():
    r = R.derive_routine(trace(GOAL_EDIT, *PLAN_EDIT), GOAL_EDIT)
    g = T9.FakeGuest9(TREE, shown="P")
    real = g.op_press

    def torn(bundle, trigger, label):
        out = real(bundle, trigger, label)
        if bundle == T4.TE and g.te_front == P("alt.txt"):
            g.fs[P("alt.txt")] = g.fs[P("alt.txt")][:6]                          # an interrupted save
        return out
    g.op_press = torn
    g, rec, _, _ = replay(r, {"file": "alt.txt", "detail": "Preis: 4 Euro", "text": "Preis: 6 Euro"}, guest=g)
    assert g.fs[P("alt.txt")] == TREE["P/alt.txt"]                                     # the preimage again
    assert rec.errors9 and rec.errors9[0]["restored"] and rec.stopped.startswith("refused (write check failed")
    # an anchor the new file does not hold: nothing is written
    g, rec, _, _ = replay(r, {"file": "alt.txt", "detail": "Preis: 9 Euro"})
    assert rec.stopped and g.fs[P("alt.txt")] == TREE["P/alt.txt"] and not any(op == "enter" for op, _a in g.log)


def test_the_guests_irreversible_guard_still_stops_a_replay():
    comp = [{"step": S("launch_app", "Finder"), "resolved": {"op": "launch", "bundle": T4.FINDER}},
            {"step": S("object_create", "Finder", "folder", "Neu"),
             "resolved": {"op": "object_create", "entry": dict(T4.NEWF, path="Ablage > In den Papierkorb legen")}}]
    r = R.derive_routine(comp, "x")
    g, rec, _, _ = replay(r, {"name": "Neu 2"})
    assert rec.stopped == "irreversible-action guard (declined)" and rec.irreversible_executed == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
