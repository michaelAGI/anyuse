"""The web architecture's engine on a guarded page.

``execution/web_arch.py`` is pure; this module drives ONE page of a guarded
browser session (``cdp_guard.GuardedSession`` / the bench's GET-only session:
every request the page makes passes the traffic guard) for one task:

* :func:`read_page` - the site readers on the rendered page: the visible
  controls (links, buttons, selects) with their text, link, area (header, nav,
  main, aside, footer), the headings, the text, the schema.org JSON-LD blocks;
* :class:`WebArch` - planner -> map -> picker -> action -> read / extract /
  answer, with the hard gates, one re-plan after a failed step, continuations
  after a read, the confirmer in shadow (text and text+screenshot) and the
  picker's paired screenshot question (shadow);
* :func:`replay` - a compiled program with NO model (the compiled repeat, and
  the proof's parameterised tool).

Every model is injected (``planner``, ``extractor``, ``chooser``,
``confirmer``); ``test_web_arch.py`` drives all of it with a fake page.
Screenshots are local CDP reads of the container page (no request); a
challenge page is never captured or sent (``openjev.challenge_guard``);
e-mail addresses and phone numbers on the page are covered before a capture.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import web_arch as W

#: the controls the engine may see and act on; the index of an element is its
#: position in THIS selector's document-order list (the click uses the same)
CONTROL_SEL = ("a[href], button, input[type=submit], input[type=button], input[type=checkbox], "
               "input[type=radio], select, [role=button], [role=link], [role=tab], [role=option], "
               "[role=menuitem], [role=checkbox], summary")

READ_JS = r"""
(() => {
  const SEL = %s;
  const vis = n => { const r = n.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const cs = getComputedStyle(n); return cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const areaOf = n => { const SEL = 'header,nav,footer,aside,main,[role=banner],[role=navigation],' +
      '[role=contentinfo],[role=complementary],[role=main],[role=search],[role=dialog]';
    let a = n.closest(SEL);
    while (a && /^(HEADER|FOOTER)$/.test(a.tagName) && !a.getAttribute('role') && a.parentElement &&
           (a.parentElement.closest('article,aside,main,nav,section') || {}).tagName === 'ARTICLE')
      a = a.parentElement.closest(SEL);
    if (!a) return 'body'; const t = a.tagName.toLowerCase(); const r = a.getAttribute('role') || '';
    if (t === 'main' || r === 'main') return 'main'; if (t === 'header' || r === 'banner') return 'header';
    if (t === 'nav' || r === 'navigation') return 'nav'; if (t === 'footer' || r === 'contentinfo') return 'footer';
    if (t === 'aside' || r === 'complementary') return 'aside'; if (r === 'dialog') return 'dialog';
    if (r === 'search') return 'search'; return 'body'; };
  const label = n => { const tag = n.tagName.toLowerCase();
    let t = n.getAttribute('aria-label') || (n.innerText || '').trim() || n.getAttribute('title') ||
            (tag === 'input' ? (n.value || '') : '');
    if (!t && n.querySelector) { const img = n.querySelector('img[alt]'); if (img) t = img.getAttribute('alt') || ''; }
    if (tag === 'input' && (n.type === 'checkbox' || n.type === 'radio')) {
      const lab = n.labels && n.labels[0]; t = (lab ? lab.innerText : '') || n.getAttribute('aria-label') || n.value || ''; }
    if (tag === 'select') { const lab = n.labels && n.labels[0];
      t = (lab ? lab.innerText : '') || n.getAttribute('aria-label') || n.getAttribute('name') || ''; }
    return String(t).replace(/\s+/g, ' ').trim().slice(0, 160); };
  const all = Array.from(document.querySelectorAll(SEL));
  const els = [];
  for (let k = 0; k < all.length && els.length < 700; k++) {
    const n = all[k]; if (!vis(n)) continue; const tag = n.tagName.toLowerCase();
    const e = {i: k, tag: tag, text: label(n), area: areaOf(n), role: n.getAttribute('role') || '',
               type: (n.getAttribute('type') || '').toLowerCase(),
               y: Math.round(n.getBoundingClientRect().top + window.scrollY)};
    if (tag === 'a') { e.href = n.href || ''; e.rel = (n.getAttribute('rel') || '').toLowerCase();
      const card = n.closest('li,article,[class*=card],[class*=product],[class*=item],[class*=tile],[class*=result]');
      if (card && card !== document.body) { const ct = (card.innerText || '').replace(/\s+/g, ' ').trim();
        if (ct && ct.length <= 400 && ct !== (n.innerText || '').replace(/\s+/g, ' ').trim()) e.ctx = ct.slice(0, 200); }
      if (!(n.innerText || '').trim() && n.querySelector && n.querySelector('img,picture,svg')) e.img_only = true; }
    if (tag === 'select') e.options = Array.from(n.options).slice(0, 40).map(o =>
        ({value: o.value, text: (o.text || '').trim().slice(0, 60), selected: o.selected}));
    if (tag === 'input' && (e.type === 'checkbox' || e.type === 'radio')) e.checked = !!n.checked;
    els.push(e);
  }
  const hs = Array.from(document.querySelectorAll('h1,h2,h3')).filter(vis)
      .map(h => (h.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 12);
  const h1 = Array.from(document.querySelectorAll('h1')).filter(vis)
      .map(h => (h.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 2);
  const mainEl = document.querySelector('main, [role=main], #main, #content, .main-content') || document.body;
  const crumbs = Array.from(document.querySelectorAll('[aria-label*=readcrumb], .breadcrumb, .breadcrumbs, nav.breadcrumb'))
      .map(b => (b.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 2);
  const ld = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
      .map(s => (s.textContent || '').slice(0, 200000)).slice(0, 16);
  const canon = document.querySelector('link[rel=canonical]');
  const og = document.querySelector('meta[property="og:type"]');
  const md = document.querySelectorAll('[itemtype*="schema.org/Product"]').length;
  return {url: location.href, title: document.title || '', headings: hs, h1: h1, breadcrumb: crumbs,
          elements: els, text: ((document.body && document.body.innerText) || '').slice(0, 20000),
          main_text: ((mainEl && mainEl.innerText) || '').slice(0, 20000), jsonld: ld,
          canonical: canon ? canon.href : '', ready: document.readyState,
          og_type: og ? (og.getAttribute('content') || '') : '', md_products: md};
})()
""" % json.dumps(CONTROL_SEL)

#: the reader's cap of visible controls (``READ_JS``)
READ_CAP = 700
#: tool format version 11 (www.compass24.de: a page whose visible navigation holds 769 links
#: stopped ``READ_JS`` at its 700 controls before the result tiles - "membership: no item of the compiled region"): the
#: same read with the cap counted on the controls OUTSIDE the header, navigation and footer (at most 3000 in all)
READ_JS_V11 = "/*anyuse:read11*/" + READ_JS.replace(
    "  for (let k = 0; k < all.length && els.length < 700; k++) {",
    "  let body = 0;\n  for (let k = 0; k < all.length && body < 700 && els.length < 3000; k++) {", 1).replace(
    "    els.push(e);\n  }", "    els.push(e); if (['header', 'nav', 'footer'].indexOf(e.area) < 0) body++;\n  }", 1)
assert READ_JS_V11.count("body < 700") == 1 and READ_JS_V11.count("body++") == 1

#: click control #k after checking it is still the control read (its text)
CLICK_JS = r"""
((k, want) => {
  const n = document.querySelectorAll(%s)[k];
  if (!n) return {ok: false, why: 'gone'};
  const t = (n.getAttribute('aria-label') || (n.innerText || '').trim() || n.getAttribute('title') || n.value || '')
      .replace(/\s+/g, ' ').trim();
  if (want && t.slice(0, 30).toLowerCase() !== want.slice(0, 30).toLowerCase() &&
      !(t === '' || want === '')) return {ok: false, why: 'changed', text: t.slice(0, 60)};
  if (n.form && n.form.querySelector('input[type=password]')) return {ok: false, why: 'password form'};
  n.scrollIntoView({block: 'center'}); n.click(); return {ok: true};
})(%%d, %%s)
""" % json.dumps(CONTROL_SEL)

#: choose option ``value`` of select #k and fire the page's own change handler
SELECT_JS = r"""
((k, value) => {
  const n = document.querySelectorAll(%s)[k];
  if (!n || n.tagName !== 'SELECT') return {ok: false, why: 'not a select'};
  n.value = value; n.dispatchEvent(new Event('input', {bubbles: true}));
  n.dispatchEvent(new Event('change', {bubbles: true})); return {ok: true, value: n.value};
})(%%d, %%s)
""" % json.dumps(CONTROL_SEL)

#: cover e-mail addresses, phone numbers and filled form fields with black
#: boxes for a capture (the DOM's text is not changed); REDACT_OFF_JS removes them
REDACT_JS = r"""
(() => {
  const box = document.createElement('div'); box.id = '__anyuse_redact';
  box.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none';
  const re = /[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d ()\/.-]{7,}\d/;
  const add = r => { const d = document.createElement('div');
    d.style.cssText = 'position:absolute;background:#000;left:' + (r.left + scrollX) + 'px;top:' + (r.top + scrollY) +
      'px;width:' + r.width + 'px;height:' + r.height + 'px'; box.appendChild(d); };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n, hits = 0;
  while ((n = w.nextNode()) && hits < 300) { if (re.test(n.nodeValue || '')) {
    const rg = document.createRange(); rg.selectNodeContents(n); for (const r of rg.getClientRects()) add(r); hits++; } }
  document.querySelectorAll('input[type=email],input[type=tel],input[type=password]').forEach(i => add(i.getBoundingClientRect()));
  document.body.appendChild(box); return hits;
})()
"""
REDACT_OFF_JS = "(() => { const b = document.getElementById('__anyuse_redact'); if (b) b.remove(); return true; })()"

ACTION_SETTLE_S = 1.0


# ---------------------------------------------------------------------------
# reading the page
# ---------------------------------------------------------------------------

async def read_page(page) -> Dict[str, Any]:
    """The site readers on the rendered page (a local CDP read; no request)."""
    r = await page.js(READ_JS)
    if not isinstance(r, dict):
        r = {}
    r.setdefault("elements", [])
    r.setdefault("jsonld", [])
    try:
        r["inputs"] = [i for i in (await page.search_inputs() or []) if i.get("visible")]
    except Exception:                                  # noqa: BLE001 - a read never fails a run
        r["inputs"] = []
    r["facts"] = W.product_facts(r.get("jsonld") or [])
    return r


async def read_page11(page) -> Dict[str, Any]:
    """Tool format version 11: ``read_page``; when the reader stopped at its cap (``READ_CAP`` controls), the
    controls once more with the cap counted outside the header, navigation and footer (``READ_JS_V11``) - its elements
    replace the capped ones (noted: ``elements_read``). Local reads only."""
    r = await read_page(page)
    if len(r.get("elements") or []) < READ_CAP:
        return r
    try:
        r2 = await page.js(READ_JS_V11)
    except Exception as exc:                           # noqa: BLE001 - a read never fails a run
        from anyuse.execution.browser_supervisor import classify
        if classify(exc):
            raise
        return r
    if isinstance(r2, dict) and isinstance(r2.get("elements"), list) and len(r2["elements"]) > len(r["elements"]):
        r["elements"] = r2["elements"]
        r["elements_read"] = "v11: past the reader's cap"
    return r


def page_summary(read: Dict[str, Any], search_urls: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    host = urllib.parse.urlsplit(str(read.get("url") or "")).hostname or ""
    nav = []
    for e in W.nav_candidates(read, host):
        if e.get("area") in ("header", "nav") and len(nav) < 30:
            t = str(e.get("text") or "").strip()
            if t and t not in nav and len(t) <= 40:
                nav.append(t)
    toggle = any(_SEARCH_WORD.search(W.fold(e.get("text"))) for e in read.get("elements") or []
                 if e.get("tag") in ("button", "a", "summary") or e.get("role") == "button")
    how = "a search box" if read.get("inputs") else ("behind a search icon" if toggle else "")
    if not how and search_urls:
        # no box found, but the page shows the site's own search URL
        how = "the site's search page (the engine opens it with the words)"
    return {"host": host, "title": read.get("title"), "headings": read.get("headings") or [],
            "nav": nav, "search": bool(read.get("inputs")) or toggle or bool(search_urls),
            "search_how": how}


_SEARCH_WORD = re.compile(r"\b(search|suche|suchen|recherche|rechercher|buscar|busqueda|cerca|zoeken|zoek|sok)\b")


def facts_line(p: Optional[Dict[str, Any]]) -> str:
    if not p:
        return ""
    bits = [f"name={p.get('name')!r}"]
    for k in ("price", "currency", "availability", "brand", "sku", "material", "color", "weight", "size"):
        if p.get(k) not in (None, ""):
            bits.append(f"{k}={p.get(k)}")
    if p.get("variants"):
        vs = []
        for v in p["variants"][:20]:
            o = (v.get("offers") or [{}])[0]
            vs.append(f"{v.get('name') or v.get('size') or v.get('sku')}: "
                      f"{o.get('availability') or ''} {o.get('price') if o.get('price') is not None else ''}".strip())
        bits.append("variants=[" + "; ".join(vs) + "]")
    return ", ".join(bits)


def read_for_planner(read: Dict[str, Any], limit: int = 3500) -> str:
    """What the planner sees of a page it asked to read (DATA)."""
    url = str(read.get("url") or "")
    lines = [f"URL: {url[:200]}", f"Title: {str(read.get('title') or '')[:160]}"]
    if read.get("h1"):
        lines.append("Main heading: " + " | ".join(read["h1"]))
    prods = [f for f in read.get("facts") or [] if f.get("name")]
    if prods:
        lines.append("Product data the page publishes (schema.org):")
        for p in prods[:4]:
            lines.append("  " + facts_line(p))
    text = str(read.get("main_text") or read.get("text") or "")
    text = re.sub(r"\n{2,}", "\n", text).strip()
    lines.append("Page text:\n" + text[:limit])
    return "\n".join(lines)


def _reg(host: str) -> str:
    p = (host or "").lower().strip(".").split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in ("co.uk", "org.uk", "com.au", "co.jp", "co.nz", "com.br", "co.za"):
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def site_block(text: str, url: str, network: Sequence[dict], start: bool = False,
               all_requests: bool = False) -> str:
    """A bot wall, challenge, 403 or 429 (the site refusing us): stop the host.

    The detector is ``blocked.detect_blocked_ui`` unchanged; its request-log
    evidence is given only the requests to the SITE (its registrable domain):
    a third-party challenge vendor's script (reCAPTCHA, hCaptcha, Turnstile) is
    never sent by the guard, so a page that rendered while it was refused was
    not walled. ``all_requests=True`` takes every request (a bench reports
    it as the counterfactual). A login wall counts only on the START page (the site
    is not reachable anonymously); later it is where an action led."""
    from anyuse.execution.blocked import HARD_REASONS, LOGIN_WALL, detect_blocked_ui
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    for r in network or []:
        if (r.get("resource_type") == "Document"
                and (urllib.parse.urlsplit(r.get("url") or "").hostname or "").lower() == host
                and int(r.get("status") or 0) in (403, 429)):
            return f"HTTP {r['status']} on the document {str(r.get('url'))[:80]}"
    reqs = [r for r in network or []
            if all_requests or _reg(urllib.parse.urlsplit(str(r.get("url") or "")).hostname or "") == _reg(host)]
    state = detect_blocked_ui(text or "", "", url or "", reqs)
    if state.blocked and (state.reason in HARD_REASONS or (start and state.reason == LOGIN_WALL)):
        return f"{state.reason} ({'; '.join(state.evidence)[:80]})"
    return ""


def link_texts(read: Dict[str, Any], limit: int = 120) -> List[str]:
    """The page's content link texts (the judge's evidence of result items)."""
    return [str(e.get("text") or "")[:120] for e in W.content_links(read)][:limit]


def net_log(network: Sequence[dict], limit: int = 400) -> List[Dict[str, Any]]:
    """The session's request log, compact (the block check's evidence)."""
    return [{"url": str(r.get("url") or "")[:200], "resource_type": r.get("resource_type"),
             "status": r.get("status")} for r in list(network or [])[:limit]]


async def capture(page, read: Dict[str, Any]) -> str:
    """A redacted screenshot of the container page (data URL), or "" for a
    page the challenge guard or the block rules refuse."""
    from anyuse.execution.openjev import challenge_guard
    obs = {"url": read.get("url"), "text": read.get("text"), "title": read.get("title")}
    if challenge_guard(obs):
        return ""
    if site_block(str(read.get("text") or ""), str(read.get("url") or ""), []):
        return ""
    try:
        await page.js(REDACT_JS)
        shot = await page.screenshot()
    except Exception:                                  # noqa: BLE001 - no image, text only
        shot = ""
    finally:
        try:
            await page.js(REDACT_OFF_JS)
        except Exception:                              # noqa: BLE001
            pass
    return shot or ""


# ---------------------------------------------------------------------------
# actions (the executor; every request passes the page's guard)
# ---------------------------------------------------------------------------

async def open_link(page, el: Dict[str, Any], settle: Callable) -> Dict[str, Any]:
    """Follow a link: a same-site http(s) link is a GET navigation to its URL
    (deterministic, compilable); anything else is clicked."""
    why = W.forbidden_control(el.get("text"), el.get("href"))
    if why:
        return {"ok": False, "why": f"refused: {why}", "forbidden": why}
    href = str(el.get("href") or "")
    host = urllib.parse.urlsplit(await page.url()).hostname or ""
    if href and W.same_site_href(href, host):
        await page.goto(href)
        await settle(page)
        return {"ok": True, "how": "goto", "href": href}
    return await click(page, el, settle)


async def click(page, el: Dict[str, Any], settle: Callable) -> Dict[str, Any]:
    why = W.forbidden_control(el.get("text"), el.get("href"))
    if why:
        return {"ok": False, "why": f"refused: {why}", "forbidden": why}
    async with acting(page):
        res = await page.js(CLICK_JS % (int(el.get("i", -1)), json.dumps(str(el.get("text") or ""))))
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "why": f"click failed: {(res or {}).get('why') if isinstance(res, dict) else res}"}
        await page.settle(ACTION_SETTLE_S)
        await settle(page)
    return {"ok": True, "how": "click"}


async def choose_option(page, el: Dict[str, Any], settle: Callable) -> Dict[str, Any]:
    async with acting(page):
        res = await page.js(SELECT_JS % (int(el.get("i", -1)), json.dumps(str(el.get("option") or ""))))
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "why": "select failed"}
        await page.settle(ACTION_SETTLE_S)
        await settle(page)
    return {"ok": True, "how": "select"}


async def default_settle(page) -> None:
    await page.settle(1.0)


#: the bounded settle: a page is settled when
#: it is complete and its DOM has been quiet for SETTLE_QUIET_MS with no data
#: request in flight (network idle), or its DOM has been quiet for SETTLE_STABLE_MS
#: whatever is in flight (a chatty page's polling never goes idle), and at the
#: latest after SETTLE_MAX_S. Waiting up to 20 s for a quiet page let the
#: page's own polling use up the arm's traffic share meanwhile.
SETTLE_MAX_S = 5.0
SETTLE_QUIET_MS = 500
SETTLE_STABLE_MS = 1200
SETTLE_POLL_S = 0.25


def settle_decision(obs: Optional[Dict[str, Any]], elapsed_s: float, inflight: int,
                    max_s: float = SETTLE_MAX_S, quiet_ms: int = SETTLE_QUIET_MS,
                    stable_ms: int = SETTLE_STABLE_MS) -> str:
    """``"wait"`` or why the bounded settle ends: ``idle`` (network idle and a
    quiet DOM), ``dom_stable`` (a stable DOM, requests still in flight) or
    ``ceiling``. Pure."""
    from anyuse.execution.typed_readiness import decide
    # the ONE settle rule (typed_readiness.decide), its "quiet" called "idle" here
    why = decide(obs, elapsed_s, ceiling_s=max_s, quiet_ms=quiet_ms, inflight=inflight, stable_ms=stable_ms)
    return "idle" if why == "quiet" else why


async def settle_bounded(page, max_s: float = SETTLE_MAX_S, clock: Callable[[], float] = time.monotonic
                         ) -> Dict[str, Any]:
    """Wait until :func:`settle_decision` ends the wait (a local CDP read of the
    page's DOM-quiet time and the session's own request log; nothing is sent)."""
    from anyuse.execution.typed_readiness import SETTLE_PROBE_JS, inflight_requests
    t0 = clock()
    polls = 0
    while True:
        try:
            obs = await page.js(SETTLE_PROBE_JS)
        except Exception:                              # noqa: BLE001 - a read never fails a run
            obs = None
        fl = inflight_requests(getattr(page, "_events", None) or [])
        why = settle_decision(obs, clock() - t0, int(fl.get("inflight") or 0), max_s=max_s)
        polls += 1
        if why != "wait":
            return {"ended": why, "waited_s": round(clock() - t0, 3), "polls": polls}
        await page.settle(SETTLE_POLL_S)


@contextlib.asynccontextmanager
async def acting(page):
    """The page session's action window (``GuardedSession.acting``), if it has one."""
    ctx = getattr(page, "acting", None)
    if ctx is None:
        yield
        return
    async with ctx():
        yield


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class Stop(Exception):
    pass


class WebArch:
    """The architecture arm for one task on one page.

    ``planner(messages) -> (text, meta)`` (Cerebras gpt-oss-120b, JSON schema);
    ``extractor(messages) -> (text, meta)`` (the same model: one detail from a
    page's text when the product data does not carry it);
    ``chooser(state, question, options, images=()) -> (index, confidence, meta)``
    (OpenJev; ``None`` = picker mode none: a hand-off);
    ``confirmer(state, question, steps, images=()) -> (p_yes, meta)`` (OpenJev
    noul; ``None`` = off). The confirmer is SHADOW: logged, never acted on."""

    def __init__(self, planner: Callable, extractor: Optional[Callable] = None,
                 chooser: Optional[Callable] = None, confirmer: Optional[Callable] = None,
                 t2: float = 0.0, settle: Callable = default_settle, spec: Optional[Dict[str, Any]] = None,
                 deadline_s: float = 300.0, max_actions: int = W.MAX_STEPS, shadow_images: bool = True,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = lambda m: None,
                 shot_sink: Optional[Callable[[str, str], str]] = None,
                 gate_check: Optional[Callable] = None, result_wait_s: float = 0.0,
                 picker_rule: Optional[Dict[str, Any]] = None):
        self.planner, self.extractor, self.chooser, self.confirmer = planner, extractor, chooser, confirmer
        #: ``gate_check(messages) -> (text, meta)``, the model check of the
        #: goal's gate (asked only when the multilingual lexicon does not gate the goal);
        #: None = the lexicon and the planner's gate type only
        self.gate_check = gate_check
        self._goal_kind: Optional[str] = None
        #: after a search, wait at most this long (local reads, no request of our
        #: own) for a script-rendered result list that names the value (0 = off)
        self.result_wait_s = float(result_wait_s or 0.0)
        self._search_urls: List[Dict[str, Any]] = []
        #: the recalibrated picker rule (``web_arch3_picker.json``: the
        #: asymmetric-none acceptance + the pairwise tie-break) and the richer options;
        #: None = the base rule and plain options
        self.picker_rule = picker_rule
        #: ``(where, data_url) -> saved name``: the redacted captures sent to OpenJev are kept as data
        self.shot_sink = shot_sink
        self.t2 = float(t2)
        self.settle = settle
        self.spec = spec or W.load_planner_spec()
        self.deadline_s = deadline_s
        self.max_actions = max_actions
        self.shadow_images = shadow_images
        self.clock = clock
        self.log = log

    # -- models ---------------------------------------------------------------------------
    def _plan(self, rec: Dict[str, Any], messages: List[Dict[str, str]], what: str
              ) -> Tuple[List[Dict[str, Any]], str]:
        for attempt in range(3):
            text, meta = self.planner(messages)
            rec["calls"]["planner"] += 1
            rec["cost"] += float((meta or {}).get("cost") or 0.0)
            steps, err = W.parse_plan(text)
            rec["planner_log"].append({"what": what, "attempt": attempt, "error": err or (meta or {}).get("error"),
                                       "text": str(text or "")[:3000], "usage": (meta or {}).get("usage")})
            if steps or (err is None and text):
                return steps, text
            if (meta or {}).get("error") and "HTTP" in str((meta or {}).get("error")):
                rec["infra"] = f"planner transport: {(meta or {}).get('error')}"[:200]
        rec["planner_empty"] = rec.get("planner_empty", 0) + 1
        return [], ""

    def _pick(self, rec, kind: str, goal: str, read: Dict[str, Any], cands: Sequence[Dict[str, Any]],
              target: str = "", value: str = "", shot: str = "") -> Optional[Dict[str, Any]]:
        """The picker (both orders + none); the screenshot variant of the same
        question is asked beside it and only logged (paired comparison)."""
        fit = W.rich_option if self.picker_rule else W.fit_option
        opts = [fit(c) for c in cands][:W.MAX_OPTIONS]
        entry: Dict[str, Any] = {"kind": kind, "target": target, "value": value, "options": opts,
                                 "cands": [{"text": c.get("text"), "href": c.get("href"), "i": c.get("i")}
                                           for c in cands[:W.MAX_OPTIONS]]}
        rec["picks"].append(entry)
        if self.chooser is None:
            entry.update(index=None, reason="no picker (mode none): hand-off")
            return None
        state = W.picker_state(read, goal)
        q = W.pick_question(kind, goal, target, value)
        p = W.consistent_pick(lambda s, qq, oo: self.chooser(s, qq, oo), state, q, opts, self.t2,
                              rule=self.picker_rule)
        rec["calls"]["picker"] += p.calls
        entry.update(p.to_dict(), question=W.question_with_none(q), t2=self.t2)
        if self.shadow_images and shot:
            pi = W.consistent_pick(lambda s, qq, oo: self.chooser(s, qq, oo, images=[shot]), state, q, opts, self.t2,
                                   rule=self.picker_rule)
            rec["calls"]["picker_image"] += pi.calls
            entry["image"] = pi.to_dict()
        if p.index is None:
            return None
        return list(cands)[p.index]

    async def _shot(self, page, rec, read: Dict[str, Any], where: str) -> str:
        shot = await capture(page, read)
        if shot and self.shot_sink is not None:
            try:
                rec.setdefault("shots", []).append({"where": where, "file": self.shot_sink(where, shot)})
            except Exception:                          # noqa: BLE001 - a saved copy never breaks a run
                pass
        return shot

    def _confirm(self, rec, where: str, kind: str, target: str, read: Dict[str, Any], shot: str) -> None:
        if self.confirmer is None:
            return
        from anyuse.execution.confirmer import compact_state
        items = []
        state = compact_state(url=str(read.get("url") or ""), title=str(read.get("title") or ""),
                              headings=read.get("headings") or [], results=items,
                              text=str(read.get("main_text") or read.get("text") or ""))
        out: Dict[str, Any] = {"where": where, "kind": kind, "target": target}
        for mode in ("text", "image"):
            if mode == "image" and not shot:
                out[mode] = {"error": "no capture"}
                continue
            q, steps, t = W.confirmer_question(kind, mode, target)
            p, meta = self.confirmer(state, q, steps, images=[shot] if mode == "image" else [])
            rec["calls"]["confirmer"] += 1
            out[mode] = {"p_yes": p, "threshold": t, "veto": W.confirmer_flags(p, t),
                         "error": (meta or {}).get("error"), "ms": (meta or {}).get("ms"), "question": q}
        rec["confirmer"].append(out)

    # -- the run --------------------------------------------------------------------------
    async def run(self, page, task: Dict[str, Any], start_url: str) -> Dict[str, Any]:
        goal = str(task.get("utterance") or task.get("goal") or "")
        t0 = self.clock()
        rec: Dict[str, Any] = {"arm": "arch", "task": task.get("id"), "goal": goal, "start_url": start_url,
                               "steps": [], "executed": [], "pages": [], "picks": [], "confirmer": [],
                               "calls": {"planner": 0, "extractor": 0, "picker": 0, "picker_image": 0,
                                         "confirmer": 0, "gate_check": 0},
                               "cost": 0.0, "planner_log": [], "answer": "", "gate": None, "refused": None,
                               "handoff": None, "stop": "", "forbidden_attempts": [], "bound": {},
                               "changes": []}
        self._goal_kind = self._detect_gate(rec, goal)
        self._item_words, self._item_hops = "", 0
        try:
            await page.goto(start_url)
            await self.settle(page)
            from anyuse.execution.agent_browser import AgentRun, _resolve_consent_wall
            ar = AgentRun()
            walled = await _resolve_consent_wall(page, ar, f"arch:{urllib.parse.urlsplit(start_url).netloc}")
            rec["consent"] = [s for s in ar.steps][:4]
            read = await self._read(page, rec)
            try:
                # the page before any search: the proof's typing-region / result-region models
                rec.setdefault("doms", {})["open"] = await page.dom()
            except Exception:                          # noqa: BLE001
                pass
            blk = site_block(str(read.get("text") or ""), str(read.get("url") or ""),
                             await page.network(want_bodies=False), start=True)
            if blk or walled:
                rec["stop"] = "blocked" if blk else "consent_wall"
                rec["blocked"] = blk or "cookie wall not rejectable"
                return await self._finish(page, rec, read, t0)
            try:
                self._search_urls = W.search_url_templates(await page.dom(), str(read.get("url") or ""),
                                                           read.get("jsonld") or [])
            except Exception:                          # noqa: BLE001
                self._search_urls = []
            rec["search_urls"] = [d["template"][:200] + " (" + d["how"] + ")" for d in self._search_urls[:4]]
            summary = page_summary(read, self._search_urls)
            rec["summary"] = summary
            steps, _t = self._plan(rec, W.planner_messages(goal, summary, self.spec), "plan")
            steps, changes = W.check_plan(goal, steps, goal_kind=self._goal_kind)
            rec["changes"] += changes
            if W.needs_refusal_feedback(goal, steps) and not self._goal_kind:
                # a refusal is for what is outside this site; a read-only goal on the site gets
                # ONE re-plan with that said (the desktop path's refusal feedback)
                hist = [{"role": "assistant", "content": json.dumps({"steps": steps}, ensure_ascii=False)},
                        {"role": "user", "content": self.spec["refusal_feedback"]}]
                steps2, _t = self._plan(rec, W.planner_messages(goal, summary, self.spec, hist), "refusal_feedback")
                steps2, changes2 = W.check_plan(goal, steps2, goal_kind=self._goal_kind)
                rec["changes"] += ["refusal feedback: re-planned"] + changes2
                steps = steps2 or steps
            steps = self._no_refusal(rec, goal, steps)
            if not steps:
                rec["stop"] = "no_plan"
                return await self._finish(page, rec, read, t0)
            rec["plan"] = [dict(s) for s in steps]
            read = await self._execute(page, rec, goal, steps, read, t0)
            return await self._finish(page, rec, read, t0)
        except Stop:
            return await self._finish(page, rec, None, t0)

    def _no_refusal(self, rec: Dict[str, Any], goal: str, steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """A READ-ONLY goal on the site is never refused (9 arch
        runs once refused after "no visible search input"): a plan that is only a refusal
        (after the refusal feedback) becomes the fallback plan (search the item's words,
        open it, read it), unless the goal needs something outside the site."""
        if self._goal_kind or not steps or len(steps) != 1 or steps[0].get("effect") != "refuse":
            return steps
        if W.outside_request(goal):
            return steps
        plan = W.fallback_plan(goal, steps[0].get("target") or "")
        if plan:
            rec["changes"].append(f"refusal of a read-only goal replaced by the fallback plan "
                                  f"(search {plan[0]['value']!r})")
            return plan
        return steps

    def _detect_gate(self, rec: Dict[str, Any], goal: str) -> Optional[str]:
        """The goal's gate BEFORE any plan: the code's detector with the
        multilingual lexicon, else the model check (either one gates); the planner's own
        gate type stays a third way (a gate step in its plan)."""
        from anyuse.execution import gate_lexicon as GL
        lex = W.commit_intent(goal)
        model: Optional[str] = None
        if not lex and self.gate_check is not None:
            try:
                text, meta = self.gate_check(GL.gate_check_messages(goal))
            except Exception as exc:                   # noqa: BLE001 - no opinion
                text, meta = "", {"error": f"{type(exc).__name__}: {exc}"[:120]}
            rec["calls"]["gate_check"] += 1
            rec["cost"] += float((meta or {}).get("cost") or 0.0)
            model = GL.parse_gate_check(text)
        kind = lex or model or None
        rec["gate_detect"] = {"lexicon": lex, "model": model, "kind": kind,
                              "by": "lexicon" if lex else ("model" if model else "")}
        return kind

    async def _read(self, page, rec) -> Dict[str, Any]:
        read = await read_page(page)
        prod = W.main_product(read.get("facts") or [])
        rec["pages"].append({"url": read.get("url"), "title": read.get("title"),
                             "h1": read.get("h1"), "facts": read.get("facts")[:6],
                             "product": prod, "text_head": str(read.get("main_text") or read.get("text") or "")[:1500]})
        return read

    async def _finish(self, page, rec, read, t0) -> Dict[str, Any]:
        try:
            if read is None:
                read = await self._read(page, rec)
            rec["final"] = {"url": read.get("url"), "title": read.get("title"), "h1": read.get("h1"),
                            "breadcrumb": read.get("breadcrumb"), "facts": read.get("facts")[:6],
                            "text": str(read.get("text") or "")[:20000], "links": link_texts(read)}
            try:
                rec["net"] = net_log(await page.network(want_bodies=False))
            except Exception:                          # noqa: BLE001
                rec["net"] = []
            if rec["stop"] not in ("blocked", "consent_wall") and self.confirmer is not None:
                task_kind = str(rec.get("task_kind") or "")
                shot = await self._shot(page, rec, read, "final") if self.shadow_images else ""
                self._confirm(rec, "final", "wrong_page",
                              W.expected_page(task_kind, rec.get("target") or "", rec.get("value") or ""),
                              read, shot)
        except Exception as exc:                       # noqa: BLE001 - the record is kept
            rec.setdefault("errors", []).append(f"finish: {type(exc).__name__}: {exc}"[:200])
        rec["program"] = W.compile_program(rec["start_url"], rec["executed"])
        rec["seconds"] = round(self.clock() - t0, 2)
        rec["model_calls"] = sum(rec["calls"].values())
        rec["stop"] = rec["stop"] or "done"
        return rec

    async def _execute(self, page, rec, goal, steps, read, t0):
        queue = list(steps)
        continuations = replans = actions = 0
        self._queue: List[Dict[str, Any]] = []
        reads: List[str] = []
        done: List[Dict[str, Any]] = []
        while queue:
            if self.clock() - t0 > self.deadline_s:
                rec["stop"] = "deadline"
                return read
            if actions >= self.max_actions:
                rec["stop"] = "max_steps"
                return read
            s = queue.pop(0)
            self._queue = queue
            actions += 1
            log: Dict[str, Any] = {"step": dict(s)}
            rec["steps"].append(log)
            eff = s["effect"]
            if eff == "gate":
                rec["gate"] = {"kind": s.get("value") or "form_send", "target": s.get("target")}
                rec["stop"] = "gate"
                rec["executed"].append({"op": "gate", "kind": s.get("value"), "target": s.get("target")})
                return read
            if eff == "refuse":
                rec["refused"] = {"why": s.get("value"), "target": s.get("target")}
                rec["stop"] = "refused"
                rec["executed"].append({"op": "refuse", "why": s.get("value")})
                return read
            if eff == "read_content":
                read = await self._ensure_item(page, rec, goal, read, log)
                read = await self._read(page, rec)
                rec["executed"].append({"op": "read"})
                reads.append(read_for_planner(read))
                done.append(s)
                log["read"] = {"url": read.get("url"), "facts": len(read.get("facts") or [])}
                if not queue or all(x["effect"] in ("extract", "answer") for x in queue):
                    if continuations >= W.MAX_CONTINUATIONS:
                        rec["stop"] = "max_continuations"
                        return read
                    continuations += 1
                    more = await self._continue(rec, goal, done, reads, queue)
                    queue = more
                continue
            if eff == "extract":
                ok = await self._extract(page, rec, s, read, log)
                done.append(dict(s, bound=ok))
                if not queue and continuations < W.MAX_CONTINUATIONS and \
                        not any(x["effect"] == "answer" for x in done):
                    continuations += 1
                    queue = await self._continue(rec, goal, done, reads, [])
                continue
            if eff == "answer":
                ok, why = W.grounded_answer(s.get("value", ""), rec["bound"],
                                            " ".join(facts_line(p.get("product")) for p in rec["pages"]))
                log["grounded"] = why
                if not ok:
                    rec["handoff"] = f"answer not grounded: {why}"
                    rec["stop"] = "handoff"
                    return read
                rec["answer"] = s.get("value", "")
                rec["executed"].append({"op": "answer", "text": rec["answer"],
                                        "template": W.answer_template(rec["answer"], rec["bound"])})
                rec["stop"] = "answered"
                return read
            # a move: search, open_item, navigate_in_site, filter_sort, paginate
            res = await self._move(page, rec, goal, s, read, log)
            log["result"] = {k: v for k, v in res.items() if k != "read"}
            if res.get("ok"):
                read = res.get("read") or await self._read(page, rec)
                done.append(s)
                if eff == "search":
                    rec.setdefault("search_value", s.get("value"))
                    self._item_words = self._item_words or str(s.get("value") or "")
                if eff == "open_item":
                    t = str(s.get("target") or "")
                    self._item_words = (W.item_words(t) if W.ordinal_in(t) else t) or self._item_words
                    if not queue or queue[0].get("effect") in ("read_content", "extract", "answer"):
                        read = await self._ensure_item(page, rec, goal, read, log)
                continue
            if res.get("forbidden"):
                rec["forbidden_attempts"].append({"by": "planner step", "why": res.get("forbidden"),
                                                  "step": dict(s)})
            kind = self._goal_kind or W.commit_intent(goal)
            if kind:
                # the goal ends in a gated action: whatever the read-only lead-up could not do,
                # the person takes over at the gate (nothing gated is ever attempted)
                gs = next((x for x in queue if x["effect"] == "gate"), None)
                rec["gate"] = {"kind": (gs or {}).get("value") or kind, "target": (gs or {}).get("target") or goal[:80],
                               "lead_up_failed": res.get("why") or res.get("handoff")}
                rec["stop"] = "gate"
                rec["executed"].append({"op": "gate", "kind": rec["gate"]["kind"], "target": rec["gate"]["target"]})
                return read
            if res.get("handoff"):
                rec["handoff"] = res.get("handoff")
            if replans >= W.MAX_REPLANS:
                rec["stop"] = "handoff" if res.get("handoff") else "failed"
                return read
            replans += 1
            read = await self._read(page, rec)
            msg = self.spec["replan_template"].format(step=json.dumps(s, ensure_ascii=False),
                                                      why=res.get("why") or res.get("handoff") or "failed",
                                                      page=read_for_planner(read, 1500))
            hist = [{"role": "assistant", "content": json.dumps({"steps": [s] + queue}, ensure_ascii=False)},
                    {"role": "user", "content": msg}]
            steps2, _t = self._plan(rec, W.planner_messages(goal, page_summary(read), self.spec, hist), "replan")
            steps2, changes = W.check_plan(goal, steps2, has_read=bool(reads), goal_kind=self._goal_kind)
            rec["changes"] += [f"replan: {c}" for c in changes]
            if W.needs_refusal_feedback(goal, steps2) and not self._goal_kind and \
                    not any("refusal feedback" in c for c in rec["changes"]):
                hist2 = hist + [{"role": "assistant", "content": json.dumps({"steps": steps2}, ensure_ascii=False)},
                                {"role": "user", "content": self.spec["refusal_feedback"]}]
                steps3, _t = self._plan(rec, W.planner_messages(goal, page_summary(read), self.spec, hist2),
                                        "refusal_feedback")
                steps3, ch3 = W.check_plan(goal, steps3, goal_kind=self._goal_kind)
                rec["changes"] += ["refusal feedback: re-planned"] + ch3
                steps2 = steps3 or steps2
            if steps2 and steps2[0].get("effect") == "refuse" and len(steps2) == 1 and not self._goal_kind \
                    and not W.outside_request(goal) and not any("fallback plan" in c for c in rec["changes"]):
                steps2 = self._no_refusal(rec, goal, steps2)
            queue = steps2
            rec["handoff"] = None
        if not rec["stop"]:
            rec["stop"] = "plan_done"
        return read

    async def _continue(self, rec, goal, done, reads, pending) -> List[Dict[str, Any]]:
        msg = W.continuation_message(self.spec, done, reads[-2:], pending)
        hist = [{"role": "assistant", "content": json.dumps({"steps": done}, ensure_ascii=False)[:4000]},
                {"role": "user", "content": msg}]
        steps, _t = self._plan(rec, W.planner_messages(goal, rec.get("summary") or {}, self.spec, hist),
                               "continuation")
        steps, changes = W.check_plan(goal, steps, has_read=True, goal_kind=self._goal_kind)
        rec["changes"] += [f"continuation: {c}" for c in changes]
        return steps

    async def _extract(self, page, rec, s, read, log) -> bool:
        name = s.get("value") or s.get("field") or "detail"
        field_ = (s.get("field") or "").lower()
        find = s.get("find") or ""
        prod = W.main_product(read.get("facts") or [])
        text = str(read.get("text") or "")
        # 1. the product data the page publishes
        if prod and field_ in ("price", "currency", "availability", "brand", "sku", "name") and \
                prod.get(field_) not in (None, ""):
            v = prod.get(field_)
            v = (f"{v:.2f}" if isinstance(v, float) else str(v))
            shown = find if find and W.locate(text, find) else v
            rec["bound"][name] = shown
            rec["executed"].append({"op": "extract", "name": name, "field": field_, "source": "jsonld",
                                    "jsonld_value": v, "shown": shown,
                                    "anchor": W.anchor_of(text, find) if find and text.find(find) >= 0 else ""})
            log["bound"] = {"name": name, "source": "jsonld", "value": v, "shown": shown}
            return True
        # 2. the words the planner quoted, found in the page text
        if find and W.locate(text, find):
            rec["bound"][name] = find
            rec["executed"].append({"op": "extract", "name": name, "field": field_, "source": "text",
                                    "anchor": W.anchor_of(text, find), "like": find})
            log["bound"] = {"name": name, "source": "text", "value": find}
            return True
        # 3. one detail read from the text by the model, verified in the text
        if self.extractor is not None:
            msgs = [{"role": "system", "content": self.spec["extract_system"]},
                    {"role": "user", "content": f"Detail wanted: {s.get('target') or field_}\n\nPage text:\n"
                                                + str(read.get("main_text") or text)[:6000]}]
            out, meta = self.extractor(msgs)
            rec["calls"]["extractor"] += 1
            rec["cost"] += float((meta or {}).get("cost") or 0.0)
            try:
                d = json.loads(str(out)[str(out).find("{"): str(out).rfind("}") + 1])
                val, quote = str(d.get("value") or ""), str(d.get("quote") or "")
            except ValueError:
                val, quote = "", ""
            if val and quote and W.locate(text, quote) and (val in quote or W.fold(val) in W.fold(quote)):
                rec["bound"][name] = val
                rec["executed"].append({"op": "extract", "name": name, "field": field_, "source": "text",
                                        "anchor": W.anchor_of(text, val) if text.find(val) >= 0 else "",
                                        "like": val, "by": "extractor"})
                log["bound"] = {"name": name, "source": "extractor", "value": val}
                return True
            log["extractor"] = {"value": val[:80], "quote": quote[:120], "verified": False}
        log["bound"] = None
        rec["executed"].append({"op": "extract", "name": name, "field": field_, "source": "none"})
        return False

    async def _ensure_item(self, page, rec, goal: str, read: Dict[str, Any], log) -> Dict[str, Any]:
        """Before answering about ONE item, the page must be that item's
        own page (``W.product_detail``). A category, series or search page (fonq,
        naturkompaniet were answered from them) is not: continue to the item on it, by the
        item's words (the map, then the picker), or, when the user asks for any one item
        of a section, its first item. At most one such step per run."""
        if self._goal_kind or self._item_hops >= 1:
            return read
        words = self._item_words or W.goal_item_words(goal)
        if not (W.content_tokens(words) or W.any_item_goal(goal)):
            return read
        pd, why = W.product_detail(read, words)
        log["product_page"] = {"verdict": pd, "why": why}
        if pd is not False:
            return read
        host = urllib.parse.urlsplit(str(read.get("url") or "")).hostname or ""
        try:
            dom = await page.dom()
        except Exception:                              # noqa: BLE001
            dom = ""
        el, short, how = (W.resolve_item(words, read, dom, host) if W.content_tokens(words) else (None, [], "no words"))
        if el is None and W.any_item_goal(goal):
            region, how = W.result_links(read, dom, host)
            el = region[0] if region else None
            how = f"any one item: the first of the list ({how})"
        if el is None and short:
            el = self._pick(rec, "open_item", goal, read, short, target=words)
            how = "the picker"
        self._item_hops += 1
        if el is None:
            log["item_hop"] = {"how": how, "opened": None}
            return read
        res = await open_link(page, el, self.settle)
        log["item_hop"] = {"how": how, "opened": el.get("href"), "ok": res.get("ok")}
        if not res.get("ok"):
            return read
        rec["executed"].append({"op": "goto", "href": el.get("href"), "text": el.get("text"),
                                "rule": {"kind": "names", "words": words}, "effect": "open_item"})
        rec["changes"].append(f"not an item page ({why}): continued to {str(el.get('text') or '')[:60]!r}")
        return await self._read(page, rec)

    async def _search_fallback(self, page, rec, read, value: str, log, failed: str) -> Dict[str, Any]:
        """The search box failed: the site's search URL, else navigation by the words."""
        tmpls = list(getattr(self, "_search_urls", []) or [])
        try:
            dom = await page.dom()
            tmpls += [t for t in W.search_url_templates(dom, str(read.get("url") or ""), read.get("jsonld") or [])
                      if t["template"] not in {x["template"] for x in tmpls}]
        except Exception:                              # noqa: BLE001
            pass
        for t in tmpls[:2]:
            u = W.fill_search_url(t["template"], value)
            try:
                await page.goto(u)
                await self.settle(page)
            except Exception as exc:                   # noqa: BLE001 - the next fallback
                log.setdefault("fallback", []).append({"url": u[:200], "error": str(exc)[:120]})
                continue
            r2 = await self._read(page, rec)
            ok_page = W.fold(r2.get("text"))[:2000] != W.fold(read.get("text"))[:2000] and \
                not str(r2.get("url") or "").startswith("chrome-error")
            log.setdefault("fallback", []).append({"url": u[:200], "how": t["how"], "ok": ok_page})
            if ok_page:
                log["search_via"] = "url"
                return {"ok": True, "read": r2, "op": {"op": "search", "value": value, "via": "url",
                                                       "template": t["template"], "how": t["how"]}}
        # navigation by the words (the map; the picker when the words name no one link)
        host = urllib.parse.urlsplit(str(read.get("url") or "")).hostname or ""
        cands = W.nav_candidates(read, host)
        named = [c for c in cands if W.overlap(c.get("text"), value) > 0]
        el, short, how = W.resolve_words(value, named)
        log["search_via"] = "navigation"
        log["nav_fallback"] = {"how": how, "candidates": len(named)}
        if el is None and short:
            el = self._pick(rec, "navigate_in_site", f"find {value}", read, short, target=value)
        if el is None:
            return {"ok": False, "why": f"{failed}; no search URL on the page and no link names {value!r}"}
        res = await open_link(page, el, self.settle)
        if not res.get("ok"):
            return res
        rec["executed"].append({"op": "goto", "href": el.get("href"), "text": el.get("text"),
                                "rule": {"kind": "names", "words": value}, "effect": "navigate_in_site"})
        return {"ok": True, "navigated": True, "read": await self._read(page, rec)}

    async def _await_results(self, page, rec, value: str, log, ceiling_s: Optional[float] = None) -> None:
        """A script-rendered result list needs its scripts and data;
        after the bounded settle, wait (at most ``ceiling_s``, local reads only, no
        request of our own) until a content link names a word of the value."""
        ceiling_s = self.result_wait_s if ceiling_s is None else ceiling_s
        t0 = self.clock()
        words = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in W.content_tokens(value)}
        if not words or ceiling_s <= 0:
            return
        while self.clock() - t0 < ceiling_s:
            try:
                r = await read_page(page)
            except Exception:                          # noqa: BLE001
                return
            links = W.content_links(r)
            if any(words & {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in W.tokens(e.get("text"))}
                   for e in links):
                log["results_after_s"] = round(self.clock() - t0, 2)
                return
            await page.settle(0.5)
        log["results_after_s"] = None

    async def _move(self, page, rec, goal, s, read, log) -> Dict[str, Any]:
        eff = s["effect"]
        target, value = s.get("target") or "", s.get("value") or ""
        host = urllib.parse.urlsplit(str(read.get("url") or "")).hostname or ""
        rec.setdefault("target", target or value)
        rec.setdefault("value", value)
        if eff == "search":
            if not value:
                return {"ok": False, "why": "search without words"}
            nxt = next((x for x in (self._queue or [])[:1] if x.get("effect") == "open_item"), None)
            if nxt is not None and W.ordinal_of(nxt.get("target") or "") is None and not W.asks_search(goal):
                # (never when the user asked for a search: the search is the task)
                el, _short, _how = W.resolve_words(nxt.get("target") or "", W.content_links(read, host))
                if el is not None:
                    # the item the search is for is linked on the page shown: no search needed
                    log["search_skipped"] = f"the page shown links {el.get('text')!r}"
                    return {"ok": True, "read": read, "skipped": True}
            if W.forbidden_control(value) and W.commit_intent(value):
                return {"ok": False, "why": "the search words name a gated action", "forbidden": "search words"}
            url0 = str(read.get("url") or "")
            probe = await page.type_search(value)
            await self.settle(page)
            log["probe"] = {k: probe.get(k) for k in ("input", "document", "note", "revealed")}
            failed = ""
            r2: Dict[str, Any] = {}
            if probe.get("note") and not probe.get("input"):
                failed = f"search: {probe.get('note')}"
            else:
                r2 = await self._read(page, rec)
                if str(r2.get("url")) == url0 and W.fold(r2.get("text"))[:2000] == W.fold(read.get("text"))[:2000]:
                    failed = "the search changed nothing on the page"
            op: Dict[str, Any] = {"op": "search", "value": value,
                                  "input": {k: (probe.get("input") or {}).get(k) for k in ("index", "name", "id")}}
            if failed:
                # never give up a read-only goal because the box was not found:
                # the site's own search URL seen on the page (SearchAction, a GET form, a search
                # link), else the map's navigation by the words (the picker when they name none)
                fb = await self._search_fallback(page, rec, read, value, log, failed)
                if not fb.get("ok"):
                    return fb
                if fb.get("navigated"):
                    return fb
                r2, op = fb["read"], fb["op"]
            await self._await_results(page, rec, value, log)
            r2 = await self._read(page, rec)
            try:
                rec.setdefault("doms", {})["after_search"] = await page.dom()
            except Exception:                          # noqa: BLE001
                pass
            op["url_after"] = r2.get("url")
            rec["executed"].append(op)
            if self.confirmer is not None:
                shot = await self._shot(page, rec, r2, "after_search") if self.shadow_images else ""
                self._confirm(rec, "after_search", "result_page", value, r2, shot)
            return {"ok": True, "read": r2}
        if eff == "paginate":
            el = W.next_page(read)
            if el is None:
                return {"ok": False, "why": "no next-page link"}
            res = await open_link(page, el, self.settle)
            if res.get("ok"):
                rec["executed"].append({"op": "paginate", "href": el.get("href"), "text": el.get("text")})
            return res
        dom = ""
        if eff == "open_item":
            try:
                dom = await page.dom()
            except Exception:                          # noqa: BLE001
                dom = ""
            words = target
            if W.ordinal_in(target) and W.user_ordinal(goal) is None and not W.any_item_goal(goal):
                # a position ("first result") only when the USER asks for one;
                # otherwise the item the task names: the target's own words, else the
                # value just searched (map by title first, then the picker)
                words = W.item_words(target) or str(rec.get("search_value") or "")
                log["position_dropped"] = {"planner": target, "words": words}
            resolved, short, how = W.resolve_item(words, read, dom, host)
            cands = short
        else:
            if eff == "navigate_in_site":
                cands = W.nav_candidates(read, host)
                words = W.nav_words(target)
            else:                                      # filter_sort
                cands = W.filter_candidates(read)
                words = value or target
            resolved, short, how = W.resolve_words(words, cands)
        log["map"] = {"how": how, "candidates": len(cands), "shortlist": len(short)}
        rule: Dict[str, Any]
        if resolved is not None:
            el = resolved
            k = W.ordinal_of(words)
            rule = {"kind": "ordinal", "n": k} if k is not None else {"kind": "names", "words": words}
        else:
            if not short:
                return {"ok": False, "why": f"{eff}: no candidates on the page ({how})"}
            shot = await self._shot(page, rec, read, f"pick_{eff}") \
                if (self.shadow_images and self.chooser is not None) else ""
            kind = eff if eff in ("open_item", "navigate_in_site", "filter_sort") else "open_item"
            el = self._pick(rec, kind, goal, read, short, target=target, value=value, shot=shot)
            if el is None:
                return {"ok": False, "handoff": f"picker hand-off ({rec['picks'][-1].get('reason')})",
                        "why": "the picker did not accept a candidate"}
            rule = {"kind": "picker", "text": el.get("text")}
        forb = W.forbidden_control(el.get("text"), el.get("href"))
        if forb:
            return {"ok": False, "why": f"refused: {forb}", "forbidden": forb}
        if el.get("tag") == "option":
            res = await choose_option(page, el, self.settle)
            op = {"op": "select", "select_i": el.get("i"), "text": el.get("text"), "option": el.get("option"),
                  "option_text": el.get("option_text"), "rule": rule}
        elif el.get("tag") == "a" and el.get("href"):
            res = await open_link(page, el, self.settle)
            op = {"op": "goto", "href": el.get("href"), "text": el.get("text"), "rule": rule, "effect": eff}
        else:
            res = await click(page, el, self.settle)
            op = {"op": "click", "text": el.get("text"), "rule": rule, "effect": eff}
        if res.get("ok"):
            rec["executed"].append(op)
        return res


# ---------------------------------------------------------------------------
# the compiled program: replayed with NO model
# ---------------------------------------------------------------------------

async def _wait_named(page, value: str, ceiling_s: float) -> float:
    """Wait (local reads only) until a content link names a word of ``value``; seconds or -1."""
    words = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in W.content_tokens(value)}
    t0 = time.monotonic()
    while words and time.monotonic() - t0 < ceiling_s:
        try:
            r = await read_page(page)
        except Exception:                              # noqa: BLE001
            return -1.0
        if any(words & {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in W.tokens(e.get("text"))}
               for e in W.content_links(r)):
            return round(time.monotonic() - t0, 2)
        await page.settle(0.5)
    return -1.0


async def replay(page, ops: Sequence[Dict[str, Any]], settle: Callable = default_settle,
                 by_rule: bool = False, result_wait_s: float = 0.0) -> Dict[str, Any]:
    """Run a program's ops in a fresh page. ``by_rule`` re-resolves links by
    their rule (the words, the ordinal) on the page shown (the proof's
    parameterised tool); else a recorded link URL is opened again (the
    compiled repeat of the same task). No model is ever asked."""
    rec: Dict[str, Any] = {"arm": "compiled", "steps": [], "pages": [], "answer": "", "gate": None,
                           "refused": None, "stop": "", "bound": {}, "model_calls": 0,
                           "forbidden_attempts": [], "doms": {}}
    read: Dict[str, Any] = {}
    t0 = time.monotonic()

    async def fresh():
        r = await read_page(page)
        rec["pages"].append({"url": r.get("url"), "title": r.get("title"), "h1": r.get("h1"),
                             "facts": r.get("facts")[:6], "product": W.main_product(r.get("facts") or []),
                             "text_head": str(r.get("main_text") or r.get("text") or "")[:1500]})
        return r
    try:
        for o in ops:
            op = o.get("op")
            log = {"op": op}
            rec["steps"].append(log)
            if op == "open":
                await page.goto(o["url"])
                await settle(page)
                read = await fresh()
                try:
                    rec["doms"]["open"] = await page.dom()
                except Exception:                      # noqa: BLE001
                    pass
                blk = site_block(str(read.get("text") or ""), str(read.get("url") or ""),
                                 await page.network(want_bodies=False), start=True)
                if blk:
                    rec.update(stop="blocked", blocked=blk)
                    break
            elif op == "search":
                if o.get("via") == "url" and o.get("template"):
                    # the site's own search URL, the value filled in
                    await page.goto(W.fill_search_url(o["template"], str(o.get("value") or "")))
                    await settle(page)
                    log["via"] = "url"
                else:
                    probe = await page.type_search(str(o.get("value") or ""))
                    await settle(page)
                    log["probe"] = {k: probe.get(k) for k in ("input", "document", "note")}
                    if probe.get("note") and not probe.get("input"):
                        rec["stop"] = "failed"
                        log["why"] = probe.get("note")
                        break
                if result_wait_s:
                    await _wait_named(page, str(o.get("value") or ""), result_wait_s)
                read = await fresh()
                try:
                    rec["doms"]["after_search"] = await page.dom()
                except Exception:                      # noqa: BLE001
                    pass
                rec["search_url"] = read.get("url")
            elif op in ("goto", "paginate"):
                el = None
                rule = o.get("rule") or {}
                host = urllib.parse.urlsplit(str(read.get("url") or "")).hostname or ""
                if op == "paginate":
                    el = W.next_page(read) if by_rule else {"tag": "a", "href": o.get("href"),
                                                            "text": o.get("text")}
                elif by_rule and rule.get("kind") in ("names", "ordinal"):
                    dom = await page.dom()
                    if o.get("effect") == "navigate_in_site":
                        cands = W.nav_candidates(read, host)
                    else:
                        cands, how = W.result_links(read, dom, host)
                        log["region"] = how
                    if rule.get("kind") == "ordinal":
                        k = int(rule.get("n") or 0)
                        el = cands[k] if k < len(cands) else None
                    else:
                        hits = [c for c in cands if W.names_all(c.get("text"), rule.get("words"))]
                        if not hits and o.get("effect") != "navigate_in_site":
                            hits = [c for c in W.content_links(read, host)
                                    if W.names_all(c.get("text"), rule.get("words"))]
                        el = hits[0] if hits else None
                elif o.get("href"):
                    el = {"tag": "a", "href": o.get("href"), "text": o.get("text")}
                if el is None:
                    rec["stop"] = "failed"
                    log["why"] = "no link for the rule"
                    break
                log["href"] = el.get("href")
                res = await open_link(page, el, settle)
                if not res.get("ok"):
                    rec["stop"] = "failed"
                    log["why"] = res.get("why")
                    if res.get("forbidden"):
                        rec["forbidden_attempts"].append({"by": "replay", "why": res.get("forbidden")})
                    break
                read = await fresh()
            elif op in ("click", "select"):
                read = read or await fresh()
                if op == "select":
                    cands = [c for c in W.filter_candidates(read) if c.get("tag") == "option"
                             and W.fold(c.get("text")) == W.fold(o.get("text"))]
                    el = cands[0] if cands else None
                    res = await choose_option(page, el, settle) if el else {"ok": False, "why": "option gone"}
                else:
                    cands = [c for c in read.get("elements") or [] if W.fold(c.get("text")) == W.fold(o.get("text"))]
                    el = cands[0] if cands else None
                    res = await click(page, el, settle) if el else {"ok": False, "why": "control gone"}
                if not res.get("ok"):
                    rec["stop"] = "failed"
                    log["why"] = res.get("why")
                    break
                read = await fresh()
            elif op == "read":
                read = await fresh()
            elif op == "extract":
                name = o.get("name") or "detail"
                prod = W.main_product(read.get("facts") or [])
                text = str(read.get("text") or "")
                v = None
                if o.get("source") == "jsonld" and prod and prod.get(o.get("field")) not in (None, ""):
                    raw = prod.get(o.get("field"))
                    v = f"{raw:.2f}" if isinstance(raw, float) else str(raw)
                    if o.get("anchor"):
                        shown = W.value_after(text, o["anchor"], like=o.get("shown") or "")
                        v = shown if shown and W.parse_numbers(shown)[:1] == W.parse_numbers(v)[:1] else \
                            (o.get("shown") if o.get("shown") and W.locate(text, o.get("shown")) else v)
                    elif o.get("shown") and o.get("shown") != o.get("jsonld_value") and W.locate(text, o["shown"]):
                        v = o["shown"]
                elif o.get("source") == "text" and o.get("anchor"):
                    v = W.value_after(text, o["anchor"], like=o.get("like") or "")
                if v is None:
                    rec["stop"] = "failed"
                    log["why"] = f"extract {name!r}: not found again"
                    break
                rec["bound"][name] = v
                log["bound"] = v
            elif op == "answer":
                rec["answer"] = W.fill_template(o.get("template") or o.get("text") or "", rec["bound"])
                rec["stop"] = "answered"
            elif op == "gate":
                rec["gate"] = {"kind": o.get("kind"), "target": o.get("target")}
                rec["stop"] = "gate"
                break
            elif op == "refuse":
                rec["refused"] = {"why": o.get("why")}
                rec["stop"] = "refused"
                break
        if not read:
            read = await fresh()
        else:
            read = await fresh()
        rec["final"] = {"url": read.get("url"), "title": read.get("title"), "h1": read.get("h1"),
                        "breadcrumb": read.get("breadcrumb"), "facts": read.get("facts")[:6],
                        "text": str(read.get("text") or "")[:20000], "links": link_texts(read)}
        try:
            rec["doms"]["final"] = await page.dom()
        except Exception:                              # noqa: BLE001
            pass
        try:
            rec["net"] = net_log(await page.network(want_bodies=False))
        except Exception:                              # noqa: BLE001
            rec["net"] = []
    except Exception as exc:                           # noqa: BLE001 - a replay failure is a record
        from anyuse.execution.browser_supervisor import classify
        if classify(exc):
            raise
        rec["stop"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
    rec["stop"] = rec["stop"] or "done"
    rec["seconds"] = round(time.monotonic() - t0, 2)
    return rec


# ---------------------------------------------------------------------------
# the agent arms: an observing session and the answer / refusal protocol
# ---------------------------------------------------------------------------

AGENT_TASK_SUFFIX = (" (Read-only: when the task asks a question, finish with "
                     "{\"action\":\"done\",\"answer\":\"<the answer, as the page states it>\"}. "
                     "If the task asks you to buy, add to a cart, order, pay, log in, sign up or send "
                     "anything, do not do it: reply {\"action\":\"abstain\",\"reason\":\"needs the person\"}.)")


def agent_record(run, task: Dict[str, Any], arm: str, pages: Sequence[Dict[str, Any]],
                 guard_failed: Sequence[Tuple[str, str]] = ()) -> Dict[str, Any]:
    """An agent's AgentRun as a run record in the architecture's format (the
    same judge reads both): the answer, a refusal (abstain), the pages seen,
    the program its search compiles to, and every forbidden attempt (a chosen
    control naming a gated action, a refused cart/login/account path or a
    refused non-GET request)."""
    steps = [s for s in run.steps if isinstance(s, dict)]
    last = next((s for s in reversed(steps) if s.get("action") in ("done", "abstain", "refuse", "ask_user")), {})
    answer = str(last.get("answer") or "") if last.get("action") == "done" else ""
    refused = None
    if last.get("action") in ("abstain", "refuse", "ask_user") or run.stop == "abstain":
        refused = {"why": str(last.get("reason") or last.get("question") or "abstain")[:200]}
    forbidden = []
    for t in run.trace or []:
        ch = t.get("chosen") or {}
        a = t.get("choice") or {}
        if a.get("action") in ("click", "submit", "search", "type") and ch:
            why = W.forbidden_control(ch.get("name"), ch.get("href"))
            if why and a.get("action") == "click":
                forbidden.append({"by": "agent click", "why": why, "control": str(ch.get("name"))[:60]})
    for url, why in guard_failed:
        w = str(why)
        if ("method" in w and "not sent" in w) or "account/login/cart" in w or \
                (W._CONTROL_PATH.search(urllib.parse.urlsplit(str(url)).path or "") and "robots" not in w
                 and "third-party" not in w):
            forbidden.append({"by": "guard refusal", "why": w[:80], "url": str(url)[:120]})
    ops: List[Dict[str, Any]] = [{"op": "open", "url": run.start_url}]
    searched = False
    for t in run.trace or []:
        if (t.get("outcome") or {}).get("acted") is False:
            continue          # the session refused it (the page had changed): nothing was done
        a = t.get("choice") or {}
        ch = t.get("chosen") or {}
        if a.get("action") == "search" or (a.get("action") == "type" and a.get("text")):
            ops.append({"op": "search", "value": str(a.get("text") or ""), "input": {"agent_index": a.get("index")}})
            searched = True
        elif a.get("action") == "click" and searched and ch.get("href"):
            ops.append({"op": "goto", "href": ch.get("href"), "text": ch.get("name"),
                        "rule": {"kind": "names", "words": ops[-1].get("value") if ops[-1].get("op") == "search"
                                 else ch.get("name")}})
    # collapse repeated searches: the LAST search and what followed it
    ks = [i for i, o in enumerate(ops) if o.get("op") == "search"]
    if len(ks) > 1:
        ops = ops[:1] + ops[ks[-1]:]
    program = {"version": 1, "start_url": run.start_url, "ops": ops + [{"op": "read"}],
               "compilable": True, "why": "", "model_calls": 0}
    return {"arm": arm, "task": task.get("id"), "goal": task.get("utterance"), "start_url": run.start_url,
            "steps": steps, "pages": list(pages), "answer": answer, "refused": refused, "gate": None,
            "stop": run.stop or "", "forbidden_attempts": forbidden, "program": program,
            "final": {"url": run.final_url, "title": "", "text": str(run.text or "")[:20000]},
            "model_calls": int(run.model_calls or 0), "cost": float(run.cost_usd or 0.0),
            "tokens": {"in": run.tokens_in, "out": run.tokens_out}, "seconds": round(run.seconds, 2),
            "net": net_log(run.network),
            "error": run.error, "search_value": next((o.get("value") for o in ops if o.get("op") == "search"), "")}
