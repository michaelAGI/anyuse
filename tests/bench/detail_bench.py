"""Offline A/B bench: item pages proved from the values they showed, one result per result card,
and visit_results rows checked on their own item page.

Two arms run the same corpus, each in its own process that imports ``anyuse``
from its own checkout: ``old`` (``--old-root``, the code before the change)
and ``new`` (``--new-root``, default: the checkout holding this file).

A. Item pages. A site per layout and label language: a JSON API
   (``/api/item?q=``) answers one item's fields, and its HTML page (``/item?q=``)
   shows them with labels as a dl, a table (row or column headers), "Label:
   value" text, a label span before a value span, JSON-LD, itemprop,
   aria-label, or not at all (only the JSON request in the HAR). Labels and
   values in English, German, Japanese, Russian and Spanish. Scenarios, each a
   compile from a HAR with plain ``--har-expect`` values:

   * good: three HARs; one HAR and two typed cases in page order; one HAR and
     two DOM-only page reads (attach-style) that label their values;
   * wrong: a typed case in another order than the page showed; a HAR case
     whose expected values are another item's summary; a page read whose
     labels put two values under each other's names.

   Counted: good cases proved, wrong cases proved (must be 0), the names each
   value was given by, the compiled template's output names.

B. Result cards holding two links. Pages whose cards hold a title and an ad, a
   variant, a shop link or a reviews link, in list, grid, table, classless and
   Japanese layouts, plus controls (a grid of rows, a card mentioning another
   listed result, a page with one result). (1) The agent's answer lists a card's
   extra link as a result of its own: resolved with the page's DOM
   (``answer_refs.resolve_on_dom``). (2) A compiled HTML tool whose selector
   matches every link of a card, proved on clean validation pages, then called
   on a page where one card gained an ad or variant links. Counted: extra links
   kept as results, distinct results merged into one (must be 0).

C. visit_results tables (the fake shop of ``tests/test_demo_breadth_filtered.py``,
   verifier on, no model): a clean table; a card whose link opens the page of a
   set listed beside it (the tool's own name check passes), or listed further
   down among the results the tool does not open; the set's card whose link
   opens the plain product listed further down; a card whose link
   opens an unrelated listed product; and, as a stated limit, a card whose link
   opens an unlisted set whose name holds the card's name. Counted per row:
   wrong rows accepted (must be 0 outside the limit row), right rows withheld.

Usage::

    python tests/bench/detail_bench.py --old-root /path/to/old/checkout [--json results.json]

No network: every request goes to an in-process transport. No model. Not part
of the test suite and not shipped.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# A. item pages
# ---------------------------------------------------------------------------

#: per language: the page's labels (title, summary, version) and four items
LANGS = {
    "en": (("Name", "Summary", "Version"),
           [("Serde Package", "a framework for serializing data", "1.0.210"),
            ("Tokio Runtime", "an asynchronous runtime for rust", "1.40.0"),
            ("Rand Crate", "random number generators and more", "0.8.5"),
            ("Regex Engine", "regular expressions for text search", "1.10.6")]),
    "de": (("Bezeichnung", "Beschreibung", "Ausgabe"),
           [("Akkuschrauber Bosch GSR", "kompakter Schrauber für die Werkstatt", "2.4.1"),
            ("Bohrhammer Makita HR", "kräftiger Hammer für Beton und Stein", "3.0.7"),
            ("Stichsäge Bosch PST", "leichte Säge für Holz und Kunststoff", "1.9.2"),
            ("Winkelschleifer Metabo W", "robuster Schleifer für Metall", "4.2.0")]),
    "ja": (("名前", "概要", "バージョン"),
           [("東京タワー 模型", "精密な縮尺の東京タワーの模型です", "3.2.0"),
            ("大阪城 模型", "木製の大阪城の組み立て模型です", "2.1.4"),
            ("富士山 置物", "陶器でできた富士山の置物です", "1.0.9"),
            ("京都 金閣寺 模型", "金色の塗装をした金閣寺の模型です", "5.3.1")]),
    "ru": (("Название", "Описание", "Версия"),
           [("Москва Сити", "деловой район в центре города", "5.1.0"),
            ("Казанский собор", "собор на Невском проспекте", "2.0.3"),
            ("Байкал Тур", "поездка к озеру Байкал летом", "1.4.8"),
            ("Эрмитаж Гид", "путеводитель по залам музея", "3.3.3")]),
    "es": (("Nombre", "Descripción", "Versión"),
           [("Sagrada Familia Guía", "la basílica de Gaudí en Barcelona", "2.2.0"),
            ("Alhambra Visita", "el palacio nazarí de Granada", "1.7.1"),
            ("Camino Santiago Mapa", "las etapas del camino francés", "4.0.5"),
            ("Prado Museo Plano", "las salas del museo en Madrid", "3.1.2")]),
}
SLUGS = ["alpha", "beta", "gamma", "delta"]
LAYOUTS = ("dl", "table_row", "table_col", "colon", "label_span", "jsonld", "itemprop", "aria", "json_only")
ITEMPROPS = ("name", "description", "softwareVersion")


def item_body(layout, labels, values, swap=False):
    """The item page's body: ``values`` under ``labels`` in ``layout``; ``swap``
    puts the summary and the version under each other's labels."""
    vals = list(values)
    if swap:
        vals[1], vals[2] = vals[2], vals[1]
    pairs = list(zip(labels, [html.escape(v) for v in vals]))
    if layout == "dl":
        return "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"
    if layout == "table_row":
        return "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in pairs) + "</table>"
    if layout == "table_col":
        return ("<table><thead><tr>" + "".join(f"<th>{k}</th>" for k, _v in pairs) + "</tr></thead><tbody><tr>"
                + "".join(f"<td>{v}</td>" for _k, v in pairs) + "</tr></tbody></table>")
    if layout == "colon":
        return "".join(f"<p>{k}: {v}</p>" for k, v in pairs)
    if layout == "label_span":
        return "".join(f'<div class="row"><span class="label">{k}:</span><span class="value">{v}</span></div>'
                       for k, v in pairs)
    if layout == "jsonld":
        ld = {"@context": "https://schema.org", "@type": "SoftwareApplication",
              **{p: v for p, v in zip(ITEMPROPS, vals)}}
        return (f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
                + "".join(f'<div class="v">{v}</div>' for _k, v in pairs))
    if layout == "itemprop":
        return "".join(f'<div><span itemprop="{p}">{v}</span></div>' for p, (_k, v) in zip(ITEMPROPS, pairs))
    if layout == "aria":
        return "".join(f'<div><span aria-label="{k}">{v}</span></div>' for k, v in pairs)
    return ""


