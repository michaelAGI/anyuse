"""A CDP page session in which EVERY request is decided and paced before it is sent.

Background: a browser path that paced only top-level
navigations and charged a page load's sub-requests afterwards let one load send
1,011 requests to one host. Here the page runs with ``Fetch.enable``
on every request, whatever its type and stage. Each ``Fetch.requestPaused`` is
answered by :class:`LoadGuard`, and a request is only sent (``continueRequest``)
once it has passed all of these:

* it is http(s) and goes to the page's OWN site (:class:`OriginPolicy`: the
  same host, plus the site's own API/CDN hosts only when the run lists them
  explicitly). Every other host is failed in the browser and recorded; a
  third-party RESPONSE that reached the network anyway is a fatal violation;
* its method is GET or HEAD, or one the caller's run policy explicitly allows
  (``RequestBudget(allow_methods=...)``); any other method is failed before it
  is sent and recorded in ``RequestBudget.refused_methods``;
* its resource type is Document, XHR, Fetch or Script. Images, fonts, media,
  stylesheets, pings, manifests and anything else are failed in the browser;
* it is not an account/login/register/cart path, and ``allow_url`` (robots)
  allows it;
* the page load has sent fewer than ``per_load_cap`` requests, and the host
  fewer than ``per_host_cap`` in the whole run. At the host cap the host is
  stopped;
* it waited on the SHARED per-host pacer (the one the HTTP fetcher and the
  compiled requests use). That gives >= ``min_interval_s`` between any two
  requests to one host, sub-requests and redirect hops included.

Request economy (docs/TRAFFIC_POLICY.md):
the guard keeps a RUN cache of STATIC assets only (scripts; stylesheets, images
and fonts when the realistic-browser mode lets them be sent at all). Documents
and XHR/fetch answer data are NEVER cached, so a validation input's data always
comes from the site. The cache follows HTTP: only a 200 GET that the response
lets a private cache keep (no ``no-store``/``no-cache``/``Vary: *``, an explicit
or heuristic freshness lifetime), keyed by method + full URL + the request
headers that can change it (``Accept``, ``Accept-Language``, ``Origin`` and every
header the response ``Vary``s on), dropped once stale; a request or response
that carries a validation input (URL, body or header) is never stored or
answered. A hit is answered with ``Fetch.fulfillRequest``: nothing is sent, so
it is neither paced nor counted. The one exception is a one-shot HANDOFF: a
discovery document just fetched by the paced HTTP GET is given to the browser
render once, and never when its URL carries an input value. Every request that
carries a validation input is recorded with the path it took (``network``,
``refused``, ``cache``); a ``cache`` path is a violation.

Defence in depth: the browser cache is disabled, so every request passes the
interceptor. WebSocket, EventSource, sendBeacon, workers and service-worker
registration are removed before any page script runs. Child targets (OOPIF,
workers) are auto-attached paused and never resumed. An independent AUDIT counts
what reached the network in the ``Network`` domain. A response for a request the
guard never continued, a WebSocket, or a host over either cap is a
:class:`TrafficViolation`, and the caller must stop the run.

Container-only, like :mod:`agent_cdp`: nothing here can reach the desktop.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution.agent_browser import SEARCH_TOKENS_JS
from anyuse.execution.agent_cdp import CdpAgentSession

ALLOWED_TYPES = frozenset({"Document", "XHR", "Fetch", "Script"})
#: realistic-browser mode (OFF by default): same-site stylesheets, images and
#: fonts are also sent, up to the run's per-site byte ceiling
REALISTIC_EXTRA_TYPES = frozenset({"Stylesheet", "Image", "Font"})
#: methods the guard sends without an explicit caller policy (read-only)
DEFAULT_METHODS = frozenset({"GET", "HEAD"})
PER_LOAD_CAP = 30
#: a catalogued search provider (``web_scope`` kind ``provider``) has its OWN share of requests
#: per page load, outside the site's per-load cap and static share; counted in the run total (the site's ceiling) and
#: paced with the site
PROVIDER_LOAD_SHARE = 10
#: ONE pacing policy for every browser path: >= DOC_MIN_INTERVAL_S between two documents
#: (main-frame navigations) of one site and >= REQUEST_MIN_INTERVAL_S between any two requests to one host. The
#: agent's guarded page (``harness_product.guarded_page_factory``), the replay and the fallback (the benches' guarded
#: site context ``web_arch_live`` / ``web_arch2_live.SiteCtx``, which ``watch_live`` and the MCP route use) read these.
DOC_MIN_INTERVAL_S = 3.0
REQUEST_MIN_INTERVAL_S = 1.0
PER_HOST_CAP = 200
#: ONLY static assets may be answered from the run cache.
#: Documents and XHR/fetch data are always fetched from the site (a cached one
#: would let a proof replay be answered with data the run had already seen).
STATIC_TYPES = frozenset({"Script", "Stylesheet", "Image", "Font"})
#: (sites did not render while the static share skipped their assets): with ``RequestBudget(browser_statics=True)``
#: a STATIC asset (script,
#: stylesheet, font, image; GET/HEAD) of the site itself, its own host, its store platform's asset CDN or its granted
#: search provider loads as in a normal browser visit - sent, counted APART (``RequestBudget.static_sent``), logged and
#: run-cached, outside every request ceiling (the run total, the per-load cap, the static share, the provider's share).
#: Only a runaway guard per page load remains; hitting it is a typed stop (the site is stopped for the run).
#: (a store's start page can load hundreds of distinct script modules - a legitimate heavy page, stopped by a guard
#: of 300): the guard catches only REAL LOOPS - the SAME
#: static URL requested more than ``STATIC_RUNAWAY_REPEAT`` times in one page load, or more than
#: ``STATIC_RUNAWAY_DISTINCT`` distinct statics in one page load
STATIC_RUNAWAY_REPEAT = 5
STATIC_RUNAWAY_DISTINCT = 1000
#: the refusal's prefix (and the typed stop's reason) when the runaway guard is hit
STATIC_RUNAWAY = "static runaway"
#: browser statics in flight at once (the "behave like a person" burst, docs/TRAFFIC_POLICY.md); they leave the
#: 1 req/s lane of documents and data
BROWSER_STATIC_IN_FLIGHT = 4
#: the web-scope kinds whose static assets are browser statics ("": the site itself, by the run's origin policy)
BROWSER_STATIC_KINDS = frozenset({"", "own_host", "platform_cdn", "provider", "page_code"})
#: (a store's checkout script files fetched by fetch()): an XHR / fetch GET of
#: a static FILE - by its URL's extension, or by its response's content type - is a browser static too, not a data call
STATIC_FILE_EXT = re.compile(r"\.(?:m?js|css|woff2?|ttf|otf|eot|png|jpe?g|gif|svg|webp|avif|ico|bmp)$", re.I)
STATIC_MIME = re.compile(r"^(?:(?:application|text)/(?:x-)?(?:javascript|ecmascript)|text/css|font/|image/|"
                         r"application/(?:font-woff2?|x-font-\w+|vnd\.ms-fontobject))", re.I)
#: how many cache refusals keep their headers; the requests a proof replay's script share always leaves free
CACHE_REFUSALS_KEPT = 60
REPLAY_DOC_RESERVE = 4
#: every static resource type the static-share rule covers (Media is never sent
#: by default; listed so a caller that allows it is covered too)
SHARE_TYPES = STATIC_TYPES | {"Media"}
CACHE_TYPES = STATIC_TYPES
#: at most this many raw CDP events of flagged requests are kept per run
VIOLATION_EVENTS_MAX = 400
#: resource types that carry answer data: never cached
DATA_TYPES = frozenset({"Document", "XHR", "Fetch"})
#: request headers that can change a static response: always part of the key
KEY_HEADERS = ("accept", "accept-language", "origin")
#: heuristic freshness (RFC 9111 4.2.2): 10% of the age since Last-Modified, capped
HEURISTIC_MAX_S = 24 * 3600.0
CACHE_MAX_BYTES = 4 * 1024 * 1024
#: the SITE static cache's lifetime for a
#: static asset whose response gives no usable freshness (a cookie, no-cache, no lifetime):
#: one benchmark session of one site, as a person's browser keeps a site's scripts
SITE_CACHE_TTL_S = 6 * 3600.0
#: freshness refusals the site static cache overrides (never ``no-store`` or ``Vary: *``)
SITE_CACHE_OVERRIDES = ("sets a cookie", "no freshness information", "no-cache (needs revalidation)",
                        "stale on arrival", "expired")
#: data requests whose PATH names
#: telemetry are the page's background noise even inside an action window
#: (beacons, pixels, analytics, logging, metrics); with ``noise_share`` on they
#: are counted against the static share, never the action share
TELEMETRY_PATH = re.compile(
    r"(?i)(^|[/._-])(collect|track|tracking|tracker|beacon|beacons|pixel|pixels|web-pixels[^/]*|"
    r"analytics|telemetry|rum|monorail|produce_batch|metrics|gtm|gtag|impression|impressions|"
    r"log-error|logging|heartbeat|ping|pageview|pageviews)([/._?-]|$)")
#: resource types that are the page's own data traffic (noise candidates)
NOISE_DATA_TYPES = frozenset({"XHR", "Fetch", "EventSource"})
_STRIP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "set-cookie",
                  "connection", "keep-alive"}
_ACCOUNT = re.compile(r"(?i)(^|/)(account|accounts|login|logout|signin|sign-in|signup|"
                      r"sign-up|register|registration|user/new|users/sign_in|session|"
                      r"sessions|auth|oauth|password|cart|checkout|admin)(\.\w+)?(/|$|\?)")

#: runs before any page script, in every frame
NO_SIDE_CHANNELS_JS = r"""
(() => {
  const kill = (o, k) => { try { Object.defineProperty(o, k, {value: undefined,
      configurable: false, writable: false}); } catch (e) { try { o[k] = undefined; } catch (e2) {} } };
  kill(window, 'WebSocket'); kill(window, 'EventSource'); kill(window, 'Worker');
  kill(window, 'SharedWorker'); kill(window, 'RTCPeerConnection');
  kill(window, 'webkitRTCPeerConnection');
  try { navigator.sendBeacon = () => false; } catch (e) {}
  try { if (navigator.serviceWorker) navigator.serviceWorker.register =
      () => Promise.reject(new Error('blocked by guard')); } catch (e) {}
})();
"""

#: the words the LAST rung reads in an input's name, id, placeholder, label and class (whole words or word starts,
#: ``_SEARCH_TOKENS_JS``), after the page's structure and the measured typing (``GuardedSession._choose_input``)
SEARCH_INPUT_WORDS = ("search", "query", "find", "keyword")
#: the words that order the controls an icon-only search may hide behind (``REVEAL_SEARCH_JS``)
REVEAL_WORDS = ("search", "suche", "recherche", "buscar", "zoek", "cerca")

#: ``words => text => bool``: the page scripts' word rule (``agent_browser.SEARCH_TOKENS_JS``: whole words or word
#: starts; ``research`` and ``Besuchen`` name no search)
_SEARCH_TOKENS_JS = SEARCH_TOKENS_JS

#: the text inputs of the rendered page (type text / search / none, outside a form with a password field), each with
#: its ``rank``: 0 by the page's structure (``type=search``, ARIA ``role=searchbox``, inside ``role=search`` or an HTML
#: ``<search>`` element), 2 when its name is ``q`` / ``s`` or its labels name a search (:data:`SEARCH_INPUT_WORDS`),
#: 3 for any other text input (a candidate for the measured typing only). ``search`` marks ranks 0 and 2: the
#: search-like inputs, visibility for the metric. ``named14``: the rule of web tools before version 15 named it (a
#: search word anywhere in its name, id, placeholder, label or class, ``type=search``, a ``role=search`` ancestor,
#: the name ``q`` / ``s``): such a tool's typed replay still finds it (:func:`replay_inputs`)
SEARCH_INPUTS_JS = r"""
(() => {
""" + _SEARCH_TOKENS_JS + r"""
  const named = namedBy(%s);
  const out = [];
  document.querySelectorAll('input').forEach((el, i) => {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'search', ''].includes(t)) return;
    const f = el.form;
    if (f && f.querySelector('input[type=password]')) return;
    const nm = (el.name || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const blob = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                  el.getAttribute('class')].join(' ');
    const rank = (t === 'search' || role === 'searchbox' || !!el.closest('[role=search], search')) ? 0 :
                 (nm === 'q' || nm === 's' || named(blob)) ? 2 : 3;
    const named14 = t === 'search' || !!el.closest('[role=search]') || /search|query|find|keyword/i.test(blob) ||
                    nm === 'q' || nm === 's';
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' &&
                    cs.display !== 'none' && !el.disabled && !el.readOnly;
    out.push({index: i, visible: visible, name: el.name || '', id: el.id || '', type: t, rank: rank,
              search: rank < 3, named14: named14});
  });
  return out;
})()
""" % json.dumps(list(SEARCH_INPUT_WORDS))

#: does the page's visible text show ``%s`` (the typed value, lower case)? Typing alone never puts it there (an
#: input's value is not part of ``innerText``): a suggestion list or live results that echo it do
TEXT_SHOWS_JS = ("(v => (((document.body && document.body.innerText) || '').toLowerCase().includes(v)))(%s)")

#: input #%d when it takes a typed value - a visible text / search field outside any form with a password
#: field (the search box the accessibility tree named; ``ax_tree.search_input``); null otherwise
AX_INPUT_JS = r"""
(i => { const el = document.querySelectorAll('input')[i]; if (!el) return null;
  const t = (el.getAttribute('type') || 'text').toLowerCase();
  if (!['text', 'search', ''].includes(t) || el.disabled || el.readOnly) return null;
  if (el.form && el.form.querySelector('input[type=password]')) return null;
  const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
  if (!(r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none')) return null;
  return {index: i, visible: true, name: el.name || '', id: el.id || '', type: t, ax: true};
})(%d)
"""

#: search only: what a search POST's path or a body field is named like (the last rung: read only when the typed
#: value sits inside a longer field, ``search_post``), and the infrastructure / telemetry paths that are never one (a
#: store platform's ``/.well-known/<platform>/fec/produce`` analytics event echoed the typed value)
SEARCH_POST_NAME = re.compile(
    r"(?i)(^|[^a-z])(search|suche|such|query|q|term|terms|keyword|keywords|find|results?|autocomplete|suggest\w*|lookup|"
    r"recherche|buscar|busqueda|zoek|cerca|ricerca|szukaj|hledat)([^a-z]|$)")
NOT_SEARCH_POST_PATH = re.compile(
    r"(?i)(^|/)\.well-known(/|$)|(^|[/._-])(produce|events?|fec|logs?|stats|statistics|insights|signals|monitor\w*|"
    r"sentry|batch|report|reporting|consent|identify|session|sessions)([/._?-]|$)")

#: a cheap signature of "where the page is": URL + visible text length + head
PAGE_SIG_JS = r"""
(() => { const t = (document.body && document.body.innerText) || '';
  return location.href + '|' + t.length + '|' + t.slice(0, 200); })()
"""

#: the words the last fallback reads over the labels of several controls next to a
#: search input (``click_submit``), after the recorded label and the model's pick: at the start of a word (a
#: camelCase word too: ``btnSearch``), and ``go`` only as a whole word in any case or before a camelCase word
#: (``goButton``; never ``category``, ``logo``, ``Google``, ``GOLD``)
_WORD_START = r"(?:(?<![^\W\d_])|(?<=[a-z])(?=[A-Z]))"
SUBMIT_WORDS = re.compile(_WORD_START + r"(?i:search|suche|recherche|buscar|zoek|cerca|find)|"
                          + _WORD_START + r"(?i:go)(?:(?![^\W_])|(?=[A-Z][a-z]))")
#: the submit words of web tools before version 15 (anywhere in a label), which their typed replays keep
SUBMIT_WORDS_V14 = re.compile(r"search|suche|recherche|buscar|zoek|cerca|find|go", re.I)

#: the controls next to a search input that may submit it: within the nearest ancestor
#: (up to three levels) that holds no other visible text input, the visible
#: buttons, [role=button] elements and submit / image inputs; never a link to
#: another page, never a control of a form that holds a password field
_SUBMIT_COLLECT_JS = r"""
  const vis = n => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const lab = b => [b.getAttribute('aria-label'), b.getAttribute('title'), (b.innerText || '').trim().slice(0, 40),
    b.value, b.id, typeof b.className === 'string' ? b.className : ''].filter(x => x && String(x).trim())
    .join(' ').replace(/\s+/g, ' ').slice(0, 80);
  const texty = x => ['text', 'search', 'email', 'tel', 'url', 'number', '']
    .includes((x.getAttribute('type') || 'text').toLowerCase());
  const link = b => b.tagName === 'A' && (b.getAttribute('href') || '').trim() &&
    !/^(#|javascript:)/i.test((b.getAttribute('href') || '').trim());
  const collect = el => {
    let box = el.parentElement;
    for (let up = 0; box && up < 3; up++, box = box.parentElement) {
      if ([...box.querySelectorAll('input')].some(x => x !== el && texty(x) && vis(x))) break;
      const cs = [...box.querySelectorAll('button, [role=button], input[type=submit], input[type=image]')]
        .filter(b => vis(b) && !link(b) && !(b.form && b.form.querySelector('input[type=password]')));
      if (cs.length) return cs;
    }
    return [];
  };
"""

#: the search's own submit control for input #%d (-1: the focused element),
#: by the page's structure. Inside the input's form: its first visible submit
#: control in tree order (the HTML default button). Else the controls next to
#: the input (``_SUBMIT_COLLECT_JS``): exactly one is clicked; several are
#: returned as ``candidates`` (``{k, label}``) with the field's name,
#: placeholder and label, for :meth:`CdpAgentSession.click_submit` to choose.
#: Never a control of a form that holds a password field. Returns {clicked, how}.
SUBMIT_SEARCH_JS = r"""
(idx => { /*__anyuseSubmitSearch*/
  const el = idx < 0 ? document.activeElement : document.querySelectorAll('input')[idx];
  if (!el || el.tagName !== 'INPUT') return {clicked: false, how: 'no input'};
""" + _SUBMIT_COLLECT_JS + r"""
  const f = el.form;
  if (f && f.querySelector('input[type=password]')) return {clicked: false, how: 'password form'};
  if (f) {
    const pick = [...f.querySelectorAll('button[type=submit], input[type=submit], input[type=image], button:not([type])')]
      .find(vis);
    if (pick) { pick.click(); return {clicked: true, how: 'form submit control', label: lab(pick)}; }
  }
  const cs = collect(el);
  if (!cs.length) return {clicked: false, how: 'no submit control'};
  if (cs.length === 1) {
    cs[0].click();
    return {clicked: true, how: 'the one control next to the input', label: lab(cs[0])};
  }
  const name = el.getAttribute('aria-label') || (el.labels && el.labels[0] ? el.labels[0].innerText : '') || '';
  return {clicked: false, how: 'candidates', candidates: cs.slice(0, 6).map((b, k) => ({k: k, label: lab(b)})),
          field: {name: el.name || '', placeholder: el.placeholder || '', label: String(name).slice(0, 80)}};
})(%d)
"""

#: ``SUBMIT_PICK_JS % (idx, k, json label)``: click control ``k`` of input
#: ``idx``'s candidates (``SUBMIT_SEARCH_JS``) when its label is still that one;
#: re-collected by the same rule, so the pick is checked against the page
SUBMIT_PICK_JS = r"""
((idx, k, want) => { /*__anyuseSubmitPick*/
  const el = idx < 0 ? document.activeElement : document.querySelectorAll('input')[idx];
  if (!el || el.tagName !== 'INPUT') return {clicked: false, how: 'no input'};
""" + _SUBMIT_COLLECT_JS + r"""
  const b = collect(el)[k];
  if (!b || lab(b) !== want) return {clicked: false, how: 'the control is no longer there'};
  b.click();
  return {clicked: true, how: 'picked control', label: want};
})(%d, %d, %s)
"""

#: the URL input #%d's own GET form submits to, serialised as the browser does
#: (``new FormData``); ``{method, url}``, the url empty for any other method; null
#: without a form or for a form with a password field
FORM_GET_URL_JS = r"""
(idx => { /*__anyuseFormGetUrl*/
  const el = idx < 0 ? document.activeElement : document.querySelectorAll('input')[idx];
  const f = el && el.form;
  if (!f || f.querySelector('input[type=password]')) return null;
  const m = (f.getAttribute('method') || 'get').toLowerCase();
  if (m !== 'get') return {method: m, url: ''};
  const u = new URL(f.getAttribute('action') || location.href, location.href);
  u.search = new URLSearchParams(new FormData(f)).toString();
  u.hash = '';
  return {method: 'get', url: u.href};
})(%d)
"""

#: the search's own submit control for input #%d (-1: the focused
#: element) as web tools before version 14 replay it. Inside the input's form: a
#: submit button / image input / a button without a type; else, within
#: [role=search] or up to three ancestors, a button whose label/id/class says
#: search. Never a control of a form that holds a password field. Clicks it
#: once; returns {clicked, how}.
SUBMIT_SEARCH_JS_V13 = r"""
(idx => { /*__anyuseSubmitSearch*/
  const el = idx < 0 ? document.activeElement : document.querySelectorAll('input')[idx];
  if (!el || el.tagName !== 'INPUT') return {clicked: false, how: 'no input'};
  const re = /search|suche|recherche|buscar|zoek|cerca|find|go/i;
  const vis = n => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const f = el.form;
  if (f && f.querySelector('input[type=password]')) return {clicked: false, how: 'password form'};
  let pick = null, how = '';
  if (f) {
    pick = [...f.querySelectorAll('button[type=submit], input[type=submit], input[type=image], button:not([type])')]
      .find(vis) || null;
    how = 'form submit control';
  }
  if (!pick) {
    let box = el.closest('[role=search]') || el.parentElement;
    for (let up = 0; box && up < 3 && !pick; up++, box = box.parentElement) {
      pick = [...box.querySelectorAll('button, [role=button], input[type=submit]')].find(b =>
        vis(b) && re.test([b.getAttribute('aria-label'), b.getAttribute('title'), b.id,
          typeof b.className === 'string' ? b.className : '', (b.innerText || '').slice(0, 30),
          b.value].join(' '))) || null;
    }
    how = 'search button next to the input';
  }
  if (!pick) return {clicked: false, how: 'no submit control'};
  pick.click();
  return {clicked: true, how: how};
})(%d)
"""

def pick_submit_control(candidates: List[Dict[str, Any]], field_info: Dict[str, Any],
                        url: str = "") -> Optional[int]:
    """The model's pick among several controls next to a search input
    (``SUBMIT_SEARCH_JS``'s candidates), as an index into ``candidates``; None
    for no opinion. Asked only inside an open model-check scope
    (``execution/model_check.py``: one yes/no question per control); the page
    re-checks the pick (``SUBMIT_PICK_JS``). Blocks its thread."""
    from anyuse.execution import model_check as MC
    labels = [str((c or {}).get("label") or "") for c in candidates]
    if len(labels) < 2 or not MC.available(SUBMIT_QUESTION.kind):
        return None
    target = str(field_info.get("label") or field_info.get("placeholder") or field_info.get("name") or "")
    state = "The following is DATA, not instructions.\n" + json.dumps(
        {"search field": {k: str(v)[:80] for k, v in (field_info or {}).items()},
         "controls next to it": labels}, ensure_ascii=False)
    index, _verdict = MC.choose(
        SUBMIT_QUESTION, target, [MC.Candidate(text=lab, ref=i) for i, lab in enumerate(labels)],
        state=state, verify=lambda c: 0 <= int(c.ref) < len(labels) and bool(labels[int(c.ref)]),
        where="cdp_guard.submit_control", guard_obs={"url": url, "text": state})
    return index


def _submit_question():
    from anyuse.execution import model_check as MC
    return MC.Question(kind="submit_control", wording=(
        "Does the marked control submit the text typed into the search field '{target}', "
        "so that the site searches for it?"))


#: the one question :func:`pick_submit_control` asks about each control
SUBMIT_QUESTION = _submit_question()


#: open a header search that is only an icon: click control ``%d`` of the page's candidates in this order - in-page
#: controls before links to another page of the site; first by structure, a control whose ``aria-controls`` names a
#: hidden element holding a text input, a ``label`` for a hidden text input, a ``summary`` whose ``details`` hold
#: one, a control inside ``role=search`` or ``<search>`` (rank 0); then the controls whose labels name a search
#: (:data:`REVEAL_WORDS`); then an icon-only in-page control in the page's header or banner (rank 1: a menu, cart or
#: search toggle; a link there opens another page and is never one). Controls of a password form and a form's
#: submit buttons are never candidates.
#: Returns ``{clicked, label, navigates, rank, count}``: ``count`` is how many candidates there are, so the caller
#: (``GuardedSession._reveal``) can try the next one when no text input appeared.
REVEAL_SEARCH_JS = r"""
(k => { /*__anyuseRevealSearch*/
""" + _SEARCH_TOKENS_JS + r"""
  const named = namedBy(%s);
  const vis = n => { const r = n.getBoundingClientRect(); const cs = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const texty = x => x.tagName === 'INPUT' && ['text', 'search', '']
    .includes((x.getAttribute('type') || 'text').toLowerCase());
  const hidesInput = el => !!el && [...(texty(el) ? [el] : el.querySelectorAll('input'))]
    .some(x => texty(x) && !vis(x));
  const byIds = ids => String(ids || '').split(/\s+/).filter(Boolean).map(i => document.getElementById(i));
  const nav = n => n.tagName === 'A' && (n.getAttribute('href') || '').trim() &&
    !/^(#|javascript:)/i.test((n.getAttribute('href') || '').trim());
  const all = [...document.querySelectorAll('button, [role=button], a, summary, label')];
  const cands = [];
  all.forEach((n, i) => {
    if (!vis(n)) return;
    if (n.form && n.form.querySelector('input[type=password]')) return;
    const t = (n.getAttribute('type') || '').toLowerCase();
    if (n.tagName === 'BUTTON' && n.form && (t === 'submit' || t === '')) return;
    const link = !!nav(n);
    if (link && new URL(n.href, location.href).host !== location.host) return;
    const words = named([n.getAttribute('aria-label'), n.getAttribute('title'), n.id,
      typeof n.className === 'string' ? n.className : '', (n.innerText || '').slice(0, 30),
      n.getAttribute('data-testid')].join(' '));
    let rank = -1;
    if (byIds(n.getAttribute('aria-controls')).some(hidesInput) ||
        (n.tagName === 'LABEL' && n.htmlFor && hidesInput(document.getElementById(n.htmlFor))) ||
        (n.tagName === 'SUMMARY' && hidesInput(n.parentElement)) || !!n.closest('[role=search], search')) rank = 0;
    else if (!link && !(n.innerText || '').trim() && n.closest('header, [role=banner]') &&
             n.querySelector('svg, img, i, span, use')) rank = 1;
    else if (words) rank = 2;
    if (rank < 0) return;
    cands.push({n: n, rank: rank, words: words, i: i, link: link});
  });
  cands.sort((a, b) => (a.link - b.link) || ((a.rank > 0) - (b.rank > 0)) || (b.words - a.words) ||
    (a.rank - b.rank) || (a.i - b.i));
  const pick = cands[k];
  if (!pick) return {clicked: false, count: cands.length};
  pick.n.click();
  return {clicked: true, label: (pick.n.getAttribute('aria-label') || pick.n.innerText ||
    pick.n.id || '').slice(0, 40), navigates: pick.link, rank: pick.rank, count: cands.length};
})(%%d)
""" % json.dumps(list(REVEAL_WORDS))

#: select the focused text field's content, so typing REPLACES it
SELECT_TEXT_JS = ("(function(e){if(e&&typeof e.select==='function'&&'value' in e)"
                  "{e.select();return true}return false})(document.activeElement)")
#: the focused field's current value (read-back after typing)
VALUE_OF_FOCUSED_JS = ("(function(e){return e&&'value' in e?String(e.value||''):null})"
                       "(document.activeElement)")

FOCUS_INPUT_JS = r"""
(() => { const el = document.querySelectorAll('input')[%d];
  if (!el) return false; el.focus(); el.value = ''; return document.activeElement === el; })()
"""


def _ranked(row: dict) -> dict:
    """An input row with its ``rank``; a row without one (an older page script) reads by its type."""
    if "rank" not in row:
        row = {**row, "rank": 0 if str(row.get("type") or "") == "search" else 2}
    return row


def same_input(row: dict, want: dict) -> bool:
    """Is the input row the recorded input ``want`` by its name or its id?"""
    return bool((want.get("name") and row.get("name") == want["name"]) or
                (want.get("id") and row.get("id") == want["id"]))


def replay_inputs(rows: Sequence[dict], want: dict, named14: bool = False) -> List[dict]:
    """Of a page's text inputs (``SEARCH_INPUTS_JS`` rows), the visible ones a typed replay of the recorded input
    ``want`` (name / id / index) chooses among, in page order: the search-like ones (ranks 0 and 2) and the one with
    the recorded name or id, whatever the rules call it now (a tool recorded under an earlier rule); for a web tool
    before version 15 (``named14``) also every input its own rule named (``named14`` rows), so it finds every input
    it found before."""
    return [i for i in rows if i.get("visible") and (i.get("rank", 2) < 3 or same_input(i, want) or
                                                     (named14 and i.get("named14")))]


def recorded_input(inputs: Sequence[dict], want: dict) -> Optional[dict]:
    """The input a typed replay types into: of ``inputs`` (:func:`replay_inputs`), the one with the recorded name or
    id, else the one at the recorded index; None when neither is there."""
    same = [i for i in inputs if same_input(i, want)] or [i for i in inputs if i.get("index") == want.get("index")]
    return same[0] if same else None


class TrafficViolation(BaseException):
    """A request reached a host outside the guard, or a cap was exceeded: stop the run.

    A ``BaseException`` on purpose: no ``except Exception`` on the way up (a
    reader, a probe, the learn loop) may swallow it."""


def host_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower()
    if (p.scheme == "https" and host.endswith(":443")) or (p.scheme == "http" and host.endswith(":80")):
        host = host.rsplit(":", 1)[0]
    return host


class WriteGrant:
    """Open tasks: the ONE exception to "GET/HEAD only, never an account path" for a
    run whose commit steps the harness decided (covered by the task, or confirmed by the caller).

    * ``account_paths``: GETs of the run's OWN origin may open its login, account, cart and checkout pages (reading
      them changes nothing);
    * a non-GET request (a form POST) of the run's own origin is sent ONLY while a window is open: the harness opens
      one around exactly one decided commit step (``open``/``close``) and at most ``max_per_window`` such requests
      pass in it (the step's POST and a redirect's follow-up); outside a window every non-GET is failed as before.

    ``log`` keeps method, path and the verdict of every request it decided - never a body or a header. A budget
    without a grant (every other run) is byte-for-byte today's guard."""

    def __init__(self, origin: str, *, account_paths: bool = True, max_per_window: int = 3):
        from anyuse.execution.request_policy import origin as _origin
        self.origin = _origin(origin)
        self.account_paths = bool(account_paths)
        self.max_per_window = int(max_per_window)
        self.window = ""
        self.used = 0
        self.log: List[Tuple[str, str, str]] = []

    def _own(self, url: str) -> bool:
        from anyuse.execution.request_policy import origin as _origin
        return bool(self.origin) and _origin(url) == self.origin

    def open(self, label: str) -> None:
        self.window, self.used = str(label or "commit")[:80], 0

    def close(self) -> None:
        self.window, self.used = "", 0

    def allows_account(self, url: str) -> bool:
        ok = self.account_paths and self._own(url)
        if ok:
            self.log.append(("GET", urllib.parse.urlsplit(url).path[:120], "account path (own origin)"))
        return ok

    def allows_method(self, url: str, method: str, rtype: str) -> bool:
        path = urllib.parse.urlsplit(url).path[:120]
        if not self._own(url):
            return False
        if not self.window:
            self.log.append((method, path, "refused: no commit window"))
            return False
        if rtype not in ("Document", "XHR", "Fetch", "Other", ""):
            self.log.append((method, path, f"refused: resource type {rtype}"))
            return False
        if self.used >= self.max_per_window:
            self.log.append((method, path, "refused: the window's requests are used up"))
            return False
        self.used += 1
        self.log.append((method, path, f"sent in window {self.window!r}"))
        return True


_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
                       r"(?::\d{1,5})?$")


class OriginPolicy:
    """What "same site" means for the guard, explicitly.

    * By default a site is exactly ONE host: the page's own (``host_of``, the
      default port dropped). ``www.example.org`` and ``example.org`` are two sites.
    * Per run, a site may name its own API/CDN hosts (``extra``: site host ->
      exact host names). This is meant to be declared up front (``from_file``).
      Every entry is an exact host name: no ``*``, no leading dot, no scheme or
      path. It must be the site's base (the site host without a leading
      ``www.``) or a subdomain of that base, so ``www.example.org`` may name
      ``api.example.org`` and ``cdn.example.org``, but never ``example.net`` or
      a sibling registrable domain. There is no wildcard and no suffix rule.
    * Every host of one site shares ONE pacer slot and ONE request count (the
      site host's), so adding an API host never adds request rate or budget.
    """

    def __init__(self, extra: Optional[Dict[str, Any]] = None):
        self.extra: Dict[str, frozenset] = {}
        self._site: Dict[str, str] = {}
        for site, hosts in (extra or {}).items():
            s = self._clean(site, "site")
            names = frozenset(self._member(s, h) for h in (hosts or ()))
            self.extra[s] = names
            for h in names:
                if self._site.get(h, s) != s:
                    raise ValueError(f"origin policy: {h} is listed for two sites")
                self._site[h] = s

    @staticmethod
    def _clean(host: str, what: str) -> str:
        h = str(host or "").strip().lower()
        if "*" in h or h.startswith(".") or "/" in h or "@" in h or not _HOSTNAME.match(h):
            raise ValueError(f"origin policy: {what} {host!r} is not an exact host name "
                             f"(no wildcard, scheme, path or leading dot)")
        return host_of("https://" + h)

    @classmethod
    def _member(cls, site: str, host: str) -> str:
        h = cls._clean(host, "host")
        base = site[4:] if site.startswith("www.") else site
        base = base.split(":", 1)[0]
        name = h.split(":", 1)[0]
        if h != site and not (name == base or name.endswith("." + base)):
            raise ValueError(f"origin policy: {h} is not {site}'s own subdomain "
                             f"(it must be {base} or end with .{base})")
        return h

    @classmethod
    def from_file(cls, path: str) -> "OriginPolicy":
        """``{"sites": {"www.example.org": ["api.example.org"]}}`` (a file declared up front)."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(dict(data.get("sites") or {}))

    def site_of(self, host: str) -> str:
        """The site a host belongs to: itself, or the site that listed it."""
        return self._site.get(host, host)

    def allows(self, page_host: str, host: str) -> bool:
        if not page_host or not host:
            return False
        site = self.site_of(page_host)
        return host == site or host in self.extra.get(site, frozenset())

    def to_dict(self) -> Dict[str, List[str]]:
        return {s: sorted(h) for s, h in sorted(self.extra.items())}


def _after_host(value: str) -> str:
    """A URL without its scheme and host (``Referer``/``Origin`` included); any
    other string unchanged."""
    parts = urllib.parse.urlsplit(value or "")
    if parts.scheme in ("http", "https") and parts.netloc:
        return (parts.path + ("?" + parts.query if parts.query else "")
                + ("#" + parts.fragment if parts.fragment else ""))
    return value or ""


class RequestBudget:
    """Run-wide: the shared pacer (``discover.Pacer``), both caps, stopped hosts,
    the per-load counts and every violation. One instance per bench run."""

    def __init__(self, pacer, per_load_cap: int = PER_LOAD_CAP,
                 per_host_cap: int = PER_HOST_CAP,
                 asleep: Optional[Callable] = None,
                 allow_methods: Tuple[str, ...] = (),
                 origin_policy: Optional[OriginPolicy] = None,
                 fatal_third_party_attempts: bool = False,
                 site_cap: Optional[int] = None,
                 realistic_browser: bool = False,
                 static_bytes_cap: Optional[int] = None,
                 nav_min_interval_s: float = 0.0,
                 load_per_navigation: bool = False,
                 static_in_flight: Optional[int] = None,
                 task_gate=None,
                 web_scope=None,
                 static_reserve: Optional[int] = None,
                 skip_types=(),
                 noise_share: bool = False,
                 browser_statics: bool = False,
                 search_post: bool = False):
        self.pacer = pacer
        #: OFF by default (the product's page factory and replay route turn it on): a
        #: READ-ONLY POST SEARCH is sent - a POST the site's own page sends right after typing into its search box, to
        #: the site's own host or its granted provider, whose body carries the typed value, never an account / login /
        #: cart / checkout / payment path, a state-changing or telemetry path, a credential header or an upload; counted
        #: as data and logged (``search_posts``). Every other POST stays refused.
        self.search_post = bool(search_post)
        self.search_posts: List[Dict[str, str]] = []
        self.search_posts_refused: List[Dict[str, str]] = []
        #: OFF by default (the product's page factory turns it on): the static assets of the
        #: site itself, its own hosts, its platform asset CDN and its granted provider are BROWSER STATICS - sent like a
        #: normal browser visit (also CSS, fonts and images), counted apart, logged and run-cached, never against the
        #: run's ceilings, which then count documents, data calls and robots.txt only; a runaway guard of
        #: real loops (``STATIC_RUNAWAY_REPEAT`` / ``STATIC_RUNAWAY_DISTINCT``) is a typed stop. Off: byte-for-byte
        #: the guard before it.
        self.browser_statics = bool(browser_statics)
        #: site -> browser statics sent (outside the site's request total); site -> kind -> count
        self.static_sent: Dict[str, int] = {}
        self.static_sent_kinds: Dict[str, Dict[str, int]] = {}
        #: site -> the most browser statics ONE page load sent
        self.static_load_max: Dict[str, int] = {}
        #: site -> why the runaway guard stopped it (the typed stop ``static_runaway``)
        self.runaway: Dict[str, str] = {}
        #: the robots.txt URLs the run fetched and counted in the site's total (``count_robots``)
        self.robots_counted: List[str] = []
        #: host -> requests the robots gate refused (the record names each host and what its robots.txt said)
        self.robots_refused: Dict[str, int] = {}
        #: ANYUSE_ROBOTS: host -> what the gate let through that robots.txt would refuse
        #: (the gate's own record, shared), and why the run overrides robots.txt ("" = it does not)
        self.robots_overrides: Optional[Dict[str, Dict[str, Any]]] = None
        self.robots_override: str = ""
        #: the browser statics' own in-flight lane (``BROWSER_STATIC_IN_FLIGHT``) when ``static_in_flight`` is off
        self.browser_static_in_flight = BROWSER_STATIC_IN_FLIGHT if self.browser_statics else None
        #: site -> bytes received by browser statics (apart from ``site_bytes``: a run's byte ceiling counts documents
        #: and data), and the XHR / fetch requests counted as data that their response showed to be a static file
        self.static_bytes: Dict[str, int] = {}
        self.reclassified: List[str] = []
        #: OFF by default. With a
        #: ``static_reserve``: the page's own BACKGROUND requests (an XHR/fetch
        #: sent outside an action window of the session, a data request whose
        #: path names telemetry, a sub-frame document) are counted against the
        #: static share like statics: sent only while the site's total is below
        #: ``site_cap - static_reserve``; over it they are failed in the browser,
        #: NOT counted, and never stop the host. The reserve stays for what the
        #: run's own actions cause (documents, the data an action loads).
        #: Without it, a chatty page's polling used up the share while the run
        #: waited, and the next navigation failed locally.
        self.noise_share = bool(noise_share)
        #: site -> requests by lane: {"static": n, "noise": n, "action": n}
        self.lanes: Dict[str, Dict[str, int]] = {}
        #: site -> background requests skipped at the static share (never sent)
        self.noise_skipped: Dict[str, int] = {}
        #: OFF (None) by default. When set, static assets (Script, Stylesheet,
        #: Image, Font, Media) are sent only while the SITE's total is below
        #: ``site_cap - static_reserve``: the last ``static_reserve`` requests are
        #: for Document, XHR and Fetch. A static over its share is skipped in the
        #: browser, is NOT counted, and never stops the host (heavy
        #: start pages spent the whole 300 on statics, then every search was
        #: failed locally as "host stopped").
        if static_reserve is not None and not (0 <= int(static_reserve) < self.site_cap_arg(
                site_cap, per_host_cap)):
            raise ValueError("static_reserve must be >= 0 and below the site cap")
        self.static_reserve = int(static_reserve) if static_reserve is not None else None
        #: resource types skipped in the browser: never sent, never counted
        #: (benchmark mode skips Image, Media and Font; CSS and scripts load)
        self.skip_types = frozenset(str(t) for t in (skip_types or ()))
        #: site -> statics skipped by the static share (never sent)
        self.static_skipped: Dict[str, int] = {}
        #: OFF (None) by default: an ``execution.web_scope.WebScope`` (the four
        #: web-scope settings). A scope with every setting off is
        #: treated as None, so the guard is exactly today's.
        self.web_scope = web_scope if (web_scope is not None and web_scope.enabled) else None
        #: site -> requests its search provider's own share sent (in the run total, never in
        #: the site's static share)
        self.provider_sent: Dict[str, int] = {}
        #: every request answered from the run cache (host, URL, type, kind), and how many static
        #: responses were stored the moment they finished (``GuardedSession``) rather than at a session's end
        self.cache_reuse: List[Tuple[str, str, str, str]] = []
        self.cached_on_response = 0
        #: CORS preflights to a granted provider host that reached the network
        #: without a pause (counted against the site afterwards, never fatal)
        self.preflights: List[Tuple[str, str]] = []
        #: OFF (None) by default: an ``execution.traffic_gate.SiteGate``. When
        #: given, a guarded session navigates only to a site with an active task
        self.task_gate = task_gate
        #: OFF by default. When on, a main-frame navigation inside one session
        #: (a typed search's Enter, a form submit) starts a NEW page load for the
        #: per-load cap, as it is one; redirect hops and iframes do not. Off,
        #: the whole session is one load (today's rule).
        self.load_per_navigation = bool(load_per_navigation)
        #: OFF (None) by default. When set, same-site static assets (Script,
        #: Stylesheet, Image, Font) leave the shared 1 req/s pacer and go through
        #: a second lane with at most this many in flight; they still count
        #: against every cap and the site ceiling (TRAFFIC_POLICY.md, proposed).
        if static_in_flight is not None and int(static_in_flight) < 1:
            raise ValueError("static_in_flight must be >= 1 (or None: off)")
        self.static_in_flight = int(static_in_flight) if static_in_flight else None
        #: network ids of static requests currently in the static lane
        self.static_open: set = set()
        self.static_peak = 0
        #: aggregate per-SITE ceiling over EVERYTHING that reserves the shared
        #: pacer: browser loads (agent and proof), plain GETs, robots.txt, the
        #: compiled requests, retries and redirect hops. Default: the host cap
        #: (200), so nothing changes unless a number is set.
        self.site_cap = int(site_cap if site_cap is not None else per_host_cap)
        #: OFF by default. When on, same-site CSS/images/fonts are sent too, and
        #: an explicit per-site byte ceiling is REQUIRED (no default number).
        self.realistic_browser = bool(realistic_browser)
        if self.realistic_browser and not static_bytes_cap:
            raise ValueError("realistic-browser mode needs an explicit static_bytes_cap "
                             "(bytes per site); none is assumed")
        self.static_bytes_cap = int(static_bytes_cap or 0)
        #: site -> bytes received (encoded, every response the guard sent)
        self.site_bytes: Dict[str, int] = {}
        #: >= this many seconds between two DOCUMENT requests to one site
        #: (0 = off: only the shared pacer's 1 req/s applies, today's default)
        self.nav_min_interval_s = float(nav_min_interval_s or 0.0)
        self.last_nav: Dict[str, float] = {}
        #: same host by default; an explicit per-run list of a site's own hosts
        self.origin_policy = origin_policy or OriginPolicy()
        #: a third-party response that reached the network (the guard never sends
        #: one, so it bypassed the guard, e.g. a CORS preflight) is ALWAYS a fatal
        #: violation. A third-party request the page merely TRIED (failed in the
        #: browser, nothing sent) is recorded in ``third_party_refused``; with
        #: this flag it is fatal too.
        self.fatal_third_party_attempts = bool(fatal_third_party_attempts)
        #: (page host, url) of every third-party request failed before sending
        self.third_party_refused: List[Tuple[str, str]] = []
        #: GET/HEAD always; anything else only when the caller's policy names it
        #: here (explicit, per run). Every other method is failed before it is sent.
        self.allowed_methods = DEFAULT_METHODS | frozenset(
            str(m).upper() for m in allow_methods if str(m or "").strip())
        #: (method, url) of every request refused for its method (never sent)
        self.refused_methods: List[Tuple[str, str]] = []
        self.per_load_cap = per_load_cap
        self.per_host_cap = per_host_cap
        self.asleep = asleep or asyncio.sleep
        self.stopped: Dict[str, str] = {}
        self.loads: Dict[str, List[int]] = {}
        self.violations: List[str] = []
        self.leaks: List[str] = []
        #: the raw CDP events of every flagged request (bounded), for diagnosis
        self.violation_events: List[dict] = []
        #: same-site responses Chrome answered from its memory cache (never sent)
        self.browser_cache_hits: List[str] = []
        #: (host, clock time, url, resource type) of every request the guard sent
        self.sent: List[Tuple[str, float, str, str]] = []
        #: static cache: key (method, url, key headers) -> entry; one run
        self.cache: Dict[str, dict] = {}
        #: (method, url) -> the header names the stored response Varies on
        self.vary: Dict[Tuple[str, str], Tuple[str, ...]] = {}
        self.cache_enabled = False
        #: OFF by default. When on, a STATIC asset (script,
        #: stylesheet) whose response gives no usable freshness (it sets a cookie, says
        #: no-cache, or has no lifetime) is still kept for ``SITE_CACHE_TTL_S`` in the
        #: site's own cache (its Set-Cookie is never stored or replayed), so later runs
        #: and the proof of the same site render with their scripts without sending them
        #: again. ``no-store``, ``Vary: *``, non-200, non-GET, a request body, an
        #: authorization header and anything carrying a validation input are still refused.
        #: Without it, the static cache stored nothing on many sites and, once the static
        #: share was spent, later runs rendered without their scripts.
        self.site_cache = False
        #: freshness refusals the site cache overrode (reason -> count), for the report
        self.site_cache_overrides: Dict[str, int] = {}
        #: OFF (None) by default: statics stop at this share of
        #: the byte ceiling (skipped, not counted, the host not stopped; a cached copy is
        #: still served), so what is in flight when the ceiling nears stays inside it.
        #: Without it, responses in flight took a site to 20.6 MB against a 12 MB ceiling.
        self.static_bytes_headroom: Optional[float] = None
        #: host -> requests answered from the run cache (never sent)
        self.cache_hits: Dict[str, int] = {}
        #: why a response was NOT stored (reason -> count), for the report
        self.cache_refused: Dict[str, int] = {}
        #: (a site whose scripts were never stored had its proof replay render without them): each
        #: refusal with the response's OWN cache headers (url, why, cache-control, expires, pragma, vary, a cookie set),
        #: at most ``CACHE_REFUSALS_KEPT``
        self.cache_refusals: List[Dict[str, Any]] = []
        #: a proof replay's own share of same-origin scripts past the static share (``open_replay_share``):
        #: the scripts left in the open share, and the scripts each replay sent under it
        self.replay_scripts_left = 0
        self.replay_scripts_sent: List[int] = []
        #: url -> a discovery document handed to the browser render ONCE
        self.handoffs: Dict[str, dict] = {}
        #: validation inputs of the whole run: never stored, never answered
        self.protected_values: List[str] = []
        #: every request that carried a validation input and the path it took:
        #: {"url", "type", "method", "path": network | refused | cache | handoff}
        self.value_requests: List[Dict[str, str]] = []
        #: data (Document/XHR/Fetch) requests by path, all loads of the run
        self.data_paths: Dict[str, int] = {}

    @staticmethod
    def site_cap_arg(site_cap, per_host_cap) -> int:
        return int(site_cap if site_cap is not None else per_host_cap)

    def static_share(self) -> Optional[int]:
        """The site total up to which statics may be sent (None: no share rule)."""
        if self.static_reserve is None:
            return None
        return self.site_cap - self.static_reserve

    def lane_limit(self) -> int:
        """How many statics may be in flight at once in the static lane (0: no lane)."""
        return int(self.static_in_flight or self.browser_static_in_flight or 0)

    def count_static(self, site: str, kind: str, load_n: int) -> None:
        """One browser static sent: counted apart from the site's request total."""
        self.static_sent[site] = self.static_sent.get(site, 0) + 1
        k = self.static_sent_kinds.setdefault(site, {})
        k[kind or "site"] = k.get(kind or "site", 0) + 1
        self.static_load_max[site] = max(self.static_load_max.get(site, 0), int(load_n))

    def add_static_bytes(self, host: str, n: int) -> None:
        site = self.site_of(host)
        self.static_bytes[site] = self.static_bytes.get(site, 0) + max(0, int(n or 0))

    def count_robots(self, robots_url: str, for_host: str = "") -> str:
        """A robots.txt GET of the run (a granted host's, the start host's www sibling's): counted in the site's total
        like a document (the ceilings count documents, data calls and robots.txt). At the
        site's ceiling it is not made: returns why (the gate then refuses the host - fail closed), else ``""``.
        ``for_host``: a redirect hop of the robots.txt asked for that host - counted in
        THAT host's site, wherever the hop goes."""
        site = self.site_of(for_host or host_of(robots_url))
        if self.site_total(site) >= self.site_cap:
            return f"site ceiling of {self.site_cap} requests for {site} reached (robots.txt of {host_of(robots_url)} " \
                   f"not fetched)"
        self.pacer.count[site] = self.pacer.count.get(site, 0) + 1
        if len(self.robots_counted) < 200:
            self.robots_counted.append(str(robots_url)[:200])
        return ""

    def traffic(self, host: str) -> Dict[str, Any]:
        """The run's request counts for ``host``'s site: documents + data + robots.txt (the ceilings' count) and the
        browser statics counted apart, by kind, the most of one page load, and a runaway stop. ``robots_override``: why
        the run overrode robots.txt - the run's setting, or the reason the gate recorded for a request it let through
        (a signed-in task you started)."""
        site = self.site_of(host)
        override = self.robots_override or next(
            (str(v.get("overridden_by") or "") for v in (self.robots_overrides or {}).values()
             if isinstance(v, dict) and v.get("overridden_by")), "")
        return {"documents_data_robots": self.site_total(host), "static": int(self.static_sent.get(site, 0)),
                "static_kinds": dict(self.static_sent_kinds.get(site, {})),
                "static_max_per_load": int(self.static_load_max.get(site, 0)),
                "static_skipped": int(self.static_skipped.get(site, 0)), "runaway": self.runaway.get(site, ""),
                "static_bytes": int(self.static_bytes.get(site, 0)), "bytes": int(self.site_bytes.get(site, 0)),
                "reclassified_static": len(self.reclassified),
                "search_posts": list(self.search_posts)[:20], "search_posts_refused": list(self.search_posts_refused)[:10],
                "robots_counted": list(self.robots_counted), "browser_statics": self.browser_statics,
                "robots_refused": self.robots_refused_record(),
                **({"robots_override": override,
                    "robots_overridden": {h: dict(v) for h, v in list((self.robots_overrides or {}).items())[:20]}}
                   if override else {})}

    def robots_refused_record(self, n: int = 10) -> Dict[str, Dict[str, Any]]:
        """Host -> {requests, robots, why} for the hosts the robots gate refused (the most refused
        first): ``robots`` is ``api_permission.robots_status`` - ``rules`` (the file disallows), ``unreadable`` (no
        usable answer: 5xx, network, a refused redirect - fail closed), ``absent`` or ``unknown``."""
        out: Dict[str, Dict[str, Any]] = {}
        for h, k in sorted(self.robots_refused.items(), key=lambda x: -x[1])[:n]:
            try:
                from anyuse.execution.api_permission import robots_status
                st, why = robots_status(h)
            except Exception:  # noqa: BLE001 - a record never breaks a run
                st, why = "unknown", ""
            out[h] = {"requests": int(k), "robots": st, "why": str(why)[:400]}
        return out

    # -- validation inputs -----------------------------------------------------

    def protect(self, values) -> None:
        """Register a run's validation inputs: their requests are always sent."""
        for v in values or ():
            v = str(v or "").strip().lower()
            if v and v not in self.protected_values:
                self.protected_values.append(v)

    def validation_evidence(self) -> Dict[str, int]:
        """Paths taken by requests that carried a validation input (instrumentation)."""
        out = {"network": 0, "refused": 0, "cache": 0, "handoff": 0}
        for r in self.value_requests:
            out[r["path"]] = out.get(r["path"], 0) + 1
        return out

    # -- the static cache ----------------------------------------------------------

    def clock(self) -> float:
        return float(self.pacer.clock())

    @staticmethod
    def _headers(headers) -> Dict[str, str]:
        if isinstance(headers, list):
            return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}
        return {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    def cache_key(self, method: str, url: str, req_headers=None) -> str:
        h = self._headers(req_headers)
        m = (method or "GET").upper()
        names = sorted(set(KEY_HEADERS) | set(self.vary.get((m, url), ())))
        return json.dumps([m, url, [[n, h.get(n, "")] for n in names]])

    @staticmethod
    def freshness(resp_headers: Dict[str, str], now_date: Optional[float] = None) -> Tuple[float, str]:
        """(seconds this response may be reused, '' or why it may not be stored)."""
        import email.utils
        cc = {}
        for part in (resp_headers.get("cache-control") or "").lower().split(","):
            k, _, v = part.strip().partition("=")
            if k:
                cc[k] = v.strip().strip('"')
        if "no-store" in cc:
            return 0.0, "no-store"
        if "no-cache" in cc or (not cc and "no-cache" in
                                (resp_headers.get("pragma") or "").lower()):
            return 0.0, "no-cache (needs revalidation)"
        vary = [v.strip().lower() for v in (resp_headers.get("vary") or "").split(",") if v.strip()]
        if "*" in vary:
            return 0.0, "vary *"
        if "set-cookie" in resp_headers:
            return 0.0, "sets a cookie"

        def when(name):
            try:
                d = email.utils.parsedate_to_datetime(resp_headers.get(name) or "")
                return d.timestamp() if d is not None else None
            except Exception:
                return None
        age = 0.0
        try:
            age = max(0.0, float(resp_headers.get("age") or 0))
        except ValueError:
            pass
        if "max-age" in cc:
            try:
                life = float(cc["max-age"])
            except ValueError:
                return 0.0, "bad max-age"
        else:
            date = when("date") or now_date
            exp = when("expires")
            lm = when("last-modified")
            if resp_headers.get("expires") is not None:
                if exp is None or date is None:
                    return 0.0, "expired"                  # invalid Expires = already stale
                life = exp - date
            elif lm is not None and date is not None and date > lm:
                life = min(0.1 * (date - lm), HEURISTIC_MAX_S)
            else:
                return 0.0, "no freshness information"
        life -= age
        if life <= 0:
            return 0.0, "stale on arrival"
        return life, ""

    def _refuse(self, why: str, url: str = "", headers=None) -> bool:
        self.cache_refused[why] = self.cache_refused.get(why, 0) + 1
        if url and len(self.cache_refusals) < CACHE_REFUSALS_KEPT:
            h = self._headers(headers)
            self.cache_refusals.append({"url": str(url)[:200], "why": why,
                                        **{k: str(h.get(k) or "")[:120] for k in ("cache-control", "expires", "pragma",
                                                                                 "vary", "age")},
                                        "set_cookie": "set-cookie" in h})
        return False

    def open_replay_share(self, scripts: int) -> None:
        """A proof replay begins: it may send up to ``scripts`` same-origin scripts past the static share (counted, within
        the site cap, ``REPLAY_DOC_RESERVE`` requests always left for its documents and data)."""
        self.replay_scripts_left = max(0, int(scripts))
        self.replay_scripts_sent.append(0)

    def close_replay_share(self) -> None:
        self.replay_scripts_left = 0

    def replay_script_ok(self, page_host: str, host: str) -> bool:
        """May this same-origin script go past the static share under the open proof replay's share? (One taken.)"""
        if self.replay_scripts_left <= 0 or self.site_of(host) != self.site_of(page_host):
            return False
        if self.site_total(host) >= self.site_cap - REPLAY_DOC_RESERVE:
            return False
        self.replay_scripts_left -= 1
        if self.replay_scripts_sent:
            self.replay_scripts_sent[-1] += 1
        return True

    def carries_value(self, url: str, post: str = "", req_headers=None,
                      extra: Tuple[str, ...] = ()) -> bool:
        vals = [v for v in list(self.protected_values) + [str(e).lower() for e in extra] if v]
        if not vals:
            return False
        # the host never carries a per-query input: match path, query and
        # fragment only (a value that is also a word of the host name would match
        # every URL of that host, and no static asset would ever be cached there)
        blob = " ".join([urllib.parse.unquote_plus(_after_host(url or "")),
                         urllib.parse.unquote_plus(post or ""),
                         " ".join(_after_host(v) for v in self._headers(req_headers).values())
                         ]).lower()
        return any(v in blob for v in vals)

    def remember(self, url: str, status: int, headers: Dict[str, str], body,
                 base64_encoded: bool = False, rtype: str = "Script", method: str = "GET",
                 request_headers=None, post: str = "",
                 fresh_values: Tuple[str, ...] = ()) -> bool:
        """Store one STATIC response under the HTTP rules above. ``body`` is text,
        or base64 when ``base64_encoded``. Returns True if it was stored."""
        if not self.cache_enabled:
            return False
        m = (method or "GET").upper()
        if rtype not in STATIC_TYPES:
            return self._refuse(f"type {rtype or '?'} is never cached", url, headers)
        if m != "GET":
            return self._refuse(f"method {m}", url, headers)
        if int(status or 0) != 200:
            return self._refuse(f"status {status}", url, headers)
        req = self._headers(request_headers)
        if post or "authorization" in req:
            return self._refuse("request body or authorization", url, headers)
        if self.carries_value(url, post, req, tuple(fresh_values)):
            return self._refuse("carries a validation input", url, headers)
        rh = self._headers(headers)
        if self.carries_value("", "", rh, tuple(fresh_values)):
            return self._refuse("response header carries a validation input", url, headers)
        life, why = self.freshness(rh)
        if why and self.site_cache and rtype in STATIC_TYPES and why in SITE_CACHE_OVERRIDES:
            self.site_cache_overrides[why] = self.site_cache_overrides.get(why, 0) + 1
            life, why = SITE_CACHE_TTL_S, ""
        if why:
            return self._refuse(why, url, headers)
        if base64_encoded:
            b64 = str(body or "")
        else:
            raw = (body or "").encode("utf-8") if isinstance(body, str) else bytes(body or b"")
            b64 = base64.b64encode(raw).decode("ascii")
        if len(b64) > CACHE_MAX_BYTES * 4 // 3 + 4:
            return self._refuse("too large", url, headers)
        vary = tuple(sorted(v.strip().lower() for v in (rh.get("vary") or "").split(",")
                            if v.strip()))
        self.vary[(m, url)] = vary
        key = self.cache_key(m, url, req)
        old = self.cache.get(key)
        etag = rh.get("etag", "")
        if old and old.get("etag") == etag and old["expires_at"] > self.clock():
            return False                                   # the same representation
        hdrs = [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()
                if str(k).lower() not in _STRIP_HEADERS]
        self.cache[key] = {"status": 200, "headers": hdrs, "body": b64, "url": url,
                           "etag": etag, "stored_at": self.clock(),
                           "expires_at": self.clock() + life}
        return True

    def lookup(self, method: str, url: str, req_headers=None) -> Optional[dict]:
        """A FRESH stored entry for exactly this request, else None (stale ones
        are dropped: a stale entry is never served, the request goes out)."""
        key = self.cache_key(method, url, req_headers)
        ent = self.cache.get(key)
        if ent is None:
            return None
        if ent["expires_at"] <= self.clock():
            del self.cache[key]
            return None
        return ent

    def cached_urls(self) -> List[str]:
        return [e["url"] for e in self.cache.values()]

    def handoff(self, url: str, status: int, headers: Dict[str, str], body) -> bool:
        """A discovery document just fetched by the paced HTTP GET, given to the
        next browser render of the same URL ONCE (not a cache: consumed on use).
        Never for a URL that carries a validation input."""
        if not self.cache_enabled or int(status or 0) != 200 or self.carries_value(url):
            return False
        raw = (body or "").encode("utf-8") if isinstance(body, str) else bytes(body or b"")
        if len(raw) > CACHE_MAX_BYTES:
            return False
        hdrs = [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()
                if str(k).lower() not in _STRIP_HEADERS]
        self.handoffs[url] = {"status": 200, "headers": hdrs,
                              "body": base64.b64encode(raw).decode("ascii"), "url": url}
        return True

    def site_of(self, host: str) -> str:
        site = self.origin_policy.site_of(host)
        if site == host and self.web_scope is not None:
            # a provider / own / third-party host the scope allowed for a site:
            # it shares that site's pacer slot, request total and byte total
            return self.web_scope.site_for(host) or site
        return site

    def allowed_types(self) -> frozenset:
        return ALLOWED_TYPES | (REALISTIC_EXTRA_TYPES if self.realistic_browser else frozenset())

    def lane(self, host: str, kind: str) -> None:
        site = self.site_of(host)
        d = self.lanes.setdefault(site, {})
        d[kind] = d.get(kind, 0) + 1

    def site_total(self, host: str) -> int:
        """Every request counted for the site, from every channel (one pacer)."""
        return int(self.pacer.count.get(self.site_of(host), 0))

    def own_total(self, host: str) -> int:
        """The site's total without its search provider's share: what the site's OWN static
        and noise shares are measured on - the provider's requests count in the run total, never in the site's share."""
        site = self.site_of(host)
        return self.site_total(host) - int(getattr(self, "provider_sent", {}).get(site, 0))

    def site_refusal(self, url_or_host: str) -> str:
        """'' or why the SITE may take no more requests (for every channel)."""
        host = host_of(url_or_host) if "://" in url_or_host else url_or_host
        site = self.site_of(host)
        if self.site_total(site) >= self.site_cap:
            return f"site ceiling of {self.site_cap} requests for {site} reached"
        return ""

    def bind_fetcher(self, fetch) -> None:
        """Hold an ``HttpFetcher`` (robots, plain GETs, the compiled requests via
        ``PacedTransport``) to the same site ceiling and the same site grouping."""
        fetch.site_refusal = self.site_refusal
        fetch.site_of = self.site_of

    def add_bytes(self, host: str, n: int) -> None:
        site = self.site_of(host)
        self.site_bytes[site] = self.site_bytes.get(site, 0) + max(0, int(n or 0))

    def host_count(self, host: str) -> int:
        """Requests counted for ``host``'s SITE (every host of a site shares one count)."""
        return int(self.pacer.count.get(self.site_of(host), 0))

    def max_load(self, host: str) -> int:
        return max(self.loads.get(host) or [0])

    def check(self) -> None:
        """Raise if any cap was ever exceeded or any bypass was seen."""
        served = [r["url"][:80] for r in self.value_requests if r["path"] in ("cache", "handoff")]
        msg = f"validation input answered without the site: {served[:3]}"
        if served and msg not in self.violations:
            self.violations.append(msg)
        over = [h for h, n in self.pacer.count.items() if n > self.per_host_cap]
        big = [h for h, ls in self.loads.items() if ls and max(ls) > self.per_load_cap]
        over += [f"{h} (site ceiling)" for h, n in self.pacer.count.items()
                 if n > self.site_cap and h not in over]
        if over or big or self.violations:
            raise TrafficViolation(f"over host cap: {over}; over load cap: {big}; "
                                   f"violations: {self.violations[:5]}")


class LoadGuard:
    """The decisions for ONE page load (one session)."""

    def __init__(self, budget: RequestBudget, allow_url: Optional[Callable] = None,
                 fresh_values: Tuple[str, ...] = ()):
        self.budget = budget
        self.allow_url = allow_url
        #: the session's inputs: a request carrying one (URL, body or header) is
        #: never answered from the run cache, and its response is never stored
        self.fresh_values = [str(v).lower() for v in fresh_values if str(v or "").strip()]
        budget.protect(self.fresh_values)
        self.fulfilled_ids: set = set()
        self.fulfilled_without_id: Dict[str, int] = {}
        self.cache_hits = 0
        #: same-site responses Chrome answered from its memory cache (no pause, 0 bytes)
        self.browser_cache_hits: List[str] = []
        #: the raw events of every request the audit flagged
        self.violation_events: List[dict] = []
        self.page_host = ""
        self.count: Dict[str, int] = {}
        self.continued_ids: set = set()
        #: networkId -> the page host the guard decided that request under (a goto to
        #: another host of the site moves ``page_host``; the audit judges each request
        #: by the host in force when the guard sent it)
        self.continued_page_host: Dict[str, str] = {}
        #: (session event index, page host) at every navigation that gave one
        self.page_host_log: List[Tuple[int, str]] = []
        #: networkId -> the request headers as the guard saw them PAUSED (the
        #: form the cache lookup keys on); ``Network.requestWillBeSent`` shows a
        #: different header set, so entries stored from it never matched a
        #: later lookup (0 cache hits in every run)
        self.paused_headers: Dict[str, Any] = {}
        self.continued_without_id: Dict[str, int] = {}
        self.failed: List[Tuple[str, str]] = []
        #: the resource type of each refused request, by its recorded URL (an image or a font refused
        #: on another domain is never a search data call - ``harness_tools.search_refusal``)
        self.failed_types: Dict[str, str] = {}
        self.load_capped = False
        #: requests sent in the CURRENT page load, and the totals of the
        #: earlier loads of this session (load_per_navigation only)
        self.load_n = 0
        self.load_totals: List[int] = []
        #: the search provider's requests sent in the CURRENT page load (its own share, ``PROVIDER_LOAD_SHARE``)
        self.provider_load_n = 0
        #: browser statics sent in the CURRENT page load (the runaway guard's count), and
        #: whether the last decision was one (``budget.browser_statics``)
        self.static_load_n = 0
        self.last_static = False
        self.last_search_post = ""
        #: how often the CURRENT page load asked for each static URL (the runaway guard counts loops only)
        self.static_urls: Dict[str, int] = {}
        #: the values this session typed (``GuardedSession.note_typed``): a static whose QUERY carries one is a data
        #: call (a JSONP search endpoint), counted like one
        self.typed: List[str] = []
        #: network ids sent as browser statics (their bytes are counted apart); counted XHR / fetch GETs that may still
        #: turn out to be a static file by their response's content type: nid -> (site, kind, url, load number)
        self.static_ids: set = set()
        self.data_meta: Dict[str, Tuple[str, str, str, int]] = {}
        #: XHR / fetch GETs sent as browser statics by their URL's extension: nid -> (site, kind, url, load number); a
        #: response that is data after all (a store platform's ``/cart.js`` answers JSON) moves back to the data count
        self.ext_meta: Dict[str, Tuple[str, str, str, int]] = {}
        self.last_by_ext = False
        self._main_frame: Optional[str] = None
        #: static lane (budget.static_in_flight): requests counted and waiting
        #: for a free slot, as (requestId, networkId); continued on release
        self.static_queue: List[Tuple[str, str]] = []
        #: (a store's ~120 checkout-script prefetches, counted as statics, can fill the lane while the page's own
        #: provider scripts wait behind them): the lane's PRIORITY per queued network id - like a
        #: browser, render-critical scripts, stylesheets and fonts first, images next, a fetch of a "file" (a prefetch,
        #: a poll) last; first come first within a priority
        self.static_prio: Dict[str, Tuple[int, int]] = {}
        self._static_seq = 0
        #: network ids THIS guard put into the run-wide ``budget.static_open``;
        #: released at close, so a page closed mid-load cannot leak its slots
        #: into the next session's lane
        self.static_mine: set = set()
        self._send: Optional[Callable] = None
        #: responses from third-party hosts the guard never sent (reported)
        self.leaks: List[str] = []
        #: web scope (budget.web_scope): the allowance the last decision used,
        #: the hosts this session sent to under one, and the session's hook that
        #: reads a paused RESPONSE body (the site's own document/script) for the
        #: scope, then continues it: ``await read_body(request_id, url, rtype)``
        self.last_kind = ""
        self.scope_hosts: set = set()
        self.read_body: Optional[Callable] = None
        #: action window (``budget.noise_share``): > 0 while an action of the
        #: session (a navigation, click, typing, Enter) runs, and until
        #: ``act_until`` (budget clock) after it; a data request outside it is
        #: the page's own background traffic
        self.acting = 0
        self.act_until = 0.0

    def _rec(self, url: str, n: int) -> str:
        """A URL as the guard RECORDS it: provider keys redacted when a web scope is on."""
        if self.budget.web_scope is None:
            return url[:n]
        from anyuse.execution.web_scope import redact_keys
        return redact_keys(url)[:n]

    def _host_ok(self, host: str) -> bool:
        """The page's site, or a host this session sent to under the web scope."""
        return self.budget.origin_policy.allows(self.page_host, host) or host in self.scope_hosts

    def scan_wanted(self, params: dict) -> bool:
        """Should this paused RESPONSE be shown to the web scope? Only the site's
        OWN documents and scripts (same site or its own hosts), 200, when a
        provider or POST-search setting is on."""
        scope = self.budget.web_scope
        if scope is None or not scope.wants_sources or self.read_body is None:
            return False
        if str(params.get("resourceType") or "") not in ("Document", "Script"):
            return False
        if int(params.get("responseStatusCode") or 0) != 200:
            return False
        host = host_of(str((params.get("request") or {}).get("url") or ""))
        if self.budget.origin_policy.allows(self.page_host, host):
            return True
        return bool(scope.is_own_host(self.budget.site_of(self.page_host), host))

    def observe_source(self, url: str, rtype: str, body: str) -> None:
        scope = self.budget.web_scope
        if scope is not None and body:
            scope.observe(self.budget.site_of(self.page_host), url, rtype, body)

    def start(self, url: str, at: Optional[int] = None) -> None:
        """A navigation: ``at`` is the session's event count when it began, so the
        audit judges the events before it by the previous page host."""
        self.page_host = host_of(url)
        if at is not None:
            self.page_host_log.append((int(at), self.page_host))

    def _page_host_at(self, i: int) -> str:
        """The page host in force at event index ``i`` (the current one if unknown)."""
        host = ""
        for at, h in self.page_host_log:
            if at <= i:
                host = h
            else:
                break
        return host or self.page_host

    def in_action(self) -> bool:
        return self.acting > 0 or self.budget.clock() < self.act_until

    def is_noise(self, url: str, rtype: str, frame: str = "") -> bool:
        """The page's own background traffic (``budget.noise_share``): a data
        request outside an action window, a data request whose path names
        telemetry, or a sub-frame document. Pure but for the window state."""
        if rtype == "Document":
            return bool(frame and self._main_frame and frame != self._main_frame)
        if rtype not in NOISE_DATA_TYPES:
            return False
        try:
            path = urllib.parse.urlsplit(url).path or "/"
        except ValueError:
            path = "/"
        if TELEMETRY_PATH.search(path):
            return True
        return not self.in_action()

    def decide(self, url: str, rtype: str, method: str = "GET", post: str = "",
               headers=None, noise: bool = False, frame: str = "") -> str:
        """'' to send; otherwise why the request is failed. Without a web scope
        pure; with one, the scope remembers which site a host it allowed
        belongs to (for the site's accounting) and :attr:`last_kind` names the
        allowance used (``""``: today's same-site rule). :attr:`last_static`:
        the request is a browser static (``budget.browser_statics``)."""
        self.last_kind = ""
        self.last_static = False
        self.last_search_post = ""
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return f"not http(s): {url[:60]}"
        m = (method or "GET").upper()
        host = host_of(url)
        scope = self.budget.web_scope
        grant = getattr(self.budget, "write_grant", None)
        if m == "POST" and self.budget.search_post:
            # the page's own read-only POST search
            sp_why, sp_kind = self.search_post(url, host, rtype, post, headers, p.path)
            if not sp_why:
                self.last_search_post = f"a search POST right after typing, its body carries the typed value" + \
                    (" (the granted provider)" if sp_kind else "")
                self.last_kind = sp_kind
                types = self.budget.allowed_types()
                return self._after_scope(url, rtype, m, post, headers, noise, frame, host, p, grant, types)
            if len(self.budget.search_posts_refused) < 40:
                self.budget.search_posts_refused.append({"host": host, "path": p.path[:120], "why": sp_why[:120]})
        if scope is None:
            if m not in self.budget.allowed_methods and not (grant is not None and
                                                             grant.allows_method(url, m, rtype)):
                return f"method {m} is not allowed by the run's policy"
            if not self.budget.origin_policy.allows(self.page_host, host):
                return f"third-party host {host}"
            types = self.budget.allowed_types()
        else:
            same = self.budget.origin_policy.allows(self.page_host, host)
            page_site = self.budget.site_of(self.page_host)
            types = self.budget.allowed_types()
            granted = same and m not in self.budget.allowed_methods and grant is not None and \
                grant.allows_method(url, m, rtype)
            if not same or (m not in self.budget.allowed_methods and not granted):
                v = scope.classify(page_site, url, m, rtype, post,
                                   self.budget._headers(headers), same_site=same,
                                   values=tuple(self.fresh_values) +
                                   tuple(self.budget.protected_values) + tuple(self.typed),
                                   acting=self.in_action() and bool(self.typed))
                if not v.ok:
                    why = v.why or f"third-party host {host}"
                    if not why.startswith(("method ", "third-party host ")):
                        why = f"third-party host {host}: {why}"
                    scope.refused(page_site, host, why)
                    return why
                self.last_kind = v.kind
                if v.kind == "provider":
                    types = types | {"Preflight"}
        return self._after_scope(url, rtype, m, post, headers, noise, frame, host, p, grant, types)

    def _after_scope(self, url: str, rtype: str, m: str, post: str, headers, noise: bool, frame: str, host: str, p,
                     grant, types) -> str:
        """``decide`` past the method and host rules: the type, statics, shares, ceilings, robots."""
        if self.telemetry_frame(url, rtype, frame):
            # (a store platform's web-pixel sandbox frames, several per page load):
            # a sub-frame document whose path is an analytics / pixel sandbox is refused like a tracker - never loaded
            return (f"telemetry frame: a sub-frame analytics / pixel document of {host} (never loaded, like a "
                    f"tracker)")
        static = self.browser_static(url, rtype, m, post, headers)
        if static:
            types = types | STATIC_TYPES
        if rtype not in types:
            return f"resource type {rtype or '?'} is blocked"
        if rtype in self.budget.skip_types:
            return f"resource type {rtype} skipped (benchmark mode: never sent, not counted)"
        if static:
            self.last_static = True
            return self._decide_static(url, host, p.path, grant)
        share = self.budget.static_share()
        provider = self.last_kind == "provider"
        if provider:
            # the provider's own share per page load; the site's static share and per-load cap
            # are the site's own (its run total still counts every provider request)
            if self.provider_load_n >= PROVIDER_LOAD_SHARE:
                return (f"provider share of {PROVIDER_LOAD_SHARE} per page load reached (the site's own search "
                        f"provider; not sent, not counted)")
            share = None
        if noise and share is not None and self.budget.own_total(host) >= share:
            site = self.budget.site_of(host)
            self.budget.noise_skipped[site] = self.budget.noise_skipped.get(site, 0) + 1
            return (f"noise share of {share} for {site} reached (the page's own background "
                    f"request; not sent, not counted; the host is not stopped)")
        if share is not None and rtype in SHARE_TYPES and self.budget.own_total(host) >= share and \
                not (rtype == "Script" and self.budget.replay_script_ok(self.page_host, host)):
            # a proof replay's own share of same-origin scripts goes past the static share (counted)
            site = self.budget.site_of(host)
            self.budget.static_skipped[site] = self.budget.static_skipped.get(site, 0) + 1
            return (f"static share of {share} for {site} reached (static skipped, not "
                    f"counted; the host is not stopped)")
        hr = self.budget.static_bytes_headroom
        if hr and rtype in SHARE_TYPES and self.budget.static_bytes_cap and \
                self.budget.site_bytes.get(self.budget.site_of(host), 0) >= hr * self.budget.static_bytes_cap:
            site = self.budget.site_of(host)
            self.budget.static_skipped[site] = self.budget.static_skipped.get(site, 0) + 1
            return (f"static share: the byte headroom of {hr:.2f} x {self.budget.static_bytes_cap} for {site} "
                    f"reached (static skipped, not counted; the host is not stopped)")
        if self.budget.realistic_browser and rtype not in REALISTIC_EXTRA_TYPES and \
                self.budget.site_bytes.get(self.budget.site_of(host), 0) >= \
                self.budget.static_bytes_cap:
            # the byte ceiling holds for every request, not only CSS/images/fonts
            # (held for those only, it let documents and scripts take a
            # site past 40 MB against a 30 MB per-site ceiling); a data request
            # at it stops the host
            return (f"host cap: byte ceiling of {self.budget.static_bytes_cap} for "
                    f"{self.budget.site_of(host)} reached")
        if rtype in REALISTIC_EXTRA_TYPES and \
                self.budget.site_bytes.get(self.budget.site_of(host), 0) >= \
                self.budget.static_bytes_cap:
            return (f"byte ceiling of {self.budget.static_bytes_cap} for "
                    f"{self.budget.site_of(host)} reached (static assets)")
        if _ACCOUNT.search(p.path) and not (grant is not None and grant.allows_account(url)):
            return "account/login/cart path"
        site = self.budget.site_of(host)
        if site in self.budget.stopped:
            return f"host stopped: {self.budget.stopped[site]}"
        if self.allow_url is not None:
            try:
                ok = bool(self.allow_url(url))
            except Exception:
                ok = False
            if not ok:
                rr = getattr(self.budget, "robots_refused", None)
                if isinstance(rr, dict):
                    rr[host] = rr.get(host, 0) + 1
                return f"robots_refused: {self._rec(url, 100)}"
        if self.load_n >= self.budget.per_load_cap and not provider:
            return f"page-load cap of {self.budget.per_load_cap} reached"
        if self.budget.host_count(host) >= self.budget.per_host_cap:
            return f"host cap of {self.budget.per_host_cap} reached"
        if self.budget.site_total(host) >= self.budget.site_cap:
            return f"host cap: site ceiling of {self.budget.site_cap} reached"
        return ""

    def search_post(self, url: str, host: str, rtype: str, post: str, headers, path: str) -> Tuple[str, str]:
        """``("", kind)`` when this POST is the page's own read-only search, else
        ``(why not, "")``. Every condition holds: an XHR / fetch / document POST sent while (or right after) this session
        types into the site's search box (``GuardedSession.note_typed`` + the action window); to the site's own host
        (kind ``""``), a catalogued provider the run's scope granted for the site, or the host the site's page sends the
        typed value to (``WebScope.observed_call``; both kind ``provider``); never an account / login /
        cart / checkout / payment path, a state-changing or telemetry path, a credential header, a GraphQL mutation or
        an upload; a small form or JSON body (``web_scope.body_pairs``) with a field whose whole value is the typed
        value (``web_scope.field_carrying``), or, the last rung, the typed value inside a longer field under a path
        or field named like a search (:data:`SEARCH_POST_NAME`)."""
        from anyuse.execution import web_scope as WSC
        if rtype not in ("XHR", "Fetch", "Document"):
            return f"a {rtype or '?'} POST is never a search", ""
        if not self.typed:
            return "no search value was typed into the site's search box in this session", ""
        if not self.in_action():
            return "not sent right after typing into the site's search box", ""
        kind = ""
        observed = False
        scope = self.budget.web_scope
        site = self.budget.site_of(self.page_host)
        if not self.budget.origin_policy.allows(self.page_host, host):
            if scope is None or not scope.providers or WSC.contact_refusal(host, scope.refuse_gov) or (
                    scope.grant_for(site, host) is None and not scope.observed_providers):
                return f"third-party host {host} (not the site's own host or its granted provider)", ""
            if scope.grant_for(site, host) is not None:
                scope.host_site[host] = WSC._name(site)
            else:
                # the host the site's page sends the typed value to: decided by the scope's observed-provider rule
                # once every check below holds
                observed = True
            kind = "provider"
        if _ACCOUNT.search(path) or re.search(r"(?i)(^|/)(payment|pay|billing|order|orders|wishlist)(/|$|\.)", path):
            return "an account / login / cart / checkout / payment path", ""
        w = WSC.state_word(path)
        if w:
            return f"its path names a state change ({w})", ""
        if TELEMETRY_PATH.search(path or "/") or NOT_SEARCH_POST_PATH.search(path or "/"):
            return "its path names telemetry or infrastructure (an analytics event that echoes the query is not the " \
                   "search)", ""
        hdrs = self.budget._headers(headers)
        if "authorization" in hdrs:
            # (cookies: every guarded session is a NEW anonymous browser context - the page's own anonymous ones only)
            return "a credential header", ""
        pairs, why = WSC.body_pairs(post, hdrs.get("content-type", ""))
        if pairs is None:
            return f"its body is not a small search body ({why})", ""
        gql = WSC.graphql_refusal(post)
        if gql:
            return gql, ""
        # a query string inside a body field (a multi-query search body: "params": "query=...&hitsPerPage=20") is
        # read as fields too
        pairs = list(pairs) + [(k2, v2) for _k, v in pairs if "=" in str(v) and "://" not in str(v)
                               for k2, v2 in urllib.parse.parse_qsl(str(v), keep_blank_values=True)]
        for k, _v in pairs:
            w = WSC.state_word(k)
            if w:
                return f"a body field names a state change ({k[:40]!r}: {w})", ""
        # a body field whose whole value is the typed value, sent right after typing into the site's search box,
        # is the page's search whatever it is named (whether its answer is a listing that changes with the input
        # is the proof's question); the value inside a longer field leaves the decision to the names
        if not WSC.field_carrying(pairs, self.typed):
            blob = " ".join([urllib.parse.unquote_plus(post or "")] + [str(v) for _k, v in pairs]).lower()
            if not any(v and v in blob for v in self.typed):
                return "its body does not carry the typed value", ""
            if not SEARCH_POST_NAME.search(path or "") and not any(
                    SEARCH_POST_NAME.search(str(k)) and any(t and t in str(v).lower() for t in self.typed)
                    for k, v in pairs):
                return ("the typed value is inside a longer field, and neither its path nor that field is named "
                        "like a search"), ""
        if observed:
            v = scope.observed_call(site, url, "POST", rtype, post, hdrs, values=self.typed, acting=True)
            if v is None or not v.ok:
                return (v.why if v is not None else
                        f"third-party host {host} (not the site's own host or its search provider)"), ""
        return "", kind

    def browser_static(self, url: str, rtype: str, method: str = "GET", post: str = "", headers=None) -> bool:
        """Is this a BROWSER STATIC - a script, stylesheet, font or image fetched by GET/HEAD
        from the site itself, its own host, its platform asset CDN or its granted search provider (the kind the decision
        just used, :attr:`last_kind`), carrying no body and no searched value in its query (a JSONP search endpoint is a
        data call)? Only with ``budget.browser_statics``."""
        self.last_by_ext = False
        if not self.budget.browser_statics or self.last_kind not in BROWSER_STATIC_KINDS:
            return False
        if rtype not in STATIC_TYPES and not (rtype in ("XHR", "Fetch") and self.static_file(url)):
            return False
        self.last_by_ext = rtype in ("XHR", "Fetch")
        if (method or "GET").upper() not in DEFAULT_METHODS or post:
            return False
        return not self._query_carries_value(url)

    @staticmethod
    def static_file(url: str) -> bool:
        """Does the URL name a static FILE (a script, stylesheet, font or image, by its path's extension)?"""
        try:
            return bool(STATIC_FILE_EXT.search(urllib.parse.urlsplit(url).path or ""))
        except ValueError:
            return False

    def telemetry_frame(self, url: str, rtype: str, frame: str = "") -> bool:
        """A SUB-FRAME document (not the page's main frame) whose path names an analytics /
        pixel sandbox (``TELEMETRY_PATH``: web-pixels, pixel, analytics, collect, beacon, ...). Only with
        ``budget.browser_statics`` and a known main frame; a sub-frame that renders the site's own content stays a
        document."""
        if not self.budget.browser_statics or rtype != "Document" or not frame or not self._main_frame or \
                frame == self._main_frame:
            return False
        try:
            path = urllib.parse.urlsplit(url).path or "/"
        except ValueError:
            return False
        return bool(TELEMETRY_PATH.search(path))

    def _query_carries_value(self, url: str) -> bool:
        """Does the URL's QUERY carry a validation input or a value this session typed (a search endpoint)? The path is
        not read: a product image named after the searched word is an asset, not a search."""
        vals = [v for v in list(self.budget.protected_values) + list(self.fresh_values) + list(self.typed)
                if len(v) >= 2]
        if not vals:
            return False
        try:
            query = urllib.parse.unquote_plus(urllib.parse.urlsplit(url).query or "").lower()
        except ValueError:
            return False
        return bool(query) and any(v in query for v in vals)

    def _decide_static(self, url: str, host: str, path: str, grant) -> str:
        """A browser static's decision: the account path, a stopped site, robots.txt (as for every request), then the
        runaway guard - no request ceiling, no share, no byte ceiling (a run's byte ceiling counts documents and data)."""
        if _ACCOUNT.search(path) and not (grant is not None and grant.allows_account(url)):
            return "account/login/cart path"
        site = self.budget.site_of(host)
        if site in self.budget.stopped:
            return f"host stopped: {self.budget.stopped[site]}"
        if self.allow_url is not None:
            try:
                ok = bool(self.allow_url(url))
            except Exception:
                ok = False
            if not ok:
                rr = getattr(self.budget, "robots_refused", None)
                if isinstance(rr, dict):
                    rr[host] = rr.get(host, 0) + 1
                return f"robots_refused: {self._rec(url, 100)}"
        if self.last_by_ext and self.budget.site_total(host) >= self.budget.site_cap:
            # a fetch of a static FILE is a browser static unless its response is data (then it is counted): it is
            # sent only while the run's ceiling has room for it
            return (f"site ceiling of {self.budget.site_cap} reached for a fetch of a file that may be data (not sent; "
                    f"the host is not stopped)")
        if self.last_by_ext:
            return ""                                  # a fetch of a "file" may be polled data (/cart.js): never a loop
        key = url.split("#", 1)[0]
        seen = self.static_urls.get(key, 0)
        if seen >= STATIC_RUNAWAY_REPEAT:
            return (f"{STATIC_RUNAWAY} guard: the same static asset asked for more than {STATIC_RUNAWAY_REPEAT} times in "
                    f"one page load of {site} - a loop ({self._rec(url, 100)}; the site is stopped for the run)")
        if not seen and len(self.static_urls) >= STATIC_RUNAWAY_DISTINCT:
            return (f"{STATIC_RUNAWAY} guard: more than {STATIC_RUNAWAY_DISTINCT} distinct static assets in one page "
                    f"load of {site} (the site is stopped for the run)")
        self.static_urls[key] = seen + 1
        return ""

    def value_bearing(self, url: str, post: str = "", headers=None) -> bool:
        return self.budget.carries_value(url, post, headers, tuple(self.fresh_values))

    def cacheable(self, url: str, rtype: str, method: str = "GET", post: str = "",
                  headers=None) -> bool:
        """May this request be answered from the static cache? Never a document,
        XHR or fetch, never a non-GET, never a request carrying an input."""
        return (self.budget.cache_enabled and rtype in CACHE_TYPES
                and (method or "GET").upper() == "GET"
                and not self.value_bearing(url, post, headers))

    def _note(self, url: str, rtype: str, method: str, path: str, bearing: bool) -> None:
        if rtype in DATA_TYPES:
            self.budget.data_paths[path] = self.budget.data_paths.get(path, 0) + 1
        if bearing:
            self.budget.value_requests.append({"url": url[:200], "type": rtype,
                                               "method": method, "path": path})

    async def handle(self, params: dict, send: Callable) -> bool:
        """Answer one ``Fetch.requestPaused``. ``send(method, params)`` is awaited.
        Returns True if the request was sent."""
        rid = params.get("requestId")
        req = params.get("request") or {}
        url = str(req.get("url") or "")
        rtype = str(params.get("resourceType") or "")
        method = str(req.get("method") or "GET").upper()
        if not rid:
            return False
        if params.get("responseStatusCode") is not None or params.get("responseErrorReason"):
            # response stage: the request already went out through this guard.
            # With a provider/POST-search scope the site's own document or
            # script is shown to the scope first (read_body continues it)
            if self.scan_wanted(params):
                await self.read_body(rid, url, rtype)
                return False
            await send("Fetch.continueRequest", {"requestId": rid})
            return False
        post = str(req.get("postData") or "")
        rheaders = req.get("headers") or {}
        frame = str(params.get("frameId") or "")
        if rtype == "Document" and self.budget.noise_share and self._main_frame is None and frame:
            self._main_frame = frame
        if rtype == "Document" and self.budget.load_per_navigation and \
                not params.get("redirectedRequestId"):
            if self._main_frame is None:
                self._main_frame = frame
            elif frame == self._main_frame and (self.load_n > 0 or self.provider_load_n > 0 or self.static_load_n > 0):
                self.load_totals.append(self.load_n)
                self.load_n = 0
                self.provider_load_n = 0
                self.static_load_n = 0
                self.static_urls = {}
                self.load_capped = False
        if rtype == "Document" and params.get("redirectedRequestId") and self.budget.web_scope is not None and \
                (self._main_frame is None or frame == self._main_frame):
            # the site's own server sent its navigation to this host: evidence that binds the host to the site
            self.budget.web_scope.bind(self.budget.site_of(self.page_host), host_of(url),
                                       "a redirect of the site's navigation")
        bearing = self.value_bearing(url, post, rheaders)
        noise = bool(self.budget.noise_share and self.is_noise(url, rtype, frame))
        why = self.decide(url, rtype, method, post, rheaders, noise=noise, frame=frame)
        kind = self.last_kind
        static = self.last_static
        if not why and self.last_search_post and len(self.budget.search_posts) < 200:
            # every search POST on the run record (method, host, path, why allowed)
            self.budget.search_posts.append({"method": method, "host": host_of(url),
                                             "path": urllib.parse.urlsplit(url).path[:120], "type": rtype,
                                             "why": self.last_search_post})
        if why.startswith(STATIC_RUNAWAY):
            # the runaway guard is a typed stop - the site takes nothing more in this run
            site = self.budget.site_of(host_of(url))
            self.budget.runaway.setdefault(site, why)
            self.budget.stopped.setdefault(site, why)
        if why.startswith("method "):
            self.budget.refused_methods.append((method, self._rec(url, 200)))
        capped = why.startswith(("page-load cap", "host cap", "static share", "noise share", "provider share"))
        ent, path = None, ""
        if not why or capped:
            if rtype == "Document" and url in self.budget.handoffs and not bearing and \
                    method == "GET":
                ent, path = self.budget.handoffs.pop(url), "handoff"
            elif self.cacheable(url, rtype, method, post, rheaders):
                ent = self.budget.lookup(method, url, rheaders)
                path = "cache" if ent is not None else ""
        if ent is not None:
            # answered here (static cache, or the one-shot discovery handoff)
            self._note(url, rtype, method, path, bearing)
            if kind:
                # a web-scope host answered from the run cache is still this
                # session's allowed host: its Network.responseReceived must not
                # read as a third-party bypass (a cookie script on the site's own
                # subdomain cached in one session and served from the cache in
                # the next was audited as a fatal leak)
                self.scope_hosts.add(host_of(url))
            if rtype in ("Document", "Script") and self.budget.web_scope is not None and \
                    self.budget.web_scope.wants_sources and self._host_ok(host_of(url)):
                try:
                    self.observe_source(url, rtype, base64.b64decode(ent["body"]).decode(
                        "utf-8", "replace"))
                except Exception:              # noqa: BLE001 - a scan never blocks
                    pass
            nid = params.get("networkId")
            if nid:
                self.fulfilled_ids.add(nid)
            else:
                h = host_of(url)
                self.fulfilled_without_id[h] = self.fulfilled_without_id.get(h, 0) + 1
            self.cache_hits += 1
            h = host_of(url)
            self.budget.cache_hits[h] = self.budget.cache_hits.get(h, 0) + 1
            if path == "cache" and len(self.budget.cache_reuse) < 500:
                # every in-run reuse is recorded (host, URL, type, the scope's kind)
                self.budget.cache_reuse.append((h, self._rec(url, 160), rtype, kind or "site"))
            await send("Fetch.fulfillRequest", {"requestId": rid,
                                                "responseCode": ent["status"],
                                                "responseHeaders": ent["headers"],
                                                "body": ent["body"]})
            return False
        if why.startswith("page-load cap"):
            self.load_capped = True
        if why.startswith("host cap") and rtype not in SHARE_TYPES:
            # only a Document/XHR/Fetch (or other data request) can stop the
            # host; a static at a cap is just not sent
            self.budget.stopped[self.budget.site_of(self.page_host)] = why
        if why.startswith("third-party host "):
            self.budget.third_party_refused.append((self.page_host, self._rec(url, 200)))
            if self.budget.fatal_third_party_attempts:
                self.budget.violations.append(f"third-party request attempted from "
                                              f"{self.page_host}: {self._rec(url, 100)}")
        if why:
            self.failed.append((self._rec(url, 160), why))
            if len(self.failed_types) < 5000:
                self.failed_types[self._rec(url, 160)] = str(rtype or "")
            self._note(url, rtype, method, "refused", bearing)
            await send("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"})
            return False
        host = host_of(url)
        site = self.budget.site_of(host)
        if rtype == "Document" and self.budget.nav_min_interval_s > 0:
            last = self.budget.last_nav.get(site)
            if last is not None:
                wait = last + self.budget.nav_min_interval_s - self.budget.clock()
                if wait > 0:
                    await self.budget.asleep(wait)
        if rtype in CACHE_TYPES and params.get("networkId"):
            self.paused_headers[str(params.get("networkId"))] = rheaders
        if static:
            return await self._send_static(params, rid, url, rtype, method, kind, bearing, send)
        if self.budget.static_in_flight and rtype in STATIC_TYPES:
            # the static lane: counted like every request (site ceiling, host
            # cap, load cap), but not spaced by the 1 req/s pacer; at most
            # ``static_in_flight`` are open at once, the rest wait their turn
            self.budget.pacer.count[site] = self.budget.pacer.count.get(site, 0) + 1
            self.count[host] = self.count.get(host, 0) + 1
            self._count_load(kind, site)
            nid = str(params.get("networkId") or "")
            if nid:
                self.continued_ids.add(nid)
                self.continued_page_host[nid] = self.page_host
            else:
                self.continued_without_id[host] = self.continued_without_id.get(host, 0) + 1
            self.budget.sent.append((host, self.budget.pacer.clock(), self._rec(url, 200), rtype))
            self._note(url, rtype, method, "network", bearing)
            self._scope_sent(kind, site, host)
            self.budget.lane(host, "static")
            self._send = send
            if nid and len(self.budget.static_open) >= self.budget.static_in_flight:
                self.static_queue.append((rid, nid))
                return True
            if nid:
                self.budget.static_open.add(nid)
                self.static_mine.add(nid)
                self.budget.static_peak = max(self.budget.static_peak,
                                              len(self.budget.static_open))
            await send("Fetch.continueRequest", {"requestId": rid})
            return True
        gap = self.budget.pacer.reserve(site)
        if gap > 0:
            await self.budget.asleep(gap)
        if rtype == "Document":
            self.budget.last_nav[site] = self.budget.clock()
        self.count[host] = self.count.get(host, 0) + 1
        self._count_load(kind, site)
        nid = params.get("networkId")
        if nid and self.budget.browser_statics and rtype in ("XHR", "Fetch") and method in DEFAULT_METHODS and \
                not post and kind in BROWSER_STATIC_KINDS and not self._query_carries_value(url) and \
                len(self.data_meta) < 4000:
            # its response may show a static file (a script fetched by fetch()): then it is a browser static
            self.data_meta[str(nid)] = (site, kind, url, len(self.load_totals))
        if nid:
            self.continued_ids.add(nid)
            self.continued_page_host[str(nid)] = self.page_host
        else:
            self.continued_without_id[host] = self.continued_without_id.get(host, 0) + 1
        self.budget.sent.append((host, self.budget.pacer.clock(), self._rec(url, 200), rtype))
        self._note(url, rtype, method, "network", bearing)
        self._scope_sent(kind, site, host)
        self.budget.lane(host, "static" if rtype in SHARE_TYPES else ("noise" if noise else "action"))
        await send("Fetch.continueRequest", {"requestId": rid})
        return True

    async def _send_static(self, params: dict, rid: str, url: str, rtype: str, method: str, kind: str, bearing: bool,
                           send: Callable) -> bool:
        """Send one browser static: counted apart (``budget.static_sent``, the page load's
        runaway count), never in the site's request total; logged like every sent request (``budget.sent``, the scope's
        record); through the static lane (at most ``budget.lane_limit()`` in flight), outside the 1 req/s lane of
        documents and data; stored in the run cache when it finishes (``GuardedSession._cache_on_finish``)."""
        host = host_of(url)
        site = self.budget.site_of(host)
        self.count[host] = self.count.get(host, 0) + 1
        self.static_load_n += 1
        self.budget.count_static(site, kind, self.static_load_n)
        nid = str(params.get("networkId") or "")
        if nid:
            self.continued_ids.add(nid)
            self.continued_page_host[nid] = self.page_host
            self.static_ids.add(nid)
            if self.last_by_ext and len(self.ext_meta) < 4000:
                self.ext_meta[nid] = (site, kind, url, len(self.load_totals))
        else:
            self.continued_without_id[host] = self.continued_without_id.get(host, 0) + 1
        self.budget.sent.append((host, self.budget.pacer.clock(), self._rec(url, 200), rtype))
        self._note(url, rtype, method, "network", bearing)
        self._scope_sent(kind, site, host)
        self.budget.lane(host, "static")
        limit = self.budget.lane_limit()
        if limit:
            self._send = send
            if nid and len(self.budget.static_open) >= limit:
                self._static_seq += 1
                self.static_prio[nid] = (0 if rtype in ("Script", "Stylesheet", "Font") else
                                         1 if rtype == "Image" else 2, self._static_seq)
                self.static_queue.append((rid, nid))
                return True
            if nid:
                self.budget.static_open.add(nid)
                self.static_mine.add(nid)
                self.budget.static_peak = max(self.budget.static_peak, len(self.budget.static_open))
        await send("Fetch.continueRequest", {"requestId": rid})
        return True

    def _count_load(self, kind: str, site: str) -> None:
        """One sent request in the page load's count: the provider's own share or the
        site's per-load count."""
        if kind == "provider":
            self.provider_load_n += 1
            self.budget.provider_sent[site] = self.budget.provider_sent.get(site, 0) + 1
        else:
            self.load_n += 1

    def reclassify(self, network_id: str, mime: str, status: int = 200) -> bool:
        """An XHR / fetch GET is a browser static when it fetches a static FILE - by its
        URL's extension or its response's content type (``STATIC_MIME``). A counted one whose 200 response is a static
        file moves from the data count to the browser statics (the site's total and the page load's count give it
        back); one sent as a static by its extension whose response is data after all (a JSON, HTML, XML or text
        answer) moves to the data count. True when it moved."""
        nid = str(network_id or "")
        back = self.ext_meta.pop(nid, None)
        mt = str(mime or "").strip().lower()
        if back is not None:
            if not mt or STATIC_MIME.match(mt):
                return False
            if not re.match(r"^(?:application/(?:[\w.+-]*json|xml|x-www-form-urlencoded)|text/(?:html|plain|xml))", mt):
                return False
            site, kind, url, load = back
            b = self.budget
            b.pacer.count[site] = b.pacer.count.get(site, 0) + 1
            same_load = load == len(self.load_totals)
            if kind == "provider":
                b.provider_sent[site] = b.provider_sent.get(site, 0) + 1
                if same_load:
                    self.provider_load_n += 1
            elif same_load:
                self.load_n += 1
            if same_load:
                self.static_load_n = max(0, self.static_load_n - 1)
            b.static_sent[site] = max(0, b.static_sent.get(site, 0) - 1)
            ks = b.static_sent_kinds.setdefault(site, {})
            ks[kind or "site"] = max(0, ks.get(kind or "site", 0) - 1)
            self.static_ids.discard(nid)
            if len(b.reclassified) < 500:
                b.reclassified.append("data: " + self._rec(url, 150))
            return True
        meta = self.data_meta.pop(nid, None)
        if meta is None or int(status or 0) != 200 or not STATIC_MIME.match(str(mime or "").strip()):
            return False
        site, kind, url, load = meta
        b = self.budget
        b.pacer.count[site] = max(0, b.pacer.count.get(site, 0) - 1)
        same_load = load == len(self.load_totals)
        if kind == "provider":
            b.provider_sent[site] = max(0, b.provider_sent.get(site, 0) - 1)
            if same_load:
                self.provider_load_n = max(0, self.provider_load_n - 1)
        elif same_load:
            self.load_n = max(0, self.load_n - 1)
        if same_load:
            self.static_load_n += 1
        b.count_static(site, kind, self.static_load_n)
        self.static_ids.add(str(network_id))
        if len(b.reclassified) < 500:
            b.reclassified.append(self._rec(url, 160))
        return True

    def _scope_sent(self, kind: str, site: str, host: str) -> None:
        if kind and self.budget.web_scope is not None:
            self.scope_hosts.add(host)
            self.budget.web_scope.record(site, kind, host)

    async def release(self, network_id: str) -> None:
        """A static request finished or failed: free its slot, start the next.

        A request can also end while it still WAITS in the queue: a navigation
        (Enter in a typed search) cancels the old document's paused statics and
        Chrome reports ``loadingFailed`` for them. Such an entry is dropped:
        continuing it later would take a slot that no ``loadingFinished`` ever
        frees, so after four of them every later static of the session would
        wait forever (the result page stays empty)."""
        if not network_id:
            return
        if network_id not in self.budget.static_open:
            self.static_queue = [q for q in self.static_queue if q[1] != network_id]
            self.static_prio.pop(network_id, None)
            return
        self.budget.static_open.discard(network_id)
        while self.static_queue and self._send is not None and \
                len(self.budget.static_open) < self.budget.lane_limit():
            # the highest priority first (``static_prio``; a request queued without one keeps its place: priority 0)
            best = min(range(len(self.static_queue)),
                       key=lambda q: self.static_prio.get(self.static_queue[q][1], (0, 0)))
            rid, nid = self.static_queue.pop(best)
            self.static_prio.pop(nid, None)
            self.budget.static_open.add(nid)
            self.static_mine.add(nid)
            self.budget.static_peak = max(self.budget.static_peak,
                                          len(self.budget.static_open))
            await self._send("Fetch.continueRequest", {"requestId": rid})

    def audit(self, events: List[dict]) -> List[str]:
        """What reached the network (``Network`` domain) that the guard did not send."""
        problems: List[str] = []
        seen: Dict[str, int] = {}
        spare = dict(self.continued_without_id)
        spare_cached = dict(self.fulfilled_without_id)
        flagged: set = set()
        # A same-site IFRAME's stylesheet is answered from
        # Chrome's memory cache even with
        # Network.setCacheDisabled. Chrome sends requestServedFromCache, no Fetch pause, and
        # loadingFinished with 0 bytes; responseReceived still repeats the original request's
        # encodedDataLength. Only a response with ALL three marks, from the page's own site
        # (the origin policy, not web-scope hosts), is counted as a browser cache hit.
        from_cache: set = set()
        finished: Dict[str, int] = {}
        received: Dict[str, int] = {}
        for e in events:
            if e.get("sessionId"):
                continue
            m0, p0 = e.get("method"), e.get("params") or {}
            rid0 = str(p0.get("requestId") or "")
            if m0 == "Network.requestServedFromCache":
                from_cache.add(rid0)
            elif m0 == "Network.loadingFinished":
                finished[rid0] = finished.get(rid0, 0) + int(p0.get("encodedDataLength") or 0)
            elif m0 == "Network.dataReceived":
                received[rid0] = received.get(rid0, 0) + int(p0.get("encodedDataLength") or 0)
        for i, e in enumerate(events):
            if e.get("sessionId"):
                continue                      # a paused child target: never resumed
            m, p = e.get("method"), e.get("params") or {}
            # the page host this event is judged by: the one the guard decided the request
            # under, else the one in force at this point of the session (a goto to the site's
            # shop subdomain made every earlier load of its www host, each paused and decided by
            # the guard, read as third-party)
            under = self.continued_page_host.get(str(p.get("requestId") or "")) or \
                self._page_host_at(i)
            if m in ("Network.webSocketCreated", "Network.webSocketWillSendHandshakeRequest"):
                problems.append(f"websocket {str(p.get('url'))[:80]}")
                continue
            if m == "Network.responseReceived":
                url = str((p.get("response") or {}).get("url") or "")
            elif m == "Network.requestWillBeSent" and p.get("redirectResponse"):
                url = str((p.get("redirectResponse") or {}).get("url") or "")
            else:
                continue
            if not url.startswith(("http://", "https://")):
                continue
            host = host_of(url)
            if str(p.get("type") or "") == "Preflight" and self._granted_provider(host):
                # the CORS preflight of a documented provider read the guard
                # allowed: Chrome sends it before the paused request, so it is
                # counted against the site afterwards and reported, never fatal
                site = self.budget.site_of(self.page_host)
                self.budget.pacer.count[site] = self.budget.pacer.count.get(site, 0) + 1
                self.budget.preflights.append((host, self._rec(url, 120)))
                continue
            if not (self.budget.origin_policy.allows(under, host) or host in self.scope_hosts):
                # never sent by the guard (every third-party request is failed),
                # so it bypassed it (a CORS preflight is not exposed to Fetch
                # interception): FATAL, the run stops
                self.leaks.append(f"{p.get('type') or m}: {self._rec(url, 100)}")
                problems.append(f"third-party request reached the network: "
                                f"{p.get('type') or m} {self._rec(url, 100)}")
                flagged.add(str(p.get("requestId") or ""))
                continue
            rid = p.get("requestId")
            if rid in self.fulfilled_ids:
                continue                      # answered from the run cache: never sent
            srid = str(rid or "")
            if m == "Network.responseReceived" and rid not in self.continued_ids and \
                    srid in from_cache and finished.get(srid) == 0 and \
                    received.get(srid, 0) == 0 and \
                    self.budget.origin_policy.allows(under, host):
                self.browser_cache_hits.append(self._rec(url, 100))
                continue
            if rid not in self.continued_ids and spare.get(host, 0) == 0 and \
                    spare_cached.get(host, 0) > 0:
                spare_cached[host] -= 1       # a cache answer without a network id
                continue
            seen[host] = seen.get(host, 0) + 1
            if rid not in self.continued_ids:
                if spare.get(host, 0) > 0:
                    spare[host] -= 1
                else:
                    problems.append(f"request to {host} reached the network without the "
                                    f"guard: {self._rec(url, 100)}")
                    flagged.add(srid)
        for host, n in seen.items():
            if n > self.count.get(host, 0):
                # a redirect hop is one response per hop and one pause per hop
                if not any(host in pr for pr in problems):
                    problems.append(f"{n} responses from {host} but the guard sent "
                                    f"{self.count.get(host, 0)}")
                    flagged.update(str((e.get("params") or {}).get("requestId") or "")
                                   for e in events if e.get("method") == "Network.responseReceived"
                                   and host_of(str(((e.get("params") or {}).get("response") or {})
                                                   .get("url") or "")) == host)
        if flagged:
            # every event of a flagged request, kept so the next alarm can be diagnosed; bounded
            keep = [e for e in events if str((e.get("params") or {}).get("requestId") or "")
                    in flagged or str((e.get("params") or {}).get("networkId") or "") in flagged]
            self.violation_events.extend(keep[:VIOLATION_EVENTS_MAX])
        return problems

    def _granted_provider(self, host: str) -> bool:
        scope = self.budget.web_scope
        site = self.budget.site_of(self.page_host)
        return bool(scope is not None and scope.providers and
                    (scope.grant_for(site, host) or scope.observed_for(site, host)))

    def close(self, events: List[dict]) -> None:
        # the page is gone: its unfinished static requests will never report
        # loadingFinished, so free their slots and drop what still waited
        self.budget.static_open.difference_update(self.static_mine)
        self.static_mine.clear()
        self.static_queue.clear()
        totals = self.load_totals + [self.load_n]
        if self.page_host:
            self.budget.loads.setdefault(self.page_host, []).extend(totals)
        problems = self.audit(events)
        if max(totals) > self.budget.per_load_cap:
            problems.append(f"page load sent {max(totals)} > {self.budget.per_load_cap}")
        for h in sorted({self.budget.site_of(h) for h in self.count}):
            if self.budget.host_count(h) > self.budget.per_host_cap:
                problems.append(f"{h}: {self.budget.host_count(h)} > {self.budget.per_host_cap}")
        self.budget.violations.extend(problems)
        self.budget.leaks.extend(self.leaks)
        self.budget.browser_cache_hits.extend(self.browser_cache_hits)
        room = VIOLATION_EVENTS_MAX - len(self.budget.violation_events)
        if room > 0:
            self.budget.violation_events.extend(self.violation_events[:room])


class GuardedSession(CdpAgentSession):
    """``CdpAgentSession`` with the guard answering every paused request.

    ``budget`` is shared by all sessions of a run; each session is one page load
    (plus, for the typing probe, the requests the typing and Enter cause)."""

    def __init__(self, endpoint: str, user_agent: str = "", allow_url=None,
                 budget: Optional[RequestBudget] = None, fresh_values: Tuple[str, ...] = (),
                 **kw):
        super().__init__(endpoint, user_agent=user_agent or "research-probe/0.1",
                         allow_url=None)
        if budget is None:
            raise ValueError("GuardedSession needs the run's RequestBudget")
        self.budget = budget
        self.guard = LoadGuard(budget, allow_url=allow_url, fresh_values=tuple(fresh_values))
        #: every request is paused for the guard: a dropped connection is never
        #: reconnected (it released the paused requests); the page is closed
        self._intercepting = True
        self.refused: List[str] = []
        #: network request id -> host, for the per-site byte count
        self._rid_host: Dict[str, str] = {}
        #: every failed request's Chrome error (url, type, errorText, blockedReason), so a
        #: navigation that ends on an error page gets a typed reason (``site_canon.classify_failure``)
        self.load_errors: List[Dict[str, Any]] = []
        self._rid_url: Dict[str, Tuple[str, str]] = {}
        #: web scope: command id -> (paused request id, url, resource type) of a
        #: response body read in flight; the paused response continues on reply
        self._body_reads: Dict[int, Tuple[str, str, str]] = {}
        self.guard.read_body = self._read_body
        #: (a search provider's scripts were sent AGAIN on the results page - the run
        #: cache was filled only by ``harvest_cache``, which the product never called, and the browser's own cache is
        #: disabled): a static response the guard sent is stored in the run cache when it FINISHES, so the next page
        #: load of the run reuses it. Request / response metadata by network request id, and the body reads in flight
        self._req_meta: Dict[str, Tuple[str, Any, str]] = {}
        self._resp_meta: Dict[str, Tuple[str, str, int, Dict[str, Any]]] = {}
        self._cache_reads: Dict[int, Tuple[str, str, Dict[str, Any], str, Any, str]] = {}

    async def __aenter__(self):
        await self._open()
        return self

    async def _open(self):
        # bounded, off the event loop, crash-watched; a failure closes the
        # target it created (execution/browser_supervisor.py)
        await self._pin_new_page(self.setup)

    async def setup(self) -> None:
        """Everything before the first navigation; the interceptor comes FIRST."""
        patterns = [{"urlPattern": "*", "requestStage": "Request"}]
        scope = self.budget.web_scope
        if scope is not None and scope.wants_sources:
            # the site's own documents and scripts pause once more at the
            # RESPONSE stage, so the scope sees a provider named in them (or a
            # POST search form) before the page's scripts run
            patterns += [{"urlPattern": "*", "resourceType": t, "requestStage": "Response"}
                         for t in ("Document", "Script")]
        await self.call("Fetch.enable", {"patterns": patterns})
        await self.call("Network.enable", {"maxTotalBufferSize": 64_000_000,
                                           "maxResourceBufferSize": 32_000_000})
        await self.call("Network.setCacheDisabled", {"cacheDisabled": True})
        for method, params in (("Inspector.enable", {}),
                               ("Network.setBypassServiceWorker", {"bypass": True}),
                               ("Page.setPrerenderingAllowed", {"isAllowed": False}),
                               ("Target.setAutoAttach", {"autoAttach": True,
                                                         "waitForDebuggerOnStart": True,
                                                         "flatten": True})):
            try:
                await self.call(method, params)
            except Exception:
                pass
        await self.call("Page.enable")
        await self.call("Page.addScriptToEvaluateOnNewDocument", {"source": NO_SIDE_CHANNELS_JS})
        await self.call("Runtime.enable")
        await self.call("Network.setUserAgentOverride", {"userAgent": self.user_agent})
        await self.call("Emulation.setDeviceMetricsOverride",
                        {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                         "mobile": False})

    async def __aexit__(self, *exc):
        try:
            self.guard.close(self._events)
        finally:
            await super().__aexit__(*exc)

    async def _send(self, method: str, params: dict) -> None:
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))

    async def _read_body(self, request_id: str, url: str, rtype: str) -> None:
        """Ask for a paused response's body; :meth:`_on_reply` shows it to the
        web scope and continues the response (it never blocks a page)."""
        self._id += 1
        self._body_reads[self._id] = (request_id, url, rtype)
        await self._ws.send(json.dumps({"id": self._id, "method": "Fetch.getResponseBody",
                                        "params": {"requestId": request_id}}))

    async def _on_reply(self, msg: dict) -> None:
        cjob = self._cache_reads.pop(msg.get("id"), None)
        if cjob is not None:
            url, rtype, rheaders, method, hdrs, post = cjob
            res = msg.get("result") or {}
            if "error" not in msg and self.budget.remember(
                    url, 200, dict(rheaders or {}), res.get("body") or "",
                    base64_encoded=bool(res.get("base64Encoded")), rtype=rtype, method=method,
                    request_headers=hdrs, post=post, fresh_values=tuple(self.guard.fresh_values)):
                self.budget.cached_on_response += 1
            return
        job = self._body_reads.pop(msg.get("id"), None)
        if job is None:
            return
        request_id, url, rtype = job
        try:
            res = msg.get("result") or {}
            body = res.get("body") or ""
            if res.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", "replace")
            self.guard.observe_source(url, rtype, body)
        except Exception:                         # noqa: BLE001 - a scan never blocks
            pass
        await self._send("Fetch.continueRequest", {"requestId": request_id})

    async def _cache_on_finish(self, rid: str) -> None:
        """A static response the guard SENT has finished: ask for its body (non-blocking; :meth:`_on_reply` stores it
        under the run cache's HTTP rules - ``no-store`` and the rest refused and recorded as before). Its own host, a
        platform CDN or a provider host the scope allowed alike (``LoadGuard._host_ok``)."""
        meta = self._resp_meta.pop(rid, None)
        if meta is None or not self.budget.cache_enabled:
            return
        url, rtype, _status, rheaders = meta
        method, rq_headers, post = self._req_meta.pop(rid, ("GET", None, ""))
        if not self.guard._host_ok(host_of(url)):
            return
        hdrs = self.guard.paused_headers.get(rid, rq_headers)
        if not self.guard.cacheable(url, rtype, method, post, hdrs) or \
                self.budget.lookup(method, url, hdrs) is not None:
            return
        self._id += 1
        self._cache_reads[self._id] = (url, rtype, rheaders, method, hdrs, post)
        await self._ws.send(json.dumps({"id": self._id, "method": "Network.getResponseBody",
                                        "params": {"requestId": rid}}))

    async def _maybe_gate(self, msg: dict) -> None:
        if msg.get("sessionId"):
            return
        m = msg.get("method")
        if m == "Network.requestWillBeSent":
            p = msg.get("params") or {}
            self._rid_host[p.get("requestId")] = host_of(str((p.get("request") or {})
                                                              .get("url") or ""))
            if self.budget.cache_enabled and str(p.get("type") or "") in CACHE_TYPES and len(self._req_meta) < 4000:
                rq = p.get("request") or {}
                self._req_meta[str(p.get("requestId") or "")] = (
                    str(rq.get("method") or "GET"), rq.get("headers"),
                    str(rq.get("postData") or ("<body>" if rq.get("hasPostData") else "")))
            if str(p.get("type") or "") == "Document" and len(self._rid_url) < 2000:
                self._rid_url[str(p.get("requestId") or "")] = (str((p.get("request") or {}).get("url") or ""),
                                                                "Document")
            return
        if m == "Network.responseReceived":
            p = msg.get("params") or {}
            rid = str(p.get("requestId") or "")
            resp = p.get("response") or {}
            if self.budget.browser_statics and (rid in self.guard.data_meta or rid in self.guard.ext_meta):
                self.guard.reclassify(rid, str(resp.get("mimeType") or ""), int(resp.get("status") or 0))
            scope = self.budget.web_scope
            if scope is not None and scope.platform_cdn:
                # what a document-named asset host answers: a data answer ends its asset grant
                scope.note_response(self.budget.site_of(self.guard.page_host), host_of(str(resp.get("url") or "")),
                                    str(resp.get("mimeType") or ""), int(resp.get("status") or 0))
            if self.budget.cache_enabled and str(p.get("type") or "") in CACHE_TYPES and \
                    rid in self.guard.continued_ids and int(resp.get("status") or 0) == 200 and \
                    len(self._resp_meta) < 4000:
                self._resp_meta[rid] = (str(resp.get("url") or ""), str(p.get("type") or ""), 200,
                                        dict(resp.get("headers") or {}))
            return
        if m == "Network.loadingFinished":
            p = msg.get("params") or {}
            await self._cache_on_finish(str(p.get("requestId") or ""))
            host = self._rid_host.get(p.get("requestId"))
            # a request the guard answered itself (the static cache, a handoff:
            # Fetch.fulfillRequest) moved no byte over the network; Chrome still reports its
            # body as encodedDataLength, which is never booked against the site's byte
            # ceiling (booked, it gave a site 1.7 MB for 2 requests sent)
            if host and p.get("requestId") not in self.guard.fulfilled_ids:
                if self.budget.browser_statics and str(p.get("requestId") or "") in self.guard.static_ids:
                    # browser statics' bytes are counted apart: a run's byte ceiling counts documents and data
                    self.budget.add_static_bytes(host, int(p.get("encodedDataLength") or 0))
                else:
                    self.budget.add_bytes(host, int(p.get("encodedDataLength") or 0))
            if self.budget.lane_limit():
                await self.guard.release(str(p.get("requestId") or ""))
            return
        if m == "Network.loadingFailed":
            fp = msg.get("params") or {}
            if len(self.load_errors) < 200:
                u, ty = self._rid_url.get(str(fp.get("requestId") or ""), ("", str(fp.get("type") or "")))
                self.load_errors.append({"url": u[:300], "type": str(fp.get("type") or ty or ""),
                                         "errorText": str(fp.get("errorText") or "")[:120],
                                         "blockedReason": str(fp.get("blockedReason") or "")[:60],
                                         "canceled": bool(fp.get("canceled"))})
            if self.budget.lane_limit():
                await self.guard.release(str((msg.get("params") or {}).get("requestId") or ""))
            return
        if m != "Fetch.requestPaused":
            return
        params = msg.get("params") or {}
        sent = await self.guard.handle(params, self._send)
        if not sent:
            url = str((params.get("request") or {}).get("url") or "")
            if self.guard.failed and self.guard.failed[-1][1].startswith("robots_refused"):
                self.refused.append(url)

    #: seconds after an action during which the page's data requests still
    #: count as caused by it (``budget.noise_share``; the bounded settle's ceiling)
    action_tail_s: float = 5.0

    @contextlib.asynccontextmanager
    async def acting(self, tail_s: Optional[float] = None):
        """An action window: the data requests the page sends while an action
        runs (and ``action_tail_s`` after it) are the action's, not background
        noise (``RequestBudget(noise_share=True)``; nothing changes otherwise)."""
        self.guard.acting += 1
        try:
            yield
        finally:
            self.guard.acting -= 1
            tail = self.action_tail_s if tail_s is None else float(tail_s)
            self.guard.act_until = max(self.guard.act_until, self.budget.clock() + tail)

    async def goto(self, url: str) -> None:
        gate = self.budget.task_gate
        if gate is not None and not gate.holds(self.budget.site_of(host_of(url))):
            from anyuse.execution.traffic_gate import GateRefused
            raise GateRefused(f"no active task for {self.budget.site_of(host_of(url))}: "
                              f"take the run's SiteGate before navigating")
        self._seen = None
        self.guard.start(url, at=len(self._events))
        async with self.acting():
            await self.call("Page.navigate", {"url": url}, timeout=60.0)
            await self.settle(3.0)

    async def click(self, index: int) -> None:
        async with self.acting():
            await super().click(index)

    async def type(self, text: str) -> None:
        async with self.acting():
            await super().type(text)

    async def press_enter(self) -> None:
        async with self.acting():
            await super().press_enter()

    async def search(self, index: int, text: str) -> dict:
        self.note_typed(text)
        async with self.acting():
            return await super().search(index, text)

    def note_typed(self, value: str) -> None:
        """A value the run types into the site's own search box is the web
        scope's evidence for the read-only POST rule (``WebScope.note_values``,
        ``ANYUSE_ALLOW_READONLY_POST_SEARCH``): a small same-site POST whose
        search field carries exactly this value is the page's own search. The
        rule itself is unchanged (no state words, no GraphQL mutation, no
        credential header, <= 8 KB); nothing is noted without a web scope."""
        v = str(value or "").strip()
        if v and v.lower() not in self.guard.typed:
            # a static whose query carries the typed value is a data call, never a browser static
            self.guard.typed.append(v.lower())
        scope = self.budget.web_scope
        if not v or scope is None or not self.guard.page_host:
            return
        scope.note_values(self.budget.site_of(self.guard.page_host), [v])

    # -- the run cache -----------------------------------------------------

    async def harvest_cache(self) -> int:
        """Copy this load's STATIC responses that the guard SENT into the run
        cache, under the HTTP rules (``RequestBudget.remember``); a CDP read of
        the browser's buffer, nothing goes to the site. Documents, XHR/fetch and
        anything carrying an input value are never kept."""
        if not self.budget.cache_enabled:
            return 0
        reqs: Dict[str, dict] = {}
        for e in self._events:
            if e.get("method") == "Network.requestWillBeSent" and not e.get("sessionId"):
                p = e.get("params") or {}
                reqs[p.get("requestId")] = p.get("request") or {}
        n = 0
        for e in list(self._events):
            if e.get("sessionId") or e.get("method") != "Network.responseReceived":
                continue
            p = e.get("params") or {}
            rid = p.get("requestId")
            resp = p.get("response") or {}
            url = str(resp.get("url") or "")
            rtype = str(p.get("type") or "")
            if rid not in self.guard.continued_ids:
                continue
            if not self.guard._host_ok(host_of(url)) or \
                    int(resp.get("status") or 0) != 200:
                continue
            rq = reqs.get(rid) or {}
            method = str(rq.get("method") or "GET")
            post = str(rq.get("postData") or ("<body>" if rq.get("hasPostData") else ""))
            hdrs = self.guard.paused_headers.get(rid, rq.get("headers"))
            if not self.guard.cacheable(url, rtype, method, post, hdrs):
                continue
            if self.budget.lookup(method, url, hdrs) is not None:
                continue
            try:
                res = await self.call("Network.getResponseBody", {"requestId": rid},
                                      timeout=10)
            except Exception:
                continue
            if self.budget.remember(url, 200, dict(resp.get("headers") or {}),
                                    res.get("body") or "",
                                    base64_encoded=bool(res.get("base64Encoded")),
                                    rtype=rtype, method=method,
                                    request_headers=hdrs, post=post,
                                    fresh_values=tuple(self.guard.fresh_values)):
                n += 1
        return n

    # -- the typing probe ----------------------------------------------------

    #: typed search readiness: wait for the DOM to stop
    #: changing instead of the fixed timer; the timer stays the ceiling
    typed_readiness: bool = True
    #: extra seconds after Enter, ONLY while a request carrying the typed value
    #: is unanswered (0 = off: the timer is the hard ceiling, as before)
    inflight_grace_s: float = 0.0
    #: open an icon-only header search when no search input is visible
    reveal_search: bool = True
    #: when Enter moved nothing, click the search's submit control once
    submit_fallback: bool = True
    #: when Enter and the submit control moved nothing, load the URL the input's own
    #: GET form submits to (same site only); a web tool before version 14 replays
    #: without it (``type_search(form_fallback=False)``)
    form_url_fallback: bool = True

    async def _typed_wait(self, ceiling_s: float, value: str, out: dict, stage: str,
                          grace: float = 0.0, since: Optional[int] = None) -> None:
        """The shared settle rule with the page settle's ``stable_ms``, so a request without the
        value that never answers (a long poll) does not hold the stage to its ceiling. ``since``: the
        event index taken before the action; a request carrying the value sent from there on that
        answered ends the stage once the DOM is quiet."""
        if not self.typed_readiness:
            await self.settle(ceiling_s)
            return
        from anyuse.execution.typed_readiness import PAGE_SETTLE_STABLE_MS, wait_settled
        rep = await wait_settled(self, ceiling_s, value=value, inflight_grace_s=grace,
                                 stable_ms=PAGE_SETTLE_STABLE_MS, since=since)
        out.setdefault("settle", {})[stage] = rep.to_dict()

    #: main-frame navigation events that mean "Enter started a navigation"
    NAV_EVENTS = ("Page.frameRequestedNavigation", "Page.frameScheduledNavigation",
                  "Page.frameStartedNavigating", "Page.frameStartedLoading")

    async def _await_enter_navigation(self, ev0: int, sent0: int,
                                      detect_s: float = 1.0) -> dict:
        """After Enter: if the page asked for a navigation, wait until its
        document request has actually been SENT (the guard may hold it for the
        navigation spacing, ``nav_min_interval_s``), before the DOM-quiet
        readiness may end the wait. Without this, a page that is quiet because
        its navigation is still held is read as "settled" on the old URL.
        No navigation requested within ``detect_s``: an in-page search, return.
        The ceiling is the spacing plus two seconds."""
        t0 = time.monotonic()
        ceiling = detect_s + float(self.budget.nav_min_interval_s or 0.0) + 2.0
        requested = False
        while time.monotonic() - t0 < ceiling:
            if not requested:
                requested = any(e.get("method") in self.NAV_EVENTS and not e.get("sessionId")
                                for e in self._events[ev0:])
                if not requested and time.monotonic() - t0 >= detect_s:
                    return {"requested": False, "waited_s": round(time.monotonic() - t0, 3)}
            if requested and any(ty == "Document" for (_h, _t, _u, ty)
                                 in self.budget.sent[sent0:]):
                return {"requested": True, "sent": True,
                        "waited_s": round(time.monotonic() - t0, 3)}
            await self.settle(0.1)
        return {"requested": requested, "sent": False,
                "waited_s": round(time.monotonic() - t0, 3)}

    async def submit_search(self, text: str) -> None:
        self.note_typed(text)
        async with self.acting():
            await self._submit_search(text)

    async def _submit_search(self, text: str) -> None:
        """The agent's ``search`` transaction in a guarded session: Enter,
        wait until a navigation it asked for was actually SENT (the guard may
        hold it for the navigation spacing), then until the page settles with
        no value-bearing request unanswered; when nothing moved, click the
        search's own submit control once and wait the same way."""
        start = await self.url()
        ev0, sent0 = len(self._events), len(self.budget.sent)
        for kind in ("rawKeyDown", "char", "keyUp"):
            await self.call("Input.dispatchKeyEvent",
                            {"type": kind, "key": "Enter", "code": "Enter",
                             "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                             "text": "\r" if kind == "char" else ""})
        info: dict = {}
        nav = await self._await_enter_navigation(ev0, sent0)
        await self._typed_wait(5.0, text, info, "enter", grace=self.inflight_grace_s, since=ev0)
        moved = (nav.get("requested") or await self.url() != start or any(
            ty in ("Document", "XHR", "Fetch") for (_h, _t, _u, ty) in self.budget.sent[sent0:]))
        if not moved and self.submit_fallback:
            res = await self.click_submit(-1)
            if res.get("clicked"):
                self.submitted_by_button = str(res.get("how") or "button")
                ev1, sent1 = len(self._events), len(self.budget.sent)
                await self._await_enter_navigation(ev1, sent1)
                await self._typed_wait(5.0, text, info, "submit", grace=self.inflight_grace_s,
                                       since=ev1)

    #: how many text inputs the measured typing tries, and how many reveal candidates are clicked, at most
    MEASURE_INPUTS = 3
    REVEAL_TRIES = 3

    async def text_inputs(self) -> List[dict]:
        """Every text input of the page with its ``rank`` (``SEARCH_INPUTS_JS``)."""
        return [_ranked(i) for i in list(await self.js(SEARCH_INPUTS_JS) or []) if isinstance(i, dict)]

    async def search_inputs(self) -> List[dict]:
        """The search-like inputs: by the page's structure or by their names (ranks 0 and 2)."""
        return [i for i in await self.text_inputs() if i.get("rank", 2) < 3]

    async def recorded_inputs(self, want: dict, named14: bool = False) -> List[dict]:
        """The inputs a typed replay of the recorded input ``want`` chooses among (:func:`replay_inputs`), and first
        the text field the accessibility tree called a search box when ``want`` was recorded that way (``ax``) and
        the rules miss it. A local read: nothing is typed."""
        inputs = replay_inputs(await self.text_inputs(), want, named14)
        if want.get("ax") and not any(i.get("index") == want.get("index") for i in inputs):
            # a text field the accessibility tree calls a search box (its role, its landmark, its name or
            # its own form's search button) that the page's search-like heuristics missed
            try:
                got = await self.js(AX_INPUT_JS % int(want.get("index", -1)))
            except Exception:                          # noqa: BLE001 - not typable is "not on the page"
                got = None
            if isinstance(got, dict):
                inputs = [got] + inputs
        return inputs

    async def _typing_shows(self, pick: dict, value: str, out: dict) -> str:
        """Type ``value`` into input ``pick`` and say what the page did with it, or ``""``: a request carrying the
        typed value, its text now showing the value (suggestions, live results), or its own GET form submitting it
        to this site. The value stays typed."""
        from anyuse.execution import web_scope as WSC
        idx = int(pick["index"])
        try:
            shown_before = bool(await self.js(TEXT_SHOWS_JS % json.dumps(str(value).lower()), read_only=True))
        except Exception:                              # noqa: BLE001 - unreadable: no evidence from the text
            shown_before = True
        if not await self.js(FOCUS_INPUT_JS % idx):
            return ""
        before = len(self.budget.sent)
        out["_from"] = (before, len(self.guard.failed))
        typed_at = len(self._events)
        await self.call("Input.insertText", {"text": str(value)})
        await self._typed_wait(3.0, value, out, "typed", since=typed_at)
        if any(WSC.carried_value(u, values=[value]) for (_h, _t, u, ty) in self.budget.sent[before:]
               if ty != "Document"):
            return "a request the page sent carries the typed value"
        try:
            if not shown_before and await self.js(TEXT_SHOWS_JS % json.dumps(str(value).lower()), read_only=True):
                return "the page shows the typed value"
        except Exception:                              # noqa: BLE001
            pass
        start = await self.url()
        if await self.form_get_url(idx, start, value):
            return "its own GET form submits the typed value to this site"
        return ""

    async def _choose_input(self, value: str, inputs: List[dict], out: dict) -> Tuple[Optional[dict], bool]:
        """The search box among the visible text ``inputs`` and whether ``value`` is already typed into it: by the
        page's structure (rank 0); else, with a choice to make, the input whose typing the page answers
        (:meth:`_typing_shows`, inputs named like a search tried first); else one named like a search (the last
        rung). ``out["chosen_by"]`` says which."""
        vis = [i for i in inputs if i.get("visible")]
        structural = [i for i in vis if i.get("rank", 2) == 0]
        if structural:
            out["chosen_by"] = "structure"
            return structural[0], False
        named = [i for i in vis if i.get("rank", 2) == 2]
        if len(vis) == 1 and named:
            out["chosen_by"] = "name"
            return named[0], False
        for cand in sorted(vis, key=lambda i: (i.get("rank", 2), int(i.get("index", 0))))[:self.MEASURE_INPUTS]:
            why = await self._typing_shows(cand, value, out)
            if why:
                out["chosen_by"] = f"typing: {why}"
                return cand, True
            await self.js(FOCUS_INPUT_JS % int(cand["index"]))         # empties it again
        if named:
            out["chosen_by"] = "name"
            return named[0], False
        return None, False

    async def _reveal(self, value: str, out: dict, shown: Sequence[dict] = ()) -> List[dict]:
        """Open a search that is only an icon: click the page's candidates (``REVEAL_SEARCH_JS``) one at a time
        until a text input that was hidden is visible (``shown``: the inputs read before); the inputs then, the
        newly shown first (``[]``: none)."""
        seen = {i.get("index") for i in shown if i.get("visible")}
        for k in range(self.REVEAL_TRIES):
            rev = await self.js(REVEAL_SEARCH_JS % k)
            if not isinstance(rev, dict) or not rev.get("clicked"):
                return []
            out["revealed"] = {key: rev.get(key) for key in ("label", "navigates")}
            if rev.get("navigates"):
                await self.settle(float(self.budget.nav_min_interval_s or 0.0) + 3.0)
            if self.typed_readiness:
                await self._typed_wait(3.0, "", out, "revealed")
            else:
                await self.settle(1.5)
            now = [i for i in await self.text_inputs() if i.get("visible")]
            new = [i for i in now if i.get("index") not in seen]
            if new or rev.get("navigates"):
                return new + [i for i in now if i not in new]
            if int(rev.get("count") or 0) <= k + 1:
                break
        return []

    async def type_search(self, value: str, enter_settle_s: float = 5.0,
                          want: Optional[dict] = None,
                          form_fallback: Optional[bool] = None, named14: bool = False) -> dict:
        self.note_typed(value)
        async with self.acting():
            return await self._type_search(value, enter_settle_s=enter_settle_s, want=want,
                                           form_fallback=form_fallback, named14=named14)

    async def _type_search(self, value: str, enter_settle_s: float = 5.0,
                           want: Optional[dict] = None,
                           form_fallback: Optional[bool] = None, named14: bool = False) -> dict:
        """Type ``value`` into the page's search box (:meth:`_choose_input`; when no input is one, a search
        that is only an icon is opened first, :meth:`_reveal`), record the
        same-host XHR/fetch URLs sent while typing, press Enter, read where the
        page went. Choosing the box may type ``value`` into up to
        :attr:`MEASURE_INPUTS` text inputs (each emptied again) and click up to
        :attr:`REVEAL_TRIES` controls; nothing else is clicked or typed.

        ``want`` (typed replay): the input recorded at discovery
        (name/id/index); the visible input with the same name or id is used
        (whatever the page's search-like rules call it now), else the
        search-like one at the same index, else nothing is typed
        (:meth:`recorded_inputs`, :func:`recorded_input`; ``named14``: a web
        tool before version 15, which also finds the inputs its own rule
        named and reads the submit words of its version, ``SUBMIT_WORDS_V14``).
        Its ``submit_label`` is the control a demonstration clicked.

        When Enter moved nothing: the search's own submit control
        (:meth:`click_submit`), then, with ``form_fallback`` (default
        :attr:`form_url_fallback`), the input's own GET form URL. ``False``
        keeps the rule of web tools before version 14. ``out`` says whether the
        page moved (``moved``) and which documents it loaded (``documents``)."""
        form_fallback = self.form_url_fallback if form_fallback is None else bool(form_fallback)
        out = {"input": None, "xhr": [], "document": "", "note": ""}
        typed = False
        if want:
            # the recorded input by its own name or id stays a candidate when the search-like rules no longer name
            # it (a tool recorded under an earlier rule); the index fallback reads the search-like inputs only
            inputs = await self.recorded_inputs(want, named14)
        else:
            read = await self.text_inputs()
            pick, typed = await self._choose_input(value, read, out)
            if pick is None and self.reveal_search:
                # a header search that is only an icon
                shown = await self._reveal(value, out, read)
                pick, typed = await self._choose_input(value, shown, out) if shown else (None, False)
            inputs = [pick] if pick is not None else []
        before, nfail = out.pop("_from", (len(self.budget.sent), len(self.guard.failed)))
        if not inputs:
            out["note"] = "no visible search input"
            return out
        pick = inputs[0]
        if want:
            pick = recorded_input(inputs, want)
            if pick is None:
                out["note"] = "the recorded search input is not on the page"
                return out
        out["input"] = pick
        if not typed:
            if self.typed_readiness:
                # let the page finish taking over its input before typing (ceiling 2 s)
                await self._typed_wait(2.0, "", out, "before")
            if not await self.js(FOCUS_INPUT_JS % int(pick["index"])):
                out["note"] = "could not focus the input"
                return out
            before, nfail = len(self.budget.sent), len(self.guard.failed)
            typed_at = len(self._events)
            await self.call("Input.insertText", {"text": str(value)})
            await self._typed_wait(3.0, value, out, "typed", since=typed_at)
        if self.typed_readiness:
            # the page's scripts may take over the input after the value was
            # typed and wipe it; then the search would run without it. Check
            # the input, and type once more (the same value, nothing else).
            from anyuse.execution.typed_readiness import INPUT_VALUE_JS
            try:
                held = await self.js(INPUT_VALUE_JS % int(pick["index"]))
            except Exception:
                held = None
            if isinstance(held, str) and str(value).strip().lower() not in held.lower():
                out["retyped"] = True
                if await self.js(FOCUS_INPUT_JS % int(pick["index"])):
                    typed_at = len(self._events)
                    await self.call("Input.insertText", {"text": str(value)})
                    await self._typed_wait(3.0, value, out, "retyped", since=typed_at)
        out["xhr"] = [u for (_h, _t, u, ty) in self.budget.sent[before:] if ty in ("XHR", "Fetch")]
        start = await self.url()
        ev0, sent0 = len(self._events), len(self.budget.sent)
        for kind in ("rawKeyDown", "char", "keyUp"):
            await self.call("Input.dispatchKeyEvent",
                            {"type": kind, "key": "Enter", "code": "Enter",
                             "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                             "text": "\r" if kind == "char" else ""})
        if self.typed_readiness:
            out["enter_navigation"] = await self._await_enter_navigation(
                ev0, sent0)
        await self._typed_wait(enter_settle_s, value, out, "enter",
                               grace=self.inflight_grace_s, since=ev0)
        after = await self.url()
        nav = out.get("enter_navigation") or {}
        moved = (after != start or nav.get("requested") or any(
            ty in ("Document", "XHR", "Fetch") for (_h, _t, _u, ty) in self.budget.sent[sent0:]))
        if not moved and self.submit_fallback:
            # Enter did nothing (no navigation, no request, same URL):
            # click the search's own submit control once
            res = await self.click_submit(int(pick["index"]),
                                          prefer_label=str((want or {}).get("submit_label") or ""),
                                          legacy=not form_fallback, words14=named14)
            out["submit"] = res if isinstance(res, dict) else {"clicked": False}
            if out["submit"].get("clicked"):
                ev1, sent1 = len(self._events), len(self.budget.sent)
                if self.typed_readiness:
                    out["submit_navigation"] = await self._await_enter_navigation(ev1, sent1)
                await self._typed_wait(enter_settle_s, value, out, "submit",
                                       grace=self.inflight_grace_s, since=ev1)
                after = await self.url()
                moved = after != start or any(ty in ("Document", "XHR", "Fetch") for (_h, _t, _u, ty)
                                              in self.budget.sent[sent1:])
        if not moved and form_fallback:
            # the submit control moved nothing either: the input's own GET form, as the browser submits it
            url = await self.form_get_url(int(pick["index"]), start, value)
            if url:
                out["form_url"] = url
                ev2 = len(self._events)
                await self.goto(url)
                await self._typed_wait(enter_settle_s, value, out, "form", since=ev2)
                after = await self.url()
                moved = after != start
        if form_fallback:
            out["moved"] = bool(moved)
            out["documents"] = [u for (_h, _t, u, ty) in self.budget.sent[sent0:]
                                if ty == "Document"][:6]
        out["document"] = after if (after != start and after.startswith(("http://", "https://"))) \
            else ""
        out["refused"] = [list(f) for f in self.guard.failed[nfail:][:5]]
        out["xhr"] += [u for (_h, _t, u, ty) in self.budget.sent[before:]
                       if ty in ("XHR", "Fetch") and u not in out["xhr"]]
        return out
