"""execution/web_runtime/discover_browser.py (v3 guarded browser arm), offline.

A fake CDP websocket stands in for the container browser, so the REAL
``GuardedSession`` and ``LoadGuard`` run: no socket, no browser, no desktop.
What must hold:

* a page is read with one plain GET first and rendered only when that GET
  does not carry it; a server-rendered page never opens a browser session;
* in a rendered load every request, the page's scripts included, is paced and
  capped; third-party scripts are never sent; the HTTP and browser sides share
  one pacer and one request cap;
* robots and account paths are refused before a session is opened;
* the typing probe reports the same-host XHR and the document URL after Enter;
* a CAPTCHA seen in the browser stops the host for the HTTP side too;
* a violation found after a session raises TrafficViolation, and no reader's
  ``except Exception`` swallows it.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))

from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import cdp_guard as G  # noqa: E402
import discover_browser as DB  # noqa: E402

H = "js.example.test"
SHELL = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"
RENDERED = ("<html><head><title>Items</title></head><body><main>"
            "<input type='search' placeholder='Search items'>"
            + "".join(f"<a href='/item/i{i}'>Item {i}</a>" for i in range(12))
            + "</main></body></html>")
STATIC = ("<html><head><title>Static</title></head><body><main>"
          + "".join(f"<p>paragraph {i} " + "words " * 20 + f"<a href='/p/{i}'>p{i}</a></p>"
                    for i in range(12)) + "</main></body></html>")
CAPTCHA = ("<html><head><title>Just a moment...</title></head><body><div class='cf-turnstile'>"
           "challenges.cloudflare.com/turnstile</div></body></html>")


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    async def asleep(self, s):
        self.sleep(s)


class FakeBrowserWS:
    """A page per URL: its DOM and the sub-requests the load makes."""

    def __init__(self, pages):
        self.pages = pages
        self.inbox = []
        self.sent = []
        self.url = "about:blank"
        self.n = 0

    def _pause(self, url, rtype):
        self.n += 1
        self.inbox.append({"method": "Fetch.requestPaused", "params": {
            "requestId": f"f{self.n}", "networkId": f"n{self.n}",
            "request": {"url": url, "method": "GET"}, "resourceType": rtype}})

    def _load(self, url):
        self.url = url
        self._pause(url, "Document")
        for sub, rtype in self.pages.get(url, ("", []))[1]:
            self._pause(sub, rtype)

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m, p = msg.get("method"), msg.get("params") or {}
        if m in ("Fetch.continueRequest", "Fetch.failRequest"):
            return
        result = {}
        if m == "Page.navigate":
            self._load(p["url"])
        elif m == "Runtime.evaluate":
            e = p["expression"]
            if "outerHTML.length" in e:
                val = {"ready": "complete", "len": 100, "title": 5}
            elif "outerHTML" in e:
                val = self.pages.get(self.url, ("", []))[0]
            elif "location.href" in e:
                val = self.url
            elif "innerText" in e:
                val = "text"
            elif "querySelectorAll('input').forEach" in e:
                val = [{"index": 0, "visible": True, "name": "", "id": "", "type": "search"}]
            elif "activeElement" in e:
                val = True
            else:
                val = None
            result = {"result": {"value": val}}
        elif m == "Input.insertText":
            q = urllib.parse.quote_plus(p["text"])
            self._pause(f"https://{H}/api/suggest?q={q}", "XHR")
            self._pause(f"https://tracker.other.test/t?q={q}", "XHR")
            self.typed = p["text"]
        elif m == "Input.dispatchKeyEvent" and p.get("type") == "char":
            self._load(f"https://{H}/find?term={urllib.parse.quote_plus(self.typed)}")
        self.inbox.append({"id": msg["id"], "result": result})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None


class TestSession(G.GuardedSession):
    PAGES = {}
    WS = []

    async def _open(self):
        self._ws = FakeBrowserWS(self.PAGES)
        self.WS.append(self._ws)
        await self.setup()

    async def __aexit__(self, *exc):
        self.guard.close(self._events)
        self._ws = None


def setup(pages, http_pages=None, allow=lambda u: "/private" not in u):
    clock = Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    http_calls = []

    def client(u, h):
        http_calls.append(u)
        body = (http_pages or {}).get(u)
        if body is None:
            return 404, {"content-type": "text/html"}, "<html>nf</html>"
        return 200, {"content-type": "text/html"}, body
    http = D.HttpFetcher(user_agent="research-probe/0.1 (read-only; 1 req/s)", allow_url=allow,
                         pacer=pacer, client=client, max_requests_per_host=200)
    budget = G.RequestBudget(pacer, asleep=clock.asleep)
    TestSession.PAGES = pages
    TestSession.WS = []
    b = DB.BrowserFetcher("http://192.0.2.1:9222", http, budget,
                          session_factory=functools.partial(TestSession, user_agent=http.user_agent,
                                                            budget=budget))
    b._settler.stable_ms = 0
    return b, http, budget, clock, http_calls


class BrowserArm(unittest.TestCase):

    def test_server_rendered_page_never_opens_the_browser(self):
        u = f"https://{H}/"
        b, http, budget, _c, calls = setup({}, {u: STATIC})
        r = b.get(u)
        self.assertIn("paragraph 3", r.body)
        self.assertEqual(TestSession.WS, [])
        self.assertEqual(calls, [u])
        self.assertNotIn(H, b.js_hosts)

    def test_js_shell_is_rendered_under_the_guard(self):
        u = f"https://{H}/"
        subs = ([(f"https://{H}/static/app{i}.js", "Script") for i in range(40)]
                + [("https://cdn.other.test/lib.js", "Script"),
                   (f"https://{H}/logo.png", "Image"), (f"https://{H}/s.css", "Stylesheet")])
        b, http, budget, clock, calls = setup({u: (RENDERED, subs)}, {u: SHELL})
        r = b.get(u)
        self.assertIn("Item 3", r.body)
        self.assertIn(H, b.js_hosts)
        self.assertTrue(b.page_info[u]["rendered"])
        self.assertTrue(b.page_info[u]["search_inputs"][0]["visible"])
        # 1 plain GET + at most 30 in the load
        self.assertEqual(budget.loads[H], [30])
        self.assertEqual(http.pacer.count[H], 31)
        times = [t for (h, t, _u, _ty) in budget.sent if h == H]
        self.assertTrue(all(b_ - a >= 1.0 - 1e-9 for a, b_ in zip(times, times[1:])))
        self.assertFalse(any("other.test" in x[2] for x in budget.sent))
        budget.check()

    def test_robots_and_account_paths_are_refused_before_a_session(self):
        b, _h, _b, _c, _calls = setup({})
        run, _ = b.open(f"https://{H}/private/x")
        self.assertIn("robots_refused", run.error)
        run = b.navigator(f"https://{H}/account/login", "v")
        self.assertFalse(run.dom)
        self.assertEqual(TestSession.WS, [])

    def test_typing_probe_reports_same_host_xhr_and_document(self):
        u = f"https://{H}/"
        pages = {u: (RENDERED, []),
                 f"https://{H}/find?term=item+3": ("<html><title>Find</title></html>", [])}
        b, _h, budget, _c, _calls = setup(pages)
        obs = b.search_probe(u, "item 3")
        self.assertEqual(obs["document"], f"https://{H}/find?term=item+3")
        self.assertEqual(obs["xhr"], [f"https://{H}/api/suggest?q=item+3"])
        self.assertFalse(any("tracker" in x[2] for x in budget.sent))
        tmpl = D.templates_from_observed([obs["document"]], "item 3", H)
        self.assertEqual(tmpl[0][0], f"https://{H}/find?term={{v}}")

    def test_observed_templates_take_the_sites_own_subdomains_only(self):
        got = D.templates_from_observed(["https://api.shop.test/s?q=x"], "x", "www.shop.test")
        self.assertEqual(got, [("https://api.shop.test/s?q={v}", "query", "q")])
        self.assertEqual(D.templates_from_observed(["https://other.test/s?q=x"], "x", "www.shop.test"), [])

    def test_captcha_in_browser_stops_the_host_everywhere(self):
        u = f"https://{H}/"
        b, http, _b, _c, _calls = setup({u: (CAPTCHA, [])}, {u: SHELL})
        b.get(u)
        self.assertIn(H, http.blocked)
        self.assertIn("not re-hit", http.get(f"https://{H}/other").error)

    def test_a_violation_stops_the_run_through_the_readers(self):
        u = f"https://{H}/"
        b, _h, budget, _c, _calls = setup({u: (RENDERED, [])}, {u: SHELL})
        budget.violations.append("simulated bypass")
        with self.assertRaises(G.TrafficViolation):
            D.discover_host(f"https://{H}", b, search_probe=b.search_probe)

    def test_needs_render_rule(self):
        self.assertTrue(DB.needs_render(SHELL, f"https://{H}/"))
        self.assertFalse(DB.needs_render(STATIC, f"https://{H}/"))
        self.assertTrue(DB.needs_render("<html><body>" + "x " * 500 + "</body></html>",
                                        f"https://{H}/"))


if __name__ == "__main__":
    unittest.main()
