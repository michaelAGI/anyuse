"""Web tool version 9, offline (a fake shop built from saved evidence; no browser, no network, no
model): the answer's URL is the URL actually LOADED; the page's canonical link is answered only when it demonstrably
names the SAME product (the clean URL of the page loaded, or a link carrying the product's SKU / GTIN, or a slug
naming every word of the product's name and its distinguishing words); otherwise it is kept as evidence only.

The wrong outputs:

* www.kamdi24.de: the tool opened ``.../kamdi24-geschenkgutschein-feuer-100`` (and ``...-grillfleisch-250``)
  and read its name and price right, but answered the page's canonical link ``.../kamdi24-geschenkgutschein-feuer-25``
  - the shop's voucher pages all declare the 25 EUR voucher as canonical (``og:url`` names the page itself);
* www.rowertour.com: the tool opened the yellow variant ``.../p/g8866/soczewki-accent-shot?variant=283749``
  (32.26 PLN) and answered the family's canonical URL, which shows the default clear variant at 29.26 PLN.

What must hold:

1. a version-9 tool answers the loaded URL where the canonical names another product (another voucher's slug) or
   drops the loaded URL's variant parameter; the canonical is kept in ``checks.url``; the same demonstrations compiled
   as version 8 answer the canonical, as measured (version 8 unchanged);
2. the normal case: a canonical that is the clean URL of the page loaded (only tracking / session parameters
   differ) is answered; a canonical carrying the product's SKU, or a slug naming the product's every word and number,
   is answered too;
3. the rule on the saved page records (kamdi24, rowertour).
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from test_demo_compile import dom_of, read_of, sigs_of  # noqa: E402

BASE = "https://www.shop9.test"
SITE = "www.shop9.test"
NAV = [("Gutscheine", "/gutscheine/"), ("Warenkorb", "/cart")]
HEADER = "Zum Hauptinhalt springen\nKontakt\nMein Konto\n"


def product(slug, name, price, **kw):
    """One product of the fake shop. ``query``: the parameters of the URL its result link opens; ``canonical``: its
    page's canonical link (default: its clean URL); ``tile``: its title in the result list (default: its name);
    ``buy``: the buy box's lines after the name (text); ``buy_dom``: the buy box's DOM (default: the lines as <p>);
    ``sku`` / ``gtin`` / ``color`` / ``size`` / ``availability``: its JSON-LD; ``md_availability``: its microdata;
    ``brand``: its JSON-LD brand."""
    p = {"slug": slug, "name": name, "price": price, "query": "", "canonical": None, "tile": name,
         "buy": ["{price} €", "Auf Lager", "Lieferzeit: 1-3 Werktage"], "buy_dom": None, "sku": "", "gtin": "",
         "color": "", "size": "", "availability": "InStock", "md_availability": "", "brand": "", "in_list": True}
    p.update(kw)
    return p


def eur(x):
    return f"{x:.2f}".replace(".", ",")


class Shop:
    """A fake shop: a start page, a search listing every catalog product (in catalog order) as a result tile, and
    each product's own page (JSON-LD, canonical link, buy box text and DOM)."""

    def __init__(self, catalog):
        self.catalog = catalog

    def url_of(self, p):
        return f"{BASE}/p/{p['slug']}" + (f"?{p['query']}" if p["query"] else "")

    def spec(self, url):
        sp = urllib.parse.urlsplit(url)
        nav = [(t, BASE + h, "nav", "") for t, h in NAV]
        if sp.path in ("", "/"):
            return {"url": url, "title": "Shop9", "h1": ["Shop9"], "links": nav, "text": "Willkommen", "jsonld": []}
        if sp.path == "/search":
            v = dict(urllib.parse.parse_qsl(sp.query)).get("q", "")
            hits = [p for p in self.catalog if p["in_list"]] if v.strip() else []
            links = [(p["tile"], self.url_of(p), "main", f"{p['tile']} {eur(p['price'])} €") for p in hits]
            return {"url": url, "title": "Suche | Shop9", "h1": [], "links": nav + links,
                    "text": "\n".join(p["tile"] for p in hits) or "Keine Ergebnisse gefunden", "jsonld": []}
        return None

    def find(self, url):
        sp = urllib.parse.urlsplit(url)
        slug = sp.path.strip("/").split("/")[-1]
        same = [p for p in self.catalog if p["slug"] == slug]
        return next((p for p in same if p["query"] == sp.query), same[0] if same else None)

    def product_page(self, url):
        p = self.find(url)
        lines = [ln.format(price=eur(p["price"])) for ln in p["buy"]]
        main = "Startseite\n" + p["name"] + "\n" + "\n".join(lines)
        ld = {"@context": "https://schema.org", "@type": "Product", "name": p["name"],
              "offers": {"@type": "Offer", "price": p["price"], "priceCurrency": "EUR"}}
        if p["availability"]:
            ld["offers"]["availability"] = "https://schema.org/" + p["availability"]
        for k in ("sku", "gtin", "color", "size"):
            if p[k]:
                ld[k] = p[k]
        if p["brand"]:
            ld["brand"] = {"@type": "Brand", "name": p["brand"]}
        jsonld = [json.dumps(ld)]
        buy_dom = p["buy_dom"] or "".join(f"<p>{ln}</p>" for ln in lines)
        md = ""
        if p["md_availability"]:
            md = (f'<div itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="{p["name"]}">'
                  f'<link itemprop="availability" href="https://schema.org/{p["md_availability"]}"></div>')
        dom = (f"<html><body><header><a href=\"{BASE}/cart\">Warenkorb</a></header><main><nav>Startseite</nav>"
               f"<h1>{p['name']}</h1>{buy_dom}{md}</main></body></html>")
        return {"url": url, "title": f"{p['name']} | Shop9", "h1": [p["name"]], "headings": [p["name"]],
                "elements": [{"i": 0, "tag": "a", "text": "Warenkorb", "href": BASE + "/cart", "area": "header"}],
                "text": HEADER + main, "main_text": main, "jsonld": jsonld, "facts": W.product_facts(jsonld),
                "canonical": p["canonical"] or f"{BASE}/p/{p['slug']}", "og_type": "product", "md_products": 0,
                "breadcrumb": [], "inputs": [], "meta": {"og:type": "product", "og:url": url},
                "microdata": {}, "link_sigs": {}, "dom": dom}

    def read(self, url):
        spec = self.spec(url)
        if spec is None:
            return self.product_page(url)
        r = read_of(spec)
        r["dom"] = dom_of(spec)
        return r


