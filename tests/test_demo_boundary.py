"""Web tool version 9, offline (a fake shop built from saved evidence; no browser, no network, no
model): the visible text a version-9 tool reads its statements in (availability, delivery and label lines, prices) has a
SEPARATOR at every element boundary - a line break around block / inline-block / flex boxes and for ``<br>``, a tab
around table cells (``demo_tool.TEXT_JS_V9`` in the page; ``demo_cards.sep_text_of`` on a recorded DOM) - so adjacent
elements' texts never glue; and the statement matching tolerates glued text (a lower- to upper-case boundary inside a
word is a word break; Polish "ł" and Nordic "ø" / "æ" fold to their base letters).

The evidence: www.rowertour.com - the page's buy box reads "Produkt dostępnyWysyłka w ciągu: 2-3 dni" in ``innerText``
(two elements' texts glued), so version 8 found neither the in-stock "dostępny" nor the delivery line and answered "not
stated" for the JSON-LD InStock; the reader marked it wrong (the page states availability). ``web_arch.fold`` also keeps
"ł": version 8's Polish delivery phrase "wysylka w" never matched "Wysyłka w".

What must hold:

1. on the rowertour page (glued in ``innerText``, separate elements in the DOM) a version-9 tool answers the JSON-LD
   InStock (the visible "dostępny" supports it); the same demonstration compiled as version 8 answers "not stated",
   as measured, and never evaluates the version-9 text read;
2. on the saved page record (no DOM, the glued text only) version 9's statement matching finds "dostępny";
3. the separator reading itself (blocks, inline-blocks, ``<br>``, table cells, hidden elements, inline elements that
   stay one word) and its normal form; the in-page read carries its marker;
4. a Polish delivery line "Wysyłka w ciągu: 2-3 dni" is version 9's visible statement (version 8: "not stated").
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_canonical import FakePage, Shop, _settle, compiled, product, run  # noqa: E402

NAME = "Soczewki Accent Shot"
#: the saved record's buy box (pages[1]: innerText)
GLUED = ["Kod producenta: 610-40-L154_ACC", "Kod EAN: 5902891396420", "{price} zł", "Cena katalogowa: 45,90 zł",
         "Kolor wariantu: żółty", "Ilość", "DO KOSZYKA", "Produkt dostępnyWysyłka w ciągu: 2-3 dni",
         "Zapytaj o produkt"]
#: the same buy box's elements: the availability and the delivery line are two inline-block elements
DOM = ('<p>Kod producenta: 610-40-L154_ACC</p><p>Kod EAN: 5902891396420</p><div class="price">{price} zł</div>'
       '<p>Cena katalogowa: 45,90 zł</p><p>Kolor wariantu: żółty</p><p>Ilość</p><button>DO KOSZYKA</button>'
       '<div class="availability"><span style="display: inline-block">Produkt dostępny</span>'
       '<span style="display:inline-block">Wysyłka w ciągu: 2-3 dni</span></div><a href="#q">Zapytaj o produkt</a>')


def rower_shop(buy=GLUED, dom=DOM):
    return Shop([product("soczewki-accent-shot", NAME, 32.26, buy=buy, buy_dom=dom.replace("{price}", "32,26"),
                         sku="283749", color="żółty")])


def test_prior_glued_statement_is_read_and_version_8_unchanged():
    shop = rower_shop()
    t9, t8 = compiled(shop, 0, availability="Produkt dostępny"), compiled(shop, 0, version=8,
                                                                           availability="Produkt dostępny")
    assert t9["fields"]["availability"] == "jsonld" and t9["fields"]["text_rule"] == "9", t9["fields"]
    assert "text_rule" not in t8["fields"], t8["fields"]
    r9, r8 = run(shop, t9, NAME), run(shop, t8, NAME)
    assert r9["status"] == "ok" and r9["output"]["availability"] == "InStock", r9
    assert "availability" not in r9["checks"], r9["checks"]                       # the structured class is supported
    assert r8["status"] == "ok" and r8["output"]["availability"] == C.NOT_STATED, r8   # the measured output
    # the version-9 page record keeps the separator text; version 8 never reads it
    prod9 = r9["pages"][-1]
    assert "Produkt dostępny\nWysyłka w ciągu: 2-3 dni" in prod9["main_text_sep"], prod9["main_text_sep"]
    assert "main_text_sep" not in r8["pages"][-1], r8["pages"][-1].keys()
    seen = []

    class Spy(FakePage):
        async def js(self, expr, read_only=False):
            seen.append(str(expr)[:20])
            return await super().js(expr, read_only)
    import asyncio
    asyncio.run(T.run_tool(Spy(shop), t8, NAME, _settle, result_wait_s=0.3))
    assert not any("anyuse:text9" in s for s in seen), seen


def test_prior_saved_record_without_dom():
    # pages[1] of the saved record: the glued innerText only, no DOM, no separator text
    rec = {"url": "https://www.rowertour.com/p/g8866/soczewki-accent-shot?variant=283749", "h1": [NAME],
           "title": "Soczewki Accent Shot - Rowertour.com",
           "facts": [{"type": "Product", "name": NAME, "sku": "283749", "gtin": "5902891396420", "color": "żółty",
                      "price": 32.26, "currency": "PLN", "availability": "InStock", "offers": [], "variants": []}],
           "microdata": {}, "meta": {},
           "main_text": "Soczewki Accent Shot\nOpinie: 0\nSoczewki Accent Shot\n" + "\n".join(GLUED).replace(
               "{price}", "32,26")}
    fields = {"name": NAME, "price": 32.26, "currency": "PLN", "availability": "InStock", "url": rec["url"]}
    s8 = {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "availability_vocab": "8", "url": "canonical"}
    av8, ev8 = C.availability_answer(rec, fields, s8, NAME)
    assert av8 == C.NOT_STATED and ev8["statement_kind"] == "none", (av8, ev8)       # the measured output
    av9, ev9 = C.availability_answer(rec, fields, dict(s8, text_rule="9"), NAME)
    assert av9 == "InStock" and ev9 == {}, (av9, ev9)
    assert C.deglue("Produkt dostępnyWysyłka") == "Produkt dostępny Wysyłka"
    assert C.deglue("Trenażer SARISMag+") == "Trenażer SARIS Mag+" and C.deglue("ab 40€") == "ab 40€"


def test_the_separator_reading():
    dom = ("<html><head><title>x</title><style>p{}</style></head><body><header>Kopf</header><main>"
           "<h1>Name <b>W</b>ort</h1><div><span style='display:inline-block'>A</span><span style='display:flex'>B"
           "</span></div>Zeile 1<br>Zeile 2<table><tr><td>Preis</td><td>9,99 €</td></tr></table>"
           "<div hidden>versteckt</div><div style='display:none'>weg</div><p style='visibility:hidden'>unsichtbar</p>"
           "<script>var x = 1;</script><span>in</span><span>line</span></main><footer>Fuß</footer></body></html>")
    got = K.sep_text_of(dom)
    assert got["main_text"] == "Name Wort\nA\nB\nZeile 1\nZeile 2\nPreis\t9,99 €\ninline", got["main_text"]
    assert got["text"] == "Kopf\n" + got["main_text"] + "\nFuß", got["text"]
    assert K.sep_join(["a ", "\t", " b", "\n", "\n\t", "c"]) == "a\tb\nc"
    assert T.TEXT_JS_V9.lstrip().startswith("/*anyuse:text9*/")
    assert K.recorded_answer(T.TEXT_JS_V9, {"dom": dom}) == got
    # a separator read that holds less than SEP_COVER of the innerText's letters (cut short, failed) is not read
    page = {"main_text": "Soczewki Accent Shot\nProdukt dostępnyWysyłka w ciągu: 2-3 dni", "main_text_sep": "Soczewki"}
    assert C._vis_text(page) == "Soczewki Accent Shot\nProdukt dostępny Wysyłka w ciągu: 2-3 dni", C._vis_text(page)
    page["main_text_sep"] = "Soczewki Accent Shot\nProdukt dostępny\nWysyłka w ciągu: 2-3 dni"
    assert C._vis_text(page) == page["main_text_sep"]
    # ... nor one holding text the innerText does not show (a recorded DOM read without its style sheets: a hidden
    # cookie dialog); a kaminofen-shop.de page: 5,709 letters in the DOM's text, 1,178 in its innerText
    page["main_text_sep"] = "Cookie-Einstellungen\nWir nutzen Cookies und vergleichbare Technologien\n" + page["main_text_sep"]
    assert C._vis_text(page) == "Soczewki Accent Shot\nProdukt dostępny Wysyłka w ciągu: 2-3 dni", C._vis_text(page)
    assert "checkVisibility" in T.TEXT_JS_V9


def test_a_polish_delivery_line_is_the_visible_statement():
    buy = ["{price} zł", "Wysyłka w ciągu: 2-3 dni", "Zapytaj o produkt"]
    shop = rower_shop(buy=buy, dom="".join(f"<p>{b}</p>" for b in buy))
    t9, t8 = compiled(shop, 0, availability="2-3 dni"), compiled(shop, 0, version=8, availability="2-3 dni")
    r9, r8 = run(shop, t9, NAME), run(shop, t8, NAME)
    assert r9["status"] == "ok" and r9["output"]["availability"] == "Wysyłka w ciągu: 2-3 dni", r9
    assert r9["checks"]["availability"]["statement_kind"] == "delivery", r9["checks"]
    assert r8["status"] == "ok" and r8["output"]["availability"] == C.NOT_STATED, r8


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
