"""Offline A/B bench: the verifier, the breadth tools, the web compile and the answer resolver read the site's own
evidence before their word lists.

Two arms run the same corpus, each in its own process that imports ``anyuse`` from its own checkout: ``old``
(``--old-root``, the code before the change) and ``new`` (``--new-root``, default: the checkout holding this file).
Each case is one judgement on an offline page, of one of three kinds:

* ``beyond``: a page in a language or markup the hand-written lists do not cover (Dutch, Portuguese, Polish,
  Turkish, Czech, Swedish, Vietnamese, Thai, Indonesian; schema.org markup; a currency outside the tables; a class
  or parameter name in no list), with the right decision;
* ``lists``: a case the old lists decided right, which must stay right;
* ``decoy``: a page that must stay refused or unread (a word inside a longer word, a model number beside the
  query, another currency's code, a link to the site's own search).

Sections:

A. availability on a product page (``attribute_verify.verify_product``): ``itemprop=availability`` words, the buy
   control's state with the structured data, the phrase lists' own cases;
B. prices and currencies: Unicode currency symbols, ISO 4217 codes, the page's own mark beside its structured
   price, the tables' own cases, codes of another currency, abbreviations inside words;
C. the site's own search endpoints (``verify_search_links``): HTML search controls and value-carrying keys;
D. a stated result count beside the quoted echo of the input (``verify_order`` on a lone result);
E. an empty search (``verify_empty``): the site's empty page learned at the proof, a query-bound statement, a
   statement only the check model reads (faked) on a tool call, the verifier's own phrases;
F. breadth tools (``demo_breadth``): a sort compiled and run on a shop whose sort labels are in another language,
   in-stock filters by whole words, card dates from markup, the stated total beside the echo, the next page by
   the number its URL increments;
G. the web compile (``harness_compile``): which tool a task compiles to (a faked check model, then whole words),
   the site's furniture by its own pages;
H. answer references (``answer_refs``): a label by the page's own repetition before the lexicon;
I. the no-model promise: with a verifier model configured (faked, counting its questions), a watch run and a proof
   replay make 0 calls, a tool call still asks.

Usage::

    python tests/bench/generalize_verify_bench.py --old-root /path/to/old/checkout [--out <dir>] [--json results.json]

No network: every page is built in memory. No real model: the configured models are fakes. Not part of the test
suite and not shipped.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Any, Dict, List

CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROWS: List[Dict[str, Any]] = []


def case(section: str, name: str, kind: str, want: Any, fn) -> None:
    """Run one judgement ``fn() -> got`` and book it with its wanted outcome."""
    try:
        got = fn()
    except Exception as exc:                           # noqa: BLE001 - a crash is a wrong decision, booked
        got = f"error: {type(exc).__name__}: {exc}"[:200]
    ROWS.append({"section": section, "case": name, "kind": kind, "want": want, "got": got, "ok": got == want})


def call(fn, *args, **kw):
    """``fn(*args, **kw)``; an older signature without the keyword arguments gets the positional ones only."""
    try:
        return fn(*args, **kw)
    except TypeError:
        return fn(*args)


# ---------------------------------------------------------------------------
# A. availability
# ---------------------------------------------------------------------------

NAME = "Widget X 200"
URL = "https://shop.test/p/widget-x-200"


def product_page(AV, lines, price="", currency="", availability="", extra_dom="", sku=""):
    offer = {}
    if price:
        offer["price"] = price
    if currency:
        offer["priceCurrency"] = currency
    if availability:
        offer["availability"] = "https://schema.org/" + availability
    doc = {"@context": "https://schema.org", "@type": "Product", "name": NAME, "offers": offer}
    if sku:
        doc["sku"] = sku
    ld = '<script type="application/ld+json">%s</script>' % json.dumps(doc) if offer else ""
    dom = f"<html><head><title>{NAME}</title>{ld}</head><body><h1>{NAME}</h1>{extra_dom}</body></html>"
    return AV.Page(dom, "\n".join([NAME] + list(lines)), URL)


def avail_status(AV, page, av, others=()):
    ans = {"name": NAME, "price": 49.9, "currency": "EUR", "availability": av, "url": URL}
    v = call(AV.verify_product, ans, page, ("availability",), others=others)
    d = next(x for x in v.decisions if x.attr == "availability")
    return "accept" if d.status == AV.VERIFIED else "refuse"


def marked(words, cls):
    return f'<div class="stock"><span itemprop="availability" content="https://schema.org/{cls}">{words}</span></div>'


def buy_form(disabled=False, sku="WX-200"):
    return (f'<form method="post" action="/cart/add"><input type="hidden" name="product" value="{sku}">'
            f'<button type="submit"{" disabled" if disabled else ""}>…</button></form>')


def shop_page(AV, words, cls_markup, structured, form=""):
    extra = (marked(words, cls_markup) if cls_markup else f"<p>{words}</p>") + form
    return product_page(AV, ["49,90 €", words, "—"], price="49.90", currency="EUR", availability=structured,
                        extra_dom=extra, sku="WX-200")


#: (language, words in stock, words out of stock): none of them in the verifier's phrase lists
AVAIL_WORDS = [
    ("vi", "Còn hàng", "Hết hàng"), ("th", "มีสินค้า", "สินค้าหมด"), ("tr", "Stokta var", "Stokta yok"),
    ("id", "Stok tersedia", "Stok habis"), ("pt", "Pronta entrega", "Fora de estoque"),
    ("pl", "Wysyłamy od ręki", "Chwilowo brak"), ("cs", "Ihned k odeslání", "Dočasně nelze koupit"),
    ("sv", "Leverans direkt", "Tillfälligt slut"), ("nl", "Direct uit voorraad", "Tijdelijk niet te koop"),
]
#: a negation before a phrase the lists read as in stock
NEGATED = [("en", "Not in stock"), ("fr", "Pas en stock"), ("es", "No disponible"), ("pl", "Nie ma na stanie"),
           ("pl", "Brak w magazynie"), ("cs", "Není skladem")]


def run_availability():
    from anyuse.execution import attribute_verify as AV
    S = "A availability"
    for lang, inw, outw in AVAIL_WORDS:
        case(S, f"{lang}: itemprop InStock, answer InStock", "beyond", "accept",
             lambda: avail_status(AV, shop_page(AV, inw, "InStock", "InStock"), "InStock"))
        case(S, f"{lang}: itemprop OutOfStock, stale structured InStock, answer InStock", "decoy", "refuse",
             lambda: avail_status(AV, shop_page(AV, outw, "OutOfStock", "InStock"), "InStock"))
        case(S, f"{lang}: itemprop OutOfStock, answer 'not stated'", "decoy", "refuse",
             lambda: avail_status(AV, shop_page(AV, outw, "OutOfStock", "OutOfStock"), "not stated"))
        case(S, f"{lang}: structured OutOfStock, buy control disabled, answer OutOfStock", "beyond", "accept",
             lambda: avail_status(AV, shop_page(AV, outw, "", "OutOfStock", buy_form(disabled=True)), "OutOfStock"))
        case(S, f"{lang}: structured OutOfStock, no statement, buy control enabled, answer OutOfStock", "decoy",
             "refuse", lambda: avail_status(AV, shop_page(AV, "—", "", "OutOfStock", buy_form()), "OutOfStock"))
        case(S, f"{lang}: no buy control where the run's other item page shows one, answer InStock", "decoy", "refuse",
             lambda: avail_status(AV, shop_page(AV, outw, "", "InStock"), "InStock",
                                  others=[shop_page(AV, inw, "", "InStock", buy_form())]))
    for lang, words in NEGATED:
        case(S, f"{lang}: {words!r} beside a stale structured InStock, answer InStock", "decoy", "refuse",
             lambda w=words: avail_status(AV, shop_page(AV, w, "", "InStock"), "InStock"))
    for words in ("Ausverkauft", "Out of stock", "Épuisé"):
        case(S, f"a stale itemprop InStock around {words!r}, answer InStock", "decoy", "refuse",
             lambda w=words: avail_status(AV, shop_page(AV, w, "InStock", "InStock"), "InStock"))
    case(S, "structured OutOfStock, buy control disabled, the box says 'Auf Lager', answer OutOfStock", "decoy",
         "refuse", lambda: avail_status(AV, shop_page(AV, "Auf Lager", "", "OutOfStock", buy_form(disabled=True)),
                                        "OutOfStock"))
    for words, structured, answer, want in (
            ("Auf Lager", "InStock", "InStock", "accept"),
            ("Derzeit nicht auf Lager", "OutOfStock", "OutOfStock", "accept"),
            ("In stock", "InStock", "InStock", "accept"),
            ("En stock", "InStock", "InStock", "accept"),
            ("Out of stock", "InStock", "InStock", "refuse"),
            ("Ware befindet sich in Zulauf", "InStock", "InStock", "refuse")):
        case(S, f"lists: {words!r}, answer {answer}", "lists", want,
             lambda w=words, s=structured, a=answer: avail_status(AV, shop_page(AV, w, "", s), a))


# ---------------------------------------------------------------------------
# B. prices and currencies
# ---------------------------------------------------------------------------

def heading_page(AV, heading, lines):
    dom = f"<html><head><title>{heading}</title></head><body><h1>{heading}</h1></body></html>"
    return AV.Page(dom, "\n".join([heading] + list(lines)), URL)


def price_status(AV, page, price, cur):
    v = AV.verify_product({"name": NAME, "price": price, "currency": cur, "url": URL}, page, ("price", "name", "url"))
    p = next(x for x in v.decisions if x.attr == "price").status
    c = next(x for x in v.decisions if x.attr == "currency").status
    return "accept" if (p, c) == (AV.VERIFIED, AV.VERIFIED) else "refuse"


def run_prices():
    from anyuse.execution import attribute_verify as AV
    S = "B prices"
    beyond = [("1.290.000₫", 1290000.0, "VND"), ("฿1,290", 1290.0, "THB"), ("₹ 2,499", 2499.0, "INR"),
              ("R$ 1.299,00", 1299.0, "BRL"), ("¥12,800", 12800.0, "JPY"), ("1 990 ₽", 1990.0, "RUB"),
              ("1.299,00 ₺", 1299.0, "TRY"), ("₱1,299", 1299.0, "PHP"), ("₩12,000", 12000.0, "KRW"),
              ("TRY 1.299,00", 1299.0, "TRY"), ("IDR 150.000", 150000.0, "IDR"), ("PLN 129,99", 129.99, "PLN"),
              ("Rp 150.000", 150000.0, "IDR"), ("1.299,00 TL", 1299.0, "TRY"), ("349 000 đ", 349000.0, "VND")]
    for line, price, cur in beyond:
        case(S, f"{line!r} {cur}", "beyond", "accept",
             lambda l=line, p=price, c=cur: price_status(
                 AV, product_page(AV, [l, "—"], price=str(p), currency=c), p, c))
    lists = [("219,99 €", 219.99, "EUR", "EUR"), ("129,99 zł", 129.99, "PLN", ""), ("1 290 Kč", 1290.0, "CZK", ""),
             ("1.299 kr", 1299.0, "SEK", "SEK"), ("$19.99", 19.99, "USD", ""), ("£24.50", 24.5, "GBP", "GBP")]
    for line, price, cur, st in lists:
        case(S, f"lists: {line!r} {cur}", "lists", "accept",
             lambda l=line, p=price, c=cur, s=st: price_status(
                 AV, product_page(AV, [l, "In den Warenkorb"], price=str(p) if s else "", currency=s), p, c))
    for heading in ("NIVEA MEN SHOWER GEL 250 ML", "AMD 5800X3D", "TOP 10 Bestseller"):
        # a capital word that spells an ISO 4217 code beside a number in the heading: the one price stays 3,99 €
        case(S, f"lists: the heading {heading!r} beside '3,99 €'", "lists", "accept",
             lambda h=heading: price_status(AV, heading_page(AV, h, ["3,99 €", "In den Warenkorb"]), 3.99, "EUR"))
    case(S, "decoy: 'USD 19.99' on a page whose structured currency is EUR", "decoy", "refuse",
         lambda: price_status(AV, product_page(AV, ["USD 19.99", "Add to cart"], price="19.99", currency="EUR"),
                              19.99, "EUR"))
    for line in ("Leistung 500 W", "Elektronik 230 V", "Nokia 3310", "Stockholm 2025"):
        case(S, f"decoy: no price in {line!r}", "decoy", [],
             lambda l=line: [list(x) for x in call(AV.prices_in, l)])


# ---------------------------------------------------------------------------
# C. the site's own search endpoints
# ---------------------------------------------------------------------------

BASE = "https://shop.test"


def search_link_status(AV, dom, page_url, value, result_url):
    page = AV.Page(f"<html><body>{dom}<ul><li><a href='{BASE}/p/1'>{value} 1</a></li></ul></body></html>", "x",
                   page_url)
    v = AV.verify_search_links({"results": [{"title": f"{value} seti", "url": result_url}]}, page, value)
    return "accept" if v.decisions[0].status == AV.VERIFIED else "refuse"


def run_search():
    from anyuse.execution import attribute_verify as AV
    S = "C search links"
    beyond = [
        ("tr <search>", '<search><form action="/ara"><input type="text" name="kelime"></form></search>',
         f"{BASE}/sonuc", "bisiklet", f"{BASE}/ara?kelime=bisiklet+seti"),
        ("pl role=search", '<form role="search" action="/szukaj"><input type="text" name="fraza"></form>',
         f"{BASE}/wyniki", "rower", f"{BASE}/szukaj?fraza=rower+miejski"),
        ("cs type=search", '<form action="/hledat"><input type="search" name="vyraz"></form>',
         f"{BASE}/vysledky", "kolo", f"{BASE}/hledat?vyraz=kolo+horske"),
        ("vi <search>", '<search><form action="/tim-kiem"><input name="tu_khoa"></form></search>',
         f"{BASE}/ket-qua", "xe", f"{BASE}/tim-kiem?tu_khoa=xe+dap"),
        ("sv value-carrying key", "", f"{BASE}/sok?fras=cykel", "cykel", f"{BASE}/sok?fras=cykel+barn"),
        ("id value-carrying key", "", f"{BASE}/cari?kata=sepeda", "sepeda", f"{BASE}/cari?kata=sepeda+lipat"),
    ]
    for name, dom, page_url, value, link in beyond:
        case(S, f"{name}: a 'did you mean' link to the site's search", "beyond", "refuse",
             lambda d=dom, u=page_url, v=value, l=link: search_link_status(AV, d, u, v, l))
    for name, dom, page_url, value, link in (
            ("q key", "", f"{BASE}/search?q=fahrrad", "fahrrad", f"{BASE}/search?q=fahrrad+kinder"),
            ("suchbegriff form", '<form action="/finden" method="get"><input type="text" name="suchbegriff"></form>',
             f"{BASE}/ergebnis", "fahrrad", f"{BASE}/finden?suchbegriff=fahrrad+kinder")):
        case(S, f"lists: {name}", "lists", "refuse",
             lambda d=dom, u=page_url, v=value, l=link: search_link_status(AV, d, u, v, l))
    for name, dom, page_url, value, link in (
            ("a key only named like a search", "", f"{BASE}/search?q=fahrrad&search_type=products", "fahrrad",
             f"{BASE}/search?search_type=products&item=5"),
            ("a filter input 'research_area'", '<form action="/finden"><input type="search" name="q"></form>'
             '<form action="/papers"><input type="text" name="research_area"></form>', f"{BASE}/ergebnis", "alpha",
             f"{BASE}/papers?research_area=alpha+beta")):
        case(S, f"decoy: {name} is a result", "decoy", "accept",
             lambda d=dom, u=page_url, v=value, l=link: search_link_status(AV, d, u, v, l))


# ---------------------------------------------------------------------------
# D. a stated result count
# ---------------------------------------------------------------------------

def lone_result_status(AV, statement, value, url="https://shop.test/ara?k=x"):
    dom = ("<html><body><main><h1>—</h1><p>%s</p><div class='r'><a href='https://shop.test/p/1'>%s Pro</a>"
           "</div></main></body></html>" % (html.escape(statement), html.escape(value.title())))
    page = AV.Page(dom, "—\n%s\n%s Pro" % (statement, value.title()), url)
    ans = {"results": [{"title": f"{value.title()} Pro", "url": "https://shop.test/p/1"}]}
    d = call(AV.verify_order, ans, page, value=value).decisions[0]
    return "accept" if d.status == AV.VERIFIED else "refuse"


def run_counts():
    from anyuse.execution import attribute_verify as AV
    S = "D result counts"
    for lang, line, value in (
            ("tr", "'bisiklet' için 12 sonuç bulundu", "bisiklet"),
            ("vi", "Kết quả tìm kiếm cho “xe đạp”: 12", "xe đạp"),
            ("th", "พบ 12 รายการสำหรับ \"จักรยาน\"", "จักรยาน"),
            ("pl", "Wyniki wyszukiwania „rower” (1.234)", "rower"),
            ("id", "Hasil pencarian untuk «sepeda» - 7", "sepeda"),
            ("pt", "Resultados para “bicicleta”: 38", "bicicleta"),
            ("cs", "Výsledky pro „kolo“ (15)", "kolo"),
            ("sv", "Sökresultat för ”cykel”: 9", "cykel")):
        case(S, f"{lang}: a lone result where the echo states more", "beyond", "refuse",
             lambda l=line, v=value: lone_result_status(AV, l, v))
        one = line.replace("12", "1").replace("1.234", "1").replace("7", "1").replace("38", "1").replace(
            "15", "1").replace("9", "1")
        case(S, f"{lang}: a lone result where the echo states one", "beyond", "accept",
             lambda l=one, v=value: lone_result_status(AV, l, v))
    case(S, "lists: '2 Produkte gefunden' beside a lone result", "lists", "refuse",
         lambda: lone_result_status(AV, "2 Produkte gefunden", "kaffee"))
    case(S, "lists: '1 Treffer' beside a lone result", "lists", "accept",
         lambda: lone_result_status(AV, "1 Treffer", "kaffee"))
    for name, line, value in (("a model number", "Bisiklet 26 jant", "bisiklet"),
                              ("a price beside the echo", "'Kaffee' ab 12,99 €", "kaffee"),
                              ("a date beside the echo", "'Kaffee' am 12.03.2025", "kaffee"),
                              ("two other numbers", "“iPhone 15” 128 GB, 2 colours", "iphone 15"),
                              ("a page number beside the echo", "Suchergebnisse für „Kaffee“ – Seite 2", "kaffee"),
                              ("a rating count beside the echo", "Kundenbewertungen zu „Kaffee“ (4)", "kaffee"),
                              ("a cart count beside the echo", "„Kaffee“ im Warenkorb: 3", "kaffee")):
        case(S, f"decoy: {name} is no count", "decoy", "accept",
             lambda l=line, v=value: lone_result_status(AV, l, v))


# ---------------------------------------------------------------------------
# E. an empty search
# ---------------------------------------------------------------------------

def empty_status(AV, page, value, sig=None):
    d = (call(AV.verify_empty, page, value, empty_page=sig) if sig is not None else
         AV.verify_empty(page, value)).decisions[0]
    return "accept" if d.status == AV.VERIFIED else "refuse"


def learned_empty(EP, empty, full, values, made_up):
    def pg(text, v):
        return EP.Page("text", v, text=text)
    return EP.learn("text", [pg(full.format(v=v, n=4 + i), v) for i, v in enumerate(values)],
                    [pg(empty.format(v=v), v) for v in made_up])


def run_empty():
    from anyuse.execution import attribute_verify as AV
    from anyuse.execution import empty_page as EP
    from anyuse.execution import model_check as MC
    S = "E empty search"
    for lang, empty, full, values, typed in (
            ("th", "หน้าแรก\nไม่พบสินค้าที่ตรงกับ {v}\nลองค้นหาอีกครั้ง", "หน้าแรก\nพบสินค้า {n} รายการ\n{v} รุ่น A\n{v} รุ่น B",
             ("จักรยาน", "หมวก"), "รองเท้าแตะ"),
            ("vi", "Trang chủ\nRất tiếc, {v} hiện chưa có\nThử từ khóa khác", "Trang chủ\n{n} sản phẩm\n{v} A\n{v} B",
             ("xe đạp", "mũ bảo hiểm"), "dép"),
            ("id", "Beranda\nMaaf, {v} belum tersedia\nCoba kata lain", "Beranda\n{n} produk\n{v} A\n{v} B",
             ("sepeda", "helm"), "sandal")):
        sig = learned_empty(EP, empty, full, values, ("zorvex qua", "limbrak tu"))
        page = AV.Page("", empty.format(v=typed), "https://shop.test/s?q=x")
        case(S, f"{lang}: the site's empty page learned at the proof", "beyond", "accept",
             lambda p=page, t=typed, s=sig: empty_status(AV, p, t, s or {}))
        full_page = AV.Page("", full.format(v=typed, n=3), "https://shop.test/s?q=x")
        case(S, f"{lang}: a result page against the learned empty page", "decoy", "refuse",
             lambda p=full_page, t=typed, s=sig: empty_status(AV, p, t, s or {}))
    for lang, text, value in (("ru", "По запросу «велосипед» ничего не найдено", "велосипед"),):
        page = AV.Page(f"<html><body><main><p>{text}</p></main></body></html>", text,
                       "https://shop.test/search?q=" + urllib.parse.quote(value))
        case(S, f"{lang}: a statement bound to the query", "beyond", "accept",
             lambda p=page, v=value: empty_status(AV, p, v))
    cz = AV.Page("<html><body><main><h1>Hledání</h1><p>Pro „kolo“ jsme bohužel nic nenašli</p></main></body></html>",
                 "Hledání\nPro „kolo“ jsme bohužel nic nenašli", "https://shop.test/hledat?q=kolo")

    def yes(instructions, state, obs):
        return 0.95 if "nic nenašli" in state.rsplit(">> ", 1)[-1] else 0.05

    def on_tool_call():
        with MC.using(yes), MC.scope("product"):
            return empty_status(AV, cz, "kolo")
    case(S, "cs: a statement only the check model reads, on a tool call", "beyond", "accept", on_tool_call)
    case(S, "cs: the same page with no scope open (a watch run, a proof)", "decoy", "refuse",
         lambda: empty_status(AV, cz, "kolo"))

    def in_a_reproof():
        no_model = getattr(AV, "no_model", lambda why="": contextlib.nullcontext())
        with MC.using(yes), MC.scope("product"), no_model("a re-proof asks no model"):
            return empty_status(AV, cz, "kolo")
    case(S, "cs: the same page in a re-proof inside a tool call's scope", "decoy", "refuse", in_a_reproof)
    case(S, "lists: 'Keine Ergebnisse gefunden'", "lists", "accept",
         lambda: empty_status(AV, AV.Page("", "Suche\nKeine Ergebnisse gefunden", "https://shop.test/s?q=x"),
                              "Kaffee"))
    case(S, "decoy: '12 Ergebnisse' beside 'Keine Treffer im Blog'", "decoy", "refuse",
         lambda: empty_status(AV, AV.Page("", "Suche\n12 Ergebnisse\nKeine Treffer im Blog",
                                          "https://shop.test/s?q=x"), "Kaffee"))


# ---------------------------------------------------------------------------
# F. breadth tools
# ---------------------------------------------------------------------------

#: per language: the sort control's label, its parameter, and its numbered options (value, text, the shop's sort)
SORT_LANGS = {
    "tr": ("Sırala", "siralama", [("1", "Önerilen", ""), ("2", "Fiyata göre artan", "price-asc"),
                                  ("3", "En yeniler", "newest")]),
    "pl": ("Kolejność", "kolejnosc", [("1", "Trafność", ""), ("2", "Od najtańszych", "price-asc"),
                                      ("3", "Najnowsze", "newest")]),
    "pt": ("Ordem", "ordem", [("1", "Relevância", ""), ("2", "Menor valor", "price-asc"),
                              ("3", "Lançamentos", "newest")]),
    "cs": ("Řazení", "razeni", [("1", "Doporučené", ""), ("2", "Od nejlevnějšího", "price-asc"),
                                ("3", "Novinky", "newest")]),
    "sv": ("Visa", "visa", [("1", "Populärast", ""), ("2", "Billigast först", "price-asc"),
                            ("3", "Nyheter", "newest")]),
    "vi": ("Xếp theo", "xep", [("1", "Phổ biến", ""), ("2", "Giá thấp đến cao", "price-asc"),
                               ("3", "Hàng mới về", "newest")]),
    "id": ("Urutan", "urutan", [("1", "Paling sesuai", ""), ("2", "Harga terendah", "price-asc"),
                                ("3", "Terbaru", "newest")]),
    "th": ("เรียงตาม", "riang", [("1", "แนะนำ", ""), ("2", "ราคาต่ำไปสูง", "price-asc"), ("3", "มาใหม่", "newest")]),
}


def lang_shop(TF, label, key, sorts):
    class LangShop(TF.Shop):
        def dom(self, url, state=None):
            sp = urllib.parse.urlsplit(url)
            if sp.path == "/search":
                q = dict(urllib.parse.parse_qsl(sp.query))
                if key in q:
                    val = q.pop(key)
                    q["sort"] = next((s for v, _t, s in sorts if v == val), "")
                dom, ld = super().dom(sp._replace(query=urllib.parse.urlencode(q)).geturl(), state)
                opts = "".join(f'<option value="{v}">{t}</option>' for v, t, _s in sorts)
                i, j = dom.index('<select id="s" name="sort">'), dom.index("</select>")
                dom = dom[:i] + f'<select id="s" name="{key}">{opts}' + dom[j:]
                return dom.replace('<label for="s">Sortieren nach</label>', f'<label for="s">{label}</label>'), ld
            return super().dom(url, state)

    class LangPage(TF.FakePage):
        def act(self, k, arg, select):
            r = self.read()
            e = next((x for x in r["elements"] if x["i"] == k), None)
            if select and e is not None and e["tag"] == "select":
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.cur).query))
                q[key] = arg
                self.cur = TF.BASE + "/search?" + urllib.parse.urlencode(q)
                return {"ok": True, "value": arg}
            return super().act(k, arg, select)
    return LangShop, LangPage


def sort_case(TF, C, label, key, sorts, option, want_key, value="Bohrhammer", shown="Akkuschrauber"):
    """Compile the demonstration of ``option`` on a search for ``shown`` and run the tool on ``value``."""
    shop_cls, page_cls = lang_shop(TF, label, key, sorts)
    real = TF.FakePage
    TF.FakePage = page_cls
    try:
        tr = TF.filtered_trace(shop_cls(), value=shown, option=option)
    finally:
        TF.FakePage = real
    g = C.compile_trace(tr, version=10)
    if not g.get("ok"):
        return f"not compiled: {str(g.get('why'))[:80]}"
    a = (g["tool"].get("applied") or [{}])[0]
    if a.get("key") != want_key:
        return f"key {a.get('key')!r}"
    r = asyncio.run(TF.T.run_tool(page_cls(shop_cls()), g["tool"], value, TF._settle, result_wait_s=0.2))
    if r.get("status") != "ok":
        return f"run {r.get('status')}: {str(r.get('why'))[:60]}"
    rows = (r.get("output") or {}).get("results") or []
    if want_key == "price_asc":
        prices = [x.get("price") for x in rows]
        return "ok" if len(prices) >= 2 and prices == sorted(prices) else f"prices {prices}"
    return "ok"


def facet_case(TF, C, numbered):
    """A brand filter demonstrated without a reported kind, its refined cards in rising price order (``numbered``:
    the brand links carry numeric ids, the cheapest card above the id, one before it below): the compiled kind."""
    class NumberedBrands(TF.Shop):
        IDS = {"bosch": "60", "makita": "12", "einhell": "77", "metabo": "31", "ryobi": "58"}

        def hits(self, q, sort="", brand="", instock=""):
            inv = {v: k for k, v in self.IDS.items()}
            return super().hits(q, sort, inv.get(brand, brand), instock)

        def search_dom(self, url, state):
            d = super().search_dom(url, state)
            for b, n in self.IDS.items():
                d = d.replace(f"brand={b}", f"brand={n}")
            return d
    real = list(TF.CATALOG)
    if not numbered:
        TF.CATALOG[0], TF.CATALOG[2] = TF.CATALOG[2], TF.CATALOG[0]
    try:
        tr = TF.filtered_trace(NumberedBrands() if numbered else TF.Shop(), how="brand", option="Bosch")
    finally:
        TF.CATALOG[:] = real
    if not numbered:
        tr = dict(tr, result=dict(tr["result"], applied="Marke Bosch"))
        tr["steps"] = [dict(st, action=dict(st["action"], result=tr["result"]))
                       if (st.get("action") or {}).get("action") == "done" else st for st in tr["steps"]]
    g = C.compile_trace(tr, version=10)
    if not g.get("ok"):
        return f"not compiled: {str(g.get('why'))[:80]}"
    return (g["tool"].get("applied") or [{}])[0].get("kind")


def date_rows(cards):
    return [{"title": t, "url": f"https://s.test/p/{u}", "card_text": d} for t, u, d, _m in cards]


def dates_status(DB, cards, key):
    dom = "<html><body><ul>" + "".join(f"<li><a href='/p/{u}'>{t}</a>{m}</li>" for t, u, _d, m in cards) + \
        "</ul></body></html>"
    rs = date_rows(cards)
    dates = DB.markup_dates(dom, "https://s.test/s?q=x", rs) if hasattr(DB, "markup_dates") else None
    got = call(DB.check_sorted, rs, key, dates) if dates is not None else DB.check_sorted(rs, key)
    return got[0]


def run_breadth():
    from anyuse.execution import demo_breadth as DB
    from anyuse.execution import demo_compile as C
    import test_demo_breadth_filtered as TF
    S = "F breadth"
    for lang, (label, key, sorts) in SORT_LANGS.items():
        case(S, f"{lang}: price sort {sorts[1][1]!r} compiled and run", "beyond", "ok",
             lambda l=label, k=key, s=sorts: sort_case(TF, C, l, k, s, s[1][1], "price_asc"))
        # demonstrated on "Bosch": newest first, its first page's prices in no order (on "Akkuschrauber" they
        # also rise, and the cards cannot tell the two orders apart)
        case(S, f"{lang}: newest-first sort {sorts[2][1]!r} compiled", "beyond", "ok",
             lambda l=label, k=key, s=sorts: sort_case(TF, C, l, k, s, s[2][1], "date_desc", shown="Bosch"))
    case(S, "lists: 'Preis aufsteigend' compiled and run", "lists", "ok",
         lambda: sort_case(TF, C, "Sortieren nach", "sort", [(v, t, v) for v, t in TF.SORTS], "Preis aufsteigend",
                           "price_asc"))
    case(S, "lists: 'Neueste' compiled", "lists", "ok",
         lambda: sort_case(TF, C, "Sortieren nach", "sort", [(v, t, v) for v, t in TF.SORTS], "Neueste", "date_desc",
                           shown="Bosch"))
    case(S, "decoy: a brand filter whose few refined cards rise in price is a facet", "decoy", "facet",
         lambda: facet_case(TF, C, numbered=False))
    case(S, "decoy: a brand filter with a numeric id is a facet, no price bound", "decoy", "facet",
         lambda: facet_case(TF, C, numbered=True))
    for k in ("stockstatus", "instockonly", "lagerstatus"):
        case(S, f"lists: parameter {k}=1 is an in-stock filter", "lists", "availability",
             lambda k=k: DB.classify_param(k, "1")["kind"])
    for k, v, label, want, kind in (("city", "stockholm", "Stockholm (3)", "facet", "decoy"),
                                    ("resort", "alpen", "", "facet", "decoy"),
                                    ("region", "sortland", "Sortland", "facet", "decoy"),
                                    ("instock", "1", "", "availability", "lists"),
                                    ("orderby", "price", "", "sort", "lists"),
                                    ("productSort", "2", "Preis aufsteigend", "sort", "lists"),
                                    ("listOrder", "2", "Preis aufsteigend", "sort", "lists")):
        case(S, f"{'decoy' if kind == 'decoy' else 'lists'}: parameter {k}={v} is a {want}", kind, want,
             lambda k=k, v=v, label=label: DB.classify_param(k, v, label)["kind"])
    for lang, text, value, n in (("tr", "Arama\n'matkap' için 128 sonuç\nFiltre", "matkap", 128),
                                 ("vi", "Kết quả cho “máy khoan”: 57", "máy khoan", 57),
                                 ("pl", "Znaleziono 214 produktów dla „wiertarka”", "wiertarka", 214),
                                 ("id", "Menampilkan 1-24 dari 340 hasil untuk \"bor\"", "bor", 340),
                                 ("pt", "38 resultados para “furadeira”", "furadeira", 38)):
        case(S, f"{lang}: the stated total beside the echo", "beyond", n,
             lambda t=text, v=value: call(DB.stated_total, t, value=v))
    case(S, "lists: '1–24 von 123 Treffern'", "lists", 123, lambda: call(DB.stated_total, "1–24 von 123 Treffern",
                                                                         value="Kaffee"))
    case(S, "decoy: a price beside the echo is no total", "decoy", None,
         lambda: call(DB.stated_total, "„Kaffee“ ab 12,99 €", value="Kaffee"))
    for lang, cards in (
            ("tr", [("A", "a", "1 Haziran 2025", "<time datetime='2025-06-01'>1 Haziran 2025</time>"),
                    ("B", "b", "12 Mart 2025", "<time datetime='2025-03-12'>12 Mart 2025</time>"),
                    ("C", "c", "20 Kasım 2024", "<time datetime='2024-11-20'>20 Kasım 2024</time>")]),
            ("th", [("A", "a", "1 มิ.ย. 2568", "<span itemprop='datePublished' content='2025-06-01'>1 มิ.ย. 2568</span>"),
                    ("B", "b", "12 มี.ค. 2568", "<span itemprop='datePublished' content='2025-03-12'>12 มี.ค. 2568</span>"),
                    ("C", "c", "20 พ.ย. 2567", "<span itemprop='datePublished' content='2024-11-20'>20 พ.ย. 2567</span>")]),
            ("vi", [("A", "a", "1 tháng 6, 2025", "<time datetime='2025-06-01T08:00'>1 tháng 6, 2025</time>"),
                    ("B", "b", "12 tháng 3, 2025", "<time datetime='2025-03-12T08:00'>12 tháng 3, 2025</time>"),
                    ("C", "c", "20 tháng 11, 2024", "<time datetime='2024-11-20T08:00'>20 tháng 11, 2024</time>")])):
        case(S, f"{lang}: newest first by the cards' markup dates", "beyond", "ok",
             lambda c=cards: dates_status(DB, c, "date_desc"))
        wrong = [cards[1], cards[0], cards[2]]
        case(S, f"{lang}: two markup dates out of order", "decoy", "failed",
             lambda c=wrong: dates_status(DB, c, "date_desc"))
    case(S, "lists: German month names", "lists", "ok",
         lambda: dates_status(DB, [("A", "a", "1. Juni 2025", ""), ("B", "b", "12. März 2025", ""),
                                   ("C", "c", "20. November 2024", "")], "date_desc"))
    for lang, page, p, want in (
            ("vi", {"url": "https://s.test/tim?q=x&trang=2", "elements": [
                {"tag": "a", "text": "Trang sau", "href": "https://s.test/tim?q=x&trang=3"},
                {"tag": "a", "text": "Xe đạp", "href": "https://s.test/p/1"}]}, 2, "trang=3"),
            ("pl", {"url": "https://s.test/szukaj?q=x&strona=2", "elements": [
                {"tag": "a", "text": "Następna strona", "href": "https://s.test/szukaj?q=x&strona=3"},
                {"tag": "a", "text": "Rower", "href": "https://s.test/p/1"}]}, 2, "strona=3"),
            ("th", {"url": "https://s.test/s?q=x", "elements": [
                {"tag": "a", "text": "รีวิว 2", "href": "https://s.test/s?q=x&rating=2"},
                {"tag": "a", "text": "ถัดไป", "href": "https://s.test/s?q=x&hn=2"},
                {"tag": "a", "text": "3", "href": "https://s.test/s?q=x&hn=3"}]}, 1, "hn=2"),
            ("id", {"url": "https://s.test/cari/sepeda/halaman/2/", "elements": [
                {"tag": "a", "text": "Berikutnya", "href": "https://s.test/cari/sepeda/halaman/3/"}]}, 2, "halaman/3/")):
        case(S, f"{lang}: the next page by the number its URL increments", "beyond", want,
             lambda pg=page, p=p, w=want: (lambda el: w if el is not None and el["href"].endswith(w) else
                                           (el or {}).get("href"))(DB.next_link(pg, "s.test", p, [pg["url"]])))
    case(S, "lists: 'Weiter' to page 3", "lists", "page=3",
         lambda: (lambda el: "page=3" if el is not None and el["href"].endswith("page=3") else el)(
             DB.next_link({"url": "https://s.test/s?q=x&page=2", "elements": [
                 {"tag": "a", "text": "Weiter", "href": "https://s.test/s?q=x&page=3"}]}, "s.test", 2,
                 ["https://s.test/s?q=x&page=2"])))
    ratings = [{"tag": "a", "text": f"{n} Sterne & mehr", "href": f"https://s.test/s?q=x&rating={n}"} for n in (4, 3, 2)]
    case(S, "lists: 'Weiter' on page 1 beside a numbered rating facet", "lists", "page=2",
         lambda: (lambda el: "page=2" if el is not None and el["href"].endswith("page=2") else (el or {}).get("href"))(
             DB.next_link({"url": "https://s.test/s?q=x", "elements": ratings + [
                 {"tag": "a", "text": "Weiter", "href": "https://s.test/s?q=x&page=2"}]}, "s.test", 1,
                 ["https://s.test/s?q=x"])))
    case(S, "decoy: a rating filter link is no next page", "decoy", None,
         lambda: DB.next_link({"url": "https://s.test/tim?q=x", "elements": [
             {"tag": "a", "text": "Đánh giá", "href": "https://s.test/tim?q=x&rating=2"}]}, "s.test", 1,
             ["https://s.test/tim?q=x"]))


# ---------------------------------------------------------------------------
# G. the web compile
# ---------------------------------------------------------------------------

ATTRIBUTE_TASKS = ("Bu ürünün fiyatı nedir: {q}", "Ile kosztuje {q}?", "Giá của {q} là bao nhiêu?",
                   "Berapa harga {q}?", "Kolik stojí {q}?", "Vad kostar {q}?", "Quanto custa o {q}?")
LIST_TASKS = ("Find hotels in Stockholm near {q}", "Show the pricing page of {q}", "Wyszukaj {q}")


def fake_check_model(instructions, state, obs):
    """A configured check model as a small model answers: yes for a task about one item's price."""
    return 0.95 if any(t.replace("{q}", "") .split()[0] in state for t in ATTRIBUTE_TASKS) else 0.05


