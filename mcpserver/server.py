"""
AnyUse MCP Server Implementation

Wraps the existing anyuse Agent as an MCP server, allowing external
AI systems to invoke computer control capabilities.

The server exposes these tools:
    - run_task: Execute any natural language task
    - run_workflow: Execute a saved workflow by name
    - list_workflows: List all saved workflows
    - get_context: Get current macOS screen context
    - get_workflow_details: Get detailed info about a workflow

All actual execution flows through the anyuse Agent, which handles:
    - Screen capture and analysis
    - Action execution (click, type, scroll)
    - Workflow learning and replay
    - Credential management
    - Goal verification
"""

import asyncio
import json
import os
import sys
import threading
import time
import math
from typing import Any, Sequence

from anyuse.mcpserver import (
    agent_tools, compiled_tools, desktop_compiled_tools, learn_tools, meta_tools, skill_tools, tool_names,
)
from anyuse.settings import env_value_problem, get as get_setting
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
)


PROFILE_ENV = "ANYUSE_MCP_PROFILE"
PROFILE_DEFAULT = "default"
PROFILE_FULL = "full"
#: four fixed tools (mcpserver/meta_tools.py, docs/MCP_META.md); a projection
#: of ``default`` whose tool list does not grow with the compiled workflows
PROFILE_META = "meta"

DEFAULT_STATIC_TOOLS = frozenset({
    "list_workflows", "get_workflow_details", "desktop_status", "get_guide",
    compiled_tools.TOOL_LIST_NAME, compiled_tools.TOOL_RUN_NAME,
    compiled_tools.TOOL_EXPLAIN_NAME, learn_tools.TOOL_PROPOSE_NAME,
    agent_tools.TOOL_NAME,
})
FULL_STATIC_TOOLS = frozenset({
    "run_task", "run_workflow", "list_workflows", "get_context",
    "get_workflow_details", "desktop_execute", "desktop_status",
    "get_guide", compiled_tools.TOOL_LIST_NAME,
    compiled_tools.TOOL_RUN_NAME, compiled_tools.TOOL_EXPLAIN_NAME,
    learn_tools.TOOL_PROPOSE_NAME,
})
#: older names of static tools that still call them (mcpserver/tool_names.py)
STATIC_ALIASES = {agent_tools.TOOL_NAME: agent_tools.LEGACY_NAMES}

#: the kinds of a listed tool (which module runs it)
KIND_STATIC = "static"
KIND_WEB_API = "web_api"
KIND_WEB_TOOL = "web_tool"
KIND_DESKTOP = "desktop"
KIND_SKILL = "skill"


def mcp_profile(value: str | None = None) -> str:
    """Resolve the launch profile; unknown values fail closed to ``default``."""
    selected = str(get_setting(PROFILE_ENV, value)).strip().lower()
    return selected if selected in (PROFILE_FULL, PROFILE_META) else PROFILE_DEFAULT


def anyuse_version() -> str:
    """AnyUse's own version for serverInfo (the SDK would report its own otherwise)."""
    try:
        import importlib.metadata
        return importlib.metadata.version("anyuse")
    except Exception:  # noqa: BLE001 - a source checkout without installed metadata
        from anyuse import __version__
        return __version__


_INSTRUCTIONS_FAILURES = (
    "Registered rea.* operations retain their own verification tier. A model_judged replay calls its observer "
    "model and retains that judgment; it is not deterministic. REA refusal or uncertainty is terminal: no UI "
    "fallback or automatic retry. Run registered REA operations inside the VM's GUI session. "
    "A failed call returns ok: false with a reason and `next`; follow `next` at most once. A `next` that "
    "starts with \"hand to the user (do not run it yourself)\" is a step only the user takes: pass it on. "
    "The agent tool browses, reads and fills; it hands logins, payments, sent forms and deletions to the "
    "user unless the user set ANYUSE_MCP_AGENT_COMMITS=agent.")


def server_instructions(profile: str) -> str:
    """Short guidance the client shows the model before it calls anything."""
    if profile == PROFILE_META:
        return ("AnyUse serves tools compiled from a task done once on a website or a Mac app, proved on "
                "inputs they were not built from. Use list_sites, then list_operations {site}, then "
                "call_operation {site, operation, args}; a compiled call answers without a model. The agent "
                "harness is site `agent`, operation `run`. " + _INSTRUCTIONS_FAILURES)
    return ("AnyUse serves tools compiled from a task done once on a website or a Mac app, proved on inputs "
            "they were not built from. Each `<host>__<workflow>` tool (the host's dots as underscores) is a "
            "proved request that answers without a model; agent_run drives a task when no tool exists yet. "
            + _INSTRUCTIONS_FAILURES)


