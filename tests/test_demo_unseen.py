"""Offline (fake pages of the recorded www.gamer.no structure; no browser, no network, no model): a
result card that matches no compiled structure makes the output INCOMPLETE, and the tool says which;
it never silently drops a result.

The wrong output: www.gamer.no, for the value "første", skipped the site's 2nd result, a "Kommentar" article
(``article.articlelist__article--kommentar``), and listed the 1st, 3rd-6th: the demonstration's page had
shown feature and news cards (two card structures) and one review (a title-link structure), none of
them the comment layout, so its link was in no compiled card and the list went on without it. The
structure below is the demonstration's recorded one:
``div.grid > div > section > article.articlelist__article--<kind> > a.flex.bg-surface``.

What must hold:

1. on the recorded page the version-6 tool answers INCOMPLETE - the top-5 list it could read and the
   unread result (its URL and text), not an accepted answer; the version-5 runtime still skips it (the
   measured wrong output);
2. an unseen card after the top k does not make the list incomplete;
3. a layout the demonstration's page showed among its reported results without the demonstrator
   reporting it (topic tiles) is calibrated at compile time and not taken for an unseen result; the same
   kind of tile unseen at compile time is reported;
4. only links of the compiled result pattern inside the results container count (a pagination link, a
   footer link of the pattern, are not results);
5. the in-page read reports the container's links that no card holds, with their chains.
"""
import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_cards import read_of_html  # noqa: E402
from test_demo_cardrole import FakePage  # noqa: E402

BASE = "https://www.gamer.test"
SITE = "www.gamer.test"


def article(kind, kicker, title, lead, slug, n):
    return (f'<article class="articlelist__article articlelist__article--{kind}">'
            f'<a class="flex bg-surface" href="/artikler/{slug}/{n}"><figure class="relative overflow-hidden">'
            f'<div class="image-container"><img src="data:," alt=""></div></figure><div class="grow flex">'
            f'<h6 class="articlelist__article__kicker font-medium-plus"><span>{kind.capitalize()}</span><span>{kicker}</span></h6>'
            f'<h4 class="font-bold-plus articlelist__article__headline">{title}</h4>'
            f'<p class="articlelist__article__preamble">{lead}</p><div class="mt-auto text-xs">4. januar 2024</div>'
            f'</div></a></article>')


def tile(title, slug, n, cls="topic-tile"):
    return f'<div class="{cls}"><a class="tile-link" href="/artikler/{slug}/{n}">{title}</a></div>'


def page(q, items, extra_after=""):
    return (f'<html><head><title>{q} - Søk - Gamer.test</title></head><body><header><nav>'
            f'<a href="/anmeldelser">Anmeldelser</a><a href="/artikler/redaksjonen/1">Om oss</a></nav></header>'
            f'<main><h1>Søk</h1><div class="grid"><div><section>{"".join(items)}'
            f'<a class="more" href="/sok?q={q}&amp;page=2">Flere resultater</a></section></div></div>{extra_after}'
            f'</main><footer><a href="/artikler/personvern/2">Personvern</a></footer></body></html>')


#: the demonstration's page ("oktober"): features, one review, news; the reported 5 are the first five
DEMO = [("feature", "Spill i oktober", "Desse spela gler vi oss til i oktober", "Hausttrøkket er tilbake.",
         "desse-spela-gler-vi-oss-til-oktober", 515485),
        ("anmeldelse", "", "Hva venter oss i oktober?", "Mørke dager kan trygt tilbringes.", "hva-venter-oss-i-oktober",
         78559),
        ("nyhet", "PlayStation Plus - oktober", "Dette er spillene du får med PlayStation Plus i oktober",
         "Måneden byr på bilbaner.", "dette-er-spillene-du-far-med-playstation-plus-i-oktober", 515481),
        ("nyhet", "Wittard i oktober", "Velkommen til ruinene", "Eventyrspillet har fått utgiver.",
         "velkommen-til-ruinene", 140922),
        ("feature", "Oktober", "Spillene vi spiller i oktober", "En måned full av skrekk.", "spillene-i-oktober", 515100),
        ("nyhet", "Oktober", "Nye spill i oktober", "Her er listen.", "nye-spill-i-oktober", 515200)]
