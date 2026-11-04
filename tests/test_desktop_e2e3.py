"""Arch3, offline, with the
FAKE guest and fake models.

What is pinned here: the map check resolves a scope and every planned
toggle_select / navigate to MAP IDENTITIES before it decides whether the plan
already applies the scope, and de-duplicates by identity, not by wording
(in E2 "Oxford-Wörterbuch" was applied a second time
next to the planned "Oxford German Dictionary", by the picker, in all six
architecture runs; offline: a second navigate "Tuesday"). Arch2's other
rules are unchanged.

No desktop, no AX, no subprocess, no network.
"""
import json
from collections import Counter

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e2 as Y
from anyuse.execution import desktop_e2e3 as Z

import test_desktop_e2e as T3
import test_desktop_e2e2 as T2

EMAP2 = Y.EffectMap2()
S = T2.S
DICT = "com.apple.Dictionary"
SRC = next(e for e in EMAP2.entries("Dictionary", "toggle_select"))
OXFORD = "Suchen > Oxford German Dictionary"

#: the recorded E2 plan (every architecture run planned this shape)
E2_GOAL = "Schlag „Zeitgeist“ nur im Oxford-Wörterbuch nach."
E2_PLAN = [S("launch_app", "Dictionary"),
           S("toggle_select", "Dictionary", "Suchen", "Oxford German Dictionary", "Oxford-Wörterbuch"),
           S("scoped_find", "Dictionary", "", "Zeitgeist", "Oxford-Wörterbuch")]


def _eff(ref):
    return [(s["effect"], s["value"] or s["target"]) for s in ref.steps]


# ── identities ──────────────────────────────────────────────────────────────

def test_scopes_and_steps_resolve_to_map_identities():
    oj = Z.scope_identity(EMAP2, "Dictionary", "Oxford-Wörterbuch")
    assert oj.kind == "option" and oj.keys == {OXFORD} and oj.how == "bare" and oj.unique
    assert Z.step_identity(EMAP2, S("toggle_select", "Dictionary", "Suchen", "Oxford German Dictionary")).keys == {OXFORD}
    assert Z.covers(Z.step_identity(EMAP2, E2_PLAN[1]), oj)
    assert Z.scope_identity(EMAP2, "Dictionary", "Duden").how == "label"
    # a location: the name's words without kind or stop words
    assert Z.scope_identity(EMAP2, "Notes", "the AnyUse-E2 folder") == Z.scope_identity(EMAP2, "Notes", "AnyUse-E2")
    assert Z.scope_identity(EMAP2, "Notes", "AnyUse-E2").kind == "location"
    # a word shared with several options: the picker's candidates, never map-decided
    many = Z.scope_identity(EMAP2, "Dictionary", "dictionary")
    assert many.kind == "option" and len(many.keys) > 1 and not many.unique
    # nothing to resolve to
    assert Z.scope_identity(EMAP2, "TextEdit", "open document").kind == "none"


# ── the regression: E2 (Oxford) ─────────────────────────────────────────────

def test_oxford_scope_already_applied_by_the_planned_toggle_is_not_applied_twice():
    old = Y.refine_plan(E2_GOAL, E2_PLAN, EMAP2)                 # Arch2: a second, worded toggle
    assert [s["effect"] for s in old.steps].count("toggle_select") == 2
    new = Z.refine_plan3(E2_GOAL, E2_PLAN, EMAP2)
    assert _eff(new) == [("launch_app", ""), ("toggle_select", "Oxford German Dictionary"),
                         ("scoped_find", "Zeitgeist")], new.changes
    assert not new.problems
    assert not [c for c in new.changes if "applied as" in c]
    assert any("already applied" in c and "same map identity" in c for c in new.changes)


