"""The browser's accessibility tree as a general web signal. Pinned, on synthetic trees
(the CDP node shape ``Accessibility.getFullAXTree`` returns):
* the reader (``ax_tree.read_snapshot`` / ``read_raw``) joins the tree to the page reader's control and input orders
  through DOM nodes (a fake CDP page); a page without CDP and without a recorded snapshot gives no tree;
* (a) the agent's observation: a text field the tree calls a search box - next to a "Suchen" button
  (www.spurweite-n.de), in the search landmark, named "Produktsuche", role searchbox - is listed as a search box and
  typed by the same rule (``cdp_guard.AX_INPUT_JS`` live, ``dom_page.ax_input`` offline); a newsletter e-mail field is
  not; an icon button whose DOM text is empty is listed with its computed name (www.holzprofi.com's "Search");
  controls of the search and main landmarks come before the navigation's;
* (b) result regions: a link the tree puts in a navigation / banner / contentinfo / complementary landmark is never a
  region link; among clusters holding as many wanted results, the one outside the tree's chrome wins;
* (c) a link's computed name is the LAST title candidate (a "Weiterlesen" label link);
* (d) the verifier's own read (``attribute_verify.verify_ax``): a result whose link is missing, only in the chrome, or
  named otherwise is UNCERTAIN and FINAL (withheld; no model call is spent); agreement changes nothing; no tree, no
  decision. The verifier imports nothing from ``ax_tree``.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_ax_tree.py
"""
import asyncio
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import answer_refs as AR  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import ax_tree as AXT  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_policy as DPOL  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402

BASE = "https://www.shop.test"


class Snap:
    """A synthetic tree: ``add(role, name, parent, backend=..., url=...)`` -> node id; ``snap(ctl, inp, href)``."""

    def __init__(self):
        self.nodes = [{"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Shop"}}]

    def add(self, role, name="", parent="1", backend=0, url="", ignored=False):
        nid = str(len(self.nodes) + 1)
        n = {"nodeId": nid, "parentId": parent, "role": {"value": role}, "name": {"value": name}}
        if backend:
            n["backendDOMNodeId"] = backend
        if url:
            n["properties"] = [{"name": "url", "value": {"type": "string", "value": url}}]
        if ignored:
            n["ignored"] = True
        self.nodes.append(n)
        return nid

    def text(self, s, parent):
        return self.add("StaticText", s, parent)

    def snap(self, ctl=(), inp=(), href=()):
        return {"nodes": self.nodes, "ctl": list(ctl), "inp": list(inp), "href": list(href)}


def raw_of(snap):
    return AXT.compact(snap["nodes"], snap["ctl"], snap["inp"], snap["href"])


# ------------------------------------------------------------------------------------------------- the reader
def test_the_reader_joins_the_tree_to_the_page_readers_orders():
    s = Snap()
    main = s.add("main")
    s.add("link", "Kettle", main, backend=11, url=BASE + "/p/kettle")
    s.add("textbox", "", main, backend=21)

    class FakeCdp:
        async def call(self, method, params=None, timeout=0):
            return {"Accessibility.getFullAXTree": {"nodes": s.nodes}, "DOM.getDocument": {"root": {"nodeId": 1}},
                    "DOM.pushNodesByBackendIdsToFrontend": {"nodeIds": [100 + b for b in params.get(
                        "backendNodeIds", [])]} if method.startswith("DOM.push") else {},
                    "DOM.querySelectorAll": {"nodeIds": [111] if "a[href]" in str(params.get("selector")) else [121]},
                    "DOM.disable": {}}.get(method, {})

        async def js(self, expr):
            return [BASE + "/p/kettle"]
    snap = asyncio.run(AXT.read_snapshot(FakeCdp()))
    assert snap["ctl"] == [11] and snap["inp"] == [21] and snap["href"] == [BASE + "/p/kettle"]
    raw = asyncio.run(AXT.read_raw(FakeCdp()))
    read = AXT.enrich({"elements": [{"i": 0, "tag": "a", "href": BASE + "/p/kettle", "text": "Kettle", "area": "body"}]},
                      raw)
    e = read["elements"][0]
    assert (e["ax_role"], e["ax_name"], e["ax_landmark"]) == ("link", "Kettle", "main")
    assert asyncio.run(AXT.read_raw(object())) is None                         # no CDP, no recorded snapshot
    assert AXT.links(raw)[BASE + "/p/kettle"][0]["landmark"] == "main"


