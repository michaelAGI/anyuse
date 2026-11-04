"""Web tool version 10, offline (a fake shop; no browser, no network, no model): a FILTERED / SORTED
search demonstrated once and compiled as bound parameters.

The demonstration searches, then sets a sort order (a drop-down: "Preis aufsteigend") or a filter (a brand link
"Bosch (3)", a checkbox "Nur auf Lager") on the result list. The compiler binds it as the URL parameters the
refinement set (``refine.mode == "url"``: applied to the search's own result URL at run time), else as the controls
found again by their labels (``"controls"``: a list re-rendered by script, the URL unchanged). Every run checks the
list: the sorted order on the prices / dates the result cards show; the filter value named by every result or shown
as an active-filter chip; a URL-bound filter still in the URL loaded. A check that fails is a failure, one that
cannot be verified INCOMPLETE - never a silent answer.

What must hold:

1. a sort by price (drop-down, URL parameter) compiles to ``refine.mode == "url"`` with ``{"sort": "price-asc"}``, and
   replays on new values sorted (verified on the cards' prices: the sale price, never the struck regular one); a site
   that ignores the parameter fails the check;
2. a brand filter (a link) is verified on every result's title or the active-filter chip; a filter dropped by the
   site fails; an in-stock filter (a checkbox) fails when a listed result is shown out of stock;
3. a list re-rendered by script (the URL unchanged) compiles to the controls and replays by finding the drop-down's
   option again; a missing control is an explicit failure;
4. the validity rules (no refinement, an unchanged page: invalid) and versions < 10 refusing the workflow;
5. the pure readers: sort keys and parameter classes in the 11 languages, card prices and dates.
"""
import asyncio
import html
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_breadth as DB  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

BASE = "https://www.bshop.test"
SITE = "www.bshop.test"
PER = 4


def eur(x):
    return f"{x:.2f}".replace(".", ",")


#: (slug, name, price, regular price or None, date, brand, in stock)
CATALOG = [
    ("bosch-akkuschrauber-gsr-12", "Bosch Akkuschrauber GSR 12", 89.0, 109.0, "2025-03-12", "Bosch", True),
    ("makita-akkuschrauber-df333", "Makita Akkuschrauber DF333", 79.5, None, "2025-05-02", "Makita", True),
    ("bosch-akkuschrauber-easydrill", "Bosch Akkuschrauber EasyDrill", 64.9, None, "2024-11-20", "Bosch", False),
    ("einhell-akkuschrauber-te-cd", "Einhell Akkuschrauber TE-CD", 49.99, 59.99, "2025-06-01", "Einhell", True),
    ("bosch-akkuschrauber-psr-18", "Bosch Akkuschrauber PSR 18", 129.0, None, "2025-01-15", "Bosch", True),
    ("metabo-akkuschrauber-bs-18", "Metabo Akkuschrauber BS 18", 149.0, None, "2024-09-30", "Metabo", True),
    ("ryobi-akkuschrauber-r18", "Ryobi Akkuschrauber R18", 99.0, None, "2025-02-11", "Ryobi", False),
    ("bosch-bohrhammer-pbh", "Bosch Bohrhammer PBH 2100", 119.0, None, "2025-04-04", "Bosch", True),
    ("makita-bohrhammer-hr2470", "Makita Bohrhammer HR2470", 159.0, None, "2024-12-24", "Makita", True),
    ("einhell-bohrhammer-tc-rh", "Einhell Bohrhammer TC-RH 620", 69.0, None, "2025-05-20", "Einhell", True),
    ("bosch-stichsaege-pst", "Bosch Stichsäge PST 650", 59.0, None, "2025-03-30", "Bosch", True),
]
SORTS = [("", "Relevanz"), ("price-asc", "Preis aufsteigend"), ("price-desc", "Preis absteigend"), ("newest", "Neueste")]


