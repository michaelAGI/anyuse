"""The repair ladder's SENTINEL and the gate's coverage rule (``execution/repair_ladder.py``), offline on the fake shop of ``test_repair_ladder`` (no browser, no network, no model).

The simulator found tier-0 answers that today's runtime checks accept although drift made them wrong: "no results"
when the result structure no longer reads anything and the page shows a "no results" phrase somewhere; a search
parameter the site now ignores, so the "result page" is the start page and its teaser cards are read as the results; a
paged list cut short. With the ladder ON, an accepted answer is compared with the last known good (LKG) answer for the
same input; a COLLAPSE sends it up the ladder instead of being accepted.

What must hold:

1. no results where the LKG had results: flagged, re-located at tier 1, the LKG answer accepted again; with no LKG the
   tier-0 answer stands (today's behaviour);
2. a result page that no longer names the input (the LKG's did): flagged; the search step is re-located (the site's own
   form) and repaired; unrepaired, the answer is never reported as a success ("changed, unverified");
3. ``sentinel``: a list shorter than half the LKG's, a paged list ending early at a "last page": flagged; an unchanged
   answer: not;
4. the gate's coverage rule: a repaired structure that reads fewer than half of the LKG run's results is rejected;
5. the checker does not agree with a list that under-reads (one result of the render's many naming the input);
6. a repaired run whose answer COLLAPSES against the LKG is rejected by the gate (with the steps to follow); a re-derived
   structure that also reads a block of the results' own markup BEFORE the anchored results is never a candidate.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_check as K  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import BASE, PRODUCTS, RENAMED, Shop, Store, compiled, eur, ladder_run  # noqa: E402


class EmptyPhrase(Shop):
    """A redesign that renamed the result classes and shows a filter box saying "Keine Ergebnisse gefunden"."""

    def results(self, q):
        return super().results(q).replace("<main>", "<main><div class=\"facet\">Keine Ergebnisse gefunden für Ihre "
                                                   "Filter</div>", 1)


class TeaserStart(Shop):
    """The search parameter renamed; the start page shows bestseller cards in the result list's structure."""

    def start(self):
        cards = "".join(f"<li class=\"product\"><div class=\"info\"><h3 class=\"name\"><a class=\"title\" href=\"/p/{s}\">"
                        f"{n}</a></h3><span class=\"price\">{eur(pr)}</span></div></li>" for n, pr, s in PRODUCTS[:4])
        return super().start().replace("<p>Werkzeug für alle.</p>", "<div class=\"listing\"><ul class=\"products\">" +
                                       cards + "</ul></div>")


def test_no_results_against_the_lkg():
    v = "Akkuschrauber"
    tool = compiled(v, "site_search")
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, v, st.s)["status"] == "ok"
        out = ladder_run(EmptyPhrase(cls=RENAMED), tool, v, st.s)
        lad = out["ladder"]
        assert "no results where" in lad["evidence"]["sentinel"] and lad["outcome"] == "repaired", lad
        assert out["status"] == "ok" and len(out["output"]["results"]) == 5
    finally:
        st.close()
    st = Store(tool)
    try:
        out = ladder_run(EmptyPhrase(cls=RENAMED), tool, v, st.s)
        assert out["status"] == "no_results" and out["ladder"]["outcome"] == "ok"      # no LKG: today's answer
    finally:
        st.close()


def test_a_page_that_no_longer_names_the_input():
    v = "Akkuschrauber"
    tool = compiled(v, "site_search")
    st = Store(tool)
    try:
        assert ladder_run(TeaserStart(), tool, v, st.s)["status"] == "ok"
        assert L.RepairStore.lkg(st.s, v)["echo"] is True
        drift = TeaserStart(qkey="suchbegriff")
        plain = asyncio.run(L.run_with_ladder(lambda: D.DomPage(drift), tool, v, D.no_settle, result_wait_s=0.0))
        assert plain["status"] == "ok"                       # today's checks accept the start page's teasers
        out = ladder_run(drift, tool, v, st.s)
        lad = out["ladder"]
        assert "no longer names the input" in lad["evidence"]["sentinel"] and lad["outcome"] == "repaired", lad
        assert st.s.revisions()[-1]["diff"]["ops"][0]["op"] == "rename_url_param"
    finally:
        st.close()
    st = Store(tool)
    try:
        assert ladder_run(TeaserStart(), tool, v, st.s)["status"] == "ok"
        out = ladder_run(TeaserStart(qkey="suchbegriff"), tool, v, st.s, config=L.LadderConfig(enabled=True, tier1=False))
        lad = out["ladder"]
        assert lad["outcome"] == "needs_human" and out["status"] == "incomplete" and out["accepted"] is False, lad
        assert out["output"] is None and out["unverified_output"]["results"] and "sentinel" in out["why"]
        assert "REPAIRED" not in lad["line"] and "status incomplete" in lad["line"]
    finally:
        st.close()


