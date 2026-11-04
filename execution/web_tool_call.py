"""SAVED BROWSER-REPLAYED WEB TOOLS over MCP, and the MODEL FALLBACK on a miss (docs/MCP_WEB_TOOLS.md).

A saved web tool (a compiled demonstration tool, versions 5-15: ``demo_compile`` / the agent harness's
``compiled_demo.json``) runs through the CLI (``anyuse watch run --browser``) and over MCP, next to API recipes
(``compiled_tools.run_compiled``) and desktop tools. This module is the one call path both MCP profiles use:

* **the tools**: every ``workflow_<id>/compiled_demo.json`` of the workflow store whose status is ``ready`` (the
  fresh-value proof passed) and whose ``tool_sha256`` matches its tool (a changed file is not served). Typed name
  ``<host>__<workflow>`` (the API tools' rule, mcpserver/tool_names.py; a name shared with an API tool or another
  saved tool gets a suffix hashed from the tool's directory; the older ``<host>.<workflow>``, or
  ``<host>.<workflow>.<sha12>`` when an API tool had that one, is an alias), one input ``value``; a host the
  registry marks browser-only carries ``browser_only: true``.
* **the call** (``WebToolRoute.call``): the saved tool replays in the guarded CONTAINER browser through the CLI's own
  replay (``watch_live.make_replay``: robots.txt on every host, the pacer's >= 3 s between documents, GET only, the
  per-site request and byte caps, the NEVER_CONTACT refusals (and the government ones with ANYUSE_REFUSE_GOV_HOSTS), a
  new anonymous context), after the browser
  supervisor's health probe and the process's per-host pacer (``repair_pace.PACER``); the route's own traffic ledger
  (a ``watch.WatchStore`` at ``<data dir>/web_tools``) books every contact against the watch's daily per-site caps, a
  site that blocked is never contacted again and a traffic violation stops the route (``guard/HARD_STOP.json``). The
  attribute verifier runs inside the replay when ``ANYUSE_ATTRIBUTE_VERIFY`` is on, as on the CLI path; the call is a
  tool call (the ``product`` scope of ``model_check``), so the verifier may ask its configured model about an attribute
  the rules leave open, and a version-15 tool's checks may ask the configured check model about its result page and
  buy box, where ``anyuse watch run`` asks none; the call's ``model_calls`` and ``cost_usd`` count both. A re-proof
  or another input's check the repair ladder runs inside the call asks neither (``attribute_verify.no_model``).
* **the answer**: ``ok`` (``verification``: ``verified`` when the verifier accepted it, ``plain`` when it is off - as
  the run record says), ``needs_review`` (``review_view``: the answer with both sources' evidence; never accepted),
  ``withheld`` / ``failed`` / ``refused`` with a typed ``reason_code``. No container browser endpoint attached
  (``ANYUSE_LEARN_ENDPOINT``, or one that is not recognisably a throwaway browser): the typed refusal ``no_browser``,
  nothing is sent.

**The model fallback** (``ANYUSE_WEB_FALLBACK``, ON by default - about US$0.50 per
1,000 runs; without a configured model it is never tried: ``no_model_configured``; the cascade
principle): replay at 0 model calls -> on a MISS (``failed``, ``incomplete``, ``known_wrong``; never ``needs_review``,
which goes to the caller, and never a block, a refusal, a rate limit or a browser outage) the agent harness answers the
same value live with the configured model on the same site, with its READ-ONLY browser tool set for a lookup (``LookupToolbox``:
open, search, click, select, read; forbidden controls refused) in the same guard stack -> the INDEPENDENT verifier
(``attribute_verify.check`` on the page the agent ended on, plus an identity rule against the input) -> only a passed
answer is ``ok`` with ``source: "fallback"``; otherwise ``needs_review`` / ``withheld``. At most ``MAX_CALLS`` model
calls per run (the agent's decisions and the verifier's second stage together), every request of them under the one
daily spend cap (``ANYUSE_SPEND_DAILY_USD``). A verified fallback answer is BOOKED: the verdict store (``correct``,
source ``checker``, reference ``web_fallback``), the repair ladder's store (the input's last known good answer: its
anchors for tier 1, the gate's reference) and the route's miss ledger. With the fallback on, a tool whose input has a
booked answer (or that already has a repaired revision) replays through ``repair_ladder.run_with_ladder`` (tiers 0-1:
0 model calls; the gate's checker agrees only with a repaired answer of the same object as the verified fallback
answer), so the next call is 0 calls again. With the fallback OFF the route is the CLI path byte for byte: no verdict
store, no ladder (as ``anyuse watch run``).

Everything is injectable (the replay, the fallback, the probe, the endpoint check, the policy, the model, the stores):
``test_mcp_web_tools.py`` runs it with fakes and on recorded pages (``dom_page``).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import threading
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

FORMAT = "agent_harness.web_tool/1"
TOOL_FILE = "compiled_demo.json"
SETTING = "ANYUSE_WEB_FALLBACK"
ENDPOINT_ENV = "ANYUSE_LEARN_ENDPOINT"
KIND = "web_tool"
#: the model calls ONE fallback run may make: the agent's policy decisions and the verifier's second stage together
MAX_CALLS = 8
#: of those, kept for the verifier's second stage (the agent takes at most MAX_CALLS - VERIFY_RESERVE decisions)
VERIFY_RESERVE = 2
#: the agent's own run budget (US$); the daily cap governs the spend across runs
AGENT_MAX_USD = 0.05
#: the fallback agent's run budget on a provider other than Cerebras (a decision costs about US$0.003-0.05 there);
#: MAX_CALLS still bounds the decisions and the daily cap the total
OTHER_AGENT_MAX_USD = 0.50
FEATURE = "web_fallback"
REF = "web_fallback"
VALUE_MAX = 300
#: the outcomes a fallback may answer; anything else (needs_review, a block, a refusal, a rate limit, an outage) not
MISS_STATUSES = ("failed", "incomplete", "known_wrong")
FALLBACK_WORKFLOWS = ("product_lookup", "site_search")
#: a miss that is a determination about the QUESTION, not a breakage of the tool: the tool's own ambiguity check found
#: several results that answer the input equally (a brand name typed into a shop's search: the fallback picked one
#: product, and the reader could not call it correct). An ambiguous query has no single answer for a
#: fallback to supply, so it is not asked.
QUESTION_MISS = re.compile(r"\bambiguous\b", re.I)
#: a product answer's identity: the share of the input's tokens its answered name must hold (the tool's own bar)
IDENTITY_SHARE = 0.75
#: the actions of the read-only lookup tool set (no typing outside a search box, no form, no login)
LOOKUP_ACTIONS = ("open", "search", "click", "select", "scroll", "back", "read_text", "read_table", "extract_fields")

OK, NO_RESULTS, NEEDS_REVIEW, WITHHELD, FAILED, REFUSED = "ok", "no_results", "needs_review", "withheld", "failed", \
    "refused"
SOURCE_REPLAY, SOURCE_LADDER, SOURCE_FALLBACK = "replay", "ladder", "fallback"

INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string", "minLength": 1, "maxLength": VALUE_MAX,
                             "description": "the input for this tool: a product name or a search term"}},
    "required": ["value"], "additionalProperties": False}
OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "status": {"type": "string"}, "result": {},
                   "source": {"type": "string"}, "verification": {"type": "string"},
                   "reason_code": {"type": "string"}, "review_view": {"type": ["object", "null"]}},
    "required": ["ok", "status"], "additionalProperties": True}


def verdict_store_path() -> str:
    """The product's verdict store for saved web tools' answers (``<data dir>/web_tools/verdicts.jsonl``): what the MCP
    route and ``anyuse watch run`` both check every replay against (a known wrong answer is never returned as ok)."""
    from anyuse.paths import anyuse_data_path
    return os.path.join(str(anyuse_data_path("web_tools")), "verdicts.jsonl")


def known_wrong_check(store: Any, tool: Dict[str, Any], value: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    """Silent known wrongs 0 (0 cost): a run record whose answer (``ok`` or ``no_results``) matches an
    answer the verdict store holds as WRONG becomes the typed miss ``known_wrong`` (``verdict_store.flag``: not
    accepted, the answer kept only as evidence). A store that cannot be read changes nothing (it is a local file)."""
    from anyuse.execution import verdict_store as VS
    try:
        return VS.check(store, tool, value, rec)
    except Exception:                                  # noqa: BLE001 - an unreadable store flags nothing
        return rec


def fallback_enabled(explicit: Any = None) -> bool:
    """The one switch (``ANYUSE_WEB_FALLBACK``, ON by default; about US$0.50
    per 1,000 runs); ``explicit`` overrides it (tests, benches)."""
    if explicit is not None:
        return bool(explicit)
    try:
        from anyuse import settings as S
        return bool(S.get(SETTING))
    except Exception:                                  # noqa: BLE001 - an unreadable setting spends nothing: OFF
        return False


#: the fallback is on by default, but without a model key it is never tried -
#: the miss is returned with this typed reason, no site contact, no crash
NO_MODEL = "no_model_configured"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def tool_sha(tool: Dict[str, Any]) -> str:
    """``harness_compile.save_web_tool``'s digest of a tool."""
    return hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def host_of(tool: Dict[str, Any]) -> str:
    s = tool.get("search") or {}
    u = str(s.get("template") or s.get("start_url") or tool.get("start_url") or "")
    return (urllib.parse.urlsplit(u).hostname or str(tool.get("site") or "")).lower()


