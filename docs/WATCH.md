# `anyuse watch`: change monitoring with compiled tools (experimental)

> **Experimental.** The command has been run live on only a few sites so far. Expect
> drift: when a site changes, the replay is reported as drift and the tool has to be
> made again.

A watched item is **one compiled tool and one input**: a product to look up or a term
to search. Every run replays the tool with **0 model calls**, stores a dated snapshot,
and logs what changed since the last good snapshot.

```
anyuse watch add TOOL INPUT [--every 1d]    watch INPUT with the compiled tool TOOL (a pinned copy is kept)
anyuse watch run [--dry-run] [--explain]    replay every due item, snapshot, diff, log
anyuse watch log [--item ID] [--runs]       the changelog (changes, drift, baselines); the run log
anyuse watch list | remove ITEM
anyuse watch install-hook --print           print the advisory agent hook (never installed)
```

## What TOOL can be

- **A compiled web tool**: a name from `anyuse workflow list`, or the path to its
  `compiled_api.json`. Each run sends the tool's proved request, with the checks
  `anyuse workflow play --no-fallback` makes: a passed proof, an acknowledged host and a
  read-only request. It needs no browser, except a tool that is served by its browser
  replay (it has no compiled request). INPUT is the tool's input (`q` for most tools);
  a tool with more inputs takes the others with `--param KEY=VALUE`.
- **A saved browser tool** (made by `anyuse agent run --compile`): its name, or the
  path to its `compiled_demo.json`. Each run replays it in the guarded browser, so
  `watch run` needs `--browser` (or `ANYUSE_WATCH_BROWSER`): the DevTools endpoint of a
  headless browser, such as `http://127.0.0.1:9222`.

With the `crates_search` tool from [QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md):

```
anyuse workflow list                    # the names TOOL can be
anyuse watch add crates_search serde
anyuse watch run
```

A wrong TOOL is refused with the names you have. A tool that can never run (its proof
did not pass, or its request is not read-only) is refused when you add it.

`watch add` keeps a pinned copy of the tool, so every run replays the same tool. If you
recompile the tool later, the item keeps the old copy: run `anyuse watch remove ITEM`
and `anyuse watch add` again to watch the new version.

## Snapshots and diffs

Each run stores the structured output (a product's name, price, currency,
availability and URL, a result list's titles and URLs, or a compiled web tool's
extracted result) together with its **evidence**: the page and search URL (or the
request URL), where each field came from (`jsonld`, `microdata`, `og`, `h1`, `text`),
the time and the tool's digest. A deterministic
diff against the item's last good snapshot goes into the changelog (`CHANGELOG.md`
for people, `changelog.jsonl` for tools) with both sides' evidence.

