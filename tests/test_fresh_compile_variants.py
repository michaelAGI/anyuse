"""Compiling on unseen sites (variant paths and the www.jazzzeitung.de read-back), offline. Pinned:
* result links whose path AND a query value vary per item (product variants: a wine's vintages share a path,
  told apart by ``articleNo``) form a path cluster (``param`` "item+query"); the value stays part of each target's
  identity, the compiled tool reads every variant and navigates the full URL; a query value on ONE constant path
  (``/item?id=3``) is still that path's query cluster;
* an ``article``'s own ``header`` / ``footer`` is content, never page chrome - in the READ mirror (``area_of``), the
  link scanner and both page scripts; a header in ``main`` / ``section`` / ``body`` stays chrome;
* an orphan link to a target a card already read (a post's date link) is that result's second link, never a card of
  another layout: a blog whose titles sit in ``article > header`` compiles, replays ``ok`` and is accepted by the
  independent judge. No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_fresh_compile_variants.py
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
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import discover as DV  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import url_identity as UI  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

WINES = [("Baron Reserva", "baron-reserva-P1", [101, 102]), ("Palador Crianza", "palador-crianza-P2", [201]),
         ("Avior Reserva", "avior-reserva-P3", [301]), ("Tobia Crianza", "tobia-crianza-P4", [401, 402])]
WBASE = "https://www.wine.test"


class WineShop(D.DomSite):
    """Search results link each wine's variants by path + ``articleNo`` (vintages share the path)."""

    def __init__(self):
        super().__init__([])

    def start(self):
        return ('<html><body><header><nav><a href="/">Start</a></nav><form action="/search" method="get">'
                '<input type="search" name="q"></form></header><main><h1>Weine</h1></main></body></html>')

    def results(self, q):
        cards = "".join(f'<li class="product"><h3><a href="/{slug}/?filter.region=Rioja&amp;articleNo={no}">'
                        f'{html.escape(name)} {2019 + j}</a></h3><span class="price">12,90 €</span></li>'
                        for name, slug, nos in WINES for j, no in enumerate(nos)
                        if any(w in name.lower() for w in q.lower().split()))
        return (f'<html><body><header><nav><a href="/">Start</a></nav><form action="/search" method="get">'
                f'<input type="search" name="q" value="{html.escape(q)}"></form></header><main><h1>Treffer</h1>'
                f'<ul class="products">{cards}</ul></main></body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path == "/search" and "q" in q:
            return D.Recorded(url, self.results(q["q"])), url
        if sp.path in ("", "/"):
            return D.Recorded(url, self.start()), url
        return D.Recorded(url, "<html><body><h1>Wein</h1></body></html>"), url


def test_a_path_plus_identity_query_value_forms_a_region_and_keeps_every_variant():
    urls = [f"{WBASE}/baron-reserva-P1/?articleNo=101", f"{WBASE}/baron-reserva-P1/?articleNo=102",
            f"{WBASE}/palador-crianza-P2/?articleNo=201"]
    assert UI.path_companions(urls) == {"articleNo"} and UI.page_noise(urls) == set()   # identity, not noise
    assert UI.identity(urls[0]) != UI.identity(urls[1])
    assert UI.path_companions(["https://s.test/item?id=3", "https://s.test/item?id=4"]) == set()
    site = WineShop()
    page = site.read(*site.route(WBASE + "/search?q=reserva crianza"))
    want = [e["href"] for e in page["elements"] if e.get("tag") == "a" and "articleNo" in str(e.get("href"))]
    DV.PATH_QUERY_CLUSTERS = False
    assert C.region_from_dom(page["dom"], page["url"], want) is None or \
        C.region_from_dom(page["dom"], page["url"], want)["want_in_cluster"] < len(want)
    DV.PATH_QUERY_CLUSTERS = True
    reg = C.region_from_dom(page["dom"], page["url"], want)
    assert reg is not None and reg["want_in_cluster"] == len({UI.path_only(u) for u in want}) and "{v}" in reg["template"]
    tools = HT.BrowserTools(D.DomPage(site), WBASE + "/", "www.wine.test")
    pol = P.ScriptedPolicy([T.Search(0, "Reserva"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if "articleNo" in e.href]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="wine").run("search for {q}",
                                                                                        {"q": "Reserva"})
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    out = HC.replay_web(c["tool"], site, "Crianza")
    rows = out["output"]["results"]
    assert out["status"] == "ok" and len(rows) == 3 and all("articleNo=" in r["url"] for r in rows)
    assert len({r["url"] for r in rows}) == 3                                  # the two Tobia vintages stay two


def node_of(html_text, tag_text):
    root, _base = K.parse(html_text)
    return next(n for n in K.walk(root) if n.tag == "a" and K.text_of(n) == tag_text)


def test_an_articles_own_header_is_content_and_a_page_header_stays_chrome():
    dom = ('<html><body><header><a href="/">Logo</a></header><main><section><header><a href="/m">Menu</a></header>'
           '<article><header><h2><a href="/2024/05/post">Post title</a></h2></header>'
           '<footer><a href="/2024/05/post#c">Kommentar</a></footer></article></section></main></body></html>')
    assert K.area_of(node_of(dom, "Logo")) == "header" and K.area_of(node_of(dom, "Menu")) == "header"
    assert K.area_of(node_of(dom, "Post title")) == "main" and K.area_of(node_of(dom, "Kommentar")) == "main"
    scan = DV.scan_page(dom, "https://b.test/")
    chrome = {ln.text: ln.chrome for ln in scan.links}
    assert chrome["Logo"] and not chrome["Post title"]
    src = (pathlib.Path(__file__).resolve().parent.parent / "execution" / "demo_cards.py").read_text(encoding="utf-8")
    for js in (RR.READ_JS, src):
        assert "=== 'ARTICLE'" in js and "a.parentElement.closest('article,aside,main,nav,section')" in js


class Blog(D.DomSite):
    """A WordPress-like blog: every result title in ``article > header``, a date link to the same post, "Weiterlesen"."""

    POSTS = [("2024/04", "joe-viera", "Zum Tod der Jazzlegende Joe Viera"),
             ("2022/09", "pharoah-sanders", "Zum Tod des Saxophonisten Pharoah Sanders"),
             ("2022/03", "jakob-manz", "Jakob Manz erhält den Jazzpreis"), ("2021/11", "nicole-j", "Saxophone Workbook")]

    def __init__(self):
        super().__init__([])

    def page(self, q):
        arts = "".join(f'<article class="post"><header class="entry-header"><h2 class="entry-title">'
                       f'<a href="/cms/{d}/{s}/">{html.escape(t)}</a></h2><div class="entry-meta"><a href="/cms/{d}/{s}/">'
                       f'{d}</a></div></header><p>Text</p><a href="/cms/{d}/{s}/">Weiterlesen</a></article>'
                       for d, s, t in self.POSTS if q and (any(w in t.lower() for w in q.lower().split())
                                                           or q.lower() in ("saxophon", "jazz")))
        return (f'<html><body><header class="site-header"><a href="/cms/">Jazzzeitung</a><form action="/cms/" method="get">'
                f'<input type="search" name="s" value="{html.escape(q)}"></form></header><main>{arts}</main></body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        return D.Recorded(url, self.page(q.get("s", ""))), url


def test_a_blog_with_titles_in_article_headers_compiles_and_replays():
    site = Blog()
    tools = HT.BrowserTools(D.DomPage(site), "https://www.blog.test/cms/", "www.blog.test")
    pol = P.ScriptedPolicy([T.Search(0, "Saxophon"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if e.text == "Weiterlesen"]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="blog").run("search for {q}",
                                                                                        {"q": "Saxophon"})
    assert [r["title"] for r in tr.result["results"]][:1] == ["Zum Tod der Jazzlegende Joe Viera"]   # no label title
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    out = HC.replay_web(c["tool"], site, "Jazz")
    assert out["status"] == "ok", out.get("why")
    assert HC.independent_verdict(c["tool"], out)["verdict"] == "accept"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
