"""Attribute verifier fixes: the correct answers the verifier held back in an acceptance run, each turned into a
general rule, and the wrong answers that must stay withheld. Fakes only (pages built inline), no model, no network.

* fix 1a - the buy box counts CONTENT lines: a quantity select's bare option lines ("1", "2", "3") and one more variant
  line never push the availability line out of the window (www.michenaud.com);
* fix 1b - a result card never climbs into the PAGE's structure (the page heading, header, nav, footer, main): a single
  answered result's card no longer reads the page's h1 as its own heading (www.coffee-perfect.de "Entkalker");
* fix 2 - a quoted DELIVERY TIME is compatible with every orderable class (goods inbound, in stock, a few left,
  pre-order) and contradicts only out of stock / store only (www.feuerdepot.de); a schema-class answer is
  unchanged, so the version-13 InStock beside "Ware befindet sich in Zulauf" and teufel.de's LimitedAvailability beside
  "Auf Lager" stay conflicts;
* fix 3 - the runtime second stage asks the MATCH question (the card's naming texts first) only for a
  NEAR-TITLE doubt (the card shows >= 90 % of the title: truncation, quotes, punctuation); a doubt about WHICH text is
  the title (a date badge, another card's title, a longer naming text) keeps the CHOICE question, so a date badge is
  never confirmed by a yes on the answer (www.raumfahrer.net);
* fix 4 - the paged runner (and the filtered runner) get the verifier's hook: with the setting on, every paged row
  is checked on the result page it was read from (``verify_paged``; local reads, no request), an off-scope promo row
  is withheld; off, the record is unchanged.
"""
from __future__ import annotations

import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import attribute_verify as AV  # noqa: E402


def jsonld(name, price, availability, currency="EUR"):
    return ('<script type="application/ld+json">' + json.dumps(
        {"@context": "https://schema.org", "@type": "Product", "name": name,
         "offers": {"@type": "Offer", "price": str(price), "priceCurrency": currency,
                    "availability": f"https://schema.org/{availability}"}}) + "</script>")


# --- fix 1a: a product page whose availability line sits after a variant line and a quantity select ----------------
PIANO = "Yamaha Arius YDP-146 White"
PIANO_URL = "https://shop.example/p63511/yamaha-arius-ydp-146-white.php"
PIANO_LINES = ["Accueil > Piano > Yamaha > Yamaha Arius YDP-146 White", "YAMAHA ARIUS YDP-146 WHITE", "3 finitions",
               "1049.00 €", "Dont 0.90 € d'écotaxe", "Quantité", "1", "2", "3", "RÉF. SHOP : 63511",
               "Stock Internet :\t En stock", "Magasin Nantes :\t En stock", "DESCRIPTION",
               "Piano numérique meuble en finition blanche."]


def piano_page(lines=PIANO_LINES, availability="InStock"):
    dom = (f"<html><head>{jsonld(PIANO, '1049.00', availability)}<link rel='canonical' href='{PIANO_URL}'></head>"
           f"<body><nav>Accueil</nav><h1>YAMAHA ARIUS YDP-146 WHITE</h1><div>" +
           "".join(f"<p>{ln}</p>" for ln in lines[2:]) + "</div></body></html>")
    return AV.Page(dom, "\n".join(lines), PIANO_URL)


PIANO_ANSWER = {"name": PIANO, "price": 1049.0, "currency": "EUR", "availability": "InStock", "url": PIANO_URL}


