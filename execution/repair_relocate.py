"""Tier 1 of the repair ladder: the failing step LOCALIZED and RE-LOCATED by
general rules, with NO model. Step-local: only the step that broke is re-found, and every answer is a patch in the
closed language of ``repair_patch`` (validated, applied to a copy, accepted only by the ladder's gate).

* ``localize(tool, out)``: the step a tier-0 failure points at - ``search`` (the query did not reach the site's search),
  ``region`` (the result containers are not read), ``refine`` (a refinement was not applied), ``pager`` (a later page
  was not reached) - most likely first; nothing for a block, an infrastructure error, a product page's identity or
  fields (no re-locate rule: tier 2 / 3).
* ``probe(page, tool, value, settle)``: the evidence, read on a FRESH page with the tool's own search navigation (no
  other request): the result page with its DOM; for a search failure also the start page.
* ``candidates(step, tool, value, ev, anchors)``: the re-located targets, from the tool's recorded ANCHORS:

  - region: the last known good answer's results (URL and title) that the page still lists - else the links of the
    region's URL pattern whose text names the input - give the result CARDS again (``demo_compile.compile_cards``:
    the compile's own rule, its query-region and widget checks included) -> ``replace_anchor cards``;
  - search: the site's own GET search form (a search-like input in a form on the result or the start page, on the
    tool's own host) gives the parameter that carries the input -> ``rename_url_param search`` or ``rebind_param``
    (a new path); a typed search's input found again by its name / id -> ``replace_anchor search_input``;
  - refine: a URL parameter on the page's links / drop-downs carrying the recorded VALUE under another name whose
    label classifies the same (``demo_breadth.classify_param``: the same sort key, range or facet) ->
    ``rename_url_param refine``; a control whose label names the same sort order -> ``replace_anchor refine_control``;
  - pager: page 1's next-page link (``demo_breadth.next_link``) gives the page mechanism again (``_page_mech``) ->
    ``rename_url_param pager`` or ``replace_anchor pager``.

PURE apart from ``probe``.
"""
from __future__ import annotations

import copy
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W

STEPS = ("search", "region", "refine", "pager")
#: links a query-named re-locate takes as its anchors
MAX_WANTED = 6
#: the share of the input's words a query-named link must name
NAMED_SHARE = 0.75


def localize(tool: Dict[str, Any], out: Dict[str, Any]) -> List[str]:
    """The steps a tier-0 outcome points at, most likely first (``[]``: nothing to re-locate)."""
    st = str(out.get("status") or "")
    why = str(out.get("why") or "")
    if st in ("ok", "no_results", "blocked", "error", ""):
        return []
    wf = tool.get("workflow")
    if why.startswith("search_page:"):
        return ["search"]
    if why.startswith("refine:") or any(v.get("status") == "failed" for v in (out.get("checks") or {}).get("refined") or []):
        return ["refine", "region"] if wf == "filtered_search" else ["region"]
    if wf == "paged_search" and why.startswith("paged:") and "result card" not in why:
        return ["pager"]
    if why.startswith(("membership:", "incomplete:", "query:")) or "result card" in why or "could not be read" in why:
        # the query did not reach the site's search when the result page does not name the input (a search parameter
        # the site ignores shows another page): the search first; else the result structure first
        pages = out.get("pages") or []
        txt = " ".join(str((pages[0] if pages else {}).get(k) or "") for k in ("title", "h1", "text"))
        missed = bool(pages) and C.name_share(txt, out.get("value") or "") < 0.5
        return ["search", "region"] if missed or why.startswith("query:") else ["region", "search"]
    if st == "incomplete" and wf in ("filtered_search", "paged_search", "visit_results"):
        return ["region"]
    return []


# ---------------------------------------------------------------------------
# the evidence
# ---------------------------------------------------------------------------

