"""A descriptive scope resolves to exactly one recorded container, or the caller is asked (``desktop_fixes`` item 4),
offline with fakes.

Arch4's location check refused descriptions: GG05 A (the plan's scope 'Angebote für draußen' while
the run had entered 'Außenflächen' FOR that very description: "the container shown is 'Außenflächen', the plan
names 'Angebote für draußen'") and SG09 A / E (the plan named the document 'aktuellen': "the front document is
'Probenplan.txt', the plan names the document 'aktuellen'"). Pinned here, in the recorded worlds:

* GG05 A: the description was resolved by the run's own navigate: the folder is created there and sorted;
* SG09 A: the open step resolved 'aktuellen' (its scope) to Probenplan.txt: the date is changed there;
* SG09 E: no step resolved 'aktuellen': nothing is written, the run hands off asking the caller (never a refusal,
  never a guess);
* still refused: a scope that NAMES another container or document this run saw; a description this run resolved
  to another container; a description resolved to two is asked, not guessed;
* the frozen Arch12 without the layer still refuses (its bytes are pinned);
* a routine's replay: the description resolves through the replay's own navigate to the variant's folder.

No desktop, no AX, no subprocess, no network.
"""
import sys

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z
from anyuse.execution import desktop_fixes as F2
from anyuse.execution import desktop_routine as R

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
ROOT = T4.ROOT
Arch12F = F2.with_desktop_fixes(Z.Arch12)

# the pool's GG05
GG05_GOAL = ("Geh mal in die Angebote für draußen, nicht zu den Räumen. Da brauch ich einen leeren Ordner namens "
             "Rückfragen. Lass die Liste dort nach Änderungsdatum sortiert.")
GG05_TREE = {"Angebote Herbstmarkt/Übersicht.txt": "Angebote getrennt nach Außenflächen und Innenräumen.",
             "Angebote Herbstmarkt/Außenflächen/Hof.txt": "Hof\nTagespreis: 240 Euro",
             "Angebote Herbstmarkt/Außenflächen/Garten.txt": "Garten\nTagespreis: 280 Euro",
             "Angebote Herbstmarkt/Innenräume/Saal.txt": "Saal\nTagespreis: 320 Euro"}
DESC = "Angebote für draußen"
# GG05's recorded arm-A plan, verbatim
GG05_PLAN = plan4(S("launch_app", "Finder"), S("navigate", "Finder", DESC),
                  S("object_create", "Finder", "folder", "Rückfragen", sc=DESC),
                  S("toggle_select", "Finder", "Sortieren nach", "Änderungsdatum", sc=DESC))
SG09_GOAL = ("Ändere im Probenplan den Sopran-Termin vom 3. Oktober auf den 10. Oktober, aber nur im aktuellen, nicht "
             "im Entwurf.")
SG09_TREE = {"Chor Probenplanung/Probenplan.txt": "Sopran: 3. Oktober, 19:00\nBass: 5. Oktober, 19:30\nKonzert: 3. Dezember",
             "Chor Probenplanung/Probenplan_Entwurf.txt":
                 "Sopran: 3. Oktober, 18:30\nBass: 4. Oktober, 19:30\nKonzert: 3. Dezember"}
SG09_DONE = "Sopran: 10. Oktober, 19:00\nBass: 5. Oktober, 19:30\nKonzert: 3. Dezember"
SG09_REST = plan4(S("extract", "none", "the old Sopran date", "old_date", find="3. Oktober"),
                  S("doc_save", "TextEdit", "", "10. Oktober", sc="aktuellen", mode="edit", find="{{old_date}}"))


def _arch(g, planner, want, cls=Arch12F):
    return cls(g, EMAP, planner=planner, config11=T11.cfg(), jchooser=T11.ConfOracle(want, 0.95), spend=None,
               inbox=None, sleep=lambda s: None, precondition13=True)


def _run(cls, tree, shown, goal, plans, want):
    g = T9.FakeGuest9(tree, shown=shown)
    rec = X.Rec("T", cls.__name__, 1)
    _arch(g, FakePlanner4(*plans), want, cls).run(X.Task("T", goal, "single-app", {}, [], {}), rec)
    return g, rec


def test_gg05_a_the_description_resolved_by_the_runs_navigate_is_the_container_shown():
    g, rec = _run(Arch12F, GG05_TREE, "Angebote Herbstmarkt", GG05_GOAL, [GG05_PLAN], "Hof.txt")
    assert rec.status == "completed", (rec.stopped, [s.get("why") for s in rec.steps])
    assert g.isdir(f"{ROOT}/Angebote Herbstmarkt/Außenflächen/Rückfragen")
    assert not g.isdir(f"{ROOT}/Angebote Herbstmarkt/Innenräume/Rückfragen")
    oc = next(s for s in rec.steps if s["step"]["effect"] == "object_create")
    assert "was resolved in this run to 'Außenflächen'" in oc["location"] and rec.location_refusals == 0
    assert [k for k, v in g.sort.items() if v] == [o["key"] for o in T4.SORT["options"] if o["label"] == "Änderungsdatum"]
    # the frozen Arch12, without the layer: still refused, nothing created
    g0, rec0 = _run(Z.Arch12, GG05_TREE, "Angebote Herbstmarkt", GG05_GOAL, [GG05_PLAN], "Hof.txt")
    assert rec0.stopped == "refused (location check)" and not g0.isdir(f"{ROOT}/Angebote Herbstmarkt/Außenflächen/Rückfragen")


