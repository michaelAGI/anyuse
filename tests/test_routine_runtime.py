"""Routine runtime: run, run_until, hand_off, resume, receipts, autonomy.

Offline: fake runners and a fake advisor, receipts in a temp directory.
No network, no model, no desktop.
"""
from __future__ import annotations

import copy
import inspect
import json
import os
import shutil
import tempfile
import time

from anyuse.execution.routine import Engine, ReceiptStore, SkillRunner, outcome
from anyuse.execution.skill_manifest import manifest_digest
from test_skill_manifest import PID, TD, base, proved_step

DATA = {"search": {"items": ["a1", "a2"]}, "detail": {"title": "Title a2"}}


class FakeRunner:
    def __init__(self, data=None):
        self.data = dict(DATA if data is None else data)
        self.calls = []
        self.fresh_problem = None
        self.script = {}          # step id -> list of outcomes to return in turn

    def fresh_check(self, step, bound):
        return self.fresh_problem

    def execute(self, step, bound, ctx):
        self.calls.append((step["id"], dict(bound), ctx["idempotency_key"]))
        queue = self.script.get(step["id"])
        if queue:
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                return item()
            return item
        return outcome(True, dict(self.data.get(step["id"], {})),
                       effect_class=step["effects"]["class"])


class FakeAdvisor:
    max_cost_usd = 0.01

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def propose(self, step_id, candidates):
        self.calls += 1
        return {"choice": self.answers.pop(0), "cost_usd": 0.004}


class Tmp:
    def __enter__(self):
        self.path = tempfile.mkdtemp()
        return self.path

    def __exit__(self, *_):
        shutil.rmtree(self.path, ignore_errors=True)


def engine(tmp, manifest=None, runner=None, advisor=None):
    runner = runner or FakeRunner()
    return Engine(manifest or base(), {"compiled_tool": runner},
                  ReceiptStore(tmp), advisor=advisor), runner


def test_run_hands_off_at_the_human_step_then_resumes_to_completion():
    with Tmp() as tmp:
        eng, runner = engine(tmp)
        res = eng.run({"q": "x"})
        assert res["status"] == "handed_off", res
        ho = res["handoff"]
        assert ho["reason"] == "human_step" and ho["next_step"]["id"] == "pick"
        assert ho["next_step"]["bound_inputs"] == {"candidates": ["a1", "a2"]}
        assert ho["next_step"]["expected_outputs"]["required"] == ["id"]
        assert ho["completed_steps"] == ["search"]
        # a fresh engine and store object: state comes only from disk receipts
        eng2, runner2 = engine(tmp)
        done = eng2.resume(res["run_id"], expected_head=ho["head"],
                           human={"step": "pick", "outputs": {"id": "a2"}})
        assert done["status"] == "completed", done
        assert done["outputs"] == {"title": "Title a2"}
        assert [c[0] for c in runner.calls] == ["search"]
        assert [c[0] for c in runner2.calls] == ["detail"]
        assert runner2.calls[0][1] == {"id": "a2"}
        kinds = [(r["type"], r.get("step"), r.get("status")) for r in done["receipts"]]
        assert kinds == [("step", "search", "completed"), ("handoff", "pick", None),
                         ("step", "pick", "completed"), ("step", "detail", "completed")]
        assert set(done) <= {"status", "run_id", "manifest_digest", "outputs", "receipts"}
        # resuming a finished run is idempotent and runs nothing
        again = eng2.resume(res["run_id"])
        assert again["status"] == "completed" and len(runner2.calls) == 1


def test_human_input_is_typed_and_checked_against_postconditions():
    with Tmp() as tmp:
        eng, _ = engine(tmp)
        rid = eng.run({"q": "x"})["run_id"]
        bad = eng.resume(rid, human={"step": "pick", "outputs": {"id": 7}})
        assert bad["status"] == "handed_off" and "expected string" in bad["error"]
        forged = eng.resume(rid, human={"step": "pick", "outputs": {"id": "zzz"}})
        assert forged["status"] == "handed_off" and "postcondition" in forged["error"]
        extra = eng.resume(rid, human={"step": "pick",
                                       "outputs": {"id": "a1", "admin": True}})
        assert extra["status"] == "completed"
        chain = ReceiptStore(tmp).load(rid)
        assert all("admin" not in json.dumps(r.get("outputs") or {}) for r in chain)


