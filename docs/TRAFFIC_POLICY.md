# Traffic policy

What AnyUse sends to a website, and what it refuses to send. One guard applies it
to every browser path: `anyuse agent run`, MCP `agent_run`, compiling and its proof
replays, `anyuse watch run`, and the MCP web-tool route with its fallback. Code:
`execution/cdp_guard.py`, `execution/web_scope.py`, `execution/api_permission.py`,
`execution/discover.py`.

## Pacing and budgets

| rule | value | knob |
|---|---|---|
| spacing between two documents (main-frame navigations) of one site | **≥ 3 s** | `cdp_guard.DOC_MIN_INTERVAL_S` |
| spacing between two requests to one host | **≥ 1 s** | `cdp_guard.REQUEST_MIN_INTERVAL_S` |
| requests of one compile run (`--compile`: the agent's run plus both proof replays) | **50** | `harness_product.COMPILE_SITE_CAP` |
| requests of any other agent run | **200** | `harness_product.RUN_SITE_CAP` |
| requests per page load | **30** | `RequestBudget(per_load_cap=)` |
| share of a run's ceiling kept for documents and data | **40 %** | `harness_product.STATIC_RESERVE_FRAC` |
| browser statics in flight at once | **4** | `cdp_guard.BROWSER_STATIC_IN_FLIGHT` |
| methods | GET and HEAD; anything else is refused before it is sent (read-only search POST: below) | `RequestBudget(allow_methods=)` |
| redirects | followed within the same site (the start host and its `www.` sibling, http → https); never to another domain | `site_canon.is_same_site_redirect` |
| User-Agent | AnyUse's own on every browser path; compiled HTTP replays send it too, unless the site is set to `identity = browser`, which replays the recorded browser's ([SITE_MODES.md](SITE_MODES.md)) | per-site `identity` (compiled replays only) |

The ceilings count **documents, data calls** (XHR / fetch, a search provider's
call) **and every `robots.txt` request**. A request whose query carries the typed
value is always counted as data.

## What a tool sends by itself

Besides the request a call asks for, a compiled web tool sends a few requests of its
own to check, heal or rebuild itself. Each goes through the same executor as a call:
the pre-send policy, held and blocked hosts, the site's modes and `robots.txt` (the
gone check uses the `robots.txt` rules already known, so it adds no request while the
site is down).