# ---------------------------------------------------------------------------
# the saved tools and their typed specs
# ---------------------------------------------------------------------------

def _base_dir(storage=None) -> str:
    if storage is not None:
        return str(getattr(storage, "base_dir", "") or os.path.dirname(str(storage.get_workflow_dir("x"))))
    from anyuse.memory.storage import WorkflowStorage
    return str(WorkflowStorage.default_base_dir())


def saved_tools(storage=None) -> List[Dict[str, Any]]:
    """``[{name, tool, record, path}]``: every ready saved web tool (by path alone: no workflow is parsed, nothing is
    created). A record whose tool does not match its ``tool_sha256``, a draft, a failed proof: not served."""
    out: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(_base_dir(storage), "workflow_*", TOOL_FILE))):
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        tool = rec.get("tool") if isinstance(rec, dict) else None
        if rec.get("format") != FORMAT or rec.get("status") != "ready" or not (rec.get("proof") or {}).get("passed"):
            continue
        if not isinstance(tool, dict) or not tool.get("workflow") or rec.get("tool_sha256") != tool_sha(tool):
            continue
        out.append({"name": str(rec.get("workflow") or ""), "tool": tool, "record": rec, "path": path})
    return out


def _browser_only(host: str) -> bool:
    try:
        from anyuse.execution.blocked import is_browser_only
        return bool(is_browser_only(host))
    except Exception:                                  # noqa: BLE001 - an unreadable registry fails no listing
        return False


def proof_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Counts and enums of the saved proof (no page text): fresh values, model calls, the independent judge."""
    p = rec.get("proof") or {}
    cases = [c for c in p.get("cases") or [] if isinstance(c, dict)]
    return {"status": "proved" if p.get("passed") else "not proved",
            "fresh_cases": sum(1 for c in cases if c.get("passed")), "cases": len(cases),
            "independent": bool(p.get("independent")), "model_calls": int(p.get("model_calls") or 0)}


def example_of(rec: Dict[str, Any]) -> Optional[str]:
    """The first passing proof case's value (an input the proof already used)."""
    for c in (rec.get("proof") or {}).get("cases") or []:
        if isinstance(c, dict) and c.get("passed") and str(c.get("value") or "").strip():
            return str(c["value"])
    return None


