"""Arm C = ``desktop_e2e14.Arch14``, offline with fakes.

Arm C is a thin composition: Arch12 (the Arch9 / Arch10 recovery layer, Arch12's filter and planner checks) with
its declared precondition (``precondition13``) switched on, and OpenJev as the picker under Arch8's FROZEN pick protocol
(rich options, both orders with NONE last, the pair follow-up, t2 = 0.45, the date veto) instead of the Cerebras
chooser. Pinned here:

* OpenJev's text chooser behind the interface Arch11's ``ask11`` calls (the options in order, NONE last, its
  confidence carried, errors and out-of-range answers are answer errors);
* the configuration is Arch8's frozen one for OpenJev (pair, 0.45) with the date veto, or none;
* the pick: Arch12's filter first (every candidate contradicted = no OpenJev call), then both orders, NONE last,
  Arch8's rich options and Arch10's typed-evidence state; acted on only at or above t2; the pair follow-up only
  after two DIFFERENT candidates; Arch8's date veto over the candidates OpenJev was asked about;
* the arm end to end on fakes: Arch12's search-scope rows and value check with OpenJev picks, the declared
  precondition ON by default, the date veto writing nothing, the settings cross-check on OpenJev, compiled mode
  without a picker;
* the randomized balanced order within task pairs and the order seed.

No desktop, no AX, no subprocess, no network.
"""
import sys

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e7 as R7
from anyuse.execution import desktop_e2e10 as T
from anyuse.execution import desktop_e2e12 as Z
from anyuse.execution import desktop_e2e14 as F

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e10 as T10
import test_desktop_e2e12_precondition_ext as T13
import test_desktop_e2e12_search_scope as TS
import test_desktop_e2e12_value as TV

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES = T4.FINDER, T4.NOTES
NOW, HEAD_DE = T10.NOW, T10.HEAD_DE
ROWS_SE01, GOAL_SE01, STEP_SE01 = T10.ROWS_SE01, T10.GOAL_SE01, T10.STEP_SE01
RichOracle = T10.RichOracle
V2 = T9.V2


def cfg(t2=0.45, veto=True):
    return {"model": "openjev", "protocol": "pair", "t2": t2, "date_veto": veto}


class ConfByAsk:
    """A fake OpenJev whose answer AND confidence follow a script per ask: [(want, conf), ...] (cycled)."""

    def __init__(self, seq):
        self.seq, self.asked = seq, []

    def __call__(self, state, q, options):
        self.asked.append((state, q, list(options)))
        want, conf = self.seq[(len(self.asked) - 1) % len(self.seq)]
        for i, o in enumerate(options[:-1]):
            if want != "none" and want in o:
                return i, conf, {"cost": 0.0, "ms": 3}
        return len(options) - 1, conf, {"cost": 0.0, "ms": 3}


# ── OpenJev behind Arch11's interface; the configuration ──────────────────────────────────────────────────

def test_openjev_behind_the_json_interface_keeps_the_order_and_puts_none_last():
    o = RichOracle("b.txt", conf=0.71)
    j = F.openjev_as_json14(o)
    assert j("S", "Q", [("C2", "b.txt | x"), ("C1", "a.txt | y")]) == ("C2", False, {"cost": 0.0, "ms": 3,
                                                                                   "confidence": 0.71})
    assert o.asked[0][2] == ["b.txt | x", "a.txt | y", F.NONE14] and o.asked[0][:2] == ("S", "Q")
    assert j("S", "Q", [("C1", "a.txt")])[:2] == (None, True)                       # NONE (the last option)
    err = F.openjev_as_json14(lambda s, q, o: (None, 0.0, {"error": "timed out"}))("S", "Q", [("C1", "a")])
    assert err[0] is None and err[1] is False and err[2]["error"] == "timed out"
    out = F.openjev_as_json14(lambda s, q, o: (7, 0.9, {}))("S", "Q", [("C1", "a")])
    assert out[0] is None and "out of range" in out[2]["error"]


