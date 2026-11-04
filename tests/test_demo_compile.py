"""Offline (a fake shop and a fake search site; no browser, no network, no model): ONE
demonstration compiles into a checked tool with an explicit input binding, or an
explicit failure.

What must hold:

1. a product-lookup trace (search -> result page -> the opened item -> done) compiles
   into a tool whose search is the site's own result URL with ``{q}`` (the input
   binding), whose result region is the opened item's link cluster, and whose field
   sources are where the product page states the reported name and price;
2. the compiler reads the trace's actions and pages only (a rationale key in an action
   is ignored; the tool holds no other value than the demonstrated one);
3. a trace that never entered the value, opened no item, or ended on a page with no
   price source is an explicit compile failure; so is an invalid demonstration
   (unfinished, forbidden attempt, a list page, a price not on the page);
4. the checks on a result page: the best-matching item is chosen; the empty state
   needs the page's own statement and no item naming the value (recommended items
   do not make a list); items that do not name the value are a failure, never an
   answer; an off-site or error page is a failure;
5. product identity and field sanity: a page naming another product, a list page,
   a price the page does not show are failures;
6. a site-search trace compiles into a list tool; its list must belong to the query.
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402

BASE = "https://www.shop.test"
CATALOG = [("acme-kettle-2000", "Acme Kettle 2000 Steel", 49.99, "InStock"),
           ("acme-kettle-3000", "Acme Kettle 3000 Glass", 59.99, "InStock"),
           ("borealis-toaster-4", "Borealis Toaster 4 Slice", 39.50, "OutOfStock"),
           ("borealis-toaster-2", "Borealis Toaster 2 Slice", 29.00, "InStock"),
           ("cumulus-blender-x", "Cumulus Blender X", 89.00, "InStock"),
           ("cumulus-blender-pro", "Cumulus Blender Pro", 129.00, "InStock"),
           ("delta-mixer-7", "Delta Mixer 7", 75.00, "InStock")]
NAV = [("Kettles", "/c/kettles"), ("Toasters", "/c/toasters"), ("Blenders", "/c/blenders"),
       ("My account", "/account"), ("Cart", "/cart")]
SEARCH_LD = json.dumps({"@context": "https://schema.org", "@type": "WebSite", "url": BASE + "/",
                        "potentialAction": {"@type": "SearchAction", "target": BASE + "/search?q={search_term_string}",
                                            "query-input": "required name=search_term_string"}})


def eur(p):
    return f"{p:.2f}".replace(".", ",") + " €"


def search_hits(v):
    hits = [c for c in CATALOG if C.name_share(c[1], v) > 0]
    return sorted(hits, key=lambda c: -C.name_share(c[1], v))


def spec_for(url, recommend=True):
    """A fake page: title, h1, text, links (text, href, area, ctx), JSON-LD."""
    p = urllib.parse.urlsplit(url)
    nav = [(t, BASE + h, "nav", "") for t, h in NAV]
    if p.path in ("", "/"):
        feat = [(c[1], f"{BASE}/p/{c[0]}", "main", f"{c[1]} {eur(c[2])}") for c in CATALOG[:5]]
        return {"url": url, "title": "Shop Test - Kitchen", "h1": ["Welcome"], "links": nav + feat,
                "text": "Welcome to the kitchen shop\nKettles Toasters Blenders\n" +
                        "\n".join(f"{c[1]} {eur(c[2])}" for c in CATALOG[:5]), "jsonld": [SEARCH_LD]}
    if p.path == "/search":
        v = dict(urllib.parse.parse_qsl(p.query)).get("q", "")
        hits = search_hits(v)
        # a "product of the day" sidebar BEFORE the results, with the results' URL pattern
        day = [("Delta Mixer 7 (product of the day)", f"{BASE}/p/delta-mixer-7", "aside", "")] \
            if not any(c[0] == "delta-mixer-7" for c in hits) else []
        if hits:
            links = day + [(c[1], f"{BASE}/p/{c[0]}", "main", f"{c[1]} {eur(c[2])}") for c in hits]
            text = f"{len(hits)} results for \"{v}\"\n" + "\n".join(f"{c[1]} {eur(c[2])}" for c in hits)
        else:
            links = [(c[1], f"{BASE}/p/{c[0]}", "main", f"{c[1]} {eur(c[2])}") for c in CATALOG[:3]] if recommend else []
            text = f"Keine Treffer für „{v}“\nDas könnte Ihnen gefallen:\n" + \
                "\n".join(f"{c[1]} {eur(c[2])}" for c in CATALOG[:3])
        return {"url": url, "title": f"Search: {v}", "h1": [f"Search: {v}"], "links": nav + links, "text": text,
                "jsonld": []}
    if p.path.startswith("/p/"):
        slug = p.path.split("/")[-1]
        c = next(x for x in CATALOG if x[0] == slug)
        ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": c[1], "sku": slug,
                         "offers": {"@type": "Offer", "price": c[2], "priceCurrency": "EUR",
                                    "availability": "https://schema.org/" + c[3]}})
        other = [(x[1], f"{BASE}/p/{x[0]}", "aside", "") for x in CATALOG if x[0] != slug][:2]
        return {"url": url, "title": f"{c[1]} | Shop Test", "h1": [c[1]], "links": nav + other,
                "text": f"{c[1]}\n{eur(c[2])}\nArt.-Nr. {slug}\n" + ("Auf Lager" if c[3] == "InStock" else "Ausverkauft") +
                        "\nIn den Warenkorb", "jsonld": [ld], "canonical": url, "og_type": "product"}
    return {"url": url, "title": "Not found", "h1": ["Page not found"], "links": nav, "text": "404 Not Found",
            "jsonld": []}


def sigs_of(spec):
    """The result containers (demo_tool.LINK_SIG_JS) of the fake page's links."""
    out = {}
    for _t, h, area, _ctx in spec["links"]:
        sig = {"main": "div#page>main>ul.products>li.product", "aside": "div#page>aside>ul.day>li.day"}.get(area, "")
        out.setdefault(h, [])
        if sig not in out[h]:
            out[h].append(sig)
    return out


