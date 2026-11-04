"""Offline A/B bench: do the compile-side judgements read the site's own evidence first?

Each case is a page, a set of recorded sessions, a link list or an executor result,
with the answer a person would give. Three kinds:

* ``gap``: a case a hand-written list misses: a page in a language outside the
  phrase lists (Dutch, Portuguese, Polish, Turkish, Czech, Swedish, Vietnamese,
  Thai, Indonesian, Swahili), class names that are not English, a payload under
  a name no framework list holds, a search vendor no catalogue names;
* ``kept``: a case the lists already decided right (no regression);
* ``decoy``: a case that must stay refused (an error page, a JavaScript literal, a
  furniture box that only mentions the input, an id in a link's address).

The judgements: the empty-answer cascade's error lines (``result_contract``), an
attach session's list or item page (``attach.session_shape``), the result list and
region with page furniture (``html_extract``), embedded JSON payloads
(``embedded_json``), link identity (``url_identity``, ``discover.link_clusters``),
whether the site answered (``api_recompile.site_did_not_answer``), the discovery
replay's empty answer (``discover_replay``), the compile diagnostics' substring
tests (``api_compile``), the heal's made-up search (``fresh_truth.query_bound``)
and the agent answer's item names (``compiled_tools._item_name``). Where a check
model is part of the case it is a fake given to ``model_check.using``; nothing
opens a socket, starts a browser or calls a model.

Usage::

    python tests/bench/generalize_compile_bench.py run --old <tree> --new <tree> \\
        [--python <interpreter>] [--out <dir>]
    python tests/bench/generalize_compile_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the branch forked from, ``--new`` the branch.
The script copies itself into ``--out`` (outside both trees) and runs that copy once
per tree, with a pkgroot holding an ``anyuse`` symlink to the tree and the tree's
``tools/guarded_tests`` on PYTHONPATH (its sitecustomize refuses sockets). HOME and
every AnyUse data path point into ``--out``, and no ``ANYUSE_*`` variable is passed
through.

Pass: NEW decides every case right, and every case OLD decided right NEW decides
right too.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Tuple

ARMS = ("OLD", "NEW")
Case = Tuple[str, str, str, Any, Callable[[], Any]]


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

def _fake(answer):
    """A fake check model: ``answer(kind, line) -> P(yes)``; ``kind`` is ``error``
    for the question whether a line reports a failure, else ``empty``."""
    def ask(instructions, state, obs):
        kind = "error" if "failed to answer" in instructions else "empty"
        return answer(kind, state.rsplit(">> ", 1)[-1])
    return ask


#: statements of "no results" in languages no phrase list reads
STATEMENTS = {
    "sw": "Hakuna matokeo ya “{q}”.",
    "id": "Tidak ada hasil untuk “{q}”.",
    "vi": "Không tìm thấy kết quả nào cho “{q}”.",
    "th": "ไม่พบผลลัพธ์สำหรับ “{q}”",
    "tr": "“{q}” için sonuç bulunamadı.",
    "nl": "Geen resultaten voor “{q}”.",
}


def empty_cases() -> List[Case]:
    from anyuse.execution import empty_page as EP
    from anyuse.execution import model_check as MC
    from anyuse.execution import result_contract as RC

    def verdict(page, fake=None, sig=None):
        contract = RC.replay_contract("q")
        if sig is not None:
            contract["empty"] = {**contract["empty"], "page": sig}
        if fake is None:
            v = RC.check_html(contract, page, "ul.r li", {"q": "zzqx"}, [])
        else:
            with MC.scope("product"), MC.using(fake):
                v = RC.check_html(contract, page, "ul.r li", {"q": "zzqx"}, [],
                                  empty=EP.Context(probe=False, learn=False))
        return "empty" if v.ok and v.kind == "empty" else "refused"

    def page(statement, extra="", footer=""):
        return (f"<html><body><main><ul class='r'></ul><p>{statement.format(q='zzqx')}</p>"
                f"{extra}</main><footer>{footer}</footer></body></html>")

    def learned_footer(footer):
        def result_page(q):
            rows = "".join(f"<li class='r'><a href='/p/{i}'>{q} {i}</a></li>" for i in range(4))
            return (f"<html><body><main><ul class='r'>{rows}</ul></main>"
                    f"<footer>{footer}</footer></body></html>")

        def made_up(q):
            return (f"<html><body><main><p>Neno “{q}” halipo.</p></main>"
                    f"<footer>{footer}</footer></body></html>")
        return EP.learn("html", [EP.Page("html", q, markup=result_page(q), items=["x"])
                                 for q in ("ndizi", "embe")],
                        [EP.Page("html", q, markup=made_up(q)) for q in ("vorquazib", "limtrax")])

    yes_empty = _fake(lambda kind, line: 0.05 if kind == "error" else
                      (0.95 if "zzqx" in line else 0.05))
    out: List[Case] = []
    footer = "<p>Order desk: something went wrong with an order, we fix it.</p>"
    for lang, st in STATEMENTS.items():
        # the site's chrome (a footer its result pages show too) holds an error phrase
        out.append((f"{lang}: error phrase in the site's footer, model reads the statement",
                    "empty: error lines", "gap", "empty",
                    lambda st=st: verdict(page(st, footer=footer), yes_empty,
                                          sig=learned_footer(footer))))
    for help_line in ("Please try again later.", "Something went wrong? Contact us.",
                      "If something went wrong, write to us."):
        out.append((f"en: statement beside help text {help_line!r}", "empty: error lines", "gap",
                    "empty", lambda h=help_line: verdict(page("No results found for “{q}”.",
                                                              f"<p>{h}</p>"))))
    # an error-looking line the check model reads as no error (a shop's order notice)
    out.append(("sw: a line the model clears as no error", "empty: error lines", "gap", "empty",
                lambda: verdict(page(STATEMENTS["sw"], "<p>Service temporarily unavailable "
                                                       "for orders over 30 kg</p>"), yes_empty)))
    out.append(("en: plain statement", "empty: error lines", "kept", "empty",
                lambda: verdict(page("No results found for “{q}”."))))
    out.append(("sw: model reads the statement", "empty: error lines", "kept", "empty",
                lambda: verdict(page(STATEMENTS["sw"]), yes_empty)))
    for err in ("Something went wrong.", "Service temporarily unavailable",
                "Internal Server Error", "Too many requests. Please try again later.",
                "Произошла ошибка", "エラーが発生しました"):
        out.append((f"error {err!r} beside a statement", "empty: error lines", "decoy",
                    "refused", lambda e=err: verdict(page("No results found for “{q}”.",
                                                          f"<p>{e}</p>"))))
    out.append(("error line the model reads as an error", "empty: error lines", "decoy",
                "refused", lambda: verdict(page(STATEMENTS["sw"], "<p>Something went wrong.</p>"),
                                           _fake(lambda kind, line: 0.95))))
    out.append(("error line the model clears, no answer about the statement",
                "empty: error lines", "decoy", "refused",
                lambda: verdict(page("No results found for “{q}”.", "<p>Something went wrong.</p>"),
                                _fake(lambda kind, line: 0.05 if kind == "error" else 0.5))))
    out.append(("error line the model gives no answer about", "empty: error lines", "decoy",
                "refused", lambda: verdict(page(STATEMENTS["sw"], "<p>Something went wrong.</p>"),
                                           _fake(lambda kind, line: 0.95 if kind == "empty"
                                                 else 0.5))))
    return out


def shape_cases() -> List[Case]:
    from anyuse.execution import attach as A

    def search(word, v, echo=True, home=""):
        rows = "".join(f"<li class='hit'><a href='/product/{v}-{i}'>{v} model {i}</a></li>"
                       for i in range(3))
        field = f"<input type='search' name='q' value='{v}'>" if echo else ""
        return {"snapshots": [
            {"url": "https://shop.test/", "dom": f"<html><body>{home}</body></html>"},
            {"url": f"https://shop.test/{word}/{v}",
             "dom": f"<html><body><form>{field}</form><ul>{rows}</ul></body></html>", "text": v}]}

    def item(word, v, vals):
        directory = ("<html><body><ul>" + "".join(
            f"<li class='m'><a href='/{word}/{x}'>{x.title()}</a></li>" for x in vals) +
            "</ul></body></html>")
        return {"snapshots": [
            {"url": "https://rocks.test/all", "dom": directory},
            {"url": f"https://rocks.test/{word}/{v}",
             "dom": f"<html><body><h1>{v.title()}</h1><p>A record.</p></body></html>", "text": v}]}

    vals = ["fiets", "lamp", "stoel"]
    out: List[Case] = []
    for lang, word, echo in (("nl", "zoeken", True), ("pt", "busca", False), ("pl", "szukaj", True),
                             ("tr", "arama", False), ("cs", "hledat", True), ("sv", "sok", False),
                             ("id", "cari", True), ("vi", "tim-kiem", False)):
        out.append((f"{lang}: path search /{word}/<input>", "attach: list or item", "gap", "list",
                    lambda w=word, e=echo: A.session_shape([search(w, v, e) for v in vals], vals)))
    rocks = ["quartz", "pyrite", "galena"]
    for word in ("find", "s", "results"):
        out.append((f"item pages under /{word}/ listed by a directory", "attach: list or item",
                    "gap", "detail",
                    lambda w=word: A.session_shape([item(w, v, rocks) for v in rocks], rocks)))
    out.append(("item pages under /package/ listed by a directory", "attach: list or item",
                "kept", "detail",
                lambda: A.session_shape([item("package", v, rocks) for v in rocks], rocks)))
    out.append(("bare /search/<input> pages", "attach: list or item", "kept", "list",
                lambda: A.session_shape([{"snapshots": [{"url": f"https://x.test/search/{v}",
                                                         "dom": "<ul></ul>", "text": v}]}
                                         for v in vals], vals)))
    out.append(("bare /package/<input> pages", "attach: list or item", "kept", "detail",
                lambda: A.session_shape([{"snapshots": [{"url": f"https://x.test/package/{v}",
                                                         "dom": "<h1>x</h1>", "text": v}]}
                                         for v in vals], vals)))
    def own_links(v):
        links = ("<ul class='links'>"
                 f"<li><a href='https://code.test/{v}/{v}'>{v} source code</a></li>"
                 f"<li><a href='https://docs.test/{v}/'>{v} documentation</a></li>"
                 f"<li><a href='/package/{v}/versions'>All {v} versions</a></li>"
                 f"<li><a href='/package/{v}/issues'>Open {v} issues</a></li></ul>")
        return {"snapshots": [{"url": f"https://pkgs.test/package/{v}",
                               "dom": f"<html><body><h1>{v}</h1>{links}</body></html>",
                               "text": v}]}
    out.append(("item pages whose own links name the input (code, docs, versions, issues)",
                "attach: list or item", "kept", "detail",
                lambda: A.session_shape([own_links(v) for v in vals], vals)))
    popular = "<ul>" + "".join(f"<li class='pop'>{v}</li>" for v in vals) + "</ul>"
    out.append(("/search/ with the inputs as popular searches on the home page",
                "attach: list or item", "decoy", "list",
                lambda: A.session_shape([search("search", v, home=popular) for v in vals], vals)))
    return out


def region_cases() -> List[Case]:
    from anyuse.execution import html_extract as X

    def list_page(q, rows_cls, side_cls, pop):
        hits = "".join(f'<li class="hit"><a>{q} {n}</a> <span>{q} {n} details</span></li>'
                       for n in ("one", "two", "three"))
        box = "".join(f'<li class="pop"><a>{p}</a></li>' for p in pop)
        return (f'<html><body><div class="{rows_cls}"><ul class="hits">{hits}</ul></div>'
                f'<div class="{side_cls}"><ul class="popular">{box}</ul>'
                f'<p>{"static shop text " * 30}</p></div></body></html>')

    def lists(rows_cls, side_cls, values=True):
        a = list_page("lampara", rows_cls, side_cls, ["mesa", "silla", "sofa"])
        b = list_page("cortina", rows_cls, side_cls, ["cama", "espejo", "alfombra"])
        sel, _items = X.derive_list_items([a, b], values=["lampara", "cortina"] if values else None)
        return "hits" if "li.hit" in sel else ("popular" if "li.pop" in sel else sel or "none")

    def region(q, where, crumb_cls="", rows=None):
        recs = "".join(f"<div><a>{q} {r}</a><span>{len(r) * 3} stars</span></div>"
                       for r in (rows or ["one", "two", "three", "four"]))
        crumb = f'<div class="{crumb_cls}">Start › Suche › {q}</div>' if crumb_cls else ""
        return (f'<html><body><div id="kopf"><p>{"static " * 60}</p></div>'
                f'<{where} id="inhalt">{crumb}<div class="treffer">{recs}</div></{where}>'
                f'</body></html>')

    def regions(where, crumb_cls="", second_rows=None):
        pages = [("alfa", region("alfa", where, crumb_cls)),
                 ("beta", region("beta", where, crumb_cls, second_rows or ["fem", "sex", "sju", "atta"]))]
        sel, items, _how = X.derive_result_region(pages)
        return f"{len(items)} records" if sel else "none"

    def related(q):
        rows = "<li class='hit'><a href='/p/top'>Aanbieding van de week</a></li>" + "".join(
            f"<li class='hit'><a href='/p/{q}-{i}'>{q} model {i}</a></li>" for i in range(2))
        rel = "".join(f"<li class='rel'><a href='/s/{q}+{w}'>{q} {w}</a></li>"
                      for w in ("goedkoop", "tweedehands", "nieuw", "kinder", "elektrisch"))
        return (f"<html><body><main><ul class='results'>{rows}</ul></main>"
                f"<aside><ul class='related'>{rel}</ul></aside></body></html>")

    def related_lists():
        sel, _items = X.derive_list_items([related("fiets"), related("lamp")],
                                          values=["fiets", "lamp"])
        return "hits" if "li.hit" in sel else ("related" if "li.rel" in sel else sel or "none")

    def crumb_page(q, n):
        crumb = f'<nav class="breadcrumb"><a>Home</a> / <a>Zoeken</a> / {q}</nav>'
        body = "".join(f'<div class="hit"><a>{q} item {i}</a><span>{q} {i} tekst</span></div>'
                       if i % 2 else f'<section><b>{q} item {i}</b><i>referentie</i></section>'
                       for i in range(n))
        return (f'<html><body><main><div id="results">{crumb}{body}</div>'
                f'<p>{"static site text " * 40}</p></main></body></html>')

    def crumb_region():
        sel, items, _how = X.derive_result_region([("alfa", crumb_page("alfa", 1)),
                                                   ("beta", crumb_page("beta", 3))])
        return f"{len(items)} records" if sel else "none"

    def recent(q):
        return (f'<html><body><main><p>{"static " * 50}</p></main><aside id="recent">'
                f'<div><a>{q}</a></div><p>older search one</p><span>older search two</span>'
                f'</aside></body></html>')
    out: List[Case] = [
        ("es: result rows under a 'sidebar' class, a popular box outside", "html: furniture", "gap",
         "hits", lambda: lists("sidebar", "col")),
        ("sv: the only input-bound region sits in an <aside>", "html: furniture", "gap",
         "4 records", lambda: regions("aside")),
        ("de: a breadcrumb under a class no list knows", "html: furniture", "gap",
         "4 records", lambda: regions("div", crumb_cls="brotkrumen")),
        ("es: result rows outside furniture, a popular box in a 'sidebar'", "html: furniture",
         "kept", "hits", lambda: lists("results", "sidebar")),
        ("es: without the inputs, furniture breaks the tie", "html: furniture", "kept",
         "popular", lambda: lists("sidebar", "col", values=False)),
        ("de: a breadcrumb under the 'breadcrumb' class", "html: furniture", "kept",
         "4 records", lambda: regions("div", crumb_cls="breadcrumb")),
        ("nl: a related-searches box in an <aside> beside a partly pinned result list",
         "html: furniture", "kept", "hits", related_lists),
        ("nl: a breadcrumb <nav> inside the result container", "html: furniture", "kept",
         "1 records", crumb_region),
        ("a recent-searches box in an <aside> naming the input once", "html: furniture", "decoy",
         "none", lambda: (lambda sel: sel or "none")(X.derive_result_region(
             [("alfa", recent("alfa")), ("beta", recent("beta"))])[0])),
    ]
    return out


def embedded_cases() -> List[Case]:
    from anyuse.execution.api_compile import compile_embedded_json, visible_items
    from anyuse.execution.embedded_json import find_embedded

    items = ["Fiets Alpha", "Fiets Beta", "Fiets Gamma"]
    hits = {"zoek": {"resultaten": [{"naam": t} for t in items]}}

    def compiled(script):
        body = (f"<!DOCTYPE html><html><body><ul>{''.join(f'<li>{t}</li>' for t in items)}</ul>"
                f"<script>{script}</script></body></html>")
        rec = {"param": {"name": "q", "value": "Fiets"}, "ui": {"items": items, "text": ""},
               "network": [{"url": "https://fiets.test/zoeken?q=Fiets", "method": "GET",
                            "status": 200, "mime": "text/html", "bytes": len(body), "body": body}]}
        tmpl, _why = compile_embedded_json(rec, visible_items(rec), "q", "Fiets")
        return tmpl.embedded.get("name", tmpl.embedded.get("id", "")) if tmpl else "none"

    text = json.dumps(hits)
    menu = json.dumps({"nav": [{"label": x} for x in ("Home", "Help", "About", "Shop")]})
    return [
        ("nl: window.ZOEK_STATE = {...}", "embedded JSON", "gap", "window.ZOEK_STATE",
         lambda: compiled("window.ZOEK_STATE = " + text + ";")),
        ("pt: var resultados = [...]", "embedded JSON", "gap", "resultados",
         lambda: compiled("var resultados = " + json.dumps(hits["zoek"]["resultaten"]) + ";")),
        ("pl: self.dane = JSON.parse('...')", "embedded JSON", "gap", "self.dane",
         lambda: compiled("self.dane = JSON.parse(" + json.dumps(text) + ");")),
        ("menu under a listed name, results under the page's own", "embedded JSON", "gap",
         "window.zoekResultaten",
         lambda: compiled("window.__INITIAL_STATE__ = " + menu + ";\nwindow.zoekResultaten = "
                          + text + ";")),
        ("window.__INITIAL_STATE__ = {...}", "embedded JSON", "kept", "window.__INITIAL_STATE__",
         lambda: compiled("window.__INITIAL_STATE__ = " + text + ";")),
        ("a JavaScript object literal", "embedded JSON", "decoy", 0,
         lambda: len(find_embedded("<script>window.STATE = {hits: [1, 2, 3], when: new Date()};"
                                   "</script>"))),
        ("a template literal that runs code", "embedded JSON", "decoy", 0,
         lambda: len(find_embedded("<script>x.y = JSON.parse(`{\"a\": \"${s}\", "
                                   "\"b\": 1234567890}`)</script>"))),
        ("an assignment in the page's text", "embedded JSON", "decoy", 0,
         lambda: len(find_embedded(f"<p>app.data = {text}</p>"))),
    ]


def identity_cases() -> List[Case]:
    from anyuse.execution import discover as D
    from anyuse.execution import url_identity as UI

    def clusters(hrefs, host):
        rows = "".join(f"<li><a href='{h}'>Item {i}</a></li>" for i, h in enumerate(hrefs))
        scan = D.scan_page(f"<html><body><ul>{rows}</ul></body></html>", f"https://{host}/search?q=x")
        return max((len(c.values) for c in D.link_clusters(scan.links, host, min_members=2)),
                   default=0)

    def distinct(urls):
        noise = UI.page_noise(urls)
        return len({UI.identity(u, noise) for u in urls})
    return [
        ("results told apart only by 'ito'", "link identity", "gap", 5,
         lambda: clusters([f"/show?ito={i}" for i in range(1, 6)], "tv.test")),
        ("results told apart only by '_sid'", "link identity", "gap", 3,
         lambda: distinct([f"https://shop.test/p?_sid={s}" for s in ("a1", "b2", "c3")])),
        ("shop results with per-item _pos and a session _sid", "link identity", "kept", 3,
         lambda: clusters([f"/products/{s}?_pos={i}&_sid=ab&_ss=r" for i, s in
                           enumerate(("fin", "mask", "reel"), 1)], "dive.test")),
        ("click ids on every link", "link identity", "kept", 3,
         lambda: distinct([f"https://s.test/p/{s}?gclid=x{i}&utm_source=nl" for i, s in
                           enumerate("abc")])),
        ("one product twice at two positions", "link identity", "decoy", 3,
         lambda: distinct([f"https://s.test/p/{s}?_pos={i}" for i, s in enumerate("abca", 1)])),
        ("two products, one of them twice at two positions", "link identity", "decoy", 2,
         lambda: distinct([f"https://s.test/p/{s}?_pos={i}&_sid=ab" for i, s in
                           enumerate("aba", 1)])),
    ]


def outage_cases() -> List[Case]:
    from anyuse.execution.api_recompile import site_did_not_answer as did
    quoted = ("result contract refused: the page reports an error ('Service temporarily "
              "unavailable'), not an empty result")
    return [
        ("a 200 whose refusal quotes 'temporarily unavailable'", "site answered", "gap", False,
         lambda: did(200, quoted)),
        ("a 404 whose message says 'connection'", "site answered", "gap", False,
         lambda: did(404, "non-2xx status 404 (connection: close)")),
        ("a stored message that quotes a page line with 'connection'", "site answered", "gap",
         False, lambda: did(None, 'the page says "connection to the warehouse lost" beside 0 rows')),
        ("a 503", "site answered", "kept", True, lambda: did(503, "")),
        ("a transport timeout", "site answered", "kept", True,
         lambda: did(None, "transport error: timed out")),
        ("a name that did not resolve", "site answered", "kept", True,
         lambda: did(None, "transport error: pre-send policy refused request: destination "
                           "address resolution failed closed: gaierror")),
        ("a browser rung's navigation timeout", "site answered", "kept", True,
         lambda: did(None, "Page.navigate timed out; browser answers, the page does not")),
        ("a transport message with an apostrophe before a quoted host", "site answered", "kept",
         True, lambda: did(None, "the browser couldn't load the page: connection reset by "
                                 "'shop.test'")),
        ("a refused private address", "site answered", "decoy", False,
         lambda: did(None, "transport error: pre-send policy refused request: private address")),
    ]


def replay_cases() -> List[Case]:
    from anyuse.execution import discover_replay as R
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    from anyuse.execution.agent_browser import AgentRun
    from anyuse.execution.cdp_replay import ReplayTemplate

    def empty(q):
        return (f"<html><body><main><ul class='hits'></ul><p>Pro „{q}“ jsme nic nenašli.</p>"
                f"</main><footer>Kontakt</footer></body></html>")

    def full(q):
        rows = "".join(f"<li class='hit'><a href='/i/{i}'>{q} {i}</a></li>" for i in range(4))
        return f"<html><body><main><ul class='hits'>{rows}</ul></main><footer>Kontakt</footer></body></html>"

    def run(dom, pinned=True, q="zzqx"):
        expect = {}
        if pinned:
            sig = EP.learn("html", [EP.Page("html", v, markup=full(v), items=["x"])
                                    for v in ("alder", "birch")],
                           [EP.Page("html", v, markup=empty(v)) for v in ("zorquavex", "limtrax")])
            contract = RC.replay_contract("q")
            contract["empty"] = {**contract["empty"], "page": sig}
            expect = {RC.CONTRACT_KEY: contract}
        t = ReplayTemplate(url="https://cz.test/s?q={q}", params=["q"],
                           extract={"items": "li.hit"}, expect=expect)
        out = R.GuardedReplay(lambda url, v: AgentRun(value=v, start_url=url, final_url=url,
                                                      dom=dom)).execute(t, {"q": q})
        return "empty" if out.get("success") else "refused"
    rows = "".join(f"<div class='card'><a href='/i/{i}'>kafka {i}</a></div>" for i in range(3))
    return [
        ("cs: the pinned site empty page", "discovery replay", "gap", "empty",
         lambda: run(empty("zzqx"))),
        ("en: a statement beside result rows the selector no longer reads", "discovery replay",
         "gap", "refused",
         lambda: run(f"<html><body><main>{rows}<p>No results found for kafka.</p></main>"
                     f"</body></html>", pinned=False, q="kafka")),
        ("en: a plain statement", "discovery replay", "kept", "empty",
         lambda: run("<html><body><ul></ul><p>No results found for zzqx</p></body></html>",
                     pinned=False)),
        ("cs: the pinned empty page beside unread rows", "discovery replay", "decoy", "refused",
         lambda: run(empty("zzqx").replace("<ul class='hits'></ul>", "".join(
             f"<div class='row'><a href='/i/{i}'>zzqx {i}</a></div>" for i in range(3))))),
    ]


def diagnostics_cases() -> List[Case]:
    import urllib.parse
    from anyuse.execution.api_compile import _labels_in_html, _searched_after, diagnose_failure

    def post(keys):
        url = "https://search.provider.test/1/q?" + urllib.parse.urlencode([(k, "v") for k in keys])
        rec = {"param": {"name": "q", "value": "kafka"}, "ui": {"items": ["kafka"], "text": ""},
               "network": [{"url": url, "method": "POST", "status": 200,
                            "mime": "application/json", "body": "{}"}]}
        return "API key" in diagnose_failure(rec)

    def html_holds(body, labels=("Art", "Ink")):
        return _labels_in_html({"ui": {"items": list(labels), "text": ""},
                                "network": [{"url": "https://x.test/s", "mime": "text/html",
                                             "body": body}]})

    def searched(post_data, value="art"):
        return _searched_after({"network": [{"url": "https://x.test/"},
                                            {"url": "https://x.test/api", "post_data": post_data}]},
                               0, value)
    return [
        ("a vendor key in a credential-shaped name", "compile diagnostics", "gap", True,
         lambda: post(["x-search-api-key", "app"])),
        ("labels only inside longer words", "compile diagnostics", "gap", False,
         lambda: html_holds("<p>Start the pinkest party</p>")),
        ("a later body with the value inside a longer word", "compile diagnostics", "gap", False,
         lambda: searched("action=start&page=1")),
        ("a vendor's api key", "compile diagnostics", "kept", True,
         lambda: post(["x-algolia-api-key"])),
        ("labels as whole words", "compile diagnostics", "kept", True,
         lambda: html_holds("<ul><li>Art</li><li>Ink</li></ul>")),
        ("a later body with the value", "compile diagnostics", "kept", True,
         lambda: searched("query=art&page=1")),
        ("an application id", "compile diagnostics", "decoy", False,
         lambda: post(["x-algolia-application-id"])),
    ]


def heal_cases() -> List[Case]:
    from anyuse.execution import fresh_truth as FT
    from anyuse.execution import model_check as MC

    def made_up(statement):
        rows = "".join(f"<li class='r'><a href='/p/{n}'>{n}</a></li>"
                       for n in ("maple-top", "birch-top", "cedar-top", "delta-top"))
        body = f"<p class='notice'>{statement}</p>" if statement else ""

        def read(v):
            return (f"https://feed.test/search?q={v}",
                    f"<html><body>{body.replace('{q}', v)}<ul id='results'>{rows}</ul></body></html>")
        return read

    def bound(statement, fake=None):
        read = made_up(statement)
        if fake is None:
            got = FT.query_bound(read, "ul#results li", "vorquazib")
        else:
            with MC.using(fake):
                got = FT.query_bound(read, "ul#results li", "vorquazib")
        return got[2] or "none"
    reader = _fake(lambda kind, line: 0.05 if kind == "error" else
                   (0.95 if "vorquazib" in line else 0.05))
    return [
        ("id: the made-up page's statement read by the model in the heal",
         "heal: made-up search", "gap", "model",
         lambda: bound("Tidak ada hasil untuk {q}.", reader)),
        ("vi: the same", "heal: made-up search", "gap", "model",
         lambda: bound("Không tìm thấy kết quả nào cho {q}.", reader)),
        ("en: a listed-language statement, no model", "heal: made-up search", "kept",
         "fallback", lambda: bound("No results for {q}.")),
        ("id: no model configured", "heal: made-up search", "kept", "none",
         lambda: bound("Tidak ada hasil untuk {q}.")),
        ("a popular list with no statement, model reads none", "heal: made-up search", "decoy",
         "none", lambda: bound("", reader)),
    ]


def item_name_cases() -> List[Case]:
    from anyuse.mcpserver import compiled_tools as CT
    n = CT._item_name
    return [
        ("nl keys: naam, prijs, link", "agent item names", "gap", "Fietsbel",
         lambda: n({"prijs": "9,99", "naam": "Fietsbel", "link": "https://f.test/p/fietsbel"})),
        ("pl keys", "agent item names", "gap", "Wiertarka Bosch PSB 500",
         lambda: n({"cena": "199 zł", "nazwa": "Wiertarka Bosch PSB 500",
                    "adres": "https://s.test/p/wiertarka-bosch-psb-500"})),
        ("vi keys", "agent item names", "gap", "Nồi cơm điện",
         lambda: n({"giá": "250.000đ", "tên": "Nồi cơm điện",
                    "đường dẫn": "https://c.test/sp/noi-com-dien"})),
        ("a name the address starts but does not end", "agent item names", "gap", "Randall Shop",
         lambda: n({"name": "Randall Shop", "url": "https://shops.test/s/rand"})),
        ("a name and its version", "agent item names", "kept", "rand",
         lambda: n({"name": "rand v0.10.3", "url": "https://pkgs.test/crates/rand"})),
        ("plain title", "agent item names", "kept", "Cedar Point", lambda: n({"title": "Cedar Point"})),
        ("a slug field beside the title", "agent item names", "kept", "Pizza Margherita (large)",
         lambda: n({"title": "Pizza Margherita (large)", "slug": "pizza-margherita",
                    "url": "https://menu.test/p/pizza-margherita"})),
        ("an id in the address", "agent item names", "decoy", "Bosch drill",
         lambda: n({"id": "12345", "title": "Bosch drill", "url": "https://s.test/p/12345"})),
        ("an address number before a street name", "agent item names", "decoy", "123 Main St",
         lambda: n({"name": "123 Main St", "url": "https://maps.test/a/123"})),
    ]


GROUPS = (empty_cases, shape_cases, region_cases, embedded_cases, identity_cases, outage_cases,
          replay_cases, diagnostics_cases, heal_cases, item_name_cases)


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

def run_arm(arm: str, tree: str, out_file: str) -> None:
    import anyuse
    here = os.path.realpath(os.path.dirname(anyuse.__file__))
    if here != os.path.realpath(tree):
        raise SystemExit(f"{arm}: anyuse resolves to {here}, not {tree}")
    rows = []
    for group in GROUPS:
        try:
            cases = group()
        except Exception as exc:                    # noqa: BLE001 - a group the tree cannot build
            rows.append({"arm": arm, "group": group.__name__, "case": "(group)", "kind": "gap",
                         "want": None, "got": f"error: {type(exc).__name__}: {exc}"[:200],
                         "ok": False})
            continue
        for name, label, kind, want, fn in cases:
            try:
                got = fn()
            except Exception as exc:                # noqa: BLE001 - a case the tree cannot run
                got = f"error: {type(exc).__name__}: {exc}"[:200]
            rows.append({"arm": arm, "group": label, "case": name, "kind": kind,
                         "want": want, "got": got, "ok": got == want})
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def compare(files: List[str]) -> str:
    rows = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            rows.extend(json.load(fh))
    by = {}
    for r in rows:
        by.setdefault((r["group"], r["case"]), {})[r["arm"]] = r
    groups = list(dict.fromkeys(r["group"] for r in rows))
    lines = ["# generalised compile judgements: OLD vs NEW", "",
             "| group | kind | cases | OLD right | NEW right |", "|---|---|---|---|---|"]
    for g in groups:
        for kind in ("gap", "kept", "decoy"):
            cs = [v for (gg, _c), v in by.items() if gg == g and
                  any(r["kind"] == kind for r in v.values())]
            if not cs:
                continue
            old = sum(1 for v in cs if v.get("OLD", {}).get("ok"))
            new = sum(1 for v in cs if v.get("NEW", {}).get("ok"))
            lines.append(f"| {g} | {kind} | {len(cs)} | {old} | {new} |")
    tot = {a: sum(1 for r in rows if r["arm"] == a and r["ok"]) for a in ARMS}
    n = len(by)
    lines += ["", f"all cases: {n}; OLD right {tot.get('OLD', 0)}, NEW right {tot.get('NEW', 0)}"]
    wrong_new = [k for k, v in by.items() if not v.get("NEW", {}).get("ok")]
    regressions = [k for k, v in by.items() if v.get("OLD", {}).get("ok")
                   and not v.get("NEW", {}).get("ok")]
    for g, c in wrong_new:
        r = by[(g, c)].get("NEW", {})
        lines.append(f"NEW wrong: {g} / {c}: want {r.get('want')!r}, got {r.get('got')!r}")
    for g, c in regressions:
        lines.append(f"REGRESSION: {g} / {c}")
    lines.append("")
    lines.append("PASS" if not wrong_new and not regressions else "FAIL")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-generalize-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "generalize_compile_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    results = []
    for arm in ARMS:
        tree = trees[arm]
        pkgroot = os.path.join(out, f"pkgroot_{arm}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", f"api_hosts_{arm}.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", f"blocked_{arm}.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", f"robots_{arm}.json"),
            "ANYUSE_MODEL_CHECKS": "none",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{arm}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--arm", arm,
                               "--tree", tree, "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0 if text.rstrip().endswith("PASS") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--arm", required=True, choices=list(ARMS))
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.arm, os.path.abspath(args.tree), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
