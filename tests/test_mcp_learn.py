"""The MCP learning surface: propose and explicitly delegated confirm.

Milestone 7. Everything is injected — a fake agent, a fake navigator, a fake
transport, a temp candidate directory and a fake endpoint check — so this file
opens no socket, starts no browser and makes no model call. The property under
test is the boundary: nothing here can reach the desktop, nothing is persisted
without an explicit confirmation that restates the URL, and the preview a
caller confirms is the preview the result echoes.
"""
from __future__ import annotations

import sys
import tempfile
import unittest

from anyuse.mcpserver import learn_tools
from test_learn_loop import (REGEX, SERDE, TOKIO, DeadTransport, FakeAgent,
                             FakeNavigator, executor_for)

OK_ENDPOINT = lambda: {"ok": True, "endpoint": "http://192.168.64.5:9222"}   # noqa: E731
LOOPBACK = lambda: {"ok": False, "error": "http://127.0.0.1:9222 is a loopback endpoint",   # noqa: E731
                    "endpoint": "http://127.0.0.1:9222"}


class Propose(unittest.TestCase):

    def test_propose_without_an_explicit_store_writes_nothing(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(learn_tools.CANDIDATE_STORE_ENV, None)
            out = learn_tools.propose(
                {"task": "search for {q}", "url": "https://ex.test/", "value": "serde"},
                agent=FakeAgent([SERDE]), endpoint_check=OK_ENDPOINT)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["saved"])
        self.assertEqual(out["persisted"], "nothing")

    def test_explicit_store_retains_candidate_but_no_runnable_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = learn_tools.propose({"task": "search for {q}", "url": "https://ex.test/",
                                       "value": "serde"}, agent=FakeAgent([SERDE]),
                                      data_dir=tmp, endpoint_check=OK_ENDPOINT)
            self.assertTrue(out["ok"])
            self.assertEqual(out["candidate"]["final_url"], "https://ex.test/search?q=serde")
            self.assertEqual(out["candidate"]["preview"][:1], ["serde v1"])
            self.assertIn("candidate only", out["persisted"])
            self.assertIn("CLI/human", out["next"])
            self.assertIn("not exposed", out["next"])
            self.assertTrue(out["saved"].startswith(tmp))

    def test_propose_refuses_a_loopback_endpoint_before_running_anything(self):
        agent = FakeAgent([SERDE])
        out = learn_tools.propose({"task": "t {q}", "url": "https://ex.test/",
                                   "value": "serde"}, agent=agent,
                                  endpoint_check=LOOPBACK)
        self.assertFalse(out["ok"])
        self.assertIn("loopback", out["error"])
        self.assertEqual(agent.calls, [])

    def test_propose_refuses_a_non_https_origin(self):
        out = learn_tools.propose({"task": "t {q}", "url": "http://ex.test/",
                                   "value": "serde"}, agent=FakeAgent([SERDE]),
                                  endpoint_check=OK_ENDPOINT)
        self.assertFalse(out["ok"])
        self.assertIn("https", out["error"])