def item_page(layout, labels, values, swap=False):
    return (f"<html><head><title>{html.escape(values[0])}</title></head><body><main>"
            f"{item_body(layout, labels, values, swap)}</main></body></html>")


def host_of(layout, lang):
    return f"{layout.replace('_', '-')}-{lang}.items.test"


def api_doc(values):
    return {"data": {"title": values[0], "about": values[1], "release": values[2], "kind": "item"}}


def har_entry(url, mime, text):
    return {"startedDateTime": "2026-10-01T00:00:00+00:00", "time": 1,
            "request": {"method": "GET", "url": url, "cookies": [], "headers": [], "queryString": []},
            "response": {"status": 200, "statusText": "OK", "headers": [{"name": "content-type", "value": mime}],
                         "content": {"size": len(text), "mimeType": mime, "text": text}, "redirectURL": ""},
            "cache": {}, "timings": {}}


def write_har(work, layout, lang, k):
    labels, items = LANGS[lang]
    host, slug = host_of(layout, lang), SLUGS[k]
    entries = [har_entry(f"https://{host}/api/item?q={slug}", "application/json",
                         json.dumps(api_doc(items[k]), ensure_ascii=False))]
    if layout != "json_only":
        entries.append(har_entry(f"https://{host}/item?q={slug}", "text/html",
                                 item_page(layout, labels, items[k])))
    path = os.path.join(work, f"{host}-{slug}.har")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"log": {"version": "1.2", "creator": {"name": "bench", "version": "1"}, "entries": entries}},
                  fh, ensure_ascii=False)
    return path


