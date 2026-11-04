"""Offline (the fake shop of test_demo_compile.py): the independent oracle and the
denominator ledger.

What must hold:

1. the oracle renders the site's own search by ANOTHER code path than the tool: the
   start page and the first visible search box (never the tool's result URL, its
   recorded input or its region); the site's search URL only when there is no box or
   Enter reloads the start page; it waits for a script-rendered list; the product
   page is kept as the page shows it;
2. the blind case shows the query, the tool's answer or failure and the oracle's
   pages, never the demonstrator or the tool's internals; the reader's answer is
   parsed per case, a missing or unreadable case is undeterminable;
3. tier 1 (report only): eligible only for a result-URL tool; covered only when HTTP
   gives the browser's own answer;
4. the ledger accounts for every scheduled request: an unrun request is
   ``not_run`` (a failure), an accepted output without an audit is ``unaudited``,
   a wrong audit is listed; the site-clustered interval is deterministic.
"""
import asyncio
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import demo_oracle as O  # noqa: E402
from test_demo_compile import BASE, FakeShopPage, dom_of, spec_for, tool  # noqa: E402


async def _settle(page):
    return None


def test_the_oracle_types_into_the_sites_own_box():
    page = FakeShopPage(late=2)
    ev = asyncio.run(O.oracle_search(page, BASE + "/", "Borealis Toaster 2 Slice", [BASE + "/search?q={q}"], _settle,
                                     wait_s=2.0))
    assert page.typed == [("Borealis Toaster 2 Slice", None)], page.typed     # the first box, no recorded input
    assert page.gotos == [BASE + "/"] and ev["how"] == "typed", (page.gotos, ev["how"])
    assert any("Borealis Toaster 2 Slice" in x["text"] for x in ev["links"]) and ev["waited_s"] is not None
    # Enter reloads the start page: the site's search URL seen on the start page
    page = FakeShopPage(reload_on_enter=True)
    ev = asyncio.run(O.oracle_search(page, BASE + "/", "Delta Mixer 7", [BASE + "/search?q={q}"], _settle, wait_s=0.5))
    assert page.gotos[-1] == BASE + "/search?q=Delta+Mixer+7" and "search URL" in ev["how"], ev["how"]
    page = FakeShopPage(no_box=True)
    ev = asyncio.run(O.oracle_search(page, BASE + "/", "Delta Mixer 7", [], _settle, wait_s=0.3))
    assert ev.get("search_failed") and "no search box" in ev["how"]
    it = asyncio.run(O.oracle_item(FakeShopPage(), BASE + "/p/delta-mixer-7", _settle))
    assert it["h1"] == ["Delta Mixer 7"] and "75,00 €" in it["text"]


def test_the_blind_case_and_the_parse():
    out = {"status": "ok", "output": {"name": "Delta Mixer 7", "price": 75.0, "currency": "EUR",
                                      "availability": "InStock", "url": BASE + "/p/delta-mixer-7"},
           "checks": {"secret": "region regex"}, "search_via": "url"}
    ev = {"url": BASE + "/search?q=Delta", "title": "t", "h1": [], "text": "1 result", "links": [], "how": "typed"}
    c = O.reader_case("s1v1", "product_lookup", "Delta Mixer 7", out, ev, {"url": BASE + "/p/delta-mixer-7",
                                                                           "title": "x", "h1": [], "text": "75,00 €"})
    blob = O.reader_prompt([c])
    assert "Delta Mixer 7" in blob and "region regex" not in blob and "demonstrat" not in blob.lower()
    assert "NOT enough" in blob
    f = O.reader_case("s1v2", "product_lookup", "Q", {"status": "failed", "why": "query: no result names"}, ev)
    assert f["tool_answer"] == {"failed": True, "reason": "query: no result names"}
    got = O.parse_reader('```json\n{"cases": [{"id": "s1v1", "verdict": "Correct", "membership": true, '
                         '"why": "ok"}, {"id": "s1v2", "verdict": "maybe"}]}\n```', ["s1v1", "s1v2", "s1v3"])
    assert got["s1v1"]["verdict"] == "correct" and got["s1v1"]["membership"] is True
    assert got["s1v2"]["verdict"] == "undeterminable" and got["s1v2"]["error"]
    assert got["s1v3"]["verdict"] == "undeterminable" and got["s1v3"]["error"]
    assert O.parse_reader("not json", ["a"])["a"]["verdict"] == "undeterminable"


