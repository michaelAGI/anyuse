"""Arch2: the planner fixes, offline,
with the FAKE guest and fake models.

What is pinned here: the Arch2 planner spec is frozen (sha256) and carries the
scope argument and each app's observed map vocabulary; a goal's restriction
("nur im Duden", "only from the X folder") must be APPLIED by some step (a
scope, a navigate target, a toggle_select value) or the plan is re-planned
once with feedback and otherwise not executed; a scope becomes the map's own
step (toggle option or location); lead-up steps the next step does not need
are dropped deterministically (search before an open/select of a content
row, select before open, a navigate the goal does not name before a
container-free step) and kept where the goal asks for them; the map declares
the coupled marks each probe saw (Position fixieren -> X/Y locks) and both
the flag check and the judge accept exactly those; the Arch2 tasks are
fresh and their coupled marks come from the map.

No desktop, no AX, no subprocess, no network.
"""
import hashlib
import json

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e2 as Y

import test_desktop_e2e as T3

EMAP2 = Y.EffectMap2()
S = lambda e, a, t="", v="", sc="": {"effect": e, "app": a, "target": t, "value": v, "scope": sc}  # noqa: E731


def plan2(*steps):
    return json.dumps({"steps": [dict(zip(("effect", "app", "target", "value", "scope"), s + ("",) * (5 - len(s))))
                                 for s in steps]})


class FakePlanner2:
    def __init__(self, *texts):
        self.texts, self.calls, self.histories = list(texts), 0, []

    def __call__(self, goal, history=None):
        self.calls += 1
        self.histories.append(history)
        return (self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]), {"cost": 0.001}


def arch2(guest, planner, chooser=None, spend=None):
    return Y.Arch2(guest, EMAP2, planner=planner, chooser=chooser, spend=spend, inbox=None, sleep=lambda s: None)


# ── the Arch2 planner ────────────────────────────────────────────────────────

def test_planner_v2_is_frozen_and_carries_scope_and_the_map_vocabulary():
    spec = Y.load_planner2_spec()
    assert hashlib.sha256(spec["w2_system"].encode()).hexdigest() == spec["w2_sha256"]
    assert spec["plan_schema"]["properties"]["steps"]["items"]["required"] == \
        ["effect", "app", "target", "value", "scope"]
    assert spec["map_sha256"] == hashlib.sha256(open(Y.MAP2_PATH, "rb").read()).hexdigest()
    w2 = spec["w2_system"]
    for label in ("Duden-Wissensnetz deutsche Sprache", "1x | 2x | 4x | 8x", "Position fixieren", "Hinzugefügt am",
                  "Intelligente Bindestriche", "Als Galerie"):
        assert label in w2
    assert spec["call"]["model"] == "gpt-oss-120b" and spec["call"]["temperature"] == 0.1
    m = Y.planner2_messages("Open the file from March.", spec)
    assert m[1]["content"] == "Plan this goal: Open the file from March." and len(m) == 2
    fb = Y.feedback_messages('{"steps": []}', [{"phrase": "nur im duden nach", "key": ["duden"]}], spec)
    assert fb[0]["role"] == "assistant" and "nur im duden nach" in fb[1]["content"]


def test_parse_plan2_reads_scope_and_rejects_outside_the_vocabulary():
    steps, err = Y.parse_plan2(plan2(("launch_app", "Dictionary"), ("scoped_find", "Dictionary", "", "Fernweh",
                                                                     "Duden")))
    assert err is None and steps[1]["scope"] == "Duden" and steps[0]["scope"] == ""
    assert Y.parse_plan2(plan2(("delete_file", "Finder")))[1]
    assert Y.parse_plan2(plan2(("launch_app", "Safari")))[1]


# ── restrictions ────────────────────────────────────────────────────────────

