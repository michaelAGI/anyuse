"""execution/cdp_guard.py, offline, against a fake CDP websocket.

No socket, no browser, no desktop. What must hold:

* the interceptor is on for EVERY request before the first navigation;
* every same-host request, sub-requests included, waits on the shared pacer, so
  two sends to one host are >= 1 s apart, also across the HTTP fetcher;
* a page load sends at most 30 requests; a host at most 200 in the run, and the
  host is then stopped;
* third-party hosts, images, fonts, media, stylesheets, account paths and
  robots-disallowed URLs are failed in the browser and never sent;
* the audit turns any request that reached the network without the guard,
  a websocket, or a cap overrun into a TrafficViolation.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from anyuse.execution import discover as D
from anyuse.execution import cdp_guard as G

H = "js.example.test"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    async def asleep(self, s):
        self.sleep(s)


class FakeWS:
    """Replays scripted CDP events; answers every command that has an id."""

    def __init__(self, script=None):
        self.inbox = list(script or [])
        self.sent = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m = msg.get("method")
        if m not in ("Fetch.continueRequest", "Fetch.failRequest"):
            if m == "Runtime.evaluate":
                self.inbox.append({"id": msg["id"], "result": {"result": {"value": ""}}})
            else:
                self.inbox.append({"id": msg["id"], "result": {}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None

    def answered(self, method):
        return [m["params"]["requestId"] for m in self.sent if m.get("method") == method]


def paused(i, url, rtype="Script", nid=True, method="GET"):
    p = {"requestId": f"f{i}", "request": {"url": url, "method": method}, "resourceType": rtype}
    if nid:
        p["networkId"] = f"n{i}"
    return {"method": "Fetch.requestPaused", "params": p}


def setup(allow=lambda u: "/private" not in u, count=0):
    clock = Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    if count:
        pacer.count[H] = count
    budget = G.RequestBudget(pacer, asleep=clock.asleep)
    return clock, pacer, budget


def session(budget, script, allow=lambda u: "/private" not in u):
    s = G.GuardedSession("http://192.0.2.1:9222", user_agent="research-probe/0.1",
                         allow_url=allow, budget=budget)
    s._ws = FakeWS(script)
    return s


def run(coro):
    return asyncio.run(coro)


class Guard(unittest.TestCase):

    def test_interception_is_on_for_every_request_before_navigation(self):
        _c, _p, budget = setup()
        s = session(budget, [])
        run(s.setup())
        methods = [m["method"] for m in s._ws.sent]
        self.assertEqual(methods[0], "Fetch.enable")
        pat = s._ws.sent[0]["params"]["patterns"]
        self.assertEqual(pat, [{"urlPattern": "*", "requestStage": "Request"}])  # no type filter
        self.assertIn("Network.setCacheDisabled", methods)
        add = next(m for m in s._ws.sent if m["method"] == "Page.addScriptToEvaluateOnNewDocument")
        for k in ("WebSocket", "EventSource", "sendBeacon", "serviceWorker", "Worker"):
            self.assertIn(k, add["params"]["source"])
        self.assertLess(methods.index("Page.addScriptToEvaluateOnNewDocument"),
                        len(methods))

    def test_every_subrequest_is_paced_and_the_load_is_capped_at_30(self):
        clock, pacer, budget = setup()
        script = [paused(0, f"https://{H}/", "Document")] + [
            paused(i, f"https://{H}/a{i}.js", "Script" if i % 2 else "XHR") for i in range(1, 50)]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        sent = s._ws.answered("Fetch.continueRequest")
        failed = s._ws.answered("Fetch.failRequest")
        self.assertEqual(len(sent), 30)
        self.assertEqual(len(failed), 20)
        times = [t for (h, t, _u, _ty) in budget.sent if h == H]
        self.assertEqual(len(times), 30)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 1.0 - 1e-9 for g in gaps), gaps)
        self.assertEqual(pacer.count[H], 30)
        self.assertTrue(s.guard.load_capped)
        s.guard.close([])
        self.assertEqual(budget.loads[H], [30])
        budget.check()                                   # within both caps

    def test_third_party_and_blocked_types_are_never_sent(self):
        _c, pacer, budget = setup()
        script = [paused(0, f"https://{H}/", "Document"),
                  paused(1, "https://cdn.other.test/x.js", "Script"),
                  paused(2, "https://www.google-analytics.com/collect", "XHR"),
                  paused(3, f"https://{H}/logo.png", "Image"),
                  paused(4, f"https://{H}/site.css", "Stylesheet"),
                  paused(5, f"https://{H}/f.woff2", "Font"),
                  paused(6, f"https://{H}/v.mp4", "Media"),
                  paused(7, f"https://{H}/beacon", "Ping"),
                  paused(8, "https://other.test/frame", "Document"),
                  paused(9, f"https://{H}/manifest.json", "Manifest"),
                  paused(10, f"https://{H}/api/items", "Fetch")]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f10"])
        self.assertEqual(len(s._ws.answered("Fetch.failRequest")), 9)
        self.assertEqual(pacer.count, {H: 2})
        self.assertNotIn("cdn.other.test", pacer.count)

    def test_robots_and_account_paths_are_failed_before_sending(self):
        _c, pacer, budget = setup()
        script = [paused(0, f"https://{H}/", "Document"),
                  paused(1, f"https://{H}/private/api", "XHR"),
                  paused(2, f"https://{H}/account/login", "Document"),
                  paused(3, f"https://{H}/cart", "XHR")]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])
        self.assertEqual(pacer.count[H], 1)
        self.assertEqual(s.refused, [f"https://{H}/private/api"])

    def test_host_cap_of_200_stops_the_host(self):
        _c, pacer, budget = setup(count=195)
        script = [paused(0, f"https://{H}/", "Document")] + [
            paused(i, f"https://{H}/x{i}", "XHR") for i in range(1, 10)]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 5)
        self.assertEqual(pacer.count[H], 200)
        self.assertIn(H, budget.stopped)
        s2 = session(budget, [paused(0, f"https://{H}/b", "Document")])
        run(s2.goto(f"https://{H}/b"))
        self.assertEqual(s2._ws.answered("Fetch.continueRequest"), [])
        # the HTTP side refuses the host too (one budget: the shared pacer count)
        http = D.HttpFetcher(pacer=pacer, max_requests_per_host=200,
                             client=lambda u, h: (200, {}, "x"))
        self.assertIn("request cap", http.get(f"https://{H}/c").error)
        budget.check()

    def test_http_and_browser_share_one_pacer(self):
        clock, pacer, budget = setup()
        http = D.HttpFetcher(pacer=pacer, client=lambda u, h: (200, {"content-type": "text/html"},
                                                                "<html></html>"))
        http.get(f"https://{H}/robots-free")
        t_http = clock.t
        s = session(budget, [paused(0, f"https://{H}/", "Document")])
        run(s.goto(f"https://{H}/"))
        t_browser = budget.sent[0][1]
        self.assertGreaterEqual(t_browser - t_http, 1.0)

    def test_redirect_hops_are_each_paced(self):
        _c, pacer, budget = setup()
        script = [paused(0, f"https://{H}/", "Document"),
                  paused(1, f"https://{H}/en/", "Document"),
                  paused(2, "https://elsewhere.test/", "Document")]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        times = [t for (_h, t, _u, _ty) in budget.sent]
        self.assertGreaterEqual(times[1] - times[0], 1.0)

    def test_audit_catches_a_request_that_bypassed_the_guard(self):
        _c, _p, budget = setup()
        s = session(budget, [paused(0, f"https://{H}/", "Document")])
        run(s.goto(f"https://{H}/"))
        events = [{"method": "Network.responseReceived",
                   "params": {"requestId": "n0", "response": {"url": f"https://{H}/"}}},
                  {"method": "Network.responseReceived",
                   "params": {"requestId": "zz", "response": {"url": f"https://{H}/sneaky"}}}]
        s.guard.close(events)
        self.assertTrue(any("without the guard" in v for v in budget.violations))
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    # a live hard stop, reproduced locally:
    # a same-site iframe's stylesheet answered from Chrome's memory cache
    @staticmethod
    def _memory_cache_hit(rid, url, wire=0, finished=True):
        ev = [{"method": "Network.requestWillBeSent",
               "params": {"requestId": rid, "type": "Stylesheet", "request": {"url": url}}},
              {"method": "Network.requestServedFromCache", "params": {"requestId": rid}},
              {"method": "Network.responseReceived",
               "params": {"requestId": rid, "type": "Stylesheet",
                          "response": {"url": url, "status": 200, "encodedDataLength": 1078,
                                       "fromDiskCache": False, "fromPrefetchCache": False}}},
              {"method": "Network.dataReceived",
               "params": {"requestId": rid, "dataLength": 1078, "encodedDataLength": 0}}]
        if finished:
            ev.append({"method": "Network.loadingFinished",
                       "params": {"requestId": rid, "encodedDataLength": wire}})
        return ev

    def _loaded(self):
        _c, _p, budget = setup()
        s = session(budget, [paused(0, f"https://{H}/", "Document")])
        run(s.goto(f"https://{H}/"))
        return budget, s, [{"method": "Network.responseReceived",
                            "params": {"requestId": "n0", "response": {"url": f"https://{H}/"}}}]

    def test_a_same_site_memory_cache_hit_is_counted_not_a_violation(self):
        budget, s, events = self._loaded()
        events += self._memory_cache_hit("m1", f"https://{H}/Fonts/kit/stylesheet.css")
        s.guard.close(events)
        self.assertEqual(budget.violations, [])
        self.assertEqual(len(budget.browser_cache_hits), 1)
        self.assertEqual(budget.violation_events, [])
        budget.check()

    def test_an_unmarked_response_stays_fatal_next_to_a_cache_hit(self):
        budget, s, events = self._loaded()
        events += self._memory_cache_hit("m1", f"https://{H}/Fonts/kit/stylesheet.css")
        events.append({"method": "Network.responseReceived",
                       "params": {"requestId": "zz", "response": {"url": f"https://{H}/sneaky"}}})
        s.guard.close(events)
        self.assertTrue(any("without the guard" in v for v in budget.violations))
        # the flagged request's raw events are kept for diagnosis, the cache hit's are not
        self.assertEqual({e["params"]["requestId"] for e in budget.violation_events}, {"zz"})
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_a_cache_marked_third_party_response_stays_fatal(self):
        budget, s, events = self._loaded()
        events += self._memory_cache_hit("t1", "https://tracker.example/x.css")
        s.guard.close(events)
        self.assertTrue(any("third-party" in v for v in budget.violations))
        self.assertEqual(budget.browser_cache_hits, [])
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_a_cache_marked_response_with_bytes_or_no_finish_stays_fatal(self):
        for wire, finished in ((5000, True), (0, False)):
            budget, s, events = self._loaded()
            events += self._memory_cache_hit("m1", f"https://{H}/a.css", wire, finished)
            s.guard.close(events)
            self.assertTrue(any("without the guard" in v for v in budget.violations),
                            (wire, finished))
            with self.assertRaises(G.TrafficViolation):
                budget.check()

    # a live hard stop: a demonstration went from www.viridea.it to
    # shop.viridea.it; the audit judged the earlier www loads (each paused and decided
    # by the guard) against the final host and read them as third-party
    W, S = "www.viridea.test", "shop.viridea.test"

    def _two_hosts(self):
        _c, _p, budget = setup()
        s = session(budget, [paused(0, f"https://{self.W}/", "Document"),
                             paused(1, f"https://{self.W}/a.js", "Script")])
        run(s.goto(f"https://{self.W}/"))
        s._ws.inbox.append(paused(2, f"https://{self.S}/", "Document"))
        run(s.goto(f"https://{self.S}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1", "f2"])
        for rid, url in (("n0", f"https://{self.W}/"), ("n1", f"https://{self.W}/a.js"),
                         ("n2", f"https://{self.S}/")):
            s._events.append({"method": "Network.responseReceived",
                              "params": {"requestId": rid, "response": {"url": url}}})
        return budget, s

    def test_loads_before_a_goto_to_another_host_are_judged_by_their_own_host(self):
        budget, s = self._two_hosts()
        s.guard.close(s._events)
        self.assertEqual(budget.violations, [])
        budget.check()

    def test_a_response_the_guard_never_sent_stays_fatal_after_a_host_change(self):
        budget, s = self._two_hosts()
        s._events.append({"method": "Network.responseReceived",
                          "params": {"requestId": "zz", "response": {"url": f"https://{self.W}/x"}}})
        s._events.append({"method": "Network.responseReceived",
                          "params": {"requestId": "yy", "response": {"url": f"https://{self.S}/y"}}})
        s.guard.close(s._events)
        self.assertTrue(any(f"{self.W}/x" in v for v in budget.violations))
        self.assertTrue(any("without the guard" in v and f"{self.S}/y" in v
                            for v in budget.violations))
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_websocket_is_a_violation(self):
        _c, _p, budget = setup()
        g = G.LoadGuard(budget)
        g.start(f"https://{H}/")
        g.close([{"method": "Network.webSocketCreated", "params": {"url": f"wss://{H}/ws"}}])
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_a_third_party_response_that_reached_the_network_is_fatal(self):
        # guard hardening: before, a third-party preflight was only logged as a
        # "leak"; now anything outside the origin policy that reached the network
        # stops the run
        _c, _p, budget = setup()
        g = G.LoadGuard(budget)
        g.start(f"https://{H}/")
        g.close([{"method": "Network.responseReceived",
                  "params": {"requestId": "p1", "type": "Preflight",
                             "response": {"url": "https://api.other.test/x"}}}])
        self.assertEqual(len(budget.leaks), 1)
        self.assertTrue(any("third-party request reached the network" in v
                            for v in budget.violations))
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_a_load_over_the_cap_is_a_violation(self):
        _c, _p, budget = setup()
        budget.loads[H] = [31]
        with self.assertRaises(G.TrafficViolation):
            budget.check()
        _c, pacer, budget = setup(count=201)
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_child_target_events_are_not_resumed(self):
        _c, _p, budget = setup()
        child = paused(5, f"https://{H}/w.js", "Script")
        child["sessionId"] = "child-1"
        s = session(budget, [paused(0, f"https://{H}/", "Document"), child])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])



class Methods(unittest.TestCase):
    """Guard hardening: the method is part of the request decision."""

    def test_get_and_head_are_sent_everything_else_is_refused_and_recorded(self):
        _c, pacer, budget = setup()
        script = [paused(0, f"https://{H}/", "Document"),
                  paused(1, f"https://{H}/api/q", "XHR", method="HEAD"),
                  paused(2, f"https://{H}/api/search", "XHR", method="POST"),
                  paused(3, f"https://{H}/api/item/1", "Fetch", method="DELETE"),
                  paused(4, f"https://{H}/api/item/1", "Fetch", method="put")]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f2", "f3", "f4"])
        self.assertEqual(pacer.count[H], 2)                 # refused ones never paced
        self.assertEqual([m for m, _u in budget.refused_methods], ["POST", "DELETE", "PUT"])
        self.assertTrue(all("method" in why for _u, why in s.guard.failed))

    def test_a_method_is_sent_only_when_the_callers_policy_names_it(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        budget = G.RequestBudget(pacer, asleep=clock.asleep, allow_methods=("post",))
        script = [paused(0, f"https://{H}/", "Document"),
                  paused(1, f"https://{H}/api/search", "XHR", method="POST"),
                  paused(2, f"https://{H}/api/x", "XHR", method="PATCH")]
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(budget.refused_methods, [("PATCH", f"https://{H}/api/x")])

    def test_a_refused_method_is_never_answered_from_the_cache(self):
        _c, _p, budget = setup()
        budget.cache_enabled = True
        g = G.LoadGuard(budget)
        g.start(f"https://{H}/")
        self.assertIn("method POST", g.decide(f"https://{H}/a.js", "Script", "POST"))
        self.assertFalse(g.cacheable(f"https://{H}/a.js", "Script", "POST"))



class Origins(unittest.TestCase):
    """Guard hardening: "same site" is explicit."""

    def test_default_is_same_host_only(self):
        pol = G.OriginPolicy()
        self.assertTrue(pol.allows(H, H))
        self.assertFalse(pol.allows(H, "api." + H))
        self.assertFalse(pol.allows("www.example.org", "example.org"))
        self.assertFalse(pol.allows("", H))

    def test_an_explicit_subdomain_list_per_site(self):
        pol = G.OriginPolicy({"www.example.org": ["api.example.org", "cdn.example.org"]})
        self.assertTrue(pol.allows("www.example.org", "api.example.org"))
        self.assertTrue(pol.allows("api.example.org", "www.example.org"))  # same site
        self.assertFalse(pol.allows("www.example.org", "img.example.org"))  # not listed
        self.assertFalse(pol.allows("other.test", "api.example.org"))
        self.assertEqual(pol.site_of("api.example.org"), "www.example.org")

    def test_wildcards_and_foreign_domains_are_refused(self):
        for bad in (["*.example.org"], [".example.org"], ["example.net"],
                    ["evil-example.org"], ["https://api.example.org"],
                    ["api.example.org/x"], ["org"]):
            with self.assertRaises(ValueError, msg=bad):
                G.OriginPolicy({"www.example.org": bad})
        with self.assertRaises(ValueError):
            G.OriginPolicy({"*.example.org": []})
        # a non-www site may list only its own subdomains, never a sibling
        with self.assertRaises(ValueError):
            G.OriginPolicy({"search.example.org": ["api.example.org"]})
        G.OriginPolicy({"search.example.org": ["api.search.example.org"]})

    def test_expected_file(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "origins.json")
            with open(path, "w") as fh:
                json.dump({"sites": {"www.example.org": ["api.example.org"]}}, fh)
            pol = G.OriginPolicy.from_file(path)
        self.assertEqual(pol.to_dict(), {"www.example.org": ["api.example.org"]})

    def test_listed_api_host_is_sent_and_shares_the_sites_pacer_and_count(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        pol = G.OriginPolicy({"www.example.org": ["api.example.org"]})
        budget = G.RequestBudget(pacer, asleep=clock.asleep, origin_policy=pol)
        script = [paused(0, "https://www.example.org/", "Document"),
                  paused(1, "https://api.example.org/search?q=x", "Fetch"),
                  paused(2, "https://tracker.test/t.js", "Script")]
        s = session(budget, script)
        run(s.goto("https://www.example.org/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(pacer.count, {"www.example.org": 2})
        times = [t for (_h, t, _u, _ty) in budget.sent]
        self.assertGreaterEqual(times[1] - times[0], 1.0)
        self.assertEqual(budget.third_party_refused,
                         [("www.example.org", "https://tracker.test/t.js")])
        s.guard.close([{"method": "Network.responseReceived",
                        "params": {"requestId": "n1",
                                   "response": {"url": "https://api.example.org/search?q=x"}}}])
        budget.check()

    def test_attempts_are_recorded_and_fatal_only_when_the_run_says_so(self):
        _c, _p, budget = setup()
        s = session(budget, [paused(0, f"https://{H}/", "Document"),
                             paused(1, "https://cdn.other.test/x.js")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])
        self.assertEqual(len(budget.third_party_refused), 1)
        budget.check()                                    # refused before sending
        clock = Clock()
        strict = G.RequestBudget(D.Pacer(1.0, clock=clock, sleeper=clock.sleep),
                                 asleep=clock.asleep, fatal_third_party_attempts=True)
        s = session(strict, [paused(0, f"https://{H}/", "Document"),
                             paused(1, "https://cdn.other.test/x.js")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])
        with self.assertRaises(G.TrafficViolation):
            strict.check()



class TrafficKnobs(unittest.TestCase):
    """Guard hardening: the knobs exist, today's defaults are unchanged."""

    def test_defaults_are_todays_numbers(self):
        _c, pacer, budget = setup()
        self.assertEqual((budget.per_load_cap, budget.per_host_cap, budget.site_cap),
                         (30, 200, 200))
        self.assertEqual(pacer.min_interval_s, 1.0)
        self.assertFalse(budget.realistic_browser)
        self.assertEqual(budget.nav_min_interval_s, 0.0)
        self.assertEqual(budget.allowed_types(), G.ALLOWED_TYPES)

    def test_realistic_mode_needs_an_explicit_byte_ceiling(self):
        clock = Clock()
        with self.assertRaises(ValueError):
            G.RequestBudget(D.Pacer(1.0, clock=clock, sleeper=clock.sleep),
                            realistic_browser=True)

    def test_realistic_mode_sends_same_site_css_images_fonts_up_to_the_byte_ceiling(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        budget = G.RequestBudget(pacer, asleep=clock.asleep, realistic_browser=True,
                                 static_bytes_cap=1000)
        rw = lambda i, u: {"method": "Network.requestWillBeSent",
                           "params": {"requestId": f"n{i}", "request": {"url": u}}}
        done = lambda i, n: {"method": "Network.loadingFinished",
                             "params": {"requestId": f"n{i}", "encodedDataLength": n}}
        script = [paused(0, f"https://{H}/", "Document"), rw(0, f"https://{H}/"), done(0, 300),
                  paused(1, f"https://{H}/site.css", "Stylesheet"),
                  rw(1, f"https://{H}/site.css"), done(1, 800),
                  paused(2, f"https://{H}/logo.png", "Image"),       # over the ceiling
                  paused(3, f"https://{H}/f.woff2", "Font"),
                  paused(4, f"https://{H}/v.mp4", "Media"),          # never
                  paused(5, "https://cdn.other.test/x.css", "Stylesheet"),  # third party
                  paused(6, f"https://{H}/api", "XHR")]              # byte-gated too
        s = session(budget, script)
        run(s.goto(f"https://{H}/"))
        # the per-site byte ceiling holds for EVERY request (not only
        # for CSS/images/fonts: documents/scripts overshot it live)
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(budget.site_bytes[H], 1100)
        why = dict((u, w) for u, w in s.guard.failed)
        self.assertIn("byte ceiling", why[f"https://{H}/logo.png"])
        self.assertIn("blocked", why[f"https://{H}/v.mp4"])

    def test_default_mode_still_blocks_css_images_fonts(self):
        _c, _p, budget = setup()
        s = session(budget, [paused(0, f"https://{H}/", "Document"),
                             paused(1, f"https://{H}/site.css", "Stylesheet"),
                             paused(2, f"https://{H}/a.png", "Image")])
        run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])

    def test_the_site_ceiling_counts_every_channel_and_stops_them_all(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        budget = G.RequestBudget(pacer, asleep=clock.asleep, site_cap=6)
        page = "<html><body>x</body></html>"
        http = D.HttpFetcher(pacer=pacer, max_requests_per_host=200, check_destination=False,
                             client=lambda u, h: (200, {"content-type": "text/html"}, page))
        budget.bind_fetcher(http)
        http.get(f"https://{H}/robots.txt")                  # robots
        http.get(f"https://{H}/")                            # discovery GET
        http.get(f"https://{H}/")                            # a retry

        class Inner:
            def send(self, method, url, headers, body, timeout_s):
                from anyuse.execution.api_step import ApiResponse
                return ApiResponse(200, {}, "{}")
        tr = D.PacedTransport(http, inner=Inner())
        tr.send("GET", f"https://{H}/api?q=a", {}, None, 5.0)   # proof request
        self.assertEqual(budget.site_total(H), 4)
        s = session(budget, [paused(0, f"https://{H}/b", "Document"),
                             paused(1, f"https://{H}/b.js"),
                             paused(2, f"https://{H}/c.js")])           # browser
        run(s.goto(f"https://{H}/b"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(budget.site_total(H), 6)
        self.assertIn("site ceiling", s.guard.failed[-1][1])
        self.assertIn("site ceiling", http.get(f"https://{H}/d").error)
        with self.assertRaises(PermissionError):
            tr.send("GET", f"https://{H}/api?q=b", {}, None, 5.0)
        budget.check()                                          # at, not over

    def test_listed_api_host_counts_toward_its_site_on_the_http_side_too(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        pol = G.OriginPolicy({"www.example.org": ["api.example.org"]})
        budget = G.RequestBudget(pacer, asleep=clock.asleep, origin_policy=pol)
        http = D.HttpFetcher(pacer=pacer, check_destination=False,
                             client=lambda u, h: (200, {"content-type": "text/html"}, "x"))
        budget.bind_fetcher(http)
        http.get("https://www.example.org/")
        http.get("https://api.example.org/x")
        self.assertEqual(pacer.count, {"www.example.org": 2})
        self.assertGreaterEqual(clock.sleeps[-1], 1.0 - 1e-9)   # one pacer slot

    def test_navigation_spacing_knob(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        budget = G.RequestBudget(pacer, asleep=clock.asleep, nav_min_interval_s=3.0)
        s = session(budget, [paused(0, f"https://{H}/", "Document"),
                             paused(1, f"https://{H}/a.js"),
                             paused(2, f"https://{H}/next", "Document")])
        run(s.goto(f"https://{H}/"))
        docs = [t for (_h, t, _u, ty) in budget.sent if ty == "Document"]
        self.assertGreaterEqual(docs[1] - docs[0], 3.0 - 1e-9)
        times = [t for (_h, t, _u, _ty) in budget.sent]
        self.assertAlmostEqual(times[1] - times[0], 1.0)       # sub-requests: pacer only


if __name__ == "__main__":
    unittest.main()
