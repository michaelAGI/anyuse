"""Web tool version 11, fix 3 (the drift census): a structured price is a machine number.

The census's case (research ``snapshots/r7_www_vamida_at``): the product page's microdata ``price`` is "8.159" (a net
price with three decimals), its Open Graph ``product:price:amount`` "8.16", the buy box "€ 8,16 / VK* € 9,95". The
price reader takes "8.159" for a thousands group - 8159.0 - and version 8's price check calls it a CONFLICT against
the shown 8.16 (both arms: a break of an answer the last wave gave).

What must hold (a fake product page shaped on that snapshot):

1. with ``number_rule: "11"`` the microdata, Open Graph and JSON-LD prices are read as machine numbers - "8.159" is
   8.159 - and the page has no price CONFLICT; a price that disagrees with the shown one is still one;
2. without the key (versions 6-10) every reading is exactly as before (8159.0 and the CONFLICT); ``web_arch``'s
   readers keep their defaults;
3. a grouped or comma price ("1.299,00", "8,16") is never a machine number.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402

NAME = "Weleda Mandel Wohltuende Reinigungsmilch"
SRC10 = {"name": "h1", "price": "og", "availability": "microdata", "availability_vocab": "8", "url": "canonical",
         "url_rule": "same_product", "text_rule": "9", "statement_rule": "10", "price_rule": "current"}
SRC11 = dict(SRC10, stock_rule="11", number_rule="11")


def page(md_price="8.159", og="8.16", ld_price=None):
    blocks = [json.dumps({"@type": "Product", "name": NAME, "offers": {"@type": "Offer", "price": ld_price,
                                                                        "priceCurrency": "EUR"}})] if ld_price else []
    # the census page's buy box (research runs/census/r7_www_vamida_at/v2_S_tool.json), its availability line "Auf Lager"
    text = (f"Direkt zum Inhalt\nWarenkorb\n-18%\n{NAME}\nWohltuende Reinigungsmilch \u00a0 Weleda GesmbH & CO KG\n"
            f"Reinigt schonend die Haut\n{NAME} reinigt empfindliche Haut besonders sanft und eignet sich zudem zur "
            f"Entfernung von Make-up.\n\u20ac\u00a08,16\nVK* \u20ac\u00a09,95\n\ninkl. MwSt. zzgl. Versand\n\nIn den Warenkorb\n"
            f"Auf Lager\n\nKurzinformation")
    return {"url": "https://www.v.test/vichy-200", "title": NAME, "h1": [NAME], "text": text, "main_text": text,
            "jsonld": blocks, "facts": W.product_facts(blocks), "canonical": "",
            "microdata": {"name": "", "price": md_price, "currency": "EUR", "availability": "http://schema.org/InStock"},
            "meta": {"product:price:amount": og, "product:price:currency": "EUR"}, "struck_prices": ["€ 9,95"]}


def conflicts(p, src):
    f = C.extract_fields(p, src, NAME)
    return f, C.field_conflicts(p, f, src, NAME, v8=True)


def test_the_census_page_reads_8_159_in_version_11():
    p = page()
    f10, c10 = conflicts(p, SRC10)
    f11, c11 = conflicts(p, SRC11)
    assert "price" in c10 and c10["price"]["structured"][0]["value"] == 8159.0, c10
    assert "price" not in c11 and f11["price"] == 8.16, (f11, c11)
    # a microdata-sourced tool answers the machine number
    f, _c = conflicts(p, dict(SRC11, price="microdata"))
    assert f["price"] == 8.159
    # JSON-LD read as machine numbers only in version 11
    q = page(ld_price="8.159")
    assert C.extract_fields(q, dict(SRC10, price="jsonld"), NAME)["price"] == 8159.0
    assert C.extract_fields(q, dict(SRC11, price="jsonld"), NAME)["price"] == 8.159


def test_a_real_price_disagreement_stays_a_conflict():
    for src in (SRC10, SRC11):
        _f, c = conflicts(page(md_price="7.50"), src)
        assert "price" in c and c["price"]["visible"] == 8.16 and c["price"]["structured"][0]["value"] == 7.5, c


def test_defaults_and_grouped_numbers():
    assert W._num("8.159") == 8159.0 and W._num("8.159", machine=True) == 8.159
    assert W._num("1.299,00", machine=True) == 1299.0 and C._price11("8,16") == 8.16 and C._price11("1.299") == 1.299
    assert W.product_facts(['{"@type": "Product", "name": "x", "offers": {"price": "8.159"}}'])[0]["price"] == 8159.0


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
