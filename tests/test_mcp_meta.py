"""ANYUSE_MCP_PROFILE=meta: four fixed tools over the default profile's run paths.

Offline: fake workflow storage in a temp dir, a fake HTTP transport for the
compiled web rung, a fake checked executor and approver for compiled desktop
tools. Covers:

* the measurement in docs/MCP_META.md: tool count and tools/list bytes at 0, 5
  and 50 compiled workflows under ``default`` and ``meta``;
* list_sites / list_operations rows, ``{columns, rows}`` shaping;
* every failure class and its ``next`` hint;
* call_operation reaching the SAME gates, with the same arguments and in the
  same order, as ``run_compiled_workflow``, the typed ``<host>__<workflow>``
  tools and ``desktop__<workflow>`` (recording gates wrap the real ones);
* shell quoting of names and hosts in ``next``, one malformed record not
  breaking the listings, and validation failing closed without ``jsonschema``;
* the settings registry: ``meta`` resolves and needs no typed confirmation.

Every Fixture points ANYUSE_DATA_DIR at its temp dir and replaces
``notify.notify_card`` with a recorder, so an inbox card stays in the temp dir
and no notification leaves the process even outside the guarded runner.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from anyuse import settings
from anyuse.execution import api_compile, api_permission, api_step, hil_gate, notify
from anyuse.execution.executor import StateGraphExecutor
from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.mcpserver import compiled_tools, desktop_compiled_tools, meta_tools
from anyuse.mcpserver.server import DEFAULT_STATIC_TOOLS, create_server, mcp_profile

import test_launch_mcp_profile as launch
import test_routine_example as rex

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(HERE, "docs", "MCP_META.md")

#: tools/list measured with N compiled workflows (docs/MCP_META.md, "Measurement").
#: scenario -> N -> (default tools, default bytes, meta tools, meta bytes,
#: bytes of the meta list_operations result for the workflows' one site).
#: The bytes are how this SDK version serialises the listing; under another
#: version only the invariants are asserted.
MEASURED_SDK = "1.30.0"
MEASURED = {
    "web": {0: (9, 6721, 4, 1767, 0), 5: (14, 11441, 4, 1767, 2545),
            50: (59, 53921, 4, 1767, 24956)},
    "desktop": {0: (9, 6721, 4, 1767, 0), 5: (14, 9631, 4, 1767, 1929),
                50: (59, 35821, 4, 1767, 18805)},
}


async def _listed(server):
    return (await server.request_handlers[ListToolsRequest](ListToolsRequest())).root


async def _called(server, name, arguments=None):
    req = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
    return (await server.request_handlers[CallToolRequest](req)).root


def listed(server):
    return asyncio.run(_listed(server))


def call(server, name, arguments=None):
    """A meta tool's JSON payload (or any tool's first text block)."""
    result = asyncio.run(_called(server, name, arguments))
    return json.loads(result.content[0].text)


def tool_list_size(server):
    """(tool count, bytes of the tools/list result as the SDK serialises it)."""
    result = listed(server)
    text = result.model_dump_json(by_alias=True, exclude_none=True)
    return len(result.tools), len(text.encode("utf-8"))


def doc_row(scenario, n, measured):
    d_tools, d_bytes, m_tools, m_bytes, ops_bytes = measured
    return (f"| {scenario} | {n} | {d_tools} | {d_bytes} | {m_tools} | {m_bytes} "
            f"| {ops_bytes if n else '-'} |")


#: what the real transport raises before anything is sent to a destination the pre-send policy refuses
POLICY_REFUSAL = ("pre-send policy refused request: nonpublic destination '10.0.0.7' is refused by default")
#: what it raises when the site answered with a redirect to an origin off the allowlist
REDIRECT_REFUSAL = ("pre-send policy refused a redirect: origin 'https://1.1.1.1' is not in the exact-origin "
                    "allowlist")


class ListTransport:
    """Search answers with records (one lacks a field); 500 for 'boom', a changed shape for 'gone',
    the pre-send policy's refusal for 'walled', a refused redirect for 'moved'."""

    def __init__(self):
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        if "q=boom" in url:
            return ApiResponse(500, {"Content-Type": "application/json"}, "{}")
        if "q=gone" in url:
            return ApiResponse(200, {"Content-Type": "application/json"}, '{"packages": []}')
        if "q=walled" in url:
            raise PermissionError(POLICY_REFUSAL)
        if "q=moved" in url:
            raise PermissionError(REDIRECT_REFUSAL)
        if "q=slow" in url:
            return ApiResponse(429, {"Content-Type": "application/json",
                                     "Retry-After": "120"}, "{}")
        body = {"crates": [{"id": "serde", "downloads": 10},
                           {"id": "serde_json", "downloads": 7},
                           {"id": "serde_yaml"}]}
        return ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(body))


def records_template():
    return RequestTemplate(
        method="GET", url="https://crates.io/api/v1/crates", query={"q": "{q}"},
        headers={"accept": "application/json"}, extract={"items": "crates[*]"},
        expect={"status_in": [200], "content_type": "json", "path": "crates",
                "min_items": 1}, params=["q"])


