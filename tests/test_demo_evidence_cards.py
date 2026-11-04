"""Web tool version 15, offline (fake shops served from generated DOMs; no browser, no network, no model): the result
cards are read with the site's own evidence before the hand-written word lists.

What must hold:

1. a block of the result structure that shows the same items on the start page is no result list, whatever its id
   is called: a Dutch or Portuguese sidebar id ("zijbalk", "barra-lateral") is rejected by that differential and the
   results beside it are compiled (a version-14 compile keeps the sidebar and answers its news); the listed sidebar
   name is rejected as before; a demonstrated result that only such a block shows is left out; such a block alone is
   an explicit failure; a sidebar-named wrapper around the results stays the results;
2. a sidebar-named container that holds the page's main column by a theme's class name only ("...-main-content") is
   decided by the structure: it holds every card, so it is around the results;
3. a UI affix every title shares, set off by a colon, a bar or an arrow, is no part of a title in any language
   ("Lees meer: ...", "Xem thêm » ...", "... | Ver"); a brand every title starts with stays; a version-14 tool keeps
   the affix;
4. a title-role link text the page gives several targets ("Snel bekijken" on every card) and a text the page marks as
   a date (``<time>``, a Czech month no list holds) are no titles, also as the heading the link sits in: the card's
   other link names the result; the version-11 lists still decide their own cases;
5. a link parameter only one card carries, or with a value the other cards' links do not carry, marks a placement;
   ``rel`` stays the standard's; the listed value word decides what is left, as before.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import html
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
import test_demo_sidebar_facets as S10  # noqa: E402

SITE = "www.winkel.test"
BASE = f"https://{SITE}"
#: a Dutch tool shop's catalogue (title, slug)
PRODUCTS = [("Bosch boormachine PSB 500", "bosch-psb-500"), ("Makita boormachine HP457", "makita-hp457"),
            ("Metabo boormachine SBE 650", "metabo-sbe-650"), ("Einhell boormachine TC-ID", "einhell-tc-id"),
            ("Ryobi boormachine R18PD", "ryobi-r18pd"), ("Bosch schroefmachine GSR 12V", "bosch-gsr-12v"),
            ("Makita schroefmachine DF333", "makita-df333"), ("Metabo schroefmachine PowerMaxx", "metabo-powermaxx")]


class Shop(D.DomSite):
    """``/`` (a search form), ``/zoeken?q=`` (result cards), ``/p/<slug>``. ``lead`` / ``tail``: a UI affix around
    each title link's text; ``role``: the title-role heading link shows that text (a label, or a ``<time>`` when it
    starts with "time:") and another link to the same target names the product."""

    def __init__(self, lead="", tail="", tile="kaart", role=""):
        super().__init__([])
        self.lead, self.tail, self.tile, self.role = lead, tail, tile, role

    def hits(self, q):
        words = [w for w in q.lower().split() if w]
        return [p for p in PRODUCTS if words and all(w in p[0].lower() for w in words)]

    def card(self, title, slug):
        text = " ".join(x for x in (self.lead, html.escape(title), self.tail) if x)
        other = ""
        if self.role:
            text = (f"<time datetime=\"2026-09-20\">{self.role[5:]}</time>" if self.role.startswith("time:")
                    else html.escape(self.role))
            other = f"<a class=\"titel\" href=\"/p/{slug}\">{html.escape(title)}</a>"
        return (f"<li class=\"{self.tile}\"><a class=\"foto\" href=\"/p/{slug}\"><img src=\"/i/{slug}.jpg\" alt=\"\"></a>"
                f"<h3 class=\"naam\"><a href=\"/p/{slug}\">{text}</a></h3>{other}"
                f"<span class=\"prijs\">€ 49,95</span></li>")

    def page(self, main):
        return (f"<html><head><title>Winkel</title></head><body><header><nav><a href=\"/\">Start</a> "
                f"<a href=\"/klantenservice\">Klantenservice</a></nav><form action=\"/zoeken\" method=\"get\" "
                f"role=\"search\"><input type=\"search\" name=\"q\"></form></header><main>{main}</main><footer>"
                f"<a href=\"/over-ons\">Over ons</a></footer></body></html>")

    def results(self, q):
        hits = self.hits(q)
        if not hits:
            return self.page("<h1>Zoekresultaten</h1><p>Geen resultaten.</p>")
        lst = "<ul class=\"lijst\">" + "".join(self.card(t, s) for t, s in hits) + "</ul>"
        return self.page(f"<h1>Zoekresultaten voor {html.escape(q)}</h1><div class=\"resultaten\">{lst}</div>")

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path in ("", "/"):
            return D.Recorded(url, self.page("<h1>Welkom</h1><p>Gereedschap voor iedereen.</p>")), url
        if sp.path == "/zoeken" and "q" in q:
            return D.Recorded(url, self.results(q["q"])), url
        if sp.path.startswith("/p/"):
            p = next((x for x in PRODUCTS if x[1] == sp.path[3:]), None)
            if p:
                return D.Recorded(url, self.page(f"<h1>{html.escape(p[0])}</h1><p>€ 49,95</p>")), url
        return D.Recorded(url, D.NOT_FOUND, {}, 404), url


def page_of(site, url):
    rec, u = site.route(url)
    r = dict(site.read(rec, u))
    r.setdefault("search_urls", [])
    return r


def trace(site, value, reported=None):
    """The demonstration: search ``value``, report the results as the page lists them (the plain list's titles)."""
    start = BASE + "/"
    rurl = f"{BASE}/zoeken?{urllib.parse.urlencode({'q': value})}"
    p0, p1 = page_of(site, start), page_of(site, rurl)
    rep = reported or [{"title": t, "url": f"{BASE}/p/{s}"} for t, s in site.hits(value)[:5]]
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": p0, "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}},
                      {"k": 1, "page": p1, "action": {"action": "done", "result": {"results": rep}}, "outcome": {}}],
            "final_page": p1, "result": {"results": rep}, "stop": "done", "forbidden": []}


