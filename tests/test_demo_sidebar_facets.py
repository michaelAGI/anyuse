"""Web tool version 10, offline (a fake site built from saved evidence; no browser, no network, no
model): an element whose OWN id or class names a SIDEBAR ("sidebar", "side-bar", "aside", "rail", ...) is a widget for
the result-region rule (version 8's ``query_region``), so a structure holding a demonstrated result's sidebar card is
rejected and the next candidate region - the results outside the sidebar - is compiled.

The evidence (www.woodworker.de): the demonstration (value "cologne") reported five results, the first a
link of the "weiter News" list ``div#sidebar-a > ul > li``; the compiled structure kept three card specs - that
sidebar list and the result table's rows ``table > tbody > tr.resultRowOne`` / ``tr.resultRowTwo`` - and version 8's
query-region check passed it (no ``aside``, no widget token, "weiter News" is no widget label). The value "Zukunft"
answered sidebar news and a category link: wrong; the second value the same.

What must hold:

1. on the woodworker-like page a version-10 compile rejects the sidebar ("inside a sidebar container") and keeps the
   table rows only; run on new values it answers the table's results, while the same demonstration compiled at version
   9 keeps the sidebar and answers its news first (the measured wrong output, unchanged);
2. a demonstrated result that ONLY the sidebar shows is left out of the next candidate region (noted), the others
   compile; a sidebar holding every reported result is an explicit compile failure;
3. ``sidebar_name``: sidebar names and layout names (``has-sidebar``, ``content-sidebar-wrap``, Shopware's
   ``cms-section-sidebar-main-content``) that are not a sidebar.
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
from test_demo_sidebar import FakePage, _settle, read_of_dom  # noqa: E402,F401

BASE = "https://www.woodworker.test"
SITE = "www.woodworker.test"
#: the "weiter News" sidebar: the site's newest news, the same on every page (the saved titles, shortened)
NEWS = [("imm-cologne-kehrt-2024-in-den-januar-zurueck", "imm cologne kehrt 2024 in den Januar zurück"),
        ("plaedoyer-fuer-den-dauermischwald", "Plädoyer für den Dauermischwald"),
        ("perfect-sortiment-von-hesse-lignal", "Perfect-Sortiment von Hesse Lignal"),
        ("tannenholzdekor-abies", "Tannenholzdekor Abies erweitert die Kollektion"),
        ("zukunft-der-tischlerei-sichern", "Zukunft der Tischlerei sichern"),
        ("handwerk-digital", "Handwerk digital: neue Förderung")]
#: the news archive the search finds in (a table row per hit)
ARCHIVE = NEWS + [("koelner-moebelmesse-findet-2023-im-juni-statt", "Kölner Möbelmesse findet 2023 im Juni statt"),
                  ("noch-ein-winter-ohne-moebelmesse-in-koeln", "Noch ein Winter ohne Möbelmesse in Köln - cologne"),
                  ("als-sonderedition-geplante-imm-cologne-faellt-aus", "Als Sonderedition geplante imm cologne fällt aus"),
                  ("imm-cologne-mit-spezialkonzept-fuer-2021", "imm cologne mit Spezialkonzept für 2021"),
                  ("zukunft-holzbau-studie", "Studie: die Zukunft im Holzbau"),
                  ("zukunftspreis-fuer-schreiner", "Zukunft-Preis für junge Schreiner"),
                  ("zukunft-werkstatt-2030", "Werkstatt 2030: Zukunft der Maschinen")]


def hits(q):
    return [a for a in ARCHIVE if C.name_share(a[1], q) >= 1.0 or q.lower() in a[0]] if q else []


def side(ident="sidebar-a", news=NEWS):
    items = "".join(f'<li><a href="/news/a/{s}">{html.escape(t)}</a></li>' for s, t in news)
    return f'<div id="{ident}"><h3>weiter News</h3><ul>{items}</ul></div>'


def table(rows):
    out = []
    for k, (s, t) in enumerate(rows):
        cls = "resultRowOne" if k % 2 == 0 else "resultRowTwo"
        out.append(f'<tr class="{cls}"><td><b><a href="/news/a/{s}">{html.escape(t)}</a></b></td>'
                   f'<td>News</td></tr>')
    return "<table><tbody>" + "".join(out) + "</tbody></table>"


class Site:
    def __init__(self, ident="sidebar-a", news=NEWS, results=True, wrapper=""):
        self.ident, self.news, self.results, self.wrapper = ident, news, results, wrapper

    def dom(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("keywords", "")
        body = (f'<header><nav><a href="/">Start</a><a href="/news/">News</a></nav><form action="/suche">'
                f'<input name="keywords"></form></header>{side(self.ident, self.news)}')
        if not q:
            body += "<h1>woodworker</h1><p>Willkommen</p>"
        else:
            found = hits(q) if self.results else []
            res = f"<h1>Suchergebnis: {html.escape(q)}</h1>" + (table(found) if found else "<p>Keine Treffer</p>")
            if self.wrapper:
                # a sidebar-named WRAPPER around the filter panel and the results (Shopware's search page)
                res = f'<div class="{self.wrapper}"><div class="filter-panel">Filter</div>{res}</div>'
            body += res
        return f"<html><head><title>woodworker</title></head><body>{body}</body></html>"

    def read(self, url):
        r = read_of_dom(url, self.dom(url))
        r["inputs"] = [{"index": 0, "visible": True, "name": "keywords"}]
        return r


def trace(site, value="cologne", reported=None):
    """The demonstration: search "cologne", report five results - the sidebar's first."""
    start = BASE + "/"
    rurl = f"{BASE}/suche?keywords={urllib.parse.quote_plus(value)}"
    rep = reported or [{"title": t, "url": f"{BASE}/news/a/{s}"} for s, t in [NEWS[0]] + hits(value)[1:5]]
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": site.read(start), "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "keywords"},
                                   "url_after": rurl}},
                      {"k": 1, "page": site.read(rurl), "action": {"action": "done", "result": {"results": rep}},
                       "outcome": {}}],
            "final_page": site.read(rurl), "result": {"results": rep}, "stop": "done", "forbidden": []}