class Fixture:
    """Two proved crates.io tools (rex), one record list tool, a checked record,
    and a desktop app with a read class and a file class."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        # inbox cards and the 429 host hold stay in this temp dir
        self.data = os.path.join(self.tmp, "data")
        self._env = mock.patch.dict(os.environ, {
            "ANYUSE_DATA_DIR": self.data,
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(self.tmp, "blocked.json"),
            "ANYUSE_NOTIFY_CHANNEL": "none"})
        self._env.start()
        self.notified = []

        def record_notice(card, **kwargs):
            self.notified.append(card)
            return {"sent": False}
        self._notify = mock.patch.object(notify, "notify_card", side_effect=record_notice)
        self._notify.start()
        self.storage, self.hosts = rex.setup(self.tmp)
        persist_compiled(self.storage, "crates_records", compiled_record(
            records_template(), dict(rex.PROOF, cases=[
                {"value": "tokio", "passed": True}, {"value": "rand", "passed": True}]),
            workflow="crates_records"))
        persist_compiled(self.storage, "crates_checked", compiled_record(
            rex.detail_template(), dict(rex.PROOF, status="checked (selection not held out)",
                                        selection_held_out=False),
            workflow="crates_checked"))
        self.desktop = launch.Storage(os.path.join(self.tmp, "desktop"))
        launch.record(self.desktop, "read_setting", "read_value")
        launch.record(self.desktop, "write_file", "file_write")
        self.skills = os.path.join(self.tmp, "skills")
        os.makedirs(self.skills)
        self.transport = ListTransport()
        self.executor = launch.CheckedExecutorFake()
        self.approvals = []

    def close(self):
        self._notify.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, profile, *, approve=None, desktop_executor=None, transport=None):
        approver = None
        if approve is not None:
            async def approver(context):
                self.approvals.append(context)
                return approve
        return create_server(
            profile=profile, compiled_storage=self.storage,
            compiled_hosts_path=self.hosts, desktop_storage=self.desktop,
            desktop_executor=desktop_executor or self.executor,
            desktop_approver=approver, skills_dir=self.skills,
            compiled_executor=ApiStepExecutor(transport=transport or self.transport))


class GateRecorder:
    """Wraps real gate functions; records calls made while ``run_compiled`` (or
    the desktop typed-tool runner) is on the stack. ``answers`` overrides a gate."""

    #: (module, function, label, what to log). A run rewrites the record's
    #: health, so the proof pin is logged by the digest it compares.
    WEB_GATES = (
        (api_permission, "host_acknowledged", "host", lambda a, k: a),
        (hil_gate, "classify_step", "hil", lambda a, k: a),
        (api_compile, "proof_matches_template", "proof_pin",
         lambda a, k: ((a[0].get("proof") or {}).get("template_digest"),)),
        (api_step, "readonly_post_enabled", "readonly_post", lambda a, k: a),
    )

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []
        self.runs = []
        self.active = 0
        self._patches = []

    def _spy(self, label, real, summary):
        def spy(*args, **kwargs):
            if self.active:
                self.calls.append((label, repr(summary(args, kwargs)),
                                   repr(sorted(kwargs.items()))))
            if label in self.answers:
                return self.answers[label]
            return real(*args, **kwargs)
        return spy

    def __enter__(self):
        for module, attr, label, summary in self.WEB_GATES:
            self._patches.append(mock.patch.object(
                module, attr, self._spy(label, getattr(module, attr), summary)))
        real_run = compiled_tools.run_compiled
        signature = inspect.signature(real_run)

        def run_spy(*args, **kwargs):
            # callers pass positionally or by keyword; log the bound values
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            a = bound.arguments
            self.runs.append(("run_compiled", a["name"], repr(sorted((a["params"] or {}).items())),
                              id(a["storage"]), type(a["executor"]).__name__, a["hosts_path"],
                              a["record_outcome"], a["require_proved"], a["post_card"]))
            self.active += 1
            try:
                return real_run(*args, **kwargs)
            finally:
                self.active -= 1
        self._patches.append(mock.patch.object(compiled_tools, "run_compiled", run_spy))
        real_desktop = desktop_compiled_tools.run_typed_tool

        async def desktop_spy(name, arguments=None, **kwargs):
            self.runs.append(("desktop", name, repr(sorted(dict(arguments or {}).items()))))
            self.active += 1
            try:
                return await real_desktop(name, arguments, **kwargs)
            finally:
                self.active -= 1
        self._patches.append(mock.patch.object(
            desktop_compiled_tools, "run_typed_tool", desktop_spy))
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()


class MeasurementTests(unittest.TestCase):
    def measure(self, scenario, n):
        tmp = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"ANYUSE_DATA_DIR": os.path.join(tmp, "data")})
        env.start()
        try:
            storage, hosts = launch.Storage(os.path.join(tmp, "wf")), os.path.join(tmp, "h.json")
            desktop = launch.Storage(os.path.join(tmp, "desktop"))
            skills = os.path.join(tmp, "skills")
            os.makedirs(skills)
            api_permission.acknowledge_host("crates.io", path=hosts)
            for i in range(n):
                if scenario == "web":
                    persist_compiled(storage, f"lookup_{i:03d}", compiled_record(
                        rex.detail_template(), rex.PROOF, workflow=f"lookup_{i:03d}"))
                else:
                    launch.record(desktop, f"read_{i:03d}", "read_value")
            out = []
            for profile in ("default", "meta"):
                server = create_server(
                    profile=profile, compiled_storage=storage, compiled_hosts_path=hosts,
                    desktop_storage=desktop, skills_dir=skills)
                out.extend(tool_list_size(server))
            site = "crates.io" if scenario == "web" else "Settings"
            result = asyncio.run(_called(server, "list_operations", {"site": site}))
            out.append(len(result.content[0].text.encode("utf-8")) if n else 0)
            return tuple(out)
        finally:
            env.stop()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_meta_tool_list_is_fixed_and_default_grows(self):
        got = {scenario: {n: self.measure(scenario, n) for n in rows}
               for scenario, rows in MEASURED.items()}
        for scenario, rows in got.items():
            for n, row in rows.items():
                print(doc_row(scenario, n, row))
            # the invariant, whatever the SDK: meta is the same 4 tools and the
            # same bytes at 0, 5 and 50, below default; default grows per workflow
            self.assertEqual({row[2:4] for row in rows.values()}, {(4, rows[0][3])})
            self.assertTrue(all(row[3] < row[1] for row in rows.values()), rows)
            self.assertEqual([rows[n][0] for n in (0, 5, 50)],
                             [len(DEFAULT_STATIC_TOOLS) + n for n in (0, 5, 50)])
            self.assertLess(rows[0][1], rows[5][1])
            self.assertLess(rows[5][1], rows[50][1])
        with open(DOC, encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn(f"MCP Python SDK {MEASURED_SDK}", doc)
        sdk = importlib.metadata.version("mcp")
        if sdk != MEASURED_SDK:
            print(f"mcp {sdk}: exact bytes are pinned for {MEASURED_SDK} only")
            return
        for scenario, rows in MEASURED.items():
            for n, expected in rows.items():
                self.assertEqual(got[scenario][n], expected, (scenario, n))
                self.assertIn(doc_row(scenario, n, expected), doc, (scenario, n))

    def test_meta_listing_reads_no_record(self):
        fx = Fixture()
        try:
            with mock.patch.object(meta_tools, "all_operations",
                                   side_effect=AssertionError("read records")), \
                    mock.patch.object(compiled_tools, "list_compiled",
                                      side_effect=AssertionError("read records")):
                names = [tool.name for tool in listed(fx.server("meta")).tools]
        finally:
            fx.close()
        self.assertEqual(names, list(meta_tools.TOOL_NAMES))


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.server = self.fx.server("meta")

    def tearDown(self):
        self.fx.close()

    def test_list_sites_counts_web_hosts_and_desktop_apps(self):
        with mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "on"}):
            out = call(self.server, "list_sites")
        self.assertEqual((out["ok"], out["count"]), (True, 2))
        self.assertEqual(out["sites"], [
            {"site": "Settings", "kinds": ["desktop"], "operations": 2, "ready": 2},
            {"site": "crates.io", "kinds": ["web"], "operations": 4, "ready": 3},
        ])
        with mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "off"}):
            out = call(self.server, "list_sites")
        self.assertEqual(out["sites"][0]["ready"], 0)

    def test_list_operations_carries_parameters_returns_proof_risk_approval(self):
        out = call(self.server, "list_operations", {"site": "crates.io"})
        ops = {row["operation"]: row for row in out["operations"]}
        self.assertEqual(set(ops), {"crates_search", "crates_detail", "crates_records",
                                    "crates_checked"})
        records = ops["crates_records"]
        self.assertEqual(records["parameters"][0]["name"], "q")
        self.assertEqual(records["parameters"][0]["type"], "string")
        self.assertEqual(records["parameters"][0]["example"], "tokio")
        self.assertIn("max_length", records["parameters"][0])
        self.assertEqual(records["returns"], {"shape": "list", "keys": ["items"]})
        self.assertEqual(records["proof"]["status"], "proved")
        self.assertEqual(records["risk_class"], "read_only")
        self.assertEqual((records["approval_needs"], records["ready"]), ([], True))
        detail = ops["crates_detail"]
        self.assertEqual(detail["returns"], {"shape": "fields",
                                             "keys": ["name", "description"]})
        checked = ops["crates_checked"]
        self.assertFalse(checked["ready"])
        self.assertIn("only 'proved' runs", checked["why_not"])
        self.assertNotIn("notes", json.dumps(out))

        with mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "on"}):
            desk = call(self.server, "list_operations", {"site": "Settings"})
        rows = {row["operation"]: row for row in desk["operations"]}
        self.assertEqual(rows["read_setting"]["parameters"],
                         [{"name": "label", "required": True, "type": "string"}])
        self.assertEqual((rows["read_setting"]["approval_needs"],
                          rows["read_setting"]["ready"]), ([], True))
        # a call started with its values asks nothing: no per-call need
        self.assertEqual(rows["write_file"]["approval_needs"], [])
        self.assertTrue(rows["write_file"]["ready"])
        self.assertEqual(rows["write_file"]["risk_class"], "file_write")
        with mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "off"}):
            desk = call(self.server, "list_operations", {"site": "Settings"})
        row = {r["operation"]: r for r in desk["operations"]}["read_setting"]
        self.assertFalse(row["ready"])
        self.assertIn("ANYUSE_DRIVE_ENABLED", row["why_not"])

    def test_unacknowledged_host_is_listed_with_its_approval_need(self):
        os.unlink(self.fx.hosts)
        ops = {row["operation"]: row for row in call(
            self.server, "list_operations", {"site": "crates.io"})["operations"]}
        self.assertIn("host_acknowledgement", ops["crates_search"]["approval_needs"])
        self.assertFalse(ops["crates_search"]["ready"])

    def test_unknown_site_points_at_list_sites(self):
        out = call(self.server, "list_operations", {"site": "nowhere.example"})
        self.assertEqual((out["ok"], out["class"], out["next"]),
                         (False, "unknown_site", "list_sites {}"))


class ServerInfoTests(unittest.TestCase):
    """serverInfo carries AnyUse's version and short instructions for both profiles."""

    def test_server_version_is_anyuses_own_and_instructions_are_set(self):
        from anyuse.mcpserver.server import anyuse_version
        for profile, words in (("meta", ("list_sites", "list_operations", "call_operation")),
                               ("default", ("<host>__<workflow>", "agent_run"))):
            opts = create_server(profile=profile).create_initialization_options()
            self.assertEqual(opts.server_version, anyuse_version())
            self.assertNotEqual(opts.server_version, importlib.metadata.version("mcp"))
            for word in (*words, "hand to the user (do not run it yourself)", "ANYUSE_MCP_AGENT_COMMITS"):
                self.assertIn(word, opts.instructions or "", (profile, word))

    def test_a_site_without_its_port_resolves_when_unambiguous(self):
        ops = [meta_tools.Operation(site="127.0.0.1:18555", name="fixture_once", kind="web", row={"n": 1}),
               meta_tools.Operation(site="shop.test", name="search", kind="web", row={"n": 2})]
        out = meta_tools.list_operations("127.0.0.1", ops)
        self.assertTrue(out["ok"])
        self.assertEqual(out["site"], "127.0.0.1:18555")
        self.assertEqual(meta_tools.list_operations("127.0.0.1:18555", ops)["count"], 1)
        two = ops + [meta_tools.Operation(site="127.0.0.1:9000", name="other", kind="web", row={})]
        self.assertEqual(meta_tools.list_operations("127.0.0.1", two)["class"], meta_tools.UNKNOWN_SITE)
        self.assertEqual(meta_tools.resolve_site("nope.test", ops), "nope.test")

    def test_latency_is_rounded_to_one_decimal(self):
        self.assertEqual(compiled_tools.round_ms(7.683375000056003), 7.7)
        self.assertEqual(compiled_tools.round_ms(17), 17)
        self.assertIsNone(compiled_tools.round_ms(None))
        self.assertIs(compiled_tools.round_ms(True), True)


