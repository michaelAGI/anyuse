"""A compiled demonstration tool, run in a guarded browser page with NO model.
The tool spec comes from
``execution/demo_compile.compile_trace``; every invocation runs the tool's checks and
returns an accepted output (``ok``: the fields or the result list; ``no_results``) or
an explicit failure (``failed`` with the reason). A ``blocked`` status means the site
refused us (the bench stops the host).

Reliable rendering:

* the caller opens every invocation in a NEW browser context (``fresh_context``);
* the result wait: after the search, local reads only (nothing is
  sent by us) until the number of result containers is STABLE (two consecutive reads
  ``POLL_S`` apart agree) and a region item names a word of the input, or the page states
  no results in two consecutive reads; at most ``RESULT_WAIT_S`` (then the page is read
  as it is);
* one bounded retry of a navigation that got no answer (a ``Page.navigate`` that
  timed out while the browser answered): recorded as infrastructure in
  the invocation's ``infra_retries``, never a new attempt;
* the search URL fallback: a typed search whose Enter
  reloads the start page, or finds no search box, goes to the site's own search URL
  seen on the start page at demonstration time.

The page is duck-typed (``goto``, ``js``, ``dom``, ``type_search``, ``network``,
``settle``); ``test_demo_render.py`` drives it with a fake page.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W

#: the bounded wait for a script-rendered result list (seconds, local reads)
RESULT_WAIT_S = 8.0
POLL_S = 0.5
#: (a result list fetched by the page's own scripts, each paced
#: at 1 s): past ``RESULT_WAIT_S`` the result wait goes on while the page still has a script, XHR or fetch in flight,
#: at most this much longer (recorded in the wait trail); waiting sends nothing
RESULT_INFLIGHT_GRACE_S = 8.0
#: the replay's final page keeps its DOM (up to this many characters) beside its text: a refused answer can be judged
#: offline later
FINAL_DOM_CHARS = 600_000

#: the page's own product metadata beyond READ_JS: Open Graph / product meta tags and
#: the first schema.org Product microdata item (a local read)
PAGE_EXTRA_JS = r"""
(() => {
  const meta = {};
  document.querySelectorAll('meta[property], meta[name]').forEach(m => {
    const k = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
    if (/^(og:title|og:type|og:url|og:price:amount|og:price:currency|product:price:amount|product:price:currency|product:availability|og:availability)$/.test(k) && !(k in meta))
      meta[k] = (m.getAttribute('content') || '').slice(0, 300);
  });
  const md = {};
  const scope = document.querySelector('[itemtype*="schema.org/Product"]');
  const pick = (root, prop) => { const n = root.querySelector('[itemprop="' + prop + '"]'); if (!n) return '';
    return String(n.getAttribute('content') || n.getAttribute('href') || n.innerText || '').trim().slice(0, 300); };
  if (scope) { md.name = pick(scope, 'name'); md.price = pick(scope, 'price');
    md.currency = pick(scope, 'priceCurrency'); md.availability = pick(scope, 'availability'); }
  return {meta: meta, microdata: md};
})()
"""


#: tools of version >= 6: ``PAGE_EXTRA_JS`` with the Product item's OWN properties.
#: A property belongs to the nearest ancestor item (``itemscope``) - a page whose product was
#: named after its nested brand item (``itemprop=brand itemscope`` > ``itemprop=name``).
#: The name is the Product's own ``itemprop=name``, never a nested item's (brand, manufacturer, offer);
#: price, currency and availability are the Product's own or its own offers' (the first that states
#: one). ``demo_cards.microdata_of`` is the same reading on a recorded DOM.
PAGE_EXTRA_JS_V6 = r"""
/*anyuse:extra6*/ (() => {
  const meta = {};
  document.querySelectorAll('meta[property], meta[name]').forEach(m => {
    const k = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
    if (/^(og:title|og:type|og:url|og:price:amount|og:price:currency|product:price:amount|product:price:currency|product:availability|og:availability)$/.test(k) && !(k in meta))
      meta[k] = (m.getAttribute('content') || '').slice(0, 300);
  });
  const md = {};
  const scope = document.querySelector('[itemtype*="schema.org/Product"]');
  const owner = n => { const p = n.parentElement; return p ? p.closest('[itemscope]') : null; };
  const own = (item, prop) => { for (const n of item.querySelectorAll('[itemprop]')) {
      if ((n.getAttribute('itemprop') || '').split(/\s+/).indexOf(prop) >= 0 && owner(n) === item) return n; }
    return null; };
  const val = n => n ? String(n.getAttribute('content') || n.getAttribute('href') || n.innerText || '').trim().slice(0, 300) : '';
  if (scope) {
    const offers = Array.from(scope.querySelectorAll('[itemscope]')).filter(o =>
      (o.getAttribute('itemprop') || '').split(/\s+/).indexOf('offers') >= 0 && owner(o) === scope);
    const pick = prop => { const n = own(scope, prop); if (n) return val(n);
      for (const o of offers) { const m = own(o, prop); if (m) return val(m); } return ''; };
    md.name = val(own(scope, 'name')); md.price = pick('price');
    md.currency = pick('priceCurrency'); md.availability = pick('availability');
  }
  return {meta: meta, microdata: md};
})()
"""


#: version 8: ``PAGE_EXTRA_JS_V6`` plus the price texts the page STRIKES THROUGH (a
#: text node with a currency-marked number whose element, or one of its 3 ancestors, is ``del`` / ``s`` / ``strike``
#: or computes ``text-decoration-line: line-through``; <= 30, <= 40 characters) - a regular price shown next to
#: the current one. ``demo_cards.struck_of`` is the same reading on a recorded DOM (inline styles only).
PAGE_EXTRA_JS_V8 = r"""
/*anyuse:extra8*/ (() => {
  const meta = {};
  document.querySelectorAll('meta[property], meta[name]').forEach(m => {
    const k = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
    if (/^(og:title|og:type|og:url|og:price:amount|og:price:currency|product:price:amount|product:price:currency|product:availability|og:availability)$/.test(k) && !(k in meta))
      meta[k] = (m.getAttribute('content') || '').slice(0, 300);
  });
  const md = {};
  const scope = document.querySelector('[itemtype*="schema.org/Product"]');
  const owner = n => { const p = n.parentElement; return p ? p.closest('[itemscope]') : null; };
  const own = (item, prop) => { for (const n of item.querySelectorAll('[itemprop]')) {
      if ((n.getAttribute('itemprop') || '').split(/\s+/).indexOf(prop) >= 0 && owner(n) === item) return n; }
    return null; };
  const val = n => n ? String(n.getAttribute('content') || n.getAttribute('href') || n.innerText || '').trim().slice(0, 300) : '';
  if (scope) {
    const offers = Array.from(scope.querySelectorAll('[itemscope]')).filter(o =>
      (o.getAttribute('itemprop') || '').split(/\s+/).indexOf('offers') >= 0 && owner(o) === scope);
    const pick = prop => { const n = own(scope, prop); if (n) return val(n);
      for (const o of offers) { const m = own(o, prop); if (m) return val(m); } return ''; };
    md.name = val(own(scope, 'name')); md.price = pick('price');
    md.currency = pick('priceCurrency'); md.availability = pick('availability');
  }
  const rx = /\d[\d .,\u00a0]*\s?(€|eur|kr|sek|nok|dkk|zł|zl|pln|chf|£|\$|:-|,-)|(€|£|\$|chf)\s?\d/i;
  const struck = [];
  const isStruck = e => { for (let k = 0; k < 4 && e && e !== document.body; k++, e = e.parentElement) {
      if (/^(DEL|S|STRIKE)$/.test(e.tagName)) return true;
      const cs = getComputedStyle(e); if (String(cs.textDecorationLine || cs.textDecoration || '').indexOf('line-through') >= 0) return true; }
    return false; };
  const w = document.body ? document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT) : null; let t, n = 0;
  while (w && (t = w.nextNode()) && n < 4000 && struck.length < 30) { n++;
    const s = (t.nodeValue || '').replace(/\s+/g, ' ').trim();
    if (!s || s.length > 40 || !rx.test(s) || !t.parentElement) continue;
    if (isStruck(t.parentElement) && struck.indexOf(s) < 0) struck.push(s); }
  return {meta: meta, microdata: md, struck: struck};
})()
"""


#: version 9 ("Produkt dostępny" and the next inline-block element's "Wysyłka w
#: ciągu: 2-3 dni" read as "Produkt dostępnyWysyłka ..." by ``innerText``): the body's and the main region's visible
#: text with a SEPARATOR at every element boundary - a line break around every element whose computed display is not
#: ``inline`` / ``contents`` (block, inline-block, flex, grid, list item, table row, ...) and for ``<br>``, a tab around
#: a table cell; ``display: none`` and an element with no box (``checkVisibility``: inside a closed ``<details>``, ...)
#: skipped, ``visibility: hidden`` text left out; <= 20,000 characters each (a local read). ``demo_cards.sep_text_of`` is the same reading on a recorded DOM (the tags' default display and inline
#: styles only).
TEXT_JS_V9 = r"""
/*anyuse:text9*/ (() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'TITLE', 'SVG', 'MATH', 'IFRAME', 'OBJECT',
                        'EMBED', 'CANVAS', 'VIDEO', 'AUDIO', 'IMG', 'PICTURE', 'INPUT', 'TEXTAREA']);
  const read = (root, limit) => {
    const out = []; let size = 0; let budget = 30000;
    const visit = el => {
      if (size >= limit || budget-- <= 0 || SKIP.has(String(el.tagName || '').toUpperCase())) return;
      if (el.tagName === 'BR') { out.push('\n'); return; }
      const cs = getComputedStyle(el); const d = cs.display;
      if (d === 'none') return;
      if (d !== 'contents' && typeof el.checkVisibility === 'function' && !el.checkVisibility()) return;
      const hide = cs.visibility === 'hidden' || cs.visibility === 'collapse';
      const sep = d === 'table-cell' ? '\t' : (d === 'inline' || d === 'contents') ? '' : '\n';
      out.push(sep);
      for (const c of el.childNodes) {
        if (c.nodeType === 3) { if (!hide) { const t = (c.nodeValue || '').replace(/[ \t\n\r\f]+/g, ' ');
            out.push(t); size += t.length; } }
        else if (c.nodeType === 1) visit(c);
        if (size >= limit) break;
      }
      out.push(sep);
    };
    if (root) visit(root);
    return out.join('').replace(/ *\t */g, '\t').replace(/[ \t]*\n[ \t\n]*/g, '\n').replace(/\t{2,}/g, '\t')
        .trim().slice(0, limit);
  };
  const mainEl = document.querySelector('main, [role=main], #main, #content, .main-content') || document.body;
  return {text: read(document.body, 20000), main_text: read(mainEl, 20000)};
})()
"""


#: the result CONTAINER of every link (a local read): the nearest ancestor that repeats among
#: its siblings (a list item / tile) and three levels above it, as tag#id.class descriptors
#: (ids and classes with a digit dropped: hashed names change between deploys). A sidebar of
#: popular items shares the results' URL pattern but not their container
#: (a "Przepis dnia" teaser and the results are both /przepis/...).
LINK_SIG_JS = r"""
(() => {
  const desc = n => { if (!n || !n.tagName) return '';
    const id = n.id && !/\d/.test(n.id) ? '#' + n.id : '';
    const cls = (typeof n.className === 'string' ? n.className : '').split(/\s+/)
        .filter(c => c && !/\d/.test(c)).slice(0, 2).join('.');
    return n.tagName.toLowerCase() + id + (cls ? '.' + cls : ''); };
  const itemOf = a => { let n = a;
    for (let k = 0; k < 8 && n && n.parentElement; k++) { const p = n.parentElement; const d = desc(n);
      let same = 0; for (const c of p.children) { if (c.tagName === n.tagName && desc(c) === d) same++; }
      if (same >= 2) return n; n = p; }
    return null; };
  const out = {};
  const links = document.querySelectorAll('a[href]');
  for (let i = 0; i < links.length && i < 3000; i++) { const a = links[i]; const h = String(a.href || '').split('#')[0];
    const it = itemOf(a); let sig = '';
    if (it) { const p = it.parentElement; const g = p && p.parentElement; const gg = g && g.parentElement;
      sig = [desc(gg), desc(g), desc(p), desc(it)].join('>'); }
    const l = out[h] || (out[h] = []); if (l.indexOf(sig) < 0 && l.length < 4) l.push(sig); }
  return out;
})()
"""


#: links whose tile (an ancestor of <= 900 characters, <= 7 levels up) shows a price (a local read)
PRICED_LINKS_JS = r"""
(() => { const rx = /(\d[\d.,\s\u00a0]*\s?(€|eur|kr|sek|nok|dkk|zł|zl|pln|chf|£|\$|:-|,-))|((€|£|\$|chf)\s?\d)/i;
  const out = {}; const links = document.querySelectorAll('a[href]');
  for (let i = 0; i < links.length && i < 3000; i++) { const a = links[i]; let n = a; let hit = false;
    for (let k = 0; k < 7 && n; k++) { const t = n.innerText || ''; if (t.length > 900) break;
      if (rx.test(t)) { hit = true; break; } n = n.parentElement; }
    if (hit) out[String(a.href || '').split('#')[0]] = true; }
  return out; })()
