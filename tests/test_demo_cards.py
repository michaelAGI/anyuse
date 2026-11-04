"""Result cards, offline (fake pages built from the multiplayer.it structure;
no browser, no network, no model): ONE result per result container.

The wrong output: multiplayer.it's site-search tool kept "the first k links of the result
URL pattern" and listed a game's per-platform links ("PC", "XONE", "PS4"; "IPHONE") - sub-links of
ONE result card - as separate results.

What must hold:

1. a demonstration whose result page shows a game card with per-platform sub-links compiles into a
   tool whose result CARD is the repeated game card (not a URL cluster and not the platform links'
   container), with the title link's path inside it;
2. on an unseen result page where every game has several platforms, the tool lists one result per
   card - the games' titles - and never a platform link; the version-4 region with the
   containers read from the same demonstration lists the platform links (the regression);
3. the demonstration's own result page is read back by the compiled structure (the compile's
   self-check), and a structure that does not read it back is an explicit compile failure;
4. a demonstration whose result page shows ONE result (no repeated ancestor:
   www.stockmann.com) compiles into the title link's own chain, which finds every result on a page
   with several (the version-4 one-result container excludes both results);
5. a title in the first plain ``div`` of a card and platform links in its third are told apart
   (positional paths), and several demonstrated results find the card that separates them;
6. an unreadable card map is an explicit failure, never an answer.
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

BASE = "https://multiplayer.test"
NAV = '<header><nav><a href="/giochi/">Giochi</a><a href="/articoli/">Articoli</a><a href="/video/">Video</a></nav></header>'
ASIDE = ('<aside class="col-md-4"><ul id="platform_slug" class="list-unstyled m-0">' +
         "".join(f'<li><a href="/{p}/">{p}</a></li>' for p in ("pc-windows", "playstation-4", "xbox-one")) +
         "</ul></aside>")


def game_card(slug, title, platforms, own):
    """One game card of the real structure (the kept multiplayer.it render)."""
    subs = ", ".join(f'<a href="/giochi/{slug}-per-{p}.html" title="{p}" class="text-uppercase font-weight-bold">{p}</a>'
                     for p in platforms)
    return (f'<div class="media position-relative mb-4 media--gamecard _search-gamecard">'
            f'<figure class="overflow-hidden position-relative mb-0 mr-2"><a href="/giochi/{slug}-per-{own}.html" '
            f'class="text-decoration-none"><img src="data:," alt=""></a></figure>'
            f'<div class="media-body"><h3 class="media-heading m-0 font-weight-bold text-primary text-uppercase ls-n05">'
            f'<a href="/giochi/{slug}-per-{own}.html" class="text-decoration-none">{title}</a></h3>'
            f'<div class="text-uppercase text-primary m-0 font-weight-bold">Gioco</div>'
            f'<div class="m-0">Data di uscita: <time>1 Febbraio 2019</time></div>'
            f'<div class="m-0">Disponibile per: {subs}</div></div></div>')


def article_card(slug, title):
    return (f'<div class="media position-relative mb-4 media--article">'
            f'<figure class="overflow-hidden position-relative mb-0 mr-2"><a href="/articoli/{slug}.html" '
            f'class="text-decoration-none"><img src="data:," alt="{title}"></a></figure>'
            f'<div class="media-body"><h3 class="media-heading m-0 font-weight-bold text-primary text-uppercase ls-n05">'
            f'<a href="/articoli/{slug}.html" class="text-decoration-none">{title}</a></h3>'
            f'<div class="m-0">Speciale</div></div></div>')


def result_html(q, cards, empty=False):
    body = ("<p>La ricerca non ha prodotto risultati</p>" if empty else
            '<div class="row"><div><div>' + "".join(cards) + "</div></div></div>")
    return (f"<html><head><title>Risultati per {q}</title></head><body>{NAV}"
            f'<div class="container"><div class="row">{ASIDE}<div class="col-md-8 order-md-1"><div class="p-2 p-md-3">'
            f'<h1 class="h4">Risultati per "{q}"</h1>{body}</div></div></div></div>'
            f'<footer><a href="/privacy/">Privacy</a></footer></body></html>')


#: the demonstration's page ("nuovi"): one game with 4 platforms, two with one, two articles
DEMO_CARDS = [game_card("dragons-lalba-dei-nuovi-cavalieri", "Dragons: L'Alba dei Nuovi Cavalieri",
                        ["pc", "ps4", "xone", "nsw"], "nsw"),
              game_card("nintendogs-3ds", "Nintendogs + Cats", ["3ds"], "3ds"),
              game_card("ea-sports-active-more-workouts", "EA Sports Active: Nuovi Esercizi", ["wii"], "wii"),
              article_card("gta-6-tutti-i-nuovi-dettagli", "Uragani e nuovi dettagli in GTA 6 - Speciale"),
              article_card("rtx-spark-provato", "RTX Spark: il gaming sui nuovi Ultrabook"),
              game_card("nuovi-orizzonti", "Nuovi Orizzonti", ["pc"], "pc")]
REPORTED = [("DRAGONS: L'ALBA DEI NUOVI CAVALIERI", "/giochi/dragons-lalba-dei-nuovi-cavalieri-per-nsw.html"),
            ("NINTENDOGS + CATS", "/giochi/nintendogs-3ds-per-3ds.html"),
            ("EA SPORTS ACTIVE: NUOVI ESERCIZI", "/giochi/ea-sports-active-more-workouts-per-wii.html"),
            ("Uragani e nuovi dettagli in GTA 6 - Speciale", "/articoli/gta-6-tutti-i-nuovi-dettagli.html"),
            ("RTX Spark: il gaming sui nuovi Ultrabook", "/articoli/rtx-spark-provato.html")]
#: an unseen query ("Gear"): every game has several platforms, its title links the first
GEAR = [("the-phantom-pain", "Metal Gear Solid V: The Phantom Pain", ["x360", "pc", "xone", "ps4", "ps3"]),
        ("metal-gear-solid-ground-zeroes", "Metal Gear Solid V: Ground Zeroes", ["x360", "pc", "ps4", "xone", "ps3"]),
        ("metal-gear-solid-rising", "Metal Gear Rising: Revengeance", ["pc", "x360", "ps3"]),
        ("gears-of-war-4", "Gears of War 4", ["xone", "pc"]),
        ("gears-of-war-3", "Gears of War 3", ["x360"]),
        ("gears-5", "Gears 5", ["xone", "pc", "xboxseriesx"]),
        ("metal-gear-survive", "Metal Gear Survive", ["pc", "ps4", "xone"])]
PLATFORM_WORDS = {"pc", "x360", "xone", "ps4", "ps3", "nsw", "3ds", "wii", "xboxseriesx", "iphone"}


def gear_html():
    return result_html("Gear", [game_card(s, t, ps, ps[0]) for s, t, ps in GEAR])


def read_of_html(html, url):
    """A page read as READ_JS gives it, from the HTML itself (elements numbered in CONTROL_SEL order)."""
    root, base = K.parse(html)
    els = []
    for k, n in enumerate(K.control_nodes(root)):
        e = {"i": k, "tag": n.tag, "text": K.label_of(n), "area": K.area_of(n), "role": n.attrs.get("role", ""),
             "type": (n.attrs.get("type") or "").lower(), "y": 10 * k}
        if n.tag == "a":
            e["href"] = K.href_of(n, url, base)
        els.append(e)
    text = K.text_of(root, 20000)
    title = next((K.text_of(n) for n in K.walk(root) if n.tag == "title"), "")
    return {"url": url, "title": title, "h1": [], "headings": [], "elements": els, "text": text, "main_text": text,
            "jsonld": [], "facts": [], "canonical": "", "og_type": "", "md_products": 0, "breadcrumb": [],
            "inputs": [{"index": 0, "visible": True, "name": "q"}], "meta": {}, "microdata": {},
            "link_sigs": link_sigs(root, url, base), "dom": html}


def link_sigs(root, url, base):
    """``demo_tool.LINK_SIG_JS`` in Python (the version-4 containers): per URL the chain of each
    link's nearest repeated ancestor."""
    out = {}
    for n, h in K.anchors(root, url, base):
        x, it = n, None
        for _ in range(8):
            if x.parent is None or x.parent.tag.startswith("#"):
                break
            if len(K.same_desc_siblings(x)) >= 2:
                it = x
                break
            x = x.parent
        sig = ">".join(K.chain_of(it)) if it is not None else ""
        lst = out.setdefault(h.split("#")[0], [])
        if sig not in lst and len(lst) < 4:
            lst.append(sig)
    return out


