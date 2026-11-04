"""The page's own read-only POST search under the web-scope setting
``ANYUSE_ALLOW_READONLY_POST_SEARCH``, offline against a fake CDP websocket.

The setting used to be on without the scope being told which value the run
typed (``WebScope.note_values`` had a single bench caller),
so a page's XHR POST search whose JSON body carries the typed words was refused
as "not the page's own search". Now the value typed into the site's own search
box (the engine's ``type_search``, the agents' ``search``/``submit_search``) is
the scope's evidence. The rule itself is unchanged:

* allowed: a same-site POST whose search-like field carries exactly the typed
  value (or the fields of a POST search form in the site's own document);
* refused: the same POST before anything was typed; a body naming a state
  change; a GraphQL mutation even when it carries the value; telemetry POSTs;
  a credential header; nothing is noted without a web scope.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D
from anyuse.execution import web_scope as W

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))

H = "www.shop.example"


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


def paused(i, url, rtype="XHR", method="GET", post=None, headers=None):
    req = {"url": url, "method": method}
    if post is not None:
        req["postData"] = post
    if headers:
        req["headers"] = headers
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": f"f{i}", "networkId": f"n{i}", "resourceType": rtype,
                       "frameId": "main", "request": req}}


def bench_session(readonly_post=True, scope_on=True):
    from ladder_verify import GetOnlySession
    clock = Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    sc = W.WebScope(readonly_post=readonly_post, blocklist_domains=frozenset()) if scope_on else None
    b = G.RequestBudget(pacer, asleep=clock.asleep, web_scope=sc, per_load_cap=300, per_host_cap=300,
                        site_cap=300)
    s = GetOnlySession("http://192.0.2.1:9222", user_agent="research-probe/0.1",
                       allow_url=lambda u: True, budget=b)
    s._ws = FakeWS([paused(0, f"https://{H}/", "Document")])
    return s, b


JSON = {"Content-Type": "application/json"}


def search_post(i, value, extra=None):
    body = {"query": value, "page": 1}
    body.update(extra or {})
    return paused(i, f"https://{H}/api/search", "XHR", "POST", json.dumps(body), JSON)


async def typed_then(s, value, events, how="type_search"):
    await s.goto(f"https://{H}/")
    if how == "type_search":
        await s.type_search(value)            # the fake page shows no input: nothing else is typed
    else:
        await s.submit_search(value)
    s._ws.inbox.extend(events)
    await s.settle(0.1)


class ReadonlyPostSearch(unittest.TestCase):
    def test_the_typed_value_makes_the_pages_json_post_search_its_own(self):
        s, b = bench_session()
        asyncio.run(typed_then(s, "granite mortar", [search_post(1, "granite mortar")]))
        self.assertIn("f1", s._ws.answered("Fetch.continueRequest"))
        self.assertEqual(b.web_scope.usage[H], {"post_search": 1})

    def test_the_agents_search_transaction_notes_the_value_too(self):
        s, b = bench_session()
        asyncio.run(typed_then(s, "granite", [search_post(1, "granite")], how="submit_search"))
        self.assertIn("f1", s._ws.answered("Fetch.continueRequest"))

    def test_before_anything_is_typed_the_same_post_is_refused(self):
        s, b = bench_session()

        async def go():
            await s.goto(f"https://{H}/")
            s._ws.inbox.append(search_post(1, "granite"))
            await s.settle(0.1)
        asyncio.run(go())
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])
        self.assertIn("not the page's own search", s.guard.failed[-1][1])

    def test_a_different_value_is_not_the_search(self):
        s, b = bench_session()
        asyncio.run(typed_then(s, "granite", [search_post(1, "marble")]))
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])

    def test_state_words_graphql_mutations_telemetry_and_credentials_stay_refused(self):
        s, b = bench_session()
        gql = json.dumps({"query": "mutation AddToCart($q: String) { add(q: $q) { id } }",
                          "variables": {"q": "granite"}})
        asyncio.run(typed_then(s, "granite", [
            search_post(1, "granite", {"subscribe": True}),
            paused(2, f"https://{H}/graphql", "XHR", "POST", gql, JSON),
            paused(3, f"https://{H}/.well-known/shopify/monorail/unstable/produce_batch", "XHR", "POST",
                   json.dumps({"events": [{"q": "granite"}]}), JSON),
            paused(4, f"https://{H}/api/search", "XHR", "POST", json.dumps({"query": "granite"}),
                   {"Content-Type": "application/json", "Authorization": "Bearer x"}),
            paused(5, f"https://{H}/cart/add", "XHR", "POST", "q=granite")]))
        self.assertEqual(sorted(s._ws.answered("Fetch.failRequest")), ["f1", "f2", "f3", "f4", "f5"])
        self.assertEqual(b.web_scope.usage.get(H, {}), {})

    def test_without_the_setting_or_the_scope_every_post_is_refused(self):
        for kw in ({"readonly_post": False, "scope_on": True}, {"scope_on": False}):
            s, b = bench_session(**kw) if kw.get("scope_on") else bench_session(scope_on=False)
            asyncio.run(typed_then(s, "granite", [search_post(1, "granite")]))
            self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"], kw)


if __name__ == "__main__":
    unittest.main()