class Shop:
    """A fake shop: start page, search (sort / brand / in-stock parameters, pages of ``PER``), product pages.
    ``xhr``: the sort drop-down and the in-stock checkbox re-render the list by script (the URL unchanged);
    ``ignore_sort``: the site ignores the sort parameter; ``drop_brand``: it drops the brand parameter (redirect)."""

    def __init__(self, xhr=False, ignore_sort=False, drop_brand=False, repeat_last=False, per=PER, pager="page",
                 total=True, conflict="", long_cards=False, nav=True):
        self.xhr, self.ignore_sort, self.drop_brand, self.repeat_last = xhr, ignore_sort, drop_brand, repeat_last
        self.per, self.pager, self.total, self.conflict = per, pager, total, conflict
        self.long_cards, self.nav = long_cards, nav

    # -- the search's results
    def hits(self, q, sort="", brand="", instock=""):
        words = W.content_tokens(q)
        hs = [c for c in CATALOG if words and all(C.names_all_words(c[1], w) if hasattr(C, "names_all_words") else
                                                  W.names_all_compound(c[1], w) for w in words)]
        if brand:
            hs = [c for c in hs if c[5].lower() == brand.lower()]
        if instock:
            hs = [c for c in hs if c[6]]
        if sort and not self.ignore_sort:
            if sort == "price-asc":
                hs = sorted(hs, key=lambda c: c[2])
            elif sort == "price-desc":
                hs = sorted(hs, key=lambda c: -c[2])
            elif sort == "newest":
                hs = sorted(hs, key=lambda c: c[4], reverse=True)
        return hs

    def card(self, c):
        reg = f'<del class="old">{eur(c[3])} €</del> ' if c[3] else ""
        y, m, d = c[4].split("-")
        return (f'<li class="product-item"><a class="title" href="/p/{c[0]}">{html.escape(c[1])}</a>'
                f'<div class="price">{reg}<span class="now">{eur(c[2])} €</span></div>'
                f'<div class="date">{d}.{m}.{y}</div><div class="stock">{"Auf Lager" if c[6] else "Ausverkauft"}</div>'
                + (f'<p class="desc">{"Robust, leise und ausdauernd. " * 16}</p>' if self.long_cards else "") + '</li>')

    def search_dom(self, url, state):
        q = state.get("q", "")
        hs = self.hits(q, state.get("sort", ""), state.get("brand", ""), state.get("instock", ""))
        page = int(state.get("page") or state.get("p") or 1) if str(state.get("page") or state.get("p") or "1").isdigit() else 1
        if self.pager == "cursor":
            page = {"": 1, "c2": 2, "c3": 3}.get(state.get("cursor", ""), 1)
        last = max(1, -(-len(hs) // self.per))
        if self.repeat_last and page > last:
            page = last
        shown = hs[(page - 1) * self.per: page * self.per]
        base = {k: v for k, v in state.items() if k not in ("page", "p", "cursor") and v}
        brands = sorted({c[5] for c in self.hits(q)})
        facets = "".join(f'<li><a href="/search?{urllib.parse.urlencode(dict(base, brand=b.lower()))}">{b} '
                         f'({sum(1 for c in self.hits(q) if c[5] == b)})</a></li>' for b in brands)
        opts = "".join(f'<option value="{v}"{" selected" if state.get("sort", "") == v else ""}>{t}</option>'
                       for v, t in SORTS)
        chk = " checked" if state.get("instock") else ""
        chips = ""
        if state.get("brand"):
            nob = {k: v for k, v in base.items() if k != "brand"}
            b = next((c[5] for c in CATALOG if c[5].lower() == state["brand"]), state["brand"])
            chips = f'<div class="active-filters"><a class="chip" href="/search?{urllib.parse.urlencode(nob)}">{b} ×</a></div>'
        nav = ""
        if (page < last or self.repeat_last) and self.nav:
            if self.pager == "cursor":
                nxt = urllib.parse.urlencode(dict(base, cursor=f"c{page + 1}"))
            else:
                nxt = urllib.parse.urlencode(dict(base, page=page + 1))
            nums = "".join(f'<a href="/search?{urllib.parse.urlencode(dict(base, page=k))}">{k}</a>'
                           for k in range(1, last + 1) if k != page) if self.pager == "page" else ""
            nav = f'<nav class="pagination">{nums}<a rel="next" href="/search?{nxt}">Weiter</a></nav>'
        total = (f'<p class="count">{(page - 1) * self.per + 1}–{(page - 1) * self.per + len(shown)} von {len(hs)} '
                 f'Treffern</p>') if self.total and hs else ""
        body = (f'<h1>Suchergebnisse für „{html.escape(q)}“</h1>{chips}<aside class="filters"><h3>Marke</h3><ul>{facets}'
                f'</ul><label><input type="checkbox" name="instock"{chk}>Nur auf Lager</label></aside>'
                f'<label for="s">Sortieren nach</label><select id="s" name="sort">{opts}</select>{total}'
                + (f'<ul class="products">{"".join(self.card(c) for c in shown)}</ul>' if shown else
                   "<p>Leider keine Ergebnisse gefunden.</p>") + nav)
        return self.wrap(f"Suche: {q}", body)

    def wrap(self, title, main):
        return (f'<html><head><title>{html.escape(title)} | BShop</title></head><body><header><a href="/">BShop</a>'
                f'<a href="/cart">Warenkorb</a><form action="/search"><input name="q"></form></header><main>{main}'
                f'</main><footer><a href="/impressum">Impressum</a></footer></body></html>')

    def product(self, slug):
        c = next(x for x in CATALOG if x[0] == slug)
        stock = "Auf Lager" if c[6] else "Ausverkauft"
        if self.conflict == slug:
            stock = "Ausverkauft"
        ld = {"@context": "https://schema.org", "@type": "Product", "name": c[1], "brand": {"@type": "Brand", "name": c[5]},
              "offers": {"@type": "Offer", "price": c[2], "priceCurrency": "EUR",
                         "availability": "https://schema.org/" + ("InStock" if c[6] else "OutOfStock")}}
        main = (f'<h1>{html.escape(c[1])}</h1><p class="price">{eur(c[2])} €</p><p>{stock}</p>'
                f'<button>In den Warenkorb</button><p>Lieferzeit: 1-3 Werktage</p>')
        dom = self.wrap(c[1], main).replace("</head>", f'<script type="application/ld+json">{json.dumps(ld)}</script></head>')
        return dom, [json.dumps(ld)]

    def dom(self, url, state=None):
        sp = urllib.parse.urlsplit(url)
        if sp.path in ("", "/"):
            return self.wrap("BShop", '<h1>Willkommen bei BShop</h1><p>Werkzeug online kaufen</p>'), []
        if sp.path == "/search":
            st = dict(urllib.parse.parse_qsl(sp.query))
            if self.drop_brand:
                st.pop("brand", None)
            st.update(state or {})
            return self.search_dom(url, st), []
        if sp.path.startswith("/p/"):
            return self.product(sp.path[3:])
        return self.wrap("404", "<h1>Seite nicht gefunden</h1>"), []

    def read(self, url, state=None):
        dom, ld = self.dom(url, state)
        loaded = url
        if self.drop_brand and "brand=" in url:
            q = [(k, v) for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query) if k != "brand"]
            loaded = BASE + "/search?" + urllib.parse.urlencode(q)
        return read_of_dom(loaded, dom, ld)


def read_of_dom(url, dom, jsonld=()):
    """READ_JS's view of a DOM: every control with its label, link, rel, type, checked state and a select's options;
    the visible text, the headings, the JSON-LD."""
    root, base = K.parse(dom)
    els = []
    for k, n in enumerate(K.control_nodes(root)):
        e = {"i": k, "tag": n.tag, "text": K.label_of(n), "area": K.area_of(n), "role": n.attrs.get("role") or "",
             "type": (n.attrs.get("type") or "").lower(), "y": 100 + k}
        if n.tag == "a":
            e["href"] = K.href_of(n, url, base)
            e["rel"] = (n.attrs.get("rel") or "").lower()
            # READ_JS's card context: the closest list item / article / card-like ancestor's text (<= 400 characters)
            x = n.parent
            while x is not None and not x.tag.startswith("#") and x.tag not in ("li", "article") and \
                    not re.search(r"card|product|item|tile|result", x.attrs.get("class") or ""):
                x = x.parent
            if x is not None and not x.tag.startswith("#") and x.tag != "body":
                ct = K.text_of(x, 1000)
                if ct and len(ct) <= 400 and ct != e["text"]:
                    e["ctx"] = ct[:200]
        if n.tag == "input" and e["type"] in ("checkbox", "radio"):
            e["checked"] = "checked" in n.attrs
            lab = n.parent if n.parent is not None and n.parent.tag == "label" else None
            e["text"] = K.text_of(lab, 160) if lab is not None else e["text"]
        if n.tag == "select":
            e["options"] = [{"value": o.attrs.get("value", ""), "text": K.text_of(o, 60), "selected": "selected" in o.attrs}
                            for o in K.walk(n) if o.tag == "option"]
            lab = next((x for x in K.walk(root) if x.tag == "label" and x.attrs.get("for") == n.attrs.get("id")), None)
            e["text"] = K.text_of(lab, 160) if lab is not None else (n.attrs.get("name") or "")
        els.append(e)
    main = next((x for x in K.walk(root) if x.tag == "main"), root)
    h1 = [K.text_of(x, 200) for x in K.walk(root) if x.tag == "h1"]
    title = next((K.text_of(x, 200) for x in K.walk(root) if x.tag == "title"), "")
    seps = K.sep_text_of(dom)
    return {"url": url, "title": title, "h1": h1, "headings": h1, "elements": els, "text": seps["text"],
            "main_text": seps["main_text"], "jsonld": list(jsonld), "facts": W.product_facts(list(jsonld)),
            "canonical": "", "og_type": "product" if jsonld else "", "md_products": 0, "breadcrumb": [],
            "inputs": [{"index": 0, "visible": True, "name": "q"}], "meta": {}, "microdata": {}, "link_sigs": {},
            "dom": dom}


class FakePage:
    """The shop behind ``run_tool``'s duck-typed page: navigation, the page readers, clicks and drop-down choices
    (a script-rendered list keeps its URL: ``Shop.xhr``)."""

    def __init__(self, shop):
        self.shop, self.cur, self.state, self.gotos = shop, BASE + "/", {}, []

    def read(self):
        return self.shop.read(self.cur, self.state)

    async def goto(self, url):
        self.cur, self.state = urllib.parse.urljoin(self.cur, url), {}
        self.gotos.append(self.cur)

    async def settle(self, s):
        return None

    async def js(self, expr, read_only=False):
        r = self.read()
        if expr == RR.READ_JS:
            return {k: v for k, v in r.items() if k not in ("dom", "link_sigs", "facts")}
        if expr == T.LINK_SIG_JS:
            return {}
        if expr == T.PRICED_LINKS_JS:
            return {}
        m = re.search(r"\}\)\((\d+), (\"(?:[^\"\\\\]|\\\\.)*\")\)\s*$", str(expr))
        if m and ("n.click()" in expr or "dispatchEvent(new Event('change'" in expr):
            return self.act(int(m.group(1)), json.loads(m.group(2)), "change" in expr)
        return K.recorded_answer(expr, r)

    def act(self, k, arg, select):
        r = self.read()
        e = next((x for x in r["elements"] if x["i"] == k), None)
        if e is None:
            return {"ok": False, "why": "gone"}
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.cur).query))
        if select and e["tag"] == "select":
            if self.shop.xhr:
                self.state["sort"] = arg
            else:
                q["sort"] = arg
                self.cur = BASE + "/search?" + urllib.parse.urlencode({kk: v for kk, v in q.items() if v})
            return {"ok": True, "value": arg}
        if e["tag"] == "input":
            if self.shop.xhr:
                self.state["instock"] = "" if self.state.get("instock") else "1"
            else:
                q["instock"] = "" if q.get("instock") else "1"
                self.cur = BASE + "/search?" + urllib.parse.urlencode({kk: v for kk, v in q.items() if v})
            return {"ok": True}
        if e.get("href"):
            self.cur, self.state = e["href"], {}
        return {"ok": True}

    async def type_search(self, value, want=None, **kw):
        self.cur, self.state = BASE + "/search?" + urllib.parse.urlencode({"q": value}), {}
        return {"input": want or {"index": 0}, "document": self.cur, "note": ""}

    async def search_inputs(self):
        return [{"index": 0, "visible": True, "name": "q"}]

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur

    async def dom(self):
        return self.read()["dom"]