def test_restrictions_are_read_in_english_and_german():
    k = lambda g: [r["key"] for r in Y.restrictions(g)]  # noqa: E731
    assert k("Schlag „Fernweh“ nur im Duden nach.") == [["duden"]]
    assert k("Schlag „Zeitgeist“ nur im Oxford-Wörterbuch nach.") == [["oxford"]]
    assert k("Look up 'wanderlust' only in the Apple dictionary.") == [["apple"]]
    assert k("Open the workshop note, but only from the AnyUse-E2 folder.") == [["anyuse", "e2"]]
    assert k("Das Wörterbuch soll nur noch in Wikipedia suchen.") == [["wikipedia"]]
    assert k("Limit the search to the Downloads folder.") == [["downloads"]]
    assert k("Beschränk die Suche auf den Ordner Rechnungen.") == [["rechnungen"]]
    # no restriction: plain location words, a count, "only" without a place
    assert k("Open the file from March in the test folder.") == []
    assert k("Keep only 3 files in the folder.") == []
    assert k("I only want German definitions.") == []


def test_a_restriction_is_carried_only_by_a_step_that_applies_it():
    g = "Open the workshop note, but only from the AnyUse-E2 folder."
    assert Y.missing_restrictions(g, [S("launch_app", "Notes"), S("open_item", "Notes", "workshop note in AnyUse-E2")])
    assert not Y.missing_restrictions(g, [S("navigate", "Notes", "AnyUse-E2"), S("open_item", "Notes", "workshop")])
    assert not Y.missing_restrictions(g, [S("open_item", "Notes", "workshop", "", "AnyUse-E2")])
    g = "Schlag „Fernweh“ nur im Duden nach."
    assert Y.missing_restrictions(g, [S("scoped_find", "Dictionary", "", "Fernweh")])
    assert not Y.missing_restrictions(g, [S("toggle_select", "Dictionary", "source", "Duden"),
                                          S("scoped_find", "Dictionary", "", "Fernweh")])
    # a restriction that names the app is carried by a step in that app
    assert not Y.missing_restrictions("Search only in Notes for 'milk'.", [S("scoped_find", "Notes", "", "milk")])


# ── the post-plan check against the map ─────────────────────────────────────

def _eff(ref):
    return [(s["effect"], s["target"] or s["value"]) for s in ref.steps]


