"""
AnyUse MCP Server

Exposes AnyUse agent capabilities via Model Context Protocol (MCP).
External AI systems (Claude Desktop, other agents) can invoke anyuse
to control macOS through natural language.

Usage:
    # Run as MCP server
    python -m anyuse.mcpserver

    # Or use the serve function
    from anyuse.mcpserver import serve
    serve()

The MCP server exposes these tools:
    - run_task: Execute a natural language task on macOS
    - run_workflow: Execute a saved workflow by name
    - list_workflows: List all saved workflows
    - get_context: Get current macOS screen context (frontmost app, running apps, etc.)
    - get_workflow_details: Get detailed info about a workflow before running it

All execution is handled by the existing anyuse Agent, which manages
supervision, learning, verification, and credentials.
"""

# Lazy on purpose: `anyuse workflow play --param` imports
# `anyuse.mcpserver.compiled_tools` on the compile-only install
# (`pip install .`, no `mcp` extra). An eager import of `server` here pulled
# in the `mcp` SDK and made that command fail with ModuleNotFoundError
#. `create_server`/`serve` still resolve on
# first attribute access and still need the `mcp` extra.


def __getattr__(name):
    if name in ("create_server", "serve"):
        from anyuse.mcpserver import server as _server
        return getattr(_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.1.0"
__all__ = [
    "create_server",
    "serve",
]

