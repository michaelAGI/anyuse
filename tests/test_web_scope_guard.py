"""The web scope inside execution/cdp_guard.py, offline, against a fake CDP
websocket (no socket, no browser). Every allow and deny path of the four
settings as the guard applies them, plus: all settings off = today's guard."""
from __future__ import annotations

import asyncio
import json
import sys
import os
import unittest

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D
from anyuse.execution import web_scope as W

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))

H = "www.example.org"
CSE_PAGE = ('<html><script async src="https://cse.google.com/cse.js?cx=0123456789abc:defgh">'
            '</script><div class="gcse-search"></div></html>')
FORM_PAGE = ('<html><form method="post" action="/search"><input type="text" name="keyword" '
             'placeholder="Search"><input type="hidden" name="lang" value="en"></form></html>')
TINY_BLOCK = frozenset({"google-analytics.com", "doubleclick.net"})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s

    async def asleep(self, s):
        self.sleep(s)


class FakeWS:
    """Scripted CDP events; answers commands; a response-body read is answered
    with ``bodies[url]`` and THEN the events the page would send after the
    document continued (``after``)."""

    def __init__(self, script=None, bodies=None, after=None):
        self.inbox = list(script or [])
        self.sent = []
        self.bodies = dict(bodies or {})
        self.after = list(after or [])
        self.paused_urls = {}
        for e in self.inbox:
            if e.get("method") == "Fetch.requestPaused":
                p = e["params"]
                self.paused_urls[p["requestId"]] = p["request"]["url"]

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m = msg.get("method")
        if m == "Fetch.getResponseBody":
            url = self.paused_urls.get(msg["params"]["requestId"], "")
            self.inbox.append({"id": msg["id"], "result": {"body": self.bodies.get(url, ""),
                                                           "base64Encoded": False}})
            self.inbox.extend(self.after)
            self.after = []
            return
        if m not in ("Fetch.continueRequest", "Fetch.failRequest", "Fetch.fulfillRequest"):
            self.inbox.append({"id": msg["id"], "result": {}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None

    def answered(self, method):
        return [m["params"]["requestId"] for m in self.sent if m.get("method") == method]


def paused(i, url, rtype="Script", method="GET", status=None, post=None, headers=None):
    req = {"url": url, "method": method}
    if post is not None:
        req["postData"] = post
    if headers:
        req["headers"] = headers
    p = {"requestId": f"f{i}", "request": req, "resourceType": rtype, "networkId": f"n{i}"}
    if status is not None:
        p["responseStatusCode"] = status
    return {"method": "Fetch.requestPaused", "params": p}


def budget_with(scope=None, count=0, **kw):
    clock = Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    if count:
        pacer.count[H] = count
    b = G.RequestBudget(pacer, asleep=clock.asleep, web_scope=scope, **kw)
    return pacer, b


def session(budget, script, bodies=None, after=None, allow=lambda u: True, cls=None):
    cls = cls or G.GuardedSession
    s = cls("http://192.0.2.1:9222", user_agent="research-probe/0.1", allow_url=allow,
            budget=budget)
    s._ws = FakeWS(script, bodies, after)
    return s


def run(coro):
    return asyncio.run(coro)


def doc_load(url=f"https://{H}/"):
    """The document's request stage and its (paused) response stage."""
    return [paused(0, url, "Document"), paused(100, url, "Document", status=200)]


def scope(**kw):
    kw.setdefault("blocklist_domains", TINY_BLOCK)
    return W.WebScope(**kw)


class AllOff(unittest.TestCase):
    def test_a_scope_with_every_setting_off_is_todays_guard(self):
        _p, b = budget_with(scope())
        self.assertIsNone(b.web_scope)
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, "https://cse.google.com/cse.js?cx=1", "Script"),
                        paused(2, f"https://{H}/search", "XHR", method="POST", post="q=x")])
        s0 = session(b, [])
        run(s0.setup())
        self.assertEqual(s0._ws.sent[0]["params"]["patterns"],
                         [{"urlPattern": "*", "requestStage": "Request"}])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])
        self.assertEqual(len(b.third_party_refused), 1)
        self.assertEqual(len(b.refused_methods), 1)


