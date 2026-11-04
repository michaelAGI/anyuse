"""Arch11, offline, with a FAKE guest
and a fake chooser (the earlier arms' fakes).

Arm B = Arch8's incumbent picker protocol (rich options, both orders, NONE
last, the pair follow-up, min confidence >= t2; no positive-evidence rule, no
date veto) with a Cerebras JSON chooser that states its own confidence, on top
of the Arch9 / Arch10 recovery layer (typed evidence, the filter, the one inspection,
the identity recheck, the exact diff, the journal, the concurrency checks, the
planner checks). What is pinned here:

* the chooser's messages, schema and parsing (NONE last; the confidence clamped;
  a missing confidence never passes t2 > 0);
* the pick: both orders with NONE last; acted on only when both name the same
  candidate, neither NONE, min confidence >= t2; the pair follow-up only after
  two DIFFERENT candidates, with only those two (and NONE) and the fields that
  tell them apart; protocol "off" has no follow-up; every candidate contradicted
  = no call; no positive-evidence rule;
* ``decide11`` reproduces the arm's decision (and its calls) from answers
  recorded at t2 = 0, and Arch8's rule chooses t2;
* the settings cross-check through the same chooser (its confidence);
* the arm end to end (no OpenJev anywhere) and the inherited write-time
  concurrency check;
* the reporting helpers (Wilson, the seeded order).

No desktop, no AX, no subprocess, no network.
"""
import datetime as dt
import json
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e7 as R7
from anyuse.execution import desktop_e2e9 as N
from anyuse.execution import desktop_e2e10 as T
from anyuse.execution import desktop_e2e11 as E

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e10 as T10

EMAP = W.EffectMap4()
FakePlanner4 = T4.FakePlanner4
FINDER, TE = T4.FINDER, T4.TE
NOW = T10.NOW
HEAD_DE = T10.HEAD_DE
ROWS_SE01, GOAL_SE01, STEP_SE01 = T10.ROWS_SE01, T10.GOAL_SE01, T10.STEP_SE01
V2 = T9.V2


class ConfOracle:
    """A fake Cerebras chooser: the ID whose option text holds ``want`` (else NONE) with ``conf``; ``by_ask`` gives
    (want, conf) per ask in sequence (cycled)."""

    def __init__(self, want=None, conf=0.9, by_ask=None, cost=0.001):
        self.want, self.conf, self.by_ask, self.cost, self.asked = want, conf, by_ask, cost, []

    def __call__(self, state, q, options):
        self.asked.append((state, q, list(options)))
        want, conf = (self.by_ask[(len(self.asked) - 1) % len(self.by_ask)] if self.by_ask
                      else (self.want, self.conf))
        for cid, text in options:
            if want not in (None, "none") and want in text:
                return cid, False, {"cost": self.cost, "ms": 5, "confidence": conf}
        return None, True, {"cost": self.cost, "ms": 5, "confidence": conf}


def cfg(t2=0.5, protocol="pair"):
    return {"model": "fake", "protocol": protocol, "t2": t2}


# ── the chooser ──────────────────────────────────────────────────────────────

def test_the_messages_schema_and_parsing_carry_a_confidence_and_a_fixed_last_none():
    msgs = E.json_messages11("STATE X", "Q?", [("C2", "b.txt"), ("C1", "a.txt")])
    u = msgs[1]["content"]
    assert "STATE X" in u and u.index("C2: b.txt") < u.index("C1: a.txt") < u.index(E.NONE11)
    assert "\"confidence\"" in u and msgs[0]["content"] == T.JSON_SYSTEM10
    sc = E.json_schema11(["C1", "C2"])
    assert sc["properties"]["choice"]["enum"] == ["C1", "C2", "NONE"] and sc["required"] == ["choice", "confidence"]
    assert E.parse_json11('{"choice": "C2", "confidence": 0.83}', ["C1", "C2"]) == ("C2", False, 0.83, None)
    assert E.parse_json11('{"choice": "NONE", "confidence": 0.7}', ["C1"]) == (None, True, 0.7, None)
    assert E.parse_json11('{"choice": "C1", "confidence": 7}', ["C1"])[2] == 1.0          # clamped
    assert E.parse_json11('{"choice": "C1", "confidence": "high"}', ["C1"])[2] == 0.0    # unreadable = 0
    assert E.parse_json11('{"choice": "C9", "confidence": 1}', ["C1"])[3]
    assert E.parse_json11("no json", ["C1"])[3]


# ── the pick ────────────────────────────────────────────────────────────────

def test_both_orders_with_none_last_and_the_rich_options():
    o = ConfOracle("schreiner_v2", 0.9)
    r = E.pick11(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o)
    assert r["index"] == 2 and r["calls"] == 2 and r["calls_by"] == {"cerebras": 2}
    (s0, q0, o0), (s1, _q1, o1) = o.asked
    assert [c for c, _t in o0] == ["C1", "C3"] and [c for c, _t in o1] == ["C3", "C1"]   # C2: not the newest
    assert [d[0] for d in r["dropped"]] == ["C2"]
    assert dict(o0)["C3"].startswith("angebot_schreiner_v2.txt") and "modified 2026-08-19" in dict(o0)["C3"]
    assert T.LEGEND10 in s0 and s0.index("C1: ") < s0.index("C3: ") and s1.index("C3: ") < s1.index("C1: ")
    assert "\"unknown\" is NOT a misfit" in q0 and "Answer with JSON" not in q0       # the format is in the message
    assert [a["round"] for a in r["answers"]] == ["first", "first"]