def furniture_demo(start_text, result_text, titles):
    results = [{"title": t, "url": f"https://site.test/p/{i}"} for i, t in enumerate(titles)]
    els = [{"tag": "a", "text": t, "href": r["url"], "area": "main"} for t, r in zip(titles, results)]
    start = {"url": "https://site.test/", "text": start_text, "elements": []}
    fin = {"url": "https://site.test/s?q=x", "text": result_text, "elements": els}
    return {"workflow": "site_search", "value": "x", "steps": [{"page": start}, {"page": fin}], "final_page": fin,
            "result": {"results": results}}


def run_compile():
    from anyuse.execution import harness_compile as HC
    from anyuse.execution import harness_types as T
    from anyuse.execution import model_check as MC
    S = "G web compile"

    def wf(task, model=False):
        tr = T.Trace(kind="web", task=task, stop="done", result={"name": "X"})
        if model:
            with MC.using(fake_check_model):
                return HC.workflow_of(tr)
        return HC.workflow_of(tr)
    for task in ATTRIBUTE_TASKS:
        case(S, f"check model configured: {task!r}", "beyond", "product_lookup", lambda t=task: wf(t, True))
    for task in LIST_TASKS:
        case(S, f"decoy, no model: {task!r}", "decoy", "site_search", lambda t=task: wf(t))
        case(S, f"decoy, check model configured: {task!r}", "decoy", "site_search", lambda t=task: wf(t, True))
    for task in ("Wie viel kostet {q}? Preis bitte", "Is {q} in stock", "Prix de {q}", "Was ist der Kaufpreis von {q}?",
                 "Quanto costa il {q}?"):
        case(S, f"lists, no model: {task!r}", "lists", "product_lookup", lambda t=task: wf(t))
    for lang, banner, cart, titles in (
            ("nl", "Uitverkoop", "Winkelwagen", ["Gazelle Uitverkoop Stadsfiets", "Batavus Stadsfiets Dames",
                                                 "Cortina Transportfiets", "Sparta Elektrische fiets"]),
            ("pt", "Promoção", "Carrinho", ["Caloi Promoção Bicicleta Urbana", "Sense Bicicleta Aro 29",
                                            "Oggi Bicicleta Elétrica", "Groove Bicicleta Infantil"]),
            ("pl", "Wyprzedaż", "Koszyk", ["Kross Wyprzedaż Rower Miejski", "Romet Rower Górski",
                                           "Unibike Rower Trekkingowy", "Merida Rower Szosowy"])):
        d = furniture_demo(f"{banner}\n{cart}\nStart", f"{banner}\n{cart}\n" + "\n".join(titles), titles)
        case(S, f"{lang}: the banner on every page is no proof value", "beyond", False,
             lambda d=d, b=banner: b.casefold() in [v["value"].casefold() for v in HC.site_vocabulary(d, "zzz")])
        case(S, f"{lang}: a brand is still a proof value", "decoy", True,
             lambda d=d, t=titles: t[1].split()[0].casefold() in
             [v["value"].casefold() for v in HC.site_vocabulary(d, "zzz")])
    titles = ["Bosch Akkuschrauber Warenkorb", "Makita Akkuschrauber Warenkorb", "Metabo Akkuschrauber", "Einhell Set"]
    one = furniture_demo("", "Akkuschrauber\n" + "\n".join(titles), titles)
    one["steps"] = one["steps"][1:]
    case(S, "lists: 'Warenkorb' on a one-page demonstration", "lists", False,
         lambda: "warenkorb" in [v["value"].casefold() for v in HC.site_vocabulary(one, "Bohrer")])