class AgentRunOverMetaTests(unittest.TestCase):
    """The agent harness through call_operation on site ``agent``."""

    def call(self, payload):
        async def run_agent(args):
            return dict(payload)
        return asyncio.run(meta_tools.call_operation(
            "agent", "run", {"task": "x", "site": "example.org"},
            operations=[meta_tools.agent_operation()], run_web=None, run_desktop=None,
            run_agent=run_agent))

    def test_a_confirm_required_answer_keeps_its_question_and_resume_token(self):
        out = self.call({"ok": False, "outcome": "confirm_required", "reason": "not asked by the task",
                         "confirm_required": {"question": "confirm", "token": "TKN", "expires_in_s": 300},
                         "resume": {"token": "TKN", "expires_in_s": 300}})
        self.assertEqual(out["outcome"], "confirm_required")
        self.assertEqual(out["resume"]["token"], "TKN")
        self.assertEqual(out["confirm_required"]["token"], "TKN")
        self.assertIn('"resume": "<the token>"', out["next"])

    def test_a_human_required_stop_hands_the_step_to_the_user(self):
        reason = "hand to the user (do not run it yourself): step 0, the login step 'log in' on 'x'"
        out = self.call({"ok": False, "outcome": "human_required", "reason": reason})
        self.assertEqual(out["next"], reason)
        self.assertNotIn("resume", out)

    def test_the_agent_operation_lists_the_policy_choices(self):
        params = {p["name"]: p for p in meta_tools.agent_operation().row["parameters"]}
        self.assertIn("claude", params["policy"]["enum"])
        self.assertEqual(params["policy"]["default"], "model")


class ShapingTests(unittest.TestCase):
    def test_records_become_columns_and_rows_with_null_for_missing(self):
        shaped = meta_tools.shape_result({"items": [{"a": 1, "b": 2}, {"a": 3}, {"c": 4}],
                                          "total": 3})
        self.assertEqual(shaped, {"items": {"columns": ["a", "b", "c"],
                                            "rows": [[1, 2, None], [3, None, None],
                                                     [None, None, 4]]},
                                  "total": 3})

    def test_one_record_scalars_and_mixed_lists_are_left_alone(self):
        self.assertEqual(meta_tools.shape_result([{"a": 1}]), [{"a": 1}])
        self.assertEqual(meta_tools.shape_result({"items": ["x", "y"]}), {"items": ["x", "y"]})
        self.assertEqual(meta_tools.shape_result([{"a": 1}, "b"]), [{"a": 1}, "b"])
        self.assertEqual(meta_tools.shape_result([{"a": 1}, {"a": 2}]),
                         {"columns": ["a"], "rows": [[1], [2]]})

    def test_call_operation_returns_columns_rows_end_to_end(self):
        fx = Fixture()
        try:
            out = call(fx.server("meta"), "call_operation", {
                "site": "crates.io", "operation": "crates_records", "args": {"q": "serde"}})
        finally:
            fx.close()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["items"], {
            "columns": ["id", "downloads"],
            "rows": [["serde", 10], ["serde_json", 7], ["serde_yaml", None]]})
        self.assertEqual((out["served_by"], out["model_calls"]), ("api", 0))
        self.assertEqual(out["proof"]["status"], "proved")


class FailureClassTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def op(self, operation, args=None, site="crates.io", **server_kw):
        return call(self.fx.server("meta", **server_kw), "call_operation",
                    {"site": site, "operation": operation, "args": args or {}})

    def assertClass(self, out, cls, next_part):
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["class"], cls, out)
        self.assertIn(cls, meta_tools.FAILURE_CLASSES)
        self.assertTrue(out["reason"], out)
        self.assertIn(next_part, out["next"], out)

    def test_unknown_site_operation_and_input(self):
        self.assertClass(self.op("x", site="nowhere.example"), "unknown_site", "list_sites {}")
        self.assertClass(self.op("nope"), "unknown_operation",
                         'list_operations {"site": "crates.io"}')
        self.assertClass(self.op("crates_detail"), "input",
                         'list_operations {"site": "crates.io"}')
        self.assertClass(self.op("crates_detail", {"crate": "x", "extra": 1}), "input",
                         "list_operations")
        self.assertClass(self.op("crates_detail", {"crate": 7}), "input", "list_operations")
        self.assertEqual(self.fx.transport.sent, [])

    def test_not_proved_stale_demoted_and_host(self):
        self.assertClass(self.op("crates_checked", {"crate": "serde_json"}),
                         "not_proved", "hand to the user")
        def edit(workflow, change):
            path = os.path.join(self.fx.storage.get_workflow_dir(workflow),
                                api_compile.COMPILED_FILE)
            with open(path) as fh:
                record = json.load(fh)
            change(record)
            with open(path, "w") as fh:
                json.dump(record, fh)
        edit("crates_detail", lambda r: r["template"].update(timeout_s=3.0))
        self.assertClass(self.op("crates_detail", {"crate": "serde_json"}),
                         "stale_proof", "compile and prove")
        edit("crates_search", lambda r: r.update(health={
            "preferred": False, "consecutive_failures": 3,
            "demoted_reason": "3 failures in a row"}))
        # a web-only tool has no recorded path: recompiling is the way back
        self.assertClass(self.op("crates_search", {"q": "serde"}), "demoted",
                         "anyuse workflow compile crates_search")
        os.unlink(self.fx.hosts)
        out = self.op("crates_records", {"q": "serde"})
        self.assertClass(out, "host_not_acknowledged",
                         "anyuse workflow acknowledge-host crates.io")
        self.assertEqual(out["detail"]["unacknowledged_hosts"], ["crates.io"])
        self.assertEqual(self.fx.transport.sent, [])

    def test_hil_gate_refusal_is_needs_approval(self):
        with GateRecorder({"hil": "destructive"}) as gates:
            out = self.op("crates_records", {"q": "serde"})
        self.assertClass(out, "needs_approval", "never runs over MCP")
        self.assertEqual(out["detail"]["risk_class"], "destructive")
        self.assertTrue(any(label == "hil" for label, *_ in gates.calls))
        self.assertEqual(self.fx.transport.sent, [])

    def test_failed_run_and_rate_limit(self):
        # a changed response on a web-only tool, which has no recorded path to fall back to: record and recompile
        self.assertClass(self.op("crates_records", {"q": "gone"}), "failed",
                         "anyuse workflow compile crates_records")
        # a server error says nothing about the request: try again later, not record again. A fresh fixture:
        # three counted failures in a row take a compiled rung out of service
        self.fx.close()
        self.fx = Fixture()
        out = self.op("crates_records", {"q": "boom"})
        self.assertClass(out, "unavailable", "did not answer")
        self.assertIn("again later", out["next"])
        self.assertNotIn("record the search again", out["next"])
        self.assertNotIn("anyuse workflow compile", out["next"])
        # the pre-send policy refused the destination: nothing was sent, and the reason says why
        out = self.op("crates_records", {"q": "walled"})
        self.assertClass(out, "refused", "nothing was sent")
        self.assertIn("nonpublic destination", out["reason"])
        self.assertNotIn("record the search again", out["next"])
        self.assertNotIn("did not answer", out["next"])
        out = self.op("crates_records", {"q": "slow"})
        self.assertClass(out, "rate", "do not retry now")
        # the 429 posted an inbox card in the fixture's data dir; the user
        # notice went to the recorder, not to any channel
        self.assertEqual(len(self.fx.notified), 1)
        self.assertTrue(os.listdir(self.fx.data))

    def test_blocked_and_auth_from_the_run_payload(self):
        base = {"ok": False, "provenance": {}, "served_by": "none", "error": "blocked"}
        out = meta_tools._web_failure(
            {**base, "blocked": {"reason": "captcha_generic"},
             "needs_human": {"card": "c1", "kind": "challenge"}}, "crates.io", "x")
        self.assertEqual(out["class"], "blocked")
        self.assertIn("anyuse inbox do c1", out["next"])
        self.assertEqual(out["needs_human"]["card"], "c1")
        out = meta_tools._web_failure({**base, "blocked": {"reason": "login_wall"}},
                                      "crates.io", "x")
        self.assertEqual(out["class"], "auth")
        self.assertTrue(out["next"].startswith("hand to the user (do not run it yourself)"))
        self.assertIn("signs in to crates.io in their own browser, then retries", out["next"])
        self.assertNotIn("anyuse login", out["next"])

    def test_shell_commands_in_next_quote_names_hosts_and_cards(self):
        evil = "evil; rm -rf ~"
        persist_compiled(self.fx.storage, evil, compiled_record(
            rex.detail_template(), rex.PROOF, workflow=evil))
        path = os.path.join(self.fx.storage.get_workflow_dir(evil), api_compile.COMPILED_FILE)
        with open(path) as fh:
            record = json.load(fh)
        record["health"] = {"preferred": False, "demoted_reason": "failures"}
        with open(path, "w") as fh:
            json.dump(record, fh)
        out = self.op(evil, {"crate": "serde"})
        self.assertEqual(out["class"], "demoted")

        def command(text):
            return shlex.split(re.search(r"`(anyuse [^`]*)`", text).group(1))
        self.assertEqual(command(out["next"]), ["anyuse", "workflow", "compile", evil])
        base = {"ok": False, "provenance": {}, "error": "x"}
        out = meta_tools._web_failure({**base, "unacknowledged_hosts": [evil]},
                                      "crates.io", "op")
        self.assertEqual(command(out["next"]), ["anyuse", "workflow", "acknowledge-host", evil])
        self.assertIn("the user runs", out["next"])
        self.assertIn("in their own terminal", out["next"])
        self.assertNotIn("terms", out["next"])
        out = meta_tools._web_failure(
            {**base, "served_by": "none", "blocked": {"reason": "captcha_generic"},
             "needs_human": {"card": evil}}, "crates.io", "op")
        self.assertEqual(command(out["next"]), ["anyuse", "inbox", "do", evil])
        out = meta_tools._desktop_failure({"ok": False, "blocked": True, "error": "no"},
                                          "Settings", evil)
        self.assertEqual(command(out["next"]), ["anyuse", "workflow", "play", evil])
        out = meta_tools._web_failure({**base, "served_by": "api"}, "crates.io", evil)
        self.assertEqual(command(out["next"]), ["anyuse", "workflow", "play", evil])

    def test_one_malformed_record_is_skipped_not_fatal(self):
        persist_compiled(self.fx.storage, "odd_cases", compiled_record(
            rex.detail_template(), dict(rex.PROOF, cases=5), workflow="odd_cases"))
        # the default listing still describes it; only meta's own row building trips
        self.assertIn("odd_cases", [row["workflow"] for row in compiled_tools.list_compiled(
            self.fx.storage, self.fx.hosts)])
        server = self.fx.server("meta")
        sites = call(server, "list_sites")
        self.assertTrue(sites["ok"], sites)
        ops = call(server, "list_operations", {"site": "crates.io"})
        self.assertEqual(sorted(row["operation"] for row in ops["operations"]),
                         ["crates_checked", "crates_detail", "crates_records", "crates_search"])
        self.assertTrue(self.op("crates_records", {"q": "serde"})["ok"])
        self.assertClass(self.op("odd_cases", {"crate": "x"}), "unknown_operation",
                         "list_operations")

    def test_missing_jsonschema_fails_closed(self):
        with self.assertRaises(meta_tools.ValidatorUnavailable), \
                mock.patch.dict(sys.modules, {"jsonschema": None}):
            meta_tools.validate_args({"q": "x"}, {"type": "object", "properties": {
                "q": {"type": "string"}}, "required": ["q"]})
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            out = self.op("crates_records", {"q": "serde"})
        self.assertClass(out, "error", "install the mcp extra")
        self.assertIn("nothing ran", out["reason"])
        self.assertEqual(self.fx.transport.sent, [])

    def test_desktop_classes(self):
        # a file class started with its values asks nothing
        out = self.op("write_file", {"label": "x"}, site="Settings", approve=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.fx.approvals, len(self.fx.executor.calls)), ([], 1))
        # an approval level that holds steps is not run over MCP: a typed needs_approval
        launch.record(self.fx.desktop, "write_held", "file_write", extra={"approval_level": "all"})
        self.assertClass(self.op("write_held", {"label": "x"}, site="Settings"),
                         "needs_approval", "approval level")
        self.assertEqual(len(self.fx.executor.calls), 1)
        rows = {r["operation"]: r for r in call(self.fx.server("meta"), "list_operations",
                                                {"site": "Settings"})["operations"]}
        self.assertEqual(rows["write_held"]["approval_needs"], ["not_over_mcp"])
        self.assertFalse(rows["write_held"]["ready"])
        self.assertIn("approval level (all)", rows["write_held"]["why_not"])

        class DriveOff(launch.CheckedExecutorFake):
            async def execute_checked_step(self, step, **kw):
                self.calls.append(step)
                return ({"success": False,
                         "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)"}, {})
        self.assertClass(self.op("read_setting", {"label": "Version"}, site="Settings",
                                 desktop_executor=DriveOff()), "drive_disabled",
                         "ANYUSE_DRIVE_ENABLED")
        self.assertClass(self.op("read_setting", {}, site="Settings"), "input",
                         'list_operations {"site": "Settings"}')

    def test_login_changes_nothing_and_hands_the_cli_step_to_the_user(self):
        def tree():
            out = {}
            for root, _dirs, files in os.walk(self.fx.tmp):
                for name in files:
                    with open(os.path.join(root, name), "rb") as fh:
                        out[os.path.join(root, name)] = fh.read()
            return out
        before = tree()
        out = call(self.fx.server("meta"), "login", {"site": "crates.io"})
        self.assertEqual(tree(), before)
        self.assertEqual(self.fx.transport.sent, [])
        self.assertEqual((out["ok"], out["class"], out["known_site"]),
                         (False, "needs_user", True))
        self.assertTrue(out["next"].startswith("hand to the user (do not run it yourself)"))
        self.assertIn("own browser, then retries", out["next"])
        self.assertNotIn("anyuse login", out["next"])

    def test_listings_and_login_without_injected_storage_create_nothing(self):
        data = os.path.join(self.fx.tmp, "fresh-data")
        with mock.patch.dict(os.environ, {"ANYUSE_DATA_DIR": data}):
            server = create_server(profile="meta", skills_dir=self.fx.skills)
            self.assertEqual(call(server, "list_sites"), {"ok": True, "sites": [], "count": 0})
            self.assertEqual(call(server, "login", {"site": "x"})["known_site"], False)
            self.assertEqual(call(server, "list_operations", {"site": "x"})["class"],
                             "unknown_site")
        self.assertFalse(os.path.exists(data))


