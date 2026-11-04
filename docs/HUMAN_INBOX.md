# Human inbox

A routine run sometimes needs a person:

* to approve a step;
* to enter a value only they know;
* to sign in;
* to deal with a site's human check;
* to do a step themselves;
* to say whether an uncertain write happened.

Every such stop gets a **card** for audit. AnyUse then asks the person
directly when it can. Each run resumes on its own, from its receipts, after
its fresh checks.

Code: `execution/human_inbox.py`. Engine hooks: `execution/routine.py`.
Surfaces: `mcpserver/skill_tools.py`, `mcpserver/server.py`, and `cli.py`
(`anyuse inbox`). Tests: `test_human_inbox.py` (offline). They use fake
elicitation clients and a real in-memory MCP client, with and without the
elicitation capability.

## Asking the human

### 1. Elicitation first

If the connected MCP client advertises the **elicitation** capability, AnyUse
sends one `elicitation/create` form while the tool call is still open. The form
has a clear message (what is needed, on which host, what continues after) and
a typed schema. The client shows it the way Claude Code shows its own
permission prompts:

| Card | Form |
|---|---|
| `approval` | `approve: boolean`. The message names the step, its effects and the approval digest. The answer is recorded against that digest; if the run changed, it is refused. |
| `value` | One string per value, marked sensitive (`writeOnly`, `x-anyuse-sensitive`). |
| `do_step`, and `challenge` / `login_or_2fa` when the person does the step | `done: boolean` plus the step's declared result fields |
| `consent` | `choice` is `reject` or `accept`; the per-host answer is remembered, but only rejection can become an automatic browser step |
| `challenge` / `login_or_2fa` that continue in the session | `done: boolean` |
| `reconcile` | `outcome: completed \| not_done`, plus the observed outputs if it happened |
| direct compiled call (no run) | `done: boolean`, plus optional `result` text handed back to the agent |

On `accept`, the answer goes through the same engine checks as any human input:

* the step is still pending;
* the bound-inputs digest is unchanged;
* the manifest version is unchanged;
* the preconditions hold now;
* the approval/reconcile digest matches.

The receipt carries `channel: mcp-elicitation`, and the run resumes. If the
run needs a person again, AnyUse asks again in the same call (at most 8 times).
**No code is typed**: the client's own prompt UI is the trusted surface. The
agent-environment refusal does not apply here, because the agent is the one
calling and the form goes to the person.

### 2. The inbox as the fallback

AnyUse falls back to the inbox in four cases: no client is attached
(scheduled or CLI runs); the client lacks elicitation; the person declines or
cancels; or the answer is `approve: false`, `done: false` or invalid. In each
case the card stays **pending** and the tool result carries `needs_human`
(card id, kind, host, what is needed, `asked`).

```sh
anyuse inbox                 # pending cards: kind, host, needed, continues after
anyuse inbox --all --json    # including done / superseded / closed
anyuse inbox do <id>         # do it; the run then resumes
anyuse inbox skip <id>       # put aside; the run keeps waiting
anyuse inbox cancel <id>     # cancel that run for good
```

When no client with elicitation is attached, AnyUse can also send you a
short, value-free notice (card id, what is needed, how to answer) through
`ANYUSE_NOTIFY_CHANNEL` (`imessage` outbound only, or `webhook`; default
`none`). See docs/NOTIFY.md. A notice never answers a card.

`do` and `cancel` keep the terminal gate of `anyuse routine approve`:

* stdin and stdout must be TTYs;
* with agent markers present, the command refuses unless the machine is opted in;
* the card is shown on `/dev/tty`, and results are read from there (a value
  with echo off);
* the person types back a code bound to the card, run, step, inputs and answer.

### What cannot answer a card

* **A tool call.** The only inbox tool is `anyuse_inbox_list`, which is
  read-only.
* **The resume tool under the default profile.** It refuses a `human` answer,
  and it refuses in-band approval/reconcile.
* **An in-band resume on a blocked step.** It returns the same card and never
  re-dispatches the step.

## Challenges and sign-ins

