"""The agent's focus and click act on the element the model was shown.

The step agent observes the page, asks the model, and only then acts on the
index the model chose. Pages change in between (a banner or an ad slot that
loads late, a suggestion list that re-sorts, a feed that prepends rows), so
the same index can name another control by the time of the click. The session
keeps the observed nodes behind a CDP object handle and the page resolves the
element the model saw, or refuses (``agent_browser.BOUND_PICK_FN``).

Two kinds of test, both offline:

* the session's protocol and the step agent's handling of a refusal, against
  scripted CDP answers;
* the real observe and pick scripts run in Node against a small DOM stand-in
  whose list changes between the observation and the click. They need a Node
  binary (``ANYUSE_TEST_NODE``, else ``node`` on PATH) and are skipped without
  one.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import unittest

from anyuse.execution import agent_browser as AB
from anyuse.execution.agent_cdp import CdpAgentSession


def run(coro):
    return asyncio.run(coro)


ROWS = [{"i": 0, "tag": "input", "type": "search", "name": "Search", "href": ""},
        {"i": 1, "tag": "a", "type": "", "name": "Home", "href": "/"},
        {"i": 2, "tag": "a", "type": "", "name": "Products", "href": "/products"}]
OBS = {"url": "https://shop.test/", "title": "Shop", "elements": ROWS, "text": "Shop"}


class Scripted(CdpAgentSession):
    """CDP answers by method; ``pick`` answers the bound pick."""

    def __init__(self, pick=None, dead_handle=False):
        super().__init__("http://192.0.2.1:9222")
        self.sent, self.pick, self.dead_handle, self.waited = [], pick, dead_handle, []
        self.observe_rows = None

    def _obs(self):
        return dict(OBS, elements=self.observe_rows) if self.observe_rows else OBS

    async def _call(self, method, params, timeout):
        params = params or {}
        self.sent.append((method, params))
        if method == "Runtime.evaluate":
            expr = params["expression"]
            if params.get("returnByValue") is False:
                return {"result": {"type": "object", "objectId": "snap-1"}}
            if "const c = ordered[idx]" in expr:
                return {"result": {"value": True}}
            if "function (snap, o)" in expr:
                return {"result": {"value": self.pick or {"ok": True, "how": "moved", "at": 2}}}
            return {"result": {"value": self._obs()}}
        if method == "Runtime.callFunctionOn":
            if "return this.obs" in params["functionDeclaration"]:
                return {"result": {"value": self._obs()}}
            if self.dead_handle:
                raise RuntimeError("Runtime.callFunctionOn: {'code': -32000, "
                                   "'message': 'Cannot find context with specified id'}")
            return {"result": {"value": self.pick or {"ok": True, "how": "held", "at": 2}}}
        return {}

    async def settle(self, seconds):
        self.waited.append(seconds)

    def methods(self):
        return [m for m, _ in self.sent]


class SessionBinding(unittest.TestCase):

    def test_a_click_after_an_observation_is_bound_to_the_row_the_model_saw(self):
        s = Scripted()
        self.assertEqual(run(s.observe()), OBS)
        run(s.click(2))
        calls = [p for m, p in s.sent if m == "Runtime.callFunctionOn"
                 and "function (snap, o)" in p.get("functionDeclaration", "")]
        self.assertEqual(len(calls), 1, s.methods())
        p = calls[0]
        self.assertEqual(p["objectId"], "snap-1")
        self.assertEqual(p["arguments"][0], {"objectId": "snap-1"})
        opts = p["arguments"][1]["value"]
        self.assertEqual(opts["index"], 2)
        self.assertTrue(opts["act"])
        self.assertEqual(opts["expect"], {"tag": "a", "type": "", "name": "Products",
                                          "href": "/products"})
        self.assertEqual(opts["prev"]["name"], "Home")
        self.assertIsNone(opts["next"])
        self.assertEqual(s.waited, [5.0])
        self.assertEqual(s.last_pick["how"], "held")

    def test_focus_is_bound_too_and_does_not_click(self):
        s = Scripted()
        run(s.observe())
        self.assertTrue(run(s.focus(0)))
        opts = [p for m, p in s.sent if m == "Runtime.callFunctionOn"][-1]["arguments"][1]["value"]
        self.assertFalse(opts["act"])
        self.assertEqual(opts["expect"]["name"], "Search")

    def test_a_refused_click_waits_briefly_and_says_why(self):
        s = Scripted(pick={"ok": False, "how": "gone"})
        run(s.observe())
        run(s.click(1))
        self.assertEqual(s.waited, [1.0])
        self.assertEqual(AB.pick_refusal(s), "gone")

    def test_an_index_the_model_was_not_shown_acts_on_nothing_and_is_no_refusal(self):
        s = Scripted()
        run(s.observe())
        sent = len(s.sent)
        run(s.click(7))
        self.assertFalse(run(s.focus(-1)))
        # nothing is sent to the page: there is no row to bind to and nothing to act on
        self.assertEqual(s.sent[sent:], [])
        self.assertEqual(s.last_pick, {"ok": False, "how": "unseen"})
        # the page did not change, so the step is not marked as refused
        self.assertEqual(AB.pick_refusal(s), "")
        self.assertEqual(s.waited, [5.0])

    def test_the_rank_among_equal_rows_is_sent_with_the_pick(self):
        s = Scripted()
        s.observe_rows = [dict(ROWS[1], i=0), dict(ROWS[2], i=1), dict(ROWS[2], i=2),
                          dict(ROWS[2], i=3)]
        run(s.observe())
        run(s.click(2))
        opts = [p for m, p in s.sent if m == "Runtime.callFunctionOn"][-1]["arguments"][1]["value"]
        self.assertEqual((opts["rank"], opts["count"]), (1, 3))

    def test_a_dead_handle_falls_back_to_matching_the_row_alone(self):
        s = Scripted(dead_handle=True)
        run(s.observe())
        run(s.click(2))
        exprs = [p["expression"] for m, p in s.sent if m == "Runtime.evaluate"]
        self.assertTrue(exprs[-1].startswith("(function (snap, o)"), exprs[-1][:60])
        self.assertIn(")(null, {", exprs[-1])
        self.assertEqual(s.last_pick["how"], "moved")

    def test_without_an_observation_the_pick_is_by_index(self):
        s = Scripted()
        run(s.click(2))
        exprs = [p["expression"] for m, p in s.sent if m == "Runtime.evaluate"]
        self.assertEqual(len(exprs), 1)
        self.assertIn("const c = ordered[idx]", exprs[0])
        self.assertTrue(exprs[0].rstrip().endswith("(2)"))
        self.assertEqual(s.last_pick, {"ok": True, "how": "index"})
        self.assertEqual(AB.pick_refusal(s), "")

    def test_a_navigation_drops_the_observation(self):
        s = Scripted()
        run(s.observe())
        s._pace = lambda url: asyncio.sleep(0)
        run(s.goto("https://shop.test/other"))
        run(s.click(2))
        self.assertEqual(s.last_pick, {"ok": True, "how": "index"})

    def test_the_challenge_check_does_not_replace_the_observation(self):
        s = Scripted()
        run(s.observe())
        seen = s._seen
        run(s.challenge_observation())
        self.assertIs(s._seen, seen)

    def test_the_next_observation_releases_the_previous_nodes(self):
        s = Scripted()
        run(s.observe())
        self.assertNotIn("Runtime.releaseObjectGroup", s.methods())
        run(s.observe())
        self.assertIn("Runtime.releaseObjectGroup", s.methods())


class Page:
    """A step-agent page whose bound picks are refused while ``refuse`` is set."""

    def __init__(self, endpoint="", allow_url=None, refuse=""):
        self.refuse, self.last_pick, self.log, self.url_now = refuse, {}, [], "https://shop.test/"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def goto(self, url):
        self.url_now = url

    async def observe(self):
        return dict(OBS, url=self.url_now)

    async def _pick(self, idx, what):
        if self.refuse:
            self.last_pick = {"ok": False, "how": self.refuse}
            self.refuse = ""
            return False
        self.last_pick = {"ok": True, "how": "held", "at": idx}
        self.log.append((what, idx))
        return True

    async def focus(self, idx):
        return await self._pick(idx, "focus")

    async def click(self, idx):
        await self._pick(idx, "click")

    async def type(self, text):
        self.log.append(("type", text))

    async def press_enter(self):
        self.log.append(("enter", None))

    async def settle(self, s):
        pass

    async def dom(self):
        return "<html><head><title>Shop</title></head><body>rows</body></html>"

    async def text(self):
        return "rows"

    async def url(self):
        return self.url_now

    async def network(self):
        return []


def scripted_brain(actions):
    seen = []

    def brain(messages, images=()):
        seen.append(json.loads(messages[-1]["content"]))
        return dict(actions[min(len(seen) - 1, len(actions) - 1)]), {}, False
    brain.seen = seen
    return brain


class AgentHandlesARefusal(unittest.TestCase):

    def _run(self, actions, refuse):
        page = Page(refuse=refuse)
        brain = scripted_brain(actions)
        agent = AB.StepAgent(brain, session_factory=lambda ep, **kw: page, max_steps=5,
                             stay_on_site=False)
        return agent.run("find {q}", "https://shop.test/", "x"), page, brain

    def test_a_refused_submit_presses_no_enter(self):
        run_, page, brain = self._run([{"action": "submit", "index": 0}, {"action": "done"}],
                                      refuse="gone")
        self.assertNotIn(("enter", None), page.log)
        out = run_.steps[0]["outcome"]
        self.assertFalse(out["acted"])
        self.assertEqual(out["page_changed"], "gone")

    def test_a_refused_type_types_nothing(self):
        run_, page, _ = self._run([{"action": "type", "index": 0, "text": "red"},
                                   {"action": "done"}], refuse="ambiguous")
        self.assertEqual(page.log, [])
        self.assertEqual(run_.typed, [])
        self.assertEqual(run_.steps[0]["outcome"]["page_changed"], "ambiguous")

    def test_the_model_is_told_and_its_next_click_goes_through(self):
        run_, page, brain = self._run([{"action": "click", "index": 2},
                                       {"action": "click", "index": 2}, {"action": "done"}],
                                      refuse="gone")
        self.assertEqual(page.log, [("click", 2)])
        told = brain.seen[1]["history"][0]
        self.assertEqual(told["outcome"]["note"], AB.PAGE_CHANGED_NOTE)
        self.assertNotIn("outcome", run_.steps[1])
        self.assertEqual(run_.stop, "done")

    def test_a_submit_naming_no_shown_element_still_presses_enter(self):
        # {"action": "submit"} without an index, after typing: Enter goes to the
        # field that has the focus, as before; the model is not told the page changed
        run_, page, brain = self._run([{"action": "submit"}, {"action": "done"}],
                                      refuse="unseen")
        self.assertEqual(page.log, [("enter", None)])
        self.assertEqual(run_.steps[0], {"action": "submit"})
        self.assertNotIn("outcome", brain.seen[1]["history"][0])

    def test_a_traced_refusal_is_marked_as_not_acted(self):
        page = Page(refuse="gone")
        agent = AB.StepAgent(scripted_brain([{"action": "click", "index": 2}, {"action": "done"}]),
                             session_factory=lambda ep, **kw: page, max_steps=5,
                             stay_on_site=False, trace=True)
        run_ = agent.run("find {q}", "https://shop.test/", "x")
        out = run_.trace[0]["outcome"]
        self.assertIs(out["acted"], False)
        self.assertEqual(out["pick"], {"ok": False, "how": "gone"})

    def test_an_accepted_pick_leaves_the_step_unchanged(self):
        run_, page, _ = self._run([{"action": "submit", "index": 0}, {"action": "done"}],
                                  refuse="")
        self.assertEqual(page.log, [("focus", 0), ("enter", None)])
        self.assertEqual(run_.steps[0], {"action": "submit", "index": 0})


# ---------------------------------------------------------------------------
# the real scripts, in Node, against a DOM stand-in
# ---------------------------------------------------------------------------

#: A DOM stand-in with just what the collector and the pick read, and a CDP
#: answerer for Runtime.evaluate / callFunctionOn / releaseObjectGroup and
#: Page.navigate. Object handles die with their document, as in Chrome. One
#: JSON request per line on stdin, one answer per line on stdout.
DOM_JS = r"""
'use strict';
const vm = require('vm');
let doc = null, ctx = null, gen = 0, handles = new Map(), nextId = 1, log = [], pendingNav = null, builder = '';
const BLOCK = new Set(['HTML', 'BODY', 'DIV', 'UL', 'LI', 'P', 'NAV', 'SECTION', 'MAIN', 'HEADER', 'FORM']);
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
  focus() { doc.active = this; log.push({act: 'focus', lid: this.lid, text: this.innerText}); }
  click() {
    log.push({act: 'click', lid: this.lid, text: this.innerText});
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
  gen += 1; handles = new Map();
  doc = {url: url, root: new El('html'), body: new El('body'), active: null, title: ''};
  doc.root.appendChild(doc.body);
  const document = {
    get body() { return doc.body; }, get documentElement() { return doc.root; },
    get title() { return doc.title; }, set title(t) { doc.title = String(t); },
    get activeElement() { return doc.active || doc.body; },
    querySelectorAll: sel => doc.root.querySelectorAll(sel), createElement: tag => new El(tag),
  };
  const sandbox = {document: document, location: {get href() { return doc.url; }}, h: h, URL: URL, console: console};
  sandbox.window = sandbox;
  ctx = vm.createContext(sandbox);
  vm.runInContext(builder, ctx);
  vm.runInContext('page(location.href)', ctx);
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
  return {};
}
require('readline').createInterface({input: process.stdin}).on('line', line => {
  const q = JSON.parse(line);
  let out = {};
  try {
    if (q.op === 'load') { builder = q.builder; navigate(q.url); }
    else if (q.op === 'cdp') out.result = cdp(q.method, q.params || {});
    else if (q.op === 'eval') out.value = byValue(vm.runInContext(q.js, ctx)).value;
    else if (q.op === 'log') { out.log = log; log = []; }
    else if (q.op === 'html') out.html = '<!doctype html>' + doc.root.outerHTML;
    if (pendingNav) { const u = pendingNav; pendingNav = null; navigate(u); }
  } catch (e) { out = {error: e && e.message ? {code: e.code || -1, message: e.message} : String(e)}; }
  process.stdout.write(JSON.stringify(out) + '\n');
});
"""


def node_binary() -> str:
    return os.environ.get("ANYUSE_TEST_NODE") or shutil.which("node") or ""


class NodeDom:
    """One Node process holding one page built by ``builder`` (defines ``page(url)``)."""

    def __init__(self, builder: str, url: str = "https://shop.test/"):
        self.proc = subprocess.Popen([node_binary(), "-e", DOM_JS], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        self.op("load", builder=builder, url=url)

    def op(self, op, **kw):
        self.proc.stdin.write(json.dumps(dict(kw, op=op)) + "\n")
        self.proc.stdin.flush()
        out = json.loads(self.proc.stdout.readline())
        if isinstance(out, dict) and out.get("error") and op != "cdp":
            raise AssertionError(out["error"])
        return out

    def eval(self, js):
        # a block, so names declared by one call do not clash with the next
        return self.op("eval", js="{" + js + "}").get("value")

    def log(self):
        return self.op("log")["log"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


class DomSession(CdpAgentSession):
    """The agent's CDP session with the DOM stand-in behind its CDP calls."""

    def __init__(self, dom: NodeDom):
        super().__init__("http://192.0.2.1:9222")
        self.dom_ = dom

    async def _call(self, method, params, timeout):
        out = self.dom_.op("cdp", method=method, params=params or {})
        if out.get("error"):
            raise RuntimeError(f"{method}: {out['error']}")
        return out.get("result") or {}

    async def settle(self, seconds):
        return None