def create_server(*, profile: str | None = None, compiled_storage=None,
                  compiled_hosts_path: str | None = None, desktop_storage=None,
                  desktop_executor=None, desktop_approver=None,
                  skills_dir: str | None = None, compiled_executor=None,
                  desktop_guest_runner=None, agent_runner=None, agent_injected=None,
                  web_route=None, rea_store=None) -> Server:
    """
    Create and configure the MCP server.

    ``compiled_executor`` is the api-step executor handed to
    ``compiled_tools.run_compiled`` (``None``: the real ``ApiStepExecutor``);
    tests pass one over a fake transport. ``desktop_guest_runner`` is the guest
    job agent's channel a compiled desktop tool runs through (``None``:
    ``ANYUSE_DESKTOP_GUEST_SESSION``, else every desktop call is refused with
    the foreground interlock's typed reason; execution/desktop_tools.py).
    ``web_route`` is the saved web tools' call path (``web_tool_call.WebToolRoute``; ``None``: one over
    ``compiled_storage`` with the container browser of ``ANYUSE_LEARN_ENDPOINT``); tests pass one with fakes.
    
    Returns:
        Configured MCP Server instance
    """
    selected_profile = mcp_profile(profile)
    server = Server("anyuse", version=anyuse_version(), instructions=server_instructions(selected_profile))
    from anyuse.execution.rea_catalog import CatalogStore
    registered_rea = rea_store if rea_store is not None else CatalogStore()

    def rea_specs():
        return registered_rea.list_tools()

    def collisions():
        from collections import Counter
        registered = rea_specs()
        if not registered:
            # An empty REA registry must leave existing lazy meta listings
            # inert (not instantiate browser/workflow storage).
            return set()
        specs = web_typed_tools() + web_tool_specs() + desktop_specs() + skill_tool_specs() + registered
        counts = Counter(spec["name"] for spec in specs)
        for spec in specs:
            for alias in spec.get("aliases") or ():
                counts[alias] += 1
        static = set(DEFAULT_STATIC_TOOLS) | set(FULL_STATIC_TOOLS) | set(meta_tools.TOOL_NAMES)
        for aliases in STATIC_ALIASES.values():
            static.update(aliases)
        return {name for name, count in counts.items() if count > 1 or name in static}

    def safe_rea_specs():
        blocked = collisions()
        return [spec for spec in rea_specs() if spec["name"] not in blocked]

    async def run_rea_checked(name, arguments):
        if name in collisions():
            return {"ok": False, "status": "refused", "reason": "ambiguous tool name", "model_calls": 0,
                    "automatic_retry": False, "terminal_on_failure": True}
        return await asyncio.to_thread(registered_rea.execute, name, dict(arguments or {}))

    def web_typed_tools() -> list[dict]:
        # The default (launch) profile lists only web tools whose proof status
        # is "proved" (held-out validation, docs/ATTACH.md). A tool persisted
        # as "checked (selection not held out)" stays reachable under the
        # explicit full profile, with its status in the description.
        specs = compiled_tools.executable_typed_tools(compiled_storage, compiled_hosts_path)
        if selected_profile == PROFILE_FULL:
            return specs
        return [spec for spec in specs
                if str((spec.get("proof") or {}).get("status") or "proved") == "proved"]

    route_box: dict = {}

    def web_route_obj():
        """The saved web tools' call path (docs/MCP_WEB_TOOLS.md): one per server."""
        if web_route is not None:
            return web_route
        if "r" not in route_box:
            from anyuse.execution.web_tool_call import WebToolRoute
            route_box["r"] = WebToolRoute(storage=compiled_storage)
        return route_box["r"]

    def web_tool_specs() -> list[dict]:
        """Saved browser-replayed web tools (both profiles; under meta as call_operation rows). A name an API tool
        already has gets a suffix; so does an older dotted name (the tool digest, as it was listed before)."""
        from anyuse.execution import web_tool_call
        storage = getattr(web_route_obj(), "storage", None) or compiled_storage
        # the API tools' names by path alone (no WorkflowStorage is created; one odd compile record hides nothing)
        listing = compiled_storage if compiled_storage is not None else meta_tools._WorkflowDir()
        try:
            api = compiled_tools.executable_typed_tools(listing, compiled_hosts_path)
        except Exception:
            api = []
        return web_tool_call.tool_specs(storage, taken=[spec["name"] for spec in api],
                                        taken_aliases=[old for spec in api for old in spec.get("aliases", ())])

    async def run_web_tool_checked(tool_name: str, arguments: dict):
        """The one saved-web-tool run path (``<host>__<workflow>`` web tools and ``call_operation``); off the loop."""
        from anyuse.execution import web_tool_call
        spec = web_tool_call.find(web_tool_specs(), tool_name)
        if spec is None:
            return None
        return await asyncio.to_thread(web_route_obj().call, spec["entry"], dict(arguments or {}))

    def skill_tool_specs() -> list[dict]:
        # Skills/routines (docs/SKILLS.md). Default profile: only manifests
        # whose steps are all proved/replay/human; agent steps never auto-run
        # in any profile (no model advisor is configured on this surface).
        return skill_tools.tool_definitions(
            skills_dir, launch_profile=selected_profile != PROFILE_FULL)

    def elicitor():
        """An elicitation channel to the person, when the connected client
        advertises it (docs/HUMAN_INBOX.md, "Asking the human"); else None
        and cards wait for `anyuse inbox do`."""
        from mcp import types as mcp_types
        from anyuse.execution.human_inbox import McpElicitor
        try:
            ctx = server.request_context
        except LookupError:
            return None
        try:
            ok = ctx.session.check_client_capability(mcp_types.ClientCapabilities(
                elicitation=mcp_types.ElicitationCapability()))
        except Exception:
            ok = False
        if not ok:
            return None
        return McpElicitor(ctx.session, asyncio.get_running_loop(), ctx.request_id)

    async def ask_about_block(payload):
        """A direct compiled call hit a block page: ask the person once."""
        need = payload.get("needs_human") if isinstance(payload, dict) else None
        asker = elicitor() if need else None
        if asker is None:
            if need:
                # no client can ask the person: notify them (docs/NOTIFY.md)
                from anyuse.execution import notify
                sent = notify.notify_card({"id": need.get("card"), "kind": need.get("kind")})
                if sent.get("sent"):
                    payload = {**payload, "needs_human": {**need,
                                                          "notified": sent.get("channel")}}
            return payload
        from anyuse.execution import human_inbox
        out = await asyncio.to_thread(human_inbox.elicit_card, human_inbox.Inbox(),
                                      need["card"], engine_for=None, elicitor=asker)
        if out.get("status") == "done_by_human":
            return {**payload, "status": "done_by_human", "action_required": "",
                    "human_result": out.get("human_result", ""),
                    "needs_human": {**need, "answered": True}}
        return {**payload, "needs_human": {**need, **out}}

    async def run_compiled_checked(workflow: str, parameters: dict) -> dict:
        """``run_compiled_workflow`` and ``call_operation`` run web workflows
        here (the typed web tools reach the same ``run_compiled`` through
        ``compiled_tools.run_typed_tool``, then the same block handling)."""
        payload = await asyncio.to_thread(
            compiled_tools.run_compiled, workflow, parameters,
            compiled_storage, compiled_executor, compiled_hosts_path,
            require_proved=selected_profile != PROFILE_FULL)
        return await ask_about_block(payload)

    def desktop_specs(storage=None) -> list[dict]:
        """The compiled desktop tools as this server runs them: its own guest
        runner and executor decide the route and readiness (both profiles)."""
        return desktop_compiled_tools.executable_typed_tools(
            desktop_storage if storage is None else storage,
            guest_runner=desktop_guest_runner, executor=desktop_executor)

    async def run_agent_checked(arguments: dict) -> dict:
        """The one agent-harness run path (``agent_run`` and ``call_operation`` on site ``agent``):
        ``agent_runner`` is ``harness_product.run_agent_task`` (a test injects a fake); the guest runner of
        this server is the harness's desktop channel; off the loop (a run may take minutes)."""
        injected = dict(agent_injected or {})
        if rea_store is not None:
            injected.setdefault("rea_store", registered_rea)
        if desktop_guest_runner is not None and "guest" not in injected:
            injected["guest"] = desktop_guest_runner
        return await asyncio.to_thread(agent_tools.handle, arguments, run=agent_runner,
                                       storage=desktop_storage, **injected)

    async def run_desktop_checked(tool_name: str, arguments: dict):
        """The one compiled-desktop run path (``desktop__<workflow>`` and
        ``call_operation``)."""
        return await desktop_compiled_tools.run_typed_tool(
            tool_name, arguments, storage=desktop_storage,
            executor=desktop_executor, approver=desktop_approver,
            guest_runner=desktop_guest_runner)

    def alias_input_error(entry: tool_names.Entry, arguments: dict) -> CallToolResult | None:
        """The SDK's own input check for a call by an older name: the listed tool's input schema."""
        spec = entry.spec or {}
        schema = (agent_tools.TOOL_SCHEMA if entry.source == agent_tools.TOOL_NAME
                  else spec.get("input_schema") or spec.get("inputSchema"))
        if not schema:
            return None
        import jsonschema
        try:
            jsonschema.validate(instance=arguments or {}, schema=schema)
        except jsonschema.ValidationError as exc:
            return CallToolResult(content=[TextContent(type="text", text=f"Input validation error: {exc.message}")],
                                  isError=True)
        return None

    def tool_table() -> tool_names.Registry:
        """This profile's tools by listed name (``^[a-zA-Z0-9_-]{1,64}$``, unique), each with its kind,
        the name its module knows it by and the older names that still call it."""
        table = tool_names.Registry()
        if selected_profile == PROFILE_META:
            for name in meta_tools.TOOL_NAMES:
                table.add(KIND_STATIC, name)
            return table
        static = FULL_STATIC_TOOLS if selected_profile == PROFILE_FULL else DEFAULT_STATIC_TOOLS
        for name in sorted(static):
            table.add(KIND_STATIC, name, STATIC_ALIASES.get(name, ()))
        for kind, specs in ((KIND_WEB_API, web_typed_tools()), (KIND_WEB_TOOL, web_tool_specs()),
                            (KIND_DESKTOP, desktop_specs()), (KIND_SKILL, skill_tool_specs())):
            for spec in specs:
                table.add(kind, spec["name"], spec.get("aliases") or (), spec, shares=spec.get("shares") or "")
        return table
    
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools."""
        if selected_profile == PROFILE_META:
            # Constants only: no record is read, so the listing cannot grow.
            return [Tool(name=spec["name"], description=spec["description"],
                         inputSchema=spec["inputSchema"])
                    for spec in meta_tools.TOOL_DEFINITIONS]
        tools = [
            Tool(
                name="run_task",
                description=(
                    "Execute a natural language task on macOS. "
                    "The anyuse agent will analyze the screen, plan actions, "
                    "and execute them to complete the task. "
                    "Examples: 'Open Safari', 'Search Google for AI news', "
                    "'Create a new folder on Desktop called Projects'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Natural language description of the task to perform"
                        },
                        "max_turns": {
                            "type": "integer",
                            "description": "Maximum turns in agent loop (default: 20)",
                            "default": 20
                        },
                        "verbose": {
                            "type": "boolean", 
                            "description": "Include detailed execution info in response",
                            "default": False
                        }
                    },
                    "required": ["task"]
                }
            ),
            Tool(
                name="run_workflow",
                description=(
                    "Execute a saved workflow by name. "
                    "Workflows are pre-recorded action sequences that can be replayed. "
                    "Use this when you know a workflow exists for the task - "
                    "it's faster than run_task since it doesn't need AI planning."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the saved workflow to execute"
                        }
                    },
                    "required": ["name"]
                }
            ),
            Tool(
                name="list_workflows",
                description=(
                    "List all saved workflows available for execution. "
                    "Returns workflow names, descriptions, and success rates."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="get_context",
                description=(
                    "Get current macOS screen context. "
                    "Returns frontmost app, running apps, screen size, and open windows. "
                    "Use this to understand the current state before running tasks."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="get_workflow_details",
                description=(
                    "Get detailed information about a saved workflow. "
                    "Returns steps, descriptions, success rate, and verification status. "
                    "Use this to understand what a workflow does before running it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the workflow to get details for"
                        }
                    },
                    "required": ["name"]
                }
            ),
            Tool(
                name="desktop_execute",
                description=(
                    "Execute a desktop automation task with sandbox enforcement. "
                    "Accepts boundary rules, an optional plan with checkpoints, and "
                    "scoped credentials. This is the primary tool used by "
                    "the orchestrator's DESKTOP_AGENT nodes for gated desktop automation."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Natural language task description. With a plan: just the goal. Without a plan: full description."
                        },
                        "plan": {
                            "type": "object",
                            "description": "Optional step-by-step execution plan with checkpoints",
                            "properties": {
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "index": {"type": "integer"},
                                            "instruction": {"type": "string"},
                                            "checkpoints": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "id": {"type": "string", "description": "e.g. cp_a1b2c3d4"},
                                                        "condition": {"type": "string", "description": "What to verify"},
                                                        "guard_node_id": {"type": "string", "description": "Node for complex eval"}
                                                    },
                                                    "required": ["id", "condition"]
                                                }
                                            }
                                        },
                                        "required": ["index", "instruction"]
                                    }
                                },
                                "task_text": {"type": "string", "description": "Readable fallback of steps"},
                                "checkpoint_callback_url": {"type": "string", "description": "URL for checkpoint evaluation POSTs"}
                            }
                        },
                        "boundary": {
                            "type": "object",
                            "description": "Sandbox rules from orchestrator BOUNDARY node",
                            "properties": {
                                "list_mode": {"type": "string", "description": "'blocklist' or 'allowlist'"},
                                "allowed_domains": {"type": "array", "items": {"type": "string"}},
                                "blocked_domains": {"type": "array", "items": {"type": "string"}},
                                "allowed_apps": {"type": "array", "items": {"type": "string"}},
                                "blocked_apps": {"type": "array", "items": {"type": "string"}},
                                "max_actions": {"type": "integer"},
                                "action_rules": {
                                    "type": "object",
                                    "description": "Per-action rules: {action_name: 'allow'|'guard'|'exclude'}"
                                },
                                "confirmation_behavior": {
                                    "type": "string",
                                    "description": "'auto' (default), 'selective', or 'always'"
                                },
                                "safety_instructions": {"type": "string"},
                                "guard_callback_url": {
                                    "type": "string",
                                    "description": "URL for guarded action evaluation POSTs"
                                },
                            }
                        },
                        "credentials": {
                            "type": "object",
                            "description": "Scoped credential key-value pairs"
                        },
                        "screen_dimensions": {
                            "type": "object",
                            "properties": {
                                "width": {"type": "integer"},
                                "height": {"type": "integer"}
                            }
                        },
                        "turn_limit": {
                            "type": "integer",
                            "description": "Max agent loop iterations (default: 50)",
                            "default": 50
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max seconds for execution (default: 300)",
                            "default": 300
                        },
                        "orchestrator_callbacks": {
                            "type": "array",
                            "description": "Channels back to graph engine for progress updates",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "event": {"type": "string"},
                                    "url": {"type": "string"}
                                }
                            }
                        },
                    },
                    "required": ["task"]
                }
            ),
            Tool(
                name="desktop_status",
                description=(
                    "Check if the AnyUse desktop worker is available and healthy. "
                    "Returns status (ready/busy/offline) and current task if busy."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="get_guide",
                description=(
                    "Get the auto-generated guide for an app or website domain. "
                    "Returns use cases, UI landmarks, keyboard shortcuts, and navigation tips "
                    "learned from previous user and agent activity."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "app_or_domain": {
                            "type": "string",
                            "description": "Application name or website domain (e.g. 'Safari', 'stripe.com')"
                        }
                    },
                    "required": ["app_or_domain"]
                }
            ),
            # --- COMPILED workflows (compile-to-API). These two never drive the
            # desktop: they list proved HTTP templates and replay a read-only one
            # through the normal ladder, with the existing HIL gate deciding.
            Tool(
                name=compiled_tools.TOOL_LIST_NAME,
                description=compiled_tools.TOOL_LIST_DESCRIPTION,
                inputSchema=compiled_tools.TOOL_LIST_SCHEMA,
            ),
            Tool(
                name=compiled_tools.TOOL_RUN_NAME,
                description=compiled_tools.TOOL_RUN_DESCRIPTION,
                inputSchema=compiled_tools.TOOL_RUN_SCHEMA,
            ),
            Tool(
                name=compiled_tools.TOOL_EXPLAIN_NAME,
                description=compiled_tools.TOOL_EXPLAIN_DESCRIPTION,
                inputSchema=compiled_tools.TOOL_EXPLAIN_SCHEMA,
            ),
            # --- LEARNING proposal only. A proposal is untrusted evidence;
            # there is deliberately no agent/MCP self-confirmation tool.
            # Confirmation and persistence stay in the CLI/human path.
            Tool(
                name=learn_tools.TOOL_PROPOSE_NAME,
                description=learn_tools.TOOL_PROPOSE_DESCRIPTION,
                inputSchema=learn_tools.TOOL_PROPOSE_SCHEMA,
            ),
            # --- the agent harness: one open-ended run
            Tool(
                name=agent_tools.TOOL_NAME,
                description=agent_tools.TOOL_DESCRIPTION,
                inputSchema=agent_tools.TOOL_SCHEMA,
            ),
        ]
        table = tool_table()
        static = {entry.name for entry in table.of_kind(KIND_STATIC)}
        tools = [tool for tool in tools if tool.name in static]
        # Host-specific tools exist only for records whose proof/version pin,
        # host acknowledgement, HIL class and health all authorize execution.
        # Their descriptions are generated from trusted metadata, never page
        # text or a recorded title.
        for entry in table.entries:
            spec = entry.spec
            if entry.kind == KIND_WEB_API:
                tools.append(Tool(
                    name=entry.name, description=spec["description"],
                    inputSchema=spec["input_schema"], outputSchema=spec["output_schema"],
                    _meta={"anyuse": {"tool_set_version": spec["pins"]["tool_set_version"],
                                      "proof_id": spec["proof"]["id"],
                                      "compiled_schema_version":
                                          spec["pins"]["compiled_schema_version"]}},
                ))
            elif entry.kind == KIND_WEB_TOOL:
                tools.append(Tool(
                    name=entry.name, description=spec["description"],
                    inputSchema=spec["input_schema"], outputSchema=spec["output_schema"],
                    _meta={"anyuse": {"kind": spec["kind"], "tool_sha256": spec["tool_sha256"][:16],
                                      "tool_version": spec["version"], "workflow": spec["tool_workflow"],
                                      "browser_only": spec["browser_only"]}},
                ))
            elif entry.kind == KIND_DESKTOP:
                tools.append(Tool(
                    name=entry.name, description=spec["description"],
                    inputSchema=spec["input_schema"], outputSchema=spec["output_schema"],
                    _meta={"anyuse": {"compile_class": spec["compile_class"],
                                      "proof_digest": spec["proof"]["artifact_digest"]}},
                ))
            elif entry.kind == KIND_SKILL:
                tools.append(Tool(
                    name=entry.name, description=spec["description"],
                    inputSchema=spec["inputSchema"], outputSchema=spec.get("outputSchema"),
                    _meta=spec.get("_meta"),
                ))
        for spec in rea_specs():
            tools.append(Tool(name=spec["name"], description=spec["description"],
                              inputSchema=spec["input_schema"],
                              _meta={"anyuse": {"kind": "rea", "verification_tier": spec["verification_tier"],
                                                "require_restoration": spec["require_restoration"]}}))
        blocked = collisions()
        return [tool for tool in tools if tool.name not in blocked] if blocked else tools
    
    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        """
        Handle tool invocations.
        
        All tools delegate to the anyuse Agent which handles actual execution.

        ANYUSE_ROBOTS: a tool call is work the caller explicitly started - under
        ``user_initiated`` its runs may fetch paths robots.txt disallows (recorded); the context reaches the worker
        threads (``asyncio.to_thread`` copies it).
        """
        from anyuse.execution.api_permission import user_initiated
        with user_initiated("mcp"):
            return await _call_tool(name, arguments)

    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        try:
            if selected_profile != PROFILE_META and name in collisions():
                return [TextContent(type="text", text=json.dumps({"ok": False, "status": "refused",
                    "reason": "ambiguous tool name", "model_calls": 0, "automatic_retry": False}))]
            if selected_profile != PROFILE_META and any(spec["name"] == name for spec in safe_rea_specs()):
                payload = await run_rea_checked(name, arguments)
                return [TextContent(type="text", text=json.dumps(payload, default=str))]
            # MCP clients may call a remembered or guessed name without first
            # refreshing tools/list.  Authorization is the current profile's
            # exact advertised set (or an older name of one of its tools), not
            # merely the presence of a dispatch branch.
            table = tool_table()
            entry = table.resolve(name)
            if entry is None:
                shared = table.sharing(name)
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": (f"Tool {name!r} is not available in the "
                              f"{selected_profile!r} MCP profile"
                              + (f"; {len(shared)} tools share that name now, each under its own: "
                                 f"{', '.join(shared)}" if shared else "")),
                    **({"tools": shared} if shared else {}),
                }))]
            if name != entry.name:
                # an older name: the SDK looks tools up by their listed name, so it validated nothing
                refused = alias_input_error(entry, arguments)
                if refused is not None:
                    return refused
            name = entry.source
            if selected_profile == PROFILE_META:
                if collisions():
                    return [TextContent(type="text", text=json.dumps({"ok": False, "status": "refused",
                        "reason": "ambiguous tool names in server configuration", "model_calls": 0,
                        "automatic_retry": False}))]
                payload = await meta_tools.dispatch(
                    name, arguments, compiled_storage=compiled_storage,
                    hosts_path=compiled_hosts_path, desktop_storage=desktop_storage,
                    run_web=run_compiled_checked, run_desktop=run_desktop_checked,
                    desktop_specs=desktop_specs, run_agent=run_agent_checked,
                    web_tool_specs=web_tool_specs, run_web_tool=run_web_tool_checked,
                    rea_specs=safe_rea_specs, run_rea=run_rea_checked)
                return [TextContent(type="text", text=json.dumps(
                    payload, separators=(",", ":"), default=str))]
            if entry.kind == KIND_SKILL:
                payload = await asyncio.to_thread(
                    skill_tools.handle, name, arguments, directory=skills_dir,
                    storage=compiled_storage, hosts_path=compiled_hosts_path,
                    executor=compiled_executor,
                    launch_profile=selected_profile != PROFILE_FULL,
                    elicitor=elicitor())
                if payload is not None:
                    text = json.dumps(payload, indent=2, default=str)
                    return ([TextContent(type="text", text=text)], json.loads(text))
            elif entry.kind != KIND_STATIC:
                if entry.kind == KIND_WEB_TOOL:
                    payload = await run_web_tool_checked(name, arguments)
                elif entry.kind == KIND_WEB_API:
                    payload = await asyncio.to_thread(
                        compiled_tools.run_typed_tool, name, arguments,
                        storage=compiled_storage, hosts_path=compiled_hosts_path,
                        executor=compiled_executor,
                        require_proved=selected_profile != PROFILE_FULL)
                    payload = await ask_about_block(payload)
                else:
                    payload = await run_desktop_checked(name, arguments)
                if payload is not None:
                    # Dynamic tools advertise a typed outputSchema, so the SDK
                    # requires structured content next to the text (without it
                    # every call came back as "Output validation error"). The
                    # JSON round-trip normalises non-JSON values (default=str)
                    # before the MCP library validates the contract.
                    text = json.dumps(payload, indent=2, default=str)
                    return ([TextContent(type="text", text=text)], json.loads(text))
            elif name == "run_task":
                return await _run_task(arguments)
            elif name == "run_workflow":
                return await _run_workflow(arguments)
            elif name == "list_workflows":
                return await _list_workflows()
            elif name == "get_context":
                return await _get_context()
            elif name == "get_workflow_details":
                return await _get_workflow_details(arguments)
            elif name == "desktop_execute":
                return await _desktop_execute(arguments)
            elif name == "desktop_status":
                return await _desktop_status()
            elif name == "get_guide":
                return await _get_guide(arguments)
            elif name in (compiled_tools.TOOL_LIST_NAME, compiled_tools.TOOL_RUN_NAME,
                          compiled_tools.TOOL_EXPLAIN_NAME):
                # Synchronous and side-effect-free apart from the workflow's own
                # health record; run it off the event loop so a slow endpoint
                # cannot stall the server.
                if name == compiled_tools.TOOL_LIST_NAME:
                    payload = {"compiled_workflows": compiled_tools.list_compiled(
                        compiled_storage, compiled_hosts_path)}
                elif name == compiled_tools.TOOL_RUN_NAME:
                    payload = await run_compiled_checked(
                        str(arguments.get("workflow", "")),
                        dict(arguments.get("parameters") or {}))
                else:
                    payload = compiled_tools.explain_compiled(
                        str(arguments.get("workflow", "")), compiled_storage,
                        compiled_hosts_path)
                return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
            elif name == learn_tools.TOOL_PROPOSE_NAME:
                # Drives a CONTAINER browser and may take a minute; off the loop.
                payload = await asyncio.to_thread(learn_tools.handle, name, arguments)
                return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
            elif name == agent_tools.TOOL_NAME:
                # One agent run (a guarded page or the guest); may take minutes; off the loop.
                payload = await run_agent_checked(arguments)
                return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Unknown tool: {entry.name}"
                })
            )]
        except Exception as e:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__
                })
            )]
    
    return server


async def _run_task(arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Execute a natural language task."""
    from anyuse.core.agent import Agent
    
    task = arguments.get("task", "")
    if not task:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "Task is required"
            })
        )]
    
    max_turns = arguments.get("max_turns", 20)
    verbose = arguments.get("verbose", False)
    
    # Full-profile escape hatch only.  Even there MCP never auto-confirms.
    agent = Agent(
        auto_confirm=False,
        max_turns=max_turns,
        verbose=verbose,
        auto_learn=True,   # Learn successful tasks
        auto_replay=True,  # Replay known workflows
    )
    
    # Run in executor to not block async
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, agent.run, task)
    
    # Return structured result
    response = result.to_dict()
    
    return [TextContent(
        type="text",
        text=json.dumps(response, indent=2, default=str)
    )]


