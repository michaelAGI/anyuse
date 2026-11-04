"""Web tool version 8, offline (a fake shop built from saved evidence; no browser, no network, no
model): when a product shows a regular / former price next to another price, the CURRENT (sale) price is the
answer; a structured price that disagrees with the visible current price is a CONFLICT.

The wrong outputs: www.calmwaters.de answered 59.99 EUR for both values - the page's "Regulärer Preis: 59,99 €*" -
where the page sells at "Verkaufspreis: 49,99 €*". The product page carries no structured price (its microdata has no
price, its Open Graph price 47.49 is the "Ab 5" tier price), so the tool compiled a TEXT price: the main text's first
currency-marked number.

What must hold:

1. on the calmwaters page a version-8 tool answers 49.99 (the compile reads the reported 49,99 back AFTER the
   product's name, with the current-price rule); the same demonstration compiled as version 7 answers 59.99, as
   measured (version 7 unchanged);
2. the regular / former / reference labels and the selling-price labels of the 11 languages, a struck-through price
   (``del`` / ``s`` / ``strike``, a line-through style), unit prices and savings: the current price is chosen only
   where a regular price is shown next to it; one price alone is never re-judged;
3. a structured price (JSON-LD offer, microdata) that agrees with the visible current price is answered; one that
   is the regular price is a CONFLICT naming both sides - a version-7 tool never runs the check;
4. a page with one visible price and a structured price checks nothing new.
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
from test_demo_compile import dom_of, read_of, sigs_of  # noqa: E402

BASE = "https://www.calmwaters.test"
SITE = "www.calmwaters.test"
#: (slug, name) - the two values' products and the demonstrated one
CATALOG = [("calmwaters-26LP6211", "WC-Sitz Motiv Seestern mit Absenkautomatik, abnehmbar, aus antibakteriellem Duroplast"),
           ("calmwaters-26LP6212", "WC-Sitz Motiv Sandstrand mit Absenkautomatik, abnehmbar, aus antibakteriellem Duroplast"),
           ("calmwaters-26LP6213", "WC-Sitz Motiv Palmenparadies aus antibakteriellem Duroplast mit Absenkautomatik, "
                                   "abnehmbar")]
NAV = [("WC-Sitz & Toilettendeckel", "/wc-sitze/"), ("Badewannen", "/badewannen/"), ("Warenkorb", "/cart")]
#: the saved product page's header (body text only: the live main text started at the breadcrumb)
HEADER = "Zum Hauptinhalt springen\nKostenloser Versand (ab 40€) & Rückversand\nWunschlieferdatum\n0\n"
#: the saved product page's buy box (both values' records), around the name and the prices
BUY_BOX = ("WC-Sitz & Toilettendeckel\nWC-Sitze mit Absenkautomatik\n{name}\nDurchschnittliche Bewertung:\n5\n"
           "Montagelevel:\nSehr leicht\nBildergalerie überspringen\nslide 1\n \n \n \n{prices}\n\nSofort versandfertig\n"
           "Lieferung ca. Di, 06.10. - Do, 08.10.\n\nPreise inkl. MwSt. & kostenloser Versand\n\nStaffelpreise\n"
           "Anzahl\tStückpreis\t\nAktion\n\nAb 1\t49,99 €*\t\n\nAb 2\t48,49 €* -1,50 €*\t\n\n"
           "Ab 5\t47,49 €* -2,50 €*\t\nIn den Warenkorb\nKostenloser Versand (ab 40€) & Rückversand")
SALE = "Regulärer Preis:\n59,99 €*\nVerkaufspreis:\n49,99 €*"
SALE_DOM = '<span class="list-price">Regulärer Preis: 59,99 €*</span><span class="price">Verkaufspreis: 49,99 €*</span>'


class Shop:
    """The fake shop: the product page's price display (text and DOM), its JSON-LD offer price and its microdata
    price (None: none, as on www.calmwaters.de)."""

    def __init__(self, prices=SALE, prices_dom=SALE_DOM, ld_price=None, md_price=None, availability="InStock",
                 buy_box=BUY_BOX):
        self.prices, self.prices_dom, self.ld_price, self.md_price = prices, prices_dom, ld_price, md_price
        self.availability, self.buy_box = availability, buy_box

    def spec(self, url):
        p = urllib.parse.urlsplit(url)
        nav = [(t, BASE + h, "nav", "") for t, h in NAV]
        if p.path in ("", "/"):
            return {"url": url, "title": "Calmwaters", "h1": ["Calmwaters"], "links": nav, "text": "Willkommen",
                    "jsonld": []}
        if p.path == "/search":
            v = dict(urllib.parse.parse_qsl(p.query)).get("search", "")
            hits = [c for c in CATALOG if C.names_value(c[1], v)]
            links = [(c[1], f"{BASE}/p/{c[0]}", "main", c[1]) for c in hits]
            return {"url": url, "title": "Suchergebnis | Calmwaters", "h1": [], "links": nav + links,
                    "text": "\n".join(c[1] for c in hits) or "Keine Ergebnisse gefunden", "jsonld": []}
        return None

    def product(self, url):
        slug = urllib.parse.urlsplit(url).path.strip("/").split("/")[-1]
        c = next(x for x in CATALOG if x[0] == slug)
        main = self.buy_box.format(name=c[1], prices=self.prices)
        jsonld = []
        if self.ld_price is not None:
            jsonld = [json.dumps({"@context": "https://schema.org", "@type": "Product", "name": c[1],
                                  "offers": {"@type": "Offer", "price": self.ld_price, "priceCurrency": "EUR",
                                             "availability": "https://schema.org/" + self.availability}})]
        md = (f'<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">{c[1]}</h1>'
              f'<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
              + (f'<meta itemprop="price" content="{self.md_price}">' if self.md_price is not None else "")
              + f'<meta itemprop="priceCurrency" content="EUR"><link itemprop="availability" '
                f'href="https://schema.org/{self.availability}"></div>{self.prices_dom}</div>')
        from anyuse.execution import web_arch as W
        return {"url": url, "title": f"{c[1]} | Calmwaters", "h1": [c[1]], "headings": [c[1]],
                "elements": [{"i": 0, "tag": "a", "text": "Warenkorb", "href": BASE + "/cart", "area": "header"}],
                "text": HEADER + main, "main_text": main, "jsonld": jsonld, "facts": W.product_facts(jsonld),
                "canonical": f"{BASE}/p/{slug}", "og_type": "product", "md_products": 1, "breadcrumb": [],
                "inputs": [], "meta": {"og:type": "product", "product:price:amount": "47.49",
                                       "product:price:currency": "EUR"},
                "microdata": {}, "link_sigs": {}, "dom": f"<html><body><main>{md}</main></body></html>"}

    def read(self, url):
        spec = self.spec(url)
        if spec is None:
            return self.product(url)
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


def trace(shop, k=2, price="49,99", availability="Sofort versandfertig"):
    """The demonstration (Sonnet's): search the Palmenparadies seat, open it, report it."""
    c = CATALOG[k]
    value = "WC-Sitz Motiv Palmenparadies aus antibakteriellem Duroplast mit Absenkautomatik"
    start = BASE + "/"
    rurl = f"{BASE}/search?search={urllib.parse.quote_plus(value)}"
    purl = f"{BASE}/p/{c[0]}"
    rep = {"name": c[1], "price": price, "currency": "EUR", "availability": availability, "url": purl}
    return {"version": 1, "workflow": "product_lookup", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": shop.read(start), "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "search"},
                                   "url_after": rurl}},
                      {"k": 1, "page": shop.read(rurl), "action": {"action": "click", "element": 4},
                       "outcome": {"ok": True, "how": "goto", "href": purl, "url_after": purl}},
                      {"k": 2, "page": shop.read(purl), "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": shop.read(purl), "result": rep, "stop": "done", "forbidden": []}