class DictGuest(T3.FakeGuest):
    """``FakeGuest`` plus the Dictionary: the Suchen source group and a search field."""

    def __init__(self, source="Apple Dictionary"):
        super().__init__()
        self.front = DICT
        self.src = {o["key"]: o["label"] == source for o in SRC["options"]}
        self.search = ""

    def _top(self, bundle):
        return self.src if bundle == DICT else super()._top(bundle)

    def op_press(self, bundle, trigger, label):
        if bundle == DICT:
            for o in SRC["options"]:
                if o["trigger"]["key"] == trigger.get("key"):        # the items share an identifier
                    for k in self.src:
                        self.src[k] = k == o["key"]
                    return {"status": "done"}
            return {"status": "refused", "detail": "unresolved"}
        return super().op_press(bundle, trigger, label)

    def op_search_fields(self, bundle):
        return {"status": "done", "fields": [{"index": 0, "text": "Suchen", "value": self.search}]}

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if bundle == DICT and "search_field" in target:
            self.search = text
            return {"status": "done"}
        return super().op_enter(bundle, target, text, commit, keys)

    def op_read_state(self, read):
        out = []
        for p in read:
            if p["kind"] == "dictionary":
                out.append({"ok": True, "search": self.search})
            elif p["kind"] == "marks" and p["bundle"] == DICT:
                out.append({"ok": True, "items": [{"path": o["path"], "mark": self.src[o["key"]]}
                                                  for o in SRC["options"]]})
            else:
                out += super().op_read_state([p])["read"]
        return {"status": "done", "read": out}


def test_oxford_end_to_end_needs_no_picker_and_presses_once():
    t = Y.task2_by_id("E2")
    g = DictGuest("Apple Dictionary")
    ch = T3.FakeChooser("Oxford")
    a = Z.Arch3(g, EMAP2, planner=T2.FakePlanner2(T2.plan2(*[tuple(s.values()) for s in E2_PLAN])), chooser=ch,
                sleep=lambda s: None)
    out = Y.run_one2(t, "arch", 1, g, arch=a, emap=EMAP2)
    assert out["judge"]["success"], (out["judge"], out["steps"])
    assert ch.calls == 0 and out["calls"]["picker"] == 0
    assert [s["step"]["effect"] for s in out["steps"]] == ["launch_app", "toggle_select", "scoped_find"]
    assert [a_["trigger"]["key"] for op, a_ in g.log if op == "press"] == \
        [next(o for o in SRC["options"] if o["path"] == OXFORD)["trigger"]["key"]]
    # Arch2 on the same plan: the extra toggle goes to the picker
    g2 = DictGuest("Apple Dictionary")
    ch2 = T3.FakeChooser("Oxford")
    a2 = T2.arch2(g2, T2.FakePlanner2(T2.plan2(*[tuple(s.values()) for s in E2_PLAN])), ch2)
    out2 = Y.run_one2(t, "arch", 1, g2, arch=a2, emap=EMAP2)
    assert ch2.calls == 2 and out2["judge"]["success"]


# ── insertion and de-duplication by identity ─────────────────────────────────

def test_an_unapplied_scope_is_inserted_as_the_option_it_names():
    r = Z.refine_plan3(E2_GOAL, [S("launch_app", "Dictionary"),
                                 S("scoped_find", "Dictionary", "", "Zeitgeist", "Oxford-Wörterbuch")], EMAP2)
    assert _eff(r) == [("launch_app", ""), ("toggle_select", "Oxford German Dictionary"), ("scoped_find", "Zeitgeist")]
    assert r.steps[1]["target"] == "Suchen"                       # map-decided, no picker
    assert EMAP2.resolve_toggle("Dictionary", r.steps[1]["target"], r.steps[1]["value"]).by == "map"