def test_the_configuration_is_prior_frozen_one_for_openjev_with_its_date_veto():
    assert F.config_of14() == {"model": "openjev", "protocol": "pair", "t2": 0.45, "date_veto": True}
    try:
        F.config_of14({"pickers": {}})
        raise AssertionError("an unfrozen configuration must not load")
    except KeyError:
        pass
    try:
        F.Arch14(T9._se01_guest(), EMAP, planner=None, config14=cfg(), openjev=None)
        raise AssertionError("the arm needs its picker")
    except X.GuestError:
        pass


# ── the pick ─────────────────────────────────────────────────────────────────────────────────────────────

def test_prior_filter_first_then_both_orders_with_none_last_and_the_rich_options():
    o = RichOracle("schreiner_v2", conf=0.5)
    r = F.pick14(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o, dated=False)
    assert r["index"] == 2 and r["calls"] == 2 and r["calls_by"] == {"openjev": 2} and r["mode"].startswith("openjev")
    assert [d[0] for d in r["dropped"]] == ["C2"]                                   # Arch9's rank: not the newest
    (s0, q0, o0), (s1, _q1, o1) = o.asked
    assert o0[-1] == o1[-1] == F.NONE14 and o0[:-1] == o1[:-1][::-1] and len(o0) == 3
    assert o0[1].startswith("angebot_schreiner_v2.txt") and "modified 2026-08-19" in o0[1]
    assert T.LEGEND10 in s0 and s0.index("C1: ") < s0.index("C3: ") and s1.index("C3: ") < s1.index("C1: ")
    assert "\"unknown\" is NOT a misfit" in q0
    lo = F.pick14(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, RichOracle("schreiner_v2", 0.44),
                  dated=False)
    assert lo["index"] is None and "below t2 = 0.45" in lo["reason"]


def test_two_different_candidates_get_prior_pair_follow_up_and_none_in_one_order_does_not():
    o = ConfByAsk([("schreiner_v2", 0.9), ("dachdecker", 0.8), ("schreiner_v2", 0.95), ("schreiner_v2", 0.6)])
    r = F.pick14(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o, dated=False)
    assert r["index"] == 2 and r["calls"] == 4 and "after the pair follow-up" in r["reason"]
    s2, _q, o2 = o.asked[2]
    assert o2 == [o.asked[0][2][1], o.asked[0][2][0], F.NONE14] and "They differ in:" in s2
    assert [a["round"] for a in r["answers"]] == ["first", "first", "pair", "pair"]
    o3 = ConfByAsk([("schreiner_v2", 0.9), ("none", 0.9)])
    r3 = F.pick14(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o3, dated=False)
    assert r3["index"] is None and len(o3.asked) == 2 and "NONE in one order" in r3["reason"]


def test_every_candidate_contradicted_is_a_hand_off_without_an_openjev_call():
    goal = "Lösch die Notiz mit dem alten Wlan-Passwort. Nicht die Notiz über die Reinigung."
    rows = [{"label": "Reinigung", "text": "Reinigung | Firma kommt dienstags."}]
    o = RichOracle("Reinigung", 0.99)
    r = F.pick14(cfg(), goal, {"effect": "select_item", "target": "Notiz mit dem alten Wlan-Passwort"}, "Notes.", rows,
                 "notes_notes", NOW, o)
    assert r["index"] is None and o.asked == [] and r["calls_by"] == {} and "contradicts" in r["reason"]