def tools(site, value="boormachine"):
    g15, g14 = C.compile_trace(trace(site, value)), C.compile_trace(trace(site, value), version=14)
    assert g15["ok"] and g14["ok"], (g15.get("why"), g14.get("why"))
    return g15["tool"], g14["tool"]


def answer(site, tool, value):
    out = D.run(site, tool, value)
    return out["status"], [(r["title"], r["url"].rsplit("/", 1)[-1]) for r in (out.get("output") or {}).get("results") or []]


# ---------------------------------------------------------------------------
# 1-2: a block that shows the same items without the query
# ---------------------------------------------------------------------------

def test_a_sidebar_in_any_language_is_rejected_by_the_start_page_differential():
    for ident in ("zijbalk-a", "barra-lateral", "sidebar-a"):
        site = S10.Site(ident=ident)
        tr = S10.trace(site)
        g15, g14 = C.compile_trace(tr), C.compile_trace(tr, version=14)
        assert g15["ok"] and g14["ok"], (ident, g15.get("why"), g14.get("why"))
        c15 = [c["chain"] for c in g15["tool"]["region"]["cards"]]
        assert len(c15) == 2 and all(c[-1] in ("tr.resultRowOne", "tr.resultRowTwo") for c in c15), (ident, c15)
        st = g15["report"]["structure"]
        assert st["kept"] == "cards (the next candidate region)", (ident, st)
        assert any("in a block that shows the same items on the start page" in r for r in st["rejected"]), st
        want = [t for _s, t in S10.hits("Zukunft")][:5]
        r15 = S10.run(site, g15["tool"], "Zukunft")
        assert r15["status"] == "ok" and S10.titles(r15) == want, (ident, S10.titles(r15))
        r14 = S10.run(site, g14["tool"], "Zukunft")
        if ident == "sidebar-a":
            assert S10.titles(r14) == want                                    # the listed name, as before
        else:
            assert S10.titles(r14)[:2] == [t for _s, t in S10.NEWS[:2]], (ident, S10.titles(r14))   # the old miss


class RotatingSite(S10.Site):
    """The sidebar shows other news on the start page than on the result page (it rotates its items)."""

    OTHER = [(f"meldung-{k}", f"Meldung Nummer {k}") for k in range(6)]

    def dom(self, url):
        self.news = S10.NEWS if "keywords=" in url else self.OTHER
        return super().dom(url)


