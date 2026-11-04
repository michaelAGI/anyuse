# How compile works

`anyuse workflow compile` turns a recorded web workflow into a request AnyUse can
replay without a browser and without a model. It keeps the result only if it
reproduces what the page showed for values that were never in the recording.

## What compile does

```
$ anyuse workflow compile crates_search --recording recordings/
Workflow: crates_search   (recording: crates_search.rec.json)
Extraction: ok — winning request explains the screen
  GET https://crates.io/api/v1/crates?page=1&per_page=10&sort=relevance&q={q}
  returns: a list (items from crates[*].id), at least 1 item(s) per call
Fresh-input proof: PASSED (2/2 cases) — passed on 2 fresh value(s) not present
  in the recording (coverage >= 0.8, UI order preserved)
  [ok  ] 'regex': coverage=1.00 order_ok=True ui=10 api=10
  [ok  ] 'tokio': coverage=1.00 order_ok=True ui=10 api=10
What was checked: for each of 2 values it was not built from, at least 80% of the
  first 10 labels the page showed came back in the same order. It does not promise
  every input behaves the same way.
✓ Persisted as the preferred implementation: …/compiled_api.json
```

1. **Read the recording**: what the screen showed, plus the network traffic it produced.
2. **Find the request that explains the screen**: the response whose content covers the
   visible result list. The typed value becomes a parameter (`{q}`); volatile values
   (cache-busters, timestamps, tracking keys) are dropped.
3. **Prove it**: replay the template with values that were not in the recording and
   compare the result with what the page showed for them.
