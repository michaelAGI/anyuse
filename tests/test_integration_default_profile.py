"""feat/integration: the default (launch) MCP profile, end to end and offline.

One server over temp storage must list compiled WEB tools (real compiled
records, host acknowledged), compiled DESKTOP tools (proof-pinned record), and
skill/routine tools whose steps are proved/human, and must refuse raw actuators
(run_task, desktop_execute, run_workflow, get_context) without the explicit
full profile. A web tool whose proof status is "checked (selection not held
out)" and a manifest with an agent step stay out of the default profile.
No network, desktop, model or real ~/.anyuse: HTTP is never sent (nothing is
executed on a web tool), the desktop executor is a fake.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import tempfile
import unittest

from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution.api_step import ApiStepExecutor
from anyuse.execution.skill_gen import RESUME_TOOL_NAME
from anyuse.execution.skill_manifest import validate_manifest
from anyuse.mcpserver import compiled_tools
from anyuse.mcpserver.server import create_server

import test_launch_mcp_profile as launch
import test_routine_example as rex

RAW_ACTUATORS = ("run_task", "desktop_execute", "run_workflow", "get_context")


def agent_manifest():
    """The example routine with its human pick turned into an agent step."""
    m = copy.deepcopy(json.load(open(rex.MANIFEST_FILE)))
    m["name"] = "crates_agent_pick"
    pick = next(s for s in m["steps"] if s["id"] == "pick")
    pick["mode"] = "agent"
    pick["action"] = {"kind": "agent_choice", "choose_from": "$in.candidates", "into": "crate"}
    pick["autonomy"] = {"level": "model_proposes_human_confirms"}
    return m


async def listed(server):
    return (await server.request_handlers[ListToolsRequest](ListToolsRequest())).root.tools


async def called(server, name, arguments=None):
    req = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
    return (await server.request_handlers[CallToolRequest](req)).root


class DefaultProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage, self.hosts = rex.setup(self.tmp)
        checked = dict(rex.PROOF, status="checked (selection not held out)",
                       selection_held_out=False)
        persist_compiled(self.storage, "crates_checked", compiled_record(
            rex.detail_template(),
            checked, workflow="crates_checked"))
        self.desktop = launch.Storage(os.path.join(self.tmp, "desktop"))
        launch.record(self.desktop)
        self.skills = os.path.join(self.tmp, "skills")
        os.makedirs(self.skills)
        shutil.copy(rex.MANIFEST_FILE, self.skills)
        agent = agent_manifest()
        self.assertEqual(validate_manifest(agent), [])
        with open(os.path.join(self.skills, "agent.manifest.json"), "w") as fh:
            json.dump(agent, fh)
        self.executor = launch.CheckedExecutorFake()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, profile):
        return create_server(profile=profile, compiled_storage=self.storage,
                             compiled_hosts_path=self.hosts, desktop_storage=self.desktop,
                             desktop_executor=self.executor, skills_dir=self.skills)

    def test_default_lists_web_desktop_and_proved_skill_tools(self):
        names = {t.name for t in asyncio.run(listed(self.server("default")))}
        web = {compiled_tools.typed_tool_name("crates.io", w)
               for w in ("crates_search", "crates_detail")}
        self.assertTrue(web <= names, (web, names))
        self.assertIn("desktop__read_setting", names)
        self.assertIn("routine__crates_lookup", names)
        self.assertIn(RESUME_TOOL_NAME, names)
        # not proved (selection not held out) and agent-step manifests stay out
        self.assertNotIn(compiled_tools.typed_tool_name("crates.io", "crates_checked"), names)
        self.assertNotIn("routine__crates_agent_pick", names)
        for raw in RAW_ACTUATORS:
            self.assertNotIn(raw, names)

    def test_default_refuses_raw_actuators_even_when_called_by_name(self):
        server = self.server("default")
        for raw in RAW_ACTUATORS:
            result = asyncio.run(called(server, raw, {"task": "x", "name": "x"}))
            payload = json.loads(result.content[0].text)
            self.assertFalse(payload["success"], raw)
            self.assertIn("not available", payload["error"])
        self.assertEqual(self.executor.calls, [])

    def test_default_resume_refuses_approval_without_a_human_channel(self):
        result = asyncio.run(called(self.server("default"), RESUME_TOOL_NAME, {
            "run_id": "r1", "approval": {"step": "detail", "inputs_digest": "x"}}))
        self.assertEqual(result.structuredContent["status"], "refused")

    def test_default_run_compiled_workflow_refuses_a_checked_record(self):
        result = asyncio.run(called(self.server("default"), compiled_tools.TOOL_RUN_NAME, {
            "workflow": "crates_checked", "parameters": {"crate": "serde_json"}}))
        payload = json.loads(result.content[0].text)
        self.assertFalse(payload["ok"])
        self.assertIn("not 'proved'", payload["error"])
        self.assertIn("full MCP profile", payload["error"])

    def test_checked_record_runs_only_without_the_proved_requirement(self):
        transport = rex.FakeTransport()
        kw = dict(storage=self.storage, hosts_path=self.hosts,
                  executor=ApiStepExecutor(transport=transport))
        refused = compiled_tools.run_compiled(
            "crates_checked", {"crate": "serde_json"}, require_proved=True, **kw)
        self.assertFalse(refused["ok"])
        self.assertEqual(transport.sent, [])
        ran = compiled_tools.run_compiled("crates_checked", {"crate": "serde_json"}, **kw)
        self.assertTrue(ran["ok"], ran)
        self.assertEqual(len(transport.sent), 1)
        proved = compiled_tools.run_compiled(
            "crates_detail", {"crate": "serde_json"}, require_proved=True, **kw)
        self.assertTrue(proved["ok"], proved)

    def test_full_profile_is_the_explicit_escape_hatch(self):
        names = {t.name for t in asyncio.run(listed(self.server("full")))}
        self.assertIn("routine__crates_agent_pick", names)
        self.assertIn(compiled_tools.typed_tool_name("crates.io", "crates_checked"), names)
        for raw in RAW_ACTUATORS:
            self.assertIn(raw, names)


if __name__ == "__main__":
    unittest.main()
