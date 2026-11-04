# Routines: composition v0

A routine is a manifest ([SKILLS.md](SKILLS.md)) whose steps compose proved tools,
pinned skills, human hand-offs and bounded agent choices. It is a versioned,
typed state machine, not a free-form plan. v0 ships the minimum: a few
read-only atomic tools, explicit bindings, one human hand-off, and durable
resume.

## Execution model (`execution/routine.py`)

* Steps run in declared order. The validator guarantees that order is a
  topological order of the binding DAG (`derived` and `after` point
  backwards only). No parallel branches in v0.
* **State comes only from receipts.** Every transition is an append-only
  receipt: `step` (completed | failed | uncertain | refused), `handoff`,
  `approval`, `decision` (model), `reconcile`. Receipts are hash-chained from
  a hashed `run.json` and written with temp file + fsync + hard link (the link
  refuses to overwrite, so two writers cannot both append receipt N).
  `load` re-verifies the chain; a modified receipt or `run.json` stops the run.
* Before a machine step runs: bind inputs (typed, provenance-checked),
  evaluate preconditions, require an approval bound to the exact
  `inputs_digest` when `approval.required`, then call the runner's
  `fresh_check` immediately before dispatch. For compiled tools that check
  requires the live record's proof id and template digest to equal the
  manifest's pins, the proof to be current, and the tool runnable without a
  human. A re-compiled tool therefore stops a routine; it never silently
  changes it. The runtime cascade's automatic recompile does not swap a tool
  whose current proof a registered manifest pins
  ([COMPILE.md](COMPILE.md#automatic-recompilation)): the routine keeps
  running through the browser replay until you recompile and re-pin.
* After it runs: the reported effect class must not exceed the declared one;
  outputs must match the declared types (undeclared fields are dropped, never
  forwarded); postconditions must hold.

### API

| Call | Behaviour |
|---|---|
| `run(inputs)` | validate inputs, create a run, drive until completion or a stop |
| `run_until(inputs, step)` | same, stop with `paused` after `step` |
| `hand_off(run_id)` | read-only: state, next step, its bound inputs, `inputs_digest`, expected outputs schema, preconditions and whether they hold now |
| `resume(run_id, expected_head=, human=, approval=, reconcile=)` | rebuild from receipts, refuse if `expected_head` is stale or the manifest digest differs, apply the human input / approval / reconcile, re-check fresh state, continue |

Statuses: `completed`, `handed_off` (reasons: `human_step`, `approval`,
`confirm_model_proposal`, `blocked`, `human_only_host`), `paused`,
`needs_reconcile`, `failed`, `refused`, `cancelled`.

Every stop that needs a person (including `needs_reconcile`) gets a card
(`docs/HUMAN_INBOX.md`). AnyUse asks through MCP elicitation when the client
supports it. Otherwise it falls back to `anyuse inbox do|skip|cancel <id>`.
Under the default profile, `resume` refuses a `human` answer from a tool call.
A block page (CAPTCHA, challenge, 403, login wall) reported by a runner is a
`blocked` receipt and a card, not a failure. What happens after the person
deals with it is set by `ANYUSE_CHALLENGE_POLICY` (default `human_does_step`:
the person does the step, AnyUse does not reuse their session).

### Uncertain effects

A timeout or runner exception after dispatch leaves the effect unknown. For a
read-only, idempotent step the runtime retries. For anything else it writes an
`uncertain` receipt and returns `needs_reconcile`; every later resume returns
the same until a human reconciles with `completed` (plus the observed outputs)
or `not_done` (which alone permits a new attempt). An irreversible action is
never repeated to find out whether it happened. Runners receive a stable
`idempotency_key` (run, step, inputs digest) for conditional writes where the
target supports them. A precondition check cannot close a check-to-write race;
conditional writes can.

### Autonomy per step

Models act only on `agent` steps (`action.kind = agent_choice`), and only by
choosing one element of an explicit candidate list (`choose_from`) or
abstaining:

| Level | Behaviour |
|---|---|
| `rule` | no model. `rule: "only"` takes a single candidate; otherwise hand off |
| `model_proposes_human_confirms` | proposal recorded as a `decision` receipt, then `confirm_model_proposal` hand-off; a human accepts it or picks another candidate |
| `model_decides_within_limits` | read-only and reversible steps only; the advisor's `max_cost_usd` is reserved against `limits.cap_usd` and `limits.max_decisions` BEFORE each call; every call is audited with its cost; limit reached, abstention or out-of-list answer hands off |

A model never creates bindings, approves, reconciles, skips a step, or declares
success. Success is always typed outputs plus postconditions. A model-selected
branch stays a model decision in the receipts (`actor: model`,
`decision: <hash>`) even when the tool it feeds is proved.

## Composition v0 example

`examples/routines/crates_lookup.manifest.json`:

1. `search` — proved compiled tool `crates_search(q) -> items[]`
2. `pick` — human hand-off: choose one of `search.items`
   (postcondition: the choice is in the list)
3. `detail` — proved compiled tool `crates_detail(crate) -> {name, description}`
   (precondition: non-empty; postcondition: `name == crate`)

`test_routine_example.py` builds it from real compiled records, runs it
through the real `compiled_tools.run_compiled` with a fake transport, restarts
between hand-off and resume (new engine, store and runner objects), checks the
exact URLs sent, and shows that re-compiling `crates_detail` while the run
waits stops it at the fresh check. It also drives the same flow through
`mcpserver/skill_tools.handle`.

## Threat model

| Threat | Control |
|---|---|
| **Confused deputy across apps.** A routine with read access to app A and write access to app B moves A's data into B although nobody authorised that transfer. | Every machine step names its `app` (origin + tenant). A derived value carries the origin of its producer; human and agent steps do not launder origin. A value from A reaches a step on B only through a declared `data_transfers` edge naming the field and a purpose. Checked at validation and again at bind time. Edges are part of the digest, so adding one is a new version. |
| Prompt/label injection through manifests | MCP descriptions use only enums, counts and the digest. Author text appears only in SKILL.md, flattened by `plain()`. The manifest is data. Nothing in it is executed as code. |
| Forged or drifted proof | Proof id + template digest pinned per step and re-checked immediately before dispatch. The served proof id is compared again on the result. |
| Swapped manifest mid-run | Digest in `run.json` and every receipt. Resume under a different digest is refused. |
| Tampered state / replayed resume | Hash-chained receipts, hashed `run.json`, `expected_head` optimistic check, `run_id` format check (no path traversal). |
| Forged human input | Human outputs are typed and must satisfy postconditions (e.g. `in` the candidate list). Undeclared fields are dropped. |
| Model overreach | See autonomy. Membership check on every choice. Cost reserved before each call. |
| Approval reuse | An approval binds to step id + exact `inputs_digest` inside one run of one manifest digest. Different inputs need a new approval. |
| Duplicate irreversible effects | `uncertain` receipts plus a mandatory human reconcile. Retries only for idempotent/read-only steps. Idempotency key passed to runners. |
| Desktop/actuation from MCP | `skill_tools` wires only the compiled-tool runner, whose `run_compiled` never drives the desktop, plus skill composition. There is no replay or agent actuator on this surface. |

Not covered:

* Who is "human" is not authenticated. Under the full profile, `resume`
  trusts its caller the way the rest of the local MCP server does. Under the
  default profile, a human step is answered only through elicitation or the
  CLI ([HUMAN_INBOX.md](HUMAN_INBOX.md)).
* **Approvals and reconciles have an out-of-band channel.**
  A person runs `anyuse routine show <run_id>`, then
  `anyuse routine approve <run_id> <step_id> --confirm <approval_digest>` or
  `anyuse routine reconcile <run_id> <step_id> --outcome completed|not_done
  [--outputs JSON] --confirm <reconcile_digest>`. The digest is shown in the
  hand-off. It binds the run, the step, the exact bound-inputs digest, the
  declared effects, the action pins (proof id, template digest, sub-skill
  digest) and the manifest version. A wrong digest, a step that is not
  pending, or a changed manifest version is refused.
* In the default MCP profile, the engine counts only CLI-channel receipts
  with a matching digest, and `resume` refuses in-band `approval` and
  `reconcile`. A resume still re-runs the precondition check and the
  runner's fresh check before dispatch. A resume may quote the head of the
  hand-off it saw, even if a human approval or reconcile receipt was
  appended after it.
* **The terminal gate** (`execution/human_gate.py`). `anyuse routine
  approve|reconcile` also needs stdin and stdout to be TTYs, shows the effect
  summary on `/dev/tty` and has the person type back a six-character code. Under
  a known agent environment marker it refuses unless the machine is opted in
  (`anyuse routine allow-agent-terminals`, `--revoke` to undo). It raises the bar
  and is not authentication
  ([THREAT_MODEL.md](THREAT_MODEL.md#routine-approvals-the-out-of-band-human-channel)).
* Tenant qualification is declared (`apps.<app>.tenant`) and enforced only as
  part of the app identity. Credentials are not handled here at all. The
  compiled path excludes them.
* A timed-out runner thread cannot be killed. Its late result is ignored,
  and a non-read-only step is marked uncertain.
* Receipts are integrity-protected against accidental or naive edits, not
  against an attacker who can rewrite the whole directory (no signing key).

## Limits of v0

* Sequential only. Nested skills must be all-`proved` (no nested hand-off).
* Only `compiled_tool` has a production runner. `replay` and `agent` steps
  need injected runners/advisors. Over MCP, agent steps always hand off.
* No held-out end-to-end validation on live sites yet; the example runs on a
  fake transport.
* Approval-requiring writes are supported by the runtime but no write runner
  ships. The approval is written by a person through `anyuse routine
  approve`.
* The model advisor interface (`propose(step_id, candidates)`,
  `max_cost_usd`) has only fakes. No provider is wired.
