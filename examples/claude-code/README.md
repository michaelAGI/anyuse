# AnyUse from Claude Code

With `list_compiled_workflows`, `explain_workflow` and `run_compiled_workflow`
an agent can run your compiled site workflows in milliseconds, with zero model
calls, and see the evidence for why each one is trusted. These three tools never
drive your desktop.

## 0. What you need first

One compiled workflow. If you have none, [QUICKSTART_COMPILE.md](../../docs/QUICKSTART_COMPILE.md)
walks you through one, or import a HAR you already have:

```sh
anyuse workflow compile crates_search \
  --from-har serde.har --har-param q=serde --har-expect 'serde|serde_json|serde_yaml' \
  --from-har tokio.har --har-param q=tokio --har-expect 'tokio|tokio-util|tokio-stream' \
  --from-har clap.har  --har-param q=clap  --har-expect 'clap|clap_builder|clap_derive'
```

AnyUse reads crates.io's `robots.txt`, then asks before the proof replays the
compiled request against it; `--yes` answers for scripts.
Nothing is persisted as the preferred implementation unless the fresh-input
proof passed.

## 1. Point Claude Code at the server

Install AnyUse into a venv (`pip install -e '/path/to/anyuse[mcp]'`), then
either register it once:

```sh
claude mcp add --transport stdio anyuse -- /path/to/anyuse/.venv/bin/python -P -m anyuse.mcpserver
```

or copy `.mcp.json` into your project and set `command` to that venv's Python:

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

Then `claude mcp list` should show `anyuse`. Full setup, including Codex:
[INSTALL_FOR_AGENTS.md](../../docs/INSTALL_FOR_AGENTS.md).

## 2. See what is there

```
> what AnyUse workflows can you run?
```

`list_compiled_workflows` answers with one row per compiled workflow:

```json
{
  "workflow": "crates_search",
  "ladder_tier": "1_proved_request",
  "ladder_tier_means": "one proved HTTP request; milliseconds; zero model calls; …",
  "parameters": ["q"],
  "result_shape": "list",
  "urls": ["https://crates.io/api/v1/crates"],
  "host_acknowledged": true,
  "runnable_without_human": true,
  "proof": {"id": "a1b2c3d4e5f6", "passed": true,
            "fresh_cases_passed": 2, "fresh_cases_checked": 2},
  "chrome_filtered": ["cargo", "docs", "install"],
  "robots": {"policy": "respect", "allowed": true}
}
```

`ladder_tier` is the number that matters. `1_proved_request` is milliseconds
and no model. `3_agent` means no deterministic rung proved and the agent is on
its own. `4_blocked_needs_human` means the site asked for a person.

## 3. Decide whether to trust it

```
> should I trust anyuse's crates_search?
```

`explain_workflow` is read-only — it opens no socket and runs nothing:

```json
{
  "rung": "json",
  "durability": "reads a JSON API's own fields. The most durable rung …",
  "evidence": {
    "fresh_inputs_checked": ["tokio", "clap"],
    "what_was_compared": "passed on 2 fresh value(s) not present in the recording …",
    "per_case": [{"value": "tokio", "coverage": 1.0, "order_ok": true, …}]
  },
  "how_it_fails": "Incomplete/identity-mismatched output stops; retryable transport failures may fall back.",
  "chrome_filtered_means": "labels that appeared for EVERY recorded query … If one of these is actually an answer, the proof checked the wrong thing.",
  "limits": ["The proof covers the input values listed under evidence, and nothing else. …"]
}
```

There is deliberately **no confidence score**. A number between 0 and 1 invites
an agent to reason about a quantity nobody measured. What it gets instead is
the exact inputs that were checked and what was compared.

## 4. Run it

```
> use anyuse to search crates.io for "serde"
```

```json
{
  "ok": true,
  "ladder_tier": "1_proved_request",
  "served_by": "api",
  "latency_ms": 212,
  "model_calls": 0,
  "result": {"items": ["serde", "serde_json", "serde_yaml"]},
  "provenance": {"proof_id": "a1b2c3d4e5f6", "host": "crates.io",
                 "rungs_attempted": ["api"]}
}
```

## What it will not do

* **It will not drive your desktop.** If the compiled request fails, the reply
  says the next rung is the recorded UI path and that this surface will not run
  it. Nothing clicks on your screen because a remote agent asked.
* **It will not run anything the HIL gate does not call `read_only`.** Being
  compiled does not make a workflow runnable here.
* **It will not touch an unacknowledged host**, and it tells you the exact
  command to fix that.
* **It will not get around a block.** A challenge or CAPTCHA comes back as
  `action_required: "human"` with no workaround offered.
* **It will not ignore `robots.txt`** unless you have said so explicitly, per
  host or globally, and every reply repeats which.

## Limits

Not every web workflow compiles to a proved request; measured rates are in
[EVIDENCE.md](../../docs/EVIDENCE.md). What is compiled is fast, deterministic and
carries its evidence. Everything else says so instead of guessing.