def test_a_sidebar_that_rotates_its_items_is_decided_by_its_name():
    # other items on the start page that do not name the value more: no evidence the block depends on the query,
    # so its sidebar or widget name decides, as before
    for ident in ("sidebar-a", "beliebt", "popular-news"):
        site = RotatingSite(ident=ident)
        g15 = C.compile_trace(S10.trace(site))
        assert g15["ok"], (ident, g15.get("why"))
        c15 = [c["chain"] for c in g15["tool"]["region"]["cards"]]
        assert all(f"div#{ident}" not in c for c in c15), (ident, c15)
        out = S10.run(site, g15["tool"], "Zukunft")
        assert out["status"] == "ok" and S10.titles(out) == [t for _s, t in S10.hits("Zukunft")][:5], (ident, out)


class WidgetNamedShop(Shop):
    """The results sit in a container whose class holds a widget word, and the start page shows other products in
    the same list."""

    def __init__(self, cls):
        super().__init__()
        self.cls = cls

    def results(self, q):
        lst = "<ul class=\"lijst\">" + "".join(self.card(t, s) for t, s in self.hits(q)) + "</ul>"
        return self.page(f"<h1>Zoekresultaten voor {html.escape(q)}</h1><div class=\"{self.cls}\">{lst}</div>")

    def route(self, url):
        if urllib.parse.urlsplit(url).path in ("", "/"):
            lst = "<ul class=\"lijst\">" + "".join(self.card(t, s) for t, s in PRODUCTS[5:]) + "</ul>"
            return D.Recorded(url, self.page(f"<h1>Welkom</h1><div class=\"{self.cls}\">{lst}</div>")), url
        return super().route(url)


def test_a_block_whose_other_items_name_the_value_is_the_results_whatever_its_name():
    for cls in ("producten-beliebt", "related-products"):
        site = WidgetNamedShop(cls)
        g15, g14 = C.compile_trace(trace(site, "boormachine")), C.compile_trace(trace(site, "boormachine"), version=14)
        assert not g14["ok"] and "widget container" in g14["why"], (cls, g14.get("why"))      # the name, as before
        assert g15["ok"], (cls, g15.get("why"))
        assert answer(site, g15["tool"], "schroefmachine") == ("ok", list(site.hits("schroefmachine"))), cls


def test_a_result_only_the_block_shows_is_left_out_and_the_block_alone_fails():
    news = [("weltpremiere-cologne-sidebar-only", "Weltpremiere in cologne: nur in der Seitenleiste")] + S10.NEWS[1:]
    site = S10.Site(ident="zijbalk", news=news)
    rep = [{"title": news[0][1], "url": f"{S10.BASE}/news/a/{news[0][0]}"}] + \
        [{"title": t, "url": f"{S10.BASE}/news/a/{s}"} for s, t in S10.hits("cologne")[:4]]
    g = C.compile_trace(S10.trace(site, reported=rep))
    assert g["ok"], g.get("why")
    assert g["report"]["structure"]["dropped_widget_results"] == [rep[0]["url"]], g["report"]["structure"]
    site2 = S10.Site(ident="zijbalk", results=False)
    rep2 = [{"title": t, "url": f"{S10.BASE}/news/a/{s}"} for s, t in S10.NEWS[:3]]
    g2 = C.compile_trace(S10.trace(site2, value="News", reported=rep2))
    assert not g2["ok"] and "not the query-dependent region" in g2["why"], g2.get("why")


def test_a_sidebar_named_wrapper_around_the_results_stays_the_results():
    for wrapper in ("cms-element-sidebar-filter", "cms-section-sidebar-main-content", "content-sidebar"):
        site = S10.Site(news=[], wrapper=wrapper)
        rep = [{"title": t, "url": f"{S10.BASE}/news/a/{s}"} for s, t in S10.hits("cologne")[:5]]
        g15 = C.compile_trace(S10.trace(site, reported=rep))
        g14 = C.compile_trace(S10.trace(site, reported=rep), version=14)
        assert g15["ok"] and g15["report"]["structure"]["kept"] == "cards", (wrapper, g15["report"]["structure"])
        assert g15["tool"]["region"]["cards"] == g14["tool"]["region"]["cards"], wrapper
    # version 15 names a sidebar by its tokens alone; the structure decides what a theme's token order said
    assert K.sidebar_name("cms-section-sidebar-main-content", v15=True)
    assert not K.sidebar_name("cms-section-sidebar-main-content")
    root, _b = K.parse('<div class="cms-section-sidebar"><div class="cms-section-sidebar-main-content"><ul>'
                       '<li><a href="/a">a</a></li><li><a href="/b">b</a></li></ul></div></div>')
    ul = next(n for n in K.walk(root) if n.tag == "ul")
    assert K.holds_main(ul.parent.parent) and not K.holds_main(ul.parent.parent, v15=True)
    assert K.holds_main(K.parse("<div><main><p>x</p></main></div>")[0], v15=True)


