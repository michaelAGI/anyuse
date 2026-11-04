"""Run-wide reserve-before-send model budget, using exact-loopback fakes."""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from unittest import mock

from anyuse.execution.agent_browser import EscalatingAgent, EscalationLevel
from anyuse.execution.discover import model_ranker
from anyuse.execution.judge import LlmJudge
from anyuse.execution.model_adapter import (
    DEFAULT_RUN_BUDGET_USD,
    ModelAdapter,
    RunBudget,
)
from test_agent_escalation import Session, SOLVE, TYPE, fake_models
from test_model_adapter import _answer, _server


class AdapterBudgetTests(unittest.TestCase):
    def test_success_reserves_maximum_then_settles_to_actual_usage(self):
        budget = RunBudget(0.005)
        with _server(_answer("ok", prompt=10, completion=5)) as (adapter, state):
            adapter.set_budget(budget, price_in=2.0, price_out=4.0)
            result = adapter.complete([{"role": "user", "content": "x"}], max_tokens=100)
        self.assertTrue(result.ok)
        self.assertEqual(len(state.requests), 1)
        self.assertAlmostEqual(budget.spent_usd, (10 * 2 + 5 * 4) / 1_000_000)
        self.assertEqual(budget.reserved_usd, 0.0)

    def test_each_adapter_retry_needs_a_fresh_reservation(self):
        # Reserve the actual input envelope plus a 50-token output.
        maximum = (ModelAdapter._input_token_bound([{"role": "user", "content": "x"}], minimum=50) + 50) / 1_000_000
        budget = RunBudget(maximum)
        with _server({"status": 429}, _answer("must not be sent"), retries=2,
                     sleeper=lambda _seconds: None) as (adapter, state):
            adapter.set_budget(budget, price_in=1.0, price_out=1.0)
            result = adapter.complete([{"role": "user", "content": "x"}], max_tokens=50)
        self.assertFalse(result.ok)
        self.assertTrue(result.budget_exhausted)
        self.assertEqual(result.llm_calls, 1)
        self.assertEqual(len(state.requests), 1, "the 429 retry was refused before send")
        self.assertAlmostEqual(budget.spent_usd, maximum)
        self.assertEqual(budget.refused_requests, 1)

    def test_environment_default_is_two_cents_and_override_is_per_object(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANYUSE_RUN_BUDGET_USD", None)
            self.assertEqual(RunBudget.from_env().limit_usd, DEFAULT_RUN_BUDGET_USD)
            os.environ["ANYUSE_RUN_BUDGET_USD"] = "0.007"
            self.assertEqual(RunBudget.from_env().limit_usd, 0.007)

    def test_non_finite_budget_values_are_rejected(self):
        for value in ("nan", "inf", "-0.1"):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"ANYUSE_RUN_BUDGET_USD": value}):
                with self.assertRaises(ValueError):
                    RunBudget.from_env()

    def test_role_adapter_gives_judge_and_ranker_calls_a_budget_by_default(self):
        adapter = ModelAdapter.for_role("judge", provider="local",
                                        base_url="http://127.0.0.1:1/v1", model="fake")
        self.assertEqual(adapter.budget.limit_usd, DEFAULT_RUN_BUDGET_USD)
        self.assertGreater(adapter.price_in + adapter.price_out, 0)

    def test_judge_and_ranker_calls_are_refused_before_send(self):
        with _server(_answer('{"choice":"opt0","confidence":0.9}')) as (adapter, state):
            adapter.set_budget(RunBudget(0.00001), price_in=1.0, price_out=1.0)
            verdict = LlmJudge(adapter=adapter, seed=0).triage({"screen": "changed"})
            order, _cost = model_ranker(adapter=adapter)(
                "search", "example.test", [{"template": "/search?q={q}"}])
        self.assertFalse(verdict.answered)
        self.assertIn("budget exhausted", verdict.reason)
        self.assertEqual(order, [])
        self.assertEqual(state.requests, [])


class EscalationBudgetTests(unittest.TestCase):
    def test_levels_share_one_budget_and_later_level_is_never_called(self):
        # Each L1 maximum is 50 * ($0.25 + $0.69) / 1M = $0.000047.
        # Two observed calls fit after settlement; the third reservation does not.
        budget = RunBudget(0.0001)
        levels = [EscalationLevel("L1", "agent", max_tokens=50),
                  EscalationLevel("L2", "agent_l2", max_tokens=50)]
        with fake_models({"L1": [TYPE, TYPE, TYPE], "L2": SOLVE}) as (brains, state):
            run = EscalatingAgent(
                levels, endpoint="http://192.168.64.5:9222", session_factory=Session,
                brains=brains, pause_s=0, run_budget=budget).run(
                    "search for {q}", "https://ex.test/", "serde")
        self.assertEqual(run.stop, "budget_exhausted")
        self.assertEqual(run.escalation["stopped"], "budget_exhausted")
        self.assertEqual(state["L2"]["requests"], [])
        self.assertGreaterEqual(run.escalation["run_budget"]["refused_requests"], 1)

    def test_uncredentialed_levels_are_pruned_and_logged_once_before_run(self):
        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        levels = [EscalationLevel("L1", "agent"),
                  EscalationLevel("L2", "agent_l2"),
                  EscalationLevel("L3", "agent_l3")]
        agent = EscalatingAgent(levels, brains={"L1": lambda _messages: ({}, {}, False)})
        handler = Capture()
        logger = logging.getLogger("anyuse.execution.agent_browser")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                    "ANYUSE_MODEL_ENV_FILE": os.path.join(tmp, "missing.env")}, clear=False):
                os.environ.pop("OPENROUTER_API_KEY", None)
                self.assertEqual([level.name for level in agent._available_levels()], ["L1"])
                self.assertEqual([level.name for level in agent._available_levels()], ["L1"])
        finally:
            logger.setLevel(old_level)
            logger.removeHandler(handler)
        notices = [message for message in messages if "pruned unavailable" in message]
        self.assertEqual(len(notices), 1)
        self.assertIn("L2, L3", notices[0])


if __name__ == "__main__":
    unittest.main()