class ProviderGrant(unittest.TestCase):
    def test_grant_from_the_sites_own_document_then_provider_search(self):
        pacer, b = budget_with(scope(providers=True))
        after = [paused(1, "https://cse.google.com/cse.js?cx=0123456789abc:defgh", "Script"),
                 paused(2, "https://cse.google.com/cse/element/v1?q=granite&cx=1&cse_tok=T0K",
                        "Script"),
                 paused(3, "https://www.google-analytics.com/g/collect", "XHR"),
                 paused(4, "https://www.google.com/search?q=granite", "XHR")]
        s = session(b, doc_load(), {f"https://{H}/": CSE_PAGE}, after)
        s0 = session(b, [])
        run(s0.setup())
        pats = s0._ws.sent[0]["params"]["patterns"]
        self.assertIn({"urlPattern": "*", "resourceType": "Document",
                       "requestStage": "Response"}, pats)
        run(s.goto(f"https://{H}/"))
        run(s.settle(0.01))
        self.assertIn("google_cse", b.web_scope.grants[H])
        cont = s._ws.answered("Fetch.continueRequest")
        self.assertEqual([r for r in cont if r in ("f0", "f100", "f1", "f2")],
                         ["f0", "f100", "f1", "f2"])
        self.assertEqual(sorted(s._ws.answered("Fetch.failRequest")), ["f3", "f4"])
        # every provider request counts against the SITE (one pacer slot, one total)
        self.assertEqual(pacer.count, {H: 3})
        self.assertEqual(b.web_scope.usage[H], {"provider": 2})
        # the key the page sent is never in the guard's records
        self.assertFalse(any("T0K" in u for _h, _t, u, _ty in b.sent))
        s.guard.close([])
        b.check()

    def test_no_grant_without_the_pages_own_evidence(self):
        pacer, b = budget_with(scope(providers=True))
        after = [paused(1, "https://cse.google.com/cse/element/v1?q=x", "Script")]
        s = session(b, doc_load(), {f"https://{H}/": "<html>no search backend</html>"}, after)
        run(s.goto(f"https://{H}/"))
        run(s.settle(0.01))
        self.assertIn("f1", s._ws.answered("Fetch.failRequest"))
        self.assertEqual(b.third_party_refused[0][0], H)

    def test_a_third_party_document_is_never_scanned(self):
        # a third-party page (an ad iframe) naming a provider grants nothing
        _p, b = budget_with(scope(providers=True))
        g = G.LoadGuard(b)
        g.start(f"https://{H}/")
        g.read_body = lambda *a: None
        self.assertFalse(g.scan_wanted({"resourceType": "Document", "responseStatusCode": 200,
                                        "request": {"url": "https://ads.other.net/f"}}))
        self.assertTrue(g.scan_wanted({"resourceType": "Script", "responseStatusCode": 200,
                                       "request": {"url": f"https://{H}/app.js"}}))
        self.assertFalse(g.scan_wanted({"resourceType": "XHR", "responseStatusCode": 200,
                                        "request": {"url": f"https://{H}/api"}}))

    def test_preflight_of_a_granted_read_is_counted_not_fatal(self):
        pacer, b = budget_with(scope(providers=True))
        b.web_scope.observe(H, f"https://{H}/", "Document",
                            'algolia.init({appId:"ABCDEF1234"})')
        g = G.LoadGuard(b)
        g.start(f"https://{H}/")
        host = "abcdef1234-dsn.algolia.net"
        ev = [{"method": "Network.responseReceived",
               "params": {"requestId": "p1", "type": "Preflight",
                          "response": {"url": f"https://{host}/1/indexes/*/queries"}}}]
        self.assertEqual(g.audit(ev), [])
        self.assertEqual(pacer.count[H], 1)
        self.assertEqual(len(b.preflights), 1)
        # ... but a response from any host the scope did not allow stays FATAL
        bad = [{"method": "Network.responseReceived",
                "params": {"requestId": "p2", "type": "XHR",
                           "response": {"url": "https://tracker.other.net/x"}}}]
        self.assertTrue(g.audit(bad))

    def test_site_ceiling_covers_provider_requests(self):
        pacer, b = budget_with(scope(providers=True), count=299, site_cap=300,
                               per_host_cap=300)
        b.web_scope.observe(H, f"https://{H}/", "Document", CSE_PAGE)
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, "https://cse.google.com/cse.js?cx=1", "Script")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])
        self.assertEqual(pacer.count[H], 300)

    def test_provider_robots_is_consulted(self):
        _p, b = budget_with(scope(providers=True))
        b.web_scope.observe(H, f"https://{H}/", "Document", CSE_PAGE)
        asked = []

        def allow(u):
            asked.append(u)
            return "cse.google.com" not in u
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, "https://cse.google.com/cse.js?cx=1", "Script")], allow=allow)
        run(s.goto(f"https://{H}/"))
        self.assertIn("https://cse.google.com/cse.js?cx=1", asked)
        self.assertIn("f1", s._ws.answered("Fetch.failRequest"))
        self.assertTrue(s.guard.failed[-1][1].startswith("robots_refused"))


