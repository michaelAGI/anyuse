"""Compiling on unseen sites (walls, proof values, typed stops, titles), offline. Pinned:
* a challenge-platform request in the log is a wall only when the page did not render its content (a short page, or one
  that says it checks the browser) - www.sportsbikeshop.co.uk and www.digitalkamera.de rendered their whole start page
  while the CDN's background bot-detection script ran; that passive script is never a wall (digitalkamera's
  late-rendering search page); a real interstitial still stops;
* the proof's values from the result titles keep their order (the words more titles share first): a "distinguishing"
  order (one or two titles, rare in the rest of the page, longer first) was measured against live proof
  values and not adopted;
* a search whose own results document robots.txt disallows is the typed stop ``robots_search`` (www.lampen24.nl; the
  form's own target, with or without the value); a
  disallowed suggestion request while the results page loads is not (www.toolnut.com's /search/suggest); a refused
  third-party request that carried the value, then a "no results" answer, is the typed stop ``provider_search``;
* a lookup that reads one unchanged page again and again gets ONE corrective observation (no step used;
  www.4teachers.de, www.fairplay-online.de);
* the verifier reads an element's text in document order (www.raumtextilienshop.de: a highlighted term inside a title);
* a link that holds its title and a teaser paragraph is titled by its text before the paragraph - in the agent's answer
  and in a tool compiled with ``link_names`` 3; an older tool reads as before.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_fresh_compile_stops.py
"""
import html
import pathlib
import sys
import tempfile
import types
import urllib.parse

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import blocked as B  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402


# ------------------------------------------------------------------------------------------------- the challenge script
JSD = "https://www.bikes.test/cdn-cgi/challenge-platform/scripts/jsd/main.js"


def test_a_background_challenge_script_on_a_rendered_page_is_not_a_wall():
    shop = " ".join(f"Motorcycle helmet model {i} in stock, free delivery over 50 pounds." for i in range(80))
    assert len(shop) > B.CHALLENGE_MAX_VISIBLE_TEXT
    st = B.detect_blocked_ui(shop, "", "https://www.bikes.test/", [{"url": JSD}])
    assert not st.blocked, st
    # a short page with the same request is still the wall; so is a long page that says it checks the browser
    assert B.detect_blocked_ui("Just a moment...", "", "https://www.bikes.test/", [{"url": JSD}]).blocked
    long_check = "Checking your browser before accessing www.bikes.test. " + shop
    assert B.detect_blocked_ui(long_check, "", "https://www.bikes.test/", [{"url": JSD}]).blocked


def test_a_passive_detection_script_is_never_a_wall():
    # www.digitalkamera.de's search page (its results render late: a short text) with the CDN's background
    # bot-detection script in the log - no wall; an interstitial's own markers still are
    short = "Suche nach Canon - Ergebnisse werden geladen"
    jsd = [{"url": JSD}, {"url": "https://www.bikes.test/cdn-cgi/challenge-platform/h/g/scripts/jsd/4df4a60fa397/main.js?"},
           # its beacon (a POST the read-only browser refuses, still in the network log)
           {"url": "https://www.bikes.test/cdn-cgi/challenge-platform/h/g/jsd/oneshot/4df4a60fa397/0.70:179:0fe/a49"}]
    assert not B.detect_blocked_ui(short, "", "https://www.bikes.test/Suche?Q=Canon", jsd).blocked
    assert B.challenge_served_instead(short, "https://www.bikes.test/Suche?Q=Canon", [r["url"] for r in jsd]) == ""
    orchestrate = [{"url": "https://www.bikes.test/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=1"}]
    assert B.detect_blocked_ui(short, "", "https://www.bikes.test/", orchestrate).blocked
    assert B.detect_blocked_ui(short, "", "https://www.bikes.test/?__cf_chl_tk=abc", []).blocked
    assert B.is_passive_detection(JSD) and not B.is_passive_detection(orchestrate[0]["url"])