# ---------------------------------------------------------------------------
# H. answer references
# ---------------------------------------------------------------------------

def run_labels():
    from anyuse.execution import answer_refs as AR
    S = "H answer labels"
    base = "https://news.test/a/"

    def cards_with(headings, second):
        links = []
        for i, (slug, head) in enumerate(headings):
            links += [AR.Link(2 * i + 1, base + slug, head), AR.Link(2 * i + 2, base + slug, second(i))]
        return links

    def title_of(links, element):
        got, _rep = AR.resolve_answer("site_search", {"results": [{"element": element}]}, links)
        return got["results"][0]["title"]
    teasers = ["Het water is dit jaar warmer dan ooit gemeten", "Waarom de bomen vroeger hun bladeren laten vallen",
               "De gemeente kiest voor een ontwerp van hout"]
    for lang, word in (("nl", "Meer"), ("fr", "Plus"), ("en", "Details"), ("sv", "Mer"), ("it", "Leggi")):
        heads = [("one", word), ("two", "Bossen in de winter"), ("three", "Een nieuwe brug voor de stad")]
        case(S, f"{lang}: a heading {word!r} on a page of cards is its title", "beyond", word,
             lambda h=heads: title_of(cards_with(h, lambda i: teasers[i]), 1))
    heads = [("one", "Bossen in de winter"), ("two", "Een nieuwe brug"), ("three", "Het water")]
    for lang, label in (("pt", "Leia mais"), ("tr", "Devamını oku"), ("pl", "Czytaj więcej")):
        case(S, f"{lang}: {label!r} repeated on every card is a label", "lists" if lang == "pl" else "beyond",
             "Een nieuwe brug", lambda l=label: title_of(cards_with(heads, lambda i: l), 4))
    case(S, "lists: 'Weiterlesen' on a page with one result", "lists", "Bosch GSR 12V",
         lambda: title_of([AR.Link(1, base + "x", "Bosch GSR 12V"), AR.Link(2, base + "x", "Weiterlesen")], 2))
    # one card's "Weiterlesen" below its heading, the other cards with a heading and a teaser
    mixed = [AR.Link(1, base + "x", "Bosch GSR 12V"), AR.Link(2, base + "x", "Weiterlesen")] + \
        cards_with([("two", "Bossen in de winter"), ("three", "Een nieuwe brug")], lambda i: teasers[i])[:4]
    for k, link in enumerate(mixed[2:]):
        link.index = 10 + k
    case(S, "lists: one card's 'Weiterlesen' below its heading, among cards without one", "lists", "Bosch GSR 12V",
         lambda: title_of(mixed, 2))


