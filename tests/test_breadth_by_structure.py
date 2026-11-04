"""Breadth tools read a demonstration's own pages before their lexicons (``execution/demo_breadth.py``).

* a sort's key: the order the refined page's cards satisfy and the list before did not, before the reported kind and
  the 11 languages' sort words - a shop whose sort labels and parameter are in Turkish compiles and is checked;
* a filter's kind: a price range the refined cards keep; an in-stock filter by whole words ("Stockholm" is a city);
  a facet the refined page shows (its label on every card, an active-filter chip) is no sort or price range by the
  cards, whatever order its few cards happen to show;
* a card's date: its markup's ``<time datetime>`` first, a numeric date next, month names last;
* the stated total: the number beside the page's quoted echo of the input first;
* the next page: ``rel=next``, then the link whose URL increments the page number, then words.

Offline: the fake shop of ``test_demo_breadth_filtered.py`` with its labels in another language; no model.
"""
from __future__ import annotations

import asyncio
import os
import sys
import urllib.parse

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_breadth as DB  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
import test_demo_breadth_filtered as TF  # noqa: E402

#: the Turkish shop's sort parameter and its numbered values
TR_KEY = "siralama"
TR_SORTS = [("1", "Önerilen", ""), ("2", "Fiyata göre artan", "price-asc"), ("3", "Fiyata göre azalan", "price-desc"),
            ("4", "En yeniler", "newest")]


class TurkishShop(TF.Shop):
    """The fake shop with a sort drop-down in Turkish whose parameter is ``siralama=<number>``."""

    def dom(self, url, state=None):
        sp = urllib.parse.urlsplit(url)
        if sp.path == "/search":
            q = dict(urllib.parse.parse_qsl(sp.query))
            if TR_KEY in q:
                val = q.pop(TR_KEY)
                q["sort"] = next((s for v, _t, s in TR_SORTS if v == val), "")
            dom, ld = super().dom(sp._replace(query=urllib.parse.urlencode(q)).geturl(), state)
            opts = "".join(f'<option value="{v}">{t}</option>' for v, t, _s in TR_SORTS)
            i, j = dom.index('<select id="s" name="sort">'), dom.index("</select>")
            return dom[:i] + f'<select id="s" name="{TR_KEY}">{opts}' + dom[j:], ld
        return super().dom(url, state)


class TurkishPage(TF.FakePage):
    def act(self, k, arg, select):
        r = self.read()
        e = next((x for x in r["elements"] if x["i"] == k), None)
        if select and e is not None and e["tag"] == "select":
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.cur).query))
            q[TR_KEY] = arg
            self.cur = TF.BASE + "/search?" + urllib.parse.urlencode(q)
            return {"ok": True, "value": arg}
        return super().act(k, arg, select)


def turkish_trace(option):
    real = TF.FakePage
    TF.FakePage = TurkishPage
    try:
        tr = TF.filtered_trace(TurkishShop(), option=option)
    finally:
        TF.FakePage = real
    return tr


def run(tool, value):
    return asyncio.run(TF.T.run_tool(TurkishPage(TurkishShop()), tool, value, TF._settle, result_wait_s=0.2))


def test_a_sort_in_a_language_of_no_lexicon_compiles_from_the_cards_it_shows():
    assert DB.sort_key("Fiyata göre artan") == "" and not DB.is_sort(TR_KEY)       # no word of the lexicons
    g = C.compile_trace(turkish_trace("Fiyata göre artan"), version=10)
    assert g["ok"], g.get("why")
    a = g["tool"]["applied"][0]
    assert a["kind"] == "sort" and a["key"] == "price_asc" and a["param"] == TR_KEY, a
    r = run(g["tool"], "Bohrhammer")
    prices = [x["price"] for x in r["output"]["results"]]
    assert r["status"] == "ok" and prices == sorted(prices) and len(prices) >= 2, r
    desc = C.compile_trace(turkish_trace("Fiyata göre azalan"), version=10)
    assert desc["ok"], desc.get("why")
    # the list before the sort was already in descending price order: the cards cannot tell, the lexicons cannot
    # either - the key stays unknown and the tool's own check reports the order as unverifiable, never as sorted
    assert desc["tool"]["applied"][0]["key"] in ("price_desc", ""), desc["tool"]["applied"]


def test_the_lexicons_own_sorts_still_compile():
    g = C.compile_trace(TF.filtered_trace(TF.Shop()), version=10)
    assert g["ok"] and g["tool"]["applied"][0]["key"] == "price_asc", g.get("why")
    g2 = C.compile_trace(TF.filtered_trace(TF.Shop(), option="Neueste"), version=10)
    assert g2["ok"] and g2["tool"]["applied"][0]["key"] == "date_desc", g2.get("why")


def rows(prices=(), dates=(), urls=None):
    n = max(len(prices), len(dates))
    out = []
    for i in range(n):
        txt = " ".join(x for x in ((TF.eur(prices[i]) + " €") if i < len(prices) else "",
                                   dates[i] if i < len(dates) else "") if x)
        out.append({"title": f"Item {i}", "url": (urls or {}).get(i) or f"https://s.test/p/{i}", "card_text": txt})
    return out