async def _run_workflow(arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Execute a saved workflow by name."""
    from anyuse.core.agent import Agent
    
    name = arguments.get("name", "")
    if not name:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "Workflow name is required"
            })
        )]
    
    agent = Agent(auto_confirm=False)
    
    # Run in executor to not block async  
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, agent.play, name)
    
    response = result.to_dict()
    
    return [TextContent(
        type="text",
        text=json.dumps(response, indent=2, default=str)
    )]


async def _list_workflows() -> Sequence[TextContent]:
    """List all saved workflows."""
    from anyuse.memory.storage import WorkflowStorage
    
    storage = WorkflowStorage()
    workflows = storage.list_all()
    
    workflow_list = []
    for wf in workflows:
        workflow_list.append({
            "name": wf.name,
            "description": wf.description,
            "steps": len(wf.steps),
            "success_rate": f"{wf.success_rate:.0%}",
            "runs": wf.run_count,
            "verified": wf.verified,
        })
    
    return [TextContent(
        type="text",
        text=json.dumps({
            "workflows": workflow_list,
            "count": len(workflow_list)
        }, indent=2)
    )]


async def _get_context() -> Sequence[TextContent]:
    """Get current macOS screen context."""
    from anyuse.context.macos import MacOSContext
    
    loop = asyncio.get_event_loop()
    
    def get_context_sync():
        ctx = MacOSContext()
        return ctx.get_full_context()
    
    # Run in executor to avoid blocking
    context = await loop.run_in_executor(None, get_context_sync)
    
    return [TextContent(
        type="text",
        text=json.dumps({
            "frontmost_app": context.get("frontmost_app", "Unknown"),
            "running_apps": context.get("running_apps", []),
            "screen": context.get("screen_info", {}),
            "open_windows": context.get("open_windows", {}),
        }, indent=2, default=str)
    )]


async def _get_workflow_details(arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Get detailed information about a specific workflow."""
    from anyuse.memory.storage import WorkflowStorage
    
    name = arguments.get("name", "")
    if not name:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "Workflow name is required"
            })
        )]
    
    storage = WorkflowStorage()
    workflow = storage.get(name) or storage.find(name)
    
    if not workflow:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Workflow '{name}' not found"
            })
        )]
    
    # Build step summaries (without large binary data)
    steps_summary = []
    for i, step in enumerate(workflow.steps):
        steps_summary.append({
            "index": i,
            "action": step.action,
            "description": step.description or f"{step.action} with {step.args}",
            "replay_mode": step.replay_mode,
            "checkpoint": step.checkpoint,
        })
    
    return [TextContent(
        type="text",
        text=json.dumps({
            "success": True,
            "workflow": {
                "name": workflow.name,
                "description": workflow.description,
                "steps": steps_summary,
                "step_count": len(workflow.steps),
                "trigger_phrases": workflow.trigger_phrases,
                "tags": workflow.tags,
                "success_rate": f"{workflow.success_rate:.0%}",
                "run_count": workflow.run_count,
                "verified": workflow.verified,
                "robustness_score": f"{workflow.robustness_score:.0%}",
                "created_at": workflow.created_at,
                "last_run": workflow.last_run,
            }
        }, indent=2)
    )]


