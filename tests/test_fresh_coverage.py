"""The fresh-site compile share, offline - three failure classes
of the recorded compiles, each made general:

* proof values a real search can answer: the words of the result cards' TEMPLATE are never proof
  values - by the results' own URLs (``harness_compile.SLUG_CHECK``: www.foto-erhardt.de took 'MwSt' from
  "... inkl. MwSt. Versand gratis") and by words in (almost) every title (``EVERY_CHECK``); a result's OTHER link
  texts are candidates too (``RESULT_LINKS``: www.numisbids.com's titles were "Lot 236"); a proof replay the SITE sent
  away from its result page is inconclusive, never a pass (``redirected_away``: www.craftbeer-shop.com's 'Nepomucen'
  went to the home page, www.fernsehserien.de's 'US-Ermittlern' to the one show's page) - a tool that reaches no list
  for any value still never passes;
* a demonstration's "no results" is read again by the VERIFIER's own code (``attribute_verify.read_no_results``):
  stated / disputed / unrendered, typed in the compile's reason (``demo_compile.no_results_reason``), never a silent
  draft (the cascade still retries all three: the value is one the site lists);
* an empty start page names what the guard refused that the page needed (``harness_tools.refusal_evidence``: the
  site's own hosts, robots.txt - www.headphonecheck.com).

Offline, on the fake shop of ``test_repair_ladder`` (no browser, no network, no model).

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_fresh_coverage.py
"""
import os
import re
import sys
import types

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
import test_harness_compile as THC  # noqa: E402


# ---------------------------------------------------------------------------
# proof values: never the cards' template
# ---------------------------------------------------------------------------
def _demo(rows, value="Sony", links=(), text=None):
    """A demonstration's result list (``rows``: (title, url)) on a page that shows every title."""
    els = [{"tag": "a", "href": u, "text": t, "area": "body"} for t, u in rows] + \
        [{"tag": "a", "href": u, "text": t, "area": "body"} for t, u in links]
    shown = text if text is not None else " ".join([t for t, _u in rows] + [t for t, _u in links])
    return {"workflow": "site_search", "value": value,
            "result": {"results": [{"title": t, "url": u} for t, u in rows]},
            "final_page": {"text": shown, "elements": els}}


SHOP_ROWS = [
    ("Sofort lieferbar Sony Alpha ILCE-7 V Gehäuse 2.799,00 € inkl. MwSt. Versand gratis",
     "https://shop.example-foto.de/kameras/sony-alpha-ilce-7-v-gehaeuse.html"),
    ("Sofort lieferbar Sony Alpha ILCE-6700 Gehäuse 1.397,00 € inkl. MwSt. Versand gratis",
     "https://shop.example-foto.de/kameras/sony-alpha-ilce-6700-gehaeuse.html"),
    ("Sofort lieferbar Sigma 18-50mm DC DN Sony-E 449,00 € inkl. MwSt. Versand gratis",
     "https://shop.example-foto.de/objektive/sigma-18-50mm-dc-dn-sony-e.html"),
    ("In 2-5 Werktagen Sony FX3 Camcorder 3.999,00 € inkl. MwSt. Versand gratis",
     "https://shop.example-foto.de/video/sony-fx3-camcorder.html"),
]


def test_a_word_of_the_cards_template_is_never_a_proof_value_and_url_words_come_first():
    vals = [v["value"] for v in HC.site_vocabulary(_demo(SHOP_ROWS), "Sony")]
    for furniture in ("MwSt", "Versand", "gratis"):                # in every title, in no result URL
        assert furniture not in vals, (furniture, vals)
    # the words the site's own result URLs carry first (the more titles share them, the earlier), the rest after
    assert vals[:3] == ["Alpha", "Gehäuse", "ILCE-7"], vals
    url_words = [v for v in vals if HC.in_slugs(v, HC.slug_text([(a, b) for a, b in SHOP_ROWS], "Sony"))]
    assert vals[:len(url_words)] == url_words and "lieferbar" in vals[len(url_words):]


def test_the_rules_are_switches_and_off_restores_the_old_candidates():
    old = (HC.SLUG_CHECK, HC.EVERY_CHECK)
    try:
        HC.SLUG_CHECK = HC.EVERY_CHECK = False
        vals = [v["value"] for v in HC.site_vocabulary(_demo(SHOP_ROWS), "Sony")]
        assert "MwSt" in vals                           # the value the live compile chose
    finally:
        HC.SLUG_CHECK, HC.EVERY_CHECK = old


def test_opaque_result_urls_give_no_slug_evidence_but_every_card_words_still_go():
    rows = [(f"Lot {n} Roman denarius Estimate 100 USD", f"https://auction.example.com/sale/11/lot/{n}")
            for n in (1, 2, 3, 4)]
    rows[1] = ("Lot 2 Juba denarius Estimate 300 USD", rows[1][1])
    assert HC.slug_text(rows) is None                               # ids only: no evidence, no slug rule
    vals = [v["value"] for v in HC.site_vocabulary(_demo(rows, value="denarius"), "denarius")]
    assert "Estimate" not in vals and "Roman" in vals and "Juba" in vals