| what | when | requests | knob |
|---|---|---|---|
| the check of a request out of service ([COMPILE.md](COMPILE.md#a-demoted-request-comes-back-by-itself)) | the first call after its wait (10 minutes, doubling to a day) | the call's request; **+1** for the last good answer's input when the call asked another; when that answer changed since: **+1** browser replay, or **+1–2** plain reads of the server-rendered result page, or, with neither, **+1** request for a made-up input when the call asked for the last good answer's input | `RecompilePolicy.probe_after_s` |
| a heal ([COMPILE.md](COMPILE.md#healing-without-a-browser)) | a call fails on an answer from the site, or the request is taken out as gone, and no lower step answered; not again for 30 minutes after a failed one | about **7–10**; **+1** page read for a made-up input when the list changed since the last good answer; **+1–2** page reads when it puts a request back in service whose list changed; **+2** made-up searches when it changes the request | `ANYUSE_AUTO_HEAL` |
| the gone check ([COMPILE.md](COMPILE.md#when-the-site-does-not-answer)) | after **4** calls in a row the site did not answer, over at least **30 minutes**; none when the browser replay or the agent answered | **1** GET of the site's own page, at most once every **30 minutes** | `RecompilePolicy.gone_after_outages`, `gone_after_s` |
| the site's empty page ([below](#empty-answers)) | a passing proof; an empty answer the empty page does not settle by itself | **2** made-up searches per proof; **0–3** per empty answer (**4–5** once after the site changed its empty page) | `ANYUSE_EMPTY_PROBE` |
| a chain's generated fitting input (compile or recompile, when no recorded input is free to fit it) | the compile of a search that opens a result | **3** values, each **≤ 3** plain-HTTP requests (search, open, result page), through the proof's executor and pre-send checks; with a browser replay, one page load per value first, under the per-load cap | `chain_fit.FIT_TRIES`, `ANYUSE_CHAIN_FIT` |
| the site's page for a tool that reads a provider key ([below](#provider-keys)) | every call | **+1** GET of the page that states the provider's key, without cookies, before the provider request | `ANYUSE_PAGE_KEYS` |

Keeping evidence sends nothing: the pages the agent answered on and the recordings a
tool was compiled from are kept from what was already loaded (`ANYUSE_KEEP_EVIDENCE`).

## Empty answers

A compiled web tool checks an empty answer against the site's own empty page
([COMPILE.md](COMPILE.md#when-an-empty-result-counts)). It learns that page with
searches for made-up inputs:

| when | searches |
|---|---|
| after a passing proof (compile, recompile, a heal that changed the request) | **2** made-up inputs |
| after a saved web tool's passing proof | **2** replays of made-up values, inside the compile's ceiling |
| the first empty answer of a tool proved before this release | **3**, once: an input known to have results and 2 made-up ones (4 when the first such input finds nothing and a second is tried) |
| an empty answer that is the site's empty page | **0** |
| an empty answer the site's empty page does not settle by itself: lines it lacks, a page that is not it, or a site whose empty page does not name the input | **at most 3**: 1 made-up search, and up to 2 inputs known to have results |
| the first empty answer after the site changed its empty page (the made-up search no longer shows it) | **4**, once: that made-up search, an input known to have results and 2 new made-up ones (5 when a second such input is tried); the new page is kept on the tool's record |

Each search is a normal read-only search through the same checks as the call: the
pre-send policy, `robots.txt`, held and blocked hosts, the site's modes. A made-up
search the site blocks is recorded like any block. In the browser replay each search
is one page load, paced like any other, and its traffic is never kept as recompile
evidence. `ANYUSE_EMPTY_PROBE=off` sends none of them.

## Static assets load like a normal visit

Scripts, stylesheets, fonts and images fetched by GET/HEAD from the site itself,
its own subdomains and CDN hosts, its store platform's asset CDN, or its granted
search provider are sent as a browser would send them. They are counted
separately, logged, and cached for the rest of the run (a script already fetched is
served from the run cache to later pages; never a response with `no-store`, a
cookie, a request body or an input value). Media, pings, manifests, WebSockets and
workers stay blocked.

**Runaway guard.** The same static URL requested more than 5 times in one page
load, or more than 1,000 distinct statics in one page load, stops the site for the
run with the typed stop `static_runaway`.

**Settling.** Waiting sends nothing: the document complete, the DOM quiet for
500 ms and no XHR / fetch in flight (at most 5 s; a replay's result wait may go on
up to 8 s more while the page's own requests are in flight). While only requests
that do not carry the input are open (a long poll, a beacon), a DOM quiet for
1.2 s ends the wait. A typed search's wait also ends once the requests carrying
the input have answered and the DOM has been quiet for 500 ms; a request carrying
the input that is still open holds it.

**Finding the search box.** Without a recorded input, the page's structure decides
first: an input of type `search`, an ARIA `searchbox`, or one inside a search landmark
(`role=search`, `<search>`). Among several other text inputs, the input is typed into
one at a time (at most three; inputs named like a search first) and the first one the
page answers is the search box: a request carrying the typed value, the page's text
showing it (suggestions, live results), or its own GET form submitting it to the
site. Each input that does not answer is emptied again. Only when none answers is an
input named like a search used. A replay types into the input its tool recorded: the
one with its name or id, else the one at its place among the inputs that read as a
search box; a tool saved by an older version also counts the inputs its own rule named (a
search word anywhere in the name, id, placeholder, label or class). The repair's probe
looks for the recorded input among the same inputs; one whose recorded name or id is
gone counts as moved. A search that is only an icon is opened by clicking
candidate controls, one at a time and at most three, until a hidden text input
appears: a control that names a hidden input (`aria-controls`, a `label`, a
`<details>`), a control in a search landmark, then a control named like a search,
then an icon-only button in the page header; in-page controls before a link to
another page of the site. An icon-only link in the header (an account or cart
page) is never clicked to open a search.

## Read-only search POST

A POST is sent only when it is the page's own search, and all of these hold:

| condition | how it is checked |
|---|---|
| sent right after typing into the site's search box | inside that action's window (+5 s) |
| to the site's own host or its granted search provider | the run's origin policy and `ANYUSE_THIRD_PARTY`; never a refused host |
| the body carries the typed value | a small form or JSON body (no multipart, ≤ 8 KB) with a field whose whole value is the typed value, whatever the field and the path are called; when the value sits inside a longer field, the path or that field must be named like a search |
| never a login, account, cart, checkout or payment path | path and body words, telemetry paths, GraphQL mutations, an `Authorization` header |
| no cookies beyond an anonymous context | every guarded session is a new, empty browser context |

It is counted as data and logged on the run record (`traffic.search_posts`). Every
other POST is refused. Whether the answer is a result list that changes with the
input is decided by the proof, which runs the search again with other inputs.

## robots.txt (RFC 9309)

One module (`execution/api_permission.py`) applies `robots.txt` on every path.

| robots.txt answer | verdict |
|---|---|
| 2xx | the file's rules |
| 404 and other 4xx (except 429) | no rules: the site is allowed |
| 429 | back off (`Retry-After`, at most 10 s) and retry twice; still 429: refused for this run |
| 5xx or no answer | complete disallow for this run |
| redirect | followed for at most 5 hops; the reached file applies to the host asked. A hop to a refused host, a non-public address, a loop or a 6th hop is a complete disallow |

Rules are matched per RFC 9309 §2.2: the group whose `User-agent` equals the product
token `anyuse-compiled-workflow` or a leading part of it (`anyuse-compiled`, `anyuse`),
in any case, else `*`. A value that only appears inside the token (`flow`, `use`) names
another crawler. `*` and a final `$` are wildcards; paths compare percent-decoded; the longest match
wins and `Allow` wins a tie. Files are cached for at most 24 hours
(`ANYUSE_ROBOTS_TTL_S`); an unreachable verdict is re-read after an hour; a network
failure on your side is never cached. A refusal on the run record says whether a
file disallowed the request or could not be read.

**`ANYUSE_ROBOTS`:**

| value | robots.txt applies to |
|---|---|
| `respect` (default) | everything except the site's own pages in a signed-in task you started |
| `user_initiated` | everything except a lookup or compile you started explicitly (an MCP tool call, `anyuse agent run` / `agent replay`); background work (watch runs, schedules, multi-page and result-visiting runs) still respects it |
| `ignore` | nothing |

In every mode the User-Agent setting, pacing, budgets, the refused-host list, the
third-party rules and the no-bypass rule for CAPTCHAs and bot walls stay in force,
and what an override let through is recorded on the run.

**A signed-in task you started is not crawling.** A task you start yourself (an MCP
tool call, `anyuse agent run` or `agent replay`) that signs in on a site, with your
credentials as `${secret:NAME}` values or with your imported `login=replay` session,
works in your own account there. Its requests to that site's own origin (the start
host and its `www` sibling) are not held to `robots.txt`, so your orders, cart and
checkout pages open even when the site disallows them for crawlers. Each request
`robots.txt` would refuse is recorded on the run: `traffic.robots_override` reads "a
signed-in task you started (cli)" (or `mcp`), and `traffic.robots_overridden` names
the host, what `robots.txt` said and how many requests passed. A compiled request's
result carries the same reason in `robots_overridden`. Other hosts the page uses keep
their own `robots.txt`. Watch runs, schedules, multi-page and result-visiting runs, recompiles and
compile-time discovery are not started by you and still respect `robots.txt`.

A search whose results `robots.txt` disallows ends the run with the typed stop
`robots_search`.

## Hosts that are never contacted

Hosts on the never-contact list are refused before any request, in every mode
(`web_scope.contact_refusal`). With `ANYUSE_REFUSE_GOV_HOSTS` on, government, `.int`,
EU-institution and military hosts (`execution/gov_hosts.py`) are refused the same way
on every path; it is off by default, so they are treated like any other host. The
benchmarks never choose such a host as a site, whatever the setting.

## Third-party hosts: `ANYUSE_THIRD_PARTY`

| mode | what may be sent besides the site itself |
|---|---|
| `off` | nothing: first party only |
| `search` (default) | the site's own search provider: the third-party host the site's own page sends the typed value to in a data call (an XHR, fetch, JSONP script or event stream with the value as a parameter, a body field or a path segment, not inside another URL); after that, reads under the same path, their CORS preflight and a read-only search POST right after typing. The catalogue (Google CSE, Algolia, Yext, Elastic App / Site Search, Typesense Cloud, Coveo, Bing Custom Search, Searchspring, Klevu, Doofinder, Findologic, Boost Commerce) is the hint that lets a provider's widget script and documented read query through once the site's own page names it. The provider has its own share of 10 requests per page load; plus the store platform's asset CDN for static assets |
| `any` | any third-party host the page uses for content, except hosts on the bundled EasyList/EasyPrivacy domain list; never a document of another site |

In every mode: read-only as the page itself does it; `robots.txt` of every host;
every third-party host, grant and refusal logged on the run (an observed provider
under `third_party.observed`, with the path and the request that made it one); an
allowed host shares the site's pacing and budget. A host on the ad/tracker list, a
telemetry path or a path that names a state change never becomes a provider, even
when it receives the typed value.

**Asset hosts.** A host the site's own document loads its static assets from
(`img src` and `srcset`, `script src`, a stylesheet, preload or icon `link`, CSS
`url()`) is treated as first party for scripts, CSS, fonts and images — never for
documents or data calls, never a host on the ad/tracker list, never a request that
carries the typed value. Once such a host answers the page with data (JSON, HTML,
XML, text), its grant ends for the run. A host only linked to (`a href`) is not one.
The store platforms' own asset CDNs (Shopify, BigCommerce, Wix, Squarespace) are
also recognised by name, and the run record names the platform.

**When a host is not allowed**, the typed stops are `provider_search` (a provider
request carrying the value was refused and the answer had no rows; the stop names
the scope's reason, such as the ad/tracker list) and `off_site_search` (the site's
search form submits to another site). A demonstration whose typed search lands back on
the unchanged start page, after the search's own button, the box's GET form and the
page's search URL, stops with `no_site_search`.

## Web scope settings

| setting | default | what it allows |
|---|---|---|
| `ANYUSE_ALLOW_SITE_OWN_HOSTS` | on | the site's own subdomains and CDN hosts on the same registrable domain (shared-hosting suffixes such as `github.io` count per tenant), or hosts declared per site with a reason in `ANYUSE_SITE_HOSTS_FILE` |
| `ANYUSE_THIRD_PARTY_PAGE_CODE` | on | the page's own scripts and stylesheets from other domains, GET only, counted as statics; never images, fonts, data calls or anything carrying the typed value |

**The site's registrable domain.** A host's registrable domain is one label under its
public suffix. Suffixes come from the built-in lists (`co.uk`, `com.au`, `github.io`, …),
and under a two-letter country code a generic second level (`com`, `net`, `org`, `edu`,
`gov`, `ac`, `co`) is a suffix too: `shop-a.com.pk` and `other.com.pk` are two sites.
Where neither knows the suffix and the country code has public second levels this
build does not list (`.it`, `.pl`, `.jp`, …), a sibling host is the site's own only when
the site's own document or script names it, or the site's navigation redirects to it.
With a pinned copy of the Public Suffix List at
`execution/web_scope_data/public_suffix_list.dat` and its SHA-256 in
`public_suffix_list.dat.sha256` next to it, that list decides instead; a file whose
digest does not match is not used.

Known trackers and ad hosts stay blocked under every rule: the bundled domain list
plus a small pinned supplement for tracker script hosts
(`execution/web_scope_data/tracker_supplement.json`). Own subdomains whose first label
names tracking (`metrics`, `analytics`, `pixel`, `beacon`, `ads`, …) are refused too.
Widening settings are security-relevant and need a confirmation in the terminal
([SETTINGS.md](SETTINGS.md)).

**Always refused for compiled tools, whatever the settings:** account, login and cart paths; anything
`robots.txt` disallows, except the site's own pages in a signed-in task you started; POSTs
whose path or fields name a state change (login, register, password, cart, checkout, payment, account, edit, save, delete, subscribe,
comment, vote, follow, contact, …) or telemetry; GraphQL mutations and
subscriptions; analytics endpoints of search providers; challenge providers
(Cloudflare challenges, reCAPTCHA, hCaptcha) outside a live solve.

**Third parties stay fatal.** A response from a host no rule allowed that reached the
network anyway is a `TrafficViolation` and the run stops.

## Provider keys

In the browser a grant never reads a key; the page sends its own public search key.

A compiled HTTP tool may send a provider's public key when the recording shows the
site's own page stating that exact value by name earlier (`"searchKey":"..."`,
`data-search-key="..."`, `?apiKey=...&`). The tool then has two steps: GET that page
of the site, without cookies, and read the key by its name; then call the provider
the way the page did. The provider's app id and index are read the same way when the
same page states them next to the key, with the recorded value as the fallback; the
key has no fallback.

- The template stores where the page states the key (its name and punctuation),
  never the key. A rotated key needs no recompile.
- Results, traces, logs and records show `<page value>` for the key. The recorders
  replace it with `<redacted>` in the request and `<page value>` in the page bodies
  and in other requests' URLs.
- A credential-like field whose value no page of the site stated is handled as
  before: attach, a HAR and a replay's traffic keep the request as a fact and drop its
  bodies; a `learn` recording keeps it as the browser sent it, and the compile never
  puts the value in a template.
- Each call sends two requests: the page, then the provider. There is no key cache.
- `ANYUSE_PAGE_KEYS=off`: such searches stay on the browser replay; a tool already
  compiled this way sends nothing.

**A provider's search POST.** A search POST is a read the site's own page sends its
search provider, a host outside the site's own domain (Typesense's `multi_search`, for
example), when it passes the observed-provider rule of `ANYUSE_THIRD_PARTY` (it carries
the typed value, no telemetry or state-change path or field, no GraphQL mutation). Such a POST in a
tool that reads its key from the page runs without `ANYUSE_API_READONLY_POST` once its host is acknowledged
(the proof asks for it, as for every new host), and the rule is checked again on
every send. Every other POST still needs `ANYUSE_API_READONLY_POST` and its exact
endpoint approval.

## Live-solve exception

While a person solves a challenge on one page (`ANYUSE_CHALLENGE_POLICY=human_solves_live`,
see [HUMAN_INBOX.md](HUMAN_INBOX.md)), that page may load the check from the
challenge provider's own hosts (`challenges.cloudflare.com`, the reCAPTCHA and
hCaptcha hosts), GET/HEAD only, https only. The grant opens with the solve and is
revoked when it clears, expires, is declined or the page closes.

## Caching never answers a check

The run cache holds static assets only. Documents and data always go to the site. A
request or response that carries a value being checked (in the URL, the body or a
header) is never stored or answered from a cache; every such request is recorded
with its path, and a cache path for it counts as a violation. Storage otherwise
follows HTTP rules for a private cache (only a 200 GET; `no-store`, `no-cache`,
`Set-Cookie`, `Authorization` or a request body prevent it; entries expire by
`max-age`, `Expires` or heuristic freshness of at most 24 h).

## Browser infrastructure

`execution/browser_supervisor.py`. A dead browser, a stalled DevTools call, a
crashed or vanished page target or a dropped websocket is an `infrastructure`
failure, never a product failure: the run stops and the site is not contacted again
for it. The browser is health-checked before a site is contacted; every task uses
one page that is closed explicitly; DOM and text reads are bounded; a dropped
websocket re-attaches to the same target and only re-sends a read.

## The bundled blocklist

`execution/web_scope_data/easylist_domains.txt.gz` is a domain-only extract of the
EasyList and EasyPrivacy server lists (57,066 domains), checked by sha256 at load;
a mismatch refuses the list. Nothing is downloaded at runtime. It keeps its upstream
licence (GPL-3.0-or-later or CC BY-SA 3.0), see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). Host-and-path rules are not part
of a domain-only list.
