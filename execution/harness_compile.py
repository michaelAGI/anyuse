"""Every success compiled: a harness trace through the EXISTING
compile/proof seams, saved as a tool that replays with 0 model calls.

* web: ``trace_to_demo`` renders the trace as the web demonstrator's trace; ``demo_compile.demo_validity``
  and ``compile_trace`` build the tool; ``prove_web`` replays it on fresh values (``dom_page.run`` offline,
  ``demo_tool.run_tool`` live); ``save_web_tool`` persists it beside the workflow (``compiled_demo.json``).
* desktop: ``trace_to_drive_recording`` renders the trace as the guest drive harness's ``workflow.json``
  and hands it, unchanged, to ``draft_compile.draft_to_workflow`` (candidates -> the run's own values as
  parameters, no model proposal -> ``compile_record`` -> ``draft_proof.prove`` -> ``save_workflow``);
  ``replay_desktop`` is ``draft_compile.replay_saved`` with an approver that is never asked.

A run that did not end ``done``, a trace the validity check rejects, a proof that fails: a draft with
the reasons, never a saved tool. The model calls of every compile and replay are the process counter's
delta.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import harness_types as T

WEB_TOOL_FILE = "compiled_demo.json"
WEB_DRAFT_FILE = "compiled_demo.draft.json"
MIN_FRESH_CASES = 2
_NAV_KINDS = {T.Search.kind: "search", T.Click.kind: "click", T.Open.kind: "goto", T.Select.kind: "select"}


# ---------------------------------------------------------------------------
# web: trace -> demonstration trace -> tool -> proof -> saved
# ---------------------------------------------------------------------------
def _demo_result(trace: T.Trace, workflow: str) -> Optional[Dict[str, Any]]:
    """The policy's ``done`` result in the demonstrator's finish format."""
    res = trace.result
    table = next((s.outcome.data.get("rows") for s in reversed(trace.steps)
                  if s.action is not None and s.action.kind == T.ReadTable.kind and s.outcome.ok), None)
    if not isinstance(res, dict):
        # a bare list of rows, or nothing: take the last table read
        res = {"results": table} if table else None
        if res is None:
            return None
    elif workflow != "product_lookup" and not res.get("no_results") and not (res.get("results") or res.get("rows")) \
            and table:
        # a done result without rows (a summary in words) after a table read: the rows the run READ are the
        # demonstration's results (the compiler still checks them against the page)
        res = dict(res, results=table)
    if res.get("no_results") is True:
        return {"no_results": True}
    if workflow == "product_lookup":
        f = res.get("fields") if isinstance(res.get("fields"), dict) else res
        keep = {k: f.get(k) for k in ("name", "price", "currency", "availability", "url") if f.get(k) is not None}
        return keep or None
    rows = res.get("results") or res.get("rows") or []
    got = [{"title": str(x.get("title") or x.get("name") or "")[:300], "url": str(x.get("url") or x.get("href") or "")[:500]}
           for x in rows if isinstance(x, dict)]
    return {"results": got[:10]} if got else None


#: the LAST rung of "does the task ask for one item's attribute": words of such a task, EN/DE/FR/IT/ES/NL - whole
#: words (``_ATTRIBUTE_EXACT``), words starting with a stem (``_ATTRIBUTE_STEMS``) and phrases; the answer's own
#: shape and the configured check model (``model_check``, in the compile scope) come first
ATTRIBUTE_WORDS = ("price", "cost", "availab", "in stock", "stock", "preis", "kostet", "verfügbar", "lieferbar",
                   "auf lager", "prix", "disponib", "prezzo", "precio", "prijs", "beschikbaar", "voorraad")
_ATTRIBUTE_EXACT = frozenset(("cost", "costs", "costa", "costano", "costo", "costi", "stock", "kostet", "kosten",
                              "prix", "prezzo", "prezzi", "prijs", "prijzen", "price", "prices", "preis", "preise"))
_ATTRIBUTE_STEMS = ("availab", "verfügbar", "verfugbar", "lieferbar", "disponib", "precio", "beschikbaar", "voorraad",
                    "preis")
#: the nouns that end a German or Dutch compound naming a price ("Kaufpreis", "Endpreis", "verkoopprijs")
_ATTRIBUTE_HEADS = ("preis", "preise", "prijs", "prijzen")
_ATTRIBUTE_PHRASES = ("in stock", "auf lager")


def _question():
    from anyuse.execution import model_check as MC
    q = _QUESTION.get("q")
    if q is None:
        q = _QUESTION["q"] = MC.Question(
            "task_asks_item_attribute",
            "Does this task ask for a fact about ONE item, such as its price or whether it can be bought, rather "
            "than for a list of search results?")
    return q


_QUESTION: Dict[str, Any] = {}


def asks_attribute(task: str, scope: str = "compile") -> Optional[bool]:
    """Does the task ask for one item's attribute? The configured check model's clear answer (``model_check``: one
    yes / no question about the task text), else None (no model, no clear answer). ``scope``: the model-check scope
    opened around that question alone (``compile``: at compile time); ``""``: only inside a scope the caller already
    holds open (an agent's own step, a tool call), and nothing outside one. Asks nothing when no check model is
    configured."""
    from anyuse.execution import model_check as MC
    text = " ".join(str(task or "").split())
    if not text:
        return None
    with (MC.scope(scope) if scope else contextlib.nullcontext()):
        if not MC.current_scope() or not MC.available(_question().kind):
            return None
        v = MC.check(_question(), "", (), state=text[:MC.MAX_STATE_CHARS], where="harness_compile.workflow_of")
    return True if v.yes else False if v.no else None


def task_names_attribute(task: str) -> bool:
    """The lexicon's reading (the last rung): a word of the task is one of ``_ATTRIBUTE_EXACT``, starts with a stem of
    ``_ATTRIBUTE_STEMS``, ends a compound with one of ``_ATTRIBUTE_HEADS``, or the task holds a phrase of
    ``_ATTRIBUTE_PHRASES`` - never a word that only contains one ("Stockholm", "pricing")."""
    words = [w for w in re.split(r"[^\w]+", str(task or "").casefold()) if w]
    joined = f" {' '.join(words)} "
    return any(w in _ATTRIBUTE_EXACT or w.startswith(_ATTRIBUTE_STEMS) or w.endswith(_ATTRIBUTE_HEADS)
               for w in words) or any(f" {p} " in joined for p in _ATTRIBUTE_PHRASES)


def workflow_of(trace: T.Trace, scope: str = "compile") -> str:
    """The tool a web trace compiles to: ``product_lookup`` when the run answered ONE item's facts (a name with a price,
    a currency or an availability) or the task asks for an attribute and the answer is no list; else ``site_search``
    (without this, ``agent run --compile`` built site_search tools only). Whether the task asks for an attribute: the
    configured check model's answer first (``asks_attribute``, in ``scope``), the lexicon last
    (``task_names_attribute``)."""
    res = trace.result if isinstance(trace.result, dict) else {}
    if res.get("results") or res.get("rows") or res.get("no_results"):
        return "site_search"
    facts = res.get("fields") if isinstance(res.get("fields"), dict) else res
    if facts.get("name") and any(facts.get(k) not in (None, "") for k in ("price", "currency", "availability")):
        return "product_lookup"
    if not facts:
        return "site_search"
    asked = asks_attribute(str(trace.task or ""), scope)
    if asked is not None:
        return "product_lookup" if asked else "site_search"
    return "product_lookup" if task_names_attribute(str(trace.task or "")) else "site_search"


def trace_to_demo(trace: T.Trace, workflow: str = "site_search") -> Dict[str, Any]:
    """The harness trace as ``demo_policy.run_demo``'s trace (the compiler reads nothing else)."""
    value = next(iter(trace.values.values()), "") if trace.values else ""
    steps: List[Dict[str, Any]] = []
    forbidden: List[Dict[str, Any]] = []
    last_page: Dict[str, Any] = {}
    for s in trace.steps:
        if s.action is None:
            continue
        page = dict(s.observation.page or {})
        if page:
            last_page = page
        kind = s.action.kind
        if kind in _NAV_KINDS:
            a: Dict[str, Any] = {"action": _NAV_KINDS[kind]}
            if kind == T.Open.kind:
                a["url"] = s.action.url
            else:
                a["element"] = int(s.action.index)
            if kind == T.Search.kind:
                a["text"] = s.action.text
            if kind == T.Select.kind:
                a["option"] = s.action.option
            o = s.outcome
            outcome = {"ok": o.ok, "how": o.how, "url_after": o.url_after, "why": o.why}
            if o.input:
                outcome["input"] = dict(o.input)
            if o.href:
                outcome["href"] = o.href
            if o.forbidden:
                outcome["forbidden"] = o.forbidden
                forbidden.append({"step": len(steps), "why": o.forbidden})
            if (o.data or {}).get("search_post"):
                # the results came from the site's read-only POST search after typing
                outcome["data"] = {"search_post": list(o.data["search_post"])[:2]}
            for key in ("documents", "search_url", "form_url", "submit"):
                # how the typed search moved (the documents it loaded, its fallbacks, the control it clicked)
                if (o.data or {}).get(key):
                    outcome[key] = o.data[key]
            steps.append({"k": len(steps), "page": page, "action": a, "outcome": outcome,
                          "mapping": {str(k): v for k, v in (s.observation.mapping or {}).items()}})
        elif kind == T.Done.kind:
            steps.append({"k": len(steps), "page": page, "action": {"action": "done", "result": _demo_result(trace, workflow)},
                          "outcome": {}})
        elif kind == T.Fail.kind:
            steps.append({"k": len(steps), "page": page, "action": {"action": "fail"}, "outcome": {}})
        # reads, scrolls and refusals change no page: they are not demonstration steps
    stop = {T.Stop.DONE: "done", T.Stop.FAIL: "fail", T.Stop.BLOCKED: "blocked"}.get(trace.stop, trace.stop)
    if forbidden:
        stop = "forbidden"
    return {"version": 1, "workflow": workflow, "site": trace.site, "start_url": trace.start_url, "value": value,
            "steps": steps, "calls": trace.decisions, "cli": "agent_harness", "forbidden": forbidden, "stop": stop,
            "result": _demo_result(trace, workflow) if trace.stop == T.Stop.DONE else None,
            "final_page": last_page, "harness_run_id": trace.run_id, "harness_digest": trace.digest(),
            # the demonstration cascade: which model made the demonstration
            "demonstrated_by": str(getattr(trace, "demonstrated_by", "") or "")}