# ------------------------------------------------------------------------------------------------- proof values
def test_the_proof_values_keep_the_shared_words_first():
    # measured against every value the live runs tried: a word several titles share (and the cards' other
    # text repeats) passed the proof more often than a long word of one title (a tagline, a teaser) - the order is kept
    titles = ["Barón de Ley Reserva 2021 Verlässlich gut", "Barón de Ley Reserva Edición Aniversario 2021 Rioja Klasse",
              "Palador Crianza 2022 Rioja-Tradition in Perfektion", "Barriton Reserva Edición 2019 Gereifter Schatz"]
    text = ("Reserva aus der Rioja, ein Reserva mit Kraft. " * 8) + " ".join(titles)
    demo = {"workflow": "site_search",
            "result": {"results": [{"title": t, "url": f"https://www.wein.test/{i}"} for i, t in enumerate(titles)]},
            "final_page": {"text": text, "elements": [{"tag": "a", "text": t, "href": f"/{i}", "area": "main"}
                                                      for i, t in enumerate(titles)]}}
    vals = [v["value"] for v in HC.site_vocabulary(demo, "Rotwein")]
    assert vals[:3] == ["Reserva", "Barón", "Edición"], vals
    assert vals.index("Reserva") < vals.index("Rioja-Tradition") and vals.index("Edición") < vals.index("Gereifter")


# ------------------------------------------------------------------------------------------------- typed stops
BASE = "https://www.shop.test"
ITEMS = [("p1", "Red Kettle"), ("p2", "Blue Kettle"), ("p3", "Kettle Descaler")]


class GuardedShop(D.DomSite):
    """A shop whose search runs through a guard that may refuse requests (``refuse(url, value) -> [(url, why)]``)."""

    def __init__(self, refuse=None, results=True):
        super().__init__([])
        self.guard = types.SimpleNamespace(failed=[])
        self.refuse = refuse
        self.results = results

    def page(self, q):
        rows = "".join(f'<li class="product"><h3><a href="/{s}">{html.escape(t)}</a></h3><span>12 €</span></li>'
                       for s, t in ITEMS if self.results and q and any(w in t.lower() for w in q.lower().split()))
        return (f'<html><body><header><form action="/search" method="get"><input type="search" name="q" '
                f'value="{html.escape(q)}"></form></header><main><h1>Shop</h1><ul class="r">{rows}</ul><p>'
                f'{"Search results" if q else "Welcome"}</p></main></body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query)).get("q", "")
        if sp.path == "/search" and q and self.refuse:
            refused = self.refuse(url, q)
            self.guard.failed.extend(refused)
            if any(u.split("?")[0] == url.split("?")[0] for u, _w in refused):
                return D.Recorded(BASE + "/", self.page("")), BASE + "/"     # the document was refused: page stays
        return D.Recorded(url, self.page(q if sp.path == "/search" else "")), url


def tools_for(site):
    page = D.DomPage(site)
    page.guard = site.guard
    return HT.BrowserTools(page, BASE + "/", "www.shop.test")


def rows(t, o, h):
    return T.Done({"results": [{"element": e.index} for e in o.elements if e.href and "/p" in e.href]})


def test_a_search_whose_results_robots_disallows_is_a_typed_stop():
    site = GuardedShop(refuse=lambda url, q: [(url, f"robots_refused: {url}")])
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.ReadText(), T.ReadText(), T.ReadText(), T.ReadText()])
    tr = H.Harness(HT.Toolbox(browser=tools_for(site)), pol, H.HarnessConfig(), run_id="robots").run(
        "search for {q}", {"q": "Kettle"})
    assert tr.stop == T.Stop.ROBOTS_SEARCH and "/search?q=Kettle" in tr.stop_reason, (tr.stop, tr.stop_reason)
    assert len(tr.steps) == 1                                           # stopped at the search, no reading loop


def test_the_search_forms_own_target_refused_without_the_value_is_the_robots_stop():
    # www.lampen24.nl: the form sent its target with an empty query; robots.txt disallows the target
    site = GuardedShop(refuse=lambda url, q: [(BASE + "/search?q=", "robots_refused: " + BASE + "/search?q=")])
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.ReadText(), T.ReadText(), T.ReadText(), T.ReadText()])
    tr = H.Harness(HT.Toolbox(browser=tools_for(site)), pol, H.HarnessConfig(), run_id="robots-empty").run(
        "search for {q}", {"q": "Kettle"})
    assert tr.stop == T.Stop.ROBOTS_SEARCH and len(tr.steps) == 1, (tr.stop, tr.stop_reason)
    # without a known form target, a refusal that does not carry the value is not the wall
    g = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[(BASE + "/search?q=", "robots_refused: x")]))
    assert HT.search_refusal(g, {"failed": 0}, "Kettle", BASE + "/", target="", url_after=BASE + "/") is None