async def probe(page, tool: Dict[str, Any], value: str, settle: Callable, step: str) -> Dict[str, Any]:
    """The failing step's evidence on a fresh page: the tool's own search (``demo_tool._search``), the result page read
    with its DOM and the tool's cards; for ``search`` also the start page (one more GET of the site's own start page).
    ``{"result": read, "start": read | None, "status", "why", "gotos"}``."""
    from anyuse.execution import demo_tool as T
    out: Dict[str, Any] = {"value": value, "status": "", "why": "", "checks": {}, "pages": [], "model_calls": 0,
                           "search_via": ""}
    ev: Dict[str, Any] = {"result": None, "start": None, "status": "", "why": ""}
    got = await T._search(page, tool, value, settle, out, time.monotonic())
    if got.get("done"):
        ev.update(status=out.get("status"), why=out.get("why"))
        if out.get("status") == "blocked":
            return ev
    ev["result"] = await T.read_full(page, with_dom=True, tool=tool, own_microdata=True, struck=True, cap11=C.v11(tool))
    if step == "search":
        await page.goto(str((tool.get("search") or {}).get("start_url") or tool.get("start_url")))
        await settle(page)
        ev["start"] = await T.read_full(page, with_dom=True)
    return ev


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------

def candidates(step: str, tool: Dict[str, Any], value: str, ev: Dict[str, Any],
               anchors: Sequence[Sequence[str]] = (), avoid: Sequence[str] = (),
               fallback: bool = False) -> List[Dict[str, Any]]:
    """The re-located targets of ``step`` as patches: ``[{"rule", "patch", "note"}]`` (at most 2; possibly none).
    ``avoid``: the result URLs of a KNOWN WRONG answer - never re-location anchors. ``fallback``
    (off by default): a search step with no GET search form on the result or the start page takes
    the tool's recorded fallback search URLs (``_fallback_search``)."""
    read = ev.get("result") or {}
    if not read.get("dom") and step != "search":
        return []
    if step == "region":
        got = _region(tool, value, read, anchors, avoid)
    elif step == "search":
        got = _search(tool, value, read, ev.get("start"))
        if fallback and not any(c.get("rule") == "search: the site's own GET search form" for c in got):
            got = got + _fallback_search(tool)
    elif step == "refine":
        got = _refine(tool, value, read)
    elif step == "pager":
        got = _pager(tool, value, read)
    else:
        got = []
    out, seen = [], set()
    for c in got:
        k = repr(c["patch"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out[:2]


def _links(read: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in read.get("elements") or [] if e.get("tag") == "a" and e.get("href")]


def wanted_of(tool: Dict[str, Any], value: str, read: Dict[str, Any],
              anchors: Sequence[Sequence[str]] = (), avoid: Sequence[str] = ()) -> Tuple[List[Tuple[str, str]], str]:
    """The anchors a region re-locate reads the page by: the last known good answer's results the page still lists
    (by URL; a product by its path), else the links of the region's URL pattern whose text names the input (outside
    the page's header, navigation and footer). ``avoid``: a known wrong answer's result URLs are never
    anchors. ``(wanted, how)``."""
    by_path = tool.get("workflow") == "product_lookup"
    bad = {C._norm(str(u)) for u in avoid}
    links = [e for e in _links(read) if C._norm(C.strip_fragment(str(e["href"]))) not in bad]
    have = [(str(u), str(t)) for u, t in anchors
            if any(C._same_result(str(u), str(e["href"]), by_path) for e in links)]
    if have:
        return have[:MAX_WANTED * 2], "the last known good answer's results"
    rx = (tool.get("region") or {}).get("regex") or ""
    import re
    try:
        pat = re.compile(rx) if rx else None
    except re.error:
        pat = None
    out: List[Tuple[str, str]] = []
    seen = set()
    for e in links:
        u = C.strip_fragment(str(e["href"]))
        if e.get("area") in ("header", "nav", "footer") or not C.same_site(u, str(tool.get("site") or "")):
            continue
        if pat is not None and not pat.match(u):
            continue
        if C.name_share(f"{e.get('text') or ''} {e.get('ctx') or ''}", value) < NAMED_SHARE:
            continue
        if C._norm(u) in seen or W.forbidden_control(e.get("text"), u):
            continue
        seen.add(C._norm(u))
        out.append((u, str(e.get("text") or "")))
        if len(out) >= MAX_WANTED:
            break
    return out, "the region's links naming the input"


def _region(tool: Dict[str, Any], value: str, read: Dict[str, Any], anchors: Sequence[Sequence[str]],
            avoid: Sequence[str] = ()) -> List[Dict[str, Any]]:
    region = tool.get("region") or {}
    if not region.get("cards"):
        return []
    wanted, how = wanted_of(tool, value, read, anchors, avoid)
    if not wanted:
        return []
    # the broken structure's own card map is left out: the candidate structures are read on the DOM
    read = {k: v for k, v in read.items() if k != "cards"}
    wf = "product_lookup" if tool.get("workflow") == "product_lookup" else "site_search"
    cand = copy.deepcopy(region)
    report: Dict[str, Any] = {}
    # a version-11 region's cards are described without BEM modifiers (``demo_cards.bem_free``): the re-derived
    # cards must be described the way its run-time read (``CARDS_JS_V11``) describes the page. A version-15 region
    # is re-derived by version 15's card rule and reading, the site's own evidence first (``web_arch.site_evidence``),
    # as its compile read the page
    v15 = int(region.get("cards_version") or 0) >= C.CARDS_VERSION_V15
    cv = C.CARDS_VERSION_V15 if v15 else C.CARDS_VERSION_V12 if C.cards_v12(region) else \
        C.CARDS_VERSION_V11 if C.cards_v11(region) else 10
    with W.site_evidence(W.site_evidence_first() or v15):
        why = C.compile_cards(cand, read, wanted, wf, report, v8=True, value=value, start_page=None,
                              overlay=bool((tool.get("search") or {}).get("overlay")), v10=True, cards_version=cv)
    specs = cand.get("cards") or []
    if why or not specs or specs == region.get("cards") or len(specs) > 4 or cand.get("containers"):
        return []
    early = foreign_before(read, specs, [u for u, _t in wanted], region.get("regex") or "", bem=C.cards_v11(region))
    if early:
        # the structure also reads a container BEFORE the anchored results that holds none of them (a promo block, a
        # teaser in the results' own markup): it would answer it - never a candidate
        return []
    return [{"rule": "region: the result cards re-derived from " + how,
             "patch": {"ops": [{"op": "replace_anchor", "anchor": "cards", "value": specs}],
                       "why": f"tier 1: the cards re-derived from {how} ({len(wanted)})"},
             "note": {"wanted": len(wanted), "kept": report.get("structure", {}).get("kept")}}]


def foreign_before(read: Dict[str, Any], specs: Sequence[Dict[str, Any]], anchors: Sequence[str], regex: str = "",
                   bem: bool = False) -> int:
    """The cards a structure reads as RESULTS (a link of the region's URL pattern outside the header, navigation and
    footer) in containers (a card's parent) that come BEFORE the first container holding an anchored result and hold
    none: a block of the results' own markup placed before them (0: none). ``bem``: the specs are a version-11 region's
    (described without BEM modifiers)."""
    from anyuse.execution import demo_cards as K
    with K.bem_free(bem):
        return _foreign_before(read, specs, anchors, regex)


def _foreign_before(read: Dict[str, Any], specs: Sequence[Dict[str, Any]], anchors: Sequence[str], regex: str = "") -> int:
    import re
    from anyuse.execution import demo_cards as K
    root, base = K.parse(str(read.get("dom") or ""))
    url = str(read.get("url") or "")
    want = {C._norm(str(u)) for u in anchors}
    try:
        pat = re.compile(regex, re.I) if regex else None
    except re.error:
        pat = None

    def reads(a: Any) -> bool:
        if "href" not in a.attrs or K.area_of(a) in ("header", "nav", "footer"):
            return False
        h = C.strip_fragment(K.href_of(a, url, base))
        return pat is None or bool(pat.match(h))
    groups: List[Tuple[Any, List[bool]]] = []
    taken: List[Any] = []
    for x in K.walk(root):
        if x.tag in ("html", "body") or any(K.contains(t, x) for t in taken[-3:]):
            continue
        c = K.chain_of(x)
        if not any(c == list(sp.get("chain") or []) and (sp.get("kind") != "title" or x.tag == "a") for sp in specs):
            continue
        taken.append(x)
        links = [a for a in ([x] if x.tag == "a" else [a for a in K.walk(x) if a.tag == "a"]) if reads(a)]
        if not links:
            continue
        hit = any(C._norm(K.href_of(a, url, base)) in want for a in links)
        par = x.parent
        if groups and groups[-1][0] is par:
            groups[-1][1].append(hit)
        else:
            groups.append((par, [hit]))
    first = next((k for k, (_p, hs) in enumerate(groups) if any(hs)), None)
    return 0 if first is None else sum(len(hs) for _p, hs in groups[:first])


def _forms(read: Optional[Dict[str, Any]], host: str) -> List[Tuple[str, str]]:
    """The site's GET search forms on a page (its DOM): ``(path, the input's parameter)`` on ``host``."""
    from anyuse.execution import demo_cards as K
    from anyuse.execution import dom_page as D
    if not read or not read.get("dom"):
        return []
    root, base = K.parse(str(read.get("dom") or ""))
    url = str(read.get("url") or "")
    out: List[Tuple[str, str]] = []
    ins = [x for x in K.walk(root) if x.tag == "input"]
    for i in D.search_inputs_of(root):
        el = ins[i["index"]]
        f = D.form_of(el)
        nm = el.attrs.get("name") or ""
        if f is None or not nm:
            continue
        u = D.form_url(f, url, base, {nm: "QQQ"})
        if not u:
            continue
        sp = urllib.parse.urlsplit(u)
        if (sp.hostname or "").lower() != host or W.forbidden_control("", u):
            continue
        if (sp.path or "/", nm) not in out:
            out.append((sp.path or "/", nm))
    return out


def _search(tool: Dict[str, Any], value: str, read: Dict[str, Any], start: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    s = tool.get("search") or {}
    tmpl = str(s.get("template") or "") if s.get("mode") == "url" else \
        next((t for t in s.get("fallback_templates") or [] if "{q}" in t), "")
    out: List[Dict[str, Any]] = []
    if s.get("mode") == "typed" and start:
        # the typed search's own input found again (a search-like input on the start page) when the tool's replay no
        # longer finds the recorded one by its name or id (``cdp_guard.replay_inputs``: by the rules of the tool's
        # version)
        from anyuse.execution import demo_cards as K
        from anyuse.execution import dom_page as D
        from anyuse.execution.cdp_guard import replay_inputs, same_input
        root, _b = K.parse(str(start.get("dom") or ""))
        rows, v15 = D.text_inputs_of(root), C.v15(tool)
        vis = [i for i in rows if i.get("visible") and (i["rank"] < 3 if v15 else i["named14"])]
        want = s.get("input") or {}
        if vis and not any(same_input(i, want) for i in replay_inputs(rows, want, not v15)):
            v = {k: vis[0][k] for k in ("name", "id") if vis[0].get(k) and _key_ok(vis[0][k])}
            if v:
                out.append({"rule": "search: the typed search's input found again", "note": {},
                            "patch": {"ops": [{"op": "replace_anchor", "anchor": "search_input", "value": v}],
                                      "why": "tier 1: the search box found again by its role"}})
    if not tmpl:
        return out
    sp = urllib.parse.urlsplit(tmpl)
    host = (sp.hostname or "").lower()
    old_key = next((k for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True) if "{q}" in v), "")
    for path, key in _forms(read, host) + _forms(start, host):
        if not _key_ok(key):
            continue
        same_path = (path.rstrip("/") or "/") == ((sp.path or "/").rstrip("/") or "/")
        if same_path and key == old_key:
            continue
        if same_path and old_key:
            op = {"op": "rename_url_param", "scope": "search", "from": old_key, "to": key}
        else:
            op = {"op": "rebind_param", "param": "q", "to": {"in": "query", "key": key}}
            if not same_path:
                op["to"]["path"] = path
        out.append({"rule": "search: the site's own GET search form", "note": {"path": path, "key": key},
                    "patch": {"ops": [op], "why": f"tier 1: the site's search form sends the input as {key!r} to {path}"}})
    return out


def _fallback_search(tool: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The fallback search (www.anisearch.de: the compiled search URL carries a stale token the site ignores - 3 values
    and 6 replays failed - and the site's own search form is a POST, so tier 1 found no search
    candidate): the search URLs the compile saw on the site's start page (``search.fallback_templates``, the oracle's
    path), on the tool's own scheme and host, as ``rebind_param`` patches (the input moved to the fallback's path and
    parameter; the gate decides)."""
    s = tool.get("search") or {}
    tmpl = str(s.get("template") or "") if s.get("mode") == "url" else ""
    if not tmpl:
        return []
    sp = urllib.parse.urlsplit(tmpl)
    old_key = next((k for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True) if "{q}" in v), "")
    out: List[Dict[str, Any]] = []
    for fb in s.get("fallback_templates") or []:
        fp = urllib.parse.urlsplit(str(fb))
        key = next((k for k, v in urllib.parse.parse_qsl(fp.query, keep_blank_values=True) if "{q}" in v), "")
        if "{q}" not in str(fb) or not key or not _key_ok(key) or (fp.scheme, (fp.hostname or "").lower()) != \
                (sp.scheme, (sp.hostname or "").lower()) or W.forbidden_control("", str(fb).replace("{q}", "x")):
            continue
        path = fp.path or "/"
        if (path.rstrip("/") or "/") == ((sp.path or "/").rstrip("/") or "/") and key == old_key:
            continue
        out.append({"rule": "search: the tool's recorded fallback search URL", "note": {"path": path, "key": key},
                    "patch": {"ops": [{"op": "rebind_param", "param": "q", "to": {"in": "query", "key": key, "path": path}}],
                              "why": f"tier 1: the site's search seen on its start page sends the input as {key!r} to "
                                     f"{path}"}})
    return out


def _key_ok(k: str) -> bool:
    from anyuse.execution import repair_patch as P
    return bool(P._KEY.match(str(k or "")))


def _params_on_page(read: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(name, value, label) of the URL parameters the page's links and drop-downs carry (a drop-down's options: its
    own ``name`` with each option's value, or the option's URL)."""
    from anyuse.execution import demo_cards as K
    out: List[Tuple[str, str, str]] = []
    for e in _links(read):
        for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(str(e["href"])).query, keep_blank_values=True):
            out.append((k, v, str(e.get("text") or "")))
    root, base = K.parse(str(read.get("dom") or ""))
    for sel in (x for x in K.walk(root) if x.tag == "select"):
        nm = sel.attrs.get("name") or ""
        for o in (x for x in K.walk(sel) if x.tag == "option"):
            val = o.attrs.get("value", K.text_of(o, 60))
            lab = K.text_of(o, 60)
            if val.startswith(("/", "http")) or "?" in val:
                u = urllib.parse.urljoin(str(read.get("url") or ""), val)
                for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query, keep_blank_values=True):
                    out.append((k, v, lab))
            elif nm:
                out.append((nm, val, lab))
    return out


