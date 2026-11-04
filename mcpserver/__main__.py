"""
Entry point for running anyuse as an MCP server.

Usage:
    /path/to/anyuse/.venv/bin/python -P -m anyuse.mcpserver

Use the Python of the venv AnyUse is installed in. -P keeps an `anyuse` folder in the
client's working directory from shadowing the installed package.

This starts the MCP server using stdio transport, allowing
AI assistants like Claude Desktop to connect and use anyuse
capabilities.

Configuration via environment: compiled workflows need no model at all. The
agent tool (`agent_run`, and the cascade's agent step) needs a configured model,
for example ANYUSE_MODEL_PROVIDER=claude-cli; see docs/MODEL_CONFIGURATION.md.

Tool names match ^[a-zA-Z0-9_-]{1,64}$, the format Anthropic's and OpenAI's tool
APIs accept: agent_run, <host>__<workflow> (crates_io__search_crates),
desktop__<workflow>, routine__<name>. The older dotted names (agent.run,
<host>.<workflow>) still call the same tools; see docs/MCP_WORKFLOWS.md.
    
Example Claude Desktop config (claude_desktop_config.json):
    {
        "mcpServers": {
            "anyuse": {
                "command": "/path/to/anyuse/.venv/bin/python",
                "args": ["-P", "-m", "anyuse.mcpserver"],
                "env": {
                    "ANYUSE_DATA_DIR": "/path/to/anyuse-data"
                }
            }
        }
    }
"""

import sys

if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        # print the usage instead of starting a stdio server that waits for a client
        print(__doc__.strip())
        sys.exit(0)
    from anyuse.mcpserver.server import main
    main()

