"""Field conflicts, offline (a fake shop built from saved evidence; no browser, no network, no
model): a field on which the page's STRUCTURED data and its VISIBLE text disagree is CONFLICT - the
tool's output is a failure for that field, never either side silently.

The wrong outputs: www.session.de answered availability "LimitedAvailability" for both values from the
product page's schema.org JSON-LD offer (the compiled source) while the same page's visible text says "Online
sofort lieferbar"; the saved product page carries TWO JSON-LD Product blocks for the one product
(LimitedAvailability and InStock). The demonstration itself had reported "Online sofort lieferbar" against
the same LimitedAvailability source.

What must hold:

1. schema.org availability values are normalized to a small vocabulary before comparing (a URL or a
   bare name, any case; SoldOut / Discontinued are out of stock, PreSale is a pre-order, ...);
2. on the recorded page the version-6 tool answers a FAILURE with availability CONFLICT and both
   sides' evidence (the structured statements with their sources, the visible statement); a
   version-5 tool still answers as it did;
3. structured data that disagree among themselves (the two JSON-LD blocks) are a conflict too;
4. agreeing sides, or a page whose text states nothing (www.poco.de), are no conflict;
5. the compile notes the demonstrator's reported availability disagreeing with the compiled source;
6. a structured name that shares no word with the page's heading while the heading names the input
   better (www.ambientedirect.com: the nested brand's name) is a name CONFLICT.
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

BASE = "https://www.session.test"
SITE = "www.session.test"
#: (slug, name, price) - the two values, their B-stock twins and the demonstrated product
CATALOG = [("line-6-pod-express-guitar", "Line 6 POD Express Guitar", 159.0),
           ("line-6-pod-express-guitar-b-ware", "Line 6 POD Express Guitar B-Ware", 139.0),
           ("kong-nightshift-power-soak", "Kong NightShift Power Soak", 149.0),
           ("kong-nightshift-power-soak-b-ware", "Kong NightShift Power Soak B-Ware", 129.0),
           ("universal-audio-uafx-knuckles-92-rev-f-dual-rec-amplifier",
            "Universal Audio UAFX Knuckles '92 Rev F Dual Rec Amplifier", 319.0)]
NAV = [("Gitarren", "/gitarren/"), ("Bässe", "/baesse/"), ("Warenkorb", "/cart")]
#: the saved product page's visible text (the lines around the offer)
VISIBLE_IN_STOCK = "Art.-Nr.: 10033009\n{price} inkl. MwSt.\nversandkostenfrei\n✅ Online sofort lieferbar\n" \
                   "Bestellungen bis 13 Uhr: Versand meist noch am gleichen Tag\nIN DEN WARENKORB"


def eur(p):
    return f"{p:.2f}".replace(".", ",") + " €"


def ld(name, price, availability, url=""):
    return json.dumps({"@context": "https://schema.org", "@type": "Product", "name": name, "brand": "Line 6",
                       "sku": "10033009", **({"url": url} if url else {}),
                       "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR",
                                  "availability": "https://schema.org/" + availability}})


class Shop:
    """The fake shop: per product the JSON-LD availabilities (in page order) and the visible text."""

    def __init__(self, blocks=("LimitedAvailability", "InStock"), visible=VISIBLE_IN_STOCK):
        self.blocks, self.visible = blocks, visible

    def spec(self, url):
        p = urllib.parse.urlsplit(url)
        nav = [(t, BASE + h, "nav", "") for t, h in NAV]
        if p.path in ("", "/"):
            return {"url": url, "title": "session", "h1": ["session"], "links": nav, "text": "Willkommen bei session",
                    "jsonld": []}
        if p.path == "/search":
            v = dict(urllib.parse.parse_qsl(p.query)).get("search", "")
            hits = [c for c in CATALOG if C.name_share(c[1], v) >= 0.75]
            links = [(c[1], f"{BASE}/products/{c[0]}/", "main", f"{c[1]} {eur(c[2])}") for c in hits]
            return {"url": url, "title": "Suchergebnis | session", "h1": [], "links": nav + links,
                    "text": "\n".join(f"{c[1]} {eur(c[2])}" for c in hits) or "Keine Ergebnisse gefunden", "jsonld": []}
        slug = p.path.strip("/").split("/")[-1]
        c = next(x for x in CATALOG if x[0] == slug)
        purl = f"{BASE}/products/{slug}/"
        blocks = [ld(c[1], c[2], a, purl if k else "") for k, a in enumerate(self.blocks)]
        text = f"Home\nGitarren\n{c[1]}\n{c[1]}\n" + self.visible.format(price=eur(c[2]))
        return {"url": url, "title": f"{c[1]} kaufen | Jetzt bei session!", "h1": [c[1]], "links": nav, "text": text,
                "jsonld": blocks, "canonical": purl, "og_type": "product"}

    def read(self, url):
        r = read_of(self.spec(url))
        r["dom"] = dom_of(self.spec(url))
        return r


class FakePage:
    def __init__(self, shop, microdata=None):
        self.shop, self.cur, self.gotos, self.microdata = shop, "", [], microdata or {}

    async def goto(self, url):
        self.cur = url
        self.gotos.append(url)

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        r = self.shop.read(self.cur)
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom", "link_sigs")}
        if expr in (T.PAGE_EXTRA_JS, getattr(T, "PAGE_EXTRA_JS_V6", None)):
            return {"meta": {}, "microdata": dict(self.microdata) if "/products/" in self.cur else {}}
        if expr == T.LINK_SIG_JS:
            return sigs_of(self.shop.spec(self.cur))
        if expr == T.PRICED_LINKS_JS:
            return {}
        return K.recorded_answer(expr, r)

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur


async def _settle(page):
    return None


def run(page, tool, value):
    return asyncio.run(T.run_tool(page, tool, value, _settle, result_wait_s=0.3))


def trace(shop):
    """The demonstration (www.session.de's): search, open the product, report it."""
    c = CATALOG[-1]
    value = c[1]
    start = BASE + "/"
    rurl = f"{BASE}/search?search={urllib.parse.quote_plus(value)}"
    purl = f"{BASE}/products/{c[0]}/"
    rep = {"name": c[1], "price": "319,00", "currency": "EUR", "availability": "Online sofort lieferbar", "url": purl}
    return {"version": 1, "workflow": "product_lookup", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": shop.read(start), "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "search"},
                                   "url_after": rurl}},
                      {"k": 1, "page": shop.read(rurl), "action": {"action": "click", "element": 4},
                       "outcome": {"ok": True, "how": "goto", "href": purl, "url_after": purl}},
                      {"k": 2, "page": shop.read(purl), "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": shop.read(purl), "result": rep, "stop": "done", "forbidden": []}


