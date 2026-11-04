"""Offline (the fake shop of test_demo_compile.py and a scripted CLI; no model, no
browser): the agent-generated demonstration as a no-tools policy loop.

What must hold:

1. the loop sends the goal, the input value and a compact observation (the search
   boxes first, numbered elements, a <= 6,000-character text excerpt, the steps so
   far) and executes ONE parsed action per call; the trace it records compiles
   (``demo_compile``) and is a valid demonstration;
2. the parsed action keeps its own keys only (a rationale is dropped), code fences are
   tolerated, an unknown action or a product result without a name is unreadable (a
   wasted step with feedback, never a guess);
3. cart, checkout, login and account controls are never executed: the attempt ends
   the demonstration (``forbidden``), the page never goes there; another website is
   refused;
4. one attempt: at most 10 calls; the call cap stops the loop.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_policy as P  # noqa: E402
from test_demo_compile import BASE, FakeShopPage  # noqa: E402


async def _settle(page):
    return None


def element(prompt, kind, text):
    m = re.search(r"\[(\d+)\] " + kind + r" \"" + re.escape(text) + r"\"", prompt)
    assert m, (kind, text, prompt[:2000])
    return int(m.group(1))


class ScriptedCli:
    """Answers by what the prompt shows (a stand-in for ``claude -p`` / ``codex exec``)."""

    def __init__(self, plan):
        self.plan, self.prompts = list(plan), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        step = self.plan.pop(0) if self.plan else "fail"
        return step(prompt) if callable(step) else step, {"seconds": 0.01}


def demo(plan, value="Delta Mixer 7", budget=None, page=None):
    cli = ScriptedCli(plan)
    page = page or FakeShopPage()
    tr = asyncio.run(P.run_demo(page, "product_lookup", value, BASE + "/", "www.shop.test", cli, _settle,
                                cli_name="scripted", budget=budget))
    return tr, cli, page


def good_plan(value="Delta Mixer 7"):
    return [lambda p: json.dumps({"action": "search", "element": element(p, "search box", "q"), "text": value,
                                  "reason": "the search box is at the top"}),
            lambda p: "```json\n" + json.dumps({"action": "click", "element": element(p, "link", value)}) + "\n```",
            lambda p: json.dumps({"action": "done", "result": {"name": value, "price": "75,00 €", "currency": "EUR",
                                                               "availability": "Auf Lager",
                                                               "url": BASE + "/p/delta-mixer-7"},
                                  "thoughts": "done"})]


def test_the_policy_loop_records_a_trace_that_compiles():
    tr, cli, page = demo(good_plan())
    assert tr["stop"] == "done" and tr["calls"] == 3 and len(tr["steps"]) == 3, tr["stop"]
    first = cli.prompts[0]
    assert "INPUT VALUE (type exactly this into the site's search): Delta Mixer 7" in first
    assert re.search(r"ELEMENTS:\n\[0\] search box", first), first[:600]
    assert len(tr["steps"][0]["obs"]["text"]) <= C.OBS_TEXT_CHARS
    assert "(none)" in first and "now at https://www.shop.test/search?q=Delta+Mixer+7" in cli.prompts[1]
    # the rationale never enters the trace
    assert all(set(s["action"]) <= {"action", "element", "text", "url", "result"} for s in tr["steps"])
    assert "thoughts" not in json.dumps(tr["result"])
    ok, why, _ = C.demo_validity(tr)
    assert ok, why
    got = C.compile_trace(tr)
    assert got["ok"] and got["tool"]["search"]["template"] == BASE + "/search?q={q}", got


def test_parse_action():
    assert P.parse_action('{"action": "click", "element": 3, "why": "x"}', "site_search") == \
        ({"action": "click", "element": 3}, "")
    assert P.parse_action('Sure! {"action":"goto","url":"/search?q=a"} done', "site_search")[0] == \
        {"action": "goto", "url": "/search?q=a"}
    assert P.parse_action('{"action": "hover", "element": 1}', "site_search")[0] is None
    assert P.parse_action('{"action": "done", "result": {"price": "1"}}', "product_lookup")[0] is None
    assert P.parse_action('{"action": "done", "result": {"no_results": true}}', "product_lookup")[0] == \
        {"action": "done", "result": {"no_results": True}}
    a, _ = P.parse_action('{"action": "done", "result": {"results": [{"title": "A", "url": "/a", "x": 1}]}}',
                          "site_search")
    assert a == {"action": "done", "result": {"results": [{"title": "A", "url": "/a"}]}}
    assert P.parse_action("no json here", "site_search") == (None, "no JSON object")


def test_forbidden_controls_end_the_demonstration():
    plan = [lambda p: json.dumps({"action": "click", "element": element(p, "link", "Cart")})]
    tr, _cli, page = demo(plan)
    assert tr["stop"] == "forbidden" and tr["forbidden"] and not any("/cart" in u for u in page.gotos), page.gotos
    assert not C.demo_validity(tr)[0]
    plan = [lambda p: json.dumps({"action": "goto", "url": BASE + "/account/orders"})]
    tr, _cli, page = demo(plan)
    assert tr["stop"] == "forbidden" and not any("/account" in u for u in page.gotos)
    plan = [lambda p: json.dumps({"action": "goto", "url": "https://elsewhere.test/"}), "not json",
            json.dumps({"action": "fail", "reason": "nothing"})]
    tr, cli, page = demo(plan)
    assert tr["stop"] == "fail" and tr["calls"] == 3 and page.gotos == [BASE + "/"]
    assert "FAILED: blocked: another website" in cli.prompts[1] and "not one valid JSON action" in cli.prompts[2]


def test_one_attempt_at_most_ten_calls_and_the_call_cap():
    nav = lambda p: json.dumps({"action": "click", "element": element(p, "link", "Kettles")})  # noqa: E731
    tr, cli, _page = demo([nav] * 20)
    assert tr["stop"] == "max_steps" and tr["calls"] == 10 and len(cli.prompts) == 10
    left = [2]

    def budget():
        left[0] -= 1
        return left[0] >= 0
    tr, cli, _page = demo([nav] * 20, budget=budget)
    assert tr["stop"] == "call_cap" and tr["calls"] == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