class SameGatesTests(unittest.TestCase):
    """call_operation and the existing tools reach the same gates in the same order.

    Web: the real host-acknowledgement, HIL, proof-pin and read-only POST
    functions are wrapped and their calls inside ``run_compiled`` recorded, for
    ``run_compiled_workflow``, the typed ``<host>__<workflow>`` tool and
    ``call_operation``.

    Desktop, two levels. With ``launch.CheckedExecutorFake`` the comparison
    covers only what reaches the executor (step, index, workflow, graph) and the
    approver's context; the fake has no drive or drift gate. The last case uses
    the real ``StateGraphExecutor`` with a fake AX tree and a recording
    platform, so the executor's own ``ANYUSE_DRIVE_ENABLED`` gate and its drift
    evaluation run on both paths.
    """

    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def run_both(self, default_call, meta_call, **server_kw):
        logs = []
        for profile, (name, args) in (("default", default_call), ("meta", meta_call)):
            with GateRecorder() as gates:
                result = asyncio.run(_called(self.fx.server(profile, **server_kw), name, args))
            try:
                payload = json.loads(result.content[0].text)
            except ValueError:
                raise AssertionError(result.content[0].text[:600])
            logs.append((gates.runs, gates.calls, payload))
        return logs

    def test_web_run_compiled_workflow_and_call_operation(self):
        (d_runs, d_calls, d_out), (m_runs, m_calls, m_out) = self.run_both(
            (compiled_tools.TOOL_RUN_NAME,
             {"workflow": "crates_records", "parameters": {"q": "serde"}}),
            ("call_operation", {"site": "crates.io", "operation": "crates_records",
                                "args": {"q": "serde"}}))
        self.assertTrue(d_out["ok"] and m_out["ok"], (d_out, m_out))
        self.assertEqual(d_runs, m_runs)
        self.assertTrue(m_runs[0][7], "require_proved")
        self.assertEqual(d_calls, m_calls)
        labels = [label for label, *_ in m_calls]
        for gate in ("host", "hil", "proof_pin"):
            self.assertIn(gate, labels)
        self.assertEqual(len(self.fx.transport.sent), 2)

    def test_web_typed_tool_and_call_operation(self):
        # crates_search extracts a list of strings: the typed tool's legacy
        # outputSchema (no proof-time schema in this fixture) admits no object
        # items, so crates_records would fail the SDK's output check there
        typed = compiled_tools.typed_tool_name("crates.io", "crates_search")
        (d_runs, d_calls, d_out), (m_runs, m_calls, m_out) = self.run_both(
            (typed, {"q": "serde"}),
            ("call_operation", {"site": "crates.io", "operation": "crates_search",
                                "args": {"q": "serde"}}))
        self.assertTrue(d_out["ok"] and m_out["ok"], (d_out, m_out))
        self.assertEqual(d_runs, m_runs)
        self.assertTrue(m_runs[0][7], "require_proved")
        self.assertEqual(d_calls, m_calls)
        self.assertEqual(len(self.fx.transport.sent), 2)

    def test_web_failures_give_the_same_next_step_on_both_profiles(self):
        # the typed tool returns run_compiled's payload, whose next step is "fallback"; call_operation's is "next"
        typed = compiled_tools.typed_tool_name("crates.io", "crates_search")
        for value, says, never in (("boom", "did not answer", "record the search again"),
                                   ("walled", "nothing was sent", "record the search again"),
                                   ("gone", "record the search again", "did not answer"),
                                   ("moved", "record the search again", "nothing was sent")):
            # a fresh fixture: three counted failures in a row take a compiled rung out of service
            self.fx.close()
            self.fx = Fixture()
            (_, _, d_out), (_, _, m_out) = self.run_both(
                (typed, {"q": value}),
                ("call_operation", {"site": "crates.io", "operation": "crates_search",
                                    "args": {"q": value}}))
            self.assertFalse(d_out["ok"] or m_out["ok"], (d_out, m_out))
            self.assertIn(says, d_out["fallback"], (value, d_out))
            self.assertIn(says, m_out["next"], (value, m_out))
            self.assertNotIn(never, d_out["fallback"], (value, d_out))
            self.assertNotIn(never, m_out["next"], (value, m_out))

    def test_web_refusal_reaches_the_same_gates(self):
        os.unlink(self.fx.hosts)
        (d_runs, d_calls, d_out), (m_runs, m_calls, m_out) = self.run_both(
            (compiled_tools.TOOL_RUN_NAME,
             {"workflow": "crates_records", "parameters": {"q": "serde"}}),
            ("call_operation", {"site": "crates.io", "operation": "crates_records",
                                "args": {"q": "serde"}}))
        self.assertEqual((d_runs, d_calls), (m_runs, m_calls))
        self.assertFalse(d_out["ok"])
        self.assertEqual(m_out["class"], "host_not_acknowledged")
        self.assertEqual(self.fx.transport.sent, [])

    def test_desktop_typed_tool_and_call_operation(self):
        (d_runs, _, d_out), (m_runs, _, m_out) = self.run_both(
            ("desktop__read_setting", {"label": "Version"}),
            ("call_operation", {"site": "Settings", "operation": "read_setting",
                                "args": {"label": "Version"}}))
        self.assertTrue(d_out["ok"] and m_out["ok"], (d_out, m_out))
        self.assertEqual(d_runs, m_runs)
        calls = [(step.command, idx, wf.name, type(graph).__name__)
                 for step, idx, wf, graph in self.fx.executor.calls]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][:3], ({"role": "AXStaticText", "title": "Version",
                                         "action": "read"}, 0, "read_setting"))

    def test_desktop_real_checked_executor_drive_and_drift_gates(self):
        class AxTree:
            rows = [{"role": "AXStaticText", "title": "Version"}]

            def find(self, role=None, title=None):
                return [r for r in self.rows if r["role"] == role
                        and (title is None or r["title"] == title)]

            def snapshot(self):
                raise RuntimeError("no AX snapshot in tests")

        class Platform:
            def __init__(self):
                self.verbs = []

            def perform_verb(self, target, verb, value=None):
                self.verbs.append((target["title"], verb, value))
                return True

        for drive in ("off", "on"):
            platform = Platform()
            executor = StateGraphExecutor(ax_tree=AxTree(), platform=platform,
                                          drift_gate_enabled=True)
            with mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": drive}):
                (d_runs, _, d_out), (m_runs, _, m_out) = self.run_both(
                    ("desktop__read_setting", {"label": "Version"}),
                    ("call_operation", {"site": "Settings", "operation": "read_setting",
                                        "args": {"label": "Version"}}),
                    desktop_executor=executor)
            self.assertEqual(d_runs, m_runs)
            d_step = d_out["steps"][0]
            m_step = (m_out["result"] if m_out["ok"] else m_out["steps"])[0]
            self.assertEqual(d_step["execution"], m_step["execution"])
            self.assertEqual(d_step["verification"].get("drift_gate"),
                             m_step["verification"].get("drift_gate"))
            self.assertIn("drift_gate", d_step["verification"])
            if drive == "off":
                self.assertFalse(d_out["ok"])
                self.assertIn("ANYUSE_DRIVE_ENABLED", d_step["execution"]["error"])
                self.assertEqual(m_out["class"], "drive_disabled")
                self.assertEqual(platform.verbs, [])
            else:
                self.assertTrue(d_out["ok"] and m_out["ok"], (d_out, m_out))
                # a "read" step is read, never
                # pressed; the platform branch used to send it as invoke (AXPress)
                self.assertEqual(platform.verbs, [])
                self.assertTrue(d_step["execution"].get("read_only"))

    def test_desktop_file_class_asks_nobody_in_either_profile(self):
        (d_runs, _, d_out), (m_runs, _, m_out) = self.run_both(
            ("desktop__write_file", {"label": "x"}),
            ("call_operation", {"site": "Settings", "operation": "write_file",
                                "args": {"label": "x"}}), approve=True)
        self.assertTrue(d_out["ok"] and m_out["ok"], (d_out, m_out))
        self.assertEqual(d_runs, m_runs)
        self.assertEqual(self.fx.approvals, [])                 # starting the call was the approval
        self.assertEqual(len(self.fx.executor.calls), 2)


class ProfileTests(unittest.TestCase):
    def test_meta_refuses_every_other_tool_by_name(self):
        fx = Fixture()
        try:
            server = fx.server("meta")
            for name in ("run_task", "run_compiled_workflow", "list_compiled_workflows",
                         "get_guide", "desktop_status", "desktop__read_setting",
                         compiled_tools.typed_tool_name("crates.io", "crates_detail")):
                out = call(server, name, {})
                self.assertFalse(out["success"], name)
                self.assertIn("not available in the 'meta' MCP profile", out["error"])
            default_names = {tool.name for tool in listed(fx.server("default")).tools}
            self.assertFalse(set(meta_tools.TOOL_NAMES) & default_names)
        finally:
            fx.close()
        self.assertEqual(fx.executor.calls, [])
        self.assertEqual(fx.transport.sent, [])

    def test_profile_resolution_and_settings_registry(self):
        self.assertEqual(mcp_profile("meta"), "meta")
        self.assertEqual(mcp_profile("META"), "meta")
        self.assertEqual(mcp_profile("full"), "full")
        self.assertEqual(mcp_profile("bogus"), "default")
        spec = settings.SETTINGS["ANYUSE_MCP_PROFILE"]
        self.assertIn("meta", spec.allowed_values)
        self.assertEqual(settings.parse_for_storage("MCP_PROFILE", "meta"),
                         ("ANYUSE_MCP_PROFILE", "meta"))
        self.assertFalse(spec.needs_confirmation("meta"))
        self.assertTrue(spec.needs_confirmation("full"))
        with open(os.path.join(HERE, "docs", "SETTINGS.md"), encoding="utf-8") as fh:
            table = fh.read()
        self.assertTrue(re.search(r"\| `ANYUSE_MCP_PROFILE` \* \| `default`, `meta`, `full`",
                                  table))

    def test_setting_meta_from_the_cli_asks_no_terminal_code(self):
        from click.testing import CliRunner
        from anyuse.cli import cli
        from anyuse.execution import human_gate
        tmp = tempfile.mkdtemp()
        saved = {k: os.environ.get(k) for k in ("ANYUSE_DATA_DIR", "ANYUSE_MCP_PROFILE")}
        try:
            os.environ["ANYUSE_DATA_DIR"] = tmp
            os.environ.pop("ANYUSE_MCP_PROFILE", None)
            gate = mock.Mock(return_value="refused: no person here")
            with mock.patch.object(human_gate, "TtyGate", return_value=gate):
                result = CliRunner().invoke(cli, ["settings", "set", "MCP_PROFILE", "meta"])
                self.assertEqual(result.exit_code, 0, result.output)
                gate.assert_not_called()
                self.assertEqual(mcp_profile(), "meta")
                result = CliRunner().invoke(cli, ["settings", "set", "MCP_PROFILE", "full"])
                self.assertNotEqual(result.exit_code, 0)
                gate.assert_called_once()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp, ignore_errors=True)