class BuyBoxContentLines(unittest.TestCase):
    def test_option_lines_are_taken_but_not_counted(self):
        lines = ["H", "a", "1", "2", "3", "b", "c"]
        self.assertEqual(AV._content_window(lines, 0, 0, 2), (0, 5))      # a, (1 2 3), b
        self.assertEqual(AV._content_window(lines, 6, 2, 0), (1, 6))      # a, (1 2 3), b before c
        self.assertEqual(AV._content_window(["x"], 0, 4, 4), (0, 0))

    def test_the_stock_line_after_a_variant_line_and_a_quantity_select_is_in_the_box(self):
        page = piano_page()
        box = AV.buy_box(page, PIANO, 1049.0)
        self.assertIn(PIANO_LINES.index("Stock Internet :\t En stock"), box)
        d = AV.read_availability(page, PIANO_ANSWER)
        self.assertEqual(d.status, AV.VERIFIED, d.why)

    def test_a_wrong_class_in_the_same_place_still_conflicts(self):
        lines = [ln.replace("En stock", "Rupture de stock") for ln in PIANO_LINES]
        d = AV.read_availability(piano_page(lines), PIANO_ANSWER)
        self.assertEqual(d.status, AV.CONFLICT, d.why)

    def test_a_long_select_never_widens_the_box_past_its_content_lines(self):
        # 200 quantity options, then the add-to-cart control and the availability statement (www.feuerdepot.de's
        # layout): the options are skipped, the content lines after them are the box's
        lines = ["Ofen Modell X 8 kW", "2.029,00 €", "Gewünschte Menge:"] + [str(i) for i in range(1, 201)] + \
                ["IN DEN WARENKORB", "Lieferzeit ca.: 5-10 Werktage *", "Versandkostenfrei", "Kunden kauften auch",
                 "Anderer Ofen 12 kW", "Ausverkauft", "Noch ein Ofen", "x", "y", "z", "Nicht lieferbar"]
        page = AV.Page("<html><body><h1>Ofen Modell X 8 kW</h1></body></html>", "\n".join(lines))
        box = AV.buy_box(page, "Ofen Modell X 8 kW", 2029.0)
        texts = [page.lines[j] for j in box]
        self.assertIn("Lieferzeit ca.: 5-10 Werktage *", texts)
        self.assertNotIn("Nicht lieferbar", texts)


# --- fix 1b: a single answered result on a page with a heading, a header and a nav ---------------------------------
ONE_URL = "https://shop.example/coffee-perfect-Fluessigentkalker"
ONE = (f"<html><body><header><nav><a href='/konto'>Anmelden oder Registrieren</a><a href='/service'>Service Center</a>"
       f"</nav></header><main><h1>Zu \"Entkalker\" wurde 1 Produkt gefunden</h1><div class='filter'><form><select>"
       f"<option>Beste Ergebnisse</option><option>Preis aufsteigend</option></select></form></div>"
       f"<div class='listing'><div class='box'><a href='{ONE_URL}'><img alt='coffee perfect Flüssigentkalker'></a>"
       f"<a href='{ONE_URL}'>coffee perfect Flüssigentkalker</a><span>7,99 €</span><form><button>In den Warenkorb"
       f"</button></form></div></div></main><footer>Impressum</footer></body></html>")
ONE_TEXT = ("Anmelden oder Registrieren\nService Center\nZu \"Entkalker\" wurde 1 Produkt gefunden\nBeste Ergebnisse\n"
            "coffee perfect Flüssigentkalker\n7,99 €\nIn den Warenkorb\nImpressum")
ONE_ANSWER = {"results": [{"title": "coffee perfect Flüssigentkalker", "url": ONE_URL}]}