class PostSearch(unittest.TestCase):
    def test_the_pages_post_search_form(self):
        from ladder_verify import GetOnlySession
        pacer, b = budget_with(scope(readonly_post=True))
        after = [paused(1, f"https://{H}/search", "Document", method="POST",
                        post="keyword=granite&lang=en"),
                 paused(2, f"https://{H}/cart/add", "XHR", method="POST", post="id=1"),
                 paused(3, f"https://{H}/search", "Document", method="PUT",
                        post="keyword=granite")]
        s = session(b, doc_load(), {f"https://{H}/": FORM_PAGE}, after, cls=GetOnlySession)
        run(s.goto(f"https://{H}/"))
        run(s.settle(0.01))
        self.assertIn("f1", s._ws.answered("Fetch.continueRequest"))
        self.assertEqual(sorted(s._ws.answered("Fetch.failRequest")), ["f2", "f3"])
        self.assertEqual(b.web_scope.usage[H], {"post_search": 1})
        self.assertEqual({m for m, _u in b.refused_methods}, {"POST", "PUT"})

    def test_bench_session_without_scope_still_fails_every_post(self):
        from ladder_verify import GetOnlySession
        _p, b = budget_with(None)
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, f"https://{H}/search", "Document", method="POST",
                               post="keyword=granite")], cls=GetOnlySession)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])

    def test_xhr_post_with_the_search_value(self):
        _p, b = budget_with(scope(readonly_post=True))
        b.web_scope.note_values(H, ["granite"])
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, f"https://{H}/api/find", "XHR", method="POST",
                               post=json.dumps({"query": "granite"}),
                               headers={"Content-Type": "application/json"}),
                        paused(2, f"https://{H}/api/find", "XHR", method="POST",
                               post=json.dumps({"query": "granite", "subscribe": True}))])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertIn("state change", s.guard.failed[-1][1])


class CacheAndAudit(unittest.TestCase):
    def test_own_host_script_served_from_the_run_cache_is_not_a_leak(self):
        _p, b = budget_with(scope(own_hosts=True))
        b.cache_enabled = True
        url = "https://cookies.example.org/cookie.js"
        hdrs = {"cache-control": "max-age=600", "content-type": "text/javascript"}
        self.assertTrue(b.remember(url, 200, hdrs, "x()", rtype="Script"))
        s = session(b, [paused(0, f"https://{H}/", "Document"), paused(1, url, "Script")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), ["f1"])
        ev = [{"method": "Network.responseReceived",
               "params": {"requestId": "n1", "type": "Script", "response": {"url": url}}}]
        self.assertEqual(s.guard.audit(ev), [])


class OwnAndThirdParty(unittest.TestCase):
    def test_own_cdn_host_counts_against_the_site(self):
        pacer, b = budget_with(scope(own_hosts=True))
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, "https://static.example.org/app.js", "Script"),
                        paused(2, "https://metrics.example.org/b", "XHR"),
                        paused(3, "https://cdn.other.net/x.js", "Script"),
                        paused(4, "https://static.example.org/api", "XHR", method="POST",
                               post="a=1")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(pacer.count, {H: 2})
        self.assertEqual(b.web_scope.usage[H], {"own_host": 1})
        ev = [{"method": "Network.responseReceived",
               "params": {"requestId": "n1", "type": "Script",
                          "response": {"url": "https://static.example.org/app.js"}}}]
        s.guard.close(ev)
        b.check()

    def test_a_redirect_of_the_sites_navigation_binds_a_host_under_an_unlisted_suffix(self):
        # .it has public second levels this build does not list: the site's own redirect names the host
        site = "www.negozio.it"
        pacer, b = budget_with(scope(own_hosts=True))
        hop = paused(1, "https://shop.negozio.it/", "Document")
        hop["params"]["redirectedRequestId"] = "f0"
        s = session(b, [paused(0, f"https://{site}/", "Document"), hop,
                        paused(2, "https://shop.negozio.it/app.js", "Script"),
                        paused(3, "https://other.negozio.it/app.js", "Script")])
        run(s.goto(f"https://{site}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1", "f2"])
        self.assertIn("redirect", b.web_scope.bound[site]["shop.negozio.it"])
        self.assertNotIn("other.negozio.it", b.web_scope.bound[site])

    def test_allow_with_blocklist(self):
        pacer, b = budget_with(scope(third_party="allow_with_blocklist"))
        asked = []
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, "https://cdn.jsdelivr.net/npm/x.js", "Script"),
                        paused(2, "https://www.google-analytics.com/g/collect", "XHR"),
                        paused(3, "https://api.other.net/q", "XHR", method="POST", post="a=1"),
                        paused(4, "https://challenges.cloudflare.com/t.js", "Script")],
                    allow=lambda u: asked.append(u) or True)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(pacer.count, {H: 2})
        self.assertIn("https://cdn.jsdelivr.net/npm/x.js", asked, "third-party robots")
        self.assertEqual(b.web_scope.usage[H], {"third_party": 1})
        self.assertTrue(any("blocklist" in w for _u, w in s.guard.failed))

    def test_allow_all_is_still_get_only(self):
        _p, b = budget_with(scope(third_party="allow_all"))
        s = session(b, [paused(0, f"https://{H}/", "Document"),
                        paused(1, "https://www.google-analytics.com/g/collect", "XHR"),
                        paused(2, "https://api.other.net/q", "XHR", method="POST", post="a=1")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])


if __name__ == "__main__":
    unittest.main()
