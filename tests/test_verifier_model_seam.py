"""The verifier's model is asked only on a tool call (``attribute_verify.default_resolver``).

With a verifier model configured (``ANYUSE_MODEL_PROVIDER`` or ``ANYUSE_JUDGE_MODEL_PROVIDER``), the in-run verifier
of a saved web tool asks it about an attribute its rules leave open - but only inside a tool call (the ``product``
scope of ``model_check``, opened by the MCP web-tool route). A watch run, a proof's replay and a repair's re-proof
replay the same tool with the rules alone: 0 model calls, and what only the model could settle is withheld, never
accepted.

Offline: a fake shop served from generated pages, a fake configured verifier model that counts its questions.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from anyuse.execution import watch as WA  # noqa: E402
from anyuse.execution import web_tool_call as WT  # noqa: E402
import test_mcp_web_tools as TM  # noqa: E402
import test_repair_ladder as TL  # noqa: E402
import test_watch as TW  # noqa: E402

VALUE = "Bosch GSR 12V Akkuschrauber"


class OpenShop(TL.Shop):
    """The ladder's fake shop, whose product pages add a stock line no rule of the verifier reads to their buy box:
    the rules leave the availability open, a question for the verifier's model."""

    def product(self, slug):
        dom = super().product(slug)
        return dom.replace("<p>Auf Lager</p>", "<p>Auf Lager</p><p>Lagerbestand wird geprüft</p>") if dom else dom


class TextPage(D.DomPage):
    """A recorded page that also answers ``text()`` (the browser's visible text), as a live page does."""

    async def text(self):
        return str(self.read().get("text") or "")


class FakeModel:
    """A configured verifier model: answers every question with its first option and line 1, and counts."""

    def __init__(self):
        self.asked = []

    def __call__(self, prompt, schema):
        self.asked.append(prompt)
        return {"choice": schema["properties"]["choice"]["enum"][0], "line": 1, "calls": 1, "usd": 0.0}


@pytest.fixture()
def model(monkeypatch, tmp_path):
    monkeypatch.setenv(AV.SETTING, "on")
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    AV._DEFAULT.pop("r", None)
    fake = FakeModel()
    monkeypatch.setattr(AV, "model_configured", lambda: True)
    monkeypatch.setattr(AV, "configured_model", lambda budget_usd=None, adapter=None: fake)
    return fake


def run_once(value=VALUE):
    return asyncio.run(T.run_tool(TextPage(OpenShop()), TL.compiled(VALUE), value, D.no_settle, result_wait_s=0.0))


def test_the_default_resolver_asks_only_inside_a_tool_call(model):
    assert AV.default_resolver().model is None
    assert "asks no model" in AV.default_resolver().unavailable
    with MC.scope("product"):
        assert AV.default_resolver().model is model
        with AV.no_model("a proof replay asks no model"):
            r = AV.default_resolver()
            assert r.model is None and r.unavailable == "a proof replay asks no model"
    for name in ("compile", "heal"):
        with MC.scope(name):
            assert AV.default_resolver().model is None
    with MC.scope("agent"):
        assert AV.default_resolver().model is model


def test_a_tool_call_asks_the_model_and_a_plain_replay_does_not(model):
    with MC.scope("product"):
        asked = run_once()
    assert len(model.asked) >= 1, asked.get("checks")
    model.asked.clear()
    plain = run_once()
    assert model.asked == []
    assert plain["status"] != "ok" and AV.MODEL_UNAVAILABLE in plain["why"]


def test_the_mcp_route_opens_the_tool_call_scope(model):
    fx = TM.Fixture(tool=TL.compiled(VALUE), fallback=False)
    try:
        def replay(entry, value, caps, ladder=None):
            return asyncio.run(T.run_tool(TextPage(OpenShop()), entry["tool"], value, D.no_settle,
                                          result_wait_s=0.0))
        fx.route.replay = replay
        entry = WT.tool_specs(fx.storage)[0]["entry"]
        out = fx.route.call(entry, {"value": VALUE})
        assert len(model.asked) >= 1, out
        assert MC.current_scope() == ""                     # the scope ends with the call
    finally:
        fx.close()


def test_a_watch_run_makes_no_model_call(model):
    calls = []

    def replay(item, tool, caps):
        calls.append(item["id"])
        with MC.scope("product"):                           # even a scope left open around the replay is closed
            return run_once(str(item["input"]))
    with TW.Store() as s:
        s.st.add(s.tool(TL.compiled(VALUE)), VALUE, "1h")
        rep = WA.run_watch(s.st, replay, now=TW.hours(0))
    assert calls and model.asked == []
    assert rep["model_calls"]["replay"] == 0
    assert rep["entries"][0]["kind"] == "drift", rep["entries"][0]


def test_a_proof_replay_makes_no_model_call(model):
    tool = TL.compiled(VALUE)

    def replay(t, _site, value):
        with MC.scope("product"):
            out = asyncio.run(T.run_tool(TextPage(OpenShop()), t, value, D.no_settle, result_wait_s=0.0))
        out["model_calls"] = len(model.asked)
        return out
    proof = HC.prove_web(tool, None, ["Makita DF333 Akkuschrauber", "Metabo PowerMaxx Akkuschrauber"],
                         demonstrated=VALUE, replay=replay, independent=False)
    assert model.asked == []
    assert not proof["passed"]
    assert all(c.get("model_calls", 0) == 0 for c in proof["cases"]), proof["cases"]
    assert any(AV.MODEL_UNAVAILABLE in str(c.get("why")) or "attribute verify" in str(c.get("why"))
               for c in proof["cases"]), proof["cases"]


def test_a_repairs_reproof_makes_no_model_call(model):
    from anyuse.execution import repair_ladder as L
    store = TL.Store(TL.compiled(VALUE))
    try:
        ladder = L._Ladder(lambda: TextPage(OpenShop()), D.no_settle, L.LadderConfig(enabled=True, pacing=False),
                           store.s, None, None, None, None, {"result_wait_s": 0.0})

        async def go():
            with MC.scope("product"):
                first = await ladder._run(store.s.active_tool(), VALUE)
                n0 = len(model.asked)
                ladder.reproving = True
                await ladder._run(store.s.active_tool(), VALUE)
                return first, n0
        first, n0 = asyncio.run(go())
        assert n0 >= 1                                      # the tool call's own answer is checked with the model
        assert len(model.asked) == n0                       # the re-proof's run asks nothing more
    finally:
        store.close()


def test_the_repair_checker_asks_no_model_without_an_injected_resolver(model):
    from anyuse.execution import repair_check as RCK
    page = TextPage(OpenShop())

    async def item():
        await page.goto(f"{TL.BASE}/p/bosch-gsr-12v")
        r = page.read()
        return {"url": r.get("url"), "title": r.get("title"), "h1": r.get("h1"), "text": r.get("text"),
                "dom": await page.dom()}
    it = asyncio.run(item())
    out = {"status": "ok", "output": {"name": VALUE, "price": 89.9, "currency": "EUR", "availability": "InStock",
                                      "url": it["url"]}}
    ev = {"links": [{"url": it["url"], "text": VALUE}]}
    with MC.scope("product"):
        j = RCK.judge({"workflow": "product_lookup"}, VALUE, out, ev, {it["url"]: it}, attribute_verify=True)
    assert model.asked == []
    assert j["verdict"] == "disagrees" and AV.MODEL_UNAVAILABLE in str(j.get("attribute_verify"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