class CardStopsAtPageStructure(unittest.TestCase):
    def test_a_single_result_card_never_holds_the_page_heading(self):
        page = AV.Page(ONE, ONE_TEXT, "https://shop.example/search?search=Entkalker")
        cards = AV.card_texts(page, [ONE_URL])[ONE_URL]
        for c in cards:
            self.assertNotIn("gefunden", c["text"])
            self.assertNotIn("Anmelden", c["text"])
        v = AV.verify_list(ONE_ANSWER, page)
        self.assertEqual(v.verdict, AV.ACCEPT, v.why())

    def test_a_result_whose_own_heading_is_an_h1_still_reads_its_card(self):
        dom = ("<html><body><main><div class='r'><h1><a href='https://s.example/a'>Erster Artikel über Kaffee</a></h1>"
               "<p>Ein Ausschnitt</p></div><div class='r'><h1><a href='https://s.example/b'>Zweiter Artikel über Tee"
               "</a></h1><p>Noch ein Ausschnitt</p></div></main></body></html>")
        text = "Erster Artikel über Kaffee\nEin Ausschnitt\nZweiter Artikel über Tee\nNoch ein Ausschnitt"
        ans = {"results": [{"title": "Erster Artikel über Kaffee", "url": "https://s.example/a"},
                           {"title": "Zweiter Artikel über Tee", "url": "https://s.example/b"}]}
        v = AV.verify_list(ans, AV.Page(dom, text, "https://s.example/?s=Artikel"))
        self.assertEqual(v.verdict, AV.ACCEPT, v.why())

    def test_a_title_from_another_text_of_the_card_stays_open(self):
        # the card names its result "Flüssigentkalker 1 l"; the answer's title is the price line: never verified
        dom = ONE.replace(">coffee perfect Flüssigentkalker</a>", ">Flüssigentkalker 1 l</a>")
        page = AV.Page(dom, ONE_TEXT.replace("coffee perfect Flüssigentkalker", "Flüssigentkalker 1 l"), "")
        v = AV.verify_list({"results": [{"title": "7,99 €", "url": ONE_URL}]}, page)
        self.assertEqual(v.verdict, AV.WITHHOLD, v.why())


# --- fix 2: a quoted delivery time beside an orderable class ------------------------------------------------------
OFEN = "KACHELOFENEINSATZ BUDERUS LOGAFLAME HLS117 8 KW"
OFEN_URL = "https://shop.example/kachelofeneinsatz-buderus-logaflame-hls117-8kw/"


def ofen_page(status_lines=("Lieferzeit ca.: 5-10 Werktage *", "Ware befindet sich in Zulauf"), schema="InStock",
              extra=()):
    lines = [OFEN, "Versandkostenfrei", "-35 %", "3.099,95 €", "2.029,00 €", "Preise inkl. USt.", "Gewünschte Menge:"] + \
            [str(i) for i in range(1, 41)] + ["IN DEN WARENKORB", *status_lines, "BESTPREISANGEBOT", "Vergleichen",
                                              "Beschreibung", "Ein Kachelofeneinsatz für Austauschgeräte.", *extra]
    dom = (f"<html><head>{jsonld(OFEN, '2029.00', schema)}<link rel='canonical' href='{OFEN_URL}'></head><body>"
           f"<h1>{OFEN}</h1></body></html>")
    return AV.Page(dom, "\n".join(lines), OFEN_URL)


def ofen_answer(availability):
    return {"name": OFEN, "price": 2029.0, "currency": "EUR", "availability": availability, "url": OFEN_URL}


class DeliveryTimeIsOrderable(unittest.TestCase):
    def test_a_quoted_delivery_time_beside_goods_inbound_is_verified(self):
        d = AV.read_availability(ofen_page(), ofen_answer("Lieferzeit ca.: 5-10 Werktage"))
        self.assertEqual(d.status, AV.VERIFIED, d.why)
        self.assertIn("backorder", d.why)

    def test_the_version_13_in_stock_beside_goods_inbound_stays_a_conflict(self):
        d = AV.read_availability(ofen_page(), ofen_answer("InStock"))
        self.assertEqual(d.status, AV.CONFLICT, d.why)

    def test_a_limited_answer_beside_auf_lager_stays_a_conflict(self):
        page = ofen_page(status_lines=("Auf Lager, in 2 – 4 Werktagen bei dir",), schema="LimitedAvailability")
        d = AV.read_availability(page, ofen_answer("LimitedAvailability"))
        self.assertEqual(d.status, AV.CONFLICT, d.why)

    def test_a_delivery_time_beside_out_of_stock_or_store_only_is_a_conflict(self):
        for line in ("Ausverkauft", "Nur im Laden erhältlich"):
            page = ofen_page(status_lines=("Lieferzeit ca.: 5-10 Werktage *", line))
            d = AV.read_availability(page, ofen_answer("Lieferzeit ca.: 5-10 Werktage"))
            self.assertEqual(d.status, AV.CONFLICT, (line, d.why))

    def test_a_delivery_time_quoted_from_outside_the_buy_box_stays_open(self):
        # the quote is on the page (an accessory's line far below), the buy box states only goods inbound
        page = ofen_page(status_lines=("Ware befindet sich in Zulauf",),
                         extra=[f"Zubehör Nr. {i}" for i in range(30)] + ["Lieferzeit ca.: 2-3 Werktage"])
        d = AV.read_availability(page, ofen_answer("Lieferzeit ca.: 2-3 Werktage"))
        self.assertEqual(d.status, AV.UNCERTAIN, d.why)