def test_agreement_below_t2_is_a_hand_off_without_a_follow_up():
    o = ConfOracle("schreiner_v2", 0.6)
    r = E.pick11(cfg(t2=0.7), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o)
    assert r["index"] is None and "below t2" in r["reason"] and len(o.asked) == 2
    assert E.pick11(cfg(t2=0.6), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                    ConfOracle("schreiner_v2", 0.6))["index"] == 2


def test_two_different_candidates_get_the_pair_follow_up_with_only_those_two():
    o = ConfOracle(by_ask=[("schreiner_v2", 0.9), ("dachdecker", 0.8), ("schreiner_v2", 0.95), ("schreiner_v2", 0.9)])
    r = E.pick11(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o)
    assert r["index"] == 2 and r["calls"] == 4 and "after the pair follow-up" in r["reason"]
    (s2, _q, o2), (_s3, _q3, o3) = o.asked[2:]
    assert [c for c, _t in o2] == ["C3", "C1"] and [c for c, _t in o3] == ["C1", "C3"]
    assert "A = C3, B = C1" in s2 and "They differ in:" in s2 and "C2: angebot_schreiner_v1" not in s2
    assert [a["round"] for a in r["answers"]] == ["first", "first", "pair", "pair"]
    # the follow-up says NONE in one order: a hand-off
    o2b = ConfOracle(by_ask=[("schreiner_v2", 0.9), ("dachdecker", 0.8), ("schreiner_v2", 0.95), ("none", 0.9)])
    r2 = E.pick11(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o2b)
    assert r2["index"] is None and "pair follow-up: NONE in one order" in r2["reason"]
    # protocol "off" (Arch7's rule alone): no follow-up
    o3b = ConfOracle(by_ask=[("schreiner_v2", 0.9), ("dachdecker", 0.8)])
    r3 = E.pick11(cfg(protocol="off"), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o3b)
    assert r3["index"] is None and len(o3b.asked) == 2 and "disagree" in r3["reason"]


def test_none_in_one_order_has_no_follow_up_in_the_frozen_protocol():
    o = ConfOracle(by_ask=[("schreiner_v2", 0.9), ("none", 0.9)])
    r = E.pick11(cfg(), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, o)
    assert r["index"] is None and len(o.asked) == 2 and "NONE in one order" in r["reason"]


def test_every_candidate_contradicted_is_no_call_and_there_is_no_positive_evidence_rule():
    goal = "Lösch die Notiz mit dem alten Wlan-Passwort. Nicht die Notiz über die Reinigung."
    rows = [{"label": "Reinigung", "text": "Reinigung | Firma kommt dienstags."}]
    o = ConfOracle("Reinigung", 0.99)
    r = E.pick11(cfg(), goal, {"effect": "select_item", "target": "Notiz mit dem alten Wlan-Passwort"}, "Notes.",
                 rows, "notes_notes", NOW, o)
    assert r["index"] is None and o.asked == [] and "contradicts" in r["reason"]
    rows2 = rows + [{"label": "Büro", "text": "Büro | Schlüssel beim Empfang."}]
    r2 = E.pick11(cfg(), "Lösch die Notiz mit dem alten Wlan-Passwort.",
                  {"effect": "select_item", "target": "Notiz mit dem alten Wlan-Passwort"}, "Notes.", rows2,
                  "notes_notes", NOW, ConfOracle("Büro", 0.99))
    assert r2["index"] == 1                       # Arch10's positive-evidence rule would hand it off


def test_decide11_reproduces_the_arms_decision_from_answers_recorded_at_t2_0():
    seqs = [[("schreiner_v2", 0.9), ("schreiner_v2", 0.7)],
            [("schreiner_v2", 0.9), ("dachdecker", 0.8), ("schreiner_v2", 0.95), ("schreiner_v2", 0.6)],
            [("schreiner_v2", 0.9), ("none", 0.8)]]
    for seq in seqs:
        rec0 = E.pick11(cfg(t2=0.0), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                        ConfOracle(by_ask=seq))
        rec = {"ids": [f"C{i + 1}" for i in range(3)], "alive": [0, 1, 2], "answers": rec0["answers"]}
        for proto in E.PROTOCOLS11:
            for t2 in (0.0, 0.5, 0.65, 0.8, 1.0):
                live = E.pick11(cfg(t2=t2, protocol=proto), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files",
                                NOW, ConfOracle(by_ask=seq))
                assert E.decide11(rec, proto, t2) == (live["id"], live["calls"]), (seq, proto, t2)
    assert E.decide11({"alive": [], "answers": []}, "pair", 0.0) == (None, 0)