def compile_web(trace: T.Trace, workflow: str = "site_search", version: Optional[int] = None) -> Dict[str, Any]:
    """``{"ok", "tool", "why", "validity", "demo"}``: the validity check, then the compile."""
    from anyuse.execution import demo_compile as C
    demo = trace_to_demo(trace, workflow)
    if trace.stop != T.Stop.DONE:
        return {"ok": False, "why": f"the run did not finish ({trace.stop}: {trace.stop_reason[:160]})", "demo": demo,
                "validity": None, "tool": None}
    from anyuse.execution import answer_refs as AR
    why = AR.refusal(trace.refs)
    if why:
        # an uncertain reference (a cut string that starts several links) is never compiled into a tool
        return {"ok": False, "why": f"ambiguous_reference: {why}", "demo": demo, "validity": None, "tool": None}
    ok, why, det = C.demo_validity(demo)
    if not ok:
        return {"ok": False, "why": f"not a valid demonstration: {why}", "validity": det, "demo": demo, "tool": None}
    got = C.compile_trace(demo, version=version)
    if not got.get("ok"):
        return {"ok": False, "why": f"compile: {got.get('why')}", "validity": det, "demo": demo, "tool": None,
                "report": got.get("report")}
    return {"ok": True, "tool": got["tool"], "why": "compiled", "validity": det, "demo": demo, "report": got.get("report")}