def demo_trace(cards=None, reported=None, value="nuovi"):
    start = BASE + "/"
    rurl = f"{BASE}/ricerca/?q={value}"
    home = read_of_html(result_html("", [], empty=True).replace("La ricerca non ha prodotto risultati", "Benvenuti"), start)
    rp = read_of_html(result_html(value, DEMO_CARDS if cards is None else cards), rurl)
    rep = {"results": [{"title": t, "url": BASE + u} for t, u in (REPORTED if reported is None else reported)]}
    return {"version": 1, "workflow": "site_search", "site": "multiplayer.test", "start_url": start, "value": value,
            "steps": [{"k": 0, "page": home, "action": {"action": "goto", "url": rurl},
                       "outcome": {"ok": True, "how": "goto", "href": rurl, "url_after": rurl}},
                      {"k": 1, "page": rp, "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": rp, "result": rep, "stop": "done", "forbidden": []}


class FakePage:
    """A guarded page on fake HTML (every read local); ``cards_ok=False``: the card read fails."""

    def __init__(self, pages, cards_ok=True):
        self.pages, self.cur, self.gotos, self.cards_ok = pages, "", [], cards_ok

    async def goto(self, url):
        self.cur = url
        self.gotos.append(url)

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        html = self.pages.get(self.cur) or "<html><body>404 Not Found</body></html>"
        r = read_of_html(html, self.cur)
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom", "link_sigs")}
        if expr == T.LINK_SIG_JS:
            return r["link_sigs"]
        if K.specs_of_js(expr) is not None or K.specs_of_js_v6(expr) is not None or K.specs_of_js_v8(expr) is not None \
                or K.specs_of_js_v10(expr) is not None or K.specs_of_js_v11(expr) is not None \
                or K.specs_of_js_v12(expr) is not None:
            if not self.cards_ok:
                raise RuntimeError("Runtime.evaluate: script error")
            return K.recorded_answer(expr, r)
        return None

    async def search_inputs(self):
        return [{"index": 0, "visible": True, "name": "q", "id": "", "type": "search"}]

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur


async def _settle(page):
    return None


def run(page, tool, value):
    return asyncio.run(T.run_tool(page, tool, value, _settle, result_wait_s=0.5))


def compiled():
    got = C.compile_trace(demo_trace())
    assert got["ok"], got
    return got["tool"]


def test_the_result_card_is_the_repeated_game_card():
    t = compiled()
    assert t["version"] == C.TOOL_VERSION and "containers" not in t["region"], t["region"]
    cards = t["region"]["cards"]
    assert [c["kind"] for c in cards] == ["card"] and cards[0]["chain"][-1] == "div.media.position-relative", cards
    # the title link's path inside the card: the heading's link (the figure's image link for the articles)
    assert any(r.startswith("a.text-decoration-none" + K.SEP + "h3.media-heading") for r in cards[0]["rels"]), cards
    rows = t and C.compile_trace(demo_trace())["report"]["structure"]["results"]
    assert [r["how"] for r in rows][:3] == ["separates"] * 3, rows


def test_one_result_per_card_on_an_unseen_page():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    out = run(FakePage({url: gear_html()}), t, "Gear")
    assert out["status"] == "ok" and out["accepted"] and out["model_calls"] == 0, out
    titles = [r["title"] for r in out["output"]["results"]]
    assert titles == [g[1] for g in GEAR[:5]], titles
    assert not any(W in PLATFORM_WORDS for W in (x.lower() for x in titles)), titles
    urls = [r["url"] for r in out["output"]["results"]]
    assert urls == [f"{BASE}/giochi/{s}-per-{ps[0]}.html" for s, _t, ps in GEAR[:5]], urls
    items = out["checks"]["result_page"]["items"]
    assert len(items) == len(GEAR), items           # one per card, the sub-links never items


def test_the_prior_region_listed_the_platform_links():
    t = compiled()
    rp = demo_trace()["steps"][1]["page"]
    # the version-4 compile: the containers of EVERY link of the reported URLs (a title link and a
    # platform link share the URL of a one-platform game)
    old = {k: v for k, v in t["region"].items() if k != "cards"}
    old["containers"] = C.containers_of(rp, [BASE + u for _t, u in REPORTED])
    assert len(old["containers"]) >= 2, old["containers"]
    r = read_of_html(gear_html(), f"{BASE}/ricerca/?q=Gear")
    j = C.judge_result_page(dict(t, version=4, region=old), r, "Gear")
    assert j["status"] == "list" and [x["title"].lower() for x in j["results"]][1:3] == ["pc", "xone"], j
    j2 = C.judge_result_page(t, r, "Gear")
    assert j2["status"] == "list" and [x["title"] for x in j2["results"]] == [g[1] for g in GEAR[:5]], j2


def test_the_compile_reads_the_demonstration_back():
    got = C.compile_trace(demo_trace())
    chk = got["report"]["structure"]["checks"][0]
    assert chk["ok"] and chk["expected_on_page"] == 5 and not chk["missing"], chk
    assert chk["first_items"][0]["sub_links"] == 3, chk     # the Dragons card's 3 other platforms
    # a structure that cannot read the reported results back: an explicit failure
    tr = demo_trace()
    region = {"regex": C.regex_of_template(BASE + "/{v}", 2)}
    page = tr["steps"][1]["page"]
    bad = [{"kind": "card", "chain": ["div", "div", "div", "div.nothing-like-this"], "rels": [""]}]
    ok, det = C.structure_check(page, dict(region, cards=bad), [(BASE + u, t) for t, u in REPORTED], "site_search")
    assert not ok and det["items"] == 0, det


def tile(slug, name, price):
    return (f'<div class="product"><div class="product-tile js-gtm-product-tile"><div class="image-container">'
            f'<a href="/{slug}/1-1.html"><img src="data:," alt="{name}"></a></div>'
            f'<div class="pdp-link"><a class="link" href="/{slug}/1-1.html">{name}</a></div>'
            f'<div class="price">{price} €</div></div></div>')


def shop_html(q, tiles):
    # the site's service pages share the products' URL pattern (www.stockmann.com: stockmann.com/<a>/<b>)
    svc = "".join(f'<li><a href="/{x}/palvelut.html">Palvelu {x}</a></li>' for x in ("naiset", "miehet", "kauneus"))
    return (f"<html><head><title>{q}</title></head><body>{NAV}<a href=\"/haku?q={q}\">Siirry pääsisältöön</a>"
            f'<main><div class="search-results"><div class="row product-grid">' + "".join(tiles) +
            f'</div></div><ul class="services">{svc}</ul></main></body></html>')


def test_a_one_result_page_compiles_into_the_title_chain():
    start = BASE + "/"
    rurl = f"{BASE}/haku?q=Pomar+Hunaja"
    rp = read_of_html(shop_html("Pomar Hunaja", [tile("pomar-hunaja-pistokkaat", "HUNAJA pistokkaat", "119,00")]), rurl)
    purl = f"{BASE}/pomar-hunaja-pistokkaat/1-1.html"
    import json
    ld = json.dumps({"@type": "Product", "name": "Pomar HUNAJA pistokkaat", "offers": {"price": 119.0, "priceCurrency": "EUR"}})
    from anyuse.execution import web_arch as W
    prod = {"url": purl, "title": "Pomar HUNAJA pistokkaat", "h1": ["Pomar HUNAJA pistokkaat"], "elements": [],
            "text": "Pomar HUNAJA pistokkaat 119,00 € Varastossa", "main_text": "Pomar HUNAJA pistokkaat 119,00 € Varastossa",
            "jsonld": [ld], "facts": W.product_facts([ld]), "canonical": purl, "og_type": "product"}
    rep = {"name": "Pomar HUNAJA pistokkaat", "price": "119,00", "currency": "EUR", "url": purl}
    tr = {"version": 1, "workflow": "product_lookup", "site": "multiplayer.test", "start_url": start,
          "value": "Pomar Hunaja",
          "steps": [{"k": 0, "page": read_of_html(shop_html("", []), start), "action": {"action": "goto", "url": rurl},
                     "outcome": {"ok": True, "how": "goto", "href": rurl, "url_after": rurl}},
                    {"k": 1, "page": rp, "action": {"action": "click", "element": 3},
                     "outcome": {"ok": True, "how": "goto", "href": purl, "url_after": purl}},
                    {"k": 2, "page": prod, "action": {"action": "done", "result": rep}, "outcome": {}}],
          "final_page": prod, "result": rep, "stop": "done", "forbidden": []}
    got = C.compile_trace(tr)
    assert got["ok"], got
    cards = got["tool"]["region"]["cards"]
    assert [c["kind"] for c in cards] == ["title"], cards
    # an unseen page with two products: both are results (the version-4 "" container keeps neither)
    r = read_of_html(shop_html("ONE PIECE", [tile("one-piece-bitty-pop", "FUNKO Bitty POP One Piece Going Merry", "24,99"),
                                             tile("one-piece-going-merry-laiva", "ONE PIECE GOING MERRY laiva, 28 cm",
                                                  "55,99")]), f"{BASE}/haku?q=ONE+PIECE")
    items = C.region_items(r, got["tool"]["region"])
    assert [i["url"].split("/")[3] for i in items] == ["one-piece-bitty-pop", "one-piece-going-merry-laiva"], items
    j = C.judge_result_page(got["tool"], r, "ONE PIECE GOING MERRY laiva")
    assert j["status"] == "pick" and "laiva" in j["pick"]["url"], j
    old = {k: v for k, v in got["tool"]["region"].items() if k != "cards"}
    old["containers"] = C.containers_of(rp, [purl])
    assert old["containers"] == [""], old            # the version-4 one-result container
    assert C.judge_result_page(dict(got["tool"], region=old), r, "ONE PIECE GOING MERRY laiva")["status"] == "failed"


def plain_card(k, title):
    return (f'<div class="hit"><div><a href="/g/{k}">{title}</a></div><div>2019</div>'
            f'<div><a href="/g/{k}-pc">pc</a></div></div>')


def test_positions_tell_a_title_div_from_a_platform_div():
    cards = [plain_card(f"game-{n}", f"Game Number {n}") for n in range(6)]
    html = f"<html><body><main><div class=\"results\">{''.join(cards)}</div></main></body></html>"
    url = f"{BASE}/s?q=game"
    st = K.result_cards(html, url, [(f"{BASE}/g/game-0", "Game Number 0"), (f"{BASE}/g/game-1", "Game Number 1")],
                        C.regex_of_template(BASE + "/g/{v}", 1))
    assert [(s["kind"], s["chain"][-1]) for s in st["cards"]] == [("card", "div.hit")], st
    assert st["cards"][0]["rels"] == ["a" + K.SEP + "div@0"], st        # the FIRST plain div
    r = read_of_html(html, url)
    items = C.region_items(r, {"regex": C.regex_of_template(BASE + "/g/{v}", 1), "cards": st["cards"]})
    assert [i["title"] for i in items] == [f"Game Number {n}" for n in range(6)], items
    # one demonstrated result: its siblings hold the others at the same place
    st1 = K.result_cards(html, url, [(f"{BASE}/g/game-2", "Game Number 2")], C.regex_of_template(BASE + "/g/{v}", 1))
    assert st1["cards"][0]["chain"][-1] == "div.hit" and st1["results"][0]["how"] == "siblings", st1


def test_the_dom_links_are_serialized_as_the_browser_does():
    # a recorded DOM keeps the raw href ("?q=Knoll International", "/kompottlöffel"); the reader's a.href
    # (and every URL of the trace) is the browser's serialization ("%20", "%C3%B6")
    assert K.browser_url("https://X.test/p/kompottlöffel?q=Knoll International&x='a'") == \
        "https://x.test/p/kompottl%C3%B6ffel?q=Knoll%20International&x=%27a%27"
    assert K.browser_url("https://x.test/a%20b?q=100%") == "https://x.test/a%20b?q=100%"
    tiles = [f'<div class="hit"><a class="t" href="/p/löffel-{k}.html?q=Knoll International">Löffel Nummer {k}</a></div>'
             for k in range(4)]
    html = f"<html><body><main><div class=\"results\">{''.join(tiles)}</div></main></body></html>"
    url = f"{BASE}/s?q=L%C3%B6ffel"
    page = read_of_html(html, url)
    for e in page["elements"]:                      # the reader's elements carry a.href (serialized)
        if e.get("href"):
            e["href"] = K.browser_url(e["href"].replace("%20", " ").replace("%C3%B6", "ö"))
    want = [(f"{BASE}/p/l%C3%B6ffel-{k}.html?q=Knoll%20International", f"Löffel Nummer {k}") for k in range(2)]
    region = {"regex": C.regex_of_template(BASE + "/p/{v}", 1)}
    rep: dict = {}
    assert C.compile_cards(region, dict(page, url=url), want, "site_search", rep) == "", rep
    assert region["cards"][0]["chain"][-1] == "div.hit" and rep["structure"]["checks"][-1]["ok"], rep
    rc = K.recorded_cards(page, region["cards"])
    assert rc["aligned"] == 1.0 and len(rc["map"]) == 4, rc


def test_an_unreadable_card_map_is_an_explicit_failure():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    out = run(FakePage({url: gear_html()}, cards_ok=False), t, "Gear")
    assert out["status"] == "failed" and not out["accepted"] and "containers could not be read" in out["why"], out


def test_the_cards_js_carries_its_specs_and_the_reader_selector():
    from anyuse.execution.web_arch_run import CONTROL_SEL
    specs = compiled()["region"]["cards"]
    js = K.cards_js(specs)
    assert K.specs_of_js(js) == specs and CONTROL_SEL in js and K.MARK in js
    assert K.specs_of_js("(() => 1)()") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
