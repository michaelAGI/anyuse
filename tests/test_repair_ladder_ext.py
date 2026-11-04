"""The repair ladder on VERSION-11 tools, offline: the fake shop of
``test_repair_ladder.py`` served from generated DOMs (``execution/dom_page.py``; no browser, no network, no model).

A version-11 tool reads its cards with ``CARDS_JS_V11`` (the card descriptors without a BEM modifier class; the
title rule) and its result page with ``READ_JS_V11`` past the reader's cap of 700 controls. What must
hold:

1. BEM: every result card carries a modifier (``product product--grid``); the shop renames its classes
   (``tile tile--grid``). Tier 1 re-derives the cards the way the version-11 read describes the page (no ``--``
   modifier in the new descriptors) and the LKG answer is accepted; the region keeps ``cards_version`` 11;
2. the reader's cap: a navigation of 750 links before the results. The version-11 tool reads the results past the cap
   (the offline page answers ``READ_JS_V11``); with renamed classes on top, tier 1's probe reads past the cap too and
   repairs the cards; a version-10 tool stops at the cap (no repair);
3. the sentinel on a version-11 tool: no results / a collapsed list against the LKG is never accepted;
4. tier 3 recompiles at the tool's OWN version (11); the tier-3 boundary refuses a version-10 recompile of a
   version-11 tool;
5. the structure guard and tier 2's card candidates read a version-11 region's cards without BEM modifiers;
6. a version-12 region is re-derived at cards version 12.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import repair_model as M  # noqa: E402
from test_repair_ladder import RENAMED, Shop, Store, ladder_run, trace  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"
Q = "Akkuschrauber"


class BemShop(Shop):
    """Every result card carries a BEM modifier of its block class (``product product--grid``)."""

    def results(self, q):
        c = self.c("product")
        return super().results(q).replace(f"<li class=\"{c}\">", f"<li class=\"{c} {c}--grid\">")


class BigNav(Shop):
    """A navigation of 750 links before the results (the drift census's www.compass24.de: 769)."""

    def results(self, q):
        nav = "<nav class=\"mega\">" + "".join(f"<a href=\"/kategorie/{k}\">Kategorie {k}</a>" for k in range(750)) + \
            "</nav>"
        return super().results(q).replace("<main>", "<main>" + nav, 1)


def compiled11(value=V, workflow="product_lookup", site=None, version=11):
    g = C.compile_trace(trace(site or Shop(), value, workflow), version=version)
    assert g["ok"], g.get("why")
    return g["tool"]


def specs_have_modifier(tool):
    return any("--" in d for sp in (tool.get("region") or {}).get("cards") or [] for d in sp.get("chain") or [])


def test_bem_modifiers_relocated_the_version_11_way():
    tool = compiled11(site=BemShop())
    assert tool["version"] == 11 and tool["region"]["cards_version"] == 11 and not specs_have_modifier(tool)
    st = Store(tool)
    try:
        out = ladder_run(BemShop(), tool, V, st.s)
        assert out["status"] == "ok" and out["ladder"]["outcome"] == "ok", out.get("why")
        drift = BemShop(cls=RENAMED)
        out = ladder_run(drift, tool, V, st.s)
        lad = out["ladder"]
        assert lad["evidence"]["tier0"]["status"] == "failed", lad["evidence"]["tier0"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        rev = st.s.revisions()[-1]["tool"]
        assert not specs_have_modifier(rev) and rev["region"]["cards_version"] == 11 and rev["version"] == 11
        assert any("tile" in d for sp in rev["region"]["cards"] for d in sp["chain"])
        assert out["output"]["name"] == V
        # the next run is a plain tier-0 run on the repaired revision
        out = ladder_run(drift, tool, V, st.s)
        assert out["status"] == "ok" and out["ladder"]["tier"] == 0 and out["ladder"]["revision"] == 1
    finally:
        st.close()


def test_a_version_12_region_is_relocated_at_cards_version_12():
    """The same for a version-12 tool (its cards read with the promo rule too): the re-derived region keeps
    ``cards_version`` 12 and no BEM modifier."""
    tool = compiled11(site=BemShop(), version=12)
    assert tool["region"]["cards_version"] == 12
    st = Store(tool)
    try:
        assert ladder_run(BemShop(), tool, V, st.s)["status"] == "ok"
        out = ladder_run(BemShop(cls=RENAMED), tool, V, st.s)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        rev = st.s.revisions()[-1]["tool"]
        assert rev["region"]["cards_version"] == 12 and rev["version"] == 12 and not specs_have_modifier(rev)
    finally:
        st.close()


def test_results_past_the_readers_cap():
    t11 = compiled11(Q, "site_search")
    t10 = compiled11(Q, "site_search", version=10)
    st = Store(t11)
    try:
        assert ladder_run(Shop(), t11, Q, st.s)["status"] == "ok"
        # version 11 reads past the cap at tier 0 (READ_JS_V11 on the offline page)
        out = ladder_run(BigNav(), t11, Q, st.s)
        assert out["status"] == "ok" and out["ladder"]["tier"] == 0 and len(out["output"]["results"]) == 5, out.get("why")
        # renamed classes on top: tier 1's probe reads past the cap and re-derives the cards
        out = ladder_run(BigNav(cls=RENAMED), t11, Q, st.s)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 1, lad
        assert [r["url"] for r in out["output"]["results"]] == [a[0] for a in st.s.lkg(Q)["anchors"]]
    finally:
        st.close()
    st = Store(t10)
    try:
        assert ladder_run(Shop(), t10, Q, st.s)["status"] == "ok"
        out = ladder_run(BigNav(), t10, Q, st.s)
        assert out["status"] != "ok" and out["ladder"]["outcome"] in (L.NEEDS_HUMAN, L.UNVERIFIED), out["ladder"]
    finally:
        st.close()


class Fewer(Shop):
    """The search lists one product only."""

    def hits(self, q):
        return super().hits(q)[:1]


class NoHits(Shop):
    def hits(self, q):
        return []


def test_the_sentinel_on_a_version_11_tool():
    tool = compiled11(Q, "site_search")
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, Q, st.s)["status"] == "ok" and st.s.lkg(Q)["count"] == 5
        for site, flag in ((NoHits(), "no results"), (Fewer(), "collapsed")):
            out = ladder_run(site, tool, Q, st.s)
            lad = out["ladder"]
            assert out["status"] != "ok" and flag in lad["evidence"]["sentinel"], lad
            assert lad["outcome"] in (L.NEEDS_HUMAN, L.UNVERIFIED) and st.s.active_rev() == 0
        assert st.s.lkg(Q)["count"] == 5
    finally:
        st.close()


class Demonstrator:
    def __init__(self, site):
        self.site, self.calls = site, []

    def __call__(self, goal, hint):
        self.calls.append((goal, hint))
        return {"trace": trace(self.site, V), "calls": 4, "usd": 0.12}


def test_tier3_recompiles_at_the_tools_own_version():
    tool = compiled11()
    moved = Shop(ppath="/produkt/", cls=RENAMED)
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
        out = ladder_run(moved, tool, V, st.s, config=L.LadderConfig(enabled=True, tier1=False, tier2=False, tier3=True),
                         demonstrator=Demonstrator(moved), checker=lambda t, v, o: {"verdict": "agrees"})
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 3, lad
        rev = st.s.revisions()[-1]["tool"]
        assert rev["version"] == 11 and rev["region"]["cards_version"] == 11
    finally:
        st.close()
    t10 = C.compile_trace(trace(moved, V), version=10)["tool"]
    assert "version" in L.tier3_boundary(tool, t10)
    assert L.tier3_boundary(tool, C.compile_trace(trace(moved, V), version=11)["tool"]) == ""


def test_the_structure_guard_and_card_candidates_without_bem_modifiers():
    tool = compiled11(Q, "site_search", site=BemShop())
    site = BemShop(cls=RENAMED)
    url = f"https://www.ladder-shop.test/search?q={Q}"
    page = D.DomPage(site)
    asyncio.run(page.goto(url))
    read = dict(page.read(), url=url, dom=page.cur.dom)
    cands = M.card_candidates(read, tool)
    assert cands and all("--" not in d for c in cands for d in c["spec"]["chain"]), cands
    cand = dict(tool, region=dict(tool["region"], cards=[cands[0]["spec"]]))
    assert L.structure_guard(tool, cand, read, [[r, ""] for r in [e["href"] for e in read["elements"]
                                                                  if "/p/" in (e.get("href") or "")][:2]]) == ""


if __name__ == "__main__":
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            print("ok", k)
    print("all ok")