# ---------------------------------------------------------------------------
# I. the no-model promise
# ---------------------------------------------------------------------------

def run_seam():
    from anyuse.execution import attribute_verify as AV
    from anyuse.execution import demo_tool as T
    from anyuse.execution import dom_page as D
    from anyuse.execution import harness_compile as HC
    from anyuse.execution import model_check as MC
    from anyuse.execution import watch as WA
    import test_repair_ladder as TL
    import test_watch as TW
    S = "I no-model promise"
    value = "Bosch GSR 12V Akkuschrauber"

    class OpenShop(TL.Shop):
        def product(self, slug):
            dom = super().product(slug)
            return dom.replace("<p>Auf Lager</p>", "<p>Auf Lager</p><p>Lagerbestand wird geprüft</p>") if dom else dom

    class TextPage(D.DomPage):
        async def text(self):
            return str(self.read().get("text") or "")

    asked: List[str] = []

    def fake(prompt, schema):
        asked.append(prompt)
        return {"choice": schema["properties"]["choice"]["enum"][0], "line": 1, "calls": 1, "usd": 0.0}
    AV._DEFAULT.pop("r", None)
    AV.model_configured = lambda: True
    AV.configured_model = lambda budget_usd=None, adapter=None: fake
    tool = TL.compiled(value)

    def run_once(v=value):
        return asyncio.run(T.run_tool(TextPage(OpenShop()), tool, v, D.no_settle, result_wait_s=0.0))

    def watch_calls():
        asked.clear()

        def replay(item, t, caps):
            return run_once(str(item["input"]))
        with TW.Store() as s:
            s.st.add(s.tool(tool), value, "1h")
            WA.run_watch(s.st, replay, now=TW.hours(0))
        return len(asked)

    def proof_calls():
        asked.clear()

        def replay(t, _site, v):
            return asyncio.run(T.run_tool(TextPage(OpenShop()), t, v, D.no_settle, result_wait_s=0.0))
        HC.prove_web(tool, None, ["Makita DF333 Akkuschrauber", "Metabo PowerMaxx Akkuschrauber"],
                     demonstrated=value, replay=replay, independent=False)
        return len(asked)

    def tool_call_asks():
        asked.clear()
        with MC.scope("product"):
            run_once()
        return len(asked) >= 1
    case(S, "a watch run makes 0 verifier model calls", "beyond", 0, watch_calls)
    case(S, "a proof replay makes 0 verifier model calls", "beyond", 0, proof_calls)
    case(S, "a tool call still asks the verifier model", "lists", True, tool_call_asks)