def compiled(shop=None, version=8, **kw):
    got = C.compile_trace(trace(shop or Shop(), **kw), version=version)
    assert got["ok"], got
    return got["tool"]


V1 = "WC-Sitz Motiv Seestern mit Absenkautomatik"
V2 = "WC-Sitz Motiv Sandstrand mit Absenkautomatik"


def test_prior_calmwaters_page_answers_the_sale_price_and_version_7_unchanged():
    shop = Shop()
    t8, t7 = compiled(shop), compiled(shop, version=7)
    assert t8["version"] == 8 and C.v8(t8) and t7["version"] == 7 and not C.v8(t7), (t8["version"], t7["version"])
    assert t8["fields"]["price"] == "text" and t8["fields"]["price_rule"] == "current", t8["fields"]
    assert t8["fields"]["price_at"] == "after_name", t8["fields"]
    assert "price_rule" not in t7["fields"] and t7["fields"]["price"] == "text", t7["fields"]
    for v in (V1, V2):
        r8, r7 = run(shop, t8, v), run(shop, t7, v)
        assert r8["status"] == "ok" and r8["output"]["price"] == 49.99, r8
        assert r8["output"]["currency"] == "EUR" and r8["output"]["availability"] == "InStock", r8["output"]
        assert r7["status"] == "ok" and r7["output"]["price"] == 59.99, r7          # the measured wrong output
    # the saved records keep the body text only (the header's "ab 40 €" first): after the name, the same answer
    page = shop.product(f"{BASE}/p/{CATALOG[0][0]}")
    body = dict(page, main_text="")
    assert C.extract_fields(body, t8["fields"], V1)["price"] == 49.99
    assert C.extract_fields(body, t7["fields"], V1)["price"] == 40.0                 # version 7 on the body text
    # the version-8 record keeps the main text and the struck prices
    r8 = run(shop, t8, V1)
    assert "main_text" in r8["pages"][-1] and r8["pages"][-1]["struck_prices"] == [], r8["pages"][-1].keys()
    assert "main_text" not in run(shop, t7, V1)["pages"][-1]


