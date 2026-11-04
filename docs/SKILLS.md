# Skills: one executable manifest, three projections

The skills surface is wired into the MCP server (`mcpserver/server.py`, see
"Integration" below).

This page is about skill manifests. To write each proved compiled tool as a
`SKILL.md` folder of its own, use `anyuse skills export DIR --profile meta`
([INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md), "Make your tools show up as skills").
`--profile` names the MCP profile the agent's server runs with: under `meta` each
skill pre-approves and calls `call_operation`, under `default` the typed tool.
Without it the export follows `ANYUSE_MCP_PROFILE` where it runs.

## Why one manifest

`SKILL.md` and the MCP tool are generated from ONE executable manifest. If the prose an agent reads and the tool it calls
are authored separately, they drift, and the agent acts on the prose. Here:

| Projection | Module | Carries |
|---|---|---|
| Runtime | `execution/routine.py` | executes the manifest itself |
| MCP tool | `execution/skill_gen.py::mcp_tool` | typed `inputSchema`/`outputSchema`, `_meta.anyuse.manifest_digest` |
| SKILL.md | `execution/skill_gen.py::skill_md` | the same digest, the tool name, steps, bindings, hand-offs, limits |

`manifest_digest` (sha256 of the canonical JSON) is the immutable version
reference. The tool's `outputSchema` pins it as a `const`, every receipt
records it, and a resume against a different digest is refused.

## Schema `anyuse.manifest/1`

Defined and validated in `execution/skill_manifest.py`
(`validate_manifest` returns every problem; empty means executable).

```
{schema, kind: skill|routine, name, version: MAJOR.MINOR.PATCH, summary,
 inputs: {name: type},  outputs: {name: type + from:{step, output}},
 apps: {app: {origin, tenant}},
 data_transfers: [{from_app, to_app, fields[], purpose}],
 steps: [step, ...]}               # declared order = a topological order
```

Each step:

| Field | Meaning |
|---|---|
| `id` | `[a-z][a-z0-9_]*`, unique |
| `mode` | how it executes: `proved` \| `replay` \| `agent` \| `human` |
| `evidence` | what is known: `status` = `proved` \| `candidate` \| `recorded` \| `unresolved` \| `none`, plus `proof_id`, `scope`, `origin` |
| `action` | `compiled_tool {ref, proof_id, template_digest}` \| `skill {ref, digest}` \| `replay {ref}` \| `agent_choice {choose_from, into, rule?}` \| `human {instruction}` |
| `app` | the declared app it acts on (required for machine steps) |
| `inputs` / `outputs` | typed: `string`, `integer`, `number`, `boolean`, `scalar`, `array(items)`, `object(properties)` with bounds |
| `bindings` | exactly one per input: `const {value}` \| `input {name}` \| `derived {step, output}` |
| `preconditions` / `postconditions` | machine predicates: `exists`, `non_empty`, `eq`, `ne`, `in`, `len_min`, `len_max`, `matches`, `type` over `$run.`, `$in.`, `$out.`, `$steps.<id>.` |
| `effects` | `class` (= `hil_gate.StepClass`), `reversible` |
| `policy` | `timeout_s` ≤ 600, `retries` ≤ 5, `idempotent` |
| `approval` | `required` |
| `autonomy` | `level`: `rule` \| `model_proposes_human_confirms` \| `model_decides_within_limits` (+ `limits.cap_usd`, `limits.max_decisions`) |

### Rules the validator enforces

1. **Mode and evidence are separate.** `mode: proved` requires
   `evidence.status: proved`, a `proof_id` equal to `action.proof_id`, and a
   `template_digest` (compiled tool) or a manifest `digest` (skill).
2. **A recording never confers `proved`.** `evidence.origin: recording` with
   status `proved` is invalid. `manifest_from_recording` produces `replay`
   steps with `recorded` evidence (conservatively side-effecting, approval
   required) and explicit `human` steps with `unresolved` evidence.
   `step_from_compiled` yields `proved` only when the compiled record is
   passed, current and `runnable_without_human`; otherwise a human step with
   `candidate` evidence.
