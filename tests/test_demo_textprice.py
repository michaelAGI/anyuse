"""A product's TEXT price is self-checked at compile time (www.tanzschuhe.de: the product page's first price was
its free-shipping threshold "Gratis Versand ... ab 40 €", above the product's own 89,00 €, and the version-6
rule - the first price of the main text - read 40 € on every held-out product). Offline (a fake shop; no
browser, no network, no model).

What must hold:

(These are the VERSION-7 compile's rules - ``compile_trace(version=7)``; version 8 reads a text price
with the current-price rule, ``test_demo_saleprice.py``.)

1. a text price whose FIRST price is the product's compiles as before (no ``price_at``: the version-6 rule, the
   same tool);
2. a text price below a banner price compiles with ``price_at: after_name`` (the first price after the
   product's name), which reads the reported price back on the demonstration's page and the right price on
   another product's page;
3. a text price at neither place keeps the version-6 rule, noted (the change is only where the after-name rule
   demonstrably reads the reported price and the first-price rule does not);
4. at run time, a page that does not name the product gives no price (a failed check, never a guess); a tool
   without ``price_at`` (every tool compiled before) reads exactly as before.
"""
import copy
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402

BANNER = "Gratis Versand innerhalb Deutschlands ab 40 €!\nHilfe Konto Merkliste\n"


def text_trace(banner=BANNER, own_price="49,99 €", reported_price="49,99 €", tail=""):
    """The fake shop's product lookup with NO structured price (the text is the only price source)."""
    tr = TC.product_trace(reported={"name": "Acme Kettle 2000 Steel", "price": reported_price, "currency": "EUR",
                                    "availability": "", "url": TC.BASE + "/p/acme-kettle-2000"})
    fin = tr["final_page"]
    for p in [fin, tr["steps"][2]["page"]]:
        p["jsonld"], p["facts"] = [], []
        p["text"] = p["main_text"] = f"{banner}Acme Kettle 2000 Steel\n{own_price}\nArt.-Nr. acme-kettle-2000\n{tail}"
    return tr


def page_of(name, price, banner=BANNER):
    return {"url": TC.BASE + "/p/x", "title": f"{name} | Shop Test", "h1": [name], "canonical": TC.BASE + "/p/x",
            "text": f"{banner}{name}\n{price}\nIn den Warenkorb", "facts": [], "meta": {}, "microdata": {}}


def test_a_first_price_compiles_as_before():
    got = C.compile_trace(text_trace(banner="Hilfe Konto Merkliste\n"), version=7)
    assert got["ok"], got
    assert got["tool"]["fields"]["price"] == "text" and "price_at" not in got["tool"]["fields"], got["tool"]["fields"]


def test_a_price_below_a_banner_price_is_read_after_the_name():
    got = C.compile_trace(text_trace(), version=7)
    assert got["ok"], got
    f = got["tool"]["fields"]
    assert f["price"] == "text" and f["price_at"] == "after_name", f
    assert any("price_at: after_name" in n for n in got["report"]["field_notes"]), got["report"]["field_notes"]
    # another product's page: its own price, never the banner's
    out = C.extract_fields(page_of("Borealis Toaster 4 Slice", "39,50 €"), f, "Borealis Toaster 4 Slice")
    assert out["price"] == 39.5 and out["currency"] == "EUR", out
    # the version-6 rule alone would have read the banner
    old = {k: v for k, v in f.items() if k != "price_at"}
    assert C.extract_fields(page_of("Borealis Toaster 4 Slice", "39,50 €"), old, "Borealis Toaster 4 Slice")["price"] == 40.0


def test_a_price_at_neither_place_keeps_the_version_6_rule():
    # the reported price is shown only far below, after another price that follows the name
    tr = text_trace(own_price="UVP 59,99 €", reported_price="49,99 €", tail="Jetzt nur 49,99 €")
    got = C.compile_trace(tr, version=7)
    assert got["ok"] and "price_at" not in got["tool"]["fields"], got
    assert any("version-6 rule kept" in n for n in got["report"]["field_notes"]), got["report"]["field_notes"]
    # version 8: the UVP is a regular price - the current price after the name is read
    f8 = C.compile_trace(tr)["tool"]["fields"]
    assert f8["price_rule"] == "current" and f8["price_at"] == "after_name", f8


def test_at_run_time_a_page_without_the_name_gives_no_price():
    f = {"name": "h1", "price": "text", "availability": "none", "url": "canonical", "price_at": "after_name"}
    page = page_of("Acme Kettle 2000 Steel", "49,99 €")
    page["h1"] = ["Something else"]
    page["title"] = "Something else"
    out = C.extract_fields(page, f, "Acme Kettle 2000 Steel")
    assert out["price"] is None, out
    ok, why, _checks = C.check_product(page, out, "Acme Kettle 2000 Steel", "www.shop.test")
    assert not ok, why
    # a tool without price_at reads the first price, as every earlier tool does
    f2 = {k: v for k, v in f.items() if k != "price_at"}
    assert C.extract_fields(page_of("Acme Kettle 2000 Steel", "49,99 €", banner=""), f2, "x")["price"] == 49.99
    assert C.text_price("a 5 € b", "after_name", "") is None and C.text_price("", "", "") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
