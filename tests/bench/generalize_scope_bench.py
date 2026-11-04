"""Offline A/B bench: does the scope, search and discovery code decide by the site's own evidence, in any language?

Each case is one decision with the answer a person would give (``want``), made by the tree's own code with no
socket, no browser and no model:

* ``robots``: which robots.txt group applies to AnyUse (``api_permission.RobotsRules``): a group for another crawler
  whose name sits inside AnyUse's token (``flow``, ``use``) must not, AnyUse's own family name must;
* ``own_host``: is a host the site's own (``web_scope.WebScope.classify``, site-own hosts on): sibling shops under
  country suffixes nobody owns (``com.pk``, ``org.ng``, ``net.pl``, ``co.ve``, ``web.id``, ``waw.pl``), and the
  site's own CDN hosts the site's document names;
* ``search_post``: is a POST sent right after typing the page's own search (``cdp_guard.LoadGuard.decide``, the
  search-POST knob on): Dutch, Polish, Turkish, Czech, Swedish, Vietnamese, Thai, Indonesian and Portuguese field
  names, and decoys that must stay refused (a state word, a telemetry path, the value inside a longer field, not
  right after typing, a credential header);
* ``post_rule``: the same-site POST rule of the web scope (``WebScope.classify``, read-only POST search on), with a
  ``<search>`` form and a ``/research`` decoy;
* ``asset_host``: static assets from a host the site's own document loads them from (a store platform no catalogue
  lists), the catalogue's own platform, and hosts only linked to or answering with data;
* ``learn_value``: is a page learn opened for a generated value kept (``learn_loop._generated_page_ok`` with the
  tree's own cascade): a shop whose cart line says "0 items" on every page, empty pages in Portuguese and Czech
  that show suggestions, an English empty page, a good page;
* ``discover``: paging keys in Polish, Turkish, Czech and Vietnamese, per-page, year and id decoys, filter, listing,
  detail-key and language words, search inputs by structure, Dutch function words, a Dutch-named sort;
* ``search_box``: which input a typed search uses, and which control opens a search that is only an icon: each
  tree's own page scripts (``cdp_guard.SEARCH_INPUTS_JS``, ``REVEAL_SEARCH_JS``) evaluated in a small DOM stand-in in
  Node, then the tree's ``GuardedSession.type_search`` driven by those answers and by how the fixture page answers
  typing (a request carrying the value, its text showing it, its own GET form);
* ``input_parity``: the offline page (``dom_page``, which the offline replays and benches drive) against the live
  script on the same pages: the search-like inputs, the input a typed replay of each recorded input types into (a
  version-15 tool and one before), and whether the probe canary finds what the live replay types into.

Usage::

    python tests/bench/generalize_scope_bench.py run --old <tree> --new <tree> \\
        [--python <interpreter>] [--node <node binary>] [--out <dir>]
    python tests/bench/generalize_scope_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the branch forked from (read only), ``--new`` the branch. The script copies
itself into ``--out`` (outside both trees) and runs that copy once per tree, with a pkgroot holding an ``anyuse``
symlink to the tree and the tree's ``tools/guarded_tests`` on PYTHONPATH (its sitecustomize refuses sockets). HOME
and every AnyUse data path point into ``--out``; no ``ANYUSE_*`` variable is passed through.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

GROUPS = ("robots", "own_host", "search_post", "post_rule", "asset_host", "learn_value", "discover", "search_box",
          "input_parity")
#: cases NEW is expected to answer differently from the person, with the reason (the cost of a rule)
KNOWN_COSTS = {
    ("own_host", "sv: a sibling host the site never names"):
        "under a country suffix with public second levels this build does not list, only the site's own "
        "document, script or redirect binds a sibling host",
}


def row(group: str, case: str, want: Any, got: Any, why: str = "", lang: str = "") -> Dict[str, Any]:
    return {"group": group, "case": case, "want": want, "got": got, "why": str(why)[:200], "lang": lang}


# ---------------------------------------------------------------------------------------------------------------------
# robots
# ---------------------------------------------------------------------------------------------------------------------

def robots_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import api_permission as P
    out = []
    cases = [(f"group '{v}' (another crawler)", f"User-agent: {v}\nDisallow: /\n\nUser-agent: *\nDisallow: /p/\n",
              "/x", True) for v in ("flow", "work", "use", "any", "compiled-workflow")]
    cases += [("group 'AnyUse' (the product family)", "User-agent: AnyUse\nDisallow: /x\n\nUser-agent: *\nDisallow: /\n",
               "/x", False),
              ("group 'AnyUse', another path", "User-agent: AnyUse\nDisallow: /x\n\nUser-agent: *\nDisallow: /\n",
               "/y", True),
              ("group with the full token", "User-agent: anyuse-compiled-workflow\nDisallow: /x\n", "/x", False),
              ("group with token and version", "User-agent: AnyUse-Compiled-Workflow/0.1\nDisallow: /x\n", "/x",
               False),
              ("only the '*' group", "User-agent: *\nDisallow: /x\n", "/x", False)]
    for case, text, path, want in cases:
        r = P.RobotsRules()
        r.parse(text.splitlines())
        out.append(row("robots", case, want, bool(r.can_fetch(P.ROBOTS_AGENT, "https://site.test" + path))))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# own hosts
# ---------------------------------------------------------------------------------------------------------------------

OWN_CASES = [
    # (case, site, host, want own, the site's own document names the host)
    ("pk: another shop under com.pk", "www.shop-a.com.pk", "other.com.pk", False, False),
    ("pk: the shop's own CDN", "www.shop-a.com.pk", "cdn.shop-a.com.pk", True, False),
    ("ng: another shop under org.ng", "www.duka.org.ng", "another.org.ng", False, False),
    ("pl: another shop under net.pl", "www.sklep.net.pl", "inny.net.pl", False, False),
    ("pl: a city suffix the lists miss", "a.waw.pl", "b.waw.pl", False, False),
    ("ve: another shop under co.ve", "tienda.co.ve", "otra.co.ve", False, False),
    ("id: another shop under web.id", "www.toko.web.id", "lain.web.id", False, False),
    ("uk: the site's static host (listed suffix)", "www.example.co.uk", "static.example.co.uk", True, False),
    ("io: a sibling tenant (shared hosting)", "alice.github.io", "bob.github.io", False, False),
    ("cz: the shop's CDN (flat country suffix)", "www.obchod.cz", "cdn.obchod.cz", True, False),
    ("th: the shop's images (listed suffix)", "www.ranshop.co.th", "img.ranshop.co.th", True, False),
    ("sv: the CDN the site's document names", "www.butik.se", "cdn.butik.se", True, True),
    ("vi: the CDN the site's document names", "www.cuahang.vn", "cdn.cuahang.vn", True, True),
    ("sv: a sibling host the site never names", "www.butik.se", "x.butik.se", True, False),
    ("tr: the shop's API host (listed suffix)", "www.dukkan.com.tr", "api.dukkan.com.tr", True, False),
]


def own_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import web_scope as W
    out = []
    for case, site, host, want, named in OWN_CASES:
        sc = W.WebScope(own_hosts=True, blocklist_domains=frozenset({"tracker.test"}))
        if named:
            sc.observe(site, f"https://{site}/", "Document", f'<img src="https://{host}/a.png"><p>shop</p>')
        v = sc.classify(site, f"https://{host}/app.js", "GET", "Script")
        out.append(row("own_host", case, want, bool(v.ok and v.kind == W.KIND_OWN), v.why or v.detail,
                       case.split(":")[0]))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# search POSTs
# ---------------------------------------------------------------------------------------------------------------------

JSON_CT = {"content-type": "application/json"}
FORM_CT = {"content-type": "application/x-www-form-urlencoded"}
POST_CASES = [
    # (case, lang, path, body, headers, typed, acting, want)
    ("nl: trefwoord", "nl", "/api/zoeken", {"trefwoord": "fiets"}, JSON_CT, "fiets", True, True),
    ("pl: fraza", "pl", "/api/wyszukaj", {"fraza": "rower", "strona": 1}, JSON_CT, "rower", True, True),
    ("tr: kelime (form body)", "tr", "/arama/sonuc", "kelime=bisiklet&sayfa=1", FORM_CT, "bisiklet", True, True),
    ("cs: hledat path", "cs", "/hledat", {"vyraz": "kolo"}, JSON_CT, "kolo", True, True),
    ("sv: fras", "sv", "/api/sok", {"fras": "cykel"}, JSON_CT, "cykel", True, True),
    ("vi: tu_khoa", "vi", "/api/tim-kiem", {"tu_khoa": "xe dap"}, JSON_CT, "xe dap", True, True),
    ("th: Thai field name", "th", "/api/ค้นหา", {"คำ": "จักรยาน"}, JSON_CT, "จักรยาน", True, True),
    ("id: kata", "id", "/api/cari", {"kata": "sepeda"}, JSON_CT, "sepeda", True, True),
    ("pt: termo in a query string inside the body", "pt", "/api/x", {"params": "termo=bicicleta&hits=20"}, JSON_CT,
     "bicicleta", True, True),
    ("en: query (old case)", "en", "/api/Search", {"query": "bike", "page": 1}, JSON_CT, "bike", True, True),
    ("decoy: a state word in the body", "nl", "/api/zoeken", {"trefwoord": "fiets", "addToCart": 1}, JSON_CT, "fiets",
     True, False),
    ("decoy: a telemetry path", "en", "/collect", {"q": "bike"}, JSON_CT, "bike", True, False),
    ("decoy: an events path", "en", "/api/events", {"query": "bike"}, JSON_CT, "bike", True, False),
    ("decoy: the value inside a longer field, no search name", "en", "/api/notes", {"note": "bike rack"}, JSON_CT,
     "bike", True, False),
    ("decoy: not right after typing", "nl", "/api/zoeken", {"trefwoord": "fiets"}, JSON_CT, "fiets", False, False),
    ("decoy: a credential header", "nl", "/api/zoeken", {"trefwoord": "fiets"},
     {"content-type": "application/json", "authorization": "Bearer x"}, "fiets", True, False),
]


def search_post_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import cdp_guard as G
    from anyuse.execution import discover as D
    out = []
    for case, lang, path, body, hdrs, typed, acting, want in POST_CASES:
        pacer = D.Pacer(0.0, clock=lambda: 1000.0, sleeper=lambda s: None)
        b = G.RequestBudget(pacer, site_cap=20, load_per_navigation=True, browser_statics=True, search_post=True)
        g = G.LoadGuard(b)
        g.start("https://shop.test/")
        g.typed.append(typed.lower())
        g.acting = 1 if acting else 0
        why = g.decide("https://shop.test" + urllib.parse.quote(path, safe="/"), "XHR", "POST",
                       body if isinstance(body, str) else json.dumps(body, ensure_ascii=False), hdrs)
        out.append(row("search_post", case, want, why == "",
                       why or "sent", lang))
    return out


def post_rule_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import web_scope as W
    out = []
    site = "www.winkel.test"
    cases = [("pl: fraza right after typing", "pl", "/api/wyszukaj", json.dumps({"fraza": "kawa"}), ["kawa"], (),
              True, True, ""),
             ("tr: kelime right after typing", "tr", "/arama", "kelime=kahve", ["kahve"], (), True, True, ""),
             ("vi: tu_khoa right after typing", "vi", "/tim-kiem", json.dumps({"tu_khoa": "ca phe"}), ["ca phe"], (),
              True, True, ""),
             ("pl: a POST form inside <search>", "pl", "/szukaj", "fraza=kawa", [], (), False, True,
              '<search><form method="post" action="/szukaj"><input name="fraza"></form></search>'),
             ("en: q (old case)", "en", "/search", "q=coffee", ["coffee"], (), False, True, ""),
             ("decoy: /research with a run value", "en", "/research/topics", "topic=coffee", [], ("coffee",), False,
              False, ""),
             ("decoy: a state word", "en", "/cart/search", "q=coffee", ["coffee"], (), True, False, ""),
             ("decoy: a run value in a field named like nothing", "en", "/api/items", json.dumps({"name": "coffee"}),
              [], ("coffee",), True, False, "")]
    for case, lang, path, body, typed, values, acting, want, page in cases:
        sc = W.WebScope(readonly_post=True)
        if page:
            sc.observe(site, f"https://{site}/", "Document", page)
        if typed:
            sc.note_values(site, typed)
        kw = {"acting": acting} if "acting" in W.WebScope.classify.__code__.co_varnames else {}
        v = sc.classify(site, f"https://{site}{path}", "POST", "XHR", body, same_site=True, values=values, **kw)
        out.append(row("post_rule", case, want, bool(v.ok), v.why or v.detail, lang))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# asset hosts
# ---------------------------------------------------------------------------------------------------------------------

def asset_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import web_scope as W
    site = "www.fietsen.test"
    page = ('<link rel="stylesheet" href="https://static.winkelplatform.test/thema/basis.css">'
            '<style>@font-face{src:url("https://lettertype.cdnplatforma.test/f/a.woff2")}</style>'
            '<img srcset="https://beelden.winkelplatform.test/p/1.webp 1x">'
            '<script src="https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.js"></script>'
            '<a href="https://sociaal.netwerk.test/fietsen">volg ons</a>')
    cases = [("nl: a stylesheet from the platform host the document loads", "https://static.winkelplatform.test/a.css",
              "Stylesheet", True, None),
             ("pl: a font the document's CSS loads", "https://lettertype.cdnplatforma.test/f/a.woff2", "Font", True,
              None),
             ("nl: an image from the srcset host", "https://beelden.winkelplatform.test/p/2.webp", "Image", True, None),
             ("en: the catalogued platform's script (old case)",
              "https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.js", "Script", True, None),
             ("decoy: a host only linked to", "https://sociaal.netwerk.test/x.png", "Image", False, None),
             ("decoy: data from the asset host", "https://static.winkelplatform.test/api/p.json", "XHR", False, None),
             ("decoy: an image after the host answered with data", "https://beelden.winkelplatform.test/p/3.webp",
              "Image", False, ("beelden.winkelplatform.test", "application/json"))]
    out = []
    for case, url, rtype, want, data in cases:
        sc = W.WebScope(providers=True, platform_cdn=True, observed_providers=True, mode="search",
                        blocklist_domains=frozenset({"tracker.test"}))
        sc.observe(site, f"https://{site}/", "Document", page)
        if data and hasattr(sc, "note_response"):
            sc.note_response(site, data[0], data[1], 200)
        v = sc.classify(site, url, "GET", rtype)
        out.append(row("asset_host", case, want, bool(v.ok), v.why or v.detail, case.split(":")[0]))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# learn: pages of generated values
# ---------------------------------------------------------------------------------------------------------------------

def _shop_page(value: str, rows: Sequence[str], related: Sequence[str], empty_line: str, extra: str = "") -> str:
    items = "".join(f'<li class="produto"><a href="/p/{i}">{r}</a> <span>9,90</span></li>'
                    for i, r in enumerate(rows))
    rel = "".join(f'<li><a href="/busca?q={x}">{x}</a></li>' for x in related)
    body = f'<ul class="lista">{items}</ul>' if rows else f"<p>{empty_line.format(q=value)}</p>"
    return (f"<html><head><title>Busca: {value} - loja.test</title></head><body>{extra}<nav><a>Inicio</a>"
            f"<a>Ofertas</a></nav><h1>Resultados para {value}</h1>{body}<ul class=\"relacionadas\">{rel}</ul>"
            f"</body></html>")


def _run(value: str, dom: str):
    from anyuse.execution.agent_browser import AgentRun
    text = "\n".join(x.strip() for x in re.sub(r"<[^>]+>", "\n", dom).split("\n") if x.strip())
    url = f"https://loja.test/busca?q={urllib.parse.quote_plus(value)}"
    return AgentRun(value=value, task="t", start_url="https://loja.test/", final_url=url, dom=dom, text=text,
                    steps=[{"action": "navigate", "url": url}], model_calls=0)


def learn_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import learn_loop as L
    out = []
    static = ["Cafe", "Cha", "Leite"]
    cart = '<header><a class="cart">Carrinho: 0 items</a></header>'
    sites = {
        "pt": ("Nenhum produto encontrado para {q}.", ["cafe torrado", "cafe moido", "cafe em grao"],
               ["cha verde", "cha preto", "cha mate"], ["leite integral", "leite desnatado", "leite em po"]),
        "cs": ("Pro dotaz {q} nebyly nalezeny zadne vysledky.", ["kava zrnkova", "kava mleta", "kava instantni"],
               ["caj zeleny", "caj cerny", "caj bylinny"], ["mleko plnotucne", "mleko odtucnene", "mleko kozi"]),
    }
    for lang, (line, first_rows, second_rows, good_rows) in sites.items():
        def page(v, rows, rel=None, _line=line):
            return _run(v, _shop_page(v, rows, rel if rel is not None else static, _line))
        first, second = page("cafe", first_rows), page("cha", second_rows)

        def navigate(v, _page=page, _first=first, _second=second):
            known = {"cafe": _first, "cha": _second}
            return known.get(v) or _page(v, [], [f"{v} preto", f"{v} branco", f"{v} azul"])
        cand = _cand(L, first)
        empty = _page_ok(L, cand, page("zuvak", [], ["zuvak preto", "zuvak branco", "zuvak azul"]), [second],
                         navigate)
        good = _page_ok(L, cand, page("leite", good_rows), [second], navigate)
        out.append(row("learn_value", f"{lang}: the site's own empty page with suggestions", False, empty[0],
                       empty[1], lang))
        out.append(row("learn_value", f"{lang}: a page with results", True, good[0], good[1], lang))
    # a shop whose every page says "0 items" in its cart line
    c_first = _run("koffie", _shop_page("koffie", ["koffie bonen", "koffie filter", "koffie molen"], static, "",
                                        cart))
    c_gen = _run("thee", _shop_page("thee", ["thee groen", "thee zwart", "thee munt"], static, "", cart))
    cand = _cand(L, c_first)
    got = _page_ok(L, cand, c_gen, [], None)
    out.append(row("learn_value", "nl: a cart line '0 items' on every page, results shown", True, got[0], got[1], "nl"))
    en_empty = _run("zzz", _shop_page("zzz", [], [], "No results found for {q}"))
    got = _page_ok(L, cand, en_empty, [], None)
    out.append(row("learn_value", "en: 'No results found' and no list (old case)", False, got[0], got[1], "en"))
    return out


def _cand(L, run):
    cand = L.Candidate(workflow="w", task="t", start_url="https://loja.test/", param="q", value=run.value,
                       final_url=run.final_url,
                       url_template=run.final_url.replace(urllib.parse.quote_plus(run.value), "{q}"))
    cand.recording = run.as_recording("w", "q", "agent0")
    cand.preview_selector, cand.preview = L.preview_items(run.dom)
    return cand


def _page_ok(L, cand, run, good, navigate) -> Tuple[bool, str]:
    landing = lambda c, r: (True, "")  # noqa: E731
    if hasattr(L, "SiteEmptyPage"):
        return L._generated_page_ok(cand, run, landing, L.SiteEmptyPage(cand, good, navigate))
    return L._generated_page_ok(cand, run, landing)


# ---------------------------------------------------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------------------------------------------------

def _html(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def discover_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import discover as D
    out = []
    O = "https://winkel.test"

    def pages(body, url):
        return D.pagination_candidates(D.scan_page(_html("Lijst", body), url))
    for lang, key, vals, texts in (("pl", "strona", [2, 3, 4, 5], ["2", "3", "4", "5"]),
                                   ("tr", "sayfa", [1, 2, 4, 9], ["1", "2", "4", "9"]),
                                   ("cs", "od", [20, 40, 60], ["2", "3", "4"]),
                                   ("en", "page", [2, 3], ["2", "3"])):
        body = "".join(f"<a href='/lijst?q=thee&{key}={v}'>{t}</a>" for v, t in zip(vals, texts))
        got = pages(body, O + "/lijst?q=thee")
        out.append(row("discover", f"{lang}: paging key '{key}'", True, any(c.param == key for c in got), "", lang))
    got = pages("<link rel='next' href='/lijst?q=thee&trang=2'>", O + "/lijst?q=thee")
    out.append(row("discover", "vi: rel=next with 'trang'", True, any(c.param == "trang" for c in got), "", "vi"))
    for case, key, vals in (("decoy: per-page choice", "ile", [12, 24, 48]),
                            ("decoy: year filter", "rok", [2019, 2020, 2021, 2022]),
                            ("decoy: item ids", "id", [101, 102, 103])):
        body = "".join(f"<a href='/lijst?{key}={v}'>{v}</a>" for v in vals)
        out.append(row("discover", case, False, bool(pages(body, O + "/lijst")), "", ""))
    opts = "".join(f"<option value='{v}'>{v}</option>" for v in ("a", "b", "c", "d", "e"))
    for name, want in (("location", 2.0), ("sortBy", 4.0)):
        sc = D.scan_page(_html("L", f"<form method='get' action='/lijst'><select name='{name}'>{opts}</select></form>"),
                         O + "/lijst")
        got = [c.score for c in D.filter_candidates(sc)]
        out.append(row("discover", f"filter score of a select named '{name}'", want, got[0] if got else None, "",
                       "en"))
    for text, want in (("Newsletter", False), ("Listen now", False), ("New arrivals", True), ("Browse all", True)):
        sc = D.scan_page(_html("H", f"<a href='/x'>{text}</a>"), O + "/")
        out.append(row("discover", f"listing link '{text}'", want, bool(D.listing_links(sc, None)), "", "en"))
    for url, key, want in (("https://s.test/tvshow/1", "tv", False), ("https://s.test/p/12345-blue-shirt", "12345",
                                                                      True)):
        out.append(row("discover", f"detail key {key!r} in {url[15:]}", want, bool(D.key_in_url(url, key)), "", ""))
    for v, want in (("tv", False), ("ai", False), ("pc", False), ("db", False), ("en", True), ("pt-br", True),
                    ("go", False), ("jq", False)):
        out.append(row("discover", f"value {v!r} is a section or language", want, bool(D.is_nav_value(v)), "", ""))
    page = ("<search><input name='fraza'></search><input role='searchbox' name='aranan'>"
            "<input name='research_area'><input name='q'>")
    inputs = {i["name"]: i for i in D.scan_page(_html("S", page), O + "/").inputs}
    for name, want, lang in (("fraza", True, "pl"), ("aranan", True, "tr"), ("research_area", False, "en"),
                             ("q", True, "en")):
        out.append(row("discover", f"search input '{name}'", want, bool(D.is_search_input(inputs[name])), "", lang))
    forms = ("<form action='/research/list' method='get' class='research-filter'>"
             "<input name='topic' placeholder='Research topic'></form>"
             "<search><form action='/zoeken' method='get'><input name='zoekterm'></form></search>"
             "<form action='/szukaj' method='get'><input name='miasto'><input name='fraza' role='searchbox'></form>"
             "<form action='/search' method='get'><input name='q' placeholder='Search'></form>")
    caps = {c.param: c.score for c in D.search_from_forms(D.scan_page(_html("F", forms), O + "/"))}
    out.append(row("discover", "pl: the form's role=searchbox field is its search field", True, "fraza" in caps,
                   ",".join(caps), "pl"))
    out.append(row("discover", "nl: a GET form inside <search> ranks above a /research form", True,
                   caps.get("zoekterm", -1) > caps.get("topic", -1), str(caps), "nl"))
    out.append(row("discover", "en: a /search form named q ranks first (old case)", True,
                   max(caps, key=caps.get) == "q", str(caps), "en"))
    out += _frame_rows(D)
    out += _sort_rows(D)
    return out


def _frame_rows(D) -> List[Dict[str, Any]]:
    O = "https://winkel.test"
    body = ("<div class='kop'><a href='/over'>Over ons</a><a href='/hulp'>Hulp</a></div><p>Voor deze winkel geldt: "
            "naar onze voorwaarden, maar niet voor alles.</p><div class='lijst'>{rows}</div>")
    scans, bodies = [], {}
    for word, rows in (("thee", ["groene thee", "zwarte thee", "kruidenthee"]),
                       ("koffie", ["koffiebonen arabica", "koffiebonen robusta", "filterkoffie"]),
                       ("melk", ["volle melk", "halfvolle melk", "karnemelk"])):
        b = _html(word, body.format(rows="".join(f"<a href='/p/{r.replace(' ', '-')}'>{r}</a>" for r in rows)))
        scans.append(D.scan_page(b, f"{O}/{word}"))
        bodies[f"{O}/{word}"] = b
    words = D.content_word_values(scans, n=8, bodies=bodies)
    function = {"voor", "deze", "naar", "onze", "maar"}
    return [row("discover", "nl: function words are no search values", True, not (set(words) & function),
                ",".join(words), "nl")]


def _sort_rows(D) -> List[Dict[str, Any]]:
    from anyuse.execution.api_step import ApiResponse
    O = "https://winkel.test"
    items = ["appel", "banaan", "citroen", "dadel", "druif", "kiwi", "mango", "peer"]

    def shop(url, reorder):
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        html = {"content-type": "text/html"}
        if p.path != "/lijst":
            return 404, html, _html("Niet gevonden", "")
        names = list(items)
        v = q.get("volgorde", "naam")
        if reorder:
            k = {"naam": 0, "prijs": 3, "nieuw": 5, "populair": 2}.get(v, 0)
            names = names[k:] + names[:k]
            if v == "prijs":
                names = names[::-1]
        rows = "".join(f"<li class='product'><a href='/p/{n}'>{n}</a> <span>{n} fruit</span></li>" for n in names)
        links = "".join(f"<a href='/lijst?volgorde={x}'>{x}</a>" for x in ("naam", "prijs", "nieuw", "populair"))
        return 200, html, _html("Fruit", f"<ul class='lijst'>{rows}</ul>{links}")

    class Transport:
        def __init__(self, reorder):
            self.reorder = reorder

        def send(self, method, url, headers, body, timeout_s):
            st, h, text = shop(url, self.reorder)
            return ApiResponse(status=st, headers=h, text=text)
    out = []
    os.environ["ANYUSE_API_MODALITY"] = "on"
    for reorder, want, case in ((True, True, "nl: a filter 'volgorde' that reorders the same items is a sort"),
                                (False, False, "decoy: a filter that reorders nothing")):
        f = D.HttpFetcher(user_agent="research-probe/0.1", pacer=D.Pacer(0.0),
                          client=lambda url, h, _r=reorder: shop(url, _r))
        cap = D.Capability(cap="filter", template=O + "/lijst?volgorde={v}", param="volgorde",
                           values=["naam", "prijs", "nieuw", "populair"], source="links")
        got = D.prove(cap, O, f, transport=D.PacedTransport(f, inner=Transport(reorder)),
                      proof_preflight=lambda c, r: "")
        out.append(row("discover", case, want, got.status == "proved", got.reason, "nl"))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# the search box: page scripts in a DOM stand-in, then the tree's own typed search
# ---------------------------------------------------------------------------------------------------------------------

#: a small DOM for the page scripts: elements from a JSON tree ``[tag, attrs, text, children]``; ``hidden`` hides an
#: element and everything in it; a click on an element with ``data-toggle`` shows or hides the element of that id
DOM_JS = r"""
'use strict';
const vm = require('vm');
const parse = sel => sel.split(',').map(s => s.trim()).filter(Boolean).map(s => {
  const m = /^([a-zA-Z0-9]*)(.*)$/.exec(s), out = {tag: m[1].toUpperCase(), has: []};
  const re = /\[([^\]=]+)(?:=([^\]]+))?\]/g; let a;
  while ((a = re.exec(m[2]))) out.has.push({k: a[1], v: a[2] === undefined ? null : a[2].replace(/^['"]|['"]$/g, '')});
  return out; });
const ok = (el, a) => el.hasAttribute(a.k) && (a.v === null || el.getAttribute(a.k) === a.v);
const matches = (el, sel) => parse(sel).some(p => (!p.tag || el.tagName === p.tag) && p.has.every(a => ok(el, a)));
let doc = null, clicks = [], navigated = '';
class El {
  constructor(tag, attrs, text) { this.tagName = tag.toUpperCase(); this.attrs = Object.assign({}, attrs || {});
    this.text = text || ''; this.children = []; this.parentNode = null; }
  getAttribute(k) { return k in this.attrs ? String(this.attrs[k]) : null; }
  hasAttribute(k) { return k in this.attrs; }
  get id() { return this.attrs.id || ''; }
  get name() { return this.attrs.name || ''; }
  get className() { return this.attrs['class'] || ''; }
  get placeholder() { return this.attrs.placeholder || ''; }
  get value() { return this.attrs.value || ''; }
  get disabled() { return 'disabled' in this.attrs; }
  get readOnly() { return 'readonly' in this.attrs; }
  get htmlFor() { return this.attrs['for'] || ''; }
  get href() { return new URL(this.attrs.href || '', doc.url).href; }
  get parentElement() { return this.parentNode; }
  get form() { for (let n = this.parentNode; n; n = n.parentNode) if (n.tagName === 'FORM') return n; return null; }
  hidden() { for (let n = this; n; n = n.parentNode) if ('hidden' in n.attrs) return true; return false; }
  get innerText() { return this.hidden() ? '' : [this.text, ...this.children.map(c => c.innerText)].filter(Boolean).join(' '); }
  getBoundingClientRect() { const z = this.hidden(); return {width: z ? 0 : 100, height: z ? 0 : 20, top: 0, left: 0}; }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (matches(n, sel)) return n; return null; }
  querySelectorAll(sel) { const out = []; const walk = n => n.children.forEach(c => { if (matches(c, sel)) out.push(c); walk(c); });
    walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  click() { clicks.push(this.attrs['data-name'] || this.text || this.tagName);
    const t = this.attrs['data-toggle'];
    if (t) { const el = doc.byId[t]; if (el) { if ('hidden' in el.attrs) delete el.attrs.hidden; else el.attrs.hidden = ''; } }
    if (this.tagName === 'A' && this.attrs.href && !/^#/.test(this.attrs.href)) navigated = this.href; }
}
function build(spec, parent) {
  const [tag, attrs, text, kids] = spec; const el = new El(tag, attrs, text); el.parentNode = parent || null;
  if (el.attrs.id) doc.byId[el.attrs.id] = el;
  (kids || []).forEach(k => el.children.push(build(k, el))); return el;
}
function load(spec) {
  doc = {url: 'https://shop.test/', byId: {}}; clicks = []; navigated = '';
  const root = build(['html', {}, '', [['body', {}, '', spec]]], null);
  const document = {querySelectorAll: s => root.querySelectorAll(s), getElementById: i => doc.byId[i] || null,
                    get body() { return root.children[0]; }, get activeElement() { return root.children[0]; }};
  const sandbox = {document: document, URL: URL, JSON: JSON,
    location: {href: doc.url, host: 'shop.test'},
    getComputedStyle: el => ({visibility: 'visible', display: el.hidden() ? 'none' : 'block'})};
  return vm.createContext(sandbox);
}
const lines = require('fs').readFileSync(0, 'utf8').split('\n').filter(Boolean);
const out = [];
for (const line of lines) {
  const q = JSON.parse(line); const ctx = load(q.dom); const res = {};
  res.inputs = vm.runInContext(q.inputs, ctx);
  res.reveal = [];
  for (const js of q.reveals) {
    const c2 = load(q.dom);
    const r = vm.runInContext(js, c2);
    res.reveal.push({result: r, clicks: clicks.slice(), navigated: navigated, inputs: vm.runInContext(q.inputs, c2)});
  }
  out.push(res);
}
process.stdout.write(JSON.stringify(out));
"""

#: (case, lang, dom, the input index the page answers typing at and how, the truth: the input's name, or "" for none,
#: and for a reveal case the control a person clicks)
BOX_CASES = [
    ("pt: a store finder, a postcode field and the search box with suggestions", "pt",
     [["header", {}, "", [["input", {"name": "find-a-store", "placeholder": "Find a store"}],
                          ["input", {"name": "cep"}], ["input", {"name": "termo"}]]]],
     (2, "request"), "termo", ""),
    ("nl: one unnamed box in a GET form", "nl",
     [["form", {"action": "/zoeken", "method": "get"}, "", [["input", {"name": "zoekterm"}]]]],
     (0, "form"), "zoekterm", ""),
    ("pl: live results that echo the input, next to a newsletter field", "pl",
     [["input", {"name": "email", "placeholder": "e-mail"}], ["input", {"name": "fraza"}]],
     (1, "text"), "fraza", ""),
    ("en: a type=search box (old case)", "en",
     [["input", {"name": "email"}], ["input", {"name": "q", "type": "search"}]], (None, ""), "q", ""),
    ("en: one box named query (old case)", "en", [["input", {"name": "query"}]], (None, ""), "query", ""),
    ("decoy: a research field before the real box", "en",
     [["input", {"name": "research_topic"}], ["input", {"name": "q"}]], (None, ""), "q", ""),
    ("decoy: only a newsletter field", "en", [["input", {"name": "nieuwsbrief"}]], (None, ""), "", ""),
    ("de: an icon-only toggle with aria-controls, and a 'Besuchen' link", "de",
     [["header", {}, "", [["a", {"href": "/filialen", "data-name": "Besuchen"}, "Besuchen"],
                          ["button", {"aria-controls": "suchfeld", "data-toggle": "suchfeld", "data-name": "icon"},
                           "", [["svg", {}]]],
                          ["div", {"id": "suchfeld", "hidden": ""}, "", [["input", {"type": "search", "name": "q"}]]]]]],
     (None, ""), "q", "icon"),
    ("nl: a cart icon first, then the search icon (no words)", "nl",
     [["header", {}, "", [["button", {"class": "btn-icon", "data-name": "cart"}, "", [["svg", {}]]],
                          ["button", {"class": "btn-icon", "data-toggle": "zoekpaneel", "data-name": "loep"}, "",
                           [["svg", {}]]],
                          ["div", {"id": "zoekpaneel", "hidden": ""}, "", [["input", {"type": "search",
                                                                                      "name": "zoek"}]]]]]],
     (None, ""), "zoek", "loep"),
    ("nl: a button named Zoeken (old case)", "nl",
     [["button", {"data-toggle": "zp", "data-name": "Zoeken"}, "Zoeken"],
      ["div", {"id": "zp", "hidden": ""}, "", [["input", {"type": "search", "name": "q"}]]]],
     (None, ""), "q", "Zoeken"),
    ("en: three icon-only header buttons, then a 'Search' button (old case)", "en",
     [["header", {}, "", [["button", {"class": "i", "data-name": "menu"}, "", [["svg", {}]]],
                          ["button", {"class": "i", "data-name": "account"}, "", [["svg", {}]]],
                          ["button", {"class": "i", "data-name": "cart"}, "", [["svg", {}]]],
                          ["button", {"data-toggle": "sp", "data-name": "Search"}, "Search"],
                          ["div", {"id": "sp", "hidden": ""}, "", [["input", {"type": "search", "name": "q"}]]]]]],
     (None, ""), "q", "Search"),
    ("de: an icon-only account link in the header, then a 'Suche' link (old case)", "de",
     [["header", {}, "", [["a", {"href": "/konto", "data-name": "konto"}, "", [["svg", {}]]],
                          ["a", {"href": "/suche", "data-toggle": "sp", "data-name": "Suche"}, "Suche"],
                          ["div", {"id": "sp", "hidden": ""}, "", [["input", {"type": "search", "name": "q"}]]]]]],
     (None, ""), "q", "Suche"),
    ("decoy: no search anywhere, two header icons", "en",
     [["header", {}, "", [["button", {"class": "i", "data-name": "cart"}, "", [["svg", {}]]],
                          ["button", {"class": "i", "data-name": "account"}, "", [["svg", {}]]]]]],
     (None, ""), "", ""),
]


class _Fake:
    """Answers the tree's guarded session from the stand-in's results for one fixture."""

    def __init__(self, res: Dict[str, Any], answers: Tuple[Optional[int], str], new_scripts: bool):
        self.res, self.answer, self.new = res, answers, new_scripts
        self.rows = res["inputs"]
        self.focused, self.shows, self.typed, self.clicked = None, False, [], []

    def evaluate(self, e: str, session) -> Any:
        if "__anyuseSettle" in e:
            return {"quiet_ms": 900, "mutations": 1, "ready": "complete"}
        if "__anyuseRevealSearch" in e:
            m = re.search(r"\}\)\((\d+)\)\s*$", e)
            k = int(m.group(1)) if m else 0
            if k >= len(self.res["reveal"]):
                return {"clicked": False}
            got = self.res["reveal"][k]
            if (got.get("result") or {}).get("clicked"):
                self.clicked.append(got["clicks"][-1] if got["clicks"] else "?")
                self.rows = got["inputs"]
            return got.get("result")
        if "querySelectorAll('input').forEach" in e:
            return [dict(r) for r in self.rows]
        if "el.focus(); el.value = ''" in e:
            m = re.search(r"querySelectorAll\('input'\)\[(\d+)\]", e)
            self.focused = int(m.group(1)) if m else None
            self.shows = False
            return True
        if "innerText) || '').toLowerCase().includes" in e:
            return self.shows
        if "__anyuseFormGetUrl" in e:
            if self.answer[1] == "form" and self.focused == self.answer[0]:
                return {"method": "get", "url": "https://shop.test/zoeken?zoekterm=fiets"}
            return None
        if "String(el.value" in e:
            return self.typed[-1] if self.typed else ""
        if "__anyuseSubmitSearch" in e:
            return {"clicked": False, "how": "no submit control"}
        if "location.href" in e:
            return "https://shop.test/"
        return None

    def typed_into(self, text: str, session) -> None:
        self.typed.append(text)
        if self.focused is not None and self.focused == self.answer[0]:
            if self.answer[1] == "request":
                session.budget.sent.append(("shop.test", 0.0, f"https://shop.test/api/s?t={urllib.parse.quote(text)}",
                                            "XHR"))
            elif self.answer[1] == "text":
                self.shows = True


class _WS:
    def __init__(self, fake: _Fake, session):
        self.fake, self.session, self.inbox = fake, session, []

    async def send(self, raw):
        msg = json.loads(raw)
        m, p = msg.get("method"), msg.get("params") or {}
        if m in ("Fetch.continueRequest", "Fetch.failRequest"):
            return
        val = None
        if m == "Runtime.evaluate":
            val = self.fake.evaluate(p["expression"], self.session)
        elif m == "Input.insertText":
            self.fake.typed_into(p["text"], self.session)
        self.inbox.append({"id": msg["id"], "result": {"result": {"value": val}}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))


def box_rows(node: str) -> List[Dict[str, Any]]:
    from anyuse.execution import cdp_guard as G
    from anyuse.execution import discover as D
    new_scripts = "%d" in G.REVEAL_SEARCH_JS
    reveals = [G.REVEAL_SEARCH_JS % k for k in range(3)] if new_scripts else [G.REVEAL_SEARCH_JS]
    reqs = [json.dumps({"dom": dom, "inputs": G.SEARCH_INPUTS_JS, "reveals": reveals}) for _c, _l, dom, *_r in BOX_CASES]
    with tempfile.TemporaryDirectory(prefix="anyuse-box-") as d:
        js = os.path.join(d, "dom.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(DOM_JS)
        proc = subprocess.run([node, js], input="\n".join(reqs), capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise SystemExit(f"the DOM stand-in failed: {proc.stderr[:600]}")
    results = json.loads(proc.stdout)
    out = []
    for (case, lang, dom, answers, want, want_click), res in zip(BOX_CASES, results):
        fake = _Fake(res, answers, new_scripts)
        s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(D.Pacer(0.0)))
        s._ws = _WS(fake, s)
        got = asyncio.run(s.type_search("fiets"))
        names = {r["index"]: r.get("name", "") for r in fake.rows}
        chosen = names.get((got.get("input") or {}).get("index"), "") if got.get("input") else ""
        out.append(row("search_box", case, want, chosen,
                       f"chosen by {got.get('chosen_by', 'the first search-like input')}; typed {len(fake.typed)}x",
                       lang))
        if want_click or "icon" in case or "decoy: no search" in case:
            out.append(row("search_box", case + " [control clicked]", want_click,
                           fake.clicked[-1] if fake.clicked and chosen else "",
                           f"clicked {fake.clicked}", lang))
    return out


#: (case, dom): more pages whose text inputs the offline page must read as the live script does
PARITY_PAGES = [
    ("structure: type=search, role=searchbox, the two landmarks, the names q and s",
     [["input", {"type": "search", "name": "a"}], ["input", {"role": "searchbox", "name": "b"}],
      ["div", {"role": "search"}, "", [["input", {"name": "c"}]]], ["search", {}, "", [["input", {"name": "d"}]]],
      ["input", {"role": "search", "name": "e"}], ["input", {"name": "q"}], ["input", {"name": "s"}]]),
    ("names: whole words and word starts, and a search word inside a compound",
     [["input", {"name": "plz", "placeholder": "Search products"}], ["input", {"id": "txtSearch"}],
      ["input", {"name": "searchTerm"}], ["input", {"class": "sitesearch-field"}], ["input", {"name": "research_topic"}],
      ["input", {"name": "KEYWORDS"}], ["input", {"name": "my-query"}], ["input", {"aria-label": "findStore"}],
      ["input", {"name": "Suchbegriff"}], ["input", {"name": "HTMLSearchField"}]]),
    ("what is no text input or not shown: a sign-in form, other types, hidden and disabled fields",
     [["form", {}, "", [["input", {"name": "user"}], ["input", {"type": "password", "name": "pw"}]]],
      ["input", {"type": "hidden", "name": "token"}], ["input", {"type": "checkbox", "name": "search"}],
      ["input", {"name": "q", "hidden": ""}], ["div", {"hidden": ""}, "", [["input", {"type": "search", "name": "q2"}]]],
      ["input", {"name": "q3", "disabled": ""}], ["input", {"name": "termo"}], ["input", {"class": "searchbox"}]]),
]


def _markup(spec) -> str:
    import html as _html
    out = []
    for tag, attrs, *rest in spec:
        text, kids = (list(rest) + ["", []])[:2]
        a = "".join(f' {k}="{_html.escape(str(v))}"' if v != "" else f" {k}" for k, v in attrs.items())
        out.append(f"<{tag}{a}>" if tag == "input" else f"<{tag}{a}>{_html.escape(text)}{_markup(kids)}</{tag}>")
    return "".join(out)


def parity_rows(node: str) -> List[Dict[str, Any]]:
    """The offline page (``dom_page``) against the live script on the same page: its search-like inputs; for every
    visible input as a tool recorded it (its name, or its index alone; a version-15 tool and one before), the input
    the live replay types into against the offline one; and whether the probe canary on the live session finds what
    the live replay types into."""
    import inspect
    from anyuse.execution import cdp_guard as G
    from anyuse.execution import demo_cards as K
    from anyuse.execution import discover as Dsc
    from anyuse.execution import dom_page as DP
    from anyuse.execution import repair_canary as RC
    pages = [(c, dom) for c, _l, dom, *_r in BOX_CASES] + PARITY_PAGES
    reqs = [json.dumps({"dom": dom, "inputs": G.SEARCH_INPUTS_JS, "reveals": []}) for _c, dom in pages]
    with tempfile.TemporaryDirectory(prefix="anyuse-parity-") as d:
        js = os.path.join(d, "dom.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(DOM_JS)
        proc = subprocess.run([node, js], input="\n".join(reqs), capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise SystemExit(f"the DOM stand-in failed: {proc.stderr[:600]}")
    frozen_kw = "named14" in inspect.signature(G.GuardedSession.type_search).parameters
    new_scripts = "%d" in G.REVEAL_SEARCH_JS
    out = []

    def live_session(res):
        s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(Dsc.Pacer(0.0)))
        s._ws = _WS(_Fake(res, (None, ""), new_scripts), s)
        return s
    for (case, dom), res in zip(pages, json.loads(proc.stdout)):
        rows = res["inputs"]
        root, _b = K.parse(f"<html><body>{_markup(dom)}</body></html>")
        live = sorted(r["index"] for r in rows if r.get("rank", 2) < 3)
        offline = sorted(r["index"] for r in DP.search_inputs_of(root))
        out.append(row("input_parity", f"{case} [the search-like inputs]", live, offline, f"live {live}"))
        site = DP.DomSite([DP.Recorded("https://shop.test/", f"<html><body>{_markup(dom)}</body></html>")])
        for r in rows:
            if not r.get("visible"):
                continue
            wants = ([{"name": r["name"], "index": r["index"]}] if r.get("name") else []) + [{"index": r["index"]}]
            for want in wants:
                for frozen in (False, True):
                    kw = {"named14": True} if frozen and frozen_kw else {}
                    got = asyncio.run(live_session(res).type_search("fiets", want=dict(want), **kw))
                    live_pick = (got.get("input") or {}).get("index")
                    page = DP.DomPage(site)
                    asyncio.run(page.goto("https://shop.test/"))
                    off = asyncio.run(page.type_search("fiets", want=dict(want), named14=frozen))
                    off_pick = (off.get("input") or {}).get("index")
                    label = f"{case} [{'v14' if frozen else 'v15'} tool recorded {json.dumps(want)}]"
                    out.append(row("input_parity", label, live_pick, off_pick, f"live {live_pick}, offline {off_pick}"))
                    step = {"step": "search", "kind": "type", "identity": dict(want), "named14": frozen}
                    present = asyncio.run(RC.control_present(live_session(res), step))[0]
                    out.append(row("input_parity", label + " [canary]", live_pick is not None, present,
                                   f"the live replay types into {live_pick}"))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# arms, report, driver
# ---------------------------------------------------------------------------------------------------------------------

def run_arm(label: str, tree: str, out_path: str, node: str) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    t0 = time.monotonic()
    rows = (robots_rows() + own_rows() + search_post_rows() + post_rule_rows() + asset_rows() + learn_rows()
            + discover_rows() + box_rows(node) + parity_rows(node))
    meta = {"label": label, "tree": tree, "seconds": round(time.monotonic() - t0, 2)}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, ensure_ascii=False, indent=1)


def compare(paths: Sequence[str]) -> str:
    arms: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        arms[d["meta"]["label"]] = d
    old = {(r["group"], r["case"]): r for r in arms.get("OLD", {}).get("rows", [])}
    new = {(r["group"], r["case"]): r for r in arms.get("NEW", {}).get("rows", [])}
    keys = list(new) or list(old)
    lines = ["right answers per group (decoys: wanted refusals or 'none' answers kept)", ""]
    lines.append(f"{'group':12} {'cases':>5} {'OLD':>5} {'NEW':>5} {'decoys OLD':>11} {'decoys NEW':>11}")
    ok_all = True
    for g in GROUPS:
        ks = [k for k in keys if k[0] == g]
        if not ks:
            continue
        o = sum(1 for k in ks if k in old and old[k]["got"] == old[k]["want"])
        n = sum(1 for k in ks if new[k]["got"] == new[k]["want"])
        dk = [k for k in ks if "decoy" in k[1]]
        do = sum(1 for k in dk if k in old and old[k]["got"] == old[k]["want"])
        dn = sum(1 for k in dk if new[k]["got"] == new[k]["want"])
        lines.append(f"{g:12} {len(ks):>5} {o:>5} {n:>5} {f'{do}/{len(dk)}':>11} {f'{dn}/{len(dk)}':>11}")
    lines.append("")
    fixed = [k for k in keys if k in old and old[k]["got"] != old[k]["want"] and new[k]["got"] == new[k]["want"]]
    lost = [k for k in keys if k in old and old[k]["got"] == old[k]["want"] and new[k]["got"] != new[k]["want"]]
    both = [k for k in keys if new[k]["got"] != new[k]["want"] and (k not in old or old[k]["got"] != old[k]["want"])]
    lines.append(f"fixed by NEW ({len(fixed)}):")
    lines += [f"  {g}: {c}  [OLD {old[(g, c)]['got']!r} -> NEW {new[(g, c)]['got']!r}]" for g, c in fixed]
    lines.append(f"right in OLD, wrong in NEW ({len(lost)}):")
    for g, c in lost:
        cost = KNOWN_COSTS.get((g, c))
        lines.append(f"  {g}: {c}  [{new[(g, c)]['why'][:100]}]" + (f"  KNOWN COST: {cost}" if cost else ""))
        ok_all = ok_all and bool(cost)
    lines.append(f"wrong in both ({len(both)}):")
    for g, c in both:
        # a known cost the OLD tree already carries is wrong in both arms
        cost = KNOWN_COSTS.get((g, c))
        lines.append(f"  {g}: {c}  [{new[(g, c)]['why'][:100]}]" + (f"  KNOWN COST: {cost}" if cost else ""))
    langs = sorted({new[k]["lang"] for k in fixed if new[k]["lang"]})
    lines.append(f"languages fixed: {', '.join(langs)}")
    lines.append("seconds: " + ", ".join(f"{lab} {arms[lab]['meta']['seconds']}" for lab in arms))
    lines.append("PASS" if ok_all and not [k for k in both if k not in KNOWN_COSTS] else "FAIL")
    return "\n".join(lines)


def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-generalize-scope-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    node = args.node or shutil.which("node") or os.path.expanduser("~/.local/node/bin/node")
    if not os.path.exists(node):
        raise SystemExit("a node binary is needed for the search-box cases (--node)")
    me = os.path.join(out, "generalize_scope_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    results = []
    for label, tree in trees.items():
        home = os.path.join(out, f"home_{label}")
        os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, tree, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", "api_hosts.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", "blocked.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", "robots.json"),
            "ANYUSE_MODEL_CHECKS": "none",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, "-B", me, "arm", "--label", label, "--tree", tree,
                               "--out-file", path, "--node", node], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--node", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    a.add_argument("--node", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.tree), os.path.abspath(args.out_file), args.node)
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
