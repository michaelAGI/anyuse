"""Typed-search behaviour of the scripted arm and the ladder's agent
session, offline against a scripted fake CDP websocket (no browser).

* a header search that is only an icon is opened once (REVEAL_SEARCH_JS);
* when Enter moves nothing (no navigation, no request, same URL) the search's
  own submit control is clicked once (SUBMIT_SEARCH_JS);
* the agent session's Enter gets the same fallback;
* the agent sees icon-only search controls ("search (icon)"), and OBSERVE_JS
  and PICK_JS keep one element filter (indices must agree).
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest

from anyuse.execution import agent_browser as AB
from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D


def run(c):
    return asyncio.run(c)


class WS:
    """Answers Runtime.evaluate by marker; ``script`` maps a marker to a list of
    answers (popped in order) or a callable."""

    def __init__(self, script):
        self.script, self.inbox, self.sent, self.typed, self.evals = script, [], [], [], []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m, p = msg.get("method"), msg.get("params") or {}
        if m in ("Fetch.continueRequest", "Fetch.failRequest"):
            return
        val = None
        if m == "Runtime.evaluate":
            e = p["expression"]
            for marker, ans in self.script.items():
                if marker in e:
                    self.evals.append(marker)
                    val = ans(e) if callable(ans) else (ans.pop(0) if len(ans) > 1 else ans[0])
                    break
        elif m == "Input.insertText":
            self.typed.append(p["text"])
        self.inbox.append({"id": msg["id"], "result": {"result": {"value": val}}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))


QUIET = {"quiet_ms": 900, "mutations": 1, "ready": "complete"}
INPUT = [{"index": 0, "visible": True, "name": "q", "id": "", "type": "search"}]


def session(script):
    s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(D.Pacer(1.0)))
    # the scripted markers are matched first (other scripts mention
    # location.href and activeElement too)
    base = dict(script)
    base.update({"__anyuseSettle": [QUIET], "String(el.value": lambda e: "red fox",
                 "location.href": ["https://h.test/"]})
    base["el.focus(); el.value = ''"] = [True]
    s._ws = WS(base)
    return s


class ScriptedArm(unittest.TestCase):

    def test_icon_only_header_search_is_opened_then_typed(self):
        s = session({"__anyuseRevealSearch": [{"clicked": True, "label": "Search",
                                               "navigates": False}],
                     "querySelectorAll('input').forEach": [[], INPUT],
                     "__anyuseSubmitSearch": [{"clicked": False}]})
        out = run(s.type_search("red fox"))
        self.assertEqual(out["revealed"], {"label": "Search", "navigates": False})
        self.assertEqual(out["input"]["name"], "q")
        self.assertEqual(s._ws.typed, ["red fox"])

    def test_no_toggle_means_no_input(self):
        s = session({"__anyuseRevealSearch": [{"clicked": False}],
                     "querySelectorAll('input').forEach": [[]]})
        out = run(s.type_search("red fox"))
        self.assertEqual(out["note"], "no visible search input")
        self.assertNotIn("revealed", out)
        self.assertEqual(s._ws.typed, [])

    def test_recorded_input_replay_never_reveals(self):
        s = session({"__anyuseRevealSearch": [{"clicked": True}],
                     "querySelectorAll('input').forEach": [[]]})
        out = run(s.type_search("red fox", want={"name": "q"}))
        self.assertNotIn("__anyuseRevealSearch", s._ws.evals)
        self.assertEqual(out["note"], "no visible search input")

    def test_enter_that_moved_nothing_clicks_the_submit_control(self):
        s = session({"querySelectorAll('input').forEach": [INPUT],
                     "__anyuseSubmitSearch": [{"clicked": True, "how": "form submit control"}]})
        out = run(s.type_search("red fox"))
        self.assertEqual(out["submit"], {"clicked": True, "how": "form submit control"})
        self.assertIn("submit", out["settle"])

    def test_enter_that_sent_a_document_needs_no_button(self):
        s = session({"querySelectorAll('input').forEach": [INPUT],
                     "__anyuseSubmitSearch": [{"clicked": True}]})
        real = s._await_enter_navigation

        async def nav(ev0, sent0, detect_s=1.0):
            s.budget.sent.append(("h.test", 0.0, "https://h.test/s?q=red+fox", "Document"))
            return await real(ev0, sent0, detect_s=0.01)
        s._await_enter_navigation = nav
        out = run(s.type_search("red fox"))
        self.assertNotIn("submit", out)
        self.assertNotIn("__anyuseSubmitSearch", s._ws.evals)

    def test_submit_fallback_can_be_switched_off(self):
        s = session({"querySelectorAll('input').forEach": [INPUT],
                     "__anyuseSubmitSearch": [{"clicked": True}]})
        s.submit_fallback = False
        out = run(s.type_search("red fox"))
        self.assertNotIn("submit", out)


class AgentSession(unittest.TestCase):

    def _agent(self, sigs, submit):
        s = session({"location.href + '|'": sigs, "__anyuseSubmitSearch": [submit]})
        s.settle = lambda sec: asyncio.sleep(0)
        return s

    def test_agent_enter_without_effect_clicks_submit_once(self):
        s = self._agent(["u|10|a", "u|10|a"], {"clicked": True, "how": "search button next to the input"})
        run(s.press_enter())
        self.assertEqual(s._ws.evals.count("__anyuseSubmitSearch"), 1)
        self.assertEqual(s.submitted_by_button, "search button next to the input")
        exprs = [m["params"]["expression"] for m in s._ws.sent
                 if m.get("method") == "Runtime.evaluate"
                 and "__anyuseSubmitSearch" in m["params"]["expression"]]
        self.assertTrue(exprs[0].rstrip().endswith("(-1)"))   # the focused input

    def test_agent_enter_that_changed_the_page_is_left_alone(self):
        s = self._agent(["u|10|a", "u2|99|b"], {"clicked": True})
        run(s.press_enter())
        self.assertNotIn("__anyuseSubmitSearch", s._ws.evals)


class SearchTransaction(unittest.TestCase):
    """GuardedSession.search: replace, read back, submit, wait (one step)."""

    def _s(self, held, submit):
        s = session({"e.select()": [True], "String(e.value||'')": held,
                     "__anyuseSubmitSearch": [submit],
                     "const c = ordered[idx]": [True]})
        return s

    def test_text_is_replaced_read_back_and_submitted(self):
        s = self._s(["red fox"], {"clicked": False})
        out = run(s.search(3, "red fox"))
        self.assertTrue(out["focused"] and out["held"])
        self.assertNotIn("retyped", out)
        self.assertEqual(s._ws.typed, ["red fox"])
        self.assertEqual(out["submit"], "enter")
        # the field is selected before typing, so the text replaces it
        self.assertLess(s._ws.evals.index("e.select()"),
                        s._ws.evals.index("String(e.value||'')"))
        keys = [m["params"].get("key") for m in s._ws.sent
                if m.get("method") == "Input.dispatchKeyEvent"]
        self.assertEqual(keys, ["Enter"] * 3)

    def test_a_wiped_field_is_typed_once_more(self):
        s = self._s(["", "red fox"], {"clicked": False})
        out = run(s.search(3, "red fox"))
        self.assertTrue(out.get("retyped"))
        self.assertEqual(s._ws.typed, ["red fox", "red fox"])

    def test_nothing_moved_uses_the_submit_button(self):
        s = self._s(["red fox"], {"clicked": True, "how": "form submit control"})
        out = run(s.search(3, "red fox"))
        self.assertEqual(out["submit"], "form submit control")


class ObserveFilter(unittest.TestCase):

    def test_icon_only_search_controls_are_named(self):
        self.assertIn("'search (icon)'", AB.OBSERVE_JS)

    def test_observe_and_pick_share_one_collector(self):
        self.assertIn(AB._COLLECT_JS, AB.OBSERVE_JS)
        self.assertIn(AB._COLLECT_JS, AB.PICK_JS)
        self.assertIn("const c = ordered[idx]", AB.PICK_JS)

    def test_search_controls_come_first(self):
        js = AB._COLLECT_JS
        self.assertIn("cands.filter(c => c.rank === 0).concat(", js)
        self.assertIn(".slice(0, 40)", js)

    def test_progress_state_includes_visible_text(self):
        a = {"url": "https://h/x", "title": "t", "elements": [], "text": "no results yet"}
        b = dict(a, text="12 results for alpha")
        self.assertNotEqual(AB.page_state(a), AB.page_state(b))

    def test_escalation_prompt_offers_one_step_search(self):
        self.assertIn('{"action":"search"', AB.SYSTEM_ESCALATION)
        self.assertNotIn('"search"', AB.SYSTEM)     # the historical prompt is unchanged

    def test_js_templates_format(self):
        self.assertIn("(3)", G.SUBMIT_SEARCH_JS % 3)
        self.assertIn("(-1)", G.SUBMIT_SEARCH_JS % -1)
        AB.PICK_JS % ("true", 2)


if __name__ == "__main__":
    unittest.main()