def test_a_results_other_link_texts_are_candidates_when_its_title_has_no_word():
    rows = [(f"Lot {n}", f"https://auction.example.com/sale/11/lot/{n}") for n in (236, 237, 238)]
    links = [("Maurétanie, Juba Ier. Denier, Utique.", "https://auction.example.com/sale/11/lot/236"),
             ("Maurétanie, Juba Ier. Quinaire, Utique.", "https://auction.example.com/sale/11/lot/237"),
             ("Rome, Auguste. Denier, Lyon.", "https://auction.example.com/sale/11/lot/238")]
    got = HC.site_vocabulary(_demo(rows, value="denarius", links=links), "denarius")
    assert got and all(v["source"] == "result_links" for v in got)
    assert [v["value"] for v in got][:4] == ["Maurétanie", "Juba", "Denier", "Utique"]     # shared by 2 of 3 first
    old = HC.RESULT_LINKS
    try:
        HC.RESULT_LINKS = False
        assert HC.site_vocabulary(_demo(rows, value="denarius", links=links), "denarius") == []
    finally:
        HC.RESULT_LINKS = old


def test_urls_that_do_not_name_their_results_give_no_evidence_and_every_card_words_decide_only_then():
    # a category and an id in the path: no URL names its result - the titles keep their words
    rows = [(f"Innenläufen Plissee Crush {c}", f"https://www.tex-shop.de/plissee/xart-{n}.html")
            for n, c in ((19321, "Sand"), (19322, "Grau"), (19320, "Weiss"), (19323, "Rot"))]
    assert HC.slug_text(rows) is None
    vals = [v["value"] for v in HC.site_vocabulary(_demo(rows, value="Plissee"), "Plissee")]
    assert vals and "Innenläufen" not in vals and "Crush" not in vals and "Sand" in vals     # every-card words go
    # URLs that name their results: a word of every title that the URLs carry is a name, not furniture
    rows = [(f"Makita {m} 18V Li-ion accu haakse boormachine {s}",
             f"https://www.tools-nl.nl/makita-{m.lower()}-18v-li-ion-accu-haakse-boormachine-{s.replace(' ', '-')}.html")
            for m, s in (("DDA450ZK", "body in koffer"), ("DA001GZ", "body"), ("DA333DZ", "set 2 accus"),
                         ("DDA351Z", "zonder lader"))]
    assert HC.slug_text(rows) is not None
    vals = [v["value"] for v in HC.site_vocabulary(_demo(rows, value="boormachine"), "boormachine")]
    assert "haakse" in vals and "DDA450ZK" in vals


def test_a_sort_order_is_never_a_facet_value():
    rows = [("Krebshaus Aquarium 30 l", "https://www.shrimp-shop.de/krebshaus-aquarium-30-l"),
            ("Krebshaus Futter Mix", "https://www.shrimp-shop.de/krebshaus-futter-mix")]
    demo = _demo(rows, value="Krebs")
    demo["final_page"]["elements"].append({"tag": "select", "text": "Sortierung", "options": [
        {"text": "Sortierung"}, {"text": "Erscheinungsdatum"}, {"text": "Beliebtheit"}, {"text": "Futter"}]})
    got = {v["value"]: v["source"] for v in HC.site_vocabulary(demo, "Krebs")}
    assert "Erscheinungsdatum" not in got and "Beliebtheit" not in got
    assert got.get("Futter") in ("result_titles", "facet")


def test_the_slug_rule_reads_transliterated_words():
    assert HC.in_slugs("Händler", " die siedler von catan aufbruch der haendler ")
    assert HC.in_slugs("US-Ermittlern", " the first am tatort mit den ermittlern ")
    assert not HC.in_slugs("MwSt", " sony alpha ilce gehaeuse ")
    assert HC.in_slugs("anything", None)


# ---------------------------------------------------------------------------
# a proof replay the site sent away from its result page
# ---------------------------------------------------------------------------
def _tool(tpl):
    return {"search": {"mode": "url", "template": tpl}}


@pytest.mark.parametrize("tpl,value,final,away", [
    ("https://www.shop-a.de/search/?qs={q}&search=", "Nepomucen", "https://www.shop-a.de/", True),
    ("https://www.tv-b.de/suche/{q}", "US-Ermittlern", "https://www.tv-b.de/the-first-48-mit-den-us-ermittlern", True),
    ("https://www.tv-b.de/suche/{q}", "First", "https://www.tv-b.de/suche/First", False),
    # a 404 on the tool's own URL (a category path, www.wijnbeurs.nl) is not a redirect: it still fails
    ("https://www.wine-c.nl/wijnlanden/spaanse-wijn/{q}", "Olarra", "https://www.wine-c.nl/wijnlanden/spaanse-wijn/Olarra",
     False),
    ("https://x.example.de/search?q={q}", "Hans Wurst", "https://x.example.de/suche/hans-wurst", False),
    ("https://x.example.de/search?q={q}", "Händler", "https://x.example.de/search/?query=H%C3%A4ndler", False),
])
def test_redirected_away(tpl, value, final, away):
    assert bool(HC.redirected_away(_tool(tpl), {"final_page": {"url": final}}, value)) is away


def _replays(table):
    def rep(tool, site, v):
        status, why, final = table[v]
        out = {"status": status, "why": why, "model_calls": 0, "final_page": {"url": final, "dom": "", "text": ""}}
        if status == "ok":
            out["output"] = {"results": [{"title": f"{v} {i}", "url": f"https://www.shop-a.de/p/{v.lower()}-{i}"}
                                         for i in range(3)]}
        return out
    return rep


MEMBERSHIP = "membership: no item of the compiled region on the result page and no 'no results' statement"


