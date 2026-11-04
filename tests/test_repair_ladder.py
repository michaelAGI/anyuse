"""The repair ladder (``execution/repair_ladder.py``, ``repair_relocate.py``), offline: a fake
shop served from generated DOMs (``execution/dom_page.py``; no browser, no network, no model).

What must hold:

1. OFF by default: ``LadderConfig()`` is disabled and ``run_with_ladder`` without a config (or for a tool below version
   10) gives exactly ``run_tool``'s result and writes nothing;
2. tier 0: an accepted run records its answer as the last known good (LKG) and the readout says tier 0, revision 0;
3. tier 1, step-local, no model: the shop renames its result classes - the tool fails at membership, tier 1 re-derives
   the result cards from the LKG's results, the gate accepts (the same answer as the LKG) and revision 1 is active
   (parent digest, the patch as its diff, tier 1, US$0); the next run is a plain tier-0 run on revision 1; a rollback
   re-activates revision 0 and keeps both;
4. tier 1 on the search step: the shop renames its search parameter - the site's own GET form gives the new name;
5. two steps broke at once (renamed classes AND a renamed search parameter): tier 1 re-locates the first, sees the
   candidate fail at the other, re-locates that one on the patched tool and accepts both as ONE patch (2 operations);
6. a new card layout among the results (the run is INCOMPLETE: cards it cannot read): the cards it read and the ones it
   could not are the anchors; the re-derived structure reads both layouts and the LKG answer is accepted again;
7. the gate: a repaired answer that differs from the LKG (or with no LKG) needs the independent checker - without its
   agreement the outcome is "changed, unverified", the old revision stays active and the run keeps its failure (never
   reported as a success); with it, the repair is accepted; a second input with an LKG is replayed and must pass.
"""
import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import copy
import html
import json
import os
import shutil
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import repair_pace as _RP  # noqa: E402

# pacing is ON in the ladder's default configuration; the fake shops' pages need no 10-s interval (the
# pacer itself is tested in test_repair_pace.py)
_RP.reset(_RP.Pacer(min_interval_s=0.0))

SITE = "www.ladder-shop.test"
BASE = f"https://{SITE}"
PRODUCTS = [("Bosch GSR 12V Akkuschrauber", 89.90, "bosch-gsr-12v"), ("Makita DF333 Akkuschrauber", 99.00, "makita-df333"),
            ("Metabo PowerMaxx Akkuschrauber", 119.00, "metabo-powermaxx"), ("Bosch PSR Akkuschrauber Set", 79.50, "bosch-psr"),
            ("Einhell TE-CD Akkuschrauber", 49.95, "einhell-te-cd"), ("Bosch GBH Bohrhammer", 159.00, "bosch-gbh"),
            ("Makita HR2470 Bohrhammer", 139.00, "makita-hr2470"), ("Ryobi R18 Bohrhammer", 129.00, "ryobi-r18")]


def eur(x):
    return f"{x:.2f}".replace(".", ",") + " €"