async def _settle(page):
    return None


def run(shop, tool, value, params=None):
    return asyncio.run(T.run_tool(FakePage(shop), tool, value, _settle, result_wait_s=0.2, params=params))


def el_of(page, pred):
    return next(e for e in page["elements"] if pred(e))


def mapping(e):
    return {str(e["i"]): {"src": "el", "i": e["i"], "text": e.get("text"), "href": e.get("href", ""), "tag": e["tag"]}}


def step(k, page, action, outcome, e=None):
    st = {"k": k, "page": page, "action": action, "outcome": outcome}
    if e is not None:
        st["mapping"] = mapping(e)
    return st


def reported(page, shop=None, n=5, extra=()):
    out = []
    for e in page["elements"]:
        if e["tag"] == "a" and "/p/" in (e.get("href") or "") and len(out) < n:
            out.append(dict({"title": e["text"], "url": e["href"]}, **dict(extra)))
    return out


def filtered_trace(shop, value="Akkuschrauber", how="sort", option="Preis aufsteigend"):
    """The demonstration: search, then sort by the drop-down (``how="sort"``), follow the brand link ``option``
    (``"brand"``) or tick the in-stock checkbox (``"stock"``); report the top results and what it applied."""
    pg = FakePage(shop)
    start = BASE + "/"
    p0 = shop.read(start)
    asyncio.run(pg.type_search(value))
    rurl = pg.cur
    p1 = pg.read()
    steps = [step(0, p0, {"action": "search", "element": 0, "text": value},
                  {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl})]
    if how == "sort":
        e = el_of(p1, lambda x: x["tag"] == "select")
        opt = next(o for o in e["options"] if o["text"] == option)
        asyncio.run(pg.js(RR.SELECT_JS % (e["i"], json.dumps(opt["value"]))))
        steps.append(step(1, p1, {"action": "select", "element": e["i"], "option": option},
                          {"ok": True, "how": "select", "option_text": option, "option_value": opt["value"],
                           "url_after": pg.cur}, e))
        applied = [{"kind": "sort", "control": "Sortieren nach", "option": option}]
    elif how == "brand":
        e = el_of(p1, lambda x: x["tag"] == "a" and x["text"].startswith(option))
        asyncio.run(pg.goto(e["href"]))
        steps.append(step(1, p1, {"action": "click", "element": e["i"]},
                          {"ok": True, "how": "goto", "href": e["href"], "url_after": pg.cur}, e))
        applied = [{"kind": "filter", "control": "Marke", "value": option}]
    else:
        e = el_of(p1, lambda x: x["tag"] == "input" and x["type"] == "checkbox")
        asyncio.run(pg.js(RR.CLICK_JS % (e["i"], json.dumps(e["text"]))))
        steps.append(step(1, p1, {"action": "click", "element": e["i"]},
                          {"ok": True, "how": "click", "url_after": pg.cur}, e))
        applied = [{"kind": "filter", "control": "Nur auf Lager"}]
    fin = pg.read()
    rep = {"results": reported(fin), "applied": applied}
    steps.append(step(2, fin, {"action": "done", "result": rep}, {}))
    return {"version": 1, "workflow": "filtered_search", "site": SITE, "start_url": start, "value": value,
            "steps": steps, "final_page": fin, "result": rep, "stop": "done", "forbidden": []}


