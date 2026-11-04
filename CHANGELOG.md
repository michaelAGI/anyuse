# Changelog

## 0.4.0 — 2026-11-03

* Consolidated optional extras into six groups (`mcp`, `cdp`, `desktop`, `rea-native`,
  `registry`, `test`) and removed legacy caption-only watcher commands, options, and
  dependencies (`watcher start` records structured steps by default).
* Packaging & wheel completeness: ship `anyuse.watcher`, `tools/confirmer/laya_worker.py`,
  `testdata/discovery`, `execution/guest/interlock_drive_e2e.py`, and `licenses/` inside
  built wheels and sdists.
* Replay & watch safety: live browser replay and `anyuse watch` no longer screen
  benchmark-only competitor domains, and `anyuse watch` records user-blocked hosts under
  `ANYUSE_DATA_DIR/never_contact.txt` without writing into installed package data.
* `execution/orchestrator.py`: guard replay transition outcomes when `state_graph` is
  absent (`replay_failed` returns cleanly instead of raising `AttributeError`).
* `execution/map_review.py`: require an explicit model (`codex:<model>`) when using the
  `codex` CLI reviewer.
* REA native harness: `rea_plan`, `rea_observe`, mixed REA + Accessibility replay, and
  reused Ghidra project cache (`execution/rea/`).

## 0.3.0 — 2026-10-18

First public release.

* Compile a task recorded once in a website into a proved HTTP request or a
  deterministic browser replay; replay it without a model; serve the proved tools
  over MCP (`python -m anyuse.mcpserver`, optional `meta` profile).
* macOS desktop path: recordings through the Accessibility tree, the app's scripting
  dictionary before clicks, compiled desktop tools run by a job agent inside a guest
  VM (`execution/guest/`).
* REA native-app harness (experimental). It refuses to start outside a macOS VM
  unless `anyuse map rea --allow-host` is passed for that invocation.
* `anyuse learn "TASK {q}" --url URL --values a,b`: an agent does the task once in a
  container browser, AnyUse generates the missing held-out values (from the result
  page, else with one model call; `--no-generate` turns this off) and saves a proved
  tool. A page only the browser replay reproduces is saved as a browser replay.
* When a compiled request breaks, the call falls back to the browser replay; AnyUse
  rebuilds the request from that page load (else from the stored recordings), proves
  it again on held-out inputs and swaps it in.
* For a new host, `compile` and `learn` read its `robots.txt`, then ask before the proof
  replays the compiled request against it; `--yes` answers for scripts.
* MCP tool names follow the `[a-zA-Z0-9_-]{1,64}` format Anthropic's and OpenAI's APIs
  accept (`crates_io__crates_search`, `agent_run`); the old dotted names still work.
* Learned desktop runs replay without a model; recorded keyboard shortcuts are sent
  for the keyboard layout in use at replay time.
* `anyuse workflow rename NAME NEW_NAME`.
* Record once: `workflow compile --from-har` takes one HAR plus `--expect VALUE='a|b|c'`
  for the proof's other inputs, typed from what the page showed.
* Over MCP, `agent_run` hands every commit step (a login, a cart, a form sent, a payment,
  a deletion) to the user (`outcome: human_required`);
  `ANYUSE_MCP_AGENT_COMMITS=agent` lets the calling agent approve them.
* `anyuse map rea setup [--prefix DIR]` installs Node, a JDK, Ghidra and REA at
  pinned, checksummed versions into one folder, writes an `env.sh` and runs REA's
  Ghidra check; `anyuse map rea --allow-host setup` installs them on your own Mac.
  On Apple silicon it compiles Ghidra's decompiler from the archive's sources, since
  the archive ships no arm64 binary.
* `anyuse map rea analyze` passes `--query` and `--function` values to REA in
  option form, so a value that starts with a dash reaches REA intact.
* `--function` accepts Objective-C names (`-[Class method:]`, mapped to Ghidra's
  `Class::method:`) as well as `Class::method:` and addresses.
