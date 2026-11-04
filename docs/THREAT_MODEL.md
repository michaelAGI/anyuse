# Threat model

## What leaves the machine

Model use is role-scoped and explicit. The resolved provider receives only the
following data for that role:

| role | data sent |
|---|---|
| planner | task text, supplied parameters, workflow intents and reliability history, current DOM/AX text, state-graph summaries, perception signals, and up to 2,000 characters of app-guide context |
| visual | the current screenshot as an in-request data URL, the step intent/target/expected result, and up to 8,000 characters of DOM/AX context |
| agent | task text, the last four chosen actions, current URL, up to 40 interactive DOM elements, and up to 2,500 characters of visible page text; no screenshot |
| judge | bounded drift/block evidence and bounded candidate labels/identifiers; the LLM tier receives no screenshot unless a caller explicitly places one in evidence |
| model check (`ANYUSE_MODEL_CHECKS`: OpenJev, Laya, Jev or the judge model) | for an empty answer of a compiled web tool that the site's own pages do not settle, on a tool call only: the search input (cut to 80 characters) and at most six page lines that name it or stand just below such a line, each with the two lines above it, up to 1,200 characters per question; for a result name or a "no results" line in Chinese, Japanese, Korean or Thai that holds the input inside a longer word, on a tool call only: the input (cut to 80 characters), up to 120 characters of that name or line on each side of it, and one candidate word per question; in a heal, or the check of a request out of service, whose result page alone does not say which list is the answer: the input (cut to 80 characters), the page's visible lines (or the answer's first ten entries) up to 1,200 characters per question, and one list entry or other line per question, at most six questions; in a compile, `learn`, `attach` or HAR import: for an item page's value, the field's label (cut to 80 characters), up to 40 lines of the recorded page within 1,200 characters and one line that holds a value per question; for a chain's fitting input, the site's host, the input, the first three result rows (eight fields each, values cut to 60 characters) and one field name with up to three of its values per question; in a demonstration or the learn agent whose typed search did nothing: the labels of the controls next to the search box, or the search addresses the page shows, with the box's name, label and placeholder (never in a replay or a proof); never a challenge page |

Screenshots therefore leave the machine only on the visual role. The web agent sends DOM/visible text,
not pixels. Page, AX and recording content is always placed in model **data**;
it is not promoted into trusted instructions. Structured output is still
untrusted and is parsed/validated before it can select an action.

Deterministic compiled and browser replay report `llm_calls == 0` and send
nothing to any model, except, when a model check is configured, an empty answer
the site's own pages do not settle on a tool call, an input that stands inside a
longer word of a script without spaces on a tool call, and a heal or the check of
a request out of service whose result page alone does not say which list is the
answer; their calls are counted in `model_calls`. A swap is still decided by a
re-proof that asks no model. Missing credentials refuse locally. Provider 402,
408/429/5xx after the adapter's bounded retries, timeouts and DNS/connect
failures return `provider_unreachable`; callers never receive a silent empty
answer. See `docs/MODEL_CONFIGURATION.md` for endpoint, credential, retry and
precedence details.

## Web scope of the guarded browser

`execution/web_scope.py`, `docs/TRAFFIC_POLICY.md` ("Web scope"). What each
setting changes for an attacker-controlled or careless page:

* **Search providers.** Under `ANYUSE_THIRD_PARTY=search` (the default) a
  third-party host becomes the site's provider for the run when the site's own
  page sends it the run's typed value in a data call (an XHR, fetch, JSONP
  script or event stream; the value as a parameter, body field or path segment,
  never inside another URL). The grant is per site and covers GET/HEAD under the
  path the value went to, their CORS preflight, and a small read-only search
  POST right after typing. Right after typing, the CORS preflight of such a POST
  goes to any host not on the snapshot whose path the POST rule would not refuse,
  because the preflight comes before the body that carries the value. A host on
  the ad/tracker snapshot, a challenge provider, a never-contacted host, a
  telemetry path and a path or body field that names a state change are
  refused; `robots.txt` of the host and the
  site's budget (the provider's share of 10 requests per page load) apply. A
  page can also name a provider from the declared catalogue in its OWN
  document/script, which lets that provider's widget script and documented read
  query through. A malicious or careless page can therefore make the guard send
  the run's typed value, and reads after it, to a host the snapshot misses: the
  same requests a visitor's browser sends. Every such host is on the run record
  (`third_party.observed`). The guard never extracts, stores or reuses a key;
  catalogued key names are redacted in records, an uncatalogued provider's key
  names are not.