def test_scope_becomes_the_maps_own_step():
    r = Y.refine_plan("Schlag „Fernweh“ nur im Duden nach.",
                      [S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "Fernweh", "Duden")], EMAP2)
    assert _eff(r)[1] == ("toggle_select", "Suchen") and r.steps[1]["value"] == "Duden-Wissensnetz deutsche Sprache"
    assert r.steps[2]["effect"] == "scoped_find" and not r.problems
    r = Y.refine_plan("Open the workshop note, but only from the AnyUse-E2 folder.",
                      [S("launch_app", "Notes"), S("open_item", "Notes", "workshop note", "", "AnyUse-E2")], EMAP2)
    assert _eff(r) == [("launch_app", ""), ("navigate", "AnyUse-E2"), ("open_item", "workshop note")]
    # already applied: nothing inserted twice
    r = Y.refine_plan("x", [S("toggle_select", "Dictionary", "Suchen", "Duden"),
                            S("scoped_find", "Dictionary", "", "w", "Duden")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["toggle_select", "scoped_find"]
    # a scope the map cannot apply: the plan is not executed
    r = Y.refine_plan("Search 'Budget' only in the open document.",
                      [S("scoped_find", "TextEdit", "", "Budget", "open document")], EMAP2)
    assert r.problems


def test_minimal_plan_drops_lead_ups_the_next_step_does_not_need():
    r = Y.refine_plan("Open the file from March in the test folder.",
                      [S("launch_app", "Finder"), S("navigate", "Finder", "test folder"),
                       S("scoped_find", "Finder", "", "March"), S("open_item", "Finder", "file from March")], EMAP2)
    assert _eff(r) == [("launch_app", ""), ("navigate", "test folder"), ("open_item", "file from March")]
    r = Y.refine_plan("Open the invoice in the test folder, write 'bezahlt' into it and save it.",
                      [S("launch_app", "Finder"), S("select_item", "Finder", "invoice"),
                       S("open_item", "Finder", "invoice"), S("doc_save", "TextEdit", "", "bezahlt")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "open_item", "doc_save"]
    r = Y.refine_plan("Colour meter to 4x.", [S("launch_app", "Notes"), S("navigate", "Notes", "Rezepte"),
                                              S("toggle_select", "Notes", "Darstellung", "Als Galerie")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "toggle_select"]


def test_a_scope_does_not_keep_a_lead_up_search_alive():
    # dev F2: scope "Projekt" on every step; the navigate applies it, the search is a lead-up
    r = Y.refine_plan("Open the file from March in the Projekt folder.",
                      [S("launch_app", "Finder"), S("navigate", "Finder", "Projekt", "", "Projekt"),
                       S("scoped_find", "Finder", "", "March", "Projekt"),
                       S("open_item", "Finder", "file from March", "", "Projekt")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "navigate", "open_item"] and not r.problems
    r = Y.refine_plan("Open the plumber note, but only from the AnyUse-Pick folder.",
                      [S("launch_app", "Notes"), S("scoped_find", "Notes", "", "plumber", "AnyUse-Pick"),
                       S("open_item", "Notes", "plumber note")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "navigate", "open_item"] and not r.problems


def test_an_unapplicable_scope_is_refused_only_when_it_alone_carries_a_restriction():
    # dev F7: the Finder location copied onto the TextEdit save of a cross-app goal
    r = Y.refine_plan("Open the cost plan in the Projekt folder, write 'ok' into it and save it.",
                      [S("launch_app", "Finder"), S("navigate", "Finder", "Projekt", "", "Projekt"),
                       S("open_item", "Finder", "cost plan", "", "Projekt"), S("doc_save", "TextEdit", "", "ok", "Projekt")],
                      EMAP2)
    assert not r.problems and [s["effect"] for s in r.steps] == ["launch_app", "navigate", "open_item", "doc_save"]
    r = Y.refine_plan("Save it only in the Projekt folder.", [S("doc_save", "TextEdit", "", "ok", "Projekt")], EMAP2)
    assert r.problems


def test_minimal_plan_keeps_what_the_goal_asks_for():
    # the goal asks for a search
    st = [S("launch_app", "Finder"), S("navigate", "Finder", "Documents"), S("scoped_find", "Finder", "", "agenda"),
          S("open_item", "Finder", "agenda")]
    assert Y.refine_plan("Find the file named 'agenda' in Documents and open it.", st, EMAP2).steps == \
        [dict(s) for s in st]
    # select before a gate is needed; a navigate the goal names is kept
    st = [S("launch_app", "Finder"), S("navigate", "Finder", "Downloads"), S("toggle_select", "Finder", "sort", "Name"),
          S("select_item", "Finder", "draft"), S("human_gate", "Finder", "draft", "delete")]
    assert [s["effect"] for s in Y.refine_plan("Sort Downloads by name, then trash the draft.", st, EMAP2).steps] \
        == [s["effect"] for s in st]
    # an app whose rows the map does not list keeps its search
    st = [S("launch_app", "Calendar"), S("scoped_find", "Calendar", "", "yoga"), S("open_item", "Calendar", "yoga")]
    assert len(Y.refine_plan("Open the yoga event.", st, EMAP2).steps) == 3


def test_an_invented_scope_is_ignored_and_a_written_then_saved_text_is_one_save():
    # the goal has no restriction and does not name the scope: the planner invented it
    r = Y.refine_plan("Search the system log for 'bluetooth'.",
                      [S("launch_app", "Console"), S("scoped_find", "Console", "", "bluetooth", "Systemprotokoll")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "scoped_find"] and r.steps[1]["scope"] == ""
    # a scope the goal names is applied even without a restriction word
    r = Y.refine_plan("Schlag „Resilienz“ im Duden nach.",
                      [S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "Resilienz", "Duden")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "toggle_select", "scoped_find"]
    r = Y.refine_plan("Save the document with the text 'Stand 3'.",
                      [S("launch_app", "TextEdit"), S("field_rw", "TextEdit", "document text", "Stand 3"),
                       S("doc_save", "TextEdit", "", "")], EMAP2)
    assert [(s["effect"], s["value"]) for s in r.steps] == [("launch_app", ""), ("doc_save", "Stand 3")]
    # a different text is not merged
    r = Y.refine_plan("x", [S("field_rw", "TextEdit", "t", "A"), S("doc_save", "TextEdit", "", "B")], EMAP2)
    assert len(r.steps) == 2


# ── the map: declared coupled settings ──────────────────────────────────────

def test_map2_declares_what_each_probe_saw_move_and_nothing_else_changed():
    lock = next(x for x in EMAP2.app("Digital Color Meter")["unmapped_observed"]["flag_toggle"]
                if x["path"] == "Darstellung > Position fixieren")
    assert lock["side_effects"] == ["_NS:184|x|0", "_NS:191|y|0"]
    assert lock["side_effect_paths"] == ["Darstellung > X-Koordinate fixieren", "Darstellung > Y-Koordinate fixieren"]
    assert EMAP2.coupled_paths("Finder", "Darstellung > Sortieren nach > Änderungsdatum") == \
        ["Darstellung > Sortieren nach > "]
    assert EMAP2.coupled_paths("Digital Color Meter", "Darstellung > Vergrößerung > 2x") == []

    def strip(d):
        if isinstance(d, dict):
            return {k: strip(v) for k, v in d.items() if k not in ("side_effect_paths",)
                    and not (k == "side_effects" and "trigger" in d and "options" not in d)}
        return [strip(x) for x in d] if isinstance(d, list) else d
    assert strip(json.load(open(X.MAP_PATH))) == strip(EMAP2.data)


class CoupledGuest(T3.FakeGuest):
    """Pressing Position fixieren also marks the X and Y locks (as in the guest)."""
    XY = ["_NS:184|x|0", "_NS:191|y|0"]

    def op_press(self, bundle, trigger, label):
        r = super().op_press(bundle, trigger, label)
        if trigger.get("identifier") == T3.LOCK["trigger"]["identifier"]:
            self.other_marks = sorted(set(self.other_marks) | set(self.XY)) if self.lock else ["_NS:999||0"]
        return r

    def op_read_state(self, read):
        out = super().op_read_state(read)
        for p, r in zip(read, out["read"]):
            if p["kind"] == "marks" and p["bundle"] == T3.DCM:
                for n in ("X", "Y"):
                    r["items"].append({"path": f"Darstellung > {n}-Koordinate fixieren", "mark": self.lock})
        return out


E8 = Y.task2_by_id("E8")
E8_PLAN = plan2(("launch_app", "Digital Color Meter"), ("toggle_select", "Digital Color Meter", "Vergrößerung", "2x"),
                ("flag_toggle", "Digital Color Meter", "Position fixieren", "on"))


def test_coupled_locks_pass_the_flag_check_and_the_judge_only_when_declared():
    g = CoupledGuest()
    g.front = T3.DCM
    out = Y.run_one2(E8, "arch", 1, g, arch=arch2(g, FakePlanner2(E8_PLAN)), emap=EMAP2)
    assert out["status"] == "completed", out["steps"]
    flag = [s for s in out["steps"] if s["step"]["effect"] == "flag_toggle"][0]
    assert flag["outcome"] == "ok" and "declared side effects" in flag["check"]
    assert out["judge"]["success"], out["judge"]
    assert out["judge"]["coupled_from_map"] == ["Darstellung > X-Koordinate fixieren",
                                                "Darstellung > Y-Koordinate fixieren"]
    # the base judge (no map-derived coupling) counts the two locks as unexpected
    assert len(X.judge(E8, out["read_start"], out["read_end"], out)["unexpected"]) == 2
    # the base flag check (no option-level side effects) fails on the same press
    g = CoupledGuest()
    g.front = T3.DCM
    rec = X.Rec("E8", "arch", 1)
    old = X.Arch(g, EMAP2, planner=lambda goal: (E8_PLAN.replace(', "scope": ""', ''), {"cost": 0.0}),
                 sleep=lambda s: None)
    old.run(E8, rec)
    assert any("sentinel changed" in (s.get("why") or "") for s in rec.steps if s["step"]["effect"] == "flag_toggle")


def test_an_undeclared_coupled_change_is_still_unexpected():
    g = CoupledGuest()
    g.front = T3.DCM
    out = Y.run_one2(E8, "arch", 1, g, arch=arch2(g, FakePlanner2(E8_PLAN)), emap=EMAP2)
    end = json.loads(json.dumps(out["read_end"]))
    end[0]["items"].append({"path": "Darstellung > Mausposition anzeigen", "mark": True})
    j = Y.judge(E8, out["read_start"], end, out, EMAP2)
    assert not j["success"] and j["unexpected"] == ["menu mark changed: Darstellung > Mausposition anzeigen (off -> on)"]


# ── the architecture arm (Arch2) ────────────────────────────────────────────

def test_a_dropped_restriction_is_replanned_once_with_feedback():
    g = T3.FakeGuest()
    p = FakePlanner2(plan2(("launch_app", "Dictionary"), ("scoped_find", "Dictionary", "", "Fernweh")),
                     plan2(("launch_app", "Dictionary"), ("scoped_find", "Dictionary", "", "Fernweh", "Duden")))
    a = arch2(g, p)
    rec = X.Rec("x", "arch", 1)
    steps, err = a.plan(rec, "Schlag „Fernweh“ nur im Duden nach.")
    assert err is None and p.calls == 2 and rec.calls["planner"] == 2
    assert p.histories[0] is None and "nur im duden nach" in p.histories[1][1]["content"]
    assert [s["effect"] for s in steps] == ["launch_app", "toggle_select", "scoped_find"]
    assert rec.plan[-1]["refined"]["changes"]


def test_a_restriction_still_dropped_after_feedback_executes_nothing():
    g = T3.FakeGuest()
    t = X.Task("R", "Schlag „Fernweh“ nur im Duden nach.", "x", {}, [], {})
    p = FakePlanner2(plan2(("launch_app", "Dictionary"), ("scoped_find", "Dictionary", "", "Fernweh")))
    rec = X.Rec("R", "arch", 1)
    arch2(g, p).run(t, rec)
    assert p.calls == 2 and rec.actions_executed == 0 and "restriction not carried" in rec.stopped
    assert not [op for op, _a in g.log if op in ("press", "enter", "select", "launch")]


def test_search_before_open_is_dropped_and_the_picker_opens_directly():
    g = T3.FakeGuest()
    t = X.task_by_id("T2")
    p = FakePlanner2(plan2(("launch_app", "Finder"), ("navigate", "Finder", "test folder"),
                           ("scoped_find", "Finder", "", "March"), ("open_item", "Finder", "file from March")))
    ch = T3.FakeChooser(["Testordner", "Besprechung"])
    out = Y.run_one2(t, "arch", 1, g, arch=arch2(g, p, ch), emap=EMAP2)
    assert out["judge"]["success"], out["steps"]
    assert not [s for s in out["steps"] if s["step"]["effect"] == "scoped_find"]
    pressed = [a["trigger"]["identifier"] for op, a in g.log if op == "press"]
    assert pressed == [T3.OPEN["trigger"]["identifier"]]


def test_a_kind_word_in_a_navigate_target_needs_no_picker():
    g = T3.FakeGuest()
    g.notes_folder, g.shown_note = X.NOTES_START, "Startnotiz"
    t = X.task_by_id("T5")
    p = FakePlanner2(plan2(("launch_app", "Notes"), ("navigate", "Notes", "folder AnyUse-Pick"),
                           ("open_item", "Notes", "note about the plumber")))
    ch = T3.FakeChooser("Handwerker")
    out = Y.run_one2(t, "arch", 1, g, arch=arch2(g, p, ch), emap=EMAP2)
    nav = [s for s in out["steps"] if s["step"]["effect"] == "navigate"][0]
    assert nav["resolution"]["by"] == "map" and "without kind words" in nav["resolution"]["why"]
    assert out["judge"]["success"] and ch.calls == 2          # the open's pick only


def test_compiled_replay_of_a_prior_success_makes_zero_model_calls():
    g = CoupledGuest()
    g.front = T3.DCM
    first = Y.run_one2(E8, "arch", 1, g, arch=arch2(g, FakePlanner2(E8_PLAN)), emap=EMAP2)
    assert first["judge"]["success"]
    g2 = CoupledGuest()
    g2.front = T3.DCM
    rep = Y.run_one2(E8, "arch", 1, g2, arch=arch2(g2, None), compiled=first["compiled"], mode="compiled",
                     emap=EMAP2)
    assert rep["judge"]["success"] and rep["model_calls"] == 0


def test_the_budget_reserves_the_v2_planners_worst_case():
    led = X.Ledger(cap=X.WORST["planner2"] * 1.5)
    g = T3.FakeGuest()
    a = arch2(g, FakePlanner2(plan2(("refuse", "none", "x", "outside_apps"))), spend=led)
    rec = X.Rec("x", "arch", 1)
    a.plan(rec, "Pay my bill.")
    a.plan(rec, "Pay my bill.")
    try:
        a.plan(rec, "Pay my bill.")
        assert False, "the cap should stop the third call"
    except X.RunStop:
        pass


# ── the Arch2 tasks ─────────────────────────────────────────────────────────

def test_prior_tasks_are_fresh_local_and_their_coupling_comes_from_the_map():
    old = {X.norm(t.text) for t in X.TASKS + X.DEV_TASKS}
    ids = [t.id for t in Y.TASKS2]
    assert ids == [f"E{i}" for i in range(1, 12)]
    kinds = {t.kind for t in Y.TASKS2}
    assert {"restriction", "coupled", "gate", "refuse"} <= kinds
    for t in Y.TASKS2 + Y.DEV2_TASKS:
        assert X.norm(t.text) not in old
        assert t.start["app"] in X.MANAGED
        assert "coupled" not in t.expect                       # never hand-written in TASKS2
        for p in t.read:
            if p["kind"] == "folder":
                assert p["path"].startswith(Y.GEN2)
        if t.start["app"] == "com.apple.Dictionary":
            assert t.start["dictionary"]["source"] not in ("Wikipedia", "Alle Lexika")
            assert t.expect["group"][0]["selected"] not in ("Wikipedia", "Alle Lexika")
    assert {t.id for t in Y.TASKS2} & {t.id for t in Y.DEV2_TASKS} == set()
    assert Y.expected_coupled(Y.task2_by_id("E8").expect, EMAP2) == \
        ["Darstellung > X-Koordinate fixieren", "Darstellung > Y-Koordinate fixieren"]
    assert Y.expected_coupled(Y.task2_by_id("E10").expect, EMAP2) == ["Darstellung > Sortieren nach > "]
    # restriction tasks carry a restriction the reader finds
    for t in Y.TASKS2:
        assert bool(Y.restrictions(t.text)) == (t.kind == "restriction"), t.id


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            import traceback
            f += 1; print("FAIL", n, "::", repr(e)[:400]); traceback.print_exc(limit=3)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