# ------------------------------------------------------------------------------------------------- (a) the observation
QUICK = (f'<html><body><nav><a href="{BASE}/news">News</a></nav><main><h1>N-Bahn</h1>'
         '<form name="QuickSearch" method="GET" action="/ncontent/ndb/ndb-searchresult.asp">'
         '<input type="text" class="adapted" name="Sel_Mixed_Full"><input type="submit" value="Suchen" name="Suchen">'
         '</form><form name="nl"><input type="email" name="mail"><button>Abonnieren</button></form>'
         f'<button class="icon"><svg><title>Search</title></svg></button><a href="{BASE}/m/1">Modell 1</a></main>'
         '</body></html>')


def quick_snap():
    s = Snap()
    nav = s.add("navigation")
    s.add("link", "News", nav, backend=1, url=BASE + "/news")
    main = s.add("main")
    box = s.add("generic", "", main)
    s.add("textbox", "", box, backend=10)
    s.add("button", "Suchen", box, backend=2)
    nl = s.add("generic", "", main)
    s.add("textbox", "E-Mail", nl, backend=11)
    s.add("button", "Abonnieren", nl, backend=3)
    s.add("button", "Search", main, backend=4)
    s.add("link", "Modell 1", main, backend=5, url=BASE + "/m/1")
    # controls in CONTROL_SEL order: News, Suchen (submit), Abonnieren, the icon button, Modell 1; inputs: text, submit,
    # email
    return s.snap(ctl=[1, 2, 3, 4, 5], inp=[10, 2, 11], href=[BASE + "/news", "", "", "", BASE + "/m/1"])


def test_the_tree_names_search_boxes_the_heuristics_miss():
    site = D.DomSite([D.Recorded(BASE + "/", QUICK, ax=quick_snap())])
    page = D.DomPage(site)
    asyncio.run(page.goto(BASE + "/"))
    from anyuse.execution import demo_tool as T
    r = asyncio.run(T.read_full(page, with_dom=True))
    assert not r["inputs"]                                       # the page's search-like heuristics see none
    assert [a["index"] for a in r["ax_inputs"]] == [0] and "search button" in r["ax_inputs"][0]["why"]
    obs, mapping = DPOL.build_observation(r)
    assert obs["elements"][0]["kind"] == "search box" and mapping[0] == {"src": "input", "input": {"index": 0, "ax": True}}
    texts = [it["text"] for it in obs["elements"]]
    assert "Search" in texts                                     # the icon button, by its computed name
    assert texts.index("Modell 1") < texts.index("News")          # main before the navigation
    # typed by the same rule offline (a GET form): the site's search URL
    got = asyncio.run(page.type_search("Märklin", want={"index": 0, "ax": True}))
    assert got["input"]["name"] == "Sel_Mixed_Full" and "Sel_Mixed_Full=M%C3%A4rklin" in got["document"]
    # the compiled tool keeps the rule
    assert C._input_descriptor({"outcome": {"input": got["input"]}}) == {"index": 0, "name": "Sel_Mixed_Full",
                                                                          "ax": True}


@pytest.mark.parametrize("role,name,where,want", [
    ("searchbox", "", "main", True), ("textbox", "", "search", True), ("textbox", "Produktsuche", "main", True),
    ("combobox", "Suchbegriff eingeben", "main", True), ("textbox", "E-Mail-Adresse", "main", False),
    ("textbox", "Besucher", "main", False), ("checkbox", "Search", "main", False)])
def test_what_the_tree_calls_a_search_box(role, name, where, want):
    s = Snap()
    land = s.add(where)
    s.add(role, name, land, backend=7)
    t = AXT.Tree(raw_of(s.snap(inp=[7])))
    assert bool(AXT.search_input(t, 2)) is want