def test_a_title_links_tile_is_its_repeated_ancestor():
    dom = ('<html><body><div class="raster">' + "".join(
        f'<div class="kaartje"><div class="beeld"><img alt=""></div><div class="tekst"><a href="/p/{k}">Boor {k}</a>'
        f'<span>Bosch boormachine met koffer</span></div></div>' for k in range(3)) + '</div></body></html>')
    spec = {"kind": "title", "chain": ["div.raster", "div.kaartje", "div.tekst", "a"], "rels": [""]}
    it15 = K.structure_items(dom, BASE + "/zoeken?q=boormachine", [spec], v10=True, v15=True)
    it14 = K.structure_items(dom, BASE + "/zoeken?q=boormachine", [spec], v10=True)
    assert len(it15) == 3 and all("boormachine met koffer" in it["text"] for it in it15), it15
    assert all("koffer" not in it["text"] for it in it14), it14                  # no listed tile class: the link only
    assert [it["spec"] for it in it15] == [0, 0, 0] and "spec" not in it14[0]
    # two parts of one card with one descriptor (the title's and the brand's) are no tiles: the card is
    dom = ('<html><body><ul class="raster">' + "".join(
        f'<li><div class="vak"><h3><a href="/p/{k}">GSR 18V-{k}</a></h3></div><div class="vak"><span>Bosch</span>'
        f'<span>99,00 €</span></div></li>' for k in range(3)) + '</ul></body></html>')
    spec = {"kind": "title", "chain": ["li", "div.vak", "h3", "a"], "rels": [""]}
    it15 = K.structure_items(dom, BASE + "/zoeken?q=bosch", [spec], v10=True, v15=True)
    assert len(it15) == 3 and all("Bosch" in it["text"] for it in it15), it15


# ---------------------------------------------------------------------------
# 3: a UI affix every title shares
# ---------------------------------------------------------------------------

def test_ui_affixes_by_the_list_differential():
    cases = [
        (["Lees meer: Bosch boormachine PSB", "Lees meer: Makita boormachine", "Lees meer: Metabo boor"],
         "boormachine", ["Bosch boormachine PSB", "Makita boormachine", "Metabo boor"]),
        (["Xem thêm » Máy khoan A", "Xem thêm » Máy khoan B", "Xem thêm » Máy bắn vít"], "máy khoan",
         ["Máy khoan A", "Máy khoan B", "Máy bắn vít"]),
        (["อ่านต่อ: สว่านไฟฟ้า A", "อ่านต่อ: สว่านไฟฟ้า B", "อ่านต่อ: สว่าน C"], "สว่าน",
         ["สว่านไฟฟ้า A", "สว่านไฟฟ้า B", "สว่าน C"]),
        (["Furadeira A | Ver", "Furadeira B | Ver", "Parafusadeira C | Ver"], "furadeira",
         ["Furadeira A", "Furadeira B", "Parafusadeira C"]),
        (["Matkap A incele »", "Matkap B incele »", "Matkap C incele »"], "matkap", ["Matkap A", "Matkap B", "Matkap C"]),
    ]
    for titles, value, want in cases:
        lead, tail = C.ui_affixes15(titles, value)
        assert [C.strip_affixes15(t, lead, tail) for t in titles] == want, (titles, lead, tail)
    # a brand every title starts with, a brand set off by a dash, a run naming the input, two titles: no affix
    for titles, value in ((["Apple iPhone 15 hoesje", "Apple iPad hoes", "Apple Watch band"], "hoes"),
                          (["Bosch - GSR 18V-55", "Bosch - GSR 18V-90", "Bosch - GSR 18V-21"], "gsr 18v"),
                          (["Boormachine: Bosch", "Boormachine: Makita", "Boormachine: Metabo"], "boormachine"),
                          (["Lees: A b", "Lees: C d"], "x")):
        assert C.ui_affixes15(titles, value) == ([], []), titles


