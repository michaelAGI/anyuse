"""Tier 2 of the repair ladder (``execution/repair_model.py``), offline: the fake
shop of ``test_repair_ladder`` and a FAKE Cerebras adapter (no network, no key, no model).

What must hold:

1. the model's patch (a card spec copied from the prompt's CARD CANDIDATES) is validated, applied to a copy, run and
   gated; accepted, it is revision 1 with tier 2, its calls and its cost in the revision and in the cost ledger;
2. ADVERSARIAL model answers through the whole ladder - a change of the checks, the hosts, the page cap, a POST, a
   login, a cart, another host, free text - are never applied: the outcome is needs_human, the active revision and the
   tool's checks / hosts are unchanged, and each call is still in the cost ledger;
3. the daily budget: at most 3 tier-2 calls per tool per day (a 4th failing run that day skips tier 2), again the next
   day;
4. a model patch whose card structure reads a SIDEBAR (or a block before the anchored results) is refused before it
   runs (``structure_guard``, fixed code) - even when its answer would equal the last known good one; the prompt's
   card candidates never offer a sidebar structure;
5. the prompt shows the failing reason, the candidates and the element table - never the tool's checks, fields' rules
   or hosts; ``patch_from_text`` takes the first JSON object holding operations; a non-Cerebras adapter (OpenRouter) is
   refused.
"""
import datetime as dt
import json
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import repair_model as M  # noqa: E402
from test_repair_ladder import BASE, PRODUCTS, RENAMED, Shop, Store, compiled, ladder_run  # noqa: E402
from test_repair_state import Clock  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"


class FakeAdapter:
    """The model adapter's surface: ``complete`` answers with ``answer(prompt)``; usage 3000 / 400 tokens."""

    def __init__(self, answer, provider="cerebras", base_url="https://api.cerebras.ai/v1"):
        self.config = SimpleNamespace(provider=provider, base_url=base_url, model="gpt-oss-120b")
        self.price_in, self.price_out = 0.25, 0.69
        self.answer, self.prompts = answer, []

    def complete(self, messages, temperature=0.0, max_tokens=None, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        return SimpleNamespace(ok=True, text=self.answer(p), error="", llm_calls=1,
                               usage=SimpleNamespace(prompt_tokens=3000, completion_tokens=400))


def copy_candidate(prompt):
    """A well-behaved model: it copies the first card candidate's spec."""
    line = next(x for x in prompt.splitlines() if x.startswith("C0:"))
    spec = json.loads(line.split("spec = ", 1)[1])
    return "Here is the patch:\n" + json.dumps({"ops": [{"op": "replace_anchor", "anchor": "cards", "value": [spec]}],
                                                "why": "the results moved into new containers"})


def cfg(**kw):
    # tier 2 is behind a flag (``repair_ladder.TIER2_BY_DEFAULT``)
    return L.LadderConfig(enabled=True, tier1=False, tier2=True, **kw)


def test_a_model_patch_is_validated_applied_gated_and_kept():
    tool = compiled(V)
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, V, st.s, config=cfg())["status"] == "ok"
        ad = FakeAdapter(copy_candidate)
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg(), model=M.CerebrasProposer(adapter=ad))
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == "repaired" and lad["tier"] == 2, lad
        usd = round((3000 * 0.25 + 400 * 0.69) / 1e6, 6)
        assert lad["model_calls"] == 1 and lad["usd"] == usd and f"US${usd:.4f}" in lad["line"]
        rev = st.s.revisions()[-1]
        assert rev["tier"] == 2 and rev["calls"] == 1 and rev["cost_usd"] == usd and rev["diff"]["ops"][0]["anchor"] == "cards"
        c = st.s.costs()
        assert len(c) == 1 and c[0]["tier"] == 2 and c[0]["prompt_tokens"] == 3000 and c[0]["usd"] == usd
    finally:
        st.close()


ATTACKS = [
    {"ops": [{"op": "replace_anchor", "anchor": "checks", "value": []}], "why": "drop the checks"},
    {"ops": [{"op": "replace_anchor", "anchor": "hosts", "value": ["evil.test"]}]},
    {"ops": [{"op": "replace_anchor", "anchor": "max_pages", "value": 999}]},
    {"ops": [{"op": "insert_nav_step", "at": "after_search", "method": "POST", "link": {"text": "Go", "path": "/search"}}]},
    {"ops": [{"op": "insert_nav_step", "at": "after_search", "link": {"text": "Anmelden", "path": "/login"}}]},
    {"ops": [{"op": "insert_nav_step", "at": "after_search", "link": {"text": "Kaufen", "path": "/cart/add"}}]},
    {"ops": [{"op": "rebind_param", "param": "q", "to": {"in": "query", "key": "q", "path": "https://evil.test/s"}}]},
    {"ops": [{"op": "submit_form", "form": 1, "fields": {"user": "a", "pw": "b"}}]},
    {"ops": [{"op": "set", "path": "fields.price", "value": "text"}]},
]


def test_adversarial_model_answers_are_never_applied():
    tool = compiled(V)
    clock = Clock()
    for k, attack in enumerate(ATTACKS + ["I cannot help with that."]):
        st = Store(tool, clock=clock)
        try:
            assert ladder_run(Shop(), tool, V, st.s, config=cfg())["status"] == "ok"
            text = attack if isinstance(attack, str) else json.dumps(attack)
            out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg(),
                             model=M.CerebrasProposer(adapter=FakeAdapter(lambda p, t=text: t)))
            lad = out["ladder"]
            assert lad["outcome"] == "needs_human" and out["status"] == "failed", (k, lad)
            att = [a for a in lad["evidence"]["attempts"] if a.get("tier") == 2]
            assert att and att[-1]["verdict"] == "invalid", (k, att)
            assert st.s.active_rev() == 0 and len(st.s.revisions()) == 1
            active = st.s.active_tool()
            assert active["checks"] == tool["checks"] and active["hosts"] == tool["hosts"]
            assert len(st.s.costs()) == 1 and lad["model_calls"] == 1
        finally:
            st.close()