# --- fix 3: the question follows the doubt -------------------------------------------------------------------------
class FakeModel:
    """Says MATCH to every match question (the worst case for a date badge) and, asked to choose, picks the option
    that names ``pick``; every prompt is kept."""

    def __init__(self, pick: str = "Starship"):
        self.pick, self.asked = pick, []

    def __call__(self, prompt, schema):
        enum = schema["properties"]["choice"]["enum"]
        self.asked.append(("match" if "match" in enum else "choice", prompt))
        if "match" in enum:
            return {"choice": "match", "line": 1, "calls": 1, "usd": 0.0002}
        best = next((o for o in enum if self.pick in o), "none of these")
        return {"choice": best, "line": 1, "calls": 1, "usd": 0.0002}


def news_page(cards):
    dom = "<html><body><main><h1>Suche</h1>" + "".join(cards) + "</main></body></html>"
    return AV.Page(dom, "", "https://news.example/?s=x")


class QuestionFollowsTheDoubt(unittest.TestCase):
    def test_a_near_title_is_put_to_the_match_question(self):
        title = "Die Regierung will ab 2027 von allen Bürgern Bankdaten abfragen lassen"
        card = ("<div class='c'><a href='https://news.example/a'>Die Regierung will ab 2027 von allen Bürgern Bankdaten "
                "abfragen</a><p>Ein Ausschnitt</p></div><div class='c'><a href='https://news.example/b'>Anderes</a></div>")
        v = AV.verify_list({"results": [{"title": title, "url": "https://news.example/a"}]}, news_page([card]))
        d = v.decisions[0]
        self.assertEqual((d.status, d.doubt), (AV.UNCERTAIN, "near_title"), d.why)
        m = FakeModel()
        v = AV.Resolver(m)(v, {})
        self.assertEqual([k for k, _p in m.asked], ["match"])
        self.assertIn("Die Regierung will ab 2027", m.asked[0][1].split("Evidence lines")[1])   # naming texts first
        self.assertEqual(v.verdict, AV.ACCEPT, v.why())

    def test_a_date_badge_keeps_the_choice_question_and_stays_withheld(self):
        url = "https://news.example/starship-14"
        card = (f"<div class='c'><a href='{url}'>Sep. 20 2026</a><h2><a href='{url}'>Starship soll Ende September 2026 "
                f"zum 14. Testflug starten</a></h2><p>Raumfahrt</p></div>"
                f"<div class='c'><a href='https://news.example/other'>Ein anderer Artikel</a></div>")
        v = AV.verify_list({"results": [{"title": "Sep. 20 2026", "url": url}]}, news_page([card]))
        d = v.decisions[0]
        self.assertEqual(d.doubt, "", d.why)
        m = FakeModel(pick="Starship")
        v = AV.Resolver(m)(v, {})
        self.assertEqual([k for k, _p in m.asked], ["choice"])       # never a yes / no on the answer
        self.assertEqual(v.verdict, AV.WITHHOLD, v.why())

    def test_a_title_naming_another_card_keeps_the_choice_question(self):
        cards = ["<div class='c'><a href='https://news.example/a'>Erster Artikel über Kaffee und Tee</a></div>",
                 "<div class='c'><a href='https://news.example/b'>Zweiter Artikel über Wasser</a></div>"]
        swapped = {"results": [{"title": "Zweiter Artikel über Wasser", "url": "https://news.example/a"},
                               {"title": "Erster Artikel über Kaffee und Tee", "url": "https://news.example/b"}]}
        v = AV.verify_list(swapped, news_page(cards))
        self.assertEqual([d.status for d in v.decisions if d.attr.startswith("title[")], [AV.CONFLICT, AV.CONFLICT],
                         v.why())
        m = FakeModel(pick="Erster")
        v = AV.Resolver(m)(v, {})
        self.assertEqual({k for k, _p in m.asked}, {"choice"})
        self.assertEqual(v.verdict, AV.WITHHOLD, v.why())