def _refine(tool: Dict[str, Any], value: str, read: Dict[str, Any]) -> List[Dict[str, Any]]:
    from anyuse.execution import demo_breadth as DB
    ref = tool.get("refine") or {}
    applied = tool.get("applied") or []
    out: List[Dict[str, Any]] = []
    here = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(str(read.get("url") or "")).query, keep_blank_values=True))
    if ref.get("mode") == "url":
        pairs = _params_on_page(read)
        for k0, v0 in (ref.get("params") or {}).items():
            a = next((x for x in applied if x.get("param") == k0), {})
            votes: Dict[str, int] = {}
            for k, v, lab in pairs:
                if k == k0 or k in here or v != str(v0) or not _key_ok(k):
                    continue
                c = DB.classify_param(k, v, lab)
                if c["kind"] != a.get("kind", c["kind"]):
                    continue
                if c["kind"] == "sort" and a.get("key") and c.get("key") and c["key"] != a["key"]:
                    continue
                votes[k] = votes.get(k, 0) + 1
            if votes:
                best = sorted(votes.items(), key=lambda x: -x[1])
                if len(best) == 1 or best[0][1] > best[1][1]:
                    out.append({"rule": "refine: the recorded value under another parameter name", "note": {"votes": votes},
                                "patch": {"ops": [{"op": "rename_url_param", "scope": "refine", "from": k0, "to": best[0][0]}],
                                          "why": f"tier 1: the page's controls set {best[0][0]}={v0} for the same "
                                                 f"{a.get('kind', 'refinement')}"}})
    elif ref.get("mode") == "controls":
        for i, ctl in enumerate(ref.get("controls") or []):
            a = applied[i] if i < len(applied) else {}
            key = a.get("key") or DB.sort_key(ctl.get("option_text") or ctl.get("text") or "")
            if not key:
                continue
            for e in read.get("elements") or []:
                if ctl.get("tag") == "select" and e.get("tag") == "select":
                    opt = next((o for o in e.get("options") or [] if DB.sort_key(o.get("text")) == key and
                                DB._f(o.get("text")) != DB._f(ctl.get("option_text"))), None)
                    if opt is not None and opt.get("text"):
                        out.append({"rule": "refine: the drop-down option naming the same sort order", "note": {},
                                    "patch": {"ops": [{"op": "replace_anchor", "anchor": "refine_control", "index": i,
                                                       "value": {"option_text": str(opt["text"])[:120]}}],
                                              "why": f"tier 1: the option {opt['text']!r} names the sort {key}"}})
                        break
                elif ctl.get("tag") == e.get("tag") and ctl.get("tag") != "select" and DB.sort_key(e.get("text")) == key \
                        and DB._f(e.get("text")) != DB._f(ctl.get("text")) and not W.forbidden_control(e.get("text"), e.get("href")):
                    out.append({"rule": "refine: the control naming the same sort order", "note": {},
                                "patch": {"ops": [{"op": "replace_anchor", "anchor": "refine_control", "index": i,
                                                   "value": {"text": str(e.get("text"))[:120]}}],
                                          "why": f"tier 1: the control {e.get('text')!r} names the sort {key}"}})
                    break
    return out