def test_the_order_and_the_range_the_cards_show():
    before = rows([89.0, 79.5, 64.9, 49.99])
    after = rows([49.99, 64.9, 79.5, 89.0])
    assert DB.refinement_orders(before, after) == {"price_asc"}
    assert DB.refinement_orders(after, after) == set()                  # the list was already in that order
    assert DB.range_kept(rows([12.0, 55.0, 80.0, 30.0]), rows([12.0, 30.0, 45.0]), "10-50") == (10.0, 50.0)
    assert DB.range_kept(rows([12.0, 55.0, 80.0]), rows([12.0, 30.0, 45.0]), "50") == (None, 50.0)
    assert DB.range_kept(rows([12.0, 30.0]), rows([12.0, 30.0, 45.0]), "50") is None   # nothing was outside before


class NumberedBrands(TF.Shop):
    """The fake shop whose brand links carry numeric ids (``brand=60``) instead of the brand's name."""
    IDS = {"bosch": "60", "makita": "12", "einhell": "77", "metabo": "31", "ryobi": "58"}

    def hits(self, q, sort="", brand="", instock=""):
        inv = {v: k for k, v in self.IDS.items()}
        return super().hits(q, sort, inv.get(brand, brand), instock)

    def search_dom(self, url, state):
        d = super().search_dom(url, state)
        for b, n in self.IDS.items():
            d = d.replace(f"brand={b}", f"brand={n}")
        return d


def _without_reported_kind(tr):
    tr = dict(tr, result=dict(tr["result"], applied="Marke Bosch"))
    tr["steps"] = [dict(st, action=dict(st["action"], result=tr["result"]))
                   if (st.get("action") or {}).get("action") == "done" else st for st in tr["steps"]]
    return tr


def test_a_facet_the_page_shows_is_no_sort_or_price_range_by_the_cards():
    real = list(TF.CATALOG)
    # the Bosch screwdrivers rise in price in the shop's order, the first page of the search does not
    TF.CATALOG[0], TF.CATALOG[2] = TF.CATALOG[2], TF.CATALOG[0]
    try:
        tr = _without_reported_kind(TF.filtered_trace(TF.Shop(), how="brand", option="Bosch"))
        g = C.compile_trace(tr, version=10)
    finally:
        TF.CATALOG[:] = real
    assert g["ok"], g.get("why")
    a = g["tool"]["applied"][0]
    # every refined card names "Bosch" and the chip shows it: a facet, though its three prices happen to rise
    assert a["kind"] == "facet" and a["label"] == "Bosch", a
    # a numeric brand id is no price bound: every refined price is at least 60 and the list before held 49,99
    g = C.compile_trace(TF.filtered_trace(NumberedBrands(), how="brand", option="Bosch"), version=10)
    assert g["ok"], g.get("why")
    a = g["tool"]["applied"][0]
    assert a["kind"] == "facet" and a["value"] == "60", a
    # a price bound the control's label shows is read from the cards
    assert DB.range_kept(rows([12.0, 55.0, 80.0]), rows([12.0, 30.0, 45.0]), "50", "Unter 50 TL") == (None, 50.0)
    assert DB.range_kept(rows([12.0, 55.0, 80.0]), rows([12.0, 30.0, 45.0]), "50", "Bosch") is None


def test_an_in_stock_filter_by_whole_words():
    assert DB.classify_param("city", "stockholm", "Stockholm (3)")["kind"] == "facet"
    assert DB.classify_param("instock", "1")["kind"] == "availability"
    # a parameter name runs its words together: a part that holds a stock word names the filter
    for key in ("stockstatus", "instockonly", "onlyinstock", "lagerstatus", "hideoutofstock"):
        assert DB.classify_param(key, "1")["kind"] == "availability", key
    assert DB.classify_param("filter", "instock", "Nur auf Lager")["kind"] == "availability"
    assert DB.classify_param("avail", "yes")["kind"] == "availability"
    assert DB.classify_param("resort", "alpen")["kind"] == "facet"      # "sort" inside a word is no sort
    assert DB.classify_param("orderby", "price")["kind"] == "sort"
    for key in ("productSort", "listOrder", "sSortBy", "iSortCol"):                 # camelCase parts are words
        assert DB.classify_param(key, "2", "Preis aufsteigend")["kind"] == "sort", key
    assert DB.classify_param("border", "2", "Rand")["kind"] == "facet"
    assert DB.classify_param("region", "sortland", "Sortland")["kind"] == "facet"       # a place, no sort label
    assert DB.is_sort("s", "Sortieren nach") and DB.is_sort("s", "Sort by") and DB.is_sort("s", "Sortierung")


