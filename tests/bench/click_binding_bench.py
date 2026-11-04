"""Offline A/B bench: does the step agent click the element the model saw?

The step agent observes a page, asks the model, and then acts on the index the
model chose. Each case here is a fixture page whose layout changes on its own
clock (a banner or an ad slot that loads late, a newsletter box that goes
away, a suggestion list that re-sorts, a feed that prepends rows, a list that
re-renders its nodes). The pages run in a DOM stand-in inside Node, and the
tree's own :class:`CdpAgentSession` and :class:`StepAgent` drive them: the
session's CDP calls are answered by the stand-in (Runtime.evaluate,
Runtime.callFunctionOn, Runtime.releaseObjectGroup, Page.navigate,
Input.insertText), so the observe and pick scripts that run are the tree's
own. The page clock advances only when the session settles and while the
model "thinks". No socket is opened, no browser is started, no model is called.

The model is a scripted policy: per scenario a fixed list of goals (click the
element named X, type into the input named Y), each with a proof text that
says it was reached. At each step it acts on the first goal not yet proved,
on the element of that name in the observation it was given, then takes a
seeded 0.8 to 3.0 s to answer. The element the model meant is read from the
page at the moment of the observation; the element acted on is read from the
stand-in's click and focus log.

Usage::

    python tests/bench/click_binding_bench.py run --old <tree> --new <tree> \\
        [--python <interpreter>] [--node <node binary>] [--out <dir>] [--seeds N]
    python tests/bench/click_binding_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the feature branch forked from, ``--new``
the feature checkout. The script copies itself into ``--out`` (outside both
trees) and runs that copy once per tree. Each run's PYTHONPATH is a pkgroot
directory holding an ``anyuse`` symlink to the tree, then the tree, then its
``tools/guarded_tests`` (whose sitecustomize refuses sockets). HOME and every
AnyUse data path point into ``--out``, and no ``ANYUSE_*`` variable is passed
through.

Scenarios (25 seeds each by default):

* ``static``: a shop (Home, Products, About, Contact; four products). Nothing
  changes. Goals: Products, then Widget B.
* ``banner``: the shop; on every page a "Special offer" link is put at the top
  of the page after U(0, 8) s.
* ``ads``: the shop; an ad slot with two off-site links loads into the nav
  after U(0, 8) s. An off-site click ends on the browser's error page, as the
  agent's stay-on-site gate leaves it.
* ``dismiss``: the shop with a newsletter box (two buttons) above the nav that
  removes itself after U(0, 8) s.
* ``skeleton``: the shop; after U(0, 8) s the nav shows four "Loading"
  placeholders for 1.5 s, then is rendered again as new nodes.
* ``typeahead_reuse`` / ``typeahead_replace``: a search input; 0.3 s after
  typing, four suggestions appear, and 0.2 to 2.7 s later they re-sort, by
  relabelling the same nodes or by replacing them. Goals: type into
  "Search products", then click "Widget B".
* ``feed``: eight story links; a "Breaking news" row is prepended every 1.5 to
  4 s. Goal: Story 3.
* ``rows_rerender``: five rows of (link "Row n", button "Details"); every 1 to
  3 s the list is rendered again as new, equal nodes. Goal: the Details of
  Row 3.
* ``rows_prepend``: the same rows; after U(0, 8) s a row 0 is added on top and
  the list is rendered again as new nodes.
* ``placeholder``: the typeahead page, whose search input changes its
  placeholder every 0.4 to 1.2 s (Search products, Search widgets, Search
  gifts). Goals: type into the input whose name starts with "Search", then
  click "Widget B".
* ``equal_buttons``: a box of four "View" buttons between two links; after
  U(0, 8) s a "Promo" link is added on top and the box is rendered again as
  new nodes. Goal: the third View.

Metrics per arm and scenario: completion (the last proof is on the final
page), steps, index actions, wrong actions (the element acted on is not the
one the model meant), refused actions (nothing acted on), wrong actions per
100 steps, observations per task, and for the new tree how each pick was
resolved. Paired per case: extra observations (new minus old).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

START = "https://shop.test/"
SEEDS = 25
MAX_STEPS = 10
THINK_S = (0.8, 3.0)

#: the DOM stand-in and CDP answerer (one JSON request per line on stdin, one
#: answer per line on stdout); timers run on a page clock that only ``advance``
#: moves; object handles die with their document, as in Chrome
DOM_JS = r"""
'use strict';
const vm = require('vm');
let doc = null, ctx = null, gen = 0, handles = new Map(), nextId = 1, log = [], pendingNav = null;
let builder = '', cfg = {}, clock = 0, timers = [], timerId = 1, rng = 1, host = '';
const BLOCK = new Set(['HTML', 'BODY', 'DIV', 'UL', 'LI', 'P', 'NAV', 'SECTION', 'MAIN', 'HEADER', 'FORM']);
function rand() { rng = (rng + 0x6D2B79F5) | 0; let t = Math.imul(rng ^ (rng >>> 15), 1 | rng);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }
function parseSel(sel) {
  return sel.split(',').map(s => s.trim()).filter(Boolean).map(s => {
    const m = /^([a-zA-Z0-9]*)(.*)$/.exec(s), out = {tag: m[1].toUpperCase(), has: [], not: []};
    const re = /(:not\()?\[([^\]=]+)(?:=([^\]]+))?\]\)?/g;
    let a;
    while ((a = re.exec(m[2]))) (a[1] ? out.not : out.has).push(
        {k: a[2], v: a[3] === undefined ? null : a[3].replace(/^['"]|['"]$/g, '')});
    return out;
  });
}
const attrOk = (el, a) => el.hasAttribute(a.k) && (a.v === null || el.getAttribute(a.k) === a.v);
const matches = (el, sel) => parseSel(sel).some(p => (!p.tag || el.tagName === p.tag) &&
    p.has.every(a => attrOk(el, a)) && !p.not.some(a => attrOk(el, a)));
class El {
  constructor(tag, attrs, text) {
    this.tagName = String(tag).toUpperCase(); this.attrs = {}; this.lid = null;
    for (const [k, v] of Object.entries(attrs || {})) { if (k === 'lid') this.lid = v; else this.attrs[k] = String(v); }
    this.text = text || ''; this.children = []; this.parentNode = null; this.hidden = false;
    this.listeners = {}; this._value = null; this.form = null;
  }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs['class'] || ''; }
  get placeholder() { return ['INPUT', 'TEXTAREA'].includes(this.tagName) ? (this.attrs.placeholder || '') : undefined; }
  get value() { return ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(this.tagName) ?
      (this._value !== null ? this._value : (this.attrs.value || '')) : undefined; }
  set value(v) { this._value = String(v); }
  get innerText() {
    if (this.isHidden()) return '';
    return [this.text, ...this.children.map(c => c.innerText)].filter(Boolean).join(BLOCK.has(this.tagName) ? '\n' : ' ');
  }
  set textContent(t) { this.text = String(t); this.children.forEach(c => { c.parentNode = null; }); this.children = []; }
  isHidden() { for (let n = this; n; n = n.parentNode) if (n.hidden) return true; return false; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === doc.root; }
  getBoundingClientRect() { const z = this.isHidden() || !this.isConnected; return {width: z ? 0 : 120, height: z ? 0 : 24, top: 0, left: 0}; }
  closest(sel) { for (let n = this; n && n !== doc.root; n = n.parentNode) if (matches(n, sel)) return n; return null; }
  matches(sel) { return matches(this, sel); }
  querySelectorAll(sel) { const out = []; const walk = n => n.children.forEach(c => { if (matches(c, sel)) out.push(c); walk(c); }); walk(this); return out; }
  appendChild(c) { c.remove(); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) { c.remove(); const i = ref ? this.children.indexOf(ref) : -1; c.parentNode = this;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  prepend(c) { return this.insertBefore(c, this.children[0] || null); }
  remove() { const p = this.parentNode; if (p) { p.children.splice(p.children.indexOf(this), 1); this.parentNode = null; } }
  replaceWith(c) { const p = this.parentNode; if (p) { p.insertBefore(c, this); this.remove(); } }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, extra) {
    const ev = Object.assign({type: t, target: this, defaultPrevented: false}, extra || {});
    ev.preventDefault = () => { ev.defaultPrevented = true; };
    for (let n = this; n; n = n.parentNode) (n.listeners[t] || []).forEach(fn => fn(ev));
    return ev;
  }
  scrollIntoView() {}
  focus() { doc.active = this; log.push({act: 'focus', lid: this.lid, t: clock}); }
  click() {
    log.push({act: 'click', lid: this.lid, t: clock});
    const ev = this.dispatch('click'), a = this.closest('a[href]');
    if (a && !ev.defaultPrevented) pendingNav = new URL(a.getAttribute('href'), doc.url).href;
  }
  get outerHTML() {
    const at = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
    const t = this.tagName.toLowerCase();
    return `<${t}${at}>${this.text}${this.children.map(c => c.outerHTML).join('')}</${t}>`;
  }
}
function h(tag, attrs, text, kids) { const e = new El(tag, attrs, text); (kids || []).forEach(k => e.appendChild(k)); return e; }
function navigate(url) {
  gen += 1; handles = new Map(); timers = [];
  let u = url;
  try { if (new URL(url).host !== host) u = 'chrome-error://chromewebdata/'; } catch (e) { u = 'chrome-error://chromewebdata/'; }
  doc = {url: u, root: new El('html'), body: new El('body'), active: null, title: ''};
  doc.root.appendChild(doc.body);
  const g = gen;
  const document = {
    get body() { return doc.body; }, get documentElement() { return doc.root; },
    get title() { return doc.title; }, set title(t) { doc.title = String(t); },
    get activeElement() { return doc.active || doc.body; },
    querySelectorAll: sel => doc.root.querySelectorAll(sel), createElement: tag => new El(tag),
  };
  const later = (fn, ms) => { const id = timerId++; timers.push({at: clock + Math.max(0, ms || 0), fn: fn, id: id, g: g}); return id; };
  const sandbox = {
    document: document, h: h, URL: URL, console: console, rand: rand, CFG: cfg,
    location: {get href() { return doc.url; }, assign: v => { pendingNav = new URL(v, doc.url).href; }},
    setTimeout: later, clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
  };
  sandbox.window = sandbox;
  ctx = vm.createContext(sandbox);
  if (u.startsWith('chrome-error:')) return;
  vm.runInContext(builder, ctx);
  vm.runInContext('page(location.href)', ctx);
}
function flushNav() { while (pendingNav) { const u = pendingNav; pendingNav = null; navigate(u); } }
function advance(ms) {
  const end = clock + ms;
  for (;;) {
    const due = timers.filter(t => t.g === gen && t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    timers = timers.filter(t => t !== due);
    clock = due.at;
    due.fn();
    flushNav();
  }
  clock = end;
}
function byValue(v) {
  if (v === undefined) return {type: 'undefined'};
  try { return {type: typeof v, value: JSON.parse(JSON.stringify(v))}; } catch (e) { return {type: 'object'}; }
}
function keep(v) { const id = `${gen}.${nextId++}`; handles.set(id, v); return {type: 'object', objectId: id}; }
function cdp(method, p) {
  if (method === 'Page.navigate') { navigate(p.url); return {frameId: 'F'}; }
  if (method === 'Runtime.releaseObjectGroup') { handles = new Map(); return {}; }
  if (method === 'Runtime.evaluate') {
    try {
      const v = vm.runInContext(p.expression, ctx);
      return {result: p.returnByValue === false && v && typeof v === 'object' ? keep(v) : byValue(v)};
    } catch (e) { return {result: {type: 'object', subtype: 'error'}, exceptionDetails: {text: String(e)}}; }
  }
  if (method === 'Runtime.callFunctionOn') {
    if (!handles.has(p.objectId)) throw {code: -32000, message: 'Cannot find context with specified id'};
    const args = (p.arguments || []).map(a => 'objectId' in a ? handles.get(a.objectId) : a.value);
    try {
      const fn = vm.runInContext('(' + p.functionDeclaration + ')', ctx);
      const v = fn.apply(handles.get(p.objectId), args);
      return {result: p.returnByValue ? byValue(v) : keep(v)};
    } catch (e) { return {result: {type: 'object', subtype: 'error'}, exceptionDetails: {text: String(e)}}; }
  }
  if (method === 'Input.insertText') {
    const el = doc.active;
    if (el && el.value !== undefined) { el.value = el.value + p.text; el.dispatch('input'); }
    return {};
  }
  return {};
}
require('readline').createInterface({input: process.stdin}).on('line', line => {
  const q = JSON.parse(line);
  let out = {};
  try {
    if (q.op === 'load') {
      builder = q.builder; cfg = q.cfg || {}; clock = 0; timers = []; log = []; rng = q.seed | 0;
      pendingNav = null; host = new URL(q.url).host; navigate(q.url);
    } else if (q.op === 'cdp') out.result = cdp(q.method, q.params || {});
    else if (q.op === 'advance') advance(q.ms);
    else if (q.op === 'eval') out.value = byValue(vm.runInContext(q.js, ctx)).value;
    else if (q.op === 'log') { out.log = log; log = []; }
    else if (q.op === 'html') out.html = '<!doctype html>' + doc.root.outerHTML;
    else if (q.op === 'text') out.text = doc.body.innerText;
    flushNav();
  } catch (e) { out = {error: e && e.message ? {code: e.code || -1, message: e.message} : String(e)}; }
  out.clock = clock;
  process.stdout.write(JSON.stringify(out) + '\n');
});
"""

SHOP = r"""
function nav() {
  return h('nav', {}, '', [
    h('a', {href: '/', lid: 'home'}, 'Home'), h('a', {href: '/products', lid: 'products'}, 'Products'),
    h('a', {href: '/about', lid: 'about'}, 'About'), h('a', {href: '/contact', lid: 'contact'}, 'Contact')]);
}
function page(url) {
  const path = new URL(url).pathname;
  document.title = 'Shop ' + path;
  if (CFG.dismiss) {
    const box = h('div', {id: 'newsletter'}, 'Get our newsletter', [
      h('button', {lid: 'nothanks'}, 'No thanks'), h('button', {lid: 'subscribe'}, 'Subscribe')]);
    document.body.appendChild(box);
    setTimeout(() => box.remove(), rand() * 8000);
  }
  document.body.appendChild(nav());
  const main = h('main', {}, '');
  if (path === '/products') {
    main.appendChild(h('p', {}, 'Our products'));
    ['A', 'B', 'C', 'D'].forEach(x => main.appendChild(
      h('a', {href: '/product/' + x.toLowerCase(), lid: 'product-' + x}, 'Widget ' + x)));
  } else if (path.startsWith('/product/')) {
    main.appendChild(h('p', {}, 'Widget ' + path.slice(9).toUpperCase() + ' details'));
  } else {
    main.appendChild(h('p', {}, 'Welcome: ' + path));
  }
  document.body.appendChild(main);
  if (CFG.banner) setTimeout(() => document.body.prepend(
    h('a', {href: '/offer', lid: 'offer'}, 'Special offer: 10% off')), rand() * 8000);
  if (CFG.skeleton) setTimeout(() => {
    const n = document.querySelectorAll('nav')[0];
    n.textContent = '';
    [0, 1, 2, 3].forEach(i => n.appendChild(h('a', {href: '#', lid: 'loading' + i}, 'Loading')));
    setTimeout(() => n.replaceWith(nav()), 1500);
  }, rand() * 8000);
  if (CFG.ads) setTimeout(() => {
    const n = document.querySelectorAll('nav')[0];
    n.insertBefore(h('div', {id: 'ad'}, '', [
      h('a', {href: 'https://ads.test/flights', lid: 'ad-flights'}, 'Cheap flights'),
      h('a', {href: 'https://ads.test/casino', lid: 'ad-casino'}, 'Play now')]), n.children[1]);
  }, rand() * 8000);
}
"""

TYPEAHEAD = r"""
function opt(x) {
  const o = h('div', {role: 'option', lid: 'opt-' + x}, 'Widget ' + x);
  o.addEventListener('click', () => location.assign('/product/' + o.lid.slice(4).toLowerCase()));
  return o;
}
function page(url) {
  const path = new URL(url).pathname;
  document.title = 'Store';
  const input = h('input', {type: 'text', placeholder: 'Search products', lid: 'search'});
  document.body.appendChild(h('header', {}, '', [h('a', {href: '/', lid: 'home'}, 'Home'), input]));
  if (CFG.rotate) {
    const tips = ['Search products', 'Search widgets', 'Search gifts'];
    let k = 0;
    const spin = () => { k += 1; input.setAttribute('placeholder', tips[k % 3]); setTimeout(spin, 400 + rand() * 800); };
    setTimeout(spin, 400 + rand() * 800);
  }
  const lb = h('div', {role: 'listbox', id: 'suggestions'});
  document.body.appendChild(lb);
  if (path.startsWith('/product/')) document.body.appendChild(
    h('p', {}, 'Widget ' + path.slice(9).toUpperCase() + ' details'));
  input.addEventListener('input', () => setTimeout(() => {
    lb.textContent = '';
    ['A', 'B', 'C', 'D'].forEach(x => lb.appendChild(opt(x)));
    setTimeout(() => {
      const order = ['A', 'B', 'C', 'D'].map(x => [rand(), x]).sort().map(p => p[1]);
      if (CFG.reuse) {
        const o = lb.children;
        order.forEach((x, k) => { o[k].textContent = 'Widget ' + x; o[k].lid = 'opt-' + x; });
      } else {
        lb.textContent = '';
        order.forEach(x => lb.appendChild(opt(x)));
      }
    }, 200 + rand() * 2500);
  }, 300));
}
"""

FEED = r"""
function page(url) {
  const path = new URL(url).pathname;
  document.title = 'News';
  if (path.startsWith('/story/')) { document.body.appendChild(h('p', {}, 'Story ' + path.slice(7) + ' full text')); return; }
  if (path.startsWith('/breaking/')) { document.body.appendChild(h('p', {}, 'Breaking ' + path.slice(10))); return; }
  const list = h('ul', {id: 'feed'});
  [1, 2, 3, 4, 5, 6, 7, 8].forEach(n => list.appendChild(
    h('li', {}, '', [h('a', {href: '/story/' + n, lid: 'story' + n}, 'Story ' + n)])));
  document.body.appendChild(list);
  let k = 0;
  const tick = () => {
    k += 1;
    list.prepend(h('li', {}, '', [h('a', {href: '/breaking/' + k, lid: 'breaking' + k}, 'Breaking news ' + k)]));
    setTimeout(tick, 1500 + rand() * 2500);
  };
  setTimeout(tick, 1500 + rand() * 2500);
}
"""

ROWS = r"""
function render(list, ns) {
  list.textContent = '';
  ns.forEach(n => {
    const b = h('button', {lid: 'details' + n}, 'Details');
    b.addEventListener('click', () => location.assign('/row/' + n + '/details'));
    list.appendChild(h('li', {}, '', [h('a', {href: '/row/' + n, lid: 'row' + n}, 'Row ' + n), b]));
  });
}
function page(url) {
  const path = new URL(url).pathname;
  document.title = 'Results';
  let m = /^\/row\/(\d+)(\/details)?$/.exec(path);
  if (m) { document.body.appendChild(h('p', {}, 'Row ' + m[1] + (m[2] ? ' details' : ' summary'))); return; }
  const list = h('ul', {id: 'rows'});
  let ns = [1, 2, 3, 4, 5];
  render(list, ns);
  document.body.appendChild(list);
  if (CFG.rerender) {
    const again = () => { render(list, ns); setTimeout(again, 1000 + rand() * 2000); };
    setTimeout(again, 1000 + rand() * 2000);
  }
  if (CFG.prepend) setTimeout(() => { ns = [0].concat(ns); render(list, ns); }, rand() * 8000);
}
"""

EQUAL = r"""
function box(promo) {
  const b = h('div', {id: 'box'});
  if (promo) b.appendChild(h('a', {href: '/promo', lid: 'promo'}, 'Promo'));
  b.appendChild(h('a', {href: '/', lid: 'items'}, 'Items'));
  [1, 2, 3, 4].forEach(n => {
    const v = h('button', {lid: 'view' + n}, 'View');
    v.addEventListener('click', () => location.assign('/item/' + n));
    b.appendChild(v);
  });
  b.appendChild(h('a', {href: '/help', lid: 'help'}, 'Help'));
  return b;
}
function page(url) {
  const path = new URL(url).pathname;
  document.title = 'Items';
  const m = /^\/item\/(\d+)$/.exec(path);
  if (m) { document.body.appendChild(h('p', {}, 'Item ' + m[1] + ' page')); return; }
  const b = box(false);
  document.body.appendChild(b);
  setTimeout(() => b.replaceWith(box(true)), rand() * 8000);
}
"""

#: (builder, page config, goals). A goal is (action, element name, the element
#: it follows or "", the text that proves it was reached, text to type). A name
#: ending in "*" matches every name that starts with the rest; "View#2" is the
#: third element named "View".
SHOP_GOALS = [("click", "Products", "", "Our products", ""),
              ("click", "Widget B", "", "Widget B details", "")]
TYPE_GOALS = [("type", "Search products", "", "@Widget B", "wid"),
              ("click", "Widget B", "", "Widget B details", "")]
SCENARIOS: Dict[str, Any] = {
    "static": (SHOP, {}, SHOP_GOALS),
    "banner": (SHOP, {"banner": True}, SHOP_GOALS),
    "ads": (SHOP, {"ads": True}, SHOP_GOALS),
    "dismiss": (SHOP, {"dismiss": True}, SHOP_GOALS),
    "skeleton": (SHOP, {"skeleton": True}, SHOP_GOALS),
    "typeahead_reuse": (TYPEAHEAD, {"reuse": True}, TYPE_GOALS),
    "typeahead_replace": (TYPEAHEAD, {}, TYPE_GOALS),
    "feed": (FEED, {}, [("click", "Story 3", "", "Story 3 full text", "")]),
    "rows_rerender": (ROWS, {"rerender": True}, [("click", "Details", "Row 3", "Row 3 details", "")]),
    "rows_prepend": (ROWS, {"prepend": True}, [("click", "Details", "Row 3", "Row 3 details", "")]),
    "placeholder": (TYPEAHEAD, {"rotate": True}, [("type", "Search*", "", "@Widget B", "wid"),
                                                  ("click", "Widget B", "", "Widget B details", "")]),
    "equal_buttons": (EQUAL, {}, [("click", "View#2", "", "Item 3 page", "")]),
}


def named(name: str, want: str) -> bool:
    """Whether an element name matches a goal's element name (see SCENARIOS)."""
    want = want.split("#", 1)[0]
    return name.startswith(want[:-1]) if want.endswith("*") else name == want


class Dom:
    """The Node process holding the page of the current case."""

    def __init__(self, node: str):
        self.proc = subprocess.Popen([node, "-e", DOM_JS], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)

    def op(self, op: str, **kw) -> Dict[str, Any]:
        self.proc.stdin.write(json.dumps(dict(kw, op=op)) + "\n")
        self.proc.stdin.flush()
        out = json.loads(self.proc.stdout.readline())
        if out.get("error") and op != "cdp":
            raise RuntimeError(f"{op}: {out['error']}")
        return out

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def session_class(dom: Dom, record: Dict[str, Any]):
    from anyuse.execution.agent_cdp import CdpAgentSession

    class BenchSession(CdpAgentSession):
        def __init__(self, endpoint, allow_url=None, **kw):
            super().__init__(endpoint, allow_url=allow_url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def _pace(self, url):
            return None

        async def _call(self, method, params, timeout):
            out = dom.op("cdp", method=method, params=params or {})
            if out.get("error"):
                raise RuntimeError(f"{method}: {out['error']}")
            return out.get("result") or {}

        async def settle(self, seconds):
            dom.op("advance", ms=int(seconds * 1000))

        async def observe(self):
            record["observes"] += 1
            return await super().observe()

        async def focus(self, index):
            ok = await super().focus(index)
            self._note()
            return ok

        async def click(self, index):
            await super().click(index)
            self._note()

        def _note(self):
            last = getattr(self, "last_pick", None)
            if isinstance(last, dict) and last:
                record["picks"].append(str(last.get("how") or ""))

        async def dom(self):
            return dom.op("html")["html"]

        async def text(self):
            return dom.op("text").get("text") or ""

        async def network(self, want_bodies=True):
            return []
    return BenchSession


def policy(goals, dom: Dom, collect_js: str, record: Dict[str, Any], rng_key: str):
    """The scripted model: the first goal not yet proved, on the element of that
    name in the observation it was given; then a seeded think time."""
    def proved(goal, text: str, names: List[str]) -> bool:
        proof = goal[3]
        return proof[1:] in names if proof.startswith("@") else proof in text

    def brain(messages, images=()):
        body = json.loads(messages[-1]["content"])
        els = body.get("elements") or []
        names = [str(e.get("name") or "") for e in els]
        text = str(body.get("visible_text") or "")
        # what the model saw, read from the page before any time passes
        lids = dom.op("eval", js="(() => {" + collect_js + " return ordered.map(c => c.n.lid); })()")["value"] or []
        settle_log(record, dom)
        done = 0
        for k, goal in enumerate(goals):
            if proved(goal, text, names):
                done = k + 1
        if done >= len(goals):
            action = {"action": "done"}
        else:
            kind, name, after, _proof, typed = goals[done]
            start = names.index(after) + 1 if after and after in names else 0
            nth = int(name.split("#", 1)[1]) if "#" in name else 0
            hits = [i for i in range(start, len(names)) if named(names[i], name)]
            idx = hits[nth] if nth < len(hits) else -1
            if after and after not in names:
                idx = -1
            if idx < 0:
                action = {"action": "read"}
            else:
                action = {"action": kind, "index": idx}
                if typed:
                    action["text"] = typed
                record["pending"] = {"kind": kind, "meant": lids[idx] if idx < len(lids) else None}
        record["calls"] += 1
        think = random.Random(f"{rng_key}:{record['calls']}").uniform(*THINK_S)
        dom.op("advance", ms=int(think * 1000))
        return action, {}, False
    return brain


def settle_log(record: Dict[str, Any], dom: Dom) -> None:
    """Score the previous index action against the stand-in's act log."""
    entries = dom.op("log")["log"]
    pend = record.pop("pending", None)
    if pend is None:
        return
    want = "focus" if pend["kind"] == "type" else "click"
    acted = [e for e in entries if e["act"] == want]
    record["actions"] += 1
    if not acted:
        record["refused"] += 1
    elif acted[0]["lid"] != pend["meant"]:
        record["wrong"] += 1
        record["wrong_lids"].append(f"{pend['meant']}->{acted[0]['lid']}")


def run_case(dom: Dom, scenario: str, seed: int) -> Dict[str, Any]:
    from anyuse.execution import agent_browser as AB
    builder, cfg, goals = SCENARIOS[scenario]
    record: Dict[str, Any] = {"observes": 0, "calls": 0, "actions": 0, "wrong": 0, "refused": 0,
                              "picks": [], "wrong_lids": []}
    dom.op("load", builder=builder, cfg=cfg, seed=seed * 7919 + len(scenario), url="https://shop.test/blank")
    agent = AB.StepAgent(policy(goals, dom, AB._COLLECT_JS, record, f"{scenario}:{seed}"),
                         session_factory=session_class(dom, record), endpoint="http://192.0.2.1:9222",
                         max_steps=MAX_STEPS)
    run = agent.run("bench task {q}", START, "x")
    settle_log(record, dom)
    final = str(run.text or "")
    return {"scenario": scenario, "seed": seed, "completed": goals[-1][3] in final,
            "steps": len([s for s in run.steps if isinstance(s, dict) and "action" in s]),
            "observes": record["observes"], "actions": record["actions"], "wrong": record["wrong"],
            "refused": record["refused"], "picks": record["picks"], "wrong_lids": record["wrong_lids"],
            "stop": run.stop, "error": run.error, "final_url": run.final_url}


# ---------------------------------------------------------------------------
# one tree
# ---------------------------------------------------------------------------

def run_arm(label: str, tree: str, out_path: str, node: str, seeds: int) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    from anyuse.execution import agent_browser as AB
    dom = Dom(node)
    rows: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    try:
        for scenario in SCENARIOS:
            for seed in range(seeds):
                rows.append({"arm": label, **run_case(dom, scenario, seed)})
    finally:
        dom.close()
    meta = {"label": label, "tree": tree, "anyuse": real,
            "bound_pick": hasattr(AB, "BOUND_PICK_FN"), "wall_s": round(time.monotonic() - t0, 2)}
    with open(out_path, "w") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, indent=1, default=str)
    print(f"{label}: {len(rows)} cases in {meta['wall_s']} s -> {out_path}")


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

def summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    steps = sum(r["steps"] for r in rows)
    wrong = sum(r["wrong"] for r in rows)
    return {"cases": n, "completed": sum(r["completed"] for r in rows),
            "steps": steps, "actions": sum(r["actions"] for r in rows), "wrong": wrong,
            "refused": sum(r["refused"] for r in rows),
            "wrong_per_100_steps": round(100.0 * wrong / steps, 2) if steps else 0.0,
            "observes_per_task": round(statistics.mean(r["observes"] for r in rows), 2) if n else 0.0}


def compare(paths: List[str]) -> str:
    data = {}
    for p in paths:
        with open(p) as fh:
            d = json.load(fh)
        data[d["meta"]["label"]] = d
    old, new = data.get("OLD", {}).get("rows", []), data.get("NEW", {}).get("rows", [])
    lines = ["scenario            arm  cases done  steps acts wrong refused wrong/100 obs/task",
             "-" * 82]
    total = {"OLD": [], "NEW": []}
    for scenario in SCENARIOS:
        for label, rows in (("OLD", old), ("NEW", new)):
            rs = [r for r in rows if r["scenario"] == scenario]
            total[label] += rs
            s = summary(rs)
            lines.append(f"{scenario:<19} {label:<4} {s['cases']:>5} {s['completed']:>4} {s['steps']:>6} "
                         f"{s['actions']:>4} {s['wrong']:>5} {s['refused']:>7} {s['wrong_per_100_steps']:>9} "
                         f"{s['observes_per_task']:>8}")
    lines.append("-" * 82)
    for label in ("OLD", "NEW"):
        s = summary(total[label])
        lines.append(f"{'all':<19} {label:<4} {s['cases']:>5} {s['completed']:>4} {s['steps']:>6} "
                     f"{s['actions']:>4} {s['wrong']:>5} {s['refused']:>7} {s['wrong_per_100_steps']:>9} "
                     f"{s['observes_per_task']:>8}")
    mutating = [r for r in total["OLD"] + total["NEW"] if r["scenario"] != "static"]
    s_old = summary([r for r in mutating if r["arm"] == "OLD"])
    s_new = summary([r for r in mutating if r["arm"] == "NEW"])
    lines += ["", f"mutating pages: wrong per 100 steps OLD {s_old['wrong_per_100_steps']} -> "
                  f"NEW {s_new['wrong_per_100_steps']}; completed OLD {s_old['completed']}/{s_old['cases']} "
                  f"-> NEW {s_new['completed']}/{s_new['cases']}"]
    key = {(r["scenario"], r["seed"]): r for r in old}
    pairs = [(key[(r["scenario"], r["seed"])], r) for r in new if (r["scenario"], r["seed"]) in key]
    if pairs:
        extra = [n["observes"] - o["observes"] for o, n in pairs]
        lines.append(f"extra observations per task (NEW - OLD): mean {statistics.mean(extra):+.2f}, "
                     f"max {max(extra):+d}, min {min(extra):+d}")
        for scenario in SCENARIOS:
            ex = [n["observes"] - o["observes"] for o, n in pairs if o["scenario"] == scenario]
            if ex:
                lines.append(f"  {scenario:<19} mean {statistics.mean(ex):+.2f}")
        static = [(o, n) for o, n in pairs if o["scenario"] == "static"]
        same = sum(1 for o, n in static if (o["completed"], o["steps"], o["observes"])
                   == (n["completed"], n["steps"], n["observes"]))
        lines.append(f"static pages identical per case (completion, steps, observations): {same}/{len(static)}")
    hows: Dict[str, int] = {}
    for r in new:
        for h in r.get("picks") or []:
            hows[h] = hows.get(h, 0) + 1
    if hows:
        lines.append("NEW picks resolved as: " + ", ".join(f"{k} {v}" for k, v in sorted(hows.items())))
    errors = [r for r in old + new if r.get("error")]
    if errors:
        lines.append(f"cases with an error: {len(errors)} (first: {errors[0]['error']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# both trees
# ---------------------------------------------------------------------------

def default_node() -> str:
    found = shutil.which("node")
    if found:
        return found
    guess = os.path.expanduser("~/.local/node/bin/node")
    return guess if os.path.exists(guess) else ""


def run(args) -> int:
    node = args.node or default_node()
    if not node:
        raise SystemExit("needs a Node binary: pass --node")
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-click-binding-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "click_binding_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    results = []
    for label, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, tree, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", f"api_hosts_{label}.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", f"blocked_{label}.json"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--label", label,
                               "--tree", tree, "--out-file", path, "--node", node,
                               "--seeds", str(args.seeds)], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--node", default="")
    r.add_argument("--out", default="")
    r.add_argument("--seeds", type=int, default=SEEDS)
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    a.add_argument("--node", required=True)
    a.add_argument("--seeds", type=int, default=SEEDS)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.tree), os.path.abspath(args.out_file),
                args.node, args.seeds)
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