def test_run_until_pauses_and_hand_off_reports_the_expected_precondition():
    with Tmp() as tmp:
        m = base()
        m["steps"][2]["preconditions"] = [{"op": "non_empty", "ref": "$in.id"}]
        m["steps"] = [m["steps"][0], m["steps"][2]]
        m["steps"][1]["bindings"] = {"id": {"kind": "const", "value": "a1"}}
        eng, runner = engine(tmp, m)
        res = eng.run_until({"q": "x"}, "search")
        assert res["status"] == "paused" and res["handoff"]["next_step"]["id"] == "detail"
        nxt = res["handoff"]["next_step"]
        assert nxt["preconditions"] == [{"op": "non_empty", "ref": "$in.id"}]
        assert nxt["preconditions_hold_now"] is True
        view = eng.hand_off(res["run_id"])
        assert view["handoff"]["next_step"]["id"] == "detail"
        assert [c[0] for c in runner.calls] == ["search"]
        assert eng.resume(res["run_id"])["status"] == "completed"
        assert [c[0] for c in runner.calls] == ["search", "detail"]


def test_resume_rechecks_fresh_state_before_executing():
    with Tmp() as tmp:
        eng, runner = engine(tmp)
        rid = eng.run({"q": "x"})["run_id"]
        runner.fresh_problem = "the compiled proof changed since the manifest pinned it"
        res = eng.resume(rid, human={"step": "pick", "outputs": {"id": "a1"}})
        assert res["status"] == "refused" and "proof changed" in res["error"]
        assert [c[0] for c in runner.calls] == ["search"]
        runner.fresh_problem = None
        assert eng.resume(rid)["status"] == "completed"


def test_stale_tampered_or_foreign_runs_are_refused():
    with Tmp() as tmp:
        eng, _ = engine(tmp)
        res = eng.run({"q": "x"})
        rid = res["run_id"]
        stale = eng.resume(rid, expected_head="0" * 64,
                           human={"step": "pick", "outputs": {"id": "a1"}})
        assert stale["status"] == "refused" and "stale" in stale["error"]
        other = copy.deepcopy(base())
        other["version"] = "1.0.1"
        eng_other, _ = engine(tmp, other)
        assert "different manifest" in eng_other.resume(rid)["error"]
        path = os.path.join(tmp, rid, "000000.json")
        rec = json.load(open(path))
        rec["outputs"]["items"] = ["evil"]
        json.dump(rec, open(path, "w"))
        assert "chain broken" in eng.resume(rid)["error"]
        assert "malformed" in eng.resume("../etc")["error"]


def test_input_contract_is_enforced_before_any_run_exists():
    with Tmp() as tmp:
        eng, runner = engine(tmp)
        assert eng.run({"q": "x", "extra": 1})["status"] == "refused"
        assert eng.run({"q": "x" * 51})["status"] == "refused"
        assert eng.run({})["status"] == "refused"
        assert runner.calls == [] and os.listdir(tmp) == []


def write_manifest():
    m = base()
    m["steps"] = [m["steps"][0]]
    m["steps"][0].update({
        "effects": {"class": "side_effecting", "reversible": False},
        "approval": {"required": True},
        "policy": {"timeout_s": 0.2, "retries": 0, "idempotent": False}})
    m["outputs"] = {}
    return m


def test_a_write_waits_for_an_approval_bound_to_its_exact_inputs():
    with Tmp() as tmp:
        eng, runner = engine(tmp, write_manifest())
        res = eng.run({"q": "x"})
        assert res["status"] == "handed_off" and res["handoff"]["reason"] == "approval"
        digest = res["handoff"]["next_step"]["inputs_digest"]
        wrong = eng.resume(res["run_id"], approval={"step": "search",
                                                    "inputs_digest": "f" * 64})
        assert wrong["status"] == "handed_off" and runner.calls == []
        ok = eng.resume(res["run_id"], approval={"step": "search", "inputs_digest": digest})
        assert ok["status"] == "completed" and len(runner.calls) == 1