class FakePage:
    def __init__(self, shop):
        self.shop, self.cur, self.gotos = shop, "", []

    async def goto(self, url):
        self.cur = url
        self.gotos.append(url)

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        r = self.shop.read(self.cur)
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom", "link_sigs", "facts")}
        if expr == T.PAGE_EXTRA_JS:
            return {"meta": dict(r.get("meta") or {}), "microdata": {}}
        if expr == T.LINK_SIG_JS:
            spec = self.shop.spec(self.cur)
            return sigs_of(spec) if spec else {}
        if expr == T.PRICED_LINKS_JS:
            return {}
        return K.recorded_answer(expr, r)

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur


async def _settle(page):
    return None


def run(shop, tool, value):
    return asyncio.run(T.run_tool(FakePage(shop), tool, value, _settle, result_wait_s=0.3))


def trace(shop, k, value=None, availability="Auf Lager"):
    """The demonstration: search for catalog product ``k``'s name (or ``value``), open it, report it."""
    p = shop.catalog[k]
    value = value or p["name"]
    start = BASE + "/"
    rurl = f"{BASE}/search?q={urllib.parse.quote_plus(value)}"
    purl = shop.url_of(p)
    rep = {"name": p["name"], "price": eur(p["price"]), "currency": "EUR", "availability": availability, "url": purl}
    rpage = shop.read(rurl)
    el = next(e["i"] for e in rpage["elements"] if e.get("href") == purl)
    return {"version": 1, "workflow": "product_lookup", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": shop.read(start), "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"},
                                   "url_after": rurl}},
                      {"k": 1, "page": rpage, "action": {"action": "click", "element": el},
                       "outcome": {"ok": True, "how": "goto", "href": purl, "url_after": purl}},
                      {"k": 2, "page": shop.read(purl), "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": shop.read(purl), "result": rep, "stop": "done", "forbidden": []}


def compiled(shop, k=0, version=None, **kw):
    got = C.compile_trace(trace(shop, k, **kw), version=version)
    assert got["ok"], got
    return got["tool"]


# ---------------------------------------------------------------------------
# the two shops
# ---------------------------------------------------------------------------

KAMDI = BASE + "/p/kamdi24-geschenkgutschein-feuer-25"