def test_a_version_15_tool_answers_the_titles_without_the_affix():
    plain = Shop()
    t15, t14 = tools(plain)
    want = [(t, s) for t, s in plain.hits("schroefmachine")]
    assert answer(plain, t15, "schroefmachine") == ("ok", want) == answer(plain, t14, "schroefmachine")
    for lead, tail in (("Bekijk »", ""), ("", "| Bestel")):
        site = Shop(lead=lead, tail=tail)
        st15, got15 = answer(site, t15, "schroefmachine")
        st14, got14 = answer(site, t14, "schroefmachine")
        assert st15 == "ok" and got15 == want, (lead, tail, got15)
        assert st14 == "ok" and got14 != want and all((lead or tail) in t for t, _s in got14), (lead, tail, got14)


# ---------------------------------------------------------------------------
# 4: a label or a date in the title role
# ---------------------------------------------------------------------------

def _read(version, vis, value="eccoflow", dates=None):
    """Three cards; each card's title-role link shows ``vis(k)``, its other link to the result the name."""
    els, cmap_map, rows = [], {}, []
    names = ["Juwel Eccoflow 600", "Juwel Eccoflow 1000", "Juwel Eccoflow 1500"]
    for k, n in enumerate(names):
        u = f"https://www.aqua.test/p/{n.replace(' ', '-').lower()}/"
        b = len(els)
        els += [{"i": b, "tag": "a", "href": u, "text": vis(k)}, {"i": b + 1, "tag": "a", "href": u, "text": n}]
        cmap_map[str(b)] = [0, k, 1, 0, 1, vis(k)]
        cmap_map[str(b + 1)] = [0, k, 0, 1, 0, n]
        rows.append([k, 0, b, 2, "", "main", 1, ""])
    spec = {"kind": "card", "chain": ["div", "ul", "li.kaart"], "rels": ["a"], "roles": [{"anchor": "a"}]}
    region = {"cards": [spec], "cards_version": version, "template": "https://www.aqua.test/p/{v}/"}
    got = K.read_region_v6(els, {"map": cmap_map, "cards": rows}, region, "www.aqua.test", value=value, dates=dates)
    return [i["title"] for i in got["items"]], names


def test_a_label_on_every_card_is_no_title():
    got15, names = _read(15, lambda k: "Snel bekijken")
    assert got15 == names, got15
    assert _read(12, lambda k: "Snel bekijken")[0] == ["Snel bekijken"] * 3              # the old lists miss it
    # the lists' own case is decided as before; a title-role text naming the input stays a title
    assert _read(15, lambda k: "Schnellansicht")[0] == names == _read(12, lambda k: "Schnellansicht")[0]
    own = [f"Eccoflow pomp {k}" for k in range(3)]
    assert _read(15, lambda k: own[k])[0] == own
    # without the input (no differential that is safe to read) the label stays as the old rule reads it
    assert _read(15, lambda k: "Snel bekijken", value="")[0] == ["Snel bekijken"] * 3


def test_a_heading_that_is_a_label_or_a_marked_date_names_nothing():
    plain = Shop()
    t15, t14 = tools(plain)
    want = [(t, s) for t, s in plain.hits("boormachine")]
    for role in ("Snel bekijken", "time:20. září 2026", "Schnellansicht"):
        site = Shop(role=role)
        st15, got15 = answer(site, t15, "boormachine")
        st14, got14 = answer(site, t14, "boormachine")
        assert st15 == "ok" and got15 == want, (role, got15)
        assert got14 != want, (role, got14)            # the heading names every card, as before


def test_a_date_the_page_marks_is_no_title():
    dom = ('<html><body><ul><li><time datetime="2026-09-20">20. září 2026</time></li>'
           '<li><span itemprop="datePublished">3. října 2026</span></li></ul></body></html>')
    assert K.page_dates15(dom) == {"20. zari 2026", "3. rijna 2026"}, K.page_dates15(dom)
    marks = ["20. září 2026", "3. října 2026", "20. září 2026"]
    got15, names = _read(15, lambda k: marks[k], dates=K.page_dates15(dom))
    assert got15 == names, got15
    assert _read(12, lambda k: marks[k])[0] == marks                                    # the old month list misses it
    assert not C.no_title11("20. září 2026")
    # the list's own case ("Sep. 20 2026") as before
    assert _read(15, lambda k: "Sep. 20 2026")[0] == names == _read(12, lambda k: "Sep. 20 2026")[0]


# ---------------------------------------------------------------------------
# 5: a placement marked in the list's own links
# ---------------------------------------------------------------------------