def compiled(shop=None):
    # version 10: a "limited" structured class against a visible "in stock" is a CONFLICT (version 11: a few
    # left is in stock - test_demo_lowstock.py)
    got = C.compile_trace(trace(shop or Shop(blocks=("LimitedAvailability",))), version=10)
    assert got["ok"], got
    return got


def test_schema_availability_is_normalized_to_a_small_vocabulary():
    cases = {"https://schema.org/InStock": "in_stock", "http://schema.org/InStock": "in_stock", "InStock": "in_stock",
             "instock": "in_stock", "OnlineOnly": "in_stock", "https://schema.org/LimitedAvailability": "limited",
             "InStoreOnly": "store_only", "OutOfStock": "out_of_stock", "SoldOut": "out_of_stock",
             "Discontinued": "out_of_stock", "PreOrder": "preorder", "PreSale": "preorder", "BackOrder": "backorder",
             "MadeToOrder": "backorder", "": "", "Available": "", "limited": "limited"}
    for raw, want in cases.items():
        assert C.availability_class(raw) == want, (raw, C.availability_class(raw), want)
    assert set(C.AVAILABILITY) >= {v for v in cases.values() if v}
    assert C.text_availability_class("✅ Online sofort lieferbar") == "in_stock"
    assert C.text_availability_class("Leider ausverkauft") == "out_of_stock"
    assert C.text_availability_class("Nur noch 2 Stück auf Lager") == "limited"
    assert C.text_availability_class("Gitarren und Bässe") == ""


def test_the_prior_page_is_a_conflict_never_either_side():
    got = compiled()
    tool = got["tool"]
    assert tool["version"] == 10 and tool["fields"]["availability"] == "jsonld", tool["fields"]
    value = CATALOG[0][1]
    out = run(FakePage(Shop()), tool, value)
    assert out["status"] == "failed" and not out["accepted"] and out["output"] is None, out
    assert out["fields_seen"]["availability"] == T.CONFLICT and out["fields_seen"]["price"] == 159.0, out
    ev = out["conflicts"]["availability"]
    assert [(s["source"], s["value"], s["class"]) for s in ev["structured"]] == \
        [("jsonld", "LimitedAvailability", "limited"), ("jsonld", "InStock", "in_stock")], ev
    assert ev["visible"] == "in_stock" and "sofort lieferbar" in ev["visible_statement"], ev
    assert ev["why"] == "the page's structured data and its visible text disagree; the page's structured data disagree", ev
    assert "LimitedAvailability" in out["why"] and "visible in_stock" in out["why"], out["why"]
    # the second value the same
    out2 = run(FakePage(Shop()), tool, CATALOG[2][1])
    assert out2["status"] == "failed" and out2["fields_seen"]["availability"] == T.CONFLICT, out2
    # the version-5 runtime is unchanged: it answered LimitedAvailability (the measured wrong output)
    old = run(FakePage(Shop()), dict(tool, version=5), value)
    assert old["status"] == "ok" and old["output"]["availability"] == "LimitedAvailability" and "conflicts" not in old