def test_prior_date_veto_acts_over_the_candidates_openjev_was_asked_about():
    # SE01-like: Arch8's veto is kept FROZEN with its flaw: the roofer's newer offer shares "Angebot" (Arch8's
    # SE01, all three runs), so the right newest carpenter's offer is handed off
    r = F.pick14(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, RichOracle("schreiner_v2", 0.9),
                 dated=True)
    assert r["index"] is None and "contradicts the newest modification date" in r["reason"]
    assert r["date_check"] == {"asks": "newest", "extreme": "C1", "pick": "C3", "among": ["C1", "C3"]}
    # an excluded candidate is dropped by Arch12's filter first: the veto never compares with it
    goal = "Mach mal das neueste Angebot auf, nicht das vom Dachdecker, und setz den Preis auf 4.850 Euro."
    step = {"effect": "open_item", "target": "das neueste Angebot", "target_ui": ""}
    r2 = F.pick14(cfg(), goal, step, HEAD_DE, ROWS_SE01, "finder_files", NOW, RichOracle("schreiner_v2", 0.9), dated=True)
    assert r2["index"] == 2 and "C1" not in r2["alive"], (r2["reason"], r2["dropped"])
    assert r2.get("date_check", {}).get("extreme") in (None, "C3")
    # without a superlative there is no date check; with the veto off (never in the arm) neither
    r3 = F.pick14(cfg(veto=False), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                  RichOracle("schreiner_v2", 0.9), dated=True)
    assert r3["index"] == 2 and "date_check" not in r3


# ── the arm, end to end on fakes ─────────────────────────────────────────────────────────────────────────

def arch14(guest, planner, oj, t2=0.45, **kw):
    return F.Arch14(guest, EMAP, planner=planner, openjev=oj, config14=cfg(t2=t2), spend=None, inbox=None,
                    sleep=lambda s: None, **kw)


def test_the_arm_is_prior_with_the_precondition_on_and_openjev_everywhere_a_pick_is_asked():
    o = RichOracle("x")
    a = arch14(T9._se01_guest(), FakePlanner4(*T9.SE01_PLAN), o)
    assert isinstance(a, Z.Arch12) and a.precondition13 is True and a.picker_kind == "openjev"
    assert a.chooser is o and a.threshold == 0.45 and a.picker_name == "openjev" and "date veto" in a.protocol
    assert F.Arch14(T9._se01_guest(), EMAP, planner=None).precondition13 is True     # compiled: the flag too
    c = F.Arch14(T9._se01_guest(), EMAP, planner=None, spend=None, inbox=None)
    assert c.config14 is None and c.chooser is None and c.jchooser is None
    # Arch7's settings cross-check asks OpenJev's text chooser with Arch7's none option, as Arch8 does
    p = R7.consistent_pick7(a.chooser, "x", "S", ["Grammatik", "Rechtschreibung"], a.threshold)
    assert o.asked[-2][2][-1] == o.asked[-1][2][0] == R7.NONE_OPTION and p.calls == 2


def test_se01_like_the_frozen_date_veto_writes_nothing():
    g = T9._se01_guest()
    rec = X.Rec("SE01", "arch", 1)
    arch14(g, FakePlanner4(*T9.SE01_PLAN), RichOracle("schreiner_v2", 0.9)).run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert "5.050" in g.fs[V2] and rec.stopped == "picker hand-off (declined)" and T9.ops(g, "enter") == []
    pick = next(s["pick"] for s in rec.steps if s.get("pick"))
    assert pick["date_check"]["extreme"] == "C1" and rec.picker_calls10 == {"openjev": 2}


def test_sf07_like_prior_search_rows_with_an_openjev_pick():
    g = TS.FakeSearch12(notes=dict(TS.NOTES_SF07))
    g.notes_folder, g.shown_note = "Alltag_Sonstiges", "Zahnarzt Kinder"
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("navigate", "Notes", "Gesundheit_Log"),
                           S("scoped_find", "Notes", "", "Zahnarzt"),
                           S("open_item", "Notes", TS.STEP_SF07["target"], sc="Gesundheit_Log")))
    o = RichOracle("Zahnarzt Termin", 0.8)
    rec = X.Rec("SF07", "arch", 1)
    arch14(g, p, o).run(X.Task("SF07", TS.SF07, "single-app", {"app": NOTES}, [], {}), rec)
    assert not rec.stopped and g.shown_note == "Zahnarzt Termin" and g.notes_search == "Zahnarzt", \
        (rec.stopped, [s.get("why") for s in rec.steps])
    step = next(s for s in rec.steps if s.get("pick"))
    assert step["pick"]["alive"] == ["C1"] and len(o.asked[0][2]) == 2            # the right note + NONE


