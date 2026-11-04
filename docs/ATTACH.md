# Attach recorder — "your agent did it once, now it's a proved tool"

Any agent drives a Chromium: browser-use, Hermes, Claude Code with a browser,
or a person. AnyUse **attaches over the Chrome DevTools Protocol, observes
without actuating**, and writes one sanitised session file. Several sessions
of the same task with different inputs are compiled and proved by the
unchanged compiler (`execution/api_compile.py`, `api_verify.py`). The
threshold is the same as for a HAR or an agent recording.

Attach talks to the browser over DevTools, which needs the `cdp` extra:
`pip install -e '.[cdp]'`. Start a browser with DevTools on loopback and a throwaway
profile, so none of your cookies are involved:

```sh
# macOS
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$(mktemp -d)"
# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="$(mktemp -d)" &
```

```sh
anyuse attach --cdp http://127.0.0.1:9222 [--target ID] [--task NAME] [--input q=VALUE]
#   ... the agent does its task; Ctrl-C (or --max-seconds) ends the session
anyuse compile last [--n 4] [--task NAME] [--confirm URL]   # 4 sessions: 1 compile, 1 selection, 2 held out
anyuse attach --list
anyuse attach --forget [--session ID]
```

Code: `execution/attach.py`. Tests: `test_attach.py` (offline, fakes only).

## Design

The recorder and the actuators are separate. `agent_cdp.py` drives a browser.
`attach.py` cannot:

| piece | what it does |
|---|---|
| `validate_endpoint` | accepts `http://127.0.0.1:PORT` or `http://[::1]:PORT` and nothing else |
| `pin_target` | reads `/json/version` + `/json/list` (no proxy, no redirects) and pins one browser and one page target |
| `CdpObserver` | the only protocol client. It sends only `Network.enable`, `Page.enable`, `Network.getResponseBody` and `DOMSnapshot.captureSnapshot`. Any other method raises `ReadOnlyViolation` before a byte is sent. |
| `Recorder` | pure and synchronous. It turns protocol events into sanitised requests, navigations and snapshots, and filters secrets at ingestion. |
| `observe` | the async loop. It takes a snapshot after each main-frame navigation, load event and document/XHR/fetch response (debounced), plus a final one at stop. |
| `write_session` / `purge_expired` / `forget` | storage, retention and deletion |
| `compile_sessions` | sessions → list or item-page ground truth → shared proof split → executable template + report |

**What is recorded:**

* Every http(s) request, with URL, method, status, MIME type and size. Of the
  headers, only `content-type` is kept.
* Bodies of document, XHR and fetch responses (JSON/HTML/text, ≤ 8 MB), taken
  from the browser's own buffer. The recorder sends no network traffic.
* Main-frame navigations.
* DOM snapshots. A snapshot keeps only laid-out text nodes and tag names, plus
  the `class` and `id` attributes. It keeps no form values of any kind and no
  script, style, iframe, svg, textarea, select or `contenteditable` content.

**The ground truth.** An attached session has no human typing "what the page
showed". Ground truth is the **captured visible text of the result page**:

* The result page is the latest snapshot whose URL (or else text) contains the
  input. If there is none, it is the last snapshot.
* The result list is derived across the sessions' result pages by the same rule
  the learn loop uses (`learn_loop.derive_ground_truth`): a repeated group must
  exist on every page and must change when the input changes.
* AnyUse takes the page the agent ended on as the answer. The proof checks that
  the compiled request, bound to inputs it was not compiled from, reproduces the
  list those inputs' pages showed; it cannot check that the agent chose the right
  page. If the agent ended on the wrong page family, the tool reproduces the wrong
  page.
* That is what the confirmation catches: `compile last` shows the proved URL and a
  sample of the derived ground truth, and persists only after you answer the prompt
  or pass `--confirm <exact proved URL>`.

**Selection and validation: "proved" needs held-out validation.**
Fitting and selection do not see the validation sessions:

