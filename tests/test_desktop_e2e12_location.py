"""Arch12, offline with fakes: a location the user excludes is a hard
pre-action check.

Arch11 on GF07: "Inside the current bids, make an empty folder called Review ... Don't put it beside the current
and archived folders." The plan created "Review" in the folder shown (Courtyard Bids), beside Current and Archived:
a real wrong action with residual damage.

Pinned here: the user's location exclusions ("don't put it beside X", "not in X", "nicht in den Ordner X", "nicht
neben X") are parsed (EN, DE); before ``object_create`` the folder shown and its entries are observed and checked;
a violation creates NOTHING and asks the planner once more with the observation (the run's one re-plan); a second
violation refuses (a location refusal). A goal without an exclusion is unchanged. No desktop, no AX, no
subprocess, no network.
"""
from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, ROOT = T4.FINDER, T4.ROOT
cfg = T11.cfg

GF07 = ("Inside the current bids, make an empty folder called Review and leave that list sorted by kind. Don't put it "
        "beside the current and archived folders.")
TREE = {"B/Courtyard Bids/readme.txt": "Current contains active bids. Archived contains closed bids.",
        "B/Courtyard Bids/Current/paving.txt": "Paving bid\nStatus: awaiting review",
        "B/Courtyard Bids/Current/lighting.txt": "Lighting bid\nStatus: awaiting review",
        "B/Courtyard Bids/Archived/paving-previous.txt": "Previous paving bid\nStatus: closed"}
BAD = plan4(S("launch_app", "Finder"), S("object_create", "Finder", "folder", "Review"),
            S("toggle_select", "Finder", "Sortieren nach", "Art"))
GOOD = plan4(S("launch_app", "Finder"), S("navigate", "Finder", "Current"), S("object_create", "Finder", "folder", "Review"),
             S("toggle_select", "Finder", "Sortieren nach", "Art"))


def test_the_exclusions_are_parsed_in_english_and_german():
    ex = Z.location_exclusions12(GF07)
    assert [(e["rel"], e["words"]) for e in ex] == [("beside", ["current", "archived"])]
    assert [(e["rel"], e["words"]) for e in Z.location_exclusions12("Leg den Ordner an, aber nicht in den Ordner "
                                                                     "Archiv.")] == [("in", ["archiv"])]
    assert Z.location_exclusions12("Bitte nicht neben die Rechnungen legen.")[0]["rel"] == "beside"
    assert Z.location_exclusions12("Make a folder Review in Current.") == []
    assert Z.location_exclusions12("I mean the one whose text says it was replaced, not just whichever file looks "
                                   "older.") == []


def test_the_target_location_is_checked_by_its_name_and_its_entries():
    ex = Z.location_exclusions12(GF07)
    assert Z.location_problems12(ex, "Courtyard Bids", ["Archived", "Current", "readme.txt"])
    assert Z.location_problems12(ex, "Current", ["lighting.txt", "paving.txt"]) == []
    ex2 = Z.location_exclusions12("Create the folder, but not in the Archive folder.")
    assert Z.location_problems12(ex2, "Archive", []) and Z.location_problems12(ex2, "Current", ["Archive"]) == []


def arch12(guest, planner):
    return Z.Arch12(guest, EMAP, planner=planner, config11=cfg(), jchooser=T11.ConfOracle("Art", 0.99), spend=None,
                    inbox=None, sleep=lambda s: None)


def test_gf07_like_nothing_is_created_beside_the_excluded_folders_and_the_replan_creates_it_inside():
    g = T9.FakeGuest9(dict(TREE), shown="B/Courtyard Bids")
    p = FakePlanner4(BAD, GOOD)
    rec = X.Rec("GF07", "arch", 1)
    arch12(g, p).run(X.Task("GF07", GF07, "single-app", {"app": FINDER}, [], {}), rec)
    assert f"{ROOT}/B/Courtyard Bids/Review" not in g.fs
    assert g.isdir(f"{ROOT}/B/Courtyard Bids/Current/Review"), (rec.stopped, [s.get("why") for s in rec.steps])
    assert not rec.stopped and rec.location_replans12 == 1 and p.calls == 2
    fb = p.histories[1][-1]["content"]
    assert "'Courtyard Bids'" in fb and "'Current'" in fb and "nothing was created" in fb
    first = next(s for s in rec.steps if s["step"]["effect"] == "object_create")
    assert first["outcome"] == "failed" and first["location12"]["problems"]


def test_a_second_violation_refuses_and_creates_nothing():
    g = T9.FakeGuest9(dict(TREE), shown="B/Courtyard Bids")
    rec = X.Rec("GF07", "arch", 1)
    arch12(g, FakePlanner4(BAD, BAD)).run(X.Task("GF07", GF07, "single-app", {"app": FINDER}, [], {}), rec)
    assert not any(k.endswith("/Review") for k in g.fs) and rec.stopped == "refused (location check)"


def test_without_an_exclusion_object_create_is_unchanged():
    g = T9.FakeGuest9(dict(TREE), shown="B/Courtyard Bids")
    rec = X.Rec("X1", "arch", 1)
    arch12(g, FakePlanner4(plan4(S("launch_app", "Finder"), S("object_create", "Finder", "folder", "Review")))).run(
        X.Task("X1", "Make an empty folder called Review here.", "single-app", {"app": FINDER}, [], {}), rec)
    assert g.isdir(f"{ROOT}/B/Courtyard Bids/Review") and not rec.stopped
    assert "location12" not in next(s for s in rec.steps if s["step"]["effect"] == "object_create")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