def titles(out):
    return [r["title"] for r in (out.get("output") or {}).get("results") or []]


def test_a_price_sort_compiles_as_a_url_parameter_and_is_verified():
    shop = Shop()
    g = C.compile_trace(filtered_trace(shop), version=10)
    assert g["ok"], g.get("why")
    t = g["tool"]
    assert t["version"] == 10 and t["workflow"] == "filtered_search" and t["refine"] == {"mode": "url", "params": {"sort": "price-asc"}}, t
    assert t["applied"][0]["kind"] == "sort" and t["applied"][0]["key"] == "price_asc", t["applied"]
    assert t["region"]["cards_version"] == 10 and t["params"][0]["name"] == "sort", t["params"]
    for value in ("Bohrhammer", "Bosch"):
        r = run(shop, t, value)
        want = [c[1] for c in sorted(shop.hits(value), key=lambda c: c[2])][:PER]
        assert r["status"] == "ok" and titles(r) == want, (value, r["status"], r["why"], titles(r), want)
        prices = [x["price"] for x in r["output"]["results"]]
        assert prices == sorted(prices) and r["checks"]["refined"][0]["status"] == "ok", r["checks"]["refined"]
        assert r["output"]["applied"][0]["key"] == "price_asc" and r["checks"]["refine"]["mode"] == "url"
    # the struck regular price is never the card's price (Bosch GSR 12: 89,00 now, 109,00 struck)
    r = run(shop, t, "Akkuschrauber")
    gsr = next(x for x in r["output"]["results"] if "GSR" in x["title"])
    assert gsr["price"] == 89.0, gsr
    # the bound parameter overridden: price descending
    r = run(shop, t, "Akkuschrauber", params={"sort": "price-desc"})
    prices = [x["price"] for x in r["output"]["results"]]
    assert r["status"] == "ok" and prices == sorted(prices, reverse=True) and r["output"]["applied"][0]["key"] == "price_desc", r
    # a site that ignores the sort parameter: the check FAILS (never an unsorted list answered as sorted)
    r = run(Shop(ignore_sort=True), t, "Akkuschrauber")
    assert r["status"] == "failed" and r["why"].startswith("sort: the list is not sorted by price, asc"), r["why"]
    # newest first (dates on the cards)
    g2 = C.compile_trace(filtered_trace(shop, option="Neueste"))
    assert g2["ok"] and g2["tool"]["applied"][0]["key"] == "date_desc", g2.get("why") or g2["tool"]["applied"]
    r = run(shop, g2["tool"], "Bosch")
    dates = [x["date"] for x in r["output"]["results"]]
    assert r["status"] == "ok" and dates == sorted(dates, reverse=True), r


