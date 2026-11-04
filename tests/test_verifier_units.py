"""The verifier's false accepts on one-off answers, each turned into a
general rule in the verifier's OWN reads (``attribute_verify``; independent of the extractor; doubt or disagreement
withholds). Pages built inline, no model, no network.

* www.espresso-international.de 'Siebträger': a single answered result with no list around it (a promo link
  in the page's top bar) was verified as "the first 1 of the page's own 1 result items" while the page's title stated
  187 results. A single result in no list is checked against the page's own count statement (``stated_count``:
  visible lines, then the title and h1): another count than one is UNCERTAIN, final.
* www.aquarium-ratgeber.com 'Guppy': an ad's price link inside a result card was answered as a result of
  its own; the order check left it out without a word and verified the rest. Every answered result must be placed on
  its OWN item of the page's list: one on no item is UNCERTAIN, two on one item a CONFLICT.
* www.tabakplatz.de 'Pfeife': the availability pill "sofort versandbereit" - the first text of each
  whole-card link - was answered as every title and verified as "beginning with what names the card". A text the
  page's OTHER result items show too (``label_titles``) is a label of the list, not a name: UNCERTAIN.
* www.inhaltsangabe.de 'Faust': the site sent the search to its home page; "Faust I" from its popular-works
  box was the only source relating the list to the value. The answered results never relate the list to its value
  alone; a heading inside an answered card is the answer's text; a results line ("Suchergebnisse für
  "Schleifstein"") is the page's own statement.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import attribute_verify as AV  # noqa: E402

BASE = "https://shop.example"


def page(body, title="Shop", url=f"{BASE}/search?q=x", text=None):
    dom = f"<html><head><title>{title}</title></head><body>{body}</body></html>"
    return AV.Page(dom, text if text is not None else "", url)


def text_of(p):
    return "\n".join(p.lines)


# ----------------------------------------------------------------------------------------- a single result in no list

def promo_page(title):
    return page(f'<div class="topbar"><p>13% auf <a href="{BASE}/products/gran-crema">Gran Crema</a></p></div>'
                f'<main id="MainContent"><h2>Top Probiersets</h2><p>Wählen Sie Ihre Option</p></main>', title=title)


def test_a_single_result_in_no_list_must_agree_with_the_pages_count():
    ans = {"results": [{"title": "Gran Crema", "url": f"{BASE}/products/gran-crema"}]}
    p = promo_page('Suche: 187 Ergebnisse gefunden für "Siebträger"')
    assert AV.stated_count(p)["count"] == 187
    d = AV.verify_order(ans, p).decisions[0]
    assert d.status == AV.UNCERTAIN and d.final and "states 187 results" in d.why, d.why
    assert AV.check("site_search", ans, p, AV.Resolver(None)).verdict == AV.WITHHOLD
    # the page states one result: verified as before; no statement at all: unchanged (verified)
    one = promo_page('Zu "Siebträger" wurde 1 Produkt gefunden')
    assert AV.verify_order(ans, one).decisions[0].status == AV.VERIFIED
    none = promo_page("Suche")
    assert AV.verify_order(ans, none).decisions[0].status == AV.VERIFIED
    # switched off: the old reading (a proof must be able to fail - the rule is what withholds)
    AV.ALONE_NEEDS_COUNT = False
    try:
        assert AV.verify_order(ans, p).decisions[0].status == AV.VERIFIED
    finally:
        AV.ALONE_NEEDS_COUNT = True


# ------------------------------------------------------------------------------------------- an ad link inside a card

NAMES = ["Endlers Guppy", "Guppy Poecilia reticulata", "Anfängerfische", "Platy", "Molly", "Schwertträger"]


def fish(k):
    return f"{BASE}/fische/{['endlers-guppy', 'guppy', 'anfaengerfische', 'platy', 'molly', 'schwerttraeger'][k]}/"


def guide_page(ad_in=0):
    # the page: an image column and a text column per result; the text column holds the ad's price link and a
    # "Weiterlesen" button to the result
    rows = []
    for k, n in enumerate(NAMES):
        ad = ('<div class="rcb-box-text-item"><span class="label">Preis:</span><span class="value"><strong><a href='
              '"https://www.partner.example/endler?sPartner=1">aktuell 3.99€ bei Partner *</a></strong></span></div>'
              '<div class="rcb-affi-hint">* Anzeige</div>') if k == ad_in else ""
        rows.append(f'<div class="row related-content-box"><div class="col-4 col-md-5 img-container"><a href="{fish(k)}">'
                    f'<img alt="{n}"></a></div><div class="col-8 col-md-7 text-container"><div class="h3">{n}</div>'
                    f'<div class="rcb-box-text-item"><span class="label">Größe:</span><span class="value">4 cm</span>'
                    f'</div>{ad}<p></p><a href="{fish(k)}" class="btn btn-secondary">Weiterlesen: {n}</a></div></div>')
    return page(f'<main><h1>Suchergebnisse für Guppy</h1><div class="results">{"".join(rows)}</div></main>',
                url=f"{BASE}/?s=Guppy", text="Suchergebnisse für Guppy\n" + "\n".join(NAMES))


AD = "https://www.partner.example/endler?sPartner=1"


def test_every_answered_result_is_placed_on_its_own_item():
    ok = {"results": [{"title": NAMES[k], "url": fish(k)} for k in range(3)]}
    assert AV.verify_order(ok, guide_page()).decisions[0].status == AV.VERIFIED
    assert AV.check("site_search", ok, guide_page(), AV.Resolver(None), value="Guppy").verdict == AV.ACCEPT
    # the ad's price link inside the first card answered as a result: the order check no longer leaves an answered
    # result out without a word - withheld with or without the tool's scope (with it, the scope check also says so)
    ad = {"results": [{"title": NAMES[0], "url": fish(0)}, {"title": "aktuell 3.99€ bei Partner *", "url": AD},
                      {"title": NAMES[1], "url": fish(1)}]}
    d = AV.verify_order(ad, guide_page()).decisions[0]
    assert d.status == AV.UNCERTAIN and "in none of the page's" in d.why, d.why
    assert AV.check("site_search", ad, guide_page(), AV.Resolver(None), value="Guppy").verdict == AV.WITHHOLD
    v = AV.check("site_search", ad, guide_page(), AV.Resolver(None), scope=["shop.example"], value="Guppy")
    assert v.verdict == AV.WITHHOLD and any(x.attr.startswith("scope") and x.status != AV.VERIFIED for x in v.decisions)
    AV.ORDER_PLACES_EVERY = False
    try:
        assert AV.verify_order(ad, guide_page()).decisions[0].status == AV.VERIFIED
    finally:
        AV.ORDER_PLACES_EVERY = True


def test_two_answered_results_on_one_item_conflict_and_one_on_none_is_uncertain():
    items = [{"urls": {"a.example/p/one", "a.example/p/one-v"}, "lead": "a.example/p/one", "listed": ["a.example/p/one"],
              "text": "One"},
             {"urls": {"a.example/p/two"}, "lead": "a.example/p/two", "listed": ["a.example/p/two"], "text": "Two"}]
    real = AV.result_items
    try:
        AV.result_items = lambda *a, **k: (items, "")
        two = {"results": [{"title": "One", "url": "https://a.example/p/one"}, {"title": "V", "url": "https://a.example/p/one-v"}]}
        d = AV.verify_order(two, page("<p>x</p>")).decisions[0]
        assert d.status == AV.CONFLICT and "one result item" in d.why, d.why
        off = {"results": [{"title": "One", "url": "https://a.example/p/one"}, {"title": "X", "url": "https://a.example/p/nine"}]}
        d = AV.verify_order(off, page("<p>x</p>")).decisions[0]
        assert d.status == AV.UNCERTAIN and "in none of the page's 2 result items" in d.why, d.why
        good = {"results": [{"title": "One", "url": "https://a.example/p/one"}, {"title": "Two", "url": "https://a.example/p/two"}]}
        assert AV.verify_order(good, page("<p>x</p>")).decisions[0].status == AV.VERIFIED
        AV.ORDER_PLACES_EVERY = False
        try:
            # the old reading let both through (the merge and the silent skip)
            assert AV.verify_order(two, page("<p>x</p>")).decisions[0].status == AV.VERIFIED
            assert AV.verify_order(off, page("<p>x</p>")).decisions[0].status == AV.VERIFIED
        finally:
            AV.ORDER_PLACES_EVERY = True
    finally:
        AV.result_items = real


# ------------------------------------------------------------------------------------------------ a label of the list

PRODUCTS = ["Eurojet Piezofeuerzeug Mattschwarz", "Euro Jet Pfeifenfeuerzeug Anthrazit", "Cool Pfeifenfeuerzeug Silber",
            "Cool Pfeifenfeuerzeug Gun", "Zippo Pfeifeneinsatz Benzin", "White Elephant Aktivkohlefilter"]


def pill_page():
    cards = "".join(f'<a class="art" href="{BASE}/p{k}" title="{n}"><div class="bild"><img alt="{n}">'
                    f'<span class="pill">sofort versandbereit</span></div><p class="name">{n}</p>'
                    f'<p class="preis">7,95 Euro</p></a>' for k, n in enumerate(PRODUCTS))
    return page(f'<main><h1>Suche: Pfeife</h1><p>77 Artikel gefunden</p><div class="track">{cards}</div></main>',
                url=f"{BASE}/suche/Pfeife")


def test_a_title_the_other_result_items_show_too_is_a_label():
    p = pill_page()
    labels = {"results": [{"title": "sofort versandbereit", "url": f"{BASE}/p{k}"} for k in range(4)]}
    v = AV.verify_list(labels, p)
    assert all(d.status == AV.UNCERTAIN and "label of the list" in d.why for d in v.decisions), \
        [d.why for d in v.decisions]
    assert AV.check("site_search", labels, p, AV.Resolver(None), value="Pfeife").verdict == AV.WITHHOLD
    # the products' own names: verified
    names = {"results": [{"title": PRODUCTS[k], "url": f"{BASE}/p{k}"} for k in range(4)]}
    assert all(d.status == AV.VERIFIED for d in AV.verify_list(names, p).decisions)
    # one label answered alone is still a label (the page's other items show it)
    one = {"results": [{"title": "sofort versandbereit", "url": f"{BASE}/p0"}]}
    assert AV.verify_list(one, p).decisions[0].status == AV.UNCERTAIN
    AV.LABEL_CHECK = False
    try:
        assert all(d.status == AV.VERIFIED for d in AV.verify_list(labels, p).decisions)
    finally:
        AV.LABEL_CHECK = True


def test_two_results_of_one_name_are_not_a_label():
    # the same company listed twice (www.elektro.net): a name two items share is no label of the list
    names = ["A. + H. Meyer GmbH", "A. + H. Meyer GmbH", "Bauer Elektro", "Clemens Licht", "Dorn Technik"]
    cards = "".join(f'<li><h3><a href="{BASE}/firma/{k}/">{n}</a></h3><p>Ort {k}</p></li>' for k, n in enumerate(names))
    p = page(f'<main><h1>Firmen: Meyer</h1><ul class="hits">{cards}</ul></main>')
    ans = {"results": [{"title": n, "url": f"{BASE}/firma/{k}/"} for k, n in enumerate(names[:3])]}
    assert AV.label_titles(p, ans["results"]) == {}
    # three items showing it make it a label
    p3 = page(f'<main><h1>Firmen: Meyer</h1><ul class="hits">'
              + "".join(f'<li><h3><a href="{BASE}/firma/{k}/">{n}</a></h3><p>Ort {k}</p></li>'
                        for k, n in enumerate(["A. + H. Meyer GmbH"] * 3 + names[2:])) + '</ul></main>')
    assert AV.label_titles(p3, ans["results"][:1]) == {0: 2}


# --------------------------------------------------------------------------------- the answer cannot vouch for itself

def home_page(heading_in_card=False):
    works = ["Faust I", "Der Prozess", "Die Verwandlung", "Maria Stuart"]
    lis = "".join((f'<li><h3><a href="{BASE}/w/{k}/">{w}</a></h3></li>' if heading_in_card else
                   f'<li><a href="{BASE}/w/{k}/"><span>{w}</span></a></li>') for k, w in enumerate(works))
    return page(f'<main><h2>Beliebte Werke</h2><ol>{lis}</ol></main>', title="Inhaltsangaben", url=f"{BASE}/")


def test_the_answer_cannot_vouch_for_its_own_value():
    ans = {"results": [{"title": "Faust I", "url": f"{BASE}/w/0/"}]}
    for hp in (home_page(), home_page(heading_in_card=True)):
        d = AV.verify_query(ans, hp, "Faust", built=True).decisions[0]
        assert d.status == AV.UNCERTAIN and d.final and "cannot vouch for itself" in d.why, d.why
        assert AV.check("site_search", ans, hp, AV.Resolver(None), value="Faust", built_url=True).verdict == AV.WITHHOLD
    # a results line of the page itself relates the list to the value (www.knivesandtools.de)
    lined = page(f'<nav><a href="/">Home</a> <span>Suchergebnisse für "Faust"</span></nav><main><h1>Suchergebnisse</h1>'
                 f'<ol><li><a href="{BASE}/w/0/">Faust I</a></li><li><a href="{BASE}/w/9/">Faust II</a></li></ol></main>',
                 text='Home\nSuchergebnisse für "Faust"\nSuchergebnisse\nFaust I\nFaust II')
    assert AV.results_line(lined, "Faust", ["Faust I"]) == 'Suchergebnisse für "Faust"'
    d = AV.verify_query(ans, lined, "Faust", built=True).decisions[0]
    assert d.status == AV.VERIFIED and "results line" in d.why, d.why
    # a line that IS an answered title is the answer's, not the page's
    assert AV.results_line(lined, "Faust", ['Suchergebnisse für "Faust"']) == ""
    assert AV.results_line(lined, "Faust", ["Faust"]) == 'Suchergebnisse für "Faust"'
    AV.QUERY_PAGE_ONLY, AV.QUERY_HEAD_OUTSIDE_CARDS = False, False
    try:
        assert AV.verify_query(ans, home_page(), "Faust", built=True).decisions[0].status == AV.VERIFIED
    finally:
        AV.QUERY_PAGE_ONLY, AV.QUERY_HEAD_OUTSIDE_CARDS = True, True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
