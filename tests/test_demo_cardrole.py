"""Card roles, offline (fake pages of the recorded www.autoplus.fr structure; no browser, no network,
no model): a card's result link must have the demonstrated title link's ROLE - its place in the card, its
own descriptor, whether it is a heading link - not merely be a link in the card; a category / tag link is
never a result.

The wrong output: www.autoplus.fr ("Auto") listed "LES INCONTOURNABLES" (``/actualite/les-incontournables``,
a category link) as its 4th result. The card (``div.post-item``, the demonstration's recorded structure)
holds a category link ``a.post-item-cat`` and the article's title link ``h3.post-item-title > a``; the
article's URL has three path segments (``/actualite/les-incontournables/assurance-auto-...-1467374.html``),
outside the compiled URL pattern (one or two segments), so the only link of the card left in the region was
the category link, and the version-5 fallback took it. The demonstration's own page already had three such
cards ("INSOLITE", "GUIDES D'ACHAT ET COMPARATIFS").

What must hold:

1. the compiled card carries the title link's role (``a``, a heading link);
2. on the "Auto" page the version-6 tool lists the card's article at its title path whatever its URL
   pattern, never the category link; the version-5 runtime still lists the category (the measured
   wrong output);
3. a card of another layout whose title link is not at the compiled path but has the demonstrated role
   (a heading link, the same descriptor) is read by its role; a category link is never a result, even
   in a heading when it is a section of the card's other link;
4. a card with no link of the demonstrated role is reported as unread (the list is INCOMPLETE when it
   sits among the top k), never answered with a category link;
5. the in-page read carries the role rule, and the recorded reading answers what the page would.
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

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_cards import read_of_html  # noqa: E402

BASE = "https://www.autoplus.test"
SITE = "www.autoplus.test"


def card(cat, cat_href, title, href, heading="h3"):
    """One ``div.post-item`` of the recorded structure (a category link, then the title link in a heading)."""
    cat_link = f'<a class="post-item-cat" href="{cat_href}">{cat}</a>' if cat else ""
    title_link = (f'<{heading} class="post-item-title"><a href="{href}">{title}</a></{heading}>' if heading else
                  f'<p class="post-item-lead"><a class="more" href="{href}">{title}</a></p>')
    return (f'<div class="post-item"><div class="post-item-img"><span><img src="data:," alt=""></span></div>'
            f'<div class="post-item-txt">{cat_link}{title_link}</div></div>')


def page(q, cards):
    return (f'<html><head><title>Recherche {q} - Auto Plus</title></head><body><header><nav>'
            f'<a href="/actualite">Actualité</a><a href="/pratique">Pratique</a><a href="/essais">Essais</a></nav>'
            f'</header><div id="content" class="pull-left"><div id="results" class="search list-large-items">'
            f'<h1>Résultats pour {q}</h1><div class="row items-posts">{"".join(cards)}</div></div></div>'
            f'<footer><a href="/mentions-legales">Mentions légales</a></footer></body></html>')


#: the demonstration's page ("attendues"): the 5 reported results, then a card whose article URL carries
#: its category (three path segments)
DEMO = [("Camping-car gallerie", "/camping-car", "Salon VDL 2026 : quelles sont les nouveautés attendues ?",
         "/diaporamas/salon-vdl-2026-quelles-sont-les-nouveautes-attendues-1468124.html"),
        ("Actualité", "/actualite", "Mondial 2026 : quelles sont les nouveautés attendues chez Hyundai ?",
         "/actualite/mondial-2026-quelles-sont-les-nouveautes-attendues-chez-hyundai-1468785.html"),
        ("Actualité gallerie", "/actualite", "Mondial 2026 : quelles sont les nouveautés attendues chez Citroën ?",
         "/actualite/mondial-2026-quelles-sont-les-nouveautes-attendues-chez-citroen-1468946.html"),
        ("Actualité", "/actualite", "Le Pape Léon XIV attendu à Paris dans un énorme 4x4 Mercedes Classe G",
         "/actualite/le-pape-leon-xiv-attendu-a-paris-dans-un-enorme-4x4-mercedes-classe-g-1467943.html"),
        ("Actualité", "/actualite", "Alfa Romeo pourrait ressusciter son mythique Spider avec un concept très attendu",
         "/actualite/alfa-romeo-pourrait-ressusciter-son-mythique-spider-1467777.html"),
        ("Insolite", "/actualite/insolite", "Une Honda Civic Type R HRC radicale attendue pour 2027",
         "/actualite/insolite/une-honda-civic-type-r-hrc-radicale-attendue-pour-2027-1465618.html"),
        ("Actualité", "/actualite", "Tesla devra encore attendre pour déployer son FSD partout en Europe",
         "/actualite/tesla-attendre-deployer-fsd-partout-europe-1468768.html")]
#: the "Auto" page (the oracle's render): the 4th card is the category + article card
AUTO = [("Pratique gallerie", "/pratique", "Peut-on sauver une auto destinée à la casse ?",
         "/pratique/peut-on-sauver-une-auto-destinee-a-la-casse-1468129.html"),
        ("Actualité", "/actualite", "Carburant : les auto-écoles privées d'aides du gouvernement",
         "/actualite/carburant-les-auto-ecoles-privees-daides-du-gouvernement-1468062.html"),
        ("Actualité", "/actualite", "Assurance auto : dans cette région, couvrir votre voiture coûte particulièrement cher",
         "/actualite/assurance-auto-dans-cette-region-couvrir-votre-voiture-coute-particulierement-cher-1468711.html"),
        ("LES INCONTOURNABLES", "/actualite/les-incontournables",
         "Assurance auto : jusqu'où peut-on personnaliser son contrat ?",
         "/actualite/les-incontournables/assurance-auto-jusquou-peut-on-personnaliser-son-contrat-1467374.html"),
        ("Actualité", "/actualite", "Hyundai annonce deux nouveaux SUV au Mondial de l'Auto de Paris",
         "/actualite/hyundai-annonce-deux-nouveaux-suv-au-mondial-de-lauto-de-paris-1468233.html"),
        ("Pratique", "/pratique", "Rouler à l'huile de colza peut-il faire sauter votre assurance auto ?",
         "/pratique/rouler-a-lhuile-de-colza-peut-il-faire-sauter-votre-assurance-auto-1468234.html")]


def cards_of(rows, **kw):
    return [card(c, BASE + ch, t, BASE + h, **kw) for c, ch, t, h in rows]


def trace():
    start = BASE + "/"
    rurl = BASE + "/?s=attendues"
    rp = read_of_html(page("attendues", cards_of(DEMO)), rurl)
    rep = {"results": [{"title": t, "url": BASE + h} for _c, _ch, t, h in DEMO[:5]]}
    home = read_of_html(page("", []), start)
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": "attendues",
            "steps": [{"k": 0, "page": home, "action": {"action": "goto", "url": rurl},
                       "outcome": {"ok": True, "how": "goto", "href": rurl, "url_after": rurl}},
                      {"k": 1, "page": rp, "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": rp, "result": rep, "stop": "done", "forbidden": []}


class FakePage:
    def __init__(self, pages):
        self.pages, self.cur = pages, ""

    async def goto(self, url):
        self.cur = url

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        r = read_of_html(self.pages.get(self.cur) or "<html><body>404 Not Found</body></html>", self.cur)
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom", "link_sigs")}
        if expr in (T.LINK_SIG_JS, T.PRICED_LINKS_JS):
            return {}
        return K.recorded_answer(expr, r)

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur


def compiled():
    got = C.compile_trace(trace(), version=10)
    assert got["ok"], got
    return got["tool"]


def run(tool, html, value):
    url = C.fill(tool["search"]["template"], value)
    return asyncio.run(T.run_tool(FakePage({url: html}), tool, value, lambda p: asyncio.sleep(0), result_wait_s=0.3))


def test_the_compiled_card_carries_the_title_links_role():
    t = compiled()
    cards = t["region"]["cards"]
    assert [c["chain"][-1] for c in cards] == ["div.post-item"], cards
    assert t["region"]["cards_version"] == (C.CARDS_VERSION_V8 if C.v8(t) else 6), t["region"]   # version 8: titles
    assert cards[0]["roles"] == [{"anchor": "a", "heading": True}], cards
    assert cards[0]["rels"] == ["a" + K.SEP + "h3.post-item-title" + K.SEP + "div.post-item-txt"], cards
    # the compiled URL pattern (one or two path segments) does not cover the category-in-path article
    import re
    rx = re.compile(t["region"]["regex"])
    assert rx.match(BASE + DEMO[1][3]) and not rx.match(BASE + DEMO[5][3]) and rx.match(BASE + "/actualite/insolite")


def test_the_prior_card_lists_its_article_never_its_category():
    t = compiled()
    out = run(t, page("Auto", cards_of(AUTO)), "Auto")
    assert out["status"] == "ok" and out["accepted"], out
    got = [r["title"] for r in out["output"]["results"]]
    assert got == [x[2] for x in AUTO[:5]], got
    assert out["output"]["results"][3]["url"] == BASE + AUTO[3][3], out["output"]["results"][3]
    # the version-5 runtime: the category link is the 4th result (the measured wrong output)
    old_region = {k: v for k, v in t["region"].items() if k != "cards_version"}
    old = run(dict(t, version=5, region=old_region), page("Auto", cards_of(AUTO)), "Auto")
    assert [r["title"] for r in old["output"]["results"]][3] == "LES INCONTOURNABLES", old["output"]


def test_a_title_link_of_another_layout_is_read_by_its_role():
    t = compiled()
    rows = list(AUTO)
    html = page("Auto", cards_of(rows[:3]) + [card(*rows[3][:2], rows[3][2], BASE + rows[3][3], heading="h2")] +
                cards_of(rows[4:]))
    html = html.replace(f'href="{rows[3][1]}"', f'href="{BASE}{rows[3][1]}"')
    out = run(t, html, "Auto")
    assert out["status"] == "ok", out
    assert out["output"]["results"][3]["title"] == AUTO[3][2], out["output"]["results"]
    # a category link in a heading is still a section of the card's article: never the result
    cat_in_heading = card("", "", AUTO[3][2], BASE + AUTO[3][3]).replace(
        '<div class="post-item-txt">', f'<div class="post-item-txt"><h4><a href="{BASE}/actualite/les-incontournables">'
                                        f'LES INCONTOURNABLES</a></h4>')
    r = read_of_html(page("Auto", cards_of(AUTO[:3]) + [cat_in_heading] + cards_of(AUTO[4:])), BASE + "/?s=Auto")
    items = C.region_items(r, t["region"])
    assert [i["title"] for i in items][3] == AUTO[3][2], items
    assert K.section_link(BASE + "/actualite/les-incontournables", [BASE + AUTO[3][3]])
    assert not K.section_link(BASE + AUTO[2][3], [BASE + AUTO[3][3]])


def test_a_card_with_no_link_of_the_title_role_is_unread_never_its_category():
    t = compiled()
    # the 4th card's article link is a "more" link in a paragraph (another role): only the category link
    # and a lead link remain; the card is reported unread and the top-5 list is INCOMPLETE
    odd = card(AUTO[3][0], BASE + AUTO[3][1], AUTO[3][2], BASE + AUTO[3][3], heading=None)
    out = run(t, page("Auto", cards_of(AUTO[:3]) + [odd] + cards_of(AUTO[4:])), "Auto")
    assert out["status"] == "incomplete" and not out["accepted"], out
    assert out["missing"][0]["why"] == "a result card with no link in the demonstrated title role", out["missing"]
    assert out["missing"][0]["title"].startswith("LES INCONTOURNABLES"), out["missing"]
    assert [r["title"] for r in out["output"]["results"]] == [x[2] for x in AUTO[:3]] + [AUTO[4][2], AUTO[5][2]], out
    assert all("les-incontournables" not in r["url"] for r in out["output"]["results"]), out
    # after the top 5 it does not make the list incomplete
    out = run(t, page("Auto", cards_of(AUTO[:5]) + [odd]), "Auto")
    assert out["status"] == "ok" and len(out["output"]["results"]) == 5, out


def test_the_in_page_read_carries_the_role_rule():
    t = compiled()
    specs = t["region"]["cards"]
    js = K.cards_js_v6(specs)
    assert K.specs_of_js_v6(js) == specs and K.specs_of_js(js) is None and js.lstrip().startswith(K.MARK6)
    assert "r.anchor === D(a) && !!r.heading === hrole" in js and "cards: rows" in js
    # the version-5 read is unchanged
    assert K.specs_of_js(K.cards_js(specs)) == specs and K.specs_of_js_v6(K.cards_js(specs)) is None
    r = read_of_html(page("Auto", cards_of(AUTO)), BASE + "/?s=Auto")
    ans = K.recorded_answer(js, r)
    assert ans["n"] == len(AUTO) and [c[3] for c in ans["cards"]] == [2] * len(AUTO), ans["cards"]
    assert all(v[4] == 1 for v in ans["map"].values() if v[2]), ans["map"]      # title-path links have the role


def test_a_card_whose_links_the_reader_sees_is_read_whatever_its_own_box():
    # a card of floated columns has a zero-height box (the in-page flag says not shown): its links, which
    # the reader lists as visible, decide (as on www.stereo.de)
    t = compiled()
    r = read_of_html(page("Auto", cards_of(AUTO)), BASE + "/?s=Auto")
    ans = K.recorded_answer(K.cards_js_v6(t["region"]["cards"]), r)
    for row in ans["cards"]:
        row[6] = 0
    got = K.read_region_v6(r["elements"], ans, t["region"], SITE)
    assert [i["title"] for i in got["items"]] == [x[2] for x in AUTO], got["items"]
    # a card none of whose links the reader saw (a hidden card) is neither a result nor unread
    gone = {BASE + AUTO[0][3], BASE + AUTO[0][1]}
    els = [e for e in r["elements"] if e.get("href") not in gone]
    got = K.read_region_v6(els, ans, t["region"], SITE)
    assert [i["title"] for i in got["items"]] == [x[2] for x in AUTO[1:]] and got["incomplete"] == [], got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
