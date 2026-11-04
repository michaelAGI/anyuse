# AnyUse settings

AnyUse behavior settings have one persistent home: `settings.toml` in the
AnyUse data directory (`~/.anyuse/settings.toml`, or
`$ANYUSE_DATA_DIR/settings.toml`). Use the CLI rather than editing the file when
possible:

```console
anyuse settings
anyuse settings get ANYUSE_RUN_BUDGET_USD
anyuse settings set ANYUSE_RUN_BUDGET_USD 0.5
anyuse settings reset ANYUSE_RUN_BUDGET_USD
```

Resolution is always **explicit argument > environment variable > settings
file > built-in default**. Thus existing deployments can add no file and keep
their current behavior, and a temporary environment override does not rewrite
the file. `reset` removes only the file value; an environment variable may
still win.

The TOML file is a flat table. AnyUse writes it atomically with owner-only
permissions and uses Python's standard-library `tomllib` to read it. Secrets,
credential-file paths, browser endpoints, and storage-location variables do
not belong in this file.

## Registry

An asterisk marks a security-relevant setting. Setting a value that enables or
relaxes one with `anyuse settings set` asks for the same typed-code confirmation
in the terminal as `anyuse routine approve`; an environment variable is read
without it.

| Setting | Type / values | Default | Meaning |
|---|---|---:|---|
| `ANYUSE_MCP_PROFILE` * | `default`, `meta`, `full` | `default` | MCP tool surface. `meta` serves the same compiled operations and run paths as `default` through four fixed tools ([MCP_META.md](MCP_META.md)); it is not a relaxation and needs no confirmation. `full` exposes compatibility/debug tools, including raw agent tools, and needs confirmation. |
| `ANYUSE_RUN_BUDGET_USD` * | non-negative number or `unlimited` | `1.0` | Maximum model spend reserved for one run, in US dollars (about 30 Sonnet-class decisions). Raising it above `1.0` or setting `unlimited` needs confirmation; lowering it does not. See [Spend caps](#spend-caps). |
| `ANYUSE_SPEND_DAILY_USD` * | non-negative number or `unlimited` | `5.0` | Global cap on model spend per UTC day, in US dollars, over every request of an `anyuse` CLI or MCP server process; booked in `spend/ledger.jsonl`. Raising it above `5.0` or setting `unlimited` needs confirmation; lowering it does not. See [Spend caps](#spend-caps). |
| `ANYUSE_ATTRIBUTE_VERIFY` | boolean | `on` | On by default (about US$0.07 per 1,000 runs, and on a tool call up to about US$0.20 more per 1,000 product answers whose availability only the phrase lists read; `off` / `0` turn it off). Without a configured model the rules still decide and an attribute only the model could settle is withheld, typed `verifier_model_unavailable` (never ok). Verifier independence: a web answer's critical attributes (availability, price, currency, name, URL; a list's titles) must agree with an independent second reading of the page. The page's own evidence comes first (visible words its markup marks as the availability, the state of the product's buy control, currency symbols and ISO 4217 codes, the quoted echo of the input, the site's empty page), the verifier's phrase lists last; unknown phrases are uncertain. Only an uncertain attribute, or an availability only the phrases settled, asks the verifier's model (closed schema, abstention allowed; about US$0.0002 per call on Cerebras gpt-oss-120b), and only on a tool call over MCP: a watch run, a proof and a re-proof ask none. An attribute still uncertain withholds the answer. A two-source disagreement that no rule calls a known wrong pattern (the structured data vouches for the answer, the model reads the visible buy box otherwise) returns the answer as `needs_review`: never accepted, with both sources' evidence, 0 extra calls. Each row `visit_results` opens is checked on its own item page (its identity against its card on the list page, its fields); a row that does not verify is withheld and the table is incomplete. |
| `ANYUSE_WEB_FALLBACK` | boolean | `on` | On by default (about US$0.50 per 1,000 runs; `off` / `0` turn it off). Without a configured model it is never tried: the miss carries `fallback.reason_code = no_model_configured`, no site contact. Saved web tools over MCP ([MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md)): when a replay misses (failed, incomplete, a known wrong answer; never `needs_review`, which goes to the caller), the agent harness answers the same value on the same site with Cerebras gpt-oss-120b and a read-only browser, at most 8 model calls per run (the agent's decisions and the verifier's second stage), every request under the daily cap (`ANYUSE_SPEND_DAILY_USD`). Only an answer the independent attribute verifier passes is returned (`source: fallback`); it is booked (the verdict store, the repair step's last known good answer) so the next replay can be model-free again. Off: the MCP route is the CLI's replay path unchanged. |
| `ANYUSE_AGENT_ESCALATION` * | boolean | `off` | Permit escalation beyond the base learn-agent model after measured failure. |
| `ANYUSE_AGENT_MAX_STEPS` | integer | `20` | Agent harness: the most steps, one policy call each, an open-ended agent run takes. |
| `ANYUSE_AGENT_MAX_USD` * | non-negative number or `unlimited` | `1.0` | Agent harness: the most an open-ended agent run (`anyuse agent run`, MCP `agent_run`) may spend on its policy's model calls, in US dollars. A CLI policy books the CLI's reported cost. The run stops when it reaches the cap. `0` allows no model calls: a run whose policy calls a model stops before its first call. Raising it above `1.0` or setting `unlimited` needs confirmation; lowering it does not. See [Spend caps](#spend-caps). |
| `ANYUSE_AGENT_UNASKED_IRREVERSIBLE` * | `ask`, `stop` (`hold` = `ask`) | `ask` | Agent harness, open tasks: a commit step (a login, a cart, a form sent, a payment, a deletion) the task did not cover. `ask`: the caller is asked once at that step (MCP `confirm_required` with a resume token; a CLI prompt at a terminal); no answer, or no one to ask, means stop. `stop`: never asked; the run stops and reports it (unattended use). A covered step runs without a question (initiation = approval); a deviation always stops. There is no `allow`; no value needs confirmation. |
| `ANYUSE_MCP_AGENT_COMMITS` * | `human`, `agent` | `human` | `agent_run` over MCP: who does a commit step (a login, a cart, a form sent, a payment, a deletion). `human`: the run stops with `human_required` and you do that step, even when the task asked for it. `agent`: the calling agent's task approves it (`ANYUSE_AGENT_UNASKED_IRREVERSIBLE` then applies). Setting `agent` needs a confirmation. |
| `ANYUSE_AGENT_CONTAINER_SANDBOX` * | boolean | `off` | Agent harness: let a runtime-written tool run in the Apple container sandbox (no host mount but the task workspace, network off). Off: the sandbox is planned, never started. A running Tart VM refuses it. Enabling needs confirmation. |
| `ANYUSE_MAP_REVIEWER` * | string | `claude:sonnet` | Compile from the map (`anyuse workflow from-map`): gate R, the independent reviewer that must say the plan covers the task before the workflow is saved `ready` - `cli:model` (claude or codex), `none` disables it (then never ready). Cost: one CLI call per plan, ~5 s (about US$0.006-0.01 at API list price). |
| `ANYUSE_FILL_SHOW_VALUES` * | boolean | `off` | Agent bridge (`anyuse workflow fill`): the model also sees the values of the fields on screen (never a password field's: macOS does not expose it). Per run: `fill --show-values`. |
| `ANYUSE_APP_BLOCKLIST` * | string | empty | Draft replay and fill: apps (bundle ids, or names) an app switch (`open_app`) may never open; comma-separated. The bundle id decides first, the name is an extra. |
| `ANYUSE_APP_ALLOWLIST` * | string | empty | Draft replay and fill: when set, the only apps (bundle ids, or names) an app switch may open; comma-separated. |
| `ANYUSE_WATCH_DENYLIST_APPS` | string | empty | Watcher: more apps it never captures - bundle ids (exact) or app names (whole words); comma-separated. They add to the built-in lists: password managers (Keychain Access, Passwords, 1Password, Bitwarden, KeePassXC, ...) and banking / payment / finance apps (Wallet, Sparkasse, S-Banking, VR Banking, comdirect, DKB, N26, ING, Commerzbank, Deutsche Bank, Postbank, PayPal, Klarna, MoneyMoney, StarMoney, Outbank, ...). |
| `ANYUSE_WATCH_DENYLIST_TITLES` | string | empty | Watcher: more window-title words (whole words) that make a window never captured; comma-separated. They add to the built-in ones (password, Kennwort, private browsing, ...; Online-Banking, Überweisung, Kontoauszug, TAN, pushTAN, PayPal, Klarna, Sparkasse, ...). |
| `ANYUSE_FILL_FREE_TEXT` * | boolean | `off` | Agent bridge: the model may type its own text; it is saved in the step and typed on every later run. Per run: `fill --free-text`. |
| `ANYUSE_FILL_SENSITIVE` * | boolean | `off` | Agent bridge: password, payment and terminal steps can be filled; a password is typed from the credential vault by alias, never shown to the model or saved. Per run: `fill --sensitive`. |
| `ANYUSE_REA_NM` | optional absolute path | empty | Existing real `nm`/`llvm-nm` Mach-O tool for REA symbol evidence. Explicit API argument overrides this setting; otherwise only fixed existing CLT/Xcode toolchain locations are checked. System installer shims and scripts are refused without invocation. No tools are installed or selected system-wide. |
| `ANYUSE_MAP_EFFORT` | `default`, `rea-fast`, `rea-strict` | `default` | Mapping and proof effort tier. `default` records HTTP/DOM and AX workflows on normal runs with zero Frida overhead; `rea-fast` enables REA (`binary_runtime_hypothesis`) with a 3-trial (`1` learn, `1` fresh, `1` held-out restart) campaign; `rea-strict` uses the 9-trial (`3+3+3`) campaign. New REA workflows freeze the resolved effort; later setting changes do not alter existing workflows. An explicit spec or CLI effort takes precedence. |
| `ANYUSE_REA_AUTO_THRESHOLD` | non-negative integer | `3` | Number of vision/computer-use fallback runs on the same unmapped task before AnyUse flags it for REA escalation (`0` disables counting new runs). Counts persist across CLI/MCP and library calls in `workflows/vision_fallbacks.json` under the AnyUse data directory (or the supplied workflow storage directory). Reports include `rea_escalation`; this recommends a discovery/proof workflow and does not start one automatically. |
| `ANYUSE_REA_GUI_CAPTURE` | `auto`, `off`, `windows`, `live` | `auto` | GUI fallback capture mode. `off` disables capture. `windows` records bounded, redacted actuation timing and geometry. `live` requests around-actuation differential capture through an explicitly attached trusted probe. `auto` requests `live` for `rea-fast`/`rea-strict` effort, `windows` when the task reaches the REA fallback escalation threshold, otherwise `off`. The setting never attaches a probe or starts a mapping/proof campaign automatically. Without an available probe, live requests retain the redacted GUI window with a degraded capture status. Native candidates remain investigation evidence and create no task proof. |
| `ANYUSE_MODEL_PROVIDER` | unset, `claude-cli`, `codex-cli`, `openai`, `anthropic`, `gemini`, `openrouter`, `cerebras`, `local` | unset | Optional provider preset for all model roles; role-specific environment overrides still win. `claude-cli` and `codex-cli` run the Claude Code or Codex CLI you are logged into and need no key. `codex-cli` and `openai` also need `ANYUSE_MODEL_NAME`. `local` is an OpenAI-compatible server at `http://127.0.0.1:8000/v1`. The others read their key from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` or `CEREBRAS_API_KEY`. See [MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md). |
| `ANYUSE_MODEL_BASE_URL` * | string | unset | Optional OpenAI-compatible base URL for all roles. Prompts may be sent there. |
| `ANYUSE_MODEL_NAME` | string | unset | Optional model name for all roles. |
| `ANYUSE_MODEL_API_KEY_ENV` * | string | unset | Optional name of the credential environment variable for all roles. |
| `ANYUSE_MODEL_RETRIES` | integer `0`–`5` | `2` | Extra attempts after retryable provider failures. |
| `ANYUSE_MODEL_PRICE_IN`, `ANYUSE_MODEL_PRICE_OUT` | non-negative number | unset | Optional per-million-token list prices used by run-budget accounting. |
| `ANYUSE_<ROLE>_MODEL_<FIELD>` | typed as the matching global field | inherit | Per-role overrides for `PLANNER`, `VISUAL`, `AGENT`, `JUDGE`, `AGENT_L2`, and `AGENT_L3`; fields are `PROVIDER`, `BASE_URL` *, `NAME`, `API_KEY_ENV` *, `RETRIES`, `PRICE_IN`, and `PRICE_OUT`. |
| `ANYUSE_GEMINI_MODEL_FLASH` | string | `gemini-3.5-flash` | General perception/control Gemini model. |
| `ANYUSE_GEMINI_MODEL_COMPUTER_USE` | string | `gemini-2.5-computer-use-preview-10-2025` | Last-resort visual computer-use model. |
| `ANYUSE_ROBOTS` * | `respect`, `user_initiated`, `ignore` | `respect` | `robots.txt`. `respect`: RFC 9309 everywhere, except a signed-in task you started (an MCP tool call, `anyuse agent run` / `agent replay` that logs in with your `${secret:NAME}` credentials, or a compiled tool that uses your imported `login=replay` session): its requests to that site's own origin are not held to robots.txt, and each one robots.txt would refuse is recorded (`traffic.robots_override`: "a signed-in task you started"). `user_initiated`: a lookup or compile the user or caller explicitly started (an MCP tool call, `anyuse agent run` / `agent replay`) may fetch paths robots.txt disallows; multi-page and result-visiting tools and unattended or background work (watch runs, schedules) still respect it. `ignore`: robots.txt never applied. Pacing, budgets and the other traffic rules stay the same in every mode ([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md)); robots.txt is still fetched and counted, and the run record says what was overridden (`traffic.robots_overridden`: "robots would disallow"). Read once, in `api_permission.robots_override`. |
| `ANYUSE_ROBOTS_POLICY` * | `respect`, `ignore` | `respect` | The older global switch: `ignore` means `ANYUSE_ROBOTS=ignore`. Prefer `ANYUSE_ROBOTS`. |
| `ANYUSE_REFUSE_GOV_HOSTS` * | boolean | `off` | Refuse government, `.int`, EU-institution and military hosts (`execution/gov_hosts.py`) on every path before any request: runs, compiles, replays, robots.txt fetches, third-party requests, site modes. Off: they are treated like any other host. The never-contact list is refused either way. Turning it on needs no confirmation. See [TRAFFIC_POLICY.md](TRAFFIC_POLICY.md#hosts-that-are-never-contacted). |
| `ANYUSE_CONSENT_POLICY` | `reject_nonessential`, `ask`, `never` | `reject_nonessential` | Browser handling for cookie-consent walls. The default clicks only exact reject/necessary-only controls, not accept. |
| `ANYUSE_CHALLENGE_POLICY` * | `human_does_step`, `continue_in_own_session`, `human_solves_live` | `human_does_step` | After a third-party anti-bot challenge. `human_solves_live` lets the PERSON solve it live (their attached browser, or a loopback-only live view AnyUse relays their pointer and keys to); no model sees it. Setting either non-default value needs confirmation. See [HUMAN_INBOX.md](HUMAN_INBOX.md#human-solves-live). |
| `ANYUSE_LIVE_SOLVE_ORIGINS` * | comma list over `user_task`, `scheduled`, `bulk` | `user_task` | Run origins where `human_solves_live` applies. Adding `scheduled` or `bulk` means unattended runs stop on a challenge, notify you and wait for you; it needs confirmation and `anyuse settings` prints a warning. `bench` is never accepted. |
| `ANYUSE_LIVE_SOLVE_TIMEOUT_S` | integer `30`–`3600` | `900` | How long a run waits for a live solve; the live view expires then and the run falls back to `human_does_step`. |
| `ANYUSE_ROBOTS_TTL_S` | non-negative integer | `86400` | `robots.txt` cache lifetime; `0` disables disk caching. |
| `ANYUSE_DRIVE_ENABLED` * | boolean | `off` | Keystone permission for mouse/keyboard actuation. |
| `ANYUSE_HIL_MODE` * | `off`, `destructive`, `all` | `off` | Human approval holds for desktop actions. Changing to `off` is a relaxation and needs confirmation. |
| `ANYUSE_API_MODALITY` | boolean | `on` | Deterministic HTTP replay kill switch. |
| `ANYUSE_API_READONLY_POST` * | boolean | `off` | Permit separately approved read-only POST operations. Exact endpoint approval is still required. One exception does not need it: a search POST that the site's own page sends its search provider using a public key read from that page, in the read shape the observed-provider rule accepts (`ANYUSE_THIRD_PARTY`), runs once the provider's host is acknowledged (the proof asks for it). Every other POST needs this setting and its approval. See [TRAFFIC_POLICY.md](TRAFFIC_POLICY.md#provider-keys). |
| `ANYUSE_PAGE_KEYS` * | boolean | `on` | Compiled HTTP tools may read a search provider's public key, app id or index that the site's own page states by name from that page at run time, and send it as the page did. The tool stores where the page states it, never the value; results, traces and records show `<page value>` for a key. Each call then sends two requests: the page, then the provider. Off: the compile refuses such searches as before, and a tool already compiled this way sends nothing and leaves the call to the browser replay. A landing page's id is read whatever this says. Changing it needs no confirmation. |
| `ANYUSE_DEMO_MODEL` | `sonnet`, `cerebras`, `cascade` | `sonnet` | Which model makes a COMPILE's demonstration (`agent run --compile` with the default policy). `sonnet`: Sonnet through the Claude CLI (`claude -p --safe-mode`, no tools, an empty working directory, a clean environment: never a key or a secret), at most **US$0.40 per demonstration** by the CLI's reported cost, every call reserved and booked in the spend ledger inside `ANYUSE_SPEND_DAILY_USD`. `cerebras`: Cerebras `gpt-oss-120b` as before (about US$0.003 per compile). `cascade`: Cerebras first, then ONE Sonnet retry (the same cap) of a demonstration that failed for an agent-quality reason (no results reported for a listed value, max_steps, an unreadable answer, an ambiguous reference, no result region), once per site - never for robots, refusals, walls, typed site stops or a search that does not depend on its input. The run record and the saved tool name the model (`demonstrated_by`). Only the demonstration: the proof replays stay at 0 model calls, the verifier and the MCP route's fallback are unchanged. |
| `ANYUSE_DEMO_INSTRUCTIONS` | `v1`, `v2` | `v1` | The web demonstration's instructions. `v1`: the agent harness's prompt as it stood (byte-identical, `test_demo_instructions.py`). `v2`: the same plus five rules for site-search results (full-text matches, one row per result by its name, the result list only, `no_results` only on the page's statement, open the named search URL). Read once per demonstration policy (`harness_policies.system_prompt`, built by `harness_product`); the MCP route's fallback agent and the adapters keep their own prompts. |
| `ANYUSE_THIRD_PARTY` * | `off`, `search`, `any` | `search` | Guarded browser, third-party hosts. `off`: first-party only. `search`: the site's own search provider: the third-party host the site's own page sends the typed value to in a data call (GET/HEAD under that path, its CORS preflight, a read-only search POST right after typing; never a host on the ad/tracker snapshot, a telemetry path or a state change), plus the declared catalogue (Algolia, Doofinder, Klevu, Findologic, Searchspring, Google CSE, Yext, Coveo, Boost Commerce, ...) as the hint that lets a provider's widget script and documented read query through once the site's own page names it; the provider has its own share of 10 requests per page load (in the run total). Also the hosts the site's own document loads its static assets from (scripts, CSS, fonts, images; a host that answers with data loses it), among them the store platforms' asset CDNs (Shopify, BigCommerce, Wix, Squarespace), for static assets only, logged as `platform_cdn`. `any`: any third-party host the site's own page uses for its content (APIs, widgets, data CDNs), except ads/trackers on the pinned snapshot, never another site's page; needs confirmation. In every mode each third-party host is paced in the site's budget and listed on the run record. See [TRAFFIC_POLICY.md](TRAFFIC_POLICY.md#third-party-hosts-anyuse_third_party). |
| `ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS` * | boolean | `off` | **Superseded by `ANYUSE_THIRD_PARTY`; no longer read.** |
| `ANYUSE_ALLOW_READONLY_POST_SEARCH` * | boolean | `off` | Guarded browser: a same-site POST only when it is the page's own search (its POST search form's fields, a small body sent right after typing whose field holds exactly the typed value, or a small body carrying the search value in a field named like a search); state-changing paths/fields refused. |
| `ANYUSE_THIRD_PARTY_PAGE_CODE` * | boolean | `on` | Guarded browser: the page's own SCRIPTS and STYLESHEETS from any other registrable domain - GET only, by resource type; never images, fonts, media, XHR / fetch, websockets, beacons or a request carrying the typed value; never a host on the pinned ad / tracker snapshot, a NEVER_CONTACT host (or a government host with `ANYUSE_REFUSE_GOV_HOSTS` on) or a robots-disallowed URL; counted as browser statics. On with `ANYUSE_THIRD_PARTY` search / any; `ANYUSE_THIRD_PARTY=off` keeps it off. |
| `ANYUSE_ALLOW_SITE_OWN_HOSTS` * | boolean | `on` | Guarded browser: the site's own subdomains/CDN hosts (same registrable domain - a shared-hosting suffix counts per tenant -, or declared per site with a reason in `site_hosts.json`), GET/HEAD, in the site's budget; robots.txt of every host. ON by default; `off` = the site's exact host only. |
| `ANYUSE_THIRD_PARTY_POLICY` * | `block`, `allow_with_blocklist`, `allow_all` | `block` | **Superseded by `ANYUSE_THIRD_PARTY`; no longer read.** |
| `ANYUSE_LOCAL_FIXTURE_ORIGINS` * | comma-separated origins | unset | Exact HTTP loopback origins allowed for offline fixtures. Never use as a public-site exception. |
| `ANYUSE_COMPILE_LEVERS` | `default`/`all`, `none`/`off`, or comma-separated `repetition,volatile,post,binding,path,slots` (`nested` = `slots`) | `default` | Compiler experiment selection; `slots` binds a value inside decoded layers (for example a JSON value inside a query parameter). |
| `ANYUSE_TIER2_READINESS` | boolean | `on` | When a browser replay reads the page. `on`: once the search's own data calls for the input have answered (as many batches as the recording needed) and the result rows have stopped changing; the calls a page made while the input was typed (suggestions, previews) and requests that do not carry the input are not waited for. The recorded count checks that first read and is not waited for; a short answer whose results all name the input, or a page that says it found nothing for it, passes the check. Never later than the fixed timer's 10 s, and a read that comes up short is read again at 10 s. `off`: at the end of the fixed timer (10 s). Read when a tool is compiled or learned, and the tool keeps that choice: compile it again to change it. A tool `anyuse learn` saves as a browser replay alone keeps the settle its proof used, whatever this setting says. |
| `ANYUSE_AUTO_HEAL` | boolean | `on` | When a compiled web tool's request fails on an answer from the site (not a block, a server error or a timeout), or is taken out because it kept failing while the site's own page answered, and no browser replay or agent answers, AnyUse re-derives the broken step from plain HTTP reads of the tool's result page, else recompiles from those pages, proves the result on held-out inputs and swaps it in. Only for a page the server renders (checked when the tool is compiled and again on the live page); no browser; a configured check model (`ANYUSE_MODEL_CHECKS`) is asked only when the page alone cannot say which list is the answer; about 7 to 10 requests, 1 more page read for a made-up input when the list changed since the last good answer, and 2 more searches for made-up inputs when the heal changes the request (`ANYUSE_EMPTY_PROBE`). A failed attempt is not repeated for 30 minutes, and a tool that fails again within 10 minutes of a heal is left alone for 30 minutes ([COMPILE.md](COMPILE.md#healing-without-a-browser)). `off`: the call fails with the reason. |
| `ANYUSE_KEEP_EVIDENCE` | boolean | `on` | Compiled web tools: keep the recordings a tool was compiled from (a recording folder, a HAR, attach sessions, a `learn` run), and the pages of the last calls the agent step answered (up to 6 inputs, each at most a day old, with their redacted GET responses), in the tool's folder (`evidence/`) as evidence for an automatic recompile. A signed-in tool keeps nothing ([COMPILE.md](COMPILE.md#automatic-recompilation)). `off`: nothing new is kept. |
| `ANYUSE_CHAIN_FIT` | boolean | `on` | A search that opens a result (a chain) needs a second input to fit on. With none to spare, AnyUse takes another result's name from the first recording's search results and sends the chain's own search and open requests for it, plus one read of that result's page, through the same checks as the proof: at most 3 values, each with at most 3 plain-HTTP requests. With a browser replay configured, each value's page is loaded there first. That input never proves the tool. When no value fits, the compile names the values it tried. A small model is asked which field of a result is its name only when the site's own answer does not show it and a model check is set up (`ANYUSE_MODEL_CHECKS`). `off`: such a compile asks you to record one more input ([COMPILE.md](COMPILE.md#chains-pages-and-path-parameters)). |
| `ANYUSE_EMPTY_PROBE` | boolean | `on` | Compiled web tools and saved web tools: how an empty answer is checked against the site itself. When a tool is proved, AnyUse searches 2 made-up inputs and learns the page the site shows when it finds nothing, in any language; the proof pins it. A tool proved before learns it at its first empty answer: 1 search for an input known to have results (2 when the first finds nothing) and 2 made-up ones, once. An empty answer that is the site's own empty page costs no request; one the empty page does not settle by itself costs 1 more made-up search and up to 2 searches for inputs known to have results: at most 3. When the site has changed its empty page since, the first such answer learns it again: 2 more made-up searches, once. A saved web tool replays 2 made-up values when its proof passes and then reads its empty page with no request ([MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md#the-call)). Every search goes through the same checks as the call. `off`: no such searches; empty answers are judged by the check model on a product call, then by the page's statement in the listed languages ([COMPILE.md](COMPILE.md#when-an-empty-result-counts)). |
| `ANYUSE_JUDGE` | `none`, `jev`, `llm` | `none` | Optional conservative drift judge. `jev`: the Jev decision model on OpenRouter (`typesafe/jev-1.13`, optional and not publicly available; needs `OPENROUTER_API_KEY`); `llm`: your configured model provider ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md)). With `ANYUSE_MODEL_CHECKS=auto` and no OpenJev server or Laya directory set, the same model also answers model checks. |
| `ANYUSE_TRIAGE_RANKER` | `none`, `jev`, `llm`, `laya` | `none` | Ranker consulted only for stuck-run states the triage rules leave ambiguous; a rule validates its answer. `jev` and `llm` as for `ANYUSE_JUDGE`; `laya` is reserved (not wired in this release; see [KNOWN_ISSUES.md](KNOWN_ISSUES.md#small-decision-models)). |
| `ANYUSE_CONFIRMER` | `off`, `shadow`, `veto` | `off` | Second look after a deterministic verdict. `shadow` asks a local [Laya](https://huggingface.co/convaiinnovations/laya) model (`pip install laya`, Apache-2.0, by Convai Innovations; in an offline container, kept warm) at final and human-needed steps and logs the answer beside the rule's decision, changing nothing; `veto` lets only kinds that passed the enable rule make an outcome more careful. It never promotes and never clears a block. |
| `ANYUSE_CONFIRMER_LAYA_DIR` | path | unset | Local [Laya](https://huggingface.co/convaiinnovations/laya) wheelhouse + safetensors weights (populate with `python tools/confirmer/fetch_laya.py`), mounted as `/work` in the offline container. With `ANYUSE_MODEL_CHECKS=auto` and no OpenJev server set, Laya also answers model checks. |
| `ANYUSE_CONFIRMER_CHECKPOINT` | `en`, `typed` | `en` | Laya checkpoint the confirmer asks. |
| `ANYUSE_CONFIRMER_BACKEND` | `laya`, `openjev` | `laya` | Which model the confirmer asks. `openjev` kinds stay shadow until one passes the enable rule on held-out sites. |
| `ANYUSE_OPENJEV_URL` * | `https://` URL (`http://` only for loopback), no credentials | unset | [OpenJev](https://github.com/razorback16/openjev) `/v1/systemone` server. Unset = unavailable: every OpenJev question is "no opinion". When set, OpenJev also answers model checks while `ANYUSE_MODEL_CHECKS` is `auto`. Challenge pages are never sent. |
| `ANYUSE_OPENJEV_MODEL` | model id | `openjev-latest` | OpenJev model (DiffusionGemma); `laya-1.0` also works. |
| `ANYUSE_OPENJEV_TIMEOUT_S` | non-negative number | `3.0` | Seconds before an OpenJev question counts as no opinion. |
| `ANYUSE_OPENJEV_IMAGES` * | boolean | `off` | Send the page screenshot with an OpenJev question when the caller has one. |
| `ANYUSE_MODEL_CHECKS` | `auto`, `none`, `openjev`, `laya`, `jev`, `llm` | `auto` | Which small model answers a model check: one yes/no question per candidate line, where the site's own evidence leaves a check open. A yes counts only for a line the check then finds on the page. `auto`: [OpenJev](https://github.com/razorback16/openjev) when `ANYUSE_OPENJEV_URL` is set, else [Laya](https://huggingface.co/convaiinnovations/laya) (`pip install laya`, Apache-2.0, by Convai Innovations; populate with `python tools/confirmer/fetch_laya.py`) when `ANYUSE_CONFIRMER_LAYA_DIR` is a directory (the MCP server starts it at the first check, which has no opinion; a one-off command does not start it), else Jev (optional; not publicly available) when `ANYUSE_JUDGE=jev`, else your judge model when `ANYUSE_JUDGE=llm` and it is not a CLI provider, else none. With none of these set, nothing is asked. `none`: never ask. Challenge pages are never sent ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)). |
| `ANYUSE_MODEL_CHECK_TIMEOUT_S` | non-negative number | `10.0` | Seconds one model check may take for all its questions; past that it has no opinion. `0` means the default. |
| `ANYUSE_ROUTINE_SHADOW_RANKER` | `none`, `openjev` | `none` | Routines: at an agent-step hand-off with candidates, log which one OpenJev would pick. Receipts and cards never change. |
| `ANYUSE_NOTIFY_CHANNEL` | `none`, `imessage`, `webhook` | `none` | Notify you of a waiting card when no MCP client with elicitation can ask you (docs/NOTIFY.md). |
| `ANYUSE_NOTIFY_IMESSAGE_TO` * | one phone number or e-mail address | unset | Outbound-only iMessage recipient. |
| `ANYUSE_NOTIFY_WEBHOOK_URL` * | `https://` URL (`http://` only for loopback), no credentials | unset | Webhook that receives a value-free JSON card notice. |
| `ANYUSE_DRIFT_GATE_ENABLED` * | boolean | `on` | Non-actuating AX drift check. Disabling it needs confirmation. |
| `ANYUSE_DETERMINISTIC_REPLAY` | boolean | `on` | Try a recorded deterministic path before model planning. |
| `ANYUSE_DRIFT_MISSING_TOLERANCE` | number `0`–`1` | `0.05` | Fraction of required AX anchors allowed to be missing. |
| `ANYUSE_DRIFT_EXCLUDE_MENUBAR` | boolean | `off` | Exclude menu-bar descendants from AX drift fingerprints. |
| `ANYUSE_ELEMENT_MATCHER` | `cascade`, `weighted` | `cascade` | AX element resolution strategy. Weight-file selection remains path configuration. |
| `ANYUSE_ATTACH_TTL_DAYS` | non-negative number | `7.0` | Attach-session retention in days. |
| `ANYUSE_LEARN_CANDIDATE_TTL_HOURS` | non-negative number | `24.0` | Unconfirmed learn-candidate retention in hours. |

Boolean CLI values accept `on/off`, `true/false`, `yes/no`, and `1/0`.

## System-prompt handling (environment-only)

`ANYUSE_SYSTEM_PROMPT_MODE` selects how an explicit agent run handles supported
system permission and trust prompts. These modes apply only in a configured VM
lab scope; ordinary foreground guards remain in place. The environment variables
are separate from `settings.toml`.

| Mode | Behavior |
| --- | --- |
| `manual` (default) | No prompt inspection, policy-file access, model call or desktop input by the resolver. |
| `rules` | Match the complete dialog fingerprint to an explicit `allow`, `deny`, `open`, `dont_open` or `manual` rule. |
| `llm` | Ask the configured agent model once to choose among the current supported actions or manual handling, using the originating task and a sanitized dialog. |
| `allow_all` | Choose a supported affirmative permission/trust action inside the explicitly scoped VM lab. Password submission and unrelated destructive or financial actions remain excluded. |
| `configured_denials` | Compatibility mode for existing v1 exact privacy-denial policies; it never grants permission. |

Set `ANYUSE_SYSTEM_PROMPT_POLICY=/absolute/path/policy.json` for a private policy.
The `rules`, `llm` and `allow_all` modes require the v2 policy below, including an
exact VM boot, UID and GUI audit-session scope. Scope mismatches stop before a
model decision or button press. Authentication dialogs offer only supported
denial/manual handling: passwords and codes are never read, typed or submitted.

For the normal `anyuse agent run --app ...` route, set the mode on the client
and attach the existing guest job runner with `ANYUSE_DESKTOP_GUEST_SESSION`.
The pre-observation `resolve_system_prompt` operation runs inside that guest's
GUI-session job agent. `ANYUSE_SYSTEM_PROMPT_POLICY` on the client is an optional
absolute **guest** policy path sent as JSON data; omitting it uses the guest
process's configured policy path. The guest reads and validates the policy,
audit storage, VM/GUI state and its own `ANYUSE_DRIVE_ENABLED` authorization.
The client never reads that policy file and never falls back to host input.
Prompt resolution submits a unique guest job only once. Transport recovery
only retrieves that job's original receipt; duplicate IDs cannot dispatch or
replace the first receipt. A missing reply or an interrupted claimed job stops
with `reobserve_required` and never resubmits the choice.
LLM decisions with a caller deadline first obtain a specialized read-only
guest monotonic-clock sample. The full round trip is deducted before the one
mutating submission; queue delay cannot reset the deadline, and a restarted
server rejects the sample. This does not require synchronized wall clocks.
The default client mode sends no extra remote prompt probe. The explicit
`system-prompt inspect` and `resolve` commands below operate in the process
where they are invoked; run them inside the guest when preparing its policy.

Inspect the current prompt from the **same GUI-session process/runner** that
will resolve it, without pressing anything:

```console
anyuse agent system-prompt inspect --json
```

Review the complete snapshot. Copy its `scope` object and exact
`fingerprint_sha256` into an owner-only JSON policy file. SSH inspection reports
the SSH process's own audit session; it must not be substituted for the GUI
runner's session. Create an existing owner-only audit directory and use its
absolute path. Replace the example scope with the inspected values:

```json
{
  "format": "anyuse.system-prompt-policy/2",
  "audit_dir": "/absolute/private/audit-directory",
  "scope": {
    "kind": "vm_guest",
    "boot_id": "00000000-0000-0000-0000-000000000000",
    "uid": 501,
    "audit_id": 12345
  },
  "rules": [
    {
      "id": "reviewed-permission-choice",
      "fingerprint_sha256": "COPY_THE_EXACT_64_CHARACTER_SHA256_FROM_INSPECT",
      "action": "allow"
    }
  ]
}
```

The fingerprint hashes the complete observed dialog. Process and window
identity are independently revalidated before pressing; a changed dialog
requires a new rule review. Unsupported prompts, ambiguous controls and
unavailable requested actions stay manual. Existing v1 policies retain their
`anyuse.system-prompt-denial-policy/1` format with exact `button_title` denial
rules (`Nicht erlauben`, `Don't Allow` or `Don’t Allow`) under
`configured_denials`. `ANYUSE_DRIVE_ENABLED` remains guest-local authorization.

In `llm` mode, the host forwards only the redacted originating task, bounded to
8192 UTF-8 bytes without truncation. It forwards no separate values dictionary, history,
screenshots, model/provider settings, API keys or callbacks. Secret references
such as `${secret:AUTH}` remain references and are never resolved for this
decision. The guest uses its own existing agent-role model configuration through
`ModelAdapter`; it may differ from the host planner. Missing task/model
configuration, an oversized task, model errors and abstention stop for manual
handling. Provider configuration follows the normal `ANYUSE_MODEL_*` and
`ANYUSE_AGENT_MODEL_*` settings in the guest.
The caller forwards only remaining call, dollar and time limits. The guest
uses the smaller budget and deadline, sends one model request without retries,
and withholds input if usage is unknown, over budget or late. Reports include
`system_prompt_usage`: guest calls count once in the total, known costs and
budget charges are separate, and incomplete usage remains marked unknown.

`anyuse agent system-prompt resolve --json` explicitly attempts at most one
choice and returns `attempted`, `reobserve_required` and `next_action`. Supply
`--task 'Open the requested document'` in `llm` mode; the command never invents a
task from its caller label.
`next_action=stop` means a person must handle the situation; the command exits
with status 1. An attempted choice always invalidates earlier targets, including
an uncertain or failed press. Observe again and choose a new action before
continuing.

The desktop/REA agent harness and screenshot orchestrator resolve only before
a fresh observation and policy decision. After an attempt they discard cached
observations; the orchestrator also discards an invalidated UI recording.
Scripted and unknown harness policies stop with a structured
`reobserve_required` result so that a saved action cannot continue on a changed
screen; the built-in model policy makes a new decision from the new observation.
Read-only observations, readiness checks and per-action interlocks do not
resolve prompts. No pending click, coordinate, partial typing or native
invocation is resumed, and REA failures remain terminal. Audit receipts retain
the exact attempt and outcome under the configured private audit directory.

## Per-site web-tier modes (`anyuse sites`)

Four web-tier behaviours are **per-site options** rather than global settings, so
they do not live in `settings.toml` and have no environment variable. They are
stored per registrable domain in `site_modes.toml` (owner-only, atomic write)
and managed with a separate command:

```console
anyuse sites                                   # list sites with a non-default mode
anyuse sites show <host>
anyuse sites set <host> --identity browser --robots ignore --login replay --route replay
anyuse sites login <host> --browser chrome --profile "Profile 1" --db <cookies-db>
anyuse sites reset <host>
```

`identity` (`honest`/`browser`), `robots` (`respect`/`ignore`), `login`
(`none`/`replay`) and `route` (`ui`/`replay`) default to `honest`, `respect`, `none`
and `ui`; a site with no entry keeps those. Enabling a non-default value asks for the
typed-code terminal confirmation. Hosts on the never-contact list are refused, and
government hosts too with `ANYUSE_REFUSE_GOV_HOSTS` on. Details:
[SITE_MODES.md](SITE_MODES.md).

## Turning on escalation

Model escalation is off by default. Turn it on interactively and, for unattended
use, set a tighter per-run cap than the default:

```console
anyuse settings set ANYUSE_RUN_BUDGET_USD 0.25
anyuse settings set ANYUSE_AGENT_ESCALATION on
```

The second command requires terminal confirmation. An environment-only session
can still use `ANYUSE_AGENT_ESCALATION=on ANYUSE_RUN_BUDGET_USD=0.25 ...`; the
environment has higher precedence than the file.

## Spend caps

All model spend is governed by one daily cap, `ANYUSE_SPEND_DAILY_USD`
(default US$5 per UTC day), with narrower caps beneath it:

| Cap | Setting | Default | Booked in |
|---|---|---:|---|
| Global, per UTC day | `ANYUSE_SPEND_DAILY_USD` | `5.0` | `spend/ledger.jsonl` |
| Per run | `ANYUSE_RUN_BUDGET_USD` | `1.0` | the run's own budget (not on disk) |
| Per open-ended agent run | `ANYUSE_AGENT_MAX_USD` | `1.0` | the agent run's own cost count |
| Parameter proposals, per UTC day | `ANYUSE_DRAFT_PROPOSE_DAILY_USD` (environment) | `0.25` | `spend/draft_params.jsonl` |
| Gap fills, per UTC day | `ANYUSE_DRAFT_FILL_DAILY_USD` (environment) | none | `spend/draft_fill.jsonl` |

- **One chokepoint.** Every request goes through the model adapter. Before it
  is sent, the adapter reserves an input bound from UTF-8 messages, schema and image
  allowances, plus `max_tokens` output tokens, at the configured prices. The
  request must fit the run budget and, beneath the global cap,
  today's booked sum plus that maximum. A request that does not fit is refused
  unsent, with an error that names the setting. Afterwards the actual cost is
  booked as one ledger row: the provider's figure if it reports one, else
  tokens × list price, else the reservation's maximum. Each row carries the
  time, the feature (the model role, or `draft_propose` / `draft_fill` /
  `watch_explain`), provider, model and tokens. A row that cannot be written
  is reported on the result and in the log.
- **Beneath the global cap.** A parameter proposal or a gap fill needs room
  under its own cap and under the global cap. It is booked once in the ledger
  (by the adapter) and once in its own log.
- **Fail closed.** An unreadable or corrupt ledger, a row of today whose cost
  is not a finite number, or an unreadable cap value means no request until it
  is fixed.
- **Only product processes.** The ledger and the global cap apply in processes
  started through the `anyuse` CLI or the MCP server (`python -m anyuse.mcpserver`).
  Tests are not armed: they read no global cap and never touch
  `spend/ledger.jsonl`.
- **Not a purchase cap.** These caps limit MODEL spend. An open task's purchase cap is the task's own ("at most
  30 EUR"): it is read from the task text or values, the page's total is read
  back and compared before paying, a missing one means the payment is asked of the caller, and no setting lifts or
  fills it in.
- **Zero.** A cap of `0` is a cap, never "no cap": the daily and per-run caps then refuse every priced
  request, and an open-ended agent run with `ANYUSE_AGENT_MAX_USD=0` stops before its first model call.
  `0` allows no model calls there; `anyuse agent run --policy scripted` calls no model and still runs.
- **Lifting a cap.** `unlimited` lifts the global or the per-run cap. Raising
  either cap above its default, or lifting it, needs the typed-code
  confirmation; lowering it does not. The literal `inf` is refused.
- **Readout.** `anyuse settings spend` (`--json`) shows the caps in effect and
  today's ledger sum per feature. `anyuse doctor` prints one line with the
  global cap and today's spend. Both are read-only and create no file.

```console
anyuse settings spend
anyuse settings set ANYUSE_SPEND_DAILY_USD 2      # lowering: no confirmation
anyuse settings set ANYUSE_SPEND_DAILY_USD unlimited   # typed code
```

Cost basis of the default (list prices, 2026-11):

- a gap fill is about $0.001–0.0015 and a parameter proposal about $0.001, at
  Cerebras gpt-oss-120b list prices (US$0.25 / 0.69 per million tokens in /
  out);
- a run (an agent task, computer use, a learn run) is capped at $1.00 by default
  (`ANYUSE_RUN_BUDGET_USD`), about 30 Sonnet-class decisions;
- a Sonnet escalation call is about $0.03.

So $5 per day is about 150 Sonnet calls, or thousands of Cerebras calls.

## Security notes

The REA chooser has an explicit local `laya` backend ([Laya](https://huggingface.co/convaiinnovations/laya), `pip install laya`, Apache-2.0 by Convai Innovations; populate with `python tools/confirmer/fetch_laya.py`). It reads these environment
variables independently of the existing confirmer:

| Variable | Meaning |
| --- | --- |
| `ANYUSE_REA_LAYA_DIR` | Required local asset directory containing `wheels/` and `models/<checkpoint>/`. |
| `ANYUSE_REA_LAYA_CHECKPOINT` | `typed` (default), `en`, or `multilingual`. |
| `ANYUSE_REA_LAYA_CONTAINER` | Apple `container` executable; defaults to `/opt/homebrew/bin/container`. |
| `ANYUSE_REA_LAYA_IMAGE` | Already cached Python3.12 Linux arm64 image; defaults to `python:3.12-slim`. |

The REA saved-action chooser is explicit: `ANYUSE_REA_PICKER` accepts `auto`,
`djev`, `openjev`, `laya`, `jev` or `reasoning`. `auto` chooses a configured
`ANYUSE_DJEV_URL` (legacy `DJEV_URL` also works), otherwise [OpenJev](https://github.com/razorback16/openjev). It never
starts a local model or sends a hosted Jev request implicitly (`laya` runs the local Laya container only when explicitly selected; `jev` is optional and not publicly available). `reasoning` uses
the existing agent-role HTTP model configuration with zero retries and no tools.
The chooser CLI's `--fallback reasoning` must be requested separately; selection
does not execute an app operation. See [REA access routes](REA_ACCESS_ROUTES.md).

The backend installs Laya0.3.20 from the local wheelhouse with `--no-index` and
loads local CPU weights in its own container. It requires the existing dedicated
`anyuse-net` network, uses no DNS, publishes no port and mounts the assets read
only. The network is NAT; these settings do not claim an operating-system egress
block. Missing assets, missing cached images, unsupported input or any SDK
truncation produce an abstention. The caller must close the client after use.
Its retained metadata identifies the checkpoint, actual model-file hashes,
library version and worker protocol; each answer binds the exact request hash
and reports whether inference occurred. Choice confidence retains the SDK's
entropy-based meaning; selecting a card does not prove that card or its effect.

- `ANYUSE_DRIVE_ENABLED=on` permits actual desktop input. HIL policy is a
  separate layer; set `ANYUSE_HIL_MODE=destructive` or `all` if holds are
  required.
- `ANYUSE_MCP_PROFILE=full` broadens the MCP surface; `default` and its four-tool
  form `meta` serve the compiled tools.
- `ANYUSE_ROBOTS=user_initiated` or `ignore` applies to every host; a per-host
  override with a reason (`acknowledge-host --ignore-robots`) is the narrower option.
  Under the default `respect`, only a signed-in task you started skips robots.txt,
  and only on the site it signs in to.
- `ANYUSE_API_READONLY_POST=on` does not approve a POST by itself; the exact
  HTTPS endpoint and derived operation must also have a stored approval.
- A solved live challenge serves only the one task that asked
  ([HUMAN_INBOX.md](HUMAN_INBOX.md#human-solves-live)).
