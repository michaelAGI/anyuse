"""The run cache answers a static asset the run already fetched on a LATER page
load of the same run. Until now the product's sessions never filled it (``harvest_cache`` ran only in benches, at a
session's end) and the browser's own cache is disabled, so the provider's 15 scripts were sent again on the results
page. Pinned: a static response the guard sent is stored when it FINISHES (``GuardedSession._cache_on_finish``: a
non-blocking body read, stored by ``_on_reply`` under the cache's HTTP rules), for the site's own host, a platform CDN
host and a provider host alike; ``no-store`` is refused and recorded as before; a document / XHR is never kept; the
next page load is answered from the cache, not sent and not counted, and every reuse is recorded (and on the run
record's ``third_party.run_cache``). No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_run_cache_reuse.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import web_scope as W  # noqa: E402

H = "www.toolnut.test"
SPRING = '<html><script src="https://snapui.searchspring.io/cts0zk/bundle.js"></script>' \
         '<script src="https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.js"></script></html>'


class WS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def session(scope=None):
    clock = [1000.0]

    async def asleep(s):
        clock[0] += s
    pacer = D.Pacer(0.0, clock=lambda: clock[0], sleeper=lambda s: None)
    b = G.RequestBudget(pacer, site_cap=100, web_scope=scope, load_per_navigation=True, asleep=asleep)
    b.cache_enabled = True
    b.site_cache = True
    s = G.GuardedSession("http://192.0.2.1:9222", user_agent="test", allow_url=lambda u: True, budget=b)
    s._ws = WS()
    s.guard.start(f"https://{H}/")
    return s, b


def paused(rid, url, rtype, frame="f1"):
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": f"p{rid}", "networkId": f"n{rid}", "frameId": frame, "resourceType": rtype,
                       "request": {"url": url, "method": "GET", "headers": {}}}}


def finish(s, rid, url, rtype, headers=None, body="console.log(1)"):
    """The network's events for a sent request, then the body read's reply."""
    async def go():
        await s._maybe_gate({"method": "Network.requestWillBeSent",
                             "params": {"requestId": f"n{rid}", "type": rtype,
                                        "request": {"url": url, "method": "GET", "headers": {}}}})
        await s._maybe_gate({"method": "Network.responseReceived",
                             "params": {"requestId": f"n{rid}", "type": rtype,
                                        "response": {"url": url, "status": 200,
                                                     "headers": headers or {"Cache-Control": "max-age=3600"}}}})
        n = len(s._ws.sent)
        await s._maybe_gate({"method": "Network.loadingFinished", "params": {"requestId": f"n{rid}"}})
        for msg in s._ws.sent[n:]:
            if msg.get("method") == "Network.getResponseBody":
                await s._on_reply({"id": msg["id"], "result": {"body": body, "base64Encoded": False}})
    asyncio.run(go())


def run(s, msgs):
    async def go():
        for m in msgs:
            await s._maybe_gate(m)
    asyncio.run(go())


def answered(s, rid):
    return [m["method"] for m in s._ws.sent if (m.get("params") or {}).get("requestId") == f"p{rid}"]


def test_a_static_the_run_fetched_is_answered_from_the_cache_on_the_next_page_load():
    s, b = session()
    js = f"https://{H}/cdn/shop/theme.js"
    run(s, [paused(1, f"https://{H}/", "Document"), paused(2, js, "Script")])
    assert answered(s, 2) == ["Fetch.continueRequest"]                       # sent the first time
    finish(s, 2, js, "Script")
    assert b.cached_on_response == 1 and b.lookup("GET", js, {}) is not None
    total = b.site_total(H)
    run(s, [paused(3, f"https://{H}/search?q=drill", "Document"), paused(4, js, "Script")])
    assert answered(s, 4) == ["Fetch.fulfillRequest"]                        # the second page load: reused
    assert b.site_total(H) == total + 1                                      # only the document was sent
    assert b.cache_reuse == [("www.toolnut.test", js, "Script", "site")]


def test_no_store_and_data_are_never_kept():
    s, b = session()
    js = f"https://{H}/private.js"
    run(s, [paused(1, f"https://{H}/", "Document"), paused(2, js, "Script")])
    finish(s, 2, js, "Script", headers={"Cache-Control": "no-store"})
    assert b.lookup("GET", js, {}) is None and b.cache_refusals[-1]["why"] == "no-store"
    api = f"https://{H}/api/x.json"
    run(s, [paused(3, api, "XHR")])
    finish(s, 3, api, "XHR")
    assert b.lookup("GET", api, {}) is None and b.cached_on_response == 0


def test_platform_cdn_and_provider_statics_are_reused_and_recorded():
    sc = W.WebScope(**W.third_party_flags("search"))
    s, b = session(sc)
    sc.observe(H, f"https://{H}/", "Document", SPRING)
    bundle = "https://snapui.searchspring.io/cts0zk/bundle.js"
    theme = "https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.js"
    run(s, [paused(1, f"https://{H}/", "Document"), paused(2, bundle, "Script"), paused(3, theme, "Script")])
    assert answered(s, 2) == ["Fetch.continueRequest"] and answered(s, 3) == ["Fetch.continueRequest"]
    finish(s, 2, bundle, "Script")
    finish(s, 3, theme, "Script")
    sent_before = b.provider_sent.get(H, 0)
    run(s, [paused(4, f"https://{H}/search?q=drill", "Document"), paused(5, bundle, "Script"),
            paused(6, theme, "Script")])
    assert answered(s, 5) == ["Fetch.fulfillRequest"] and answered(s, 6) == ["Fetch.fulfillRequest"]
    assert b.provider_sent.get(H, 0) == sent_before                          # the provider's share is not used
    kinds = {(h, k) for h, _u, _t, k in b.cache_reuse}
    assert kinds == {("snapui.searchspring.io", "provider"), ("cdn.shopify.com", "platform_cdn")}

    class Factory:
        budget = b
    rec = HP.third_party_record(Factory, H)
    assert rec["run_cache"]["reused"] == {"provider": {"snapui.searchspring.io": 1},
                                          "platform_cdn": {"cdn.shopify.com": 1}}
    assert rec["run_cache"]["stored_on_response"] == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
