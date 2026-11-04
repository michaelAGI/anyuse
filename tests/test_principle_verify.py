"""A PROOF MUST BE ABLE TO FAIL. Four accepted wrong answers, typed:

* www.cyclesuk.com: a search URL the site ignored listed the same items for every value, and the proof passed on
  words of that same list. The compile's proof now compares every proof value's results with the build value's
  (``harness_compile.build_results``) and with each other (``same_results``): the same first results for two values is
  ``not_query_dependent`` - never ready. The verifier, independently, needs the page to relate the list to ITS value
  (``attribute_verify.verify_query``: a heading, the title, the result-count line, the search field the page rendered,
  a URL the site made - never one the tool built - or an answered result naming it); unknown is uncertain, final.
* www.cyclesuk.com 'saddle': ``no_results`` was read from the cart badge "0" on one line and the heading
  "Search Results" on the next. The statement is read inside ONE line, and a line stating a positive result count means
  the search is not empty (``demo_compile.no_results_statement``); the verifier reads an empty search itself
  (``attribute_verify.verify_empty``: the route used to accept it unread).
* www.gamezone.de: the "Meinst du" / similar-searches links (back to the site's own search) were compiled as the
  results. Such links are never results - in the extractor (``demo_compile.search_endpoints`` / ``search_link``: the
  compile refuses a demonstration whose results are all such links, the result page drops them) and, independently, in
  the verifier (``attribute_verify.verify_search_links``: a CONFLICT, final).

Offline, on the fake shop of ``test_repair_ladder`` (no browser, no network, no model).

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_principle_verify.py
"""
import asyncio
import html
import os
import sys
import urllib.parse

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from test_repair_ladder import BASE, PRODUCTS, SITE, Shop, eur, trace  # noqa: E402

Q = "Akkuschrauber"


class IgnoringShop(Shop):
    """A search that IGNORES its value: every query lists every product (www.cyclesuk.com's unfiltered page)."""

    def hits(self, q):
        return list(PRODUCTS)


class SuggestShop(Shop):
    """A result page that also lists "Meinst du" links back to the site's own search (www.gamezone.de)."""

    def results(self, q):
        out = super().results(q)
        sugg = "".join(f"<li><a href=\"/search?q={urllib.parse.quote(w)}\">{html.escape(w)}</a></li>"
                       for w in (q + " Set", q.lower(), "Akku", "Schrauber"))
        return out.replace("<main>", f"<main><div class=\"didyoumean\">Meinst du: <ul>{sugg}</ul></div>", 1)


def compiled(site, value=Q):
    tr = trace(site, value, "site_search")
    g = C.compile_trace(tr, version=12)
    assert g["ok"], g.get("why")
    return g, tr


# ----------------------------------------------------------------------------------------------- query dependence

def test_a_search_that_ignores_its_value_is_not_query_dependent_and_never_ready():
    shop = IgnoringShop()
    g, tr = compiled(shop)
    base = HC.build_results({"demo": tr, "tool": g["tool"], "report": g["report"]})
    assert len(base) >= 2
    p = HC.prove_web(g["tool"], shop, ["Makita", "Bosch"], demonstrated=Q, baseline=base)
    assert not p["passed"] and p["typed"] == HC.NOT_QUERY_DEPENDENT, p["reason"]
    assert p["reason"].startswith("value 'Makita': not_query_dependent: the results for 'Makita' are the results for")
    # without the build list, the two proof values still give the same list: the second one fails
    p = HC.prove_web(g["tool"], shop, ["Makita", "Bosch"], demonstrated=Q)
    assert not p["passed"] and p.get("typed") == HC.NOT_QUERY_DEPENDENT and "'Bosch'" in p["reason"], p["reason"]


def test_an_honest_search_still_proves():
    shop = Shop()
    g, tr = compiled(shop)
    base = HC.build_results({"demo": tr, "tool": g["tool"], "report": g["report"]})
    p = HC.prove_web(g["tool"], shop, ["Makita", "Bosch"], demonstrated=Q, baseline=base)
    assert p["passed"] and not p.get("typed"), p["reason"]


