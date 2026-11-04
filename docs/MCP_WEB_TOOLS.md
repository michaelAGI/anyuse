# Saved web tools over MCP, with a verified model fallback

A saved web tool is a compiled demonstration that replays a site's own search or
product page in a guarded browser and checks every answer. The MCP server serves each
ready tool to agents; when a replay misses, an optional model fallback answers the
same question on the same site, and only an independently verified answer comes back.

Code: `execution/web_tool_call.py` (the route), `mcpserver/server.py` and
`mcpserver/meta_tools.py` (the wiring). Tests: `tests/test_mcp_web_tools.py`.

## The tools

Every `workflow_<id>/compiled_demo.json` in the workflow store is served when its
status is `ready` (the fresh-value proof passed), its `tool_sha256` matches the file,
and it is not a draft. Listing reads files only; nothing is created.

| profile | what the agent sees |
|---|---|
| `default` (and `full`) | one typed tool `<host>__<workflow>` per saved tool (the host's dots as underscores, `shop_example__lookup`; the older `<host>.<workflow>` still calls it, see [MCP_WORKFLOWS.md](MCP_WORKFLOWS.md#tool-names)); input `{"value": string, 1–300 characters}`; a typed output schema; `_meta.anyuse` with `kind: "web_tool"`, `tool_sha256`, `tool_version`, `workflow`, `browser_only` |
| `meta` | the same tool as an operation of its host in `list_sites` / `list_operations` (parameter `value` with an example, `returns`, `proof`, `risk_class: read_only`, `ready` or `why_not`, `fallback: on/off`); `call_operation` runs it. The four tool definitions do not change |

Tool descriptions are built from enums and counts only (workflow, version, number of
proof values, whether the independent check accepted them). Page text and recorded
values never enter them. A host that only answers a real browser carries
`browser_only: true`; these tools are the route that works for it.

## The call

1. **Value check.** `value` must be 1–300 characters, else `refused` / `input`.
2. **Browser.** The container browser is `ANYUSE_LEARN_ENDPOINT`. If none is set, or
   it is a loopback address or a browser with pages open, the call returns
   `no_browser` and nothing is sent.
3. **Refusals before any request:** government hosts (with `ANYUSE_REFUSE_GOV_HOSTS` on), an earlier traffic violation
   (`hard_stop`), a site that blocked an earlier call (`site_blocked`), a site whose
   daily share is used (`daily_cap`: 60 requests per call, 200 per site per day, only
   ever lowered by `ANYUSE_WATCH_*`).
4. **Health.** A browser that does not answer is `failed` / `infrastructure`; nothing
   is sent.
5. **The replay**, through the same guarded browser as `anyuse watch run`
   ([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md)), in a new anonymous browser context with a
   per-host pacer (10 s or the site's crawl-delay). The attribute verifier runs inside
   the replay when `ANYUSE_ATTRIBUTE_VERIFY` is on.
6. **The route's traffic ledger** (`<data dir>/web_tools`) books every contact.

**The answer:**

| status | when | fields |
|---|---|---|
| `ok` | the answer was accepted | `result`; `verification`: `verified` or `plain` (verifier off); `source`: `replay`, `ladder` (a repaired revision answered) or `fallback`; `evidence` (URL, page title); `model_calls`, `cost_usd`, `requests` |
| `needs_review` | the verifier's two independent sources disagree | `review_view`: the answer and both sources' evidence; never accepted, the caller decides |
| `withheld` | `reason_code` `unverified`, `incomplete` or `known_wrong` | no answer is returned |
| `failed` | `check_failed`, `conflict`, `blocked`, `rate_limited`, `infrastructure`, `traffic_violation`, `not_run`, `error` | the reason |
| `refused` | `no_browser`, `input`, `refused`, `hard_stop`, `site_blocked`, `daily_cap`, `contact_paused` | nothing was sent |

A replay ends in "no results" when the page is the site's own empty page for the value,
or states it in one of the listed languages. The site's empty page is learned when the
tool is proved, from two replays of made-up values (`ANYUSE_EMPTY_PROBE`; saved with
the tool, so its `tool_sha256` covers it), and counts only when it names the value and
the page shows nothing else new. It sends no request and asks no model. A tool saved by
the current version also counts a statement that names the value in its own line in the
languages the result contract reads (Japanese, Chinese, Russian and others), before the
listed phrases. When neither settles the page, the call may ask the configured check model
([MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md#small-model-checks)) which line says the search found nothing. An
error phrase that stands only in a result's title, or in a line the site's empty and result pages share, is no error
page for it.

Under `meta` these map to the classes `needs_review`, `withheld`, `no_browser`,
`input`, `blocked`, `rate`, `refused` and `failed`, each with a `next` step
([MCP_META.md](MCP_META.md)).

**Known wrong answers are not `ok`.** Every replay is checked against the verdict
store (`<data dir>/web_tools/verdicts.jsonl`); an answer recorded as wrong becomes the
miss `known_wrong`. `anyuse watch run` checks the same store.

## The model fallback on a miss (`ANYUSE_WEB_FALLBACK`)

On by default together with the attribute verifier (about US$0.50 and US$0.07 per
1,000 runs); `off` / `0` turns either off. Without a model key both fail closed: the
verifier withholds what only a model could settle
(`reason_code: verifier_model_unavailable`), and the fallback is not tried
(`fallback: {tried: false, reason_code: no_model_configured}`).

1. The replay itself asks no model. The attribute verifier inside it may ask its
   configured model about an attribute its rules leave open, and the configured check
   model (`ANYUSE_MODEL_CHECKS`) whether a page states an empty search and, for a tool
   saved by the current version, about its result page and buy box, on this route only:
   `anyuse watch run`, a proof and a re-proof ask neither. The answer's `model_calls`
   and `cost_usd` count both.
2. On a **miss** (`failed`, `incomplete`, `known_wrong`) an agent answers the same value
   live on the same site, with a **read-only** tool set (open, search, click, select,
   scroll, back, read; no typing outside a search box, no forms, no login), inside the
   replay's own guard and budget, starting at the tool's search URL for the value.
3. The agent refers to result links by element number; references are resolved
   against the page it ended on, and an ambiguous one withholds the answer.
4. **The answer must match the input**: a product's name holds at least 75 % of the
   input's tokens and carries the fields the tool reads; a list needs a result title
   that does, or the page's heading or result count must name the input.
5. **The independent verifier** reads the page the agent ended on.
6. **Only a passed answer is `ok`**, with `source: "fallback"`. A disagreement of two
   sources is `needs_review`; anything else is `withheld`. A `no_results` answer is
   never accepted.

**Not sent to the fallback:** `needs_review` (it goes to you), blocks, refusals, rate
limits, browser outages, and a question the tool found ambiguous (several results
answer the input equally, for example a brand name instead of a product).

**Cap:** at most 8 model calls per fallback run (up to 6 agent decisions plus the
verifier's second stage), every request booked under `ANYUSE_SPEND_DAILY_USD`. Cost:
nothing when the replay answers; about US$0.003 per miss.

**A verified answer is booked** in the verdict store and, for tools saved by a current
or recent version, as the input's last known good answer for the repair steps. The next call for
that input replays through the repair steps with 0 model calls: a repaired revision answers
only when it returns the same object as the verified answer.

## Limits

- The fallback holds back correct answers wherever the verifier does.
- Repairing a tool after a booked answer needs a tool saved by a current or recent version.
- Result names in Cyrillic, Greek, Arabic, Hebrew, Chinese, Japanese, Korean, Thai and
  other non-Latin scripts are read word by word by current tools. The
  marks of Thai, Devanagari and the kana count (ガイド is not カイト); the accents of
  letters with case do not. In a script without spaces a word of the value is named by
  a name that starts with it (東京 by 東京駅), not by one that ends with it (京都 in
  東京都). An older tool reads only
  the Latin letters and digits of a name, so it matches such a name only through them;
  compile its demonstration again to update it.
- Tools saved by the current version read the site's own evidence before the hand-written
  word lists, which decide only what is left. Two hosts are one site when they share
  the registrable domain. A block of results that shows the same items on the start
  page is never the result list, whatever its id or class is called; a block that shows
  other items there without naming the value more is judged by its id or class. A text the page
  gives every card (a "quick view" label), a date the page marks, and a word run every
  title shares that a colon, a bar or an arrow sets off are no part of a title. A link
  parameter that only one result link carries, or with another value than the others,
  marks a placement. The parameter that carried the typed value is the site's search.
  A result naming the value next to a word the list repeats with other products is an
  accessory, unless another result puts another word in that place. A word of the value
  where the site's titles differ (a colour in any language) must be on the product. Availability is read in the buy-box line the site
  rendered the structured value with, prices by the site's own labels and its
  strike-through, and the answer's URL by which parameter the page's own links use to
  tell variants apart. An older tool keeps its rules; compile its demonstration again
  to update it.