def test_a_cards_markup_date_comes_first():
    dom = ("<html><body><ul>"
           "<li><a href='/p/a'>A</a><time datetime='2025-06-01'>1 Haziran 2025</time></li>"
           "<li><a href='/p/b'>B</a><time datetime='2025-03-12'>12 Mart 2025</time></li>"
           "<li><a href='/p/c'>C</a><span itemprop='datePublished' content='2024-11-20'>20 Kasım 2024</span></li>"
           "</ul></body></html>")
    rs = [{"title": t, "url": f"https://s.test/p/{u}", "card_text": d} for t, u, d in
          (("A", "a", "1 Haziran 2025"), ("B", "b", "12 Mart 2025"), ("C", "c", "20 Kasım 2024"))]
    assert DB.check_sorted(rs, "date_desc")[0] == "incomplete"           # Turkish month names: no lexicon
    dates = DB.markup_dates(dom, "https://s.test/s?q=x", rs)
    assert dates == {C._norm(f"https://s.test/p/{k}"): d for k, d in
                     (("a", (2025, 6, 1)), ("b", (2025, 3, 12)), ("c", (2024, 11, 20)))}, dates
    assert DB.check_sorted(rs, "date_desc", dates)[0] == "ok"
    assert DB.card_date("aktualisiert 2025-04-01, veröffentlicht 12. März 2025") == (2025, 4, 1)
    assert DB.card_date("veröffentlicht am 12. März 2025") == (2025, 3, 12)


def test_the_stated_total_beside_the_quoted_echo():
    assert DB.stated_total("Arama\n'matkap' için 128 sonuç\nFiltre", "matkap") == 128
    assert DB.stated_total("Kết quả cho “máy khoan”: 57", "máy khoan") == 57
    assert DB.stated_total("'matkap' için 128 sonuç") is None              # without the input: the lexicon only
    assert DB.stated_total("1–24 von 123 Treffern", "Kaffee") == 123
    assert DB.stated_total("„Kaffee“ ab 12,99 €", "Kaffee") is None
    assert DB.stated_total("Výsledky pro „vrtačka“ (215)", "vrtačka") == 215             # Czech quotes „…“
    assert DB.stated_total("Sökresultat för ”borr”: 64", "borr") == 64                  # Swedish quotes ”…”
    assert DB.stated_total("Menampilkan 1-24 dari 340 hasil untuk \"bor\"", "bor") == 340   # a pager's range and total
    assert DB.stated_total("Menampilkan 1-24 dari 340 hasil untuk bor") is None          # no echo: no lexicon word


def test_the_next_page_by_the_number_its_url_increments():
    page2 = {"url": "https://s.test/tim?q=x&trang=2", "elements": [
        {"tag": "a", "text": "Trang sau", "href": "https://s.test/tim?q=x&trang=3"},
        {"tag": "a", "text": "Xe đạp", "href": "https://s.test/p/1"}]}
    el = DB.next_link(page2, "s.test", 2, ["https://s.test/tim?q=x&trang=2"])
    assert el is not None and el["text"] == "Trang sau"
    # page 1 without a page parameter: a key the page's links number like a pager
    page1 = {"url": "https://s.test/tim?q=x", "elements": [
        {"tag": "a", "text": "Đánh giá 2", "href": "https://s.test/tim?q=x&rating=2"},
        {"tag": "a", "text": "›", "href": "https://s.test/tim?q=x&trang=2"},
        {"tag": "a", "text": "Cuối", "href": "https://s.test/tim?q=x&trang=9"}]}
    el = DB.next_link(page1, "s.test", 1, ["https://s.test/tim?q=x"])
    assert el is not None and el["href"].endswith("trang=2")
    decoy = {"url": "https://s.test/tim?q=x", "elements": [
        {"tag": "a", "text": "Đánh giá", "href": "https://s.test/tim?q=x&rating=2"}]}
    assert DB.next_link(decoy, "s.test", 1, ["https://s.test/tim?q=x"]) is None
    assert DB.increments("https://s.test/news/page/2/", "https://s.test/news/page/3/", 2)


def test_a_numbered_facet_is_no_next_page():
    ratings = [{"tag": "a", "text": f"{n} Sterne & mehr", "href": f"https://s.test/s?q=x&rating={n}"} for n in (4, 3, 2)]
    weiter = {"tag": "a", "text": "Weiter", "href": "https://s.test/s?q=x&page=2"}
    page1 = {"url": "https://s.test/s?q=x", "elements": ratings + [weiter]}
    assert DB.next_link(page1, "s.test", 1, ["https://s.test/s?q=x"])["href"].endswith("page=2")
    # a pager in no lexicon's words beside the same facet: its links number themselves
    pager = [{"tag": "a", "text": "Sonraki", "href": "https://s.test/s?q=x&sayfa=2"},
             {"tag": "a", "text": "3", "href": "https://s.test/s?q=x&sayfa=3"}]
    page1 = {"url": "https://s.test/s?q=x", "elements": ratings + pager}
    assert DB.next_link(page1, "s.test", 1, ["https://s.test/s?q=x"])["href"].endswith("sayfa=2")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