def test_a_redirected_proof_value_is_inconclusive_and_the_next_value_is_tried():
    tool = _tool("https://www.shop-a.de/search/?qs={q}&search=")
    rep = _replays({"Nepomucen": ("failed", MEMBERSHIP, "https://www.shop-a.de/"),
                    "Stout": ("ok", "", "https://www.shop-a.de/search/?qs=Stout&search="),
                    "Porter": ("ok", "", "https://www.shop-a.de/search/?qs=Porter&search=")})
    p = HC.prove_web(tool, None, ["Nepomucen", "Stout", "Porter"], replay=rep, independent=False)
    assert p["passed"], p["reason"]
    first = p["cases"][0]
    assert first["inconclusive"] and not first["passed"] and first["why"].startswith("redirected:")
    assert p["inconclusive"] == 1


def test_a_tool_whose_every_value_is_redirected_never_passes():
    tool = _tool("https://www.shop-a.de/search/?qs={q}&search=")
    rep = _replays({v: ("failed", MEMBERSHIP, "https://www.shop-a.de/") for v in ("Stout", "Porter", "Sour", "Bock")})
    p = HC.prove_web(tool, None, ["Stout", "Porter", "Sour", "Bock"], replay=rep, independent=False)
    assert not p["passed"] and "0 fresh value(s) passed" in p["reason"] and p["inconclusive"] == 4


def test_a_failure_on_the_tools_own_url_still_fails_the_proof():
    tool = _tool("https://www.wine-c.nl/wijnlanden/spaanse-wijn/{q}")
    rep = _replays({"Olarra": ("failed", MEMBERSHIP, "https://www.wine-c.nl/wijnlanden/spaanse-wijn/Olarra"),
                    "Crianza": ("ok", "", "https://www.wine-c.nl/wijnlanden/spaanse-wijn/Crianza")})
    p = HC.prove_web(tool, None, ["Olarra", "Crianza"], replay=rep, independent=False)
    assert not p["passed"] and p["reason"].startswith("value 'Olarra'") and len(p["cases"]) == 1
    old = HC.REDIRECT_INCONCLUSIVE
    try:
        HC.REDIRECT_INCONCLUSIVE = False
        rep2 = _replays({"Nepomucen": ("failed", MEMBERSHIP, "https://www.shop-a.de/")})
        p = HC.prove_web(_tool("https://www.shop-a.de/search/?qs={q}&search="), None, ["Nepomucen"], replay=rep2,
                         independent=False)
        assert p["reason"].startswith("value 'Nepomucen'")             # the old behaviour, behind the switch
    finally:
        HC.REDIRECT_INCONCLUSIVE = old


# ---------------------------------------------------------------------------
# the verifier reads an answered result where the other answered results are
# ---------------------------------------------------------------------------
def test_an_answered_result_also_teased_in_a_box_is_read_in_the_answers_list():
    box = "<div class='newest'>" + "".join(
        f"<div class='teaser'><a href='https://www.games-r.de/{s}/'>{n}</a></div>"
        for s, n in (("zug-um-zug", "Zug um Zug"), ("azul", "Azul"), ("coimbra", "Coimbra"))) + "</div>"
    found = [("freihaendler", "Freihändler Grundregeln"), ("aufbruch-der-haendler", "Aufbruch der Händler"),
             ("haendler-und-barbaren", "Händler und Barbaren"), ("coimbra", "Coimbra")]
    lst = "<div class='results'>" + "".join(
        f"<article class='post'><h2><a href='https://www.games-r.de/{s}/'>{n}</a></h2><p>Rezension</p></article>"
        for s, n in found) + "</div>"
    page = AV.Page(f"<html><body>{box}{lst}</body></html>", "", "https://www.games-r.de/?s=H%C3%A4ndler")
    answer = {"results": [{"title": n, "url": f"https://www.games-r.de/{s}/"} for s, n in found]}
    got = AV.verify_order(answer, page, ["www.games-r.de"]).decisions[0]
    assert got.status == AV.VERIFIED, got.why
    old = AV.TIE_BY_SIBLINGS
    try:
        AV.TIE_BY_SIBLINGS = False
        assert AV.verify_order(answer, page, ["www.games-r.de"]).decisions[0].status != AV.VERIFIED
    finally:
        AV.TIE_BY_SIBLINGS = old


# ---------------------------------------------------------------------------
# a demonstration's "no results", read again by the verifier
# ---------------------------------------------------------------------------
def _page(body, url="https://www.shop-a.de/search?q=Laminat", text=None):
    return AV.Page(f"<html><body>{body}</body></html>", text if text is not None else _visible(body), url)


def _visible(body):
    import re
    return "\n".join(x for x in re.sub(r"<[^>]+>", "\n", body).split("\n") if x.strip())


def test_read_no_results_stated_disputed_unrendered():
    stmt = "<header><a href='/'>Home</a></header><main><p>Leider keine Ergebnisse für Laminat.</p></main>"
    got = AV.read_no_results(_page(stmt), "Laminat")
    assert got["kind"] == "stated", got
    counted = "<main><h1>Suche</h1><p>776 Ergebnisse für Laminat</p></main>"
    assert AV.read_no_results(_page(counted), "Laminat")["kind"] == "disputed"
    items = "<nav><a href='/laminat'>Laminat</a></nav><main>" + "".join(
        f"<div><a href='/p/laminat-{i}'>Laminat Eiche {i}</a></div>" for i in range(4)) + "</main>"
    got = AV.read_no_results(_page(items), "Laminat")
    assert got["kind"] == "disputed" and len(got["items"]) == 4             # the menu link is chrome, not counted
    menu_only = "<nav>" + "".join(f"<a href='/c/laminat-{i}'>Laminat {i}</a>" for i in range(5)) + "</nav><main></main>"
    assert AV.read_no_results(_page(menu_only), "Laminat")["kind"] == "unrendered"
    assert AV.read_no_results(_page("", text=""), "Laminat")["kind"] == "unrendered"


