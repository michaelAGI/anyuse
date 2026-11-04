# Known issues

What does not work yet, or works only in part. "Next step" below means the next
step of the [cascade](COMPILE.md): compiled request, then browser replay, then
agent, then you.

## Web

- **A tool can only be rebuilt automatically from evidence it still has.** AnyUse
  rebuilds a broken request from the browser replay's network traffic, from the
  pages of the last calls the agent answered (once it has kept 4 different inputs,
  each at most a day old), or from the recordings the tool was compiled from, which
  every compile now keeps in the tool's folder. Tools compiled by an older version
  from a HAR file, a `learn` run or an attach session may have no recordings left.
  If only the agent answers for such a tool, it waits until 4 calls with different
  inputs are kept, and an agent whose page does not show its answer keeps nothing.
  Otherwise record the search again and run `anyuse workflow compile` (or `anyuse
  learn`). An older tool also does not know which inputs its replay was built from,
  so it heals through the browser replay only on calls whose input was not used in
  its proof; calls with a proof input wait without using up an attempt. A signed-in
  tool, and a tool that a registered skill depends on, are never rebuilt
  automatically.
- **Healing without a browser needs a result page that the server renders.** If the
  page's results are filled in by a script, the tool heals only through the browser
  replay. If the list of results changed since the tool's last good answer, it heals
  only when the tool's own response holds the page's entries, or when a made-up
  search empties the list (or the site says it found nothing) and the list still
  overlaps the last good answer. Otherwise only a configured check model can say
  which lines are the results. A changed GraphQL query id (a "persisted query") is
  not healed without a browser.
- **A request that disappears behind server errors is taken out of service only
  after 30 minutes.** A 5xx error, a timeout or a refused connection never counts
  against the request on its own. Only after 4 such failures in a row, spread over
  at least 30 minutes, does AnyUse check the site's own page (once, and at most every
  30 minutes). If that page answers, or the browser replay or the agent answered the
  call, the request is treated as gone and is healed or recompiled. Until then each
  call says that the site did not answer. A site whose page works while its API is
  down for more than 30 minutes also has its request taken out; the request comes
  back once its API answers again.
- **A list tool that was taken out of service, with no server-rendered result page
  and no browser replay, needs a model to come back after its list changed.** To
  come back, it compares its answer for the last good input with what the browser
  replay or the result page shows now. A tool with neither, whose list changed since
  its last good answer, comes back only when a configured check model says the
  answer's entries are results of the search (and the request gives a different
  list for a different input). Otherwise it stays out of service until a heal or a
  recompile replaces the request.
- **`learn` can only fill in missing example values where it finds them.** Given
  fewer than four values, it takes other results from the first result page, or
  else asks the configured model once. When neither gives a value whose page shows
  a result list of its own, the tool ends at `checked` (three values) or is not
  saved, and `learn` says how many values it could find.
