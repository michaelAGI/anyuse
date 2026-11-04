"""Traffic knobs (docs/TRAFFIC_POLICY.md), offline against the fake CDP.

Every knob defaults to OFF / today's behaviour; the tests pin both sides.
"""
from __future__ import annotations

import unittest

import test_cdp_guard as TG
from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D

H = TG.H


def doc(i, url, frame="main", redirected=False):
    ev = TG.paused(i, url, "Document")
    ev["params"]["frameId"] = frame
    if redirected:
        ev["params"]["redirectedRequestId"] = f"f{i - 1}"
    return ev


def budget(**kw):
    clock = TG.Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    return clock, pacer, G.RequestBudget(pacer, asleep=clock.asleep, **kw)


def typed_session_script():
    """A start page that uses the whole 30-request load, then the Enter's
    result navigation in the same session (the stored s5r_ssoar shape)."""
    script = [doc(0, f"https://{H}/")] + [TG.paused(i, f"https://{H}/s{i}.js") for i in range(1, 30)]
    script += [doc(30, f"https://{H}/discover?query=x")]
    script += [TG.paused(i, f"https://{H}/r{i}.js") for i in range(31, 36)]
    return script


class LoadPerNavigation(unittest.TestCase):

    def test_default_one_session_is_one_load(self):
        _c, _p, b = budget()
        s = TG.session(b, typed_session_script())
        TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 30)
        self.assertIn(f"https://{H}/discover?query=x", [u for u, _w in s.guard.failed])
        s.guard.close([])
        self.assertEqual(b.loads[H], [30])

    def test_on_the_result_navigation_is_a_new_load(self):
        _c, pacer, b = budget(load_per_navigation=True)
        s = TG.session(b, typed_session_script())
        TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 36)
        s.guard.close([])
        self.assertEqual(b.loads[H], [30, 6])
        b.check()                                    # each load within 30
        times = [t for (h, t, _u, _ty) in b.sent if h == H]
        self.assertTrue(all(y - x >= 1.0 - 1e-9 for x, y in zip(times, times[1:])))

    def test_redirect_hops_and_iframes_do_not_start_a_load(self):
        _c, _p, b = budget(load_per_navigation=True)
        script = [doc(0, f"https://{H}/"), doc(1, f"https://{H}/en/", redirected=True),
                  doc(2, f"https://{H}/frame", frame="child")]
        script += [TG.paused(i, f"https://{H}/a{i}.js") for i in range(3, 40)]
        s = TG.session(b, script)
        TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 30)
        s.guard.close([])
        self.assertEqual(b.loads[H], [30])


def finished(i, failed=False):
    return {"method": "Network.loadingFailed" if failed else "Network.loadingFinished",
            "params": {"requestId": f"n{i}", "encodedDataLength": 10}}


