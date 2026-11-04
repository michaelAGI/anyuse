"""A typed compiled tool declares an outputSchema, so the MCP server must return
structured content with it. Before this, the SDK turned every successful typed
call into "Output validation error: outputSchema defined but no structured
output returned". Offline: the tool list and the run are faked."""
import asyncio
import unittest

from mcp import types

from anyuse.mcpserver import compiled_tools, server as srv

SPEC = {"name": "ex.test.search", "description": "d",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}},
                         "required": ["q"]},
        "output_schema": {"type": "object", "properties": {
            "ok": {"type": "boolean"},
            "result": {"type": "object", "properties": {"items": {"type": "array"}},
                       "required": ["items"], "additionalProperties": False},
            "provenance": {"type": "object"}}, "required": ["ok"],
            "additionalProperties": True},
        "proof": {"id": "p"}, "pins": {"tool_set_version": 1,
                                       "compiled_schema_version": 3}}


class TypedToolStructuredOutput(unittest.TestCase):
    def test_typed_call_returns_schema_valid_structured_content(self):
        real_list, real_run = compiled_tools.executable_typed_tools, compiled_tools.run_typed_tool
        compiled_tools.executable_typed_tools = lambda *a, **k: [SPEC]
        compiled_tools.run_typed_tool = lambda name, args, **k: {
            "ok": True, "result": {"items": ["a", "b"]}, "model_calls": 0,
            "latency_ms": 12.5, "provenance": {"host": "ex.test"}}
        try:
            server = srv.create_server()

            async def go():
                await server.request_handlers[types.ListToolsRequest](
                    types.ListToolsRequest(method="tools/list"))
                req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                    name="ex.test.search", arguments={"q": "x"}))
                return await server.request_handlers[types.CallToolRequest](req)
            res = asyncio.run(go()).root
        finally:
            compiled_tools.executable_typed_tools, compiled_tools.run_typed_tool = real_list, real_run
        self.assertFalse(res.isError, res.content)
        self.assertEqual(res.structuredContent["result"]["items"], ["a", "b"])
        self.assertEqual(res.structuredContent["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
