"""The agent tier's escalation loop, fully offline.

Brains are real adapter-backed brains talking to an exact-loopback fake
OpenAI-compatible server; the browser is a fake session. No socket leaves
127.0.0.1, no browser starts, no model is called.

What must hold (the escalation policy): HARD triggers — (a) the
gate's refusal is where the run ended, (b) an abstention, (c) no progress —
escalate at once with no repair round; the one SOFT case (state changed, page
not there yet) gets exactly ONE repair round; never escalate on success or on a
schedule; levels run in order; caps end a level; a blocked page, a provider
outage and an EMPTY answer stop the loop without escalating and are never a
verdict.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import unittest
from http.server import BaseHTTPRequestHandler

from anyuse.execution.agent_browser import (AgentRun, CerebrasBrain, EscalatingAgent,
                                            EscalationLevel, StepAgent, classify_failure,
                                            default_levels, is_abstention,
                                            make_agent, page_kind_check, page_state)
from anyuse.execution.model_adapter import ModelAdapter, ModelConfig

KEY_ENV = "ANYUSE_TEST_ESCALATION_KEY"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_POST(self):
        st = self.server.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        st["requests"].append(body)
        reply = st["replies"].pop(0) if st["replies"] else {"action": "done"}
        if isinstance(reply, int):
            raw, status = b"{}", reply
        elif reply == FENCED:
            raw, status = json.dumps({
                "choices": [{"message": {"role": "assistant", "content":
                             "Sure, typing now:\n```json\n"
                             "{\"action\": \"type\", \"index\": 0, \"text\": \"serde\",}\n```\n"
                             "Then I will submit."},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "total_tokens": 110, "cost": st.get("cost", 0.0)}}).encode(), 200
        elif reply == PROSE:
            # a reply that is not a JSON action at all
            raw, status = json.dumps({
                "choices": [{"message": {"role": "assistant",
                                         "content": "I would click the search box first."},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "total_tokens": 110, "cost": st.get("cost", 0.0)}}).encode(), 200
        elif reply == EMPTY:
            # what gpt-oss did with max_tokens 200: all of it spent on hidden
            # reasoning, no answer text
            raw, status = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "",
                                         "reasoning": "thinking..."},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                          "total_tokens": 150, "cost": st.get("cost", 0.0)}}).encode(), 200
        else:
            raw, status = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": json.dumps(reply)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "total_tokens": 110, "cost": st.get("cost", 0.0)}}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextlib.contextmanager
def fake_models(scripts, costs=None, provider="local"):
    """One in-memory transport per level name; yields ``({level: brain}, states)``."""
    import httpx
    import io
    from types import SimpleNamespace
    brains, states = {}, {}
    for name, replies in scripts.items():
        state = {"replies": list(replies), "requests": [], "cost": (costs or {}).get(name, 0.)}
        def respond(request, state=state):
            response = {"status": 200, "headers": {}}
            sink = io.BytesIO()
            fake = SimpleNamespace(server=SimpleNamespace(state=state), headers=request.headers,
                rfile=io.BytesIO(request.content), wfile=sink,
                send_response=lambda status: response.update(status=status),
                send_header=lambda key, value: response["headers"].update({key: value}), end_headers=lambda: None)
            _Handler.do_POST(fake)
            return httpx.Response(response["status"], headers=response["headers"], content=sink.getvalue())
        cfg = ModelConfig(provider, "http://127.0.0.1:19002/v1", f"fake-{name}", KEY_ENV, timeout=2., retries=0)
        brains[name] = CerebrasBrain(adapter=ModelAdapter(cfg, transport=httpx.MockTransport(respond)), max_tokens=50)
        states[name] = state
    previous_key = os.environ.get(KEY_ENV)
    os.environ[KEY_ENV] = "escalation-test-secret"
    try:
        yield brains, states
    finally:
        if previous_key is None:
            os.environ.pop(KEY_ENV, None)
        else:
            os.environ[KEY_ENV] = previous_key


class Session:
    """A two-page site: the front page with a search box, and a result page."""
    blocked = False
    shots = 0

    def __init__(self, endpoint, allow_url=None):
        self.url_now, self.typed, self.refused = "https://ex.test/", "", []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def goto(self, url):
        self.url_now = url

    async def observe(self):
        return {"url": self.url_now, "title": self._title(), "text": "Search here",
                "elements": [{"i": 0, "tag": "input", "type": "search", "name": "Search",
                              "href": ""},
                             {"i": 1, "tag": "a", "type": "", "name": "About", "href": "#"}]}

    def _title(self):
        return "Search: " + self.typed if "q=" in self.url_now else "Example home"

    async def focus(self, idx):
        return True

    async def type(self, text):
        self.typed = text

    async def press_enter(self):
        self.url_now = f"https://ex.test/search?q={self.typed}"

    async def click(self, idx):
        pass                                # "About" goes nowhere: no progress

    async def settle(self, s):
        pass

    async def screenshot(self):
        Session.shots += 1
        return "data:image/jpeg;base64,SlBH"

    async def dom(self):
        body = "Access denied. Please verify you are human." if self.blocked else "rows"
        return f"<html><head><title>{self._title()}</title></head><body>{body}</body></html>"

    async def text(self):
        return "Verify you are human. Checking your browser." if self.blocked else "rows"

    async def url(self):
        return self.url_now

    async def network(self):
        return []


EMPTY = "EMPTY"                               # an HTTP 200 with no answer text
PROSE = "PROSE"                               # an HTTP 200 that is not a JSON action
FENCED = "FENCED"                             # a readable action inside prose + a fence
TYPE = {"action": "type", "index": 0, "text": "serde"}
SUBMIT = {"action": "submit", "index": 0}
DONE = {"action": "done"}
SOLVE = [TYPE, SUBMIT, DONE]
QUIT = [DONE]                                # "done" on the front page: never moved
WRONG = [{"action": "type", "index": 0, "text": "serdes"}, SUBMIT, DONE]  # moved, wrong page


class Refusing(Session):
    """The site's search is on a path the gate refuses: the tab is left on
    the browser's error page, as a refused navigation leaves it in Chromium."""

    async def press_enter(self):
        self.refused.append(f"https://ex.test/private/search?q={self.typed}")
        self.url_now = "chrome-error://chromewebdata/"