def cur(text, struck=()):
    got = C.current_price(text, 0, struck)
    return got["current"]["value"] if got["current"] else None


def test_regular_labels_struck_prices_units_and_savings_in_the_11_languages():
    assert cur("Regulärer Preis: 59,99 € Verkaufspreis: 49,99 €") == 49.99
    assert cur("49,99 € statt 59,99 €") == 49.99 and cur("UVP: 59,99 €\n49,99 €") == 49.99
    assert cur("Was £59.99 Now £49.99") == 49.99 and cur("RRP £59.99 £49.99") == 49.99
    assert cur("Prix barré : 59,99 € Prix promo : 49,99 €") == 49.99 and cur("49,99 € au lieu de 59,99 €") == 49.99
    assert cur("Van € 59,99 voor € 49,99") == 49.99 and cur("Adviesprijs € 59,99 € 49,99") == 49.99
    assert cur("Prezzo consigliato 59,99 € 49,99 €") == 49.99 and cur("Antes 59,99 € Ahora 49,99 €") == 49.99
    assert cur("Ordinarie pris 599 kr 499 kr") == 499.0 and cur("Vejl. pris 599 kr 499 kr") == 499.0
    assert cur("Veil. pris 599 kr 499 kr") == 499.0 and cur("Normaalihinta 59,90 € 49,90 €") == 49.9
    assert cur("Cena regularna 59,99 PLN 49,99 PLN") == 49.99
    assert cur("Niedrigster Preis der letzten 30 Tage: 54,99 € 49,99 €") == 49.99
    # a struck-through price (the page's styling) next to one that is not
    dom = '<div><del>59,99 €</del> <span>49,99 €</span><s>1,00 x</s><span style="text-decoration: line-through">' \
          '64,00 €</span></div>'
    struck = K.struck_of(dom)
    assert struck == ["59,99 €", "64,00 €"], struck
    assert cur("59,99 € 49,99 €", struck) == 49.99 and cur("59,99 € 49,99 €") is None
    assert cur("59,99 €", struck) is None                       # a struck price alone: nothing to choose
    # one price, units, savings and shipping costs are never the regular / current pair
    assert cur("49,99 €") is None and cur("49,99 € inkl. MwSt.") is None
    assert cur("statt 59,99 € nur 49,99 € (24,99 € / 1 kg)") == 49.99
    assert cur("statt 59,99 € Sie sparen 10,00 € 49,99 €") == 49.99
    assert cur("UVP 59,99 € zzgl. 4,90 € Versandkosten") is None
    kinds = [m["kind"] for m in C.price_marks("UVP 59,99 € 49,99 € (24,99 € / 1 kg) Sie sparen 10,00 €")]
    assert kinds == ["regular", "", "unit", "other"], kinds


