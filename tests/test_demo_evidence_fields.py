"""Web tool version 15, offline (a fake shop; no browser, no network; the check model is faked): a product's
availability is read in the site's own rendering before the phrase lists.

What must hold:

1. the compile learns the buy-box line the site rendered the demonstrated product's structured availability with
   (the short state line holding what the demonstration reported) and where it stands after the price;
2. a page in a language no list holds (Vietnamese): the learned line in another product's box supports its
   structured InStock, and another short state line in the same place supports a structured OutOfStock - a
   version-14 tool answers "not stated" for both; the learned line against another structured class is a CONFLICT;
3. decoys stay as before: no state line, or a delivery time in the place, is "not stated" or the delivery time;
4. the lists' own cases are decided as before (German "Auf Lager" / "Ausverkauft");
5. the check model is asked only in an open scope, and its yes supports the structured class;
6. prices: when the label lists do not read the reported price back (a Turkish display "Liste fiyatı / İndirimli
   fiyat"), the compile learns the site's own labels from the demonstration's page and every other product reads its
   selling price - a version-14 tool answers the regular price; the lists' own display (German "Regulärer Preis /
   Verkaufspreis") learns nothing and reads as before; a price the page strikes through is a regular price also
   where a label calls it the selling one;
7. the answer's URL: a parameter the page's own links use to tell variants of one path apart selects a variant,
   whatever its name (a listed noise name "s"), so the canonical link without it is not answered; a parameter whose
   value varies per link while the paths tell the targets apart is noise, so the canonical link without it is the
   clean URL; standard tracking ids and the listed names stay noise.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_saleprice as SP  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402

PRICE = "749,00 €"
PRICE_DOM = '<span class="price">749,00 €</span>'
BOX = "{name}\nThương hiệu: Acme\n{prices}\n%s\nThêm vào giỏ hàng\nMô tả sản phẩm"


class Shop(SP.Shop):
    """``test_demo_saleprice.Shop`` with each product's own availability and buy box."""

    def __init__(self, per_slug, md=True):
        super().__init__(prices=PRICE, prices_dom=PRICE_DOM, md_price="749.00" if md else None,
                         ld_price=None if md else 749.0)
        self.per_slug = per_slug

    def product(self, url):
        slug = url.rstrip("/").split("/")[-1]
        self.availability, self.buy_box = self.per_slug[slug]
        return super().product(url)


def slugs(demo, v1, v2):
    return {SP.CATALOG[2][0]: demo, SP.CATALOG[0][0]: v1, SP.CATALOG[1][0]: v2}


def compiled(shop, reported, version=None):
    got = C.compile_trace(SP.trace(shop, price="749,00", availability=reported), version=version)
    assert got["ok"], got
    return got["tool"]


VI_IN = ("InStock", BOX % "Còn hàng")


def test_the_compile_learns_the_sites_state_line_and_its_place():
    shop = Shop(slugs(VI_IN, VI_IN, VI_IN))
    t15, t14 = compiled(shop, "Còn hàng"), compiled(shop, "Còn hàng", version=14)
    a = t15["fields"]["avail_anchor"]
    assert a == {"class": "in_stock", "line": "con hang", "after_price": 1, "at": a["at"]}, a
    assert t15["fields"]["evidence_rule"] == "15" and "avail_anchor" not in t14["fields"], t14["fields"]
    # a report that is a schema word, or holds a digit, learns no line
    assert "avail_anchor" not in compiled(shop, "InStock")["fields"]


def test_a_vietnamese_shop_answers_its_structured_availability_from_its_own_rendering():
    shop = Shop(slugs(VI_IN, ("OutOfStock", BOX % "Hết hàng"), VI_IN))
    t15, t14 = compiled(shop, "Còn hàng"), compiled(shop, "Còn hàng", version=14)
    r15, r14 = SP.run(shop, t15, SP.V2), SP.run(shop, t14, SP.V2)
    assert r15["status"] == "ok" and r15["output"]["availability"] == "InStock", r15
    assert r15["checks"]["availability"]["by"] == "the site's own state line", r15["checks"]
    assert r14["status"] == "ok" and r14["output"]["availability"] == C.NOT_STATED, r14
    r15, r14 = SP.run(shop, t15, SP.V1), SP.run(shop, t14, SP.V1)
    assert r15["status"] == "ok" and r15["output"]["availability"] == "OutOfStock", r15
    assert r15["checks"]["availability"]["visible_statement"] == "Hết hàng", r15["checks"]
    assert r14["output"]["availability"] == C.NOT_STATED, r14