def test_gf02_like_prior_value_check_with_openjev_picks():
    g = TV._gf02_guest()
    p = FakePlanner4(TV.P1_GF02, TV.LOOKUP("Trödeln"),
                     plan4(S("navigate", "Notes", "Sprachzettel Seminar"), S("open_item", "Notes", "Aufschieben"),
                           S("read_content", "Notes", "note", "text")),
                     TV.LOOKUP("Prokrastination"))
    o = ConfByAsk([("Später anfangen", 0.99)] * 2 + [("Morgen reicht auch", 0.9)] * 2)
    rec = X.Rec("GF02", "arch", 1)
    arch14(g, p, o).run(X.Task("GF02", TV.GF02, "cross-app", {"app": NOTES}, [], {}), rec)
    assert g.dict_search == "Prokrastination" and not rec.stopped, (rec.stopped, [s.get("why") for s in rec.steps])
    assert rec.value_replans12 == 1 and all(a.get("text") != "Trödeln" for op, a in g.log if op == "enter")


def test_gf10_like_prior_precondition_is_on_by_default():
    g = T13.SearchGalleryGuest(notes={k: list(v) for k, v in T13.NOTES_GF10.items()})
    g.front, g.notes_folder, g.shown_note = NOTES, "Werkstatt Dichtungen", "Kaffee in der Werkstatt"
    rec = X.Rec("GF10", "arch", 1)
    arch14(g, FakePlanner4(T13.PLAN_GF10), RichOracle("Handpresse", 0.9)).run(
        X.Task("GF10", T13.GF10, "single-app", {"app": NOTES}, [], {}), rec)
    tog = next(s for s in rec.steps if s["step"]["effect"] == "toggle_select")
    assert tog["outcome"] == "ok" and [x["state"] for x in tog["precondition"]["left13"]] == \
        ["notes_search_active", "gallery_item_open"]
    assert g.view == T13.LIST_KEY and g.notes_search == "Ersatzdichtung" and not rec.stopped


# ── the order ────────────────────────────────────────────────────────────────────────────────────────────

def test_the_order_is_randomized_and_balanced_within_task_pairs():
    ids = [f"T{i:02d}" for i in range(30)]
    o = F.pair_order14(ids, 12345)
    assert o == F.pair_order14(ids, 12345) and len(o) == 60
    assert sorted({t for t, _a in o}) == ids
    firsts = [o[k][1] for k in range(0, 60, 2)]
    assert all(o[k][0] == o[k + 1][0] and {o[k][1], o[k + 1][1]} == set(F.ARMS14) for k in range(0, 60, 2))
    assert firsts.count("arch14") == 15 and firsts.count("arch8") == 15
    assert [t for t, _a in F.pair_order14(ids, 1)] != [t for t, _a in o]
    assert F.order_seed14("ab" * 32) == F.order_seed14("ab" * 32) != F.order_seed14("ab" * 32, "x")
    assert len(F.pair_order14(ids[:3], 7, arms=("arch14",))) == 3


# ── the pool's loaders ───────────────────────────────────────────────────────────────────────────────────

def test_the_prior_loaders_put_every_path_under_prior_data_root():
    import json
    from anyuse.execution import desktop_e2e9 as N
    d9 = N.load_tasks9()
    t = json.loads(json.dumps(next(x for x in d9["tasks"] if x["author_task"]["id"] == "SF08")).replace("{GEN9}",
                                                                                                         "{GEN14}"))
    data = dict(d9, tasks=[t])
    task = F.task14_by_id("SF08", data)
    assert task.id == "SF08" and all("/e14/SF08/" in p for p in task.expect["file_text"])
    assert all(k.startswith(F.GEN14 + "/SF08/") for k in F.statements14("SF08", data))
    assert F.seeds14_spec(data)["notes"] == {"Haushalt_Info": [[n["title"], n["text"]] for n in
                                                              t["author_task"]["start"]["notes_folders"][0]["notes"]]}
    assert F.GEN14.endswith("/gen/e14") and len(F.tasks14_sha256(data)) == 64


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
