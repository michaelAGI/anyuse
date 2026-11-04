"""Web tool version 8, offline (a fake shop built from saved evidence; no browser, no network, no
model): a STRUCTURED availability the visible page does not support is never answered - the answer is the page's
visible statement (a delivery time, an availability-labelled line) or "not stated".

The wrong outputs:

* massivum.de answered the microdata's LimitedAvailability; the page shows only "Lieferzeit: 13 Wochen*";
* www.mhm-modellbau.de answered the JSON-LD's OutOfStock; the page's availability line says "Keine Angaben vom
  Hersteller" (no information from the manufacturer), with a cart button.

Neither page contradicted its structured value in words the conflict check knows, so the structured class was
answered although the page does not support it.

The rule (``demo_compile.availability_answer``; version 8 only): a structured availability is answered only when the
visible page states an availability class (version 8's vocabulary; a different class is a CONFLICT, checked before);
otherwise the answer is the buy box's visible statement - the first delivery-time or availability-labelled line
after the product's name - else "not stated" (no statement, or "no information"). The structured value is kept as
evidence (``checks.availability``), never as the answer.

What must hold:

1. massivum's page: version 8 answers "Lieferzeit: 13 Wochen" (version 7: LimitedAvailability, the measured output);
2. mhm's page: version 8 answers "not stated" (version 7: OutOfStock);
3. an availability-labelled line with its value on the next line is the statement ("Verfügbarkeit Onlineshop:
   Bestellbar"); delivery times in the 11 languages are statements;
4. a supported class is answered as before (the visible "Auf Lager" and an InStock microdata), a contradicting one
   is still a CONFLICT (never a statement), and a page with no statement at all is "not stated".
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_saleprice as SP  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402

#: the saved massivum.de product page's buy box (its microdata: LimitedAvailability)
MASSIVUM = ("{name}\nKräftige Bettbeine für stabilen, sicheren Stand\n\n{prices}\n\nPreise inkl. MwSt. inkl. Versandkosten\n\n"
            "Produkt-Nr.:\nA2777\nLieferzeit:\n\n13 Wochen*\n\nProdukt Anzahl: Gib den gewünschten Wert ein oder benutze "
            "die Schaltflächen um die Anzahl zu erhöhen oder zu reduzieren.\nIN DEN WARENKORB\nZur Wunschliste hinzufügen")
#: the saved www.mhm-modellbau.de product page's buy box (its JSON-LD: OutOfStock)
MHM = ("{name}\n{prices}\n1\n2\n3\n4\n5\n   ×  \n   IN DEN WARENKORB\nDirekt zu \n\n   Keine Angaben "
       "vom Hersteller\n\nMarke\tWilesco\nHersteller-Nr.\t10700\nAngaben zur Produktsicherheit")
#: the saved www.menzels-lokschuppen.de page (an audited-correct BackOrder): its availability-labelled lines
MENZELS = ("{name}\nBildergalerie überspringen\n\n{prices}\n\nPreise inkl. MwSt. zzgl. Versandkosten\n\nIn den Warenkorb\n \n"
           "Verfügbarkeit Onlineshop\nBestellbar\nVerfügbarkeit Ladengeschäft\nBestellbar\nBeschreibung  Modular Parts.")
PRICE = "749,00 €"
PRICE_DOM = '<span class="price">749,00 €</span>'


def shop(buy_box, availability, ld=False):
    return SP.Shop(prices=PRICE, prices_dom=PRICE_DOM, md_price=None if ld else "749.00",
                   ld_price=749.0 if ld else None, availability=availability, buy_box=buy_box)


def tools(s):
    kw = dict(price="749,00", availability="Lieferzeit: 1-3 Werktage")
    return SP.compiled(s, **kw), SP.compiled(s, version=7, **kw)


def test_prior_massivum_page_answers_the_visible_delivery_time():
    s = shop(MASSIVUM, "LimitedAvailability")
    t8, t7 = tools(s)
    assert t8["fields"]["availability"] == "microdata", t8["fields"]
    r8, r7 = SP.run(s, t8, SP.V2), SP.run(s, t7, SP.V2)
    assert r8["status"] == "ok" and r8["output"]["availability"] == "Lieferzeit: 13 Wochen", r8
    ev = r8["checks"]["availability"]
    assert ev["structured"] == "LimitedAvailability" and ev["statement_kind"] == "delivery", ev
    assert r8["output"]["price"] == 749.0 and not r8.get("conflicts"), r8
    assert r7["status"] == "ok" and r7["output"]["availability"] == "LimitedAvailability", r7   # the measured output
    assert "availability" not in r7["checks"], r7["checks"]


def test_prior_mhm_page_answers_not_stated():
    s = shop(MHM, "OutOfStock", ld=True)
    t8, t7 = tools(s)
    assert t8["fields"]["availability"] == "jsonld", t8["fields"]
    r8, r7 = SP.run(s, t8, SP.V1), SP.run(s, t7, SP.V1)
    assert r8["status"] == "ok" and r8["output"]["availability"] == C.NOT_STATED == "not stated", r8
    ev = r8["checks"]["availability"]
    assert ev["structured"] == "OutOfStock" and ev["statement_kind"] == "no_information", ev
    assert "Keine Angaben vom Hersteller" in ev["visible_statement"], ev
    assert r7["status"] == "ok" and r7["output"]["availability"] == "OutOfStock", r7            # the measured output


def test_labelled_lines_and_delivery_times_in_the_11_languages():
    s = shop(MENZELS, "BackOrder")
    t8, _t7 = tools(s)
    r8 = SP.run(s, t8, SP.V1)
    assert r8["status"] == "ok" and r8["output"]["availability"] == "Verfügbarkeit Onlineshop: Bestellbar", r8
    for line, want in [("Lieferzeit ca. 4 Werktage", "Lieferzeit ca. 4 Werktage"),
                       ("Delivery time: 2-4 working days", "Delivery time: 2-4 working days"),
                       ("Délai de livraison : 3 à 5 jours", "Délai de livraison : 3 à 5 jours"),
                       ("Levertijd:\n1-2 werkdagen", "Levertijd: 1-2 werkdagen"),
                       ("Plazo de entrega: 24/48h", "Plazo de entrega: 24/48h"),
                       ("Tempi di consegna: 3-5 giorni", "Tempi di consegna: 3-5 giorni"),
                       ("Leveranstid: 3-5 dagar", "Leveranstid: 3-5 dagar"),
                       ("Leveringstid: 2-4 hverdage", "Leveringstid: 2-4 hverdage"),
                       ("Toimitusaika 2-4 päivää", "Toimitusaika 2-4 päivää"),
                       ("Czas realizacji: 24h", "Czas realizacji: 24h"),
                       ("Availability:\nAvailable on order", "Availability: Available on order")]:
        got = C.availability_statement(f"Produkt X\n12,90 €\n{line}\nIn den Warenkorb", "Produkt X")
        assert got["statement"] == want and got["kind"] in ("delivery", "labelled"), (line, got)
    for line in ("Keine Angaben", "No information", "Geen informatie", "Brak informacji"):
        assert C.availability_statement(f"Produkt X\n{line}", "Produkt X")["kind"] == "no_information", line
    # the buy box after the product's name: a shop-wide banner before it is not the product's statement
    got = C.availability_statement("Lieferzeit 1-2 Tage auf alles!\nProdukt X\n12,90 €\nIn den Warenkorb", "Produkt X")
    assert got == {"kind": "", "statement": ""}, got


def test_a_supported_class_a_contradiction_and_no_statement():
    supported = "{name}\n{prices}\nAuf Lager\nLieferzeit: 1-3 Werktage\nIn den Warenkorb"
    s = shop(supported, "InStock")
    t8, _ = tools(s)
    r = SP.run(s, t8, SP.V1)
    assert r["status"] == "ok" and r["output"]["availability"] == "InStock" and "availability" not in r["checks"], r
    contra = "{name}\n{prices}\nAusverkauft\nLieferzeit: 13 Wochen\nIn den Warenkorb"
    s = shop(contra, "InStock")
    r = SP.run(s, t8, SP.V1)
    assert r["status"] == "failed" and set(r["conflicts"]) == {"availability"}, r
    silent = "{name}\n{prices}\nIn den Warenkorb"
    s = shop(silent, "InStock")
    r = SP.run(s, t8, SP.V1)
    assert r["status"] == "ok" and r["output"]["availability"] == "not stated", r
    assert r["checks"]["availability"]["statement_kind"] == "none", r["checks"]
    # the rule is the structured source's only: a version-8 tool whose availability source is the text is as before
    page = {"text": "Produkt X\nLieferzeit: 13 Wochen", "facts": [], "microdata": {}}
    f = {"name": "Produkt X", "availability": None}
    assert C.availability_answer(page, f, {"availability": "text"}) == (None, {})
    assert C.availability_answer(page, dict(f, availability="InStock"), {"availability": "none"}) == ("InStock", {})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
