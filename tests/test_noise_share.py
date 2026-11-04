"""Traffic accounting, offline:
the noise lane of execution/cdp_guard.py and the bounded settle of
execution/web_arch_run.py, against a fake CDP websocket and a fake page.

The failure: a chatty page's own polling used up the architecture arm's
share of the approved 300 while the arm waited up to 20 s for a quiet page; the
next navigation then failed locally ("host cap"), and 9 tasks never ran.

What must hold:

a) with ``noise_share`` on, the page's BACKGROUND requests (a data request
   outside an action window, a data request whose path names telemetry, a
   sub-frame document) are counted against the static share: sent only while
   the site's total is below ``site_cap - static_reserve``; over it they are
   failed in the browser, NOT counted, and the host is NOT stopped;
b) what an action causes (the navigation's document, the data requests during
   the action and its tail) uses the reserve;
c) off (the default) nothing changes: a background XHR at the static share is
   sent from the reserve as before;
d) the lanes are reported (static / noise / action);
e) the bounded settle ends on network idle, on a stable DOM with requests
   still in flight, or at its ceiling of a few seconds; never 20 s;
f) regression: the polling pattern (a page polls ~100 XHRs while the arm
   waits) leaves the reserve for the next navigation, which is sent.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D
from anyuse.execution import web_arch_run as RR

H = "www.chatty.example"


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
    def __init__(self, script=()):
        self.inbox = list(script)
        self.sent = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("method") not in ("Fetch.continueRequest", "Fetch.failRequest",
                                     "Fetch.fulfillRequest"):
            self.inbox.append({"id": msg["id"], "result": {}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None

    def answered(self, method):
        return [m["params"]["requestId"] for m in self.sent if m.get("method") == method]


def paused(i, url, rtype, frame="main"):
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": f"f{i}", "networkId": f"n{i}", "resourceType": rtype,
                       "frameId": frame, "request": {"url": url, "method": "GET"}}}


def budget(noise=True, reserve=50, cap=110):
    clock = Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    b = G.RequestBudget(pacer, per_load_cap=cap, per_host_cap=cap, site_cap=cap,
                        asleep=clock.asleep, load_per_navigation=True, realistic_browser=True,
                        static_bytes_cap=30 * 1024 * 1024, static_in_flight=4,
                        static_reserve=reserve, skip_types=("Image", "Media", "Font"),
                        noise_share=noise)
    return clock, pacer, b


def session(b):
    s = G.GuardedSession("http://192.0.2.1:9222", budget=b, allow_url=lambda u: True)
    s._ws = FakeWS()
    return s


async def load(s, script):
    """A navigation (an action) whose page sends ``script``."""
    s._ws.inbox.extend(script)
    await s.goto(f"https://{H}/")


async def idle(s, clock, script, after_s=60.0):
    """The page's own requests while the arm is NOT acting (planning, a model call)."""
    clock.t += after_s                     # well past the action's tail
    s._ws.inbox.extend(script)
    await s.settle(0.1)


