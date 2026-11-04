"""Web tool version 10, part 2, the ORACLE for future measured rounds, offline (the fake shop of
``test_demo_breadth_filtered``; no browser, no network, no model): independent checks of the breadth workflows'
answers on a SEPARATE render made through the oracle's own path - the site's search typed into its first box, the
refinement applied by its LABEL (the oracle's own control finder: a drop-down's option, a link, a checkbox; never the
tool's URL parameters), the pages followed by the oracle's own next-page detection, each visited item rendered alone.

What must hold:

1. (a) a sorted answer agrees with the oracle's refined render (the same results in the same order; the order
   re-checked on the oracle's own cards); an answer in another order, or listing a result the render does not show,
   disagrees; a label the page does not show leaves it undeterminable;
2. (b) a paged answer agrees with the oracle's pages (page order, no duplicates); a repeated or reordered result
   disagrees;
3. (c) a visit's table: each ok row agrees when its page shows its name and price; a wrong price disagrees;
4. the blind reader's task text for each new workflow.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_visit_identity.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import copy
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_oracle as O  # noqa: E402
from test_demo_breadth_filtered import BASE, FakePage, Shop, filtered_trace, run  # noqa: E402
from test_demo_breadth_paged import paged_trace  # noqa: E402
from test_demo_breadth_visit import visit_trace  # noqa: E402


async def _settle(page):
    return None


def test_a_filtered_answer_and_the_oracles_refined_render():
    shop = Shop()
    t = C.compile_trace(filtered_trace(shop))["tool"]
    out = run(shop, t, "Akkuschrauber")
    labels = [a["label"] for a in t["applied"]]
    assert labels == ["Preis aufsteigend"], labels
    ev = asyncio.run(O.oracle_refined(FakePage(shop), BASE + "/", "Akkuschrauber", labels, [], _settle))
    assert ev["refined"] and ev["refine_trail"][0]["how"] == "select", ev["refine_trail"]
    got = O.check_refined(out, ev, t["applied"])
    assert got["verdict"] == "agrees" and got["checks"][0]["status"] == "ok", got
    swapped = copy.deepcopy(out)
    res = swapped["output"]["results"]
    res[0], res[1] = res[1], res[0]
    assert O.check_refined(swapped, ev, t["applied"])["verdict"] == "disagrees"
    foreign = copy.deepcopy(out)
    foreign["output"]["results"][0].update(url=BASE + "/p/not-listed", title="Not listed anywhere")
    got = O.check_refined(foreign, ev, t["applied"])
    assert got["verdict"] == "disagrees" and got["missing"] == [BASE + "/p/not-listed"], got
    ev2 = asyncio.run(O.oracle_refined(FakePage(shop), BASE + "/", "Akkuschrauber", ["Preis: günstigste zuerst"], [],
                                       _settle))
    assert not ev2["refined"] and O.check_refined(out, ev2, t["applied"])["verdict"] == "undeterminable"
    # a facet: the brand link found by its label
    tb = C.compile_trace(filtered_trace(shop, how="brand", option="Bosch"))["tool"]
    outb = run(shop, tb, "Akkuschrauber")
    evb = asyncio.run(O.oracle_refined(FakePage(shop), BASE + "/", "Akkuschrauber", ["Bosch"], [], _settle))
    assert O.check_refined(outb, evb, tb["applied"])["verdict"] == "agrees", O.check_refined(outb, evb, tb["applied"])


def test_a_paged_answer_and_the_oracles_pages():
    shop = Shop()
    t = C.compile_trace(paged_trace(shop))["tool"]
    out = run(shop, t, "Akkuschrauber")
    evs = asyncio.run(O.oracle_pages(FakePage(shop), BASE + "/", "Akkuschrauber", 2, [], _settle))
    assert len(evs) == 2 and "next-page link" in evs[1]["how"], [e["how"] for e in evs]
    assert O.check_paged(out, evs)["verdict"] == "agrees", O.check_paged(out, evs)
    dup = copy.deepcopy(out)
    dup["output"]["results"][-1] = dict(dup["output"]["results"][0])
    assert O.check_paged(dup, evs)["verdict"] == "disagrees"
    rev = copy.deepcopy(out)
    rev["output"]["results"].reverse()
    assert O.check_paged(rev, evs)["verdict"] == "disagrees"


def test_dev_check_2_other_url_forms_and_long_cards():
    # www.kurbelix.de: the oracle's links named the answered products by another URL of the same card - matched by title
    shop = Shop()
    t = C.compile_trace(paged_trace(shop))["tool"]
    out = run(shop, t, "Akkuschrauber")
    evs = asyncio.run(O.oracle_pages(FakePage(shop), BASE + "/", "Akkuschrauber", 2, [], _settle))
    other = copy.deepcopy(evs)
    for ev in other:
        for x in ev["links"]:
            x["url"] = x["url"].replace("/p/", "/detail/") + "?search=akkuschrauber"
    got = O.check_paged(out, other)
    assert got["verdict"] == "agrees" and not got["missing"] and got["matched_by_title"] == len(out["output"]["results"]), got
    shuffled = copy.deepcopy(other)
    shuffled[0]["links"].reverse()
    assert O.check_paged(out, shuffled)["verdict"] == "disagrees"
    # www.vinocentral.de: cards of > 400 characters have no READ_JS context; the oracle reads its own tiles
    long_shop = Shop(long_cards=True)
    tf = C.compile_trace(filtered_trace(long_shop))["tool"]
    outf = run(long_shop, tf, "Akkuschrauber")
    assert outf["status"] == "ok", outf["why"]
    ev = asyncio.run(O.oracle_refined(FakePage(long_shop), BASE + "/", "Akkuschrauber", ["Preis aufsteigend"], [], _settle))
    got = O.check_refined(outf, ev, tf["applied"])
    assert got["verdict"] == "agrees" and got["checks"][0]["status"] == "ok", got


def test_a_visit_table_and_the_oracles_item_renders():
    shop = Shop()
    t = C.compile_trace(visit_trace(shop))["tool"]
    out = run(shop, t, "Akkuschrauber")
    evs = {r["url"]: asyncio.run(O.oracle_item(FakePage(shop), r["url"], _settle)) for r in out["output"]["items"]}
    got = O.check_items(out, evs)
    assert got["verdict"] == "agrees" and all(r["verdict"] == "agrees" for r in got["rows"]), got
    bad = copy.deepcopy(out)
    bad["output"]["items"][1]["price"] = 1.23
    got = O.check_items(bad, evs)
    assert got["verdict"] == "disagrees" and got["rows"][1]["price_shown"] is False, got


def test_the_readers_task_texts():
    for wf in ("filtered_search", "paged_search", "visit_results"):
        case = O.reader_case("c1", wf, "x", {"status": "ok", "output": {}}, {"url": BASE})
        assert case["task"] == O.BREADTH_TASKS[wf], case
    assert O.reader_case("c1", "site_search", "x", {"status": "ok", "output": {}}, {})["task"].startswith("site search")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
