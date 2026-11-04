# The `meta` MCP profile: four fixed tools

`ANYUSE_MCP_PROFILE=meta` serves four tools whose names and schemas are
constants: `list_sites`, `list_operations`, `call_operation` and `login`
(the names follow [api-anything](https://github.com/goodnight000/api-anything)'s MCP server).
Compiled workflows appear as rows in tool *results*, fetched when the agent
asks, instead of as one tool each in `tools/list`. Code:
`mcpserver/meta_tools.py`; tests: `test_mcp_meta.py`.

## Measurement

Under `default`, every compiled web workflow that may run (proved, current,
host acknowledged, read-only, not demoted, or demoted with its check due) adds one `<host>__<workflow>` tool,
every proved compiled desktop record adds one `desktop__<workflow>` tool, and
every launch-eligible skill manifest adds one tool (plus the resume and inbox
tools once there is at least one). An MCP client sends the whole list to its
model on every turn, so the cost grows with what you have compiled.

Measured offline with the MCP Python SDK 1.30.0 (`MeasurementTests` in
`test_mcp_meta.py`): fake storage, `N` compiled workflows, no skills. Web: each
workflow is a proved one-parameter `crates.io` detail record on one
acknowledged host. Desktop: each is a proved `read_value` record for the app
`Settings`. Bytes are the `tools/list` result as that SDK serialises it
(`model_dump_json(by_alias=True, exclude_none=True)`). The last column is the
compact JSON returned by the meta `list_operations` for that one site, which
an agent pays only when it calls it. With SDK 1.30.0 the test pins these exact
bytes and checks the rows are in this file; with any SDK it asserts the
invariant: `meta` has the same four tools and the same bytes at 0, 5 and 50,
fewer bytes than `default`, and `default` has 9 + N tools with growing bytes.

| scenario | compiled workflows | default tools | default bytes | meta tools | meta bytes | meta list_operations bytes |
|---|---:|---:|---:|---:|---:|---:|
| web | 0 | 9 | 6721 | 4 | 1767 | - |
| web | 5 | 14 | 11441 | 4 | 1767 | 2545 |
| web | 50 | 59 | 53921 | 4 | 1767 | 24956 |
| desktop | 0 | 9 | 6721 | 4 | 1767 | - |
| desktop | 5 | 14 | 9631 | 4 | 1767 | 1929 |
| desktop | 50 | 59 | 35821 | 4 | 1767 | 18805 |

* `default` grows by about 944 bytes per web workflow and 582 bytes per
  desktop record. Its nine static tools (including `agent_run`) are 6721 bytes on their own.
* `meta` is 1767 bytes at 0, 5 and 50 workflows. Its listing reads no record
  (`test_meta_listing_reads_no_record`), so it cannot grow.
* The detail moves into `list_operations`: about 500 bytes per web operation
  and 375 per desktop operation (with its `why_not` while no guest runner is
  attached, the default: a compiled desktop operation runs only in the guest VM,
  docs/DESKTOP_TOOLS.md), for one site at a time, on demand.

## The four tools

| tool | arguments | returns |
|---|---|---|
| `list_sites` | none | `{ok: true, sites: [{site, kinds, operations, ready}], count}`. A site is a web host (the first URL's host of a compiled record) or a desktop app (`workflow_template.required_app` of a compiled desktop record, `desktop` when empty). `ready` counts operations that run without a person now. |
| `list_operations` | `site` | `{ok: true, site, operations: [...], count}`, or the `unknown_site` failure. |
| `call_operation` | `site`, `operation`, `args` | success or failure, below. |
| `login` | `site` | always `{ok: false, class: "needs_user", reason, next, known_site}`. |

An operation row from `list_operations`:

| field | web operation | desktop operation |
|---|---|---|
| `operation`, `kind` | workflow name, `web` | workflow name, `desktop` |
| `parameters` | each input: `name`, `required`, `type`, `min_length`/`max_length`, `enum`, `minimum`/`maximum` when the record has them; `example` from the first passing proof case (the held-out values `explain_workflow` already lists); `hint` for a multi-segment path parameter or a recorded transform (`lower`, `slug`, ...) | each declared parameter: `name`, `required`, `type`, plus `description`, `example`, `pattern`, `hint` when the record has them |
| `returns` | `{shape: list or fields, keys: extraction names}` | not present: the record names no result keys |
| `proof` | `status`, `id`, `current`, `scope` (rebuilt from counts and enums by `proof_split.scope_text`) | `status: proved`, `fresh_inputs`, `digest` (first 12 hex of the artifact digest) |
| `risk_class` | the HIL classification of the unbound request (`read_only`, ...) | the compile class (`read_value`, `file_write`, ...) |
| `approval_needs` | any of `host_acknowledgement`, `not_over_mcp` (not read-only), `readonly_post_approval` | `per_call_human` for `file_write`, `directory_op`, `app_script` |
| `ready`, `why_not` | whether `run_compiled` would run it now; the first refusal in `run_compiled`'s order | `false` for the file classes, and for every desktop operation while `ANYUSE_DRIVE_ENABLED` is off (the executor refuses actuating steps) |
| `tier`, `hosts` | cascade tier; every host when a chain touches more than one | |

Rows are built from record fields, counts and enums. `template.notes` and
other page text are not copied into them.

## call_operation

1. Look up `site`, then `operation` on that site (`unknown_site`,
   `unknown_operation`). A site id without its port (`127.0.0.1` for
   `127.0.0.1:8443`) resolves when exactly one listed site has that host. Two operations of one name on one site, or two web
   records of one name anywhere (`run_compiled` resolves by name alone), are
   refused as `input`.
2. Validate `args` against the input schema the matching typed tool
   advertises under `default` (`json_schema_for_inputs` for web, the record's
   parameter schema for desktop): missing, unknown, wrongly typed or too long
   values are `input`. The SDK applies the same schema to the typed tools.
3. Run through the server's own run path. The server has one closure per
   kind: `run_compiled_workflow` and `call_operation` share the web one (the
   typed web tools call the same `run_compiled` through `run_typed_tool`,
   then the same block handling), and the `desktop__<workflow>` tools and
   `call_operation` share the desktop one:
   * web: `compiled_tools.run_compiled(workflow, args, storage, executor,
     hosts_path, require_proved=True)` (proof passed, status `proved`, proof
     pinned to the current template, compiled request not demoted or its check due, every host
     acknowledged, the complete bound request classified `read_only` by the
     HIL gate, read-only POST approval), then the block handling: inbox card,
     elicitation, notification;
   * desktop: `desktop_compiled_tools.run_typed_tool(tool_name, args, ...)`
     (proof digest, the record's approval level -- a call started with its
     values asks nothing; a level that holds steps is not run over MCP --,
     every step through `execute_checked_step` with the drift gate, and the
     executor's `ANYUSE_DRIVE_ENABLED` gate).

`SameGatesTests` wraps the real gate functions (host acknowledgement, HIL
classification, proof pin, read-only POST approval) and the two run entry
points with recorders, runs the existing tool and `call_operation` against
the same fixture, and asserts the recorded calls are equal: same gates, same
arguments, same order, for a successful web run, a refused web run, a desktop
read and an approved desktop write.

**Success**: `{ok: true, site, operation, kind, result, ...}`. Web adds
`served_by`, `tier`, `latency_ms`, `model_calls`, `proof {id, status}`,
`provenance`, and `result_contract`/`empty_state` when present (`result_contract.empty_by` says
which step accepted an empty answer: `site_empty_page`, `model_check` or `page_statement`). Desktop adds
`served_by` and `proof`; its `result` is the executed steps. A block the
person answered through elicitation is `{ok: true, served_by: "human",
status: "done_by_human", result}`.

**Records**: a list of two or more objects, as the result or as one value of
the result object, becomes `{columns, rows}`. Columns are the union of keys in
first-seen order; a missing field is `null`. One record, a list of scalars and
a mixed list are returned unchanged.

**Failure**: `{ok: false, site, operation, class, reason, next}`, sometimes
with `detail` or `needs_human`. `next` is either an MCP tool call
(`list_operations {"site": "crates.io"}`) or a step starting with
`hand to the user (do not run it yourself):`. Such a step is the person's to
take (confirming a host, proving a workflow, answering an inbox card,
enabling a setting); the agent passes it on. Every workflow name, host and
card id inside a command is shell-quoted (`shlex.quote`), so
`anyuse workflow play 'evil; rm -rf ~'` stays one argument. The
`call_operation` description tells the agent to follow `next` at most once,
then stop and report.

| class | when | next |
|---|---|---|
| `unknown_site` | no operation on that site | `list_sites {}` |
| `unknown_operation` | no such operation on the site, or no record by that name | `list_operations {"site": ...}` |
| `input` | missing/unknown/invalid argument, parameter binding failed, duplicate name; a chain's search listed no result with the asked-for name (`detail.not_found`, nothing was opened) | `list_operations {"site": ...}`, or rename (user); for `not_found`, call again with the exact name the site uses |
| `not_proved` | proof did not pass, or its status is not `proved` | user: compile and prove again |
| `stale_proof` | the template changed after its proof | user: compile and prove again |
| `demoted` | the compiled request was taken out of service (demoted) and its check is not due, and no lower step answered | `out_of_service.cause` `outage` (the site did not answer) or `rate_limit` (the site asked AnyUse to wait): call again after the time `next` names, nothing needs recording. `answers` or `gone` (it kept failing while the site's own pages answered): the call first tries to heal it from its result page (`heal` says why that did not work), and AnyUse sends the request again after the time `reason` names; user: `anyuse workflow play <workflow>` runs the recorded path locally and recompiling restores it, or, for a web tool with no recorded path, record the search again and recompile |
| `host_not_acknowledged` | a host is not acknowledged | user: `anyuse workflow acknowledge-host <host>` |
| `needs_approval` | the HIL gate classifies the bound request other than `read_only`; read-only POST not enabled/approved; a desktop operation whose approval level holds steps (`all` / `destructive`; a started call otherwise asks nothing); the person declined | user step, or stop when declined |
| `drive_disabled` | the desktop executor refused: `ANYUSE_DRIVE_ENABLED` is off | user: `anyuse settings set ANYUSE_DRIVE_ENABLED on` (typed confirmation) |
| `blocked` | the site asked for a human (challenge, CAPTCHA, 403) | user: `anyuse inbox do <card>`, then call again |
| `auth` | the site answered with a login wall | user: sign in to the site in their own browser, then retry |
| `rate` | HTTP 429; the host is held for `retry_after_s` or the default TTL | do not retry now; report |
| `failed` | the run happened and failed (a non-2xx other than a server error, a shape change, a redirect the pre-send policy refused, a desktop step, or a server error that took the request out of service as `gone` because the site's own pages answered); a desktop operation refused by the foreground interlock (no guest runner on this host, or the guest refused: `refusal` says why; docs/DESKTOP_TOOLS.md) | user: `anyuse workflow play <workflow>`; a web tool with no recorded path: record the search again and recompile; for a refusal, run it in the guest VM |
| `unavailable` | the site did not answer: a refused connection, a timeout, a name that does not resolve, or a server error (5xx). Nothing says the request changed, and it is not counted against the request; from the second such call in a row, or with the site's `Retry-After`, the request waits before it is sent again | call again later (`next` names the time when there is a wait); if it still does not answer, report |
| `refused` | the pre-send policy refused the destination before the first request (a nonpublic address, or a name that resolves to one), so nothing was sent; `reason` names the setting or the address | stop and report the reason |
| `needs_user` | `login` | user: sign in to the site in their own browser, then retry |
| `error` | an exception, an unrecognised refusal, or `jsonschema` missing (arguments cannot be validated, so nothing runs) | stop and report, or install the `mcp` extra |

## login

`login(site)` reads the operation list to report `known_site` and does
nothing else: no import, no browser profile, no cookie store, no file write
(`test_login_changes_nothing_and_hands_the_cli_step_to_the_user` compares the
data tree before and after). The listings take the workflow directory by path:
on a fresh data directory `list_sites`, `list_operations` and `login` create
nothing and parse no saved workflow
(`test_listings_and_login_without_injected_storage_create_nothing`).

`login` and the `auth` class ask the user to sign in to the site in their own
browser, then retry. Compiled web requests send no cookies, so a sign-in does not
change what `call_operation` can reach; replaying a logged-in site is a separate,
opt-in per-site mode ([SITE_MODES.md](SITE_MODES.md)).

## Same gates as `default`

Everything `meta` exposes is a projection of `default`:

* web operations are the records `list_compiled_workflows` lists under
  `default`, and `call_operation` reaches them through the same call
  `run_compiled_workflow` makes, with `require_proved=True`;
* desktop operations are exactly the `desktop__<workflow>` tools `default`
  advertises (`executable_typed_tools`), run through the same
  `run_typed_tool` call;
* `call_operation` adds checks in front of those paths (lookup, schema
  validation, duplicate names) and removes none; `login` acts on nothing.

So `ANYUSE_MCP_PROFILE` accepts `meta` without the typed terminal
confirmation; only `full` still asks for it (`settings.py`,
`test_setting_meta_from_the_cli_asks_no_terminal_code`). Under `meta` the
dispatcher allows exactly the four names: every `default` and `full` tool,
typed tool and skill tool is refused when called by name
(`test_meta_refuses_every_other_tool_by_name`).

## What `meta` leaves out

* `get_guide` and `desktop_status`. No gate reads either. `desktop_status`
  reports the busy state of `desktop_execute`, which neither `default` nor
  `meta` serves, so under `meta` it would always answer `ready`. `get_guide`
  returns UI landmarks and navigation tips learned from activity on the
  screen, which serve an agent that drives the UI itself; `meta` operations
  are compiled, and their checks run in the server. Leaving them out keeps
  every `meta` result to record fields, counts, enums and extracted values.
* Skills and routines (and with them the resume and inbox tools), the
  compiled list/run/explain tools, `propose_learn_candidate`, and the saved
  workflow list/details. Use `default` for those.

## Switching it on

```sh
anyuse settings set ANYUSE_MCP_PROFILE meta      # persistent, no confirmation
ANYUSE_MCP_PROFILE=meta python -m anyuse.mcpserver   # one process
```

An agent registering the server sets it in the server's environment;
[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md) has the exact commands for Claude Code and Codex.

## The agent harness over the meta profile

The four tools are unchanged (the same 1767 bytes). The agent harness is not
a compiled site, so `list_sites` does not list it; `list_operations {"site": "agent"}` describes its one
operation `run` (the same schema as the default profile's `agent_run`), and `call_operation {"site": "agent",
"operation": "run", "args": {"task": ..., "site" | "app": ..., "values": {...}}}` runs it: the answer carries the
outcome (the stop), the reason, the model calls, the cost and the compiled tool's name when one was saved.

Commit steps (a login, a cart, a form sent, a payment, a deletion): by default the run stops at the
first one with `{"ok": false, "outcome": "human_required"}` and a reason that starts with `hand to the user
(do not run it yourself)`, and the user does that step. With `ANYUSE_MCP_AGENT_COMMITS=agent` (the user's setting): a commit step the task covers runs
(starting the task is the approval); a deviation stops; a step the task did not ask for returns `{"ok": false, "outcome": "confirm_required",
"confirm_required": {question, action, target, values, items, amount, token, expires_in_s}, "resume": {token, how}}`.
The caller answers with `call_operation {"site": "agent", "operation": "run", "args": {"resume": <token>, "confirm":
true|false}}` (a one-time code: `{"resume": <token>, "otp": "123456"}`); `task` is no longer required by the schema
(a call carries `task` or `resume`). No answer within 300 s stops the run (`confirm_unanswered`); a token answers one
question once.

## Saved web tools

Saved browser-replayed web tools ([MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md)) are operations of their host, `kind: "web_tool"`, next to the
API recipes: one parameter `value` (with an example from the tool's proof), `returns`, `proof` (fresh values, the
independent judge, the digest), `risk_class: read_only`, `ready` (a container browser endpoint is attached:
`ANYUSE_LEARN_ENDPOINT`), `why_not`, `browser_only` and `fallback` (`ANYUSE_WEB_FALLBACK` on or off).
`call_operation` runs them through the same route as the default profile's typed tool (same arguments). A success adds
`status`, `source` (`replay`, `ladder` or `fallback`), `verification` (`verified` or `plain`), `served_by`
(`browser_replay` or `fallback`), `model_calls`, `cost_usd`, `requests` and `evidence`.

The failure classes this adds:
- `needs_review` with `review_view`: both sources' evidence; the caller decides;
- `withheld`: the answer could not be verified and is not returned;
- `no_browser`: no throwaway container browser is attached, so nothing was sent;
- `refused`: a government host (with `ANYUSE_REFUSE_GOV_HOSTS` on), a hard stop after a traffic violation, a site that blocked, a paused contact (a
  compiled request the pre-send policy stopped before it was sent is `refused` too; see the table above).

A block maps to `blocked`, a rate limit or a used daily share to `rate`, and anything else to `failed`.

The four tool definitions are unchanged (1767 bytes).