def levels(**caps):
    return [EscalationLevel("L1", "agent", rounds=3, **caps),
            EscalationLevel("L2", "agent_l2", rounds=3, **caps),
            EscalationLevel("L3", "agent_l3", rounds=3, vision=True, **caps)]


def agent(brains, lv=None, **kw):
    return EscalatingAgent(lv or levels(), endpoint="http://192.168.64.5:9222",
                           session_factory=Session, brains=brains, pause_s=0, **kw)


class Triggers(unittest.TestCase):

    def test_no_escalation_on_success(self):
        with fake_models({"L1": SOLVE, "L2": [], "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(esc["solved_at"], "L1")
        self.assertEqual(esc["escalations"], [])
        self.assertEqual(len(esc["attempts"]), 1)
        self.assertEqual(st["L2"]["requests"], [], "L2 must not be called on success")
        self.assertEqual(st["L3"]["requests"], [])

    def test_soft_gets_exactly_one_repair_round(self):
        # levels() asks for 3 rounds; the policy still allows only ONE repair
        with fake_models({"L1": WRONG * 3, "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual([a["level"] for a in esc["attempts"]], ["L1", "L1", "L2"])
        self.assertEqual(esc["escalations"][0]["trigger"], "goal")
        self.assertEqual([a.get("trigger") for a in esc["attempts"][:2]], ["goal", "goal"])
        self.assertEqual(esc["solved_at"], "L2")
        self.assertEqual(len(st["L1"]["requests"]), 6, "two L1 rounds of three steps")
        # the repair round carries the check's reason, the first round does not
        bodies = [json.loads(r["messages"][1]["content"]) for r in st["L1"]["requests"]]
        self.assertNotIn("previous_attempts", bodies[0])
        self.assertIn("does not name", bodies[3]["previous_attempts"][0]["check"])
        l2 = json.loads(st["L2"]["requests"][0]["messages"][1]["content"])
        self.assertEqual(len(l2["previous_attempts"]), 2, "the next level sees why")
        self.assertEqual(st["L3"]["requests"], [])
        self.assertEqual(run.final_url, "https://ex.test/search?q=serde")

    def test_hard_never_moved_escalates_at_once(self):
        # "done" on the page it started on: nothing changed -> (c), no repair
        with fake_models({"L1": QUIT * 3, "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual([a["level"] for a in esc["attempts"]], ["L1", "L2"])
        self.assertEqual(esc["escalations"][0]["trigger"], "no_progress")
        self.assertEqual(len(st["L1"]["requests"]), 1)

    def test_hard_policy_refusal_escalates_at_once(self):
        with fake_models({"L1": SOLVE * 3, "L2": [{"action": "abstain"}],
                          "L3": [{"action": "abstain"}]}) as (brains, st):
            a = EscalatingAgent(levels(), endpoint="http://192.168.64.5:9222",
                                session_factory=Refusing, brains=brains, pause_s=0)
            run = a.run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(esc["escalations"][0]["trigger"], "policy")
        self.assertIn("gate refused", esc["escalations"][0]["reason"])
        self.assertEqual([x["level"] for x in esc["attempts"]], ["L1", "L2", "L3"])
        self.assertEqual(len(st["L1"]["requests"]), 3, "one L1 round, no repair")
        self.assertIsNone(esc["solved_at"])

    def test_hard_b_abstention_escalates_at_once(self):
        with fake_models({"L1": [{"action": "abstain", "reason": "no search"}],
                          "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual([a["level"] for a in esc["attempts"]], ["L1", "L2"])
        self.assertEqual(esc["escalations"][0]["trigger"], "abstain")
        self.assertEqual(len(st["L1"]["requests"]), 1, "no repair rounds after (b)")

    def test_hard_c_no_progress_escalates_at_once(self):
        click = {"action": "click", "index": 1}
        with fake_models({"L1": [click] * 5, "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(esc["attempts"][0]["stop"], "no_progress")
        self.assertEqual(esc["escalations"][0]["trigger"], "no_progress")
        # observed state unchanged after click 1 and click 2 -> stop before a 3rd call
        self.assertEqual(len(st["L1"]["requests"]), 2)
        self.assertEqual(esc["solved_at"], "L2")

    def test_level_order_and_human_gate(self):
        with fake_models({"L1": WRONG * 3, "L2": WRONG * 3, "L3": WRONG * 3}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertIsNone(esc["solved_at"])
        self.assertEqual([(e["from"], e["to"]) for e in esc["escalations"]],
                         [("L1", "L2"), ("L2", "L3"), ("L3", "human")])
        self.assertEqual([a["level"] for a in esc["attempts"]],
                         ["L1"] * 2 + ["L2"] * 2 + ["L3"] * 2)
        self.assertEqual(run.model_calls, 18)

    def test_vision_level_sends_a_container_screenshot(self):
        Session.shots = 0
        with fake_models({"L1": [{"action": "abstain"}], "L2": [{"action": "abstain"}],
                          "L3": SOLVE}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        self.assertEqual(run.escalation["solved_at"], "L3")
        content = st["L3"]["requests"][0]["messages"][-1]["content"]
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertTrue(content[-1]["image_url"]["url"].startswith("data:image/jpeg"))
        self.assertIsInstance(st["L1"]["requests"][0]["messages"][-1]["content"], str)
        self.assertEqual(Session.shots, 3)


class EmptyAnswers(unittest.TestCase):
    """An empty answer is never a failed round (measured: 44 L1 / 27 L2 / 4 L3 attempts ended
    on an empty answer, were counted as failed rounds and their tokens were lost)."""

    def test_adapter_flags_an_empty_answer_with_its_finish_reason(self):
        with fake_models({"L1": [EMPTY]}) as (brains, st):
            res = brains["L1"]._adapter.complete([{"role": "user", "content": "x"}])
        self.assertFalse(res.ok)
        self.assertTrue(res.empty_answer)
        self.assertFalse(res.provider_unreachable)
        self.assertEqual(res.finish_reason, "length")
        self.assertIn("finish_reason=length", res.error)
        self.assertIn("reasoning only", res.error)
        self.assertNotIn("thinking", res.error, "no content is echoed")

    def test_one_empty_answer_is_retried_in_the_same_step(self):
        with fake_models({"L1": [EMPTY] + SOLVE, "L2": [], "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(esc["solved_at"], "L1")
        self.assertEqual(len(esc["attempts"]), 1, "no repair round was consumed")
        budgets = [r["max_tokens"] for r in st["L1"]["requests"]]
        self.assertEqual(budgets[:2], [50, 100], "the retry doubles the budget")
        self.assertEqual(run.model_calls, 4, "the empty call is counted")
        self.assertEqual(run.tokens_out, 50 + 3 * 10, "and its tokens are billed")

    def test_empty_twice_is_provider_class_not_a_verdict(self):
        with fake_models({"L1": [EMPTY, EMPTY], "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(esc["stopped"], "provider_empty")
        self.assertEqual(esc["escalations"], [], "never escalated")
        self.assertIsNone(esc["solved_at"])
        self.assertNotIn("trigger", esc["attempts"][0], "never classified as a failure")
        self.assertEqual(st["L2"]["requests"], [])
        self.assertEqual(run.model_calls, 2)
        self.assertEqual(run.tokens_in, 200)

    def test_default_budgets_cover_reasoning(self):
        lv = {x.name: x for x in default_levels()}
        self.assertGreaterEqual(lv["L1"].max_tokens, 500)
        self.assertGreaterEqual(lv["L2"].max_tokens, 500)
        self.assertGreaterEqual(lv["L3"].max_tokens, 1200)
        self.assertTrue(all(x.rounds == 2 for x in lv.values()), "1 + one repair")


class UnreadableAnswers(unittest.TestCase):
    """A reply that is not a JSON action used to become ``done`` silently; the
    attempt then failed its goal check and was escalated as no_progress."""

    def test_one_unreadable_reply_is_repaired_in_the_same_step(self):
        with fake_models({"L1": [PROSE] + SOLVE, "L2": [], "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(esc["solved_at"], "L1")
        self.assertEqual(len(esc["attempts"]), 1, "no repair round was consumed")
        repair = st["L1"]["requests"][1]["messages"]
        self.assertEqual(repair[-1]["role"], "user")
        self.assertIn("not a JSON action", repair[-1]["content"])
        self.assertEqual(repair[-2]["role"], "assistant")
        self.assertIn("click the search box", repair[-2]["content"])
        self.assertEqual(st["L1"]["requests"][1]["max_tokens"], 50, "same budget")
        self.assertEqual(run.unreadable_retries, 1)
        self.assertEqual(esc["attempts"][0]["unreadable_retries"], 1)
        self.assertEqual(run.model_calls, 4, "the unreadable call is counted")
        self.assertEqual(run.tokens_out, 4 * 10, "and its tokens are billed")
        self.assertEqual(st["L2"]["requests"], [])

    def test_unreadable_twice_stops_the_loop_and_is_never_a_verdict(self):
        with fake_models({"L1": [PROSE, PROSE], "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(run.stop, "model_unreadable")
        self.assertEqual(esc["stopped"], "model_unreadable")
        self.assertEqual(esc["escalations"], [], "never escalated")
        self.assertIsNone(esc["solved_at"])
        self.assertNotIn("trigger", esc["attempts"][0], "never classified as a failure")
        self.assertEqual(st["L2"]["requests"], [])
        self.assertEqual(run.model_calls, 2)
        self.assertEqual(run.tokens_in, 200, "both calls are billed")
        self.assertTrue(run.steps[-1].get("error", "").startswith("UnreadableAnswer("),
                        run.steps[-1])

    def test_a_fenced_reply_with_prose_around_it_needs_no_repair(self):
        with fake_models({"L1": [FENCED] + SOLVE[1:], "L2": [], "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        self.assertEqual(run.escalation["solved_at"], "L1")
        self.assertEqual(run.unreadable_retries, 0)
        self.assertEqual(len(st["L1"]["requests"]), 3, "no repair message was sent")
        self.assertEqual(run.typed, ["serde"])


class Caps(unittest.TestCase):

    def test_usd_cap_ends_the_level(self):
        lv = levels(max_usd=0.001)
        with fake_models({"L1": WRONG * 3, "L2": SOLVE, "L3": []},
                         costs={"L1": 0.002}) as (brains, st):
            run = agent(brains, lv).run("search for {q}", "https://ex.test/", "serde")
        esc = run.escalation
        self.assertEqual(len(st["L1"]["requests"]), 3, "cap reached after one round")
        self.assertEqual(esc["escalations"][0]["trigger"], "cap")
        self.assertIn("level cap", esc["escalations"][0]["reason"])
        self.assertAlmostEqual(esc["attempts"][0]["usd"], 0.006)

    def test_calls_cap_is_passed_into_the_step_loop(self):
        lv = levels(max_calls=2)
        with fake_models({"L1": [SUBMIT, TYPE, TYPE], "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains, lv).run("search for {q}", "https://ex.test/", "serde")
        self.assertEqual(len(st["L1"]["requests"]), 2)
        self.assertEqual(run.escalation["attempts"][0]["stop"], "cap_calls")
        # the cap ended the steps on a page that names the value: the goal is
        # reached, so there is nothing to escalate
        self.assertEqual(run.escalation["solved_at"], "L1")

    def test_global_budget_stops_everything(self):
        with fake_models({"L1": QUIT * 3, "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains, budget_left=lambda: 0.0).run(
                "search for {q}", "https://ex.test/", "serde")
        self.assertEqual(run.escalation["stopped"], "global budget exhausted")
        self.assertEqual(st["L1"]["requests"], [])


class Stops(unittest.TestCase):

    def test_blocked_page_stops_without_escalating(self):
        class Walled(Session):
            blocked = True
        with fake_models({"L1": SOLVE, "L2": SOLVE, "L3": []}) as (brains, st):
            a = EscalatingAgent(levels(), endpoint="http://192.168.64.5:9222",
                                session_factory=Walled, brains=brains, pause_s=0,
                                blocked_check=lambda run: "captcha: verify you are human")
            run = a.run("search for {q}", "https://ex.test/", "serde")
        self.assertTrue(run.escalation["stopped"].startswith("blocked"))
        self.assertEqual(run.escalation["escalations"], [])
        self.assertEqual(st["L2"]["requests"], [], "a blocked host is never re-hit")

    def test_provider_outage_is_not_a_verdict(self):
        with fake_models({"L1": [402], "L2": SOLVE, "L3": []}) as (brains, st):
            run = agent(brains).run("search for {q}", "https://ex.test/", "serde")
        self.assertEqual(run.escalation["stopped"], "provider_unreachable")
        self.assertEqual(run.escalation["escalations"], [])
        self.assertEqual(st["L2"]["requests"], [])


class Pieces(unittest.TestCase):

    def test_page_kind_check(self):
        def r(url, title, value="serde", start="https://ex.test/"):
            return AgentRun(value=value, start_url=start, final_url=url,
                            dom=f"<html><title>{title}</title></html>")
        self.assertTrue(page_kind_check(r("https://ex.test/search?q=serde", "Results"))[0])
        self.assertTrue(page_kind_check(r("https://ex.test/p/app-editors/vim", "vim",
                                          value="app-editors/vim"))[0])
        self.assertFalse(page_kind_check(r("https://ex.test/", "serde home"))[0])
        self.assertFalse(page_kind_check(r("https://ex.test/about", "About"))[0])
        self.assertFalse(page_kind_check(r("https://other.test/?q=serde", "serde"))[0])
        self.assertFalse(page_kind_check(AgentRun(value="x", start_url="https://ex.test/",
                                                  error="boom"))[0])

    def test_classify_failure(self):
        base = dict(value="serde", start_url="https://ex.test/")
        self.assertEqual(classify_failure(AgentRun(**base, final_url="chrome-error://x",
                                                   refused=["https://ex.test/p"],
                                                   state_changed=True)), ("policy", False))
        # refused once, then carried on to a site page: judged by its page
        self.assertEqual(classify_failure(AgentRun(**base, final_url="https://ex.test/a",
                                                   refused=["https://ex.test/p"],
                                                   state_changed=True)), ("goal", True))
        self.assertEqual(classify_failure(AgentRun(**base, stop="abstain",
                                                   state_changed=True)), ("abstain", False))
        self.assertEqual(classify_failure(AgentRun(**base, stop="no_progress",
                                                   state_changed=True)), ("no_progress", False))
        self.assertEqual(classify_failure(AgentRun(**base, stop="done")), ("no_progress", False))

    def test_abstention_forms(self):
        self.assertTrue(is_abstention({"action": "abstain"}))
        self.assertTrue(is_abstention({"action": "done", "reason": "I cannot find a search"}))
        self.assertFalse(is_abstention({"action": "done"}))
        self.assertFalse(is_abstention({"action": "click", "index": 0}))

    def test_page_state_ignores_fragment_only(self):
        a = {"url": "https://ex.test/a#x", "title": "t", "elements": []}
        b = {"url": "https://ex.test/a#y", "title": "t", "elements": []}
        c = {"url": "https://ex.test/a", "title": "u", "elements": []}
        self.assertEqual(page_state(a), page_state(b))
        self.assertNotEqual(page_state(a), page_state(c))

    def test_off_by_default_and_on_by_env(self):
        old = os.environ.pop("ANYUSE_AGENT_ESCALATION", None)
        os.environ[KEY_ENV] = "x"
        try:
            self.assertIsInstance(make_agent("http://192.168.64.5:9222"), StepAgent)
            os.environ["ANYUSE_AGENT_ESCALATION"] = "on"
            self.assertIsInstance(make_agent("http://192.168.64.5:9222"), EscalatingAgent)
        finally:
            os.environ.pop("ANYUSE_AGENT_ESCALATION", None)
            os.environ.pop(KEY_ENV, None)
            if old is not None:
                os.environ["ANYUSE_AGENT_ESCALATION"] = old

    def test_openrouter_cost_is_read_and_requested(self):
        with fake_models({"L2": [DONE]}, costs={"L2": 0.0042},
                         provider="openrouter") as (brains, st):
            action, usage, cached = brains["L2"]([{"role": "user", "content": "x"}])
        self.assertAlmostEqual(usage["cost_usd"], 0.0042)
        self.assertEqual(st["L2"]["requests"][0]["usage"], {"include": True})

    def test_level_roles_resolve_through_the_adapter(self):
        from anyuse.execution.model_adapter import resolve_model_config
        saved = {k: os.environ.pop(k) for k in list(os.environ)
                 if k.startswith("ANYUSE_") and "MODEL" in k}
        try:
            self.assertEqual(resolve_model_config("agent_l2").model, "anthropic/claude-sonnet-5")
            self.assertEqual(resolve_model_config("agent_l3").provider, "openrouter")
            os.environ["ANYUSE_AGENT_L2_MODEL_NAME"] = "anthropic/claude-opus-5.5"
            self.assertEqual(resolve_model_config("agent_l2").model, "anthropic/claude-opus-5.5")
        finally:
            os.environ.pop("ANYUSE_AGENT_L2_MODEL_NAME", None)
            os.environ.update(saved)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
