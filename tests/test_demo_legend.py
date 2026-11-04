"""Web tool version 10, offline (a fake shop built from saved evidence; no browser, no network, no
model): a stock-state LEGEND is not a statement. A list of the availability classes near the buy box ("En stock / En
rupture temporaire / Non vendu au magasin": the key of a store-stock widget) names several classes; the product's own
statement is the class next to its price or its add-to-cart control.

The evidence (www.naturalia.fr): the search landed on the product's page; JSON-LD InStock; the buy box (the
separator text) reads "12,99 € / En stock / 24,33 € / Litres / Renseignez votre code postal ... / Rechercher un magasin
/ En stock / En rupture temporaire / au magasin / Non vendu au magasin". Version 8's buy-box reading (kept by version 9)
saw "in stock" AND "out of stock", stated no class, and the structured InStock was answered as "not stated" - the
reader: wrong ("the page says En stock").

What must hold:

1. naturalia's buy box: a version-10 tool answers the structured InStock (the legend removed, the box states "En
   stock"); the same demonstration compiled at version 9 answers "not stated" (the measured output, unchanged);
2. the product's own out-of-stock statement next to the legend is read (OutOfStock confirmed); a box holding ONLY the
   legend states nothing (version 9's "not stated" kept: never the legend's first class); a statement attached to the
   price wins when the rest of the box is ambiguous;
3. ``stock_legends`` on lists of classes in other languages, and lines that are no legend.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_canonical import Shop, compiled, product, run  # noqa: E402

NAME = "Eau Micellaire Purifiante 500ml Bio"
#: the saved buy box (pages[0].main_text_sep), after the name
LEGEND = ["Rechercher un magasin", "En stock", "En rupture temporaire", "au magasin", "Non vendu au magasin",
          "Changer de magasin"]
BUY = ["Skip to the end of the images gallery", "Jonzac", "Nettoie, démaquille, purifie en douceur toutes peaux.",
       "Soyez le premier à commenter ce produit", "{price} €", "En stock", "24,33 € /", "Litres",
       "Renseignez votre code postal/ville pour accéder au stock en magasin ou à la livraison à domicile en "
       "Ile-de-France :"] + LEGEND + ["Description détaillée"]


def shop_of(buy, availability="InStock"):
    return Shop([product("jonzac-eau-micel-purifia-500ml", NAME, 12.99, buy=buy, availability=availability)])


def test_prior_naturalia_box_confirms_the_structured_class_and_version_9_unchanged():
    shop = shop_of(BUY)
    t10, t9 = compiled(shop, 0, version=10, availability="En stock"), compiled(shop, 0, version=9, availability="En stock")
    assert t10["version"] == 10 and t10["fields"]["statement_rule"] == "10", t10["fields"]
    assert t9["version"] == 9 and "statement_rule" not in t9["fields"], t9["fields"]
    r10, r9 = run(shop, t10, NAME), run(shop, t9, NAME)
    assert r10["status"] == "ok" and r10["output"]["availability"] == "InStock", r10
    assert "availability" not in r10["checks"], r10["checks"]
    assert r9["status"] == "ok" and r9["output"]["availability"] == C.NOT_STATED, r9           # the measured output
    assert r9["checks"]["availability"]["structured"] == "InStock", r9["checks"]
    cls, ev = C.box_class10("\n".join(x.format(price="12,99") for x in BUY), 12.99)
    assert cls == "in_stock" and ev["legends"] == ["En stock / En rupture temporaire"], ev
    assert ev["rule"] == "the box without its legend(s)", ev


def test_own_statement_next_to_a_legend_and_a_legend_alone():
    # the product's own "Rupture de stock" next to its price, the store legend below: OutOfStock confirmed
    own_out = ["{price} €", "Rupture de stock", "24,33 € /", "Litres"] + LEGEND
    shop = shop_of(own_out, availability="OutOfStock")
    t10 = compiled(shop, 0, availability="Rupture de stock")
    r10 = run(shop, t10, NAME)
    assert r10["status"] == "ok" and r10["output"]["availability"] == "OutOfStock", r10
    # a disagreeing structured InStock is a CONFLICT naming the product's own statement, never the legend
    shop_in = shop_of(own_out, availability="InStock")
    # pinned to version 11 (version 12 answers the visible "Rupture de stock": test_demo_notnow.py)
    r = run(shop_in, compiled(shop_in, 0, version=11, availability="Rupture de stock"), NAME)
    assert r["status"] == "failed" and r["why"].startswith("conflict: availability"), r
    conf = r["conflicts"]["availability"]
    assert conf["visible"] == "out_of_stock" and conf["legends"] == ["En stock / En rupture temporaire"], conf
    # ONLY the legend: no statement - version 9's "not stated", never the legend's first class
    only = ["{price} €", "24,33 € /", "Litres"] + LEGEND
    shop2 = shop_of(only)
    r2 = run(shop2, compiled(shop2, 0, availability="En stock"), NAME)
    assert r2["status"] == "ok" and r2["output"]["availability"] == C.NOT_STATED, r2
    assert r2["checks"]["availability"]["structured"] == "InStock", r2["checks"]
    # the class attached to the price wins where the rest of the box is ambiguous (another product's statement below)
    attached = ["{price} €", "Auf Lager", "Lieferzeit 1-3 Tage",
                "Zubehör: Ersatzfilter für dieses Gerät ist leider ausverkauft und wird nicht mehr nachgeliefert"]
    box = "\n".join(x.format(price="12,99") for x in attached)
    assert C.text_availability_class(box, v8=True, v9=True) == "", box             # version 9: both classes
    cls, ev = C.box_class10(box, 12.99)
    assert cls == "in_stock" and ev["statement"] == "Auf Lager" and "price" in ev["rule"], ev


def test_legends_in_other_languages_and_lines_that_are_no_legend():
    de = ["29,90 €", "Verfügbarkeit in Filialen:", "Auf Lager", "Nicht auf Lager", "Bestellbar", "Filiale wählen"]
    got = C.stock_legends(de)
    assert [(a, b) for a, b, _c in got] == [(2, 3)] and set(got[0][2]) == {"in_stock", "out_of_stock"}, got
    en = ["In stock", "Low stock", "Sold out", "Pre-order"]
    assert C.stock_legends(en) and set(C.stock_legends(en)[0][2]) >= {"in_stock", "out_of_stock"}
    pl = ["Dostępny", "Niedostępny", "w sklepie"]
    assert C.stock_legends(pl) and C.stock_legends(pl)[0][:2] == (0, 1), C.stock_legends(pl)
    # one class, a price between two class lines, a long line: no legend
    assert C.stock_legends(["In stock", "Ready to ship"]) == []
    assert C.stock_legends(["In stock", "19,99 €", "Sold out"]) == []
    assert C.stock_legends(["In stock", "This accessory is sold out and will not be restocked in the near future"]) == []
    # version 9's reading decides whenever it states one class (the legend rule never overrides it)
    assert C.box_class10("49,00 €\nSofort lieferbar\nLieferzeit 1-2 Tage", 49.0) == ("in_stock", {})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