# ─────────────────────────────────────────────────────────────────
# Orchestrator integration tools
# ─────────────────────────────────────────────────────────────────

# Global state for desktop_status
_current_task: dict = {"busy": False, "task_id": None, "description": None}
_desktop_lease_lock = threading.Lock()
_desktop_workers: dict = {}


def _enrich_with_restoration(response: dict, result_obj: Any, *, requested: bool = True) -> dict:
    """Keep cleanup evidence when desktop execution shapes its public response."""
    from anyuse.core.result import RestoreReport
    restoration = RestoreReport.from_result(result_obj) if requested else RestoreReport()
    response.update({
        "restoration": restoration.to_dict(),
        "cleanup_status": restoration.status,
        "completion_status": ("complete" if getattr(result_obj, "success", False)
                              and restoration.status in {"restored", "not_requested"} else "incomplete"),
    })
    return response


def _enrich_with_guidance(
    response: dict,
    result_obj: Any,
    task: str,
) -> dict:
    """
    Check if the execution result carries a needs_guidance signal and
    enrich the response dict accordingly.

    The guidance signal can come from:
    1. OrchestratorResult.needs_guidance (state graph execution path)
    2. AgentResult with guidance in its dict representation
    3. PlanExecutor result with guidance context

    Args:
        response: The existing orchestrator response dict.
        result_obj: The raw execution result object (AgentResult, PlanResult, etc.)
        task: The original task description.

    Returns:
        The response dict, possibly updated with needs_guidance status.
    """
    if result_obj is None:
        return response

    # Check if the result object has needs_guidance directly
    needs_guidance = getattr(result_obj, "needs_guidance", False)
    guidance_context = getattr(result_obj, "guidance_context", None)

    # Also check the dict representation for nested guidance signals
    if not needs_guidance:
        result_dict = None
        if hasattr(result_obj, "to_dict"):
            try:
                result_dict = result_obj.to_dict()
            except Exception:
                pass
        elif isinstance(result_obj, dict):
            result_dict = result_obj

        if result_dict:
            # Check execution_result sub-dict for intervention_guidance
            exec_result = result_dict.get("execution_result")
            if isinstance(exec_result, dict):
                ig = exec_result.get("intervention_guidance")
                if ig:
                    needs_guidance = True
                    guidance_context = ig
                gfs = exec_result.get("guidance_from_steps")
                if gfs and not needs_guidance:
                    needs_guidance = True
                    guidance_context = gfs

    if needs_guidance and guidance_context:
        # Compute partial results from the response
        actions_taken = response.get("actions_taken", 0)
        partial = guidance_context.get("partial_results", {})
        steps_completed = partial.get("steps_completed", actions_taken)
        steps_remaining = partial.get("steps_remaining", 0)

        response["status"] = "needs_guidance"
        response["task"] = task
        response["blocked_step"] = {
            "intent": guidance_context.get("intent", ""),
            "what_tried": guidance_context.get("what_tried", []),
            "current_context": guidance_context.get("current_context", ""),
            "options_available": guidance_context.get("options_available", []),
            "question": guidance_context.get("question", ""),
        }
        response["partial_results"] = {
            "steps_completed": steps_completed,
            "steps_remaining": steps_remaining,
        }

    return response