NAV = r"""
function page(url) {
  document.title = 'Shop';
  document.body.appendChild(h('nav', {}, '', [
    h('a', {href: '/', lid: 'home'}, 'Home'),
    h('a', {href: '/products', lid: 'products'}, 'Products'),
    h('a', {href: '/about', lid: 'about'}, 'About')]));
}
"""

OPTIONS = r"""
function page(url) {
  document.body.appendChild(h('div', {role: 'listbox'}, '', ['Widget A', 'Widget B', 'Widget C'].map(
    (t, i) => h('div', {role: 'option', lid: 'opt' + i}, t))));
}
"""

ROWS_JS = r"""
function page(url) {
  const list = h('ul', {id: 'list'});
  [1, 2, 3].forEach(n => list.appendChild(h('li', {}, '', [
    h('a', {href: '/row/' + n, lid: 'row' + n}, 'Row ' + n),
    h('button', {lid: 'details' + n}, 'Details')])));
  document.body.appendChild(list);
}
"""

ADDS = r"""
function page(url) {
  const box = h('div', {id: 'box'});
  box.appendChild(h('a', {href: '/t', lid: 'title'}, 'Title'));
  [1, 2, 3, 4].forEach(n => box.appendChild(h('button', {lid: 'add' + n}, 'Add')));
  box.appendChild(h('a', {href: '/f', lid: 'footer'}, 'Footer'));
  document.body.appendChild(box);
}
"""