class Shop(D.DomSite):
    """The shop's server: ``/`` (a search form), ``/search?<qkey>=...`` (result cards), ``/p/<slug>`` (a product page
    with JSON-LD). Drift knobs: ``cls`` (a class-name map of the result page), ``qkey`` (the search parameter),
    ``wrap`` (the result list moved into another container), ``promo`` (a block of other products before the list)."""

    def __init__(self, cls=None, qkey="q", wrap=False, promo=False, ppath="/p/"):
        super().__init__([])
        self.cls, self.qkey, self.wrap, self.promo, self.ppath = dict(cls or {}), qkey, wrap, promo, ppath

    def c(self, name):
        return self.cls.get(name, name)

    def start(self):
        return (f"<html><head><title>Ladder Shop</title></head><body><header><nav><a href=\"/\">Start</a> "
                f"<a href=\"/konto\">Konto</a></nav><form action=\"/search\" method=\"get\" role=\"search\">"
                f"<input type=\"search\" name=\"{self.qkey}\" placeholder=\"Suche\"></form></header><main><h1>Willkommen</h1>"
                f"<p>Werkzeug für alle.</p></main></body></html>")

    def hits(self, q):
        words = [w for w in q.lower().split() if len(w) > 2]
        return [p for p in PRODUCTS if words and all(w in p[0].lower() for w in words[:1])] or \
            [p for p in PRODUCTS if any(w in p[0].lower() for w in words)]

    def results(self, q):
        hits = self.hits(q)
        cards = "".join(f"<li class=\"{self.c('product')}\"><div class=\"{self.c('info')}\"><h3 class=\"{self.c('name')}\">"
                        f"<a class=\"{self.c('title')}\" href=\"{self.ppath}{s}\">{html.escape(n)}</a></h3>"
                        f"<span class=\"{self.c('price')}\">{eur(pr)}</span></div></li>" for n, pr, s in hits)
        lst = f"<ul class=\"{self.c('products')}\">{cards}</ul>"
        if self.wrap:
            lst = f"<section class=\"results-v2\"><div class=\"inner\">{lst}</div></section>"
        promo = ""
        if self.promo:
            promo = ("<div class=\"teaser\"><ul class=\"tips\">" + "".join(
                f"<li class=\"tip\"><a href=\"/p/{s}\">{html.escape(n)}</a> {eur(pr)}</li>" for n, pr, s in PRODUCTS[-3:])
                + "</ul></div>")
        body = (f"<h1>Suchergebnisse für {html.escape(q)}</h1>" + (promo + f"<div class=\"{self.c('listing')}\">{lst}</div>"
                if hits else "<p>Keine Ergebnisse gefunden.</p>"))
        return (f"<html><head><title>Suche: {html.escape(q)}</title></head><body><header><nav><a href=\"/\">Start</a>"
                f"</nav><form action=\"/search\" method=\"get\" role=\"search\"><input type=\"search\" name=\"{self.qkey}\" "
                f"value=\"{html.escape(q)}\"></form></header><main>{body}</main><footer><a href=\"/impressum\">Impressum"
                f"</a></footer></body></html>")

    def product(self, slug):
        p = next((x for x in PRODUCTS if x[2] == slug), None)
        if p is None:
            return None
        ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": p[0],
                         "offers": {"@type": "Offer", "price": f"{p[1]:.2f}", "priceCurrency": "EUR",
                                    "availability": "https://schema.org/InStock"}})
        return (f"<html><head><title>{html.escape(p[0])}</title><script type=\"application/ld+json\">{ld}</script>"
                f"</head><body><main><h1>{html.escape(p[0])}</h1><div class=\"buybox\"><span class=\"price\">{eur(p[1])}"
                f"</span><p>Auf Lager</p></div></main></body></html>")

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path in ("", "/"):
            return D.Recorded(url, self.start()), url
        if sp.path == "/search":
            if self.qkey in q:
                return D.Recorded(url, self.results(q[self.qkey])), url
            return D.Recorded(url, self.start()), url      # an unknown parameter: the search page without a query
        if sp.path.startswith(self.ppath):
            dom = self.product(sp.path[len(self.ppath):])
            if dom:
                return D.Recorded(url, dom), url
        return D.Recorded(url, D.NOT_FOUND, {}, 404), url


def read(site, url):
    rec, u = site.route(url)
    return site.read(rec, u)


def page_dict(site, url):
    r = dict(read(site, url))
    r.setdefault("search_urls", [])
    return r


def trace(site, value, workflow="product_lookup", pick=0):
    start = BASE + "/"
    p0 = page_dict(site, start)
    rurl = f"{BASE}/search?{urllib.parse.urlencode({site.qkey: value})}"
    p1 = page_dict(site, rurl)
    steps = [{"k": 0, "page": p0, "action": {"action": "search", "element": 0, "text": value},
              "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": site.qkey}, "url_after": rurl}}]
    links = [e for e in p1["elements"] if e["tag"] == "a" and site.ppath in (e.get("href") or "")]
    if workflow == "product_lookup":
        e = links[pick]
        p2 = page_dict(site, e["href"])
        name, price, slug = next(p for p in PRODUCTS if e["href"].endswith(p[2]))
        res = {"name": name, "price": eur(price), "currency": "EUR", "availability": "Auf Lager", "url": e["href"]}
        steps.append({"k": 1, "page": p1, "action": {"action": "click", "element": e["i"]},
                      "outcome": {"ok": True, "how": "goto", "href": e["href"], "url_after": e["href"]},
                      "mapping": {str(e["i"]): {"src": "el", "i": e["i"], "text": e["text"], "href": e["href"], "tag": "a"}}})
        steps.append({"k": 2, "page": p2, "action": {"action": "done", "result": res}, "outcome": {}})
        fin = p2
    else:
        res = {"results": [{"title": e["text"], "url": e["href"]} for e in links[:5]]}
        steps.append({"k": 1, "page": p1, "action": {"action": "done", "result": res}, "outcome": {}})
        fin = p1
    return {"version": 1, "workflow": workflow, "site": SITE, "start_url": start, "value": value, "steps": steps,
            "final_page": fin, "result": res, "stop": "done", "forbidden": []}


def compiled(value="Bosch GSR 12V Akkuschrauber", workflow="product_lookup", site=None):
    g = C.compile_trace(trace(site or Shop(), value, workflow), version=10)
    assert g["ok"], g.get("why")
    return g["tool"]