#: the recorded page ("første", the oracle's render): the 2nd result is a comment article
FORSTE = [("feature", "Den første bybyggeren?", "En titt på Fortune Builder", "Var det ikke SimCity?",
           "en-titt-pa-fortune-builder", 141344),
          ("kommentar", "Kommentar: Min første kjærlighet", "Gode minner", "Spillene som fulgte med.", "gode-minner",
           142141),
          ("nyhet", "Bli den første tempelridderen", "Nytt actionspill fra Kalypso", "Samarbeid i The First Templar.",
           "nytt-actionspill-fra-kalypso", 140100),
          ("nyhet", "Her er vår første podcast", "Nettradio for massene", "Den første av mange.", "nettradio-for-massene",
           141060),
          ("anmeldelse", "James Cameron's Avatar: The Game", "Hva kom først?", "Avatar snur om på høna og egget.",
           "james-camerons-avatar-the-game", 73937),
          ("anmeldelse", "Horizon Call of the Mountain", "Dette er PlayStations første storspill i VR",
           "Et engasjerende VR-eventyr.", "dette-er-playstations-forste-storspill-i-vr", 515934),
          ("nyhet", "Tetris rundet for første gang", "13-åring ble først i verden", "Blue scuti klarte det.",
           "13-aring-ble-forst-i-verden-til-a-fullfore-tetris", 516996)]


def arts(rows):
    return [article(*r) for r in rows]


def trace(items=None):
    start = BASE + "/"
    rurl = BASE + "/sok?q=oktober"
    rp = read_of_html(page("oktober", items if items is not None else arts(DEMO)), rurl)
    rep = {"results": [{"title": r[2], "url": f"{BASE}/artikler/{r[4]}/{r[5]}"} for r in DEMO[:5]]}
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": "oktober",
            "steps": [{"k": 0, "page": read_of_html(page("", []), start), "action": {"action": "search", "element": 0,
                                                                                    "text": "oktober"},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}},
                      {"k": 1, "page": rp, "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": rp, "result": rep, "stop": "done", "forbidden": []}


def compiled(items=None, version=10):
    # the web-watch rule as versions 6-10 run it (web tool version 11: below)
    got = C.compile_trace(trace(items), version=version)
    assert got["ok"], got
    return got["tool"]


def run(tool, html, value):
    url = C.fill(tool["search"]["template"], value)
    return asyncio.run(T.run_tool(FakePage({url: html}), tool, value, lambda p: asyncio.sleep(0), result_wait_s=0.3))


def test_the_compiled_structures_are_the_demonstrations():
    t = compiled()
    got = sorted((c["kind"], c["chain"][-1]) for c in t["region"]["cards"])
    assert got == [("card", "article.articlelist__article.articlelist__article--feature"),
                   ("card", "article.articlelist__article.articlelist__article--nyhet"),
                   ("title", "a.flex.bg-surface")], got
    assert "known_other" not in t["region"], t["region"]


def test_an_unseen_result_card_makes_the_list_incomplete():
    t = compiled()
    out = run(t, page("første", arts(FORSTE)), "første")
    assert out["status"] == "incomplete" and not out["accepted"], out
    miss = out["missing"]
    assert len(miss) == 1 and miss[0]["urls"] == [f"{BASE}/artikler/gode-minner/142141"], miss
    assert miss[0]["why"] == "a result card of a layout the demonstration did not show", miss
    assert "Gode minner" in miss[0]["title"], miss
    urls = [r["url"] for r in out["output"]["results"]]
    assert urls == [f"{BASE}/artikler/{r[4]}/{r[5]}" for r in FORSTE if r[0] != "kommentar"][:5], urls
    assert out["output"]["incomplete"] is True, out["output"]
    # the version-5 runtime: the same list, accepted, the comment silently skipped (the wrong output)
    old_region = {k: v for k, v in t["region"].items() if k not in ("cards_version", "known_other")}
    old = run(dict(t, version=5, region=old_region), page("første", arts(FORSTE)), "første")
    assert old["status"] == "ok" and [r["url"] for r in old["output"]["results"]] == urls, old


