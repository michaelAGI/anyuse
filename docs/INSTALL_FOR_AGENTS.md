# Installing AnyUse for an agent (Claude Code, Codex, OpenClaw, Hermes Agent)

Steps a coding agent can follow to install AnyUse from a clone and register
its MCP server with the `meta` profile (four fixed tools,
[MCP_META.md](MCP_META.md)). A PyPI package (`pip install anyuse`) will follow;
until then, install from the clone.

Every command uses absolute paths. Replace `/abs/path/to/anyuse` with the
clone's absolute path: the directory that contains `pyproject.toml` (the
repository root is the `anyuse` package itself).

```sh
git clone https://github.com/michaelAGI/anyuse /abs/path/to/anyuse
ANYUSE_CHECKOUT=/abs/path/to/anyuse
test -f "$ANYUSE_CHECKOUT/pyproject.toml" && echo "checkout ok"
```

## 1. Python 3.11 or newer

```sh
python3 -c 'import sys; print(sys.version.split()[0]); sys.exit(sys.version_info < (3, 11))'
```

Exit status 0 means the interpreter is new enough. If it is older, look for a
newer one (`command -v python3.13 python3.12 python3.11`) and use that
interpreter's name in step 2 instead of `python3`. Do not install a Python
without asking the user.

## 2. A virtual environment and the package

With only the `mcp` extra, in editable mode, so the server runs the clone's own
code:

```sh
python3 -m venv "$ANYUSE_CHECKOUT/.venv"
"$ANYUSE_CHECKOUT/.venv/bin/python" -m pip install -e "${ANYUSE_CHECKOUT}[mcp]"
```

This installs the compile path and the MCP server and nothing that drives a
desktop. The macOS desktop stack is the separate `desktop` extra
(`pyproject.toml`); the `meta` profile does not need it.

## 3. Self-check

```sh
"$ANYUSE_CHECKOUT/.venv/bin/anyuse" doctor --install
```

It prints one `PASS` or `FAIL` line per check and exits 1 if any fails:

| check | passes when |
|---|---|
| `python` | the venv's Python is 3.11 or newer |
| `imports` | `mcp`, `httpx`, `click`, `jsonschema`, `cryptography.fernet` and AnyUse's MCP modules import |
| `data dir writable` | the data directory (`~/.anyuse`, or `$ANYUSE_DATA_DIR`) accepts a probe file, or, when it does not exist yet, its nearest existing parent is writable |
| `mcp entry` | `python -m anyuse.mcpserver` resolves and its server module imports |
| `settings readable` | `settings.toml` (if present) parses, and it shows the effective `ANYUSE_MCP_PROFILE` |

A failing line is followed by `->` and the fix. `--json` prints the same rows
as JSON. The check opens no socket, starts no server and calls no model.
(Plain `anyuse doctor` checks the compile path, including the container
runtime, the model roles and the learn endpoint. A missing one is marked `opt`
and does not change the exit status: it exits 1 only when a required check,
such as the HTTP client, fails. Serving compiled tools over MCP needs none of the
optional ones.)

## 4. Register the MCP server with the meta profile

The entry point is `python -P -m anyuse.mcpserver` (`mcpserver/__main__.py`),
speaking MCP over stdio. Register the venv's interpreter by absolute path and
set `ANYUSE_MCP_PROFILE=meta` in the server's environment.

These commands change the user's agent configuration. Show them to the user
and run them only with the user's agreement.

**Claude Code** (user scope: available in every project). Put the server name
before `--env` (`--env` takes several values and would swallow the name); `--`
separates the server command:

```sh
claude mcp add --transport stdio --scope user anyuse --env ANYUSE_MCP_PROFILE=meta \
  -- "$ANYUSE_CHECKOUT/.venv/bin/python" -P -m anyuse.mcpserver
claude mcp list
```

**Codex**:

```sh
codex mcp add anyuse --env ANYUSE_MCP_PROFILE=meta \
  -- "$ANYUSE_CHECKOUT/.venv/bin/python" -P -m anyuse.mcpserver
codex mcp list
```

or the same entry written into `~/.codex/config.toml` by hand:

```toml
[mcp_servers.anyuse]
command = "/abs/path/to/anyuse/.venv/bin/python"
args = ["-P", "-m", "anyuse.mcpserver"]
env = { ANYUSE_MCP_PROFILE = "meta" }
```

