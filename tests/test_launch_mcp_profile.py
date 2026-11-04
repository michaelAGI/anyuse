"""The default MCP profile and compiled desktop boundary (offline fakes only)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from anyuse.mcpserver.desktop_compiled_tools import (
    compiled_desktop_record,
    persist_compiled_desktop,
)
from anyuse.mcpserver.server import create_server


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


class CheckedExecutorFake:
    def __init__(self):
        self.calls = []

    async def execute_checked_step(self, step, step_idx=None, workflow=None,
                                   state_graph=None):
        self.calls.append((step, step_idx, workflow, state_graph))
        return {"success": True, "method": step.modality}, {"identity_checked": True}


def record(storage, name="read_setting", compile_class="read_value", extra=None):
    rec = compiled_desktop_record(
        workflow=name,
        compile_class=compile_class,
        parameters=[{"name": "label", "type": "string", "required": True}],
        steps=[{"intent": "read setting", "source": "workflow",
                "target_element": "value", "modality": "ax",
                "command": {"role": "AXStaticText", "title": "${label}",
                            "action": "read"}}],
        workflow_template={"id": name, "name": name, "required_app": "Settings",
                           "steps": [{"intent": "read setting",
                                      "target_element": "value"}]},
        state_graph={"states": {}, "transitions": []},
        proof={"proved": True, "fresh_inputs": 2},
        extra=extra,
    )
    persist_compiled_desktop(storage, name, rec)


async def listed(server):
    result = await server.request_handlers[ListToolsRequest](ListToolsRequest())
    return result.root.tools


async def called(server, name, arguments=None):
    request = CallToolRequest(params=CallToolRequestParams(
        name=name, arguments=arguments or {}))
    result = await server.request_handlers[CallToolRequest](request)
    return result.root


class ProfileTests(unittest.TestCase):
    def test_default_lists_compiled_desktop_and_not_raw_actuators(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(tmp)
            record(storage)
            server = create_server(profile="default", compiled_storage=storage,
                                   desktop_storage=storage)
            names = {tool.name for tool in asyncio.run(listed(server))}
        self.assertIn("desktop__read_setting", names)
        self.assertIn("list_compiled_workflows", names)
        self.assertIn("propose_learn_candidate", names)
        self.assertNotIn("run_task", names)
        self.assertNotIn("desktop_execute", names)
        self.assertNotIn("run_workflow", names)
        self.assertNotIn("get_context", names)

    def test_default_dispatch_refuses_a_remembered_unlisted_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = create_server(profile="default", compiled_storage=Storage(tmp),
                                   desktop_storage=Storage(tmp))
            result = asyncio.run(called(server, "run_task", {"task": "click everything"}))
        payload = json.loads(result.content[0].text)
        self.assertFalse(payload["success"])
        self.assertIn("not available", payload["error"])

    def test_full_is_the_explicit_escape_hatch_for_raw_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(tmp)
            names = {tool.name for tool in asyncio.run(listed(create_server(
                profile="full", compiled_storage=storage, desktop_storage=storage)))}
        self.assertIn("run_task", names)
        self.assertIn("desktop_execute", names)
        self.assertIn("get_context", names)


class DesktopCheckedPathTests(unittest.TestCase):
    def test_read_only_desktop_tool_uses_checked_executor_with_drift_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage, executor = Storage(tmp), CheckedExecutorFake()
            record(storage)
            server = create_server(profile="default", compiled_storage=storage,
                                   desktop_storage=storage, desktop_executor=executor)
            result = asyncio.run(called(server, "desktop__read_setting", {"label": "Version"}))
        self.assertFalse(result.isError, result.content)
        self.assertTrue(result.structuredContent["ok"])
        self.assertEqual(result.structuredContent["served_by"], "execute_checked_step")
        self.assertEqual(len(executor.calls), 1)
        step, index, workflow, graph = executor.calls[0]
        self.assertEqual(step.command["title"], "Version")
        self.assertEqual(index, 0)
        self.assertEqual(workflow.name, "read_setting")
        self.assertIsNotNone(graph)

    def test_file_class_started_with_values_asks_nothing_and_a_holding_level_is_refused(self):
        # a call started with its values is the approval
        asked = []

        async def approver(ctx):
            asked.append(ctx)
            return True
        with tempfile.TemporaryDirectory() as tmp:
            storage, executor = Storage(tmp), CheckedExecutorFake()
            record(storage, "write_file", "file_write")
            record(storage, "write_held", "file_write", extra={"approval_level": "all"})
            server = create_server(profile="default", compiled_storage=storage,
                                   desktop_storage=storage, desktop_executor=executor,
                                   desktop_approver=approver)
            result = asyncio.run(called(server, "desktop__write_file", {"label": "x"}))
            held = asyncio.run(called(server, "desktop__write_held", {"label": "x"}))
        self.assertTrue(result.structuredContent["ok"])
        self.assertEqual((asked, len(executor.calls)), ([], 1))
        self.assertTrue(held.structuredContent["blocked"])
        self.assertEqual(held.structuredContent["refused_by"], "approval_level")
        self.assertEqual(held.structuredContent["approval_level"], "all")
        self.assertEqual(len(executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
