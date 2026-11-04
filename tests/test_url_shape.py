"""The result URL SHAPE, offline. Pinned:
* the shape is the form of the segment that names each result - the path's LAST segment, a WORD (a letter) or a NUMBER
  (no letter) - or the site's ROOT; a link of another shape than the majority of a result list's distinct targets is
  no result, whatever its repeat count; no majority decides nothing; a list is never cut below two by shape;
* the extractor applies it at every call site: the answer resolver (``answer_refs.resolve_answer``), the region readers
  (``demo_compile.drop_categories``: ``region_items`` / ``region_read``) and the card reader's result link
  (``demo_cards.read_region_v6``): a news site whose cards also link their category once (www.cicloweb.it) compiles
  without the category, and a tool compiled WITH the category's chain (a frozen tool) replays without it;
* the verifier reads it with its own code (``attribute_verify`` section 6e): an answered category among
  the articles is a CONFLICT, the real article it took the place of is a skipped result (the order check), the site's
  home page listed among the results (www.technikaffe.de) is no result an answer could skip, results of different
  sections (``/news/x`` beside ``/news/rumors/y``) share the shape, and a list with no majority shape is UNCERTAIN.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_url_shape.py
"""
import html
import pathlib
import sys
import tempfile
import urllib.parse

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import answer_refs as AR  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import url_identity as UI  # noqa: E402

NBASE = "https://www.news.test"
#: (id, slug, title, category id, category name) - each category linked by ONE card only (the repeat rule never fires)
STORIES = [(9001, "van-der-poel-vince-ancora", "Van der Poel vince ancora, pista e Tour", 31, "Pista"),
           (9002, "uci-pubblica-la-tabella-punti", "UCI pubblica la tabella punti del Tour", 32, "Professionisti"),
           (9003, "alaphilippe-si-ritira", "Alaphilippe si ritira dopo il Tour", 33, "Ciclomercato"),
           (9004, "reusser-predica-acqua", "Marlen Reusser predica acqua al Tour", 34, "Donne"),
           (9005, "mtb-mondiali-in-valle", "Mondiali MTB in valle dopo il Tour", 35, "Mountain Bike"),
           (9006, "giro-delle-fiandre-percorso", "Il percorso del Fiandre, come il Tour", 36, "Classiche")]


def card(sid, slug, title, cid, cname):
    art = f"/news/{sid}/{slug}"
    return (f'<div class="feed-item-wrapper"><div class="row"><div class="col-4"><a href="{art}" class="d-block">'
            f'<img src="/i/{sid}.webp" alt=""></a></div><div class="col"><a href="/news/category/{cid}" class="green">'
            f'<small>{html.escape(cname)}</small></a><h3 class="feed-item-title"><a href="{art}">{html.escape(title)}</a>'
            f'</h3><p>Testo</p></div></div></div>')


def news_page(stories, q=""):
    return (f'<html><body><header><nav><a href="/">Home</a></nav><form action="/search" method="get">'
            f'<input type="search" name="q" value="{html.escape(q)}"></form></header><main><h1>Risultati per "'
            f'{html.escape(q)}"</h1><div class="feed">{"".join(card(*s) for s in stories)}</div></main>'
            f'<footer><a href="/privacy">Privacy</a></footer></body></html>')


class NewsSite(D.DomSite):
    """A www.cicloweb.it-like news search: each card links its article twice (image, title) and its category once."""

    def __init__(self):
        super().__init__([])

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path == "/search" and "q" in q:
            words = [w for w in q["q"].lower().split() if w]
            hits = [s for s in STORIES if any(w in s[2].lower() for w in words)]
            return D.Recorded(url, news_page(hits, q["q"])), url
        if sp.path in ("", "/"):
            return D.Recorded(url, news_page([], "")), url
        return D.Recorded(url, "<html><body><h1>Articolo</h1></body></html>"), url


def page(dom, u=NBASE + "/search?q=tour"):
    return AV.Page(dom, "", u)