def test_the_sites_state_line_against_another_structured_class_is_a_conflict():
    shop = Shop(slugs(VI_IN, ("OutOfStock", BOX % "Còn hàng"), VI_IN))
    t15, t14 = compiled(shop, "Còn hàng"), compiled(shop, "Còn hàng", version=14)
    r15, r14 = SP.run(shop, t15, SP.V1), SP.run(shop, t14, SP.V1)
    assert r15["status"] == "failed" and "availability" in r15["conflicts"], r15
    assert r14["status"] == "ok" and r14["output"]["availability"] == C.NOT_STATED, r14


def test_decoys_stay_not_stated_or_their_delivery_time():
    shop = Shop(slugs(VI_IN, ("OutOfStock", "{name}\n{prices}\nLieferzeit: 13 Wochen\nMô tả sản phẩm"),
                      ("InStock", "{name}\n{prices}\nMô tả sản phẩm dài")))
    t15, t14 = compiled(shop, "Còn hàng"), compiled(shop, "Còn hàng", version=14)
    for v, want in ((SP.V1, "Lieferzeit: 13 Wochen"), (SP.V2, C.NOT_STATED)):
        r15, r14 = SP.run(shop, t15, v), SP.run(shop, t14, v)
        assert r15["output"]["availability"] == want == r14["output"]["availability"], (v, r15, r14)


def test_the_lists_own_cases_are_decided_as_before():
    de = "{name}\n{prices}\n%s\nIn den Warenkorb"
    shop = Shop(slugs(("InStock", de % "Auf Lager"), ("OutOfStock", de % "Ausverkauft"), ("InStock", de % "Auf Lager")))
    t15, t14 = compiled(shop, "Auf Lager"), compiled(shop, "Auf Lager", version=14)
    for v, want in ((SP.V1, "OutOfStock"), (SP.V2, "InStock")):
        r15, r14 = SP.run(shop, t15, v), SP.run(shop, t14, v)
        assert r15["output"]["availability"] == want == r14["output"]["availability"], (v, r15, r14)


def test_the_check_model_is_asked_only_in_a_scope():
    shop = Shop(slugs(VI_IN, VI_IN, ("InStock", BOX % "Sẵn sàng giao")))
    t15 = compiled(shop, "InStock")              # no learned line: the model rung decides
    page = shop.product(f"{SP.BASE}/p/{SP.CATALOG[1][0]}")
    page["microdata"] = K.microdata_of(page["dom"])          # as the page's own reader gives it
    fields = C.extract_fields(page, t15["fields"], SP.V2)
    assert fields["availability"] == "InStock" and fields["price"] == 749.0, fields
    asked = []

    def fake(instructions, state, obs):
        asked.append(state.rsplit(">> ", 1)[-1])
        return 0.9 if "Sẵn sàng" in state.rsplit(">> ", 1)[-1] else 0.1
    with MC.using(fake):
        assert C.site_availability15(page, fields, t15["fields"]) == {} and not asked
        with MC.scope("product"):
            got = C.site_availability15(page, fields, t15["fields"])
    assert got["supports"] and got["by"] == "model_check" and got["line"] == "Sẵn sàng giao", got
    with MC.scope("product"), MC.using(lambda i, s, o: 0.05):
        got = C.site_availability15(page, fields, t15["fields"])
    assert got["supports"] is False, got
    av, ev = C.availability_answer(page, fields, t15["fields"], SP.V2)
    assert av == C.NOT_STATED, (av, ev)                  # outside a scope: the phrase lists, as before


TR_BOX = "{name}\nMarka: Acme\n{prices}\nSepete ekle\nÜrün açıklaması"


class TextPriceShop(SP.Shop):
    """Prices in the page text only (no structured price), per product."""

    def __init__(self, prices):
        super().__init__(prices="", prices_dom="", md_price=None, ld_price=None, buy_box=TR_BOX)
        self.per_slug = prices

    def product(self, url):
        self.prices = self.per_slug[url.rstrip("/").split("/")[-1]]
        return super().product(url)