FIELD = r"""
function page(url) {
  document.body.appendChild(h('form', {role: 'search'}, '', [
    h('input', {type: 'text', placeholder: 'Search shoes', lid: 'q'}),
    h('button', {type: 'submit', lid: 'go'}, 'Go')]));
  document.body.appendChild(h('a', {href: '/help', lid: 'help'}, 'Help'));
}
"""


@unittest.skipUnless(node_binary(), "needs a Node binary (ANYUSE_TEST_NODE or node on PATH)")
class RealScripts(unittest.TestCase):

    def page(self, builder):
        dom = NodeDom(builder)
        self.addCleanup(dom.close)
        return dom, DomSession(dom)

    @staticmethod
    def index_of(obs, name, nth=0):
        return [e["i"] for e in obs["elements"] if e["name"] == name][nth]

    def clicked(self, dom):
        return [e for e in dom.log() if e["act"] == "click"]

    def test_a_banner_inserted_above_does_not_redirect_the_click(self):
        dom, s = self.page(NAV)
        obs = run(s.observe())
        i = self.index_of(obs, "Products")
        dom.eval("document.body.prepend(h('a', {href: '/offer', lid: 'offer'}, 'Special offer'))")
        run(s.click(i))
        self.assertEqual([e["lid"] for e in self.clicked(dom)], ["products"])
        self.assertEqual(s.last_pick["how"], "held")
        self.assertEqual(s.last_pick["at"], i + 1)

    def test_a_rerendered_list_is_still_clicked_without_a_new_observation(self):
        dom, s = self.page(NAV)
        obs = run(s.observe())
        i = self.index_of(obs, "Products")
        dom.eval("document.body.textContent = ''; page(location.href)")
        run(s.click(i))
        self.assertEqual([e["lid"] for e in self.clicked(dom)], ["products"])
        self.assertEqual(s.last_pick["how"], "moved")

    def test_an_option_whose_label_moved_to_another_node_follows_the_label(self):
        dom, s = self.page(OPTIONS)
        obs = run(s.observe())
        i = self.index_of(obs, "Widget B")
        # the list re-sorts by reusing its nodes: each node gets another label
        dom.eval("const o = document.querySelectorAll('[role=option]');"
                 "['Widget C', 'Widget A', 'Widget B'].forEach((t, k) => { o[k].textContent = t; })")
        run(s.click(i))
        self.assertEqual([e["text"] for e in self.clicked(dom)], ["Widget B"])
        self.assertEqual(s.last_pick["how"], "moved")

    def test_a_control_that_is_gone_is_not_clicked(self):
        dom, s = self.page(NAV)
        obs = run(s.observe())
        i = self.index_of(obs, "Products")
        dom.eval("document.querySelectorAll('a[href]')[1].replaceWith("
                 "h('a', {href: '/delete', lid: 'delete'}, 'Delete list'))")
        run(s.click(i))
        self.assertEqual(self.clicked(dom), [])
        self.assertEqual(AB.pick_refusal(s), "gone")

    def test_a_repeated_label_in_a_shifted_rerender_is_found_by_its_neighbours(self):
        dom, s = self.page(ROWS_JS)
        obs = run(s.observe())
        i = self.index_of(obs, "Details", nth=1)
        # a new row on top, every row rendered again as new nodes
        dom.eval("const l = document.querySelectorAll('ul')[0]; l.textContent = '';"
                 "[0, 1, 2, 3].forEach(n => l.appendChild(h('li', {}, '', ["
                 "h('a', {href: '/row/' + n, lid: 'row' + n}, 'Row ' + n),"
                 "h('button', {lid: 'details' + n}, 'Details')])))")
        run(s.click(i))
        self.assertEqual([e["lid"] for e in self.clicked(dom)], ["details2"])
        self.assertEqual(s.last_pick["how"], "near")

    #: the ADDS box rendered again as new nodes, with a link on top and ``n`` buttons
    RERENDER_ADDS = ("const b = document.querySelectorAll('div')[0]; b.textContent = '';"
                     "b.appendChild(h('a', {href: '/p', lid: 'promo'}, 'Promo'));"
                     "b.appendChild(h('a', {href: '/t', lid: 'title'}, 'Title'));"
                     "for (let n = 1; n <= %d; n++) b.appendChild(h('button', {lid: 'new' + n}, 'Add'));"
                     "b.appendChild(h('a', {href: '/f', lid: 'footer'}, 'Footer'))")

    def test_equal_controls_shifted_by_a_new_link_are_found_by_their_order(self):
        for nth in (1, 2):
            dom, s = self.page(ADDS)
            obs = run(s.observe())
            i = self.index_of(obs, "Add", nth=nth)
            dom.eval(self.RERENDER_ADDS % 4)
            run(s.click(i))
            self.assertEqual([e["lid"] for e in self.clicked(dom)], ["new%d" % (nth + 1)])
            self.assertEqual(s.last_pick["how"], "order")

    def test_a_tie_the_index_does_not_settle_is_refused(self):
        dom, s = self.page(ADDS)
        obs = run(s.observe())
        i = self.index_of(obs, "Add", nth=1)
        # one button more than the model saw: neither order nor index tells which one it chose
        dom.eval(self.RERENDER_ADDS % 5)
        run(s.click(i))
        self.assertEqual(self.clicked(dom), [])
        self.assertEqual(AB.pick_refusal(s), "ambiguous")

    def test_a_search_field_whose_placeholder_changed_is_still_focused(self):
        dom, s = self.page(FIELD)
        obs = run(s.observe())
        i = self.index_of(obs, "Search shoes")
        # a rotating placeholder: the same field shows another prompt by the time of the pick
        dom.eval("document.querySelectorAll('input')[0].setAttribute('placeholder', 'Search bags')")
        self.assertTrue(run(s.focus(i)))
        self.assertEqual([e["lid"] for e in dom.log() if e["act"] == "focus"], ["q"])
        self.assertEqual(s.last_pick["how"], "held")

    def test_a_link_whose_label_changed_on_the_same_node_is_not_clicked(self):
        dom, s = self.page(NAV)
        obs = run(s.observe())
        i = self.index_of(obs, "Products")
        dom.eval("const a = document.querySelectorAll('a[href]')[1];"
                 "a.textContent = 'Delete list'; a.setAttribute('href', '/delete')")
        run(s.click(i))
        self.assertEqual(self.clicked(dom), [])
        self.assertEqual(AB.pick_refusal(s), "gone")

    def test_a_click_that_navigated_leaves_the_next_pick_matching_by_row(self):
        dom, s = self.page(NAV)
        obs = run(s.observe())
        run(s.click(self.index_of(obs, "Products")))
        self.assertEqual(run(s.url()), "https://shop.test/products")
        # the snapshot's document is gone; the same row is found on the new page
        run(s.click(self.index_of(obs, "About")))
        self.assertEqual([e["lid"] for e in self.clicked(dom)], ["products", "about"])
        self.assertEqual(s.last_pick["how"], "moved")

    def test_an_unchanged_page_clicks_by_the_held_node(self):
        dom, s = self.page(NAV)
        obs = run(s.observe())
        self.assertEqual([e["name"] for e in obs["elements"]], ["Home", "Products", "About"])
        run(s.click(2))
        self.assertEqual([e["lid"] for e in self.clicked(dom)], ["about"])
        self.assertEqual(s.last_pick, {"ok": True, "how": "held", "at": 2})


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