class ItemSite:
    """Every item site's API, in process."""

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        sp = urllib.parse.urlsplit(url)
        layout_lang = sp.netloc.split(".", 1)[0]
        lang = layout_lang.rsplit("-", 1)[1]
        q = urllib.parse.parse_qs(sp.query).get("q", [""])[0]
        if sp.path != "/api/item" or q not in SLUGS or lang not in LANGS:
            return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
        return ApiResponse(200, {"Content-Type": "application/json"},
                           json.dumps(api_doc(LANGS[lang][1][SLUGS.index(q)]), ensure_ascii=False))


def item_cases(work, layout, lang):
    """``(hars, [(scenario, good, cases)])`` for one site."""
    from anyuse.execution.har_import import HarCase
    _labels, items = LANGS[lang]
    hars = [write_har(work, layout, lang, k) for k in range(4)]
    shown = [list(v) for v in items]
    out = [("har3", True, [HarCase(hars[k], SLUGS[k], shown[k]) for k in range(3)]),
           ("har1_typed2", True, [HarCase(hars[0], SLUGS[0], shown[0]),
                                  HarCase("", SLUGS[1], shown[1]), HarCase("", SLUGS[2], shown[2])]),
           ("typed_wrong_order", False, [HarCase(hars[0], SLUGS[0], shown[0]),
                                         HarCase("", SLUGS[1], [shown[1][1], shown[1][0], shown[1][2]]),
                                         HarCase("", SLUGS[2], shown[2])]),
           ("har_wrong_values", False, [HarCase(hars[0], SLUGS[0], shown[0]),
                                        HarCase(hars[1], SLUGS[1], [shown[1][0], shown[3][1], shown[1][2]]),
                                        HarCase(hars[2], SLUGS[2], shown[2])])]
    return hars, out


def page_read(layout, lang, k, swap=False):
    """An attach-style read of an item page: its DOM and the values it showed, no JSON traffic."""
    labels, items = LANGS[lang]
    return {"param": {"name": "q", "value": SLUGS[k]}, "ui": {"items": list(items[k]), "text": ""},
            "dom": item_page(layout, labels, items[k], swap), "network": [],
            "final_url": f"https://{host_of(layout, lang)}/item?q={SLUGS[k]}"}


