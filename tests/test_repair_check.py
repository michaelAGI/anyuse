"""The repair ladder's independent checker (``execution/repair_check.py``), offline
on the fake shop of ``test_repair_ladder`` (no browser, no network, no model).

What must hold:

1. a product answer: agrees when the oracle's own search lists the product and its page shows the answered name and
   price; disagrees for a product the search does not list, or a price the page does not show;
2. a site-search answer: agrees when the oracle's content links hold the answered results in order; disagrees for
   results the render does not list, another order, or titles that do not name the input (a promo block);
3. no answer, or an oracle search with no box and no search URL: undeterminable - never an agreement;
4. through the ladder with NO last known good answer: the checker decides, and a repair it agrees with is accepted.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_check as K  # noqa: E402
from test_repair_ladder import BASE, RENAMED, Shop, Store, compiled, ladder_run  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"


def checker(site):
    return K.OracleChecker(lambda: D.DomPage(site), D.no_settle, wait_s=0.0)


def ask(site, tool, value, out):
    return asyncio.run(checker(site)(tool, value, out))


def test_a_product_answer():
    tool = compiled(V)
    good = {"status": "ok", "output": {"name": V, "price": 89.9, "url": BASE + "/p/bosch-gsr-12v"}}
    assert ask(Shop(), tool, V, good)["verdict"] == "agrees"
    other = {"status": "ok", "output": {"name": "Ryobi R18 Bohrhammer", "price": 129.0, "url": BASE + "/p/ryobi-r18"}}
    got = ask(Shop(), tool, V, other)
    assert got["verdict"] == "disagrees" and not got["listed"]
    price = {"status": "ok", "output": {"name": V, "price": 79.5, "url": BASE + "/p/bosch-gsr-12v"}}
    assert ask(Shop(), tool, V, price)["verdict"] == "disagrees"


def test_a_list_answer():
    v = "Akkuschrauber"
    tool = compiled(v, "site_search")
    shop = Shop()
    names = [p for p in shop.hits(v)]
    good = {"status": "ok", "output": {"results": [{"title": n, "url": f"{BASE}/p/{s}"} for n, _p, s in names[:3]]}}
    assert ask(shop, tool, v, good)["verdict"] == "agrees"
    rev = {"status": "ok", "output": {"results": list(reversed(good["output"]["results"]))}}
    assert ask(shop, tool, v, rev)["verdict"] == "disagrees"
    promo = {"status": "ok", "output": {"results": [{"title": "Ryobi R18 Bohrhammer", "url": f"{BASE}/p/ryobi-r18"},
                                                    {"title": "Makita HR2470 Bohrhammer", "url": f"{BASE}/p/makita-hr2470"}]}}
    assert ask(Shop(promo=True), tool, v, promo)["verdict"] == "disagrees"


def test_the_judgement_on_recorded_renders():
    """The same judgement on RECORDED renders (``RecordedChecker``) as on the live renders."""
    import asyncio
    from anyuse.execution import demo_oracle as O
    tool = compiled(V)
    shop = Shop()
    ev = asyncio.run(O.oracle_search(D.DomPage(shop), BASE + "/", V, [], D.no_settle, 0.0))
    item = asyncio.run(O.oracle_item(D.DomPage(shop), BASE + "/p/bosch-gsr-12v", D.no_settle))
    rec = K.RecordedChecker(lambda t, v: (ev, {BASE + "/p/bosch-gsr-12v": item}) if v == V else None)
    good = {"status": "ok", "output": {"name": V, "price": 89.9, "url": BASE + "/p/bosch-gsr-12v"}}
    price = {"status": "ok", "output": {"name": V, "price": 79.5, "url": BASE + "/p/bosch-gsr-12v"}}
    for out in (good, price):
        assert rec(tool, V, out)["verdict"] == ask(shop, tool, V, out)["verdict"]
    assert rec(tool, "Makita", good)["verdict"] == "undeterminable"
    other = {"status": "ok", "output": {"name": "Ryobi R18 Bohrhammer", "price": 129.0, "url": BASE + "/p/ryobi-r18"}}
    assert rec(tool, V, other)["verdict"] == "undeterminable"           # no render of that item page


def test_undeterminable_is_never_agreement():
    tool = compiled(V)
    assert ask(Shop(), tool, V, {"status": "failed", "why": "x"})["verdict"] == "undeterminable"
    t2 = dict(tool, search=dict(tool["search"], fallback_templates=[]))

    class NoBox(Shop):
        def start(self):
            return "<html><body><main><h1>Willkommen</h1></main></body></html>"
    got = ask(NoBox(), t2, V, {"status": "ok", "output": {"name": V, "price": 89.9, "url": BASE + "/p/bosch-gsr-12v"}})
    assert got["verdict"] == "undeterminable", got


def test_the_checker_decides_without_a_known_good_answer():
    tool = compiled(V)
    drift = Shop(cls=RENAMED)
    st = Store(tool)
    try:
        out = ladder_run(drift, tool, V, st.s, checker=checker(drift))
        lad = out["ladder"]
        assert lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        g = st.s.revisions()[-1]["evidence"]["gate"]
        assert g["same_as_lkg"] is False and g["checker"]["verdict"] == "agrees"
    finally:
        st.close()


if __name__ == "__main__":
    test_a_product_answer()
    test_a_list_answer()
    test_undeterminable_is_never_agreement()
    test_the_checker_decides_without_a_known_good_answer()
    test_the_judgement_on_recorded_renders()
    print("test_repair_check: 5 ok")
