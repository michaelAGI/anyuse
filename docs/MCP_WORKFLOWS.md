# Compiled workflows over MCP

AnyUse's MCP server lets an external agent (Claude Code, Codex, any MCP client)
discover what AnyUse can already do deterministically and call it directly: one typed
tool for every **approved** workflow compiled to a proved request
([COMPILE.md](COMPILE.md)), plus discovery tools.

## Profiles

`ANYUSE_MCP_PROFILE=default` is the shipped, fail-closed profile. It serves compiled
and proved web and desktop tools, saved browser-replayed web tools
([MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md)), read-only metadata and status, `agent_run`,
and proposal-only learning. Raw desktop control (`run_task`, `desktop_execute`, raw
workflow replay, screen context) is neither listed nor callable. `meta` serves the same
operations through four fixed tools ([MCP_META.md](MCP_META.md)). `full` adds
compatibility and debug tools and needs a confirmation in the terminal.

Compiled desktop records are listed only while their proof digest matches, and their
pinned steps run through the checked executor. A call started with its values asks
nothing; a record whose approval level holds steps (`all` / `destructive`) is not run
over MCP ([DESKTOP_TOOLS.md](DESKTOP_TOOLS.md)).

## Typed tools

`tools/list` adds names of the form `<host>__<workflow>`, the host's dots as
underscores, for example `api_example__detail(q) -> {id, title, summary}`, each with an
input JSON Schema, the output schema, a proof summary and id, the template digest, the
cascade tier, latency at compile time, the `robots.txt` status, and version pins. Only `input` parameters
appear: constants stay inside the proved template, and values taken from a response
are filled by the chain, never by the agent.

A workflow becomes a callable tool only when its proof passed and is current, it is
still the preferred step, every host is acknowledged, and the complete request is
classified `read_only`. Other records can still appear in `list_compiled_workflows`
as discovery entries. Descriptions are built only from trusted enums and pins, never
from page text. An offline transcript is in `examples/mcp-typed-tools.json`.

### Tool names

Every listed name, in every profile, matches `^[a-zA-Z0-9_-]{1,64}$`, the format
Anthropic's and OpenAI's tool APIs accept, so a client can pass the names through
unchanged:

| tool | name |
|---|---|
| the agent harness | `agent_run` |
| a compiled request or a saved web tool | `<host>__<workflow>` (`crates_io__search_crates`) |
| a compiled desktop tool | `desktop__<workflow>` |
| a skill or routine | `skill__<name>`, `routine__<name>` |

Characters outside that set become `_`. A name longer than 64 characters is cut and
ends in `_` plus 8 hex characters of the whole name's sha256, so two long names stay
apart.

Two tools that would get the same name (`late.name` and `late_name`, or the same
workflow name on `crates.io` and `crates.io:8443`) are both listed with a suffix of 6
hex characters hashed from their own host and workflow (a desktop tool: its workflow
and directory), whichever was compiled first. A tool added later never takes a name
over: the shared name, and an older dotted name both tools had, then call neither, and
a call by one returns the names that do (`tools` in the error). Each tool's suffixed
name always calls it, also once the other one is gone and its plain name is listed
again. A saved web tool whose name a compiled request has gets the same suffix.

Names from before this rule (`agent.run`, `<host>.<workflow>`, `desktop.<workflow>`,
`routine.<name>`) still call their tool, with the same input check, so existing client
configs and older resume hints keep working; `tools/list` returns only the new names.
The rule is in `mcpserver/tool_names.py`.

## Discovery tools

### `list_compiled_workflows`

No arguments. One row per compiled record:

```json
{"compiled_workflows": [{
  "workflow": "crates_search",
  "kind": "request",
  "parameters": ["q"],
  "result_shape": "list",
  "urls": ["https://crates.io/api/v1/crates"],
  "host": "crates.io",
  "hosts": ["crates.io"],
  "host_acknowledged": true,
  "risk_class": "read_only",
  "runnable_without_human": true,
  "proof": {"id": "9f2c0a71b4de", "passed": true,
            "fresh_cases_passed": 2, "fresh_cases_checked": 2,
            "what_was_checked": "passed on 2 fresh value(s) not present in the recording"},
  "health": {"preferred": true, "consecutive_failures": 0, "total_runs": 14,
             "demoted_reason": "", "consecutive_outages": 0},
  "compiled_at": "2026-11-03T14:05:11+02:00",
  "source_recording": "crates_search.rec.json",
  "origin": "recording",
  "browser_only": false
}]}
```

- `kind` is `request`, `chain` or `browser_replay`; `result_shape` is `list` or `fields`.
  A `browser_replay` tool (saved by `anyuse learn` when only the browser replay
  reproduced the page) has no compiled request: `needs_browser` is true, `note` says
  so, and it runs only while `ANYUSE_CDP_ENDPOINT` or `ANYUSE_LEARN_ENDPOINT` names a
  browser (`available_via: "browser_replay"`).
- `proof.id` identifies this template together with the evidence it was kept on.
- `origin` is `recording` (a person recorded it) or `agent` (`anyuse learn` did; then
  `learned_from` holds the task, start URL and cost). The proof standard is the same,
  but an agent-made tool is proved against the page the agent reached; read
  `learned_from.task` before relying on it.
- `browser_only` marks a host that refuses AnyUse's HTTP client but serves the
  browser; the compiled HTTP request is never tried there.

### `run_compiled_workflow`

