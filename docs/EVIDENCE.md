# Evidence

What AnyUse has measured, in short. Samples are small, so treat every number as
a rough size, not a benchmark score. Measurement write-ups are not part of this
repository.

**How the measurements are run**

- A tool is judged on **held-out values**: values that were not used to build or
  select it.
- Live rounds use **sites the project had never contacted**, and every answer is
  checked against what the page showed.

## Web

| What | Result | Sample |
|---|---|---|
| Repeat run, compiled request vs. agent | median **66.6 ms** vs. **15.8 s**, 0 model calls | 5 workflows |
| Never-seen sites that got a tool | **21 of 30** sites; on 3 unseen values each (63 calls), **46 correct, 3 wrong** (the other 9 sites had no tool and handed their calls back to the agent) | 30 new sites × 3 unseen values |
| All 63 calls on those 21 tools | 46 correct, **3 wrong** (5.4 % of the 56 answers returned), 7 answers the audit could not fully check against the page, and 7 calls refused by the tool's own checks and handed on | same round |
| Saved tools, replayed 2–5 days later | **223 of 229** replays still usable (97 %) | 88 tools from four rounds |
| Deliberately broken tools | **15 of 15** failed explicitly, no data returned | 3 tool types × 5 kinds of breakage |

## Desktop (macOS, in a VM)

| What | Result | Sample |
|---|---|---|
| Recorded workflows proved on unseen inputs | **12 of 15** on the first run; wrong target in 6 of 90 proof runs | 15 workflows, 2 unseen inputs × 3 runs |
| Tasks planned from an app's own scripting dictionary | **17 of 18** compiled with one cheap model call to choose; 9 of 18 without a model | held-out tasks in 5 apps |
| Automatic discovery of app operations | 4 of 3,303 candidates mapped; **4 of 4** proved on unseen inputs | 5 apps |
| Blender | 1 of 4 recorded flows compiles to a script; proved 2 of 2 | 4 flows |

## Single runs

One run each, on this release. They are examples, not rates.

| What | Result |
|---|---|
| crates.io, compiled from 4 recorded searches | proved on 2 of 2 held-out values; a call through the compiled request took 586 ms, 0 model calls |
| crates.io, `anyuse learn` from 2 values | generated 2 more values from the first result page and reached "proved": 32 s, 3 model calls, US$0.05; a later call 533 ms, 0 model calls |
| crates.io, compiled request taken out of service | the browser replay answered in 10.9 s with 0 model calls; AnyUse rebuilt the request from the replay's traffic and proved it again; the next call was served by the compiled request in 284 ms |
| macOS VM, learned by computer use | "open TextEdit and type a text": 22.5 s, 5 model calls; "open Chess and move e2 to e4": 32.6 s, 6 model calls; each replayed 2 of 2 times in 3.2–3.6 s with no model configured |
| macOS VM, a recorded Chess "new game" (a menu item, then a sheet button) | compiled and replayed 3 of 3 times in about 2.7 s, 0 model calls |
| macOS VM, German keyboard layout | Cmd+Z recorded on a US keyboard, replayed in TextEdit: sent as the key that types z on the German layout and undid the typing; the agent's `cmd+z` too; a recorded `cmd+q` was refused |
| Linux, Debian container, Python 3.11 and 3.12 | the web path, the offline quickstart and the MCP server passed |
| MCP tool names, both profiles | every name matches `^[a-zA-Z0-9_-]{1,64}$`; the older dotted names still work as aliases |

The crates.io commands are in [QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md); the
desktop ones are in the README's "Mac apps" section.

## Offline before and after

Each change in this release was measured against the code before it, on the same
stored pages, recordings or fixture sites on `127.0.0.1`, with no network. The scripts
are in [`tests/bench/`](../tests/bench/). These are constructed cases, chosen to show
the failure each change targets, so they say that the change works, not how often
the failure happens on real sites.

