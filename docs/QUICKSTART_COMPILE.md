# Quickstart: compile a public web workflow to an HTTP request

After the recording, everything runs as plain HTTP requests. Nothing draws on
your screen, reads your keychain or opens your browser profile.

You record a public search page, compile it and read the proof, replay it, and
hand it to an agent over MCP.

Not every site compiles. Many pages load results in ways that cannot be turned
into a stable request, and `robots.txt` rules some out. When a site does not
compile, the reason code says why (see the table at the end). Measured rates are
in [EVIDENCE.md](EVIDENCE.md).

## 0. Setup

Needs Python 3.11 or newer (`python3 --version`; macOS's default may be older,
so use a newer one, for example `python3.12`, in place of `python3`).

```sh
git clone https://github.com/michaelAGI/anyuse && cd anyuse
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .        # compile path only: httpx + click
python -m pip install -e '.[cdp]'  # only for attach and learn (a browser over DevTools)
anyuse doctor                     # read-only; reports what is missing
```

`anyuse doctor` shows where your data and secrets live, whether a container
runtime is installed, whether the `api` modality is on, and how many hosts you
have acknowledged or blocked. It changes nothing.

Compiled tools go to your data directory, `~/.anyuse` unless `ANYUSE_DATA_DIR` is set
(`anyuse doctor` prints it). A registered agent reads the same directory, so the
tool you compile here is the one your agent sees. To experiment in a scratch
directory instead, set `ANYUSE_DATA_DIR` here and register the agent with the same
value (`--env ANYUSE_DATA_DIR=...`). Secrets do not follow it: they stay in
`~/.anyuse` unless `ANYUSE_SECRETS_DIR` is set.

## 1. Record the search

