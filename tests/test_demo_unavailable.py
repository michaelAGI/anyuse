"""Web tool version 8, offline (a fake shop built from saved evidence; no browser, no network, no
model): the multilingual availability normalizer knows the UNAVAILABLE phrases of the gate lexicon's 11 languages,
so a visible "unavailable" against a structured "limited / in stock" is a CONFLICT.

The wrong outputs: spirituosenworld.de, for both of its values, answered the microdata's LimitedAvailability where
the page says "Dieser Artikel steht derzeit nicht zur Verfügung!" (and further down "Momentan nicht lieferbar").
Version 6's vocabulary missed the first phrase, and the page's back-in-stock offer "Benachrichtigen Sie mich, sobald
der Artikel lieferbar ist" read as an in-stock "lieferbar": the text stated "both", so no visible class and no CONFLICT.

What must hold:

1. on the spirituosenworld page a version-8 tool answers a FAILURE with availability CONFLICT (microdata
   LimitedAvailability vs visible out of stock, both named); a version-7 tool still answers
   LimitedAvailability;
2. the unavailable phrases of EN DE NL FR ES IT SV DA NO FI PL are out of stock for version 8; a back-in-stock
   notification offer, one option's statement and a payment / shipping option's "available" are no statement;
   version 6's vocabulary is unchanged;
3. agreeing sides ("Sofort versandfertig", Swiss "an Lager" and an InStock microdata) are no conflict;
4. a page without structured availability whose text says "steht derzeit nicht zur Verfügung" compiles a TEXT
   availability source at version 8 (version 7: none) and answers OutOfStock;
5. version 8 reads the statements in the product's buy box (``demo_compile.buy_box``: after its name), never an
   accessory's "nicht verfügbar" further down the page (www.nubert.de).
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_saleprice as SP  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402

#: the saved spirituosenworld.de product page around the name, the offer and the buy box
SPIRITUOSEN = ("Spirituosen\nLikör\nCreamliköre\n{name}\nDieser Artikel steht derzeit nicht zur Verfügung!\n"
               "Benachrichtigen Sie mich, sobald der Artikel lieferbar ist.\n\n Ich habe die Datenschutzbestimmungen zur "
               "Kenntnis genommen und bin damit einverstanden.\n\n{prices}\nInhalt: 0.02 Liter (145,00 € / 1 Liter)\n\n"
               "inkl. MwSt. zzgl. Versandkosten\n\n Momentan nicht lieferbar\n\n Merken\nArtikel-Nr.: 1072\n"
               "Marke: Mozart Likör\nHerkunftsland: Österreich")
PRICE = "2,90 €"
PRICE_DOM = '<span class="price">2,90 €</span>'


def shop(buy_box=SPIRITUOSEN, availability="LimitedAvailability", md_price="2.90"):
    return SP.Shop(prices=PRICE, prices_dom=PRICE_DOM, md_price=md_price, availability=availability, buy_box=buy_box)


def compiled(s, version=None):
    return SP.compiled(s, version=version, price="2,90", availability="Momentan nicht lieferbar")


def test_prior_spirituosenworld_page_is_a_conflict_and_version_7_unchanged():
    s = shop()
    # pinned to version 11 (version 12 answers the visible "not available": test_demo_notnow.py)
    t8, t7 = compiled(s, version=11), compiled(s, version=7)
    assert t8["fields"]["availability"] == "microdata" and t8["fields"]["availability_vocab"] == "8", t8["fields"]
    assert "availability_vocab" not in t7["fields"], t7["fields"]
    for v in (SP.V1, SP.V2):
        r8 = run = SP.run(s, t8, v)
        assert r8["status"] == "failed" and set(r8["conflicts"]) == {"availability"}, run
        ev = r8["conflicts"]["availability"]
        assert ev["visible"] == "out_of_stock" and "steht derzeit nicht zur verfugung" in ev["visible_statement"], ev
        assert [(x["source"], x["class"]) for x in ev["structured"]] == [("microdata", "limited")], ev
        assert r8["fields_seen"]["availability"] == T.CONFLICT and "vs visible out_of_stock" in r8["why"], r8
        r7 = SP.run(s, t7, v)
        assert r7["status"] == "ok" and r7["output"]["availability"] == "LimitedAvailability", r7   # the wrong output


V8_OUT = [
    # EN
    "This item is temporarily unavailable.", "No longer available", "Currently not available", "Not in stock",
    # DE
    "Dieser Artikel steht derzeit nicht zur Verfügung!", "Steht leider nicht mehr zur Verfügung",
    "Momentan nicht bestellbar", "Nicht mehr lieferbar", "Vorübergehend ausverkauft", "Zurzeit nicht erhältlich",
    # NL
    "Tijdelijk uitverkocht", "Niet meer leverbaar", "Niet verkrijgbaar",
    # FR
    "Momentanément indisponible", "Article non disponible", "Ce produit n'est plus disponible", "En rupture",
    # ES
    "No disponible", "Temporalmente agotado", "Fuera de stock", "Sin existencias",
    # IT
    "Momentaneamente non disponibile", "Non più disponibile", "Non acquistabile",
    # SV
    "Tillfälligt slut", "Finns ej i lager", "Ej tillgänglig",
    # DA
    "Midlertidigt udsolgt", "Ikke tilgængelig",
    # NO
    "Midlertidig utsolgt", "Ikke tilgjengelig",
    # FI
    "Tilapäisesti loppu", "Ei saatavilla",
    # PL
    "Chwilowo niedostępny", "Brak w magazynie", "Brak towaru",
]


def test_the_unavailable_phrases_of_the_11_languages_and_what_is_no_statement():
    for phrase in V8_OUT:
        assert C.text_availability_class(f"Produkt\n{phrase}\n12,90 €", v8=True) == "out_of_stock", phrase
    new = [p for p in V8_OUT if C.text_availability_class(f"Produkt\n{p}\n12,90 €") != "out_of_stock"]
    assert "Dieser Artikel steht derzeit nicht zur Verfügung!" in new and len(new) >= 20, new   # version 6 unchanged
    # a back-in-stock notification offer is neither in nor out of stock
    offer = "Benachrichtigen Sie mich, sobald der Artikel lieferbar ist."
    assert C.text_availability_class(offer) == "in_stock" and C.text_availability_class(offer, v8=True) == ""
    for o in ("Notify me when available", "Email me when back in stock", "M'avertir lorsque l'article est disponible",
              "Avvisami quando torna disponibile", "Mail mij wanneer het product op voorraad is",
              "Powiadom o dostępności"):
        assert C.text_availability_class(o, v8=True) == "", o
    # one option's statement is not the product's; a payment / shipping option's "available" neither
    nubert = "Farbe: Schwarz\nMehrschichtlack Weiß\n(Diese Option ist zurzeit nicht verfügbar.)\n1.300,00 €\n" \
             "Sofort verfügbar, Lieferzeit: 1-3 Werktage *"
    assert C.text_availability_class(nubert) == "" and C.text_availability_class(nubert, v8=True) == "in_stock"
    pay = "ZAHLUNGSARTEN\nVorauskasse\nWeitere Zahlungsarten verfügbar\nVERSAND"
    assert C.text_availability_class(pay) == "in_stock" and C.text_availability_class(pay, v8=True) == ""
    # the statement named in a conflict is version 8's
    t = "Dieser Artikel steht derzeit nicht zur Verfügung! Benachrichtigen Sie mich, sobald der Artikel lieferbar ist."
    assert "steht derzeit nicht zur verfugung" in C.visible_statement(t, "out_of_stock", v8=True)
    assert C.visible_statement(t, "out_of_stock") == ""


def test_agreeing_sides_are_no_conflict():
    for line in ("Sofort versandfertig", "797 An Lager", "Artikel ist lagernd", "Auf Lager"):
        box = "{name}\n{prices}\n" + line + "\nIn den Warenkorb"
        s = shop(buy_box=box, availability="InStock")
        t8 = compiled(s)
        r = SP.run(s, t8, SP.V1)
        assert r["status"] == "ok" and r["output"]["availability"] == "InStock" and not r.get("conflicts"), (line, r)


def test_a_text_source_reads_the_new_phrases_at_version_8():
    box = "{name}\nDieser Artikel steht derzeit nicht zur Verfügung!\n{prices}\nMerken"

    class NoMicrodata(SP.Shop):
        def product(self, url):
            p = super().product(url)
            p["dom"] = "<html><body><main>" + p["main_text"] + "</main></body></html>"
            return p
    s = NoMicrodata(prices=PRICE, prices_dom=PRICE_DOM, buy_box=box)
    t8, t7 = compiled(s), compiled(s, version=7)
    assert t8["fields"]["availability"] == "text" and t7["fields"]["availability"] == "none", (t8["fields"], t7["fields"])
    r8, r7 = SP.run(s, t8, SP.V1), SP.run(s, t7, SP.V1)
    assert r8["status"] == "ok" and r8["output"]["availability"] == "OutOfStock", r8
    assert r7["status"] == "ok" and r7["output"]["availability"] is None, r7


def test_the_buy_box_is_read_not_the_accessories():
    # www.nubert.de (audited correct, InStock): its accessories' cable lengths say "nicht verfügbar"
    # further down the page; version 8 reads the product's buy box (after its name)
    accessories = "\n".join(f"nuCable Lautsprecherkabel\nAusführung auswählen\n{m},0 m - nicht verfügbar - {p},00 €"
                            for m, p in ((5, 124), (7, 140), (9, 160)))
    box = ("{name}\nRegulärer Preis der 1. Wahl 1445,00 €\nFarbe auswählen\n(Diese Option ist zurzeit nicht verfügbar.)\n"
           "{prices}\n\nInkl. MwSt und zzgl. 14,90 € Versandkosten\n\nSofort verfügbar, Lieferzeit: 1-3 Werktage *\n"
           "In den Warenkorb\n" + "Produktbeschreibung " * 120 + "\nZubehör\n" + accessories)
    s = shop(buy_box=box, availability="InStock")
    page = s.product(f"{SP.BASE}/p/{SP.CATALOG[0][0]}")
    assert C.text_availability_class(page["main_text"], v8=True) == ""                  # the whole text: both
    assert C.text_availability_class(C.buy_box(page["main_text"], SP.CATALOG[0][1]), v8=True) == "in_stock"
    t8 = compiled(s)
    r = SP.run(s, t8, SP.V1)
    assert r["status"] == "ok" and r["output"]["availability"] == "InStock" and not r.get("conflicts"), r
    # a page that does not name the product: the whole text, as before
    assert C.buy_box("Lieferzeit 2 Tage\nAusverkauft", "Produkt X") == "Lieferzeit 2 Tage\nAusverkauft"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
