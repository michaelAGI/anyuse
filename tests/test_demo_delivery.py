"""Web tool version 10, offline (a fake shop built from saved evidence; no browser, no network, no
model): more DELIVERY wording - the delivery-time statements of the gate lexicon's 11 languages in their verb forms
("wysyłany jest w ciągu", "wird innerhalb ... versandt", "is shipped within", "est expédié sous", "se envía en",
"viene spedito entro", "wordt binnen ... verzonden", "skickas inom", "sendes inden for", "sendes innen",
"lähetetään ...") are a visible delivery statement (version 8's availability rule answers it where the page states no
availability class). A shipping OFFER ("Darmowa wysyłka od 199 zł", "Versand ab 50 € kostenlos") is never one.

The evidence (3trolle.pl): JSON-LD PreOrder; the buy box "27,29 zł 29,99 zł / -9% / Ten produkt wysyłany jest w ciągu
2-3 dni od złożenia zamówienia. / Dodaj do koszyka"; version 8's rule (kept by version 9) found no class and no
statement ("wysyłany jest w ciągu" is not in its vocabulary) and answered "not stated" - the reader: incomplete.

What must hold:

1. 3trolle's buy box: a version-10 tool answers the visible statement "Ten produkt wysyłany jest w ciągu 2-3 dni od
   złożenia zamówienia." (the structured PreOrder kept as evidence); version 9 answers "not stated" (the measured output);
2. the verb forms of the 11 languages are delivery statements; shipping offers are not; version 8's own vocabulary
   is unchanged.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_canonical import Shop, compiled, product, run  # noqa: E402

NAME = "My Little Pony: Frendship Eternal Cards - Star Edition - Booster"
LINE = "Ten produkt wysyłany jest w ciągu 2-3 dni od złożenia zamówienia."
#: the saved buy box (pages[1].main_text_sep), after the description
BUY = ["{price} zł", "-9%", LINE, "Dodaj do koszyka", "Dodaj do listy życzeń (", "0", ")",
       "Dostawa (Odbiór w punkcie DPD Pickup) już od 9,99 zł"]


def shop_of(buy):
    return Shop([product("38245-my-little-pony-frendship-eternal-cards-star-edition-booster", NAME, 27.29, buy=buy,
                         availability="PreOrder")])


def test_prior_3trolle_statement_is_answered_and_version_9_unchanged():
    shop = shop_of(BUY)
    t10, t9 = compiled(shop, 0, availability=LINE), compiled(shop, 0, version=9, availability=LINE)
    r10, r9 = run(shop, t10, NAME), run(shop, t9, NAME)
    assert r10["status"] == "ok" and r10["output"]["availability"] == LINE, r10
    ev = r10["checks"]["availability"]
    assert ev["structured"] == "PreOrder" and ev["statement_kind"] == "delivery", ev
    assert r9["status"] == "ok" and r9["output"]["availability"] == C.NOT_STATED, r9           # the measured output
    # a shipping OFFER in its place is no statement: "not stated", as version 9
    offer = ["{price} zł", "-9%", "Darmowa wysyłka od 199 zł", "Dodaj do koszyka"]
    shop2 = shop_of(offer)
    r = run(shop2, compiled(shop2, 0, availability="Przedsprzedaż"), NAME)
    assert r["status"] == "ok" and r["output"]["availability"] == C.NOT_STATED, r


def test_the_verb_forms_of_the_11_languages_and_offers():
    yes = ["Ten produkt wysyłany jest w ciągu 2-3 dni", "Wysyłamy w ciągu 24 godzin", "Realizacja zamówienia w ciągu 3 dni",
           "Wird innerhalb von 24 Stunden versandt", "Versand erfolgt innerhalb von 2 Tagen", "Usually dispatched within 2 days",
           "Will be shipped within 3-5 business days", "Est expédié sous 48h", "Sera livré en 3 jours",
           "Se envía en 24/48 horas", "Viene spedito entro 3 giorni", "Wordt binnen 2 werkdagen verzonden",
           "Wordt verzonden binnen 24 uur", "Skickas inom 1-3 dagar", "Sendes inden for 2 hverdage", "Sendes innen 2 dager",
           "Lähetetään 1-3 arkipäivässä"]
    no = ["Darmowa wysyłka od 199 zł", "Versand ab 50 € kostenlos", "Free shipping on orders over 50",
          "Livraison offerte dès 60 €", "Envío gratis a partir de 30 €", "Spedizione gratuita da 49 €",
          "Gratis verzending vanaf 50 €", "Fri frakt över 499 kr", "Dodaj do koszyka"]
    for s in yes:
        st = C.availability_statement("Produkt\n" + s, "Produkt", v9=True, v10=True)
        assert st["kind"] == "delivery" and st["statement"] == s, (s, st)
    for s in no:
        assert C.availability_statement("Produkt\n" + s, "Produkt", v9=True, v10=True)["kind"] == "", s
    # version 8's vocabulary is unchanged: the verb forms are version 10's only
    assert C.availability_statement("Produkt\n" + yes[0], "Produkt", v9=True)["kind"] == ""
    assert C.availability_statement("Produkt\nLieferzeit: 2-3 Tage", "Produkt", v9=True)["kind"] == "delivery"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