AnyUse does not solve CAPTCHAs or challenges
([THREAT_MODEL.md](THREAT_MODEL.md#asking-the-human-and-anti-bot-challenges)). A
block page detected by a runner is recorded as a `blocked` receipt plus a card. A
direct `run_compiled_workflow` call returns `status: needs_human` plus a standalone
card. What happens after the person has dealt with the block:

| Case | After the person is done |
|---|---|
| third-party challenge, `ANYUSE_CHALLENGE_POLICY=human_does_step` (**default**; also any unrecognised value) | The person does the blocked step in their own browser and hands back its **result**; their session and cookies are not reused. The host becomes **human-only** for the rest of the run: later steps on it become `do_step` cards. |
| third-party challenge, `continue_in_own_session` | The run continues in the same attached browser session (`ctx["session"] = "attached"`), after the precondition and fresh checks. This happens **only** if the host is on the own-sites list (`anyuse workflow acknowledge-host <host> --ignore-robots --reason "my own site"`). Otherwise the default applies. |
| `login_or_2fa`, host in the user's **own-accounts list** | The run continues in the same attached session **by default**. The own-accounts list is `<data>/accounts.json` (`credentials/accounts.py`); only entries with `created_by: user` count, not agent-created accounts. |
| `login_or_2fa`, any other host | The person does the step, as with the default challenge policy. |

Continuation also needs a runner that can share the person's attached session
(`supports_attached_session`). The compiled HTTP request step cannot, so its steps
always fall back to the person doing the step. The policy is checked when the
card is posted and again when the answer arrives; the narrower result wins.

`ANYUSE_CHALLENGE_POLICY` is in the settings registry (`anyuse settings`),
security-relevant, with the same fail-closed default.

## Human solves live

`ANYUSE_CHALLENGE_POLICY=human_solves_live`: the person solves the challenge while
AnyUse waits, or relays their pointer and keys; no model sees the page. Code:
`execution/live_solve.py`; engine hook
`Engine(origin=..., live_solver=...)`; tests `test_live_solve.py` (offline).

**Who gets it.** Every run carries an `origin` in its hash-covered metadata:
`user_task` (a person started it for one task; every MCP tool call), `scheduled`,
`bench` or `bulk`. The policy applies only to origins listed in
`ANYUSE_LIVE_SOLVE_ORIGINS` (default `user_task`; adding `scheduled` or `bulk`
is security-relevant and needs the terminal confirmation). Every other run, and
a run with no origin, falls back to `human_does_step`. `bench` is rejected by the
setting, and a bench engine or solver with the policy set refuses to start.

**Attached browser** (the user's own visible browser). The card / prompt says
"solve it in your own browser window". AnyUse then only observes the pinned
page, read-only, until it leaves the challenge state: no challenge marker from
the product's detector (`blocked.py`), still the task's host, and content
present. Then the blocked step is re-dispatched and THIS task continues.

**Headless** (AnyUse's own browser). AnyUse opens a live view:

* a stdlib HTTP server bound to `127.0.0.1` on a random port (any other bind
  address is refused; a non-loopback client, a foreign `Host` header or a
  cross-origin POST is refused);
* a one-time token in the URL: the first browser that opens it gets an
  HttpOnly, SameSite=Strict claim cookie; a second opener is refused;
* it expires when the check clears, when the person declines, or after
  `ANYUSE_LIVE_SOLVE_TIMEOUT_S` (default 900 s), and then closes;
* it shows periodic `Page.captureScreenshot` frames of that ONE page target,
  held in memory and sent only to that local viewer (not stored, logged or given
  to a model);
* it relays clicks, drags, the wheel and keys as `Input.dispatchMouseEvent` /
  `Input.dispatchKeyEvent` to the same target only. Events are rebuilt from
  scratch and bounded (coordinates inside the viewport, Shift is the only
  modifier, browser keys such as Back/Refresh/F5 are refused). The view has no
  URL field and no navigation endpoint; `Page.navigate` is not relayable. If
  the page leaves the task's host, the view closes.

**How the person is asked.** Elicitation first (the view link is in the form
message, shown by the client to the person). A CLI run in the person's own
terminal writes it to `/dev/tty` (not stdout), only without agent markers.
Otherwise, and for scheduled/bulk runs, a `challenge` card with `how:
solve_live` is posted, a value-free notice goes out through
`ANYUSE_NOTIFY_CHANNEL`, and the run **waits** (up to the timeout) instead of
failing. The link is kept Fernet-encrypted with the card, not in the card itself,
a notice, a log or a tool result:

```sh
anyuse inbox live <id>   # terminal gate + typed-back code; prints the link on /dev/tty
anyuse inbox skip <id>   # stop waiting: the run falls back to human_does_step
```

**After.** Cleared: a `live_solve` receipt and a `clearance` receipt (channel
`live-solve`) are appended and the step is re-dispatched in the same page
(`ctx["session"] = "live_solved"`); the host does **not** become human-only.
Not cleared (timeout, declined, navigated away, no channel): the usual
`human_does_step` card. One live solve per step per run.

**Session scope.** A cleared session serves the rest of that ONE task only
(`SolvedSessions`, keyed by task and host; later steps on that host get
`ctx["live_session"]`). The next task on that host asks again, also inside a
bulk run. When the task completes or is cancelled, the grant is dropped and a
headless browser's data for that origin is cleared (`Storage.clearDataForOrigin`).

**Receipt.** `type: live_solve`: task, run, manifest, step, host, origin,
mode, who (OS user and surface), channel, card, asked/ended times, outcome,
number of relayed inputs, `models_on_challenge: none`. No screenshot and no view
link.

**No model on a challenge page.** While a page shows a challenge it is
quarantined: the learn agent stops (`blocked: challenge`) before its model is
called, the L3 screenshot helper refuses, and `QUARANTINE.guard(hook)` wraps
any other model hook.

**Browser runners (wired).** The learn agent (`agent_browser.StepAgent`,
`EscalatingAgent(live_solver=, origin=, task_id=)`), the CDP replay
(`cdp_replay.ReplayExecutor/CdpDriver(live_solver=, origin=)`) and the attach
recorder (`attach.attach(..., live_solver=, origin=)`, CLI `anyuse attach
--origin user_task`) open a live solve on a detected challenge instead of
stopping, when the solver's policy applies to the run's origin. Headless
runners (agent, replay) solve in the SAME still-open page target and read it
again after it clears; the agent's model is called only after that. The
attach recorder stays read-only: the person solves in their own browser, no
snapshot is stored while the check is up, and the outcome lands in the session
file (`live_solves`). One solve per run/page; the bench driver passes no solver
and no origin. While a solve is active, the page may load the provider's own hosts
(GET/HEAD, that target only):
`docs/TRAFFIC_POLICY.md` "Live-solve exception"; the receipt counts them
(`provider_requests_allowed`).

**Runner contract.** A runner opts in with `supports_live_solve = True` and
`challenge_page(ctx)`, returning its pinned page: `target_id`, `mode`
(`attach` | `headless`), `observe()` (url, text, items) and, headless only,
a thread-safe `send(target_id, method, params)` plus `viewport`.

## Values

A value is typed in a sensitive form field or on `/dev/tty` with echo off. It
is checked against its declared type and the postconditions in memory. While
its run waits, it is **Fernet-encrypted** under the vault's key custody:

* the named key is `k_inbox`, from `watcher/entity_vault.load_or_create_named_key`;
* the key lives in the Keychain, or in the 0600 key file in degraded mode;
* if the custody does not actually keep the key, AnyUse refuses rather than
  writing plaintext.

The receipt holds only a placeholder, and digests are computed over it. The
value is revealed only in memory, immediately before the runner that consumes
it, and deleted when the run completes or is cancelled. It does not appear in a
receipt, a card, a log, a terminal echo or a tool result.

## Storage

`<ANYUSE_DATA_DIR>/inbox/` holds `cards/<id>.json` and `values/<run_id>.json`
(encrypted). Files are 0600 and written atomically; the directory is 0700.
Cards are kept after they are answered, for audit (`status`, `answered_via`).

## Limits

* **Elicitation trust.** A client that auto-accepts forms answers for the
  person; the client's UI is the trusted surface, as with Claude Code's
  permission prompts.
* **Degraded custody.** Without a Keychain, the key sits in the 0600 key file
  next to the other vault keys.
* **Standalone results.** For a direct call, the person's `result` text goes
  back to the agent as data (`human_result`). It is not verified.
* **Live solve runners.** No production runner implements `challenge_page`
  yet: the compiled HTTP request step cannot hold a page. A guarded headless session
  refuses third-party hosts, so a vendor challenge that loads from another
  host cannot render there; the live view is useful where the runner's traffic
  policy lets the challenge's own resources load.
