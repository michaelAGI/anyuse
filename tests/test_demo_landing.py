"""Version 7: a product lookup whose search lands STRAIGHT on a product's own page. Built from saved evidence of
www.skybad.de: the site's search for "Duravit ME by Starck Hero Edition Wand-WC Set 490709AAA1 spülrandlos" went to
that product's page (one schema.org Product, 398,16 EUR, InStock) - no result list; version 6 waited 8 s for result
items and failed ("membership: no item of the compiled region ..."). Offline (the fake shop of
test_demo_compile.py; no browser, no network, no model).

What must hold:

1. the search lands on a product-detail page whose OWN name names the input: a version-7 run answers that page's
   fields (``picked.landed``), the result wait settles at once; a version-6 run keeps the measured failure;
2. an identity mismatch is an explicit failure: the page is another product's, or a search page that echoes the
   query in its heading / title while its one product is another (the echo never passes the landing check);
3. a page that is not one product's (a list, an undecided page without the compiled structure) keeps the
   membership failure; a "no results" statement keeps "no results";
4. undecided by ``product_detail`` (two JSON-LD products), the compiled product-page structure (the tool's JSON-LD
   name source names the input's product, its price source a price) is a landing too; a CONFLICT on the landed
   page is a failure, as on an opened one.
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402

VALUE = "Duravit ME by Starck Hero Edition Wand-WC Set 490709AAA1 spülrandlos"
SKY = ("duravit-me-by-starck-hero-edition-wand-wc-set-490709aaa1-spuelrandlos",
       "Duravit ME by Starck Hero Edition Wand-WC Set 490709AAA1  spülrandlos, DuraFlush, 340 x 540 mm, Weiß glänzend",
       398.16, "InStock")


class Catalog:
    def __init__(self, extra):
        self.extra = extra

    def __enter__(self):
        self.old = list(TC.CATALOG)
        TC.CATALOG[:] = self.old + list(self.extra)

    def __exit__(self, *a):
        TC.CATALOG[:] = self.old


class LandingShop(TC.FakeShopPage):
    """The fake shop whose search for a value it maps (``lands``) goes straight to that product's page (a redirect)."""

    def __init__(self, lands, **kw):
        super().__init__(**kw)
        self.lands = lands

    async def goto(self, url):
        await TC.FakeShopPage.goto(self, url)
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query)).get("q", "")
        if p.path == "/search" and q in self.lands:
            self.cur = f"{TC.BASE}/p/{self.lands[q]}"


def run(page, t, value, wait=0.3):
    import asyncio
    return asyncio.run(T.run_tool(page, t, value, TC._settle, result_wait_s=wait))


def test_a_search_that_lands_on_the_inputs_product_is_answered():
    t = TC.tool()
    with Catalog([SKY]):
        out = run(LandingShop({VALUE: SKY[0]}), t, VALUE, wait=1.0)
        assert out["status"] == "ok" and out["accepted"], out
        assert out["output"]["name"] == SKY[1] and out["output"]["price"] == 398.16, out["output"]
        assert out["output"]["availability"] == "InStock" and out["output"]["url"].endswith(SKY[0]), out["output"]
        assert out["picked"]["landed"] and out["checks"]["product"]["landing"]["share"] == 1.0, out
        assert out["checks"]["result_page"]["status"] == "landed", out["checks"]["result_page"]
        assert "landed on the product's own page" in out["why"], out["why"]
        assert out["results_after_s"] is not None and out["wait_trail"][-1][3] == 1, out["wait_trail"]
        # version 6: the same page is the measured failure, after the whole wait
        out6 = run(LandingShop({VALUE: SKY[0]}), dict(t, version=6), VALUE, wait=0.3)
        assert out6["status"] == "failed" and out6["why"].startswith("membership: no item of the compiled region"), out6
        assert out6["results_after_s"] is None and len(out6["wait_trail"][0]) == 3, out6["wait_trail"]