"""


#: ONE bounded retry of a navigation that got no answer (infrastructure)
NAV_RETRIES = 1

#: the value of a field the page's structured data and visible text disagree on
CONFLICT = "CONFLICT"


def apply_site_hosts(budget, tool: Dict[str, Any]) -> Dict[str, List[str]]:
    """Web-watch: before a run of ``tool`` sends anything, its DECLARED hosts (the demonstration's,
    the site's own: ``demo_compile.site_hosts_policy``) become the run's origin policy - one site for the
    guard (one pacer slot, one request and byte count, the same caps) and for the site gate, which holds
    the site and so lets the run navigate exactly those hosts (a search on the site's shop subdomain was
    refused). Nothing else changes: a third-party host is still refused, and the
    guard's audit judges every request by the same policy. Returns the policy (``{}``: none declared)."""
    from anyuse.execution.cdp_guard import OriginPolicy
    pol = C.site_hosts_policy(tool)
    if pol:
        budget.origin_policy = OriginPolicy(pol)
    return pol


def conflict_why(conf: Dict[str, Any]) -> str:
    """One line per conflicting field: both sides, with their sources."""
    parts = []
    for f, ev in sorted(conf.items()):
        sides = ", ".join(f"{s.get('source')} {s.get('value')!r}" for s in ev.get("structured") or [])
        vis = ev.get("visible") or ""
        st = ev.get("visible_statement") or ""
        parts.append(f"{f}: {sides}" + (f" vs visible {vis}" + (f" ({st[:60]!r})" if st else "") if vis else ""))
    return "conflict: " + "; ".join(parts)


def nav_stalled(exc: BaseException) -> bool:
    """A ``Page.navigate`` that got no answer while the browser answers (``agent_cdp._stalled``)."""
    from anyuse.execution.browser_supervisor import STALLED, InfrastructureFailure
    return isinstance(exc, InfrastructureFailure) and exc.kind == STALLED and "Page.navigate" in exc.detail


class RetryNav:
    """The page with ONE bounded retry of a stalled navigation (``nav_stalled``) per invocation,
    recorded in ``note["infra_retries"]`` (infrastructure, never a new attempt); a second stall
    raises as before. Everything else is the page's own."""

    def __init__(self, page, note: Dict[str, Any]):
        self._page, self._note = page, note

    def __getattr__(self, k):
        return getattr(self._page, k)

    async def goto(self, url: str) -> None:
        try:
            await self._page.goto(url)
        except Exception as exc:                       # noqa: BLE001 - re-raised unless a first stall
            if not nav_stalled(exc) or len(self._note.get("infra_retries") or []) >= NAV_RETRIES:
                raise
            self._note.setdefault("infra_retries", []).append(
                {"what": "Page.navigate got no answer (the browser answers): one retry", "url": str(url)[:200],
                 "why": str(exc)[:200], "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            await self._page.goto(url)


async def read_cards(page, tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The card of every link (``demo_cards.CARDS_JS``, a local read) for a tool with result
    cards; None when the tool has none or the read failed."""
    from anyuse.execution import demo_cards as K
    region = tool.get("region") or {}
    specs = region.get("cards") or []
    if not specs:
        return None
    try:
        js = K.cards_js_v12 if C.cards_v12(region) else K.cards_js_v11 if C.cards_v11(region) else \
            K.cards_js_v10 if C.cards_v10(region) else \
            K.cards_js_v8 if C.cards_v8(region) else K.cards_js_v6 if C.cards_v6(region) else K.cards_js
        got = await page.js(js(specs))
    except Exception as exc:                           # noqa: BLE001 - a read never fails a run
        from anyuse.execution.browser_supervisor import classify
        if classify(exc):
            raise
        return None
    if C.cards_v6(region) and not (isinstance(got, dict) and isinstance(got.get("cards"), list)):
        return None
    return got if isinstance(got, dict) and isinstance(got.get("map"), dict) else None


async def read_full(page, with_dom: bool = False, tool: Optional[Dict[str, Any]] = None,
                    own_microdata: bool = False, struck: bool = False, sep: bool = False,
                    cap11: bool = False) -> Dict[str, Any]:
    """``web_arch_run.read_page`` plus the product meta tags and microdata (and the
    DOM when asked; the result cards for a ``tool`` with cards): all local CDP reads,
    nothing is sent. ``own_microdata``: the Product item's own properties
    (``PAGE_EXTRA_JS_V6``; tools of version >= 6). ``struck`` (tools of version >= 8): also the price texts the
    page strikes through (``PAGE_EXTRA_JS_V8``; ``struck_prices``). ``sep`` (product pages of tools of version >= 9):
    also the body's and the main region's text with element-boundary separators (``TEXT_JS_V9``; ``text_sep``,
    ``main_text_sep``). ``cap11`` (tools of version >= 11): the controls past the reader's cap
    (``web_arch_run.read_page11``)."""
    from anyuse.execution import web_arch_run as RR
    r = await (RR.read_page11(page) if cap11 else RR.read_page(page))
    try:
        extra = await page.js(PAGE_EXTRA_JS_V8 if struck else PAGE_EXTRA_JS_V6 if own_microdata else PAGE_EXTRA_JS)
    except Exception:                                  # noqa: BLE001 - a read never fails a run
        extra = None
    extra = extra if isinstance(extra, dict) else {}
    r["meta"] = extra.get("meta") or {}
    r["microdata"] = extra.get("microdata") or {}
    if struck:
        r["struck_prices"] = [str(x)[:40] for x in extra.get("struck") or []][:30]
    if sep:
        try:
            st = await page.js(TEXT_JS_V9)
        except Exception:                              # noqa: BLE001 - a read never fails a run
            st = None
        st = st if isinstance(st, dict) else {}
        r["text_sep"] = str(st.get("text") or "") if isinstance(st.get("text"), str) else ""
        r["main_text_sep"] = str(st.get("main_text") or "") if isinstance(st.get("main_text"), str) else ""
    try:
        sigs = await page.js(LINK_SIG_JS)
    except Exception:                                  # noqa: BLE001
        sigs = None
    r["link_sigs"] = sigs if isinstance(sigs, dict) else {}
    try:
        priced = await page.js(PRICED_LINKS_JS)
    except Exception:                                  # noqa: BLE001
        priced = None
    r["priced_links"] = sorted(priced) if isinstance(priced, dict) else []
    if with_dom:
        try:
            r["dom"] = await page.dom()
        except Exception:                              # noqa: BLE001
            r["dom"] = ""
        # the browser's accessibility tree with the page read the agent and the compile keep
        # (computed roles, names and landmarks: ``ax_tree.enrich``); a page without one reads as before
        from anyuse.execution import ax_tree as AXT
        raw = await AXT.read_raw(page)
        if raw:
            r["ax"] = raw
            AXT.enrich(r)
    if tool is not None and (tool.get("region") or {}).get("cards"):
        r["cards"] = await read_cards(page, tool)
    return r


def page_record(r: Dict[str, Any], limit: int = 20000) -> Dict[str, Any]:
    """What a run keeps of a page (the audit's evidence; no DOM). A version-8 read also keeps the main text and
    the struck prices (a text price is read in the main text)."""
    rec = {"url": r.get("url"), "title": r.get("title"), "h1": r.get("h1"),
           "text": str(r.get("text") or "")[:limit], "facts": (r.get("facts") or [])[:4],
           "canonical": r.get("canonical"), "meta": r.get("meta"), "microdata": r.get("microdata")}
    if "struck_prices" in r:
        rec["main_text"] = str(r.get("main_text") or "")[:limit]
        rec["struck_prices"] = r.get("struck_prices")
    if "main_text_sep" in r:
        # version 9: the main region's text with element-boundary separators (``TEXT_JS_V9``)
        rec["main_text_sep"] = str(r.get("main_text_sep") or "")[:limit]
    return rec


async def consent(page, label: str) -> bool:
    """The reject-only consent policy of every arm (``agent_browser``); True when the
    page stays a wall."""
    try:
        from anyuse.execution.agent_browser import AgentRun, _resolve_consent_wall
        return bool(await _resolve_consent_wall(page, AgentRun(), label))
    except Exception:                                  # noqa: BLE001 - no wall handling on a fake page
        return False


async def wait_results(page, tool: Dict[str, Any], value: str, ceiling_s: float,
                       clock: Callable[[], float] = time.monotonic,
                       trail: Optional[List[Any]] = None) -> Optional[float]:
    """The result wait (local reads only): until two consecutive reads ``POLL_S`` apart show
    the SAME number of result containers (the tool's own reading: one per card) and a region
    item names a word of ``value``, or both reads show the page's own "no results" statement;
    the seconds waited, or None at the ceiling. ``trail`` collects each read's (s, items,
    no-results) for the record."""
    from anyuse.execution import web_arch_run as RR
    t0 = clock()
    region = tool.get("region") or {}
    last = None
    while True:
        try:
            # version 11: past the reader's cap of controls (``read_page11``)
            r = await (RR.read_page11(page) if C.v11(tool) else RR.read_page(page))
            if region.get("cards"):
                r["cards"] = await read_cards(page, tool)
        except Exception as exc:                       # noqa: BLE001
            from anyuse.execution.browser_supervisor import classify
            if classify(exc):
                raise
            return None
        items = C.region_items(r, region) if not (region.get("cards") and r.get("cards") is None) else []
        empty = bool(C.no_results_statement(r.get("main_text") or r.get("text")))
        if not empty and tool.get("empty_page"):
            # the site's own empty page, learned when the tool was proved
            from anyuse.execution import empty_page as EP
            empty = EP.match_text(tool["empty_page"], r, value).state == EP.EMPTY
        named = any(C.name_share(it["title"] + " " + it.get("ctx", ""), value) > 0 for it in items)
        # version 7: a search that went straight to one product's own page (no item, no "no results") is settled too
        landed = bool(C.v7(tool) and not items and not empty and tool.get("workflow") == "product_lookup"
                      and W.product_detail(r, value)[0] is True)
        state = (len(items), empty, landed) if C.v7(tool) else (len(items), empty)
        if trail is not None and len(trail) < 40:
            trail.append([round(clock() - t0, 2), len(items), int(empty)] + ([int(landed)] if C.v7(tool) else []))
        if last is not None and state == last and ((state[0] > 0 and named) or empty or landed):
            return round(clock() - t0, 2)
        last = state
        if clock() - t0 >= ceiling_s:
            # a list the page's own scripts still fetch is not given up while they are in flight (bounded)
            from anyuse.execution.typed_readiness import inflight_requests
            fl = inflight_requests(getattr(page, "_events", None) or [], types=("XHR", "Fetch", "Script"))
            if not ceiling_s or not fl["inflight"] or clock() - t0 >= ceiling_s + RESULT_INFLIGHT_GRACE_S:
                return None
            if trail is not None and len(trail) < 40:
                trail.append([round(clock() - t0, 2), "inflight", int(fl["inflight"])])
        await page.settle(POLL_S)


async def _blocked(page, read: Dict[str, Any], start: bool = False) -> str:
    from anyuse.execution.web_arch_run import site_block
    try:
        net = await page.network(want_bodies=False)
    except Exception:                                  # noqa: BLE001
        net = []
    return site_block(str(read.get("text") or ""), str(read.get("url") or ""), net, start=start)


async def _search(page, tool: Dict[str, Any], value: str, settle: Callable, out: Dict[str, Any],
                  t0: float) -> Dict[str, Any]:
    """The search of every compiled tool (``run_tool``'s first part; version 10 shares it with the breadth tools):
    the result URL, else the typed search with the site's search URL as the fallback; the consent policy; a block.
    ``{"exclude": [...]}`` (the pre-search page's links, for an overlay search) or ``{"done": out}`` (the run ended)."""
    from anyuse.execution import web_arch_run as RR
    s = tool.get("search") or {}
    site = str(tool.get("site") or "")
    exclude: List[str] = []
    if s.get("mode") == "url":
        await page.goto(C.fill(s["template"], value))
        await settle(page)
        walled = await consent(page, f"tool:{site}")
        out["search_via"] = "url"
    else:
        await page.goto(str(s.get("start_url") or tool.get("start_url")))
        await settle(page)
        walled = await consent(page, f"tool:{site}")
        r0 = await RR.read_page(page)
        blk = await _blocked(page, r0, start=True)
        if blk:
            out.update(status="blocked", why=blk)
            return {"done": _done(out, t0)}
        # version 14: a dead Enter falls back to the search's own control and the input's GET form;
        # an older tool replays exactly as before
        want = dict(s.get("input") or {}) or None
        if want and not C.v14(tool):
            want.pop("submit_label", None)
        try:
            page.submit_chooser = False         # a replay asks no model: the recorded control decides
        except AttributeError:
            pass
        # a tool before version 15 also finds the inputs its own rule named (``cdp_guard.replay_inputs``)
        probe = await page.type_search(value, want=want, form_fallback=C.v14(tool), named14=not C.v15(tool))
        await settle(page)
        r1 = await RR.read_page(page)
        out["probe"] = {k: probe.get(k) for k in ("input", "document", "note")} if isinstance(probe, dict) else {}
        reloaded = W.norm_url(C.strip_fragment(r1.get("url") or "")) == W.norm_url(C.strip_fragment(r0.get("url") or "")) \
            and W.fold(r1.get("text"))[:2000] == W.fold(r0.get("text"))[:2000]
        no_box = isinstance(probe, dict) and probe.get("note") and not probe.get("input")
        if no_box or reloaded:
            # the Enter reloaded the start page, or no box: the
            # site's own search URL seen on the start page at demonstration time
            fbs = [t for t in s.get("fallback_templates") or [] if "{q}" in t]
            if not fbs:
                out.update(status="failed", why="search_page: the typed search " +
                           ("found no search box" if no_box else "reloaded the start page") +
                           " and the start page showed no search URL")
                out["pages"].append(page_record(r1, 4000))
                return {"done": _done(out, t0)}
            for t in fbs[:2]:
                # a URL that fails to load (a redirect robots.txt refuses) is not used: the next one
                await page.goto(C.fill(t, value))
                await settle(page)
                if not str(await page.url()).startswith("chrome-error"):
                    break
            out["search_via"] = "url_fallback"
        else:
            out["search_via"] = "typed"
            if s.get("overlay") and W.norm_url(C.strip_fragment(r1.get("url") or "")) == \
                    W.norm_url(C.strip_fragment(r0.get("url") or "")):
                exclude = [str(e.get("href")) for e in r0.get("elements") or [] if e.get("href")]
    if walled:
        out.update(status="failed", why="search_page: a consent wall that cannot be rejected")
        return {"done": _done(out, t0)}
    return {"exclude": exclude}


async def run_tool(page, tool: Dict[str, Any], value: str, settle: Callable,
                   result_wait_s: float = RESULT_WAIT_S, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One invocation of a compiled tool for the input ``value`` (no model). A version-10 BREADTH tool (a filtered /
    sorted search, a paged search, a visit of the top results) runs ``demo_breadth.run_breadth`` (``params``: its bound
    parameters to override). The settle reports of this invocation (``settles``, when ``settle`` keeps a
    trail: ``typed_readiness.PageSettle``) and the final page's DOM (its last page record's ``dom``) are kept."""
    trail = getattr(settle, "trail", None)
    n0 = len(trail) if isinstance(trail, list) else 0
    # a version-14 tool reads the words of every script; an older one the a-z and 0-9 runs it read before. A
    # version-15 tool reads the site's own evidence first everywhere in its run (its pager's links too), as its
    # compile did
    with W.scripts(C.v14(tool)), W.site_evidence(C.v15(tool)):
        out = await _run_tool(page, tool, value, settle, result_wait_s, params)
    if isinstance(out, dict):
        if isinstance(trail, list):
            out["settles"] = [dict(x) for x in trail[n0:]]
        await keep_final_dom(page, out)
    return out


async def keep_final_dom(page, out: Dict[str, Any]) -> None:
    """The replay's last page record gets the DOM of the page it ended on (a local read, nothing is sent)."""
    pages = out.get("pages") or []
    if not pages or not isinstance(pages[-1], dict) or pages[-1].get("dom") or not hasattr(page, "dom"):
        return
    try:
        dom = await page.dom()
    except Exception:                                  # noqa: BLE001 - a page that cannot be read keeps its text
        return
    if dom:
        pages[-1]["dom"] = str(dom)[:FINAL_DOM_CHARS]


async def _run_tool(page, tool: Dict[str, Any], value: str, settle: Callable,
                    result_wait_s: float = RESULT_WAIT_S, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if C.v10(tool) and tool.get("workflow") in ("filtered_search", "paged_search", "visit_results"):
        from anyuse.execution import demo_breadth as DB
        return await DB.run_breadth(page, tool, value, settle, result_wait_s, params)
    from anyuse.execution import web_arch_run as RR
    t0 = time.monotonic()
    out: Dict[str, Any] = {"value": value, "status": "", "output": None, "why": "", "checks": {},
                           "pages": [], "model_calls": 0, "search_via": "", "tool_sha256": C.tool_digest(tool)}
    site = str(tool.get("site") or "")
    page = RetryNav(page, out)
    try:
        got = await _search(page, tool, value, settle, out, t0)
        if "done" in got:
            return got["done"]
        exclude = got["exclude"]
        trail: List[Any] = []
        out["results_after_s"] = await wait_results(page, tool, value, result_wait_s, trail=trail)
        out["wait_trail"] = trail
        read = await read_full(page, tool=tool, own_microdata=C.v6(tool), struck=C.v8(tool),
                               sep=C.v9(tool) and tool.get("workflow") == "product_lookup", cap11=C.v11(tool),
                               with_dom=bool((tool.get("region") or {}).get("link_names")))
        if C.v11(tool) and not str(read.get("text") or "").strip():
            # version 11 (a result document whose parse never got
            # past a script in its head - no body, no text - and was judged "no item of the compiled region"): a result
            # page that shows no text is still loading - one more result wait; still none: an explicit failure
            out["render_wait_s"] = await wait_results(page, tool, value, result_wait_s, trail=trail)
            read = await read_full(page, tool=tool, own_microdata=C.v6(tool), struck=C.v8(tool),
                                   sep=tool.get("workflow") == "product_lookup", cap11=True)
            if C.v12(tool) and not str(read.get("text") or "").strip():
                # version 12 (a document whose parse waited
                # for a script the browser had not cached - a cold cache - and never got past its head): ONE reload of
                # the result page (one more GET of the same URL), then the result wait again
                again = str(await page.url() or "")
                if again.startswith("http"):
                    out["render_reload"] = again[:300]
                    await page.goto(again)
                    await settle(page)
                    out["render_wait_s"] = await wait_results(page, tool, value, result_wait_s, trail=trail)
                    read = await read_full(page, tool=tool, own_microdata=C.v6(tool), struck=C.v8(tool),
                                           sep=tool.get("workflow") == "product_lookup", cap11=True)
            if not str(read.get("text") or "").strip():
                out["pages"].append(page_record(read))
                out.update(status="failed", why=f"result_page: the page did not render (no visible text after "
                                                f"{2 * result_wait_s:.0f} s of result wait" +
                           (" and a reload)" if out.get("render_reload") else ")"))
                return _done(out, t0)
        out["pages"].append(page_record(read))
        blk = await _blocked(page, read)
        if blk:
            out.update(status="blocked", why=blk)
            return _done(out, t0)
        j = C.judge_result_page(tool, read, value, exclude, site_page=True)
        out["checks"]["result_page"] = {k: v for k, v in j.items() if k not in ("el",)}
        if j["status"] == "failed":
            out.update(status="failed", why=j["why"])
            return _done(out, t0)
        if j["status"] == "no_results":
            out.update(status="no_results", output={"no_results": True}, why=j["why"])
            return _done(out, t0)
        if j["status"] == "incomplete":
            # web-watch: results the compiled structure cannot read - reported, never silently dropped;
            # an incomplete output is not an accepted answer
            out.update(status="incomplete", why=j["why"], missing=j.get("missing"),
                       output={"results": j["results"], "incomplete": True} if "results" in j else None)
            if j.get("ambiguous"):
                # version 7: several results match the input equally closely - listed, none is chosen
                out["ambiguous"] = j["ambiguous"]
            if j.get("not_listed"):
                # version 9: no result carries the input's distinguishing words - the sibling is not answered
                out["not_listed"] = j["not_listed"]
            return _done(out, t0)
        if j["status"] == "list":
            out.update(status="ok", output={"results": j["results"]}, why=j["why"])
            return _done(await _attribute_verify(page, tool, value, out), t0)
        if j["status"] == "landed":
            # version 7: the search went straight to ONE product's own page - it is the
            # answer when it is the input's product (its own name names the input), else an explicit failure
            fields = C.extract_fields(read, tool.get("fields") or {}, value)
            ok, why, checks = C.check_landing(read, fields, value, site, C.identity_values(value), v9=C.v9(tool),
                                              v15=C.v15(tool))
            out["checks"]["product"] = checks
            _url_evidence(out, read, fields, tool)
            out["picked"] = {"title": str(fields.get("name") or "")[:200], "url": str(read.get("url") or ""),
                             "landed": True}
            if not ok:
                out.update(status="failed", why=why, fields_seen=fields)
                return _done(out, t0)
            if C.v9(tool):
                # version 9: the input's distinguishing words the product's own name lacks - its variant attributes
                ok, why9, ev9 = C.check_pick9(read, fields, value, {}, landed=True)
                if ev9:
                    out["checks"]["identity9"] = ev9
                if not ok:
                    out.update(status="failed", why=why9, fields_seen=fields)
                    return _done(out, t0)
            conf = C.field_conflicts(read, fields, tool.get("fields") or {}, value, v8=C.v8(tool))
            if conf:
                out.update(status="failed", why=conflict_why(conf), conflicts=conf,
                           fields_seen=dict(fields, **{f: CONFLICT for f in conf}))
                return _done(out, t0)
            fields = _visible_availability(out, read, fields, tool, value)
            out.update(status="ok", output=fields, why=f"{why} (the search landed on the product's own page)")
            return _done(await _attribute_verify(page, tool, value, out), t0)
        # product lookup: open the chosen item (membership: a link of the compiled region)
        el = dict(j.get("el") or {})
        el.setdefault("tag", "a")
        el["href"] = j["pick"]["url"]
        res = await RR.open_link(page, el, settle)
        if not res.get("ok"):
            out.update(status="failed", why=f"membership: the chosen item did not open ({res.get('why')})")
            return _done(out, t0)
        prod = await read_full(page, own_microdata=C.v6(tool), struck=C.v8(tool), sep=C.v9(tool))
        out["pages"].append(page_record(prod))
        blk = await _blocked(page, prod)
        if blk:
            out.update(status="blocked", why=blk)
            return _done(out, t0)
        fields = C.extract_fields(prod, tool.get("fields") or {}, value)
        # version 7: a value repeating a tile's title with its subtitle is also scored on the title
        ok, why, checks = C.check_product(prod, fields, value, site, C.identity_values(value) if C.v7(tool) else (),
                                          v9=C.v9(tool), v15=C.v15(tool))
        out["checks"]["product"] = checks
        _url_evidence(out, prod, fields, tool)
        out["picked"] = j["pick"]
        if j.get("variants_same_title"):
            # version 7: the answer is the first of several same-title results; the
            # others are listed as shown on the result page (title, shown price, URL) - none was opened
            out["variants_same_title"] = j["variants_same_title"]
        if not ok:
            out.update(status="failed", why=why, fields_seen=fields)
            return _done(out, t0)
        if C.v9(tool):
            # version 9: the distinguishing words the chosen result's title lacked - the product's variant attributes
            ok, why9, ev9 = C.check_pick9(prod, fields, value, j.get("pick") or {})
            if ev9:
                out["checks"]["identity9"] = ev9
            if not ok:
                out.update(status="failed", why=why9, fields_seen=fields)
                return _done(out, t0)
        if C.v6(tool):
            # web-watch: a field the page's structured data and visible text disagree on is
            # CONFLICT - the output is a failure for that field, never either side silently
            conf = C.field_conflicts(prod, fields, tool.get("fields") or {}, value, v8=C.v8(tool))
            if conf:
                out.update(status="failed", why=conflict_why(conf), conflicts=conf,
                           fields_seen=dict(fields, **{f: CONFLICT for f in conf}))
                return _done(out, t0)
        fields = _visible_availability(out, prod, fields, tool, value)
        if j.get("variants_same_title"):
            fields = dict(fields, variants_same_title=j["variants_same_title"])
        out.update(status="ok", output=fields, why=why)
        return _done(await _attribute_verify(page, tool, value, out), t0)
    except Exception as exc:                           # noqa: BLE001 - an invocation failure is a record
        from anyuse.execution.browser_supervisor import classify
        if classify(exc):
            raise
        out.update(status="error", why=f"{type(exc).__name__}: {exc}"[:200])
        return _done(out, t0)


async def _attribute_verify(page, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
    """Verifier independence (``execution/attribute_verify.py``; ON by default:
    ``ANYUSE_ATTRIBUTE_VERIFY``): an ``ok`` product or site-search answer is reported only when its critical attributes agree with an INDEPENDENT reading of
    the page the run ends on (its DOM and visible text: local reads, no request); an attribute that stays uncertain
    after the small model withholds the answer (``incomplete``, the answer kept as ``unverified_output``). Off: the
    run's record unchanged, byte for byte."""
    from anyuse.execution import attribute_verify as AV
    if not AV.enabled() or tool.get("workflow") not in ("product_lookup", "site_search"):
        return out
    return await AV.verify_run(page, tool, value, out)


def _visible_availability(out: Dict[str, Any], page: Dict[str, Any], fields: Dict[str, Any], tool: Dict[str, Any],
                          value: str) -> Dict[str, Any]:
    """Version 8 (``demo_compile.availability_answer``): a structured availability the visible page does not
    support is answered as the page's visible statement or "not stated" - the structured value kept as evidence
    (``checks.availability``), never as the answer. Other versions: the fields as they are."""
    if not C.v8(tool):
        return fields
    av, ev = C.availability_answer(page, fields, tool.get("fields") or {}, value)
    if not ev:
        return fields
    out["checks"]["availability"] = ev
    return dict(fields, availability=av)


def _url_evidence(out: Dict[str, Any], page: Dict[str, Any], fields: Dict[str, Any], tool: Dict[str, Any]) -> None:
    """Version 9 (``demo_compile.answer_url``): the page's canonical link, when it differs from the URL loaded,
    is kept as evidence (``checks.url``: both URLs, the one answered and why). Other versions: nothing."""
    srcs = tool.get("fields") or {}
    if srcs.get("url_rule") != "same_product":
        return
    _url, ev = C.answer_url(page, fields, srcs)
    if ev:
        out["checks"]["url"] = ev


def _done(out: Dict[str, Any], t0: float) -> Dict[str, Any]:
    out["seconds"] = round(time.monotonic() - t0, 2)
    out["accepted"] = out.get("status") in ("ok", "no_results")
    return out
