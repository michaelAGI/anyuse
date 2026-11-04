"""Web tool version 12: the buy box's visible NOT-NOW statement is the answer.

The cases (saved snapshots): spirituosenworld.de - microdata LimitedAvailability, the buy box "Dieser Artikel steht
derzeit nicht zur Verfügung!" (answered as the structured LimitedAvailability: judged WRONG; or a CONFLICT, a break);
www.vamida.at - microdata InStock, "Lieferung in 5-7 Werktagen / Wird bestellt" (answered InStock: judged INCOMPLETE;
or a CONFLICT). Every version before 12 read the visible class and either answered the structured one (version 7) or
failed with a CONFLICT (8-11).

What must hold (fake product pages shaped on those snapshots):

1. with ``stock_rule: "12"`` (a version-12 compile) a structured in-stock / few-left class against the buy box's
   out-of-stock / on-order / pre-order class is no CONFLICT; the answer is the visible class's schema word
   (``OutOfStock``, ``BackOrder``, ``PreOrder``), the structured value kept as evidence;
2. never the other way round: a visible "auf Lager" against a structured OutOfStock / PreOrder stays a CONFLICT; two
   structured statements that disagree stay a CONFLICT;
3. a version-11 tool (``stock_rule: "11"``) is exactly as before (the CONFLICT); the version-12 compile writes the key.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402

NAME = "Becherovka Kräuterlikör Mini 0,05 L"
SRC11 = {"name": "jsonld", "price": "jsonld", "availability": "microdata", "availability_vocab": "8", "url": "canonical",
         "url_rule": "same_product", "text_rule": "9", "statement_rule": "10", "stock_rule": "11", "number_rule": "11"}
SRC12 = dict(SRC11, stock_rule="12")


def page(box, md_av="http://schema.org/LimitedAvailability", ld=("LimitedAvailability",)):
    blocks = [json.dumps({"@context": "https://schema.org", "@type": "Product", "name": NAME,
                          "offers": {"@type": "Offer", "price": "2.60", "priceCurrency": "EUR",
                                     "availability": f"https://schema.org/{a}"}}) for a in ld]
    text = f"Startseite\nLikör\n{NAME}\n2,60 €\ninkl. MwSt.\n{box}\nIn den Warenkorb\nBeschreibung\nKräuterlikör."
    return {"url": "https://www.s.test/becherovka-mini", "title": NAME, "h1": [NAME], "text": text, "main_text": text,
            "jsonld": blocks, "facts": W.product_facts(blocks), "canonical": "https://www.s.test/becherovka-mini",
            "microdata": {"name": NAME, "price": "2.60", "currency": "EUR", "availability": md_av} if md_av else {},
            "meta": {}, "struck_prices": []}


def answer(p, src):
    f = C.extract_fields(p, src, NAME)
    conf = C.field_conflicts(p, f, src, NAME, v8=True)
    av, ev = C.availability_answer(p, f, src, NAME)
    return conf, av, ev


def test_the_visible_not_now_statement_is_the_answer():
    for box, md, word in (("Dieser Artikel steht derzeit nicht zur Verfügung!", "LimitedAvailability", "OutOfStock"),
                          ("Lieferung in 5-7 Werktagen\nWird bestellt", "InStock", "BackOrder"),
                          ("Ausverkauft", "InStock", "OutOfStock")):
        p = page(box, md_av=f"http://schema.org/{md}", ld=(md,))
        c11, av11, _ = answer(p, SRC11)
        conf, av, ev = answer(p, SRC12)
        assert "availability" in c11, (box, c11)
        assert "availability" not in conf and av == word, (box, conf, av, ev)
        assert ev["structured"] == md and ev["answered"] == word and ev["visible_class"] in C.NOT_NOW12


def test_never_the_other_way_round():
    for box, md in (("Auf Lager, sofort lieferbar", "OutOfStock"), ("Auf Lager, sofort lieferbar", "PreOrder")):
        conf, _av, _ev = answer(page(box, md_av=f"http://schema.org/{md}", ld=(md,)), SRC12)
        assert "availability" in conf, (box, md)
    # two structured statements that disagree (InStock / OutOfStock) stay a conflict whatever the box says
    conf, _av, _ev = answer(page("Ausverkauft", md_av="http://schema.org/InStock", ld=("OutOfStock",)), SRC12)
    assert "availability" in conf
    # an in-stock box: the structured class, as before
    conf, av, ev = answer(page("Auf Lager", md_av="http://schema.org/InStock", ld=("InStock",)), SRC12)
    assert "availability" not in conf and W.availability_word(av) == "InStock" and ev == {}


def test_the_version_12_compile_writes_the_key():
    from test_demo_canonical import Shop, compiled, product
    shop = Shop([product("becherovka-mini", NAME, 2.6, buy=["Auf Lager"])])
    t12, t11 = compiled(shop, 0, version=12), compiled(shop, 0, version=11)
    assert t12["fields"].get("stock_rule") == "12" and t11["fields"].get("stock_rule") == "11"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
