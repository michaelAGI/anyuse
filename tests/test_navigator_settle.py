"""The navigator's read wait (``execution/agent_browser.Navigator._settle``), offline with a
fake page on a fake clock: no browser, no network, no model.

What must hold:

1. a page that ships a PLACEHOLDER title and sets the real one after its own fetch (pursuit.purescript.org:
   "Pursuit", then "purescript-<pkg> - Pursuit") is read with the REAL title; the old rule (title length only,
   no fetch window) read the placeholder;
2. an own fetch still in flight holds the read; a third-party fetch does not;
3. a title that never stops changing, or an own fetch that never ends, is read at the HARD CAP (``ceiling_s``) - never
   later;
4. a session without ``js`` still just waits ``extra_settle_s``;
5. what is read is unchanged: the wait changes timing only (the read happens after ``_settle``, as before).
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution.agent_browser import Navigator  # noqa: E402

PLACEHOLDER = "Pursuit"
REAL = "purescript-arrays - Pursuit"
URL = "https://pursuit.purescript.org/packages/purescript-arrays/7.3.0"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Page:
    """A page on the fake clock: ``timeline`` holds (t, title, document length, fetches) from t=0 of the navigation;
    ``requests`` the network log: (url, t_start, t_end or None)."""

    def __init__(self, clock, timeline, requests=(), url=URL):
        self.clock, self.timeline, self.requests, self.u = clock, timeline, list(requests), url
        self.t0 = None
        self.reads = []
        self.net_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def rel(self):
        return self.clock.t - (self.t0 if self.t0 is not None else self.clock.t)

    def state(self):
        cur = self.timeline[0]
        for row in self.timeline:
            if row[0] <= self.rel():
                cur = row
        return cur

    async def goto(self, url):
        self.t0 = self.clock.t

    async def js(self, expr):
        _t, title, length, fetches = self.state()
        return {"ready": "complete", "len": length, "title": title, "fetches": fetches}

    async def network(self, want_bodies=True):
        self.net_calls.append(want_bodies)
        out = []
        for u, a, b in self.requests:
            if a <= self.rel():
                done = b is not None and b <= self.rel()
                out.append({"url": u, "resource_type": "XHR", "t_start": a, "t_end": b if done else None,
                            "status": 200 if done else None})
        return out

    async def settle(self, s):
        self.clock.t += s

    async def url(self):
        return self.u

    async def dom(self):
        title = self.state()[1]
        self.reads.append(("dom", title, round(self.rel(), 2)))
        return f"<html><head><title>{title}</title></head><body>x</body></html>"

    async def text(self):
        return self.state()[1]


def navigate(page, clock, **kw):
    nav = Navigator("http://192.168.64.5:9222", session_factory=lambda endpoint, **k: page, clock=clock, **kw)
    run = nav(URL, "arrays")
    return run, page.reads[-1]


def placeholder_page(clock):
    # complete and still at once with the placeholder; the page's own fetch (issued at boot) runs 0.4-2.5 s; the real
    # title at 2.6 s
    return Page(clock, [(0.0, PLACEHOLDER, 50000, 0), (2.5, PLACEHOLDER, 50000, 1), (2.6, REAL, 50020, 1)],
                [("https://pursuit.purescript.org/packages/purescript-arrays/7.3.0/readme", 0.4, 2.5)])


def test_the_placeholder_title_is_not_read():
    clock = Clock()
    page = placeholder_page(clock)
    run, (_k, title, at) = navigate(page, clock)
    assert run.ok and title == REAL and at >= 2.6 + 1.0, (title, at, run.error)
    # the wait reads the network log without bodies; only the recording at the end asks for them (as before)
    assert page.net_calls[-1] is True and set(page.net_calls[:-1]) == {False}
    # the old rule (no title window, no fetch window) read the placeholder - the defect
    clock = Clock()
    run, (_k, title, at) = navigate(placeholder_page(clock), clock, title_stable_ms=0, quiet_ms=0)
    assert title == PLACEHOLDER and at < 2.5, (title, at)


def test_an_own_fetch_in_flight_holds_a_third_party_one_does_not():
    clock = Clock()
    page = Page(clock, [(0.0, REAL, 50000, 0)], [("https://pursuit.purescript.org/api/x", 0.5, 4.0)])
    _run, (_k, title, at) = navigate(page, clock)
    assert title == REAL and at >= 4.0 + 1.0, at
    clock = Clock()
    page = Page(clock, [(0.0, REAL, 50000, 0)], [("https://tracker.example.net/beacon", 0.5, None)])
    _run, (_k, title, at) = navigate(page, clock)
    assert title == REAL and at < 2.0, at


def test_the_hard_cap():
    clock = Clock()
    ticker = Page(clock, [(i * 0.5, f"{i} new messages - Pursuit", 50000, 0) for i in range(100)])
    _run, (_k, _title, at) = navigate(ticker, clock, ceiling_s=10.0)
    assert 10.0 <= at <= 10.3, at
    clock = Clock()
    hung = Page(clock, [(0.0, REAL, 50000, 0)], [("https://pursuit.purescript.org/api/stream", 0.2, None)])
    _run, (_k, _title, at) = navigate(hung, clock, ceiling_s=10.0)
    assert 10.0 <= at <= 10.3, at


def test_a_session_without_js_just_waits():
    clock = Clock()

    class NoJs(Page):
        js = None
    page = NoJs(clock, [(0.0, REAL, 50000, 0)])
    nav = Navigator("http://192.168.64.5:9222", session_factory=lambda endpoint, **k: page, clock=clock,
                    extra_settle_s=2.0)
    run = nav(URL, "arrays")
    assert run.ok and page.reads[-1][2] == 2.0


if __name__ == "__main__":
    test_the_placeholder_title_is_not_read()
    test_an_own_fetch_in_flight_holds_a_third_party_one_does_not()
    test_the_hard_cap()
    test_a_session_without_js_just_waits()
    print("test_navigator_settle: 4 ok")
