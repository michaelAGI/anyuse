"""Web tool version 10, part 2 (b), offline (the fake shop of ``test_demo_breadth_filtered``; no browser, no network, no
model): a PAGINATED search (top N across result pages) demonstrated once - the search, then the next-page link to page
2 - and compiled: the page parameter (or path) the demonstration's page 2 used, else its next-page link found again;
the result structure of page 2 must read page 1 too. A run collects N results page by page and stops at N or at the
last page (no next-page link, a page that lists nothing new, a page listing no result); its pages count against the
site's caps (``MAX_PAGES``).

What must hold:

1. a page parameter (``page=2``) compiles to ``pager.mode == "param"``; a run on a new value collects its N (default:
   the demonstration's two pages' worth) in page order with no duplicates, and the site's stated total ("1–4 von 11
   Treffern") agrees when it reads to the last page;
2. a cursor the URL does not number compiles to the next-page link (``pager.mode == "link"``) and reads the same;
3. a site that serves its last page again for a page past the end stops there (no duplicates answered); a stated total
   the run cannot reach is INCOMPLETE; a later page that fails is INCOMPLETE, never a silently short list;
4. the validity rule: one result page only is invalid.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_breadth as DB  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_breadth_filtered import BASE, SITE, FakePage, Shop, el_of, reported, run, step  # noqa: E402


def paged_trace(shop, value="Akkuschrauber", click="Weiter"):
    pg = FakePage(shop)
    start = BASE + "/"
    p0 = shop.read(start)
    asyncio.run(pg.type_search(value))
    rurl = pg.cur
    p1 = pg.read()
    e = el_of(p1, lambda x: x["tag"] == "a" and x["text"] == click)
    asyncio.run(pg.goto(e["href"]))
    p2 = pg.read()
    rep = {"results": reported(p2), "total": "7"}
    steps = [step(0, p0, {"action": "search", "element": 0, "text": value},
                  {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}),
             step(1, p1, {"action": "click", "element": e["i"]},
                  {"ok": True, "how": "goto", "href": e["href"], "url_after": pg.cur}, e),
             step(2, p2, {"action": "done", "result": rep}, {})]
    return {"version": 1, "workflow": "paged_search", "site": SITE, "start_url": start, "value": value,
            "steps": steps, "final_page": p2, "result": rep, "stop": "done", "forbidden": []}


def urls(out):
    return [r["url"] for r in (out.get("output") or {}).get("results") or []]


def test_a_page_parameter_collects_n_in_page_order():
    shop = Shop()
    g = C.compile_trace(paged_trace(shop))
    assert g["ok"], g.get("why")
    t = g["tool"]
    assert t["workflow"] == "paged_search" and t["pager"]["mode"] == "param" and t["pager"]["key"] == "page", t["pager"]
    assert t["pager"]["first"] == 1 and t["pager"]["stride"] == 1 and t["n"] == 7, (t["pager"], t["n"])
    assert g["report"]["pages"] == {"page1_items": 4, "page2_items": 3}, g["report"]["pages"]
    # a new value: "Bosch" has 5 hits (pages of 4): the default n=7 reads to the last page, 5 results, total 5
    r = run(shop, t, "Bosch")
    want = [f"{BASE}/p/{c[0]}" for c in shop.hits("Bosch")]
    assert r["status"] == "ok" and urls(r) == want, (r["status"], r["why"], urls(r))
    ch = r["checks"]["paged"]
    assert ch["stated_total"] == 5 and ch["end"] == "last page (the stated total is read)", ch
    assert [p["page"] for p in ch["pages"]] == [1, 2], ch                     # no request past the last page
    assert [x["page"] for x in r["output"]["results"]] == [1, 1, 1, 1, 2] and r["output"]["total"] == 5
    # n bound lower: stops at n on page 1 (one page read)
    r = run(shop, t, "Bosch", params={"n": 3})
    assert r["status"] == "ok" and urls(r) == want[:3] and r["checks"]["paged"]["end"] == "n", r["checks"]["paged"]
    # a value of 11 hits, n=20: three pages, the stated total read to the end
    r = run(shop, t, "Akku", params={"n": 20})
    assert r["status"] == "ok" and len(urls(r)) == len(shop.hits("Akku")) == 7, (r["why"], urls(r))
    assert len(set(urls(r))) == len(urls(r))


def test_a_cursor_compiles_to_the_next_link():
    shop = Shop(pager="cursor")
    g = C.compile_trace(paged_trace(shop))
    assert g["ok"], g.get("why")
    t = g["tool"]
    assert t["pager"]["mode"] == "link" and t["pager"]["link"]["rel"] == "next", t["pager"]
    r = run(shop, t, "Akkuschrauber")
    want = [f"{BASE}/p/{c[0]}" for c in shop.hits("Akkuschrauber")]
    assert r["status"] == "ok" and urls(r) == want, (r["why"], urls(r))
    assert r["checks"]["paged"]["end"] in ("n", "last page (no next-page link)"), r["checks"]["paged"]


def test_repeats_unreachable_totals_and_a_failing_page():
    shop = Shop()
    t = C.compile_trace(paged_trace(shop))["tool"]
    # the site serves its last page again past the end: the run stops there, no duplicate answered
    rep = Shop(repeat_last=True)
    r = run(rep, t, "Bosch", params={"n": 20})
    assert r["status"] == "ok" and len(urls(r)) == 5 and len(set(urls(r))) == 5, (r["why"], urls(r))
    assert r["checks"]["paged"]["end"].startswith("last page"), r["checks"]["paged"]
    norep = dict(t, n=20)
    r2 = run(Shop(repeat_last=True, total=False), norep, "Bosch")
    assert r2["status"] == "ok" and len(urls(r2)) == 5 and r2["checks"]["paged"]["end"].startswith("last page (it repeats"), r2
    assert r2["checks"]["paged"]["duplicates"], r2["checks"]["paged"]
    # the page cap before the stated total: INCOMPLETE (never a short list answered as all)
    capped = dict(t, max_pages=1)
    r = run(shop, capped, "Akku", params={"n": 20})
    assert r["status"] == "incomplete" and "within the 1-page cap" in r["why"], r["why"]
    assert not r["accepted"] and len(r["output"]["results"]) == 4
    # a later page that is an error page: INCOMPLETE
    class Broken(Shop):
        def search_dom(self, url, state):
            if str(state.get("page")) == "2":
                return self.wrap("Fehler", "<h1>Internal Server Error</h1><p>Something went wrong</p>")
            return super().search_dom(url, state)
    r = run(Broken(), t, "Bosch", params={"n": 20})
    assert r["status"] == "incomplete" and r["why"].startswith("paged: page 2 failed"), r["why"]


def test_dev_check_2_the_last_page_served_again_and_no_next_control():
    shop = Shop()
    t = C.compile_trace(paged_trace(shop))["tool"]
    # DEV check 2 (www.kurbelix.de "Sattel"): a page that only repeats the earlier results is the last page, never an
    # "overlap" (here "Akku": 7 hits, page 3 = page 2 again, 3 repeats)
    r = run(Shop(repeat_last=True, total=False), t, "Akku", params={"n": 20})
    assert r["status"] == "ok" and len(urls(r)) == 7, (r["status"], r["why"])
    assert r["checks"]["paged"]["end"].startswith("last page (it repeats") and r["checks"]["paged"]["overlap"] == 0
    # a page with no next-page control at all ends the run without a request past the end
    page = FakePage(Shop(nav=False, total=False))
    import asyncio as _a
    from anyuse.execution import demo_tool as T
    r = _a.run(T.run_tool(page, t, "Akku", _settle_p, result_wait_s=0.2, params={"n": 20}))
    assert r["status"] == "ok" and r["checks"]["paged"]["end"] == "last page (no next-page control)", r["checks"]["paged"]
    assert not any("page=2" in g for g in page.gotos), page.gotos


async def _settle_p(page):
    return None


def test_one_page_is_invalid_and_the_mechanisms():
    shop = Shop()
    tr = paged_trace(shop)
    one = dict(tr, steps=[tr["steps"][0], dict(tr["steps"][2], k=1, page=tr["steps"][1]["page"])],
               final_page=tr["steps"][1]["page"])
    ok, why, _d = C.demo_validity(one)
    assert not ok and "only one result page was visited" in why, why
    assert DB._page_mech(tr, f"{BASE}/search?q=x", f"{BASE}/search?q=x&start=24", 24) == \
        {"mode": "param", "key": "start", "first": 0, "stride": 24}
    assert DB._page_mech(tr, f"{BASE}/suche/x/", f"{BASE}/suche/x/page/2/", 10) == {"mode": "path_append", "suffix": "page/{page}"}
    assert DB.page_url(f"{BASE}/suche/y/", {"mode": "path_append", "suffix": "page/{page}"}, 3) == f"{BASE}/suche/y/page/3/"
    assert DB.page_url(f"{BASE}/s?q=y", {"mode": "param", "key": "start", "first": 0, "stride": 24}, 3) == f"{BASE}/s?q=y&start=48"
    assert DB._page_mech(tr, f"{BASE}/s?q=x&cursor=a", f"{BASE}/s?q=x&cursor=b", 4) == {}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
