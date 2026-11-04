"""Model calls counted once, and no assumed success.

Model calls are counted ONCE, at ``execution.model_adapter`` (every adapter request as it is
sent; a model reached by another path reports to the same counter at its one chokepoint). A run's
``llm_calls`` is the delta of that counter, never a sum of per-rung self-reports.
``Agent.run`` does not assume success. The goal verifier's call is counted; a verdict below
the confidence floor, a verifier error, or no verifier at all is ``UNCERTAIN``, which stops with
its reason and is never a success.

No network and no model: every answer comes from ``httpx.MockTransport`` or a fake.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_agent_outcome_calls.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import httpx  # noqa: E402

from anyuse.core.result import AgentResult, ResultStatus  # noqa: E402
from anyuse.execution import model_adapter as M  # noqa: E402
from anyuse.execution.verifier import GoalVerifier, VerificationResult  # noqa: E402


def _adapter(replies):
    def handler(_request):
        text = replies.pop(0)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    cfg = M.ModelConfig("local", "http://127.0.0.1:9/v1", "fake", "", max_tokens=100)
    return M.ModelAdapter(cfg, transport=httpx.MockTransport(handler), budget=M.RunBudget(1.0))


# --------------------------------------------------------------------- the one counter

def test_every_adapter_request_counts_once_at_the_process_counter():
    ad = _adapter(["a", "b"])
    mark = M.process_call_count()
    assert ad.complete([{"role": "user", "content": "x"}]).ok
    assert ad.complete([{"role": "user", "content": "x"}]).ok
    assert M.calls_since(mark) == 2 == ad.call_count


def test_an_external_model_call_reports_to_the_same_counter():
    mark = M.process_call_count()
    n = M.note_external_model_call("claude-cli", "sonnet", feature="agent_harness", cost_usd=0.01)
    assert n == mark + 1 and M.calls_since(mark) == 1
    assert M.calls_since(mark + 5) == 0          # never negative


def test_the_verifier_reports_its_calls_from_the_counter_on_both_paths():
    good = '{"result": "success", "is_success": true, "confidence": 0.9, "explanation": "done"}'
    v = GoalVerifier(adapter=_adapter([good]))
    out = v.verify_goal(b"", "open the file")
    assert out.is_success and out.llm_calls == 1 and out.to_dict()["llm_calls"] == 1

    class _Resp:
        text = good

    class _Legacy:
        class models:
            @staticmethod
            def generate_content(**_kw):
                return _Resp()
    mark = M.process_call_count()
    v2 = GoalVerifier(adapter=_adapter([]), gemini_client=_Legacy())
    out2 = v2.verify_goal(b"", "open the file")
    assert out2.llm_calls == 1 and M.calls_since(mark) == 1

    class _Boom:
        def complete(self, *_a, **_k):
            raise RuntimeError("no provider")
        config = type("C", (), {"model": "x"})()
    out3 = GoalVerifier(adapter=_Boom()).verify_goal(b"", "goal")
    assert out3.result == "uncertain" and out3.llm_calls == 0


# -------------------------------------------------------------------- no assumed success

class _Orch:
    def __init__(self, response):
        self.response, self.history = response, [1, 2]

    def run(self, _task):
        return self.response


class _Verifier:
    def __init__(self, result="success", conf=0.9, calls=1, raise_=False):
        self.r, self.c, self.calls, self.raise_ = result, conf, calls, raise_

    def verify_goal(self, **_kw):
        if self.raise_:
            raise RuntimeError("provider down")
        for _ in range(self.calls):
            M.note_external_model_call("fake", "fake", feature="verifier")
        return VerificationResult(is_success=self.r == "success", confidence=self.c, result=self.r,
                                  explanation="seen on screen")


def _agent(response, verifier):
    from anyuse.core.agent import Agent
    a = Agent(enable_planning=False, enable_memory=False, enable_credentials=False,
              enable_verification=verifier is not None, auto_learn=False, auto_replay=False)
    a._orchestrator = _Orch(response)
    a._verifier = verifier
    a._screenshot_fn = lambda: b"png"
    return a


def test_a_confident_verifier_decides_and_its_call_is_counted():
    r = _agent("Task complete: file opened", _Verifier("success", 0.9)).run("open the file")
    assert r.status is ResultStatus.SUCCESS and r.success and r.llm_calls == 1
    assert "success (p=0.90)" in r.outcome_reason
    r = _agent("Task complete: file opened", _Verifier("failed", 0.8)).run("open the file")
    assert r.status is ResultStatus.FAILED and not r.success and r.llm_calls == 1


def test_an_undecided_verdict_is_uncertain_never_success():
    for v in (_Verifier("success", 0.4), _Verifier("partial", 0.95), _Verifier("uncertain", 0.99),
              _Verifier(raise_=True)):
        r = _agent("Task complete: file opened", v).run("open the file")
        assert r.status is ResultStatus.UNCERTAIN and not r.success, r
        assert r.error == r.outcome_reason and r.outcome_reason, r
        assert r.to_dict()["status"] == "uncertain" and r.to_dict()["outcome_reason"]
    r = _agent("Task complete: file opened", _Verifier(raise_=True)).run("open the file")
    assert "verifier failed" in r.outcome_reason and r.llm_calls == 0


def test_without_a_verifier_the_outcome_is_uncertain_not_assumed():
    r = _agent("Task complete: file opened", None).run("open the file")
    assert r.status is ResultStatus.UNCERTAIN and not r.success and "no goal verifier" in r.outcome_reason
    assert r.llm_calls == 0
    # the back-compat helper never assumes either
    assert _agent("Task complete", None)._is_success("Task complete", "t") is False


def test_structural_failures_need_no_model_call():
    for text in ("Error: actuation failed", "maximum turns reached", "Agent terminated",
                 "did things. Task incomplete", ""):
        r = _agent(text, _Verifier("success", 0.99)).run("open the file")
        assert r.status is ResultStatus.FAILED and r.llm_calls == 0, (text, r)


def test_no_screenshot_means_uncertain():
    a = _agent("Task complete", _Verifier("success", 0.99))

    def no_shot():
        raise RuntimeError("no screen in this process")
    a._screenshot_fn = no_shot
    r = a.run("open the file")
    assert r.status is ResultStatus.UNCERTAIN and "no screenshot" in r.outcome_reason and r.llm_calls == 0


def test_an_exception_in_the_run_still_reports_counted_calls():
    class _Bad:
        history = []

        def run(self, _t):
            M.note_external_model_call("fake", "fake")
            raise ValueError("boom")
    a = _agent("x", None)
    a._orchestrator = _Bad()
    r = a.run("t")
    assert r.status is ResultStatus.FAILED and r.llm_calls == 1 and "boom" in r.outcome_reason
    assert isinstance(r, AgentResult)



# --------------------------------------------------- the verifier's input, the run's report

class _SpyVerifier(_Verifier):
    def __init__(self, result="success", conf=0.9):
        super().__init__(result, conf)
        self.asked = []

    def verify_goal(self, **kw):
        self.asked.append(kw)
        return super().verify_goal(**kw)


def test_n15_the_verifier_judges_the_screen_not_the_agents_claim():
    # a verifier handed "Agent response: ... AnzUse" accepted the wrong text
    v = _SpyVerifier()
    r = _agent("Task complete: typed AnzUse into the document", v).run("type AnyUse")
    assert r.success and len(v.asked) == 1
    assert v.asked[0]["goal"] == "type AnyUse" and "AnzUse" not in repr(v.asked[0])
    # and a done chess move was rejected because the computer had answered it
    assert "may have reacted after the last action" in v.asked[0]["current_state"]


def test_n15_the_save_gate_and_the_result_share_one_verdict():
    class _Gated(_Orch):
        """The orchestrator's _finalize: the goal check runs before a finished run is saved."""
        goal_check = None

        def run(self, task):
            self.saved = self.goal_check(task, self.response)[0]
            return self.response
    for verdict, saved, status in (("failed", False, ResultStatus.FAILED), ("success", True, ResultStatus.SUCCESS)):
        v = _SpyVerifier(verdict, 0.9)
        a = _agent("Task complete: moved e2-e4", v)
        a._orchestrator = _Gated("Task complete: moved e2-e4")
        a._orchestrator.goal_check = a._goal_check_for_save        # what the orchestrator property wires
        r = a.run("move the white pawn from e2 to e4")
        assert a._orchestrator.saved is saved and r.status is status
        assert len(v.asked) == 1 and r.llm_calls == 1               # one verdict, not one per question
    # a check that is not decisive saves nothing either
    a = _agent("Task complete", _SpyVerifier("partial", 0.95))
    a._orchestrator = _Gated("Task complete")
    a._orchestrator.goal_check = a._goal_check_for_save
    r = a.run("t")
    assert a._orchestrator.saved is False and r.status is ResultStatus.UNCERTAIN


def test_n21_an_exception_reports_its_type_and_the_real_duration():
    import time

    class _Bad:
        history = []

        def run(self, _t):
            time.sleep(0.02)
            raise KeyError("key")
    a = _agent("x", None)
    a._orchestrator = _Bad()
    r = a.run("t")
    assert r.error.startswith("KeyError: ") and r.duration_seconds >= 0.02, (r.error, r.duration_seconds)

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
