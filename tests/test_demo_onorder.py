"""Web tool version 9, offline (a fake shop built from saved evidence; no browser, no network, no
model): the "on order / backorder" phrases of the gate lexicon's 11 languages are a visible availability class
(``backorder``) for a version-9 tool - "Wird bestellt", "auf Bestellung", "Bestellware", "on backorder", "item on
order", "sur commande", "su ordinazione", "bajo pedido", "op bestelling", "på beställning", "bestillingsvare",
"tilaustuote", "na zamówienie", ... A preposition and "order" that also occur in a shipping or discount offer ("on order
over 50 €", "sur commande de 60 €", "op bestelling vanaf 50 €") count only as the statement itself (at the start of a
line, or after an availability / product word: "Lieferbar auf Bestellung", "Leveres på bestilling").

The evidence (www.vamida.at): the compiled availability source is the page TEXT; the buy box says "Lieferung in 5-7
Werktagen" and "Wird bestellt" - no class of version 8's vocabulary, so the answer had no availability and the reader
filed it incomplete ("the page states 'Wird bestellt'"). The same page's microdata says InStock: by the conflict rule (a
visible class that differs from a structured one is a CONFLICT naming both sides, never either side silently) version 9
reports that disagreement - an honest failure, never a wrong answer.

What must hold:

1. a page that states "Wird bestellt" (text source, no disagreeing structured statement) is answered BackOrder by a
   version-9 tool; version 8 answers no availability (the measured output);
2. vamida's page (microdata InStock + "Wird bestellt") is a CONFLICT for version 9, naming both sides;
3. the phrases of the 11 languages, and the shipping-offer phrases that are no statement.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_canonical import Shop, compiled, product, run  # noqa: E402

NAME = "Vichy PURETÉ THERMALE Reinigungslotion"
#: the saved buy box (pages[1])
BUY = ["Reinigungslotion   Vichy", "Entfernt verlässlich von Unreinheiten", "€ {price}", "VK* € 19,51",
       "inkl. MwSt. zzgl. Versand", "In den Warenkorb", "Lieferung in 5-7 Werktagen", "Wird bestellt", "Kurzinformation"]


def vamida_shop(md_availability=""):
    return Shop([product("vichy-purete-thermale-reinigungslotion", NAME, 14.63, buy=BUY, availability="",
                         md_availability=md_availability)])


def test_an_on_order_statement_is_answered_and_version_8_unchanged():
    shop = vamida_shop()
    t9, t8 = compiled(shop, 0, availability="Wird bestellt"), compiled(shop, 0, version=8, availability="Wird bestellt")
    assert t9["fields"]["availability"] == "text" and t8["fields"]["availability"] == "none", (t9["fields"], t8["fields"])
    r9, r8 = run(shop, t9, NAME), run(shop, t8, NAME)
    assert r9["status"] == "ok" and r9["output"]["availability"] == "BackOrder", r9
    assert r8["status"] == "ok" and r8["output"]["availability"] is None, r8          # as measured (no availability)


def test_prior_vamida_page_is_a_conflict_for_version_9():
    shop = vamida_shop(md_availability="InStock")
    # the compiled tool: the text source (the demonstration's page had no microdata availability); pinned to version 11
    # (version 12 answers the visible "on order": test_demo_notnow.py)
    t9 = compiled(vamida_shop(), 0, version=11, availability="Wird bestellt")
    t8 = compiled(vamida_shop(), 0, version=8, availability="Wird bestellt")
    r9, r8 = run(shop, t9, NAME), run(shop, t8, NAME)
    assert r9["status"] == "failed" and r9["why"].startswith("conflict: availability"), r9
    conf = r9["conflicts"]["availability"]
    assert conf["visible"] == "backorder" and conf["structured"][0]["class"] == "in_stock", conf
    assert "wird bestellt" in conf["visible_statement"], conf
    assert r8["status"] == "ok" and r8["output"]["availability"] is None, r8


def test_the_phrases_of_the_11_languages():
    for text in ["Wird bestellt", "Lieferbar auf Bestellung", "Bestellware - Lieferzeit 2 Wochen", "Artikel im Zulauf",
                 "This item is on backorder", "Item on order", "On order", "Disponible sur commande", "Sur commande",
                 "Prodotto su ordinazione", "Disponible bajo pedido", "Op bestelling", "Leveres på bestilling",
                 "Beställningsvara", "Finns på beställning", "Bestillingsvare", "Tilaustuote", "Produkt na zamówienie",
                 "Nicht auf Lager - wird bestellt"]:
        assert C.text_availability(text, v9=True) == "BackOrder", text
        assert C.text_availability_class(text, v9=True) == "backorder", text
        assert C.text_availability(text, v8=True) != "BackOrder", text
    for text in ["Free shipping on order over 50 €", "Livraison offerte sur commande de 60 € et plus",
                 "Gratis verzending op bestelling vanaf 50 €", "Korting op bestelling boven 100 euro",
                 "Fri frakt på beställning över 499 kr", "Rabatt auf Bestellungen ab 100 €", "Auf Lager", "Bestellbar"]:
        assert C.text_availability(text, v9=True) != "BackOrder", text
    assert C.visible_statement("Lieferung in 5-7 Werktagen\nWird bestellt", "backorder", v9=True).endswith("wird bestellt")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