4. **Persist** it only if the proof passes. Replays then prefer the compiled request.
   When it fails, the call moves down the cascade below: the browser replay, then an
   agent, then you. When neither the browser replay nor an agent answers and the
   result page is rendered by the server, the call first heals the tool from that page
   ([Healing without a browser](#healing-without-a-browser)).

## The cascade

Every workflow lands on the cheapest step that holds:

| step | on every repeat | model calls | needs |
|---|---|---|---|
| 1 — a proved compiled request | one HTTP request (or a short chain), typically tens to hundreds of ms | 0 | a request that explains the screen |
| 2 — deterministic browser replay (`cdp`) | the recorded navigation with new inputs | 0 | the input is in the URL and the page still renders |
| 3 — an agent | a model drives the page | counted | nothing; works on unrecorded tasks too |
| 4 — a person | the call returns the reason for you to act on | — | the site refused, or nothing else worked |

Step 2 is slower than loading the page yourself; its value is determinism, a
postcondition and zero model calls, not speed.

### Request kinds, in order of preference

When more than one kind explains the same screen, the compiler prefers, in this order:

> **JSON API > embedded JSON > HTML selector**

- **JSON API**: a call the page makes. It does not break when the page is redesigned.
- **Embedded JSON**: the page's own data model shipped in the document. Read from
  `<script type="application/json">` (including `__NEXT_DATA__`),
  `<script type="application/ld+json">`, and any JSON object or array a page's script
  assigns to a name (`window.APP_STATE = {...}`, `var data = [...]`), also through
  `JSON.parse("...")` of one string literal. The payload that holds the visible list
  is chosen by its values and proved on fresh inputs; the name is only the address
  that finds it again. The assignment parser executes nothing: it balances brackets
  and hands the slice to `json.loads`, and a `JSON.parse` string is decoded by its
  escapes alone. A JavaScript literal that is not JSON is rejected, not interpreted.
- **HTML selector**: one GET, then a small CSS-subset path into the server-rendered
  document (`div.results li.row a.title`), derived from where the visible values sit.
  It is bound to markup, so a redesign breaks it — visibly (see *Failure handling*).

The order is about durability, not about fit on one recorded page.

### Chains, pages and path parameters

- **Chains.** A flow that needs dependent calls (search → open the first result →
  read it) compiles to a `RequestPlan`. A derived binding is only inferred when the
  same producer path and consumer site recur over at least two fitting inputs and at
  least one produced value differs from its input; otherwise the compiler reports why
  it cannot tell direct input from first-result binding. A recording directory names
  its fitting input (`NAME.fit1.json`). The other sources fit a chain on their second
  input, which then is not a proof case: the second HAR, the second attach session
  (the selection session) and the second `learn` value, when there are four inputs:
  one to compile from, one to fit on, two to prove on. When the second input did not
  open the result, the compile says to record that one again.
- **A fitting input from the site.** With three inputs (or a folder without
  `NAME.fit1.json`) no recorded input is free to fit on, so AnyUse makes one
  (`execution/chain_fit.py`). It takes another result's name from the search response
  in the first recording, sends the chain's own search and open requests for it, and
  reads that result's page with a plain GET when the page is server-rendered (else the
  open request's answer stands for the page). With a browser replay configured whose
  page carries the input, the browser captures that page and its traffic instead. The
  input fits only when its traffic shows the same search and open requests as the
  first recording, and a result page that does not show what the open request answered,
  or does not change with the opened result, fits nothing. Which field of a search
  result is its name comes from the site first: the field whose value is what you
  typed, else the one field of the opened result that the recording shows. Only when
  neither decides is a configured small model asked to choose among the result's own
  fields ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)), during
  a compile, `learn`, `attach` or a HAR import, never in the automatic recompile;
  without one, the first field whose values all read as names is taken. At most 3
  values, each with at most 3 plain-HTTP requests, all through the proof's own checks
  (origin policy, robots.txt, the host question). That input is never a proof case: every
  recorded input after the first still proves the chain, so three HARs reach `proved`
  and three attach sessions or `learn` values reach `checked`. The value is kept on the
  record (`chain_fit`: the value, where its field came from, `http` or `browser`, and
  whether the page or the open request's answer stood for the page), outside the proof,
  and a recompile tries it first. When no value fits, the compile names each value it
  tried and why, and asks for a fourth input. `ANYUSE_CHAIN_FIT=off` turns this off. A
  signed-in (`--login-replay`) capture never generates one. A flow that is not a chain
  sets no input aside: every HAR after the first is a proof case, and attach and
  `learn` keep their selection input.
- **A key the site's own page states** (`ANYUSE_PAGE_KEYS`, on). A search provider's
  call carries a public key (`x-algolia-api-key`, `x-typesense-api-key`, `apiKey`). When
  the recording shows the site's own page stating that exact value by name earlier
  (`"searchKey":"..."`, `data-search-key="..."`, `?apiKey=...&`), the compile builds a
  two-step plan: GET that page and read the value by its name and punctuation, then
  send the provider's request as the page did. The provider's app id and index, when
  the same page states them next to the key, are read the same way, with the recorded
  value as the fallback; the key has none. The plan stores where the page states the
  values, never the key, so its proof digest pins the locator and a rotated key needs
  no recompile. Results, traces and records show `<page value>` for the key. The proof
  replays both steps for every validation input. A value the page states twice with
  different values, or no longer states, stops the call at the page step.
- **A search that redirects to a landing page.** When the search URL redirects (same
  site) to a brand or collection page and the data call that explains the screen sends
  a value that page states by name (`"collectionId":"col_8812"`), the plan sends the
  search URL with the typed value, follows the redirect, reads the value, and sends the
  data call. A second recorded input must show its landing page stating another value
  through the same name, which its own data call sends; otherwise the compile says so
  (`landing_uncorroborated`) and fits on a second input like a chain. A search that
  redirects to the site's home page is refused as `search_to_home`. A server-rendered
  landing page compiles to an HTML selector on the search URL.
- **Pagination** is derived from what the first response already carries: a page or
  offset parameter when the response states a total, page count or page number; a
  cursor when it carries a continuation (`next_cursor`, `nextPageToken`, a same-origin
  `next` URL). A guessed `page` key, an offset with no known page size, or a
  cross-origin `next` link is refused. There is no loop over pages.
  `workflow compile --paginate` reports how to ask for page 2 as a candidate: it is
  not proved and not saved with the tool.
- **Path-segment parameters.** A value that spans several path segments
  (`ring/ring-core`) or is a delimited part of one (`rsync` in `rsync.1.en`) is bound
  only when every recording of a different value agrees on the same constant
  remainder. A wrong split fails the proof.

### Read-only POST (opt-in)

Some search backends answer a POST. Such a request compiles only with the explicit
`post` lever (`ANYUSE_COMPILE_LEVERS`) and runs only with
`ANYUSE_API_READONLY_POST=on` plus a stored approval for the exact HTTPS endpoint and
operation (for example `query:Search`). It is accepted only when all of these hold,
and the refusal names the one that failed:

- 2xx, https, a JSON or form body under 8 KiB, issued once during the recording;
- for GraphQL, a `query` that mentions no `mutation` or `subscription`;
- nothing carries a credential: no signed or keyed parameter, no `Cookie`,
  `Authorization` or `X-Api-Key` header, no credential-like body key;
- the body carries the typed value and the response explains the visible list.

The read-only check is re-derived from the body on every execution, so an edited
`compiled_api.json` cannot turn a mutation into a "read".

One exception needs neither the lever nor `ANYUSE_API_READONLY_POST`: the search POST a
plan that reads a public key from the site's page sends to the site's search provider, a host outside the site's own
domain (a Typesense `multi_search`, for example), when the site's own page sent it in
the read shape the observed-provider rule of `ANYUSE_THIRD_PARTY` accepts: it carries
the typed value, and no telemetry or state-change path, no state-change body field and
no GraphQL mutation.
Its key is the one credential exempted, and only in its own field with the value the
page stated. It runs once the provider's host is acknowledged (the proof asks for it as
for every new host), and the shape is checked again on every send. A body whose typed
value sits in a query string inside the JSON (Algolia's own `queries` call) is not
compiled; such a search stays on the browser replay.

## The fresh-input proof

- At least **two values that were not in the recording**, each compared on at least
  **three visible values**; fewer is "insufficient evidence", not a pass.
- At least **80 %** of the visible labels must come back, **in the page's order** for
  lists. Field-shaped results compare `field name → value`, so swapped fields fail.
  A value the response returns as a number or a boolean is compared by value: `1307`
  matches the label `1307`.
- Labels and returned values are compared after one normalisation: case folded,
  punctuation dropped, Unicode NFKC. Letters of every script count, so a Japanese,
  Russian, Greek or Arabic result list is ground truth like a Latin one. A composed
  and a decomposed accent, and full-width and half-width forms, compare equal. The
  accents of letters that have case are ignored (ΑΘΗΝΑ = Αθήνα, MÜNCHEN = München,
  Wrocław = Wroclaw), and so are the optional vowel marks of Arabic and Hebrew;
  other marks count (Thai, Devanagari, the kana voicing marks), and so do letters of
  their own such as Kazakh қ. Digits of every script compare by value (٣ = 3).
- In Chinese, Japanese and Thai, which are written without spaces between words, and
  in Korean, whose particles attach to the word, a result names the query when the
  query stands on its own ("東京タワー" names "東京": the script changes) or starts a
  longer word ("東京駅"). At the end of or inside a longer word ("京都" in "東京都庁")
  it names the query only when the site's own result lists did that on the fitting
  inputs, which the tool then records (`query_positions` in its result contract), or
  when the check model reads it as a word there on a call you make
  ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)). Validation
  inputs never set it. Tools compiled before this rule accept the query anywhere, as
  they did. In other scripts the query must be whole words of the result. A label or
  search value of two ideographs ("大阪") is long enough to match; one of two kana is
  not.