def test_a_brand_filter_and_an_in_stock_filter():
    shop = Shop()
    g = C.compile_trace(filtered_trace(shop, how="brand", option="Bosch"))
    assert g["ok"], g.get("why")
    t = g["tool"]
    a = t["applied"][0]
    assert t["refine"] == {"mode": "url", "params": {"brand": "bosch"}} and a["kind"] == "facet" and a["label"] == "Bosch", a
    r = run(shop, t, "Bohrhammer")
    assert r["status"] == "ok" and titles(r) == ["Bosch Bohrhammer PBH 2100"], r
    assert r["checks"]["refined"][0]["why"].startswith("filter: every listed result names 'Bosch'"), r["checks"]["refined"]
    # the site drops the brand parameter: the filter check fails
    r = run(Shop(drop_brand=True), t, "Bohrhammer")
    assert r["status"] == "failed" and "dropped the filter" in r["why"], r["why"]
    # in stock only (a checkbox; its URL parameter): no listed result may be shown out of stock
    g = C.compile_trace(filtered_trace(shop, how="stock"))
    assert g["ok"] and g["tool"]["applied"][0]["kind"] == "availability", g.get("why") or g["tool"]["applied"]
    r = run(shop, g["tool"], "Akkuschrauber")
    assert r["status"] == "ok" and all("EasyDrill" not in x and "Ryobi" not in x for x in titles(r)), r
    bad = dict(g["tool"], refine={"mode": "url", "params": {"instock": ""}})
    r = run(shop, bad, "Akkuschrauber")
    assert r["status"] == "failed" and "shown out of stock" in r["why"], r["why"]