* `anyuse map rea analyze --search-kind procedures` searches function names
  instead of strings.
* `anyuse watch` (experimental): change monitoring with compiled tools.
* Defaults: robots.txt respected, request pacing, no bot-wall bypass, read-only web
  requests, a daily model-spend cap. A signed-in task you start is not held to
  robots.txt on its own site (each such request is recorded on the run); background
  work is. Government hosts are refused only with `ANYUSE_REFUSE_GOV_HOSTS=on`; the
  hosts you list as never-contact are refused in every mode.
* The core installs with `httpx` and `click`; everything else is an extra
  (`mcp`, `cdp`, `desktop`, `rea-native`, `registry`, `test`).
* A compiled web tool whose site changed heals itself on the next call when its result
  page is rendered by the server, without a browser. AnyUse reads that page with plain
  requests (host acknowledged, robots.txt respected) and checks the answer against the
  page as it is now, so a list that changed since the last good answer (newest first,
  live prices) still heals; a configured check model is asked only when the page alone
  cannot say which list is the answer. It re-derives only the broken step (else
  recompiles from the pages), proves the result on two held-out inputs and swaps it
  in; the call result says so under `healed`. `ANYUSE_AUTO_HEAL=off` turns it off. The
  offline quickstart shows it.
* The recordings a tool was compiled from (a recording folder, a HAR, attach sessions,
  a `learn` run) and the pages the agent answered on are kept in the tool's folder,
  without credential values, and the automatic recompile rebuilds from them when no
  browser replay is at hand. `ANYUSE_KEEP_EVIDENCE=off` turns this off.
* The browser replay checks its answer like the compiled request does: an empty answer
  counts only when the page says on screen that nothing was found for this query, and
  one or two results only when each names the input. A broken read is reported as a
  failure instead of "no results", and is never used to rebuild the request.
* The browser replay reads the page once the search's own requests for the input have
  answered and the rows stopped changing, instead of after a fixed 10 s; it never waits
  longer than before. Type-ahead searches, short answers and empty pages are read when
  they appear. `ANYUSE_TIER2_READINESS=off` saves new tools on the timer.
* Server HTML that leaves end tags out (`<li>a<li>b`, unclosed table cells and
  paragraphs) is parsed as a browser parses it, so such result lists compile. Tools
  compiled earlier keep the page structure they were proved on.
* Sites whose results are not in Latin script (Japanese, Chinese, Korean, Russian,
  Greek, Arabic, Hebrew, Thai, Hindi) compile and pass the proof; labels also match
  across Unicode normal forms, full-width letters, capitals written without accents
  (ΑΘΗΝΑ, MUNCHEN), digits of other scripts and Arabic and Hebrew vowel marks. In
  Chinese, Japanese, Korean and Thai a one- or two-result answer counts only when the
  input stands on its own or starts a word (東京 in 東京駅, not 京都 in 東京都庁), unless
  the site's own result lists matched that way or a configured check model reads the
  input as a word there.
* A HAR exported with cookies (Firefox, Safari, Chrome with sensitive data) compiles:
  the proof replays the request without them, and one that needs them is reported as
  needing sign-in. Cookie values are never kept.
* Search-then-open chains compile from real recordings: a recording folder, HARs,
  attach sessions or `learn` values. The chain opens the result named what you ask
  for, not always the first one, and answers `not_found` when no result has that name.
  With three inputs, AnyUse fits the chain on another result's name from the site's
  own search (`ANYUSE_CHAIN_FIT`); that input never proves the tool. A chain is also
  rebuilt from the browser replay's traffic.
* A site that does not answer (server errors, timeouts, refused connections, rate
  limits) no longer takes a compiled tool out of service: the call says when AnyUse
  sends the request again, and a `Retry-After` is respected. A tool out of service is
  checked again by itself after a back-off and put back when its answer agrees with
  the site's page or browser replay as it is now. A request that keeps failing while
  the site's own page answers is taken out after 4 failures over 30 minutes and healed
  or recompiled.
