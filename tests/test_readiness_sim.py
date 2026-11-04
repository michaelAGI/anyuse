"""The browser replay's readiness settle on simulated result pages.

The pages come from ``tests/bench/readiness_bench.py``: each is a timeline on a
fake clock that releases the protocol events Chrome would send and answers the
readiness probe and the DOM read as the page stands at that instant. Every read
goes through ``CdpDriver._settle_and_read`` and ``ReplayExecutor.execute``.
The modes include search-as-you-type recordings (``typeahead``,
``typeahead+enter``) and inputs with fewer results than the recordings
(``few``); the ``ready-prev`` arm is the rule that waits for the recorded
request and result counts. Offline: no socket, no browser, no real waiting.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench"))

import readiness_bench as B  # noqa: E402

SEEDS = range(4)


def replays(arm, mode, words=B.WORDS, seeds=SEEDS, tick=None):
    tmpl = B.templates(mode, with_old=True)[arm]
    return [B.replay_case(tmpl, mode, w, s, tick=tick) for w in words for s in seeds]


class ReadinessOnSimulatedPages(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANYUSE_TIER2_READINESS", None)

    def test_a_page_filled_by_one_request_is_read_whole_and_early(self):
        for r in replays("ready", "xhr"):
            self.assertTrue(r["success"] and not r["early"], r)
            self.assertEqual(r["how"], "ready", r)
            # the poll that sees the list, the one that starts the stability
            # window, the window itself and the poll that ends it
            self.assertLessEqual(r["t_ms"], r["done_ms"] + 400 + 3 * B.POLL_MS, r)
            self.assertLess(r["t_ms"], 5000, r)

    def test_a_lazy_second_page_is_never_served_as_a_partial_answer(self):
        rows = replays("ready", "lazy")
        self.assertEqual([r for r in rows if r["wrong_success"]], [])
        for r in rows:
            self.assertTrue((not r["early"]) or r["t_ms"] >= B.CEILING_MS, r)
        # the first request alone was enough for the earlier rule
        old = replays("ready-old", "lazy")
        self.assertTrue(any(r["wrong_success"] for r in old))

    def test_thirty_rows_in_two_batches_are_not_read_at_fifteen(self):
        rows = replays("ready", "big")
        self.assertEqual([r for r in rows if r["n_read"] == 15], [])
        self.assertTrue(all(r["success"] and r["n_read"] == 30 for r in rows))
        self.assertTrue(all(r["how"] == "ready" for r in rows))

    def test_a_request_that_never_answers_is_read_at_the_ceiling(self):
        for r in replays("ready", "never", words=B.WORDS[:3]):
            self.assertEqual(r["how"], "ceiling", r)
            self.assertLessEqual(abs(r["t_ms"] - B.CEILING_MS), B.POLL_MS, r)
            self.assertFalse(r["success"], r)

    def test_a_ticking_clock_still_counts_as_stable(self):
        page = B.SimPage("amber", "xhr", 1, tick=True)
        self.assertNotEqual(page.html(5.0), page.html(6.0))
        for r in replays("ready", "xhr", words=B.WORDS[:4], tick=True):
            self.assertEqual(r["how"], "ready", r)
            self.assertTrue(r["success"] and not r["early"], r)

    def test_a_type_ahead_recording_is_read_once_the_search_call_answered(self):
        for r in replays("ready", "typeahead"):
            self.assertTrue(r["success"] and not r["early"], r)
            self.assertEqual(r["how"], "ready", r)
            self.assertLessEqual(r["t_ms"], r["done_ms"] + 400 + 3 * B.POLL_MS, r)

    def test_the_previous_rule_reads_a_type_ahead_page_at_the_ceiling(self):
        # one recorded call per prefix, and a suggestion list the replay never asks for
        for r in replays("ready-prev", "typeahead", words=B.WORDS[:3]):
            self.assertEqual(r["how"], "ceiling", r)
            self.assertLessEqual(abs(r["t_ms"] - B.CEILING_MS), B.POLL_MS, r)

    def test_type_ahead_previews_before_the_result_page_are_not_counted(self):
        for r in replays("ready", "typeahead+enter"):
            self.assertEqual(r["how"], "ready", r)
            self.assertLess(r["t_ms"], 5000, r)
            self.assertFalse(r["wrong_success"], r)
        # the suggestion and preview patterns are never waited for, the result call once
        tmpl = B.templates("typeahead+enter")["ready"]
        self.assertEqual(tmpl["readiness"].get("value_calls"), [0, 0, 1])

    def test_a_short_or_empty_answer_is_read_without_the_recorded_count(self):
        rows = replays("ready", "few")
        self.assertEqual({r["n_full"] for r in rows}, {0, 1, 2, 6})
        for r in rows:
            self.assertTrue(r["success"] and r["n_read"] == r["n_full"], r)
            self.assertLess(r["t_ms"], 5000, r)
        self.assertEqual([r for r in rows if r["wrong_success"]], [])
        # the statement the empty page makes is one the replay accepts
        from anyuse.execution import result_contract as RC
        page = B.SimPage("birch", "few", 0)
        page.now = 9.0
        phrase, why = RC.empty_evidence_page(page.html(9.0), "birch",
                                             text=B.visible_text(page.html(9.0)))
        self.assertTrue(phrase, why)

    def test_a_compiled_tool_reads_like_the_readiness_arm(self):
        compiled = B.templates("lazy")["compiled"]
        self.assertEqual(compiled["readiness"]["fallback"], "timer")
        self.assertEqual(replays("compiled", "lazy", words=B.WORDS[:4]),
                         replays("ready", "lazy", words=B.WORDS[:4]))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