`-P` keeps an `anyuse` folder in Codex's working directory from being imported
instead of the installed package, as in the command lines above.

No `PYTHONPATH` is needed: the editable install makes `anyuse` importable
from the venv. To use a data directory other than `~/.anyuse`, add
`--env ANYUSE_DATA_DIR=/abs/path` (Claude Code, Codex) or put it in the
`env` table.

Codex starts the server with a nearly empty environment: `HOME`, `PATH`,
`USER`, the locale and a few more, plus what the entry's `env` sets. Variables
exported in your shell, such as `ANYUSE_CDP_ENDPOINT`, `ANYUSE_MODEL_PROVIDER`
or a model key, do not reach it. Add them with more `--env` flags, or forward
them by name with `env_vars = ["ANYUSE_CDP_ENDPOINT"]` in the table. Values
saved with `anyuse settings set` do reach it, because the server reads them
from the data directory.

**OpenClaw** (`~/.openclaw/openclaw.json`; `openclaw config file` prints the
path it uses):

```sh
openclaw mcp add anyuse --command "$ANYUSE_CHECKOUT/.venv/bin/python" \
  --arg -P --arg -m --arg anyuse.mcpserver --env ANYUSE_MCP_PROFILE=meta
openclaw mcp doctor anyuse --probe
```

or by hand, under `mcp.servers`:

```json5
{ mcp: { servers: { anyuse: {
    command: "/abs/path/to/anyuse/.venv/bin/python",
    args: ["-P", "-m", "anyuse.mcpserver"],
    env: { ANYUSE_MCP_PROFILE: "meta" },
} } } }
```

**Hermes Agent** (`~/.hermes/config.yaml`). `hermes mcp add` asks which tools
to enable; for a setup without questions, add the entry by hand:

```yaml
mcp_servers:
  anyuse:
    command: "/abs/path/to/anyuse/.venv/bin/python"
    args: ["-P", "-m", "anyuse.mcpserver"]
    env:
      ANYUSE_MCP_PROFILE: "meta"
```

Then `hermes mcp test anyuse` (exit 0 means it connected), and `/reload-mcp` in
a running session. Hermes, like Codex, passes the server only the variables in
`env` plus a small baseline.

## 5. Verify with one offline call

The quickstart's MCP client starts the server exactly as the agent will
(`<venv python> -P -m anyuse.mcpserver` over stdio), lists its tools and calls
one. `list_sites` reads only local files:

```sh
ANYUSE_MCP_PROFILE=meta "$ANYUSE_CHECKOUT/.venv/bin/python" \
  "$ANYUSE_CHECKOUT/examples/quickstart/mcp_call.py" list_sites '{}'
```

Expected on a fresh data directory (exit status 0):

```
tools/list: 4 tools
  - list_sites
  - list_operations
  - call_operation
  - login
tools/call list_sites {}:
  ok: True
  count: 0
```

`count` is the number of sites with compiled operations. Exactly four tools
means the `meta` profile is in effect; eight or more means `default`, so check
the `ANYUSE_MCP_PROFILE` value. After registering in step 4, a new agent
session lists the same four tools (Claude Code shows them as
`mcp__anyuse__list_sites` and so on).

`count` stays 0 until the user compiles a tool into their own data directory
(`~/.anyuse`). The offline quickstart's tool lives in a throwaway folder, so it
does not count; compiling a real site ([QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md))
is the step that gives the agent something to call.

## What the agent can do afterwards

* `list_sites` / `list_operations` read the compiled records. `call_operation`
  runs a compiled operation; on failure it returns
  `{ok: false, class, reason, next}`. Follow `next` at most once. A `next` that
  starts with `hand to the user (do not run it yourself):` is a CLI step for the
  user (confirming a host, proving a workflow, answering an inbox card, signing
  in, turning on desktop actuation); pass it on.
* The agent tool (`agent_run`, or `call_operation` on site `agent`) browses,
  reads and fills, and with `compile: true` saves a proved tool from a
  successful run. A commit step (a login, a cart, a form sent, a payment, a
  deletion) stops with `outcome: "human_required"` for the user to do, unless the
  user has set `ANYUSE_MCP_AGENT_COMMITS=agent`.

## Make your tools show up as skills