def ladder_run(site, tool, value, store, **kw):
    cfg = kw.pop("config", None) or L.LadderConfig(enabled=True)
    return asyncio.run(L.run_with_ladder(lambda: D.DomPage(site), tool, value, D.no_settle, config=cfg, store=store,
                                         result_wait_s=0.0, **kw))


class Store:
    """A RepairStore in a fresh temporary directory (removed by ``close``)."""

    def __init__(self, tool, clock=None):
        self.dir = tempfile.mkdtemp(prefix="anyuse-ladder-test-")
        self.s = L.RepairStore(self.dir, tool, **({"clock": clock} if clock else {}))

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


RENAMED = {"product": "tile", "info": "tile-body", "name": "tile-name", "title": "tile-link", "products": "grid",
           "listing": "search-grid", "price": "amount"}


def test_off_by_default_is_run_tool_exactly():
    assert L.ENABLED_BY_DEFAULT is False and L.LadderConfig().enabled is False
    tool = compiled()
    for site in (Shop(), Shop(cls=RENAMED)):
        want = asyncio.run(T.run_tool(D.DomPage(site), tool, "Bosch GSR 12V Akkuschrauber", D.no_settle, result_wait_s=0.0))
        for cfg in (None, L.LadderConfig()):
            got = asyncio.run(L.run_with_ladder(lambda: D.DomPage(site), tool, "Bosch GSR 12V Akkuschrauber", D.no_settle,
                                                config=cfg, result_wait_s=0.0))
            for o in (want, got):
                o.pop("seconds", None)
                for row in o.get("wait_trail") or []:
                    row[0] = 0.0
            assert json.dumps(got, sort_keys=True, default=str) == json.dumps(want, sort_keys=True, default=str)
            assert "ladder" not in got
    # a tool below version 10 runs exactly run_tool even with the ladder on (and needs no store)
    t9 = C.compile_trace(trace(Shop(), "Bosch GSR 12V Akkuschrauber"), version=9)["tool"]
    got = asyncio.run(L.run_with_ladder(lambda: D.DomPage(Shop(cls=RENAMED)), t9, "Bosch GSR 12V Akkuschrauber",
                                        D.no_settle, config=L.LadderConfig(enabled=True), result_wait_s=0.0))
    assert got["status"] == "failed" and "ladder" not in got


def test_tier0_then_tier1_region_repair_revision_and_rollback():
    v = "Bosch GSR 12V Akkuschrauber"
    tool = compiled(v)
    st = Store(tool)
    try:
        out = ladder_run(Shop(), tool, v, st.s)
        assert out["status"] == "ok" and out["ladder"]["tier"] == 0 and out["ladder"]["outcome"] == "ok"
        assert out["ladder"]["revision"] == 0 and st.s.lkg(v)["anchors"][0][0].endswith("/p/bosch-gsr-12v")
        assert out["ladder"]["line"].startswith("tier 0: ok") and out["ladder"]["state"] == "healthy"
        drift = Shop(cls=RENAMED)
        out = ladder_run(drift, tool, v, st.s)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        assert lad["model_calls"] == 0 and lad["usd"] == 0.0 and lad["revision"] == 1 and lad["revision_before"] == 0
        assert "REPAIRED" in lad["line"] and "rev 1 (was 0)" in lad["line"] and lad["state"] == "watch"
        assert out["output"]["name"] == v and out["output"]["price"] == 89.9
        revs = st.s.revisions()
        assert [r["rev"] for r in revs] == [0, 1] and revs[1]["parent"] == revs[0]["digest"] and revs[1]["tier"] == 1
        assert revs[1]["diff"]["ops"][0]["op"] == "replace_anchor" and revs[1]["cost_usd"] == 0.0
        assert revs[1]["evidence"]["gate"]["same_as_lkg"] is True and revs[1]["evidence"]["steps"][0] == "region"
        # step-local: only the cards changed
        assert {k: v2 for k, v2 in revs[1]["tool"].items() if k != "region"} == {k: v2 for k, v2 in tool.items() if k != "region"}
        out = ladder_run(drift, tool, v, st.s)
        assert out["status"] == "ok" and out["ladder"]["tier"] == 0 and out["ladder"]["revision"] == 1
        assert st.s.rollback() == 0 and st.s.active_rev() == 0 and len(st.s.revisions()) == 2
    finally:
        st.close()


def test_tier1_search_parameter_renamed():
    v = "Makita DF333 Akkuschrauber"
    tool = compiled(v)
    assert tool["search"]["mode"] == "url" and "q={q}" in tool["search"]["template"]
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, v, st.s)["status"] == "ok"
        out = ladder_run(Shop(qkey="suchbegriff"), tool, v, st.s)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        rev = st.s.revisions()[-1]
        assert rev["diff"]["ops"] == [{"op": "rename_url_param", "scope": "search", "from": "q", "to": "suchbegriff"}]
        assert "suchbegriff={q}" in rev["tool"]["search"]["template"]
    finally:
        st.close()