# ------------------------------------------------------------------------------------------------- (b) regions
def test_a_link_in_the_trees_chrome_is_never_a_region_link():
    read = {"elements": [{"i": 0, "tag": "a", "href": BASE + "/p/1", "text": "One", "area": "body",
                          "ax_landmark": "complementary"},
                         {"i": 1, "tag": "a", "href": BASE + "/p/2", "text": "Two", "area": "body", "ax_landmark": "main"},
                         {"i": 2, "tag": "a", "href": BASE + "/p/3", "text": "Three", "area": "body"}]}
    got = C.region_links(read, {"regex": r"^https://www\.shop\.test/p/[^/]+$"})
    assert [e["text"] for e in got] == ["Two", "Three"]           # no tree for #2: as before


def test_the_cluster_outside_the_trees_chrome_wins():
    side = "".join(f'<li><a href="{BASE}/shop/bestseller/top-{k}.html">Top seller {k}</a></li>' for k in range(1, 7))
    res = "".join(f'<li><a href="{BASE}/item?id={k}">Result {k}</a></li>' for k in range(1, 4))
    dom = f'<html><body><div class="side"><ul>{side}</ul></div><div class="res"><ul>{res}</ul></div></body></html>'
    want = [BASE + "/shop/bestseller/top-2.html", BASE + "/item?id=2"]
    plain = C.region_from_dom(dom, BASE + "/search?q=x", want)
    s = Snap()
    comp = s.add("complementary", "Bestseller")
    for k in range(1, 7):
        s.add("link", f"Top seller {k}", comp, url=f"{BASE}/shop/bestseller/top-{k}.html")
    lst = s.add("list", "", s.add("main"))
    for k in range(1, 4):
        s.add("link", f"Result {k}", s.add("listitem", "", lst), url=f"{BASE}/item?id={k}")
    with_ax = C.region_from_dom(dom, BASE + "/search?q=x", want, ax=raw_of(s.snap()))
    assert "bestseller" in plain["template"], plain                # the larger cluster, without a tree
    assert "/item" in with_ax["template"] and with_ax["ax_chrome_frac"] == 0.0 and with_ax["ax_inside_frac"] == 1.0


def test_a_cluster_mostly_in_main_keeps_winning_with_a_few_sidebar_links():
    # santemagazine (measured): a result list whose URL shape also appears in the sidebar and footer stays the region
    side = "".join(f'<li><a href="{BASE}/shop/bestseller/top-{k}.html">Top seller {k}</a></li>' for k in range(1, 7))
    res = "".join(f'<li><a href="{BASE}/item?id={k}">Result {k}</a></li>' for k in range(1, 4))
    dom = f'<html><body><div class="side"><ul>{side}</ul></div><div class="res"><ul>{res}</ul></div></body></html>'
    want = [BASE + "/shop/bestseller/top-2.html", BASE + "/item?id=2"]
    s = Snap()
    lst = s.add("list", "", s.add("main"))
    comp = s.add("complementary", "Bestseller")
    for k in range(1, 7):
        s.add("link", f"Top seller {k}", comp if k <= 2 else s.add("listitem", "", lst),
              url=f"{BASE}/shop/bestseller/top-{k}.html")
    for k in range(1, 4):
        s.add("link", f"Result {k}", s.add("listitem", "", lst), url=f"{BASE}/item?id={k}")
    got = C.region_from_dom(dom, BASE + "/search?q=x", want, ax=raw_of(s.snap()))
    assert "bestseller" in got["template"] and got["ax_chrome_frac"] < C.AX_CHROME_MAJORITY