def run(site, tool, value):
    import asyncio
    from anyuse.execution import demo_tool as T
    return asyncio.run(T.run_tool(FakePage(site), tool, value, _settle, result_wait_s=0.3))


def titles(out):
    return [r["title"] for r in (out.get("output") or {}).get("results") or []]


def test_prior_woodworker_sidebar_is_rejected_and_version_9_unchanged():
    site = Site()
    tr = trace(site)
    g9, g10 = C.compile_trace(tr, version=9), C.compile_trace(tr, version=10)
    assert g9["ok"] and g10["ok"], (g9.get("why"), g10.get("why"))
    c9 = [c["chain"] for c in g9["tool"]["region"]["cards"]]
    c10 = [c["chain"] for c in g10["tool"]["region"]["cards"]]
    assert ["body", "div#sidebar-a", "ul", "li"] in c9, c9                       # the sidebar, as measured
    assert all(c[-1] in ("tr.resultRowOne", "tr.resultRowTwo") for c in c10) and len(c10) == 2, c10
    st = g10["report"]["structure"]
    assert st["kept"] == "cards (the next candidate region)", st
    assert any("inside a sidebar container (div#sidebar-a: sidebar-a)" in r for r in st["rejected"]), st["rejected"]
    for v in ("Zukunft", "Möbelmesse"):
        r9, r10 = run(site, g9["tool"], v), run(site, g10["tool"], v)
        want = [t for _s, t in hits(v)][:5]
        assert r10["status"] == "ok" and titles(r10) == want, (v, titles(r10), want)
        assert r9["status"] == "ok" and titles(r9)[:2] == [t for _s, t in NEWS[:2]], (v, titles(r9))   # the sidebar


def test_a_result_only_the_sidebar_shows_is_left_out_and_a_sidebar_alone_fails():
    # the sidebar's first news is no hit of the archive's table: only the sidebar shows it
    news = [("weltpremiere-cologne-sidebar-only", "Weltpremiere in cologne: nur in der Seitenleiste")] + NEWS[1:]
    site = Site(news=news)
    rep = [{"title": news[0][1], "url": f"{BASE}/news/a/{news[0][0]}"}] + \
        [{"title": t, "url": f"{BASE}/news/a/{s}"} for s, t in hits("cologne")[:4]]
    g10 = C.compile_trace(trace(site, reported=rep), version=10)
    assert g10["ok"], g10.get("why")
    st = g10["report"]["structure"]
    assert st["dropped_widget_results"] == [rep[0]["url"]] and st["kept"] == "cards (the next candidate region)", st
    assert C.compile_trace(trace(site, reported=rep), version=9)["ok"]
    # every reported result in the sidebar, no table: an explicit compile failure at version 10
    site2 = Site(results=False)
    rep2 = [{"title": t, "url": f"{BASE}/news/a/{s}"} for s, t in NEWS[:3]]
    g = C.compile_trace(trace(site2, value="News", reported=rep2), version=10)
    assert not g["ok"] and "not the query-dependent region" in g["why"], g.get("why")


def test_a_sidebar_named_wrapper_around_the_results_is_no_sidebar():
    # peeroton.com (Shopware): ``div.cms-element-sidebar-filter`` wraps the filter panel AND the listing
    site = Site(news=[], wrapper="cms-element-sidebar-filter")
    rep = [{"title": t, "url": f"{BASE}/news/a/{s}"} for s, t in hits("cologne")[:5]]
    g9, g10 = C.compile_trace(trace(site, reported=rep), version=9), C.compile_trace(trace(site, reported=rep), version=10)
    assert g9["ok"] and g10["ok"] and g10["report"]["structure"]["kept"] == "cards", g10["report"]["structure"]
    assert g10["tool"]["region"]["cards"] == g9["tool"]["region"]["cards"]


def test_sidebar_names_and_layout_names():
    for nm in ("sidebar", "sidebar-a", "l-sidebar", "right-sidebar", "td-main-sidebar", "side-bar", "side_col", "aside",
               "right-rail", "cms-section-sidebar-sidebar-content", "sidebarLeft", "sidebar-content", "widget-sidebar"):
        assert K.sidebar_name(nm), nm
    for nm in ("has-sidebar", "with-sidebar", "no-sidebar", "content-sidebar-wrap", "content-sidebar",
               "cms-section-sidebar-main-content", "sidebar-toggle", "main", "railway", "guardrail", "inside", ""):
        assert not K.sidebar_name(nm), nm
    root, _b = K.parse('<div class="page has-sidebar"><div class="results"><ul><li><a href="/x">x</a></li></ul></div>'
                       '</div><div id="rail"><ul><li><a href="/y">y</a></li></ul></div>')
    a = [n for n in K.walk(root) if n.tag == "a"]
    assert K.widget_why(a[0], v10=True) == "" and K.widget_why(a[1], v10=True).startswith("inside a sidebar"), a
    assert K.widget_why(a[1]) == ""                                                  # version 8's rule unchanged


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