An agent that does not know a compiled tool exists browses the site again.
`anyuse skills export DIR` writes one Agent Skills folder per proved tool the MCP
server serves, `DIR/anyuse-<tool>/SKILL.md`. Its description says what the tool
looks up and on which host, so the agent picks it when a task fits. Its body says
how to call the tool over MCP or from a shell
(`anyuse --json workflow play NAME --no-fallback --param k=v`), and what to do when a
call fails. A skill exported with `--profile meta` leads with a `call_operation` call,
which the `meta` server from step 4 serves; one exported for the `default` profile
leads with the typed tool. `--no-fallback` keeps the shell call to the proved request:
no agent, and no recording replayed on your desktop. A configured check model is asked
only when the call checks a request that is out of service and no result page shows
its list.

The command writes only into the DIR you name; it never picks an agent's folder.
The four agents read the same layout from these folders:

| agent | user-level folder | project folder |
|---|---|---|
| Claude Code | `~/.claude/skills/` | `.claude/skills/` |
| Codex | `~/.agents/skills/` | `.agents/skills/` |
| OpenClaw | `~/.agents/skills/` or `~/.openclaw/skills/` | |
| Hermes Agent | `~/.hermes/skills/` | |

The skills call `anyuse` by name, so put the venv on `PATH` first. `--profile meta`
matches the server registered in step 4 with `ANYUSE_MCP_PROFILE=meta`:

```sh
export PATH="$ANYUSE_CHECKOUT/.venv/bin:$PATH"
anyuse skills export ~/.claude/skills --profile meta      # Claude Code
anyuse skills export ~/.agents/skills --profile meta      # Codex (OpenClaw reads it too)
anyuse skills export ~/.openclaw/skills --profile meta    # OpenClaw
anyuse skills export ~/.hermes/skills --profile meta      # Hermes Agent
```

If you gave the server its own `ANYUSE_DATA_DIR` in step 4, set the same value
when you run the export, so it reads the same tools.

It prints one line per tool: `wrote` with the file, or `skipped` with the reason,
then a summary that names the profile. `--json` prints one object with `profile`,
`written`, `skipped` and `reason`; `reason` is
`null` unless the folder is unusable, a skill could not be written, or a tool
named with `--tool` was not exported. The command then exits 1 (2 when DIR is
not a folder).

* **What is exported:** web tools whose proof status is `proved` and still matches
  the tool, that are read-only, on an acknowledged host and in service (a read-only
  POST also needs `ANYUSE_API_READONLY_POST=on` and its endpoint approved); tools only
  the browser replay proved (each call needs a container browser); desktop tools
  the MCP server runs in the guest VM. A web tool taken out of service because the
  site did not answer is exported too: it goes back in service once the site answers.
  One taken out because its answers failed their checks is skipped. Anything else is
  skipped with what to do about it. `--tool NAME` (repeatable) exports only the named
  tools.
* **Names:** `anyuse-` plus the tool name, lower-cased, every other character run
  turned into one hyphen, at most 64 characters. A second tool that maps to the
  same name gets `-2`, then `-3`, and keeps it on later exports.
* **Running it again:** a folder is recognised as written by this command through
  `metadata.generated-by: anyuse` in its `SKILL.md`. It is rewritten when its
  content would change (a recompile, a new proof, a new host); `--force` rewrites
  it anyway.
  A folder the command did not write is never touched. A tool you delete keeps its
  folder: remove `DIR/anyuse-<tool>` yourself.
* **Claude Code's `allowed-tools`** names the one tool the profile serves:
  `mcp__anyuse__call_operation` for `--profile meta`, `mcp__anyuse__<typed tool>` for
  `--profile default`. That assumes the server was added under the name `anyuse`, as
  the `claude mcp add` line in step 4 does. Without `--profile` the export follows
  `ANYUSE_MCP_PROFILE` as this shell or `anyuse settings set` has it, which is not
  the server's `--env`. Export again after you change the server's profile: the skills
  are rewritten for the new one. Under `meta` the pre-approved `call_operation` runs
  any compiled operation without a permission prompt while the skill is active; its
  own checks (proof, read-only, acknowledged hosts) still apply.
* **What the text is made of:** the record's own fields (host, tool name,
  parameter names and types, proof values you gave, the result keys, when it was
  proved). Page text and result values never go into a skill, because the agent
  reads the skill as instructions.
