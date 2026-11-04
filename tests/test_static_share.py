"""The static share and benchmark static skipping in execution/cdp_guard.py,
offline against a fake CDP websocket (the failure: heavy start pages
spent the whole 300-request site cap on statics, then every search Document was
failed locally as "host stopped").

a) statics may use at most site_cap - static_reserve; over it they are skipped
   in the browser, NOT counted, and the host is NOT stopped;
b) benchmark mode skips Image, Media and Font: never sent, never counted;
c) a static at any cap never stops the host; only Document/XHR/Fetch can;
regression: the bea.gov pattern (about 300 statics on the start page, then 4
search Documents) -> the searches get through."""
from __future__ import annotations

import asyncio
import json
import unittest

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D

H = "www.bea.example"


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
    def __init__(self, script):
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


def paused(i, url, rtype):
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": f"f{i}", "networkId": f"n{i}", "resourceType": rtype,
                       "request": {"url": url, "method": "GET"}}}


def budget(**kw):
    clock = Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    kw.setdefault("realistic_browser", True)
    kw.setdefault("static_bytes_cap", 30 * 1024 * 1024)
    b = G.RequestBudget(pacer, per_load_cap=300, per_host_cap=300, site_cap=300,
                        asleep=clock.asleep, load_per_navigation=True, **kw)
    return pacer, b


def run_script(b, script):
    s = G.GuardedSession("http://192.0.2.1:9222", budget=b, allow_url=lambda u: True)
    s._ws = FakeWS(script)
    asyncio.run(s.goto(f"https://{H}/"))
    return s


STATIC_MIX = ("Script", "Stylesheet", "Image", "Font")


class StaticShare(unittest.TestCase):
    def test_a_statics_stop_at_the_share_uncounted_host_not_stopped(self):
        pacer, b = budget(static_reserve=120)
        script = [paused(0, f"https://{H}/", "Document")] + [
            paused(i, f"https://{H}/s{i}.js", "Script") for i in range(1, 260)]
        s = run_script(b, script)
        self.assertEqual(pacer.count[H], 180)            # 1 document + 179 statics
        self.assertEqual(len(s._ws.answered("Fetch.continueRequest")), 180)
        self.assertEqual(b.static_skipped[H], 259 - 179)
        self.assertNotIn(H, b.stopped)
        self.assertTrue(all("static share" in w for _u, w in s.guard.failed))

    def test_a_documents_use_the_reserve(self):
        pacer, b = budget(static_reserve=120)
        pacer.count[H] = 250                              # statics can no longer go
        s = run_script(b, [paused(0, f"https://{H}/", "Document"),
                           paused(1, f"https://{H}/a.css", "Stylesheet"),
                           paused(2, f"https://{H}/api?q=x", "XHR")])
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f2"])
        self.assertEqual(pacer.count[H], 252)

    def test_b_benchmark_skips_images_media_fonts(self):
        pacer, b = budget(static_reserve=120, skip_types=("Image", "Media", "Font"))
        s = run_script(b, [paused(0, f"https://{H}/", "Document"),
                           paused(1, f"https://{H}/a.png", "Image"),
                           paused(2, f"https://{H}/f.woff2", "Font"),
                           paused(3, f"https://{H}/v.mp4", "Media"),
                           paused(4, f"https://{H}/a.css", "Stylesheet"),
                           paused(5, f"https://{H}/a.js", "Script")])
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0", "f4", "f5"])
        self.assertEqual(pacer.count[H], 3)
        self.assertNotIn(H, b.stopped)

    def test_c_a_static_at_the_site_cap_never_stops_the_host(self):
        pacer, b = budget()                               # no reserve: today's caps
        pacer.count[H] = 300
        s = run_script(b, [paused(1, f"https://{H}/a.js", "Script")])
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])
        self.assertNotIn(H, b.stopped)
        s2 = run_script(b, [paused(2, f"https://{H}/next", "Document")])
        self.assertEqual(s2._ws.answered("Fetch.failRequest"), ["f2"])
        self.assertIn(H, b.stopped, "a document at the cap still stops the host")

    def test_byte_ceiling_holds_for_every_request_type(self):
        pacer, b = budget(static_bytes_cap=1000)
        b.site_bytes[H] = 1000
        s = run_script(b, [paused(0, f"https://{H}/", "Document"),
                           paused(1, f"https://{H}/a.js", "Script")])
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f0", "f1"])
        self.assertEqual(pacer.count.get(H, 0), 0)
        self.assertIn(H, b.stopped)

    def test_reserve_must_be_below_the_cap(self):
        with self.assertRaises(ValueError):
            budget(static_reserve=300)

    def test_regression_bea_pattern_searches_get_through(self):
        # www.bea.gov (ARM T): ~300 statics on the start page used
        # the whole cap; the 4 search Documents then failed in 0.03 s as
        # "host stopped: host cap of 300 reached"
        pacer, b = budget(static_reserve=120, skip_types=("Image", "Media", "Font"))
        start = [paused(0, f"https://{H}/", "Document")] + [
            paused(i, f"https://{H}/asset{i}", STATIC_MIX[i % 4]) for i in range(1, 301)]
        s = run_script(b, start)
        self.assertNotIn(H, b.stopped)
        self.assertLessEqual(pacer.count[H], 180)
        for k in range(4):
            sk = run_script(b, [paused(1000 + k, f"https://{H}/search?q=v{k}", "Document")])
            self.assertEqual(sk._ws.answered("Fetch.continueRequest"), [f"f{1000 + k}"], k)
        self.assertNotIn(H, b.stopped)
        b.check()

    def test_regression_without_the_fix_the_searches_fail(self):
        # the same pattern under the old guard: the cap is spent by statics
        pacer, b = budget()
        start = [paused(0, f"https://{H}/", "Document")] + [
            paused(i, f"https://{H}/asset{i}", STATIC_MIX[i % 4]) for i in range(1, 301)]
        run_script(b, start)
        self.assertEqual(pacer.count[H], 300)
        sk = run_script(b, [paused(1000, f"https://{H}/search?q=v", "Document")])
        self.assertEqual(sk._ws.answered("Fetch.failRequest"), ["f1000"])


if __name__ == "__main__":
    unittest.main()
