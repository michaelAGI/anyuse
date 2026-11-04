"""KNOWN WRONG answers in the repair ladder (``execution/verdict_store.py``, ``repair_ladder.run_with_ladder(verdicts=,
upgrade=)``), offline on the fake shop of ``test_repair_ladder.py`` (no browser, no network,
no model).

The case (the drift census's www.traumflieger.de, abstracted): a saved tool whose cards read a TEASER block of the
result page (three other products) instead of the search results; an audit judged its answer WRONG. What must hold:

1. ladder OFF with a verdict store: the run is ``known_wrong`` (never ok, its answer only evidence); without a store it
   is exactly ``run_tool``;
2. ladder ON: the tier-0 answer is flagged, its last known good copy dropped, the tool's state is ``needs_repair`` and
   the readout says the known wrong answer was withheld; tier 1 re-derives the cards from the region's links naming
   the input - and the gate requires the CHECKER to agree: without it the run stays flagged ("changed, unverified");
   with the checker's agreement the repair is accepted (revision 1, status ok, the real results);
3. a repair that repeats a known wrong answer is rejected by the gate;
4. with a newer compile (``upgrade``): the candidate is shadow-run on the known inputs, the known wrong input becomes
   usable (the checker agrees), no input is worse - the upgrade is accepted before any repair (a revision of kind
   ``upgrade``, the decision logged).
"""
import asyncio
import copy
import os
import shutil
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import verdict_store as V  # noqa: E402
from test_repair_ladder import BASE, Shop, Store, compiled, ladder_run, read, trace  # noqa: E402

Q = "Akkuschrauber"
Q2 = "Bohrhammer"


class TeaserShop(Shop):
    """The result page shows a teaser block of three accessories naming the query before the results (an audit
    judged the list of a tool reading it WRONG)."""

    def results(self, q):
        w = q.split()[0]
        tips = [(f"{w} Bit-Set 32-teilig", f"{w.lower()}-bits"), (f"{w} Koffer", f"{w.lower()}-koffer"),
                (f"{w} Ladegerät 12V", f"{w.lower()}-lader")]
        block = "<div class=\"teaser\"><ul class=\"tips\">" + "".join(
            f"<li class=\"tip\"><a href=\"/p/{s}\">{n}</a></li>" for n, s in tips) + "</ul></div>"
        return super().results(q).replace("<div class=\"listing\">", block + "<div class=\"listing\">", 1)


def teaser_tool():
    """The site-search tool whose cards read the teaser block (the known wrong structure), at version 10."""
    good = compiled(Q, "site_search")
    site = TeaserShop()
    url = f"{BASE}/search?q={Q}"
    r = read(site, url)
    rec, _u = site.route(url)
    tips = [(e["href"], e["text"]) for e in r["elements"] if e["tag"] == "a" and "/p/" in e.get("href", "")][:3]
    st = K.result_cards(rec.dom, url, tips, good["region"]["regex"])
    bad = copy.deepcopy(good)
    bad["region"]["cards"] = st["cards"]
    return good, bad


def run(site, tool, value):
    out = asyncio.run(T.run_tool(D.DomPage(site), tool, value, D.no_settle, result_wait_s=0.0))
    out.pop("seconds", None)
    for row in out.get("wait_trail") or []:
        row[0] = 0.0
    return out


class Verdicts:
    def __init__(self):
        self.d = tempfile.mkdtemp(prefix="anyuse-known-wrong-test-")
        self.s = V.VerdictStore(os.path.join(self.d, "verdicts.jsonl"))

    def close(self):
        shutil.rmtree(self.d, ignore_errors=True)


def judged_wrong(vs, tool, value, out):
    vs.add(tool=C.tool_digest(tool), site=tool["site"], value=value, status=out["status"], output=out["output"],
           verdict="wrong", source="audit", ref="r6|delayed|v1", why="a teaser list, not the search results")