* **≥ 4 sessions → `proved`** (if the proof passes).
  * The result-list selector is derived from sessions 1–2 only.
  * The request is compiled from session 1. Session 2 is the dedicated
    selection input for runner-ups. When the task opens a search result with a
    second request (a chain), session 2 is its fitting input instead: the
    chain needs a second input to tell an opened result from what was typed
    ([COMPILE.md](COMPILE.md#chains-pages-and-path-parameters)). With 3 sessions
    the chain is fitted on another result's name from session 1's search, sent
    to the site, and reaches `checked`; when the site's search does not answer
    that name, the compile says to record another session.
  * Every later session (≥ 2) is validation. It is only *read* with the
    fitted selector and replayed once.
* **3 sessions → at most `checked (selection not held out)`, never `proved`.**
  * With one compile page and two validation pages, the cross-input selector
    has to be derived from all three pages, so the validation inputs took part
    in fitting.
  * Such a tool may still be persisted, after confirmation. The status travels
    in the persisted record (`proof.status`, `proof.selection_held_out`), in
    `list_compiled_workflows`, and in the note in `ladder_tier_means`.
  * It also appears in the typed MCP tool description
    (`status checked (selection not held out)`), in the `compile last` output,
    and in `--explain`, which says why it is not proved.
* Records written before this rule have no `status`. A passed proof on those
  keeps reading as `proved`.

The input name and value come from `--input` or are inferred. Inference picks
the one query parameter of the result URLs that differs in every session. If
that is ambiguous, the compile is refused.

**"Proved"** means the compiled request passed the checks in
[COMPILE.md](COMPILE.md) on the tested inputs, and those inputs were held out from
fitting and selection. A persisted tool runs with zero model calls when it succeeds,
unless you configured a model check and it settled an answer the site's own pages
leave open ([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)); a
failure falls through to the next step of the cascade.

## Item pages: the detail proof

The **list** proof needs a repeated group that changes with the input. An item
page has no such group, so item-page sessions take a detail-specific proof
(`discover.page_fields`), with roles from `proof_split.assign`. Code:
`attach.compile_detail_sessions`. Tests: `test_attach_detail.py`, with recorded
session fixtures in `testdata/attach_detail/`.

* **Which proof.** `compile_sessions(shape="auto")` (CLI: `compile last --shape
  auto|list|detail`) picks the detail proof when the input is one path segment of
  the page every session ended on (`/package/vim`), no query parameter carries it,
  and that page is an item page, not a path-style search (`/zoeken/fiets` is a
  list). The recorded pages say which, in any language:
  * another recorded page (a listing, the site's home page) lists the input as one
    entry, or links to the page: an item page;
  * the page lists results for the input: its own search field holds the input, or
    two or more of its rows link, by a text that names the input, to the site's own
    pages of one other kind (links to related items of the same kind, to other sites
    or to the item's own sub-pages are not results): a search.
  When the pages say neither, or both, the segment before the input decides: a
  search word (`/search/vim`, `/s/vim`, `/suche/vim`) is a path-style search.
  Everything else stays on the list proof. Without `--input`, the key is
  inferred as the one path segment that differs in every session.
* **Fit (compile and selection sessions only).**
  * The item URL template comes from the compile session
    (`https://host/package/{package}`, using the recorded input name). Every
    fit session must land on it.
  * The identifying field is chosen from title, h1-h3, `itemprop=name` and
    `og:title`, in that fixed order. It must be present on every fit page,
    differ between them, and name that page's key.
* **Validation (held-out sessions).**
  * Each key is fetched **once, fresh**, through the template. It is never
    answered from a cache.
  * The field on the fresh page must equal the field on that session's
    **captured** page, which is the agent's page and the ground truth. It must
    also name the key.
  * All field texts must be pairwise different.
  * The template's origin must have been observed in the compile session(s).
    The robots and permission preflight runs before any request.
* **Status.**
  * ≥ 4 sessions → `proved` if every validation key passes.
  * 3 sessions → at most `checked (selection not held out)`.
  * The scope reads "Field proof: named fields (h1) matched, on N held-out
    input(s) …". It claims one identifying field per held-out key, nothing
    more.
* **Persistence and execution.** A passing detail proof produces an ordinary
  read-only `RequestTemplate`: GET of the item URL, `shape="fields"`, and an
  `extract_kind="detail_fields"` extractor for the one fitted title, heading,
  `itemprop=name`, or `og:title` field. After the same prompt or exact
  `--confirm URL` restatement required for list tools, `compile last` writes
  `compiled_api.json`. The tool then appears in `list_compiled_workflows` and
  the typed MCP tool set. A call re-checks that the field exists and still
  names the bound key; it never imports a model or drives the browser.
* **The split status is authoritative.** Persistence copies
  `DetailProof.to_dict()` unchanged. A four-session held-out pass is `proved`
  and is callable in the restricted MCP profile. A three-session pass may be
  stored and listed as `checked (selection not held out)`, but the restricted
  profile refuses to run it; no CLI or MCP layer promotes it.

## Endpoint, pinning and what is stored

A DevTools port gives whoever reaches it every page and cookie in that browser.
AnyUse opens no listening socket of its own, and the attach rules keep it from
reaching anything but the one browser you named:

* **The endpoint.** Only `http://127.0.0.1:PORT` or `http://[::1]:PORT`:
  * no names (`localhost` can be re-pointed);
  * no alternative loopback spellings (`127.1`, `2130706433`, `127.0.0.2`,
    `::ffff:127.0.0.1`);
  * no `0.0.0.0`, no LAN address, no credentials in the URL;
  * no path, query or fragment, and the port must be explicit;
  * `HTTP(S)_PROXY` is ignored for discovery and for the WebSocket
    (`ProxyHandler({})`, `proxy=None`), and redirects are refused.
* **Pinning, no fallback.**
  * The page WebSocket URL is *built* from the validated endpoint and the
    target id. It is never taken from `/json/list`, and a reported destination
    that differs is refused.
  * The browser's debugger URL must be on the same host:port. The browser id
    and target id are written into the session.
  * With several pages and no `--target`, the recorder refuses and names the
    candidates (id and origin only; titles can be private).
  * If the connection closes or the target detaches or crashes, the session
    ends. There is no reconnect and no other target or browser. AnyUse does not
    launch, activate or create a target.
* **Non-interference with the controlling agent.**
  * No `Runtime.*` (the page cannot observe injected code), no `Input.*`,
    `Page.navigate`, `Fetch.*`, `Emulation.*`, `Target.*` or cookie methods.
  * `Network.enable` and `Page.enable` are per-session event subscriptions.
    `DOMSnapshot.captureSnapshot` forces a layout pass, which is not observable
    by page script as an action.
* **Secrets are filtered at ingestion.** Nothing below is kept in memory past
  ingestion, so it cannot be written:
  * Cookie values; `Authorization`, `Proxy-Authorization`, `X-Api-Key`, CSRF
    headers and every other `api_step.CREDENTIAL_HEADERS` value.
  * Credential-looking query and body parameters. Their values are replaced by
    `<redacted>`.
  * Hidden and password `<input>` values and `csrf`/`token` meta content in
    stored HTML.
  * JWTs, `Bearer`/`Basic` tokens and credential-keyed `"key": "value"` pairs
    in bodies and visible text.
* **Credential-bound requests are kept only as a fact.**
  * A request that carried a credential header, a session-looking cookie
    *name* (`sess`, `sid`, `auth`, `token`, `__Host-`…), a signed query
    parameter, or a credential-keyed body is marked `credential_bound`.
  * It has no body and no post data, and the compiler never sees it. The HAR
    import differs on cookies: it writes no recorded request or response to
    disk, so it keeps a request sent with cookies, and the proof replays it
    without them.
  * A POST sent after a password field was on screen is dropped.
* **A key the site's own page stated is kept as a locator** (`ANYUSE_PAGE_KEYS`).
  A request whose only credential is a credential-like *name* (a query key, a
  header, a body key; never a cookie, an `Authorization` value or a POST sent after
  a password field) waits until the session ends. If an earlier body of the
  site's own registrable domain (a document, a data response, a same-site script)
  stated each such value by name, the request is kept with `page_keys`: the field,
  the page and the name with its punctuation, never the value; otherwise it is
  refused as above. During the session only keyed fingerprints (HMAC with a key
  made for the session) of the stated values are held, never the values. Same-site
  script bodies are read from the browser's buffer for this (at most 24) and not
  stored. The stated values are replaced by `<page value>` in every stored body, URL
  and snapshot.
* **Redirects keep their hops.** A request reached through redirects keeps the
  URLs it came through (`redirected_from`), so a search that lands on a collection
  page compiles from the search URL.
* **Signed-in pages are not stored.** If the main document was loaded with a
  credential, that page's snapshot keeps no text and no DOM. The compile
  refuses the session.
* **`--anonymous-browser` (opt-in, off by default).** Many public sites give
  every anonymous visitor a session cookie (`PHPSESSID`, `JSESSIONID`,
  `csrftoken`, `WR_SID`); by name alone that looks like a sign-in, so every
  page after the first is withheld. With the flag you state that the observed
  browser holds no sign-in and its cookie jar was empty when the session
  started. A cookie is then exempt from the name rule only if **this session saw
  the site set it** (`Set-Cookie` on a response to a request that carried no
  credential) and no password field had been shown yet. Any other cookie, every
  header rule and the password-field rule stay as above.
* **Storage.**
  * One JSON file per session under `<ANYUSE_DATA_DIR>/attach/`. The directory
    is 0700 and each file is 0600, written atomically with `O_EXCL`.
  * Sessions expire after 7 days (`ANYUSE_ATTACH_TTL_DAYS`), purged on every
    attach, list and compile.
  * `anyuse attach --forget [--session ID]` deletes them.
  * A compile that saves a tool keeps a copy of the sessions it was proved from in
    the tool's folder (`evidence/`, credential header values dropped), for the
    automatic recompile. The copy goes when the tool is deleted;
    `ANYUSE_KEEP_EVIDENCE=off` keeps none.
* **Proof traffic** follows the compile's rules ([COMPILE.md](COMPILE.md#new-hosts-and-robotstxt)),
  and the compiled template's origin must have been observed in the compile
  session(s).

## Limits

* **Only Chromium over CDP.** No Firefox and no WebKit.
* **Partial provenance.** CDP observation does not prove which *actor* caused
  a request: agent, page script or user. "Any agent" means agent-independent
  capture, not guaranteed complete recording of every interaction. Where
  provenance matters, use adapter hooks or an explicit proxy.
* **Only what happens after attach.** Attach before the agent starts. Requests
  made before attach, and bodies evicted from the browser's buffer, are
  missing.
* **Heuristic filters.** Session-cookie and secret detection work by name and
  shape, so a secret under a neutral key in a JSON body can survive. The visible
  text of result pages is stored, including anything personal on a page that was
  not detected as signed-in.
* **Result-page guess.** The ground truth is only as good as the result-page
  choice (see *The ground truth* above).
* **Detail extraction is narrow.** A persisted item-page tool
  returns the one identifying field the proof checked. It does not claim the
  rest of the page is complete or stable.

## An end-to-end run

On a public package search, three attached sessions of another agent's searches
compiled to an HTML request. With a fourth session as held-out validation it
passed the proof 2/2, and in a fresh MCP server process it answered a new value in
about 190 ms with 0 model calls.