- **On a site that lists results for anything** (a fuzzy search), an empty answer
  cannot be compared with the site's own "no results" page, because there is none.
  It counts only when the configured check model, or a "no results" sentence in one
  of the listed languages, says the search found nothing
  ([COMPILE.md](COMPILE.md#when-an-empty-result-counts)).
- **On some sites an empty answer costs one or two extra searches.** When the
  site's "no results" page does not repeat your input, AnyUse also searches for an
  input known to have results, and a second one if the first finds nothing. When
  both find nothing, the empty answer counts as a failure, even if the site really
  no longer has results for them. A tool proved by an older version learns its "no
  results" page at its first empty answer: three searches once, and in the browser
  replay three page loads (up to about 30 s). The browser replay of a newly proved
  tool learns its own "no results" page the same way.
- **A "no results" page that changes with the input can look like a failure.** When
  the site's "no results" page for an input shows lines that a made-up search does
  not (products recommended for that input, or a "did you mean" that is not a link
  to the site's own search), the call reads them as unexpected lines and moves on to
  the next step. A suggestion that links to the site's own search for another input
  counts as part of the "no results" page
  ([COMPILE.md](COMPILE.md#when-an-empty-result-counts)).
- **A saved web tool reads the "no results" page it learned strictly.** The page
  counts only when it repeats your input and shows no line the learned page lacked.
  So a page with changing offers or recommendations, or one that does not repeat
  your input, still needs an explicit "no results" sentence: a tool saved by the
  current version reads one that names your input in the languages it saw while
  compiling; an older tool needs one in a listed language
  ([MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md#the-call)).
- **A tool that only the browser replay could prove needs a browser for every
  call.** It has no compiled request. Without `ANYUSE_CDP_ENDPOINT` or
  `ANYUSE_LEARN_ENDPOINT` it is listed, but it does not run.
- **The browser replay waits the full 10 s on some pages.** If a page loads its list
  in several batches, and an input's list comes in fewer batches than the list the
  tool was compiled from, the replay reads it at the end of the 10 s window. A page
  that sends the input in another form (shortened, looked up to an id, in a header)
  keeps the older waiting rule: wait for as many requests as the recording made,
  with none still open, so a long-polling request on the same path holds it to 10 s.
  A tool compiled by an older version keeps the older waiting rule for every input
  until it is compiled again.
- **A "search, then open a result" tool needs a fourth recorded input when the
  site's search does not find another result by its name.** Such a tool is fitted on
  a second input, which then cannot also be used to prove it. With only three HAR
  files, sessions or values (or a recording folder without `NAME.fit1.json`), AnyUse
  takes another result's name from the first input's search results and sends the
  tool's own search and open requests for it, over plain HTTP or through the browser
  replay when one is configured. That input never proves the tool. When the search
  lists nothing for those names, or the result page does not show what the open
  request returned, the compile names the values it tried and asks for one more
  recording: `NAME.fit1.json` in a folder, or a second HAR file, session or value.
  Typed `--expect` cases still cannot prove this kind of tool.
- **A "search, then open" tool whose browser replay cannot capture network traffic
  is rebuilt only from its stored recordings.** The automatic recompile captures a
  second page to fit the tool on (an input the replay was built from, or else
  another result's name) and the proof pages with their traffic. Without traffic
  capture it uses the stored recordings, and finds a fitting input over plain HTTP
  when they were kept without one.
- **In languages written without spaces, an input inside a longer word counts only
  with evidence.** In Chinese, Japanese, Korean or Thai, a one- or two-result answer
  whose name holds the input at the end of or inside a longer word (大学 in 東京大学),
  and a "no results" line that writes the input without quotes
  (没有找到与南京相关的结果), count only when the site's own result lists did the
  same on the inputs the tool was built from, or when the check model reads the
  input as a word there ([COMPILE.md](COMPILE.md#the-fresh-input-proof)). Without a
  check model such an answer is refused and the call moves on to the next step.
  Tools compiled by an older version still accept the input anywhere in a word, so
  東京都庁 counts for 京都 there; compile them again to get the stricter rule.
- **Accents on letters are ignored, even where a language treats the accented
  letter as a letter of its own.** ñ matches n, й matches и and ё matches е, so
  "año" and "ano" count as the same word. The proof on unseen inputs still has to
  pass.
- **On an item's page, only values the recording names are proved.** An item's page
  is proved field by field. A recording without named fields (`ui.fields`, missing
  in a HAR's `--har-expect` or a typed `--expect`) has each value named from its own
  evidence: the JSON key that holds it in the recorded response, else the label the
  page puts next to it, else a configured check model's choice of the line that
  shows it ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)). A
  typed `--expect` case is matched by the order on the page, so values typed in
  another order fail the proof. A value that neither the page nor the response
  names, such as one written differently (`1,307` for `1307`), is not proved.
- **Not every site compiles.** The usual reasons: a proof value fails or is
  missing, the demonstration finds no results for a value, or the results sit in a
  part of the page that AnyUse skips (for example a search box in the footer). A
  site that uses an external search provider compiles to an HTTP tool only when the
  site's own page states the provider's key by name. A key that only the provider's
  own script or a server call supplies keeps the search on the browser replay, as
  does a provider that is out of scope (one on the ad/tracker list, or one reached
  without the typed value). A key inside a script whose file name changes with every
  release of the site breaks the tool at the site's next release, until it is
  compiled again. A provider search POST that does not have the read-only shape the
  site's page sends needs `ANYUSE_API_READONLY_POST`. A POST whose typed value sits
  in a query string inside the JSON body (Algolia's own `queries` call:
  `{"requests":[{"params":"query=..."}]}`) is not compiled and stays on the browser
  replay.
- **A search overlay without a data request, and a site with no search of its own,
  are not compiled.** When pressing Enter in a search box does nothing, AnyUse tries
  the search's own button (or the one control next to the box; among several, a
  control named like a search), then the box's own GET form, then the search URL the
  page shows. An overlay that draws its results without a data request that AnyUse
  can compile stays a browser tool that types the search. A search that lands back
  on the unchanged start page stops the demonstration with `no_site_search`.
- **A site that sends every search to its home page has no search to compile**
  (`search_to_home`). A search that redirects to a brand or collection page does
  compile: the HTTP tool sends the search URL, follows the site's own redirect and
  reads the values its data request sends from the landing page, provided a second
  recorded input shows the landing page stating another value. A browser tool saved
  by a recent version replays the search URL. A landing page that does not state
  those values (because they come from a script or a server call) is not compiled.
- **A tool learned from a demonstration with only one result** can miss the layout
  that other values produce.
- **One result card counts as one result only where the page repeats its cards.**
  An answer that lists a card's ad, variant or shop link next to its title is read
  as that card's single result, and a compiled HTML tool whose proof saw one item
  per card keeps one item per card. On a page without repeated cards (a single
  result, or cards with different layouts) the answer checker decides, as before.
- **A row of a "visit the results" answer that cannot be settled without a model is
  left out.** Each opened row is checked on its own item page: its name against its
  card on the list page, and its price and availability against a second reading
  of the page. A row whose page shows no second reading (no visible price or
  availability line), or whose name the page writes in other words, needs a
  configured checker model, which only a tool call over MCP asks. Without one, and
  in every watch run, the row is left out and the table is incomplete. A row whose
  link opens a page that names the result with more words (a set or a bundle of it)
  is accepted, unless that page is another result of the same list.
- **`robots.txt` still applies to tasks that do not sign in.** A task you start that
  logs in with your `${secret:NAME}` credentials, or uses a session you imported
  with `login=replay`, opens the site's own pages whatever its `robots.txt` says
  ([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md#robotstxt-rfc-9309)). A task without either,
  and every watch run, schedule and recompile, stops where the site disallows
  `/cart` or `/checkout`. A compiled request on a disallowed path is never made the
  preferred step.
- **A target that changes at payment time** is checked only in offline replays.

## Desktop

- **Electron, Qt and Java apps** are not part of the measured set of apps where
  AnyUse checks that it clicks the right element; AppKit and SwiftUI apps are.
- **Reading the Notes database** needs Full Disk Access for the runner in the VM.
  The shipped checks read Notes through the Accessibility tree instead.
- **Storing secrets in the Keychain** is covered by tests with a test-only file,
  not against a real login keychain.
- **Option shortcuts recorded by an older version, before shortcuts stored the
  character they typed**, and shortcuts recorded with no character at all, replay
  by key position. On another keyboard layout they can press a different key. They
  are still refused where that key would quit or close something.
- **When the keyboard layout cannot be read**, shortcuts use the US key positions.
  A Command or Control shortcut on a key that types Q or W on French AZERTY or
  Dvorak is then not sent.

## MCP

- **The server needs `mcp` 1.x.** The 2.x SDK removed an API it uses;
  `pyproject.toml` pins `mcp>=1.0,<2`. An unpinned install fails at start with
  `AttributeError`.

## Apps with no API (REA, experimental)

- **Coverage is small:** a handful of apps, and a recipe is valid only for the app
  build it was found on.
- **REA, Ghidra and Frida are not bundled.** `anyuse map rea setup` downloads REA,
  Ghidra, a JDK and Node at pinned versions; Frida you install yourself
  ([REA_QUICKSTART.md](REA_QUICKSTART.md)).
- **The setup is pinned for macOS on Apple silicon only.** Elsewhere, install REA
  and Ghidra yourself and pass the REA executable with `--rea`.
- **Each new Ghidra query still imports the app's binary**, which takes about 35 s.
  With the supported pinned tools, AnyUse reuses finished static analysis results
  and fixed code observations from `rea/ghidra_cache` in its data directory. Each
  cache entry is tied to the binary, the request and the tool versions; the cache
  holds at most 64 entries and 128 MiB. Ghidra projects belong to REA and are not
  reused across queries. A cache hit counts as investigation evidence only; it never
  counts as proof that a replay works.
- **Outside a VM**, REA runs only with `anyuse map rea --allow-host`, for that one
  command.

## Small decision models

- **AnyUse starts Laya only in Apple's `container`** (macOS 26 on Apple silicon).
  `tools/confirmer/fetch_laya.py` downloads the model files and the container's
  Python packages. The container itself also needs the `python:3.12-slim` image
  (`container image pull python:3.12-slim`) and a container network named
  `laya-offline` for the checks and the confirmer (`anyuse-net` for the REA chooser);
  AnyUse does not create either. On other systems, run Laya behind an
  [OpenJev](https://github.com/razorback16/openjev) server (model `laya-1.0`) and
  point `ANYUSE_OPENJEV_URL` at it ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)).
- **The `laya` triage ranker is not part of this release.** `ANYUSE_TRIAGE_RANKER=laya`
  refuses with a clear error; use `jev`, `llm` or `none`.

## Packaging

- **The repository root is the `anyuse` package.** Use `pip install -e .`; running
  from a clone without installing needs the clone's parent folder on `PYTHONPATH`.
- **The desktop extras need macOS.** The web path runs on Linux and macOS.