def test_an_identity_mismatch_is_a_failure():
    t = TC.tool()
    with Catalog([SKY]):
        # the site sends the search to ANOTHER product's page
        out = run(LandingShop({VALUE: "acme-kettle-3000"}), t, VALUE)
        assert out["status"] == "failed" and not out["accepted"], out
        assert out["why"].startswith("identity: the search landed on a product page whose own name names 0%"), out["why"]
        assert out["picked"]["landed"] and out["fields_seen"]["name"] == "Acme Kettle 3000 Glass", out
    # a search page echoing the query (heading, title) whose ONE product is another: never the input's product
    r = TC.read_of(TC.spec_for(f"{TC.BASE}/p/acme-kettle-3000"))
    r.update(url=f"{TC.BASE}/search?q=Acme+Kettle+2000+Steel", title="Search: Acme Kettle 2000 Steel",
             h1=["Search: Acme Kettle 2000 Steel"], cards={"n": 0, "map": {}, "cards": []})
    j = C.judge_result_page(t, r, "Acme Kettle 2000 Steel")
    assert j["status"] == "landed", j
    f = C.extract_fields(r, t["fields"], "Acme Kettle 2000 Steel")
    ok, why, checks = C.check_landing(r, f, "Acme Kettle 2000 Steel", "www.shop.test")
    assert not ok and "own name names" in why and checks["identity"]["share"] == 1.0, (why, checks)


def test_not_a_product_page_keeps_the_version_6_answers():
    t = TC.tool()
    # a list page with no item of the region and no statement: the membership failure, never a landing
    r = TC.read_of(TC.spec_for(f"{TC.BASE}/search?q=Zqurvant", recommend=False))
    r["text"] = r["main_text"] = "Loading..."
    for tt in (t, dict(t, version=6)):
        j = C.judge_result_page(tt, r, "Zqurvant")
        assert j["status"] == "failed" and j["why"].startswith("membership"), j
    lst = dict(r, jsonld=[json.dumps({"@type": "ItemList", "itemListElement": [{"name": "a"}, {"name": "b"}]})],
               facts=[{"name": "Zqurvant A", "price": 1.0}, {"name": "Zqurvant B", "price": 2.0}, {"name": "C"}])
    assert C.landed_product(t, lst, "Zqurvant") == "" and C.judge_result_page(t, lst, "Zqurvant")["status"] == "failed"
    # the site's own "no results" statement stays "no results"
    r = TC.read_of(TC.spec_for(f"{TC.BASE}/search?q=Zqurvant+Oxlembiq", recommend=False))
    assert C.judge_result_page(t, r, "Zqurvant Oxlembiq")["status"] == "no_results"
    # a site-search tool never lands
    assert C.landed_product(dict(t, workflow="site_search"), TC.read_of(TC.spec_for(f"{TC.BASE}/p/acme-kettle-2000")),
                            "Acme Kettle 2000 Steel") == ""


def test_the_compiled_structure_and_a_conflict_on_a_landed_page():
    t = TC.tool()
    assert t["fields"]["name"] == "jsonld" and t["fields"]["price"] == "jsonld", t["fields"]
    r = TC.read_of(TC.spec_for(f"{TC.BASE}/p/acme-kettle-2000"))
    acc = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "Acme Descaler 250 ml",
                      "offers": {"@type": "Offer", "price": 4.5, "priceCurrency": "EUR"}})
    from anyuse.execution import web_arch as W
    r.update(jsonld=r["jsonld"] + [acc], og_type="", cards={"n": 0, "map": {}, "cards": []})
    r["facts"] = W.product_facts(r["jsonld"])
    assert W.product_detail(r, "Acme Kettle 2000 Steel")[0] is None
    why = C.landed_product(t, r, "Acme Kettle 2000 Steel")
    assert why.startswith("the compiled product-page structure (jsonld name and jsonld price)"), why
    assert C.judge_result_page(t, r, "Acme Kettle 2000 Steel")["status"] == "landed"
    assert C.landed_product(dict(t, fields=dict(t["fields"], name="h1")), r, "Acme Kettle 2000 Steel") == ""
    # a landed page whose structured availability and visible text disagree: CONFLICT, a failure (pinned to version 11:
    # version 12 answers a visible "Ausverkauft" against a structured InStock - test_demo_notnow.py)
    t = C.compile_trace(TC.product_trace(), version=11)["tool"]
    with Catalog([SKY]):
        page = LandingShop({VALUE: SKY[0]})
        orig = TC.spec_for

        def spec_out_of_stock(url, recommend=True):
            s = orig(url, recommend)
            if url.endswith(SKY[0]):
                s["text"] = s["text"].replace("Auf Lager", "Ausverkauft")
            return s
        TC.spec_for = spec_out_of_stock
        try:
            out = run(page, t, VALUE)
        finally:
            TC.spec_for = orig
        assert out["status"] == "failed" and out["why"].startswith("conflict: availability"), out
        assert out["fields_seen"]["availability"] == T.CONFLICT and out["picked"]["landed"], out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