# --- fix 4: the paged / filtered hooks -----------------------------------------------------------------------------
class On:
    """The setting on, the default resolver a model-less one (open attributes are withheld) for one block."""

    def __enter__(self):
        self.old = os.environ.get(AV.SETTING)
        os.environ[AV.SETTING] = "1"
        self.saved = dict(AV._DEFAULT)
        AV._DEFAULT["r"] = AV.Resolver(None)
        return self

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop(AV.SETTING, None)
        else:
            os.environ[AV.SETTING] = self.old
        AV._DEFAULT.clear()
        AV._DEFAULT.update(self.saved)


def result_page(rows):
    dom = "<html><body><main><h1>Suche</h1>" + "".join(
        f"<div class='r'><h2><a href='{u}'>{t}</a></h2><p>Ein Ausschnitt</p></div>" for t, u in rows) + "</main></body></html>"
    return AV.Page(dom, "", "https://blog.example/?s=Orte")


class PagedHook(unittest.TestCase):
    P1 = [("Lofoten Tipps für den Sommer", "https://blog.example/lofoten"), ("Ostsee Tipps", "https://blog.example/ostsee")]
    P2 = [("Andalusien Tipps", "https://blog.example/andalusien"), ("Tessin Tipps", "https://blog.example/tessin")]

    def answer(self, rows):
        return {"results": [{"title": t, "url": u, "page": p} for p, t, u in rows], "total": None, "pages": 2}

    def test_each_row_is_verified_on_its_own_page(self):
        ans = self.answer([(1, *self.P1[0]), (1, *self.P1[1]), (2, *self.P2[0]), (2, *self.P2[1])])
        pages = {1: result_page(self.P1), 2: result_page(self.P2)}
        v = AV.verify_paged(ans, pages, None, ["blog.example"])
        self.assertEqual(v.verdict, AV.ACCEPT, v.why())
        self.assertEqual(sorted(d.attr for d in v.decisions if d.attr.startswith("title[")),
                         ["title[0]", "title[1]", "title[2]", "title[3]"])
        # a page-2 row checked against page 1 is never verified
        v = AV.verify_paged(ans, {1: result_page(self.P1), 2: result_page(self.P1)}, None, ["blog.example"])
        self.assertEqual(v.verdict, AV.WITHHOLD, v.why())

    def test_a_row_whose_page_was_not_kept_is_uncertain(self):
        ans = self.answer([(1, *self.P1[0]), (2, *self.P2[0])])
        v = AV.verify_paged(ans, {1: result_page(self.P1)}, None, ["blog.example"])
        self.assertEqual(v.verdict, AV.WITHHOLD, v.why())
        self.assertIn("page 2 of the answer was not kept", v.why())

    def test_an_off_scope_promo_row_is_withheld(self):
        promo = ("Ortea Palace Spa", "https://pauschalreisen.blog-partner.example/angebote?ref=search_results_direct_name")
        ans = self.answer([(1, *promo), (1, *self.P1[0])])
        v = AV.verify_paged(ans, {1: result_page([promo, self.P1[0]])}, None, ["blog.example"])
        self.assertEqual(v.verdict, AV.WITHHOLD, v.why())

    def test_the_paged_runner_verifies_with_the_setting_on_and_is_unchanged_off(self):
        import test_demo_breadth_paged as TP
        shop = TP.Shop()
        tool = TP.C.compile_trace(TP.paged_trace(shop))["tool"]
        os.environ[AV.SETTING] = "off"                  # on by default
        off = TP.run(shop, tool, "Bosch")
        self.assertNotIn("attribute_verify", off["checks"])
        with On():
            on = TP.run(shop, tool, "Bosch")
        av = on["checks"]["attribute_verify"]
        self.assertEqual((on["status"], av["verdict"]), ("ok", AV.ACCEPT), av.get("why"))
        self.assertEqual(sum(1 for a in av["attributes"] if a["attr"].startswith("title[")), 5)
        self.assertEqual(on["output"], off["output"])

    def test_the_filtered_runner_verifies_its_refined_page(self):
        import test_demo_breadth_filtered as TF
        shop = TF.Shop()
        tool = TF.C.compile_trace(TF.filtered_trace(shop), version=10)["tool"]
        os.environ[AV.SETTING] = "off"                  # on by default
        off = TF.run(shop, tool, "Bosch")
        self.assertNotIn("attribute_verify", off["checks"])
        with On():
            on = TF.run(shop, tool, "Bosch")
        self.assertIn("attribute_verify", on["checks"])
        self.assertIn(on["checks"]["attribute_verify"]["verdict"], (AV.ACCEPT, AV.WITHHOLD))