def test_same_results_rules():
    a = [f"{BASE}/p/{x}" for x in "abcdef"]
    assert HC.same_results(a, a[:3]) == 3                         # the shorter list's first results, in order
    assert HC.same_results(a, list(reversed(a[:5]))) == 0           # another order is another list (ordered rule)
    assert HC.same_results(a[:5], list(reversed(a[:5])), "set") == 5
    assert HC.same_results(a, [f"{BASE}/p/z"] + a[1:5]) == 0
    assert HC.same_results([], a) == 0 and HC.same_results(a, []) == 0
    assert HC.same_results([u + "#x" for u in a[:3]], a[:3]) == 3   # the fragment is no difference
    # one or two shared first results are no list (measured: a single hit that also leads the build list)
    assert HC.same_results(a[:1], a) == 0 and HC.same_results(a[:2], a) == 0


def test_the_same_list_is_allowed_when_every_shared_result_names_both_values():
    rows = [{"url": f"{BASE}/news/zelda-ocarina-of-time-{i}/", "title": f"The Legend of Zelda: Ocarina of Time {i}"}
            for i in range(5)]
    assert HC.names_both(rows, "Zelda", "Ocarina")
    assert not HC.names_both(rows, "helmet", "Headset")
    # the same list for the build value and a proof value that every shared result names: no failure
    def same_rows(tool, site, value):
        return {"status": "ok", "model_calls": 0, "why": "query: fine", "output": {"results": rows}, "final_page": None}
    p = HC.prove_web({"workflow": "site_search"}, None, ["Ocarina"], demonstrated="Zelda", replay=same_rows,
                     independent=False, baseline=rows, n=1)
    assert p["passed"] and "each naming both values" in p["cases"][0]["query_dependence"], p["reason"]
    p = HC.prove_web({"workflow": "site_search"}, None, ["Majora"], demonstrated="Zelda", replay=same_rows,
                     independent=False, baseline=rows, n=1)
    assert not p["passed"] and p["typed"] == HC.NOT_QUERY_DEPENDENT


def test_the_verifier_needs_the_page_to_relate_the_list_to_its_value():
    rows = {"results": [{"title": "Headset Bearing Races", "url": f"{BASE}/p/a"}]}
    page = AV.Page(f"<html><body><h1>Search Results</h1><ul><li><a href=\"{BASE}/p/a\">Headset Bearing Races</a></li>"
                   f"</ul></body></html>", "Search Results\nHeadset Bearing Races", f"{BASE}/search?q=tyre")
    v = AV.verify_query(rows, page, "tyre", built=True)
    d = v.decisions[0]
    assert d.status == AV.UNCERTAIN and d.final and d.critical, d.why
    # the URL counts only when the SITE made it (a typed search), never when the tool built it
    assert AV.verify_query(rows, page, "tyre", built=False).decisions[0].status == AV.VERIFIED
    # a heading, the title, the search field the page rendered, or a results line naming the value
    for dom in (f"<h1>Ergebnisse für tyre</h1>", f"<title>tyre - Suche</title>",
                f"<form><input type=\"search\" name=\"q\" value=\"tyre\"></form>"):
        pg = AV.Page(f"<html><head></head><body>{dom}<a href=\"{BASE}/p/a\">Headset</a></body></html>", "x",
                     f"{BASE}/search?q=tyre")
        assert AV.verify_query(rows, pg, "tyre", built=True).decisions[0].status == AV.VERIFIED, dom
    # an answered result naming the value is the answer vouching for itself - never enough alone
    named = {"results": [{"title": "Schwalbe tyre 28 inch", "url": f"{BASE}/p/a"}]}
    d = AV.verify_query(named, page, "tyre", built=True).decisions[0]
    assert d.status == AV.UNCERTAIN and d.final and "cannot vouch for itself" in d.why, d.why
    # in check(): the value switches it on; an uncertain query withholds and is final (no model turns it)
    v = AV.check("site_search", rows, page, AV.Resolver(None), value="tyre", built_url=True)
    assert v.verdict == AV.WITHHOLD and any(d.attr == "query" and d.final for d in v.decisions)
    assert AV.built_url({"search": {"mode": "url"}}) and not AV.built_url({"search": {"mode": "typed"}})
    # a value of short words only ("AI") is named as whole words (www.version2.dk) - by the page's heading
    ai_page = AV.Page(f"<html><body><h1>Søgeresultater for AI</h1><ul><li><a href=\"{BASE}/p/a\">Headset Bearing "
                      f"Races</a></li></ul></body></html>", "Søgeresultater for AI\nHeadset Bearing Races",
                      f"{BASE}/search?q=AI")
    assert AV.verify_query(rows, ai_page, "AI", built=True).decisions[0].status == AV.VERIFIED
    hair = AV.Page(f"<html><body><h1>Søgeresultater for hair</h1><ul><li><a href=\"{BASE}/p/a\">Headset Bearing "
                   f"Races</a></li></ul></body></html>", "Søgeresultater for hair\nHeadset Bearing Races", f"{BASE}/s")
    assert AV.verify_query(rows, hair, "AI", built=True).decisions[0].status == AV.UNCERTAIN