def kamdi_shop():
    """www.kamdi24.de: every voucher page declares the 25 EUR "Feuer" voucher's page as its canonical link."""
    return Shop([product("kamdi24-geschenkgutschein-feuer-25", "kamdi24 Geschenkgutschein Feuer 25 €", 25.0,
                         tile="kamdi24 Geschenkgutschein Feuer 25 €"),
                 product("kamdi24-geschenkgutschein-feuer-100", "kamdi24 Geschenkgutschein Feuer 100 €", 100.0,
                         canonical=KAMDI),
                 product("kamdi24-geschenkgutschein-grillfleisch-250", "kamdi24 Geschenkgutschein Grillfleisch 250", 250.0,
                         canonical=KAMDI)])


ROWER = BASE + "/p/soczewki-accent-shot"


def rower_shop():
    """www.rowertour.com: the colour variants of one family share its name; each variant's URL carries
    ``?variant=``, every variant page declares the family URL (the default variant's page) as canonical."""
    return Shop([product("soczewki-accent-shot", "Soczewki Accent Shot", 32.26, query="variant=283749", sku="283749",
                         color="żółty", canonical=ROWER),
                 product("soczewki-accent-shot", "Soczewki Accent Shot", 29.26, query="variant=283750", sku="283750",
                         color="przezroczysty", canonical=ROWER)])


def test_prior_canonical_of_another_product_is_evidence_only_and_version_8_unchanged():
    shop = kamdi_shop()
    t9, t8 = compiled(shop, 0, version=9), compiled(shop, 0, version=8)
    assert t9["version"] == 9 and C.v9(t9) and t8["version"] == 8 and not C.v9(t8), (t9["version"], t8["version"])
    assert t9["fields"]["url_rule"] == "same_product" and "url_rule" not in t8["fields"], (t9["fields"], t8["fields"])
    for value, want in (("kamdi24 Geschenkgutschein Feuer 100", "feuer-100"),
                        ("kamdi24 Geschenkgutschein Grillfleisch 250", "grillfleisch-250")):
        r9, r8 = run(shop, t9, value), run(shop, t8, value)
        assert r9["status"] == "ok" and r9["output"]["url"] == f"{BASE}/p/kamdi24-geschenkgutschein-{want}", r9
        ev = r9["checks"]["url"]
        assert ev["canonical"] == KAMDI and ev["same_product"] is False and ev["answered"] == r9["output"]["url"], ev
        assert r8["status"] == "ok" and r8["output"]["url"] == KAMDI, r8                 # the measured wrong output
        assert r8["output"]["price"] == r9["output"]["price"] and "url" not in r8["checks"], r8


def test_prior_canonical_dropping_the_variant_parameter_is_evidence_only():
    shop = rower_shop()
    t9, t8 = compiled(shop, 0), compiled(shop, 0, version=8)
    r9, r8 = run(shop, t9, "Soczewki Accent Shot"), run(shop, t8, "Soczewki Accent Shot")
    assert r9["status"] == "ok" and r9["output"]["url"] == ROWER + "?variant=283749", r9
    assert r9["output"]["price"] == 32.26 and "variant" in r9["checks"]["url"]["why"], r9["checks"]["url"]
    assert r8["status"] == "ok" and r8["output"]["url"] == ROWER and r8["output"]["price"] == 32.26, r8