# ------------------------------------------------------------------------------------------------- (c) titles
def test_a_links_computed_name_is_the_last_title_candidate():
    links = [AR.Link(3, BASE + "/a/1", "Weiterlesen", "", "Wie man eine Weiche baut")]
    row, how, _ = AR.resolve_row({"element": 3}, links)
    assert how == "element" and row["title"] == "Wie man eine Weiche baut"
    named = [AR.Link(3, BASE + "/a/1", "Wie man eine Weiche baut", "", "Artikel lesen: Weiche")]
    assert AR.resolve_row({"element": 3}, named)[0]["title"] == "Wie man eine Weiche baut"


# ------------------------------------------------------------------------------------------------- (d) the verifier
def list_page(ax):
    dom = (f'<html><body><nav><a href="{BASE}/p/9">Nine</a></nav><main><ul>'
           f'<li><h3><a href="{BASE}/p/1">Kettle Classic</a></h3></li><li><h3><a href="{BASE}/p/2">Kettle Mini</a>'
           '</h3></li></ul></main></body></html>')
    return AV.Page(dom, "Kettle Classic\nKettle Mini", BASE + "/search?q=kettle", ax=ax)


def verifier_snap(second_in="main", second_name="Kettle Mini"):
    s = Snap()
    s.add("link", "Nine", s.add("navigation"), url=BASE + "/p/9")
    lst = s.add("list", "", s.add("main"))
    s.add("link", "Kettle Classic", s.add("listitem", "", lst), url=BASE + "/p/1")
    land = s.add(second_in) if second_in != "main" else lst
    s.add("link", second_name, s.add("listitem", "", land), url=BASE + "/p/2")
    return s.snap()


ANSWER = {"results": [{"title": "Kettle Classic", "url": BASE + "/p/1"}, {"title": "Kettle Mini", "url": BASE + "/p/2"}]}


def test_the_tree_agrees_nothing_changes():
    v = AV.verify_ax(ANSWER, list_page(verifier_snap()))
    assert [d.status for d in v.decisions] == [AV.VERIFIED, AV.VERIFIED]
    assert AV.verify_ax(ANSWER, list_page(None)).decisions == []                # no tree, no second read


@pytest.mark.parametrize("snap,why", [
    (verifier_snap(second_in="navigation"), "only in the page's chrome"),
    (verifier_snap(second_name="Teekanne Gross"), "names the result otherwise"),
])
def test_a_disagreement_is_uncertain_and_final(snap, why):
    v = AV.verify_ax(ANSWER, list_page(snap))
    bad = [d for d in v.decisions if d.status != AV.VERIFIED]
    assert len(bad) == 1 and why in bad[0].why and bad[0].final
    calls = []
    full = AV.check("site_search", ANSWER, list_page(snap),
                    AV.Resolver(lambda q, sch: calls.append(q) or {"choice": "yes", "line": 1}))
    assert full.verdict == AV.WITHHOLD and calls == []            # withheld; no model call spent on it


def test_a_near_title_is_not_the_trees_disagreement():
    # a glued date in the DOM text ("Vilbelsteig10.09.2026") against the tree's spaced name: the card check's question
    ans = {"results": [{"title": "Bad Vilbel – Zwischen Skyline und Streuobstwiesen Vilbelsteig10.09.2026",
                        "url": BASE + "/p/1"}]}
    s = Snap()
    lst = s.add("list", "", s.add("main"))
    s.add("link", "Bad Vilbel – Zwischen Skyline und Streuobstwiesen Vilbelsteig 10.09.2026", s.add("listitem", "", lst),
          url=BASE + "/p/1")
    assert [d.status for d in AV.verify_ax(ans, list_page(s.snap())).decisions] == [AV.VERIFIED]


def test_a_result_the_tree_does_not_link_is_uncertain():
    ans = {"results": ANSWER["results"] + [{"title": "Ghost", "url": BASE + "/p/404"}]}
    v = AV.verify_ax(ans, list_page(verifier_snap()))
    assert v.decisions[-1].status == AV.UNCERTAIN and "no link" in v.decisions[-1].why


def test_the_verifier_reads_the_tree_with_its_own_code():
    src = (_HERE / "execution" / "attribute_verify.py").read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    assert not [ln for ln in imports if "ax_tree" in ln or "demo_" in ln or "web_arch" in ln], imports


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