def test_ladder_off_flags_a_known_wrong_answer():
    good, bad = teaser_tool()
    site = TeaserShop()
    out = run(site, bad, Q)
    assert out["status"] == "ok" and len(out["output"]["results"]) == 3
    vs = Verdicts()
    try:
        judged_wrong(vs.s, bad, Q, out)
        got = asyncio.run(L.run_with_ladder(lambda: D.DomPage(site), bad, Q, D.no_settle, result_wait_s=0.0,
                                            verdicts=vs.s))
        assert got["status"] == V.KNOWN_WRONG and got["accepted"] is False and got["output"] is None
        assert got["unverified_output"] == out["output"] and "ladder" not in got
        plain = asyncio.run(L.run_with_ladder(lambda: D.DomPage(site), bad, Q, D.no_settle, result_wait_s=0.0))
        plain.pop("seconds", None)
        for row in plain.get("wait_trail") or []:
            row[0] = 0.0
        assert plain == out
        # the good tool's answer is not flagged
        assert asyncio.run(L.run_with_ladder(lambda: D.DomPage(site), good, Q, D.no_settle, result_wait_s=0.0,
                                             verdicts=vs.s))["status"] == "ok"
    finally:
        vs.close()


def test_ladder_on_needs_repair_and_the_checker_must_agree():
    good, bad = teaser_tool()
    site = TeaserShop()
    vs = Verdicts()
    try:
        judged_wrong(vs.s, bad, Q, run(site, bad, Q))
        st = Store(bad)
        try:
            # an LKG recorded before the verdict was known (the census's case: the wrong answer was the baseline)
            out = ladder_run(site, bad, Q, st.s)
            assert out["status"] == "ok" and st.s.lkg(Q) is not None
            out = ladder_run(site, bad, Q, st.s, verdicts=vs.s)
            lad = out["ladder"]
            assert out["status"] == V.KNOWN_WRONG and lad["state"] == L.NEEDS_REPAIR, lad
            assert lad["outcome"] in (L.UNVERIFIED, L.NEEDS_HUMAN) and "KNOWN WRONG" in lad["line"]
            assert lad["evidence"]["known_wrong"]["source"] == "audit" and st.s.lkg(Q) is None
            assert st.s.active_rev() == 0
            # with the checker's agreement: tier 1's re-derived cards are accepted
            out = ladder_run(site, bad, Q, st.s, verdicts=vs.s, checker=lambda t, v, o: {"verdict": "agrees"})
            lad = out["ladder"]
            assert out["status"] == "ok" and lad["outcome"] == L.REPAIRED and lad["tier"] == 1, lad
            assert [r["url"] for r in out["output"]["results"]] == [r["url"] for r in run(site, good, Q)["output"]["results"]]
            g = st.s.revisions()[-1]["evidence"]["gate"]
            assert g["same_as_lkg"] is False and g["checker"]["verdict"] == "agrees"
            assert lad["state"] == L.WATCH and st.s.lkg(Q)["count"] == 5
            # the checker's agreement is recorded in the store
            assert any(r["source"] == "checker" and r["verdict"] == "correct" for r in vs.s.rows())
        finally:
            st.close()
    finally:
        vs.close()


def test_the_gate_rejects_a_known_wrong_repair():
    good, bad = teaser_tool()
    site = TeaserShop()
    vs = Verdicts()
    try:
        out = run(site, bad, Q)
        judged_wrong(vs.s, bad, Q, out)
        g = asyncio.run(L.gate(bad, Q, out, None, lambda *a: {"verdict": "agrees"}, verdicts=vs.s))
        assert g["verdict"] == "rejected" and "known wrong" in g["why"]
        g = asyncio.run(L.gate(good, Q, run(site, good, Q), None, lambda *a: {"verdict": "agrees"}, verdicts=vs.s))
        assert g["verdict"] == "accepted"
        # require_checker: the LKG alone is not enough
        o = run(site, good, Q)
        lkg = {"key": L.answer_key("site_search", o), "answer": L.answer_of("site_search", o), "count": 5}
        g = asyncio.run(L.gate(good, Q, o, lkg, None, require_checker=True))
        assert g["verdict"] == L.UNVERIFIED
        assert asyncio.run(L.gate(good, Q, o, lkg, None))["verdict"] == "accepted"
    finally:
        vs.close()


