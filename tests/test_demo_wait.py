"""Offline (fake pages; no browser, no network, no model): the result wait before the tool reads the result page.

A www.stockmann.com run read its result page at 0.01 s (the old wait ends at the first
read in which any region item names a word of the input). The wait now ends when the tool's own
reading is STABLE: two consecutive local reads show the same number of result containers (one per
card) and an item names a word of the input - or both show the page's own "no results" statement -
within the bounded time (the oracle's render waits the same way: local reads, a ceiling).

What must hold:

1. a list that renders progressively (3 cards, then 5) is read when it is complete, not at the
   first read that names the input (the old wait read 3);
2. a page that states "no results" while its results are still loading is not answered
   "no results" (the old wait stopped at the statement);
3. a page's own "no results" statement, shown in two consecutive reads, ends the wait at once;
4. a count that never settles ends at the ceiling (None), and every read is kept in the trail;
5. nothing is sent while waiting (local reads only: no navigation after the search).
"""
import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_cards import GEAR, FakePage, compiled, game_card, result_html  # noqa: E402


class SequencePage(FakePage):
    """The result URL shows ``frames`` in turn: one frame per page read (READ_JS), the last one
    kept; the card read and the full read see the current frame."""

    def __init__(self, url, frames):
        super().__init__({url: frames[0]})
        self.url_, self.frames, self.reads = url, frames, 0

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        if expr == RR.READ_JS:
            self.pages[self.url_] = self.frames[min(self.reads, len(self.frames) - 1)]
            self.reads += 1
        return await super().js(expr, read_only)


def cards(n):
    return [game_card(s, t, ps, ps[0]) for s, t, ps in GEAR[:n]]


def loading(q):
    return result_html(q, [], empty=False).replace('<div class="row"><div><div></div></div></div>',
                                                   "<p>Caricamento...</p>")


def run(page, t, value, wait=2.0):
    async def _settle(p):
        return None
    return asyncio.run(T.run_tool(page, t, value, _settle, result_wait_s=wait))


def test_a_progressive_list_is_read_when_complete():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    frames = [loading("Gear"), result_html("Gear", cards(3)), result_html("Gear", cards(5)),
              result_html("Gear", cards(7))]
    out = run(SequencePage(url, frames), t, "Gear")
    assert out["status"] == "ok" and out["results_after_s"] is not None, out
    assert [x[1] for x in out["wait_trail"]] == [0, 3, 5, 7, 7], out["wait_trail"]
    assert out["checks"]["result_page"]["n_items"] == 7, out["checks"]
    # the old wait: the first read naming the input (3 cards) ends it
    old = asyncio.run(_legacy_wait(SequencePage(url, frames), t, "Gear"))
    assert old == 3, old


async def _legacy_wait(page, t, value):
    """The old criterion on the same page: the item count when it stopped."""
    from anyuse.execution import web_arch_run as RR
    await page.goto(page.url_)
    for _ in range(20):
        r = await RR.read_page(page)
        r["cards"] = await T.read_cards(page, t)
        items = C.region_items(r, t["region"])
        if any(C.name_share(it["title"], value) > 0 for it in items) or \
                C.no_results_statement(r.get("main_text") or r.get("text")):
            return len(items)
    return None


def test_a_transient_no_results_statement_is_not_the_answer():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    frames = [result_html("Gear", [], empty=True), result_html("Gear", cards(5))]
    out = run(SequencePage(url, frames), t, "Gear")
    assert out["status"] == "ok" and len(out["output"]["results"]) == 5, out
    assert [x[2] for x in out["wait_trail"]][:2] == [1, 0], out["wait_trail"]


def test_the_sites_own_no_results_statement_ends_the_wait():
    t = compiled()
    url = C.fill(t["search"]["template"], "Zorquovor Pelpeltrax")
    page = SequencePage(url, [result_html("Zorquovor Pelpeltrax", [], empty=True)])
    out = run(page, t, "Zorquovor Pelpeltrax")
    assert out["status"] == "no_results" and out["accepted"], out
    assert len(out["wait_trail"]) == 2 and out["results_after_s"] is not None, out["wait_trail"]
    assert page.gotos == [url], page.gotos           # nothing sent while waiting


def test_a_count_that_never_settles_ends_at_the_ceiling():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    frames = [result_html("Gear", cards(1 + k % 7)) for k in range(40)]
    page = SequencePage(url, frames)
    page.cur = url
    ticks = iter(k * 0.5 for k in range(1000))
    trail = []
    got = asyncio.run(T.wait_results(page, t, "Gear", 4.0, clock=lambda: next(ticks), trail=trail))
    assert got is None and len(trail) >= 4, trail


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
