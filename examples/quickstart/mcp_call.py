"""Start AnyUse's MCP server over stdio, list its tools and call one.

This is what Claude Code, Codex or any MCP client does; it is written out so
the quickstart can show the exchange without an agent (and without a model).

    python mcp_call.py TOOL_NAME '{"q": "maple"}' [FULL_RESULT.json]

TOOL_NAME is a listed name, for example ``127_0_0_1__fixture_search`` (``<host>__<workflow>``,
the host's dots as underscores) or ``list_sites`` under the ``meta`` profile.

Prints the tool list and a summary of the call's structured result (the whole
result goes to FULL_RESULT.json when given). Exits 0 when the call returned
``ok: true``, 3 when the tool answered with a failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(tool: str, args: dict, out: str = "") -> int:
    params = StdioServerParameters(command=sys.executable, args=["-P", "-m", "anyuse.mcpserver"],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"tools/list: {len(tools)} tools")
            for t in tools:
                print(f"  - {t.name}")
            if tool not in {t.name for t in tools}:
                print(f"tool {tool!r} is not listed"
                      + (" (listed names have no dots: <host>__<workflow>, agent_run)" if "." in tool else ""))
                return 2
            result = await session.call_tool(tool, args)
            payload = result.structuredContent
            if payload is None:
                text = "".join(getattr(c, "text", "") for c in result.content)
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = {"text": text}
            if out:
                with open(out, "w") as fh:
                    json.dump(payload, fh, indent=1, default=str)
            print(f"tools/call {tool} {json.dumps(args)}:")
            for key in ("ok", "served_by", "latency_ms", "model_calls", "error",
                        "count", "class", "next"):
                if key in payload:
                    print(f"  {key}: {payload[key]}")
            # healed: the tool healed itself on this call; heal: why it did not
            for key in ("healed", "heal"):
                summary = payload.get(key)
                if isinstance(summary, dict):
                    shown = ("swapped", "rung", "localised", "relearned", "requests",
                             "model_calls", "browser", "reason", "next_attempt_after")
                    print(f"  {key}: " + ", ".join(f"{k}={summary[k]}" for k in shown
                                                   if summary.get(k) not in (None, "", [])))
            if "proof" in payload or "result" in payload:
                proof = payload.get("proof")
                if isinstance(proof, dict):
                    proof = (proof.get("what_was_checked") or proof.get("status")
                             or json.dumps(proof, default=str)[:160])
                print(f"  proof: {proof}")
                print(f"  result: {payload.get('result')}")
            return 0 if payload.get("ok") else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1], json.loads(sys.argv[2]),
                             sys.argv[3] if len(sys.argv) > 3 else "")))