# ---------------------------------------------------------------------------
# arms and report
# ---------------------------------------------------------------------------

SECTIONS = (run_availability, run_prices, run_search, run_counts, run_empty, run_breadth, run_compile, run_labels,
            run_seam)


def run_arm(ns):
    sys.path.insert(0, ns.pkgroot)
    sys.path.insert(1, os.path.join(ns.pkgroot, "anyuse", "tests"))
    for fn in SECTIONS:
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001 - a section that cannot start is booked
            ROWS.append({"section": fn.__name__, "case": "section", "kind": "lists", "want": "runs",
                         "got": f"error: {type(exc).__name__}: {exc}"[:200], "ok": False})
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(ROWS, fh, ensure_ascii=False, indent=1)


def _arm(arm, root, tmp):
    pk = os.path.join(tmp, "pk-" + arm)
    os.makedirs(pk)
    os.symlink(os.path.realpath(root), os.path.join(pk, "anyuse"))
    out = os.path.join(tmp, arm + ".json")
    home = os.path.join(tmp, "home-" + arm)
    data = os.path.join(home, ".anyuse")
    scratch = os.path.join(tmp, "tmp-" + arm)
    os.makedirs(data)
    os.makedirs(scratch)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_") and k != "PYTHONPATH"}
    env.update(HOME=home, TMPDIR=scratch, XDG_CONFIG_HOME=os.path.join(home, ".config"),
               XDG_DATA_HOME=os.path.join(home, ".local", "share"), XDG_CACHE_HOME=os.path.join(home, ".cache"),
               ANYUSE_DATA_DIR=data, ANYUSE_SECRETS_DIR=data,
               ANYUSE_VAULT_KEYFILE=os.path.join(data, "entity_vault.keys"),
               ANYUSE_API_HOSTS_FILE=os.path.join(data, "api_hosts.json"),
               ANYUSE_BLOCKED_HOSTS_FILE=os.path.join(data, "api_blocked_hosts.json"),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring", PYTHONDONTWRITEBYTECODE="1",
               ANYUSE_MODEL_CHECKS="none", ANYUSE_ATTRIBUTE_VERIFY="on", ANYUSE_EMPTY_PROBE="off",
               # the offline test guard: no socket, no desktop, no browser in either arm
               PYTHONPATH=os.path.join(os.path.realpath(root), "tools", "guarded_tests"))
    cmd = [sys.executable, "-B", os.path.abspath(__file__), "--arm", arm, "--pkgroot", pk, "--out", out]
    return subprocess.Popen(cmd, env=env, cwd=tmp), out