# ----------------------------------------------------------------------------------------------------- no results

def test_an_empty_search_needs_one_line_that_states_it():
    cyclesuk = "0% Finance Available\nClothing & Helmets\n0\n Search Results\nHome   Search Results\nShowing:\n1 - 30 of 7045 items"
    assert C.no_results_statement(cyclesuk) == ""
    assert C.no_results_statement("Suche\nKeine Ergebnisse gefunden.\nBeliebt") == "keine ergebnisse"
    assert C.no_results_statement("Search\nSorry, no results found for saddle") == "no results found"
    # a positive result count on any line: the search is not empty
    assert C.no_results_statement("No results for 'saddel'\nShowing results for saddle: 12 products") == ""
    assert C.positive_count_line(["Warenkorb 3 Artikel", "Zu \"Schleifstein\" wurden 79 Artikel gefunden!"]).startswith("Zu")
    assert C.positive_count_line(["Seite 1 von 3", "0 results"]) == ""


def test_the_verifier_reads_an_empty_search_itself():
    cyc = AV.Page("", "0\nSearch Results\n1 - 30 of 7045 items", f"{BASE}/search?q=saddle")
    v = AV.verify_empty(cyc, "saddle")
    assert v.verdict == AV.WITHHOLD and v.decisions[0].final, v.why()
    assert AV.verify_empty(AV.Page("", "Search Results\n0\nRadsport", ""), "saddle").verdict == AV.WITHHOLD
    assert AV.verify_empty(AV.Page("", "Suche\nLeider keine Ergebnisse für saddle", ""), "saddle").verdict == AV.ACCEPT

    class FakePage:
        def __init__(self, text):
            self._text = text

        async def dom(self):
            return "<html><body></body></html>"

        async def text(self):
            return self._text

        async def url(self):
            return f"{BASE}/search?q=saddle"
    tool = {"workflow": "site_search", "site": SITE, "search": {"mode": "url"}}
    out = {"status": "no_results", "output": None, "why": "empty_state"}
    got = asyncio.run(AV.verify_run(FakePage("0\nSearch Results\n1 - 30 of 7045 items"), tool, "saddle", dict(out)))
    assert got["status"] == "incomplete" and got["accepted"] is False and got["unverified_output"] == {"no_results": True}
    got = asyncio.run(AV.verify_run(FakePage("Leider keine Ergebnisse"), tool, "saddle", dict(out)))
    assert got["status"] == "no_results" and got["checks"]["attribute_verify"]["verdict"] == AV.ACCEPT


# --------------------------------------------------------------------------------------------------- search links