async def _try_state_graph_execution(task: str, *, action_executor=None):
    """Try state graph execution for a task. Returns OrchestratorResult or None."""
    started_at = None
    try:
        from anyuse.execution.orchestrator import ExecutionOrchestrator
        from anyuse.memory.storage import WorkflowStorage
        from anyuse.guides.storage import GuideStorage

        workflow_storage = WorkflowStorage()
        guide_storage = GuideStorage()

        workflow = workflow_storage.find(task)
        if not workflow or not workflow_storage.has_template(workflow.id):
            return None

        template = workflow_storage.get_template(workflow.id)
        if template is None:
            return None

        checker = getattr(action_executor, "_compliance_checker", None)
        if checker is not None:
            control = getattr(checker, "control", None)
            allowed, reason = control.check() if control is not None else (True, "")
            required_app = template.required_app
            if isinstance(required_app, dict):
                required_app = required_app.get("name") or required_app.get("bundle_id")
            if allowed and required_app:
                allowed, reason = checker.policy.is_app_allowed(required_app)
            if not allowed:
                from anyuse.execution.orchestrator import OrchestratorResult
                from anyuse.execution.outcome import REFUSED
                return OrchestratorResult(success=False, message="State-graph BOUNDARY refused: " + reason,
                                          mode="replay", outcome_kind=REFUSED)

        app_guide = None
        if template.required_app:
            app_guide = guide_storage.get_by_app(template.required_app)

        orchestrator = ExecutionOrchestrator(
            workflow_storage=workflow_storage,
            guide_storage=guide_storage,
            action_executor=action_executor,
            verbose=True,
        )

        import time
        started_at = time.time()
        result = await orchestrator.run(task, template, app_guide=app_guide)
        return result

    except Exception as exc:
        if started_at is not None:
            from anyuse.core.result import RestoreReport
            from anyuse.execution.orchestrator import OrchestratorResult
            from anyuse.execution.outcome import INCOMPLETE
            return OrchestratorResult(
                success=False, message=f"State-graph execution failed: {type(exc).__name__}: {exc}",
                duration_seconds=time.time() - started_at, mode="replay", outcome_kind=INCOMPLETE,
                restoration_report=RestoreReport.from_interrupted_orchestrator(orchestrator),
            )
        import logging
        logging.getLogger(__name__).debug(
            "State graph execution unavailable for MCP", exc_info=True
        )
        return None


