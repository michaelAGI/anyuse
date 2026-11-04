"""Linkless results, offline (fake pages of the recorded www.hausgarten.net structure; no browser, no
network, no model): a result without a same-site link is reported as a result with no link (its title
only), never dropped silently.

The wrong output: www.hausgarten.net ("Garten") listed the site's 6th result in 5th place: the page's 5th result,
"Ein neues Gartentor online bestellen und den Frühling begrüßen", has no same-site link in the oracle's render, and
the tool listed same-site results only. The structure below is the demonstration's recorded one:
``div.site-wrap > div#site.site > div.posts-grid > article > a.result-entry.rounded-xl`` (the whole card is its title
link).

What must hold:

1. a result card whose title link leaves the site is listed in its place with its title and NO URL
   (``link: "off-site"``) - the version-6 tool's top 5 are the site's top 5; the version-5 runtime
   listed the 6th instead (the measured wrong output);
2. a result card with no link at all but a title is listed the same way (``link: "none"``) ONLY when the tool's
   compiled card spec says the site's results have no links (``region["linkless_results"]``: the demonstration reported
   a result without one); otherwise a linkless card is a section or teaser heading, never a row (``linkless_cards``;
   www.knivesandtools.de: a section heading answered as a row); an empty card
   (a layout placeholder) is never a result;
3. a product lookup never opens a result without a same-site link: when it is the one naming the input,
   the run is an explicit failure saying so.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_cards import read_of_html  # noqa: E402
from test_demo_cardrole import FakePage  # noqa: E402

BASE = "https://www.hausgarten.test"
SITE = "www.hausgarten.test"


def entry(cat, title, lead, href):
    inner = (f'<div class="img-wrap"><figure class="posts-post-img-wrap rounded-xl"><img src="data:," alt=""></figure>'
             f'</div><div class="text-wrap"><span>{cat}</span><h3>{title}</h3><p>{lead}</p></div>')
    if href is None:
        return f'<article><div class="result-entry rounded-xl">{inner}</div></article>'
    return f'<article><a class="result-entry rounded-xl" href="{href}">{inner}</a></article>'


def page(q, entries):
    return (f'<html><head><title>Suche nach {q}</title></head><body><div class="site-wrap"><div id="site" class="site">'
            f'<header><nav><a href="{BASE}/">Hausgarten.net</a><a href="{BASE}/gartenpflege/">Gartenpflege</a></nav>'
            f'</header><h1>Suchergebnisse für: {q}</h1><div class="posts-grid">{"".join(entries)}</div>'
            f'<footer><a href="{BASE}/impressum/">Impressum</a></footer></div></div></body></html>')


#: the demonstration's page ("Pflege"): every result a same-site article
DEMO = [("Hecken", "Hecken schneiden: Tipps und Tricks für die perfekte und sichere Gartenpflege",
         "hecken-schneiden-sichere-gartenpflege"),
        ("Baumlexikon", "Traubenkirsche, Prunus padus: pflegen, schneiden & vermehren", "traubenkirsche-pflege-schneiden"),
        ("Sommerblumen", "Elfenblume, Epimedium: 12 Tipps zum Pflegen & Schneiden", "elfenblume-pflegen"),
        ("F wie Fetthenne", "Fächerahorn, Acer palmatum: Pflegen, Schneiden und mehr", "faecher-ahorn-acer-palmatum"),
        ("Rasen", "Rasenpflege im Herbst: die wichtigsten Schritte", "rasenpflege-herbst"),
        ("Rosen", "Rosen richtig pflegen", "rosen-pflegen")]
#: the "Garten" page (the oracle's render): the 5th result links off the site
GARTEN = [("Über uns", "Über Hausgarten.net", "ueber-uns"),
          ("Gartengestaltung", "Gartengestaltung 2026: Innovative Projekte und aktuelle Trends",
           "gartengestaltung-aktuelle-trends"),
          ("Gartenarbeiten", "Vom Beet bis zur Werkstatt: Die richtige Kleidung für Heim- und Gartenarbeit",
           "kleidung-gartenarbeit"),
          ("Allgemein", "Hausratversicherung: Was im Garten, auf der Terrasse und auf dem Balkon mitversichert ist",
           "hausratversicherung-garten"),
          ("Grundstücksbegrenzung", "Ein neues Gartentor online bestellen und den Frühling begrüßen",
           "https://www.gartentor-partner.test/fruehling"),
          ("Grüne Ideen", "Kreative Ideen zur Aufwertung Ihres Gartens mit minimalem Aufwand", "garten-kreativ-aufwerten"),
          ("Hecken", "Hecken schneiden: Tipps und Tricks für die perfekte und sichere Gartenpflege",
           "hecken-schneiden-sichere-gartenpflege")]


def entries(rows, lead="Mehr dazu."):
    out = []
    for cat, title, slug in rows:
        href = slug if slug is None or slug.startswith("http") else f"{BASE}/{slug}/"
        out.append(entry(cat, title, lead, href))
    return out


def trace():
    start = BASE + "/"
    rurl = BASE + "/?s=Pflege"
    rp = read_of_html(page("Pflege", entries(DEMO)), rurl)
    rep = {"results": [{"title": t, "url": f"{BASE}/{s}/"} for _c, t, s in DEMO[:5]]}
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": "Pflege",
            "steps": [{"k": 0, "page": read_of_html(page("", []), start),
                       "action": {"action": "search", "element": 0, "text": "Pflege"},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "s"}, "url_after": rurl}},
                      {"k": 1, "page": rp, "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": rp, "result": rep, "stop": "done", "forbidden": []}


def compiled():
    got = C.compile_trace(trace())
    assert got["ok"], got
    return got["tool"]


def run(tool, html, value):
    url = C.fill(tool["search"]["template"], value)
    return asyncio.run(T.run_tool(FakePage({url: html}), tool, value, lambda p: asyncio.sleep(0), result_wait_s=0.3))


def test_an_off_site_result_is_listed_in_its_place_with_no_link():
    t = compiled()
    assert [c["chain"][-1] for c in t["region"]["cards"]] == ["article"], t["region"]["cards"]
    out = run(t, page("Garten", entries(GARTEN)), "Garten")
    assert out["status"] == "ok" and out["accepted"], out
    res = out["output"]["results"]
    assert "Gartentor" in res[4]["title"] and res[4]["url"] == "" and res[4]["link"] == "off-site", res
    assert [r["url"] for r in res[:4]] == [f"{BASE}/{s}/" for _c, _t, s in GARTEN[:4]], res
    assert all("link" not in r for r in res[:4]), res
    # the version-5 runtime lists the 6th result 5th (the measured wrong output)
    old_region = {k: v for k, v in t["region"].items() if k not in ("cards_version", "known_other")}
    old = run(dict(t, version=5, region=old_region), page("Garten", entries(GARTEN)), "Garten")
    assert old["output"]["results"][4]["url"] == f"{BASE}/garten-kreativ-aufwerten/", old["output"]


def test_a_linkless_result_card_is_listed_and_an_empty_card_is_not():
    t = compiled()
    # the demonstration reported only linked results - a linkless card is no row (a heading, a teaser)
    assert t["region"]["linkless_results"] is False
    rows = list(GARTEN)
    rows[4] = ("Grundstücksbegrenzung", "Ein neues Gartentor online bestellen und den Frühling begrüßen", None)
    html = page("Garten", entries(rows))
    out = run(t, html, "Garten")
    assert out["status"] == "ok", out
    res = out["output"]["results"]
    assert all(r["url"] for r in res) and not any("Gartentor" in r["title"] for r in res), res
    # a tool whose compiled spec says the site's results have no links lists it in its place
    t = dict(t, region=dict(t["region"], linkless_results=True))
    out = run(t, html, "Garten")
    assert out["status"] == "ok", out
    res = out["output"]["results"]
    assert "Gartentor" in res[4]["title"] and res[4]["url"] == "" and res[4]["link"] == "none", res
    empty = '<article><div class="result-entry rounded-xl"></div></article>'
    html2 = page("Garten", entries(GARTEN[:2]) + [empty] + entries(GARTEN[2:4]) + entries(GARTEN[5:]))
    out2 = run(t, html2, "Garten")
    assert out2["status"] == "ok" and all(r["url"] for r in out2["output"]["results"]), out2


def test_a_product_lookup_never_opens_a_result_without_a_same_site_link():
    t = dict(compiled(), workflow="product_lookup", select={"rule": "best_match", "min_share": 0.75},
             fields={"name": "h1", "price": "text", "availability": "none", "url": "location"})
    r = read_of_html(page("Gartentor", entries(GARTEN[4:5] + GARTEN[:1])), BASE + "/?s=Gartentor")
    j = C.judge_result_page(t, r, "Ein neues Gartentor online bestellen")
    assert j["status"] == "failed" and "has no same-site link (off-site)" in j["why"], j


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