The compiler needs the same search with three different values: one it may look
at, two it may not. The two unseen values are the proof, and their ground truth is
what the page showed for them. A search that then opens a result and reads its page
needs three values, each with its traffic (HARs, attach sessions or `learn` values).
AnyUse fits it on another result's name from your first search and sends that search
to the site. It needs a fourth value only when the site's search does not answer that
name. A typed `--expect` cannot prove it
([COMPILE.md](COMPILE.md#chains-pages-and-path-parameters)).

The simplest recorder is your own browser. Open DevTools → Network, run the
search, then right-click → **Save all as HAR with content** (`serde.har`). Firefox's
and Safari's HAR exports work too. They include your cookies; AnyUse reads only their
names and replays the request without them. Note what the page showed; that becomes
`--har-expect`: at least three labels from the result list, in the order the page
showed them, separated by `|`. For a search that ends on an item's page, note the
values that page showed instead, in page order. For the two
other values (`regex`, `tokio`) either save a HAR each the same way, or just search
them and note what the page showed: step 2 takes those as `--expect`.

Instead of exporting HARs you can let AnyUse watch a browser while you, or an
agent, do the search: start Chrome with a throwaway profile and DevTools on
loopback, then follow [ATTACH.md](ATTACH.md). AnyUse only observes that browser.
Attach four searches with four different values: one to compile from, one to choose
between candidate requests, and two held out for the proof (three give at most
`checked`, never `proved`).

```sh
# macOS
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$(mktemp -d)"
# Linux (google-chrome, chromium or chromium-browser, whichever you have)
google-chrome --remote-debugging-port=9222 --user-data-dir="$(mktemp -d)" &
```

### Or let an agent find the page once

`anyuse learn` has an agent do the task once, for the first value, in a container
browser, and shows you what it found. You confirm it; the other values are then
opened without a model and go through the same proof.

Before running it, install the `cdp` extra with
`python -m pip install -e '.[cdp]'` and configure a model. For example, install
the `claude` CLI, log in, and set `export ANYUSE_MODEL_PROVIDER=claude-cli`
([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md)). Set `ANYUSE_LEARN_ENDPOINT`
to the DevTools URL at a container browser's own IP, reachable from the host.

`learn` refuses a loopback browser endpoint, so the browser must run in a container
whose own address the host can reach:

* **macOS:** Apple's `container` CLI (Apple silicon, macOS 26). Docker Desktop for Mac
  does not work here: it publishes the port on loopback, and the container's own
  address is not reachable from the Mac. On a Mac without Apple's `container`, record
  with a HAR or attach instead (step 1).
* **Linux:** rootful Docker Engine (Docker as root, the usual install): the container's bridge address is
  reachable. Rootless Podman and Docker Desktop for Linux keep containers on a private
  network, so use a HAR or attach there.

```sh
container run -d --rm --name learn-chrome chromedp/headless-shell:latest     # macOS 26, Apple silicon
# Linux: docker run -d --rm --name learn-chrome chromedp/headless-shell:latest
#        docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' learn-chrome
export ANYUSE_LEARN_ENDPOINT="http://<the container's IP>:9222"

anyuse learn "search this site for {q}" \
    --url https://crates.io --values serde,regex,tokio,clap --confirm --dry-run
```

It prints the candidate page, URL pattern and a preview of the result list, then
asks. If you say no, nothing is learned or persisted; `anyuse learn-confirm <id>
--values regex,tokio,clap` continues later without running the agent again. Four
values reach "proved" (one picks the request, two are held out for the proof);
with three the tool is only "checked", which the MCP profiles do not run. Drop
`--dry-run` to persist, which still needs the proof to pass. `learn` refuses a
loopback browser endpoint and a browser with pages open, because it clicks and
types.

Give fewer than four values and `learn` proposes the rest: other results on the
first value's page (searching `serde` shows `serde_json`, a valid input of the same
kind; navigation and footer links are not results), else one call to the configured
model asking for more values like yours. A generated value is never the one the agent
acts on; it is opened, landing-checked and proved like yours, and kept only when its
page shows a result list that differs from the first value's and is not the site's own
empty page. `learn` learns that page once, from two searches for made-up inputs through
the confirmed address (`ANYUSE_EMPTY_PROBE=off` sends none), in whatever language the
site writes it. A "0 items" line the first value's page shows too (a cart, for example)
is part of the site, not an empty result. It is printed as
generated and recorded in the tool's proof and `learned_from.generated_values`.
`--no-generate` turns this off.

```sh
anyuse learn "search this site for {q}" --url https://crates.io --values serde,regex --confirm
```

When no compiled request reproduces the page but the browser replay does, `learn`
saves the tool anyway, served by the browser replay: `anyuse workflow list` marks it
"served by the browser replay (no compiled request)", `anyuse workflow play NAME
--param q=VALUE` runs it in the browser named by `ANYUSE_CDP_ENDPOINT` (or
`ANYUSE_LEARN_ENDPOINT`), and both MCP profiles serve it while one is configured.
Every call opens the page (seconds; no model, except the checks a replay may ask one
for, listed below). `robots.txt` applies as for a compiled tool. Learning the same task
again never replaces a compiled request whose proof passed with such a tool: when this
run's request fails (a 503, fewer values), the saved one is kept and `learn` says so.
Run `anyuse workflow delete NAME` first to replace it.

## 2. Compile and read the proof

```sh
anyuse workflow compile crates_search --explain \
  --from-har serde.har --har-param q=serde --har-expect 'serde|serde_json|serde_yaml' \
  --from-har regex.har --har-param q=regex --har-expect 'regex|regex-syntax|regex-automata' \
  --from-har tokio.har --har-param q=tokio --har-expect 'tokio|tokio-util|tokio-stream'
```

Or record once: export one HAR, then search two other values on the site and type
what the page showed. The proof is the same; the typed labels are its ground truth.

```sh
anyuse workflow compile crates_search --explain \
  --from-har serde.har --har-param q=serde --har-expect 'serde|serde_json|serde_yaml' \
  --expect regex='regex|regex-syntax|regex-automata' \
  --expect tokio='tokio|tokio-util|tokio-stream'
```

Abridged output:

```
Prove against crates.io now? This sends 4 requests to crates.io (robots.txt allows /api/v1/crates). [Y/n]: y
Extraction: ok — winning request explains the screen
  GET https://crates.io/api/v1/crates?page=1&per_page=10&sort=relevance&q={q}
  extract: {'items': 'crates[*].id'}
Why this request:
  -> crates.io/api/v1/crates          coverage=1.00 bytes=2901 path=crates[*].id
     play.rust-lang.org/meta/crates   coverage=0.20 bytes=6317 path=crates[*].id
     crates.io/api/v1/site_metadata   coverage=0.00 bytes=716  path=-
Fresh-input proof: PASSED (2/2 cases) — passed on 2 fresh value(s) not present
  in the recording (coverage >= 0.8, UI order preserved)
  [ok  ] 'regex': coverage=1.00 order_ok=True ui=9 api=10
  [ok  ] 'tokio': coverage=1.00 order_ok=True ui=10 api=10
Permission: may prefer — robots.txt for crates.io allows
  anyuse-compiled-workflow on /api/v1/crates; host acknowledged
✓ Persisted as the preferred implementation
```

For a new host, AnyUse reads its `robots.txt`, then asks before the proof replays
the compiled request against it; `--yes` answers for scripts.

`--explain` lists every candidate request with its coverage, so you can see why
one won. "Proved" means: replayed for two values that were never in the
recording, and for each, at least 80 % of the visible labels matched in the same
order. It does not mean the endpoint is stable, that every input behaves this
way. `--paginate` also reports how to request page
2, and refuses to guess when the response gives nothing to go on.

The stored file carries the template and its proof.

## 3. Replay

```sh
anyuse workflow play crates_search --param q=rand
```

`rand` was never recorded: the compiled request runs with the new value.

The compiled `api` step sits above the browser and UI steps and exists only after an
explicit `compile`. `ANYUSE_API_MODALITY=off` turns it off.

## 4. Break it on purpose

The offline quickstart ([QUICKSTART.md](QUICKSTART.md)) changes its fixture's API
shape and shows the result. With the fall-back off, the tool fails with a reason and
returns zero items. It never returns an empty list that would look like "no matches",
and it never falls back to a model by itself. With the fall-back on, the tool heals
itself from plain reads of its server-rendered result page, without a browser, and
without a model unless one is configured and the page alone cannot say which list is
the answer ([COMPILE.md](COMPILE.md#healing-without-a-browser)). When the site then removes
the data, the tool fails with zero items again.

## 5. Hand it to an agent

Register the MCP server with Claude Code or Codex as described in
[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md) (the `meta` profile). The agent then
finds and calls the tool through the profile's fixed tools:

* `list_sites`: `crates.io` is listed, with its operations' proof status;
* `list_operations {"site": "crates.io"}`: the `crates_search` operation and its parameter `q`;
* `call_operation {"site": "crates.io", "operation": "crates_search", "args": {"q": "tokio"}}`:
  one HTTP request, the result list, 0 model calls.

Details: [MCP_META.md](MCP_META.md). With the `default` profile each tool is listed
under its own name instead ([MCP_WORKFLOWS.md](MCP_WORKFLOWS.md)).

## What leaves your machine

| step | what goes out | where to |
|---|---|---|
| recording | the requests the page itself makes | the site you chose, from inside the container |
| `robots.txt` check | one GET per host per day (cached) | that host |
| compile | nothing; it is local analysis | — |
| proof | the compiled request, once per fresh value, then twice with a made-up input to learn the site's empty page | that host |
| replay | the compiled request; for an empty answer at most three more searches, and up to five once when the tool learns the site's empty page (a tool proved before this release, or after the site changed that page) | that host |
| heal (after the compiled request failed on an answer from the site, or was taken out as gone) | about 7 to 10 GETs of the result page and the request, one more for a made-up input when the list changed since the last good answer, one or two more when it puts back in service a request whose list changed, two more when the request changed; a failed attempt is not repeated for 30 minutes | that host |
| check of a request out of service (10 minutes after it was taken out, doubling to a day) | the call's request; one more for the last good answer's input when the call asked another; when that answer changed, one browser replay, one or two reads of the result page, or one made-up search | that host |
| gone check (after 4 calls in a row the site did not answer, over at least 30 minutes) | one GET of the site's own page, at most once every 30 minutes; none when the browser replay or the agent answered | that host |

No credentials, cookies or tokens are sent: cookies are cleared and credential
headers are refused. The one exception is a search provider's public key that the
site's own page states by name: the tool reads it from that page, without
cookies, on every call (two requests per call: the page, then the provider) and never
stores it ([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md#provider-keys)). Only `GET` and `HEAD`
are emitted, plus such a tool's search POST to the site's provider once you
acknowledged the provider's host. No model is called in a proof. A compile asks
one only where the site's own evidence leaves a question open (which field of a
search result is its name, which line of an item's page shows a value); a heal only
when the result page read now does not say which list is the answer; the check of a
request out of service only when neither its result page nor a browser replay shows
the list; a replay only for an empty answer the site's own pages do not settle, or for
an input that a short answer holds inside a longer Chinese, Japanese, Korean or Thai
word. All of them ask only when you configured a model check (`ANYUSE_MODEL_CHECKS`,
[MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)).

## robots.txt and challenge pages

A path `robots.txt` disallows is not proved or replayed. A challenge page
(CAPTCHA, 403, 429) is recorded as blocked and handed to you; it is not retried.

## If it does not compile

| you see | it means | what to do |
|---|---|---|
| `robots.txt disallows` | the site forbids this path | stop, or ask the site |
| `no result list` / selector never matched | the recording did not capture the results | record again after the results have loaded |
| `every one of the N visible labels also appears for the OTHER queries` | the page ignored the parameter, or the selector caught only navigation | check `final_url` in the recording |
| `not carried by any path segment or query value` | the typed value is not in the URL | the search is probably a POST: see [Read-only POST](COMPILE.md#read-only-post-opt-in) |
| `signed/session-bound query parameter` | the URL carries a token | not compilable, by design |
| `no request explains the screen` | no JSON, payload or selector found | try another workflow on that site |

## More ways in

### Deterministic browser replay

On macOS with Apple's `container`:

```sh
container run -d --rm --name chrome chromedp/headless-shell
export ANYUSE_CDP_ENDPOINT=http://$(container ls | awk '/chrome/{print $6}' | cut -d/ -f1):9222
```

On Linux with rootful Docker Engine:

```sh
docker run -d --rm --name chrome chromedp/headless-shell
export ANYUSE_CDP_ENDPOINT="http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' chrome):9222"
```

The container's private address must be reachable from the host. Use a HAR or
attach with rootless Podman or Docker Desktop for Linux, as described above.

With this set, a workflow whose input is in its URL can be replayed in the
container browser with new inputs, no model calls and a postcondition. It takes
seconds instead of milliseconds. Unset the variable to turn it off; it never
starts a browser on your desktop.

### Your own site and robots.txt

By default (`respect`) a disallowed path is never compiled or replayed. For your
own site, or with the operator's permission:

```sh
anyuse workflow acknowledge-host example.com \
    --note "my own site" --ignore-robots --reason "I operate this host"
```

The reason is required and shown in every proof and MCP listing.

## Next

* [COMPILE.md](COMPILE.md): what each step of the cascade is and what it costs
* [EVIDENCE.md](EVIDENCE.md): measured results, with sample sizes
* [TEST_POLICY.md](TEST_POLICY.md): running the test suite safely