# ------------------------------------------------------------------------------------------------- the shape itself
def test_the_shape_is_the_last_segments_form_or_the_root():
    assert UI.url_shape("https://s.test/news/123/a-slug") == "word"
    assert UI.url_shape("https://s.test/news/category/123") == "number"
    assert UI.url_shape("https://s.test/2026/10/12") == "number"
    assert UI.url_shape("https://s.test/management/7637479.html") == "word"      # an extension is part of the segment
    assert UI.url_shape("https://s.test/") == UI.url_shape("https://s.test") == "root"
    arts = [f"https://s.test/news/{i}/story-{i}" for i in range(4)]
    assert UI.majority_shape(arts + ["https://s.test/news/category/7"]) == "word"
    assert UI.off_shape(arts[:3] + ["https://s.test/news/category/7"] + arts[3:]) == [3]
    # a target counts once; on a tie of targets the links decide (a card links its article twice, its category once);
    # no majority of either: nothing is decided; never below two
    assert UI.majority_shape(arts[:2] + ["https://s.test/c/1", "https://s.test/c/2"]) == ""
    assert UI.majority_shape(arts[:1] * 3 + ["https://s.test/c/1"]) == "word"
    assert UI.majority_shape(arts[:3] + ["https://s.test/c/1"] * 5) == "word"
    assert UI.off_shape(arts[:2] + ["https://s.test/c/1", "https://s.test/c/2"]) == []
    assert UI.off_shape(arts[:2] + ["https://s.test/c/1", "https://s.test/c/2"],
                        counts={UI.identity(u): 2 for u in arts}) == [2, 3]
    assert UI.off_shape(arts[:2] + ["https://s.test/c/1"], keep=3) == []
    assert UI.off_shape(arts[:3] + [""]) == []                                     # a result without a link stays
    UI.SHAPE_RULE = False
    try:
        assert UI.off_shape(arts + ["https://s.test/news/category/7"]) == []
    finally:
        UI.SHAPE_RULE = True


# ------------------------------------------------------------------------------------------------- the extractor
def test_the_resolver_drops_a_category_linked_once_by_its_shape():
    links = [AR.Link(1, f"{NBASE}/news/9001/van-der-poel", "Van der Poel"),
             AR.Link(2, f"{NBASE}/news/category/31", "Pista"),
             AR.Link(3, f"{NBASE}/news/9002/uci", "UCI"), AR.Link(4, f"{NBASE}/news/9003/alaphilippe", "Alaphilippe")]
    occ = {AR._norm(l.href): 1 for l in links}
    got, rep = AR.resolve_answer("site_search", {"results": [{"element": k} for k in (1, 2, 3, 4)]}, links,
                                 occurrences=occ)
    assert [r["title"] for r in got["results"]] == ["Van der Poel", "UCI", "Alaphilippe"] and rep["off_shape"] == 1
    # one article and one category: no majority shape - nothing is dropped by shape
    got, rep = AR.resolve_answer("site_search", {"results": [{"element": 1}, {"element": 2}]}, links, occurrences=occ)
    assert len(got["results"]) == 2 and "off_shape" not in rep


def test_the_region_readers_leave_an_off_shape_item_out():
    read = {"elements": [{"tag": "a", "href": f"{NBASE}/news/category/31", "text": "Pista"}] +
            [{"tag": "a", "href": f"{NBASE}/news/{i}/story-{i}", "text": f"Story {i}"} for i in range(3)]}
    items = [{"url": f"{NBASE}/news/category/31", "title": "Pista"}] + \
        [{"url": f"{NBASE}/news/{i}/story-{i}", "title": f"Story {i}"} for i in range(3)]
    assert [it["title"] for it in C.drop_categories(items, read)] == ["Story 0", "Story 1", "Story 2"]
    assert C.drop_categories(items[:2], read) == items[:2]                    # never below two results