def tool_specs(storage=None, taken: Sequence[str] = (), taken_aliases: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """One typed spec per saved tool. ``taken`` / ``taken_aliases``: the names and older dotted names the API typed
    tools already have; a name shared with one of them, or with another saved tool, gets a suffix from the tool's
    own directory (mcpserver/tool_names.unique), an older name the tool digest as before. Descriptions come from
    enums and counts only (page text never enters a tool instruction)."""
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.mcpserver.tool_names import SEP, fit, suffixed, unique
    used_legacy = set(taken_aliases)
    saved_list = saved_tools(storage)
    rows = []
    for t in saved_list:
        host = host_of(t["tool"])
        # a name saved as <host>__<workflow> (or the older <host>.<workflow>) is already the tool name; other
        # names get the host in front. The older dotted name stays callable as an alias.
        saved = t["name"]
        new_prefix, old_prefix = CT.tool_host_piece(host) + SEP, CT.legacy_typed_tool_name(host, "x")[:-1]
        if saved.startswith(new_prefix):
            name, legacy = fit(saved), CT.legacy_typed_tool_name(host, saved[len(new_prefix):])
        elif saved.startswith(old_prefix):
            name, legacy = CT.typed_tool_name(host, saved[len(old_prefix):]), saved
        else:
            name, legacy = CT.typed_tool_name(host, saved), CT.legacy_typed_tool_name(host, saved)
        rows.append((host, name, legacy, os.path.basename(os.path.dirname(t["path"]))))
    names = unique([(name, key) for _, name, _, key in rows], taken=taken)
    specs: List[Dict[str, Any]] = []
    for t, (host, base, legacy, key), name in zip(saved_list, rows, names):
        tool, rec = t["tool"], t["record"]
        sha = str(rec.get("tool_sha256") or "")
        param = str(rec.get("param") or "value")
        schema = json.loads(json.dumps(INPUT_SCHEMA))
        if param != "value":
            schema["properties"] = {param: schema["properties"]["value"]}
            schema["required"] = [param]
        # the dotted name this tool was listed under before: an API tool's or another saved tool's got .<sha12>
        if legacy in used_legacy:
            legacy = f"{legacy}.{sha[:12]}"
        used_legacy.add(legacy)
        own = suffixed(base, key)
        aliases = [legacy] + ([own] if own != name else [])
        wf = str(tool.get("workflow") or "")
        pf = proof_summary(rec)
        desc = (f"Run a saved AnyUse web tool for this host in the guarded container browser: a {wf} tool, version "
                f"{tool.get('version')}, 0 model calls; proof: {pf['fresh_cases']} fresh value(s) replayed at 0 model "
                f"calls{', each accepted by the independent judge' if pf['independent'] else ''}. Returns ok with "
                f"the result (verified or plain), needs_review with both sources' evidence (not accepted: you or the "
                f"user decide), or withheld / failed / refused with a typed reason_code. With ANYUSE_WEB_FALLBACK on, "
                f"a miss is answered by a model only when the independent verifier passes it (source: fallback).")
        specs.append({"name": name, "aliases": aliases, "workflow": t["name"], "kind": KIND, "host": host,
                      "site": str(tool.get("site") or host),
                      "tool_workflow": wf, "version": tool.get("version"), "tool_sha256": sha,
                      "browser_only": _browser_only(host), "description": desc,
                      "input_schema": schema, "param": param,
                      "output_schema": json.loads(json.dumps(OUTPUT_SCHEMA)), "proof": pf,
                      "example": example_of(rec), "entry": t, **({"shares": base} if name != base else {})})
    return specs


# ---------------------------------------------------------------------------
# the run record -> the caller's answer
# ---------------------------------------------------------------------------

#: an error record whose reason names the browser or the path to it (``browser_supervisor``'s infrastructure class)
_INFRA_WORDS = ("ConnectionClosed", "Connect call failed", "keepalive", "TimeoutError", "InfrastructureFailure",
                "unreachable", "Page.navigate got no answer")


def reason_code(rec: Dict[str, Any]) -> Tuple[str, str]:
    """``(status, reason_code)`` of a run record that is not an accepted answer."""
    st = str(rec.get("status") or "error")
    why = str(rec.get("why") or "")
    if st == NEEDS_REVIEW:
        return NEEDS_REVIEW, NEEDS_REVIEW
    if st == "incomplete":
        if "attribute verify" in why and "verifier_model_unavailable" in why:
            return WITHHELD, "verifier_model_unavailable"           # typed, never ok
        return WITHHELD, "unverified" if "attribute verify" in why else "incomplete"
    if st == "known_wrong":
        return WITHHELD, "known_wrong"
    if st == "failed":
        return FAILED, "conflict" if rec.get("conflicts") else "check_failed"
    if st == "blocked":
        return FAILED, "blocked"
    if st == "rate_limited":
        return FAILED, "rate_limited"
    if st == "refused":
        return REFUSED, "contact_paused" if rec.get("paused") else "refused"
    if st == "not_run":
        return FAILED, "not_run"
    if "traffic violation" in why:
        return FAILED, "traffic_violation"
    if any(w in why for w in _INFRA_WORDS):
        return FAILED, "infrastructure"
    return FAILED, "error"


def verification_of(rec: Dict[str, Any]) -> str:
    av = (rec.get("checks") or {}).get("attribute_verify") or {}
    return "verified" if av.get("verdict") == "accept" else "plain"


def evidence_of(rec: Dict[str, Any]) -> Dict[str, Any]:
    pages = rec.get("pages") or []
    last = pages[-1] if pages and isinstance(pages[-1], dict) else {}
    out = rec.get("output") if isinstance(rec.get("output"), dict) else {}
    return {"url": out.get("url") or last.get("url"), "page_title": last.get("title"),
            "search_url": (pages[0] or {}).get("url") if pages and isinstance(pages[0], dict) else None}


# ---------------------------------------------------------------------------
# the fallback agent: a read-only lookup on the same site, then the independent verifier
# ---------------------------------------------------------------------------

def _lookup_toolbox(browser):
    from anyuse.execution import harness_tools as HT

    class LookupToolbox(HT.Toolbox):
        """The harness's browser tool set restricted to a READ-ONLY lookup: no typing outside a search box, no form,
        no login; the browser's own gate still refuses every forbidden control (cart, account, checkout, ...)."""

        def available(self) -> List[str]:
            return [k for k in super().available() if k in LOOKUP_ACTIONS or k in ("done", "fail", "abstain")]

        def gate(self, action, obs):
            if action.kind not in LOOKUP_ACTIONS and action.kind not in ("done", "fail", "abstain"):
                return f"not part of a read-only lookup: {action.kind}", None
            return super().gate(action, obs)
    return LookupToolbox(browser=browser)


def start_url_of(tool: Dict[str, Any], value: str) -> str:
    """Where the fallback starts: the tool's own search URL for the value (a URL search), else its start page."""
    from anyuse.execution import demo_compile as C
    s = tool.get("search") or {}
    if s.get("mode") == "url" and "{q}" in str(s.get("template") or ""):
        return C.fill(str(s["template"]), value)
    return str(s.get("start_url") or tool.get("start_url") or ("https://" + str(tool.get("site") or "")))


def task_text(workflow: str, value: str) -> str:
    v = json.dumps(value, ensure_ascii=False)
    if workflow == "product_lookup":
        return (f"Look up the product {v} on this website and open that product's own page. Finish with done and the "
                f"result {{\"name\": ..., \"price\": ..., \"currency\": ..., \"availability\": ..., \"url\": ...}} "
                f"exactly as that product page states them: the product name as its page names it, the price as "
                f"shown, the availability in the page's own words, the url of the product page. If the site does not "
                f"have this product, finish with fail and say why. Read only: never put anything in a cart, log in or "
                f"send a form.")
    return (f"Search this website for {v}. Finish with done and the result {{\"results\": [{{\"element\": N}}, "
            f"...]}}: the results of that search (at most the first 10), each as the number of its result link in the "
            f"observation, in page order (the exact title and URL are filled in from the page; a URL shown with "
            f"\u2026 is cut, never copy it). If the site finds nothing, finish with fail and say so. Read only: never "
            f"log in or send a form.")


def normalise(workflow: str, result: Any, page_url: str = "") -> Optional[Dict[str, Any]]:
    """The agent's done result in the tool's output shape (the EXTRACTOR side's readers: ``web_arch``'s price and
    currency parsers, never the verifier's), URLs made absolute; None when it is not an answer of that shape."""
    from anyuse.execution import web_arch as W
    if not isinstance(result, dict):
        return None
    base = page_url or ""

    def absu(u: Any) -> str:
        u = str(u or "").strip()
        return urllib.parse.urljoin(base, u) if u and base else u
    if workflow == "product_lookup":
        name = str(result.get("name") or "").strip()
        if not name:
            return None
        raw_price = result.get("price")
        price = None
        if isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool):
            price = float(raw_price)
        elif raw_price not in (None, ""):
            price = W.parse_price(str(raw_price))
        cur = str(result.get("currency") or "").strip()
        if not cur and raw_price not in (None, ""):
            found = sorted(W.currencies_in(str(raw_price)))
            cur = found[0] if len(found) == 1 else ""
        cur = cur.upper() if re.fullmatch(r"[A-Za-z]{3}", cur) else (sorted(W.currencies_in(cur))[0]
                                                                     if W.currencies_in(cur) else cur)
        av = result.get("availability")
        return {"name": name[:300], "price": price, "currency": cur or None,
                "availability": None if av in (None, "") else str(av)[:200], "url": absu(result.get("url")) or base}
    rows = result.get("results")
    if not isinstance(rows, list):
        return None
    res = []
    for r in rows[:50]:
        if isinstance(r, dict) and str(r.get("title") or "").strip():
            res.append({"title": str(r["title"]).strip()[:300], "url": absu(r.get("url"))})
    return {"results": res} if res else None