class StaticLane(unittest.TestCase):

    def _script(self):
        script = [TG.paused(0, f"https://{H}/", "Document")]
        script += [TG.paused(i, f"https://{H}/s{i}.js") for i in range(1, 11)]
        return script

    def test_default_statics_are_paced_like_everything(self):
        clock, pacer, b = budget()
        s = TG.session(b, self._script())
        TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 11)
        self.assertEqual(clock.sleeps, [1.0] * 10)

    def test_at_most_n_in_flight_not_paced_still_counted(self):
        clock, pacer, b = budget(static_in_flight=4)
        script = self._script() + [finished(1), finished(2, failed=True)]
        s = TG.session(b, script)
        TG.run(s.goto(f"https://{H}/"))
        # 4 open at once, 2 released -> 2 more started; 4 still queued
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 1 + 4 + 2)
        self.assertEqual(len(s.guard.static_queue), 4)
        self.assertEqual(b.static_peak, 4)
        self.assertEqual(clock.sleeps, [])          # no 1 s spacing for statics
        self.assertEqual(pacer.count[H], 11)        # every one counted for the site
        s.guard.close([])
        self.assertEqual(b.loads[H], [11])

    def test_lane_respects_the_load_cap_and_documents_stay_paced(self):
        clock, pacer, b = budget(static_in_flight=4)
        script = [TG.paused(0, f"https://{H}/", "Document"),
                  TG.paused(1, f"https://{H}/api/a", "XHR")]
        script += [TG.paused(i, f"https://{H}/s{i}.js") for i in range(2, 40)]
        s = TG.session(b, script)
        TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(clock.sleeps, [1.0])       # the XHR after the document
        self.assertEqual(pacer.count[H], 30)
        self.assertTrue(s.guard.load_capped)

    def test_a_closed_page_does_not_leak_its_slots_to_the_next_session(self):
        """static_open is run-wide; a page closed with statics
        still open left their slots taken, and every later page's CSS waited
        forever (the DOM stayed at <head>)."""
        _c, _p, b = budget(static_in_flight=4)
        s1 = TG.session(b, self._script())           # 4 open, none finished
        TG.run(s1.goto(f"https://{H}/"))
        self.assertEqual(len(b.static_open), 4)
        s1.guard.close([])
        self.assertEqual(len(b.static_open), 0)
        s2 = TG.session(b, self._script())
        TG.run(s2.goto(f"https://{H}/"))
        self.assertEqual(len(s2._ws.answered("Fetch.continueRequest")), 1 + 4)
        s2.guard.close([])

    def test_a_queued_request_that_ended_never_takes_a_slot(self):
        """archive.ics.uci.edu: the Enter's navigation cancelled
        the start page's QUEUED statics (loadingFailed); the queue still held
        them, continued them later, and their slots were never freed."""
        _c, _p, b = budget(static_in_flight=4)
        script = self._script()                        # n1..n4 open, n5..n10 queued
        script += [finished(i, failed=True) for i in range(5, 11)]   # cancelled while queued
        script += [finished(i) for i in range(1, 5)]                 # the open ones finish
        s = TG.session(b, script)
        TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(s.guard.static_queue, [])
        self.assertEqual(len(b.static_open), 0)        # no slot held by a dead request
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 1 + 4)
        s.guard.close([])

    def test_zero_is_refused(self):
        with self.assertRaises(ValueError):
            budget(static_in_flight=0)


class Gate(unittest.TestCase):

    def test_default_gate_is_off(self):
        from anyuse.execution.traffic_gate import SiteGate
        g = SiteGate()
        self.assertFalse(g.enabled)
        self.assertTrue(g.holds("a.test"))
        for _ in range(5):
            self.assertTrue(g.try_acquire("a.test"))

    def test_one_task_per_site_and_k_sites(self):
        from anyuse.execution.traffic_gate import GateRefused, SiteGate
        g = SiteGate(per_site=1, max_sites=2)
        with g.task_sync("a.test"):
            with self.assertRaises(GateRefused):
                with g.task_sync("a.test"):
                    pass
            with g.task_sync("b.test"):
                self.assertFalse(g.try_acquire("c.test"))
            self.assertTrue(g.try_acquire("c.test"))
            g.release("c.test")
        self.assertEqual(g.active, {})
        self.assertEqual((g.peak_sites, g.peak_per_site), (2, 1))

    def test_async_tasks_wait_for_a_slot(self):
        import asyncio
        from anyuse.execution.traffic_gate import SiteGate
        g = SiteGate(per_site=1, max_sites=2)
        live, peak = {}, {"sites": 0, "same": 0}

        async def task(site):
            async with g.task(site):
                live[site] = live.get(site, 0) + 1
                peak["sites"] = max(peak["sites"], len([s for s, n in live.items() if n]))
                peak["same"] = max(peak["same"], max(live.values()))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                live[site] -= 1

        async def main():
            await asyncio.gather(*[task(s) for s in ["a", "a", "b", "c", "b", "d"]])
        asyncio.run(main())
        self.assertEqual(peak, {"sites": 2, "same": 1})

    def test_session_refuses_a_site_without_a_task(self):
        from anyuse.execution.traffic_gate import GateRefused, SiteGate
        g = SiteGate(per_site=1)
        _c, _p, b = budget(task_gate=g)
        s = TG.session(b, [TG.paused(0, f"https://{H}/", "Document")])
        with self.assertRaises(GateRefused):
            TG.run(s.goto(f"https://{H}/"))
        self.assertEqual(s._ws.sent, [])              # nothing was navigated
        with g.task_sync(H):
            s2 = TG.session(b, [TG.paused(0, f"https://{H}/", "Document")])
            TG.run(s2.goto(f"https://{H}/"))
            self.assertEqual(s2._ws.answered("Fetch.continueRequest"), ["f0"])


if __name__ == "__main__":
    unittest.main()