def test_a_script_rendered_list_compiles_to_the_controls():
    shop = Shop(xhr=True)
    g = C.compile_trace(filtered_trace(shop))
    assert g["ok"], g.get("why")
    t = g["tool"]
    assert t["refine"]["mode"] == "controls" and t["refine"]["controls"][0]["option_text"] == "Preis aufsteigend", t["refine"]
    r = run(shop, t, "Bohrhammer")
    assert r["status"] == "ok" and r["checks"]["refine"]["trail"][0]["ok"], r
    prices = [x["price"] for x in r["output"]["results"]]
    assert prices == sorted(prices) and len(prices) == 3, prices
    # the control is not on the page: an explicit failure
    gone = dict(t, refine={"mode": "controls", "controls": [dict(t["refine"]["controls"][0], option_text="Preis: günstigste")]})
    r = run(shop, gone, "Bohrhammer")
    assert r["status"] == "failed" and "is not on the result page" in r["why"], r["why"]


def test_validity_and_versions():
    shop = Shop()
    tr = filtered_trace(shop)
    assert C.demo_validity(tr)[0] and DB.validity(tr)[0]
    no = dict(tr, steps=[tr["steps"][0], dict(tr["steps"][2], k=1)], final_page=tr["steps"][1]["page"])
    no["result"] = {"results": reported(no["final_page"]), "applied": []}
    no["steps"][-1]["action"] = {"action": "done", "result": no["result"]}
    ok, why, _d = C.demo_validity(no)
    assert not ok and "no filter or sort order was applied" in why, why
    g9 = C.compile_trace(tr, version=9)
    assert not g9["ok"] and "unknown workflow" in g9["why"], g9
    # the empty state after the search is "no results", the refinement not applied
    r = run(shop, C.compile_trace(tr)["tool"], "zorquavex")
    assert r["status"] == "no_results" and r["accepted"], r


