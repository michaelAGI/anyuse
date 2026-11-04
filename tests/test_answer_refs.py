"""ONE resolver for an agent's references (execution/answer_refs.py; final-web-fresh defect 1), offline.
Pinned here: the observation shows a cut URL / text with the cut mark; the resolver turns element numbers, exact links,
cut URLs that start exactly one link (narrowed by the title when several) and a product's cut URL into the page's full
references; an ambiguous cut reference stays UNCERTAIN (never guessed) and the compile refuses it; the harness
resolves every done answer against the observation it was given on, so the compile, the report and the fallback read
resolved answers - on a shop whose product URLs are longer than the shown 70 characters, a run that answers exactly
what the observation showed now compiles, and so does one that answers element numbers. No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_answer_refs.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import answer_refs as AR  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import web_tool_call as WT  # noqa: E402

import test_harness_compile as THC  # noqa: E402
import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE
LONG = "/produkte/kategorie-werkzeug/akkuschrauber-und-bohrmaschinen/artikel-"   # every product URL > 70 characters
A = "https://www.shop.test/produkte/kategorie-werkzeug/akkuschrauber-und-bohrmaschinen/artikel-bosch-gsr-12v"
B = "https://www.shop.test/produkte/kategorie-werkzeug/akkuschrauber-und-bohrmaschinen/artikel-makita-df333"
LINKS = [AR.Link(3, A, "Bosch GSR 12V Akkuschrauber"), AR.Link(4, B, "Makita DF333 Akkuschrauber"),
         AR.Link(5, "https://www.shop.test/hilfe", "Hilfe")]


def test_the_observation_shows_a_cut_string_with_the_mark():
    e = T.Element(index=3, kind="link", text="x" * 90, href=A)
    line = e.line()
    assert ("-> " + A[:70] + AR.CUT_MARK) in line and A not in line and ("x" * 80 + AR.CUT_MARK) in line
    assert T.Element(index=1, kind="link", text="Hilfe", href="https://www.shop.test/hilfe").line().endswith(
        "-> https://www.shop.test/hilfe")


def test_element_numbers_exact_links_and_unique_cut_urls_resolve():
    got, rep = AR.resolve_answer("site_search", {"results": [{"element": 4}, 3, {"title": "Hilfe",
                                                                             "url": "https://www.shop.test/hilfe"}]}, LINKS)
    assert [r["url"] for r in got["results"]] == [B, A, "https://www.shop.test/hilfe"]
    assert got["results"][0]["title"] == "Makita DF333 Akkuschrauber" and rep["element"] == 2 and rep["exact"] == 1
    assert AR.refusal(rep) == ""
    # what the observation SHOWED (cut with the mark): the shared start is narrowed by the title
    shown = [{"title": AR.shown(l.text, 80), "url": AR.shown(l.href, 70)} for l in LINKS[:2]]
    got, rep = AR.resolve_answer("site_search", {"results": shown}, LINKS)
    assert [r["url"] for r in got["results"]] == [A, B] and rep.get("prefix_title") == 2 and not rep["uncertain"]
    # a cut URL without the mark that starts exactly one link (the fallback's live case)
    links = [AR.Link(None, "https://www.technikaffe.de/anleitung-354-synology_ds216_test__2_bay_nas_mit_intel/",
                     "Synology DS216+ Test")]
    got, rep = AR.resolve_answer("site_search", {"results": [{"title": "Synology DS216+ Test",
                                                              "url": links[0].href[:70]}]}, links)
    assert got["results"][0]["url"] == links[0].href and rep["prefix"] == 1


def test_an_ambiguous_cut_reference_stays_uncertain_and_is_never_guessed():
    got, rep = AR.resolve_answer("site_search", {"results": [{"url": A[:70] + AR.CUT_MARK}]}, LINKS)
    assert got["results"][0]["url"] == A[:70] + AR.CUT_MARK            # kept as answered
    assert rep["uncertain"] and "starts 2 different links" in rep["uncertain"][0]["why"]
    assert "never guessed" in AR.refusal(rep)
    _g, rep = AR.resolve_answer("site_search", {"results": [{"element": 99}]}, LINKS)
    assert rep["uncertain"] and "element 99" in rep["uncertain"][0]["why"]
    # an answer that is no link at all is unmatched (the consumer's own checks refuse it), not uncertain
    _g, rep = AR.resolve_answer("site_search", {"results": [{"title": "x", "url": "https://elsewhere.test/a"}]}, LINKS)
    assert rep["unmatched"] and not rep["uncertain"]


def test_a_product_answers_cut_url_is_the_loaded_page():
    got, rep = AR.resolve_answer("product_lookup", {"name": "Bosch", "url": A[:70] + AR.CUT_MARK}, LINKS, A + "#top")
    assert got["url"] == A and rep["prefix"] == 1
    got, _ = AR.resolve_answer("product_lookup", {"name": "Bosch"}, LINKS, A)
    assert got["url"] == A
    # no_results and summaries pass through
    assert AR.resolve_answer("site_search", {"no_results": True}, LINKS)[0] == {"no_results": True}


def long_shop():
    return SHOP.Shop(ppath=LONG)


def run_shop(done):
    site = long_shop()
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", SITE)
    pol = P.ScriptedPolicy([lambda t, o, h: T.Search(0, "Akkuschrauber"), done])
    return H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="refs").run(
        "search the shop for {q}", {"q": "Akkuschrauber"}), site


def as_shown(o):
    return [{"title": AR.shown(e.text, 80), "url": AR.shown(e.href, 70)} for e in o.elements if LONG in e.href][:3]


def test_the_harness_resolves_the_shown_answer_and_the_compile_accepts_it():
    tr, site = run_shop(lambda t, o, h: T.Done({"results": as_shown(o)}))
    assert tr.ok and all(LONG in r["url"] and AR.CUT_MARK not in r["url"] for r in tr.result["results"])
    assert len(tr.result["results"][0]["url"]) > 70 and tr.refs.get("prefix_title", 0) + tr.refs.get("prefix", 0) == 3
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    rep = HC.compile_and_save_web(tr, THC.storage(), site, ["Bohrhammer", "Makita"])
    assert rep["status"] == "ready", rep["reasons"]


def test_element_number_answers_compile_too_and_reach_the_report():
    tr, _site = run_shop(lambda t, o, h: T.Done({"results": [{"element": e.index} for e in o.elements
                                                             if LONG in e.href][:3]}))
    assert tr.ok and tr.refs["element"] == 3 and HC.compile_web(tr)["ok"]
    rep = H.report(tr)
    assert rep["refs"]["element"] == 3 and all(LONG in r["url"] for r in rep["result"]["results"])


def test_an_uncertain_answer_is_refused_by_the_compile():
    tr, _site = run_shop(lambda t, o, h: T.Done({"results": [{"url": AR.shown(e.href, 70)} for e in o.elements
                                                             if LONG in e.href][:2]}))
    assert tr.ok and tr.refs["uncertain"]
    c = HC.compile_web(tr)
    assert not c["ok"] and c["why"].startswith("ambiguous_reference")


def test_the_observation_keeps_the_full_link_text_and_the_resolved_title_is_whole():
    title = "Synology DS216+ Test - 2 Bay NAS mit Intel Braswell und AES-Ni, Stromverbrauch und Leistung im Vergleich"
    url = "https://www.long.test/anleitung-354-synology_ds216_test__2_bay_nas_mit_intel_braswell_und_aes_ni/"
    dom = f'<html><body><main><h1>Suche</h1><a href="{url}">{title}</a><a href="/x">X</a></main></body></html>'
    site = D.DomSite([D.Recorded("https://www.long.test/?s=nas", dom)])
    tools = HT.BrowserTools(D.DomPage(site), "https://www.long.test/?s=nas", "www.long.test")
    o = tools.observe()
    e = next(e for e in o.elements if e.href == url)
    assert e.text == title and (AR.CUT_MARK in e.line())
    got, rep = AR.resolve_on_observation("site_search", {"results": [{"title": AR.shown(title, 80),
                                                                      "url": AR.shown(url, 70)}]}, o)
    assert got["results"][0] == {"title": title, "url": url} and not rep["uncertain"]


def test_a_label_is_never_a_title_and_a_repeated_target_is_one_result():
    links = [AR.Link(1, A, "Bosch GSR 12V Akkuschrauber"), AR.Link(2, A, "Weiterlesen"),
             AR.Link(3, B, "Makita DF333 Akkuschrauber"), AR.Link(4, B, "Weiterlesen"),
             AR.Link(5, "https://www.shop.test/kat/akku", "Akku"), AR.Link(6, "https://www.shop.test/kat/akku", "Akku")]
    got, rep = AR.resolve_answer("site_search", {"results": [{"element": 2}, {"element": 4}, {"element": 1}]}, links)
    assert [r["title"] for r in got["results"]] == ["Bosch GSR 12V Akkuschrauber", "Makita DF333 Akkuschrauber"]
    assert rep["duplicate"] == 1                       # element 1 is the same target as element 2
    # a text repeated for ONE target (a tag link in every card) is not a label by this rule; it stays its own row
    got, _ = AR.resolve_answer("site_search", {"results": [{"element": 5}]}, links)
    assert got["results"][0]["title"] == "Akku"


def test_the_pages_own_cards_decide_a_label_before_the_lexicon():
    base = "https://www.nieuws.test/artikel/"
    cards = [("zee", "Meer", "Het water is dit jaar warmer dan ooit gemeten"),
             ("bos", "Bossen in de winter", "Waarom de bomen hun bladeren vroeger laten vallen"),
             ("stad", "Een nieuwe brug voor de stad", "De gemeente kiest voor een ontwerp van hout")]
    links = []
    for i, (slug, heading, teaser) in enumerate(cards):
        links += [AR.Link(2 * i + 1, base + slug, heading), AR.Link(2 * i + 2, base + slug, teaser)]
    # "Meer" is a lexicon word ("more" in Dutch) and the heading of one card: the other cards show their second
    # link, no text repeats, so it is the card's title
    got, rep = AR.resolve_answer("site_search", {"results": [{"element": 1}, {"element": 3}]}, links)
    assert [r["title"] for r in got["results"]] == ["Meer", "Bossen in de winter"] and not rep["uncertain"]
    # a label the lexicon does not know, repeated on every card: a label by the page's own repetition
    pt = []
    for i, (slug, heading, _t) in enumerate(cards):
        pt += [AR.Link(2 * i + 1, base + slug, heading), AR.Link(2 * i + 2, base + slug, "Leia mais")]
    got, _ = AR.resolve_answer("site_search", {"results": [{"element": 4}]}, pt)
    assert got["results"][0]["title"] == "Bossen in de winter"
    # one card's "Weiterlesen" below its heading, the other cards with a heading and a teaser: still a label
    mixed = [AR.Link(1, A, "Bosch GSR 12V Akkuschrauber"), AR.Link(2, A, "Weiterlesen")]
    for i, (slug, heading, teaser) in enumerate(cards[1:]):
        mixed += [AR.Link(10 + 2 * i, base + slug, heading), AR.Link(11 + 2 * i, base + slug, teaser)]
    got, _ = AR.resolve_answer("site_search", {"results": [{"element": 2}]}, mixed)
    assert got["results"][0]["title"] == "Bosch GSR 12V Akkuschrauber"
    # one result card, no other card to compare: the lexicon decides, as before
    one = [AR.Link(1, A, "Bosch GSR 12V Akkuschrauber"), AR.Link(2, A, "Weiterlesen")]
    got, _ = AR.resolve_answer("site_search", {"results": [{"element": 2}]}, one)
    assert got["results"][0]["title"] == "Bosch GSR 12V Akkuschrauber"


def test_the_fallback_reads_its_final_page_with_the_same_resolver():
    dom = f'<html><body><a href="{A}">Bosch GSR 12V Akkuschrauber</a><a href="{B}">Makita DF333 Akkuschrauber</a></body></html>'
    out = WT.resolve_on_page("site_search", {"results": [{"title": "Bosch GSR 12V Akkuschrauber", "url": A[:70]}]},
                             dom, "https://www.shop.test/suche?q=x")
    assert out["results"][0]["url"] == A
    _o, rep = WT.resolve_report("site_search", {"results": [{"url": A[:70]}]}, dom, "https://www.shop.test/suche")
    assert rep["uncertain"]


# ------------------------------------------------------------------------- the links of one result card

def _guide():
    import test_verifier_units as W
    return W


def test_the_links_of_one_result_card_are_one_result():
    from anyuse.execution import attribute_verify as AV
    W = _guide()
    page = W.guide_page()
    answer = {"results": [{"title": W.NAMES[0], "url": W.fish(0)}, {"title": "aktuell 3.99€ bei Partner *", "url": W.AD},
                          {"title": W.NAMES[1], "url": W.fish(1)}]}
    merged, rep = AR.resolve_on_dom("site_search", answer, page._dom, page.url)
    assert [r["url"] for r in merged["results"]] == [W.fish(0), W.fish(1)], merged
    assert rep["same_record"] == 1 and rep["same_record_dropped"][0] == {"url": W.AD, "kept": W.fish(0)}
    assert AV.check("site_search", merged, W.guide_page(), AV.Resolver(None), value="Guppy").verdict == AV.ACCEPT
    assert AV.check("site_search", answer, W.guide_page(), AV.Resolver(None), value="Guppy").verdict == AV.WITHHOLD
    # without the page's DOM the resolver reads the rows as before
    plain, rep = AR.resolve_answer("site_search", answer, AR.links_from_dom(page._dom, page.url), page.url)
    assert len(plain["results"]) == 3 and "same_record" not in rep
    AR.SAME_RECORD = False
    try:
        off, rep = AR.resolve_on_dom("site_search", answer, page._dom, page.url)
        assert len(off["results"]) == 3 and "same_record" not in rep
    finally:
        AR.SAME_RECORD = True


def test_a_variant_answered_before_its_title_keeps_the_title_in_the_variants_place():
    names = ["alpha", "beta", "gamma"]
    cards = "".join(f'<li class="card"><h3><a href="https://s.test/p/{n}">Title {n}</a></h3>'
                    + (f'<div class="variants"><a href="https://s.test/p/{n}-red">Red</a>'
                       f'<a href="https://s.test/p/{n}-blue">Blue</a></div>' if n == "beta" else "")
                    + "<p>about it</p></li>" for n in names)
    dom = f'<html><body><main><ul class="results">{cards}</ul></main></body></html>'
    answer = {"results": [{"url": "https://s.test/p/alpha"}, {"url": "https://s.test/p/beta-red"},
                          {"url": "https://s.test/p/beta"}, {"url": "https://s.test/p/gamma"}]}
    got, rep = AR.resolve_on_dom("site_search", answer, dom, "https://s.test/s?q=x")
    assert [r["url"] for r in got["results"]] == [f"https://s.test/p/{n}" for n in names], got
    assert rep["same_record_dropped"] == [{"url": "https://s.test/p/beta-red", "kept": "https://s.test/p/beta"}]


def test_results_of_one_grid_row_stay_separate():
    grid = "".join('<div class="row">' + "".join(
        f'<div class="card"><h3><a href="https://s.test/g/{r}{c}">Grid item {r}{c}</a></h3><p>x y</p></div>'
        for c in range(3)) + "</div>" for r in range(2))
    dom = f'<html><body><main><div class="grid">{grid}</div></main></body></html>'
    answer = {"results": [{"url": f"https://s.test/g/{r}{c}"} for r in range(2) for c in range(3)]}
    got, rep = AR.resolve_on_dom("site_search", answer, dom, "https://s.test/s?q=x")
    assert len(got["results"]) == 6 and "same_record" not in rep


def test_the_harness_merges_on_the_observation_dom():
    from types import SimpleNamespace
    W = _guide()
    page = W.guide_page()
    els = [T.Element(index=1, kind="link", text=W.NAMES[0], href=W.fish(0)),
           T.Element(index=2, kind="link", text="aktuell 3.99€ bei Partner *", href=W.AD),
           T.Element(index=3, kind="link", text=W.NAMES[1], href=W.fish(1))]
    obs = SimpleNamespace(elements=els, mapping={}, url=page.url, page={"dom": page._dom, "url": page.url, "elements": []})
    got, rep = AR.resolve_on_observation("site_search", {"results": [{"element": 1}, {"element": 2}, {"element": 3}]},
                                         obs)
    assert [r["url"] for r in got["results"]] == [W.fish(0), W.fish(1)] and rep["same_record"] == 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