def run_items(work):
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    from anyuse.execution.har_import import compile_from_har, har_to_recording, load_har
    rows = []
    for lang in LANGS:
        for layout in LAYOUTS:
            hars, scenarios = item_cases(work, layout, lang)
            for name, good, cases in scenarios:
                try:
                    compiled, proof, _report = compile_from_har(
                        cases, param_name="q", executor=ApiStepExecutor(transport=ItemSite()),
                        preflight=lambda _c: "")
                    rows.append(_item_row(lang, layout, name, good, compiled, proof))
                except Exception as exc:  # noqa: BLE001 - a crash is a row, not a lost corpus
                    rows.append({"lang": lang, "layout": layout, "scenario": name, "good": good,
                                 "passed": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
            if layout == "json_only":
                continue
            base = har_to_recording(load_har(hars[0]), "q", SLUGS[0], list(LANGS[lang][1][0]))
            for name, good, swap in (("page_reads", True, False), ("page_reads_swapped", False, True)):
                fresh = [page_read(layout, lang, 1, swap), page_read(layout, lang, 2)]
                try:
                    compiled, proof = compile_and_verify(base, fresh,
                                                         executor=ApiStepExecutor(transport=ItemSite()))
                    rows.append(_item_row(lang, layout, name, good, compiled, proof))
                except Exception as exc:  # noqa: BLE001
                    rows.append({"lang": lang, "layout": layout, "scenario": name, "good": good,
                                 "passed": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
    return rows


def _item_row(lang, layout, name, good, compiled, proof):
    t = compiled.template
    steps = getattr(t, "steps", None) if t is not None else None
    last = steps[-1].template if steps else t
    return {"lang": lang, "layout": layout, "scenario": name, "good": good, "passed": bool(proof.passed),
            "shape": getattr(t, "shape", "") if t is not None else "",
            "keys": sorted((getattr(last, "extract", None) or {}).keys()) if last is not None else [],
            "named_by": sorted({str(v) for c in proof.cases
                                for v in (c.contract.get("fields_named_by") or {}).values()}),
            "reason": str(proof.reason)[:240]}


# ---------------------------------------------------------------------------
# B. result cards holding two links
# ---------------------------------------------------------------------------

S = "https://cards.test"


def _li_cards(names, extra=None, heading="h3", cls="card"):
    """A list of cards; ``extra[k]`` is markup added inside card k."""
    extra = extra or {}
    return "".join(f'<li class="{cls}"><{heading}><a href="{S}/p/{slug}">{html.escape(title)}</a></{heading}>'
                   f'{extra.get(k, "")}<p>about {html.escape(title)}</p></li>' for k, (slug, title) in enumerate(names))


NAMES = [("alpha-drill", "Alpha Drill"), ("beta-saw", "Beta Saw"), ("gamma-sander", "Gamma Sander"),
         ("delta-router", "Delta Router")]


def card_pages():
    """``[(name, dom, answer urls, expected urls)]``; expected: the distinct results the answer names."""
    t = [f"{S}/p/{slug}" for slug, _ in NAMES]
    pages = []
    ad = f'<div class="ad"><a href="https://partner.test/offer?sid=1">Sponsored offer</a></div>'
    pages.append(("list_ad", f'<main><ul class="results">{_li_cards(NAMES, {0: ad})}</ul></main>',
                  [t[0], "https://partner.test/offer?sid=1", t[1], t[2]], t[:3]))
    var = (f'<div class="variants"><a href="{S}/p/beta-saw-red">Red</a>'
           f'<a href="{S}/p/beta-saw-blue">Blue</a></div>')
    pages.append(("list_variants", f'<main><ul class="results">{_li_cards(NAMES, {1: var})}</ul></main>',
                  [t[0], t[1], f"{S}/p/beta-saw-red", t[2]], t[:3]))
    offers = "".join(f'<div class="offer"><h3><a href="{S}/p/{slug}">{title}</a></h3><p>99 €</p>'
                     f'<a class="shop" href="https://merchant.test/go/{slug}">Zum Shop</a></div>'
                     for slug, title in NAMES)
    pages.append(("offers_shop_link", f'<main><div class="offers">{offers}</div></main>',
                  [t[0], "https://merchant.test/go/alpha-drill", t[1], t[2]], t[:3]))
    rows = "".join(f'<tr class="row"><td><a href="{S}/p/{slug}">{title}</a></td><td>a tool</td>'
                   f'<td><a href="{S}/p/{slug}/reviews">12 reviews</a></td></tr>' for slug, title in NAMES)
    pages.append(("table_reviews", f'<main><table class="list"><tbody>{rows}</tbody></table></main>',
                  [t[0], f"{S}/p/alpha-drill/reviews", t[1], t[2]], t[:3]))
    classless = "".join(f'<li><div><a href="{S}/p/{slug}">{title}</a></div><div><a href="{S}/p/{slug}-kit">'
                        f'{title} kit</a></div></li>' for slug, title in NAMES)
    pages.append(("classless", f"<main><ul>{classless}</ul></main>",
                  [t[0], f"{S}/p/alpha-drill-kit", t[1], t[2]], t[:3]))
    grid = "".join('<div class="row">' + "".join(
        f'<div class="card"><h3><a href="{S}/g/{r}{c}">Grid {r}{c} tool</a></h3><p>x y</p></div>' for c in range(3))
        + "</div>" for r in range(2))
    gu = [f"{S}/g/{r}{c}" for r in range(2) for c in range(3)]
    pages.append(("grid_rows_control", f'<main><div class="grid">{grid}</div></main>', gu, gu))
    rel = f'<p>Related: <a href="{S}/p/beta-saw">Beta Saw</a></p>'
    pages.append(("related_control", f'<main><ul class="results">{_li_cards(NAMES, {0: rel})}</ul></main>',
                  list(t), list(t)))
    one = (f'<main><div class="hit"><h2><a href="{S}/p/alpha-drill">Alpha Drill</a></h2>'
           f'<a href="https://partner.test/offer?sid=2">Sponsored offer</a></div></main>'
           f'<aside><a href="{S}/p/zeta">Zeta</a></aside>')
    pages.append(("single_result", one, [t[0], "https://partner.test/offer?sid=2"], [t[0]]))
    ja = [("tokyo-tower", "東京タワー 模型"), ("osaka-jo", "大阪城 模型"), ("fuji-san", "富士山 置物"),
          ("kinkakuji", "金閣寺 模型")]
    jad = f'<div class="pr"><a href="https://partner.test/pr?id=9">広告 特価セール</a></div>'
    pages.append(("ja_list_ad", f'<main><ul class="items">{_li_cards(ja, {2: jad}, "h2", "item")}</ul></main>',
                  [f"{S}/p/tokyo-tower", f"{S}/p/osaka-jo", f"{S}/p/fuji-san", "https://partner.test/pr?id=9"],
                  [f"{S}/p/tokyo-tower", f"{S}/p/osaka-jo", f"{S}/p/fuji-san"]))
    return pages


def run_answers():
    from anyuse.execution import answer_refs as AR
    rows = []
    for name, body, answer, expected in card_pages():
        dom = f"<html><body>{body}</body></html>"
        ans = {"results": [{"url": u} for u in answer]}
        got, rep = AR.resolve_on_dom("site_search", ans, dom, f"{S}/search?q=tool")
        urls = [str(r.get("url") or "") for r in got.get("results") or []]
        rows.append({"page": name, "answered": len(answer), "returned": len(urls),
                     "extras_kept": sum(1 for u in urls if u not in expected),
                     "distinct_merged": sum(1 for u in expected if u not in urls),
                     "same_record": int(rep.get("same_record") or 0)})
    return rows


def run_guide_verdict():
    """The aquarium page of tests/test_verifier_units.py: the answer with the ad, through the resolver, then the
    independent verifier."""
    from anyuse.execution import answer_refs as AR
    from anyuse.execution import attribute_verify as AV
    import test_verifier_units as W
    page = W.guide_page()
    ans = {"results": [{"title": W.NAMES[0], "url": W.fish(0)}, {"title": "aktuell 3.99€ bei Partner *",
                                                                 "url": W.AD},
                       {"title": W.NAMES[1], "url": W.fish(1)}]}
    got, _rep = AR.resolve_on_dom("site_search", ans, page._dom, page.url)
    return {"returned": len(got["results"]),
            "verdict": AV.check("site_search", got, W.guide_page(), AV.Resolver(None), value="Guppy").verdict}


class CardSite:
    """A search page of cards per query; ``extra`` markup in one card at runtime."""

    def __init__(self, layout, extra=None):
        self.layout, self.extra = layout, extra or {}

    def page(self, q):
        names = [(f"{q}-{k}", f"{q.title()} tool {k}") for k in range(4)]
        if self.layout == "li":
            body = f'<ul class="results">{_li_cards(names, self.extra)}</ul>'
        elif self.layout == "div":
            body = "".join(f'<div class="result"><h2><a href="{S}/p/{s}">{t}</a></h2>{self.extra.get(k, "")}'
                           f'<p>about it</p></div>' for k, (s, t) in enumerate(names))
        else:
            body = ("<table><tbody>" + "".join(
                f'<tr class="row"><td><a href="{S}/p/{s}">{t}</a></td><td>{self.extra.get(k, "")}info</td></tr>'
                for k, (s, t) in enumerate(names)) + "</tbody></table>")
        return f"<html><body><main><h1>Results for {q}</h1>{body}</main></body></html>", [t for _s, t in names]

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        return ApiResponse(200, {"content-type": "text/html; charset=utf-8"}, self.page(q)[0])


SELECTORS = {"li": "li.card a", "div": "div.result a", "table": "tr.row a"}


def run_rung():
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_step import ApiStepExecutor, RequestTemplate
    from anyuse.execution.api_verify import verify_template
    rows = []
    extras = {"ad": {2: f'<span class="ad"><a href="https://partner.test/x">Sponsored offer</a></span>'},
              "variants": {1: f'<span class="variants"><a href="{S}/p/v-red">Red</a>'
                              f'<a href="{S}/p/v-blue">Blue</a></span>'},
              "clean": {}}
    for layout, sel in SELECTORS.items():
        t = RequestTemplate(method="GET", url=f"{S}/search", query={"q": "{q}"}, extract={"items": sel},
                            extract_kind="html", shape="list", params=["q"],
                            expect={"status_in": [200], "content_type": "html", "selector": sel, "min_items": 1})
        RC.attach(t)
        site = CardSite(layout)
        cases = [{"value": q, "ui_items": site.page(q)[1]} for q in ("alpha", "beta")]
        proof = verify_template(t, cases, ApiStepExecutor(transport=site), 2)
        if proof.passed:
            RC.finalize(t, proof)
        flag = bool((RC.contract_of(t) or {}).get("one_item_per_record"))
        for kind, extra in extras.items():
            run_site = CardSite(layout, extra)
            out = ApiStepExecutor(transport=run_site).execute(t, {"q": "gamma"})
            titles = run_site.page("gamma")[1]
            items = list((out.get("extracted") or {}).get("items") or []) if out.get("success") else []
            rows.append({"layout": layout, "runtime": kind, "proved": bool(proof.passed), "flag": flag,
                         "success": bool(out.get("success")), "items": len(items),
                         "wrong_items": sum(1 for x in items if x not in titles),
                         "titles_missing": sum(1 for x in titles if x not in items)})
    return rows


# ---------------------------------------------------------------------------
# C. visit_results tables
# ---------------------------------------------------------------------------

SET = ("bosch-akkuschrauber-gsr-12-set", "Bosch Akkuschrauber GSR 12 Set", 119.0, None, "2025-03-13", "Bosch", True)


def run_visits():
    from anyuse.execution import attribute_verify as AV
    from anyuse.execution import demo_compile as C
    from anyuse.execution import demo_tool as T
    import test_demo_breadth_filtered as F
    from test_demo_breadth_visit import visit_trace

    class TextPage(F.FakePage):
        async def text(self):
            return self.read()["text"]

    def swapped(card_slug, to_slug, hide=(), move=None):
        """The card of ``card_slug`` links ``to_slug``'s page; ``hide``: results not listed; ``move``: ``(slug,
        place)`` lists that result at that place."""
        class Shop(F.Shop):
            def card(self, c):
                out = super().card(c)
                if c[0] == card_slug:
                    out = out.replace(f'href="/p/{c[0]}"', f'href="/p/{to_slug}"')
                return out

            def hits(self, q, *a, **kw):
                hs = [c for c in super().hits(q, *a, **kw) if c[0] not in hide]
                if move:
                    moved = [c for c in hs if c[0] == move[0]]
                    hs = [c for c in hs if c[0] != move[0]]
                    hs[move[1]:move[1]] = moved
                return hs
        return Shop()

    tool = C.compile_trace(visit_trace(F.Shop()))["tool"]
    os.environ[AV.SETTING] = "on"
    AV._DEFAULT["r"] = AV.Resolver(None)
    # (name, shop, k, where the set is put in the catalogue or None, tags)
    scenarios = [("clean", F.Shop(), 4, None, []),
                 ("listed_set_page", swapped("bosch-akkuschrauber-gsr-12", SET[0]), 2, 1, []),
                 ("listed_unopened_set_page", swapped("bosch-akkuschrauber-gsr-12", SET[0]), 2, 3, []),
                 ("set_card_listed_item_page", swapped(SET[0], "bosch-akkuschrauber-gsr-12",
                                                       move=("bosch-akkuschrauber-gsr-12", 3)), 2, 0, []),
                 ("unrelated_listed_page", swapped("makita-akkuschrauber-df333", "einhell-akkuschrauber-te-cd"),
                  4, None, []),
                 ("unlisted_set_page (limit)", swapped("bosch-akkuschrauber-gsr-12", SET[0], hide=(SET[0],)), 2,
                  1, ["limit"])]
    rows = []
    for name, shop, k, set_at, tags in scenarios:
        with_set = set_at is not None
        if with_set:
            F.CATALOG.insert(set_at, SET)
        try:
            listed = [c[0] for c in shop.hits("Akkuschrauber")][:k]
            out = asyncio.run(T.run_tool(TextPage(shop), tool, "Akkuschrauber", F._settle, result_wait_s=0.2,
                                         params={"k": k}))
        finally:
            if with_set:
                F.CATALOG.remove(SET)
        table = (out.get("output") or out.get("fields_seen") or out.get("unverified_output") or {}).get("items") \
            or [{"rank": r.get("rank"), "url": r.get("url"), "status": r.get("status")}
                for r in (out.get("checks") or {}).get("items") or []]
        wrong_ok = right_held = wrong = 0
        for r in table:
            rank = int(r.get("rank") or 0)
            want = listed[rank - 1] if 0 < rank <= len(listed) else ""
            right = str(r.get("url") or "").rstrip("/").endswith("/p/" + want)
            wrong += 0 if right else 1
            if r.get("status") == "ok" and not right:
                wrong_ok += 1
            if r.get("status") != "ok" and right:
                right_held += 1
        rows.append({"scenario": name, "status": out.get("status"), "accepted": bool(out.get("accepted")),
                     "rows": len(table), "wrong_rows": wrong, "wrong_rows_accepted": wrong_ok,
                     "right_rows_withheld": right_held, "limit": "limit" in tags,
                     "statuses": [r.get("status") for r in table]})
    return rows


# ---------------------------------------------------------------------------
# the arms and the report
# ---------------------------------------------------------------------------

def run_arm(ns):
    sys.path.insert(0, ns.pkgroot)
    sys.path.insert(1, os.path.join(ns.pkgroot, "anyuse", "tests"))
    os.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "on")
    result = {"items": run_items(ns.work), "answers": run_answers(), "guide": run_guide_verdict(),
              "rung": run_rung(), "visits": run_visits()}
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)


def _arm(arm, root, tmp):
    pk = os.path.join(tmp, "pk-" + arm)
    os.makedirs(pk)
    os.symlink(os.path.realpath(root), os.path.join(pk, "anyuse"))
    out = os.path.join(tmp, arm + ".json")
    work = os.path.join(tmp, "work-" + arm)
    os.makedirs(work)
    home = os.path.join(tmp, "home-" + arm)
    data = os.path.join(home, ".anyuse")
    os.makedirs(data)
    env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
               XDG_DATA_HOME=os.path.join(home, ".local", "share"),
               XDG_CACHE_HOME=os.path.join(home, ".cache"), ANYUSE_DATA_DIR=data,
               ANYUSE_SECRETS_DIR=data, ANYUSE_VAULT_KEYFILE=os.path.join(data, "entity_vault.keys"),
               ANYUSE_API_HOSTS_FILE=os.path.join(data, "api_hosts.json"),
               ANYUSE_BLOCKED_HOSTS_FILE=os.path.join(data, "api_blocked_hosts.json"),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring", ANYUSE_MODEL_CHECKS="none",
               ANYUSE_ATTRIBUTE_VERIFY="on", ANYUSE_EMPTY_PROBE="off")
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_OPENJEV_URL", "ANYUSE_JUDGE",
                 "ANYUSE_CONFIRMER", "ANYUSE_CONFIRMER_LAYA_DIR", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_JUDGE_MODEL_PROVIDER", "PYTHONPATH"):
        env.pop(name, None)
    cmd = [sys.executable, "-B", os.path.abspath(__file__), "--arm", arm, "--pkgroot", pk, "--out", out,
           "--work", work]
    return subprocess.Popen(cmd, env=env, cwd=tmp), out


def report(old, new):
    lines, checks = [], []

    def items_summary(doc):
        good = [r for r in doc["items"] if r["good"]]
        wrong = [r for r in doc["items"] if not r["good"]]
        return (sum(r["passed"] for r in good), len(good), sum(r["passed"] for r in wrong), len(wrong))

    og, on, ow, owt = items_summary(old)
    ng, nn, nw, nwt = items_summary(new)
    lines.append("A. item pages proved from plain value lists")
    lines.append(f"  good cases proved:  old {og}/{on}   new {ng}/{nn}")
    lines.append(f"  wrong cases proved: old {ow}/{owt}   new {nw}/{nwt}")
    by = {}
    for r in new["items"]:
        if r["good"]:
            k = (r["layout"], r["scenario"])
            by.setdefault(k, [0, 0])
            by[k][0] += r["passed"]
            by[k][1] += 1
    for (layout, scen), (p, n) in sorted(by.items()):
        named = sorted({x for r in new["items"] if r["layout"] == layout and r["scenario"] == scen
                        for x in r["named_by"]})
        keys = next((r["keys"] for r in new["items"] if r["layout"] == layout and r["scenario"] == scen
                     and r["lang"] == "en"), [])
        lines.append(f"    new {layout:<11} {scen:<12} {p}/{n} languages  named by {', '.join(named) or '-'}"
                     f"  keys (en) {keys}")
    missed = [f"{r['lang']}/{r['layout']}/{r['scenario']}: {r.get('error') or r['reason'][:120]}"
              for r in new["items"] if r["good"] and not r["passed"]]
    for m in missed[:12]:
        lines.append(f"    not proved: {m}")
    checks.append({"name": "no wrong item case proved (new)", "pass": nw == 0})

    lines.append("B1. answers that list a card's second link")
    for o, n in zip(old["answers"], new["answers"]):
        lines.append(f"  {n['page']:<18} answered {n['answered']}  extras kept old {o['extras_kept']} new "
                     f"{n['extras_kept']}  distinct merged old {o['distinct_merged']} new {n['distinct_merged']}")
    lines.append(f"  aquarium page verdict: old {old['guide']['verdict']} ({old['guide']['returned']} results)  "
                 f"new {new['guide']['verdict']} ({new['guide']['returned']} results)")
    checks.append({"name": "no distinct results merged (new answers)",
                   "pass": all(r["distinct_merged"] == 0 for r in new["answers"])})
    lines.append("B2. a compiled HTML tool whose selector matches every link of a card")
    for o, n in zip(old["rung"], new["rung"]):
        lines.append(f"  {n['layout']:<6} {n['runtime']:<9} flag old {o['flag']} new {n['flag']}  items old "
                     f"{o['items']} new {n['items']}  wrong items old {o['wrong_items']} new {n['wrong_items']}  "
                     f"titles missing old {o['titles_missing']} new {n['titles_missing']}")
    checks.append({"name": "no distinct results merged (new rung)",
                   "pass": all(r["titles_missing"] == 0 for r in new["rung"])})
    lines.append("C. visit_results rows on their own item page")
    for o, n in zip(old["visits"], new["visits"]):
        lines.append(f"  {n['scenario']:<26} wrong rows {n['wrong_rows']}  accepted old "
                     f"{o['wrong_rows_accepted']} new {n['wrong_rows_accepted']}  right rows withheld old "
                     f"{o['right_rows_withheld']} new {n['right_rows_withheld']}  status old {o['status']} new "
                     f"{n['status']}")
    checks.append({"name": "every wrong visit row withheld (new, limit row excluded)",
                   "pass": all(r["wrong_rows_accepted"] == 0 for r in new["visits"] if not r["limit"])})
    lines.append("checks:")
    for c in checks:
        lines.append(f"  {'PASS' if c['pass'] else 'FAIL'}  {c['name']}")
    return "\n".join(lines), {"old": old, "new": new, "checks": checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old-root", help="checkout of the code before the change")
    ap.add_argument("--new-root", default=CHECKOUT)
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--arm", default="", help=argparse.SUPPRESS)
    ap.add_argument("--pkgroot", default="", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--work", default="", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    if ns.arm:
        run_arm(ns)
        return 0
    if not ns.old_root:
        ap.error("--old-root is required: a checkout of the code before the change")
    tmp = tempfile.mkdtemp(prefix="detail-bench-")
    try:
        procs = [_arm(arm, root, tmp) for arm, root in (("old", ns.old_root), ("new", ns.new_root))]
        if [p.wait() for p, _o in procs] != [0, 0]:
            print("an arm failed")
            return 2
        docs = []
        for _p, path in procs:
            with open(path, encoding="utf-8") as fh:
                docs.append(json.load(fh))
        text, result = report(*docs)
        print(text)
        if ns.json:
            with open(ns.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
        return 0 if all(c["pass"] for c in result["checks"]) else 1
    finally:
        for arm in ("old", "new"):
            link = os.path.join(tmp, "pk-" + arm, "anyuse")
            if os.path.islink(link):
                os.unlink(link)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