- Discovery draws proof values from the site's own pages: words its item names
  share, its frequent words, minus the words of its title and host name. A word is a
  run of letters in any script (Hebrew and Arabic words may be one letter shorter).
  Chinese, Japanese, Korean and Thai text is not cut into guessed words: whole link
  labels count, and so do the starts that two or more labels share ("東京" of
  "東京タワー" and "東京駅").
- Fitting, selection and validation values are kept apart: "proved" means the
  validation values were not used to build or choose the template. A pass without
  that separation is reported as `checked (selection not held out)`.
- **Page chrome** — labels that appear for every different query (navigation,
  footers, banners) — is removed from both the ground truth and the proof. If that
  removes everything, the compiler says the selector caught only chrome or the site
  ignored the parameter.

"Proved" means the checks passed on the tested values. It does not promise
correctness for every input or for a future version of the site.

A model can optionally propose a request the page never made (for example a public
registry endpoint). It sees only a redacted summary (hosts, paths, query keys,
response shapes; no values, headers or cookies), and its proposal is a candidate that
must pass the same proof.

## New hosts and robots.txt

For a host AnyUse has not used before, `compile` and `learn` read its `robots.txt`,
then ask before the proof replays the compiled request against it; `--yes` answers
for scripts. A yes is recorded in `api_hosts.json`, the file
`anyuse workflow acknowledge-host <host>` writes; under `--dry-run` or `--no-persist`
it holds for that command only. Answer no and the proof is not run and nothing is
saved. MCP tools never confirm a host.

A path `robots.txt` disallows for `anyuse-compiled-workflow` is not compiled into the
preferred step and not replayed (`ANYUSE_ROBOTS=respect`, the default); a missing
`robots.txt` does not block. For your own site,
`anyuse workflow acknowledge-host <host> --ignore-robots --reason "…"` lifts this for
that one host, and the reason is shown in every proof and MCP listing. The global
modes are in [TRAFFIC_POLICY.md](TRAFFIC_POLICY.md).

## When a site asks for a person

`execution/blocked.py` recognises when a site asks for a person: reCAPTCHA, hCaptcha
and Turnstile signatures, challenge pages and headers (`cf-mitigated`), HTTP 429,
a bare 403, login walls, cookie-consent walls, and 2xx responses that render an
error page.