| Change | Before | After | Cases |
|---|---|---|---|
| Browser replay checks its answer (`replay_contract_bench.py`) | 52 broken reads served as "no results" | 0; genuinely empty pages still answered as empty | 465 page variants |
| HTML that leaves end tags out (`html5_tree_bench.py`) | 22 of 27 list pages compile | 27 of 27; live-page snapshots unchanged | 68 pages, 57 variants |
| Page readiness instead of a 10 s timer (`readiness_bench.py`, simulated pages) | median 10 s | median 2.7 s; no early or wrong reads | 204 cases per mode |
| Readiness for type-ahead searches and short answers (`readiness_bench.py`) | median 10 s (every type-ahead case at the timer) | median 2.6–2.7 s; 0 wrong reads; the other modes unchanged | 204 cases per mode |
| Healing without a browser (`heal_bench.py`) | 0 of 10 calls answered after the site changed | 10 of 10 in each of 4 cases, about 9 requests, 0 model calls; the 3 that must not heal (a page filled by a script, a page listing other items, the data removed) refused, 0 wrong heals | 7 cases × 10 unseen inputs |
| Healing when the list changed since the last good answer (`heal_bench.py fresh`) | 0 of 8 calls answered in each of 4 cases (newest first, an item gone, a "no results" page, a page only a check model settles) | 8 of 8 in each, exactly the site's list now, after one swap; 0 wrong swaps, also when a check model says yes to every line of a page listing other items | 6 cases × 8 calls |
| A request that fails while the site answers (`heal_bench.py fresh`) | never taken out of service; 0 of 6 calls answered | taken out at the 4th failure and healed, 3 of 6 answered; a site down everywhere is never taken out (7 checks in 5 h, 30 minutes apart or more) | 2 cases |
| Recompiles once the recordings are gone from where they were made (`heal_bench.py fresh`) | 0 recompiles | 1 each, from the kept recordings and from the pages the agent answered on | 2 cases |
| HAR files exported with cookies (`har_cookies_bench.py`) | 0 of 24 compile | 24 of 24; a request that needs its cookies is reported as needing sign-in; 0 cookie values written | 24 HARs, 3 export styles |
| Non-Latin result labels (`unicode_compile_bench.py`) | 0 of 92 compile | 92 of 92 | 10 scripts |
| Matching in every script (`matching_bench.py`) | 9 wrong matches accepted, 13 right ones refused | 0 and 1 without a check model (the one is an unquoted Chinese "no results" line, which needs the model); 0 and 0 with one | 58 cases: positions in unspaced scripts, accents, digits, empty statements |
| Search, then open the result named (`chain_row_bench.py`), recordings that opened a later result | 8 of 20 opened the wrong result | 0; a name with no result answers `not_found` | 25 unseen names |
| Chains recorded with three inputs (`chain_proof_bench.py`) | 0 of 12 compile ("record one more") | 12 of 12, fitted on a result name from the site's own search; their answers match the four-input chains | HAR, attach and learn, 2 layouts |
| Item pages without named fields (`detail_bench.py`) | 0 of 130 proved | 130 of 130; wrong cases proved 0 of 130 in both | 9 page layouts, 5 languages |
| A card's ad, variant or shop link (`detail_bench.py`) | 1–2 wrong items per page from compiled tools; 1 extra result per card in agent answers | 0 and 0; no distinct results merged, no titles lost | 6 card kinds, 2 controls |
| Opened rows that show another listed result's page (`detail_bench.py`) | 3 of 3 accepted | 3 of 3 withheld; the clean table still accepted | 6 visit scenarios |
| Empty answers checked against the site's own empty page (`empty_page_bench.py`) | 8 of 26 empty pages answered (listed languages only); 20 decoy pages (broken reads, error pages, help lines, searches that ignore the input) answered as empty | 26 of 26 in 13 languages; 0 false empty answers | 13 shops × 2 replay steps (HTTP and browser), 8 decoy kinds |
| Signed-in tasks and search providers (`policy_bench.py`) | 0 of 12 signed-in account pages opened; 1 of 7 provider searches | 12 of 12 and 7 of 7; background runs still held, 0 of 9 must-refuse requests sent | 43 request cases |
| Search-provider sites whose page states the key (`provider_bench.py`) | 0 of 3 compile (an Algolia-style GET, a Typesense-style POST, a search that redirects to a landing page) | 3 of 3, each 6 of 6 exact on unrecorded inputs; provider keys written to disk: 28 before, 0 after; 2 searches that compiled before still do | 5 sites |
| Typed searches whose Enter does nothing (`provider_bench.py`) | 1 of 4 reached the results | 4 of 4 | 4 page kinds |
| Learn agent clicks on pages that move (`click_binding_bench.py`, simulated browser) | 12 wrong clicks per 100 steps, 219 of 250 tasks done | 0 wrong clicks, 250 of 250 | 10 page behaviours × 25 seeds |
| Compile checks on pages the word lists never held (`generalize_compile_bench.py`) | 48 of 94 right | 94 of 94; every case the lists got right still right, every decoy still refused | error lines, list or item pages, page furniture, embedded data, link identity, outages, 10 languages |
| Site scope, search boxes and discovery in other languages (`generalize_scope_bench.py`) | 57 of 115 right; 21 of 22 decoys refused | 114 of 115; 22 of 22. The one miss: a sibling host under a country code whose second levels the build does not list now counts as the site's own only when the site names it or redirects to it | robots groups, own hosts, search POSTs, discovery keys, search boxes, 14 languages and country suffixes |
| Offline replays, heals and watch checks choose the same search box as the live replay (`generalize_scope_bench.py`) | 155 of 248 the same; 19 of 27 decoys | 248 of 248; 27 of 27 | current and older saved tools, recorded by name, id or position |
| Verifier, multi-page / result-visiting tools and answer labels beyond the word lists (`generalize_verify_bench.py`) | 20 of 107 right; 57 of 85 decoys refused | 107 of 107; 85 of 85; the 43 list cases right in both; proofs and watch runs ask the check model 0 times | availability, prices and currencies, result counts, empty searches, sorts, filters, pagers, 9 languages |
| Saved web tools on pages the lists never held (`generalize_demo_bench.py`) | 1 of 50 right; 15 of 20 list cases | 46 of 50 without a check model (the 4 misses are empty-page statements in Turkish, Vietnamese, Indonesian and Czech, which only a model reads), 50 of 50 with one; 20 of 20; 115 tools saved by an older version replay byte-identical | result sidebars, title affixes, dates, empty and error pages, product choice, 8 languages |
| Saved-tool calls, older multi-page tools and sibling product hosts (`generalize_demo_bench.py`) | calls 0 of 4, older multi-page runs 5 of 16 as recorded, sibling product hosts 0 of 2, heals 0 of 1 | 4 of 4, 16 of 16, 2 of 2, 1 of 1 | counted check questions, no questions in re-proofs, proof replays and watch runs, older saved pagers, dates and sorts |

## Not there yet

- **Not every site compiles.** In the fresh-site round, 9 of 30 sites never got a tool;
  their requests go to the agent or to you.
- **Fully automatic tool discovery on unseen websites** did not generalise: 7 of 45
  hosts.
- **The desktop agent (computer use)** solved 28 of 45 and 25 of 45 independently
  written tasks in two rounds, below the bar we set in advance. Its successes
  replayed 10 of 10 once compiled.
- **Compiling new desktop tasks from the app map**, without a demonstration, is early:
  2 of 35 held-out tasks compiled and proved without a model, 3 of 35 with one model
  choice.

## Reproduce

The offline path needs no network: [QUICKSTART.md](QUICKSTART.md). The live
measurements need public sites, a container browser and, for the desktop, a macOS VM.