def test_the_daily_tier2_budget():
    tool = compiled(V)
    clock = Clock()
    st = Store(tool, clock=clock)
    try:
        ad = FakeAdapter(lambda p: json.dumps({"ops": [], "why": "nothing to do"}))
        model = M.CerebrasProposer(adapter=ad)
        tiers = []
        for _ in range(4):
            out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg(), model=model)
            tiers.append([a.get("tier") for a in out["ladder"]["evidence"]["attempts"]])
        assert len(ad.prompts) == 3 and st.s.used_today(2) == 3
        assert any("daily budget" in str(a.get("skipped")) for a in out["ladder"]["evidence"]["attempts"])
        clock.advance(days=1)
        ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg(), model=model)
        assert len(ad.prompts) == 4 and st.s.used_today(2) == 1
    finally:
        st.close()


def test_the_prompt_the_parser_and_the_provider_guard():
    tool = compiled(V)
    ad = FakeAdapter(copy_candidate)
    st = Store(tool)
    try:
        ladder_run(Shop(), tool, V, st.s, config=cfg())
        ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg(), model=M.CerebrasProposer(adapter=ad))
    finally:
        st.close()
    p = ad.prompts[0]
    assert "failed at its REGION step" in p and "membership" in p and "CARD CANDIDATES" in p and "ELEMENT TABLE" in p
    assert "/p/bosch-gsr-12v" in p and V in p
    spec = json.loads(p.split("The tool's spec:\n", 1)[1].split("\n\n", 1)[0])
    assert "checks" not in spec and "hosts" not in spec and "fields" not in spec
    assert M.patch_from_text('x {"a": 1} y {"ops": [{"op": "skip_nav_step", "index": 0}]} {"ops": [1]}')["ops"][0]["op"] == \
        "skip_nav_step"
    assert M.patch_from_text('{"ops": []}') is None and M.patch_from_text("no json") is None
    for bad in (FakeAdapter(copy_candidate, provider="openrouter", base_url="https://openrouter.ai/api/v1"),
                FakeAdapter(copy_candidate, base_url="https://openrouter.ai/api/v1")):
        try:
            M.CerebrasProposer(adapter=bad)
            raise AssertionError("a non-Cerebras adapter was accepted")
        except ValueError:
            pass


class SidebarShop(Shop):
    """A sidebar of other products in the results' own markup, beside the results."""

    def results(self, q):
        side = ("<aside class=\"sidebar\"><h3>Beliebt</h3><ul class=\"" + self.c("products") + "\">" + "".join(
            f"<li class=\"{self.c('product')}\"><div class=\"{self.c('info')}\"><h3 class=\"{self.c('name')}\">"
            f"<a class=\"{self.c('title')}\" href=\"/p/{sl}\">{n}</a></h3></div></li>" for n, _p, sl in PRODUCTS[-3:])
            + "</ul></aside>")
        return super().results(q).replace("</main>", side + "</main>")


def test_a_sidebar_structure_is_refused():
    from anyuse.execution import demo_cards as K
    tool = compiled(V)
    drift = SidebarShop(cls=RENAMED)
    rec, url = drift.route(BASE + "/search?q=" + V.replace(" ", "+"))
    root, _b = K.parse(rec.dom)
    li = next(x for x in K.walk(root) if x.tag == "li" and x.parent is not None and x.parent.parent is not None and
              x.parent.parent.tag == "aside")
    a = next(x for x in K.walk(li) if x.tag == "a")
    side = {"kind": "card", "chain": K.chain_of(li), "rels": [K.rel_path(a, li)],
            "roles": [{"anchor": K.desc(a), "heading": True}]}
    answer = json.dumps({"ops": [{"op": "replace_anchor", "anchor": "cards", "value": [side]}], "why": "x"})
    st = Store(tool)
    try:
        assert ladder_run(SidebarShop(), tool, V, st.s, config=cfg())["status"] == "ok"
        out = ladder_run(drift, tool, V, st.s, config=cfg(), model=M.CerebrasProposer(adapter=FakeAdapter(lambda p: answer)))
        att = [x for x in out["ladder"]["evidence"]["attempts"] if x.get("tier") == 2]
        assert att and att[-1]["verdict"] == "refused" and "sidebar" in att[-1]["why"], att
        assert out["ladder"]["outcome"] != "repaired" and st.s.active_rev() == 0
        ad = FakeAdapter(copy_candidate)
        ladder_run(drift, tool, V, st.s, config=cfg(), model=M.CerebrasProposer(adapter=ad))
        assert "aside.sidebar" not in ad.prompts[0].split("CARD CANDIDATES", 1)[1].split("ELEMENT TABLE", 1)[0]
    finally:
        st.close()


if __name__ == "__main__":
    test_a_model_patch_is_validated_applied_gated_and_kept()
    test_adversarial_model_answers_are_never_applied()
    test_the_daily_tier2_budget()
    test_the_prompt_the_parser_and_the_provider_guard()
    test_a_sidebar_structure_is_refused()
    print("test_repair_tier2: 5 ok")