def test_the_readers():
    assert DB.sort_key("Preis aufsteigend") == "price_asc" and DB.sort_key("Prix décroissant") == "price_desc"
    assert DB.sort_key("price-desc") == "price_desc" and DB.sort_key("priceAsc") == "price_asc"
    assert DB.sort_key("Neueste zuerst") == "date_desc" and DB.sort_key("Najnowsze") == "date_desc"
    assert DB.sort_key("Cena rosnąco") == "price_asc" and DB.sort_key("Prijs hoog naar laag") == "price_desc"
    assert DB.sort_key("Relevanz") == "" and DB.sort_key("Name A-Z") == ""
    assert DB.classify_param("orderby", "price")["kind"] == "sort"
    assert DB.classify_param("price", "10-50") == {"kind": "price_range", "param": "price", "value": "10-50", "min": 10.0,
                                                   "max": 50.0, "label": ""}
    assert DB.classify_param("max_price", "30")["max"] == 30.0 and DB.classify_param("instock", "1")["kind"] == "availability"
    assert DB.classify_param("brand", "bosch", "Bosch (12)") == {"kind": "facet", "param": "brand", "value": "bosch",
                                                                "label": "Bosch"}
    assert DB.card_price("Bosch GSR 109,00 € 89,00 € Grundpreis 12,00 € / kg") == 89.0
    # a www.bevco.dk card: "/" between two prices is no unit; "/ pr. Ltr." is
    assert DB.card_price("Champagne 2016 12,5% 75 cl. 600,00 kr / 800,00 kr / pr. Ltr. Få på lager") == 600.0
    assert DB.card_price("Ankerkraut 4,99 € 49,90 € / kg") == 4.99
    # a TITLE spec's card is its link: the card text is the tile around it (its price)
    dom = ('<html><body><main><ul><li class="product"><h3><a href="/p/a">Alpha Tee</a></h3><span>4,99 €</span></li>'
           '<li class="product"><h3><a href="/p/b">Beta Tee</a></h3><span>6,49 €</span></li></ul></main></body></html>')
    root, _b = K.parse(dom)
    spec = [{"kind": "title", "chain": K.chain_of(next(n for n in K.walk(root) if n.tag == "a")), "rels": [""]}]
    got = K.cards_map_v10(root, spec)
    assert [r[7] for r in got["cards"]] == ["Alpha Tee 4,99 €", "Beta Tee 6,49 €"], got["cards"]
    assert DB.card_date("veröffentlicht am 12. März 2025") == (2025, 3, 12)
    assert DB.card_date("Posted March 2, 2024 by X") == (2024, 3, 2) and DB.card_date("2023-11-05") == (2023, 11, 5)
    assert DB.card_date("12 de marzo de 2025") == (2025, 3, 12) and DB.card_date("vor 2 Tagen") is None
    assert DB.stated_total("1–24 von 123 Treffern") == 123 and DB.stated_total("Wyniki: 57 wyników") == 57
    assert DB.stated_total("Showing 1-24 of 1,234 results") == 1234 and DB.stated_total("Neu im Shop") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