def test_search_endpoints_and_links():
    eps = C.search_endpoints(["https://www.gamezone.de/Suche/?strSearch={q}"],
                             "https://www.gamezone.de/Suche/?strSearch=Elden+Ring", "Elden Ring")
    assert eps == [("www.gamezone.de", "/suche", "strSearch")]
    assert C.search_link("https://www.gamezone.de/Suche/?strSearch=Elden%20Ring:%20Nightreign", eps)
    assert C.search_link("https://gamezone.de/suche?strSearch=playstation%205", eps)
    assert not C.search_link("https://www.gamezone.de/elden-ring/news/patch-123/", eps)
    assert not C.search_link("https://www.gamezone.de/Suche/?page=2", eps)          # no search text: not a search link
    path = C.search_endpoints(["https://www.x.test/suche/{q}"])
    assert path == [("www.x.test", "/suche/", "")] and C.search_link("https://www.x.test/suche/other", path)
    assert not C.search_link("https://www.x.test/suche/", path)
    assert C.search_endpoints(["https://www.x.test/{q}"]) == []                     # the whole path is the value: none


def test_the_compile_refuses_results_that_are_links_to_the_sites_own_search():
    shop = SuggestShop()
    tr = trace(shop, Q, "site_search")
    p1 = tr["steps"][1]["page"]
    sugg = [e for e in p1["elements"] if e["tag"] == "a" and "/search?q=" in (e.get("href") or "")]
    assert len(sugg) == 4
    tr["result"] = {"results": [{"title": e["text"], "url": e["href"]} for e in sugg]}
    tr["steps"][1]["action"]["result"] = tr["result"]
    g = C.compile_trace(tr, version=12)
    assert not g["ok"] and g["why"].startswith("search_links:"), g.get("why")
    # mixed: the search links are dropped, the real results compile
    real = trace(shop, Q, "site_search")["result"]["results"]
    tr["result"] = {"results": [{"title": e["text"], "url": e["href"]} for e in sugg[:1]] + real}
    g = C.compile_trace(tr, version=12)
    assert g["ok"], g.get("why")
    assert g["report"]["search_links"] == [sugg[0]["href"]]


def test_the_result_page_drops_links_to_the_sites_own_search():
    shop = SuggestShop()
    g, _tr = compiled(shop)
    tool = dict(g["tool"])
    # a tool whose region is the suggestion list (compiled before the search-link rule): its items are all search links
    tool["region"] = {"kind": "cluster", "template": f"{BASE}/search?q={{v}}", "where": "query", "width": 1,
                      "regex": C.regex_of_template(f"{BASE}/search?q={{v}}", 1)}
    rec, u = shop.route(f"{BASE}/search?q={Q}")
    read = shop.read(rec, u)
    j = C.judge_result_page(tool, read, Q)
    assert j["status"] == "failed" and j.get("search_links") and j["n_items"] == 0, j


def test_the_verifier_refuses_answered_links_to_the_sites_own_search():
    shop = SuggestShop()
    rec, u = shop.route(f"{BASE}/search?q={Q}")
    page = AV.Page(rec.dom, "Meinst du:\nAkkuschrauber Set", u)
    bad = {"results": [{"title": "Akkuschrauber Set", "url": f"{BASE}/search?q=Akkuschrauber%20Set"}]}
    v = AV.verify_search_links(bad, page, Q)
    assert v.verdict == AV.WITHHOLD and v.decisions[0].status == AV.CONFLICT and v.decisions[0].final
    good = {"results": [{"title": PRODUCTS[0][0], "url": f"{BASE}/p/{PRODUCTS[0][2]}"}]}
    assert all(d.status == AV.VERIFIED for d in AV.verify_search_links(good, page, Q).decisions)
    # the page's own GET search form is an endpoint too (a POST search's result page carries no value in its URL)
    form = AV.Page("<html><body><form action=\"/finden\" method=\"get\"><input type=\"text\" name=\"suchbegriff\"></form>"
                   "</body></html>", "", f"{BASE}/ergebnis")
    assert AV.is_search_link(f"{BASE}/finden?suchbegriff=x", AV.search_ends(form))

# ------------------------------------------------------------------------------------------ a row without a URL

