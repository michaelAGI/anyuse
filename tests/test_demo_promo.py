"""Web tool version 12: a card in a promotion / advertising / recommendation block beside the results is never a
result.

The drift simulator's held-out false accept (the repair ladder's measurement: www.wintotal.de, ``insert_block``
seed 3): a block of CLONED result cards (``<div class="recommendations">`` around copies of the result card, linking
other pages) before the results; the version-10 tool's card structure read the clones as its first results, its
runtime checks accepted the answer. Version 12 reads each card's container chain: a card inside a container whose
class / id names a promotion, an advertisement or a recommendation (``demo_cards.PROMO_WORDS12``, the 11 languages)
that does not hold every card of the structure is left out (``promo_flags12``; in the page ``CARDS_JS_V12``).

What must hold (the fake shop of ``test_repair_ladder.py``):

1. a block of cloned result cards in a ``sponsored-items`` / ``recommendations`` / ``Anzeigen`` container before the
   results: the version-11 tool answers the clones first (the false accept); the version-12 tool answers the results;
2. the results' OWN container named like a recommendation holds every card: nothing is left out; a "promo" class
   inside the result cards (a sale badge on the price) is no container above them: the cards stay;
3. the in-page read and the recorded read agree (``CARDS_JS_V12`` / ``cards_map_v12``); older card reads unchanged.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from test_repair_ladder import BASE, Shop, read, trace  # noqa: E402

Q = "Akkuschrauber"
CLONES = [("Akkuschrauber Bit-Set 32-teilig", "akku-bits"), ("Akkuschrauber Koffer XL", "akku-koffer"),
          ("Akkuschrauber Ladegerät 12V", "akku-lader")]


class PlainShop(Shop):
    """The shop's result list inside a plain ``div`` (the clones below share the results' card chain)."""

    def results(self, q):
        return super().results(q).replace("<div class=\"listing\">", "<div><div class=\"listing\">", 1) \
            .replace("</ul></div>", "</ul></div></div>", 1)


class PromoShop(PlainShop):
    """A block of cloned result cards (the results' own markup) in a ``wrapper`` container before the results;
    ``inside``: the results' own list wrapped in that container instead; ``badge``: a class on every card's price."""

    def __init__(self, wrapper="sponsored-items", inside=False, badge="", **kw):
        super().__init__(**kw)
        self.wrapper, self.inside, self.badge = wrapper, inside, badge

    def results(self, q):
        html = super().results(q)
        if self.badge:
            html = html.replace("<span class=\"price\">", f"<span class=\"price {self.badge}\">")
        if self.inside:
            return html.replace("<div><div class=\"listing\">", f"<div class=\"{self.wrapper}\"><div><div class=\"listing\">", 1) \
                .replace("</ul></div></div>", "</ul></div></div></div>", 1)
        cards = "".join(f"<li class=\"product\"><div class=\"info\"><h3 class=\"name\"><a class=\"title\" href=\"/p/{s}\">"
                        f"{n}</a></h3><span class=\"price\">9,99 €</span></div></li>" for n, s in CLONES)
        block = f"<div class=\"{self.wrapper}\"><div><div class=\"listing\"><ul class=\"products\">{cards}</ul></div></div></div>"
        return html.replace("<div><div class=\"listing\">", block + "<div><div class=\"listing\">", 1)


def tool(version):
    g = C.compile_trace(trace(PlainShop(), Q, "site_search"), version=version)
    assert g["ok"], g.get("why")
    return g["tool"]


def answer(site, t):
    out = asyncio.run(T.run_tool(D.DomPage(site), t, Q, D.no_settle, result_wait_s=0.0))
    return out["status"], [r["url"].rsplit("/", 1)[-1] for r in (out.get("output") or {}).get("results") or []]


def test_a_promo_block_of_cloned_cards_is_not_read():
    t11, t12 = tool(11), tool(12)
    assert t12["region"]["cards_version"] == 12 and t11["region"]["cards_version"] == 11
    real = answer(PlainShop(), t12)
    assert real[0] == "ok" and len(real[1]) == 5 and real == answer(PlainShop(), t11)
    for wrapper in ("sponsored-items", "recommendations", "searchAnzeigen", "top-seller-box", "ad-slot"):
        st11, got11 = answer(PromoShop(wrapper), t11)
        st12, got12 = answer(PromoShop(wrapper), t12)
        assert st11 == "ok" and got11[:3] == ["akku-bits", "akku-koffer", "akku-lader"], (wrapper, got11)
        assert (st12, got12) == real, (wrapper, got12)
    # a container that is no promotion word ("teaser", "shadow", "loading") is read as before
    for wrapper in ("teaser", "shadow-box", "loading"):
        assert answer(PromoShop(wrapper), t12)[1][:3] == ["akku-bits", "akku-koffer", "akku-lader"], wrapper


def test_the_results_own_container_and_a_badge_on_a_card():
    t12 = tool(12)
    real = answer(PlainShop(), t12)
    assert answer(PromoShop("product-recommendations", inside=True), t12) == real
    assert answer(PromoShop(badge="promo", inside=True, wrapper="results"), t12) == real


def test_a_sale_badge_on_a_cards_own_tile_is_no_block():
    """A shop marks discounted results with ``promo-tag`` on the result's own tile (www.skybad.de: ``li.item
    product product-item promo-tag sale`` around ``div.product-item-info``): one card, no block - never left out; two
    cloned cards in a ``recommendations`` container are a block."""
    tile = ("<li class=\"item product product-item{extra}\"><div class=\"product-item-info\"><strong class=\"product name\">"
            "<a class=\"product-item-link\" href=\"/p/{s}\">{n}</a></strong></div></li>")
    lst = "".join(tile.format(extra=" promo-tag sale" if k % 2 == 0 else "", s=f"p{k}", n=f"AquaCraft {k}") for k in range(4))
    rec = "".join(tile.format(extra="", s=f"r{k}", n=f"AquaCraft R{k}") for k in range(2))
    dom = (f"<html><body><main><div class=\"recommendations\"><ol class=\"products\">{rec}</ol></div>"
           f"<ol class=\"products list\">{lst}</ol></main></body></html>")
    root, _b = K.parse(dom)
    with K.bem_free():
        links = [a for a in K.walk(root) if a.tag == "a"]
        bases = [K._tile_of(a) for a in links]
    assert K.promo_flags12(bases) == [1, 1, 0, 0, 0, 0], K.promo_flags12(bases)


def test_the_page_read_and_the_recorded_read_agree():
    t12 = tool(12)
    site = PromoShop("sponsored-items")
    url = f"{BASE}/search?q={Q}"
    rec, _u = site.route(url)
    page = dict(read(site, url), dom=rec.dom)
    specs = t12["region"]["cards"]
    js = K.cards_js_v12(specs)
    assert js.strip().startswith(K.MARK12) and K.specs_of_js_v12(js) == specs and "sponsored" in K.CARDS_JS_V12
    got = K.recorded_answer(js, page)
    flags = [row[8] for row in got["cards"]]
    assert flags == [1, 1, 1, 0, 0, 0, 0, 0], flags
    root, _b = K.parse(rec.dom)
    assert [row[8] for row in K.cards_map_v12(root, specs)["cards"]] == flags
    assert K.promo_tokens12(K.Node("div", {"class": "topSeller-box", "id": "x"}, None)) == \
        ["top", "seller", "box", "x", "topseller", "sellerbox", "boxx"]
    # the version-11 read is the version-11 read (no 9th value)
    assert all(len(row) == 8 for row in K.recorded_answer(K.cards_js_v11(specs), page)["cards"])


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