def report(old, new):
    lines, checks = [], []
    key = {(r["section"], r["case"]): r for r in old}
    sections = list(dict.fromkeys(r["section"] for r in new))
    lines.append(f"{'section':<22} {'kind':<7} {'cases':>5} {'old right':>10} {'new right':>10}")
    for s in sections:
        for kind in ("beyond", "lists", "decoy"):
            rs = [r for r in new if r["section"] == s and r["kind"] == kind]
            if not rs:
                continue
            o = sum(1 for r in rs if key.get((s, r["case"]), {}).get("ok"))
            n = sum(1 for r in rs if r["ok"])
            lines.append(f"{s:<22} {kind:<7} {len(rs):>5} {o:>10} {n:>10}")
    tot = {k: (sum(1 for r in new if r["kind"] == k and key.get((r["section"], r["case"]), {}).get("ok")),
               sum(1 for r in new if r["kind"] == k and r["ok"]), sum(1 for r in new if r["kind"] == k))
           for k in ("beyond", "lists", "decoy")}
    for k, (o, n, t) in tot.items():
        lines.append(f"{'total':<22} {k:<7} {t:>5} {o:>10} {n:>10}")
    lines.append("cases the new arm decides differently:")
    for r in new:
        o = key.get((r["section"], r["case"]))
        if o is None or o["got"] != r["got"]:
            lines.append(f"  [{r['kind']}] {r['section']} / {r['case']}: old {o['got'] if o else '-'!r} -> new "
                         f"{r['got']!r} (want {r['want']!r})")
    wrong = [r for r in new if not r["ok"]]
    if wrong:
        lines.append("cases the new arm gets wrong:")
        for r in wrong:
            lines.append(f"  [{r['kind']}] {r['section']} / {r['case']}: {r['got']!r} (want {r['want']!r})")
    regress = [r for r in new if not r["ok"] and key.get((r["section"], r["case"]), {}).get("ok")]
    checks.append({"name": "no case the old arm got right is wrong now", "pass": not regress})
    checks.append({"name": "every lists case right (new)", "pass": all(r["ok"] for r in new if r["kind"] == "lists")})
    checks.append({"name": "every decoy refused or unread (new)", "pass": all(r["ok"] for r in new
                                                                            if r["kind"] == "decoy")})
    checks.append({"name": "more beyond-the-lists cases right than before",
                   "pass": tot["beyond"][1] > tot["beyond"][0]})
    lines.append("checks:")
    for c in checks:
        lines.append(f"  {'PASS' if c['pass'] else 'FAIL'}  {c['name']}")
    return "\n".join(lines), {"old": old, "new": new, "checks": checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old-root", help="checkout of the code before the change")
    ap.add_argument("--new-root", default=CHECKOUT)
    ap.add_argument("--out", default="", help="working directory for the arms (default: a new temporary one)")
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--arm", default="", help=argparse.SUPPRESS)
    ap.add_argument("--pkgroot", default="", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    if ns.arm:
        run_arm(ns)
        return 0
    if not ns.old_root:
        ap.error("--old-root is required: a checkout of the code before the change")
    tmp = tempfile.mkdtemp(prefix="generalize-verify-bench-", dir=ns.out or None)
    try:
        procs = [_arm(arm, root, tmp) for arm, root in (("old", ns.old_root), ("new", ns.new_root))]
        if [p.wait() for p, _o in procs] != [0, 0]:
            print("an arm failed")
            return 2
        docs = []
        for _p, path in procs:
            with open(path, encoding="utf-8") as fh:
                docs.append(json.load(fh))
        text, result = report(*docs)
        print(text)
        if ns.json:
            with open(ns.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
        return 0 if all(c["pass"] for c in result["checks"]) else 1
    finally:
        for arm in ("old", "new"):
            link = os.path.join(tmp, "pk-" + arm, "anyuse")
            if os.path.islink(link):
                os.unlink(link)
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp)


if __name__ == "__main__":
    sys.exit(main())