def test_an_uncertain_write_is_never_retried_without_a_human_reconcile():
    with Tmp() as tmp:
        runner = FakeRunner()
        runner.script["search"] = [lambda: (time.sleep(1.0), outcome(True))[1]]
        eng, _ = engine(tmp, write_manifest(), runner)
        res = eng.run({"q": "x"})
        digest = res["handoff"]["next_step"]["inputs_digest"]
        res = eng.resume(res["run_id"], approval={"step": "search", "inputs_digest": digest})
        assert res["status"] == "needs_reconcile" and "timed out" in res["error"]
        rid = res["run_id"]
        again = eng.resume(rid)
        assert again["status"] == "needs_reconcile" and len(runner.calls) == 1
        # the human says it did not happen: only then may it run again
        retried = eng.resume(rid, reconcile={"step": "search", "outcome": "not_done"})
        assert retried["status"] == "completed" and len(runner.calls) == 2
        # same idempotency key for the same run/step/inputs
        assert runner.calls[0][2] == runner.calls[1][2]


def test_reconcile_completed_records_the_human_outputs_and_moves_on():
    with Tmp() as tmp:
        runner = FakeRunner()
        runner.script["search"] = [RuntimeError("socket closed after send")]
        eng, _ = engine(tmp, write_manifest(), runner)
        res = eng.run({"q": "x"})
        digest = res["handoff"]["next_step"]["inputs_digest"]
        res = eng.resume(res["run_id"], approval={"step": "search", "inputs_digest": digest})
        assert res["status"] == "needs_reconcile"
        done = eng.resume(res["run_id"], reconcile={
            "step": "search", "outcome": "completed", "outputs": {"items": ["a9"]}})
        assert done["status"] == "completed" and len(runner.calls) == 1
        assert eng.resume(res["run_id"], reconcile={"step": "search",
                                                    "outcome": "not_done"})["status"] == "refused"


def test_read_only_transient_failures_retry_within_policy():
    with Tmp() as tmp:
        runner = FakeRunner()
        runner.script["search"] = [outcome(False, error="503", transient=True)]
        m = base()
        m["steps"] = [m["steps"][0]]
        m["outputs"] = {}
        eng, _ = engine(tmp, m, runner)
        res = eng.run({"q": "x"})
        assert res["status"] == "completed" and len(runner.calls) == 2


def test_effects_types_and_postconditions_are_checked_on_every_result():
    for scripted, needle in [
            (outcome(True, {"items": ["a"]}, effect_class="destructive"), "beyond the permitted"),
            (outcome(True, {"items": "a"}), "expected array"),
            (outcome(True, {"items": []}), "postcondition"),
            (outcome(True, {}), "missing output")]:
        with Tmp() as tmp:
            runner = FakeRunner()
            runner.script["search"] = [scripted]
            eng, _ = engine(tmp, runner=runner)
            res = eng.run({"q": "x"})
            assert res["status"] == "failed" and needle in res["error"], (needle, res)


def agent_manifest(level, limits=None, rule=None):
    m = base()
    m["steps"][1].update({
        "mode": "agent",
        "action": {"kind": "agent_choice", "choose_from": "$in.candidates", "into": "id",
                   **({"rule": rule} if rule else {})},
        "autonomy": {"level": level, **({"limits": limits} if limits else {})}})
    return m


def test_rule_level_never_calls_a_model():
    with Tmp() as tmp:
        runner = FakeRunner({"search": {"items": ["only"]}, "detail": {"title": "T"}})
        adv = FakeAdvisor(["only"])
        eng, _ = engine(tmp, agent_manifest("rule", rule="only"), runner, adv)
        assert eng.run({"q": "x"})["status"] == "completed" and adv.calls == 0
        eng2, _ = engine(tmp, agent_manifest("rule", rule="only"), FakeRunner(), adv)
        assert eng2.run({"q": "x"})["status"] == "handed_off" and adv.calls == 0


def test_model_proposes_and_a_human_confirms():
    with Tmp() as tmp:
        adv = FakeAdvisor(["a2"])
        eng, runner = engine(tmp, agent_manifest("model_proposes_human_confirms"), advisor=adv)
        res = eng.run({"q": "x"})
        ho = res["handoff"]
        assert ho["reason"] == "confirm_model_proposal" and ho["next_step"]["proposal"] == "a2"
        assert [c[0] for c in runner.calls] == ["search"]
        done = eng.resume(res["run_id"], expected_head=ho["head"],
                          human={"step": "pick", "accept_proposal": True})
        assert done["status"] == "completed" and runner.calls[-1][1] == {"id": "a2"}
        chain = ReceiptStore(tmp).load(res["run_id"])
        decision = [r for r in chain if r["type"] == "decision"][0]
        pick = [r for r in chain if r.get("step") == "pick" and r["type"] == "step"][0]
        assert decision["actor"] == "model" and pick["actor"] == "human"
        assert pick["decision"] == decision["hash"] and adv.calls == 1


