# Desktop workflows: record, compile, replay

Desktop replay sends real clicks and keys. Run it in a macOS VM, not on the Mac you
work on ([VM_RUNBOOK.md](VM_RUNBOOK.md)).

## Record → compile → replay

1. **Install and grant.** `pip install -e '.[desktop]'`, then allow the terminal you
   run AnyUse in under System Settings > Privacy & Security > Accessibility (and Screen
   & System Audio Recording for computer use). `anyuse doctor` shows both, without
   asking for them.
2. **Record.** `anyuse watcher start --steps`, do the task once in the app, press
   Ctrl+C. It records each step's action and the control's Accessibility identity;
   typed values go to the local vault. No model runs and no screen frames are taken.
   Password fields, payment fields and denylisted apps are not recorded.
3. **Compile.** `anyuse workflow drafts` lists the recording as
   `SESSION/steps_<app>_N`. `anyuse workflow from-draft SESSION/steps_textedit_1 --name
   new_note` lists the values you can turn into parameters (`c1`, `c2`, …), one per
   recorded step. Choose the one that holds what you typed, not a menu title: a
   shortcut such as Cmd+N is recorded as its menu path, so in "new TextEdit document,
   type a text" `c1` and `c2` are File and New and `c3` is the typed text. Run it again
   with `--param text=@c3`. It is saved as `ready` only after it replayed correctly
   with values that were not in the recording.
4. **Replay.** `ANYUSE_DRIVE_ENABLED=on anyuse workflow replay new_note --param
   text="Hello"`. Without `ANYUSE_DRIVE_ENABLED=on` nothing is sent to the desktop.
   Before each step the replay checks that the screen matches the recording; a
   window that is still opening is waited for (up to about 2 s), and a screen that
   stays different stops the replay with the reason instead of clicking.

# Compiled desktop tools over MCP, run in the guest

A desktop task that replayed correctly in a macOS guest VM on new values becomes a
typed MCP tool. Calls run in the guest, not on the machine that hosts the MCP
server. Code: `execution/desktop_tools.py` (records, proof, the guest route) and
`mcpserver/desktop_compiled_tools.py` (listing and dispatch). Tests:
`tests/test_desktop_tools_mcp.py`.

## What becomes a tool

A compiled routine or a `ready` draft workflow that replayed correctly **in the
guest** on new values:

- at least two replays (`MIN_GUEST_PROOFS`), each judged correct on its own expected
  end state, with 0 model calls, each changing at least one value; every parameter took
  a new value in at least one correct replay; no positive replay failed; a replay
  designed to be refused did not succeed;
- a routine that stops at a refusal, a human gate or a hand-off is not a tool.

The record is written as `compiled_desktop.json` next to its storage entry:

| key | routine (`compile_class: routine`) | draft workflow (`compile_class: draft_workflow`) |
|---|---|---|
| storage entry | `workflows/workflow_routine-<sha12>/` with `routine.json` | the saved workflow's own directory (its artifact digest must match the proved record) |
| `workflow` | the tool's name (listed as `desktop__<name>`) | the workflow's name |
| `parameters` | every routine parameter, optional (left out = the recorded value); a choice lists its identity keys and recorded labels (`enum`) | every parameter, required; a choice bound by identity lists its recorded titles and `id:<identifier>` |
| `proof` | `harness: guest`, `fresh_inputs`, the rows (values, verdict, model calls), `artifact_digest` | the same |
| `app` | `{name, bundle_id}` of the recorded app | the draft's `app` and `bundle_id` |
| `approval_level` | `auto` | the workflow's level (`anyuse workflow approvals`) |
| `payload` | `{kind: routine, routine}` | `{kind: draft_workflow, record}` |

The artifact digest covers the parameters, the app, the approval level and the
payload, so a changed routine, a proof not made in the guest, or a single proof
replay is not listed.

## The MCP tool

Listed under the default profile as `desktop__<name>` (the older `desktop.<name>` still
calls it), and under `meta` as an operation
of the site named by the app. Both go through `desktop_compiled_tools.run_typed_tool`.

- **A call with values runs with no approval prompt**: starting the call is the
  approval. This holds for every compile class (`read_value`, `file_write`,
  `directory_op`, `app_script`, routines, draft workflows). A record whose approval
  level opts into holds (`all`, `destructive`) is not run over MCP (`blocked`, meta
  class `needs_approval`).
- **A deviation stops it with its reason:** the schema's `enum`, the routine's value
  check (an external or irreversible option, an unknown name), the target's identity
  and the permitted-diff checks, the draft runner's value checks and drift gate (no
  vision fallback), the guest's own guards, and the foreground interlock
  (`refused_by: foreground_interlock`).
- The guest's irreversible-action guard still holds every irreversible press (status
  `gated`): a routine that would need one stops there.
- The listing says `ready: true` only when the server has a guest runner and no
  approval level holds steps; otherwise `why_not` says which.

## Where calls run

Every call of a compiled desktop tool goes to the **guest runner**:

- older classes and draft workflows: the job agent in the guest re-checks the record's
  proof pin, binds an Accessibility tree to the recorded app's single running process
  (nothing is launched), and runs the checked steps there with the drift gate on, every
  step behind the foreground interlock;
- a routine: replayed with no planner and no picker (0 model calls), every action sent
  to the guest job agent, which asks the interlock before each input;
- **no guest runner attached: the call is refused with a typed reason** (the foreground
  interlock's verdict for this machine), and nothing is sent.

The guest runner is set with `create_server(desktop_guest_runner=…)` or
`ANYUSE_DESKTOP_GUEST_SESSION=<session>` (with `ANYUSE_DESKTOP_GUEST_SSH` naming the
SSH wrapper that reaches the guest). Commands go over a file channel, one command file
per operation, written atomically and once. Setting up the guest is described
in [VM_RUNBOOK.md](VM_RUNBOOK.md).

## Limits

- A draft workflow over MCP needs its app already running in the guest, with exactly
  one process; nothing is launched. A routine launches its apps itself.
- A routine replays its recorded path: a new value that needs other steps fails.
- A draft step that types a vault credential has no credential resolver over MCP and
  fails.