def test_the_verifier_reads_a_recorded_accessibility_tree():
    raw = {"v": 1, "n": [[-1, "RootWebArea", "Suche", 0, "", 1], [0, "main", "", 0, "", 2],
                         [1, "link", "Laminat Eiche hell", 0, "https://www.shop-a.de/p/1", 3],
                         [1, "link", "Laminat Eiche dunkel", 0, "https://www.shop-a.de/p/2", 4],
                         [0, "navigation", "", 0, "", 5], [4, "link", "Laminat", 0, "https://www.shop-a.de/c", 6],
                         [1, "link", "Laminat Nuss", 0, "https://www.shop-a.de/p/3", 7]],
           "ctl": [], "inp": [], "href": []}
    snap = AV.ax_from_record(raw)
    page = AV.Page("<html><body></body></html>", "", "https://www.shop-a.de/search?q=Laminat", ax=snap)
    assert page.ax is not None
    assert sorted(AV.links_naming(page, "Laminat")) == ["shop-a.de/p/1", "shop-a.de/p/2", "shop-a.de/p/3"]
    assert AV.ax_from_record({"nodes": []}) is None


def test_the_compile_types_a_no_results_demonstration_and_the_cascade_still_retries_it():
    # the product path: a harness run whose demonstration answers "no results" on the shop's own result page
    # the harness asks ONCE about a "no results" its page does not state (agent_harness.answer_problem); the
    # demonstration repeats it, and the compile types it
    tr, _site = THC.web_run(script=[lambda t, o, h: T.Search(0, "Bosch"), T.Done({"no_results": True}),
                                    T.Done({"no_results": True})])
    c = HC.compile_web(tr)
    assert not c["ok"] and c["why"].startswith(f"not a valid demonstration: {C.NO_RESULTS_DISPUTED}"), c["why"]
    assert HP.demo_retry_reason({"outcome": "done", "compiled": {"status": "draft", "reasons": [c["why"]]}})
    # the site's own page states it found nothing: typed "stated" - for a value the site lists that is about THIS way
    # of searching (a refused data request, an empty suggestion panel), so the cascade may still try another way
    tr, _site = THC.web_run(value="Zxqvbn", script=[lambda t, o, h: T.Search(0, "Zxqvbn"), T.Done({"no_results": True})])
    c = HC.compile_web(tr)
    assert not c["ok"] and c["why"].startswith(f"not a valid demonstration: {C.NO_RESULTS_STATED}"), c["why"]
    assert HP.demo_retry_reason({"outcome": "done", "compiled": {"status": "draft", "reasons": [c["why"]]}})


# ---------------------------------------------------------------------------
# an empty start page names what the guard refused
# ---------------------------------------------------------------------------
def test_refusal_evidence_names_the_sites_own_hosts_and_robots(monkeypatch):
    # the evidence says what the host's robots.txt said - this fixture's file was read and disallows the path
    from anyuse.execution import api_permission as AP
    monkeypatch.setattr(AP, "robots_status", lambda h: ("rules", ""))
    page = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[
        ("https://cdn-assets.mag-test.com/app.js", "third-party host cdn-assets.mag-test.com"),
        ("https://cdn-assets.mag-test.com/app.css", "third-party host cdn-assets.mag-test.com"),
        ("https://www.tracker-x.net/t.js", "third-party host www.tracker-x.net"),
        ("https://www.mag-test.com/_next/static/a.js", "robots_refused: /_next/")]))
    ev = HT.refusal_evidence(page, "https://www.mag-test.com")
    assert "cdn-assets.mag-test.com" in ev and "tracker-x" not in ev and "1 request(s) robots.txt disallows" in ev
    assert HT.refusal_evidence(types.SimpleNamespace(), "https://www.mag-test.com") == ""


def test_refusal_evidence_says_apart_a_robots_txt_that_could_not_be_read():
    # www.heimwerker.de: the granted Algolia hosts' robots.txt gave no usable answer - the evidence named
    # "robots.txt disallows"; it names the hosts and what their robots.txt said (fail closed either way)
    # (redirects are followed to any host; a loop is unreachable - its chain is the reason)
    from anyuse.execution import api_permission as AP
    AP._ROBOTS_CACHE.pop("app1-dsn.search-test.net", None)
    AP._ROBOTS_NOTES.pop("app1-dsn.search-test.net", None)
    ok, why = AP.robots_allows("https://app1-dsn.search-test.net/1/indexes/x/query", fetcher=_redirect_loop)
    assert ok is False and "redirect_loop" in why
    assert AP.robots_status("app1-dsn.search-test.net")[0] == "unreadable"     # kept for the host
    page = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[
        ("https://app1-dsn.search-test.net/1/indexes/x/query", "robots_refused: https://app1-dsn.search-test.net/1/")] * 5
        + [("https://never-fetched.mag-test.com/a.js", "robots_refused: /a.js")]))
    ev = HT.refusal_evidence(page, "https://www.mag-test.com")
    assert "robots.txt disallows" not in ev
    assert "5 request(s) refused because robots.txt could not be read (app1-dsn.search-test.net: 5 - redirect_loop: " \
           in ev and "(redirects: https://app1-dsn.search-test.net/robots.txt -> https://www.search-test.com/robots.txt" \
           in ev
    assert "1 request(s) refused because robots.txt was not read (never-fetched.mag-test.com: 1; fail closed)" in ev
    AP._ROBOTS_CACHE.pop("app1-dsn.search-test.net", None)
    AP._ROBOTS_NOTES.pop("app1-dsn.search-test.net", None)


