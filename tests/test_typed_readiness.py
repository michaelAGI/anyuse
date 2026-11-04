"""Typed-search readiness: mutation settle + in-flight requests, offline.

A scripted fake session (no browser, no socket) answers the settle probe and
advances a fake clock on every settle() call.
"""
from __future__ import annotations

import asyncio
import unittest

from anyuse.execution import typed_readiness as T


class FakeSession:
    def __init__(self, answers, events=None, clock=None):
        self.answers = list(answers)
        self._events = list(events or [])
        self.clock = clock
        self.settled = []

    async def js(self, expr):
        if not self.answers:
            return {"quiet_ms": 10_000, "mutations": 0, "ready": "complete"}
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    async def settle(self, s):
        self.settled.append(s)
        self.clock.t += s


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def busy(q=0):
    return {"quiet_ms": q, "mutations": 5, "ready": "complete"}


def run(c):
    return asyncio.run(c)


class Decide(unittest.TestCase):

    def test_pure_rules(self):
        ok = {"quiet_ms": 600, "ready": "complete"}
        self.assertEqual(T.decide(ok, 0.1, ceiling_s=3), "quiet")
        self.assertEqual(T.decide({"quiet_ms": 100, "ready": "complete"}, 0.1, ceiling_s=3), "wait")
        self.assertEqual(T.decide({"quiet_ms": 900, "ready": "loading"}, 0.1, ceiling_s=3), "wait")
        self.assertEqual(T.decide(ok, 0.1, ceiling_s=3, inflight=1), "wait")
        self.assertEqual(T.decide(ok, 3.0, ceiling_s=3, inflight=1), "ceiling")
        # the grace applies only while a VALUE-bearing request is unanswered
        self.assertEqual(T.decide(ok, 3.5, ceiling_s=3, inflight=1, inflight_value=1,
                                  inflight_grace_s=2), "wait")
        self.assertEqual(T.decide(ok, 5.0, ceiling_s=3, inflight=1, inflight_value=1,
                                  inflight_grace_s=2), "inflight_ceiling")
        self.assertEqual(T.decide(ok, 3.0, ceiling_s=3, inflight=1, inflight_value=0,
                                  inflight_grace_s=2), "ceiling")

    def test_inflight_from_the_event_log(self):
        ev = [{"method": "Network.requestWillBeSent",
               "params": {"requestId": "1", "type": "XHR",
                          "request": {"url": "https://h.test/api?q=red+fox"}}},
              {"method": "Network.requestWillBeSent",
               "params": {"requestId": "2", "type": "Fetch", "request": {"url": "https://h.test/x"}}},
              {"method": "Network.requestWillBeSent",
               "params": {"requestId": "3", "type": "Script", "request": {"url": "https://h.test/a.js"}}},
              {"method": "Network.loadingFinished", "params": {"requestId": "2"}}]
        self.assertEqual(T.inflight_requests(ev, "red fox"), {"inflight": 1, "value": 1})
        ev.append({"method": "Network.loadingFailed", "params": {"requestId": "1"}})
        self.assertEqual(T.inflight_requests(ev, "red fox"), {"inflight": 0, "value": 0})

    def test_answered_value_calls_end_the_wait_while_other_requests_stay_open(self):
        ok = {"quiet_ms": 600, "ready": "complete"}
        self.assertEqual(T.decide(ok, 0.5, ceiling_s=3, inflight=1, value_answered=1),
                         "value_answered")
        # a request carrying the value that is still open holds the wait
        self.assertEqual(T.decide(ok, 0.5, ceiling_s=3, inflight=2, inflight_value=1,
                                  value_answered=1), "wait")
        # the DOM must still be quiet
        self.assertEqual(T.decide({"quiet_ms": 100, "ready": "complete"}, 0.5, ceiling_s=3,
                                  inflight=1, value_answered=1), "wait")
        self.assertEqual(T.decide(ok, 0.5, ceiling_s=3, inflight=1), "wait")

    def test_a_request_carries_the_value_in_any_encoding_the_page_sends(self):
        forms = T.value_forms("Red Fox")
        self.assertEqual(forms[0], "red fox")
        self.assertIn("red-fox", forms)
        for url, post in (("https://h.test/api?q=red+fox", ""), ("https://h.test/api?q=Red%20Fox", ""),
                          ("https://h.test/s/red-fox", ""), ("https://h.test/graphql",
                                                             '{"query": "Red Fox"}'),
                          ("https://h.test/api?q=RED%20FOX", "")):
            self.assertTrue(T.carries_value(url, post, forms), url)
        self.assertTrue(T.carries_value("https://h.test/api", '{"q":"caf\\u00e9"}',
                                        T.value_forms("café")))
        # a prefix sent while the word was typed does not carry the whole value
        self.assertFalse(T.carries_value("https://h.test/api?q=red+fo", "", forms))
        # a body that encodes more than it needs is read decoded
        self.assertTrue(T.carries_value("https://h.test/api", "q=%72ed+%66ox", forms))
        self.assertFalse(T.carries_value("https://h.test/poll?id=1", "", forms))
        self.assertEqual(T.value_forms("  "), ())
        self.assertFalse(T.carries_value("https://h.test/?q=", "", ()))

    def test_the_host_does_not_carry_the_value(self):
        # searching crates.io for "crates": the site's own name is not the typed value
        forms = T.value_forms("crates")
        self.assertFalse(T.carries_value("https://crates.io/api/v1/summary", "", forms))
        self.assertTrue(T.carries_value("https://crates.io/search?q=crates", "", forms))
        ev = [{"method": "Network.requestWillBeSent",
               "params": {"requestId": "p", "type": "XHR",
                          "request": {"url": "https://crates.io/api/poll"}}}]
        self.assertEqual(T.inflight_requests(ev, "crates"), {"inflight": 1, "value": 0})