def test_a_disallowed_suggestion_request_while_the_results_load_is_not_a_wall():
    site = GuardedShop(refuse=lambda url, q: [(f"{BASE}/search/suggest?q={q}&limit=4", "robots_refused: x")])
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), rows])
    tr = H.Harness(HT.Toolbox(browser=tools_for(site)), pol, H.HarnessConfig(), run_id="suggest").run(
        "search for {q}", {"q": "Kettle"})
    assert tr.stop == T.Stop.DONE and len(tr.result["results"]) == 3
    assert HT.search_refusal(types.SimpleNamespace(guard=site.guard), {"failed": 0}, "Kettle", BASE + "/",
                             target=BASE + "/search", url_after=BASE + "/search?q=Kettle") is None


def test_a_refused_search_provider_then_no_results_is_a_typed_stop():
    def provider(url, q):
        return [(f"https://api.searchprovider.test/search?q={q}&site=shop", "third-party host api.searchprovider.test")]
    site = GuardedShop(refuse=provider, results=False)
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.Done({"no_results": True})])
    tr = H.Harness(HT.Toolbox(browser=tools_for(site)), pol, H.HarnessConfig(), run_id="provider").run(
        "search for {q}", {"q": "Kettle"})
    assert tr.stop == T.Stop.PROVIDER_SEARCH and "api.searchprovider.test" in tr.stop_reason
    # the same refusal with the site's results shown: the answer stands (a site may also tell a third party)
    site2 = GuardedShop(refuse=provider)
    tr2 = H.Harness(HT.Toolbox(browser=tools_for(site2)), P.ScriptedPolicy([T.Search(0, "Kettle"), rows]),
                    H.HarnessConfig(), run_id="provider2").run("search for {q}", {"q": "Kettle"})
    assert tr2.stop == T.Stop.DONE and len(tr2.result["results"]) == 3
    assert {T.Stop.ROBOTS_SEARCH, T.Stop.PROVIDER_SEARCH} <= set(T.STOPS)


# ------------------------------------------------------------------------------------------------- the reading loop
def test_a_lookup_reading_one_unchanged_page_gets_one_correction():
    site = GuardedShop()
    seen = []

    def answer(t, o, h):
        seen.append(o.note)
        return rows(t, o, h)
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.ReadText(), T.Scroll(), T.ReadText(), answer])
    tr = H.Harness(HT.Toolbox(browser=tools_for(site)), pol, H.HarnessConfig(), run_id="reads").run(
        "search for {q}", {"q": "Kettle"})
    assert tr.stop == T.Stop.DONE and len(tr.result["results"]) == 3
    assert [c["why"] for c in tr.corrections] == ["reading one page again"]
    assert len(tr.steps) == 4                                          # the third read used no step
    assert "YOUR LAST ACTION WAS NOT RUN" in seen[0] and "Red Kettle" in seen[0]
    # given once: the reads go on after it as before
    pol2 = P.ScriptedPolicy([T.Search(0, "Kettle")] + [T.ReadText()] * 6 + [rows])
    tr2 = H.Harness(HT.Toolbox(browser=tools_for(GuardedShop())), pol2, H.HarnessConfig(), run_id="reads2").run(
        "search for {q}", {"q": "Kettle"})
    assert len(tr2.corrections) == 1 and tr2.stop == T.Stop.DONE
    # a flow (credentials) is never corrected for its reads
    pol3 = P.ScriptedPolicy([T.ReadText()] * 4 + [T.Fail("x")])
    tr3 = H.Harness(HT.Toolbox(browser=tools_for(GuardedShop())), pol3, H.HarnessConfig(), run_id="flow").run(
        "log in and open {username}", {"username": "me", "password": "x"})
    assert not tr3.corrections


# ------------------------------------------------------------------------------------------------- the verifier
def test_the_verifier_reads_a_highlighted_term_in_its_place():
    dom = ('<html><body><main><ul><li><a href="/p/1"><span>Vorhangschiene mit 3 <strong>Innenläufen</strong> #1W'
           '</span></a></li><li><a href="/p/2"><span>Gardinenschiene ohne Innenläufe</span></a></li></ul></main>'
           '</body></html>')
    page = AV.Page(dom, "", "https://www.raum.test/suche?q=Innenläufen")
    i = next(j for j, h in page.links() if h.endswith("/p/1"))
    assert page.element_text(i) == "Vorhangschiene mit 3 Innenläufen #1W"
    ans = {"results": [{"title": "Vorhangschiene mit 3 Innenläufen #1W", "url": "https://www.raum.test/p/1"}]}
    d = AV.verify_list(ans, page).decisions[0]
    assert d.status == AV.VERIFIED, d.why