def _redirect_loop(url):
    routes = {"https://app1-dsn.search-test.net/robots.txt": "https://www.search-test.com/robots.txt",
              "https://www.search-test.com/robots.txt": "https://app1-dsn.search-test.net/robots.txt"}
    from anyuse.execution import api_permission as AP
    return AP._default_fetch(url, get=lambda u: (301, {"location": routes[u]}, ""), resolver=lambda h: ["93.184.216.34"])


def test_an_empty_start_page_carries_the_evidence_on_the_product_path(monkeypatch):
    monkeypatch.setenv("ANYUSE_ALLOW_SITE_OWN_HOSTS", "off")
    site = D.DomSite([D.Recorded("https://www.mag-test.com/", "<html><head></head><body></body></html>")])
    page = D.DomPage(site)
    page.guard = types.SimpleNamespace(failed=[("https://cdn-assets.mag-test.com/app.js",
                                                "third-party host cdn-assets.mag-test.com")])
    obs = HT.BrowserTools(page, "https://www.mag-test.com/", "www.mag-test.com").observe()
    assert obs.blocked.startswith("start failed (empty_page)") and "cdn-assets.mag-test.com" in obs.blocked
    assert "ANYUSE_ALLOW_SITE_OWN_HOSTS is off" in obs.blocked
    monkeypatch.delenv("ANYUSE_ALLOW_SITE_OWN_HOSTS")              # the default (on): the scope's other reasons
    assert "still refuses" in HT.refusal_evidence(page, "https://www.mag-test.com/")




# ---------------------------------------------------------------------------
# the recorded losses (offline): a tree that hides the content, card-label titles, a result's names as URL evidence
# ---------------------------------------------------------------------------
def test_a_tree_that_hides_the_pages_content_is_no_second_read():
    links = "".join(f"<div class='card'><a href='https://www.cam-shop.test/p/{i}'>Kamera {i}</a></div>" for i in range(30))
    dom = f"<html><body><div id='consent'><a href='/privacy'>Datenschutz</a></div><main>{links}</main></body></html>"
    rows = [[-1, "RootWebArea", "Kameras", 0, "", 1], [0, "dialog", "Cookies", 0, "", 2],
            [1, "link", "Datenschutz", 0, "https://www.cam-shop.test/privacy", 3]] + \
        [[0, "none", "", 1, "", 10 + i] for i in range(40)]             # the content: ignored (aria-hidden / inert)
    page = AV.Page(dom, "Kamera 1", "https://www.cam-shop.test/search?q=Kamera",
                   ax=AV.ax_from_record({"v": 1, "n": rows, "ctl": [], "href": []}))
    answer = {"results": [{"title": f"Kamera {i}", "url": f"https://www.cam-shop.test/p/{i}"} for i in range(3)]}
    assert AV.ax_hides_content(AV.AxView(page.ax), page)
    assert AV.verify_ax(answer, page).decisions == []                  # no decision: the other checks decide
    old = AV.AX_HIDDEN_SHARE
    try:
        AV.AX_HIDDEN_SHARE = 1.01                                       # the rule off: the old uncertain decisions
        assert all(d.status == AV.UNCERTAIN for d in AV.verify_ax(answer, page).decisions)
    finally:
        AV.AX_HIDDEN_SHARE = old


def test_a_discarded_tree_leaves_the_verifiers_own_reads_to_decide_and_nothing_less():
    """Principle 1 / 2: with the tree discarded (it hid the content) an answer is accepted ONLY when the verifier's OWN
    reads of the page agree (``verify_list``: the result's own card names the title on the verifier's own parse;
    ``verify_order`` / ``verify_shape`` / ``verify_scope`` / ``verify_query``); when they cannot confirm it and no model
    is available, it is withheld - typed - never accepted."""
    rows = [[-1, "RootWebArea", "Kameras", 0, "", 1], [0, "dialog", "Cookies", 0, "", 2],
            [1, "link", "Datenschutz", 0, "https://www.cam-shop.test/privacy", 3]] + \
        [[0, "none", "", 1, "", 10 + i] for i in range(40)]
    hidden = AV.ax_from_record({"v": 1, "n": rows, "ctl": [], "href": []})
    cards = "".join(f"<li class='product'><h3><a href='https://www.cam-shop.test/p/kamera-{i}'>Kamera Alpha {i}</a></h3>"
                    f"<span>1.{i}99,00 €</span></li>" for i in range(30))
    dom = f"<html><body><h1>Suchergebnisse für Alpha</h1><ul class='results'>{cards}</ul></body></html>"
    page = AV.Page(dom, "Suchergebnisse für Alpha\n" + "\n".join(f"Kamera Alpha {i}" for i in range(30)),
                   "https://www.cam-shop.test/search?q=Alpha", ax=hidden)
    no_model = AV.Resolver(None, unavailable="no Cerebras key is configured")
    good = {"results": [{"title": f"Kamera Alpha {i}", "url": f"https://www.cam-shop.test/p/kamera-{i}"} for i in range(3)]}
    v = AV.check("site_search", good, page, no_model, scope=["www.cam-shop.test"], value="Alpha")
    assert v.verdict == AV.ACCEPT and v.model_calls == 0, v.why()          # the verifier's own reads agree
    # the tree discarded AND the verifier's own reads cannot confirm (the answered URLs are not on the page): withheld
    bad = {"results": [{"title": f"Kamera Alpha {i}", "url": f"https://www.cam-shop.test/p/other-{i}"} for i in range(3)]}
    v = AV.check("site_search", bad, page, no_model, scope=["www.cam-shop.test"], value="Alpha")
    assert v.verdict != AV.ACCEPT
    rec = AV.withhold({"status": "ok", "output": bad}, v, "answer")
    assert rec["status"] == "incomplete" and rec["accepted"] is False and AV.MODEL_UNAVAILABLE in rec["why"]
    # and a page whose own reads have nothing to read (no DOM) is never accepted either
    empty = AV.Page("", "", "https://www.cam-shop.test/search?q=Alpha", ax=hidden)
    assert AV.check("site_search", good, empty, no_model, scope=["www.cam-shop.test"], value="Alpha").verdict != AV.ACCEPT