class Confirm(unittest.TestCase):

    def _proposed(self, tmp):
        out = learn_tools.propose({"task": "search for {q}", "url": "https://ex.test/",
                                   "value": "serde"}, agent=FakeAgent([SERDE]),
                                  data_dir=tmp, endpoint_check=OK_ENDPOINT)
        return out["candidate"]

    def test_confirm_needs_the_url_restated_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            cand = self._proposed(tmp)
            for bad in ("", "https://ex.test/", "https://ex.test/search?q=regex"):
                out = learn_tools.confirm({"candidate_id": cand["candidate_id"], "url": bad,
                                           "values": ["regex", "tokio"],
                                           "delegated_confirmation": True},
                                          agent=FakeAgent([]), navigator=FakeNavigator([]),
                                          data_dir=tmp, endpoint_check=OK_ENDPOINT)
                self.assertFalse(out["ok"], bad)
                self.assertFalse(out["confirmed"])
                self.assertIn("not confirmed", out["error"])
                self.assertEqual(out["persisted"], "nothing")
                self.assertEqual(out["candidate"]["final_url"], cand["final_url"],
                                 "the refusal echoes the preview it refused")

    def test_confirm_continues_with_substitution_and_echoes_the_preview(self):
        persisted = {}

        def persist(compiled, proof, recordings, result):
            persisted["by"] = result.confirmed_by
            return "compiled_api.json"

        with tempfile.TemporaryDirectory() as tmp:
            cand = self._proposed(tmp)
            agent, nav = FakeAgent([]), FakeNavigator([REGEX, TOKIO])
            out = learn_tools.confirm({"candidate_id": cand["candidate_id"],
                                       "url": cand["final_url"],
                                       "values": ["regex", "tokio"],
                                       "delegated_confirmation": True},
                                      agent=agent, navigator=nav,
                                      executor=executor_for([SERDE, REGEX, TOKIO]),
                                      replay=None, persist=persist, data_dir=tmp,
                                      endpoint_check=OK_ENDPOINT,
                                      proof_preflight=lambda _compiled: "")
            self.assertTrue(out["ok"], out.get("reason"))
            self.assertTrue(out["confirmed"])
            self.assertEqual(out["confirmed_by"], "mcp")
            self.assertEqual(out["outcome"], "tier1")
            self.assertEqual(out["candidate"], cand, "the same preview, echoed")
            self.assertEqual(agent.calls, [], "the agent did not run again")
            self.assertEqual(len(nav.urls), 2)
            self.assertEqual(out["cost"]["agent_runs"], 1)
            self.assertEqual(out["cost"]["navigations"], 2)
            self.assertEqual(out["persisted"], "compiled_api.json")
            self.assertEqual(persisted["by"], "mcp")
            self.assertEqual(out["later_run_model_calls"], 0)
            # the candidate is consumed: a second confirmation finds nothing
            again = learn_tools.confirm({"candidate_id": cand["candidate_id"],
                                         "url": cand["final_url"],
                                         "values": ["regex", "tokio"],
                                         "delegated_confirmation": True},
                                        agent=agent, navigator=nav, data_dir=tmp,
                                        endpoint_check=OK_ENDPOINT)
            self.assertFalse(again["ok"])
            self.assertIn("no candidate", again["error"])

    def test_confirm_with_fewer_than_two_further_values_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cand = self._proposed(tmp)
            out = learn_tools.confirm({"candidate_id": cand["candidate_id"],
                                       "url": cand["final_url"], "values": ["serde", "regex"],
                                       "delegated_confirmation": True},
                                      agent=FakeAgent([]), navigator=FakeNavigator([]),
                                      data_dir=tmp, endpoint_check=OK_ENDPOINT)
            self.assertFalse(out["ok"])
            self.assertIn("two further values", out["error"])

    def test_confirm_refuses_a_loopback_endpoint_when_it_must_start_a_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            cand = self._proposed(tmp)
            out = learn_tools.confirm({"candidate_id": cand["candidate_id"],
                                       "url": cand["final_url"], "values": ["regex", "tokio"],
                                       "delegated_confirmation": True},
                                      data_dir=tmp, endpoint_check=LOOPBACK)
            self.assertFalse(out["ok"])
            self.assertIn("loopback", out["error"])
            self.assertEqual(out["persisted"], "nothing")

    def test_nonpersistent_tier2_is_not_success_and_keeps_the_candidate(self):
        from anyuse.execution.api_step import ApiStepExecutor
        from anyuse.execution.html_extract import extract
        from anyuse.execution.learn_loop import load_candidate

        pages = {REGEX.value: REGEX.dom, TOKIO.value: TOKIO.dom}

        class Replay:
            def execute(self, template, params):
                return {"success": True, "elapsed_ms": 10.0,
                        "extracted": {"items": extract(
                            pages[params["q"]], "ul.results li.row", 40)}}

        with tempfile.TemporaryDirectory() as tmp:
            cand = self._proposed(tmp)
            out = learn_tools.confirm(
                {"candidate_id": cand["candidate_id"], "url": cand["final_url"],
                 "values": ["regex", "tokio"], "delegated_confirmation": True},
                agent=FakeAgent([]), navigator=FakeNavigator([REGEX, TOKIO]),
                executor=ApiStepExecutor(transport=DeadTransport()), replay=Replay(),
                persist=lambda *_args: None, data_dir=tmp,
                endpoint_check=OK_ENDPOINT, proof_preflight=lambda _compiled: "")
            self.assertFalse(out["ok"])
            self.assertEqual(out["outcome"], "tier2_nonpersistent")
            self.assertTrue(out["candidate_retained"])
            self.assertIsNotNone(load_candidate(cand["candidate_id"], tmp))


class Registration(unittest.TestCase):

    def test_mcp_confirmation_is_default_off(self):
        out = learn_tools.confirm({"candidate_id": "deadbeef0000", "url": "https://x.test",
                                   "values": ["a", "b"]})
        self.assertFalse(out["ok"])
        self.assertIn("default-off", out["error"])

    def test_handle_ignores_tools_that_are_not_ours(self):
        self.assertIsNone(learn_tools.handle("desktop_execute", {}))

    def test_agent_self_confirmation_is_not_dispatched_over_mcp(self):
        self.assertIsNone(learn_tools.handle(learn_tools.TOOL_CONFIRM_NAME, {
            "candidate_id": "deadbeef0000", "url": "https://x.test",
            "values": ["a", "b"], "delegated_confirmation": True,
        }))

    def test_schemas_require_what_the_boundary_needs(self):
        self.assertEqual(learn_tools.TOOL_CONFIRM_SCHEMA["required"],
                         ["candidate_id", "url", "values"])
        self.assertEqual(learn_tools.TOOL_PROPOSE_SCHEMA["required"], ["task", "url", "value"])
        self.assertIn("writes nothing unless", learn_tools.TOOL_PROPOSE_DESCRIPTION)
        self.assertIn("default off", learn_tools.TOOL_CONFIRM_DESCRIPTION)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