def test_a_link_parameter_the_list_does_not_share_marks_a_placement():
    scope = {"hosts": [SITE]}

    def items(urls, rel=None):
        return [{"url": u, "link": "site", "title": u, "el": {"rel": (rel or {}).get(k, "")}} for k, u in enumerate(urls)]
    urls = [f"{BASE}/p/a?ref=zoeken", f"{BASE}/p/b?ref=zoeken", f"{BASE}/p/c?ref=zoeken_uitgelicht_xq",
            f"{BASE}/p/d?ref=zoeken"]
    v15 = K.scope_items(items(urls), scope, v15=True)
    assert [p["promo_url"] for p in v15["promo"]] == [urls[2]], v15["promo"]
    assert K.scope_items(items(urls), scope)["promo"] == []                         # no listed value word: missed
    alone = [f"{BASE}/p/a", f"{BASE}/p/b?source=kq7", f"{BASE}/p/c", f"{BASE}/p/d"]
    assert [p["promo_url"] for p in K.scope_items(items(alone), scope, v15=True)["promo"]] == [alone[1]]
    # every link carries its own value (a per-result id): no placement; the listed value word as before
    own = [f"{BASE}/p/{x}?ref=r{x}" for x in "abcd"]
    assert K.scope_items(items(own), scope, v15=True)["promo"] == []
    direct = [f"{BASE}/p/a", f"{BASE}/p/b?ref=search_results_direct_name", f"{BASE}/p/c"]
    assert len(K.scope_items(items(direct), scope, v15=True)["promo"]) == 1 == \
        len(K.scope_items(items(direct), scope)["promo"])
    # rel=sponsored is the standard's marker
    assert len(K.scope_items(items(own, {0: "sponsored"}), scope, v15=True)["promo"]) == 1


# ---------------------------------------------------------------------------
# a heal re-derives a version-15 region's cards by version 15's own reading
# ---------------------------------------------------------------------------

HAUS = "https://www.haus.test"
HAUS_ITEMS = [("gartenhaus-a", "Gartenhaus Alva kaufen"), ("gartenhaus-b", "Gartenhaus Birke kaufen"),
              ("gartenhaus-c", "Gartenhaus Cedar kaufen"), ("gartenhaus-d", "Gartenhaus Dalia kaufen")]


def _haus_read(card_cls):
    cards = "".join(f'<li class="{card_cls}"><a href="/p/{s}">{t}</a><span>1.299,00 €</span></li>'
                    for s, t in HAUS_ITEMS)
    dom = (f'<html><head><title>Suche</title></head><body><header><a href="/">Start</a><a href="/cart">Warenkorb</a>'
           f'</header><main><h1>Suchergebnisse für Gartenhaus</h1><ul class="liste">{cards}</ul></main>'
           f'<footer><a href="/impressum">Impressum</a></footer></body></html>')
    url = HAUS + "/suche?q=gartenhaus"
    site = D.DomSite([D.Recorded(url, dom)])
    return dict(site.read(site.pages[C._norm(url)], url))


def test_a_heal_re_derives_a_version_15_regions_cards_by_version_15s_reading():
    # every result title holds a word the gated-action list names ("kaufen"); a version-15 compile reads such a title
    # as a title (the page gives it one target), so the site renaming its card class is healed by the same reading
    from anyuse.execution import repair_relocate as R
    anchors = [(f"{HAUS}/p/{s}", t) for s, t in HAUS_ITEMS]

    def tool(cards_version):
        region = {"kind": "cluster", "template": HAUS + "/p/{v}", "regex": r"^https://www\.haus\.test/p/[^/?#]+$",
                  "cards": [{"kind": "card", "chain": ["body", "main", "ul.liste", "li.alt"], "rels": ["a"]}],
                  "cards_version": cards_version}
        return {"version": 15 if cards_version == 15 else 14, "workflow": "site_search", "site": "www.haus.test",
                "region": region, "search": {"mode": "url", "template": HAUS + "/suche?q={q}"}}
    got = R.candidates("region", tool(15), "gartenhaus", {"result": _haus_read("kachel")}, anchors=anchors)
    assert len(got) == 1, got
    specs = got[0]["patch"]["ops"][0]["value"]
    assert [sp["chain"] for sp in specs] == [["body", "main", "ul.liste", "li.kachel"]], specs
    # a version-12 region is re-derived as before: its compile refused these titles too
    assert R.candidates("region", tool(12), "gartenhaus", {"result": _haus_read("kachel")}, anchors=anchors) == []
    assert not W.site_evidence_first()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