def replay_web(tool: Dict[str, Any], site, value: str, params: Optional[Dict[str, Any]] = None,
               page_factory: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """One replay at 0 model calls: ``dom_page.run`` offline (``site`` a ``DomSite``); the model calls are
    the process counter's delta. The page the replay ended on is kept beside the record (``final_page``: its URL,
    DOM and visible text) for the independent judge and for a later look at a refused value."""
    from anyuse.execution import dom_page as D
    from anyuse.execution.model_adapter import calls_since, process_call_count
    mark = process_call_count()
    pages: List[Any] = []

    def factory():
        pg = page_factory() if page_factory else D.DomPage(site)
        pages.append(pg)
        return pg
    out = D.run(site, tool, value, params, factory)
    out["model_calls"] = calls_since(mark)
    out["final_page"] = _final_page(pages[-1]) if pages else None
    return out


async def final_page_async(page: Any) -> Optional[Dict[str, Any]]:
    """The page a replay ended on, read locally (URL, DOM, visible text, title; no request): the evidence the
    independent judge reads and the record a refused value keeps. None when the page cannot be read."""
    import asyncio
    try:
        url = await page.url() if hasattr(page, "url") else ""
        dom = await page.dom() if hasattr(page, "dom") else ""
        text, title = "", ""
        if hasattr(page, "read") and not asyncio.iscoroutinefunction(page.read):
            read = page.read() or {}
            text, title = str(read.get("text") or ""), str(read.get("title") or "")
        if not text and hasattr(page, "text"):
            text = str(await page.text() or "")
        out = {"url": str(url or ""), "dom": str(dom or ""), "text": text, "title": title}
        # web-ax-tree: the page's accessibility tree as the VERIFIER reads it itself (``attribute_verify.read_ax``: an
        # independent second read of a list answer); none on a page without one
        from anyuse.execution import attribute_verify as AV
        ax = await AV.read_ax(page) if AV.AX_CHECK else None
        if ax:
            out["ax_av"] = ax
        return out
    except Exception:                                  # noqa: BLE001 - a page that cannot be read is no evidence
        return None


def _final_page(page: Any) -> Optional[Dict[str, Any]]:
    import asyncio
    try:
        return asyncio.run(final_page_async(page))
    except Exception:                                  # noqa: BLE001
        return None


def independent_verdict(tool: Dict[str, Any], out: Dict[str, Any], value: Optional[str] = None) -> Dict[str, Any]:
    """The proof's judge is INDEPENDENT of the tool - an ``ok`` answer is read again on the
    page the replay ended on by ``attribute_verify`` (its own DOM parse: every title on its own card, every result
    inside the tool's declared result scope with a plain link; a product's fields against the page), rules only,
    0 model calls. ``{"verdict": "accept" | "withhold" | "uncertain", "why"}``; no page is uncertain."""
    from anyuse.execution import attribute_verify as AV
    if out.get("status") != "ok" or not isinstance(out.get("output"), dict):
        return {"verdict": "accept", "why": "no answer to verify (the run's own status is not ok)"}
    fp = out.get("final_page") or {}
    if not fp.get("dom"):
        return {"verdict": "uncertain", "why": "no page kept from the replay: nothing to read independently"}
    wf = str(tool.get("workflow") or "")
    if wf not in ("product_lookup", "site_search", "filtered_search", "paged_search"):
        return {"verdict": "accept", "why": f"no typed attribute for a {wf} answer"}
    v = AV.check(wf, out["output"], AV.Page(fp["dom"], fp.get("text") or "", fp.get("url") or "",
                                            ax=fp.get("ax_av")), None, scope=AV.scope_hosts(tool),
                 value=value, built_url=AV.built_url(tool))
    return {"verdict": v.verdict, "why": v.why()[:300], "attributes": [d.to_dict() for d in v.decisions if d.critical
                                                                       and d.status != AV.VERIFIED][:6]}


def _page_record(fp: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the proof keeps of a refused value's page: URL, title and the visible text."""
    if not fp:
        return None
    return {"url": fp.get("url"), "title": fp.get("title"), "text": str(fp.get("text") or "")[:4000]}


#: the proof tries at most this many candidate values to find MIN_FRESH_CASES that pass
PROOF_TRIES = MIN_FRESH_CASES + 2
#: function words that are never a proof value, EN/DE/FR/IT/ES/NL
FUNCTION_WORDS = frozenset({
    "with", "from", "this", "that", "your", "about", "test", "neue", "neuer", "neues", "eine", "einer", "einem", "einen",
    "über", "ueber", "unter", "für", "fuer", "oder", "nicht", "mehr", "auch", "sind", "wird", "werden", "beim", "zum",
    "zur", "dans", "pour", "avec", "sans", "plus", "sous", "della", "delle", "degli", "dello", "nella", "nelle", "sono",
    "come", "anche", "para", "como", "desde", "sobre", "entre", "voor", "over", "naar", "niet", "deze", "meer", "zijn"})
#: the LAST rung of a site's furniture (a word on every page of the site is furniture by the demonstration's own pages,
#: ``site_furniture``): its words in six languages, used only where the demonstration kept fewer than two pages
FURNITURE_WORDS = frozenset({
    "preis", "price", "prix", "prezzo", "precio", "prijs", "euro", "inkl", "versand", "shipping", "sale", "neu", "new",
    "home", "start", "seite", "page", "pagina", "menu", "warenkorb", "cart", "panier", "carrello"})
STOP_WORDS = FUNCTION_WORDS | FURNITURE_WORDS


#: a line of at most this many characters can be the site's furniture
FURNITURE_LINE_CHARS = 80


def site_furniture(demo: Dict[str, Any]) -> Optional[set]:
    """The words of the visible LINES that stand, word for word, on every page of the demonstration (its start page,
    its result pages, the page it ended on: at least two different pages with visible text) - the site's furniture by
    its own pages, in any language (a cart link, a promotion banner, the shop's name). Casefolded; None when fewer
    than two pages can be compared."""
    pages: Dict[str, set] = {}
    for p in [st.get("page") or {} for st in demo.get("steps") or []] + [demo.get("final_page") or {}]:
        if not isinstance(p, dict):
            continue
        url = str(p.get("url") or "")
        lines = {" ".join(ln.split()).casefold() for ln in str(p.get("text") or "").split("\n")
                 if ln.strip() and len(ln.strip()) <= FURNITURE_LINE_CHARS}
        if url and lines and url not in pages:
            pages[url] = lines
    if len(pages) < 2:
        return None
    return {t.casefold() for ln in set.intersection(*pages.values()) for t in _tokens(ln)}


def _tokens(text: Any) -> List[str]:
    import re
    return re.findall(r"[^\W\d_][\w'’\-]*[^\W_]|[^\W\d_]{2,}", str(text or ""))


def ui_tokens(page: Dict[str, Any]) -> set:
    """The words of the page's own interface, by structure (a proof that took "Hier" and "Personenseite"): the
    text of its header, navigation and footer controls, and the words of a link text the page uses
    for two or more different targets (a per-card label: "Personenseite", "Kulturtipps | Kunst"). Casefolded tokens; no
    word list."""
    els = [e for e in page.get("elements") or [] if isinstance(e, dict)]
    out: set = set()
    by: Dict[str, set] = {}
    for e in els:
        text = " ".join(str(e.get("text") or "").split())
        if not text:
            continue
        if str(e.get("area") or "") in ("header", "nav", "footer"):
            out.update(t.casefold() for t in _tokens(text))
        if e.get("tag") == "a" and e.get("href"):
            by.setdefault(text.casefold(), set()).add(str(e.get("href")).split("#", 1)[0])
    for text, hrefs in by.items():
        if len(hrefs) >= 2:
            out.update(t.casefold() for t in _tokens(text))
    return out


def label_tokens(titles: Sequence[str]) -> set:
    """By structure (the "Only" of titles ending ", Tool Only"): the words that occur in the titles ONLY
    inside a repeated trailing label - the same last two words ending at least two titles and a quarter of them (a
    variant label such as "Tool Only", "- HOBELMASCHINE"). Casefolded; no word list."""
    toks = [[t.casefold() for t in _tokens(x)] for x in titles]
    tails = [" ".join(t[-2:]) for t in toks if len(t) >= 3]
    from collections import Counter
    common = {tail for tail, n in Counter(tails).items() if n >= 2 and n >= max(2, len(titles) // 4)}
    if not common:
        return set()
    inside: Dict[str, int] = {}
    outside: set = set()
    for t in toks:
        tail = " ".join(t[-2:]) if len(t) >= 3 else ""
        cut = len(t) - 2 if tail in common else len(t)
        for i, w in enumerate(t):
            if i >= cut:
                inside[w] = inside.get(w, 0) + 1
            else:
                outside.add(w)
    return {w for w in inside if w not in outside}


def visible_words(page: Dict[str, Any]) -> set:
    """The words the result page SHOWS (its visible text and main text): a title word found only in an attribute (an
    image's alt, an aria-label, a title attribute; the "Picture" of image-alt titles "Picture of ...")
    is no proof value. Empty when the page kept no visible text (no rule then)."""
    text = " ".join([str(page.get("text") or ""), str(page.get("main_text") or "")])
    return {t.casefold() for t in _tokens(text)}


#: proof values a real search can answer.
#: * ``EVERY_CHECK`` (a filter): a word in at least ``EVERY_SHARE`` of >= ``EVERY_MIN`` titles that the result URLs do
#:   not carry is the cards' TEMPLATE, never a proof value - a price clause ("inkl. MwSt." where the titles are the
#:   whole card text), a badge ("Lebensmittelkennzeichnungen").
#:   A search for it lists every card (it even satisfies the query check: every card "names" it). A word of every title
#:   that the URLs DO carry is a name (the "Haakse" of every "haakse boormachine" passed live).
#: * ``SLUG_CHECK`` (an order): when the site's result URLs NAME their results (at least ``SLUG_SHARE`` of the results
#:   share ``SLUG_MIN_WORDS`` or more words, the demonstrated value's aside, with their own title), the words the URLs
#:   carry are tried first - a site names a result by its name there, never by a function word, a sort label or a
#:   state ("Wann", "nötig", "Alle", "aktuell", "Saftig", "Erscheinungsdatum"); the other words follow
#:   (never dropped: a shop's "/p/<model>" URL omits its product type).
#: Measured on every compile recording.
SLUG_CHECK = True
SLUG_SHARE = 0.5
SLUG_MIN_WORDS = 2
EVERY_CHECK = True
EVERY_SHARE = 0.9
EVERY_MIN = 4
#: (the reported titles were "Lot 236" - no word - while the same result URL's
#: other link named the coin) the words of a result's OTHER link texts on the demonstration's page (links to the
#: same URL) are candidates after the title words (source ``result_links``)
RESULT_LINKS = True
#: a drop-down option is a proof value only when a result shows it (``site_vocabulary``'s ``facet`` source)
FACET_SHOWN = True
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "å": "aa"})


def _fold_word(text: Any) -> str:
    """Casefolded, umlauts transliterated, accents stripped (a title's "Händler" is a URL's "haendler")."""
    import unicodedata
    s = str(text or "").casefold().translate(_TRANSLIT)
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def slug_text(rows: Sequence[Tuple[str, str]], demonstrated: str = "") -> Optional[str]:
    """The words of the result URLs' paths (folded, joined by spaces) when the URLs NAME their results - at least
    ``SLUG_SHARE`` of the ``(title, url)`` rows share ``SLUG_MIN_WORDS`` or more words of three letters with their own
    title, the demonstrated value's words aside -, else None (ids or categories in the path: no evidence, as in
    "/plissee/xart-19321")."""
    dem = {w for w in re.split(r"[^a-z0-9]+", _fold_word(demonstrated)) if w}
    parts, naming = [], 0
    for title, u in rows:
        path = urllib.parse.unquote(urllib.parse.urlsplit(str(u or "")).path)
        ps = [w for w in re.split(r"[^a-z0-9]+", _fold_word(path)) if w]
        parts.append(ps)
        tw = {w for w in re.split(r"[^a-z0-9]+", _fold_word(title)) if len(re.sub(r"[^a-z]", "", w)) >= 3} - dem
        if len(tw & set(ps)) >= SLUG_MIN_WORDS:
            naming += 1
    if len(rows) < 2 or naming < SLUG_SHARE * len(rows):
        return None
    return " " + " ".join(w for ws in parts for w in ws) + " "


def in_slugs(token: str, slugs: Optional[str]) -> bool:
    """Is every part (three characters or more: a word, a model number) of ``token`` inside the result URLs' words?
    True without evidence."""
    if slugs is None:
        return True
    parts = [w for w in re.split(r"[^a-z0-9]+", _fold_word(token)) if len(w) >= 3]
    return bool(parts) and all(w in slugs for w in parts)


def furniture_tokens(titles: Sequence[str]) -> set:
    """``EVERY_CHECK``: the casefolded words in at least ``EVERY_SHARE`` of the titles (>= ``EVERY_MIN`` titles)."""
    from collections import Counter
    if not EVERY_CHECK or len(titles) < EVERY_MIN:
        return set()
    n = Counter(k for t in titles for k in {x.casefold() for x in _tokens(t)})
    return {k for k, c in n.items() if c >= EVERY_SHARE * len(titles)}


def _result_link_texts(demo: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> List[str]:
    """``RESULT_LINKS``: per result, the texts of the OTHER links to its URL on the demonstration's final page."""
    from anyuse.execution import demo_compile as C
    els = [e for e in (demo.get("final_page") or {}).get("elements") or [] if isinstance(e, dict)
           and e.get("tag") == "a" and e.get("href")]
    out = []
    for r in results:
        u, title = C._norm(str(r.get("url") or "")), " ".join(str(r.get("title") or "").split()).casefold()
        texts = []
        for e in els:
            tx = " ".join(str(e.get("text") or "").split())
            if tx and C._norm(str(e["href"])) == u and tx.casefold() != title and tx not in texts:
                texts.append(tx)
        out.append(" ".join(texts))
    return out


def site_vocabulary(demo: Dict[str, Any], demonstrated: str = "") -> List[Dict[str, Any]]:
    """Proof values from the SITE'S OWN vocabulary (values picked without the site, such as "Fritzbox" / "Cava", were
    refused by honest tools), deterministic, each with its source:

    * ``result_titles``: distinctive words of the demonstration's own result titles (>= 4 letters, no function word, no
      label, nothing of the demonstrated value; shown in the page's visible text - never only in an image alt or
      another attribute - and not only inside a repeated trailing label of the titles), the words most titles share
      first (more titles -> results likely), then by first appearance;
    * ``result_links``: the same words of a result's OTHER link texts on the page (``RESULT_LINKS``);
    * ``facet``: the options of the result page's own drop-downs (brands, categories), the placeholder skipped.

    Never a word of the cards' template (``EVERY_CHECK``); the words the result URLs carry first
    (``SLUG_CHECK``). The site's autocomplete is not asked (it would cost requests and needs its endpoint found
    first)."""
    from collections import Counter
    from anyuse.execution import answer_refs as AR
    if demo.get("workflow") == "product_lookup":
        return _listed_products(demo, demonstrated)
    dem = {t.casefold() for t in _tokens(demonstrated)}
    dem_text = str(demonstrated or "").casefold()
    results = [r for r in ((demo.get("result") or {}).get("results") or []) if isinstance(r, dict) and r.get("title")]
    titles = [str(r.get("title") or "") for r in results]
    ui = ui_tokens(demo.get("final_page") or {})
    # by structure (where the word comes from): a word of a repeated trailing label, a word the page only has in
    # an attribute (an image alt) - never a proof value; nor a word of the cards' template
    labels = label_tokens(titles)
    shown = visible_words(demo.get("final_page") or {})
    # the site's furniture by its own pages first; the furniture lexicon only where they cannot be compared
    furniture = site_furniture(demo)
    stop = FUNCTION_WORDS | (furniture if furniture is not None else FURNITURE_WORDS)
    links = _result_link_texts(demo, results) if RESULT_LINKS else ["" for _r in results]
    # (the reported titles were availability badges, the results' names were their
    # other link texts) a result's names are its title AND its other link texts - the URL evidence reads both
    rows = [(f"{r.get('title') or ''} {lt}", str(r.get("url") or "")) for r, lt in zip(results, links)]
    slugs = slug_text(rows, demonstrated) if (SLUG_CHECK or EVERY_CHECK) else None

    def words(texts: Sequence[str], source: str) -> List[Dict[str, Any]]:
        every = furniture_tokens([x for x in texts if x])
        df: Counter = Counter()
        first: Dict[str, Any] = {}
        for n, t in enumerate(texts):
            for pos, tok in enumerate(dict.fromkeys(_tokens(t))):
                k = tok.casefold()
                if len(k) < 4 or k in stop or AR.is_label(tok) or k in dem or \
                        any(d in k for d in dem if len(d) >= 4) or k in dem_text or k in ui or k in labels or \
                        (shown and k not in shown) or (k in every and not (slugs is not None and in_slugs(tok, slugs))):
                    continue
                df[k] += 1
                first.setdefault(k, (n, pos, tok, t))
        return [{"value": first[k][2], "source": source, "titles": df[k], "from": first[k][3][:120]}
                for k in _proof_order(df, first)]
    out: List[Dict[str, Any]] = words(titles, "result_titles")
    if RESULT_LINKS:
        seen = {o["value"].casefold() for o in out}
        out += [o for o in words(links, "result_links") if o["value"].casefold() not in seen]
    if SLUG_CHECK and slugs is not None:
        # the words the result URLs carry first - across both sources (title words before link words within each part)
        out = [o for o in out if in_slugs(o["value"], slugs)] + [o for o in out if not in_slugs(o["value"], slugs)]
    page = demo.get("final_page") or {}
    # (a proof took 'Erscheinungsdatum', an option of the result page's SORT
    # drop-down) an option is a facet value only when a result shows it (a title or a result's link text names it) - a
    # sort order, a page size or a view names no result
    from anyuse.execution import demo_compile as C
    shown_by = titles + (_result_link_texts(demo, results) if RESULT_LINKS else [])
    for e in page.get("elements") or []:
        if not isinstance(e, dict) or e.get("tag") != "select":
            continue
        for o in (e.get("options") or [])[1:30]:
            txt = " ".join(str((o or {}).get("text") if isinstance(o, dict) else o or "").split())
            k = txt.casefold()
            if len(k) < 3 or not any(ch.isalpha() for ch in k) or k in dem_text or k in stop or AR.is_label(txt):
                continue
            if FACET_SHOWN and not any(C.names_value(x, txt) for x in shown_by if x):
                continue
            out.append({"value": txt, "source": "facet", "from": str(e.get("text") or "")[:80]})
    seen, uniq = set(), []
    for v in out:
        if v["value"].casefold() not in seen:
            seen.add(v["value"].casefold())
            uniq.append(v)
    return uniq


def _proof_order(df: Dict[str, int], first: Dict[str, Any]) -> List[str]:
    """The order the title words are tried in (:func:`site_vocabulary`): the words more titles share first, then by first
    appearance. A "distinguishing" order (a word of only one or two titles, rare in the rest of the page, the longer
    first) was measured against every value tried live (34 recordings that compile): of the values
    that passed, many recur across titles and in the cards' other text ("Reserva", "Aduro", "Sony"); the order would
    have replaced the known-passing first two values in all 13 recordings that had them with untried long words
    ("Beschwingter", "permetterselo"), and neither the titles shared nor the count
    elsewhere separated the values that passed from those that failed. Kept."""
    return sorted(df, key=lambda k: (-df[k], first[k][0], first[k][1]))


def _listed_products(demo: Dict[str, Any], demonstrated: str = "") -> List[Dict[str, Any]]:
    """A product lookup's proof values: the OTHER products the site's own result list showed for the demonstrated
    value - the links on the page the demonstration clicked from that share the clicked link's first path segment,
    named with two or more words (page order); source ``result_titles``."""
    from anyuse.execution import answer_refs as AR
    click = next((st for st in demo.get("steps") or [] if (st.get("action") or {}).get("action") == "click"
                  and (st.get("outcome") or {}).get("href")), None)
    if click is None:
        return []
    href = str(click["outcome"]["href"])
    p = urllib.parse.urlsplit(href)
    seg = (p.path.strip("/").split("/") or [""])[0]
    out, seen = [], {AR._norm(href)}
    for e in (click.get("page") or {}).get("elements") or []:
        if not isinstance(e, dict) or e.get("tag") != "a" or not e.get("href"):
            continue
        q = urllib.parse.urlsplit(str(e["href"]))
        text = " ".join(str(e.get("text") or "").split())
        if (q.hostname or "") != (p.hostname or "") or (q.path.strip("/").split("/") or [""])[0] != seg:
            continue
        if AR._norm(str(e["href"])) in seen or len(text.split()) < 2 or AR.is_label(text) or \
                text.casefold() == str(demonstrated or "").casefold():
            continue
        seen.add(AR._norm(str(e["href"])))
        out.append({"value": text[:200], "source": "result_titles", "from": str(e["href"])[:160]})
    return out


def proof_values(demo: Dict[str, Any], caller: Sequence[str] = (), n: int = MIN_FRESH_CASES
                 ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(values to try, skipped)``: the caller's values first (they are inputs the run never saw, so a held-out
    proof by construction; a value the site has no results for is inconclusive, never a pass, and the next value is
    tried), then the site's own vocabulary. The demonstrated value itself and repeats are skipped with the reason. At
    most :data:`PROOF_TRIES` values."""
    demonstrated = str(demo.get("value") or "")
    page_text = " ".join([str((demo.get("final_page") or {}).get("text") or "")] +
                         [str(r.get("title") or "") for r in ((demo.get("result") or {}).get("results") or [])
                          if isinstance(r, dict)]).casefold()
    tries, skipped = [], []
    for v in caller:
        v = str(v or "").strip()
        if not v:
            continue
        if v.casefold() == demonstrated.casefold():
            skipped.append({"value": v, "why": "the demonstrated value itself proves nothing"})
        elif v.casefold() in {t["value"].casefold() for t in tries}:
            skipped.append({"value": v, "why": "given twice"})
        else:
            shown = v.casefold() in page_text
            tries.append({"value": v, "source": "caller, on the site's own page" if shown else "caller"})
    for c in site_vocabulary(demo, demonstrated):
        if c["value"].casefold() not in {t["value"].casefold() for t in tries}:
            tries.append(c)
    return tries[:max(PROOF_TRIES, n)], skipped


def _inconclusive(status: str, why: str) -> bool:
    """A value the site has no (attributable) results for: ``no_results``, or the tool's own query check refusing a
    list that names the value nowhere. Not a failure of the tool - and never a pass."""
    return status == "no_results" or (status == "failed" and str(why or "").startswith("query:"))


#: (a search URL that ignored the value - every value listed the same 7,045 items - and
#: both proofs passed on words of that same list) a proof must be able to fail. Two values give the SAME
#: result list when their first results (up to ``QD_TOP``, at least ``QD_MIN``) are the same URLs in the same order
#: (``QD_RULE`` "ordered"; "set": ``QD_OVERLAP`` of them in the other list, measured and not used: it took 4 honest
#: proofs on 133 recordings - 'Sony', 'nötig' - whose lists share 4 of 5 results);
#: then the tool is not a search (``not_query_dependent``), never ready - unless every shared result names BOTH values
#: (the list answers both: a news site whose top Zelda news were all "Zelda: Ocarina of Time" articles).
#: Measured on the 133 recordings: a single shared first result is no
#: list ('Microconda' / 'Microlot', 'Walnuss': one hit that also leads the
#: build list) - hence ``QD_MIN``
QD_TOP = 5
QD_MIN = 3
QD_RULE = "ordered"
QD_OVERLAP = 0.8
NOT_QUERY_DEPENDENT = "not_query_dependent"


def result_urls(out: Dict[str, Any]) -> List[str]:
    """The URLs of a replay's listed results (in order)."""
    o = out.get("output") if isinstance(out.get("output"), dict) else {}
    return [str(r.get("url") or "") for r in (o or {}).get("results") or [] if isinstance(r, dict) and r.get("url")]


def same_results(a: Sequence[str], b: Sequence[str], rule: Optional[str] = None, top: Optional[int] = None) -> int:
    """The number of first results two lists share when they are the SAME list (0 when they are not)."""
    from anyuse.execution import demo_compile as C
    k = int(top or QD_TOP)
    x = [C._norm(u) for u in a if u][:k]
    y = [C._norm(u) for u in b if u][:k]
    m = min(len(x), len(y))
    if m < QD_MIN:
        return 0
    if (rule or QD_RULE) == "set":
        shared = len(set(x[:m]) & set(y[:m]))
        return shared if shared >= max(1, int(QD_OVERLAP * m + 0.999)) else 0
    return m if x[:m] == y[:m] else 0


def build_rows(c: Dict[str, Any]) -> List[Dict[str, str]]:
    """The build value's results (``{url, title}``) as the compiled tool reads them on the demonstration's own
    result page (the reported results when the page cannot be read)."""
    from anyuse.execution import demo_compile as C
    demo, tool = c.get("demo") or {}, c.get("tool") or {}
    rows: List[Dict[str, str]] = []
    try:
        k = (c.get("report") or {}).get("search_step")
        if k is not None:
            page = C.page_after(demo, int(k))
            j = C.judge_result_page(tool, page, str(demo.get("value") or ""))
            rows = [{"url": str(r.get("url") or ""), "title": str(r.get("title") or "")} for r in j.get("results") or []
                    if r.get("url")]
    except Exception:                                  # noqa: BLE001 - the reported results then
        rows = []
    if not rows:
        rows = [{"url": str(r.get("url")), "title": str(r.get("title") or "")}
                for r in ((demo.get("result") or {}).get("results") or []) if isinstance(r, dict) and r.get("url")]
    return rows


def build_results(c: Dict[str, Any]) -> List[str]:
    """The URLs of ``build_rows``."""
    return [r["url"] for r in build_rows(c)]


def names_both(rows: Sequence[Dict[str, Any]], a: str, b: str) -> bool:
    """Does every one of ``rows`` name BOTH values (its title or its URL's path words)?"""
    from anyuse.execution import demo_compile as C

    def named(r: Dict[str, Any], v: str) -> bool:
        path = urllib.parse.unquote(urllib.parse.urlsplit(str(r.get("url") or "")).path)
        text = str(r.get("title") or "") + " " + re.sub(r"[-_/.+]+", " ", path)
        return C.names_value(text, v)
    return bool(rows) and bool(a) and bool(b) and all(named(r, a) and named(r, b) for r in rows)


#: (a search for a brewery's name redirected to the home page, another search to the one matching show's own
#: page) a proof value whose search the SITE
#: sent away from its result page - to a page that does not carry the value as the search's own parameter or path
#: segment - was answered by another page, not by a list. Its membership failure is INCONCLUSIVE (never a pass; the
#: next value is tried): the tool did not fail, the site chose not to list. A tool that reaches no list for any value
#: still never passes; a page that stays on the search URL and fails (a 404 of a category path) still fails.
REDIRECT_INCONCLUSIVE = True


def _url_key(u: str) -> Tuple[str, str]:
    p = urllib.parse.urlsplit(str(u or ""))
    return (p.hostname or "").casefold(), urllib.parse.unquote_plus(p.path).rstrip("/").casefold()


def redirected_away(tool: Dict[str, Any], out: Dict[str, Any], value: str) -> str:
    """Why the replay's page is not the search's result page for ``value`` ("" when it is, or when the tool has no
    URL template to compare with): the final URL's host and path differ from the URL the template builds, and the
    value is neither one of its query values nor one of its path segments."""
    search = tool.get("search") or {}
    tpl = str(search.get("template") or "") if search.get("mode") == "url" else ""
    final = str((out.get("final_page") or {}).get("url") or "")
    if not tpl or "{q}" not in tpl or not final:
        return ""
    v = " ".join(str(value or "").split()).casefold()
    built = tpl.replace("{q}", urllib.parse.quote(str(value or "")))
    fh, fp = _url_key(final)
    bh, bp = _url_key(built)
    if (fh, fp) == (bh, bp):
        return ""
    q = urllib.parse.parse_qsl(urllib.parse.urlsplit(final).query, keep_blank_values=True)
    if any(" ".join(x.split()).casefold() == v for _k, x in q):
        return ""
    segs = [" ".join(re.split(r"[-_+\s]+", s)).strip() for s in fp.split("/") if s]
    if " ".join(re.split(r"[-_+\s]+", v)).strip() in segs:
        return ""
    return f"the site sent the search for {value!r} to another page ({final[:120]})"


def _replay_without_model(rep: Callable[..., Dict[str, Any]], tool: Dict[str, Any], site, value: str
                          ) -> Dict[str, Any]:
    """One proof replay with the in-run verifier on its rules alone (``attribute_verify.no_model``): a proof makes 0
    model calls, so what only the verifier's model could settle is withheld and the case does not pass."""
    from anyuse.execution import attribute_verify as AV
    with AV.no_model("a proof replay asks no model"):
        return rep(tool, site, value)


def prove_web(tool: Dict[str, Any], site, fresh_values: Sequence[Any], *, demonstrated: str = "",
              replay: Optional[Callable[..., Dict[str, Any]]] = None, n: int = MIN_FRESH_CASES,
              expect: Optional[Dict[str, str]] = None, independent: bool = True,
              baseline: Sequence[Any] = ()) -> Dict[str, Any]:
    """The tool on >= ``n`` values it was not compiled from, tried in order (a value is a string or
    ``{"value", "source", ...}``; :func:`proof_values`): a case PASSES when the replay ends ``ok`` with the tool's own
    checks passing, 0 model calls and, with ``independent``, the INDEPENDENT judge's accept on
    the page the replay ended on. A case is INCONCLUSIVE when the site has no results for the value the tool can show
    (``no_results``, or the tool's query check: no listed item names the value): not a failure of the tool, never a
    pass - the next value is tried. Anything else FAILS the proof (an error, a model call, a recorded status
    that differs, the judge withholding, the demonstrated value). The proof passes with ``n`` passing cases and no
    failure. Every case keeps the page it ended on.

    A proof must be able to fail: the tool's output must DEPEND on its input - a case whose
    results are the same list (``same_results``) as the build value's (``baseline``: ``build_results``) or an earlier
    case's FAILS the proof as ``not_query_dependent``; the independent judge reads each answer against its own value."""
    rep = replay or replay_web
    cases = []
    base_rows = [b if isinstance(b, dict) else {"url": str(b), "title": ""} for b in baseline]
    lists: List[Tuple[str, List[Dict[str, Any]]]] = [(demonstrated or "the build value", base_rows)] if base_rows else []
    typed = ""
    items = [v if isinstance(v, dict) else {"value": str(v)} for v in fresh_values]
    passes = 0
    for it in items:
        if passes >= n:
            break
        v = str(it.get("value") or "")
        probs: List[str] = []
        if demonstrated and v == demonstrated:
            probs.append("the demonstrated value is not a fresh value")
            cases.append({"value": v, "passed": False, "problems": probs, "source": it.get("source")})
            break
        out = _replay_without_model(rep, tool, site, v)
        status = str(out.get("status") or "")
        why = str(out.get("why") or "")
        incon = _inconclusive(status, why) and not (expect and v in expect)
        away = redirected_away(tool, out, v) if REDIRECT_INCONCLUSIVE and status == "failed" and \
            why.startswith("membership:") and not (expect and v in expect) else ""
        if away:
            incon, why = True, f"redirected: {away}; {why}"
        if status not in ("ok", "no_results") and not incon:
            probs.append(f"status {status}: {why[:160]}")
        if int(out.get("model_calls") or 0) != 0:
            probs.append(f"{out.get('model_calls')} model call(s) in a replay")
        if expect and v in expect and expect[v] != status:
            probs.append(f"the recorded run says {expect[v]}, the replay says {status}")
        ind = independent_verdict(tool, out, v) if independent and status == "ok" else None
        if ind is not None and ind["verdict"] != "accept":
            probs.append(f"the independent judge does not accept the answer ({ind['verdict']}: {ind['why'][:160]})")
        mine = [{"url": str(r.get("url") or ""), "title": str(r.get("title") or "")}
                for r in ((out.get("output") or {}).get("results") or []) if isinstance(r, dict) and r.get("url")] \
            if status == "ok" and isinstance(out.get("output"), dict) else []
        if mine:
            for other, rows in lists:
                same = same_results([r["url"] for r in rows], [r["url"] for r in mine])
                if same and names_both(mine[:same], other, v):
                    case_note = f"the same {same} first result(s) as {other!r}, each naming both values"
                    it = dict(it, query_dependence=case_note)
                elif same:
                    probs.insert(0, f"{NOT_QUERY_DEPENDENT}: the results for {v!r} are the results for {other!r} "
                                    f"(the same {same} first result(s)): the search does not depend on its input")
                    typed = typed or NOT_QUERY_DEPENDENT
                    break
            lists.append((v, mine))
        case = {"value": v, "status": status, "passed": not probs and status == "ok",
                "inconclusive": bool(incon and not probs), "problems": probs,
                "model_calls": int(out.get("model_calls") or 0), "why": why[:200],
                "n_results": len((out.get("output") or {}).get("results") or []) if isinstance(out.get("output"), dict) else 0,
                "page": _page_record(out.get("final_page"))}
        for k in ("source", "from", "titles", "query_dependence"):
            if it.get(k) is not None:
                case[k] = it[k]
        if ind is not None:
            case["independent"] = ind
        cases.append(case)
        if probs:
            break                                 # a failure of the tool: the proof fails, no further value is spent
        if case["passed"]:
            passes += 1
    failed = next((c for c in cases if c.get("problems")), None)
    passed = passes >= n and failed is None
    inconc = sum(1 for c in cases if c.get("inconclusive"))
    if passed:
        reason = (f"{passes}/{passes} fresh values replayed at 0 model calls" +
                  (", each accepted by the independent judge" if independent else "") +
                  (f" ({inconc} value(s) without results on the site: inconclusive, not counted)" if inconc else ""))
    elif failed is not None:
        reason = f"value {failed['value']!r}: " + "; ".join(failed["problems"])[:300]
    else:
        reason = (f"only {passes} fresh value(s) passed of {len(cases)} tried ({inconc} without results on the site: "
                  f"inconclusive); {n} needed")
    got = {"passed": passed, "reason": reason, "cases": cases, "min_cases": n, "independent": bool(independent),
           "model_calls": sum(c.get("model_calls", 0) for c in cases), "inconclusive": inconc,
           "query_dependence": {"baseline": [r["url"] for r in base_rows][:QD_TOP], "rule": QD_RULE, "top": QD_TOP,
                                "min": QD_MIN}}
    if typed and not passed:
        got["typed"] = typed
    return got


def save_web_tool(storage, name: str, tool: Dict[str, Any], proof: Dict[str, Any], demo: Dict[str, Any],
                  goal: str = "", param: str = "") -> Dict[str, Any]:
    """Persist beside an ordinary ``Workflow`` entry: ``compiled_demo.json`` when the proof passed (status
    ``ready``), else ``compiled_demo.draft.json`` with the reasons. A ready tool of the same name is never
    replaced (the new compile is a draft beside it)."""
    import hashlib
    from anyuse.memory.models import Workflow
    if storage is None:
        # the product's own workflow store (`anyuse agent run --compile` passes none, and without it every
        # web compile ended "compile failed: AttributeError" after its proof - the desktop seam already defaults)
        from anyuse.memory.storage import WorkflowStorage
        storage = WorkflowStorage()
    existing = storage.get(name)
    status = "ready" if proof.get("passed") else "draft"
    reasons = [] if proof.get("passed") else [str(proof.get("reason") or "proof not passed")]
    if existing is not None:
        old = load_web_tool(storage, name)
        if old and old.get("status") == "ready":
            status = "draft"
            reasons.append(f"shadow: a ready tool {name!r} exists; this compile is kept beside it as a draft")
    wf = existing or Workflow.create_new(name, goal or str(demo.get("workflow") or ""))
    if existing is None:
        wf.tags = ["agent-harness", "web", str(demo.get("site") or "")]
        storage.save(wf)
    d = str(storage.get_workflow_dir(wf.id))
    os.makedirs(d, exist_ok=True)
    rec = {"format": "agent_harness.web_tool/1", "workflow": name, "workflow_id": wf.id, "kind": "web",
           "site": demo.get("site"), "value": demo.get("value"), "tool": tool,
           # the task's own placeholder ({q}): the saved tool's input is named after it
           "param": param or "value",
           "tool_sha256": hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
           "proof": proof, "status": status, "status_reasons": reasons,
           "harness_run_id": demo.get("harness_run_id"), "harness_digest": demo.get("harness_digest"),
           "demonstrated_by": demo.get("demonstrated_by") or "",
           "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = os.path.join(d, WEB_TOOL_FILE if status == "ready" else WEB_DRAFT_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    if status == "draft" and existing is None:
        stale = os.path.join(d, WEB_TOOL_FILE)
        if os.path.isfile(stale):
            os.remove(stale)
    return {"workflow_id": wf.id, "name": name, "status": status, "path": path, "reasons": reasons, "dir": d}


def load_web_tool(storage, name: str) -> Optional[Dict[str, Any]]:
    wf = storage.get(name)
    if wf is None:
        return None
    d = str(storage.get_workflow_dir(wf.id))
    for f in (WEB_TOOL_FILE, WEB_DRAFT_FILE):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
    return None


def compile_and_save_web(trace: T.Trace, storage, site, fresh_values: Sequence[str], *, name: str = "",
                         workflow: str = "site_search", expect: Optional[Dict[str, str]] = None,
                         replay: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The whole web path: compile -> proof on fresh values -> saved (or a draft with the reasons)."""
    from anyuse.execution.model_adapter import calls_since, process_call_count
    mark = process_call_count()
    # the saved name is the callable MCP tool name (<host>__<workflow>), so agent_run reports what a caller calls
    from anyuse.mcpserver.compiled_tools import typed_tool_name
    name = name or typed_tool_name(trace.site or "site", workflow)
    param = next(iter(getattr(trace, "values", None) or {}), "") or "value"
    c = compile_web(trace, workflow)
    if not c["ok"]:
        return {"status": "draft", "name": name, "reasons": [c["why"]], "compile": c, "proof": None, "saved": None,
                "model_calls": calls_since(mark)}
    demonstrated = str(c["demo"].get("value") or "")
    # the proof's values come from the site's own vocabulary (a caller's value only when the site shows it)
    tries, skipped = proof_values(c["demo"], fresh_values)
    # the build value's own results, as the tool reads them, are the first list every proof value is compared with
    proof = prove_web(c["tool"], site, tries, demonstrated=demonstrated, replay=replay, expect=expect,
                      baseline=build_rows(c))
    proof["values"] = tries
    proof["skipped_values"] = skipped
    from anyuse.execution import empty_page as EP
    if proof.get("passed") and EP.probe_enabled():
        # the site's own empty page, from two replays of made-up inputs; saved with the tool, so its digest pins it
        rep = replay or replay_web
        sig = EP.learn_demo(lambda v: _replay_without_model(rep, c["tool"], site, v), proof.get("cases") or [],
                            [demonstrated] + [str(t.get("value") if isinstance(t, dict) else t) for t in tries],
                            seed=str(c["tool"].get("site") or ""))
        proof["empty_probes"] = EP.PROBES
        if sig is not None:
            c["tool"]["empty_page"] = sig
    saved = save_web_tool(storage, name, c["tool"], proof, c["demo"], goal=trace.task, param=param)
    return {"status": saved["status"], "name": name, "reasons": saved["reasons"], "compile": {k: c[k] for k in ("why", "validity")},
            "tool": c["tool"], "proof": proof, "saved": saved, "model_calls": calls_since(mark)}


# ---------------------------------------------------------------------------
# desktop: trace -> drive recording -> the draft seam -> saved workflow -> replay
# ---------------------------------------------------------------------------
_AX_VERBS = {T.AxPress.kind: "press", T.AxToggle.kind: "toggle", T.AxEnterText.kind: "enter_text",
             T.AxKey.kind: "press_key"}


def trace_to_drive_recording(trace: T.Trace, name: str = "") -> Dict[str, Any]:
    """The trace as the measurement harness record``'s ``workflow.json`` (section 5): the planned
    AX steps that ran (``outcome.data["step"]``, the guest's own command) with the fingerprint of the state
    before each (the observation's), the workflow intents, the state graph."""
    wf_steps: List[Dict[str, Any]] = []
    plan: List[Dict[str, Any]] = []
    states: Dict[str, Dict[str, Any]] = {}
    for s in trace.steps:
        if s.action is None or s.action.kind not in _AX_VERBS or not s.outcome.ok:
            continue
        step = dict((s.outcome.data or {}).get("step") or {})
        if not step:
            continue
        sid = s.observation.state_id or ""
        if sid and s.observation.fingerprint:
            states[sid] = dict(s.observation.fingerprint, state_id=sid)
        cmd = dict(step.get("command") or {})
        wf_steps.append({"intent": str(step.get("intent") or "")[:200], "target_element": str(step.get("target_element") or ""),
                         "expected_before_state": sid, "input_type": str(cmd.get("action") or ""),
                         "input_data": step.get("input_data")})
        plan.append({"intent": str(step.get("intent") or "")[:200], "source": "workflow",
                     "target_element": str(step.get("target_element") or ""), "modality": "ax", "command": cmd,
                     "input_data": step.get("input_data"), "needs_visual": False, "expected_after": "",
                     "expected_after_state": None, "reasoning": ""})
    name = name or f"{(trace.app or 'app').lower().replace(' ', '_')}.{trace.run_id}"
    return {"workflow": {"id": trace.run_id, "name": name, "description": trace.task[:500], "parameters": [],
                         "steps": wf_steps, "required_app": trace.app},
            "plan": plan, "graph": {"states": states}, "text": trace.task[:500], "name": name,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "taught_by": f"agent harness run {trace.run_id}"[:120]}


def value_overrides(recording: Dict[str, Any], values: Dict[str, str]) -> List[Dict[str, str]]:
    """The run's own named values as parameter overrides: each value that was typed in the recording binds the
    candidate carrying that text (``draft_params.collect_candidates``); no model proposal."""
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import draft_params as DP
    source = DC.load_source(dict(recording))
    cands = DP.collect_candidates(source.steps)
    out: List[Dict[str, str]] = []
    for pname, v in values.items():
        c = next((c for c in cands if c.source == "typed" and c.value == v), None)
        if c is not None:
            out.append({"name": pname, "ref": "@" + c.id, "type": ""})
    return out


async def compile_desktop(trace: T.Trace, storage, *, name: str = "", fresh: Optional[Sequence[Dict[str, str]]] = None,
                          verdicts=None) -> Dict[str, Any]:
    """The draft seam end to end on the rendered recording; returns ``draft_to_workflow``'s report plus the
    recording and the model calls (0: no proposal is asked)."""
    from anyuse.execution import draft_compile as DC
    from anyuse.execution.model_adapter import calls_since, process_call_count
    mark = process_call_count()
    if trace.stop != T.Stop.DONE:
        return {"status": "draft", "reasons": [f"the run did not finish ({trace.stop}: {trace.stop_reason[:160]})"],
                "recording": None, "model_calls": 0}
    rec = trace_to_drive_recording(trace, name)
    overrides = value_overrides(rec, trace.values)
    missing = [k for k in trace.values if k not in {o["name"] for o in overrides}]
    report = await DC.draft_to_workflow(rec, overrides=overrides, storage=storage, fresh=fresh, verdicts=verdicts)
    report["recording"] = rec
    report["overrides"] = overrides
    if missing:
        report.setdefault("notes", []).append(f"values never typed in the run (not parameters): {missing}")
    report["model_calls"] = calls_since(mark)
    return report


async def replay_desktop(storage, name: str, values: Dict[str, str], *, world=None, executor=None,
                         preflight=None, verdicts=None) -> Dict[str, Any]:
    """``draft_compile.replay_saved`` with an approver that is never asked (a replay started with values asks
    nothing; the record's own level decides the holds) and 0 model calls, reported from the process counter."""
    from anyuse.execution import draft_compile as DC
    from anyuse.execution.model_adapter import calls_since, process_call_count
    mark = process_call_count()
    asked: List[Dict[str, Any]] = []

    async def approver(ctx):
        asked.append(dict(ctx))
        if world is not None and hasattr(world, "note_hold"):
            world.note_hold()
        return True
    gates = preflight or (lambda: {"may_drive": True, "drive_enabled": True, "interlock": {"allowed": True}})
    ex = executor
    if ex is None and world is not None:
        ex = DC.checked_executor(world)
    on_step = getattr(world, "at_step", None) if world is not None else None
    out = await DC.replay_saved(name, values, approver=approver, storage=storage, executor=ex, preflight=gates,
                                on_step=on_step, verdicts=verdicts)
    out["asked"] = len(asked)
    out["model_calls"] = calls_since(mark)
    return out


def compile_success(trace: T.Trace, storage, **kw) -> Dict[str, Any]:
    """Dispatch by the trace's kind (web needs ``site`` and ``fresh_values``; desktop runs the draft seam)."""
    from anyuse.execution.draft_compile import run_sync
    if trace.kind == "web":
        return compile_and_save_web(trace, storage, kw["site"], kw["fresh_values"],
                                    **{k: v for k, v in kw.items() if k in ("name", "workflow", "expect", "replay")})
    if trace.kind == "desktop":
        return run_sync(compile_desktop(trace, storage, **{k: v for k, v in kw.items() if k in ("name", "fresh", "verdicts")}))
    return {"status": "draft", "reasons": [f"no compile seam for a {trace.kind} trace yet (reported, not saved)"],
            "model_calls": 0}