def test_structured_data_disagreeing_among_themselves_is_a_conflict():
    tool = compiled()["tool"]
    shop = Shop(blocks=("LimitedAvailability", "InStock"), visible="{price} inkl. MwSt.\nIN DEN WARENKORB")
    out = run(FakePage(shop), tool, CATALOG[0][1])
    assert out["status"] == "failed" and out["fields_seen"]["availability"] == T.CONFLICT, out
    assert out["conflicts"]["availability"]["visible"] == "" and \
        out["conflicts"]["availability"]["why"] == "the page's structured data disagree", out


def test_agreeing_sides_or_a_silent_text_are_no_conflict():
    tool = compiled()["tool"]
    out = run(FakePage(Shop(blocks=("InStock",))), tool, CATALOG[0][1])
    assert out["status"] == "ok" and out["output"]["availability"] == "InStock", out
    # www.poco.de: JSON-LD InStock, the product page's text states nothing - no conflict; a version-7
    # tool answers InStock, a version-8 tool "not stated" (an unsupported structured class)
    silent = Shop(blocks=("InStock",), visible="{price}\nIN DEN WARENKORB")
    tool7 = C.compile_trace(trace(Shop(blocks=("LimitedAvailability",))), version=7)["tool"]
    out = run(FakePage(silent), tool7, CATALOG[0][1])
    assert out["status"] == "ok" and out["output"]["availability"] == "InStock", out
    out = run(FakePage(silent), tool, CATALOG[0][1])
    assert out["status"] == "ok" and out["output"]["availability"] == "not stated" and not out.get("conflicts"), out
    out = run(FakePage(Shop(blocks=("OutOfStock", "SoldOut"), visible="{price}\nLeider ausverkauft")), tool,
              CATALOG[0][1])
    assert out["status"] == "ok" and out["output"]["availability"] == "OutOfStock", out
    out = run(FakePage(Shop(blocks=("LimitedAvailability",), visible="{price}\nNur noch 2 Stück auf Lager")), tool,
              CATALOG[0][1])
    assert out["status"] == "ok" and out["output"]["availability"] == "LimitedAvailability", out


def test_the_compile_notes_the_reported_availability_against_its_source():
    notes = compiled()["report"]["field_notes"]
    assert any("'Online sofort lieferbar' (in_stock) disagrees with the page's jsonld (limited)" in n
               for n in notes), notes


def test_a_nested_name_against_the_heading_is_a_name_conflict():
    # www.ambientedirect.com: no JSON-LD product, the microdata name source read the nested
    # brand's name "Knoll International"; the heading is the product's own name
    page = {"url": BASE + "/p/1", "title": "Knoll International Saarinen Esstisch Ø91cm | AmbienteDirect",
            "h1": ["Saarinen Esstisch Ø91cm"], "facts": [], "text": "Saarinen Esstisch Ø91cm 2.240,00 € Auf Lager",
            "microdata": {"name": "Knoll International", "price": "2240", "currency": "EUR", "availability": ""}}
    srcs = {"name": "microdata", "price": "microdata", "availability": "text", "url": "location"}
    value = "Knoll International Saarinen Esstisch Ø91cm"
    fields = C.extract_fields(page, srcs, value)
    assert fields["name"] == "Knoll International", fields
    conf = C.field_conflicts(page, fields, srcs, value)
    assert set(conf) == {"name"} and conf["name"]["visible"] == "Saarinen Esstisch Ø91cm", conf
    # the product's own name agrees with the heading: no conflict
    ok = dict(page, microdata=dict(page["microdata"], name="Saarinen Esstisch Ø91cm"))
    assert C.field_conflicts(ok, C.extract_fields(ok, srcs, value), srcs, value) == {}
    # a heading that names the input no better than the structured name is not a conflict
    other = dict(page, h1=["Sale"], title="Sale")
    assert "name" not in C.field_conflicts(other, fields, srcs, value)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
