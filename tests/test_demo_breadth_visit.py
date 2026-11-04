"""Web tool version 10, part 2 (c), offline (the fake shop of ``test_demo_breadth_filtered``; no browser, no network, no
model): VISIT SEVERAL RESULTS - a search whose top K results are opened, each read with the product-lookup reader,
demonstrated once (the search, the first result, the second result's URL carried in the action's ``read`` note, the
report of both) and compiled: the result structure holding the opened results, the field sources that read the
reported fields back on the opened pages, K. A run opens the top K results of a new search and returns a TABLE with a
per-item status: the identity against the result's title (version 9's matching), field sanity, a structured / visible
CONFLICT (a conflict row, never either side), version 8's availability rule.

What must hold:

1. the demonstration compiles (``k == 2``, JSON-LD sources, the version-9/10 text keys); a run on a new value opens
   its top 2 results and answers both rows ``ok`` (name, price, availability, the URL loaded);
2. ``k`` bound higher reads more rows; a row whose page states another availability than its JSON-LD is a CONFLICT
   row and the table is INCOMPLETE (not accepted); a result title with extra words still matches its product;
3. the validity rule: one result opened is invalid.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_visit_identity.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_breadth as DB  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_breadth_filtered import BASE, CATALOG, SITE, FakePage, Shop, el_of, eur, run, step  # noqa: E402


def item_of(slug):
    c = next(x for x in CATALOG if x[0] == slug)
    return {"name": c[1], "price": eur(c[2]), "currency": "EUR", "availability": "Auf Lager" if c[6] else "Ausverkauft",
            "url": f"{BASE}/p/{slug}"}


def visit_trace(shop, value="Bohrhammer"):
    pg = FakePage(shop)
    start = BASE + "/"
    p0 = shop.read(start)
    asyncio.run(pg.type_search(value))
    rurl = pg.cur
    p1 = pg.read()
    links = [e for e in p1["elements"] if e["tag"] == "a" and "/p/" in (e.get("href") or "")]
    a, b = links[0], links[1]
    asyncio.run(pg.goto(a["href"]))
    pa = pg.read()
    asyncio.run(pg.goto(b["href"]))
    pb = pg.read()
    ia, ib = item_of(a["href"].rsplit("/", 1)[1]), item_of(b["href"].rsplit("/", 1)[1])
    rep = {"items": [ia, ib]}
    steps = [step(0, p0, {"action": "search", "element": 0, "text": value},
                  {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}),
             step(1, p1, {"action": "click", "element": a["i"],
                          "read": {"results": [{"title": a["text"], "url": a["href"]}, {"title": b["text"], "url": b["href"]}]}},
                  {"ok": True, "how": "goto", "href": a["href"], "url_after": a["href"]}, a),
             step(2, pa, {"action": "goto", "url": b["href"], "read": {"item": ia}},
                  {"ok": True, "how": "goto", "href": b["href"], "url_after": b["href"]}),
             step(3, pb, {"action": "done", "result": rep}, {})]
    return {"version": 1, "workflow": "visit_results", "site": SITE, "start_url": start, "value": value,
            "steps": steps, "final_page": pb, "result": rep, "stop": "done", "forbidden": []}


def rows(out):
    return (out.get("output") or out.get("fields_seen") or {}).get("items") or []


def test_the_visit_compiles_and_reads_a_table():
    shop = Shop()
    tr = visit_trace(shop)
    ok, why, det = C.demo_validity(tr)
    assert ok and len(det["opened"]) == 2, (why, det)
    g = C.compile_trace(tr)
    assert g["ok"], g.get("why")
    t = g["tool"]
    assert t["workflow"] == "visit_results" and t["k"] == 2 and t["fields"]["name"] == "jsonld", (t["k"], t["fields"])
    assert t["fields"]["statement_rule"] == "10" and t["fields"]["url_rule"] == "same_product", t["fields"]
    r = run(shop, t, "Akkuschrauber")
    hits = shop.hits("Akkuschrauber")[:2]
    assert r["status"] == "ok" and r["accepted"], (r["status"], r["why"])
    got = rows(r)
    assert [x["name"] for x in got] == [c[1] for c in hits] and [x["price"] for x in got] == [c[2] for c in hits], got
    assert all(x["status"] == "ok" and x["currency"] == "EUR" and x["availability"] == "InStock" for x in got), got
    assert [x["url"] for x in got] == [f"{BASE}/p/{c[0]}" for c in hits], got
    assert r["checks"]["items"][0]["checks"]["identity"]["share"] == 1.0, r["checks"]["items"][0]


def test_k_conflicts_and_titles():
    shop = Shop()
    t = C.compile_trace(visit_trace(shop))["tool"]
    r = run(shop, t, "Akkuschrauber", params={"k": 4})
    assert r["status"] == "ok" and len(rows(r)) == 4, (r["why"], rows(r))
    # the third result's page says "Ausverkauft" while its JSON-LD says InStock: a CONFLICT row, the table INCOMPLETE
    third = shop.hits("Akkuschrauber")[2][0]
    bad = Shop(conflict=shop.hits("Akkuschrauber")[0][0])
    r = run(bad, t, "Akkuschrauber", params={"k": 3})
    st = [x["status"] for x in rows(r)]
    assert r["status"] == "incomplete" and not r["accepted"] and st == ["conflict", "ok", "ok"], (r["why"], st, third)
    assert rows(r)[0]["availability"] == "CONFLICT" and "conflict: availability" in rows(r)[0]["why"], rows(r)[0]
    # a result title with extra words (a tile's tagline): the product's own name is matched both ways
    prod = Shop().read(f"{BASE}/p/bosch-bohrhammer-pbh")
    fields = C.extract_fields(prod, t["fields"], "x")
    ok, why, ch = DB.check_item(prod, fields, "Bosch Bohrhammer PBH 2100 Profi-Set inkl. Koffer und Zubehörbox", SITE)
    assert ok and ch["identity"]["scored_on"] == "the product's name", (why, ch)
    ok, why, _ch = DB.check_item(prod, fields, "Makita Bohrhammer HR2470", SITE)
    assert not ok and why.startswith("identity"), why


def test_one_result_opened_is_invalid():
    shop = Shop()
    tr = visit_trace(shop)
    one = dict(tr, steps=tr["steps"][:2] + [dict(tr["steps"][3], k=2, page=tr["steps"][2]["page"])],
               final_page=tr["steps"][2]["page"], result={"items": tr["result"]["items"][:1]})
    ok, why, _d = C.demo_validity(one)
    assert not ok and "1 result(s) opened" in why, why


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