```json
{"workflow": "crates_search", "parameters": {"q": "regex"}}
```

```json
{"ok": true, "workflow": "crates_search",
 "served_by": "api", "seconds": 0.371, "llm_calls": 0, "status": 200,
 "result": {"items": ["regex", "regex-lite", "regex-automata"]},
 "provenance": {"proof_id": "9f2c0a71b4de", "host": "crates.io",
                "kind": "request", "served_by": "api",
                "rungs_attempted": ["api"],
                "urls": ["https://crates.io/api/v1/crates"],
                "compiled_at": "2026-11-03T14:05:11+02:00"}}
```

`llm_calls` counts every model call actually made on the run; a run is model-free
only when it is zero.

### `propose_learn_candidate` (proposals only)

`{task, url, value, param?, detail?, budget_usd?}`: an agent does the task once, for one
value, in the container browser named by `ANYUSE_LEARN_ENDPOINT`, and the tool returns
the candidate (final URL, title, a preview of the list, where the value sits in the
URL, the cost). It writes nothing unless `ANYUSE_MCP_CANDIDATE_STORE` names a store
(files mode 0600, expiring after `ANYUSE_LEARN_CANDIDATE_TTL_HOURS`, default 24).

A proposal is confirmed in the CLI, which compiles, proves and persists the tool;
there is no confirm tool over MCP. The endpoint check refuses a loopback address and
a browser with pages open; point it at a throwaway browser with no sign-ins.

## What it refuses

| situation | response |
|---|---|
| the fresh-input proof never passed | refused |
| the compiled request was taken out of service (demoted: answers that failed its checks, a request that kept failing while the site's own pages answered, or an older record taken out after the site did not answer) and its check is not due | the compiled request is not sent; the browser replay or the agent may still answer, and after `answers` or `gone` the call heals the tool from its result page where it can. Otherwise the call fails with `health.demoted_reason`, `out_of_service` (`cause`: `outage`, `rate_limit`, `answers` or `gone`, `next_check_after`) and a `fallback` that says when AnyUse tries again; after a site that did not answer or asked AnyUse to wait (`rate_limit`) nothing needs recording again. On the first call after `next_check_after` the request is sent once and, when it answers and passes its checks, it is back in service and answers that call (`restored`) ([COMPILE.md](COMPILE.md#a-demoted-request-comes-back-by-itself)) |
| the complete bound request is not classified `read_only` | refused before dispatch, with the classification |
| a host has not been confirmed | refused before the first request, listing every such host |
| a declared parameter is missing | refused before anything is sent |
| a chain that opens the result named by the input finds no result of that name | `ok: false`, `not_found: true`, and nothing is opened: the search answered, so the call is not counted against the tool and does not move to the next step of the cascade; `fallback` says to ask again with the exact name the site uses |
| the compiled request fails (4xx/5xx, a changed shape, a network error) | the call moves to the browser replay, then the agent, when each is available (`rungs_tried` lists every step and why it was skipped); if neither answers, a failure on an answer from the site (a 4xx, a changed shape) heals the tool from its result page where it can ([COMPILE.md](COMPILE.md#healing-without-a-browser)), else the call returns `ok: false` with the error, zero items and the next step in `fallback`. A site that did not answer (a refused connection, a timeout, a 5xx) sets `transient` and says to try again later, is not counted against the request, and from the second such call in a row (or with the site's `Retry-After`) the request waits before it is sent again (`retry_after` gives the time); after 4 such calls in a row over at least 30 minutes while the site's own pages answer, the request is taken out of service as `gone` (`out_of_service.cause`), and the same call heals it from the result pages or, when a lower step answered, recompiles it; a destination the pre-send policy refused before the first request sets `policy_refused` and says nothing was sent; only a changed site (a changed shape, a redirect the policy refused) says to record the search again, or, for a workflow that also has a recorded desktop path, to run it locally with `anyuse workflow play`: this surface never drives the desktop |

**These tools do not drive the desktop.** The executor they use has no actuator: a
non-request step returns a refusal instead of moving the mouse. Every run updates the
workflow's health record, so repeated failures through MCP lead to the same demotion,
recompile and check as local runs, and a site that did not answer is never counted.

## Configuration

[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md) has the step-by-step setup. The server
is `python -P -m anyuse.mcpserver` over stdio; its stdout carries JSON-RPC only (stray
prints go to stderr). Run it with the Python of the virtual environment AnyUse is
installed in (`pip install -e '.[mcp]'`).

**Claude Code** (`~/.claude.json` or a project's `.mcp.json`):

```json
{
  "mcpServers": {
    "anyuse": {
      "command": "/path/to/anyuse/.venv/bin/python",
      "args": ["-P", "-m", "anyuse.mcpserver"]
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.anyuse]
command = "/path/to/anyuse/.venv/bin/python"
args = ["-P", "-m", "anyuse.mcpserver"]
```

`-P` keeps an `anyuse` folder in the client's working directory from being imported
instead of the installed package.

Any other MCP client needs the same `command` and `args`.

## Before you expose it

1. Compile something: `anyuse workflow compile <name> --recording <dir>`. For a
   new host, AnyUse reads its `robots.txt`, then asks before the proof replays the
   compiled request against it; `--yes` answers for scripts.
2. Read `proof.what_was_checked`: for those values the extraction matched what the
   page showed, under one normalisation, with order kept for lists. Other inputs and
   later versions of the endpoint are not covered.