* **Provider keys in compiled HTTP tools** (`ANYUSE_PAGE_KEYS`, on). A field with
  a credential-like name is kept only when the recording shows the site's own
  page (its registrable domain) stating that exact value by name earlier; the
  tool stores the name and its punctuation, never the value, and reads the value
  from that page per call, fetched without cookies. The recorders replace the
  value in requests and bodies; results, traces and records show `<page value>`.
  No cookie and no `Authorization` header is ever read from the page or sent this
  way, and in attach no field sent after a password field. The page is fetched
  without cookies, so a value only a signed-in page states is not read. Residual: a
  site whose anonymous page states a key gets that key sent to the host the page
  sent it to, as a visitor's browser does. A
  provider's search POST in the read shape of the observed-provider rule runs once
  its host is acknowledged, without `ANYUSE_API_READONLY_POST`.
* **Read-only POST search.** Syntactic: same site only; the page's own POST
  search form (fields limited to the form's), a small body sent right after the
  run typed into the search box with a field whose whole value is the typed
  value, or a small body carrying the run's search value in a search-like
  field; state words in path or fields refuse;
  multipart, bodies over 8 KB and credential headers refuse. A site that
  performs a state change behind a POST named like a search (`/search` with
  `q=value`) would receive it: the residual risk, bounded by the value being
  the run's own search text and by the site's budget.
* **Own hosts.** Registrable-domain equality with a built-in suffix list (a
  generic second level under a country code is a suffix; an optional pinned
  Public Suffix List decides when present); under a country suffix the build
  does not list, the site's own document, script or redirect must name the
  host; shared-hosting suffixes are per tenant; tracking-labelled and blocklisted
  subdomains refused; declared hosts need an exact name and a reason and may not
  be ad/tracker/challenge hosts.
* **Third-party policy.** `ANYUSE_THIRD_PARTY=any` sends to any third-party host
  the page uses for its content that is missing from a 57k-domain snapshot
  (read-only, robots-checked, budgeted); new trackers the snapshot misses are
  sent. It never sends to challenge providers. It is security-relevant and needs
  a confirmation with a warning.
* **Unchanged:** GET/HEAD for everything else, account paths refused, robots
  respected on every host (except the site's own origin in a signed-in task you
  started, recorded on the run), fatal third-party responses, one budget per site.

## MCP launch profiles

`ANYUSE_MCP_PROFILE` accepts `default` (the Launch 1 default), `meta` and `full`.
Unknown values fail closed to `default`. The default profile exposes only:

* proof-pinned/current compiled web tools;
* proof/digest-pinned compiled desktop tools from the fixed
  `desktop_compile` classes and known Blender `app_script` shapes;
* list/details/explain/status and guide reads; and
* proposal-only learning.

The dispatcher derives the same exact allowlist as `tools/list` on every call.
A client therefore cannot invoke `run_task`, `desktop_execute`, screen context
or raw workflow replay by remembering or guessing its name. Compiled desktop
steps run only through `execute_checked_step` with the pinned workflow and
state graph, preserving D11 identity refusal, drift evaluation and independent
after-action evidence. A call started with its values is the
approval for every class, file/write included; a record whose
approval level opts into holds (`all` / `destructive`) is not run over MCP, and
every compiled desktop tool runs only in the guest VM through its job agent
([DESKTOP_TOOLS.md](DESKTOP_TOOLS.md)): with no guest runner the call is refused.

`ANYUSE_MCP_PROFILE=meta` serves the compiled part of `default` through four
fixed tools (`list_sites`, `list_operations`, `call_operation`, `login`;
docs/MCP_META.md). `call_operation` dispatches through the same server
closures as `run_compiled_workflow` and the `desktop__<workflow>` tools, so
every gate above applies unchanged; it adds a schema check in front and
removes none. `login` acts on nothing. The dispatcher allows exactly those
four names. It is a projection of `default`, not a relaxation, and needs no
typed confirmation.

`ANYUSE_MCP_PROFILE=full` is an explicit compatibility/debug escape hatch for
the general agent and free-form desktop actuator. They are excluded from
`default` because restrictions inside the compiled executor do not constrain a
sibling raw actuator. Even `full` constructs agents with `auto_confirm=False`.

## Run-wide model budget

Every OpenAI-compatible adapter owns a `RunBudget`; all escalation levels in
one agent run share the same object. The default limit is US$1.00 (about 30
Sonnet-class decisions) and
`ANYUSE_RUN_BUDGET_USD` sets a different non-negative limit, or `unlimited`
(raising or lifting it from the CLI needs the typed code). Before every
provider request—including 408/429/5xx adapter retries, empty-answer retries,
unreadable-answer repair calls, judges and rankers—the adapter reserves the
conservative maximum:

`max_tokens × (input list price + output list price) / 1,000,000`.

If that reservation does not fit, the request is refused locally as
`budget_exhausted`. A successful response settles the reservation to provider
cost or token usage. If a request was sent but no trustworthy usage is
available, its maximum remains charged; failure cannot create free budget.
Credential-less escalation levels are removed before the run and logged once,
so an unavailable level cannot consume attempts or hide a budget stop.

In a process started through the `anyuse` CLI or the MCP server, the same
reservation must also fit the global daily cap
(`ANYUSE_SPEND_DAILY_USD`, default US$5 per UTC day) beside today's rows in
`spend/ledger.jsonl`, and the settled cost is booked there. An unreadable
ledger refuses every request (docs/SETTINGS.md, "Spend caps").

Escalation remains off by default. To opt in with a tighter per-run cap:

```sh
ANYUSE_AGENT_ESCALATION=on ANYUSE_RUN_BUDGET_USD=0.25 anyuse learn ...
```

## Invariants the tests hold

* **A model verdict can only escalate.** `apply_triage` lets the judge move a
  deterministic outcome to a more conservative one and nothing else; an
  abstaining, failed or unreachable judge leaves the outcome alone, and a veto
  can never turn an abstention into a match (`test_judge.py`).
* **An outage is never a verdict.** A provider that does not answer
  (`provider_unreachable`), answers with nothing (`provider_empty`) or does not
  answer in protocol twice (`model_unreadable`) stops the agent tier's loop
  without an escalation and without a failed-round entry
  (`test_agent_escalation.py`). Compiled and browser replay make no model call
  beyond the model checks above.
* **Unreadable is not done.** A reply that is not a JSON action is never turned
  into `done`. The parser is tolerant on the way in and strict on the way out
  (`test_learn_loop.py`).
* **No provider request escapes the run cap.** Reservations happen inside the
  adapter immediately before each send, including its own retries. Tests use
  exact-loopback fakes to prove a retry is never sent when its reservation does
  not fit (`test_run_budget.py`).
* **MCP dispatch equals MCP discovery.** The default profile refuses raw tool
  names even when called directly, and compiled desktop writes stop before the
  checked executor when no human approver exists
  (`test_launch_mcp_profile.py`).

## Routine approvals: the out-of-band human channel

An MCP client may itself be a model. In the default MCP profile, a routine
approval or reconcile therefore counts only when it was written through the
CLI (`anyuse routine approve|reconcile --confirm <digest>`). The digest binds
the run, step, bound inputs, effects, action pins and manifest version
(`docs/ROUTINES.md`). The remaining threat is an agent with a shell running as
the same user. Against it, the CLI adds a terminal gate:

* **The terminal gate** (`execution/human_gate.py`) on
  `anyuse routine approve|reconcile`. `--confirm <digest>` still binds the
  request/effect, but it alone is no longer sufficient:
  1. stdin **and** stdout must be TTYs. The answer is read from `/dev/tty`,
     never from stdin, a pipe, the environment or an argument.
  2. The effect summary is shown on the terminal: manifest, version, run,
     step, effects, pins, bound inputs, digest. The person types back a
     six-character code derived from the digest and a fresh random nonce.
     The code is never an argument.
  3. With a well-known agent environment marker present (`CLAUDECODE`,
     `CLAUDE_CODE_*`, `CODEX_*`, `GEMINI_CLI`, `CURSOR_AGENT`, `AIDER_MODEL`,
     `OPENHANDS`), the command refuses unless a per-machine opt-in file
     exists. The file is written only by `anyuse routine
     allow-agent-terminals` from a TTY session with no marker, after the same
     typed-back code. It is 0600, owned by the user, names this host, and
     lives in the secret dir, which is pinned to the home directory.
     `--revoke` removes it.
* **This raises the bar; it is not authentication.**
  * An agent that can allocate a pseudo-terminal, clear its environment
    markers, or write the opt-in file itself can still pass.
  * A person can also be talked into typing the code.
  * The stronger path, a macOS LocalAuthentication / Touch ID prompt bound
    to the digest, belongs to the launch-2 app.

## Asking the human, and anti-bot challenges

(`docs/HUMAN_INBOX.md`, `execution/human_inbox.py`.)

**AnyUse never solves a CAPTCHA, never automates a challenge, never evades
bot detection, and has no stealth mode.** There is no User-Agent spoofing, no
token replay and no retry-until-it-works. A detected challenge, login wall or
403 becomes a card for a person. That includes a direct compiled-tool call,
which returns `status: needs_human`.

| Threat | Control |
|---|---|
| Automating past a third-party challenge by reusing the session a person cleared | Default `ANYUSE_CHALLENGE_POLICY=human_does_step`: the person does the blocked step and hands back its result. The host becomes human-only for the run. Unknown values fail closed to this default. `continue_in_own_session` applies only when all three hold: the host is on the own-sites list (per-host override with a mandatory reason), the runner shares the person's attached session, and the policy is still set when the answer arrives. |
| Continuing after a login on someone else's account | A login continues in the session only for a host in the user's own-accounts list (`accounts.json`, entries with `created_by: user`) and only with a session-sharing runner. Every other login is `human_does_step`. |
| Hammering a host that said no | A `blocked` receipt stops the step. No resume re-dispatches it until a person answers the card. |
| Accepting tracking cookies on the user's behalf | Browser consent handling has no automatic accept operation. `ANYUSE_CONSENT_POLICY=reject_nonessential` matches only exact reject/necessary-only labels or vetted framework reject selectors, with an explicit accept/agree/allow deny-list; it may traverse one exact settings layer. `ask` stores the person's per-host answer in a 0600 data file, but even a remembered `accept` remains a human hand-off. `never` preserves the previous no-interaction stop. A banner over served content is untouched, and tier-1 HTTP requests are unaffected. |
| A model answering for the person (an MCP client may be a model) | No tool call answers a card. The default profile refuses `resume(human=...)` and in-band approval/reconcile. A person answers through one of two channels. **Elicitation**: the form is shown by the client's own UI, like Claude Code's permission prompts; the client is trusted like that UI, and no code is typed. **CLI fallback**: TTYs, agent-marker refusal, `/dev/tty` I/O, and a typed-back code bound to the card, run, step, inputs and answer. Both channels go through the same engine checks: pending step, inputs digest, preconditions, approval/reconcile digest, then the fresh checks on resume. |
| Leaking a value only the person knows | It is typed in a form field marked sensitive (`writeOnly`), or on `/dev/tty` with echo off. Receipts and digests carry a placeholder. While the run waits, the value is Fernet-encrypted with the `k_inbox` key under the vault key custody (Keychain, else the 0600 key file). If the custody does not keep the key, AnyUse refuses rather than writing plaintext. The value is deleted when the run ends. Tests assert it is absent from receipts, cards, files, logs and tool results. |
| A model solving a challenge under `human_solves_live` | The person solves it. The live-solve code imports no model. While a page shows a challenge it is quarantined: the learn agent stops before calling its model, the L3 screenshot helper refuses, and `QUARANTINE.guard` refuses wrapped hooks. Screenshots of the page go only to the local viewer, in memory; they are never stored, logged or put in a receipt. |
| An agent or another machine driving the live view | Bound to `127.0.0.1`/`::1` only (anything else is refused at construction); non-loopback clients and foreign `Host` headers (DNS rebinding) get 403. The URL carries a 256-bit token; the first opener gets an HttpOnly SameSite=Strict claim cookie and a second opener is refused. Input needs the claim and a same-origin `Origin`. The link reaches only the person: an elicitation form, `/dev/tty` of their own terminal (agent markers refused), or `anyuse inbox live` behind the terminal gate. It is Fernet-encrypted at rest and never in a card, notice, log, receipt or tool result. It expires on clear, decline or timeout. |
| The live view as a general remote control | One pinned target. Only `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent` and `Page.captureScreenshot` can be sent (plus `Storage.clearDataForOrigin` at task end). Events are rebuilt from scratch: coordinates inside the viewport, Shift the only modifier, browser keys refused, at most 60 events per second. No navigation endpoint; leaving the task's host closes the view. |
| A cleared session reused beyond the task that asked | `SolvedSessions` is keyed by task and host and checks the origin. It is dropped when the task completes or is cancelled, and a headless browser clears that origin's data. The next task, including the next item of a bulk run, asks again. |
| Unattended or benchmark runs depending on human solving | Only origins in `ANYUSE_LIVE_SOLVE_ORIGINS` (default `user_task`; widening needs the terminal confirmation and prints a warning). `bench` is rejected by the setting, and a bench engine or solver refuses the policy outright. |
| The live-solve provider-host exception used to reach third parties | Declared hosts only (exact host plus path prefix, https, default port), GET/HEAD only, only for the one page target with an active solve, granted when the solve opens and revoked on clear, expiry, decline or page close. Each decision is logged by host and method (never the URL). The benchmark guard never consults it; bench never opens a solve. Tests: `test_live_solve_wiring.py`. |
| A dead or hung container browser turned into a product verdict, or a host re-hit after a crash | `browser_supervisor`: health ping before a site (no request reaches the site if the browser is down), crash/stall/drop become `infrastructure` (in no rate, never re-run), a dropped connection re-pins only the same target and never while an interceptor was active. Only `anyuse-browser-*` containers are ever restarted. |
| A stale answer applied to a moved-on run | The pending step, the inputs digest and the manifest version must still match, and the preconditions must hold now. Otherwise the card is `superseded`. |

Not covered:

* The CLI gate raises the bar; it is not authentication.
* An elicitation answer is exactly as trustworthy as the MCP client that shows
  the form. A client that auto-accepts forms can answer for the person. Run
  AnyUse with clients whose elicitation UI you trust, or without elicitation
  (the CLI fallback).
* A person can be talked into approving or typing a code.
* Loopback is not authentication. Another local process of the same user that
  learns the live-view link before the person opens it could claim it; the
  link is shown only on the person's surfaces and is one-time, but a local
  malicious process is out of scope, as elsewhere. Environment variables can
  set `human_solves_live` without the terminal confirmation, as for every
  setting.

## Stuck-run triage and notifications

(`execution/triage.py`; [NOTIFY.md](NOTIFY.md), `execution/notify.py`.)

| Threat | Control |
|---|---|
| A page or log talking the triage into a riskier outcome | Deterministic rules decide first. A ranker (`ANYUSE_TRIAGE_RANKER`, default `none`) is asked only for states the rules leave ambiguous. It gets the evidence as data only, chooses from `allowed_options`, and a rule validates its answer: never `human_approval` for a read-only step, never `escalate_model` with no level left, never `retry_later` without a transient signal. An abstention, failure or overruled answer falls back to `give_up`. |
| A provider outage read as "the task is impossible" | Provider stops are always `retry_later`, never a verdict. |
| Triage leaking data to a model | `none` sends nothing. `jev`/`llm` send the bounded `StuckState.evidence()`: stop words, block code, host, and at most 300 characters of the check's or error's own text. Typed values are never part of a state. `laya` is a slot that refuses (not installed); nothing is downloaded. |
| A notice leaking a value or where the person works | A notice carries only the card id, the kind of help and how to answer. Never the host, the instruction, a value, an error text or a secret. Tests assert this. |
| AppleScript injection through a notice | The script is a constant string with `on run argv`. The recipient and the text are separate argv items; the recipient is validated (one phone number or e-mail address). The tests never execute `osascript`: they use a fake runner, and the guard blocks it. |
| A webhook URL that exfiltrates or is redirected | `ANYUSE_NOTIFY_WEBHOOK_URL` is security-relevant (terminal confirmation). It must be `https://`, or `http://` to loopback only, with no credentials; an invalid value disables notices. No proxies, no redirects followed, and the URL is never logged. |
| Notice spam / a loop | Each card is notified at most once (persisted, 0600). There are at least 60 s between notices and at most 10 per hour. |
| A reply to a notice answering a card | Not built. Notices are outbound only. Reading iMessage replies would need Full Disk Access to `chat.db`; that stays a later opt-in, and a reply would still go through the inbox checks. |