* `anyuse watch` reports a site that did not answer as such, not as a change, and
  keeps the last good snapshot.
* An empty answer is checked against the site itself, in any language: when a tool is
  proved, AnyUse searches two made-up inputs and learns the page the site shows when it
  finds nothing (`ANYUSE_EMPTY_PROBE`). A page that is not that page, an error page or
  a help line such as "If no results for X, contact us" is not read as empty. `learn`
  draws test values from non-Latin result pages.
* Optional small-model checks (`ANYUSE_MODEL_CHECKS`): where the site's own evidence
  leaves a check open, AnyUse can ask the small model you set up (an OpenJev server,
  the local Laya model, Jev or your judge model) one yes/no question per candidate, and
  takes a yes only for a line it then finds on the page. Nothing is asked unless one of
  them is configured, never about the answers a proof replays, and never in a watch run.
* A site whose page sends the search to a third-party search service compiles: the
  host the page itself sends the typed value to counts as its search provider. When
  the provider needs a public key that the site's page states (Algolia or Typesense
  style), the HTTP tool reads the key, app id and index from that page on every call
  and never stores them, so a rotated key needs no recompile (`ANYUSE_PAGE_KEYS=off`
  turns this off). Its search POST runs once you acknowledged the provider's host.
* A search that redirects to a brand or collection page compiles: the tool follows the
  site's redirect and reads the values the data call needs from the landing page. A
  site that sends every query to its home page is refused as `search_to_home`. A typed
  search whose Enter does nothing tries the search's button, the box's own GET form and
  the page's search URL.
* A HAR or a typed `--expect` flow that ends on an item's page compiles and proves: each
  value is named from the recorded response, else from the label the page puts beside it,
  else by a configured check model's pick, and the tool's outputs are named after the
  page's labels. An answer that lists a
  card's ad, variant or shop link as its own result is read as that card's one result.
  A search whose top results are opened checks each row on its own item page and
  withholds a row that does not verify.
* `anyuse learn`: the agent's clicks and typing land on the element it chose even when
  the page moved after it looked; when that element is gone or ambiguous, nothing is
  clicked and the agent looks again.
* `anyuse skills export DIR` writes each proved tool as an Agent Skills folder
  (`DIR/anyuse-<tool>/SKILL.md`) for Claude Code, Codex, OpenClaw and Hermes Agent;
  `--profile meta` names `call_operation` for a server run with the meta profile.
* Setup for OpenClaw and Hermes Agent next to Claude Code and Codex
  (docs/INSTALL_FOR_AGENTS.md).
* First-hour fixes: `workflow delete`, `rename` and `export` find compiled web tools,
  and `watch add` takes them; `acknowledge-host` takes a URL; `--json` prints JSON on
  every `workflow play` failure; a model set with `anyuse settings set` is used
  everywhere; parameter typos, outages and pre-send refusals get their own messages,
  over MCP too; `credential use` obeys `ANYUSE_DRIVE_ENABLED`; `--version` works from a
  clone; an unknown `ANYUSE_MCP_PROFILE` is reported.
* Checks on a page read the site's own evidence first: its structure, schema.org and
  ARIA markup, what changes with the input, what the page does when you type. Then a
  configured check model decides, and word lists come last. Search boxes, result blocks,
  dates, prices, currencies, availability, pagers, sort options and empty pages in
  languages and markup the lists never held now work. Web tools saved from this release
  on are version 15 and read pages this way; tools saved earlier replay as before.
* A saved web tool's call counts its check-model questions in `model_calls` and
  `cost_usd`. Proof replays, re-proofs, canaries and watch runs ask none.
* robots.txt groups are matched against AnyUse's product token as RFC 9309 specifies,
  so a group for another crawler whose name is only part of AnyUse's no longer applies.
  Under a two-letter country code a generic second level (`com.pk`, `org.ng`) is a
  public suffix, so two shops there are two sites; a pinned Public Suffix List decides
  when one is installed (docs/TRAFFIC_POLICY.md).