def test_a_model_cannot_invent_a_value_outside_the_candidates():
    with Tmp() as tmp:
        adv = FakeAdvisor(["https://evil.example/exfil"])
        eng, runner = engine(tmp, agent_manifest(
            "model_decides_within_limits", {"cap_usd": 0.05, "max_decisions": 3}),
            advisor=adv)
        res = eng.run({"q": "x"})
        assert res["status"] == "handed_off" and "outside the candidates" in res["error"]
        assert [c[0] for c in runner.calls] == ["search"]
        chain = ReceiptStore(tmp).load(res["run_id"])
        d = [r for r in chain if r["type"] == "decision"][0]
        assert d["choice"] is None and d["rejected_out_of_list"] is True


def test_model_decides_within_limits_is_capped_and_audited():
    with Tmp() as tmp:
        adv = FakeAdvisor(["a1"])
        eng, runner = engine(tmp, agent_manifest(
            "model_decides_within_limits", {"cap_usd": 0.02, "max_decisions": 1}),
            advisor=adv)
        res = eng.run({"q": "x"})
        assert res["status"] == "completed" and res["outputs"] == {"title": "Title a2"}
        assert runner.calls[-1][1] == {"id": "a1"}
        chain = ReceiptStore(tmp).load(res["run_id"])
        d = [r for r in chain if r["type"] == "decision"][0]
        assert d["cost_usd"] == 0.004 and d["level"] == "model_decides_within_limits"
    with Tmp() as tmp:        # the reservation exceeds the cap: no call at all
        adv = FakeAdvisor(["a1"])
        eng, _ = engine(tmp, agent_manifest(
            "model_decides_within_limits", {"cap_usd": 0.005, "max_decisions": 1}),
            advisor=adv)
        res = eng.run({"q": "x"})
        assert res["status"] == "handed_off" and "limit" in res["error"] and adv.calls == 0
    with Tmp() as tmp:        # abstention, then the decision budget is spent
        adv = FakeAdvisor([None, "a1"])
        eng, _ = engine(tmp, agent_manifest(
            "model_decides_within_limits", {"cap_usd": 1.0, "max_decisions": 1}),
            advisor=adv)
        res = eng.run({"q": "x"})
        assert res["status"] == "handed_off" and adv.calls == 1
        res = eng.resume(res["run_id"])
        assert "limit" in res["error"] and adv.calls == 1


def test_composition_runs_a_pinned_all_proved_skill():
    with Tmp() as tmp:
        sub = base(kind="skill", name="lookup")
        sub["steps"] = [sub["steps"][0]]
        sub["outputs"] = {"items": {"type": "array", "items": {"type": "string"},
                                    "from": {"step": "search", "output": "items"}}}
        routine = base()
        routine["steps"][0] = proved_step(
            "search", inputs={"q": {"type": "string"}},
            outputs={"items": {"type": "array", "items": {"type": "string"}}},
            bindings={"q": {"kind": "input", "name": "q"}},
            action={"kind": "skill", "ref": "lookup", "digest": manifest_digest(sub)})
        runner = FakeRunner()
        store = ReceiptStore(tmp)
        runners = {"compiled_tool": runner}
        runners["skill"] = SkillRunner({"lookup": sub}, runners, store)
        eng = Engine(routine, runners, store)
        res = eng.run({"q": "x"})
        assert res["status"] == "handed_off" and res["handoff"]["next_step"]["id"] == "pick"
        chain = store.load(res["run_id"])
        assert chain[0]["provenance"]["sub_run_id"]
        changed = copy.deepcopy(sub)
        changed["version"] = "1.0.1"
        runners["skill"] = SkillRunner({"lookup": changed}, runners, store)
        res = Engine(routine, runners, store).run({"q": "x"})
        assert res["status"] == "refused" and "changed since the routine pinned" in res["error"]


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    passed = failed = 0
    for name, fn in [(n, f) for n, f in vars(mod).items()
                     if n.startswith("test_") and callable(f)
                     and getattr(f, "__module__", "") == __name__]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); passed += 1; print("OK  ", name)
        except Exception as exc:
            failed += 1; print("FAIL", name, "::", repr(exc)[:400])
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