# ------------------------------------------------------------------------------------------------- titles
CURTAINS = [("p/schiene-3", "Vorhangschiene mit 3 Innenläufen #1W",
             "Diese Vorhangschiene aus Aluminium trägt Gardinen bis 8 kg und wird mit allen Trägern geliefert."),
            ("p/schiene-2", "Vorhangschiene mit 2 Innenläufen #2W",
             "Die zweiläufige Vorhangschiene für Gardine und Schal, weiß, auf Maß gekürzt.")]


class CurtainShop(D.DomSite):
    """Each result is ONE link: its title in a span, then a teaser paragraph (www.raumtextilienshop.de)."""

    def __init__(self):
        super().__init__([])

    def page(self, q):
        cards = "".join(f'<li class="item"><a href="/{p}" class="product-link"><span class="name">{html.escape(t)}'
                        f'</span><p class="teaser">{html.escape(x)}</p></a><span class="price">19,90 €</span></li>'
                        for p, t, x in CURTAINS if q and q.lower() in (t + x).lower())
        return (f'<html><body><header><form action="/suche" method="get"><input type="search" name="q" '
                f'value="{html.escape(q)}"></form></header><main><h1>Suche</h1><ul class="list">{cards}</ul></main>'
                f'</body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        return D.Recorded(url, self.page(dict(urllib.parse.parse_qsl(sp.query)).get("q", ""))), url


def test_a_link_with_a_teaser_paragraph_is_titled_by_its_text_before_it():
    a = next(x for x in K.walk(K.parse('<a href="/x"><span>Vorhangschiene #1W</span><p>Diese Schiene ...</p></a>')[0])
             if x.tag == "a")
    assert K.link_name(a) == "" and K.link_name(a, paragraphs=True) == "Vorhangschiene #1W"
    b = next(x for x in K.walk(K.parse('<a href="/y"><p>Only a paragraph</p></a>')[0]) if x.tag == "a")
    assert K.link_name(b, paragraphs=True) == ""                       # nothing before it: its own text names it
    site = CurtainShop()
    tools = HT.BrowserTools(D.DomPage(site), "https://www.raum.test/", "www.raum.test")
    pol = P.ScriptedPolicy([T.Search(0, "Vorhangschiene"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if e.href and "/p/" in e.href]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="curtain").run(
        "search for {q}", {"q": "Vorhangschiene"})
    assert [r["title"] for r in tr.result["results"]] == [t for _p, t, _x in CURTAINS]
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    assert c["tool"]["region"].get("link_names") == 3
    out = HC.replay_web(c["tool"], site, "Vorhangschiene")
    assert out["status"] == "ok"
    assert [r["title"] for r in out["output"]["results"]] == [t for _p, t, _x in CURTAINS]
    # a tool compiled with link_names 2 reads its titles as it did (the link's whole text)
    old = dict(c["tool"], region=dict(c["tool"]["region"], link_names=2))
    out_old = HC.replay_web(old, site, "Vorhangschiene")
    t0 = out_old["output"]["results"][0]["title"]
    assert out_old["status"] == "ok" and t0.startswith(CURTAINS[0][1]) and len(t0) > len(CURTAINS[0][1])


# ------------------------------------------------------------------------------------------------ repeats
def test_a_cards_own_image_and_title_links_count_once_as_repeats():
    from anyuse.execution import demo_compile as C
    x, c = "https://www.heiz.test/heizspiegel", "https://www.heiz.test/category/news"
    els = [{"tag": "a", "href": x, "text": "Heizspiegel 2026"}, {"tag": "a", "href": x, "text": "Mehr lesen"}]
    els += [{"tag": "a", "href": x, "text": "Heizspiegel 2026"}, {"tag": "a", "href": x, "text": "Heizspiegel 2026"}]
    els += [{"tag": "a", "href": c, "text": "News"} for _ in range(3)]
    items = [{"url": f"https://www.heiz.test/a{i}"} for i in range(3)]
    news = [dict(items[0]), {"url": x, "own_repeat": 2}, dict(items[1]), dict(items[2])]
    # the article: 3 repeats, 2 of them its own card's image and title -> a result
    assert [it["url"] for it in C.drop_categories(news, {"elements": els})] == [it["url"] for it in news]
    # the same item read without its card (an older read): 3 repeats -> a category, as before
    assert x not in [it["url"] for it in C.drop_categories([dict(news[1], own_repeat=None)] + items, {"elements": els})]
    # a category linked once from each of three cards stays a category
    assert c not in [it["url"] for it in C.drop_categories(items + [{"url": c, "own_repeat": 1}], {"elements": els})]


def test_a_loose_card_spec_takes_a_sibling_card_of_another_type():
    base = ["main.site-content", "div.wp-block-query", "ul.wp-block-post-template"]
    spec = {"kind": "card", "chain": base + ["li.wp-block-post.page"], "loose": True}
    assert K.chain_matches(base + ["li.wp-block-post.post"], spec)
    assert not K.chain_matches(base + ["li.wp-block-post.post"], dict(spec, loose=False))
    assert not K.chain_matches(base + ["li.sidebar-post.post"], spec)                 # another first class
    assert not K.chain_matches(["aside.widget"] + base[1:] + ["li.wp-block-post.post"], spec)   # another list
    assert not K.chain_matches(base + ["li.wp-block-post.post"], dict(spec, kind="title"))


WP = [("pumpe-a", "page", "Wärmepumpe im Test"), ("pumpe-b", "page", "Wärmepumpe oder Gas"),
      ("pumpe-c", "page", "Wärmepumpe Kosten"), ("spiegel", "post", "Heizspiegel 2026: Heizen wird teurer"),
      ("spiegel-alt", "page", "Heizspiegel 2025 im Rückblick"), ("spiegel-gas", "page", "Gas und Heizspiegel")]


class WordPress(D.DomSite):
    """A WordPress search page: each result an ``li.wp-block-post`` card whose further class names its post type."""

    def __init__(self):
        super().__init__([])

    def page(self, q):
        cards = "".join(f'<li class="wp-block-post post-{n} {t} type-{t}"><a href="/{p}" class="thumb">'
                        f'{html.escape(x)}</a><h2 class="wp-block-post-title"><a href="/{p}">'
                        f'{html.escape(x)}</a></h2><p>Ein Beitrag.</p></li>'
                        for n, (p, t, x) in enumerate(WP) if q and q.lower() in x.lower())
        news = f'<div class="news-box"><a href="/spiegel">{html.escape(WP[3][2])}</a> <a href="/spiegel">Mehr lesen</a></div>'
        return (f'<html><body><header><form action="/" method="get"><input type="search" name="s" '
                f'value="{html.escape(q)}"></form></header><main class="site-content">{news}<div class="wp-block-query">'
                f'<ul class="wp-block-post-template">{cards}</ul></div></main></body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        return D.Recorded(url, self.page(dict(urllib.parse.parse_qsl(sp.query)).get("s", ""))), url


def test_a_compiled_tool_reads_a_result_of_another_post_type():
    site = WordPress()
    tools = HT.BrowserTools(D.DomPage(site), "https://www.heiz.test/", "www.heiz.test")
    pol = P.ScriptedPolicy([T.Search(0, "Wärmepumpe"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if e.href and "/pumpe" in e.href]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="wp").run("search for {q}",
                                                                                     {"q": "Wärmepumpe"})
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    assert all(s.get("loose") for s in c["tool"]["region"]["cards"] if s.get("kind") == "card")
    out = HC.replay_web(c["tool"], site, "Heizspiegel")
    assert [r["url"].rsplit("/", 1)[-1] for r in out["output"]["results"]] == ["spiegel", "spiegel-alt", "spiegel-gas"]
    # a tool compiled before the rule (exact card specs) reads as it did: the post-type card is not its result
    old = dict(c["tool"], region=dict(c["tool"]["region"], cards=[{k: v for k, v in s.items() if k != "loose"}
                                                                  for s in c["tool"]["region"]["cards"]]))
    out_old = HC.replay_web(old, site, "Heizspiegel")
    assert [r["url"].rsplit("/", 1)[-1] for r in out_old["output"]["results"]] == ["spiegel-alt", "spiegel-gas"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
