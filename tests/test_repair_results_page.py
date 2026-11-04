"""Ladder-only: a repaired structure reads result cards only on the SEARCH's own
result page (``repair_ladder.off_search_page``, ``LadderConfig(results_page=True)``), offline on fakes built from
the live run's saved pages (no browser, no network, no model).

The false accept (www.neuhauschocolates.com): the site's search for "Neuhaus Irrésistibles Collection"
redirected to a campaign page (``/en_BE/65-years/irresistibles.html``); tier 1 re-derived "result cards" from the links
naming the input there - a button and a product tile of a content block (``div.region > div.experience-component``) -
and the checker agreed; the reader: "the product appears only in a recommendations block, not a search result list".

What must hold:

1. ``off_search_page``: the campaign page is not the search's result page; the search's own URL is (query
   templates, path templates with the input in the path - www.ifun.de's ``/suche/{q}``, www.weser-kurier.de's
   ``/suche/{q}/all/all/``); a search that landed on the answered product's own page is not refused; a typed search
   with no recorded URL is never refused;
2. through the ladder: the fake shop's search for one input redirects to a campaign page whose content block links
   the product - with the fix OFF tier 1 re-derives the cards there and the gate accepts (the live false accept);
   with it ON the re-derived structure is refused and nothing is accepted; a real drift (renamed result classes on
   the search page) is still repaired with the fix ON.
"""
import html
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import BASE, PRODUCTS, RENAMED, Shop, Store, compiled, eur, ladder_run  # noqa: E402

NEU = "https://www.neuhauschocolates.com"
NEU_TOOL = {"workflow": "product_lookup", "search": {
    "mode": "url", "template": NEU + "/en_BE/search?q={q}&search-button=&lang=en_BE",
    "fallback_templates": [NEU + "/en_BE/search?q={q}&search-button=&lang=en_BE",
                           NEU + "/en_BE/search?q={q}&search-button=&lang=null"]}}


def test_off_search_page_on_the_rounds_urls():
    assert L.off_search_page(NEU_TOOL, NEU + "/en_BE/65-years/irresistibles.html")
    assert L.off_search_page(NEU_TOOL, NEU + "/en_BE/search?q=Neuhaus+Irr%C3%A9sistibles&search-button=&lang=en_BE") == ""
    prod = NEU + "/en_BE/shop/neuhaus-irresistibles-collection/5024181.html"
    assert L.off_search_page(NEU_TOOL, prod, {"output": {"url": prod}}) == ""          # landed on the product
    assert L.off_search_page(NEU_TOOL, prod, {"output": {"url": prod + "x"}})          # another page
    ifun = {"search": {"mode": "url", "template": "https://www.ifun.de/suche/{q}"}}
    assert L.off_search_page(ifun, "https://www.ifun.de/suche/Xbox") == ""
    assert L.off_search_page(ifun, "https://www.ifun.de/suche/Olrinnix%20Jilnixzeb") == ""
    wk = {"search": {"mode": "url", "template": "https://www.weser-kurier.de/suche/{q}/all/all/"}}
    assert L.off_search_page(wk, "https://www.weser-kurier.de/suche/festakt/all/all/") == ""
    assert L.off_search_page(wk, "https://www.weser-kurier.de/thema/festakt") != ""
    assert L.off_search_page({"search": {"mode": "typed", "start_url": NEU + "/"}}, NEU + "/x.html") == ""
    assert L.LadderConfig().results_page is True                  # ON by default


LANDING = "/aktion/akku-wochen.html"


class LandingShop(Shop):
    """The shop's search for "Metabo ..." redirects to a campaign page; its content block (the live shape) links the
    product with a button and a product tile."""

    def landing(self):
        n, pr, slug = PRODUCTS[2]
        block = (f"<div class=\"region region_landscape-small\"><div class=\"experience-component experience-custom_"
                 f"assets-button\"><div class=\"btn-container text-center\"><a class=\"btn btn-primary\" href=\"/p/{slug}\">"
                 f"{html.escape(n)} entdecken</a></div></div><div class=\"experience-component experience-tile\"><figure>"
                 f"<figcaption><div class=\"product-tile-body\"><div class=\"pdp-link d-flex\"><a class=\"productname\" "
                 f"href=\"/p/{slug}\">{html.escape(n)}</a></div><span>{eur(pr)}</span></div></figcaption></figure></div>"
                 f"</div>")
        return (f"<html><head><title>Akku-Wochen</title></head><body><header><nav><a href=\"/\">Start</a></nav></header>"
                f"<main><h1>Akku-Wochen</h1><p>Unsere Akkuschrauber im Angebot - entdecken Sie die Kampagne.</p>{block}"
                f"</main></body></html>")

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path == "/search" and "metabo" in str(q.get(self.qkey) or "").lower():
            u = BASE + LANDING
            return D.Recorded(u, self.landing()), u
        if sp.path == LANDING:
            return D.Recorded(url, self.landing()), url
        return super().route(url)