def test_two_steps_broken_at_once():
    v = "Makita DF333 Akkuschrauber"
    tool = compiled(v)
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, v, st.s)["status"] == "ok"
        out = ladder_run(Shop(cls=RENAMED, qkey="suchbegriff"), tool, v, st.s)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        ops = st.s.revisions()[-1]["diff"]["ops"]
        assert sorted(o["op"] for o in ops) == ["rename_url_param", "replace_anchor"], ops
        assert out["output"]["name"] == v
    finally:
        st.close()


class MixedLayout(Shop):
    """The third and later result cards in a new layout."""

    def results(self, q):
        out, k = [], 0
        for part in super().results(q).split("<li class=\"product\">"):
            if k >= 3:
                part = part.replace("class=\"info\"", "class=\"tile-body\"").replace("class=\"name\"", "class=\"tile-h\"")
            out.append(part)
            k += 1
        html_text = out[0]
        for k, part in enumerate(out[1:], start=1):
            html_text += ("<li class=\"product\">" if k < 3 else "<li class=\"product-v2\">") + part
        return html_text


def test_a_new_card_layout_among_the_results():
    v = "Akkuschrauber"
    tool = compiled(v, "site_search")
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, v, st.s)["status"] == "ok"
        out = ladder_run(MixedLayout(), tool, v, st.s)
        lad = out["ladder"]
        assert lad["evidence"]["tier0"]["status"] == "incomplete" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        assert len(out["output"]["results"]) == 5 and lad["evidence"]["attempts"][1]["verdict"] == "accepted"
    finally:
        st.close()


def test_the_gate_changed_unverified_checker_and_second_value():
    v, v2 = "Bosch GSR 12V Akkuschrauber", "Einhell TE-CD Akkuschrauber"
    tool = compiled(v)
    # no LKG and no checker: a repair is "changed, unverified" - never an answer, the old revision stays
    st = Store(tool)
    try:
        out = ladder_run(Shop(cls=RENAMED), tool, v, st.s)
        lad = out["ladder"]
        assert lad["outcome"] == "changed_unverified" and out["status"] == "failed" and st.s.active_rev() == 0, lad
        assert "UNVERIFIED" in lad["line"] and "not an answer" in lad["line"] and lad["state"] == "watch"
        assert lad["evidence"]["unverified"]["answer"]["name"] == v
        # the checker disagrees: still unverified
        out = ladder_run(Shop(cls=RENAMED), tool, v, st.s, checker=lambda t, val, o: {"verdict": "disagrees"})
        assert out["ladder"]["outcome"] == "changed_unverified" and st.s.active_rev() == 0
        # the checker agrees: accepted
        out = ladder_run(Shop(cls=RENAMED), tool, v, st.s, checker=lambda t, val, o: {"verdict": "agrees"})
        assert out["ladder"]["outcome"] == "repaired" and st.s.active_rev() == 1
    finally:
        st.close()
    # a second value with an LKG is replayed with the repaired tool and must pass
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, v2, st.s)["status"] == "ok"
        assert ladder_run(Shop(), tool, v, st.s)["status"] == "ok"
        out = ladder_run(Shop(cls=RENAMED), tool, v, st.s)
        g = st.s.revisions()[-1]["evidence"]["gate"]
        assert out["ladder"]["outcome"] == "repaired" and g["second"]["value"] == v2 and g["second"]["same_as_lkg"]
    finally:
        st.close()
    # the gate alone: a candidate whose second value fails is rejected; a failed run never passes
    lkg = {"key": "x"}
    ok = {"status": "ok", "output": {"name": "a", "price": 1.0}}
    g = asyncio.run(L.gate(tool, v, ok, lkg, lambda *a: {"verdict": "agrees"}, (v2, {"status": "failed", "why": "x"}, None)))
    assert g["verdict"] == "rejected"
    g = asyncio.run(L.gate(tool, v, {"status": "incomplete", "why": "x"}, lkg, lambda *a: {"verdict": "agrees"}))
    assert g["verdict"] == "rejected"
    g = asyncio.run(L.gate(tool, v, {"status": "no_results", "output": {"no_results": True}}, lkg,
                           lambda *a: {"verdict": "agrees"}))
    assert g["verdict"] == "rejected"


if __name__ == "__main__":
    test_off_by_default_is_run_tool_exactly()
    test_tier0_then_tier1_region_repair_revision_and_rollback()
    test_tier1_search_parameter_renamed()
    test_two_steps_broken_at_once()
    test_a_new_card_layout_among_the_results()
    test_the_gate_changed_unverified_checker_and_second_value()
    print("test_repair_ladder: 6 ok")
