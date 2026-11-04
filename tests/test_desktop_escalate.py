"""Escalation on a safe hand-off (``execution/desktop_escalate.py``: ``Arch8E`` = the incumbent Arch8 + a frontier
agent inside our guardrails), offline with fakes.

Pinned here:

* only a SAFE hand-off escalates (a picker hand-off of any cause, a recovery-layer hand-off, a failed declared
  precondition); a completed run, a human gate, a refusal, the guard or a plain failed step never does;
* the agent answers ONE typed JSON action per call (at most 10, one attempt); it sees the request, why the engine
  stopped, the planner's plan, the steps done, the lists it may act on with exact labels and its own results;
* our executor runs each action with every guardrail: an item only when its label names exactly one listed item
  (else a failure fed back, never a guess), the replace gate, the exact permitted diff, the journal and its
  compare-and-swap restore (a restore is an error, merged into the run), an irreversible or external option as a
  human gate (declined) instead of a press, the guest's guard;
* every call is logged with its seconds and US$; the escalation's successes compile like the engine's and become
  routines with parameters; Arch8E's compiled mode replays them with the checked replay.

No desktop, no AX, no subprocess, no network.
"""
import json
import sys

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e7 as R7
from anyuse.execution import desktop_e2e8 as R8
from anyuse.execution import desktop_escalate as E
from anyuse.execution import desktop_routine as R

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
ROOT = T4.ROOT
TREE = {"P/preise.txt": "Preise\nPreis: 5 Euro\nRest", "P/alt.txt": "Preise alt\nPreis: 4 Euro"}
GOAL = "Open the newer price file and change Preis: 5 Euro to Preis: 7 Euro, then save."
PLAN = plan4(S("launch_app", "Finder"), S("open_item", "Finder", "the newer price file"),
             S("read_content", "TextEdit", "document", "text"))
P = lambda name: f"{ROOT}/P/{name}"                                                    # noqa: E731


class Guest(T9.FakeGuest9):
    """``FakeGuest9`` plus the AX text the agent reads (the front app's window only)."""

    def op_ax_text(self, value_chars=60):
        return {"status": "done", "bundle": self.front,
                "text": json.dumps([{"app": self.front, "window": "w", "document": self.te_front}])}


def none_chooser(state, q, options):
    """ "none of these" in both orders (Arch7's second order lists it first): a safe hand-off."""
    return options.index(R7.NONE_OPTION), 0.99, {"cost": 0.0}


class Model:
    """A scripted agent: one answer per call (the last repeats), with the CLI's seconds and US$."""

    def __init__(self, *actions, usd=0.01):
        self.actions, self.usd, self.prompts = list(actions), usd, []

    def __call__(self, system, prompt):
        self.prompts.append((system, prompt))
        a = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        text = a if isinstance(a, str) else json.dumps({"thought": "t", "action": a})
        return text, {"seconds": 2.5, "cost_usd": self.usd, "usage": {"input_tokens": 100}}


def arch8(g, planner):
    return R8.Arch8(g, EMAP, planner=planner, chooser=none_chooser, spend=None, inbox=None, sleep=lambda s: None,
                    picker_name="openjev", picker_kind="openjev", config_override=(0.45, "pair"))


def arch8e(g, model, planner=None, allow=None):
    esc = E.Escalation15(g, EMAP, model, allow=allow, sleep=lambda s: None)
    return E.Arch8E(arch8(g, planner or FakePlanner4(PLAN)), esc,
                    replay=R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None))


def run(a, goal=GOAL):
    rec = X.Rec("T", "arch8e", 1)
    a.run(X.Task("T", goal, "single-app", {}, [], {}), rec)
    return rec


OPEN = {"effect": "open_item", "app": "Finder", "label": "preise.txt"}
READ = {"effect": "read_content", "app": "TextEdit"}
EDIT = {"effect": "doc_save", "app": "TextEdit", "mode": "edit", "find": "Preis: 5 Euro", "value": "Preis: 7 Euro"}
DONE = {"effect": "done"}


