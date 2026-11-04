"""Web tool version 8, offline (fake sites built from saved evidence; no browser, no network, no
model): a result's title is the VISIBLE text of its title element - without UI affixes ("> ... anzeigen", "Read:")
and without visually hidden text.

The wrong outputs:

* e-hausaufgaben.de: two titles were the cards' call-to-action links "> Hausaufgabe anzeigen" / "> Referat
  anzeigen" - the card's own title link said "Lessing, Gotthold Ephraim - Emilia Galotti - Wer ist schuld an
  Emilias Tod?" (13 words: longer than the 10-word title rule) and "Biographie" (1 word: shorter than its 2);
* www.wintotal.de: three titles carried "Read: " - the image link's ``aria-label`` ("Read: <title>"), which
  the reader takes before a link's text, beat the heading link's title by length.

What must hold (version 8 only; ``demo_cards.CARDS_JS_V8`` / ``cards_map_v8``, ``read_region_v6``,
``demo_compile.clean_title``):

1. on the e-hausaufgaben layout a version-8 tool answers the title links' texts; the version-7 tool answers the
   CTA links as measured;
2. on the wintotal layout a version-8 tool answers the heading links' visible texts; version 7 answers "Read: ...";
3. visually hidden text (an sr-only class, ``aria-hidden``, ``hidden``, an inline ``display: none``) is never part
   of a title; UI affixes are stripped (leading arrows, a leading "Read:", a trailing call to action after an
   arrow or a separator) and a title that merely ends in "mehr" / "Show" stays;
4. a version-7 tool reads its cards with ``CARDS_JS_V6`` as before; a version-8 tool with ``CARDS_JS_V8``.
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

import test_demo_sidebar as SB  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402

EH = "https://e-hausaufgaben.test"
#: (path, title, CTA, excerpt) - the saved results (and the demonstration's)
EH_DOCS = [("/Referate/D3985-Krankenversicherung.php", "Die Krankenversicherung", "Referat", "Beitrag zur Sozialversicherung"),
           ("/Referate/D5736-Kinderspiele.php", "Kinderspiele und was sie zur Entwicklung eines Kindes beitragen",
            "Referat", "Welchen Beitrag leisten Spiele?"),
           ("/Hausaufgaben/D5659-Emilia-Galotti.php",
            "Lessing, Gotthold Ephraim - Emilia Galotti - Wer ist schuld an Emilias Tod?", "Hausaufgabe",
            "Ein Beitrag zur Interpretation"),
           ("/Referate/D3302-Rousseau-Biographie.php", "Biographie", "Referat", "Rousseau: ein Beitrag zur Biographie"),
           ("/Facharbeiten/D5439-John-Dalton.php", "John Dalton Biografie, Modelle und Gesetze", "Facharbeit",
            "Daltons Beitrag zur Chemie"),
           ("/Facharbeiten/D143-Massenmedien.php", "Massenmedien", "Facharbeit", "Die Bedeutung der Anzeigen"),
           ("/Referate/D4576-Magersucht.php", "Magersucht", "Referat", "Anzeichen? Was hilft?")]


def eh_card(path, title, cta, excerpt, n):
    return (f'<div class="Suchergebnis"><div class="Titel"><div style="position:absolute;">{n}_</div>'
            f'<div class="Details"><a href="{path}" title="{html.escape(cta)}: {html.escape(title)}">{html.escape(title)}'
            f'</a></div></div>\n <div class="Details">{html.escape(excerpt)} (700 Wörter)</div>\n <div class="Link">'
            f'<a href="{path}" title="{html.escape(title)}">&gt; {cta} anzeigen</a></div>\n </div>')


class EHSite:
    def dom(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("keywords", "")
        docs = [d for d in EH_DOCS if C.name_share(" ".join(d) + " anzeigen", q) > 0] if q else []
        body = "".join(eh_card(*d, n) for n, d in enumerate(docs, start=1)) or "<p>Leider nichts gefunden.</p>"
        return (f'<html><head><title>Suche</title></head><body><div id="header"><a href="/schule.php">Schulfächer</a></div>'
                f'<div id="content"><div id="columnA"><div><h1>{len(docs)} Dokumente zum Thema {html.escape(q)}:</h1>'
                f'{body}</div></div></div><div id="footer"><a href="/impressum.php">Impressum</a></div></body></html>')

    def read(self, url):
        return SB.read_of_dom(url, self.dom(url))


WT = "https://www.wintotal.test"
#: (slug, title) - the saved results ("Windows")
WT_POSTS = [("windows-11-tipps", "Windows 11: Die besten Tipps"),
            ("microsoft-whiteboard", "Microsoft Whiteboard: Ein vielseitiges Tool für Kreative und Teams"),
            ("teams-online", "Teams online: Effizient und Einfach"),
            ("microsoft-teams", "Microsoft Teams: Funktionen sowie Vor- und Nachteile"),
            ("windows-backup", "Windows Backup: So sichern Sie Ihre Daten")]


def wt_card(slug, title, img=True):
    media = (f'<div class="ast-blog-featured-section post-thumb"><div class="post-thumb-img-content post-thumb">'
             f'<a href="{WT}/{slug}/" aria-label="Read: {html.escape(title)}"><img src="/m/{slug}.jpg" alt="x"></a>'
             f'</div></div>') if img else '<div class="ast-blog-featured-section post-thumb"></div>'
    return (f'<article class="post type-post"><div class="ast-post-format- ast-article-inner"><div class="post-content '
            f'ast-grid-common-col">{media}<span class="cat-links"><a href="{WT}/artikel/office/" rel="category tag">'
            f'Office</a></span><h2 class="entry-title ast-blog-single-element"><a href="{WT}/{slug}/" rel="bookmark">'
            f'{html.escape(title)}</a></h2><div class="ast-excerpt-container"><p>{html.escape(title)} im Überblick.</p>'
            f'</div></div></div></article>')


class WTSite:
    def dom(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("s", "")
        posts = [p for p in WT_POSTS if C.name_share(p[1], q) > 0] if q else []
        body = "".join(wt_card(s, t, img=(s != WT_POSTS[0][0])) for s, t in posts) or "<p>Nichts gefunden</p>"
        return (f'<html><head><title>Suche</title></head><body><header><nav><a href="{WT}/">Start</a></nav></header>'
                f'<div id="primary" class="content-area primary"><main id="main" class="site-main"><div class="ast-row">'
                f'{body}</div></main></div><footer><a href="{WT}/impressum/">Impressum</a></footer></body></html>')

    def read(self, url):
        return SB.read_of_dom(url, self.dom(url))


def trace(site, base, path, value, reported):
    start = base + "/"
    rurl = base + path.format(q=urllib.parse.quote_plus(value))
    rep = [{"title": t, "url": base + u if u.startswith("/") else u} for u, t in reported]
    return {"version": 1, "workflow": "site_search", "site": urllib.parse.urlsplit(base).hostname, "start_url": start,
            "value": value,
            "steps": [{"k": 0, "page": site.read(start), "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0}, "url_after": rurl}},
                      {"k": 1, "page": site.read(rurl), "action": {"action": "done", "result": {"results": rep}},
                       "outcome": {}}],
            "final_page": site.read(rurl), "result": {"results": rep}, "stop": "done", "forbidden": []}


def run(site, tool, value):
    return asyncio.run(T.run_tool(SB.FakePage(site), tool, value, SB._settle, result_wait_s=0.3))


def titles(out):
    return [r["title"] for r in (out.get("output") or {}).get("results") or []]


def test_prior_e_hausaufgaben_cards_answer_their_title_links():
    site = EHSite()
    # the demonstration: "anzeigen", the titles as its result page lists them
    tr = trace(site, EH, "/suchen.php?keywords={q}&x=0&y=0", "anzeigen", [(d[0], d[1]) for d in EH_DOCS][:5])
    t8, t7 = C.compile_trace(tr, version=10)["tool"], C.compile_trace(tr, version=7)["tool"]
    assert t8["region"]["cards_version"] == 8 and t7["region"]["cards_version"] == 6, (t8["region"], t7["region"])
    r8, r7 = run(site, t8, "Beitrag"), run(site, t7, "Beitrag")
    assert r8["status"] == "ok" and r7["status"] == "ok", (r8, r7)
    want = [d[1] for d in EH_DOCS if C.name_share(" ".join(d), "Beitrag") > 0][:5]
    assert titles(r8) == want, titles(r8)
    assert "Lessing, Gotthold Ephraim - Emilia Galotti - Wer ist schuld an Emilias Tod?" in titles(r8)
    assert "Biographie" in titles(r8)
    assert "> Hausaufgabe anzeigen" in titles(r7) and "> Referat anzeigen" in titles(r7), titles(r7)   # as measured
    assert [r["url"] for r in r8["output"]["results"]] == [r["url"] for r in r7["output"]["results"]]


def test_prior_wintotal_titles_lose_the_hidden_read_prefix():
    site = WTSite()
    tr = trace(site, WT, "/?s={q}", "Teams", [(f"{WT}/{s}/", t) for s, t in WT_POSTS if "Teams" in t])
    t8, t7 = C.compile_trace(tr, version=10)["tool"], C.compile_trace(tr, version=7)["tool"]
    r8, r7 = run(site, t8, "Windows"), run(site, t7, "Windows")
    want = [t for _s, t in WT_POSTS if C.name_share(t, "Windows") > 0]
    assert titles(r8) == want, titles(r8)
    assert titles(r7) == [want[0], "Read: " + want[1]], titles(r7)       # the post without an image: no aria-label
    r8, r7 = run(site, t8, "Teams"), run(site, t7, "Teams")
    assert not any(t.startswith("Read:") for t in titles(r8)) and titles(r8)[0] == WT_POSTS[1][1], titles(r8)
    assert sum(t.startswith("Read: ") for t in titles(r7)) == 3, titles(r7)                       # the measured output


def test_hidden_text_and_ui_affixes_are_never_part_of_a_title():
    dom = ('<a href="/x"><span class="screen-reader-text">Read: </span>Sony RX10 V Test<span aria-hidden="true"> →'
           '</span><span style="display: none">Werbung</span><span hidden>Neu</span><span class="sr-only">(extern)'
           '</span></a>')
    root, _ = K.parse(dom)
    a = K.control_nodes(root)[0]
    assert K.visible_text(a) == "Sony RX10 V Test", K.visible_text(a)
    assert K.label_of(a).startswith("Read:"), K.label_of(a)               # the reader's label still holds it
    for raw, want in [("> Hausaufgabe anzeigen", "Hausaufgabe"), ("» Referat anzeigen", "Referat"),
                      ("Read: Microsoft Whiteboard", "Microsoft Whiteboard"), ("Mehr: Die Bahn", "Die Bahn"),
                      ("Sony RX10 V Test – mehr lesen", "Sony RX10 V Test"), ("Kamera-Test | Weiterlesen", "Kamera-Test"),
                      ("Weiterlesen »", ""), ("Read more", ""), ("→ Lire la suite", ""), ("Immer mehr", "Immer mehr"),
                      ("The Truman Show", "The Truman Show"), ("Biographie", "Biographie")]:
        assert C.clean_title(raw) == want, (raw, C.clean_title(raw))
    # the in-page read checks the same (and the computed style); the recorded read is CARDS_JS_V8's on a DOM
    for needle in ("aria-hidden", "screen-reader-text", "sr-only", "display === 'none'", "visibility === 'hidden'",
                   "rect\\(0", "vtx(a)"):
        assert needle in K.CARDS_JS_V8, needle
    assert K.CARDS_JS_V8.startswith("\n/*anyuse:cards8*/") and K.specs_of_js_v8(K.cards_js_v8([{"chain": []}])) == \
        [{"chain": []}]
    assert K.specs_of_js_v6(K.cards_js_v8([{"chain": []}])) is None


def test_version_7_reads_its_cards_with_cards_js_v6():
    site = WTSite()
    tr = trace(site, WT, "/?s={q}", "Teams", [(f"{WT}/{s}/", t) for s, t in WT_POSTS if "Teams" in t])
    seen = {}

    class Spy(SB.FakePage):
        async def js(self, expr, read_only=False):
            for mark in ("/*anyuse:cards6*/", "/*anyuse:cards8*/", "/*anyuse:extra6*/", "/*anyuse:extra8*/"):
                if str(expr).lstrip().startswith(mark):
                    seen.setdefault(self.tag, set()).add(mark)
            return await super().js(expr, read_only)
    for tag, version in (("v7", 7), ("v8", 8)):
        t = C.compile_trace(tr, version=version)["tool"]
        page = Spy(site)
        page.tag = tag
        asyncio.run(T.run_tool(page, t, "Windows", SB._settle, result_wait_s=0.3))
    assert seen["v7"] == {"/*anyuse:cards6*/", "/*anyuse:extra6*/"}, seen
    assert seen["v8"] == {"/*anyuse:cards8*/", "/*anyuse:extra8*/"}, seen


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
