"""Web tool version 10, part 2, WATCH, offline (no browser, no network, no model): dated snapshots and deterministic
diffs for the breadth workflows' output shapes - a LIST OF N (a filtered / sorted search: its results with the card
prices it read and the refinement applied; a paged search: its N results with their pages and the site's stated total)
and a TABLE OF K ITEMS (visited results: each row's name, price, availability, URL and status). A replay that is not
"ok" (an incomplete table, a failed sort check) is DRIFT, never a content change; the old workflows' snapshots and
diffs are unchanged.

What must hold:

1. a list of N: results added / removed / moved, a shown price changed, the stated total changed;
2. a table of K: a row's price and availability changes (with the row's rank and name), a row added / removed /
   moved, a row whose status changed; the changelog lines name them;
3. an incomplete table is drift; the product-lookup and site-search contents are as before.
"""
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import watch as WA  # noqa: E402

AT = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
TOOL = {"version": 10, "fields": {"name": "jsonld"}}


def item(wf):
    return {"id": f"x-{wf}", "workflow": wf, "input": "Akkuschrauber", "tool_sha256": "0" * 64, "site": "www.bshop.test"}


def snap(wf, output, status="ok", at=AT):
    rec = {"status": status, "output": output, "pages": [{"url": "https://www.bshop.test/search?q=x", "title": "t"}],
           "why": "w", "checks": {}}
    return WA.snapshot_of(item(wf), TOOL, rec, at)


def res(*rows):
    return [{"title": t, "url": f"https://www.bshop.test/p/{u}", "price": p, "page": pg} for t, u, p, pg in rows]


def row(rank, u, name, price, avail="InStock", status="ok"):
    return {"rank": rank, "url": f"https://www.bshop.test/p/{u}", "title": name, "name": name, "price": price,
            "currency": "EUR", "availability": avail, "status": status, "why": ""}


def test_a_list_of_n():
    a = snap("paged_search", {"results": res(("A", "a", 10.0, 1), ("B", "b", 12.0, 1), ("C", "c", 15.0, 2)), "total": 3})
    b = snap("paged_search", {"results": res(("B", "b", 12.0, 1), ("A", "a", 10.0, 1), ("D", "d", 9.0, 2)), "total": 4})
    assert a["good"] and a["content"]["total"] == 3 and a["content"]["results"][2]["page"] == 2, a["content"]
    kinds = sorted(c["kind"] for c in WA.diff(a, b))
    assert kinds == ["added", "moved", "moved", "removed", "total"], WA.diff(a, b)
    assert WA.diff(a, a) == []
    f1 = snap("filtered_search", {"results": res(("A", "a", 10.0, None), ("B", "b", 12.0, None)),
                                  "applied": [{"kind": "sort", "key": "price_asc"}]})
    f2 = snap("filtered_search", {"results": res(("A", "a", 11.5, None), ("B", "b", 12.0, None)),
                                  "applied": [{"kind": "sort", "key": "price_asc"}]})
    ch = WA.diff(f1, f2)
    assert [c["kind"] for c in ch] == ["shown_price"] and ch[0]["before"] == 10.0 and ch[0]["after"] == 11.5, ch
    assert WA.change_line(ch[0]).startswith("shown price #1 10.0 -> 11.5")
    assert f1["content"]["applied"] == [{"kind": "sort", "key": "price_asc"}]


def test_a_table_of_k():
    t1 = snap("visit_results", {"items": [row(1, "a", "Bosch A", 89.0), row(2, "b", "Makita B", 79.5)], "k": 2})
    t2 = snap("visit_results", {"items": [row(1, "a", "Bosch A", 84.0, "OutOfStock"), row(2, "c", "Einhell C", 49.99)],
                                "k": 2})
    assert t1["good"] and t1["content"]["table"][0]["price"] == 89.0 and t1["evidence"]["sources"] == TOOL["fields"]
    ch = WA.diff(t1, t2)
    kinds = [c["kind"] for c in ch]
    assert kinds.count("row_field") == 2 and "row_added" in kinds and "row_removed" in kinds, ch
    lines = [WA.change_line(c) for c in ch]
    assert any(ln.startswith("row #1 Bosch A: price 89.0") and "-5.00" in ln for ln in lines), lines
    assert any(ln.startswith("row #1 Bosch A: availability InStock (in_stock) -> OutOfStock (out_of_stock)") for ln in lines)
    t3 = snap("visit_results", {"items": [row(1, "b", "Makita B", 79.5), row(2, "a", "Bosch A", 89.0)], "k": 2})
    assert sorted(c["kind"] for c in WA.diff(t1, t3)) == ["row_moved", "row_moved"], WA.diff(t1, t3)
    e = WA.entry_of(item("visit_results"), t1, t2)
    assert e["kind"] == "change" and "row #1 Bosch A" in WA.render_entry(e), WA.render_entry(e)


def test_drift_and_the_old_contents():
    inc = snap("visit_results", {"items": [row(1, "a", "Bosch A", 89.0), dict(row(2, "b", "Makita B", None),
                                                                                status="conflict")], "incomplete": True},
               status="incomplete")
    assert not inc["good"] and inc["drift"]["status"] == "incomplete", inc
    base = snap("visit_results", {"items": [row(1, "a", "Bosch A", 89.0)], "k": 1})
    assert WA.entry_of(item("visit_results"), base, inc)["kind"] == "drift"
    s1 = WA.snapshot_of(dict(item("site_search"), workflow="site_search"), {"version": 9},
                        {"status": "ok", "output": {"results": [{"title": "A", "url": "u"}]}, "pages": []}, AT)
    assert s1["content"] == {"results": [{"title": "A", "url": "u"}]}
    p1 = WA.snapshot_of(dict(item("product_lookup"), workflow="product_lookup"), {"version": 9, "fields": {"name": "h1"}},
                        {"status": "ok", "output": {"name": "A", "price": 1.0, "currency": "EUR", "availability": "InStock",
                                                    "url": "u"}, "pages": []}, AT)
    assert set(p1["content"]) == {"fields"} and p1["evidence"]["sources"] == {"name": "h1"}


def test_a_breadth_tool_can_be_watched():
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "t.tool.json")
        with open(tp, "w") as fh:
            json.dump({"version": 10, "workflow": "paged_search", "site": "www.bshop.test",
                       "search": {"mode": "url", "template": "https://www.bshop.test/search?q={q}"}, "n": 8}, fh)
        st = WA.WatchStore(os.path.join(d, "store"))
        it = st.add(tp, "Akkuschrauber", "1d")
        assert it["workflow"] == "paged_search", it
        with open(tp, "w") as fh:
            json.dump({"version": 9, "workflow": "paged_search", "site": "www.bshop.test",
                       "search": {"mode": "url", "template": "https://www.bshop.test/search?q={q}"}}, fh)
        try:
            st.add(tp, "Bohrhammer", "1d")
            raise AssertionError("a version-9 tool of a breadth workflow was accepted")
        except ValueError as exc:
            assert "not a compiled demonstration tool" in str(exc)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