class ContentHeadingsAreNotTheCardsName(unittest.TestCase):
    """www.multicom.no: a product card whose own title link names it in full also holds an <h3>
    review quote in its description; the quote was read as the card's heading and the title withheld."""

    URL = "https://shop.example/multicom-v360u/cat-p/c/p1"
    TITLE = 'Multicom V360U 16" QHD+ 240Hz Intel Core Ultra 9 275HX, 32GB DDR5 RAM'

    def page(self, card):
        other = "<div class='item'><section><a href='https://shop.example/other/cat-p/c/p2'>ASUS GT 730</a></section></div>"
        return news_page([f"<div class='item'>{card}</div>", other])

    def test_a_review_quote_heading_does_not_withhold_the_title_link(self):
        card = (f"<section class='name'><a href='{self.URL}'><span>Multicom</span> V360U 16\" QHD+ 240Hz "
                f"<span>Intel Core Ultra 9 275HX, 32GB DDR5 RAM</span></a></section><div class='description'>"
                f"<h3>Best i test på Tek.no i oktober 2025: Kraftig, god og fremtidsrettet.</h3><ul><li>16\" QHD+</li>"
                f"</ul></div>")
        d = AV.verify_list({"results": [{"title": self.TITLE, "url": self.URL}]}, self.page(card)).decisions[0]
        self.assertEqual(d.status, AV.VERIFIED, d.why)

    def test_a_heading_still_names_the_card_when_the_answer_is_not_its_link_text(self):
        # the answered "title" is a snippet line, the card's link and heading name the product: still withheld
        card = (f"<h3>Multicom V360U Laptop</h3><a href='{self.URL}'>Multicom V360U Laptop</a>"
                f"<p>Intel Core Ultra 9 275HX und 32GB DDR5 RAM im Test</p>")
        d = AV.verify_list({"results": [{"title": "Intel Core Ultra 9 275HX und 32GB DDR5 RAM im Test", "url": self.URL}]},
                           self.page(card)).decisions[0]
        self.assertNotEqual(d.status, AV.VERIFIED, d.why)

    def test_an_image_only_link_keeps_the_heading_as_the_name(self):
        # no link text names the card: its (unlinked) heading does, as before
        card = (f"<a href='{self.URL}'><img src='/i.jpg' alt=''></a><h3>Multicom V360U Laptop</h3><p>Ein Satz</p>")
        d = AV.verify_list({"results": [{"title": "Ein Satz über den Laptop", "url": self.URL}]},
                           self.page(card)).decisions[0]
        self.assertNotEqual(d.status, AV.VERIFIED, d.why)

if __name__ == "__main__":
    unittest.main()