def test_only_a_safe_hand_off_escalates():
    rec = X.Rec("T", "a", 1)
    rec.stopped = "picker hand-off (declined)"
    rec.steps = [{"step": S("open_item", "Finder", "x"), "outcome": "handoff", "pick": {"reason": "NONE in one order"}}]
    assert "NONE in one order" in E.safe_handoff(rec) and "open_item Finder 'x'" in E.safe_handoff(rec)
    rec.stopped = "hand-off (declined): identity recheck"
    assert E.safe_handoff(rec)
    rec.stopped, rec.steps = "failed step: press: 'Als Liste' is disabled", [
        {"step": S("toggle_select", "Notes", "Darstellung", "Als Liste"),
         "precondition": {"disabled": "'Als Liste' is disabled", "declared": ["gallery_item_open"], "restored": None}}]
    assert E.safe_handoff(rec).startswith("a failed precondition")
    rec.steps[0]["precondition"]["retry"] = "done"
    assert E.safe_handoff(rec) is None                                 # the retry worked: another failure
    for st in ("", "human gate (declined)", "refused (plan)", "irreversible-action guard (declined)",
               "auth prompt seen", "refused (write check failed: the old text restored)", "failed step: x"):
        rec.stopped, rec.steps = st, [{"step": S("launch_app", "Finder"), "outcome": "failed"}]
        assert E.safe_handoff(rec) is None, st


def test_one_typed_action_per_answer_and_the_vocabulary():
    a, thought, err = E.parse_action15('Sure: {"thought": "open it", "action": {"effect": "open_item", '
                                       '"app": "Finder", "label": "a.txt"}}')
    assert err is None and a["label"] == "a.txt" and thought == "open it"
    assert E.parse_action15('{"effect": "done"}')[0] == {"effect": "done"}
    assert E.parse_action15('{"action": {"effect": "delete_file"}}')[2].startswith("unknown effect")
    assert E.parse_action15("no json")[2] == "no JSON object"
    st, fx, err = E.to_step15({"effect": "open_item", "app": "Notizen", "label": "Türschloss"})
    assert st["app"] == "Notes" and fx == {"op": "open", "label": "Türschloss"} and not err
    st, fx, _ = E.to_step15(OPEN)
    assert fx == {"op": "open", "route": "finder", "label": "preise.txt"}
    assert E.to_step15({"effect": "doc_save", "mode": "edit", "value": "x"})[2] == "doc_save mode edit needs find"
    assert E.to_step15({"effect": "flag_toggle", "app": "TextEdit", "target": "x", "value": "maybe"})[2]
    assert E.to_step15({"effect": "navigate", "app": "Safari", "label": "x"})[2].startswith("app 'Safari'")
    assert E.press_gate15({"effect": "toggle_select", "app": "Dictionary", "target": "Suchen", "value": "Wikipedia"})
    assert E.press_gate15({"effect": "flag_toggle", "app": "Finder", "target": "Papierkorb entleeren", "value": "on"})
    assert E.press_gate15({"effect": "toggle_select", "app": "Finder", "target": "Sortieren nach",
                           "value": "Art"}) is None


def test_arch8e_escalates_a_picker_hand_off_and_the_agent_completes_inside_our_executor():
    g = Guest(TREE, shown="P")
    m = Model(OPEN, READ, EDIT, DONE, usd=0.02)
    rec = run(arch8e(g, m))
    assert rec.status == "completed", rec.stopped
    assert g.fs[P("preise.txt")] == "Preise\nPreis: 7 Euro\nRest" and g.fs[P("alt.txt")] == TREE["P/alt.txt"]
    esc = rec.escalation
    assert esc["arch8_stopped"] == "picker hand-off (declined)" and "none of these (both orders" in esc["handoff"]
    assert esc["calls"] == 4 and rec.calls["agent"] == 4 and abs(esc["usd"] - 0.08) < 1e-9 and esc["seconds"] == 10.0
    system, first = m.prompts[0]
    assert "ONE action per answer" in system and "at most 10 actions" in system
    assert "User request: " + GOAL in first and "Why our engine stopped" in first and "the newer price file" in first
    assert "preise.txt (file)" in first                                     # the exact labels it may act on
    assert "Preis: 5 Euro" in m.prompts[2][1]                                # the text read, shown next turn
    # a success of the escalation compiles like the engine's and becomes a routine with parameters
    esc_steps = [c for c in rec.compiled if (c["resolved"] or {}).get("by15") == "escalation"]
    assert [c["step"]["effect"] for c in esc_steps] == ["open_item", "read_content", "doc_save"]
    ps = {p["name"]: p["value"] for p in R.derive_routine(rec.compiled, GOAL)["params"]}
    assert ps["file"] == "preise.txt" and ps["text"] == "Preis: 7 Euro" and ps["anchor"] == "Preis: 5 Euro"