def test_card_label_titles_are_no_titles_to_compile():
    import test_repair_ladder as TL
    script = [lambda t, o, h: T.Search(0, "Akkuschrauber"),
              T.Done({"results": [{"title": "sofort versandbereit", "url": TL.BASE + "/p/" + s} for _n, _p, s in TL.PRODUCTS[:4]]
                      + [{"title": "Hersteller liefert nicht", "url": TL.BASE + "/p/" + TL.PRODUCTS[4][2]}]})]
    tr, _site = THC.web_run(value="Akkuschrauber", script=script)
    c = HC.compile_web(tr)
    assert not c["ok"] and C.LABEL_TITLES in c["why"], c["why"]
    assert HP.demo_retry_reason({"outcome": "done", "compiled": {"status": "draft", "reasons": [c["why"]]}})


def test_a_results_link_texts_count_as_url_evidence_when_its_titles_are_badges():
    rows = [("sofort versandbereit", "https://www.tabak-x.test/cool-pfeifenfeuerzeug-nice-pipe-silber"),
            ("sofort versandbereit", "https://www.tabak-x.test/zippo-pfeifeneinsatz-benzin-chrom"),
            ("Hersteller liefert nicht", "https://www.tabak-x.test/eurojet-pfeifenfeuerzeug-piezo"),
            ("sofort versandbereit", "https://www.tabak-x.test/white-elephant-aktivkohlefilter-pfeife")]
    links = [("Cool Pfeifenfeuerzeug Nice Pipe Silber", rows[0][1]), ("Zippo Pfeifeneinsatz Benzin Chrom", rows[1][1]),
             ("Eurojet Pfeifenfeuerzeug Piezo", rows[2][1]), ("White Elephant Aktivkohlefilter Pfeife", rows[3][1])]
    vals = [v["value"] for v in HC.site_vocabulary(_demo(rows, value="Raucherbedarf", links=links), "Raucherbedarf")]
    assert vals[0] == "Pfeifenfeuerzeug", vals                         # a name the URLs carry (2 results) first
    url_words = [v for v in vals if v in ("Pfeifenfeuerzeug", "Cool", "Nice", "Pipe", "Zippo", "Pfeifeneinsatz",
                                          "Benzin", "Chrom", "Eurojet", "Piezo", "White", "Elephant",
                                          "Aktivkohlefilter", "Pfeife", "Silber")]
    assert vals[:len(url_words)] == url_words                         # every URL word before the badge words
    assert vals.index("Hersteller") >= len(url_words) and "versandbereit" not in vals     # (a repeated label)


def test_refusal_evidence_names_another_sites_asset_host_refused_again_and_again():
    page = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[
        (f"https://asset1-327a.kxcdn.com/js/{i}.js", "third-party host asset1-327a.kxcdn.com") for i in range(6)] +
        [("https://www.googletagmanager.com/gtm.js", "third-party host www.googletagmanager.com")]))
    ev = HT.refusal_evidence(page, "https://www.pc-shop.test")
    assert "6 request(s) to asset1-327a.kxcdn.com" in ev and "googletagmanager" not in ev



def test_an_in_page_search_lists_what_it_drew_first(monkeypatch):
    # www.motorpasion.com: its search draws IN the page (the URL stays); the policy sees the first 60
    # elements, in page order, and never saw what the search drew at 134-153 - it guessed URLs until max_steps
    from anyuse.execution import demo_policy as DPOL
    url = "https://www.mag-test.com/"
    box = T.Element(index=0, kind="search box", text="Buscar")
    teasers = [T.Element(index=i, kind="link", text=f"Story {i}", href=f"{url}story-{i}") for i in range(1, 70)]
    drawn = [T.Element(index=100 + i, kind="link", text=f"Toyota {i}", href=f"{url}toyota/{i}") for i in range(5)]
    before, after = [box] + teasers, [box] + teasers + drawn

    async def typed(page, d, mapping, site, settle, value):
        return {"ok": True, "how": "typed", "url_after": url}
    monkeypatch.setattr(DPOL, "execute", typed)
    obs = T.Observation(kind="web", url=url, elements=before, page={"url": url, "dom": ""},
                        mapping={0: {"src": "input", "input": {"index": 0, "name": "q"}}})
    # without a search (a read, a scroll on the same URL): page order, as before
    plain = HT.BrowserTools(None, url, "www.mag-test.com")
    plain._new_first(list(before), url, {})
    assert plain._new_first(list(after), url, {}) == after
    # right after a search that stayed on its URL: the search box, then what the search drew, then the rest
    tools = HT.BrowserTools(None, url, "www.mag-test.com")
    tools._new_first(list(before), url, {})
    out = tools.dispatch(T.Search(0, "Toyota"), obs)
    assert out.ok and tools._in_page_search
    got = tools._new_first(list(after), url, {})
    assert got[:6] == [box] + drawn and not tools._in_page_search          # once: the next observation is as before
    assert tools._new_first(list(after), url, {}) == after