def test_version_11_reads_the_comment_card_as_one_of_its_kind():
    """Web tool version 11 (``demo_cards.bem_free``): ``articlelist__article--kommentar`` is a BEM modifier of
    ``articlelist__article`` - the compiled card kind: the skipped 2nd result is listed in its place."""
    t = compiled(version=11)
    assert {c["chain"][-1] for c in t["region"]["cards"] if c["kind"] == "card"} == {"article.articlelist__article"}
    out = run(t, page("første", arts(FORSTE)), "første")
    assert out["status"] == "ok" and out["accepted"], out
    assert [r["url"] for r in out["output"]["results"]] == [f"{BASE}/artikler/{r[4]}/{r[5]}" for r in FORSTE][:5]


def test_an_unseen_card_after_the_top_k_is_no_gap():
    t = compiled()
    rows = [r for r in FORSTE if r[0] != "kommentar"] + [FORSTE[1]]
    out = run(t, page("første", arts(rows)), "første")
    assert out["status"] == "ok" and out["accepted"] and len(out["output"]["results"]) == 5, out


def test_a_layout_the_demonstrator_saw_and_did_not_count_is_calibrated():
    # the demonstration page shows two topic tiles above its articles (www.androidplanet.nl's case);
    # the demonstrator reported the articles only
    tiles = [tile("Alt om oktober-spill", "tema-oktober", 9001), tile("Høstens spill", "tema-host", 9002)]
    t = compiled(tiles + arts(DEMO))
    assert len(t["region"]["known_other"]) == 1 and t["region"]["known_other"][0].endswith("a.tile-link"), t["region"]
    tiles4d = [tile("Alt om første-spill", "tema-forste", 9003)]
    rows = [r for r in FORSTE if r[0] != "kommentar"]
    out = run(t, page("første", tiles4d + arts(rows)), "første")
    assert out["status"] == "ok" and out["accepted"] and not out.get("missing"), out
    # a tile of a layout the demonstration never showed: reported
    t2 = compiled()
    other = [tile("Alt om første-spill", "tema-forste", 9003, cls="promo-card")]
    out2 = run(t2, page("første", other + arts(rows)), "første")
    assert out2["status"] == "incomplete" and out2["missing"][0]["urls"] == [f"{BASE}/artikler/tema-forste/9003"], out2


def test_only_result_pattern_links_in_the_results_container_count():
    t = compiled()
    rows = [r for r in FORSTE if r[0] != "kommentar"]
    # a pattern link after the container (a "related" box) and the footer's pattern link are not results;
    # the container's pagination link is not of the result pattern
    after = '<div class="related"><a href="/artikler/relatert/77">Relatert artikkel om første</a></div>'
    out = run(t, page("første", arts(rows), extra_after=after), "første")
    assert out["status"] == "ok" and out["accepted"], out


def test_the_in_page_read_reports_the_containers_unheld_links():
    t = compiled()
    js = K.cards_js_v6(t["region"]["cards"])
    assert "orphans.push([k, CH(a)])" in js and "box.contains(a)" in js and "document.body" in js
    r = read_of_html(page("første", arts(FORSTE)), BASE + "/sok?q=f%C3%B8rste")
    ans = K.recorded_answer(js, r)
    assert ans["container"] == "section", ans["container"]
    hrefs = {e["i"]: e["href"] for e in r["elements"] if e.get("href")}
    got = [(hrefs[i], ch.split(K.SEP)[-2]) for i, ch in ans["orphans"]]
    assert (f"{BASE}/artikler/gode-minner/142141", "article.articlelist__article.articlelist__article--kommentar") in got
    assert (f"{BASE}/sok?q=første&page=2".replace("ø", "%C3%B8"), "section") in got, got
    # no card at all, or cards spread over the whole document: no container, nothing claimed
    assert K.results_container([]) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