def demo(site, value, take_category):
    tools = HT.BrowserTools(D.DomPage(site), NBASE + "/", "www.news.test")

    def answer(t, o, h):
        rows = []
        for e in o.elements:
            if "/news/" in e.href and "/category/" not in e.href and e.text and not rows:
                rows.append({"element": e.index})
            elif "/news/category/" in e.href and take_category and len(rows) == 1:
                rows.append({"element": e.index})                   # the 2nd card's category, as the demonstrator did
            elif "/news/" in e.href and "/category/" not in e.href and e.text and len(rows) < 4 and \
                    all(o.elements[r["element"]].href != e.href for r in rows if r["element"] < len(o.elements)):
                rows.append({"element": e.index})
        return T.Done({"results": rows})
    pol = P.ScriptedPolicy([T.Search(0, value), answer])
    return H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="news").run("search for {q}",
                                                                                          {"q": value})


def test_a_news_site_whose_cards_link_their_category_compiles_without_it():
    site = NewsSite()
    tr = demo(site, "Tour", take_category=True)
    assert all("/category/" not in r["url"] for r in tr.result["results"]), tr.result
    assert (tr.refs or {}).get("off_shape") == 1
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    out = HC.replay_web(c["tool"], site, "Tour")
    rows = out["output"]["results"]
    assert out["status"] == "ok" and rows and all("/category/" not in r["url"] for r in rows)
    assert rows[0]["url"].endswith("/news/9001/van-der-poel-vince-ancora")
    assert HC.independent_verdict(c["tool"], out)["verdict"] == "accept"


def test_a_tool_compiled_with_the_category_chain_replays_without_the_category():
    site = NewsSite()
    # a tool compiled before the shape rule and before a card's links read as one result
    UI.SHAPE_RULE, AR.SAME_RECORD = False, False
    try:
        tr = demo(site, "Tour", take_category=True)
        assert any("/category/" in r["url"] for r in tr.result["results"])
        c = HC.compile_web(tr)
        assert c["ok"], c["why"]
        before = HC.replay_web(c["tool"], site, "Tour")
    finally:
        UI.SHAPE_RULE, AR.SAME_RECORD = True, True
    after = HC.replay_web(c["tool"], site, "Tour")
    assert after["status"] == "ok"
    rows = after["output"]["results"]
    assert rows and all("/category/" not in r["url"] for r in rows), rows
    assert [r["url"] for r in rows][:2] == [f"{NBASE}/news/9001/van-der-poel-vince-ancora",
                                            f"{NBASE}/news/9002/uci-pubblica-la-tabella-punti"]
    if before["status"] == "ok" and any("/category/" in r["url"] for r in before["output"]["results"]):
        assert HC.independent_verdict(c["tool"], before)["verdict"] != "accept"
    assert HC.independent_verdict(c["tool"], after)["verdict"] == "accept"


# ------------------------------------------------------------------------------------------------- the verifier
def decisions(ans, dom, scope=("www.news.test",)):
    v = AV.check("site_search", ans, page(dom), None, scope=list(scope))
    return {d.attr: d for d in v.decisions if d.attr in ("shape", "order")}, v


def test_the_verifier_calls_an_answered_category_no_result_and_sees_the_skipped_article():
    dom = news_page(STORIES, "Tour")
    # the frozen answer: card 1's category in place of its article, articles 2-4, card 5's category
    ans = {"results": [{"title": "Pista", "url": f"{NBASE}/news/category/31"}] +
           [{"title": t, "url": f"{NBASE}/news/{i}/{s}"} for i, s, t, _c, _n in STORIES[1:4]] +
           [{"title": "Mountain Bike", "url": f"{NBASE}/news/category/35"}]}
    ds, v = decisions(ans, dom)
    assert ds["shape"].status == AV.CONFLICT and "category/31" in ds["shape"].why
    assert ds["order"].status == AV.CONFLICT and "skipped" in ds["order"].why and "result 1" in ds["order"].why
    assert v.verdict != AV.ACCEPT
    # the rule off: the categories sit in their cards, in the page's order - the earlier verifier verified that order
    AV.SHAPE_CHECK = False
    try:
        ds0, _ = decisions(ans, dom)
        assert "shape" not in ds0 and ds0["order"].status == AV.VERIFIED
    finally:
        AV.SHAPE_CHECK = True
    # the right answer: the first four articles
    good = {"results": [{"title": t, "url": f"{NBASE}/news/{i}/{s}"} for i, s, t, _c, _n in STORIES[:4]]}
    ds, _ = decisions(good, dom)
    assert ds["shape"].status == AV.VERIFIED and ds["order"].status == AV.VERIFIED


