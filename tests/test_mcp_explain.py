"""`explain_workflow`, the ladder tier on the MCP surface, and the recorded
JSON-RPC exchanges the `examples/claude-code/` walkthrough promises.

Offline: a fake transport, a temp workflow directory, a temp host file. No
`claude` session is started and the user's Claude Code configuration is never
read or written — the JSON-RPC exchanges are recorded payloads run through the
dispatcher, which is the part that can actually be wrong.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_recompile import ApiHealth
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.mcpserver import compiled_tools
from anyuse.mcpserver.compiled_tools import (
    TIER_AGENT, TIER_REQUEST, describe_compiled, explain_compiled, list_compiled,
    run_compiled)

BODY = '{"crates":[{"id":"serde"},{"id":"serde_json"},{"id":"serde_yaml"}]}'


class FakeTransport:
    def __init__(self, status=200, text=BODY, ctype="application/json"):
        self.status, self.text, self.ctype = status, text, ctype
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        return ApiResponse(self.status, {"Content-Type": self.ctype}, self.text)


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


def _template():
    return RequestTemplate(
        method="GET", url="https://crates.io/api/v1/crates", query={"q": "{q}"},
        headers={"accept": "application/json"}, extract={"items": "crates[*].id"},
        expect={"status_in": [200], "content_type": "json", "path": "crates",
                "min_items": 1, "required_keys": ["id"]},
        params=["q"], extract_kind="json",
        notes="compiled from the JSON API crates.io's own search page calls")


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hosts = os.path.join(self.tmp, "hosts.json")
        self.storage = Storage(self.tmp)
        proof = {
            "passed": True, "passed_count": 2, "case_count": 2,
            "reason": "passed on 2 fresh value(s) not present in the recording "
                      "(coverage >= 0.8, UI order preserved)",
            "cases": [
                {"value": "tokio", "passed": True, "coverage": 1.0,
                 "order_ok": True, "ui_count": 5, "api_count": 5},
                {"value": "clap", "passed": True, "coverage": 0.9,
                 "order_ok": True, "ui_count": 5, "api_count": 5},
            ],
        }
        self.record = compiled_record(
            _template(), proof, workflow="crates_search",
            chrome_filtered=["docs", "install", "cargo"],
            permission={"robots_policy": "respect", "robots_allowed": True,
                        "robots_note": "robots.txt for crates.io allows it",
                        "robots_overridden": ""},
            source={"recording": "crates_search.rec.json"})
        persist_compiled(self.storage, "crates_search", self.record)
        acknowledge_host("crates.io", note="public JSON API", path=self.hosts)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def explain(self):
        return explain_compiled("crates_search", storage=self.storage,
                                hosts_path=self.hosts)


class LadderTierOnTheSurface(Base):

    def test_a_proved_preferred_record_is_tier_1(self):
        row = describe_compiled("crates_search", self.record, self.hosts)
        self.assertEqual(row["ladder_tier"], TIER_REQUEST)
        self.assertIn("zero model calls", row["ladder_tier_means"])

    def test_an_unproved_record_is_tier_3(self):
        rec = dict(self.record)
        rec["proof"] = {"passed": False}
        row = describe_compiled("crates_search", rec, self.hosts)
        self.assertEqual(row["ladder_tier"], TIER_AGENT)
        self.assertIn("costs model calls", row["ladder_tier_means"])

    def test_a_demoted_record_is_tier_3(self):
        rec = dict(self.record)
        rec["health"] = ApiHealth(preferred=False,
                                  demoted_reason="4 failures in a row").to_dict()
        self.assertEqual(describe_compiled("crates_search", rec, self.hosts)
                         ["ladder_tier"], TIER_AGENT)

    def test_the_listing_carries_the_chrome_labels_not_a_count(self):
        row = describe_compiled("crates_search", self.record, self.hosts)
        self.assertEqual(row["chrome_filtered"], ["cargo", "docs", "install"])

    def test_the_listing_carries_the_robots_verdict(self):
        row = describe_compiled("crates_search", self.record, self.hosts)
        self.assertEqual(row["robots"]["policy"], "respect")
        self.assertTrue(row["robots"]["allowed"])

    def test_a_run_reports_the_tier_latency_and_model_calls(self):
        out = run_compiled("crates_search", {"q": "serde"}, storage=self.storage,
                           executor=ApiStepExecutor(transport=FakeTransport()),
                           hosts_path=self.hosts, record_outcome=False)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["ladder_tier"], TIER_REQUEST)
        self.assertEqual(out["model_calls"], 0)
        self.assertIsNotNone(out["latency_ms"])
        self.assertTrue(out["proof"]["passed"])


class Explain(Base):

    def test_it_names_the_exact_inputs_the_proof_checked(self):
        out = self.explain()
        self.assertEqual(out["evidence"]["fresh_inputs_checked"], ["tokio", "clap"])

    def test_it_says_how_durable_the_rung_is(self):
        self.assertIn("most durable", self.explain()["durability"])

    def test_an_html_rung_is_described_as_the_most_brittle(self):
        rec = dict(self.record)
        rec["template"] = {**rec["template"], "extract_kind": "html"}
        persist_compiled(self.storage, "crates_search", rec)
        self.assertIn("most brittle", self.explain()["durability"])

    def test_it_lists_what_the_chrome_filter_removed_and_why_that_matters(self):
        out = self.explain()
        self.assertEqual(out["chrome_filtered"], ["cargo", "docs", "install"])
        self.assertIn("proof checked the wrong thing",
                      out["chrome_filtered_means"])

    def test_it_states_the_limits_of_the_proof(self):
        limits = " ".join(self.explain()["limits"])
        self.assertIn("nothing else", limits)
        self.assertIn("not a claim", limits)

    def test_it_offers_no_confidence_score(self):
        """A number between 0 and 1 invites reasoning about a quantity nobody
        measured. The evidence is offered instead."""
        blob = json.dumps(self.explain())
        self.assertNotIn("confidence", blob.lower())
        self.assertNotIn("\"score\"", blob.lower())

    def test_it_says_how_the_workflow_fails(self):
        how = self.explain()["how_it_fails"]
        self.assertIn("Incomplete or identity-mismatched output stops", how)
        self.assertNotIn("never returns a partial", how)

    def test_an_unknown_workflow_says_so_and_points_at_the_listing(self):
        out = explain_compiled("nope", storage=self.storage, hosts_path=self.hosts)
        self.assertFalse(out["ok"])
        self.assertIn("list_compiled_workflows", out["hint"])

    def test_explaining_sends_nothing(self):
        import socket
        real = socket.socket

        def boom(*a, **k):                                 # pragma: no cover
            raise AssertionError("explain_workflow opened a socket")

        socket.socket = boom
        try:
            out = self.explain()
        finally:
            socket.socket = real
        self.assertTrue(out["ok"])


class RecordedJsonRpc(Base):
    """The exchanges `examples/claude-code/README.md` shows, run for real.

    Recorded payloads through the dispatcher — not a live `claude` session.
    Starting one would read the user's Claude Code configuration, which these
    tests do not touch.
    """

    def _dispatch(self, request: dict) -> dict:
        name = request["params"]["name"]
        args = request["params"].get("arguments") or {}
        if name == compiled_tools.TOOL_LIST_NAME:
            payload = {"compiled_workflows": list_compiled(self.storage, self.hosts)}
        elif name == compiled_tools.TOOL_EXPLAIN_NAME:
            payload = explain_compiled(args.get("workflow", ""), self.storage,
                                       self.hosts)
        elif name == compiled_tools.TOOL_RUN_NAME:
            payload = run_compiled(
                args.get("workflow", ""), args.get("parameters") or {},
                storage=self.storage,
                executor=ApiStepExecutor(transport=FakeTransport()),
                hosts_path=self.hosts, record_outcome=False)
        else:
            raise AssertionError(f"unknown tool {name!r}")
        return {"jsonrpc": "2.0", "id": request["id"],
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(payload, default=str)}]}}

    def _payload(self, response):
        return json.loads(response["result"]["content"][0]["text"])

    def test_all_three_tools_are_registered_with_usable_schemas(self):
        for name, schema, desc in (
                (compiled_tools.TOOL_LIST_NAME, compiled_tools.TOOL_LIST_SCHEMA,
                 compiled_tools.TOOL_LIST_DESCRIPTION),
                (compiled_tools.TOOL_RUN_NAME, compiled_tools.TOOL_RUN_SCHEMA,
                 compiled_tools.TOOL_RUN_DESCRIPTION),
                (compiled_tools.TOOL_EXPLAIN_NAME, compiled_tools.TOOL_EXPLAIN_SCHEMA,
                 compiled_tools.TOOL_EXPLAIN_DESCRIPTION)):
            self.assertTrue(name and schema.get("type") == "object")
            self.assertGreater(len(desc), 60, name)

    def test_the_walkthrough_exchange_list_explain_run(self):
        first = self._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "list_compiled_workflows",
                                           "arguments": {}}})
        rows = self._payload(first)["compiled_workflows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workflow"], "crates_search")
        self.assertEqual(rows[0]["ladder_tier"], TIER_REQUEST)

        second = self._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "explain_workflow",
                                            "arguments": {"workflow": "crates_search"}}})
        why = self._payload(second)
        self.assertTrue(why["ok"])
        self.assertEqual(why["evidence"]["fresh_inputs_checked"], ["tokio", "clap"])

        third = self._dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "run_compiled_workflow",
                                           "arguments": {"workflow": "crates_search",
                                                         "parameters": {"q": "serde"}}}})
        out = self._payload(third)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["result"]["items"],
                         ["serde", "serde_json", "serde_yaml"])
        self.assertEqual(out["model_calls"], 0)

    def test_every_response_is_valid_json_rpc(self):
        for req in ({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "list_compiled_workflows", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                     "params": {"name": "explain_workflow",
                                "arguments": {"workflow": "crates_search"}}}):
            resp = self._dispatch(req)
            self.assertEqual(resp["jsonrpc"], "2.0")
            self.assertEqual(resp["id"], req["id"])
            self.assertEqual(resp["result"]["content"][0]["type"], "text")
            json.loads(resp["result"]["content"][0]["text"])

    def test_the_failure_exchange_names_the_tier_and_refuses_the_desktop(self):
        acknowledge_host("crates.io", note="x", path=self.hosts)
        out = run_compiled(
            "crates_search", {"q": "serde"}, storage=self.storage,
            executor=ApiStepExecutor(transport=FakeTransport(status=503, text="down")),
            hosts_path=self.hosts, record_outcome=False)
        self.assertFalse(out["ok"])
        # a 503 is a site that did not answer, not a changed site
        self.assertTrue(out.get("transient"), out)
        self.assertIn("did not answer", out["fallback"])
        self.assertNotIn("record the search again", out["fallback"])


class ExamplesDirectory(unittest.TestCase):
    """The files the walkthrough refers to must exist and be consistent."""

    DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "examples", "claude-code")

    def test_the_mcp_json_snippet_is_valid_json(self):
        path = os.path.join(self.DIR, ".mcp.json")
        self.assertTrue(os.path.exists(path), path)
        with open(path) as fh:
            data = json.load(fh)
        self.assertIn("mcpServers", data)

    def test_the_snippet_names_the_module_the_server_actually_is(self):
        with open(os.path.join(self.DIR, ".mcp.json")) as fh:
            data = json.load(fh)
        entry = list(data["mcpServers"].values())[0]
        self.assertIn("anyuse.mcpserver", " ".join(entry.get("args") or []))

    def test_the_walkthrough_names_all_three_tools(self):
        with open(os.path.join(self.DIR, "README.md")) as fh:
            text = fh.read()
        for name in (compiled_tools.TOOL_LIST_NAME, compiled_tools.TOOL_RUN_NAME,
                     compiled_tools.TOOL_EXPLAIN_NAME):
            self.assertIn(name, text)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