def test_a_newer_compile_through_the_upgrade_gate():
    good, bad = teaser_tool()
    site = TeaserShop()
    cand = C.compile_trace(trace(Shop(), Q, "site_search"), version=11)["tool"]
    assert cand["version"] == 11
    vs = Verdicts()
    try:
        judged_wrong(vs.s, bad, Q, run(site, bad, Q))
        st = Store(bad)
        try:
            assert ladder_run(site, bad, Q2, st.s)["status"] == "ok"           # a second known input (an LKG)
            seen = []

            def upgrade(t):
                seen.append(t["version"])
                return cand
            out = ladder_run(site, bad, Q, st.s, verdicts=vs.s, upgrade=upgrade,
                             checker=lambda t, v, o: {"verdict": "agrees"})
            lad = out["ladder"]
            assert out["status"] == "ok" and lad["outcome"] == L.UPGRADED and seen == [10], lad
            rev = st.s.revisions()[-1]
            assert rev["kind"] == "upgrade" and rev["tool"]["version"] == 11 and st.s.active_rev() == 1
            rows = {r["value"]: r for r in lad["evidence"]["upgrade"]["rows"]}
            assert rows[Q]["old"] == "known_wrong" and rows[Q]["new"] == "usable" and rows[Q]["change"] == "better"
            assert rows[Q2]["change"] in ("same", "better") and st.s.upgrades()[-1]["accepted"] is True
            assert st.s.rewrites_within(7) == 0 and "UPGRADED" in lad["line"]
        finally:
            st.close()
    finally:
        vs.close()


def test_an_upgrade_whose_answer_is_not_verified_is_never_reported():
    good, bad = teaser_tool()
    site = TeaserShop()
    cand = C.compile_trace(trace(Shop(), Q, "site_search"), version=11)["tool"]
    vs = Verdicts()
    try:
        judged_wrong(vs.s, bad, Q, run(site, bad, Q))
        st = Store(bad)
        try:
            # no checker: the candidate's new answer cannot be verified (known wrong -> unverified is no worse: the
            # upgrade is accepted) - and the run is never ok
            out = ladder_run(site, bad, Q, st.s, verdicts=vs.s, upgrade=lambda t: cand)
            lad = out["ladder"]
            rows = {r["value"]: r for r in lad["evidence"]["upgrade"]["rows"]}
            assert rows[Q]["old"] == "known_wrong" and rows[Q]["new"] == "unverified", rows
            assert out["status"] != "ok" and out.get("output") is None and lad["outcome"] != L.OK, lad
            assert st.s.active_rev() == 1 and lad["state"] == L.NEEDS_REPAIR
        finally:
            st.close()
    finally:
        vs.close()


def test_a_version_9_tool_is_flagged_and_upgraded():
    """The census's saved tools are versions 6-9: the ladder flags their known wrong answers, keeps their state and
    tries the upgrade gate (the repair tiers need version 10)."""
    good, bad = teaser_tool()
    bad9 = C.compile_trace(trace(Shop(), Q, "site_search"), version=9)["tool"]
    bad9["region"]["cards"] = bad["region"]["cards"]
    site = TeaserShop()
    cand = C.compile_trace(trace(Shop(), Q, "site_search"), version=12)["tool"]
    vs = Verdicts()
    try:
        judged_wrong(vs.s, bad9, Q, run(site, bad9, Q))
        st = Store(bad9)
        try:
            out = ladder_run(site, bad9, Q, st.s, verdicts=vs.s)
            lad = out["ladder"]
            assert out["status"] == V.KNOWN_WRONG and lad["state"] == L.NEEDS_REPAIR and "below version 10" in lad["why"]
            out = ladder_run(site, bad9, Q, st.s, verdicts=vs.s, upgrade=lambda t: cand,
                             checker=lambda t, v, o: {"verdict": "agrees"})
            lad = out["ladder"]
            assert out["status"] == "ok" and lad["outcome"] == L.UPGRADED and st.s.active_tool()["version"] == 12, lad
        finally:
            st.close()
    finally:
        vs.close()


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"test_repair_known_wrong: {n} ok")