def test_tier1_is_reported_only_and_strict():
    t = tool()

    def get(url):
        return 200, dom_of(spec_for(url))
    browser = {"status": "ok", "picked": {"url": BASE + "/p/delta-mixer-7"}}
    r = O.http_tier1(t, "Delta Mixer 7", get, browser)
    assert r["eligible"] and r["covered"] and r["http_verdict"] == "pick", r
    r = O.http_tier1(t, "Delta Mixer 7", get, {"status": "failed"})
    assert r["eligible"] and not r["covered"]
    r = O.http_tier1(t, "Delta Mixer 7", lambda u: (None, ""), browser)
    assert r["eligible"] and not r["covered"]
    typed = dict(t, search={"mode": "typed", "input": {"index": 0}})
    assert O.http_tier1(typed, "Delta Mixer 7", get, browser) == {"eligible": False,
                                                                  "why": "the tool types its search (no result URL)"}


def test_the_ledger_accounts_for_every_scheduled_request():
    d = tempfile.mkdtemp()
    L = O.RequestLedger(os.path.join(d, "ledger.json"))
    cohort = [{"site": f"s{i}.test", "workflow": "site_search", "demonstrator": "claude", "held_out": ["a", "b", "zq"],
               "empty": 2} for i in range(30)]
    assert L.schedule(cohort) == 180 and L.schedule(cohort) == 0            # idempotent
    s = L.summary("immediate")
    assert s["scheduled"] == 90 and s["correct"] == 0 and s["counts"] == {"not_run": 90}
    L.set(O.request_id("immediate", "s0.test", 1), tool_status="ok", audit={"verdict": "correct"})
    L.set(O.request_id("immediate", "s0.test", 2), tool_status="ok")
    L.set(O.request_id("immediate", "s0.test", 3), tool_status="no_results", audit={"verdict": "wrong"})
    L.set(O.request_id("immediate", "s1.test", 1), tool_status="failed", audit={"verdict": "undeterminable",
                                                                                 "results_exist": True})
    try:
        L.set("immediate|nowhere|v1", tool_status="ok")
        raise AssertionError("an unscheduled request was accepted")
    except KeyError:
        pass
    s = L.summary("immediate")
    assert s["correct"] == 1 and s["counts"] == {"correct": 1, "unaudited": 1, "wrong": 1, "failure": 1,
                                                 "not_run": 86}, s["counts"]
    assert s["wrong_ids"] == ["immediate|s0.test|v3"]
    d2 = L.data()["requests"]
    assert d2["immediate|s0.test|v3"]["empty"] is True and d2["immediate|s0.test|v1"]["empty"] is False
    assert O.cluster_ci([(3, 3)] * 10 + [(0, 3)] * 20, seed="x") == O.cluster_ci([(3, 3)] * 10 + [(0, 3)] * 20, seed="x")
    lo, hi = O.cluster_ci([(3, 3)] * 10 + [(0, 3)] * 20, seed="x")
    assert lo < 1 / 3 < hi
    # a site replaced before its demonstration leaves the denominator; its reserve takes the slot
    for k in (1, 2, 3):
        L.set(O.request_id("immediate", "s29.test", k), replaced_by="r1.test")
    L.schedule([{"site": "r1.test", "workflow": "site_search", "held_out": ["x", "y", "zq"], "empty": 2}],
               waves=("immediate",))
    s = L.summary("immediate")
    assert s["scheduled"] == 90 and "s29.test" not in s["per_site"] and "r1.test" in s["per_site"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