AGREE = lambda t, v, o: {"verdict": "agrees", "why": "fake: the render shows the product"}   # noqa: E731


def test_the_round_false_accept_is_refused():
    tool = compiled()
    v = PRODUCTS[2][0]
    st = Store(tool)
    try:
        off = L.LadderConfig(enabled=True, tier3=False, results_page=False)
        out = ladder_run(LandingShop(), tool, v, st.s, config=off, checker=AGREE)
        assert out["ladder"]["outcome"] == L.REPAIRED and out["ladder"]["tier"] == 1, out["ladder"]["line"]
        assert out["checks"]["result_page"]["url"].endswith(LANDING)                   # read off the campaign page
    finally:
        st.close()
    st = Store(tool)
    try:
        on = L.LadderConfig(enabled=True, tier3=False, results_page=True)
        out = ladder_run(LandingShop(), tool, v, st.s, config=on, checker=AGREE)
        assert out["status"] != "ok" and out["ladder"]["outcome"] != L.REPAIRED, out["ladder"]["line"]
        assert st.s.active_rev() == 0 and len(st.s.revisions()) == 1
        ev = out["ladder"]["evidence"]
        assert any("no result list" in str(a.get("why") or "") for a in ev["attempts"]), ev["attempts"]
    finally:
        st.close()
    # a real drift on the search page is still repaired with the fix on
    st = Store(tool)
    try:
        on = L.LadderConfig(enabled=True, tier3=False, results_page=True)
        assert ladder_run(Shop(), tool, PRODUCTS[0][0], st.s, config=on)["status"] == "ok"
        out = ladder_run(Shop(cls=RENAMED), tool, PRODUCTS[0][0], st.s, config=on)
        assert out["ladder"]["outcome"] == L.REPAIRED and out["status"] == "ok", out["ladder"]["line"]
    finally:
        st.close()


def test_a_patch_that_moves_the_search_is_judged_after_its_run():
    """On 3trolle.pl and www.careelite.de tier 1 combined new result cards with a
    search re-bound to the site's recorded fallback search; the check before the run read the OLD search's probe page
    and refused the patch - the new structure would have read another page. Now only a patch that keeps the search is
    judged on the probe page; one that moves it is judged on its own run's result page (the check after the run)."""
    trolle = {"search": {"mode": "url", "template": "https://3trolle.pl/jolisearch?s={q}",
                         "fallback_templates": ["https://3trolle.pl/szukaj?search_query={q}"]},
              "region": {"cards": [{"kind": "card", "chain": ["div.old"]}]}}
    cards = {"cards": [{"kind": "title", "chain": ["article.product-miniature", "a.thumbnail"]}]}
    same = dict(trolle, region=cards)
    moved = dict(trolle, region=cards, search={"mode": "url", "template": "https://3trolle.pl/szukaj?search_query={q}",
                                               "fallback_templates": trolle["search"]["fallback_templates"]})
    old_page = {"url": "https://3trolle.pl/jolisearch?s=Waste+Knights", "dom": "<html></html>"}
    content = {"url": "https://3trolle.pl/blog/gry-planszowe", "dom": "<html></html>"}
    assert L.results_page_refusal(trolle, same, old_page) == ""                   # the search's own page
    assert "no result list" in L.results_page_refusal(trolle, same, content)      # a content page: refused
    assert L.results_page_refusal(trolle, moved, old_page) == ""                  # the search moves: after the run
    assert L.results_page_refusal(trolle, moved, content) == ""
    care = {"search": {"mode": "url", "template": "https://www.careelite.de/?s={q}"},
            "region": {"cards": [{"kind": "card", "chain": ["div.a"]}]}}
    care_moved = dict(care, region=cards, search={"mode": "url", "template": "https://www.careelite.de/suche/?q={q}"})
    assert L.results_page_refusal(care, care_moved, {"url": "https://www.careelite.de/energie-sparen-stromspar-tipps/"}) == ""
    assert L.results_page_refusal(care, dict(care), {"url": "https://www.careelite.de/x/"}) == ""   # cards unchanged


if __name__ == "__main__":
    test_off_search_page_on_the_rounds_urls()
    test_the_round_false_accept_is_refused()
    test_a_patch_that_moves_the_search_is_judged_after_its_run()
    print("test_repair_results_page: 3 ok")