class Wait(unittest.TestCase):

    def test_quiet_page_ends_before_the_timer(self):
        c = Clock()
        s = FakeSession([busy(0), busy(200), busy(700)], clock=c)
        rep = run(T.wait_settled(s, 3.0, clock=c, poll_s=0.15))
        self.assertEqual(rep.ended, "quiet")
        self.assertLess(rep.waited_s, 1.0)

    def test_busy_page_stops_at_the_timer(self):
        c = Clock()
        s = FakeSession([busy(0)] * 100, clock=c)
        rep = run(T.wait_settled(s, 3.0, clock=c, poll_s=0.5))
        self.assertEqual(rep.ended, "ceiling")
        self.assertGreaterEqual(rep.waited_s, 3.0)
        self.assertLess(rep.waited_s, 3.6)

    def test_value_request_in_flight_extends_only_by_the_grace(self):
        c = Clock()
        ev = [{"method": "Network.requestWillBeSent",
               "params": {"requestId": "1", "type": "Fetch",
                          "request": {"url": "https://h.test/find?q=slow"}}}]
        s = FakeSession([], events=ev, clock=c)
        rep = run(T.wait_settled(s, 2.0, value="slow", clock=c, poll_s=0.5,
                                 inflight_grace_s=3.0))
        self.assertEqual(rep.ended, "inflight_ceiling")
        self.assertGreaterEqual(rep.waited_s, 5.0)
        c2 = Clock()
        s2 = FakeSession([], events=ev, clock=c2)
        rep2 = run(T.wait_settled(s2, 2.0, value="slow", clock=c2, poll_s=0.5))
        self.assertEqual(rep2.ended, "ceiling")          # default: no extension

    def test_answered_value_counts_only_requests_after_since(self):
        def sent(rid, url, type_="XHR"):
            return {"method": "Network.requestWillBeSent",
                    "params": {"requestId": rid, "type": type_, "request": {"url": url}}}

        def done(rid, failed=False):
            return {"method": "Network.loadingFailed" if failed else "Network.loadingFinished",
                    "params": {"requestId": rid}}
        ev = [sent("1", "https://h.test/api?q=red+fox"), done("1")]
        since = len(ev)
        ev += [sent("2", "https://h.test/api?q=red+fox&page=1"), done("2"),
               sent("3", "https://h.test/api?q=red+fox&page=2"), done("3", failed=True),
               sent("4", "https://h.test/other"), done("4"),
               sent("5", "https://h.test/poll?id=1")]
        self.assertEqual(T.inflight_requests(ev, "red fox", since=since),
                         {"inflight": 1, "value": 0, "answered_value": 1})
        self.assertEqual(T.inflight_requests(ev, "red fox", since=0)["answered_value"], 2)
        self.assertNotIn("answered_value", T.inflight_requests(ev, "red fox"))

    def test_a_long_poll_does_not_hold_the_wait_once_the_value_call_answered(self):
        ev = [{"method": "Network.requestWillBeSent",
               "params": {"requestId": "p", "type": "XHR",
                          "request": {"url": "https://h.test/poll?id=1"}}},
              {"method": "Network.requestWillBeSent",
               "params": {"requestId": "v", "type": "Fetch",
                          "request": {"url": "https://h.test/find?q=slow"}}},
              {"method": "Network.loadingFinished", "params": {"requestId": "v"}}]
        c = Clock()
        rep = run(T.wait_settled(FakeSession([busy(0), busy(600)], events=ev, clock=c), 3.0,
                                 value="slow", clock=c, poll_s=0.15, since=1))
        self.assertEqual(rep.ended, "value_answered")
        self.assertLess(rep.waited_s, 0.5)
        # the value call answered before the action: the long poll holds it as before
        c2 = Clock()
        rep2 = run(T.wait_settled(FakeSession([busy(0), busy(600)], events=ev, clock=c2), 3.0,
                                  value="slow", clock=c2, poll_s=0.15, since=3))
        self.assertEqual(rep2.ended, "ceiling")

    def test_page_that_cannot_answer_falls_back_to_the_timer(self):
        c = Clock()
        s = FakeSession([None, "", RuntimeError("context destroyed")], clock=c)
        rep = run(T.wait_settled(s, 3.0, clock=c, poll_s=0.15))
        self.assertEqual(rep.ended, "timer")
        self.assertAlmostEqual(c.t, 3.0, places=6)