class NoiseLane(unittest.TestCase):
    def test_a_background_xhr_over_the_static_share_is_skipped_uncounted_host_not_stopped(self):
        clock, pacer, b = budget()
        s = session(b)

        async def go():
            await load(s, [paused(0, f"https://{H}/", "Document")])
            pacer.count[H] = 60                           # the static share (110 - 50) is used
            await idle(s, clock, [paused(1, f"https://{H}/api/poll?t=1", "XHR"),
                                  paused(2, f"https://{H}/api/poll?t=2", "Fetch")])
        asyncio.run(go())
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1", "f2"])
        self.assertEqual(pacer.count[H], 60)
        self.assertEqual(b.noise_skipped[H], 2)
        self.assertNotIn(H, b.stopped)
        self.assertTrue(all("noise share" in w for _u, w in s.guard.failed))

    def test_a_background_xhr_below_the_static_share_is_sent_and_counted_as_noise(self):
        clock, pacer, b = budget()
        s = session(b)

        async def go():
            await load(s, [paused(0, f"https://{H}/", "Document")])
            await idle(s, clock, [paused(1, f"https://{H}/api/poll", "XHR")])
        asyncio.run(go())
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(b.lanes[H], {"action": 1, "noise": 1})

    def test_b_an_actions_data_uses_the_reserve(self):
        clock, pacer, b = budget()
        s = session(b)
        pacer.count[H] = 80                               # statics and noise can no longer go
        asyncio.run(load(s, [paused(0, f"https://{H}/search?q=tea", "Document"),
                             paused(1, f"https://{H}/api/results?q=tea", "XHR"),
                             paused(2, f"https://{H}/app.js", "Script")]))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f2"])
        self.assertEqual(pacer.count[H], 82)
        self.assertEqual(b.lanes[H], {"action": 2})

    def test_b_the_tail_after_an_action_still_counts_as_the_action(self):
        clock, pacer, b = budget()
        s = session(b)

        async def go():
            await load(s, [paused(0, f"https://{H}/", "Document")])
            pacer.count[H] = 70
            clock.t += 2.0                                # inside the 5 s tail
            s._ws.inbox.append(paused(1, f"https://{H}/api/lazy-list", "XHR"))
            await s.settle(0.1)
        asyncio.run(go())
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1"])

    def test_a_telemetry_path_is_noise_even_inside_an_action(self):
        clock, pacer, b = budget()
        s = session(b)
        pacer.count[H] = 70
        asyncio.run(load(s, [paused(0, f"https://{H}/", "Document"),
                             paused(1, f"https://{H}/api/collect?e=view", "XHR"),
                             paused(2, f"https://{H}/.well-known/shopify/monorail/unstable/produce_batch",
                                    "XHR"),
                             paused(3, f"https://{H}/rum/beacon", "Fetch")]))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])
        self.assertEqual(b.noise_skipped[H], 3)

    def test_a_subframe_document_is_noise_the_main_frame_is_not(self):
        clock, pacer, b = budget()
        s = session(b)
        pacer.count[H] = 70
        asyncio.run(load(s, [paused(0, f"https://{H}/", "Document", frame="main"),
                             paused(1, f"https://{H}/web-pixels@1/sandbox/", "Document", frame="sub1")]))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])

    def test_c_off_by_default_nothing_changes(self):
        clock, pacer, b = budget(noise=False)
        s = session(b)

        async def go():
            await load(s, [paused(0, f"https://{H}/", "Document")])
            pacer.count[H] = 70
            await idle(s, clock, [paused(1, f"https://{H}/api/poll", "XHR"),
                                  paused(2, f"https://{H}/api/collect", "XHR")])
        asyncio.run(go())
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f1", "f2"])
        self.assertEqual(b.noise_skipped, {})
        self.assertEqual(G.RequestBudget(D.Pacer(1.0)).noise_share, False)

    def test_f_regression_prior_pattern_the_next_navigation_gets_through(self):
        # www.cuisineaz.com and six more sites: the page kept polling while the
        # arch waited; the arm's 110 were used up and the next page failed locally
        clock, pacer, b = budget()
        s = session(b)

        async def go():
            await load(s, [paused(0, f"https://{H}/", "Document")] +
                       [paused(i, f"https://{H}/s{i}.js", "Script") for i in range(1, 40)])
            for k in range(100):
                await idle(s, clock, [paused(100 + k, f"https://{H}/api/poll?k={k}", "XHR")], after_s=10)
            s2 = session(b)
            await load(s2, [paused(900, f"https://{H}/search?q=soup", "Document")])
            return s2
        s2 = asyncio.run(go())
        self.assertLessEqual(pacer.count[H], 61)
        self.assertNotIn(H, b.stopped)
        self.assertEqual(s2._ws.answered("Fetch.continueRequest"), ["f900"])
        b.check()

    def test_f_without_the_lane_the_polling_stops_the_host(self):
        clock, pacer, b = budget(noise=False)
        s = session(b)

        async def go():
            await load(s, [paused(0, f"https://{H}/", "Document")])
            for k in range(120):
                await idle(s, clock, [paused(100 + k, f"https://{H}/api/poll?k={k}", "XHR")], after_s=10)
            s2 = session(b)
            await load(s2, [paused(900, f"https://{H}/search?q=soup", "Document")])
            return s2
        s2 = asyncio.run(go())
        self.assertIn(H, b.stopped)
        self.assertEqual(s2._ws.answered("Fetch.failRequest"), ["f900"])


class FakeSettlePage:
    """A page whose DOM mutates until ``mutate_until`` s and whose request log
    always has ``inflight`` open XHRs (a long poll)."""

    def __init__(self, clock, mutate_until=0.0, inflight=0, ready="complete"):
        self.clock, self.mutate_until, self.ready = clock, mutate_until, ready
        self.t0 = clock()
        self._events = [{"method": "Network.requestWillBeSent",
                         "params": {"requestId": f"r{i}", "type": "XHR", "request": {"url": "x"}}}
                        for i in range(inflight)]

    async def js(self, expr, read_only=False):
        el = self.clock() - self.t0
        quiet = 0 if el < self.mutate_until else (el - self.mutate_until) * 1000
        return {"quiet_ms": quiet, "mutations": 1, "ready": self.ready}

    async def settle(self, s):
        self.clock.t += s


class BoundedSettle(unittest.TestCase):
    def test_e_decision(self):
        d = RR.settle_decision
        self.assertEqual(d({"ready": "complete", "quiet_ms": 600}, 1.0, 0), "idle")
        self.assertEqual(d({"ready": "complete", "quiet_ms": 600}, 1.0, 2), "wait")
        self.assertEqual(d({"ready": "complete", "quiet_ms": 1300}, 2.0, 2), "dom_stable")
        self.assertEqual(d({"ready": "loading", "quiet_ms": 5000}, 2.0, 0), "wait")
        self.assertEqual(d(None, 5.0, 0), "ceiling")
        self.assertLessEqual(RR.SETTLE_MAX_S, 6.0)

    def test_e_a_chatty_page_settles_on_a_stable_dom(self):
        clock = Clock()
        page = FakeSettlePage(clock, mutate_until=1.0, inflight=2)
        rep = asyncio.run(RR.settle_bounded(page, clock=clock))
        self.assertEqual(rep["ended"], "dom_stable")
        self.assertLess(rep["waited_s"], 3.0)

    def test_e_a_quiet_page_settles_on_idle(self):
        clock = Clock()
        rep = asyncio.run(RR.settle_bounded(FakeSettlePage(clock, mutate_until=0.2), clock=clock))
        self.assertEqual(rep["ended"], "idle")
        self.assertLess(rep["waited_s"], 1.5)

    def test_e_a_page_that_never_settles_stops_at_the_ceiling(self):
        clock = Clock()
        rep = asyncio.run(RR.settle_bounded(FakeSettlePage(clock, mutate_until=99, inflight=1), clock=clock))
        self.assertEqual(rep["ended"], "ceiling")
        self.assertLessEqual(rep["waited_s"], RR.SETTLE_MAX_S + RR.SETTLE_POLL_S)


if __name__ == "__main__":
    unittest.main()