def tr(regular, current):
    return f"Liste fiyatı:\n{regular} €\nİndirimli fiyat:\n{current} €"


def test_the_sites_own_price_labels_read_the_selling_price():
    shop = TextPriceShop({SP.CATALOG[2][0]: tr("1.299,00", "999,00"), SP.CATALOG[0][0]: tr("2.499,00", "1.999,00"),
                          SP.CATALOG[1][0]: tr("89,00", "69,00")})
    t15 = C.compile_trace(SP.trace(shop, price="999,00", availability="Stokta"))["tool"]
    t14 = C.compile_trace(SP.trace(shop, price="999,00", availability="Stokta"), version=14)["tool"]
    assert t15["fields"]["price"] == "text" and t15["fields"]["price_rule"] == "current", t15["fields"]
    assert t15["fields"]["price_labels"] == {"regular": ["liste fiyatı"], "current": ["indirimli fiyat"]}, t15["fields"]
    assert "price_labels" not in t14["fields"]
    for v, want, old in ((SP.V1, 1999.0, 2499.0), (SP.V2, 69.0, 89.0)):
        r15, r14 = SP.run(shop, t15, v), SP.run(shop, t14, v)
        assert r15["status"] == "ok" and r15["output"]["price"] == want, r15
        assert r14["status"] == "ok" and r14["output"]["price"] == old, r14


def test_the_lists_own_display_learns_nothing_and_reads_as_before():
    shop = SP.Shop()
    t15 = C.compile_trace(SP.trace(shop))["tool"]
    assert "price_labels" not in t15["fields"] and t15["fields"]["price_rule"] == "current", t15["fields"]
    for v in (SP.V1, SP.V2):
        assert SP.run(shop, t15, v)["output"]["price"] == 49.99


def test_a_struck_price_is_regular_whatever_its_label_says():
    text = "Jetzt 59,99 €\n49,99 €"
    old = C.current_price(text, 0, ["59,99 €"])
    new = C.current_price(text, 0, ["59,99 €"], struck_first=True)
    assert old["current"] is None, old                    # labelled the selling price: the styling not read
    assert new["current"]["value"] == 49.99 and [m["value"] for m in new["regular"]] == [59.99], new


def _page(links, canonical, url):
    return {"url": url, "canonical": canonical, "h1": ["Shirt Classic"], "facts": [],
            "elements": [{"i": n, "tag": "a", "text": t, "href": h, "area": "main"} for n, (t, h) in enumerate(links)]}


def test_the_pages_own_links_say_which_url_parameter_selects_a_variant():
    base = "https://www.butik.test"
    loaded = base + "/p/shirt-classic?s=M"
    page = _page([("M", base + "/p/shirt-classic?s=M"), ("L", base + "/p/shirt-classic?s=L")],
                 base + "/p/shirt-classic", loaded)
    fields = {"name": "Shirt Classic"}
    url15, ev15 = C.answer_url(page, fields, {"url_rule": "same_product", "evidence_rule": "15"})
    url14, ev14 = C.answer_url(page, fields, {"url_rule": "same_product"})
    assert url15 == loaded and ev15["same_product"] is False, ev15
    assert url14 == base + "/p/shirt-classic" and ev14["same_product"] is True, ev14      # "s" read as noise
    # a per-link value on links the paths tell apart is noise: the canonical without it is the clean URL
    loaded2 = base + "/p/shirt-classic?trkx=a1"
    page2 = _page([("A", base + "/p/a?trkx=a1"), ("B", base + "/p/b?trkx=b2"), ("C", base + "/p/c?trkx=c3")],
                  base + "/p/shirt-classic", loaded2)
    assert C.answer_url(page2, fields, {"url_rule": "same_product", "evidence_rule": "15"})[0] == base + "/p/shirt-classic"
    assert C.answer_url(page2, fields, {"url_rule": "same_product"})[0] == loaded2
    # the listed names stay noise for both (a standard tracking id is dropped from the URL's identity before)
    for q in ("sessionid=9", "ref=abc", "zenid=7f"):
        p3 = _page([], base + "/p/shirt-classic", base + "/p/shirt-classic?" + q)
        for srcs in ({"url_rule": "same_product", "evidence_rule": "15"}, {"url_rule": "same_product"}):
            assert C.answer_url(p3, fields, srcs)[0] == base + "/p/shirt-classic", (q, srcs)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
