"""A saved web tool's call is a tool call for the configured check model (``execution/web_tool_call.py``).

What must hold:

1. a version-15 tool's call over MCP asks the check model about its result page inside the call's ``product`` scope,
   and the call's ``model_calls`` and ``cost_usd`` count every question; a version-14 tool asks nothing; a question
   the fallback agent asks during its run counts once (its run counts it already, by the process's model-call
   counter);
2. what runs without a model stays without one, also inside a tool call or under a scope opened around it or inside
   it: the repair ladder's re-proof and its canary's run of another input, a proof's replay, a watch run.

Offline: ``test_demo_compile``'s fake shop, whose search for the input states in Vietnamese (no phrase list holds it)
that it found nothing; the check model faked with ``model_check.using``; no browser, no network.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from anyuse.execution import watch as WA  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402
from anyuse.execution import web_tool_call as WT  # noqa: E402
import test_demo_compile as TD  # noqa: E402
import test_mcp_web_tools as TM  # noqa: E402
import test_repair_ladder as TL  # noqa: E402
import test_watch as TW  # noqa: E402

VALUE = "xyzzy"
VI = "Tìm kiếm\nKhông tìm thấy sản phẩm nào cho “xyzzy”\nGợi ý cho bạn"


class EmptyPage(TD.FakeShopPage):
    """The shop's search for ``VALUE`` lists nothing and says so in Vietnamese."""

    async def js(self, expr, read_only=False):
        r = await super().js(expr, read_only)
        if expr == RR.READ_JS and "/search" in self.cur and isinstance(r, dict):
            r = dict(r, text=VI, main_text=VI, elements=[e for e in r["elements"] if e.get("area") != "main"])
        return r


class Asked:
    """The check model: the empty statement is the line that says nothing was found; one cent per question."""

    def __init__(self):
        self.lines = []

    def __call__(self, instructions, state, obs):
        line = state.rsplit(">> ", 1)[-1]
        self.lines.append(line)
        return 0.95 if "Không tìm thấy" in line else 0.05, 1, 0.01, ""


async def _settle(page):
    return None


def replay_on(page_factory):
    def replay(entry, value, caps, ladder=None):
        return asyncio.run(T.run_tool(page_factory(), entry["tool"], value, _settle, result_wait_s=0.0))
    return replay


@pytest.fixture()
def tools(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    t15 = C.compile_trace(TD.product_trace())["tool"]
    t14 = C.compile_trace(TD.product_trace(), version=14)["tool"]
    return t15, t14


def test_a_version_15_tools_call_asks_the_check_model_and_counts_its_questions(tools):
    t15, t14 = tools
    model = Asked()
    for tool, asks in ((t15, True), (t14, False)):
        model.lines.clear()
        fx = TM.Fixture(tool=tool, fallback=False)
        try:
            fx.route.replay = replay_on(EmptyPage)
            name = WT.tool_specs(fx.storage)[0]["name"]
            with MC.using(model):
                out = TM.call(fx.server(), name, {"value": VALUE})
            if asks:
                assert out["ok"] and out["status"] == "no_results", out
                assert model.lines and any("Không tìm thấy" in ln for ln in model.lines), model.lines
                assert out["model_calls"] == len(model.lines) and out["cost_usd"] == round(0.01 * len(model.lines), 6)
            else:
                assert not out["ok"] and model.lines == [] and out["model_calls"] == 0, out
            assert MC.current_scope() == ""
        finally:
            fx.close()


def test_a_check_model_question_the_fallback_agent_asks_is_counted_once(tools):
    from anyuse.execution import dom_page as D
    from anyuse.execution import harness_policies as P
    from anyuse.execution import model_adapter as MA
    question = MC.Question("fallback_probe", "Is this the product's own page?")
    asked = []

    def model(instructions, state, obs):
        # a backend notes every request in the process's model-call counter, which the agent run counts by
        MA.note_external_model_call("fake", "fake", feature="model_check")
        asked.append(state)
        return 0.95, 1, 0.0, ""

    def asks(item):
        def step(task, obs, history):
            MC.check(question, "", (), state="page")       # a question the agent's own step puts to the check model
            return item(task, obs, history)
        return step

    def fallback(entry, value, caps):
        pol = P.ScriptedPolicy([asks(TM.click_to("/p/bosch")),
                                TM.done(name=TM.NAME, price="89,90 €", availability="Auf Lager", url=TM.PURL)])
        return WT.run_fallback(D.DomPage(TM.site()), entry["tool"], value, policy=pol, model=None)
    fx = TM.Fixture(fallback=True)
    try:
        fx.next_replay = dict(TM.ok_record(), status="failed", accepted=False, output=None,
                              why="identity: the result names another product")
        fx.route.fallback = fallback
        name = WT.tool_specs(fx.storage)[0]["name"]
        with MC.using(model):
            out = TM.call(fx.server(), name, {"value": TM.NAME})
        assert out["ok"] and out["source"] == "fallback", out
        assert len(asked) == 1 and out["fallback"]["model_calls"] == 1 and out["model_calls"] == 1, out
    finally:
        fx.close()


def test_a_re_proof_and_the_canarys_run_inside_the_call_ask_no_check_model(tools):
    from anyuse.execution import repair_ladder as L
    t15, _t14 = tools
    store = TL.Store(t15)
    model = Asked()
    try:
        ladder = L._Ladder(lambda: EmptyPage(), _settle, L.LadderConfig(enabled=True, pacing=False), store.s, None,
                           None, None, None, {"result_wait_s": 0.0})

        async def go():
            first = await ladder._run(store.s.active_tool(), VALUE)
            n0 = len(model.lines)
            ladder.reproving = True                         # a repair candidate, the gate's second value, a canary
            again = await ladder._run(store.s.active_tool(), VALUE)
            return first, n0, again
        with MC.scope("product"), MC.using(model):
            first, n0, again = asyncio.run(go())
        assert first["status"] == "no_results" and n0 >= 1        # the tool call's own run asks
        assert len(model.lines) == n0, model.lines                # the re-proof asks nothing more
        assert again["status"] != "no_results", again              # without the model, the phrase lists decide
    finally:
        store.close()


def test_a_proof_replay_and_a_watch_run_ask_no_check_model_whatever_scope_is_opened(tools):
    t15, _t14 = tools
    model = Asked()

    def proof_replay(t, _site, value):
        with MC.scope("product"):                           # a scope opened inside the replay stays closed
            return asyncio.run(T.run_tool(EmptyPage(), t, value, _settle, result_wait_s=0.0))
    with MC.using(model), MC.scope("agent"):
        out = HC._replay_without_model(proof_replay, t15, None, VALUE)
    assert model.lines == [] and out["status"] != "no_results", out

    def watch_replay(item, tool, caps):
        with MC.scope("product"):
            return asyncio.run(T.run_tool(EmptyPage(), tool, str(item["input"]), _settle, result_wait_s=0.0))
    with TW.Store() as s, MC.using(model):
        s.st.add(s.tool(t15), VALUE, "1h")
        rep = WA.run_watch(s.st, watch_replay, now=TW.hours(0))
    assert model.lines == [] and rep["model_calls"]["replay"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