def read_of(spec):
    els = [{"i": 0, "tag": "input", "text": "Search", "area": "header", "type": "search"}]
    for n, (t, h, area, ctx) in enumerate(spec["links"], start=1):
        e = {"i": n, "tag": "a", "text": t, "href": h, "area": area, "role": "", "type": "", "y": 100 + 20 * n}
        if ctx:
            e["ctx"] = ctx
        els.append(e)
    facts = W.product_facts(spec.get("jsonld") or [])
    return {"url": spec["url"], "title": spec["title"], "h1": spec.get("h1") or [], "headings": spec.get("h1") or [],
            "elements": els, "text": spec["text"], "main_text": spec["text"], "jsonld": spec.get("jsonld") or [],
            "facts": facts, "canonical": spec.get("canonical", ""), "og_type": spec.get("og_type", ""),
            "md_products": 0, "breadcrumb": [], "inputs": [{"index": 0, "visible": True, "name": "q"}],
            "meta": {}, "microdata": {}, "link_sigs": sigs_of(spec), "dom": dom_of(spec)}


def dom_of(spec):
    parts = [f"<html><head><title>{spec['title']}</title></head><body><header><nav>"]
    for t, h, area, _ctx in spec["links"]:
        if area == "nav":
            parts.append(f'<a href="{h}">{t}</a>')
    parts.append('</nav><form action="/search" method="get"><input type="search" name="q"></form></header><main><ul>')
    for t, h, area, _ctx in spec["links"]:
        if area == "main":
            parts.append(f'<li class="product"><a href="{h}">{t}</a></li>')
    parts.append("</ul></main><aside>")
    for t, h, area, _ctx in spec["links"]:
        if area == "aside":
            parts.append(f'<a href="{h}">{t}</a>')
    parts.append("</aside><footer><a href=\"/imprint\">Imprint</a></footer></body></html>")
    return "".join(parts)