class DesktopRestorationResponse(unittest.TestCase):
    """Public desktop responses retain cleanup receipts; no desktop or model I/O."""

    def _execute(self, result, *, state_graph):
        from types import SimpleNamespace
        from anyuse.mcpserver import server
        agent = SimpleNamespace(run=mock.Mock(return_value=result), _compliance_checker=None)
        with mock.patch("anyuse.core.agent.Agent", return_value=agent), \
                mock.patch.object(server, "_try_state_graph_execution", new=mock.AsyncMock(
                    return_value=result if state_graph else None)):
            response = asyncio.run(server._desktop_execute({"task": "fixture task"}))
        return json.loads(response[0].text), agent

    def test_state_graph_response_keeps_primary_success_and_incomplete_cleanup(self):
        from anyuse.core.result import RestoreReport
        from anyuse.execution.orchestrator import OrchestratorResult
        report = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"},
                                           {"field": "frontmost_app", "status": "unknown"}])
        result = OrchestratorResult(success=True, message="Saved", restoration_report=report)
        response, agent = self._execute(result, state_graph=True)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"], "Saved")
        self.assertEqual(response["restoration"], report.to_dict())
        self.assertEqual(response["cleanup_status"], "partial")
        self.assertEqual(response["completion_status"], "incomplete")
        agent.run.assert_not_called()

    def test_agent_response_retains_cleanup_and_primary_failure(self):
        from anyuse.core.result import AgentResult, RestoreReport
        report = RestoreReport.unknown("original identity disappeared")
        result = AgentResult.failure_result("primary refusal", restoration_report=report)
        response, agent = self._execute(result, state_graph=False)
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["result"], result.message)
        self.assertEqual(response["restoration"], report.to_dict())
        self.assertEqual(response["completion_status"], "incomplete")
        agent.run.assert_called_once_with("fixture task")

    def test_ordinary_agent_route_reports_cleanup_not_requested(self):
        from anyuse.core.result import AgentResult
        response, agent = self._execute(AgentResult.success_result("done"), state_graph=False)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["cleanup_status"], "not_requested")
        self.assertEqual(response["restoration"]["fields"], [])
        self.assertEqual(response["completion_status"], "complete")

    def test_missing_task_reports_not_requested_without_starting_an_agent(self):
        from anyuse.mcpserver import server
        with mock.patch("anyuse.core.agent.Agent") as agent:
            response = json.loads(asyncio.run(server._desktop_execute({}))[0].text)
        agent.assert_not_called()
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["cleanup_status"], "not_requested")
        self.assertEqual(response["completion_status"], "incomplete")

    def test_plan_keeps_all_cleanup_receipts_including_earlier_failure(self):
        from types import SimpleNamespace
        from anyuse.core.result import AgentResult, RestoreReport
        from anyuse.mcpserver.plan_executor import PlanExecutor
        failed = RestoreReport.from_fields([{"field": "do_not_disturb", "status": "failed"}])
        restored = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"}])
        results = [AgentResult.success_result("one", restoration_report=failed),
                   AgentResult.success_result("two", restoration_report=restored)]
        agent = SimpleNamespace(run=mock.Mock(side_effect=results), _compliance_checker=None)
        executor = PlanExecutor(agent, {"steps": [{"index": 0, "instruction": "one"},
                                                 {"index": 1, "instruction": "two"}]})
        executor._capture_screenshot = mock.AsyncMock(return_value=None)
        result = asyncio.run(executor.execute())
        self.assertTrue(result.success)  # Existing primary plan outcome is preserved.
        response = result.to_dict()
        self.assertEqual(response["cleanup_status"], "partial")
        self.assertEqual(response["completion_status"], "incomplete")
        rows = response["restoration"]["fields"]
        self.assertEqual([row["execution_index"] for row in rows], [0, 1])
        self.assertEqual([row["restoration"] for row in rows], [failed.to_dict(), restored.to_dict()])

    def test_desktop_execute_plan_response_keeps_its_aggregate_cleanup(self):
        from types import SimpleNamespace
        from anyuse.core.result import RestoreReport
        from anyuse.mcpserver import server
        from anyuse.mcpserver.plan_executor import PlanExecutionResult
        report = RestoreReport.from_fields([{"field": "window_geometry", "status": "failed"}])
        result = PlanExecutionResult(success=True, message="Plan completed", restoration_report=report)
        executor = SimpleNamespace(execute=mock.AsyncMock(return_value=result))
        with mock.patch("anyuse.core.agent.Agent", return_value=SimpleNamespace()), \
                mock.patch("anyuse.mcpserver.plan_executor.PlanExecutor", return_value=executor):
            response = json.loads(asyncio.run(server._desktop_execute({
                "task": "fixture task", "plan": {"steps": [{"instruction": "one"}]}}))[0].text)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"], "Plan completed")
        self.assertEqual(response["restoration"], report.to_dict())
        self.assertEqual(response["cleanup_status"], "failed")
        self.assertEqual(response["completion_status"], "incomplete")

    def test_failed_plan_step_stops_before_checkpoint_or_another_execution(self):
        from types import SimpleNamespace
        from anyuse.core.result import AgentResult, RestoreReport
        from anyuse.mcpserver.plan_executor import PlanExecutor
        restored = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"}])
        for checkpoints in ([], [{"id": "must_not_evaluate"}]):
            with self.subTest(checkpoints=checkpoints):
                failed = AgentResult.failure_result("Replay interrupted", restoration_report=restored)
                agent = SimpleNamespace(run=mock.Mock(return_value=failed), _compliance_checker=None)
                executor = PlanExecutor(agent, {"checkpoint_callback_url": "https://fixture.invalid/check", "steps": [
                    {"index": 0, "instruction": "first", "checkpoints": checkpoints},
                    {"index": 1, "instruction": "must not execute"}]})
                executor._capture_screenshot = mock.AsyncMock()
                executor._evaluate_checkpoint = mock.AsyncMock()
                result = asyncio.run(executor.execute())
                response = result.to_dict()
                self.assertFalse(result.success)
                self.assertEqual(response["status"], "error")
                self.assertIn("Replay interrupted", result.message)
                self.assertEqual(response["steps_completed"], 0)
                self.assertEqual(response["cleanup_status"], "restored")
                self.assertEqual(response["completion_status"], "incomplete")
                self.assertEqual(response["restoration"]["fields"][0]["restoration"], restored.to_dict())
                agent.run.assert_called_once_with("first")
                executor._capture_screenshot.assert_not_awaited()
                executor._evaluate_checkpoint.assert_not_awaited()

    def test_plan_does_not_infer_task_success_from_a_cleanup_receipt(self):
        from types import SimpleNamespace
        from anyuse.core.result import RestoreReport
        from anyuse.mcpserver.plan_executor import PlanExecutor
        restored = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"}])
        agent = SimpleNamespace(run=mock.Mock(return_value=SimpleNamespace(
            restoration_report=restored)), _compliance_checker=None)
        for plan in ({"steps": [{"instruction": "first"}]}, {"task_text": "first"}):
            with self.subTest(plan=plan):
                executor = PlanExecutor(agent, plan)
                result = asyncio.run(executor.execute())
                self.assertFalse(result.success)
                self.assertEqual(result.to_dict()["cleanup_status"], "restored")
                self.assertEqual(result.to_dict()["completion_status"], "incomplete")

    def test_started_state_graph_exception_returns_terminal_failure_with_last_cleanup(self):
        from types import SimpleNamespace
        from anyuse.core.result import RestoreReport
        from anyuse.mcpserver import server
        for report in (RestoreReport.unknown("window closed"), None):
            with self.subTest(cleanup=report.status if report else "missing"):
                run = mock.AsyncMock(side_effect=ValueError("healing failed after action"))
                orchestrator = SimpleNamespace(run=run, last_restoration_report=report)
                storage = SimpleNamespace(find=lambda task: SimpleNamespace(id="fixture"),
                                          has_template=lambda name: True,
                                          get_template=lambda name: SimpleNamespace(required_app=""))
                agent = SimpleNamespace(run=mock.Mock(side_effect=AssertionError("must not repeat task")))
                with mock.patch("anyuse.core.agent.Agent", return_value=agent), \
                        mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", return_value=orchestrator), \
                        mock.patch("anyuse.memory.storage.WorkflowStorage", return_value=storage), \
                        mock.patch("anyuse.guides.storage.GuideStorage", return_value=object()):
                    response = json.loads(asyncio.run(server._desktop_execute({"task": "fixture task"}))[0].text)
                self.assertEqual(response["status"], "error")
                self.assertIn("ValueError: healing failed after action", response["result"])
                self.assertEqual(response["cleanup_status"], "unknown")
                self.assertEqual(response["completion_status"], "incomplete")
                if report is not None:
                    self.assertEqual(response["restoration"], report.to_dict())
                else:
                    self.assertIn("did not provide", response["restoration"]["fields"][0]["reason"])
                run.assert_awaited_once()
                agent.run.assert_not_called()

    def test_unavailable_state_graph_setup_can_fall_back_without_claiming_cleanup(self):
        from types import SimpleNamespace
        from anyuse.core.result import AgentResult
        from anyuse.mcpserver import server
        storage = SimpleNamespace(find=lambda task: SimpleNamespace(id="fixture"),
                                  has_template=lambda name: True,
                                  get_template=lambda name: SimpleNamespace(required_app=""))
        agent = SimpleNamespace(run=mock.Mock(return_value=AgentResult.success_result("fresh task")))
        with mock.patch("anyuse.core.agent.Agent", return_value=agent), \
                mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", side_effect=ImportError("optional engine unavailable")), \
                mock.patch("anyuse.memory.storage.WorkflowStorage", return_value=storage), \
                mock.patch("anyuse.guides.storage.GuideStorage", return_value=object()):
            response = json.loads(asyncio.run(server._desktop_execute({"task": "fixture task"}))[0].text)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"], "fresh task")
        self.assertEqual(response["cleanup_status"], "not_requested")
        agent.run.assert_called_once_with("fixture task")

    def test_plan_freeform_and_checkpoint_failure_preserve_received_cleanup(self):
        from types import SimpleNamespace
        from anyuse.core.result import AgentResult, RestoreReport
        from anyuse.mcpserver.plan_executor import CheckpointResult, PlanExecutor
        report = RestoreReport.unknown("window closed")
        agent = SimpleNamespace(run=mock.Mock(return_value=AgentResult.success_result(
            "done", restoration_report=report)), _compliance_checker=None)
        freeform = PlanExecutor(agent, {"task_text": "fixture task"})
        result = asyncio.run(freeform.execute())
        self.assertTrue(result.success)
        self.assertEqual(result.to_dict()["restoration"], report.to_dict())
        executor = PlanExecutor(agent, {"checkpoint_callback_url": "https://fixture.invalid/check",
                                        "steps": [{"instruction": "one", "checkpoints": [{"id": "cp"}]}]})
        executor._capture_screenshot = mock.AsyncMock(return_value=None)
        executor._evaluate_checkpoint = mock.AsyncMock(return_value=CheckpointResult("cp", False, "not observed"))
        stopped = asyncio.run(executor.execute())
        self.assertFalse(stopped.success)
        self.assertIn("Checkpoint cp failed", stopped.message)
        self.assertEqual(stopped.to_dict()["cleanup_status"], "unknown")
        self.assertEqual(stopped.to_dict()["restoration"]["fields"][0]["restoration"], report.to_dict())


class RegisteredReaTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}))
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.spec = {"name": "rea.example.pause", "registration": "example", "action": "pause",
                     "description": "Pause playback; verification model_judged",
                     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                     "contract": {"effect": "pause", "verification_tier": "model_judged"},
                     "verification_tier": "model_judged", "require_restoration": True,
                     "catalog_sha256": "a" * 64}
        self.payload = {"ok": False, "status": "uncertain", "verification_tier": "model_judged", "model_calls": 2,
                        "result": {"completion_status": "cleanup_incomplete", "restoration": {"receipt": {"kept": True}}},
                        "automatic_retry": False, "terminal_on_failure": True}
        self.store = mock.Mock()
        self.store.list_tools.side_effect = lambda: [dict(self.spec)]
        self.store.execute.side_effect = lambda *args, **kwargs: dict(self.payload)

    def server(self, profile):
        return create_server(profile=profile, compiled_storage=self.fx.storage, compiled_hosts_path=self.fx.hosts,
                             desktop_storage=self.fx.desktop, desktop_executor=self.fx.executor,
                             skills_dir=self.fx.skills, rea_store=self.store)

    def test_default_full_and_meta_preserve_the_same_rea_result(self):
        for profile in ("default", "full"):
            server = self.server(profile)
            spec = next(t for t in listed(server).tools if t.name == self.spec["name"])
            self.assertIn("model_judged", spec.description)
            self.assertEqual(call(server, self.spec["name"]), self.payload)
        meta = self.server("meta")
        self.assertEqual(len(listed(meta).tools), 4)
        sites = call(meta, "list_sites")
        self.assertIn("rea.example", [s["site"] for s in sites["sites"]])
        row = call(meta, "list_operations", {"site": "rea.example"})["operations"][0]
        self.assertEqual(row["operation"], "pause")
        self.assertEqual(row["verification_tier"], "model_judged")
        got = call(meta, "call_operation", {"site": "rea.example", "operation": "pause", "args": {}})
        self.assertEqual({key: got[key] for key in self.payload}, self.payload)
        self.assertEqual(self.store.execute.call_count, 3)

    def test_meta_wrong_arguments_do_not_dispatch(self):
        got = call(self.server("meta"), "call_operation", {
            "site": "rea.example", "operation": "pause", "args": {"invented": True}})
        self.assertFalse(got["ok"])
        self.store.execute.assert_not_called()

    def test_collision_with_static_or_desktop_tool_is_withheld_and_dispatches_neither(self):
        for name in ("agent.run", "desktop.read_setting"):
            self.spec["name"] = name
            for profile in ("default", "full"):
                server = self.server(profile)
                self.assertNotIn(name, {t.name for t in listed(server).tools})
                self.assertFalse(call(server, name)["ok"])
            meta = self.server("meta")
            self.assertFalse(call(meta, "call_operation", {
                "site": "rea.example", "operation": "pause", "args": {}})["ok"])
        self.store.execute.assert_not_called()
        self.assertFalse(self.fx.executor.calls)

    def test_python_agent_selects_rea_and_retains_failure_without_ui_fallback_or_recording(self):
        from anyuse.execution.agent_orchestrator import AgentOrchestrator
        from anyuse.core.agent import Agent
        from anyuse.core.result import ResultStatus
        from anyuse.execution.model_adapter import process_call_count
        executor, recorder, capture = mock.Mock(_compliance_checker=None), mock.Mock(), mock.Mock()
        capture.capture.return_value = b"inert-screenshot"
        orch = AgentOrchestrator(adapter=mock.Mock(), action_executor=executor, screenshot_capture=capture,
                                 rea_store=self.store, auto_confirm=True)
        decisions = iter([{"thought": "Pause using the saved action", "status": "continue", "action": "rea_action",
                           "name": self.spec["name"], "inputs": {}},
                          {"thought": "must not reach this UI fallback", "status": "continue", "action": "key", "keys": "space"}])
        orch._decide = lambda *args: next(decisions)
        message = orch.run("Pause playback", recorder=recorder)
        self.assertIn("REA stopped", message)
        self.assertEqual(orch.rea_executions[0]["result"], self.payload["result"])
        recorder.discard.assert_called_once()
        recorder.save.assert_not_called()
        recorder.record_action.assert_not_called()
        self.assertEqual(executor.execute.call_args_list, [mock.call("leave_failsafe_corner", {})])
        agent = Agent.__new__(Agent)
        agent._orchestrator = orch
        agent._decided = ("Pause playback", ResultStatus.SUCCESS, "a stale optimistic goal judgment")
        agent.max_turns = 20
        result = agent._result("Pause playback", message, 0, process_call_count(), 1)
        self.assertEqual(result.status, ResultStatus.UNCERTAIN)
        self.assertFalse(result.success)
        self.assertEqual(result.to_dict()["rea_executions"][0]["verification_tier"], "model_judged")

    def test_python_agent_success_keeps_tier_and_records_the_rea_step(self):
        from anyuse.execution.agent_orchestrator import AgentOrchestrator
        executor, recorder, capture = mock.Mock(_compliance_checker=None), mock.Mock(), mock.Mock()
        capture.capture.return_value = b"inert-screenshot"
        self.payload.update(ok=True, status="completed", result={"verdict": "model_judged", "verification_tier": "model_judged"})
        orch = AgentOrchestrator(adapter=mock.Mock(), action_executor=executor, screenshot_capture=capture,
                                 rea_store=self.store, auto_confirm=True)
        decisions = iter([{"status": "continue", "action": "rea_action", "name": self.spec["name"], "inputs": {}},
                          {"status": "done", "thought": "Pause accomplished"}])
        orch._decide = lambda *args: next(decisions)
        message = orch.run("Pause playback", recorder=recorder)
        self.assertIn("Pause accomplished", message)
        self.assertEqual(orch.rea_executions[0]["verification_tier"], "model_judged")
        self.assertIn('"model_judged"', orch._build_prompt("Pause playback"))
        recorder.record_rea_action.assert_called_once()
        recorder.discard.assert_not_called()
        recorder.save.assert_called_once()

    def test_mcp_agent_run_keeps_rea_results_in_both_profiles(self):
        expected = [self.payload]
        def run(task, **kwargs):
            return {"ok": False, "outcome": "uncertain", "reason": "REA cleanup incomplete",
                    "model_calls": 2, "rea_executions": expected}
        for profile in ("default", "meta"):
            server = create_server(profile=profile, compiled_storage=self.fx.storage,
                                   compiled_hosts_path=self.fx.hosts, desktop_storage=self.fx.desktop,
                                   desktop_executor=self.fx.executor, skills_dir=self.fx.skills,
                                   agent_runner=run, rea_store=self.store)
            got = (call(server, "agent.run", {"task": "Pause playback"}) if profile == "default" else
                   call(server, "call_operation", {"site": "agent", "operation": "run", "args": {"task": "Pause playback"}}))
            self.assertFalse(got["ok"])
            self.assertEqual(got["rea_executions"], expected)

    def test_python_agent_exception_retains_prior_rea_execution(self):
        from anyuse.core.agent import Agent
        agent = Agent.__new__(Agent)
        agent.enable_memory = False
        orch = mock.Mock()
        orch.rea_executions = []
        def run(task):
            orch.rea_executions.append(self.payload)
            raise RuntimeError("inert error after native execution")
        orch.run.side_effect = run
        agent._orchestrator = orch
        result = agent.run("Pause playback")
        self.assertFalse(result.success)
        self.assertIn("inert error", result.error)
        self.assertEqual(result.to_dict()["rea_executions"], [self.payload])


