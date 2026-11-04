"""``anyuse watch`` (docs/WATCH.md) diffs the same-title variants a version-7 answer lists ("first + list
variants"), offline: a fake replay stands in for the guarded browser (no
browser, no network, no model; the store is a temporary directory).

What must hold:

1. an answer with an unchanged variants list gives an EMPTY diff; the list is part of the snapshot's content;
2. a variant's shown price change is a CHANGE (with its delta), as the answer's own price is; a variant added or
   removed, or retitled, is a change too; the changelog says so;
3. an answer without variants (every version-5/6 tool, a unique closest match) snapshots and diffs exactly as before.
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_watch as TW  # noqa: E402
from anyuse.execution import watch as WA  # noqa: E402

V7_TOOL = dict(TW.PRODUCT_TOOL, version=7)
NAME = "Cerola Vitamin C Taler Grandel"
BASE = "https://www.shop.test/p/"


def variants(prices=(38.99, 21.49, 12.99), slugs=("c96", "c48", "c24")):
    vs = [{"title": NAME, "url": BASE + s, "price": f"{p:.2f} €".replace(".", ","), "price_value": p, "answered": n == 0}
          for n, (s, p) in enumerate(zip(slugs, prices))]
    return {"count": len(vs), "title": "cerola vitamin c taler grandel", "variants": vs}


def answer(v):
    rec = TW.product(38.99, name=NAME)
    rec["output"]["url"] = BASE + "c96"
    rec["output"]["variants_same_title"] = v
    rec["variants_same_title"] = v
    return rec


def test_an_unchanged_variants_list_is_an_empty_diff_and_part_of_the_content():
    with TW.Store() as s:
        item = s.st.add(s.tool(V7_TOOL), NAME, "1h")
        rp = TW.Replay({"product": [answer(variants()), answer(variants())]})
        r1 = WA.run_watch(s.st, rp, now=TW.hours(0))
        assert r1["entries"][0]["kind"] == "baseline", r1
        name = s.st.snapshots(item["id"])[-1]
        c = json.load(open(s.st.snap_dir(item["id"]) / name))["content"]
        assert c["variants_same_title"]["count"] == 3 and len(c["variants_same_title"]["variants"]) == 3, c
        r2 = WA.run_watch(s.st, rp, now=TW.hours(1))
        assert r2["entries"][0]["kind"] == "unchanged" and r2["entries"][0]["changes"] == [], r2


def test_a_variants_price_change_is_a_change_and_so_is_an_added_or_removed_one():
    with TW.Store() as s:
        s.st.add(s.tool(V7_TOOL), NAME, "1h")
        rp = TW.Replay({"product": [answer(variants()), answer(variants(prices=(38.99, 19.99, 12.99))),
                                    answer(variants(prices=(38.99, 19.99), slugs=("c96", "c48"))),
                                    answer(variants(prices=(38.99, 19.99, 9.99), slugs=("c96", "c48", "c12")))]})
        WA.run_watch(s.st, rp, now=TW.hours(0))
        r = WA.run_watch(s.st, rp, now=TW.hours(1))
        e = r["entries"][0]
        assert e["kind"] == "change" and e["changes"] == [
            {"kind": "variant_price", "title": NAME, "url": BASE + "c48", "before": "21,49 €", "after": "19,99 €",
             "before_value": 21.49, "after_value": 19.99, "delta": -1.5, "pct": -7.0}], e
        md = open(os.path.join(s.dir, "CHANGELOG.md")).read()
        assert f"variant price 21,49 € -> 19,99 € (-1.50, -7.0 %): {NAME} {BASE}c48" in md, md
        r = WA.run_watch(s.st, rp, now=TW.hours(2))
        assert [c["kind"] for c in r["entries"][0]["changes"]] == ["variant_removed"], r["entries"][0]
        assert r["entries"][0]["changes"][0]["url"] == BASE + "c24"
        r = WA.run_watch(s.st, rp, now=TW.hours(3))
        ch = r["entries"][0]["changes"]
        assert [c["kind"] for c in ch] == ["variant_added"] and ch[0]["url"] == BASE + "c12" and ch[0]["price"] == "9,99 €", ch
        assert "variant added: Cerola Vitamin C Taler Grandel 9,99 €" in WA.change_line(ch[0])


def test_without_variants_the_snapshot_and_diff_are_as_before():
    rec = TW.product()
    snap = WA.snapshot_of({"id": "x", "workflow": "product_lookup", "tool_sha256": "t", "input": "x"}, TW.PRODUCT_TOOL,
                          rec, TW.T0)
    assert set(snap["content"]) == {"fields"}, snap["content"]
    later = WA.snapshot_of({"id": "x", "workflow": "product_lookup", "tool_sha256": "t", "input": "x"}, TW.PRODUCT_TOOL,
                           TW.product(149.0), TW.T0 + dt.timedelta(hours=1))
    assert [c["kind"] for c in WA.diff(snap, later)] == ["price"]
    # a retitled variant (the same URL)
    b = {"content": {"fields": {}, "variants_same_title": variants()}}
    v = variants()
    v["variants"][2]["title"] = NAME + " (neu)"
    a = {"content": {"fields": {}, "variants_same_title": v}}
    assert [c["kind"] for c in WA.diff(b, a)] == ["variant_retitled"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