def trace_page(url):
    spec = spec_for(url)
    r = read_of(spec)
    r["dom"] = dom_of(spec)
    r["search_urls"] = [t["template"] for t in W.search_url_templates(r["dom"], url, r["jsonld"])]
    return r


def product_trace(value="Acme Kettle 2000 Steel", open_slug="acme-kettle-2000", reported=None, extra_action_keys=None):
    start = BASE + "/"
    rurl = f"{BASE}/search?q={urllib.parse.quote_plus(value)}"
    purl = f"{BASE}/p/{open_slug}"
    c = next(x for x in CATALOG if x[0] == open_slug)
    rep = reported or {"name": c[1], "price": eur(c[2]), "currency": "EUR", "availability": "Auf Lager", "url": purl}
    a0 = {"action": "search", "element": 0, "text": value}
    if extra_action_keys:
        a0.update(extra_action_keys)
    return {"version": 1, "workflow": "product_lookup", "site": "www.shop.test", "start_url": start, "value": value,
            "steps": [{"k": 0, "page": trace_page(start), "action": a0,
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}},
                      {"k": 1, "page": trace_page(rurl), "action": {"action": "click", "element": 3},
                       "outcome": {"ok": True, "how": "goto", "href": purl, "url_after": purl}},
                      {"k": 2, "page": trace_page(purl), "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": trace_page(purl), "result": rep, "stop": "done", "forbidden": []}


def test_product_lookup_compiles_with_input_binding_region_and_field_sources():
    tr = product_trace()
    ok, why, _d = C.demo_validity(tr)
    assert ok, why
    got = C.compile_trace(tr)
    assert got["ok"], got
    tool = got["tool"]
    assert tool["search"]["mode"] == "url" and tool["search"]["template"] == BASE + "/search?q={q}", tool["search"]
    assert tool["input"]["name"] == "term" and tool["input"]["demonstrated_text_was_the_input"] is True
    assert tool["region"]["kind"] == "cluster" and "/p/" in tool["region"]["template"], tool["region"]
    assert tool["fields"]["name"] == "jsonld" and tool["fields"]["price"] == "jsonld", tool["fields"]
    assert tool["fields"]["availability"] == "jsonld" and tool["model_calls"] == 0
    assert C.fill(tool["search"]["template"], "Borealis Toaster 2") == BASE + "/search?q=Borealis+Toaster+2"
    # the region regex recognises other items of the same cluster and not the chrome
    import re
    rx = re.compile(tool["region"]["regex"])
    assert rx.match(BASE + "/p/delta-mixer-7") and not rx.match(BASE + "/c/kettles")
    assert rx.match("https://shop.test/p/delta-mixer-7#reviews")


def test_compile_reads_actions_and_pages_only():
    # a rationale in an action is never used: the tool is the same
    a = C.compile_trace(product_trace())["tool"]
    b = C.compile_trace(product_trace(extra_action_keys={"reason": "I think the kettle is best"}))["tool"]
    a.pop("compiled_from"), b.pop("compiled_from")
    assert a == b
    blob = json.dumps(a)
    # nothing but the demonstrated value's own URL shape is in the tool
    assert "Borealis" not in blob and "Cumulus" not in blob


def test_explicit_compile_failures():
    tr = product_trace()
    tr["steps"][0]["action"]["text"] = "something else entirely"
    assert C.compile_trace(tr) == {"ok": False, "why": "no search step entered the demonstrated value",
                                   "report": {"workflow": "product_lookup", "site": "www.shop.test"}}
    tr = product_trace()
    tr["steps"] = tr["steps"][:1] + [dict(tr["steps"][1], action={"action": "done", "result": tr["result"]},
                                          outcome={})]
    got = C.compile_trace(tr)
    assert not got["ok"] and "no item was opened" in got["why"]
    tr = product_trace()
    fp = tr["final_page"]
    fp.update(jsonld=[], facts=[], text=fp["text"].replace("49,99 €", ""), main_text=fp["text"].replace("49,99 €", ""))
    got = C.compile_trace(tr)
    assert not got["ok"] and "price" in got["why"], got


def test_invalid_demonstrations():
    tr = product_trace()
    tr["stop"] = "max_steps"
    assert not C.demo_validity(tr)[0]
    tr = product_trace()
    tr["forbidden"] = [{"why": "control names 'in den warenkorb'"}]
    assert not C.demo_validity(tr)[0]
    tr = product_trace(reported={"name": "Acme Kettle 2000 Steel", "price": "12,34 €", "url": BASE + "/p/acme-kettle-2000"})
    ok, why, _ = C.demo_validity(tr)
    assert not ok and "price" in why
    tr = product_trace()
    tr["final_page"] = trace_page(f"{BASE}/search?q=Acme")        # a list page, not the item
    ok, why, _ = C.demo_validity(tr)
    assert not ok
    tr = product_trace()
    tr["steps"][0]["action"]["text"] = "Kettle"                    # a part of the value below half its words
    assert not C.demo_validity(tr)[0]


def tool():
    return C.compile_trace(product_trace())["tool"]


def test_result_page_checks():
    t = tool()
    r = read_of(spec_for(f"{BASE}/search?q=Borealis+Toaster+2+Slice"))
    j = C.judge_result_page(t, r, "Borealis Toaster 2 Slice")
    assert j["status"] == "pick" and j["pick"]["url"].endswith("/p/borealis-toaster-2"), j
    # the empty state: the site's statement, and the recommended items name nothing of the value
    r = read_of(spec_for(f"{BASE}/search?q=Zqurvant+Oxlembiq"))
    j = C.judge_result_page(t, r, "Zqurvant Oxlembiq")
    assert j["status"] == "no_results" and "keine treffer" in j["empty_statement"], j
    # items and no statement, none naming the value: a failure, never an answer
    r = read_of(spec_for(f"{BASE}/search?q=Zqurvant+Oxlembiq"))
    r["text"] = r["main_text"] = "Das könnte Ihnen gefallen"
    j = C.judge_result_page(t, r, "Zqurvant Oxlembiq")
    assert j["status"] == "failed" and "no result names" in j["why"], j
    # no items and no statement: a failure
    r = read_of(spec_for(f"{BASE}/search?q=Zqurvant", recommend=False))
    r["text"] = r["main_text"] = "Loading..."
    j = C.judge_result_page(t, r, "Zqurvant")
    assert j["status"] == "failed" and "membership" in j["why"], j
    # off the site, an error page
    r = read_of(spec_for(f"{BASE}/search?q=Acme"))
    r["url"] = "https://elsewhere.test/search?q=Acme"
    assert C.judge_result_page(t, r, "Acme Kettle")["status"] == "failed"
    r = read_of(spec_for(f"{BASE}/search?q=Acme"))
    r["text"] = "Something went wrong. Please try again later."
    assert C.judge_result_page(t, r, "Acme Kettle")["status"] == "failed"
    # ambiguous partial matches are not guessed
    items = [{"title": "Acme Kettle Steel", "ctx": ""}, {"title": "Acme Kettle Glass", "ctx": ""}]
    el, why = C.best_match(items, "Acme Kettle 9000 Black")
    assert el is None, why


def test_product_identity_and_field_sanity():
    t = tool()
    p = read_of(spec_for(f"{BASE}/p/borealis-toaster-2"))
    f = C.extract_fields(p, t["fields"], "Borealis Toaster 2 Slice")
    assert f["name"] == "Borealis Toaster 2 Slice" and f["price"] == 29.0 and f["currency"] == "EUR"
    assert f["availability"] == "InStock" and f["url"] == f"{BASE}/p/borealis-toaster-2"
    ok, why, _ = C.check_product(p, f, "Borealis Toaster 2 Slice", "www.shop.test")
    assert ok, why
    ok, why, _ = C.check_product(p, f, "Cumulus Blender Pro", "www.shop.test")
    assert not ok and "identity" in why
    p2 = dict(p, text=p["text"].replace("29,00 €", ""), main_text=p["text"].replace("29,00 €", ""))
    ok, why, _ = C.check_product(p2, f, "Borealis Toaster 2 Slice", "www.shop.test")
    assert not ok and "not shown" in why
    lst = read_of(spec_for(f"{BASE}/search?q=Borealis"))
    lst["jsonld"] = [json.dumps({"@type": "ItemList", "itemListElement": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})]
    ok, why, _ = C.check_product(lst, f, "Borealis Toaster 2 Slice", "www.shop.test")
    assert not ok and "not an item page" in why


def test_site_search_compiles_and_the_list_must_belong_to_the_query():
    tr = product_trace(value="Cumulus Blender")
    tr["workflow"] = "site_search"
    rurl = tr["steps"][1]["page"]["url"]
    rep = {"results": [{"title": "Cumulus Blender X", "url": "/p/cumulus-blender-x"},
                       {"title": "Cumulus Blender Pro", "url": BASE + "/p/cumulus-blender-pro"}]}
    tr["steps"] = tr["steps"][:1] + [{"k": 1, "page": trace_page(rurl), "action": {"action": "done", "result": rep},
                                      "outcome": {}}]
    tr["final_page"], tr["result"] = trace_page(rurl), rep
    ok, why, _ = C.demo_validity(tr)
    assert ok, why
    got = C.compile_trace(tr)
    assert got["ok"], got
    t = got["tool"]
    j = C.judge_result_page(t, read_of(spec_for(f"{BASE}/search?q=Acme")), "Acme")
    assert j["status"] == "list" and all("Acme" in r["title"] for r in j["results"]), j
    r = read_of(spec_for(f"{BASE}/search?q=Acme"))
    for e in r["elements"]:
        if e.get("area") == "main":
            # the page shows other texts (a version-8 tool reads the title link's visible text in the DOM)
            r["dom"] = r["dom"].replace(f">{e['text']}<", ">Unrelated teaser<")
            e["text"] = "Unrelated teaser"
            e.pop("ctx", None)
    j = C.judge_result_page(t, r, "Acme")
    assert j["status"] == "failed" and "does not belong" in j["why"], j


def test_a_sidebar_with_the_results_url_pattern_is_not_a_result():
    tr = product_trace(value="Cumulus Blender")
    tr["workflow"] = "site_search"
    rurl = tr["steps"][1]["page"]["url"]
    rep = {"results": [{"title": "Cumulus Blender X", "url": BASE + "/p/cumulus-blender-x"}]}
    tr["steps"] = tr["steps"][:1] + [{"k": 1, "page": trace_page(rurl), "action": {"action": "done", "result": rep},
                                      "outcome": {}}]
    tr["final_page"], tr["result"] = trace_page(rurl), rep
    t = C.compile_trace(tr)["tool"]
    # the result structure (one reported result among 2: the title link's own chain,
    # inside the result list item), not a URL cluster
    assert t["region"]["cards"] and all("li.product" in c["chain"] for c in t["region"]["cards"]), t["region"]
    r = read_of(spec_for(f"{BASE}/search?q=Acme"))
    j = C.judge_result_page(t, r, "Acme")
    assert j["status"] == "list" and not any("Delta" in x["title"] for x in j["results"]), j
    # without the cards (and no container) the sidebar item would be listed first
    j2 = C.judge_result_page(dict(t, region={k: v for k, v in t["region"].items() if k != "cards"}), r, "Acme")
    assert j2["results"][0]["title"].startswith("Delta Mixer 7"), j2


def test_no_results_statements_in_the_round_languages():
    for s in ("No results found for zqurvant", "Keine Treffer für „zqurvant“", "Aucun résultat pour zqurvant",
              "No se han encontrado resultados", "Nessun risultato per", "Geen resultaten gevonden voor",
              "Inga träffar på", "Ingen resultater for", "Hakusi ei tuottanut tuloksia", "Brak wyników dla",
              "0 Ergebnisse", "Sorry, we couldn't find any results"):
        assert C.no_results_statement(s), s
    for s in ("Showing 24 results for kettle", "Sale (0)", "Internal Server Error: no results"):
        assert not C.no_results_statement(s), s


def test_value_templates():
    assert C.value_template(BASE + "/search?q=Acme+Kettle&page=1", "Acme Kettle") == BASE + "/search?q={q}&page=1"
    assert C.value_template(BASE + "/s/acme-kettle", "Acme Kettle") == BASE + "/s/{q}"
    assert C.value_template(BASE + "/search?q=other", "Acme Kettle") == ""
    assert C.fill(BASE + "/s/{q}", "a b/c") == BASE + "/s/a%20b%2Fc"


class FakeShopPage:
    """The fake shop in a (fake) guarded page: every read is local; ``late`` reads
    after a navigation show no result links yet (a script-rendered list);
    ``reload_on_enter``: the typed search's Enter reloads the start page
    (www.jernia.no); ``no_box``: no visible search input."""

    def __init__(self, late=0, reload_on_enter=False, no_box=False):
        self.cur, self.gotos, self.typed, self.late, self.since = "", [], [], late, 0
        self.reload_on_enter, self.no_box = reload_on_enter, no_box

    async def goto(self, url):
        self.cur = url
        self.gotos.append(url)
        self.since = 0

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import demo_tool as T
        from anyuse.execution import web_arch_run as RR
        if expr == RR.READ_JS:
            r = read_of(spec_for(self.cur))
            self.since += 1
            if self.since <= self.late and "/search" in self.cur:
                r["elements"] = [e for e in r["elements"] if e.get("area") != "main"]
                r["text"] = r["main_text"] = "Loading results"
            return r
        if expr == T.PAGE_EXTRA_JS:
            return {"meta": {}, "microdata": {}}
        if expr == T.LINK_SIG_JS:
            return sigs_of(spec_for(self.cur))
        from anyuse.execution import demo_cards as K
        # the card map of the page as CARDS_JS / CARDS_JS_V6 gives it (computed on the fake page's DOM)
        return K.recorded_answer(expr, read_of(spec_for(self.cur)))

    async def search_inputs(self):
        return [] if self.no_box else [{"index": 0, "visible": True, "name": "q", "id": "", "type": "search"}]

    async def type_search(self, value, want=None, **kw):
        self.typed.append((value, want))
        if self.no_box:
            return {"input": None, "document": "", "note": "no visible search input"}
        if not self.reload_on_enter:
            self.cur = f"{BASE}/search?q={urllib.parse.quote_plus(value)}"
            self.since = 0
        return {"input": {"index": 0, "name": "q", "id": ""}, "document": self.cur, "note": ""}

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def dom(self):
        return dom_of(spec_for(self.cur))

    async def text(self):
        return spec_for(self.cur)["text"]

    async def url(self):
        return self.cur


async def _settle(page):
    return None


def run_tool(page, t, value):
    import asyncio
    from anyuse.execution import demo_tool as T
    return asyncio.run(T.run_tool(page, t, value, _settle, result_wait_s=0.3))


def test_the_tool_runs_with_no_model_and_every_check():
    t = tool()
    page = FakeShopPage()
    out = run_tool(page, t, "Borealis Toaster 2 Slice")
    assert out["status"] == "ok" and out["accepted"] and out["model_calls"] == 0, out
    assert out["output"]["name"] == "Borealis Toaster 2 Slice" and out["output"]["price"] == 29.0
    assert out["output"]["availability"] == "InStock" and out["output"]["url"].endswith("/p/borealis-toaster-2")
    assert page.gotos == [BASE + "/search?q=Borealis+Toaster+2+Slice", BASE + "/p/borealis-toaster-2"]
    assert out["checks"]["product"]["identity"]["share"] == 1.0 and out["search_via"] == "url"
    out = run_tool(FakeShopPage(), t, "Zqurvant Oxlembiq")
    assert out["status"] == "no_results" and out["output"] == {"no_results": True} and out["accepted"], out
    out = run_tool(FakeShopPage(), t, "Gamma Grill 5")
    assert out["status"] == "no_results", out          # the site's statement, recommendations name nothing
    # a value that names only part of several items: an explicit failure, not a guess
    out = run_tool(FakeShopPage(), t, "Acme Kettle 9000 Black")
    assert out["status"] == "failed" and not out["accepted"], out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