3. **Explicit, typed, backward-only bindings.** `derived` names an EARLIER
   step's declared output; types must be compatible; no extra bindings.
4. **Cross-app transfer is an edge.** A value originating in app A may reach a
   step on app B only through a `data_transfers` edge naming that field.
   Human/agent steps do not launder origin.
5. **Effects need approval.** A machine-executed step that is not read-only
   or not reversible must declare `approval.required`.
6. **Retries only when safe.** `retries > 0` needs `idempotent` or read-only.
7. **Models only on agent steps.** Model autonomy levels are rejected on any
   other mode; `model_decides_within_limits` is read-only/reversible only and
   must carry a cap and a decision count.

## Generated MCP tool

* `name`: `skill__<name>` or `routine__<name>` (cut with a hash past 64 characters); the
  older `skill.<name>` / `routine.<name>` still calls it.
* `description`: built from enums, counts and the digest only; author text
  never enters it.
* `inputSchema`: typed, `additionalProperties: false`.
* `outputSchema`: `status` enum (`completed`, `handed_off`, `paused`,
  `failed`, `refused`, `needs_reconcile`), `run_id`, `manifest_digest` const,
  typed `outputs` (no `{}` leaves), `handoff`, `receipts`, `error`.
* One shared `anyuse_routine_resume` tool (run_id, expected_head, human,
  approval, reconcile).

## Generated SKILL.md

Front matter (`name`, `description`) plus: how to call the tool and resume,
typed inputs/outputs, the step table (mode, evidence, effects, approval,
autonomy), hand-off points, bindings, cross-app transfers, limits. Author text
(`summary`, human instructions) is passed through `plain()`: one line, no
control characters, no markdown/HTML structure, bounded length.

Example: `examples/routines/crates_lookup.manifest.json` and
`crates_lookup.SKILL.md`. Both are regenerated by
`test_routine_example.py` from real compiled records and must match byte for
byte.

## Integration

`mcpserver/skill_tools.py` exposes `tool_definitions()` and `handle()`;
`mcpserver/server.py` uses them as follows:

* `list_tools` appends a `Tool` (name, description, `inputSchema`,
  `outputSchema`, `_meta`) for every definition. `call_tool` checks the
  profile's advertised names first, then dispatches the `skill__<name>` /
  `routine__<name>` tools (and their older `skill.<name>` / `routine.<name>`
  names), `anyuse_routine_resume` and `anyuse_inbox_list` through
  `skill_tools.handle` (off the event loop) and returns text plus structured
  content.
* **Default MCP profile** (`ANYUSE_MCP_PROFILE` unset or `default`): a
  manifest is listed only when every step's `mode` is `proved`, `replay` or
  `human` (`skill_tools.launch_eligible`). A manifest with an `agent` step is
  listed only under the explicit `full` profile. The resume tool refuses
  `approval` and `reconcile` in the default profile, because an MCP client
  may itself be a model. A person approves or reconciles out of band with
  `anyuse routine approve|reconcile --confirm <digest from the hand-off>`,
  and resume continues only when that receipt exists and the fresh checks
  still hold (ROUTINES.md, "Not covered").
* Default profile: a compiled step whose record's proof status is not
  `proved` is refused at the fresh check.
* **Both profiles:** no model advisor is configured, so an `agent` step never
  auto-runs; it hands off to a human. No replay runner is configured, so a
  `replay` step is refused (`no_runner`) rather than executed.

Manifests live in `<ANYUSE_DATA_DIR>/skills/*.manifest.json`; receipts in
`<ANYUSE_DATA_DIR>/skills/runs/`. Invalid manifests are skipped with a log
line. This surface wires only the compiled-tool runner (read-only
`run_compiled`) and skill composition, and no model advisor, so `agent` steps
always hand off to a human over MCP.

Tests: `test_skill_manifest.py`, `test_routine_runtime.py`,
`test_routine_example.py` (all guarded and allowlisted).
