"""Web tool version 9, offline (a fake shop built from saved evidence; no browser, no network, no
model): the selection and identity tokens of a version-9 tool are space- and punctuation-insensitive - a run of the
text's words joined may be an input word ("SARIS Mag+" for "SARISMag+") and a run of the input's words joined may be
a text word ("SARISMag+" for "Saris Mag+"), up to three words; letters NFKD keeps ("ł", "ø", "æ") fold to their base
letters. Never a partial word: "SarisMagnus" is not "Saris Mag".

The false failure (www.rowertour.com): the input "Trenażer Saris Mag+", the only matching tile "Trenażer SARISMag+
699,94 zł Cena katalogowa: 1322,90 zł ..." - brand and model glued without a space: 2 of 3 words named, "no result
names the input (best share 0.67 < 0.75)".

What must hold:

1. on rowertour's result list a version-9 tool picks the glued tile and answers its product; the same demonstration
   compiled as version 8 fails as measured;
2. the matching itself on the saved list and a few cases both ways, and its limits;
3. the identity check of the opened page uses the same matching (a page naming "SARISMag+" is the input's product).
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_canonical import Shop, compiled, product, run  # noqa: E402

V2 = "Trenażer Saris Mag+"
TILE = "Trenażer SARISMag+ 699,94 zł Cena katalogowa: 1322,90 zł Wysyłka w: 24h Darmowa dostawa DO KOSZYKA"


def saris_shop():
    """www.rowertour.com's search for the trainer: the glued tile, among other trainers (the demonstration's product
    first)."""
    return Shop([product("trenazer-elite-suito-t", "Trenażer Elite Suito-T", 2799.0),
                 product("trenazer-saris-mag", "Trenażer Saris Mag+", 699.94, tile=TILE),
                 product("trenazer-saris-fluid-2", "Trenażer Saris Fluid2", 1099.0)])


def test_prior_glued_tile_is_named_and_version_8_unchanged():
    shop = saris_shop()
    t9, t8 = compiled(shop, 0), compiled(shop, 0, version=8)
    r9, r8 = run(shop, t9, V2), run(shop, t8, V2)
    assert r9["status"] == "ok" and r9["output"]["name"] == "Trenażer Saris Mag+" and r9["output"]["price"] == 699.94, r9
    assert r9["picked"]["url"].endswith("/p/trenazer-saris-mag"), r9["picked"]
    assert r8["status"] == "failed" and "no result names the input" in r8["why"], r8      # the measured false failure


def test_the_matching_on_prior_saved_list_and_both_ways():
    assert C.name_share(TILE, V2) < 0.75 and C.name_share9(TILE, V2) == 1.0
    items = [{"title": TILE, "url": "https://www.rowertour.com/p/g0236901/trenazer-saris-mag", "ctx": ""}]
    assert C.best_match(items, V2)[0] is None and C.best_match(items, V2, v9=True)[0] is items[0]
    for value, text, share in [("SARISMag+", "Trenażer SARIS Mag+", 1.0), ("Canon PGI-550 PGBK", "Canon PGI550 PGBK", 1.0),
                               ("Canon PGI550 PGBK", "Canon PGI-550 PGBK", 1.0), ("Saris Mag", "SarisMagnus", 0.5),
                               ("Podkładka czarna", "PODKŁADKA czarna", 1.0), ("Grøn Lampe", "gron lampe", 1.0),
                               ("Chablis Montserre Bio 2022", "Chablis Montserre Bio 2022", 1.0),
                               ("Sauvignon Blanc", "Sauvignon", 0.5)]:
        assert C.name_share9(text, value) == share, (value, text, C.name_share9(text, value))
    # the joined words are the input's words, not extra ones; the input's phrase across the missing space
    assert C.closeness9(TILE, V2)[1] == 0 and C.closeness9("Trenażer SARISMag+", V2) == (0, 0, 0, 0)
    assert C.tokens9("Podkładka") == ["podkladka"] and C.tokens9("Wysyłka") == ["wysylka"]


def test_the_identity_of_the_opened_page_uses_the_same_matching():
    page = {"url": "https://www.rowertour.com/p/g0236901/trenazer-saris-mag", "title": "Trenażer SARISMag+",
            "h1": ["Trenażer SARISMag+"], "facts": [], "text": "Trenażer SARISMag+\n699,94 zł", "main_text": "",
            "meta": {"og:type": "product"}, "microdata": {}}
    fields = {"name": "Trenażer SARISMag+", "price": 699.94, "currency": "PLN", "url": page["url"]}
    ok8, why8, _c = C.check_product(page, fields, V2, "www.rowertour.com")
    ok9, why9, c9 = C.check_product(page, fields, V2, "www.rowertour.com", v9=True)
    assert not ok8 and "identity" in why8 and ok9 and c9["identity"]["share"] == 1.0, (why8, why9, c9)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