class DesktopBoundaryAndOwnershipTests(unittest.TestCase):
    def test_saved_graph_uses_the_agents_checker_and_refuses_blocked_app(self):
        from types import SimpleNamespace
        from anyuse.core.restriction import ComplianceChecker, RestrictionPolicy
        from anyuse.execution.actions import ActionExecutor
        from anyuse.execution.orchestrator import OrchestratorResult
        from anyuse.mcpserver import server
        actuator = ActionExecutor(backend=SimpleNamespace())
        actuator.set_compliance_checker(ComplianceChecker(RestrictionPolicy.from_boundary({"blocked_apps": ["Fixture"]})))
        template = SimpleNamespace(required_app="Fixture")
        storage = SimpleNamespace(find=lambda _: SimpleNamespace(id="fixture"), has_template=lambda _: True,
                                  get_template=lambda _: template)
        run = mock.AsyncMock(return_value=OrchestratorResult(success=True))
        with mock.patch("anyuse.memory.storage.WorkflowStorage", return_value=storage), \
                mock.patch("anyuse.guides.storage.GuideStorage", return_value=SimpleNamespace(get_by_app=lambda _: None)), \
                mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", return_value=SimpleNamespace(run=run)) as ctor:
            result = asyncio.run(server._try_state_graph_execution("fixture", action_executor=actuator))
            self.assertFalse(result.success)
            self.assertIn("BOUNDARY", result.message)
            ctor.assert_not_called()
            template.required_app = "Other"
            result = asyncio.run(server._try_state_graph_execution("fixture", action_executor=actuator))
            self.assertTrue(result.success)
            self.assertIs(ctor.call_args.kwargs["action_executor"], actuator)

    def test_checkpoint_without_evaluator_stops_before_any_action(self):
        from types import SimpleNamespace
        from anyuse.mcpserver.plan_executor import PlanExecutor
        agent = SimpleNamespace(run=mock.Mock())
        executor = PlanExecutor(agent, {"steps": [{"instruction": "one", "checkpoints": [{"id": "cp"}]}]})
        result = asyncio.run(executor.execute())
        self.assertFalse(result.success)
        self.assertIn("evaluator", result.message)
        agent.run.assert_not_called()

    def test_checkpoint_requires_boolean_passed_and_does_not_follow_redirects(self):
        from types import SimpleNamespace
        from anyuse.mcpserver.plan_executor import PlanExecutor
        executor = PlanExecutor(SimpleNamespace(), {"checkpoint_callback_url": "https://fixture.invalid/check"})
        for payload, expected in (({"passed": "false"}, False), ({"passed": 1}, False), ({}, False),
                                  ([], False), ({"passed": False}, False), ({"passed": True}, True)):
            response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
            client = mock.AsyncMock()
            client.__aenter__.return_value = client
            client.post.return_value = response
            with mock.patch("httpx.AsyncClient", return_value=client) as ctor:
                result = asyncio.run(executor._evaluate_checkpoint({"id": "cp"}, step_index=0))
            self.assertIs(result.passed, expected)
            self.assertIs(ctor.call_args.kwargs["follow_redirects"], False)

    def _exercise_owned_worker(self, *, cancel):
        import threading
        from types import SimpleNamespace
        from anyuse.core.result import AgentResult
        from anyuse.execution.actions import ActionExecutor
        from anyuse.mcpserver import server
        entered, release = threading.Event(), threading.Event()
        backend = SimpleNamespace(click_at=mock.Mock())
        actuator = ActionExecutor(backend=backend)
        agent = SimpleNamespace(orchestrator=SimpleNamespace(executor=actuator))
        late = []
        def run(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release worker")
            late.append(actuator.execute("click_at", {"x": 1, "y": 2}))
            return AgentResult.success_result("done")
        agent.run = mock.Mock(side_effect=run)
        async def scenario():
            request = asyncio.create_task(server._desktop_execute({"task": "fixture", "timeout": 1}))
            try:
                while not entered.is_set():
                    if request.done():
                        raise AssertionError(await request)
                    await asyncio.sleep(.001)
                if cancel:
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                else:
                    response = json.loads((await request)[0].text)
                    self.assertEqual(response["status"], "timeout")
                    self.assertIs(response["worker_running"], True)
                    self.assertIs(response["state_uncertain"], True)
                self.assertTrue(server._current_task["busy"])
                owner = server._current_task["task_id"]
                overlap = json.loads((await server._desktop_execute({"task": "other"}))[0].text)
                self.assertEqual(overlap["status"], "busy")
                self.assertEqual(server._current_task["task_id"], owner)
                self.assertEqual(agent.run.call_count, 1)
            finally:
                release.set()
                workers = list(server._desktop_workers.values())
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                if not request.done():
                    await request
                await asyncio.sleep(0)
            self.assertFalse(server._current_task["busy"])
            self.assertEqual(server._desktop_workers, {})
        with mock.patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}), \
                mock.patch("anyuse.core.agent.Agent", return_value=agent), \
                mock.patch.object(server, "_try_state_graph_execution", new=mock.AsyncMock(return_value=None)):
            asyncio.run(scenario())
        self.assertTrue(late[0]["blocked"])
        backend.click_at.assert_not_called()

    def test_timeout_retains_desktop_lease_and_stops_late_dispatch(self):
        self._exercise_owned_worker(cancel=False)

    def test_caller_cancellation_retains_desktop_lease_until_actual_return(self):
        self._exercise_owned_worker(cancel=True)

    def test_desktop_rejects_invalid_timeout_without_starting_agent(self):
        from anyuse.mcpserver import server
        with mock.patch("anyuse.core.agent.Agent") as ctor:
            for timeout in (0, -1, True, "1", float("nan"), float("inf")):
                response = json.loads(asyncio.run(server._desktop_execute({"task": "fixture", "timeout": timeout}))[0].text)
                self.assertEqual(response["status"], "error")
            ctor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