# ---------------------------------------------------------------- URL shapes, card titles, in-page results
import test_repair_ladder as LADDER  # noqa: E402


class TwoShapeShop(LADDER.Shop):
    """www.multicom.no: ONE result list whose items take two URL shapes - systems ``/<slug>/system/<id>`` and
    products ``/<slug>/cat-p/c/<id>`` (two varying segments around constant ones: no URL cluster). ``only`` keeps one
    shape on the result page (a held-out value whose results all take the other shape)."""

    def __init__(self, only=""):
        super().__init__()
        self.only = only

    def href(self, i, slug):
        return f"/{slug}/system/p{100 + i}" if i % 2 == 0 else f"/{slug}/cat-p/c/p{200 + i}"

    def results(self, q):
        import html as _h
        hits = [(i, p) for i, p in enumerate(self.hits(q))]
        if self.only:
            hits = [(i, p) for i, p in hits if self.only in self.href(i, p[2])] or \
                [(1, p) for _i, p in hits]
        cards = "".join(f"<div class=\"item\"><section class=\"item-name\"><a href=\"{self.href(i, s)}\">{_h.escape(n)}</a>"
                        f"</section><div class=\"sku\">Varenr. {i}</div></div>" for i, (n, pr, s) in hits)
        body = (f"<h1>Søkeresultat: {_h.escape(q)}</h1><div id=\"product-view\"><div class=\"list\">{cards}</div></div>"
                if hits else "<p>Keine Ergebnisse gefunden.</p>")
        return (f"<html><head><title>Søk: {_h.escape(q)}</title></head><body><header><nav><a href=\"/\">Start</a></nav>"
                f"<form action=\"/search\" method=\"get\" role=\"search\"><input type=\"search\" name=\"q\" "
                f"value=\"{_h.escape(q)}\"></form></header><main>{body}</main><footer><a href=\"/om\">Om oss</a></footer>"
                f"</body></html>")


