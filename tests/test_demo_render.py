"""Offline (the fake shop of test_demo_compile.py): reliable rendering and the
reserved validation share.

What must hold:

1. the search URL fallback (www.jernia.no): a typed tool whose Enter reloads
   the start page, or that finds no search box, opens the site's own search URL seen
   on the start page at demonstration time; with none, an explicit failure;
2. the result wait: a script-rendered list that appears after a few reads is waited
   for (local reads only); without the wait the same page is an explicit failure,
   never an answer;
3. every invocation opens a NEW browser context (the bench's session class);
4. the per-site traffic plan fits the approved 300 requests / 30 MB, and no phase may
   spend what a later phase has reserved (the delayed wave keeps its share whatever
   the demonstration and the immediate wave used).
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import demo_traffic as TR  # noqa: E402
from test_demo_compile import BASE, FakeShopPage, tool  # noqa: E402


async def _settle(page):
    return None


def typed_tool():
    t = tool()
    return dict(t, search={"mode": "typed", "input": {"index": 0, "name": "q"}, "start_url": BASE + "/",
                           "fallback_templates": [BASE + "/search?q={q}"], "overlay": False})


def run(page, t, value, wait=0.3):
    return asyncio.run(T.run_tool(page, t, value, _settle, result_wait_s=wait))


def test_the_search_url_fallback_when_enter_reloads_the_start_page():
    t = typed_tool()
    page = FakeShopPage()
    out = run(page, t, "Delta Mixer 7")
    assert out["status"] == "ok" and out["search_via"] == "typed", out
    assert page.typed == [("Delta Mixer 7", {"index": 0, "name": "q"})]
    page = FakeShopPage(reload_on_enter=True)
    out = run(page, t, "Delta Mixer 7")
    assert out["status"] == "ok" and out["search_via"] == "url_fallback", out
    assert page.gotos[:2] == [BASE + "/", BASE + "/search?q=Delta+Mixer+7"], page.gotos
    page = FakeShopPage(no_box=True)
    out = run(page, t, "Delta Mixer 7")
    assert out["status"] == "ok" and out["search_via"] == "url_fallback", out
    t2 = dict(t, search=dict(t["search"], fallback_templates=[]))
    out = run(FakeShopPage(reload_on_enter=True), t2, "Delta Mixer 7")
    assert out["status"] == "failed" and "reloaded the start page" in out["why"] and not out["accepted"], out


def test_the_result_wait():
    out = run(FakeShopPage(late=3), tool(), "Delta Mixer 7", wait=3.0)
    assert out["status"] == "ok" and out["results_after_s"] is not None, out
    out = run(FakeShopPage(late=3), tool(), "Delta Mixer 7", wait=0.0)
    assert out["status"] == "failed" and not out["accepted"], out


def test_every_invocation_opens_a_fresh_context():
    import web_arch2_live as L2
    assert L2.session_class().fresh_context is True


def test_the_traffic_plan_reserves_the_validation_shares():
    assert sum(TR.SHARES.values()) + TR.MARGIN == 300 and sum(TR.BYTES.values()) + TR.MARGIN_BYTES == 30 * TR.MB
    assert TR.share_for("screen", {}) == (70, 7 * TR.MB)
    assert TR.share_for("demo", {"screen": {"requests": 40, "bytes": 2 * TR.MB}})[0] == 60
    # a screen that overspent (it cannot, but) never eats the waves' reserves
    r, _b = TR.share_for("demo", {"screen": {"requests": 120, "bytes": 2 * TR.MB}})
    assert r == 300 - 10 - 120 - 70 - 90
    # the immediate wave after a heavy demonstration keeps the delayed wave's share
    r, _b = TR.share_for("immediate", {"screen": {"requests": 70}, "demo": {"requests": 60}})
    assert r == 70
    r, _b = TR.share_for("delayed", {"screen": {"requests": 70}, "demo": {"requests": 60},
                                      "immediate": {"requests": 70}})
    assert r == 90
    # a resumed phase gets what it has left
    assert TR.share_for("immediate", {"screen": {"requests": 70}, "demo": {"requests": 60},
                                      "immediate": {"requests": 50}})[0] == 20
    assert TR.can_start("tool", 8, TR.MB) and not TR.can_start("tool", 7, TR.MB)
    assert not TR.can_start("tool", 50, 1000)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
