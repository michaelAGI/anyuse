"""The attribute verifier reads the page's own evidence before its word lists (``execution/attribute_verify.py``).

Each judgement takes the site's own evidence first - markup standards, the page's structured data, values equal to
the typed input - and the verifier's hand-written lists only last. Each case: a page in a language or markup the
lists miss, decided right now, and the lists' own cases still decided as before.

* availability: the visible words the page itself marks ``itemprop=availability``, and the state of the product's
  buy control (a form posting the product's own identifier) with the structured data, measured against the site's
  other item pages of the run; on a tool call the verifier's model reads the buy box before the phrases decide; the
  phrases last.
* prices and currencies: a currency symbol by its Unicode category, an ISO 4217 code next to a number, the page's
  own mark beside its structured price; the table of local abbreviations last, each a whole word.
* the site's own search endpoints: the key that carries the typed value, the HTML search controls (``type=search``,
  ``role=search``, ``<search>``); a key only named like a search last, and never by a substring inside a word.
* a delivery time stated as a range of days, in place of one site's wording.
* words of any script: the verifier's tokenizer keeps letters that do not decompose (ı, đ, ø, Cyrillic, Thai).
* a result count: the number beside the page's quoted echo of the typed input, before the count nouns.
* an empty search: the site's empty page learned at the proof, the empty-answer cascade (a statement bound to the
  query; on a tool call the configured check model), the verifier's own phrases last.

Offline, no model.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import currency_marks as CM  # noqa: E402

NAME = "Widget X 200"
URL = "https://shop.test/p/widget-x-200"


def product_page(lines, price="", currency="", availability="", extra_dom="", h1=NAME):
    offer = {}
    if price:
        offer["price"] = price
    if currency:
        offer["priceCurrency"] = currency
    if availability:
        offer["availability"] = "https://schema.org/" + availability
    import json
    ld = ('<script type="application/ld+json">%s</script>' % json.dumps(
        {"@context": "https://schema.org", "@type": "Product", "name": NAME, "offers": offer})) if offer else ""
    dom = f"<html><head><title>{NAME}</title>{ld}</head><body><h1>{h1}</h1>{extra_dom}</body></html>"
    return AV.Page(dom, "\n".join([h1] + list(lines)), URL)


def decision(v, attr):
    return next(d for d in v.decisions if d.attr == attr)


def price_and_currency(page, price, cur):
    v = AV.verify_product({"name": NAME, "price": price, "currency": cur, "url": URL}, page, ("price", "name", "url"))
    return decision(v, "price").status, decision(v, "currency").status


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

def availability(page, av, others=()):
    v = AV.verify_product({"name": NAME, "price": 49.9, "currency": "EUR", "availability": av, "url": URL}, page,
                          ("availability",), others=others)
    return decision(v, "availability")


def marked(words, cls):
    return f'<div class="stock"><span itemprop="availability" content="https://schema.org/{cls}">{words}</span></div>'


def buy_form(disabled=False, sku="WX-200"):
    return (f'<form method="post" action="/cart/add"><input type="hidden" name="product" value="{sku}">'
            f'<button type="submit"{" disabled" if disabled else ""}>…</button></form>')


def shop_page(words, cls_markup, structured, form=""):
    extra = (marked(words, cls_markup) if cls_markup else f"<p>{words}</p>") + form
    page = product_page(["49,90 €", words, "Beschreibung"], price="49.90", currency="EUR", availability=structured,
                        extra_dom=extra)
    import json as _j
    page._dom = page._dom.replace('"offers": {', '"sku": "WX-200", "offers": {')
    page._tree = None
    return page


def test_the_pages_own_markup_states_the_class_in_any_language():
    d = availability(shop_page("Còn hàng", "InStock", "InStock"), "InStock")              # Vietnamese
    assert d.status == AV.VERIFIED and not d.listed, d.why
    d = availability(shop_page("สินค้าหมด", "OutOfStock", "InStock"), "InStock")          # Thai, the markup disagrees
    assert d.status == AV.CONFLICT, d.why
    d = availability(shop_page("Stokta yok", "OutOfStock", "OutOfStock"), "not stated")    # Turkish
    assert d.status == AV.CONFLICT, d.why


def test_the_buy_control_and_the_structured_data_confirm_out_of_stock():
    page = shop_page("Tükendi", "", "OutOfStock", buy_form(disabled=True))
    assert AV.buy_control(page) == {"state": "disabled", "action": "/cart/add", "by": "identifier"}
    d = availability(page, "OutOfStock")
    assert d.status == AV.VERIFIED, d.why
    # an enabled control does not confirm out of stock
    assert availability(shop_page("Tükendi", "", "OutOfStock", buy_form()), "OutOfStock").status != AV.VERIFIED


def test_the_buy_control_by_its_buy_action_or_its_quantity_input():
    import json as _j
    ld = _j.dumps({"@context": "https://schema.org", "@type": "Product", "name": NAME,
                   "potentialAction": {"@type": "BuyAction", "target": "https://shop.test/basket/put"}})
    head = f'<html><head><script type="application/ld+json">{ld}</script></head><body><h1>{NAME}</h1>'

    def ctl(body):
        return AV.buy_control(AV.Page(head + body + "</body></html>", NAME, URL))
    # the form whose action is the structured data's BuyAction target
    assert ctl('<form action="/basket/put"><button>→</button></form>') == \
        {"state": "enabled", "action": "/basket/put", "by": "buy action"}
    # a POST form with a quantity input, its button marked aria-disabled
    assert ctl('<form method="post" action="/b"><input type="number" name="n"><button aria-disabled="true">→'
               '</button></form>') == {"state": "disabled", "action": "/b", "by": "quantity"}
    # a disabled fieldset around the submit control
    assert ctl('<form method="post" action="/b"><input type="number" name="n"><fieldset disabled>'
               '<input type="submit" value="→"></fieldset></form>')["state"] == "disabled"
    # a GET form with a number input is no buy control (a filter, a page jump)
    assert ctl('<form method="get" action="/s"><input type="number" name="page"><button>→</button></form>') == {}


def test_the_buy_control_measured_against_the_runs_other_item_pages():
    other = shop_page("Finns i lager", "", "InStock", buy_form())
    gone = shop_page("Tillfälligt slut", "", "InStock")                                     # Swedish, no form at all
    assert availability(gone, "InStock", others=[other]).status == AV.CONFLICT
    assert availability(gone, "InStock").status != AV.CONFLICT        # alone, an absent form is no evidence
    # a disabled control alone never contradicts an orderable class (a variant may have to be chosen first)
    d = availability(shop_page("Auf Lager", "", "InStock", buy_form(disabled=True)), "InStock")
    assert d.status == AV.VERIFIED and d.listed, d.why


def test_on_a_tool_call_the_model_reads_the_box_before_the_phrases():
    page = shop_page("มีสินค้า", "", "InStock")                                             # Thai "in stock", no markup
    d = availability(page, "not stated")
    assert d.status == AV.VERIFIED and d.listed                     # the phrases see nothing (the last rung)
    calls = []

    def model(prompt, schema):
        calls.append(prompt)
        return {"choice": "in_stock", "line": 2, "calls": 1}
    v = AV.check("product_lookup", {"name": NAME, "price": 49.9, "currency": "EUR", "availability": "not stated",
                                    "url": URL}, page, AV.Resolver(model))
    assert calls and decision(v, "availability").status == AV.WITHHELD
    assert v.verdict != AV.ACCEPT
    # without a model (a watch run, a proof) the phrases' verdict stands, as before
    v0 = AV.check("product_lookup", {"name": NAME, "price": 49.9, "currency": "EUR", "availability": "not stated",
                                     "url": URL}, page, AV.rules_resolver())
    assert decision(v0, "availability").status == AV.VERIFIED


def test_the_structure_never_verifies_over_the_words_the_page_shows():
    # a stale markup value beside visible words the phrases read as another class: the page states two things
    for words in ("Ausverkauft", "Out of stock"):
        d = availability(shop_page(words, "InStock", "InStock"), "InStock")
        assert d.status == AV.CONFLICT, (words, d.why)
    # the structured data and a disabled control say out of stock, the buy box says in stock
    d = availability(shop_page("Auf Lager", "", "OutOfStock", buy_form(disabled=True)), "OutOfStock")
    assert d.status == AV.CONFLICT, d.why


def test_the_phrase_lists_own_cases_still_decide():
    assert availability(shop_page("Auf Lager", "", "InStock"), "InStock").status == AV.VERIFIED
    assert availability(shop_page("Ware befindet sich in Zulauf", "", "InStock"), "InStock").status == AV.CONFLICT
    assert availability(shop_page("Derzeit nicht auf Lager", "", "OutOfStock"), "OutOfStock").status == AV.VERIFIED


@pytest.mark.parametrize("words", ["Not in stock", "Pas en stock", "No disponible", "Nie ma na stanie",
                                   "Brak w magazynie", "Není skladem", "Not currently in stock"])
def test_a_negation_before_an_in_stock_phrase_is_no_in_stock(words):
    # the phrase alone ("in stock", "en stock", "disponible", "na stanie", "w magazynie", "skladem") reads in stock
    assert AV.classify_line(words)[0] == "unknown"
    assert availability(shop_page(words, "", "InStock"), "InStock").status != AV.VERIFIED


def test_a_negation_in_another_clause_denies_nothing():
    assert AV.classify_line("Keine Versandkosten, auf Lager")[0] == "in_stock"
    assert AV.classify_line("Plus que 3 en stock")[0] == "limited"
    assert AV.classify_line("Nicht auf Lager")[0] == "out_of_stock"


# ---------------------------------------------------------------------------
# currencies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,price,cur", [
    ("1.290.000₫", 1290000.0, "VND"),          # Vietnamese dong: a symbol of category Sc, a grouped amount
    ("฿1,290", 1290.0, "THB"),                 # Thai baht
    ("₹ 2,499", 2499.0, "INR"),
    ("R$ 1.299,00", 1299.0, "BRL"),            # letters in front of a currency symbol
    ("¥12,800", 12800.0, "JPY"),
    ("TRY 1.299,00", 1299.0, "TRY"),           # an ISO 4217 code next to the number
    ("IDR 150.000", 150000.0, "IDR"),
])
def test_a_currency_symbol_or_code_by_standard(line, price, cur):
    page = product_page([line, "Sepete ekle"], price=str(price), currency=cur)
    assert price_and_currency(page, price, cur) == (AV.VERIFIED, AV.VERIFIED)


@pytest.mark.parametrize("line,price,cur", [
    ("Rp 150.000", 150000.0, "IDR"),           # Indonesian: the page writes its own abbreviation
    ("1.299,00 TL", 1299.0, "TRY"),            # Turkish
])
def test_the_pages_own_mark_beside_its_structured_price(line, price, cur):
    page = product_page([line, "Beli sekarang"], price=str(price), currency=cur)
    assert AV.page_marks(page)
    assert price_and_currency(page, price, cur) == (AV.VERIFIED, AV.VERIFIED)


def test_the_pages_own_mark_needs_the_structured_amount():
    # letters beside another number are not the page's mark: no structured price says so
    page = product_page(["Rp 99.000", "Beli sekarang"], price="150000", currency="IDR")
    assert AV.page_marks(page) == {}
    assert AV.prices_in("Rp 99.000", AV.page_marks(page)) == []


@pytest.mark.parametrize("line,price,cur,structured", [
    ("219,99 €", 219.99, "EUR", "EUR"),
    ("129,99 zł", 129.99, "PLN", ""),
    ("1 290 Kč", 1290.0, "CZK", ""),
    ("1.299 kr", 1299.0, "SEK", "SEK"),
    ("$19.99", 19.99, "USD", ""),
])
def test_the_tables_own_cases_still_read(line, price, cur, structured):
    page = product_page([line, "In den Warenkorb"], price=str(price) if structured else "", currency=structured)
    assert price_and_currency(page, price, cur) == (AV.VERIFIED, AV.VERIFIED)


def test_a_code_of_another_currency_is_a_conflict():
    page = product_page(["USD 19.99", "Add to cart"], price="19.99", currency="EUR")
    assert price_and_currency(page, 19.99, "EUR")[1] == AV.CONFLICT


def test_a_table_abbreviation_is_a_whole_word():
    # "lei" inside "Leistung", "ron" inside "Elektronik", "nok" inside "Nokia": no currency
    for line in ("Leistung 500 W", "Elektronik 230 V", "Nokia 3310"):
        assert AV.prices_in(line) == [], line
    assert AV.prices_in("49 lei") == [(49.0, "RON")]


def test_currency_marks_by_standard():
    assert [m for _p, m in CM.marks_in("Preis: 12,95 € statt 15,00 €")] == ["€", "€"]
    assert [m for _p, m in CM.marks_in("ab 12 USD")] == ["USD"]
    assert CM.marks_in("THE CAT 12") == []                 # three capitals that are no ISO 4217 code
    assert CM.marks_in("USD rates") == []                  # a code away from a number
    # a capital word that spells a code beside a count, a year or a model number is no currency
    for line in ("TOP 10 Bestseller", "CAD 2024 Lizenz", "SHOWER GEL 250ML", "SHOWER GEL 250 ML", "AMD 5800X3D"):
        assert CM.marks_in(line) == [], line
    assert [m for _p, m in CM.marks_in("IDR 150.000")] == ["IDR"]


def test_a_heading_that_spells_a_code_beside_a_number_is_no_price():
    for h1 in ("NIVEA MEN SHOWER GEL 250 ML", "AMD 5800X3D"):
        page = product_page(["3,99 €", "In den Warenkorb"], h1=h1)
        v = AV.verify_product({"name": h1, "price": 3.99, "currency": "EUR", "url": URL}, page, ("price",))
        assert decision(v, "price").status == AV.VERIFIED, (h1, decision(v, "price").why)
    assert CM.code_of("EUR") == "EUR" and CM.code_of("$") is None and CM.is_symbol("₫") and not CM.is_symbol("e")


# ---------------------------------------------------------------------------
# the site's own search
# ---------------------------------------------------------------------------

BASE = "https://shop.test"


def test_an_html_search_control_is_an_endpoint_in_any_language():
    # Turkish and Polish search forms: the input names are in no list, the markup says what they are
    for dom, link in (
            ('<search><form action="/ara"><input type="text" name="kelime"></form></search>', "/ara?kelime=bisiklet+seti"),
            ('<form role="search" action="/szukaj"><input type="text" name="fraza"></form>', "/szukaj?fraza=rower+miejski")):
        page = AV.Page(f"<html><body>{dom}<ul><li><a href='{BASE}/p/1'>Bisiklet 1</a></li></ul></body></html>", "x",
                       f"{BASE}/sonuc")
        ends = AV.search_ends(page, "bisiklet")
        assert AV.is_search_link(BASE + link, ends), (dom, ends)
        bad = {"results": [{"title": "Bisiklet seti", "url": BASE + link}]}
        assert AV.verify_search_links(bad, page, "bisiklet").decisions[0].status == AV.CONFLICT


def test_the_key_that_carries_the_value_comes_before_the_names():
    # the page's URL carries the value in "q"; another key merely named like a search ("search_type") is no endpoint
    page = AV.Page("<html><body></body></html>", "x", f"{BASE}/search?q=fahrrad&search_type=products")
    ends = AV.search_ends(page, "fahrrad")
    assert ends == [("shop.test", "/search", "q")]
    good = {"results": [{"title": "Fahrrad X", "url": f"{BASE}/search?search_type=products&item=5"}]}
    assert AV.verify_search_links(good, page, "fahrrad").decisions[0].status == AV.VERIFIED


def test_a_search_control_comes_before_an_input_named_like_a_search():
    # a GET filter form whose text input is named "research_area" beside the site's type=search box
    dom = ('<form action="/finden"><input type="search" name="q"></form>'
           '<form action="/papers"><input type="text" name="research_area"></form>')
    page = AV.Page(f"<html><body>{dom}</body></html>", "x", f"{BASE}/ergebnis")
    ends = AV.search_ends(page, "alpha")
    assert ("shop.test", "/finden", "q") in ends and not any(k == "research_area" for _h, _p, k in ends)


def test_names_are_the_last_rung_and_whole_words():
    form = AV.Page('<html><body><form action="/finden" method="get"><input type="text" name="suchbegriff"></form>'
                   '</body></html>', "", f"{BASE}/ergebnis")
    assert AV.is_search_link(f"{BASE}/finden?suchbegriff=x", AV.search_ends(form))
    assert AV.named_like_search("searchTerm") and AV.named_like_search("tx_search[q]") and AV.named_like_search("q")
    assert not AV.named_like_search("research") and not AV.named_like_search("besucher")


# ---------------------------------------------------------------------------
# a delivery time
# ---------------------------------------------------------------------------

def test_a_range_of_days_is_a_delivery_time():
    assert AV.classify_line("In 2 – 4 Werktagen bei dir")[0] == AV.DELIVERY
    assert AV.classify_line("Teslimat 2-3 gün")[0] in ("", AV.DELIVERY)     # a unit of no list: unread, never a class
    assert AV.classify_line("Auf Lager, in 2 – 4 Werktagen bei dir")[0] == "in_stock"
    assert AV.classify_line("30 Tage Rückgaberecht")[0] == ""


# ---------------------------------------------------------------------------
# words of any script
# ---------------------------------------------------------------------------

def test_words_of_any_script():
    assert AV.tokens("Kırmızı Bisiklet") == ["kırmızı", "bisiklet"]
    assert AV.tokens("Xe đạp điện") == ["xe", "đap", "đien"]
    assert AV.tokens("Велосипед горный 26") == ["велосипед", "горныи", "26"]
    rows = [("Велосипед горный Stels 26", "https://shop.test/p/1"), ("Шлем велосипедный", "https://shop.test/p/2")]
    dom = "<html><body><ul>" + "".join(f"<li><a href='{u}'>{t}</a> <span>1 990 ₽</span></li>" for t, u in rows) + \
        "</ul></body></html>"
    v = AV.verify_list({"results": [{"title": t, "url": u} for t, u in rows]}, AV.Page(dom, "x", "https://shop.test/s"))
    assert all(d.status == AV.VERIFIED for d in v.decisions), v.why()
    wrong = AV.verify_list({"results": [{"title": rows[1][0], "url": rows[0][1]}]}, AV.Page(dom, "x", "https://shop.test/s"))
    assert wrong.decisions[0].status != AV.VERIFIED


# ---------------------------------------------------------------------------
# a result count
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,value,n", [
    ("'bisiklet' için 12 sonuç bulundu", "bisiklet", 12),               # Turkish
    ("Kết quả tìm kiếm cho “xe đạp”: 2", "xe đạp", 2),                  # Vietnamese
    ("พบ 12 รายการสำหรับ \"จักรยาน\"", "จักรยาน", 12),                  # Thai
    ("Wyniki wyszukiwania „rower” (1.234)", "rower", 1234),             # Polish, a grouped number
    ("Hasil pencarian untuk «sepeda» - 7", "sepeda", 7),               # Indonesian
    ("Výsledky pro „kolo“ (15)", "kolo", 15),                          # Czech quotes „…“
    ("Sökresultat för ”cykel”: 9", "cykel", 9),                        # Swedish quotes ”…”
    ("Menampilkan 1-24 dari 340 hasil untuk \"bor\"", "bor", 340),    # a pager's range, then its total
])
def test_the_count_beside_the_quoted_echo_of_the_input(line, value, n):
    st = AV.count_statement(["Home", line, "Filter"], value=value)
    assert st and st["count"] == n and st["by"] == "echo", st


def test_a_page_rating_or_cart_number_beside_the_echo_is_no_count():
    for line in ("Suchergebnisse für „Bohrer“ – Seite 2", "Kundenbewertungen zu „Bohrer“ (4)",
                 "„Bohrer“ im Warenkorb: 3"):
        assert AV.count_statement([line], value="Bohrer") is None, line
    assert AV.count_statement(["Ergebnisse für „Bohrer“ (12)"], value="Bohrer")["count"] == 12
    # an excluded word inside the input itself refuses nothing
    assert AV.count_statement(["Ergebnisse für „Warenkorb Tasche“: 7"], value="Warenkorb Tasche")["count"] == 7


def test_the_count_nouns_still_read_and_decoys_are_no_count():
    assert AV.count_statement(["2 Produkte gefunden"], value="Kaffee")["count"] == 2
    assert AV.count_statement(["Zeige 1 bis 24 von 123"], value="Kaffee")["count"] == 123
    assert AV.count_statement(["Bisiklet 26 jant"], value="bisiklet") is None          # an unquoted model number
    assert AV.echo_count("“iPhone 15” 128 GB, 2 colours", "iPhone 15") is None          # two other numbers
    assert AV.echo_count("'Kaffee' ab 12,99 €", "Kaffee") is None                       # a price
    assert AV.echo_count("'Kaffee' am 12.03.2025", "Kaffee") is None                    # a date
    assert AV.echo_count("„Kaffee“ 24-1 von 300", "Kaffee") is None                     # no range of the shown results


def _single_result_page(statement):
    dom = ("<html><body><main><h1>Arama</h1><p>%s</p><div class='r'><a href='https://shop.test/p/1'>Bisiklet Pro</a>"
           "</div></main></body></html>" % statement)
    return AV.Page(dom, "Arama\n%s\nBisiklet Pro" % statement, "https://shop.test/ara?k=bisiklet")


def test_a_lone_result_against_a_count_the_page_states_in_its_own_echo():
    ans = {"results": [{"title": "Bisiklet Pro", "url": "https://shop.test/p/1"}]}
    d = AV.verify_order(ans, _single_result_page("'bisiklet' için 12 sonuç"), value="bisiklet").decisions[0]
    assert d.status == AV.UNCERTAIN and d.final, d.why
    ok = AV.verify_order(ans, _single_result_page("'bisiklet' için 1 sonuç"), value="bisiklet").decisions[0]
    assert ok.status == AV.VERIFIED, ok.why


def test_a_sparse_list_is_the_querys_when_the_echo_states_its_count():
    page = AV.Page("<html><body><h1>Tìm kiếm</h1></body></html>",
                   "Tìm kiếm\nKết quả tìm kiếm cho “xe đạp”: 2\nXe đạp A\nXe đạp B", "https://shop.test/s")
    v = AV.sparse_evidence("xe đạp", 2, page)
    assert v.verdict == AV.ACCEPT, v.why()


# ---------------------------------------------------------------------------
# an empty search
# ---------------------------------------------------------------------------

def _text_page(text, value):
    from anyuse.execution import empty_page as EP
    return EP.Page("text", value, text=text)


def thai_empty_page():
    from anyuse.execution import empty_page as EP
    empty = "หน้าแรก\nไม่พบสินค้าที่ตรงกับ {v}\nลองค้นหาอีกครั้ง"
    full = "หน้าแรก\nพบสินค้า {n} รายการ\n{v} รุ่น A\n{v} รุ่น B"
    sig = EP.learn("text", [_text_page(full.format(v="จักรยาน", n=12), "จักรยาน"),
                            _text_page(full.format(v="หมวก", n=4), "หมวก")],
                   [_text_page(empty.format(v="zorvex qua"), "zorvex qua"),
                    _text_page(empty.format(v="limbrak tu"), "limbrak tu")])
    assert sig and sig.get("marks"), sig
    return sig


def test_the_sites_own_empty_page_learned_at_the_proof():
    sig = thai_empty_page()
    page = AV.Page("", "หน้าแรก\nไม่พบสินค้าที่ตรงกับ รองเท้าแตะ\nลองค้นหาอีกครั้ง", "https://shop.test/s?q=x")
    assert AV.verify_empty(page, "รองเท้าแตะ").decisions[0].status == AV.UNCERTAIN      # no phrase of any list
    d = AV.verify_empty(page, "รองเท้าแตะ", sig).decisions[0]
    assert d.status == AV.VERIFIED and "own empty page" in d.why, d.why


def test_a_query_bound_statement_of_the_empty_answer_cascade():
    # Russian: the empty-answer cascade's statement rule reads it, bound to the query; the verifier's phrases do not
    page = AV.Page("<html><body><main><p>По запросу «велосипед» ничего не найдено</p></main></body></html>",
                   "По запросу «велосипед» ничего не найдено", "https://shop.test/search?q=велосипед")
    d = AV.verify_empty(page, "велосипед").decisions[0]
    assert d.status == AV.VERIFIED, d.why


def test_on_a_tool_call_the_check_model_reads_a_statement_no_list_has():
    from anyuse.execution import model_check as MC
    page = AV.Page("<html><body><main><h1>Hledání</h1><p>Pro „kolo“ jsme bohužel nic nenašli</p></main></body></html>",
                   "Hledání\nPro „kolo“ jsme bohužel nic nenašli", "https://shop.test/hledat?q=kolo")
    assert AV.verify_empty(page, "kolo").decisions[0].status == AV.UNCERTAIN
    asked = []

    def yes(instructions, state, obs):
        asked.append(state)
        return 0.95 if "nic nenašli" in state.rsplit(">> ", 1)[-1] else 0.05
    with MC.using(yes), MC.scope("product"):
        d = AV.verify_empty(page, "kolo").decisions[0]
    assert asked and d.status == AV.VERIFIED, d.why
    asked.clear()
    with MC.using(yes):                                   # a watch run or a proof: no scope, no question
        assert AV.verify_empty(page, "kolo").decisions[0].status == AV.UNCERTAIN
    assert asked == []
    # a proof's replay or a repair's re-proof inside a tool call's scope: no question either
    with MC.using(yes), MC.scope("product"), AV.no_model("a re-proof asks no model"):
        assert AV.verify_empty(page, "kolo").decisions[0].status == AV.UNCERTAIN
    assert asked == []


def test_the_verifiers_own_phrases_and_disputes_still_hold():
    p = AV.Page("", "Suche\nKeine Ergebnisse gefunden", "https://shop.test/s?q=x")
    assert AV.verify_empty(p, "Kaffee").decisions[0].status == AV.VERIFIED
    p2 = AV.Page("", "Suche\n12 Ergebnisse\nKeine Treffer im Blog", "https://shop.test/s?q=x")
    assert AV.verify_empty(p2, "Kaffee").decisions[0].status == AV.UNCERTAIN


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