def test_differently_worded_scopes_of_one_identity_are_applied_once():
    r = Z.refine_plan3("Look up 'Tor' and 'Zeit', only in the Oxford dictionary.",
                       [S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "Tor", "Oxford-Wörterbuch"),
                        S("scoped_find", "Dictionary", "", "Zeit", "Oxford")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["launch_app", "toggle_select", "scoped_find", "scoped_find"]
    # a planned duplicate of an option already in force is dropped
    r = Z.refine_plan3("x", [S("toggle_select", "Dictionary", "Suchen", "Oxford German Dictionary"),
                             S("toggle_select", "Dictionary", "", "Oxford"),
                             S("scoped_find", "Dictionary", "", "w")], EMAP2)
    assert [s["effect"] for s in r.steps] == ["toggle_select", "scoped_find"]
    assert any("same map option" in c for c in r.changes)
    # ... but not when another option of the group was chosen in between
    st = [S("toggle_select", "Dictionary", "Suchen", "Oxford German Dictionary"),
          S("toggle_select", "Dictionary", "Suchen", "Duden"), S("toggle_select", "Dictionary", "Suchen", "Oxford")]
    assert len(Z.refine_plan3("x", st, EMAP2).steps) == 3


def test_a_planned_step_of_another_option_does_not_cover_the_scope():
    r = Z.refine_plan3(E2_GOAL, [S("launch_app", "Dictionary"),
                                 S("toggle_select", "Dictionary", "Suchen", "Apple Dictionary"),
                                 S("scoped_find", "Dictionary", "", "Zeitgeist", "Oxford-Wörterbuch")], EMAP2)
    assert _eff(r)[2:] == [("toggle_select", "Oxford German Dictionary"), ("scoped_find", "Zeitgeist")]
    # a toggle whose own scope names another option of its group contradicts itself: not executed
    r = Z.refine_plan3(E2_GOAL, [S("toggle_select", "Dictionary", "Suchen", "Apple Dictionary", "Oxford-Wörterbuch"),
                                 S("scoped_find", "Dictionary", "", "Zeitgeist")], EMAP2)
    assert r.problems and "contradicts" in r.problems[0]


def test_a_location_scope_is_covered_by_a_navigate_that_names_the_place():
    g = "Open the workshop note, but only from the AnyUse-E2 folder."
    st = [S("launch_app", "Notes"), S("navigate", "Notes", "AnyUse-E2"),
          S("open_item", "Notes", "workshop note", "", "the AnyUse-E2 folder")]
    old = Y.refine_plan(g, st, EMAP2)
    assert [s["effect"] for s in old.steps].count("navigate") == 2      # Arch2: worded twice
    assert [s["effect"] for s in Z.refine_plan3(g, st, EMAP2).steps] == ["launch_app", "navigate", "open_item"]
    # offline: the navigate's value names the scope's place
    st = [S("launch_app", "Calendar"), S("navigate", "Calendar", "date", "Tuesday", "Tuesday"),
          S("select_item", "Calendar", "dentist appointment", "", "Tuesday"),
          S("human_gate", "Calendar", "dentist appointment", "delete", "Tuesday")]
    r = Z.refine_plan3("Delete my dentist appointment on Tuesday, only on Tuesday.", st, EMAP2)
    assert [s["effect"] for s in r.steps].count("navigate") == 1 and not r.problems
    # a planned duplicate navigate right after the same place is dropped; one after a step in the app is kept
    st = [S("navigate", "Notes", "AnyUse-E2"), S("navigate", "Notes", "folder AnyUse-E2"), S("open_item", "Notes", "x")]
    assert [s["effect"] for s in Z.refine_plan3("x", st, EMAP2).steps] == ["navigate", "open_item"]
    st = [S("navigate", "Finder", "Projekt"), S("open_item", "Finder", "Archiv"), S("navigate", "Finder", "Projekt"),
          S("open_item", "Finder", "x")]
    assert [s["effect"] for s in Z.refine_plan3("x", st, EMAP2).steps].count("navigate") == 2


def test_prior_rules_are_unchanged_where_wording_and_identity_agree():
    cases = [
        ("Schlag „Fernweh“ nur im Duden nach.", [S("launch_app", "Dictionary"),
                                                 S("scoped_find", "Dictionary", "", "Fernweh", "Duden")]),
        ("Open the workshop note, but only from the AnyUse-E2 folder.",
         [S("launch_app", "Notes"), S("open_item", "Notes", "workshop note", "", "AnyUse-E2")]),
        ("Open the file from March in the Projekt folder.",
         [S("launch_app", "Finder"), S("navigate", "Finder", "Projekt", "", "Projekt"),
          S("scoped_find", "Finder", "", "March", "Projekt"), S("open_item", "Finder", "file from March", "", "Projekt")]),
        ("Open the cost plan in the Projekt folder, write 'ok' into it and save it.",
         [S("launch_app", "Finder"), S("navigate", "Finder", "Projekt", "", "Projekt"),
          S("open_item", "Finder", "cost plan", "", "Projekt"), S("doc_save", "TextEdit", "", "ok", "Projekt")]),
        ("Save it only in the Projekt folder.", [S("doc_save", "TextEdit", "", "ok", "Projekt")]),
        ("Search 'Budget' only in the open document.", [S("scoped_find", "TextEdit", "", "Budget", "open document")]),
        ("Search the system log for 'bluetooth'.",
         [S("launch_app", "Console"), S("scoped_find", "Console", "", "bluetooth", "Systemprotokoll")]),
        ("Save the document with the text 'Stand 3'.",
         [S("launch_app", "TextEdit"), S("field_rw", "TextEdit", "document text", "Stand 3"),
          S("doc_save", "TextEdit", "", "")]),
        ("Sort Downloads by name, then trash the draft.",
         [S("launch_app", "Finder"), S("navigate", "Finder", "Downloads"), S("toggle_select", "Finder", "sort", "Name"),
          S("select_item", "Finder", "draft"), S("human_gate", "Finder", "draft", "delete")]),
        ("Colour meter to 4x.", [S("launch_app", "Notes"), S("navigate", "Notes", "Rezepte"),
                                 S("toggle_select", "Notes", "Darstellung", "Als Galerie")]),
    ]
    for goal, st in cases:
        a, b = Y.refine_plan(goal, st, EMAP2), Z.refine_plan3(goal, st, EMAP2)
        assert a.steps == b.steps and bool(a.problems) == bool(b.problems), (goal, a.to_dict(), b.to_dict())


# ── Arch3's tasks (independent authors), judge and host ─────────────────────

DATA3 = Z.load_tasks3()


def test_prior_tasks_are_the_draw_and_build_from_the_authors_data():
    drawn = Z.tasks3(DATA3, drawn=True)
    assert [t.id for t in drawn] == DATA3["drawn"] and len(drawn) == 15
    dev = Z.tasks3(DATA3, drawn=False)
    assert dev and not ({t.id for t in dev} & {t.id for t in drawn})
    for t in drawn + dev:
        a = next(x["author_task"] for x in DATA3["tasks"] if x["author_task"]["id"] == t.id)
        assert t.text == a["utterance"]                      # verbatim
        assert t.start["app"] in X.MANAGED
        for p in t.read:
            if p["kind"] == "folder":
                assert p["path"].startswith(Z.GEN3 + "/" + t.id + "/")
        if "dictionary" in t.start:
            assert t.start["dictionary"]["source"] not in ("Wikipedia", "Alle Lexika")
        for g in t.expect.get("group") or []:
            assert g["selected"] not in ("Wikipedia", "Alle Lexika")
        assert ("te_prefs" in t.start) == ("TextEdit" in a["apps"])
        for path in t.expect.get("file_text") or {}:
            assert path in Z.statements3(t.id, DATA3)             # the reader gets the author's own words
    # declared smart quotes/dashes must show in a plain-text document
    plain = DATA3["te_prefs_plain_text"]
    assert Z.task3_by_id("S10", DATA3).start["te_prefs"][plain] is False
    assert Z.task3_by_id("G07", DATA3).start["te_prefs"][plain] is True          # nothing declared: the guest's
    assert Z.task3_by_id("G10", DATA3).start["te_prefs"] == DATA3["te_prefs_baseline"]   # open doc: its own marks
    kinds = Counter(t.kind for t in drawn)
    assert kinds["gate"] + kinds["refuse"] >= 2
    assert X.tasks_sha256(drawn) == Z.tasks3_sha256(DATA3)


def test_seeds3_holds_every_task_notes_folder_once():
    spec = Z.seeds3_spec(DATA3)["notes"]
    for t in DATA3["tasks"]:
        for f in t["author_task"]["start"].get("notes_folders") or []:
            assert all([n["title"], n["text"]] in spec[f["name"]] for n in f["notes"])
    # two tasks naming the same folder share it (the union of their notes)
    d = json.loads(json.dumps(DATA3))
    a, b = d["tasks"][0]["author_task"], d["tasks"][1]["author_task"]
    a["start"]["notes_folders"] = [{"name": "Gemeinsam", "notes": [{"title": "A", "text": "a"}]}]
    b["start"]["notes_folders"] = [{"name": "Gemeinsam", "notes": [{"title": "B", "text": "b"}]}]
    assert Z.seeds3_spec(d)["notes"]["Gemeinsam"] == [["A", "a"], ["B", "b"]]


def _te_items(**marks):
    return {"ok": True, "items": [{"path": Z.TE_MENU[k], "mark": v} for k, v in marks.items()]}


def test_judge_reads_a_declared_start_for_an_app_not_running_at_the_start():
    t = Z.task3_by_id("S10", DATA3)
    decl = {Z.TE_MENU[k]: v for k, v in DATA3["tasks"][[x["author_task"]["id"] for x in DATA3["tasks"]].index("S10")]
            ["author_task"]["start"]["textedit_settings"].items()}
    auto = "Rechtschreibung autom. korrigieren"
    names = {k: decl[Z.TE_MENU[k]] for k in Z.TE_MENU}
    folder = next(p for p in t.read if p["kind"] == "folder")
    ents = {"ok": True, "entries": [{"name": f["name"], "type": "file", "sha256": f["name"], "text": f["text"]}
                                    for f in t.start["finder"]["files"]]}
    start, end = [], []
    for p in t.read:
        if p["kind"] == "folder":
            start.append(ents), end.append(ents)
        elif p["kind"] == "front":
            start.append({"ok": True, "documents": []})
            end.append({"ok": True, "documents": ["/Users/g/" + t.expect["opened"]["path"][2:]]})
        else:                                             # TextEdit marks: not running at the start
            start.append({"ok": False, "why": "not running"})
            end.append(_te_items(**dict(names, **{auto: False})))
    j = Z.judge(t, start, end, {"actions_executed": 3}, EMAP2)
    assert j["success"], j
    # another declared setting changed: unexpected
    end2 = json.loads(json.dumps(end))
    end2[[p["kind"] for p in t.read].index("marks")] = _te_items(**dict(names, **{auto: False,
                                                                                 "Intelligente Links": False}))
    j = Z.judge(t, start, end2, {"actions_executed": 3}, EMAP2)
    assert not j["success"] and any("Intelligente Links" in u for u in j["unexpected"])


def test_judge_asks_the_reader_only_for_a_changed_text_that_is_not_exact():
    t = Z.task3_by_id("G11", DATA3)
    path, want = next(iter(t.expect["file_text"].items()))
    name = path.rsplit("/", 1)[1]

    def reads(txt):
        ents = [{"name": f["name"], "type": "file", "sha256": f["text"] if f["name"] != name else txt,
                 "text": f["text"] if f["name"] != name else txt} for f in t.start["finder"]["files"]]
        return [{"ok": True, "entries": ents} if p["kind"] == "folder" else
                {"ok": True, "documents": ["/Users/g/" + t.expect["opened"]["path"][2:]]} for p in t.read]
    start = [{"ok": True, "entries": [{"name": f["name"], "type": "file", "sha256": f["text"], "text": f["text"]}
                                      for f in t.start["finder"]["files"]]} if p["kind"] == "folder"
             else {"ok": True, "documents": []} for p in t.read]
    calls = []

    def reader(task_text, statement, before, after):
        calls.append((statement, before, after))
        return after.endswith("Lamp ordered.\n") or after.endswith("Lamp ordered. "), "ok", {}
    assert Z.judge(t, start, reads(want + "\n"), {}, EMAP2, reader, Z.statements3("G11", DATA3))["success"]
    assert calls == []                                       # exact: no reader call
    j = Z.judge(t, start, reads(want + " "), {}, EMAP2, reader, Z.statements3("G11", DATA3))
    assert j["success"] and calls and calls[0][0] == Z.statements3("G11", DATA3)[path]
    assert j["reader"][0]["meets"] and [i for i in j["items"] if i.get("by") == "reader"]
    j = Z.judge(t, start, reads("Lamp ordered."), {}, EMAP2, reader, Z.statements3("G11", DATA3))
    assert not j["success"] and j["wrong_actions"] >= 1 and not j["reader"][0]["meets"]
    # no reader injected: the exact rule stands
    assert not Z.judge(t, start, reads(want + " "), {}, EMAP2)["success"]


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