def _release_desktop_lease(task_id, worker):
    # Called only when the actual operation (including its synchronous worker)
    # has finished. Timed-out clients cannot release someone else's desktop.
    with _desktop_lease_lock:
        _desktop_workers.pop(task_id, None)
        if _current_task["task_id"] == task_id:
            _current_task.update(busy=False, task_id=None, description=None)
    if not worker.cancelled():
        worker.exception()  # retrieve an abandoned client's exception


async def _desktop_execute(arguments: dict[str, Any]) -> Sequence[TextContent]:
    from anyuse.core.restriction import ExecutionControl
    import uuid
    timeout = arguments.get("timeout", 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        return [TextContent(type="text", text=json.dumps({"status": "error", "result": "timeout must be positive and finite"}))]
    task_id = str(uuid.uuid4())[:8]
    with _desktop_lease_lock:
        if _current_task["busy"]:
            return [TextContent(type="text", text=json.dumps({"status": "busy", "current_task": _current_task["task_id"],
                "result": "Another desktop execution still owns the desktop", "automatic_retry": False}))]
        _current_task.update(busy=True, task_id=task_id, description=str(arguments.get("task") or "")[:100])
    control = ExecutionControl(time.monotonic() + timeout)
    worker = asyncio.create_task(_desktop_execute_owned(arguments, control))
    _desktop_workers[task_id] = worker
    worker.add_done_callback(lambda done: _release_desktop_lease(task_id, done))
    try:
        done, _ = await asyncio.wait({worker}, timeout=max(0, control.deadline - time.monotonic()))
        if done:
            return worker.result()
        control.cancel("desktop execution deadline expired")
        response = _enrich_with_restoration({"status": "timeout", "result": control.reason,
            "current_task": task_id, "worker_running": True, "state_uncertain": True,
            "automatic_retry": False, "terminal_on_failure": True}, None, requested=True)
        return [TextContent(type="text", text=json.dumps(response))]
    except asyncio.CancelledError:
        control.cancel("desktop caller cancelled; awaiting the active worker")
        raise


async def _desktop_execute_owned(arguments: dict[str, Any], control) -> Sequence[TextContent]:
    """Execute a desktop task with BOUNDARY enforcement.
    
    This is the tool an orchestrator's desktop node calls.
    
    Behavior:
    1. Has plan? → execute steps by index, evaluate checkpoints via callbacks
    2. No plan?  → execute task as free-form instruction
    3. Has plan but no steps? → fall back to plan.task_text
    
    Action rules are checked at dispatch. A guard rule without a configured
    local evaluator fails closed; a callback URL alone is not an approval.
    """
    from anyuse.core.agent import Agent
    
    task = arguments.get("task", "")
    if not task:
        return [TextContent(
            type="text",
            text=json.dumps(_enrich_with_restoration(
                {"status": "error", "result": "No task description provided", "actions_taken": 0},
                None, requested=False))
        )]
    
    plan = arguments.get("plan")
    boundary = arguments.get("boundary", {})
    credentials = arguments.get("credentials", {})
    turn_limit = arguments.get("turn_limit", 50)
    orchestrator_callbacks = arguments.get("orchestrator_callbacks", [])
    
    execution_result_obj = None
    execution_started = False
    try:
        # Create agent with boundary enforcement
        agent = Agent(
            auto_confirm=False,
            max_turns=turn_limit,
            auto_learn=True,
            auto_replay=True,
            boundary_rules=boundary if boundary else None,
            scoped_credentials=credentials if credentials else None,
        )
        from anyuse.core.restriction import ComplianceChecker
        if getattr(agent, "_compliance_checker", None) is None:
            agent._compliance_checker = ComplianceChecker()
        agent._compliance_checker.control = control
        agent._execution_control = control
        control.require()
        
        # Pre-load scoped credentials into the vault
        if credentials and agent.credentials:
            for alias, value in credentials.items():
                agent.set_credential(alias, value)
        
        # --- Plan mode vs free-form ---
        # Track the raw result for guidance and restoration reporting.

        if plan and (plan.get("steps") or plan.get("task_text")):
            # Plan execution with checkpoints
            from anyuse.mcpserver.plan_executor import PlanExecutor

            executor = PlanExecutor(
                agent=agent,
                plan=plan,
                boundary=boundary,
            )
            execution_started = True
            plan_result = await executor.execute()
            orchestrator_response = plan_result.to_dict()
            execution_result_obj = plan_result

        else:
            # Free-form execution — try state graph first, then AI agent
            execution_started = True
            orchestrator = getattr(agent, "orchestrator", None)
            action_executor = getattr(orchestrator, "executor", None)
            if action_executor is not None:
                action_executor.set_compliance_checker(agent._compliance_checker)
            sg_result = await _try_state_graph_execution(task, action_executor=action_executor)
            if sg_result is not None:
                orchestrator_response = {
                    "status": "ok" if sg_result.success else "error",
                    "result": sg_result.message,
                    "actions_taken": len(sg_result.execution_result.steps) if sg_result.execution_result else 0,
                    "screenshots": [],
                    "checkpoint_results": [],
                    "execution_mode": "state_graph",
                    "duration_seconds": sg_result.duration_seconds,
                }
                execution_result_obj = sg_result
            else:
                # Fall back to AI agent
                from .plan_executor import run_agent_worker
                result = await run_agent_worker(agent, task)

                response = result.to_dict()
                actions = response.get("turns_used", 0)

                # Get compliance actions count if available
                if boundary and agent._compliance_checker:
                    actions = agent._compliance_checker.actions_executed

                orchestrator_response = {
                    "status": "ok" if result.success else "error",
                    "result": response.get("message", ""),
                    "actions_taken": actions,
                    "screenshots": [],
                    "checkpoint_results": [],
                    "execution_mode": "agent",
                }
                execution_result_obj = result

        control.require()
        orchestrator_response = _enrich_with_restoration(orchestrator_response, execution_result_obj)
        # Check for needs_guidance signal from the execution result
        orchestrator_response = _enrich_with_guidance(
            orchestrator_response, execution_result_obj, task
        )

        return [TextContent(
            type="text",
            text=json.dumps(orchestrator_response, indent=2, default=str)
        )]
        
    except Exception as e:
        return [TextContent(
            type="text",
            text=json.dumps(_enrich_with_restoration({
                "status": "error",
                "result": str(e),
                "actions_taken": 0,
                "screenshots": [],
                "checkpoint_results": [],
            }, execution_result_obj, requested=execution_started), default=str)
        )]
        
async def _desktop_status() -> Sequence[TextContent]:
    """Return health status of the AnyUse worker."""
    with _desktop_lease_lock:
        current = dict(_current_task)
    if current["busy"]:
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": "busy",
                "current_task": current["task_id"],
                "description": current["description"],
            })
        )]
    
    import platform as _platform
    if _platform.system() != "Darwin":
        return [TextContent(type="text", text=json.dumps(
            {"status": "unavailable", "current_task": None,
             "reason": "desktop tasks need macOS (run them in a macOS VM)"}))]
    return [TextContent(
        type="text",
        text=json.dumps({"status": "ready", "current_task": None})
    )]


