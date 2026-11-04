"""Version 13, fix 1: the product's OWN availability (``demo_compile.own_box13`` / ``word13``),
offline, on fakes built from web-heal-live's two false accepts (no browser, no network, no model).

What must hold:

1. www.kamdi24.de's voucher page: the buy box holds the voucher's "Lieferzeit: 1 bis 3 Werktage" and, in a carousel of
   other products, "Lieferzeit: Derzeit nicht verfügbar" over "22,52 €". Version 12 answered OutOfStock (the reader:
   wrong); version 13 drops the other product's not-now line - the voucher's delivery time is the answer, no CONFLICT;
2. www.musik-produktiv.de: the JSON-LD names the product twice, LimitedAvailability and InStock; the buy box says
   "Online sofort lieferbar". Version 12 answered LimitedAvailability (the reader: wrong); version 13 answers InStock;
3. the product's OWN not-now statement next to its own price, or next to a unit price, stays the answer (OutOfStock);
   a single structured word is never replaced; versions 11 / 12 read exactly as before.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402

SRC = {"name": "jsonld", "price": "jsonld", "availability": "microdata", "text_rule": "9", "statement_rule": "10",
       "url_rule": "same_product", "number_rule": "11"}


def src(rule, **kw):
    return dict(SRC, stock_rule=rule, **kw)


VOUCHER = "kamdi24 Geschenkgutschein Feuer 25 €"
KAMDI_TEXT = "\n".join(["Mein Konto", "Warenkorb", "Geschenkgutscheine", "KAMDI24 GESCHENKGUTSCHEIN FEUER 25 €",
                        "Art.-Nr. GS001", "Motiv", "Feuer", "Wert:", "25", "25,00 €", "inkl. MwSt., zzgl. Versandkosten*",
                        "IN DEN WARENKORB", "Lieferzeit: 1 bis 3 Werktage", "(Paket)", "BESCHREIBUNG",
                        "Das könnte dir auch gefallen", "Grillschürze", "Lieferzeit: 1 bis 3 Werktage", "39,90 €",
                        "Anzündholz", "Lieferzeit: 3 bis 4 Wochen", "12,90 €", "Kaminholz Buche, Paket für morgen (klein)",
                        "Lieferzeit: Derzeit nicht verfügbar", "22,52 €", "Grillhandschuhe", "Lieferzeit: 1 bis 3 Werktage",
                        "19,99 €"])
KAMDI = {"url": "https://www.kamdi24.de/kamdi24-geschenkgutschein-feuer-25", "title": VOUCHER, "h1": [VOUCHER],
         "text": KAMDI_TEXT, "main_text": KAMDI_TEXT, "main_text_sep": KAMDI_TEXT, "facts": [],
         "microdata": {"name": VOUCHER, "price": "25.00", "availability": "https://schema.org/InStock"}}
KAMDI_FIELDS = {"name": VOUCHER, "price": 25.0, "currency": "EUR", "availability": "InStock"}

AMP = "Vox VT20X Classic Blue"
MUSIK_TEXT = "\n".join(["Musik-Produktiv", "Gitarren", "E-Gitarren-Combo", AMP, AMP, "Bildergalerie überspringen",
                        "239,00 €", "inkl. MwSt.", "versandkostenfrei", "Online sofort lieferbar", "In den Warenkorb",
                        "Filialverfügbarkeit prüfen", "Beratungstermin anfragen", "Teilen"])
_P = {"type": "Product", "name": AMP, "brand": "Vox", "price": 239.0, "prices": [239.0], "currency": "EUR"}
MUSIK = {"url": "https://www.musik-produktiv.de/products/vox-vt20x-classic-blue/", "title": AMP, "h1": [AMP],
         "text": MUSIK_TEXT, "main_text": MUSIK_TEXT, "main_text_sep": MUSIK_TEXT, "microdata": {},
         "facts": [dict(_P, availability="LimitedAvailability",
                        offers=[{"price": 239.0, "currency": "EUR", "availability": "LimitedAvailability"}]),
                   dict(_P, availability="InStock",
                        offers=[{"price": 239.0, "currency": "EUR", "availability": "InStock"}])]}
MUSIK_FIELDS = {"name": AMP, "price": 239.0, "currency": "EUR", "availability": "LimitedAvailability"}


def answer(page, fields, rule, value, **kw):
    s = src(rule, **kw)
    return C.availability_answer(page, fields, s, value)[0], sorted(C.field_conflicts(page, fields, s, value, v8=True))


def test_another_products_not_now_line_is_not_the_products():
    assert answer(KAMDI, KAMDI_FIELDS, "12", VOUCHER) == ("OutOfStock", [])          # version 12: the false accept
    av, conf = answer(KAMDI, KAMDI_FIELDS, "13", VOUCHER)
    assert av == "Lieferzeit: 1 bis 3 Werktage" and conf == [], (av, conf)
    box, gone = C.own_box13(C.buy_box(KAMDI_TEXT, VOUCHER), 25.0)
    assert gone == ["Lieferzeit: Derzeit nicht verfügbar"] and "Lieferzeit: 1 bis 3 Werktage" in box


def test_the_buy_box_picks_between_two_structured_words():
    assert answer(MUSIK, MUSIK_FIELDS, "12", AMP, availability="jsonld") == ("LimitedAvailability", [])
    av, conf = answer(MUSIK, MUSIK_FIELDS, "13", AMP, availability="jsonld")
    assert av == "InStock" and conf == [], (av, conf)
    got = C.word13(MUSIK, MUSIK_FIELDS, src("13", availability="jsonld"), AMP)
    assert got["structured"] == ["LimitedAvailability", "InStock"] and got["visible_class"] == "in_stock"


def test_the_products_own_statements_stay():
    # its own not-now line next to its own price
    own = KAMDI_TEXT.replace("Lieferzeit: 1 bis 3 Werktage\n(Paket)", "Lieferzeit: Derzeit nicht verfügbar\n(Paket)", 1)
    page = dict(KAMDI, text=own, main_text=own, main_text_sep=own)
    for rule in ("12", "13"):
        assert answer(page, KAMDI_FIELDS, rule, VOUCHER)[0] == "OutOfStock"
    # next to a unit price ("2,50 €/kg" is no bare price line) and the product's price two lines up
    unit = "\n".join([VOUCHER, "25,00 €", "(2,50 € / 1 kg)", "Derzeit nicht verfügbar", "In den Warenkorb"])
    page = dict(KAMDI, text=unit, main_text=unit, main_text_sep=unit)
    assert C.own_box13(C.buy_box(unit, VOUCHER), 25.0)[1] == []
    assert answer(page, KAMDI_FIELDS, "13", VOUCHER)[0] == "OutOfStock"
    # one structured word: never replaced (LimitedAvailability alone stays the structured answer)
    one = dict(MUSIK, facts=MUSIK["facts"][:1])
    assert C.word13(one, MUSIK_FIELDS, src("13", availability="jsonld"), AMP) == {}
    # versions 11 / 12 never run the version-13 rules
    for rule in ("11", "12"):
        assert answer(MUSIK, MUSIK_FIELDS, rule, AMP, availability="jsonld")[0] == "LimitedAvailability"


def test_compile_sets_the_rule_at_version_13_only():
    from test_repair_ladder import Shop, trace
    tr = trace(Shop(), "Bosch GSR 12V Akkuschrauber")
    t12, t13 = (C.compile_trace(tr, version=v)["tool"] for v in (12, 13))
    assert t12["fields"].get("stock_rule") == "12" and t13["fields"].get("stock_rule") == "13"
    # version 13 still builds 13; newer versions keep its rule
    assert t13["version"] == 13 and C.TOOL_VERSION >= 14 and C.v13(t13) and not C.v13(t12)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