def test_the_sites_home_page_listed_among_the_results_is_no_result_to_skip():
    posts = [f"/{s}/" for s in ("mini-pc-test", "nas-stromverbrauch", "router-test", "handy-akku", "cpu-vergleich")]
    lis = [f'<li class="post"><h2><a href="{u}">Titel {k}</a></h2></li>' for k, u in enumerate(posts)]
    lis.insert(2, '<li class="post"><h2><a href="/">Technikaffe.de: Tests, Anleitungen</a></h2></li>')
    dom = (f'<html><body><header><nav><a href="/">Start</a></nav></header><main><ul class="results">{"".join(lis)}'
           f'</ul></main></body></html>')
    ans = {"results": [{"title": f"Titel {k}", "url": f"https://www.blog.test{u}"} for k, u in enumerate(posts)]}
    v = AV.check("site_search", ans, AV.Page(dom, "", "https://www.blog.test/?s=x"), None, scope=["www.blog.test"])
    ds = {d.attr: d for d in v.decisions}
    assert ds["shape"].status == AV.VERIFIED and ds["order"].status == AV.VERIFIED, ds["order"].why
    AV.SHAPE_CHECK = False
    try:
        v0 = AV.check("site_search", ans, AV.Page(dom, "", "https://www.blog.test/?s=x"), None, scope=["www.blog.test"])
        assert {d.attr: d for d in v0.decisions}["order"].status == AV.CONFLICT     # held back before
    finally:
        AV.SHAPE_CHECK = True


def test_results_of_different_sections_share_the_shape_and_no_majority_is_uncertain():
    urls = ["/news/fujifilm-x-pro4", "/news/rumors/fujifilm-novita", "/articoli/opinioni/focale-ritratto",
            "/news/instax-pal-2"]
    lis = "".join(f'<li><h3><a href="{u}">Titolo {k}</a></h3></li>' for k, u in enumerate(urls))
    dom = f'<html><body><main><ul class="r">{lis}</ul></main></body></html>'
    ans = {"results": [{"title": f"Titolo {k}", "url": f"https://www.foto.test{u}"} for k, u in enumerate(urls)]}
    v = AV.check("site_search", ans, AV.Page(dom, "", "https://www.foto.test/?s=x"), None, scope=["www.foto.test"])
    assert {d.attr: d for d in v.decisions}["shape"].status == AV.VERIFIED
    mixed = ["/p/1001", "/p/1002", "/p/blue-shoe", "/p/red-shoe"]
    lis = "".join(f'<li><h3><a href="{u}">Item {k}</a></h3></li>' for k, u in enumerate(mixed))
    dom = f'<html><body><main><ul class="r">{lis}</ul></main></body></html>'
    ans = {"results": [{"title": f"Item {k}", "url": f"https://www.shop.test{u}"} for k, u in enumerate(mixed[:2])]}
    v = AV.check("site_search", ans, AV.Page(dom, "", "https://www.shop.test/?s=x"), None, scope=["www.shop.test"])
    d = {x.attr: x for x in v.decisions}["shape"]
    assert d.status != AV.VERIFIED and v.verdict != AV.ACCEPT


def test_the_verifier_reads_the_shape_with_its_own_code():
    src = (_HERE / "execution" / "attribute_verify.py").read_text(encoding="utf-8")
    assert "import url_identity" not in src and "url_identity import" not in src
    assert AV.url_shape("news.test/news/category/31") == "number" and AV.url_shape("news.test/") == "root"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