def test_a_row_without_a_url_needs_card_evidence_in_the_verifier():
    links = "".join(f'<li class="card"><a href="{BASE}/p/{i}">Kochmesser {i}</a><span>Stahl</span></li>' for i in range(3))
    dom = (f'<html><body><main><ul class="results">{links}<li class="card"><h3>Kochmesser ohne Link</h3><p>Damast, 20 cm'
           f'</p></li></ul><section><h2>Get smarter</h2><a href="{BASE}/guide">Ratgeber</a></section></main></body></html>')
    page = AV.Page(dom, "x", f"{BASE}/search?q=Kochmesser")
    rows = [{"title": f"Kochmesser {i}", "url": f"{BASE}/p/{i}"} for i in range(3)]
    ok = AV.verify_list({"results": rows + [{"title": "Kochmesser ohne Link", "url": "", "link": "none"}]}, page)
    assert ok.decisions[3].status == AV.VERIFIED, ok.decisions[3].why
    bad = AV.verify_list({"results": rows + [{"title": "Get smarter", "url": "", "link": "none"}]}, page)
    d = bad.decisions[3]
    assert d.status == AV.UNCERTAIN and d.final and "no row" in d.why, d.why
    # a row that lacks the URL its own (linked) card has is still a result of the list (measured: gartendialog's
    # duplicated rows, whose cards show only the title)
    dup = AV.verify_list({"results": [{"title": "Kochmesser 1", "url": ""}] + rows}, page)
    assert dup.decisions[0].status == AV.VERIFIED, dup.decisions[0].why
    # an item without a link that shows only its title is a heading, never a card
    lone = (f'<html><body><main><ul class="results">{links}<li class="card"><h3>Mehr entdecken</h3></li></ul></main>'
            f'</body></html>')
    v = AV.verify_list({"results": rows + [{"title": "Mehr entdecken", "url": "", "link": "none"}]},
                       AV.Page(lone, "x", f"{BASE}/search?q=Kochmesser"))
    assert v.decisions[3].status == AV.UNCERTAIN, v.decisions[3].why


def test_the_verifier_reads_listing_pages_from_the_sites_navigation():
    nav = "".join(f'<a href="{BASE}/de/ct/{c}.htm">{c}</a>' for c in ("messer", "kochmesser", "taschenmesser", "scheren"))
    dom = (f'<html><body><header><nav>{nav}</nav></header><main><div class="groups">'
           + "".join(f'<div><a href="{BASE}/de/ct/{c}.htm">{c}</a></div>' for c in ("kochmesser-angebot", "kochmesserset"))
           + f'</div><article><header class="entry-header"><a href="{BASE}/de/pt/abc.htm">A</a></header></article></main>'
           f'</body></html>')
    page = AV.Page(dom, "x", f"{BASE}/search?q=Kochmesser")
    cats = {"results": [{"title": "Kochmesser Angebot", "url": f"{BASE}/de/ct/kochmesser-angebot.htm"},
                        {"title": "Kochmesserset", "url": f"{BASE}/de/ct/kochmesserset.htm"}]}
    v = AV.verify_listing(cats, page)
    assert v.decisions[0].status == AV.UNCERTAIN and v.decisions[0].final and "pattern" in v.decisions[0].why
    target = {"results": [{"title": "Kochmesser", "url": f"{BASE}/de/ct/kochmesser.htm"}]}
    assert "navigation links" in AV.verify_listing(target, page).decisions[0].why
    products = {"results": [{"title": "A", "url": f"{BASE}/de/pt/abc.htm"}]}
    assert AV.verify_listing(products, page).decisions[0].status == AV.VERIFIED
    # an article's own header is no navigation
    assert all("/de/pt/" not in u for u in AV.nav_links(page))
    # measured on all recordings and NOT adopted (44 / 66 correct answers held back): check() leaves it off
    assert AV.LISTING_CHECK is False
    v = AV.check("site_search", cats, page, AV.Resolver(None), value="Kochmesser", built_url=True)
    assert not any(d.attr == "listing" for d in v.decisions)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
