"""Tier 2's input design 2 (``repair_model.prompt2``), offline with a FAKE model adapter on the shop of
``test_repair_ladder.py`` (no network, no model).

What must hold:

1. the prompt shows where the last good answer's results are on the live page now (index, label, area, role, the
   repeated-container chain, the neighbours), the card candidates RANKED by the last-good results they read (the
   candidate tier 1 tried marked with its outcome), tier 1's near misses, the search forms when the search is a
   localized step, and few-shot examples of accepted repairs - and still never the tool's checks, fields' rules or
   hosts;
2. a design-2 proposer runs through the ladder exactly like design 1 (the closed language, the gate): a model that
   copies the top-ranked candidate repairs a renamed result layout, with tier 1's attempts passed as near misses;
3. design 1 is unchanged (``prompt``);
4. tier 2 is OUT of the default ladder (``TIER2_BY_DEFAULT``): with the default configuration a model is never called.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import repair_model as M  # noqa: E402
from anyuse.execution import repair_relocate as R  # noqa: E402
from test_repair_ladder import RENAMED, Shop, Store, compiled, ladder_run  # noqa: E402
from test_repair_tier2 import FakeAdapter  # noqa: E402

Q = "Akkuschrauber"
EXAMPLE = {"failure": "membership: no item of the compiled region on the result page",
           "evidence": "the last good results' links now sit in li.tile cards",
           "patch": {"ops": [{"op": "replace_anchor", "anchor": "cards", "value": [{"kind": "card", "chain": ["main", "div.grid", "ul.list", "li.tile"], "rels": ["a"], "roles": []}]}]}}


def probe(site, tool, step="region"):
    return asyncio.run(R.probe(D.DomPage(site), tool, Q, D.no_settle, step))


def test_the_prompt_shows_the_context_the_ranking_the_near_misses_and_examples():
    tool = compiled(Q, "site_search")
    site = Shop(cls=RENAMED)
    good = asyncio.run(T.run_tool(D.DomPage(Shop()), tool, Q, D.no_settle, result_wait_s=0.0))
    anchors = L.anchors_of("site_search", good)
    pr = probe(site, tool, "search")
    cands = M.ranked_candidates(pr["result"], tool, anchors)
    assert cands and cands[0]["anchors"] == 5 and cands[0]["before"] == 0, cands
    near = [{"tier": 1, "rule": "region", "patch": {"ops": [{"op": "replace_anchor", "anchor": "cards",
                                                             "value": [cands[0]["spec"]]}]},
             "verdict": "rejected", "why": "the runtime checks did not pass (failed: query)"}]
    out0 = {"status": "failed", "why": "membership: no item of the compiled region"}
    p = M.prompt2(tool, "region", out0, pr, anchors, near, [EXAMPLE], ["region", "search"])
    assert "WHERE THEY ARE ON THE LIVE PAGE NOW" in p and "neighbours:" in p and "chain=" in p
    assert "RANKED" in p and "5 last-good results" in p and "tried='rejected" in p
    assert "TIER 1 ALREADY TRIED" in p and "SEARCH FORMS on the page" in p and "Example 1:" in p
    assert "the other suspect step: SEARCH" in p
    for secret in ("\"checks\"", "\"fields\"", "\"hosts\"", "identity", "empty_state"):
        assert secret not in p, secret
    # design 1 is unchanged
    assert "RANKED" not in M.prompt(tool, "region", out0, pr, anchors)


def copy_top(prompt):
    line = next(x for x in prompt.splitlines() if x.startswith("C0:"))
    spec = json.loads(line.split("spec = ", 1)[1])
    return json.dumps({"ops": [{"op": "replace_anchor", "anchor": "cards", "value": [spec]}], "why": "top candidate"})


def test_a_design_2_proposer_through_the_ladder():
    tool = compiled(Q, "site_search")
    st = Store(tool)
    try:
        cfg = L.LadderConfig(enabled=True, tier1=False, tier2=True)
        assert ladder_run(Shop(), tool, Q, st.s, config=cfg)["status"] == "ok"
        ad = FakeAdapter(copy_top)
        model = M.CerebrasProposer(adapter=ad, design=2, examples=[EXAMPLE])
        out = ladder_run(Shop(cls=RENAMED), tool, Q, st.s, config=cfg, model=model)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == L.REPAIRED and lad["tier"] == 2, lad
        assert "Example 1:" in ad.prompts[0] and "WHERE THEY ARE ON THE LIVE PAGE NOW" in ad.prompts[0]
    finally:
        st.close()


def test_tier_2_is_out_of_the_default_ladder():
    """Tier 2 repaired 2 (design 2) / 3 (design 1) of the same 120 seeded samples - below 15 %: it is behind
    a flag; with the default configuration a model given to the ladder is never called."""
    assert L.TIER2_BY_DEFAULT is False and L.LadderConfig().tier2 is False and L.LadderConfig(enabled=True).tier2 is False
    tool = compiled(Q, "site_search")
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, Q, st.s)["status"] == "ok"
        ad = FakeAdapter(copy_top)
        out = ladder_run(Shop(cls=RENAMED), tool, Q, st.s, config=L.LadderConfig(enabled=True, tier1=False),
                         model=M.CerebrasProposer(adapter=ad, design=2))
        assert out["ladder"]["model_calls"] == 0 and not ad.prompts and out["ladder"]["outcome"] != L.REPAIRED
    finally:
        st.close()


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"test_repair_tier2_design2: {n} ok")