def test_prior_rule_chooses_the_smallest_t2_with_confident_wrong_at_most_3_percent():
    curve = [{"t2": 0.0, "cw": 0.06}, {"t2": 0.5, "cw": 0.031}, {"t2": 0.55, "cw": 0.0204},
             {"t2": 0.6, "cw": 0.03}, {"t2": 1.0, "cw": 0.0}]
    assert E.choose_t2(curve) == (0.55, True)
    assert E.choose_t2([{"t2": 0.0, "cw": 0.2}, {"t2": 1.0, "cw": 0.05}]) == (1.0, False)
    assert E.GRID11[0] == 0.0 and E.GRID11[-2:] == [0.99, 1.0] and len(E.GRID11) == 22


def test_the_settings_cross_check_asks_the_same_chooser_with_its_confidence():
    ch = E.text_chooser11(ConfOracle("Rechtschreibung", 0.77))
    assert ch("S", "Q", ["Grammatik", "Rechtschreibung", R7.NONE_OPTION])[:2] == (1, 0.77)
    assert E.text_chooser11(ConfOracle("none", 0.9))("S", "Q", [R7.NONE_OPTION, "Grammatik"])[:2] == (0, 0.9)
    i, c, m = E.text_chooser11(ConfOracle("none", 0.9))("S", "Q", ["Grammatik"])
    assert i is None and m["error"] == "NONE"


def test_the_frozen_configuration_is_required():
    try:
        E.config_of11({"frozen": None})
        raise AssertionError("an unfrozen configuration must not load")
    except KeyError:
        pass
    assert E.config_of11({"frozen": {"model": "gpt-oss-120b", "protocol": "pair", "t2": 0.4}})["t2"] == 0.4
    try:
        E.Arch11(T9._se01_guest(), EMAP, planner=None, config11=cfg(), jchooser=None)
        raise AssertionError("the arm needs its chooser")
    except X.GuestError:
        pass


# ── the arm, end to end on fakes ─────────────────────────────────────────────

def arch11(guest, planner, js, t2=0.5, **kw):
    return E.Arch11(guest, EMAP, planner=planner, config11=cfg(t2=t2), jchooser=js, spend=None, inbox=None,
                    sleep=lambda s: None, **kw)


def test_se01_like_the_arm_edits_the_newest_offer_with_cerebras_alone():
    g = T9._se01_guest()
    rec = X.Rec("SE01", "arch", 1)
    a = arch11(g, FakePlanner4(*T9.SE01_PLAN), ConfOracle("schreiner_v2", 0.9))
    assert a.picker_kind == "cerebras_picker" and a.threshold == 0.5 and a.picker_name == "cerebras:fake"
    a.run(X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2] == "Angebot Schreinerarbeiten Gartenhaus\nPreis: 4.850 Euro", (rec.stopped, rec.steps[-1])
    assert rec.picker_calls10 == {"cerebras": 2} and not rec.stopped and rec.errors9 == []
    pick = next(s["pick"] for s in rec.steps if s.get("pick"))
    assert pick["index"] == 2 and "date_check" not in pick and pick["config"]["protocol"] == "pair"


def test_a_low_confidence_pick_writes_nothing():
    g = T9._se01_guest()
    rec = X.Rec("SE01", "arch", 1)
    arch11(g, FakePlanner4(*T9.SE01_PLAN), ConfOracle("schreiner_v2", 0.4)).run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert "5.050" in g.fs[V2] and rec.stopped == "picker hand-off (declined)" and T9.ops(g, "enter") == []


def test_the_write_time_concurrency_check_is_inherited():
    g = T9._se01_guest()
    real = g.op_field
    n = {"k": 0}

    def field_then_edit(bundle, role=None, identifier=None, **kw):
        r = real(bundle, role=role, identifier=identifier, **kw)
        if bundle == TE:
            n["k"] += 1
            if n["k"] == 1:
                g.fs[V2] = g.fs[V2] + "\nNotiz: telefonisch bestätigt"
        return r
    g.op_field = field_then_edit
    rec = X.Rec("SE01", "arch", 1)
    arch11(g, FakePlanner4(*T9.SE01_PLAN), ConfOracle("schreiner_v2", 0.9)).run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2].endswith("Notiz: telefonisch bestätigt") and "5.050" in g.fs[V2]
    assert rec.stopped == "hand-off (declined): concurrency" and rec.errors9 == []


def test_compiled_mode_needs_no_chooser():
    a = E.Arch11(T9._se01_guest(), EMAP, planner=None, spend=None, inbox=None)
    assert a.config11 is None and a.chooser is None and a.jchooser is None


# ── reporting helpers ────────────────────────────────────────────────────────

def test_wilson_and_the_seeded_order():
    lo, hi = E.wilson(25, 45)
    assert abs(lo - 0.41) < 0.01 and abs(hi - 0.69) < 0.01            # a measured "25/45 [41-69 %]"
    lo0, hi0 = E.wilson(0, 30)
    assert lo0 == 0.0 and abs(hi0 - 0.114) < 0.002
    ids = [f"T{i}" for i in range(30)]
    assert E.run_order11(ids, 7) == E.run_order11(ids, 7) and sorted(E.run_order11(ids, 7)) == sorted(ids)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