async def _get_guide(arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Return auto-learned guide for an app or domain."""
    app_or_domain = arguments.get("app_or_domain", "")
    if not app_or_domain:
        return [TextContent(
            type="text",
            text=json.dumps({"success": False, "error": "app_or_domain is required"})
        )]
    
    try:
        from anyuse.guides.storage import GuideStorage
        storage = GuideStorage()
        
        guide = storage.get(app_or_domain) or storage.get_by_app(app_or_domain)
        
        if not guide:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "guide": None,
                    "message": f"No guide found for '{app_or_domain}'"
                })
            )]
        
        guide_data = {
            "name": guide.name,
            "guide_type": guide.guide_type.value if hasattr(guide.guide_type, 'value') else str(guide.guide_type),
            "use_cases": guide.use_cases[:10] if hasattr(guide, 'use_cases') else [],
            "landmarks": guide.ui_landmarks[:10] if hasattr(guide, 'ui_landmarks') else [],
            "shortcuts": dict(list(guide.common_shortcuts.items())[:10]) if hasattr(guide, 'common_shortcuts') else {},
            "navigation_tips": guide.navigation_tips[:10] if hasattr(guide, 'navigation_tips') else [],
            "linked_workflows": guide.linked_workflows[:10] if hasattr(guide, 'linked_workflows') else [],
        }

        # Add state graph summary
        try:
            graph = guide.get_state_graph()
            if graph and (graph.states or graph.transitions):
                guide_data["state_graph"] = {
                    "states": len(graph.states),
                    "transitions": len(graph.transitions),
                    "known_intents": [t.intent for t in graph.transitions[:10]],
                }
        except Exception:
            pass

        return [TextContent(
            type="text",
            text=json.dumps({
                "success": True,
                "guide": guide_data,
            }, indent=2, default=str)
        )]
    except ImportError:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "Guide system not available"
            })
        )]


def _reserve_stdout_for_protocol():
    """Keep the real stdout for JSON-RPC; send every other fd-1 write to stderr.

    stdout is the protocol channel, and code on the run path prints (e.g.
    ``WorkflowStorage`` warns about a workflow it cannot load). The real
    descriptor is duplicated for the protocol writer, then fd 1 is pointed at
    fd 2, so ``print``, ``sys.stdout`` and inherited child descriptors all land
    on stderr. Returns the protocol stream and a function that undoes it.
    """
    from io import TextIOWrapper

    import anyio

    sys.stdout.flush()
    protocol_fd = os.dup(1)
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    stream = anyio.wrap_file(TextIOWrapper(os.fdopen(protocol_fd, "wb"), encoding="utf-8"))

    def restore():
        sys.stdout.flush()
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    return stream, restore


async def serve():
    """
    Run the MCP server using stdio transport.
    
    This is the main entry point for running anyuse as an MCP server. It arms
    the spend ledger and the global daily cap (execution/spend.py).
    """
    from anyuse.execution import model_check, spend
    problem = env_value_problem(PROFILE_ENV)
    if problem:
        # a typo in the client's registration would otherwise serve the default profile without a word
        print(f"AnyUse MCP server: {problem}. Fix {PROFILE_ENV} where this server is registered.",
              file=sys.stderr, flush=True)
    model_check.stays_up()
    spend.arm()
    protocol_stdout, restore_stdout = _reserve_stdout_for_protocol()
    try:
        server = create_server()
        async with stdio_server(stdout=protocol_stdout) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    finally:
        restore_stdout()


def main():
    """Synchronous entry point."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