| change | how it is found |
|---|---|
| a field (name, URL) | whitespace- and case-folded text; the URL without its fragment |
| a price | the number (0.005 tolerance) and the currency; the delta and the percentage |
| availability | schema.org values and visible statements normalised to one vocabulary (`in_stock`, `limited`, `store_only`, `out_of_stock`, `preorder`, `backorder`) |
| a list | results keyed by URL (by title when a result has no link): added, removed, retitled, moved |
| presence | "listed" ↔ "no results" (the site's own empty page or statement, checked by the tool) |
| same-title variants | keyed by URL: a variant added, removed or retitled, or its shown price changed |
| a compiled web tool's result | per extracted name: a list's items added, removed, retitled or moved (a record keyed by its URL, with its own fields compared), a value changed |

**Drift is never a content change.** A replay that failed, could not read a result,
was refused, or found conflicting statements for a field (a **CONFLICT**: for
example two structured availabilities that disagree) is logged as DRIFT
("re-demonstrate"). Its snapshot keeps the diagnosis, and the last good snapshot
stays the baseline. For a compiled web tool, a request the site answered, but not as
it did when the request was proved, is DRIFT ("recompile": record the search again,
recompile, and watch the new version). A refused one, for example on a host that is
not acknowledged, says why. When the pre-send policy refuses the destination, the
entry says **REFUSED (nothing sent)** with the setting or the address that stopped it;
nothing needs recompiling. A chain that opens the result named by the input, whose
search lists no result of that name, is "no results", not drift.

**An outage is not drift.** When the site does not answer (a refused connection, a
timeout, a name that does not resolve, or a server error such as 503), the entry says
**SITE DID NOT ANSWER**: not a content change, not drift, and nothing to recompile.
The item's status is `unavailable`, the last good snapshot stays the baseline, and the
next `watch run` tries again.

**`needs_review`** (only with `ANYUSE_ATTRIBUTE_VERIFY` on): the page's two
independent sources disagree and no rule says which is wrong. The answer is not
accepted, the baseline stays, and the run counts it.

**The verifier asks no model in a watch run**, even with a verifier model configured.
An attribute only that model could settle is withheld, and the entry is drift, never
a change. A call of the same tool over MCP may ask the model
([MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md)).

**Known wrong answers.** Every replay is checked against the web tools' verdict
store (`<data dir>/web_tools/verdicts.jsonl`, shared with the MCP web-tool route,
[MCP_WEB_TOOLS.md](MCP_WEB_TOOLS.md)). A replay that matches an answer recorded as
wrong becomes **KNOWN WRONG** drift: not accepted, not a baseline, and named in the
changelog, the run summary and the hook line.

`watch run --dry-run` replays and prints what the changelog would say, and writes
nothing to the store except the traffic ledger.

## Traffic

A browser tool's replay runs in a new anonymous context of the guarded browser
([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md)), limited to the tool's declared hosts. A
compiled web tool sends its one request (or short chain) the way `anyuse workflow play
--no-fallback` does.

- **Caps:** 60 requests / 8 MB per run, and 200 requests / 24 MB per site and UTC
  day, dry runs included. `ANYUSE_WATCH_*` settings can lower them.
- **At most one replay per item per hour**: `--every` below 1 h is refused. The
  traffic ledger (`guard/traffic.json`) books every contact, dry runs included.
- A site that blocks is not contacted again; a traffic violation stops every run
  (`guard/HARD_STOP.json`).

## `--explain` (off by default)

Only when a diff is non-empty, a model summarises it from the diff's own lines, not
from the page text:

- `--explain-backend openjev` (default): a self-hosted [OpenJev](https://github.com/razorback16/openjev) server
  (`ANYUSE_OPENJEV_URL`) picks the most important line of the diff, or answers
  "nothing important changed (only cosmetic: an order, a format or a URL detail)".
  The answer is added to the changelog entry; the diff itself is never hidden.
- `--explain-backend cerebras`: one short summary through the model adapter's
  Cerebras preset, with a run budget of US$0.10.

Every call is logged per run (`runs.jsonl`: backend, model, calls, tokens, cost,
errors).

## The agent hook (printed, never installed)

`anyuse watch install-hook --print` prints a Claude Code `SessionStart` hook block
(and a Codex block, not yet tested with a Codex session). Its command,
`anyuse watch hook-run`, reads only the local changelog — no network, no browser, no
model — and tells the agent in one line what changed or drifted in the last 24 h, and
which sites did not answer.
It always exits 0 and never blocks. `install-hook` without `--print` refuses.

## What the replayed tools check

The watched tools refuse to guess:

- conflicting availability statements on a product page are a CONFLICT for that
  field, with both sides' evidence;
- a product's own microdata properties are read, not a nested brand's;
- a result card's link is taken at the compiled title path, or a link with the same
  role; a card with neither is reported unread;
- result links no compiled card structure holds are reported as unread
  (**INCOMPLETE**), never skipped silently;
- a result without a same-site link is listed with its title and no URL;
- the tool's declared hosts on the site's registrable domain are allowed; other
  hosts stay refused.

## Limits

- A product page whose structured data disagree while its text is silent drifts on
  every run until it is re-demonstrated or the site fixes its data.
- The Codex hook block is untested.