The step then returns `requires_human=True` with a message for a person, the pause
goes through the approval gate, and the host is put on "do not auto-retry" (the
site's `Retry-After`, else 6 h) so the next replay is refused before any request is
sent. A 5xx is the site being broken and does not set that flag. Over MCP the result
says `action_required: "human"`. `clear_blocked(host)` lifts it once you have
solved the check yourself.

A host that refuses AnyUse's HTTP client (403/429) but served the browser the same
page is marked `browser_only`: the compiled request is skipped there, and the browser
replay still runs.

## Browser replay (`cdp`)

The browser replay needs `ANYUSE_CDP_ENDPOINT` to name a DevTools endpoint; there is
no default and no auto-discovery. It opens a URL and reads the DOM; it does not
click, type, scroll or upload.

### When an empty result counts

An empty result is a **failure** unless the site itself says it found nothing for this
input. The compiled request and the browser replay check it the same way, in three
steps, and the result says which step decided (`result_contract.empty_by`). Such a call
goes on to the agent; without one it fails and says why.

1. **The site's own empty page** (`site_empty_page`). When a tool is proved, AnyUse
   searches two made-up inputs and keeps what the site shows when it finds nothing:
   the lines both pages show and no result page shows ("Không tìm thấy sản phẩm nào cho
   “zzqx”"), with the input and the numbers masked. This works in any language, and
   the proof pins it with the template. A tool proved before learns it at its first
   empty answer: one search for an input known to have results, then the two made-up
   ones; it is kept on the tool's record. An empty answer counts when it is that page
   for this input. A page that shows those lines beside lines the empty page lacks is
   compared with one more made-up search at the same moment: lines that page lacks are
   rows the selector no longer reads, or an error, and the answer is a failure; lines it
   shows too mean the site's pages changed. A line whose links all open the site's own
   search for another input ("Did you mean apple?", related searches) is a suggestion,
   not a line the empty page lacks, also when it continues the empty page's sentence.
   Then, and when the empty page does not name the input, an input known to have
   results is searched as well (a second one when the first finds nothing); when none
   finds anything, the search no longer reads its input, or the site answers every
   search this way. A page without the empty page's lines, while a made-up search at
   the same moment still shows them, is not the site's empty page: no statement on it
   counts. A JSON answer counts when the count field AnyUse learned is 0, the field
   that echoes the input names this input, no other list that held results holds any,
   and it holds no field the made-up answers lacked. When the site changes its empty
   page later (a made-up search no longer shows its lines), the next empty answer
   learns it again the same way and keeps it on the tool's record. A site that lists
   results for made-up words, or answers them with an error, has no empty page to
   learn; then the next steps decide.
2. **The check model** (`model_check`), only on a call you or your agent make or in a
   heal, and only when a model check is configured (`ANYUSE_MODEL_CHECKS`,
   [MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)). First, each
   line that reads as an error report (an error phrase of step 3, on a line the site's
   result pages do not show) is put to it: does the line report that the site failed
   to answer? A yes makes the page an error page, and nothing more is asked; only a no
   for every such line lets the next question be asked. Then it is asked, line by
   line, whether the lines that name the input, and the few lines below them, say that
   this search found nothing. A yes counts for a line the page holds; a no for every
   such line is a failure. A page with lines that read as result rows (step 3) is not
   asked about. A heal asks the same questions about the page of its made-up search
   ([below](#healing-without-a-browser)); proofs, recompiles and watch runs do not ask
   them.
3. **The page's statement** (`page_statement`), in the listed languages. A line that
   also stands on the site's result pages is help text, never a statement. Steps 2 and
   3 accept nothing on a page with lines that read as result rows: two or more lines in
   the same place of the page, each naming the input and holding a link. The sentence
   must name the input in its own line ("No matching crates for zzqx"), or sit right
   under a heading that names it, or stand at most three lines below a search summary
   that names it ("Showing results for zzqx", then "No results found."). The lines
   between them must be page controls such as "Sort by name": a line that holds a link
   or names the input is a result row, and the summary does not reach past it. A
   sentence that names another input ("No results found for kafka", "Your search for
   kafka did not match any documents") is about that input, under a heading as below a
   summary; one about "your search" or "your search term" ("Zu Ihrem Suchbegriff wurden
   keine Ergebnisse gefunden") is about this search. A "No results" line elsewhere (a
   footer, a help box) does not count, and neither does an error page: a page with a
   line that states an error ("Something went wrong.", "Service temporarily
   unavailable"). A line the site's result pages show too is its chrome, not an error;
   "Try again later" is an instruction, and an error phrase in a condition or a
   question ("Something went wrong? Contact us") is help text. Nor does a condition, a
   help instruction or a question: "If no results for zzqx, contact us",
   "S'il n'y a aucun résultat pour zzqx, contactez-nous", "Если по запросу «zzqx» ничего
   не найдено, напишите нам", "搜索“zzqx”没有结果时，请联系我们", "No results for zzqx? Check the
   spelling" say what to do when a search finds nothing, not that this one did. A
   statement followed by help ("No results found for zzqx. Contact us if you need
   help.") or by a suggestion ("No results for zzqx – did you mean zzq?") still counts.

   The statements are read in English, German, French, Spanish and Italian, and in
   Japanese, Chinese, Korean, Russian, Greek, Arabic and Hebrew ("「東京」に一致する商品は
   見つかりませんでした", "条件に合う商品はありません", "没有找到相关结果", "暂无相关结果", "검색 결과가 없습니다", "검색 결과가 없어요", "По
   запросу «Москва» ничего не найдено", "Товары не найдены", "Δεν βρέθηκαν
   αποτελέσματα", "لا توجد نتائج", "ولا توجد نتائج", "לא נמצאו תוצאות", "נמצאו 0
   תוצאות"), with the same rules. In these languages the input often comes before the
   statement, so a statement whose sentence names anything but the input, an apology or
   "your search" ("「大阪」に一致する商品は…") is about that other thing. In Chinese, Japanese,
   Korean and Thai the input must stand in the line as a result's name must (see the
   proof above): "没有找到与“京都”相关的结果" names 京都, "東京都に一致する商品は…" does not. A bare "not found" (a
   missing page's "見つかりませんでした", a help link's "找不到") is not a statement, nor is a help
   line's question ("該当する商品が見つかりませんか？", "找不到商品？"). An error message in these languages
   ("エラーが発生しました", "Произошла ошибка") makes the page an error page.

`ANYUSE_EMPTY_PROBE=off` sends no made-up searches; then only steps 2 and 3 apply.

Tools compiled or learned by this version save the browser replay with a result
contract. Such a replay also accepts one or two results when each result's name
names the input (the rule of the proof above, with the positions the compiled request
recorded; a tool that only the browser replay serves records them from the result lists
of the page it was learned on and its fitting pages); larger lists still need the
recorded minimum count. When the tool
was learned on a page that hides rows instead of removing them, the replay reads only
the rows on screen. Older tools keep the minimum count for every answer and read every
row.

The replay reads the page as soon as the search's own data calls have answered and
the result rows have stood still for 400 ms (their number and their text; a clock or
a banner elsewhere on the page does not count). A data call is one that carries the
whole input and brought rows in the recording. The calls a page made while the input
was typed (suggestions, previews before Enter) are not waited for, and nor is a request
that does not carry the input, such as a long poll. A list the recording loaded in two
batches waits for both. The replay does not wait for the recorded minimum count: it
checks the first read. That read also accepts one or two results that name the
input, and a page that says it found nothing for the input, as the replay itself does.
It never waits longer than the fixed timer's window (10 s), and a read that holds too
few results is read again at the end of that window. An input whose list comes in fewer
batches than the input the tool was compiled from is read at 10 s. The choice is saved
with the tool: compiling with `ANYUSE_TIER2_READINESS=off` saves the replay on the fixed
timer alone, a tool compiled before readiness was the default keeps the timer, and a tool
compiled before this rule keeps the earlier wait (the recorded count of requests and
results). Compile it again to change any of these.

## Compiling from a HAR

```sh
anyuse workflow compile NAME --from-har FILE --har-param q=… --har-expect 'a|b|c'
```

Repeat `--from-har` once per value: the first file is compiled from, the rest are the
proof. `--har-expect` (the visible result list) is required, because a HAR records
traffic, not what was on screen. With one HAR, two `--expect VALUE='a|b|c'` make the
proof: record once, type what the page showed for two other values. For a flow that ends
on an item's page, give the values the page showed (`'Serde|A serialization
framework|1.0.210'`). Each value is named from the HAR's own response, else from the label
the page puts beside it, else by a configured check model's pick of the line that shows it
([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)). A typed `--expect`
case is named by its order, so type its values
in the order the page shows them. The compiled tool's output names are the page's labels
(`downloads`, `version`); a value with no label is `field_1`, `field_2`, …. A search that
opens a result (a chain) needs three HARs when the site's search answers another
result's name: the chain is fitted on that name, sent to the site, and the two later
HARs prove it ([above](#chains-pages-and-path-parameters)). Otherwise it needs four: the
second fits the chain and is not a proof case. A typed `--expect` case cannot prove a
chain. Importing opens no socket and strips credentials; only the proof and a chain's
fitting input send requests.

Firefox, Safari and Chrome's export with sensitive data write your cookies into the HAR.
AnyUse keeps only their names, and a request sent with cookies stays a candidate. The
compiled request never sends cookies, so the proof replays it without them. A request
that needs them (a signed-in search) fails the proof, is not saved, and the report says
it probably needs sign-in. It says so only when the site answered: a refused
connection, a 5xx, a CAPTCHA or a rate limit is reported as itself. A bare 403 also puts
the host on "do not auto-retry", as it does anywhere (see "When a site asks for a
person" above), and the report says until when. A request with an `Authorization`,
API-key or CSRF header, or a credential in its URL, is refused at import.

## Learning without a recording

`anyuse learn "search this site for {q}" --url https://example.org --values a,b --confirm`
lets an agent do the task once in a container browser (`ANYUSE_LEARN_ENDPOINT`), shows
you the candidate, and after your confirmation sends the recordings through the same
compile → proof → persist path. Two values can reach "proved": learn generates the
missing held-out values from the result page, else with one model call, and proves
them like yours (`--no-generate` turns this off). A page only the browser replay
reproduces is saved as a tool served by that replay. See
[QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md).

The agent's clicks and typing go to the element it was shown, not to a place in a
list. Pages change while the model decides: a banner or an ad slot loads late, a
suggestion list re-sorts, a feed adds rows on top. The click still lands on the element
the model chose. A text field stays the same field when only its placeholder or value
changed. When that element is gone, or several elements look the same and neither
their neighbours nor their order tells them apart, nothing is clicked; the agent sees
why and looks at the page again (`execution/agent_browser.py`, `BOUND_PICK_FN`). An
index the model was not shown is not acted on, as before, and a submit without one
still presses Enter in the field that has the focus.

## Failure handling

Every compiled step carries a postcondition: status, content type, the result path,
a minimum item count and, where recorded, the workflow's identity. When it does not
hold, the step reports an explicit failure with **zero items** — never an empty list
that looks like "no results" — and the call moves to the browser replay, then the
agent, when each is available. When neither answers, the call heals the tool from its
result page where it can ([below](#healing-without-a-browser)); otherwise it fails with
the reason.

Repeated failures on answers from the site demote the step (three in a row, or fewer
than half of at least five runs); so does one such failure while a lower step (browser
replay or agent) still answers. A destination the pre-send policy refuses before the
first request is not counted: nothing was sent. A redirect it refuses is counted: the
site answered and points somewhere else. In a failure-injection check (3 kinds of
compiled tool × 5 kinds of breakage, among them HTTP 404 and 503, a 200 with a changed
shape and a network error) all 15 failed explicitly, returned no items and made no
model call.

### When the site does not answer

A server error (5xx), a timeout, a refused connection and a name that does not resolve
say nothing about the request, so they never count towards demoting it
(`execution/api_recompile.py`, `record_outage`). The call fails with `transient: true`
and says to call again later. After the second such call in a row the request is not
sent for 30 seconds, then twice as long after each further one, at most 15 minutes; a
`Retry-After` the site sent (seconds or a date) sets the wait instead, from the first
call on, at most a day. A call inside the wait sends nothing and says when the request
goes out again (`retry_after`). The first answer from the site ends the wait.
`health.consecutive_outages` and `health.retry_after` in the listing show it. A rate
limit (HTTP 429) is not counted either: it holds the whole host for its `Retry-After`
and the call says so ([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md)).

A request that keeps failing that way while the site itself answers was removed behind a
server error. After 4 such calls in a row over at least 30 minutes
(`gone_check_due`), the call checks whether the site answers: a browser replay or an
agent that answered this call read the site's pages, so nothing more is sent; otherwise
one GET of the site's own page (the tool's result page for its last good answer, else
the task's start page, else the site's front page) on an acknowledged host, with the
robots.txt rules known locally, at most once every 30 minutes. When the site answers,
the request is taken out of service with the cause `gone` (`health.endpoint_removed` says
why), and the same call heals it from the result pages
([below](#healing-without-a-browser)) or, when a lower step answered, recompiles it
([below](#automatic-recompilation)). A site that is down everywhere never gets there.
`health.first_outage_at` and `health.last_site_check` show the count's start and the
last check. A check of a gone request that still gets a server error keeps the cause
`gone`; one that answers and passes its checks puts it back in service and forgets the
outage.

### A demoted request comes back by itself

A demoted request is sent again, once, on the first call after a wait: 10 minutes after
it was demoted, then twice as long after each check that fails, at most a day
(`claim_probe`). The wait moves ahead under the lock on the tool's record before the
request is sent, so parallel calls send one check. A check the site answers with a
`Retry-After` waits at least that long for the next one. The check is the call's own request,
through the same executor as every other call (the pre-send policy, robots.txt, held
hosts). It puts the request back in service when the answer passes the request's checks
(its result contract among them) and, for a list tool with a last good answer, that
input's answer holds now (one more request when the call asked for another input;
`fresh_truth.answer_holds_now`): it repeats at least 80 % of the last good answer, in
order, or it matches, as the proof compares lists, what the site shows for that input
now: the browser replay (with a container browser), else a plain read of the
server-rendered result page. A tool with neither needs the answer to change with the
input (the call's own input, else one request for a made-up input) and a configured
check model to say its entries are results of the search
([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)). The call is
then answered from it. The record keeps
`restored` (`at`, `by`, `why`, the reason it was demoted). A record demoted before this
release counts its wait from its last run.

A check refused before it is sent (a held host, the pre-send policy, robots.txt) asked the
site nothing and is not counted; the next one is due a window later, as after any check.
A check the site blocks (a CAPTCHA, a sign-in wall, a rate limit, a held host) gets the
answer any call the site blocked gets: `blocked`, `action_required: "human"` and its card
in the inbox ([HUMAN_INBOX.md](HUMAN_INBOX.md)), with `probe` saying whether it was sent;
the request stays out of service.

While a request is demoted, a call that cannot be answered says what happened and what
comes next (`out_of_service`: `cause`, `next_check_after`): after a site that did not
answer (`outage`), "the site did not answer; AnyUse tries again after <time>", and nothing
needs recording again; after a check the site answered with a rate limit (`rate_limit`),
"the site asked AnyUse to wait (rate limit); AnyUse tries again after <time>", and nothing
needs recording again; after answers that failed its checks (`answers`), when AnyUse tries
again and that a site that changed for good needs a new recording; after a request that
kept failing while the site's own pages answered (`gone`), that AnyUse heals or recompiles
it from those pages when it can, else that it needs a new recording. A demoted request
whose check is due and that has no lower step is listed as runnable again
(`available_via: "api"`), so typed MCP tools, skills and routines reach the check.

### Automatic recompilation

When a lower step answered for a demoted request, the same call recompiles it
(`execution/auto_recompile.py`, `RecompileManager.recompile_now`). The CLI
(`anyuse workflow play`), the typed MCP tools and the `meta` profile's `call_operation`
all get it, since they share `run_compiled`.

* **Evidence, freshest first.** With a browser replay, the replay asks the container
  browser for the page's own GET responses (redacted, at most 24 bodies; a request
  sent with a credential header, a credential query parameter or a session cookie is
  left out). One replayed page is the compile recording: this call's page when it
  listed at least the replay's minimum count of results and its input is not one of
  the proof's held-out inputs, otherwise the page of an input the replay's selector
  was fitted on, so a "no results" call heals the tool too. A call answered with
  fewer results (one or two, under the result contract) is the compile recording
  only when no other replayed page has traffic. The validation set is the replayed
  result pages of every held-out proof input, read with the replay's selector (only
  the rows on screen, when the replay reads that way). For a search-then-open chain
  every page is replayed with its traffic, since the proof traces each held-out input's
  result id in that input's own traffic, and a second page fits the chain: an input
  the replay's selector was fitted on, else another result's name from the compile
  page's search, replayed in the browser (then over plain HTTP); the name the record
  keeps (`chain_fit`) is tried first. A held-out page that comes back without its
  traffic sets the replay aside. Without a browser, or then, AnyUse compiles from the
  pages of the last calls the agent answered, kept in the tool's folder: the newest
  compiles, the next selects, the others validate, once 4 different inputs are kept.
  Then it reads the recordings or attach sessions the tool was compiled from, and a
  chain kept without a fitting recording gets a generated one over plain HTTP.
* **Kept evidence** (`execution/evidence_store.py`, `ANYUSE_KEEP_EVIDENCE`). Every compile
  (a recording folder, a HAR, attach, `learn`) keeps the recordings it was proved from
  in the tool's folder (`evidence/`, named in `source.paths` relative to the folder), so
  they survive a rename and the attach sessions' retention; a heal or an automatic
  recompile from the browser replay that swaps a new request in keeps its pages
  instead. When the agent answers a call, the page it ended on, its visible text and
  the GET responses it made (credential-free and redacted, at most 24) are kept when its
  answer stands on that page (at least 80 % of it in the page's text): up to 6 inputs,
  newest first, each used for at most a day. A tool compiled before this release reads
  the files it was compiled from while they are there. A signed-in tool keeps nothing.
* **The same proof.** `compile_and_verify_with_fit` (or `attach.compile_sessions`) with
  the compile's own pre-send checks (`permission_preflight`). A new request on a host
  you have not confirmed, or on a path robots.txt disallows, is not swapped in.
* **Swap only at the tool's status.** A proof that passes at the tool's status (or
  better) replaces the template and its proof, restores the request as preferred and
  starts its health over. The previous record is kept as `compiled_api.previous.json`
  next to `compiled_api.json`. A weaker proof (for example `checked` for a `proved`
  tool) is not swapped in.
* **Paced.** At most three attempts per tool, at least an hour apart; the attempt is
  counted before any page is loaded, under a lock on the tool's record, so parallel
  calls make one attempt. A check that finds nothing to compile from without loading a
  page or sending a request (no capturing browser, no kept recordings, only a held-out
  input to compile from) is said in the result and not counted. A failed attempt
  leaves the tool on the lower step with the reason in `health.demoted_reason`.
* **Not automatic.** A signed-in (`login=replay`) tool: capture it again with
  `anyuse attach --login-replay` and run `anyuse compile last`. A tool whose current
  proof a registered skill or routine pins (`<ANYUSE_DATA_DIR>/skills`): a swap changes
  the proof id and template digest the manifest pinned, and its fresh check would stop
  it ([ROUTINES.md](ROUTINES.md)). Such a tool stays on the lower step, where the skill
  keeps running; recompile it yourself and pin the new proof in the manifest. The
  result's `recompiled.reason` names the skills.

The call's result carries `recompiled` (`attempted`, `swapped`, `from`, `reason`,
`next_attempt_after`); `list_compiled_workflows`, `anyuse workflow list` and
`--json` show the last attempt and the last swap under `recompile`. After three
failed attempts, record the search again and run `anyuse workflow compile` (or
`anyuse learn`).

### Healing without a browser

When the compiled request fails on an answer from the site (not a CAPTCHA, a rate limit,
a server error or a timeout), or was taken out as gone
([above](#when-the-site-does-not-answer)), and no browser replay or agent answers, the
same call heals the tool from plain HTTP reads of its result page (`execution/step_heal.py`).
No browser is used; a configured check model is asked only when the page alone cannot
say which list is the answer. The CLI, the typed MCP tools and `call_operation` all
get it, since they share `run_compiled`.

* **Only a page the server renders.** At compile, AnyUse notes whether the recorded
  result page's HTML held the results (`page.html_holds_items` in `compiled_api.json`).
  A page whose script fills the list in is never read this way.
* **The page as it is now** (`execution/fresh_truth.py`). The list read from the plain
  pages for the tool's last good answer (its drift anchor) and one more input counts
  when it repeats at least 80 % of that answer, in order and exactly. A list that
  changed since (newest first, live prices in the names) counts when it passes the list
  contract and, cheapest first: the tool's own fresh response holds at least 80 % of
  its entries (none of them: the page lists other things, refused); or the page for a
  made-up input shows no list there or is the site's own empty page
  ([below](#when-an-empty-result-counts); a configured check model may read that page
  first, and the statement in a listed language is the last step), and at least half
  of the last good answer's entries have a near entry on the page; or a configured check model says a line of the list is a result and no
  other line of the page is ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)).
  The heal then repairs against the list the page shows now, and that list becomes
  the tool's last good answer. A page that lists other things is not used. The
  result's `truth` says which step decided.
* **Under the site's traffic policy.** Every read goes through the same executor as a
  replay. The page's host must be acknowledged, and robots.txt must allow the path.
* **Step repair first.** One read of the anchor's input says which step broke (the
  request, the input's place in it, the result region, or how records are split). Only
  that step is derived again (a renamed JSON key, a moved list, a renamed class). The
  repaired request is then proved on two held-out inputs against their result pages:
  the proof's held-out inputs, and this call's own input when one of them is the anchor.
* **Then a full recompile.** When step repair cannot rebuild the step (the API answers
  `404`/`410`, the input moved), the result pages themselves are compiled and proved
  the same way. A JSON tool may come back as a request that reads the page's HTML.
* **Swap rules.** The new request must take the same input and send it, prove at least
  the status the tool had, and differ from the old one; the stored request must still
  be the one it was repaired from. The previous record is kept as
  `compiled_api.previous.json`. The call then runs again with the new request, and its
  result carries `healed` (`rung`, `localised`, `relearned`, `requests`, `model_calls`).
* **Paced.** A heal takes about 7 to 10 requests, 1 more page read for a made-up input
  when the list changed since the last good answer, and 2 more when it changes the
  request (the new request's empty page is learned). An attempt marks the tool before its
  first request, so parallel calls make one attempt. A failed attempt is not repeated
  for 30 minutes, and a tool that fails again within 10 minutes of a heal (a swap, or a
  request the heal put back in service) is left alone for 30 minutes. The run after a
  heal does not try the browser replay or the agent again (its `rungs_tried` lists them
  with `before_heal`), and a request the heal swapped in starts with health of its own.
  A call that ran a request another call has since replaced makes no attempt and says to
  call again. The result carries `heal` with the reason and `next_attempt_after`.
* **Nothing to repair.** When the read of the anchor's input shows the request works
  again, or reproduces the list the page shows now (which then becomes its last good
  answer), nothing is swapped. On a demoted request, the tool's own request is then run
  for that input once more; when it passes its checks and its answer holds now, the
  request is back in service (the check a demoted request gets,
  [above](#a-demoted-request-comes-back-by-itself)), the tool is not marked stale and the
  call runs with it (`restored`). A demoted request whose site did not answer is not
  healed; its check brings it back.
* **Not healed.** A signed-in tool, a tool a registered skill pins, a chain or a tool
  with more than one input, a tool with no last good answer, and a tool with too few
  inputs to prove on (it needs the anchor, one more input to select with and two held
  out). These are refused before any request.

`ANYUSE_AUTO_HEAL=off` turns it off ([SETTINGS.md](SETTINGS.md)).

## Typed workflows

Compiled templates (schema version 2) tag every value:

| kind | contract |
|---|---|
| `const` | part of the proved template; callers cannot override it, and changing it invalidates the proof |
| `input` | caller-supplied, with a scalar type and length, domain or numeric bounds |
| `derived` | taken from an earlier response (including cursors), or read from the site's own page by a locator (`from: page`; a key's value is never stored or shown); type, size, source and destination are checked; never a public input |

Chains carry a dataflow contract (inputs, each step's outputs, intermediate bindings,
the final output), and the proof runs the whole chain per fresh value. Older schema-version-1 records
are migrated in memory on load and keep their proof digest.

Which result a search-then-open chain opens comes from the compile and fitting
recordings:

- every recording opened the first result: the chain opens the first result;
- a recording opened a later result whose name (or id) is what was typed: the chain
  opens the result with that name, an exact match first, then one that differs only
  in case, punctuation or spacing. When no result has that name it opens nothing and
  says so (`not_found`), unless a recording showed the page opening the first result
  for a word no result was named after. Results that carry no name at all (or a
  search that no longer returns the list) mean the site changed: the call fails like
  any other failed request and moves to the next step of the cascade;
- a recording opened a later result that does not carry the typed name: the chain is
  refused. Record the search again and open the result whose name is what you typed.

The rule is stored on the template (`pick` on the first step, `row` in the dataflow
contract), and each proof case records which rule chose its result (`row_rules`).

The proof also checks, for every validation input, the value the first step hands to
the second (the result's id). A recording of real traffic (a HAR, an attach session, a
recording file) does not state that value, so the proof finds it in the recording:

- the first step's recorded answer, read with the rule above, gives the value;
- a later recorded request of the second step must carry that value;
- that request's answer must be what the page showed, so a detail the page fetched
  ahead for another result is not taken for the one that was opened.

When the value cannot be found this way, the case fails and says why and what to
record instead: the recording opened a different result, the page showed something
other than the answer fetched for that result, or the input was typed in without
traffic (`--expect`). A recording that states the values itself (`bindings`) keeps
them. Each proof case records how its value was found (`bindings_derived`: the steps,
the path, the row, the rule and which recorded requests), never the value.

A result id the search returns as a JSON number (`{"id": 4021}`) is typed `integer`
(`number` when a recording returned a decimal), so the chain passes it on as a number.

## Self-healing (opt-in)

All of this is off by default.

- **Weighted element matcher** (`ANYUSE_ELEMENT_MATCHER=weighted`): scores candidates
  with a confidence and a top-two margin, so it can abstain instead of guessing. Fit
  it to your own recordings:

  ```sh
  anyuse selfheal eval pairs.json                    # cascade vs weighted, on your data
  anyuse selfheal fit pairs.json --out weights.json  # then ANYUSE_ELEMENT_MATCHER=weighted
  ```

  `pairs.json` is `[{"target": {...}, "candidates": [...], "correct": <index|-1>}]`;
  `-1` means the element is gone and abstaining is right.
- **Selector repair** proposes a replacement only when the weighted matcher wins by a
  wide margin and the new selector is durable (it has an identifier or a name).
- **Drift tolerance** (`ANYUSE_DRIFT_MISSING_TOLERANCE`, default `0.05`): the share of
  required anchors that may be missing before drift is declared. A screen with a single
  required anchor is never tolerated into silence.
- **Typed judge** (`ANYUSE_JUDGE`: `none` (default), `jev`, `llm`): asked only at drift
  triage and to veto a low-margin re-anchor. It can escalate or abstain, not continue
  past the gate; page text reaches it as data.

## Limits

- **Read-only.** Only `GET`/`HEAD` are sent, plus read-only POST where you opted in,
  and a search provider's read POST that the site's own page sends (above).
- **No authenticated APIs.** Credential headers and signed or session-bound
  parameters make a template fail to compile; logged-in replay is a separate site mode
  ([SITE_MODES.md](SITE_MODES.md)). The one exception is a public key the site's own
  page states by name, read from that page on every call and never stored.
- **`agent run --compile` builds a browser tool only.** Its trace keeps no response
  bodies, so an HTTP plan that reads a provider key or landing-page id comes from `learn`, `attach`, a HAR or a
  recording folder.
- **The proof covers the values it checked**, under one normalisation. Endpoints can
  change without notice; the postcondition makes that visible, it does not prevent it.