def test_sg09_a_the_open_steps_scope_resolved_the_document():
    first = plan4(S("launch_app", "Finder"), S("open_item", "Finder", "Probenplan", sc="aktuellen"),
                  S("read_content", "TextEdit", "document", "text"))
    g, rec = _run(Arch12F, SG09_TREE, "Chor Probenplanung", SG09_GOAL, [first, SG09_REST], "Probenplan.txt")
    assert rec.status == "completed", (rec.stopped, [s.get("why") for s in rec.steps])
    assert g.fs[f"{ROOT}/Chor Probenplanung/Probenplan.txt"] == SG09_DONE
    assert g.fs[f"{ROOT}/Chor Probenplanung/Probenplan_Entwurf.txt"] == SG09_TREE["Chor Probenplanung/Probenplan_Entwurf.txt"]
    g0, rec0 = _run(Z.Arch12, SG09_TREE, "Chor Probenplanung", SG09_GOAL, [first, SG09_REST], "Probenplan.txt")
    assert rec0.stopped == "refused (location check)"
    assert g0.fs[f"{ROOT}/Chor Probenplanung/Probenplan.txt"] == SG09_TREE["Chor Probenplanung/Probenplan.txt"]


def test_sg09_e_a_description_no_step_resolved_asks_the_caller_and_writes_nothing():
    # the recorded arm-E plan: the open names no scope, the write carries 'aktuellen'
    first = plan4(S("launch_app", "Finder"), S("open_item", "Finder", "Probenplan"),
                  S("read_content", "TextEdit", "document", "text"),
                  S("doc_save", "TextEdit", "", "10. Oktober", sc="aktuellen", mode="edit", find="3. Oktober"))
    g, rec = _run(Arch12F, SG09_TREE, "Chor Probenplanung", SG09_GOAL, [first, SG09_REST], "Probenplan.txt")
    assert rec.stopped.startswith(F2.ASK_STOP + ": the plan's scope 'aktuellen' describes a document")
    assert "no step of this run resolved it" in rec.stopped and rec.location_refusals == 0
    assert g.fs[f"{ROOT}/Chor Probenplanung/Probenplan.txt"] == SG09_TREE["Chor Probenplanung/Probenplan.txt"]
    assert not any(op == "enter" for op, _a in g.log)


def test_a_name_of_another_container_or_document_is_still_refused():
    plan = plan4(S("launch_app", "Finder"), S("navigate", "Finder", "Außenflächen"),
                 S("object_create", "Finder", "folder", "Rückfragen", sc="Innenräume"))
    g, rec = _run(Arch12F, GG05_TREE, "Angebote Herbstmarkt", "Leg den Ordner Rückfragen nur in Innenräume an.",
                  [plan], "x")
    assert rec.stopped == "refused (location check)" and not g.isdir(f"{ROOT}/Angebote Herbstmarkt/Außenflächen/Rückfragen")
    first = plan4(S("launch_app", "Finder"), S("open_item", "Finder", "Probenplan.txt"),
                  S("read_content", "TextEdit", "document", "text"),
                  S("doc_save", "TextEdit", "", "10. Oktober", sc="Entwurf", mode="edit", find="3. Oktober"))
    rest = plan4(S("extract", "none", "the old Sopran date", "old_date", find="3. Oktober"),
                 S("doc_save", "TextEdit", "", "10. Oktober", sc="Entwurf", mode="edit", find="{{old_date}}"))
    g, rec = _run(Arch12F, SG09_TREE, "Chor Probenplanung", "Ändere nur im Entwurf den Sopran-Termin vom 3. Oktober "
                  "auf den 10. Oktober.", [first, rest], "Probenplan.txt")
    assert rec.stopped == "refused (location check)" and "names the document 'Entwurf'" in rec.steps[-1]["why"]


def test_a_description_resolved_elsewhere_is_refused_and_one_resolved_twice_is_asked():
    a = Arch12F(T9.FakeGuest9(GG05_TREE, shown="Angebote Herbstmarkt/Innenräume"), EMAP, planner=None,
                config11=T11.cfg(), jchooser=T11.ConfOracle("x"), spend=None, inbox=None, sleep=lambda s: None)
    a._reset2()
    a._seen2.update({"Außenflächen", "Innenräume"})
    a._resolve2("container", DESC, "Außenflächen")
    log = {}
    why = a._location_check(X.Rec("T", "a", 1), S("object_create", "Finder", "folder", "R", sc=DESC), log,
                            T4.FINDER)
    assert why == f"the container shown is 'Innenräume'; the plan's {DESC!r} was resolved in this run to 'Außenflächen'"
    a._resolve2("container", DESC, "Innenräume")
    why = a._location_check(X.Rec("T", "a", 1), S("object_create", "Finder", "folder", "R", sc=DESC), {}, T4.FINDER)
    assert isinstance(why, F2.Ask) and "2 different ones" in why


def test_a_routines_replay_resolves_the_description_through_its_own_navigate():
    g, rec = _run(Arch12F, GG05_TREE, "Angebote Herbstmarkt", GG05_GOAL, [GG05_PLAN], "Hof.txt")
    rt = R.derive_routine(rec.compiled, GG05_GOAL, task="GG05", arm="arch12f")
    ps = {p["name"]: p for p in rt["params"]}
    assert ps["folder"]["value"] == "Außenflächen"
    g2 = T9.FakeGuest9(GG05_TREE, shown="Angebote Herbstmarkt")
    a = R.ReplayArch(g2, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    steps, goal, probs = a.prepare(rt, {"folder": "Innenräume"})
    rec2 = X.Rec("GG05", "replay", 1, mode="compiled")
    a.run(X.Task("GG05", goal, "single-app", {}, [], {}), rec2, compiled=steps)
    assert rec2.status == "completed", (rec2.stopped, [s.get("why") for s in rec2.steps])
    assert g2.isdir(f"{ROOT}/Angebote Herbstmarkt/Innenräume/Rückfragen") and sum(rec2.calls.values()) == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
