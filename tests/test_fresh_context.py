"""Offline against a fake
browser websocket: every run starts fresh.

When a task runs more than once, a run must not inherit
anything from the one before (cookies set by the site, storage, a consent
choice, a logged state): with ``fresh_context`` on, each session opens its page
in a NEW browser context, checks that the context starts with no cookies, and
disposes of the context at the end.

What must hold:

1. off by default: the page opens in the default context (today's behaviour);
2. on: ``Target.createBrowserContext`` first, the page target created IN it,
   the start check (no cookies) recorded as ``fresh_check``;
3. a context that shows cookies at the start is recorded as not fresh;
4. the context is disposed of when the session closes;
5. two sessions get two different contexts.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import unittest

from anyuse.execution import agent_cdp as A
from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D


class FakeBWS:
    """The browser websocket: answers the Target/Storage commands."""
    ids = itertools.count(1)

    def __init__(self, cookies=()):
        self.inbox = []
        self.sent = []
        self.cookies = list(cookies)

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m, res = msg.get("method"), {}
        if m == "Target.createBrowserContext":
            res = {"browserContextId": f"CTX{next(FakeBWS.ids)}"}
        elif m == "Storage.getCookies":
            res = {"cookies": self.cookies}
        elif m == "Target.createTarget":
            res = {"targetId": f"T{next(FakeBWS.ids)}"}
        self.inbox.append({"id": msg["id"], "result": res})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None

    def calls(self, method):
        return [m.get("params") for m in self.sent if m.get("method") == method]


def session(fresh, cookies=()):
    s = A.CdpAgentSession("http://192.0.2.1:9222")
    s.fresh_context = fresh
    s._bws = FakeBWS(cookies)
    return s


async def open_close(s):
    await s._create_target()
    await s._close_target()


class FreshContext(unittest.TestCase):
    def test_1_off_by_default(self):
        self.assertFalse(A.CdpAgentSession.fresh_context)
        s = session(False)
        asyncio.run(open_close(s))
        self.assertEqual(s._bws.calls("Target.createBrowserContext"), [])
        self.assertNotIn("browserContextId", s._bws.calls("Target.createTarget")[0])
        self.assertEqual(s.fresh_check, {})

    def test_2_on_the_page_opens_in_a_new_checked_context(self):
        s = session(True)
        asyncio.run(s._create_target())
        ctx = s.context_id
        self.assertTrue(ctx)
        self.assertEqual(s._bws.calls("Target.createTarget")[0]["browserContextId"], ctx)
        self.assertEqual(s._bws.calls("Storage.getCookies")[0], {"browserContextId": ctx})
        self.assertEqual(s.fresh_check["cookies_at_start"], 0)
        self.assertTrue(s.fresh_check["fresh"])

    def test_3_cookies_at_the_start_are_not_fresh(self):
        s = session(True, cookies=[{"name": "sid", "value": "x"}])
        asyncio.run(s._create_target())
        self.assertFalse(s.fresh_check["fresh"])
        self.assertEqual(s.fresh_check["cookies_at_start"], 1)

    def test_4_the_context_is_disposed_of(self):
        s = session(True)
        asyncio.run(open_close(s))
        self.assertEqual(s._bws.calls("Target.disposeBrowserContext"), [{"browserContextId": s.context_id}])
        self.assertTrue(s.fresh_check["disposed"])

    def test_5_two_runs_two_contexts(self):
        a, b = session(True), session(True)
        asyncio.run(open_close(a))
        asyncio.run(open_close(b))
        self.assertNotEqual(a.context_id, b.context_id)

    def test_the_guarded_session_carries_the_switch(self):
        pacer = D.Pacer(1.0)
        s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(pacer), allow_url=lambda u: True)
        self.assertFalse(s.fresh_context)

        class Fresh(G.GuardedSession):
            fresh_context = True
        s2 = Fresh("http://192.0.2.1:9222", budget=G.RequestBudget(pacer), allow_url=lambda u: True)
        s2._bws = FakeBWS()
        asyncio.run(s2._create_target())
        self.assertTrue(s2.fresh_check["fresh"])


if __name__ == "__main__":
    unittest.main()