def test_one_result_list_across_two_url_shapes_is_read_from_its_cards_container():
    from anyuse.execution import agent_harness as H
    from anyuse.execution import harness_policies as P
    base, site_name = LADDER.BASE, LADDER.SITE
    site = TwoShapeShop()
    page = site.read(*site.route(base + "/search?q=Akkuschrauber"))
    want = [e["href"] for e in page["elements"] if e.get("tag") == "a" and ("/system/" in e["href"] or "/cat-p/" in e["href"])]
    assert len(want) >= 4 and C.region_from_dom(page["dom"], page["url"], want) is None      # no URL-shape cluster
    reg = C.region_from_cards(page["dom"], page["url"], want)
    assert reg["kind"] == "container" and all(re.match(reg["regex"], u) for u in want)
    assert not re.match(reg["regex"], base + "/search?q=x")                                  # never the search page
    tools = HT.BrowserTools(D.DomPage(site), base + "/", site_name)
    pol = P.ScriptedPolicy([T.Search(0, "Akkuschrauber"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if "/system/" in e.href or "/cat-p/" in e.href][:5]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="two").run("search for {q}",
                                                                                       {"q": "Akkuschrauber"})
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    assert c["tool"]["region"]["kind"] == "container" and c["tool"]["region"].get("cards")
    out = HC.replay_web(c["tool"], site, "Akkuschrauber")
    urls = [r["url"] for r in out["output"]["results"]]
    assert out["status"] == "ok" and any("/system/" in u for u in urls) and any("/cat-p/" in u for u in urls)
    # a held-out value whose results ALL take the other shape is read
    out2 = HC.replay_web(c["tool"], TwoShapeShop(only="/cat-p/"), "Bohrhammer")
    urls2 = [r["url"] for r in out2["output"]["results"]]
    assert out2["status"] == "ok" and urls2 and all("/cat-p/" in u for u in urls2)
    # the proof can still fail: a value the site has nothing for is no result list
    out3 = HC.replay_web(c["tool"], site, "Gartenschlauch")
    assert out3["status"] != "ok"


def test_a_cards_container_region_needs_every_result_in_its_own_card():
    dom = ("<html><body><main><div class='list'><div class='c'><h3><a href='/a/system/1'>A</a></h3><p>"
           "<a href='/b/cat-p/c/2'>B</a></p></div><div class='c'><a href='/c/system/3'>C</a></div></div></main>"
           "<footer><a href='/d/x/4'>D</a></footer></body></html>")
    assert C.region_from_cards(dom, "https://s.test/search?q=x", ["https://s.test/a/system/1",
                                                                   "https://s.test/c/system/3"])["kind"] == "container"
    u = "https://s.test/search?q=x"
    assert C.region_from_cards(dom, u, ["https://s.test/a/system/1", "https://s.test/b/cat-p/c/2"]) is None   # one card
    assert C.region_from_cards(dom, u, ["https://s.test/a/system/1", "https://s.test/d/x/4"]) is None         # footer only
    assert C.region_from_cards(dom, u, ["https://s.test/a/system/1", "https://s.test/zz/9"]) is None          # not inside
    assert C.region_from_cards(dom, u, ["https://s.test/a/system/1"]) is None                                 # one result


def _solr_page(q, items):
    cards = "".join(f"<li class='hit'><h4 class='title'><a href='/{s}'>{n}</a></h4><p>{q} …</p></li>" for s, n in items)
    return (f"<html><body><main><section class='results'><ul class='hits'>{cards}</ul></section></main>"
            f"<aside><a href='/x'>X</a></aside></body></html>")


def test_a_cards_own_title_is_never_a_widget_box_label():
    # www.sbz-online.de: a result's own title "Fachverband ... stellt Führungsteam neu auf" was read as its card's
    # box label ("neu" = "new") and the card became a widget box; a real labelled box ("Neue Reports") stays a widget
    items = [("fuehrungsteam", "Fachverband SHK stellt Führungsteam neu auf"), ("waerme", "Wärmepumpe im Altbau"),
             ("markt", "Wärmepumpen-Markt wächst")]
    page = {"dom": _solr_page("Wärmepumpe", items), "url": "https://s.test/suche?q=W%C3%A4rmepumpe"}
    start = {"dom": "<html><body><main><h1>Willkommen</h1></main></body></html>", "url": "https://s.test/"}
    want = [("https://s.test/fuehrungsteam", items[0][1]), ("https://s.test/waerme", items[1][1])]
    from anyuse.execution import demo_cards as K
    st = K.result_cards(page["dom"], page["url"], want)
    q = C.query_region(page, start, st["cards"], want, "Wärmepumpe", "site_search", v10=True)
    assert q["ok"] and not q["widget"], q
    box = ("<html><body><main><div class='box'><h3>Neue Reports</h3><ul><li><a href='/r1'>Report eins</a></li>"
           "<li><a href='/r2'>Report zwei</a></li></ul></div></main></body></html>")
    root, base = K.parse(box)
    a = next(n for n, h in K.anchors(root, "https://s.test/", base) if h and h.endswith("/r1"))
    assert K.widget_why(a, v10=True).startswith("inside a widget box labelled 'Neue Reports'")


def test_a_refused_image_never_stops_a_run_as_provider_search():
    # www.motorpasion.com: its in-page results' thumbnails on the publisher's image host carry the value
    img = "https://i.img-host.test/1d26ab/arene_toyota-rav4/200_200.jpeg"
    api = "https://api.search-x.test/v1/search?q=toyota"
    for types_ in ({img: "Image"}, {}):                  # by its recorded type, or by its file name when none was recorded
        page = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[(img, "third-party host i.img-host.test")],
                                                                 failed_types=types_))
        assert HT.search_refusal(page, {"failed": 0}, "Toyota", "https://www.cars.test/") is None
    page = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[(api, "third-party host api.search-x.test")],
                                                             failed_types={api: "XHR"}))
    st, why = HT.search_refusal(page, {"failed": 0}, "Toyota", "https://www.cars.test/")
    assert st == "provider_search" and "is off" not in why and "does not grant" in why


def test_in_page_results_drawn_in_the_footer_are_listed_when_they_name_the_value():
    # www.motorpasion.com: the search typed into the footer's box drew its results in the footer's own
    # container; the observation left them out as chrome
    url = "https://www.cars.test/"
    tools = HT.BrowserTools(None, url, "www.cars.test")
    teaser = {"i": 1, "tag": "a", "text": "Teaser", "href": url + "teaser", "area": "body"}
    rec0 = {"elements": [teaser], "link_sigs": {url + "teaser": ["body.home>div.teasers>ul>li"]}}
    els0 = [T.Element(index=1, kind="link", text="Teaser", href=url + "teaser")]
    tools._new_first(tools._with_changed_region(list(els0), {1: {"src": "el"}}, rec0), url, rec0)
    hits = [{"i": 10 + k, "tag": "a", "text": f"Story {k} about Toyota", "href": url + f"s{k}", "area": "footer"}
            for k in range(3)]
    network = [{"i": 20, "tag": "a", "text": "Partner site", "href": "https://partner.test/", "area": "footer"}]
    sigs = dict(rec0["link_sigs"])
    sigs.update({h["href"]: ["body.home>footer>div.foot-search-results>ul.results>li"] for h in hits})
    sigs["https://partner.test/"] = ["body.home>footer>div.network>ul>li"]
    rec1 = {"elements": [teaser] + hits + network, "link_sigs": sigs}
    tools._in_page_search, tools._in_page_value = True, "Toyota"
    got = tools._with_changed_region(list(els0), {1: {"src": "el"}}, rec1)
    assert [e.text for e in got[1:]] == ["Story 0 about Toyota", "Story 1 about Toyota", "Story 2 about Toyota"]
    tools2 = HT.BrowserTools(None, url, "www.cars.test")                 # without an in-page search: chrome stays out
    tools2._new_first(tools2._with_changed_region(list(els0), {1: {"src": "el"}}, rec0), url, rec0)
    assert len(tools2._with_changed_region(list(els0), {1: {"src": "el"}}, rec1)) == 1

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
