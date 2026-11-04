"""The PROMO SENTINEL of the repair ladder (``repair_ladder.promo_results``, ``_Ladder._promo_sentinel``),
offline on the fake shop of ``test_demo_promo.py`` (no browser, no network, no model).

The drift simulator's held-out false accept (www.wintotal.de, ``insert_block`` seed 3): a block of cloned result
cards in a promotion container before the results; a version-10 tool read the clones, its runtime checks accepted the
answer and the answer did not collapse against the last known good one, so the ladder's sentinel did not flag it.

What must hold:

1. ladder ON, a tool older than version 12: the answered results that are cards of a promotion / advertising /
   recommendation block beside the results (version 12's rule, read on the result page the run just showed - a
   local read, no request) are flagged: the answer is never accepted ("incomplete", the answer kept as evidence);
2. with a newer compile (``upgrade``: the version-12 tool), the upgrade gate accepts it - the results again, the
   same answer as the last known good one;
3. no promo block: no flag; a version-12 tool leaves the block out itself (no flag needed); ladder OFF: exactly
   ``run_tool`` (the clones answered).
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_demo_promo import Q, PlainShop, PromoShop  # noqa: E402
from test_repair_ladder import Store, ladder_run, trace  # noqa: E402


def tool(version):
    g = C.compile_trace(trace(PlainShop(), Q, "site_search"), version=version)
    assert g["ok"], g.get("why")
    return g["tool"]


def urls(out):
    return [r["url"].rsplit("/", 1)[-1] for r in ((out.get("output") or out.get("unverified_output") or {})
                                                  .get("results") or [])]


def test_the_promo_sentinel_flags_cloned_cards():
    t10 = tool(10)
    st = Store(t10)
    try:
        out = ladder_run(PlainShop(), t10, Q, st.s)
        real = urls(out)
        assert out["status"] == "ok" and out["ladder"]["outcome"] == L.OK and len(real) == 5
        out = ladder_run(PromoShop("recommendations"), t10, Q, st.s)
        lad = out["ladder"]
        assert out["status"] == "incomplete" and "promotion" in lad["evidence"]["sentinel"], lad
        assert urls(out)[:3] == ["akku-bits", "akku-koffer", "akku-lader"] and out["output"] is None
        assert lad["outcome"] in (L.NEEDS_HUMAN, L.UNVERIFIED) and st.s.lkg(Q)["count"] == 5
        # the upgrade to version 12: accepted - the results again (the last known good answer)
        out = ladder_run(PromoShop("recommendations"), t10, Q, st.s, upgrade=lambda t: tool(12))
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == L.UPGRADED and urls(out) == real, lad
        assert st.s.active_tool()["version"] == 12
    finally:
        st.close()


def test_no_flag_without_a_block_with_version_12_and_with_the_ladder_off():
    t10, t12 = tool(10), tool(12)
    st = Store(t10)
    try:
        assert ladder_run(PlainShop(), t10, Q, st.s)["ladder"]["outcome"] == L.OK
        out = ladder_run(PlainShop(), t10, Q, st.s)
        assert out["status"] == "ok" and "sentinel" not in out["ladder"]["evidence"]
    finally:
        st.close()
    st = Store(t12)
    try:
        out = ladder_run(PromoShop("recommendations"), t12, Q, st.s)
        assert out["status"] == "ok" and out["ladder"]["outcome"] == L.OK and "akku-bits" not in urls(out)
    finally:
        st.close()
    off = asyncio.run(L.run_with_ladder(lambda: D.DomPage(PromoShop("recommendations")), t10, Q, D.no_settle,
                                        result_wait_s=0.0))
    assert off["status"] == "ok" and urls(off)[:3] == ["akku-bits", "akku-koffer", "akku-lader"] and "ladder" not in off
    # the rule alone
    site = PromoShop("sponsored-items")
    rec, u = site.route(f"https://www.ladder-shop.test/search?q={Q}")
    got = L.promo_results(t10, rec.dom, u, ["https://www.ladder-shop.test/p/akku-bits",
                                           "https://www.ladder-shop.test/p/bosch-gsr-12v"])
    assert got == ["https://www.ladder-shop.test/p/akku-bits"]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"test_repair_promo: {n} ok")
