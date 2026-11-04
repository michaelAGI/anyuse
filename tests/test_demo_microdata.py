"""Microdata names, offline (fake pages of the recorded www.ambientedirect.com structure; no browser, no
network, no model): the product NAME comes from the Product item's OWN ``itemprop=name``, never from a
nested item (brand, manufacturer, offer).

The wrong output: www.ambientedirect.com answered the name "Knoll International" for "Knoll International
Saarinen Esstisch Ø91cm": the page has no JSON-LD product, and the microdata read took the FIRST
``itemprop=name`` inside the Product scope - the nested brand item's ``<meta itemprop="name"
content="Knoll International">`` - before the product's own ``<h1 itemprop="name">``. The structure below
is the one of the demonstration's recorded product page: a WebPage item on ``<body>``, breadcrumb
ListItems with their own names, the Product item with a nested Brand item, the product's own heading, and
several Offer items (variants).

What must hold:

1. the reading of a recorded DOM (``demo_cards.microdata_of``, the mirror of the in-page
   ``PAGE_EXTRA_JS_V6``) gives the Product's own name; price, currency and availability come from the
   product's own offers; the version-5 reading (the first descendant) gave the brand's;
2. a version-6 tool answers the product's own name on the recorded page; a version-5 tool still reads as
   it did;
3. a Product whose only ``itemprop=name`` is a nested item's has no own name: the heading is used,
   never the nested name; multi-token ``itemprop`` values count;
4. the compile reads the demonstration's product page the same way.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_cards import read_of_html  # noqa: E402

BASE = "https://www.ambiente.test"
SITE = "www.ambiente.test"
PRODUCTS = {"saarinen-esstisch-o91cm_pid_1266.html": ("Saarinen Esstisch Ø91cm", [2240]),
            "florence-knoll-couchtisch-quadratisch_pid_1268183.html": ("Florence Knoll Couchtisch quadratisch",
                                                                       [712, 878, 1177])}


def eur(p):
    return f"{p:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def product_html(slug):
    name, prices = PRODUCTS[slug]
    crumbs = "".join(f'<li itemprop="itemListElement" itemscope itemtype="http://schema.org/ListItem">'
                     f'<a href="{h}" itemprop="item"><span itemprop="name">{t}</span></a>'
                     f'<meta itemprop="position" content="{k + 1}"></li>'
                     for k, (t, h) in enumerate([("Home", "/"), ("Hersteller", "/hersteller"),
                                                 ("Knoll International", "/hersteller/knoll-international")]))
    offers = "".join(f'<div class="variant{" active" if k == len(prices) - 1 else ""}" itemprop="offers" itemscope '
                     f'itemtype="http://schema.org/Offer"><div class="prices">{eur(p)}</div>'
                     f'<meta itemprop="priceCurrency" content="EUR"><meta itemprop="price" content="{p}">'
                     f'<link itemprop="availability" href="http://schema.org/InStock"></div>'
                     for k, p in enumerate(prices))
    return (f'<html><head><title>Knoll International {name} | AmbienteDirect</title></head>'
            f'<body class="frontend" itemscope itemtype="http://schema.org/WebPage">'
            f'<header><nav><a href="/kategorien/moebel">Möbel</a></nav></header>'
            f'<ul class="breadcrumb" itemscope itemtype="http://schema.org/BreadcrumbList">{crumbs}</ul>'
            f'<main><div class="snippetProductsDetailDetailPage" itemscope itemtype="http://schema.org/Product">'
            f'<span itemprop="brand" itemscope itemtype="http://schema.org/Brand">'
            f'<a href="/hersteller/knoll-international" itemprop="url"><img alt=" Logo" itemprop="image" src="data:,"></a>'
            f'<meta itemprop="name" content="Knoll International"></span>'
            f'<div class="current-selection-details"><h1 itemprop="name">{name}</h1></div>'
            f'<p>Artikel-ID 224116 Auf Lager</p>{offers}<button>In den Warenkorb</button></div></main></body></html>')


def result_html(value):
    hits = [(s, n) for s, (n, _p) in PRODUCTS.items() if C.name_share("Knoll International " + n, value) >= 0.75]
    q = urllib.parse.quote(value)
    tiles = "".join(f'<div class="product-tile"><a class="tile-link" href="/knoll-international/{s}?q={q}">'
                    f'Knoll International {n}</a><div class="price">{eur(PRODUCTS[s][1][-1])}</div></div>'
                    for s, n in hits)
    other = ('<div class="product-tile"><a class="tile-link" href="/knoll-international/womb-chair_pid_1255445.html">'
             'Knoll International Womb Chair Relax</a><div class="price">5.000,00 €</div></div>')
    return (f'<html><head><title>Suchergebnisse | AmbienteDirect</title></head><body><header><nav>'
            f'<a href="/kategorien/moebel">Möbel</a></nav></header><main><h1>Suchergebnisse</h1>'
            f'<div class="results">{tiles}{other}</div></main></body></html>')


def page_read(url):
    p = urllib.parse.urlsplit(url)
    slug = p.path.rsplit("/", 1)[-1]
    html = product_html(slug) if slug in PRODUCTS else result_html(dict(urllib.parse.parse_qsl(p.query)).get("q", ""))
    r = read_of_html(html, url)
    root, _b = K.parse(html)
    r["h1"] = [K.text_of(n) for n in K.walk(root) if n.tag == "h1"]
    r["og_type"] = "product" if slug in PRODUCTS else ""
    r["md_products"] = 1 if slug in PRODUCTS else 0
    return r


def first_descendant(dom, prop):
    """The version-5 reading (``PAGE_EXTRA_JS``): the FIRST element carrying ``itemprop=prop`` inside the scope."""
    root, _b = K.parse(dom)
    scope = next(n for n in K.walk(root) if "schema.org/Product" in (n.attrs.get("itemtype") or ""))
    n = next(x for x in K.walk(scope) if (x.attrs.get("itemprop") or "") == prop)
    return K._md_value(n)


class FakePage:
    def __init__(self):
        self.cur, self.gotos = "", []

    async def goto(self, url):
        self.cur = url
        self.gotos.append(url)

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        r = page_read(self.cur)
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom", "link_sigs")}
        if expr == T.PAGE_EXTRA_JS:
            md = {}
            if "schema.org/Product" in r["dom"]:
                md = {p: first_descendant(r["dom"], p) for p in ("name", "price", "priceCurrency", "availability")}
                md["currency"] = md.pop("priceCurrency")
            return {"meta": {}, "microdata": md}
        if expr in (T.LINK_SIG_JS, T.PRICED_LINKS_JS):
            return {}
        return K.recorded_answer(expr, r)

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur


def trace():
    """The demonstration (the Florence Knoll table): search, open the product, report it."""
    value = "Knoll International Florence Knoll Couchtisch quadratisch"
    start = BASE + "/"
    rurl = f"{BASE}/suchergebnisse?q={urllib.parse.quote_plus(value)}"
    slug = "florence-knoll-couchtisch-quadratisch_pid_1268183.html"
    href = f"{BASE}/knoll-international/{slug}?q={urllib.parse.quote(value)}"
    purl = href
    rep = {"name": value, "price": "1.177,00", "currency": "EUR", "availability": "nur 2 Stück auf Lager",
           "url": f"{BASE}/knoll-international/{slug}"}
    home = read_of_html("<html><body><header><nav><a href=\"/kategorien/moebel\">Möbel</a></nav></header>"
                        "<main><h1>AmbienteDirect</h1></main></body></html>", start)
    return {"version": 1, "workflow": "product_lookup", "site": SITE, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": home, "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}},
                      {"k": 1, "page": page_read(rurl), "action": {"action": "click", "element": 1},
                       "outcome": {"ok": True, "how": "goto", "href": href, "url_after": purl}},
                      {"k": 2, "page": dict(page_read(purl), microdata={"name": "Knoll International", "price": "712",
                                                                      "currency": "EUR", "availability": ""}),
                       "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": dict(page_read(purl), microdata={"name": "Knoll International", "price": "712",
                                                          "currency": "EUR", "availability": ""}),
            "result": rep, "stop": "done", "forbidden": []}


def run(tool, value):
    return asyncio.run(T.run_tool(FakePage(), tool, value, lambda p: asyncio.sleep(0), result_wait_s=0.3))


def test_the_products_own_name_never_a_nested_items():
    dom = product_html("saarinen-esstisch-o91cm_pid_1266.html")
    md = K.microdata_of(dom)
    assert md == {"name": "Saarinen Esstisch Ø91cm", "price": "2240", "currency": "EUR",
                  "availability": "http://schema.org/InStock"}, md
    # the version-5 reading: the first itemprop=name in the scope is the nested brand's
    assert first_descendant(dom, "name") == "Knoll International"
    # several offers (variants): the product's first own offer, as before (the active one is the text's)
    md2 = K.microdata_of(product_html("florence-knoll-couchtisch-quadratisch_pid_1268183.html"))
    assert md2["name"] == "Florence Knoll Couchtisch quadratisch" and md2["price"] == "712", md2
    # the in-page read carries the same rule (nearest item scope, own offers)
    js = T.PAGE_EXTRA_JS_V6
    assert "closest('[itemscope]')" in js and "owner(n) === item" in js and "indexOf('offers')" in js
    assert K.recorded_answer(js, {"dom": dom, "meta": {"og:type": "product"}}) == \
        {"meta": {"og:type": "product"}, "microdata": md}


def test_a_nested_name_only_is_no_name_and_multi_token_props_count():
    dom = ('<div itemscope itemtype="https://schema.org/Product"><div itemprop="manufacturer" itemscope '
           'itemtype="https://schema.org/Organization"><span itemprop="name">Acme Corp</span></div>'
           '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><span itemprop="name">Offer A</span>'
           '<span itemprop="price" content="9.5">9,50</span></div></div>')
    md = K.microdata_of(dom)
    assert md["name"] == "" and md["price"] == "9.5", md
    page = {"url": BASE + "/p", "h1": ["Acme Widget"], "title": "Acme Widget", "facts": [], "microdata": md,
            "text": "Acme Widget 9,50 €"}
    assert C.extract_fields(page, {"name": "microdata", "price": "microdata"}, "Acme Widget")["name"] == "Acme Widget"
    dom2 = '<div itemscope itemtype="http://schema.org/Product"><h2 itemprop="headline name">Acme Widget</h2></div>'
    assert K.microdata_of(dom2)["name"] == "Acme Widget"
    assert K.microdata_of("<div>no item</div>") == {}


def test_the_v6_tool_answers_the_products_own_name_on_the_prior_page():
    got = C.compile_trace(trace())
    assert got["ok"], got
    tool = got["tool"]
    assert tool["version"] >= 6 and tool["fields"]["name"] == "microdata", tool["fields"]
    value = "Knoll International Saarinen Esstisch Ø91cm"
    out = run(tool, value)
    assert out["status"] == "ok" and out["output"]["name"] == "Saarinen Esstisch Ø91cm", out
    assert out["output"]["price"] == 2240.0 and out["output"]["currency"] == "EUR", out
    # the version-5 runtime is unchanged: the brand's name (the measured wrong output)
    old = run(dict(tool, version=5), value)
    assert old["status"] == "ok" and old["output"]["name"] == "Knoll International", old


def test_the_compile_reads_the_demonstrations_page_the_same_way():
    tr = trace()
    got = C.compile_trace(tr)
    # the recorded microdata named the brand; the compile re-reads the recorded DOM
    assert tr["final_page"]["microdata"]["name"] == "Knoll International"
    assert got["ok"] and got["tool"]["fields"]["name"] == "microdata", got
    fin = dict(tr["final_page"], microdata=K.microdata_of(tr["final_page"]["dom"]))
    fields = C.extract_fields(fin, got["tool"]["fields"], tr["value"])
    assert fields["name"] == "Florence Knoll Couchtisch quadratisch", fields


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
