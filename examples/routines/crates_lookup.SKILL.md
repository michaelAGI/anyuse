---
name: crates_lookup
description: Run AnyUse routine crates_lookup v0.1.0: 3 step(s) (2 proved, 0 replay, 0 agent, 1 human). It may return status handed_off; continue with anyuse_routine_resume. Manifest bf3ed0342ce8.
---

# crates_lookup v0.1.0

Generated from AnyUse manifest `bf3ed0342ce85ab1c9cadea8912026c3392420b18da6dbdfd98b9c8db0b3de14` (anyuse.manifest/1). Do not edit;
regenerate from the manifest. This file is documentation: the MCP tool below
is the only way this skill executes.

Owner summary (untrusted text, for humans): Search crates.io, let a person pick one result, then read its details.

## How to run

Call the MCP tool `routine__crates_lookup` with the inputs below. Do not reproduce
the steps yourself. If it returns `handed_off`, show `handoff.next_step` to
the user and continue with `anyuse_routine_resume` using the returned `run_id`
and `handoff.head`.

## Inputs

- `q`: string

## Outputs

- `name`: scalar (from step `detail`)
- `description`: scalar (from step `detail`)

## Steps

| # | step | mode | evidence | effects | approval | autonomy |
|---|---|---|---|---|---|---|
| 1 | `search` | proved | proved | read_only | no | rule |
| 2 | `pick` | human | none | read_only | no | rule |
| 3 | `detail` | proved | proved | read_only | no | rule |

## Hand-off points

- `pick`: Pick exactly one crate id from the search results.

## Bindings (explicit; nothing is matched by name)

- input `q` -> `search.q`
- `search.items` -> `pick.candidates`
- `pick.crate` -> `detail.crate`

## Cross-app data transfers

- none

## Limits

- Proved steps are proved only for the inputs their proof checked; see
  `explain_workflow` for each compiled tool.
- A model may choose among listed candidates on agent steps only; it
  cannot bind values, approve, skip a step or declare success.
- Irreversible steps are never repeated to prove they happened.