class TypeSearch(unittest.TestCase):
    """GuardedSession.type_search against a fake CDP websocket."""

    def _session(self, wipe_first: bool):
        import json
        from anyuse.execution import cdp_guard as G
        from anyuse.execution import discover as D

        class WS:
            def __init__(self):
                self.inbox, self.sent, self.typed, self.value_reads = [], [], [], 0

            async def send(self, raw):
                msg = json.loads(raw)
                self.sent.append(msg)
                m, p = msg.get("method"), msg.get("params") or {}
                if m in ("Fetch.continueRequest", "Fetch.failRequest"):
                    return
                val = None
                if m == "Runtime.evaluate":
                    e = p["expression"]
                    if "__anyuseSettle" in e:
                        val = {"quiet_ms": 900, "mutations": 1, "ready": "complete"}
                    elif "querySelectorAll('input').forEach" in e:
                        val = [{"index": 0, "visible": True, "name": "q", "id": "", "type": "search"}]
                    elif "activeElement" in e:
                        val = True
                    elif "String(el.value" in e:
                        self.value_reads += 1
                        val = "" if (wipe_first and self.value_reads == 1) else self.typed[-1]
                    elif "location.href" in e:
                        val = "https://h.test/"
                elif m == "Input.insertText":
                    self.typed.append(p["text"])
                self.inbox.append({"id": msg["id"], "result": {"result": {"value": val}}})

            async def recv(self):
                if not self.inbox:
                    raise asyncio.TimeoutError()
                return json.dumps(self.inbox.pop(0))

        pacer = D.Pacer(1.0)
        s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(pacer))
        s._ws = WS()
        return s

    def test_a_wiped_value_is_typed_once_more(self):
        s = self._session(wipe_first=True)
        out = run(s.type_search("red fox"))
        self.assertTrue(out.get("retyped"))
        self.assertEqual(s._ws.typed, ["red fox", "red fox"])
        self.assertEqual(out["settle"]["enter"]["ended"], "quiet")

    def test_a_kept_value_is_typed_once_and_ends_on_quiet(self):
        s = self._session(wipe_first=False)
        out = run(s.type_search("red fox"))
        self.assertNotIn("retyped", out)
        self.assertEqual(s._ws.typed, ["red fox"])
        self.assertEqual({k: v["ended"] for k, v in out["settle"].items()},
                         {"before": "quiet", "typed": "quiet", "enter": "quiet"})

    def test_enter_waits_for_a_held_navigation_to_be_sent(self):
        """A navigation the guard holds for the spacing must not be
        read as 'settled on the old URL'."""
        s = self._session(wipe_first=False)
        s.budget.nav_min_interval_s = 3.0
        polls = []

        async def settle(sec):
            polls.append(sec)
            if len(polls) == 2:          # Enter asked for a navigation
                s._events.append({"method": "Page.frameRequestedNavigation", "params": {}})
            if len(polls) == 6:          # ... and the guard released its document
                s.budget.sent.append(("h.test", 0.0, "https://h.test/s?q=x", "Document"))
        s.settle = settle
        out = run(s._await_enter_navigation(0, 0))
        self.assertTrue(out["requested"] and out["sent"], out)
        self.assertEqual(len(polls), 6)

    def test_enter_without_navigation_returns_after_detection(self):
        s = self._session(wipe_first=False)
        s.settle = lambda sec: asyncio.sleep(0.01)
        out = run(s._await_enter_navigation(0, 0, detect_s=0.05))
        self.assertFalse(out["requested"])

    def test_the_typed_waits_use_the_page_settle_stable_rule(self):
        from unittest import mock
        calls = []
        real = T.wait_settled

        async def record(session, ceiling_s, **kw):
            calls.append(kw)
            return await real(session, ceiling_s, **kw)
        s = self._session(wipe_first=True)
        with mock.patch.object(T, "wait_settled", record):
            out = run(s.type_search("red fox"))
        self.assertEqual(set(out["settle"]), {"before", "typed", "retyped", "enter"})
        self.assertEqual(len(calls), 4)
        for kw in calls:
            self.assertEqual(kw.get("stable_ms"), T.PAGE_SETTLE_STABLE_MS, kw)
        by_value = [kw for kw in calls if kw.get("value")]
        self.assertEqual(len(by_value), 3)
        for kw in by_value:
            self.assertIsInstance(kw.get("since"), int, kw)
        self.assertIsNone(calls[0].get("since"))

    def test_a_typed_search_is_not_held_by_a_long_poll(self):
        """An open request without the typed value (the page's long poll) holds no stage to
        its ceiling once the request carrying the value answered."""
        s = self._session(wipe_first=False)
        insert = s.call

        async def call(method, params=None, **kw):
            res = await insert(method, params, **kw)
            if method == "Input.insertText":
                # typing opens the page's long poll and sends the search call, which answers
                s._events.extend([
                    {"method": "Network.requestWillBeSent",
                     "params": {"requestId": "poll", "type": "XHR",
                                "request": {"url": "https://h.test/poll?id=1"}}},
                    {"method": "Network.requestWillBeSent",
                     "params": {"requestId": "v", "type": "Fetch",
                                "request": {"url": "https://h.test/api?q=red%20fox"}}},
                    {"method": "Network.loadingFinished", "params": {"requestId": "v"}}])
            return res
        s.call = call
        out = run(s.type_search("red fox", enter_settle_s=0.3))
        self.assertEqual(out["settle"]["typed"]["ended"], "value_answered", out["settle"])
        self.assertLess(out["settle"]["typed"]["waited_s"], 1.0, out["settle"])
        # Enter sent no call carrying the value, so the open poll holds that stage as before
        self.assertEqual(out["settle"]["enter"]["ended"], "ceiling", out["settle"])

    def test_readiness_off_is_the_old_timer_path(self):
        s = self._session(wipe_first=True)
        s.typed_readiness = False
        s.settle = lambda sec: asyncio.sleep(0)
        out = run(s.type_search("red fox"))
        self.assertNotIn("settle", out)
        self.assertEqual(s._ws.typed, ["red fox"])


if __name__ == "__main__":
    unittest.main()