def test_the_sentinel_rules():
    tool = {"workflow": "paged_search"}
    lkg = {"answer": {"results": ["a"] * 10}, "count": 10, "echo": False}
    short = {"status": "ok", "output": {"results": [{"url": "x"}] * 4}, "checks": {"paged": {"end": "n"}}}
    assert "collapsed" in L.sentinel(tool, "v", short, lkg)[0]
    early = {"status": "ok", "output": {"results": [{"url": "x"}] * 7},
             "checks": {"paged": {"end": "last page (it repeats the earlier results)"}}}
    why, steps = L.sentinel(tool, "v", early, lkg)
    assert "ended early" in why and steps[0] == "pager"
    same = {"status": "ok", "output": {"results": [{"url": "x"}] * 10}, "checks": {"paged": {"end": "n"}}}
    assert L.sentinel(tool, "v", same, lkg) == ("", [])
    assert L.sentinel(tool, "v", same, None) == ("", [])
    assert L.sentinel(tool, "v", {"status": "no_results"}, {"answer": {"no_results": True}}) == ("", [])


def test_the_gate_coverage_rule_and_the_checker():
    tool = compiled("Bosch GSR 12V Akkuschrauber")
    one = {"status": "ok", "output": {"name": "x", "price": 1.0}, "checks": {"result_page": {"n_items": 1}}}
    g = asyncio.run(L.gate(tool, "v", one, {"key": "k", "n_items": 12}, lambda *a: {"verdict": "agrees"}))
    assert g["verdict"] == "rejected" and "reads 1 result" in g["why"]
    v = "Akkuschrauber"
    lt = compiled(v, "site_search")
    under = {"status": "ok", "output": {"results": [{"title": PRODUCTS[0][0], "url": f"{BASE}/p/{PRODUCTS[0][2]}"}]}}
    got = asyncio.run(K.OracleChecker(lambda: D.DomPage(Shop()), D.no_settle, wait_s=0.0)(lt, v, under))
    assert got["verdict"] == "disagrees" and "answered result(s) of the render" in got["why"], got


def test_the_gate_on_a_collapsed_repair_and_a_block_before_the_results():
    tool = compiled("Akkuschrauber", "site_search")
    lkg = {"key": "k", "answer": {"results": ["u"] * 5}, "count": 5, "echo": False, "n_items": 5}
    out = {"status": "ok", "output": {"results": [{"url": "u1", "title": "t"}]}, "checks": {"result_page": {"n_items": 5}}}
    g = asyncio.run(L.gate(tool, "Akkuschrauber", out, lkg, lambda *a: {"verdict": "agrees"}))
    assert g["verdict"] == "rejected" and "collapsed" in g["why"] and g["collapse_steps"] == ["region", "search"]
    from anyuse.execution import repair_relocate as RL
    card = "<li><a href=\"/p/{0}\">{0}</a></li>"
    dom = ("<html><body><main><div><div><ul>" + "".join(card.format(f"promo-{k}") for k in range(3)) +
           "</ul></div></div><div><div><ul>" + "".join(card.format(f"result-{k}") for k in range(4)) +
           "</ul></div></div></main></body></html>")
    read = {"dom": dom, "url": BASE + "/search?q=x"}
    specs = [{"kind": "card", "chain": ["div", "div", "ul", "li"], "rels": ["a"]}]
    anchors = [f"{BASE}/p/result-{k}" for k in range(4)]
    assert RL.foreign_before(read, specs, anchors) == 3
    assert RL.foreign_before(read, specs, [f"{BASE}/p/promo-0"] + anchors) == 0


if __name__ == "__main__":
    test_no_results_against_the_lkg()
    test_a_page_that_no_longer_names_the_input()
    test_the_sentinel_rules()
    test_the_gate_coverage_rule_and_the_checker()
    test_the_gate_on_a_collapsed_repair_and_a_block_before_the_results()
    print("test_repair_sentinel: 5 ok")