def _pager(tool: Dict[str, Any], value: str, read: Dict[str, Any]) -> List[Dict[str, Any]]:
    from anyuse.execution import demo_breadth as DB
    pager = tool.get("pager") or {}
    url1 = str(read.get("url") or "")
    # page 1's next-page link by the page's structure first, for a tool of any version: the mechanism read from it
    # (a page parameter or path) replays by its URLs, and a link mode follows the next-page link the tool's run finds
    el = DB.next_link(read, str(tool.get("site") or ""), 1, [url1])
    if el is None:
        return []
    n1 = len(C.region_items(read, tool.get("region") or {})) if read.get("cards") is not None else 0
    mech = DB._page_mech({"value": value, "steps": []}, url1, str(el["href"]), n1)
    if not mech:
        mech = {"mode": "link", "link": {"text": str(el.get("text") or "")[:40],
                                         "rel": "next" if "next" in str(el.get("rel") or "").split() else ""}}
    if mech.get("mode") == "param" and pager.get("mode") == "param" and mech.get("key") != pager.get("key") and \
            int(mech.get("first", 1)) == int(pager.get("first", 1)) and int(mech.get("stride", 1)) == int(pager.get("stride", 1)):
        op: Dict[str, Any] = {"op": "rename_url_param", "scope": "pager", "from": pager["key"], "to": mech["key"]}
    else:
        if {k: v for k, v in mech.items() if k != "link"} == {k: v for k, v in pager.items() if k != "link"}:
            return []
        op = {"op": "replace_anchor", "anchor": "pager", "value": mech}
    return [{"rule": "pager: page 1's next-page link", "note": {"next": str(el["href"])[:200]},
             "patch": {"ops": [op], "why": "tier 1: the page mechanism read again from page 1's next-page link"}}]
