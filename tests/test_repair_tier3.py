"""Tier 3 of the repair ladder (``repair_ladder._Ladder._tier3``), offline with a FAKE
demonstrator (no browser, no CLI, no model): the live re-demonstration comes later.

What must hold:

1. a drift the lower tiers do not repair here (tiers 1 and 2 off; the shop renamed its result classes AND moved its
   product pages to another URL pattern) climbs to tier 3: the demonstrator gets the goal and the old tool as a hint (its search, the failure, the
   last known good results); its trace is recompiled at version 10; the new tool's answer differs from the LKG (the
   URLs moved), so the independent checker decides - without it "changed, unverified", with it a revision of tier 3
   (the recompiled trace's digest and the changed keys as its diff, the cost in the ledger);
2. a recompiled tool that adds a host to the origin policy (or changes the workflow or the checks) is refused;
3. at most 1 re-demonstration per tool per day.
"""
import copy
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import RENAMED, Shop, Store, compiled, ladder_run, trace  # noqa: E402
from test_repair_state import Clock  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"


class Demonstrator:
    """A fake demonstrator loop: records its goal and hint, answers a demonstration on ``site`` (``mutate`` edits it)."""

    def __init__(self, site, mutate=None):
        self.site, self.mutate, self.calls = site, mutate, []

    def __call__(self, goal, hint):
        self.calls.append((goal, hint))
        tr = trace(self.site, V)
        if self.mutate:
            self.mutate(tr)
        return {"trace": tr, "calls": 4, "usd": 0.12}


def cfg(**kw):
    return L.LadderConfig(enabled=True, tier1=False, tier2=False, **dict({"tier3": True}, **kw))


MOVED = dict(ppath="/produkt/", cls=RENAMED)


def test_a_redemonstration_is_recompiled_and_gated():
    tool = compiled(V)
    moved = Shop(**MOVED)
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
        dem = Demonstrator(moved)
        out = ladder_run(moved, tool, V, st.s, config=cfg(), demonstrator=dem)
        lad = out["ladder"]
        assert lad["outcome"] == "changed_unverified" and lad["tier"] == 3 and st.s.active_rev() == 0, lad
        goal, hint = dem.calls[0]
        assert V in goal and hint["tool"]["search"] == tool["search"] and hint["failure"]["status"] == "failed"
        assert hint["last_known_good"][0][0].endswith("/p/bosch-gsr-12v")
        assert lad["usd"] == 0.12 and st.s.costs()[-1]["tier"] == 3
    finally:
        st.close()
    st = Store(tool, clock=Clock())
    try:
        assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
        out = ladder_run(moved, tool, V, st.s, config=cfg(), demonstrator=Demonstrator(moved),
                         checker=lambda t, v, o: {"verdict": "agrees"})
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 3, lad
        assert out["output"]["url"].endswith("/produkt/bosch-gsr-12v")
        rev = st.s.revisions()[-1]
        assert rev["tier"] == 3 and "region" in rev["diff"]["changed"] and rev["diff"]["recompiled_from"]
        assert rev["evidence"]["gate"]["checker"]["verdict"] == "agrees" and rev["cost_usd"] == 0.12
    finally:
        st.close()


def test_a_recompiled_tool_that_leaves_the_boundary_is_refused():
    tool = compiled(V)
    moved = Shop(**MOVED)

    def new_host(tr):
        tr["final_page"] = dict(tr["final_page"], url=tr["final_page"]["url"].replace("www.", "cdn."))
        tr["steps"][-1]["page"] = tr["final_page"]
    for mutate, why in ((new_host, "adds a host"),
                        (lambda tr: tr.__setitem__("workflow", "site_search"), "changes 'workflow'")):
        st = Store(tool)
        try:
            ladder_run(Shop(), tool, V, st.s)
            out = ladder_run(moved, tool, V, st.s, config=cfg(), demonstrator=Demonstrator(moved, mutate),
                             checker=lambda t, v, o: {"verdict": "agrees"})
            att = [a for a in out["ladder"]["evidence"]["attempts"] if a.get("tier") == 3]
            assert out["ladder"]["outcome"] == "needs_human" and att[0]["verdict"] == "invalid", out["ladder"]
            assert why in att[0]["why"] or "does not compile" in att[0]["why"], att
            assert st.s.active_rev() == 0
        finally:
            st.close()
    bad = copy.deepcopy(tool)
    bad["checks"] = bad["checks"][:2]
    assert "checks" in L.tier3_boundary(tool, bad)
    bad = copy.deepcopy(tool)
    bad["version"] = 9
    assert L.tier3_boundary(tool, bad)


def test_one_redemonstration_per_day():
    tool = compiled(V)
    moved = Shop(**MOVED)
    clock = Clock()
    st = Store(tool, clock=clock)
    try:
        ladder_run(Shop(), tool, V, st.s)
        dem = Demonstrator(moved)
        ladder_run(moved, tool, V, st.s, config=cfg(), demonstrator=dem)
        out = ladder_run(moved, tool, V, st.s, config=cfg(), demonstrator=dem)
        assert len(dem.calls) == 1 and out["ladder"]["outcome"] == "needs_human"
        assert any("daily budget" in str(a.get("skipped")) for a in out["ladder"]["evidence"]["attempts"])
        clock.advance(days=1)
        ladder_run(moved, tool, V, st.s, config=cfg(), demonstrator=dem)
        assert len(dem.calls) == 2
    finally:
        st.close()


def test_tier3_is_off_by_default():
    """Tier 3 is out of the default ladder - with the default switches a demonstrator is
    never asked and the case goes to a human (US$0); ``tier3=True`` keeps it behind a flag."""
    assert L.TIER3_BY_DEFAULT is False and L.LadderConfig().tier3 is False and L.LadderConfig(enabled=True).tier3 is False
    tool = compiled(V)
    moved = Shop(**MOVED)
    st = Store(tool)
    try:
        ladder_run(Shop(), tool, V, st.s)
        dem = Demonstrator(moved)
        out = ladder_run(moved, tool, V, st.s, config=L.LadderConfig(enabled=True, tier1=False), demonstrator=dem)
        assert dem.calls == [] and out["ladder"]["outcome"] == "needs_human" and out["ladder"]["usd"] == 0.0
        assert st.s.costs() == []
    finally:
        st.close()


if __name__ == "__main__":
    test_a_redemonstration_is_recompiled_and_gated()
    test_a_recompiled_tool_that_leaves_the_boundary_is_refused()
    test_one_redemonstration_per_day()
    test_tier3_is_off_by_default()
    print("test_repair_tier3: 4 ok")
