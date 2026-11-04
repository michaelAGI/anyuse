"""THE SAFE UPGRADE GATE (``execution/repair_upgrade.py``), offline on the fake shop of
``test_repair_ladder.py`` (no browser, no network, no model).

What must hold:

1. the per-input order: usable -> anything else is WORSE; failure / unverified -> known wrong is WORSE; known wrong ->
   usable or an explicit failure is BETTER; failure / unverified -> usable is BETTER; a run that could not be judged
   (a block) leaves the input UNDECIDED; ``decide`` accepts only when every input is decided and none is worse;
2. the boundary: a candidate must be NEWER and the same tool (workflow, site, bound parameters, no new host);
3. ``evaluate``: a candidate worse on one known input is refused - the old revision stays active, the decision is
   logged and the same candidate is not tried again for that revision; a candidate no worse anywhere becomes the
   active revision (kind ``upgrade``, its usable answers the new last known good ones), not counted against the cap on
   automatic repairs; a pinned tool is never upgraded.
"""
import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import copy
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_upgrade as U  # noqa: E402
from test_repair_ladder import Shop, Store, compiled, ladder_run, trace  # noqa: E402

V1, V2 = "Bosch GSR 12V Akkuschrauber", "Makita DF333 Akkuschrauber"


def test_the_per_input_order_and_the_decision():
    assert U.compare(U.USABLE, U.FAILURE) == U.WORSE and U.compare(U.USABLE, U.UNVERIFIED) == U.WORSE
    assert U.compare(U.USABLE, U.KNOWN_WRONG) == U.WORSE and U.compare(U.FAILURE, U.KNOWN_WRONG) == U.WORSE
    assert U.compare(U.KNOWN_WRONG, U.FAILURE) == U.BETTER and U.compare(U.KNOWN_WRONG, U.USABLE) == U.BETTER
    assert U.compare(U.FAILURE, U.USABLE) == U.BETTER and U.compare(U.FAILURE, U.UNVERIFIED) == U.SAME
    assert U.compare(U.KNOWN_WRONG, U.UNVERIFIED) == U.SAME and U.compare(U.USABLE, U.USABLE) == U.SAME
    assert U.outcome_class({"status": "ok"}, verified=True) == U.USABLE
    assert U.outcome_class({"status": "ok"}) == U.UNVERIFIED
    assert U.outcome_class({"status": "known_wrong"}) == U.KNOWN_WRONG
    assert U.outcome_class({"status": "incomplete"}) == U.FAILURE and U.outcome_class({"status": "blocked"}) == U.UNDECIDED
    rows = [{"value": "a", "old": U.KNOWN_WRONG, "new": U.FAILURE}, {"value": "b", "old": U.USABLE, "new": U.USABLE}]
    assert U.decide(rows)["accepted"] is True and U.decide(rows)["better"] == 1
    assert U.decide(rows + [{"value": "c", "old": U.USABLE, "new": U.FAILURE}])["accepted"] is False
    assert U.decide(rows + [{"value": "d", "old": U.USABLE, "new": U.UNDECIDED}])["accepted"] is False
    assert U.decide([])["accepted"] is False


def test_the_boundary():
    t10 = compiled(V1)
    t11 = C.compile_trace(trace(Shop(), V1), version=11)["tool"]
    assert U.upgrade_boundary(t10, t11) == ""
    assert "not newer" in U.upgrade_boundary(t11, t10) and "not newer" in U.upgrade_boundary(t10, t10)
    assert "workflow" in U.upgrade_boundary(t10, dict(t11, workflow="site_search"))
    other = copy.deepcopy(t11)
    other["hosts"] = list(other.get("hosts") or []) + ["cdn.elsewhere.test"]
    other["site_hosts"] = {other["site"]: ["cdn.elsewhere.test"]}
    assert "host" in U.upgrade_boundary(t10, other) or C.site_hosts_policy(other) == C.site_hosts_policy(t10)


def runner(site):
    async def run(tool, value):
        out = await T.run_tool(D.DomPage(site), tool, value, D.no_settle, result_wait_s=0.0)
        out.pop("seconds", None)
        return out
    return run


def test_evaluate_refuses_a_worse_candidate_and_accepts_a_safe_one():
    t10 = compiled(V1)
    site = Shop()
    st = Store(t10)
    try:
        for v in (V1, V2):
            assert ladder_run(site, t10, v, st.s)["status"] == "ok"
        # a candidate whose run on the second known input fails (served a page its cards do not read): worse on V2
        bad = C.compile_trace(trace(site, V1), version=11)["tool"]
        bad["top_k"] = int(bad.get("top_k") or 5)
        plain = runner(site)

        async def run(tool, value):
            if C.tool_digest(tool) == C.tool_digest(bad) and value == V2:
                return await runner(Shop(cls={"product": "tile"}))(tool, value)
            return await plain(tool, value)
        res = asyncio.run(U.evaluate(st.s, bad, run))
        rows = {r["value"]: r for r in res["rows"]}
        assert res["accepted"] is False and rows[V2]["change"] == U.WORSE and st.s.active_rev() == 0, res["why"]
        assert st.s.upgrades()[-1]["accepted"] is False and st.s.upgrade_decided(C.tool_digest(bad))
        again = asyncio.run(U.evaluate(st.s, bad, run))
        assert again.get("decided_before") is True and len(st.s.upgrades()) == 1
        # the plain version-11 compile: no input worse -> the active revision, kind "upgrade"
        good = C.compile_trace(trace(site, V1), version=11)["tool"]
        res = asyncio.run(U.evaluate(st.s, good, runner(site)))
        assert res["accepted"] is True and res["rev"] == 1 and st.s.active_rev() == 1, res["why"]
        rev = st.s.revisions()[-1]
        assert rev["kind"] == "upgrade" and rev["tier"] == 0 and rev["diff"]["upgrade"]["to_version"] == 11
        assert st.s.rewrites_within(7) == 0 and st.s.lkg(V1)["rev"] == 1
        assert ladder_run(site, t10, V2, st.s)["ladder"]["revision"] == 1
    finally:
        st.close()
    # a pinned tool is never upgraded
    st = Store(t10)
    try:
        assert ladder_run(site, t10, V1, st.s)["status"] == "ok"
        st.s.pin()
        res = asyncio.run(U.evaluate(st.s, C.compile_trace(trace(site, V1), version=11)["tool"], runner(site)))
        assert res["accepted"] is False and "pinned" in res["why"] and st.s.active_rev() == 0
    finally:
        st.close()


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"test_repair_upgrade: {n} ok")