def test_the_normal_case_canonical_is_the_clean_url_of_the_same_product():
    # the result link carries tracking / session parameters; the canonical is the page's clean URL: answered
    shop = Shop([product("acme-kettle-2000", "Acme Kettle 2000 Steel", 49.0, query="utm_source=search&sid=a1b2c3")])
    t9 = compiled(shop, 0)
    r9 = run(shop, t9, "Acme Kettle 2000 Steel")
    assert r9["status"] == "ok" and r9["output"]["url"] == BASE + "/p/acme-kettle-2000", r9
    assert r9["checks"]["url"]["same_product"] is True and "clean URL" in r9["checks"]["url"]["why"], r9["checks"]
    # no parameter at all: the canonical IS the URL loaded - nothing to record
    shop = Shop([product("acme-kettle-2000", "Acme Kettle 2000 Steel", 49.0)])
    r9 = run(shop, compiled(shop, 0), "Acme Kettle 2000 Steel")
    assert r9["output"]["url"] == BASE + "/p/acme-kettle-2000" and "url" not in r9["checks"], r9
    # another path: the canonical carries the product's SKU, or its slug names the product's every word and number
    page = {"url": BASE + "/catalog/view/id/90417?cat=12", "canonical": BASE + "/acme-kettle-2000-steel-90417.html",
            "facts": [{"type": "Product", "name": "Acme Kettle 2000 Steel", "sku": "90417", "gtin": ""}], "h1": []}
    srcs = {"url": "canonical", "url_rule": "same_product"}
    url, ev = C.answer_url(page, {"name": "Acme Kettle 2000 Steel"}, srcs)
    assert ev["same_product"] is False and url == page["url"], ev       # cat=12 is not a tracking parameter
    page["url"] = BASE + "/catalog/view/id/90417"
    url, ev = C.answer_url(page, {"name": "Acme Kettle 2000 Steel"}, srcs)
    assert url == page["canonical"] and "SKU" in ev["why"], ev
    page = dict(page, canonical=BASE + "/acme-kettle-2000-steel", facts=[])
    url, ev = C.answer_url(page, {"name": "Acme Kettle 2000 Steel"}, srcs)
    assert url == page["canonical"] and ev["same_product"] is True, ev
    url, ev = C.answer_url(dict(page, canonical=BASE + "/acme-kettle-3000-steel"), {"name": "Acme Kettle 2000 Steel"},
                           srcs)
    assert url == page["url"] and ev["same_product"] is False, ev
    # a tool without the version-9 rule reads the canonical as before
    assert C.extract_fields(page, {"url": "canonical"}, "Acme Kettle 2000 Steel")["url"] == page["canonical"]


def test_the_rule_on_prior_saved_page_records():
    # www.kamdi24.de's saved product page record (pages[1])
    kamdi = {"url": "https://www.kamdi24.de/kamdi24-geschenkgutschein-feuer-100",
             "title": "kamdi24 Geschenkgutschein Feuer 100 € | kamdi24", "h1": ["KAMDI24 GESCHENKGUTSCHEIN FEUER 100 €"],
             "canonical": "https://www.kamdi24.de/kamdi24-geschenkgutschein-feuer-25",
             "meta": {"og:type": "product", "og:url": "https://www.kamdi24.de/kamdi24-geschenkgutschein-feuer-100",
                      "og:title": "kamdi24 Geschenkgutschein Feuer 100 € | kamdi24", "product:price:amount": "100",
                      "product:price:currency": "EUR"},
             "facts": [], "microdata": {"name": "", "price": "100", "currency": "EUR",
                                        "availability": "https://schema.org/InStock"},
             "text": "KAMDI24 GESCHENKGUTSCHEIN FEUER 100 €\n100,00 €*", "main_text": ""}
    srcs8 = {"name": "og", "price": "og", "availability": "microdata", "availability_vocab": "8", "url": "canonical"}
    v = "kamdi24 Geschenkgutschein Feuer"
    assert C.extract_fields(kamdi, srcs8, v)["url"] == kamdi["canonical"]                # version 8: the measured answer
    got = C.extract_fields(kamdi, dict(srcs8, url_rule="same_product"), v)
    assert got["url"] == kamdi["url"] and got["price"] == 100.0, got
    _u, ev = C.answer_url(kamdi, got, dict(srcs8, url_rule="same_product"))
    assert ev["same_product"] is False and "25" in ev["why"], ev
    # www.rowertour.com's saved record (pages[1]): the variant URL vs the family's canonical
    rower = {"url": "https://www.rowertour.com/p/g8866/soczewki-accent-shot?variant=283749",
             "canonical": "https://www.rowertour.com/p/g8866/soczewki-accent-shot", "h1": ["Soczewki Accent Shot"],
             "title": "Soczewki Accent Shot - Rowertour.com",
             "facts": [{"type": "Product", "name": "Soczewki Accent Shot", "brand": "Accent", "sku": "283749",
                        "gtin": "5902891396420", "color": "żółty", "price": 32.26, "currency": "PLN",
                        "availability": "InStock", "offers": [], "variants": []}],
             "meta": {"og:url": "https://www.rowertour.com/p/g8866/soczewki-accent-shot"}, "microdata": {},
             "text": "Soczewki Accent Shot\n32,26 zł", "main_text": ""}
    srcs = {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical", "url_rule": "same_product"}
    got = C.extract_fields(rower, srcs, "Soczewki Accent Shot")
    assert got["url"] == rower["url"] and got["price"] == 32.26, got
    srcs.pop("url_rule")
    assert C.extract_fields(rower, srcs, "Soczewki Accent Shot")["url"] == rower["canonical"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