def test_the_escalated_routine_replays_on_new_inputs_through_the_checked_replay():
    g = Guest(TREE, shown="P")
    rec = run(arch8e(g, Model(OPEN, READ, EDIT, DONE)))
    r = R.derive_routine(rec.compiled, GOAL)
    g2 = Guest(TREE, shown="P")
    a = arch8e(g2, Model(DONE))
    steps, goal, probs = a.replay.prepare(r, {"file": "alt.txt", "anchor": "Preis: 4 Euro", "text": "Preis: 6 Euro"})
    rec2 = X.Rec("T", "arch8e", 1, mode="compiled")
    a.run(X.Task("T", goal, "single-app", {}, [], {}), rec2, compiled=steps)
    assert rec2.status == "completed", rec2.stopped
    assert g2.fs[P("alt.txt")] == "Preise alt\nPreis: 6 Euro" and sum(rec2.calls.values()) == 0


def test_a_wrong_label_is_a_failure_fed_back_never_a_guess():
    g = Guest(dict(TREE, **{"P/Preise/x.txt": "x"}), shown="P")
    m = Model({"effect": "open_item", "app": "Finder", "label": "preise"}, OPEN, READ, EDIT, DONE)
    rec = run(arch8e(g, m))
    assert rec.status == "completed" and g.fs[P("preise.txt")].endswith("7 Euro\nRest")
    assert "FAILED: identity check: 'preise' is not in 'P'" in m.prompts[1][1]
    m2 = Model({"effect": "open_item", "app": "Finder", "label": "Preise"}, DONE)
    g2 = Guest(dict(TREE, **{"P/Preise/x.txt": "x"}), shown="P")
    run(arch8e(g2, m2))
    assert "is a folder, the routine opens a file" in m2.prompts[1][1]


def test_an_external_or_irreversible_option_becomes_a_declined_human_gate_and_nothing_is_pressed():
    g = Guest(TREE, shown="P")
    m = Model({"effect": "toggle_select", "app": "Dictionary", "target": "Suchen", "value": "Wikipedia"}, DONE)
    rec = run(arch8e(g, m))
    assert rec.stopped == "human gate (declined)" and rec.gate_stops == 1 and rec.calls["agent"] == 1
    assert not any(op == "press" for op, _a in g.log)


def test_a_replace_without_the_users_word_is_gated_and_a_torn_save_is_restored_and_counted():
    g = Guest(TREE, shown="P")
    m = Model(OPEN, {"effect": "doc_save", "app": "TextEdit", "mode": "replace", "value": "neu"}, DONE)
    rec = run(arch8e(g, m))
    assert rec.stopped == "human gate (declined)" and g.fs[P("preise.txt")] == TREE["P/preise.txt"]
    g = Guest(TREE, shown="P")
    real = g.op_press

    def torn(bundle, trigger, label):
        out = real(bundle, trigger, label)
        if bundle == T4.TE and g.te_front == P("preise.txt"):
            g.fs[P("preise.txt")] = "Pre"                                     # an interrupted save
        return out
    g.op_press = torn
    rec = run(arch8e(g, Model(OPEN, READ, EDIT, DONE)))
    assert g.fs[P("preise.txt")] == TREE["P/preise.txt"] and rec.errors9 and rec.errors9[0]["restored"]
    assert rec.stopped.startswith("refused (write check failed") and rec.status == ""


def test_at_most_ten_actions_one_attempt_and_unusable_answers_count():
    g = Guest(TREE, shown="P")
    m = Model("I am not sure.")
    rec = run(arch8e(g, m))
    assert rec.calls["agent"] == 10 and rec.stopped == "escalation: 10 actions without done"
    assert len(rec.escalation["model_calls"]) == 10 and rec.escalation["model_calls"][0]["error"] == "no JSON object"
    budget = {"n": 0}

    def allow():
        budget["n"] += 1
        return budget["n"] <= 2
    rec = run(arch8e(Guest(TREE, shown="P"), Model("x"), allow=allow))
    assert rec.calls["agent"] == 2 and rec.stopped == "escalation budget reached before a call"


def test_no_escalation_without_a_safe_hand_off():
    g = Guest(TREE, shown="P")
    m = Model(DONE)
    plan = plan4(S("launch_app", "Finder"), S("open_item", "Finder", "preise.txt"),
                 S("human_gate", "Finder", "preise.txt", "delete"))
    rec = run(arch8e(g, m, planner=FakePlanner4(plan)), goal="Open preise.txt, then delete it.")
    assert rec.stopped == "human gate (declined)" and m.prompts == [] and rec.escalation["handoff"] is None
    plain = [{"step": S("launch_app", "Finder"), "resolved": {"op": "launch", "bundle": T4.FINDER}}]
    assert not E.escalated(plain) and E.escalated([{"step": {}, "resolved": {"by15": "escalation"}}])


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
