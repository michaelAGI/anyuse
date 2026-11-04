"""Web tool version 8, offline (fake sites built from saved evidence; no browser, no network, no
model): at compile time the result structure must be the QUERY-DEPENDENT region - never a sidebar / widget, never a
structure that reads the same items for any query; a rejected structure falls back to the next candidate region or
is an explicit compile failure.

The wrong outputs: www.traumflieger.de ("Objektiv", "kompakte") answered the same five site articles - the "NEUE
REPORTS" sidebar (``aside > div#box_recent ... li.tf-sidelist__item``). For the demonstration value "Sony" the
sidebar's newest reports were the five Sony articles that also head the main result list, the demonstrator reported
them, the compiler took their FIRST links (the sidebar's) and the self-check passed.

What must hold (``demo_compile.query_region``; version 8 only):

1. on the traumflieger pages version 8 rejects the sidebar (inside ``aside``; it reads the same items on the start
   page; its items name "Sony" no more than there) and compiles the main article cards - the next candidate
   region; run on "Objektiv" it answers the main list, while the version-7 tool answers the sidebar as
   measured;
2. a structure that reads the same items for any query (identical on the start page, no aside, no label) falls back
   to the real result list, or - with none - is an explicit compile failure;
3. a widget named or labelled related / new / popular ("Beliebte Artikel", ``related-posts``) is rejected; a
   structure whose items name the value no more than on the start page is rejected; a result-only structure is
   kept even when its items do not name the value (a fuzzy search);
4. a search whose results render over the start page (skitour.fr) is checked without the start page's
   links, as the run leaves them out.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import html
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402

BASE = "https://www.traumflieger.test"
SITE = "www.traumflieger.test"
#: (slug, title) newest first - the saved sidebar and result titles
ARTICLES = [("sony-zv-e10-ii", "Sony ZV-E10 II: Wie gut ist die kompakte Vlogging-Kamera?"),
            ("sony-rx1r-iii", "Sony RX1R III im Test: Vollformat-Kompakte mit Luxuspreis"),
            ("sony-rx10-v-zu-teuer", "Sony RX10 V zu teuer? Diese gebrauchten Bridgekameras sind Alternativen"),
            ("sony-rx10-v-test", "Sony RX10 V Test: Kamera, Objektiv & Fotopraxis im Check"),
            ("sony-dsc-rx10-v-labortest", "Sony DSC-RX10 V im Labortest: Schwächen bei Auflösung und Randschärfe"),
            ("rollei-fotoliner-ocean", "Rollei Fotoliner Ocean Test: Kamera & Objektiv Taschen"),
            ("panasonic-tz300", "Panasonic Lumix DC-TZ300 Test: Kamera & Objektiv in der Fotopraxis"),
            ("nikon-z-dx-16-50", "Nikon Z DX 16-50 mm F2.8 VR an der Z50II im Schärfe-Check"),
            ("megadap-m2rf", "Megadap M2RF: Autofokus für Leica-M-Objektive an Canon-RF-Kameras"),
            ("leica-sl3-p", "Leica SL3-P im Praxistest: 44 Megapixel als idealer Mittelweg?"),
            ("viltrox-af-26", "Viltrox AF 26 mm F2,8 Evo: flaches Pancake für Sony E und Nikon Z"),
            ("tamron-12-20", "Tamron 12-20 mm F2,8: Ultraweitwinkelzoom für Sony E und Nikon Z"),
            ("canon-rf-24-105", "Canon RF 24-105 mm: das Objektiv für alle Fälle"),
            ("sigma-50-art", "Sigma 50 mm Art: ein Objektiv mit Charakter")]


def hits(q):
    return [a for a in ARTICLES if C.name_share(a[1], q) >= 1.0] if q else []


def sidebar(label="Neue Reports", where="aside"):
    items = "".join(f'<li class="tf-sidelist__item"><a href="/{s}"><span class="tf-sidelist__title">{html.escape(t)}'
                    f'</span></a></li>' for s, t in ARTICLES[:10])
    box = (f'<div class="tf-box" id="box_recent"><div class="tf-box__label">{label}</div><div class="tf-box__body">'
           f'<ul class="tf-sidelist">{items}</ul></div></div>')
    return f'<{where} id="col1">{box}</{where}>' if where else f'<div id="col1">{box}</div>'


def cards(arts, cls="tf-card type-page"):
    return "".join(f'<article class="{cls}"><a class="tf-card__media" href="/{s}" tabindex="-1" aria-hidden="true">'
                   f'<img alt=""></a><div class="tf-card__body"><h2 class="tf-card__title"><a href="/{s}">'
                   f'{html.escape(t)}</a></h2><p class="tf-card__excerpt">{html.escape(t)} - mehr dazu im Bericht.</p>'
                   f'</div></article>' for s, t in arts)


def page_dom(title, main, side):
    return (f'<html><head><title>{html.escape(title)}</title></head><body><header><nav><a href="/">Start</a>'
            f'<a href="/reports/">Reports</a></nav><form action="/" method="get"><input type="search" name="s">'
            f'</form></header><div id="wrap">{side}<main id="col3"><div id="col3_content" class="clearfix">{main}'
            f'</div></main></div><footer><a href="/impressum">Impressum</a></footer></body></html>')


def read_of_dom(url, dom):
    """READ_JS's view of a recorded DOM: every control (``demo_cards.control_nodes``) with its label, link and
    area; the visible text."""
    root, base = K.parse(dom)
    els = []
    for k, n in enumerate(K.control_nodes(root)):
        e = {"i": k, "tag": n.tag, "text": K.label_of(n), "area": K.area_of(n), "role": "", "type": "", "y": 100 + k}
        if n.tag == "a":
            e["href"] = K.href_of(n, url, base)
        els.append(e)
    text = K.text_of(root, 20000)
    return {"url": url, "title": "", "h1": [], "headings": [], "elements": els, "text": text, "main_text": text,
            "jsonld": [], "facts": [], "canonical": "", "og_type": "", "md_products": 0, "breadcrumb": [],
            "inputs": [{"index": 0, "visible": True, "name": "s"}], "meta": {}, "microdata": {}, "link_sigs": {},
            "dom": dom}


class Site:
    """The fake site: its start page and its search pages (``side``: the sidebar's HTML; ``teaser``: a main-area
    list shown on every page; ``results``: whether the search lists its hits)."""

    def __init__(self, side=None, teaser="", results=True, start_cards=True, result_cls="tf-card type-page"):
        self.side = sidebar() if side is None else side
        self.teaser, self.results, self.start_cards, self.result_cls = teaser, results, start_cards, result_cls

    def dom(self, url):
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query)).get("s", "")
        if not q:
            main = "<h1>Traumflieger</h1>" + self.teaser + (cards(ARTICLES[:10]) if self.start_cards else "")
            return page_dom("Traumflieger", main, self.side)
        found = hits(q)
        main = f"<h1>Suchergebnisse für: {html.escape(q)}</h1>" + self.teaser + \
            ((cards(found, self.result_cls) if found else "<p>Leider nichts gefunden.</p>") if self.results else "")
        return page_dom(f"Sie suchten nach {q} - Traumflieger", main, self.side)

    def read(self, url):
        return read_of_dom(url, self.dom(url))


def trace(site, value="Sony", reported=None):
    """The demonstration: search "Sony", report the first five results it saw."""
    start = BASE + "/"
    rurl = f"{BASE}/?s={urllib.parse.quote_plus(value)}"
    rep = reported or [{"title": t, "url": f"{BASE}/{s}"} for s, t in ARTICLES[:5]]
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": site.read(start), "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "s"}, "url_after": rurl}},
                      {"k": 1, "page": site.read(rurl), "action": {"action": "done", "result": {"results": rep}},
                       "outcome": {}}],
            "final_page": site.read(rurl), "result": {"results": rep}, "stop": "done", "forbidden": []}


class FakePage:
    def __init__(self, site):
        self.site, self.cur = site, ""

    async def goto(self, url):
        self.cur = url

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        r = self.site.read(self.cur)
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom",)}
        if expr == T.PAGE_EXTRA_JS:
            return {"meta": {}, "microdata": {}}
        if expr == T.LINK_SIG_JS:
            return {}
        if expr == T.PRICED_LINKS_JS:
            return {}
        return K.recorded_answer(expr, r)

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur


async def _settle(page):
    return None


def run(site, tool, value):
    return asyncio.run(T.run_tool(FakePage(site), tool, value, _settle, result_wait_s=0.3))


def titles(out):
    return [r["title"] for r in (out.get("output") or {}).get("results") or []]


def test_prior_traumflieger_sidebar_is_rejected_and_the_main_list_compiled():
    site = Site()
    tr = trace(site)
    g7, g8 = C.compile_trace(tr, version=7), C.compile_trace(tr)
    assert g7["ok"] and g8["ok"], (g7.get("why"), g8.get("why"))
    c7, c8 = g7["tool"]["region"]["cards"], g8["tool"]["region"]["cards"]
    assert c7[0]["chain"][-1] == "li.tf-sidelist__item", c7                       # the sidebar, as measured
    assert c8[0]["chain"][-1] == "article.tf-card.type-page", c8
    st = g8["report"]["structure"]
    assert st["kept"] == "cards (the next candidate region)", st
    assert any("inside the page's aside" in r for r in st["rejected"]), st["rejected"]
    q = [c["query_region"] for c in st["checks"] if c.get("query_region")]
    assert q[0]["same_items_share"] == 1.0 and q[0]["named_share"] <= q[0]["chance_share"], q[0]
    assert q[-1]["ok"] and q[-1]["widget"] == "", q[-1]
    # run on the saved values: version 7 answers the sidebar for any query, version 8 the query's own results
    for v in ("Objektiv", "kompakte"):
        r7, r8 = run(site, g7["tool"], v), run(site, g8["tool"], v)
        assert r7["status"] == "ok" and titles(r7) == [t for _s, t in ARTICLES[:5]], r7          # the sidebar
        want = [t for _s, t in hits(v)][:5]
        assert r8["status"] == "ok" and titles(r8) == want, (v, titles(r8), want)
    # the sidebar alone, with no other list holding the results: an explicit compile failure
    g = C.compile_trace(trace(Site(results=False)))
    assert not g["ok"] and "not the query-dependent region" in g["why"], g.get("why")
    assert C.compile_trace(trace(Site(results=False)), version=7)["ok"]


def test_the_same_items_for_any_query_fall_back_or_fail():
    teaser = '<div class="teaser-list">' + "".join(
        f'<div class="teaser"><a href="/{s}">{html.escape(t)}</a></div>' for s, t in ARTICLES[:5]) + "</div>"
    # a main-area list shown identically on every page (no aside, no label): rejected, the result list kept
    site = Site(side="", teaser=teaser)
    g = C.compile_trace(trace(site))
    assert g["ok"], g.get("why")
    st = g["report"]["structure"]
    assert any("the same items for any query" in r for r in st["rejected"]), st
    assert g["tool"]["region"]["cards"][0]["chain"][-1] == "article.tf-card.type-page", g["tool"]["region"]
    assert C.compile_trace(trace(site), version=7)["tool"]["region"]["cards"][0]["chain"][-1] == "div.teaser"
    # with no other list: an explicit compile failure naming why
    g = C.compile_trace(trace(Site(side="", teaser=teaser, results=False)))
    assert not g["ok"] and "the same items for any query" in g["why"], g.get("why")


def test_widgets_by_name_or_label_chance_and_a_result_only_structure():
    for side in (sidebar(label="Beliebte Artikel", where="div"),
                 sidebar(label="", where="div").replace('id="box_recent"', 'id="box-related-posts"')):
        # version 15: the box shows the same items on the start page (the site's own differential); version 14: its
        # name or label says widget - the same structure is kept either way
        g, g14 = C.compile_trace(trace(Site(side=side))), C.compile_trace(trace(Site(side=side)), version=14)
        st, st14 = g["report"]["structure"], g14["report"]["structure"]
        assert g["ok"] and any("the same items on the start page" in r for r in st["rejected"]), \
            (side[:80], st.get("rejected"))
        assert g14["ok"] and any("widget" in r for r in st14["rejected"]), (side[:80], st14.get("rejected"))
        assert g["tool"]["region"]["cards"][0]["chain"][-1] == "article.tf-card.type-page", g["tool"]["region"]
        assert g14["tool"]["region"]["cards"] == g["tool"]["region"]["cards"]
    # a structure on the start page whose items name the value no more than there: rejected (no label, no aside)
    plain = sidebar(label="", where="div").replace('id="box_recent"', 'id="box"')
    q = C.query_region(Site(side=plain).read(BASE + "/?s=Sony"), Site(side=plain).read(BASE + "/"),
                       C.compile_trace(trace(Site(side=plain)), version=7)["tool"]["region"]["cards"],
                       [(f"{BASE}/{s}", t) for s, t in ARTICLES[:5]], "Sony", "site_search")
    assert not q["ok"] and q["widget"] == "" and q["named_share"] <= q["chance_share"], q
    # a result-only structure is the search's own, even when its items do not name the value (a fuzzy search)
    fuzzy = Site(start_cards=False)
    rep = [{"title": t, "url": f"{BASE}/{s}"} for s, t in hits("Objektiv")[:3]]
    tr = trace(fuzzy, value="Objectiv", reported=rep)
    tr["steps"][1]["page"] = tr["final_page"] = read_of_dom(f"{BASE}/?s=Objectiv",
                                                            fuzzy.dom(f"{BASE}/?s=Objektiv").replace(
                                                                "für: Objektiv", "für: Objectiv"))
    g = C.compile_trace(tr)
    assert g["ok"], g.get("why")
    qr = [c["query_region"] for c in g["report"]["structure"]["checks"] if c.get("query_region")][-1]
    assert qr["ok"] and qr["start_items"] == 0 and "start page shows none" in qr["why"], qr


def test_an_overlay_search_is_checked_without_the_start_pages_links():
    # skitour.fr: the results render over the start page, whose table of recent outings the demonstrator
    # also reported from; the run leaves the start page's links out - so does the check
    site = Site(side=sidebar(label="", where="div").replace('id="box_recent"', 'id="box"'), start_cards=False)
    own = [(s, t) for s, t in hits("Objektiv") if (s, t) not in ARTICLES[:10]]          # not on the start page
    assert len(own) == 2, own
    reported = [{"title": ARTICLES[0][1], "url": f"{BASE}/{ARTICLES[0][0]}"}] + \
        [{"title": t, "url": f"{BASE}/{s}"} for s, t in own]
    tr = trace(site, value="Objektiv", reported=reported)
    specs = C.compile_trace(tr, version=7)["tool"]["region"]["cards"]
    rp, sp = tr["final_page"], tr["steps"][0]["page"]
    wanted = [(r["url"], r["title"]) for r in reported]
    without = C.query_region(rp, sp, specs, wanted, "Objektiv", "site_search", overlay=False)
    within = C.query_region(rp, sp, specs, wanted, "Objektiv", "site_search", overlay=True)
    assert not without["ok"] and "the same items for any query" in without["why"], without
    assert within["ok"] and within["items"] == 2 and within["same_items_share"] == 0.0, within


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
