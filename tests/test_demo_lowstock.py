"""Web tool version 11, fix 2 (the drift census): a few left is IN STOCK.

The census's cases (research ``snapshots/``): massivum.de - microdata InStock, the buy box "Nur noch 6 auf Lager!"
(both arms: CONFLICT, a break of an answer the last wave gave); www.feuerdepot.de - InStock and "nur noch wenige
Artikel"; www.graines-baumaux.fr - JSON-LD InStock and "Disponibilité : derniers articles en stock";
www.musik-produktiv.de - JSON-LD LimitedAvailability next to InStock and a visible "auf Lager" (all still failing,
CONFLICT). Version 8 reads "a few left" as the class "limited", a class of its own.

What must hold (fake product pages shaped on those snapshots):

1. with ``stock_rule: "11"`` (a version-11 compile) "limited" and "in stock" are no conflict - visible against
   structured, and two structured statements against each other; the answer stays the compiled structured value;
2. a real disagreement stays a CONFLICT (InStock against "nicht verfügbar"; PreOrder against "auf Lager");
3. without the key (versions 6-10) the conflict is exactly as before; the version-11 compile writes the key.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402

NAME = "Massivholz Couchtisch Alpina Wildeiche 110x60"
SRC10 = {"name": "jsonld", "price": "jsonld", "availability": "microdata", "availability_vocab": "8", "url": "canonical",
         "url_rule": "same_product", "text_rule": "9", "statement_rule": "10"}
SRC11 = dict(SRC10, stock_rule="11")


def page(box, md_av="http://schema.org/InStock", ld=("InStock",)):
    blocks = [json.dumps({"@context": "https://schema.org", "@type": "Product", "name": NAME,
                          "offers": {"@type": "Offer", "price": "549.00", "priceCurrency": "EUR",
                                     "availability": f"https://schema.org/{a}"}}) for a in ld]
    text = f"Startseite\nTische\n{NAME}\n549,00 €\ninkl. MwSt.\n{box}\nIn den Warenkorb\nBeschreibung\nMassive Wildeiche."
    return {"url": "https://www.m.test/alpina-110", "title": NAME, "h1": [NAME], "text": text, "main_text": text,
            "jsonld": blocks, "facts": W.product_facts(blocks), "canonical": "https://www.m.test/alpina-110",
            "microdata": {"name": NAME, "price": "549.00", "currency": "EUR", "availability": md_av} if md_av else {},
            "meta": {}, "struck_prices": []}


def conflicts(p, src):
    f = C.extract_fields(p, src, NAME)
    return f, C.field_conflicts(p, f, src, NAME, v8=True)


def test_a_few_left_is_in_stock_in_version_11():
    for box in ("Nur noch 6 auf Lager!", "Lieferzeit ca.: 1-3 Werktage * nur noch wenige Artikel lieferbar",
                "Disponibilité : derniers articles en stock"):
        p = page(box)
        f10, c10 = conflicts(p, SRC10)
        f11, c11 = conflicts(p, SRC11)
        assert "availability" in c10, (box, c10)
        assert "availability" not in c11, (box, c11)
        assert W.availability_word(f11["availability"]) == "InStock"
    # two structured statements: LimitedAvailability next to InStock, the box "auf Lager"
    p = page("Sofort lieferbar, auf Lager", md_av="", ld=("LimitedAvailability", "InStock"))
    src = dict(SRC11, availability="jsonld")
    assert "availability" in conflicts(p, dict(SRC10, availability="jsonld"))[1]
    assert "availability" not in conflicts(p, src)[1]


def test_a_real_disagreement_stays_a_conflict():
    assert "availability" in conflicts(page("Dieser Artikel ist derzeit nicht verfügbar"), SRC11)[1]
    assert "availability" in conflicts(page("Auf Lager, sofort lieferbar", md_av="http://schema.org/PreOrder"), SRC11)[1]


def test_the_version_11_compile_writes_the_key():
    from test_demo_canonical import Shop, compiled, product
    shop = Shop([product("alpina-110", NAME, 549.0, buy=["Nur noch 6 auf Lager!"])])
    t11, t10 = compiled(shop, 0, version=11), compiled(shop, 0, version=10)
    assert t11["fields"].get("stock_rule") == "11" and "stock_rule" not in t10["fields"], (t11["fields"], t10["fields"])


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