def _fold_ws(s: Any) -> str:
    return " ".join(str(s or "").split())


def resolve_on_page(workflow: str, out: Dict[str, Any], dom: str, page_url: str) -> Dict[str, Any]:
    """The agent's references RESOLVED against the page it ended on: the shared resolver (``answer_refs``, the same
    one the harness applies to every done answer and the compile reads), over the final page's DOM links. A cut
    reference that starts exactly one link is that link; one that starts several (and its title does not single one
    out) stays as answered and is reported uncertain (``resolve_report``); the verifier then refuses it."""
    from anyuse.execution import answer_refs as AR
    if not isinstance(out, dict):
        return out
    got, _rep = AR.resolve_on_dom(workflow, out, dom or "", page_url)
    return got


def resolve_report(workflow: str, out: Dict[str, Any], dom: str, page_url: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(resolved, report)``: :func:`resolve_on_page` with the shared resolver's report."""
    from anyuse.execution import answer_refs as AR
    if not isinstance(out, dict):
        return out, {}
    return AR.resolve_on_dom(workflow, out, dom or "", page_url)


def required_fields(tool: Dict[str, Any]) -> List[str]:
    """The product fields a fallback answer must carry: the tool's own output fields it has a source for."""
    outs = [str(x) for x in tool.get("output") or ["name", "price", "currency", "availability", "url"]]
    srcs = tool.get("fields") or {}
    need = [f for f in ("name", "url") if f in outs]
    for f in ("price", "availability"):
        if f in outs and (not srcs or f in srcs):
            need.append(f)
    return need


def identity_problem(workflow: str, value: str, out: Dict[str, Any], tool: Dict[str, Any],
                     page: Any = None) -> str:
    """The answer is of the INPUT (the verifier checks it against its page, not against what was asked): a product's
    answered name holds >= ``IDENTITY_SHARE`` of the input's tokens; a list has a result title that does, or the
    result page itself names the input (its main heading or its result-count statement: the page says whose results
    these are; ``attribute_verify.sparse_evidence``'s sources). Read with the verifier's own tokenizer. "" when it
    holds."""
    from anyuse.execution import attribute_verify as AV
    if workflow == "product_lookup":
        missing = [f for f in required_fields(tool) if out.get(f) in (None, "")]
        if missing:
            return f"the answer lacks {', '.join(missing)}"
        share = AV.containment(value, out.get("name"))
        if share < IDENTITY_SHARE:
            return f"the answered name holds {share:.2f} of the input's words (< {IDENTITY_SHARE})"
        return ""
    titles = [str(r.get("title") or "") for r in out.get("results") or []]
    best = max([AV.containment(value, t) for t in titles] + [0.0])
    if best >= IDENTITY_SHARE:
        return ""
    if page is not None:
        st = AV.count_statement(page.lines, value=value)
        if any(AV.names_value(h, value) for h in page.headings()) or \
                (st and AV.names_value(st["line"], value)):
            return ""
    return (f"no answered title names the input (best share {best:.2f} < {IDENTITY_SHARE}) and the page's heading "
            f"or result count does not either")


def run_fallback(page: Any, tool: Dict[str, Any], value: str, *, policy: Any, model: Any = None,
                 cap: int = MAX_CALLS, loop: Any = None, settle: Any = None) -> Dict[str, Any]:
    """ONE fallback run on ``page`` (a guarded session live, a ``dom_page.DomPage`` offline): the read-only lookup by
    ``policy`` (``decide(task, obs, history, available)``), then the independent verifier on the page the run ended
    on (``model``: its second stage, ``(prompt, schema) -> answer``; None: an open attribute is withheld). The record
    has the run record's shape (status ``ok`` only for a verified answer; ``needs_review`` / ``incomplete`` /
    ``failed``), with ``model_calls`` and ``cost_usd`` of the whole run (agent + verifier), never above ``cap`` calls,
    and ``check_calls``: the check model's requests during the agent's run that ``model_calls`` holds."""
    from anyuse.execution import agent_harness as H
    from anyuse.execution import attribute_verify as AV
    from anyuse.execution import harness_compile as HC
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import harness_types as T
    wf = str(tool.get("workflow") or "")
    rec: Dict[str, Any] = {"value": value, "status": "failed", "output": None, "why": "", "checks": {}, "pages": [],
                           "model_calls": 0, "cost_usd": 0.0, "source": SOURCE_FALLBACK, "accepted": False}
    if wf not in FALLBACK_WORKFLOWS:
        return dict(rec, status="not_run", why=f"no fallback for a {wf} tool")
    start = start_url_of(tool, value)
    browser = HT.BrowserTools(page, start, host_of(tool), settle=settle, workflow=wf, loop=loop)
    agent_cap = max(1, int(cap) - VERIFY_RESERVE)
    cfg = H.HarnessConfig(max_steps=agent_cap, max_calls=agent_cap, max_usd=AGENT_MAX_USD,
                          unasked_irreversible=H.UNASKED_STOP)
    from anyuse.execution import model_check as MC
    with MC.tally() as in_run:
        trace = H.Harness(_lookup_toolbox(browser), policy, cfg).run(task_text(wf, value), {})
    calls, usd = int(trace.model_calls or 0), float(trace.cost_usd or 0.0)
    # the check-model requests the run's own count (the process counter's delta) already holds
    rec["check_calls"] = int(in_run["noted"])
    rec["agent"] = {"stop": trace.stop, "reason": str(trace.stop_reason or "")[:300], "steps": len(trace.steps),
                    "decisions": trace.decisions, "urls": [s.observation.url for s in trace.steps if s.observation][:12],
                    "actions": [str(s.action)[:120] if s.action is not None else "(unreadable)" for s in trace.steps][:12]}
    rec.update(model_calls=calls, cost_usd=round(usd, 6))
    if trace.stop != T.Stop.DONE:
        return dict(rec, why=f"the fallback agent stopped: {trace.stop}: {str(trace.stop_reason or '')[:200]}")

    async def final():
        return await HC.final_page_async(page)
    fp = (loop.run_until_complete(final()) if loop is not None else asyncio.run(final())) or {}
    # the page the answer was read from, kept in the run record as evidence (a payload carries only its URL and title)
    rec["pages"] = [{"url": fp.get("url"), "title": fp.get("title"), "text": str(fp.get("text") or "")[:6000],
                     "dom": str(fp.get("dom") or "")[:600000]}]
    out = normalise(wf, trace.result, str(fp.get("url") or ""))
    rec["agent_result"] = trace.result
    if out is None:
        return dict(rec, why="the agent's answer is not a result of the tool's shape (a no-results answer has no "
                             "independent reading and is never accepted)")
    out, rrep = resolve_report(wf, out, str(fp.get("dom") or ""), str(fp.get("url") or ""))
    from anyuse.execution import answer_refs as AR
    if AR.refusal(trace.refs) or AR.refusal(rrep):
        return dict(rec, status="incomplete", unverified_output=out,
                    why=f"fallback reference: {AR.refusal(trace.refs) or AR.refusal(rrep)}"[:300])
    if wf == "product_lookup":
        rec["picked"] = {"url": out.get("url"), "title": out.get("name")}
    pg = AV.Page(str(fp.get("dom") or ""), str(fp.get("text") or ""), str(fp.get("url") or ""), ax=fp.get("ax_av"))
    prob = identity_problem(wf, value, out, tool, pg)
    if prob:
        return dict(rec, status="incomplete", unverified_output=out, why=f"fallback identity: {prob}")
    left = max(0, int(cap) - calls)
    resolver = AV.Resolver(model, max_calls=min(AV.MAX_MODEL_CALLS, left)) if (model is not None and left > 0) else \
        AV.Resolver(None)
    # the answer relates to ITS value and opens no search link; the fallback agent may have built the URL
    # it ended on itself, so the URL is never the page's statement here (built_url)
    v = AV.check(wf, out, pg, resolver, scope=AV.scope_hosts(tool), value=value if wf != "product_lookup" else None,
                 built_url=True)
    got = AV.withhold(dict(rec, status="ok", output=out), v, "fallback")
    got["cost_usd"] = round(usd + float(v.usd or 0.0), 6)
    got["accepted"] = got.get("status") == "ok"
    if got.get("status") == "ok":
        got["why"] = "fallback answer verified: " + v.why()[:240]
    return got


def page_contact_inner(policy_factory: Callable[[], Any], model_factory: Callable[[], Any], cap: int = MAX_CALLS):
    """``inner(ctx, endpoint, tool, value)`` for ``watch_live.make_replay``: the fallback on ONE guarded page of the
    site's context (the replay's own budget, robots and caps), kept on one event loop for the whole run."""
    def inner(ctx, endpoint: str, tool: Dict[str, Any], value: str) -> Dict[str, Any]:
        from anyuse.execution import watch_live as WL
        _L, _L3, A4 = WL._bench()
        from anyuse.execution import typed_readiness as TR
        Session = A4.session_class()
        loop = asyncio.new_event_loop()
        s = Session(endpoint, allow_url=ctx.allowed, budget=ctx.budget)
        try:
            page = loop.run_until_complete(s.__aenter__())
            try:
                return run_fallback(page, tool, value, policy=policy_factory(), model=model_factory(), cap=cap,
                                    loop=loop, settle=TR.PageSettle())
            finally:
                loop.run_until_complete(s.__aexit__(None, None, None))
        finally:
            loop.close()
    return inner


def default_policy():
    """The fallback agent on the configured ``agent`` role model (any preset); its run budget is
    :data:`AGENT_MAX_USD` on Cerebras and :data:`OTHER_AGENT_MAX_USD` on every other provider."""
    from anyuse.execution import harness_policies as P
    from anyuse.execution.model_adapter import RunBudget, resolve_model_config
    try:
        provider = resolve_model_config("agent").provider
    except Exception:                                  # noqa: BLE001 - the policy reports its own config error
        provider = "cerebras"
    usd = AGENT_MAX_USD if provider == "cerebras" else OTHER_AGENT_MAX_USD
    return P.cerebras_policy(budget=RunBudget(usd), feature=FEATURE)


def default_model():
    """The fallback's verifier: the second stage on the judge role's configured model."""
    from anyuse.execution import attribute_verify as AV
    try:
        return AV.configured_model()
    except Exception:                                  # noqa: BLE001 - no model: open attributes are withheld
        return None


# ---------------------------------------------------------------------------
# the ladder seam: a booked answer and the checker the gate asks
# ---------------------------------------------------------------------------

def _norm_url(u: Any) -> str:
    from anyuse.execution import demo_compile as C
    return C._norm(str(u or ""))


class FallbackChecker:
    """The repair gate's checker after a booked miss: ``agrees`` only when the repaired run's answer is the SAME OBJECT
    as the input's verified fallback answer (a product: the same page; a list: the same first result and at least
    half of the fallback's results); ``undeterminable`` otherwise (never ``disagrees``: a different answer is not
    proven wrong). The gate itself still needs the repaired run's own checks (and, with the setting on, the verifier
    inside the run) to pass."""

    def __init__(self, booked: Callable[[str], Optional[Dict[str, Any]]]):
        self.booked = booked

    def __call__(self, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
        ref = self.booked(value)
        o = out.get("output") if isinstance(out.get("output"), dict) else None
        if not ref or o is None or out.get("status") != "ok":
            return {"verdict": "undeterminable", "why": "no verified fallback answer for this input"}
        if "results" in ref:
            mine = [_norm_url(r.get("url")) for r in o.get("results") or [] if isinstance(r, dict)]
            want = [_norm_url(r.get("url")) for r in ref.get("results") or [] if isinstance(r, dict)]
            if mine and want and mine[0] == want[0] and len(set(mine) & set(want)) * 2 >= len(want):
                return {"verdict": "agrees", "why": "the same first result and >= half of the verified fallback's"}
            return {"verdict": "undeterminable", "why": "not the verified fallback's results"}
        if _norm_url(o.get("url")) and _norm_url(o.get("url")) == _norm_url(ref.get("url")):
            return {"verdict": "agrees", "why": "the same product page as the verified fallback answer"}
        return {"verdict": "undeterminable", "why": "not the verified fallback answer's product page"}


async def ladder_run(page_factory: Callable[[], Any], tool: Dict[str, Any], value: str, settle: Any,
                     ladder: Dict[str, Any], result_wait_s: Optional[float] = None) -> Dict[str, Any]:
    """One replay through the repair ladder with this route's seam (``WebToolRoute.ladder_for``): the store's ACTIVE
    revision at tier 0, tier 1 on a failure (0 model calls), the gate's checker the fallback checker behind the
    verdict store."""
    from anyuse.execution import repair_ladder as LD
    return await LD.run_with_ladder(page_factory, tool, value, settle, config=ladder["config"], store=ladder["store"],
                                    checker=ladder["checker"], verdicts=ladder["verdicts"], result_wait_s=result_wait_s)


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------

#: one live call at a time per process (the CLI replay's cache directory is a module global of the bench helpers)
_LIVE = threading.Lock()


def _product_scope():
    """The tool call's ``model_check`` scope: inside it the attribute verifier may ask its configured model about an
    attribute the rules leave open, and a check model may answer the questions it is asked on tool calls. ``anyuse
    watch run`` replays through the same ``watch_live`` stack without it, so a watch run asks no model."""
    from anyuse.execution import model_check as MC
    return MC.scope("product")


class WebToolRoute:
    """The call path of both MCP profiles (and of the live check's bench). Every collaborator is an attribute a test
    replaces: ``replay(entry, value, caps, ladder) -> record``, ``fallback(entry, value, caps) -> record``,
    ``probe(endpoint) -> (ok, info)``, ``check_endpoint(endpoint)`` (raises when it is not a throwaway browser),
    ``pace(host)``, ``contact`` (the live benches' contact guard; None in the product)."""

    def __init__(self, *, storage=None, root: Optional[str] = None, endpoint: Optional[str] = None,
                 fallback: Optional[bool] = None, contact: Any = None, log: Callable[[str], None] = lambda s: None):
        self.storage = storage
        self._root = root
        self._endpoint = endpoint
        self._fallback = fallback
        self.contact = contact
        self.log = log
        self.policy_factory: Callable[[], Any] = default_policy
        self.model_factory: Callable[[], Any] = default_model
        self.replay: Callable[..., Dict[str, Any]] = self._live_replay
        self.fallback: Callable[..., Dict[str, Any]] = self._live_fallback
        self.probe: Callable[[str], Tuple[bool, str]] = self._probe
        self.check_endpoint: Callable[[str], Any] = self._check_endpoint
        self.pace: Callable[[str], Any] = self._pace
        self.clock: Callable[[], _dt.datetime] = _now
        #: the last call's run records (``{"replay": ..., "fallback": ...}``): evidence for a bench, never a payload
        self.last: Dict[str, Any] = {}

    # -- configuration --------------------------------------------------------------------------------------------
    @property
    def root(self) -> str:
        if self._root:
            return self._root
        from anyuse.paths import anyuse_data_path
        return str(anyuse_data_path("web_tools"))

    def endpoint(self) -> str:
        return str(self._endpoint if self._endpoint is not None else os.environ.get(ENDPOINT_ENV, "")).strip()

    def fallback_on(self) -> bool:
        return fallback_enabled(self._fallback)

    def model_ready(self) -> bool:
        """Is the fallback's model configured (a test replaces it; the product: ``attribute_verify.model_configured``)."""
        from anyuse.execution import attribute_verify as AV
        return AV.model_configured()

    def traffic(self):
        from anyuse.execution.watch import WatchStore
        return WatchStore(self.root)

    def verdicts(self):
        from anyuse.execution.verdict_store import VerdictStore
        return VerdictStore(os.path.join(self.root, "verdicts.jsonl"))

    def repair_dir(self) -> str:
        return os.path.join(self.root, "repair")

    def repair_store(self, tool: Dict[str, Any], create: bool = False):
        """The ladder's store of this tool (None when it has none and ``create`` is False: nothing is written)."""
        from anyuse.execution import demo_compile as C
        from anyuse.execution import repair_ladder as LD
        if not create and not os.path.isdir(os.path.join(self.repair_dir(), C.tool_digest(tool)[:16])):
            return None
        return LD.RepairStore(self.repair_dir(), tool)

    def misses_path(self) -> str:
        return os.path.join(self.root, "misses.jsonl")

    def booked(self, tool: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
        """The input's latest verified fallback answer for this tool (the miss ledger), or None."""
        from anyuse.execution import demo_compile as C
        digest, hit = C.tool_digest(tool), None
        try:
            with open(self.misses_path(), encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        row = json.loads(ln)
                    except ValueError:
                        continue
                    if row.get("tool") == digest and row.get("value") == value and row.get("answer"):
                        hit = row["answer"]
        except OSError:
            return None
        return hit

    # -- the default collaborators (live) ----------------------------------------------------------------------------
    def _check_endpoint(self, endpoint: str) -> Any:
        from anyuse.execution.agent_browser import assert_container_endpoint
        return assert_container_endpoint(endpoint)

    def _probe(self, endpoint: str) -> Tuple[bool, str]:
        from anyuse.execution.browser_supervisor import probe
        return probe(endpoint)

    def _pace(self, host: str) -> float:
        from anyuse.execution import repair_pace as RP
        return asyncio.run(RP.PACER.wait(host, "mcp web tool"))

    def _cache_dir(self) -> str:
        return os.path.join(self.root, "guard", "cache")

    def _live_replay(self, entry: Dict[str, Any], value: str, caps: Dict[str, int], ladder: Any = None
                     ) -> Dict[str, Any]:
        from anyuse.execution.watch_live import make_replay
        tool = entry["tool"]
        inner = self._ladder_inner(ladder) if ladder is not None else None
        rp = make_replay(self.endpoint(), self._cache_dir(), log=self.log, contact=self.contact, inner=inner)
        with _LIVE:
            return rp(self._item(entry, value), tool, caps)

    def _live_fallback(self, entry: Dict[str, Any], value: str, caps: Dict[str, int]) -> Dict[str, Any]:
        from anyuse.execution.watch_live import make_replay
        rp = make_replay(self.endpoint(), self._cache_dir(), log=self.log, contact=self.contact,
                         inner=page_contact_inner(self.policy_factory, self.model_factory, MAX_CALLS))
        with _LIVE:
            return rp(self._item(entry, value, "fallback"), entry["tool"], caps)

    def _ladder_inner(self, ladder: Dict[str, Any]):
        """``inner`` for the ladder: every page a NEW session of the site's guarded context."""
        from anyuse.execution import watch_live as WL

        def inner(ctx, endpoint: str, tool: Dict[str, Any], value: str) -> Dict[str, Any]:
            _L, _L3, A4 = WL._bench()
            Session = A4.session_class()
            sessions: List[Any] = []

            async def close_last() -> None:
                if sessions and sessions[-1] is not None:
                    try:
                        await sessions[-1].__aexit__(None, None, None)
                    except Exception:                  # noqa: BLE001
                        pass
                    sessions[-1] = None

            async def opener():
                await close_last()
                s = Session(endpoint, allow_url=ctx.allowed, budget=ctx.budget)
                page = await s.__aenter__()
                sessions.append(s)
                return page

            async def go():
                try:
                    from anyuse.execution import typed_readiness as TR
                    return await ladder_run(opener, tool, value, TR.PageSettle(), ladder, WL.RESULT_WAIT_S)
                finally:
                    await close_last()
            return asyncio.run(go())
        return inner

    def ladder_for(self, tool: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
        """The ladder seam of this call (fallback on only): a tool of version >= 10 whose input has a booked verified
        answer, or that already has a repaired revision; None otherwise (the plain replay)."""
        from anyuse.execution import demo_compile as C
        from anyuse.execution import repair_ladder as LD
        if not self.fallback_on() or not C.v10(tool):
            return None
        st = self.repair_store(tool)
        if st is None or not (st.lkg(value) or st.active_rev() > 0):
            return None
        return {"config": LD.LadderConfig(enabled=True), "store": st, "verdicts": self.verdicts(),
                "checker": FallbackChecker(lambda v: self.booked(tool, v))}

    @staticmethod
    def _item(entry: Dict[str, Any], value: str, phase: str = "call") -> Dict[str, Any]:
        tool = entry["tool"]
        h = hashlib.sha256((str(entry["record"].get("tool_sha256")) + "\u0001" + value).encode()).hexdigest()[:10]
        return {"id": f"mcp-{phase}--{host_of(tool)}--{h}", "site": str(tool.get("site") or host_of(tool)),
                "input": value}

    # -- the call -------------------------------------------------------------------------------------------------
    def _refused(self, head: Dict[str, Any], code: str, why: str) -> Dict[str, Any]:
        return dict(head, ok=False, status=REFUSED, reason_code=code, reason=why[:400], model_calls=0, cost_usd=0.0,
                    requests=0)

    def _book_traffic(self, store, site: str, rec: Dict[str, Any], item_id: str) -> None:
        now = self.clock()
        store.book(site, int(rec.get("requests") or 0), int(rec.get("bytes") or 0), item_id, now, False)
        if rec.get("violations"):
            from anyuse.execution.watch import _write_json, iso
            _write_json(store.hard_stop_path, {"why": "a traffic violation", "at": iso(now), "item": item_id,
                                               "violations": rec.get("violations")})
        if rec.get("status") == "blocked":
            store.block(site, str(rec.get("why") or "blocked"))

    def call(self, entry: Dict[str, Any], args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """One MCP call of the saved tool ``entry`` (``saved_tools`` row) for ``args["value"]``."""
        from anyuse.execution import watch as WA
        tool, rec0 = entry["tool"], entry["record"]
        wf, host = str(tool.get("workflow") or ""), host_of(tool)
        site = str(tool.get("site") or host)
        param = str(rec0.get("param") or "value")
        value = str((args or {}).get(param) or (args or {}).get("value") or "").strip()
        head = {"tool": entry["name"], "kind": KIND, "workflow": wf, "site": host, "value": value,
                "tool_version": tool.get("version"), "tool_sha256": str(rec0.get("tool_sha256") or "")[:16],
                "browser_only": _browser_only(host)}
        self.last = {}
        if not value or len(value) > VALUE_MAX:
            return self._refused(head, "input", f"value must be 1-{VALUE_MAX} characters")
        ep = self.endpoint()
        if not ep:
            return self._refused(head, "no_browser", f"no container browser endpoint is attached ({ENDPOINT_ENV}); "
                                                     "a saved web tool runs only in a guarded container browser, so "
                                                     "nothing was sent")
        try:
            self.check_endpoint(ep)
        except Exception as exc:                       # noqa: BLE001 - not a throwaway browser: nothing is sent
            return self._refused(head, "no_browser", f"the attached endpoint is not a throwaway container browser: "
                                                     f"{str(exc)[:200]}")
        from anyuse.execution.gov_hosts import government_refusal, rule_on
        gov_on = rule_on()
        for h in dict.fromkeys([host] + [str(x) for x in tool.get("hosts") or []]):
            why = government_refusal(h, gov_on)
            if why:
                return self._refused(head, "refused", f"government host {h!r} is never contacted ({why})")
        store = self.traffic()
        if store.hard_stop_path.exists():
            return self._refused(head, "hard_stop", "a traffic violation stopped the web-tool route "
                                                    f"({store.hard_stop_path.name})")
        if site in store.blocked():
            return self._refused(head, "site_blocked", f"the site blocked an earlier call: {store.blocked()[site]}")
        now = self.clock()
        req_left, bytes_left = store.day_left(site, now)
        c = WA.caps()
        caps = {"requests": min(c["run_requests"], req_left), "bytes": min(c["run_bytes"], bytes_left)}
        if caps["requests"] <= 0 or caps["bytes"] <= 0:
            return self._refused(head, "daily_cap", f"the site's daily share is used ({c['day_requests']} requests)")
        ok, info = self.probe(ep)
        if not ok:
            return dict(head, ok=False, status=FAILED, reason_code="infrastructure",
                        reason=f"the container browser does not answer ({info}); nothing was sent", model_calls=0,
                        cost_usd=0.0, requests=0)
        self.pace(host)
        ladder = self.ladder_for(tool, value)
        from anyuse.execution import model_check as MC
        with _product_scope(), MC.tally() as asked:
            rec = self.replay(entry, value, caps, ladder)
        # every replay (fallback on or off, ladder or not): a known wrong answer is a typed miss, never ok
        rec = known_wrong_check(self.verdicts(), tool, value, rec)
        self.last = {"replay": rec}
        self._book_traffic(store, site, rec, self._item(entry, value)["id"])
        requests = int(rec.get("requests") or 0)
        ladder_row = rec.get("ladder") if isinstance(rec.get("ladder"), dict) else None
        av_usd = float(((rec.get("checks") or {}).get("attribute_verify") or {}).get("usd") or 0.0)
        # the verifier's own calls (the run's record) and the check model's (``model_check``, counted on the call)
        out = dict(head, requests=requests, model_calls=int(rec.get("model_calls") or 0) + int(asked["calls"]),
                   cost_usd=round(av_usd + float(asked["usd"]), 6), source=SOURCE_REPLAY)
        if ladder_row is not None:
            out["ladder"] = {"outcome": ladder_row.get("outcome"), "tier": ladder_row.get("tier"),
                             "line": ladder_row.get("line")}
            if ladder_row.get("outcome") == "repaired":
                out["source"] = SOURCE_LADDER
        st = str(rec.get("status") or "")
        if st in (OK, NO_RESULTS) and rec.get("accepted", True) is not False:
            return dict(out, ok=True, status=st, result=rec.get("output") if st == OK else {"no_results": True},
                        verification=verification_of(rec), evidence=evidence_of(rec))
        status, code = reason_code(rec)
        out.update(ok=False, status=status, reason_code=code, reason=str(rec.get("why") or "")[:400])
        if code == "known_wrong" and isinstance(rec.get("known_wrong"), dict):
            out["known_wrong"] = {k: rec["known_wrong"].get(k) for k in ("verdict", "source", "ref", "at", "scope")}
        if status == NEEDS_REVIEW:
            from anyuse.execution import attribute_verify as AV
            out["review_view"] = AV.review_view(rec)
            return out
        if st not in MISS_STATUSES:
            return out
        if not self.fallback_on():
            out["fallback"] = {"tried": False, "why": f"{SETTING} is off"}
            return out
        if wf not in FALLBACK_WORKFLOWS:
            out["fallback"] = {"tried": False, "why": f"no fallback for a {wf} tool"}
            return out
        if QUESTION_MISS.search(str(rec.get("why") or "")):
            out["fallback"] = {"tried": False, "why": "the tool found the query ambiguous (several results answer it "
                                                      "equally): there is no single answer for a fallback to supply"}
            return out
        if not self.model_ready():
            out["fallback"] = {"tried": False, "reason_code": NO_MODEL,
                               "why": f"{NO_MODEL}: the fallback needs a configured model (ANYUSE_MODEL_PROVIDER) "
                                      f"and none is configured"}
            return out
        return self._with_fallback(entry, value, out, store, site, caps, requests, int(rec.get("bytes") or 0))

    def _with_fallback(self, entry: Dict[str, Any], value: str, out: Dict[str, Any], store, site: str,
                       caps: Dict[str, int], used: int, used_bytes: int = 0) -> Dict[str, Any]:
        tool = entry["tool"]
        if store.hard_stop_path.exists():
            out["fallback"] = {"tried": False, "why": "a traffic violation stopped the route"}
            return out
        left = {"requests": caps["requests"] - used, "bytes": caps["bytes"] - used_bytes}
        if left["requests"] <= 0 or left["bytes"] <= 0:
            out["fallback"] = {"tried": False, "why": "the call's request share is used"}
            return out
        self.pace(host_of(tool))
        from anyuse.execution import model_check as MC
        with _product_scope(), MC.tally() as asked:
            frec = self.fallback(entry, value, left)
        # the fallback's answer too: one the store holds as wrong is never returned as ok
        frec = known_wrong_check(self.verdicts(), tool, value, frec)
        self.last["fallback"] = frec
        self._book_traffic(store, site, frec, self._item(entry, value, "fallback")["id"])
        # the agent run counts the check-model requests it made itself (``check_calls``): each request counts once
        fcalls = int(frec.get("model_calls") or 0) + max(0, int(asked["calls"]) - int(frec.get("check_calls") or 0))
        fusd = float(frec.get("cost_usd") or 0.0) + float(asked["usd"])
        info = {"tried": True, "status": frec.get("status"), "why": str(frec.get("why") or "")[:300],
                "model_calls": fcalls, "cost_usd": round(fusd, 6), "requests": int(frec.get("requests") or 0),
                "agent": frec.get("agent")}
        merged = dict(out, model_calls=int(out.get("model_calls") or 0) + fcalls,
                      cost_usd=round(float(out.get("cost_usd") or 0.0) + fusd, 6),
                      requests=int(out.get("requests") or 0) + int(frec.get("requests") or 0), fallback=info,
                      replay={"status": out.get("status"), "reason_code": out.get("reason_code"),
                              "reason": out.get("reason")})
        if frec.get("status") == OK and frec.get("accepted"):
            info["booked"] = self.book_fallback(entry, value, frec)
            for k in ("reason_code", "reason"):
                merged.pop(k, None)
            return dict(merged, ok=True, status=OK, result=frec.get("output"), source=SOURCE_FALLBACK,
                        verification="verified", evidence=evidence_of(frec))
        if frec.get("status") == NEEDS_REVIEW:
            from anyuse.execution import attribute_verify as AV
            return dict(merged, ok=False, status=NEEDS_REVIEW, reason_code=NEEDS_REVIEW, source=SOURCE_FALLBACK,
                        reason=str(frec.get("why") or "")[:400], review_view=AV.review_view(frec))
        return merged

    def book_fallback(self, entry: Dict[str, Any], value: str, frec: Dict[str, Any]) -> Dict[str, Any]:
        """Book a verified fallback answer: the verdict store (``correct``, source checker, reference web_fallback),
        the ladder's last known good answer for the input (version >= 10: the repair tiers' anchors and the gate's
        reference) and the miss ledger. Booking never changes the answer."""
        from anyuse.execution import demo_compile as C
        tool, wf = entry["tool"], str(entry["tool"].get("workflow") or "")
        digest = C.tool_digest(tool)
        booked: Dict[str, Any] = {"verdict_store": False, "ladder_lkg": False, "ledger": False}
        try:
            self.verdicts().add(tool=digest, site=str(tool.get("site") or host_of(tool)), value=value, status="ok",
                                output=frec.get("output"), verdict="correct", source="checker", ref=REF,
                                why=str(frec.get("why") or "")[:300], workflow=wf)
            booked["verdict_store"] = True
        except Exception:                              # noqa: BLE001
            pass
        if C.v10(tool):
            try:
                st = self.repair_store(tool, create=True)
                st.set_lkg(value, wf, frec, st.active_rev())
                booked["ladder_lkg"] = True
            except Exception:                          # noqa: BLE001
                pass
        try:
            os.makedirs(self.root, exist_ok=True)
            row = {"at": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": digest, "site": host_of(tool),
                   "value": value, "workflow": wf, "answer": frec.get("output"),
                   "model_calls": int(frec.get("model_calls") or 0), "cost_usd": float(frec.get("cost_usd") or 0.0)}
            with open(self.misses_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            booked["ledger"] = True
        except OSError:
            pass
        return booked


def find(specs: Sequence[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """The spec listed as ``name``, else the one spec with ``name`` among its older names."""
    from anyuse.mcpserver.tool_names import lookup
    return lookup(specs, name)


__all__ = ["SETTING", "KIND", "MAX_CALLS", "fallback_enabled", "verdict_store_path", "known_wrong_check", "saved_tools", "tool_specs", "WebToolRoute",
           "run_fallback", "normalise", "identity_problem", "FallbackChecker", "reason_code", "task_text"]