def test_a_structured_price_against_the_visible_current_price_is_a_conflict():
    # the microdata states the REGULAR price: CONFLICT, both sides named; the version-7 tool answers it as before
    shop = Shop(md_price="59.99")
    t8, t7 = compiled(Shop(md_price="49.99")), compiled(Shop(md_price="49.99"), version=7)
    assert t8["fields"]["price"] == "microdata" and t7["fields"]["price"] == "microdata", t8["fields"]
    r8 = run(shop, t8, V1)
    assert r8["status"] == "failed" and set(r8["conflicts"]) == {"price"}, r8
    ev = r8["conflicts"]["price"]
    assert ev["visible"] == 49.99 and ev["regular"] == [59.99] and ev["compiled_source"] == "microdata", ev
    assert {(s["source"], s["value"]) for s in ev["structured"]} == {("microdata", 59.99)}, ev
    assert r8["fields_seen"]["price"] == T.CONFLICT and "price: microdata 59.99 vs visible 49.99" in r8["why"], r8
    r7 = run(shop, t7, V1)
    assert r7["status"] == "ok" and r7["output"]["price"] == 59.99, r7
    # JSON-LD stating the regular price while the microdata states the current one: a conflict too
    r = run(Shop(md_price="49.99", ld_price=59.99), t8, V1)
    assert r["status"] == "failed" and "jsonld 59.99" in r["why"], r
    # every structured price the current price: answered
    r = run(Shop(md_price="49.99", ld_price=49.99), t8, V1)
    assert r["status"] == "ok" and r["output"]["price"] == 49.99 and not r.get("conflicts"), r
    # the check is version 8's: field_conflicts without v8 never names the price
    page = shop.product(f"{BASE}/p/{CATALOG[0][0]}")
    page["microdata"] = K.microdata_of(page["dom"])
    f = C.extract_fields(page, t8["fields"], V1)
    assert "price" in C.field_conflicts(page, f, t8["fields"], V1, v8=True)
    assert "price" not in C.field_conflicts(page, f, t8["fields"], V1)


def test_one_visible_price_with_a_structured_price_checks_nothing_new():
    one = Shop(prices="49,99 €*", prices_dom='<span class="price">49,99 €*</span>', md_price="49.99")
    t8 = compiled(one)
    r = run(one, t8, V2)
    assert r["status"] == "ok" and r["output"]["price"] == 49.99 and not r.get("conflicts"), r
    # the tier table's 47,49 (a visible price) as the structured price, no regular price shown: version 7's rule
    tier = Shop(prices="49,99 €*", prices_dom='<span class="price">49,99 €*</span>', md_price="47.49")
    r = run(tier, t8, V2)
    assert r["status"] == "ok" and r["output"]["price"] == 47.49, r
    # a text price with one price in its display reads it as version 7 did (after the name)
    t = compiled(Shop(prices="49,99 €*", prices_dom='<span class="price">49,99 €*</span>'))
    assert t["fields"]["price"] == "text" and t["fields"]["price_at"] == "after_name", t["fields"]
    r = run(Shop(prices="49,99 €*", prices_dom=""), t, V1)
    assert r["status"] == "ok" and r["output"]["price"] == 49.99, r


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
