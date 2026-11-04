"""The guarded session finds a page's search box by the page's structure, then by what the page does with typing,
and names last; it opens a search that is only an icon by clicking candidates until a text input appears.

Offline against a scripted fake CDP websocket (no browser):

* an input of type search (or in a search landmark) is used at once, whatever else the page holds;
* among several text inputs without such structure, the one whose typing the page answers (a request carrying the
  typed value, the page's text showing it, its own GET form submitting it) is the search box, in any language; an
  input named like a search ("Find a store") is tried first but loses when typing it does nothing;
* with no answer from the page, an input named like a search is used (the last rung); with none, nothing is typed;
* the icon-only search: when the first candidate's click shows no text input, the next one is clicked;
* the page scripts' word rules match whole words or word starts.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import unittest

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D

QUIET = {"quiet_ms": 900, "mutations": 1, "ready": "complete"}


def run(c):
    return asyncio.run(c)


class Page:
    """A fake page: its text inputs (``SEARCH_INPUTS_JS`` rows), which input answers typing and how, and the reveal
    candidates (``REVEAL_SEARCH_JS``)."""

    def __init__(self, inputs, answers=None, reveal=None, after_reveal=None):
        self.inputs, self.answers = inputs, dict(answers or {})
        self.reveal, self.after_reveal = list(reveal or []), after_reveal
        self.focused, self.clicked, self.shows, self.typed = None, [], False, []

    def evaluate(self, e, session):
        if "__anyuseSettle" in e:
            return QUIET
        if "__anyuseRevealSearch" in e:
            k = int(re.search(r"\}\)\((\d+)\)\s*$", e).group(1))
            if k >= len(self.reveal):
                return {"clicked": False, "count": len(self.reveal)}
            self.clicked.append(self.reveal[k])
            if self.after_reveal and self.after_reveal[0] == k:
                self.inputs = self.after_reveal[1]
            return {"clicked": True, "label": self.reveal[k], "navigates": False, "count": len(self.reveal)}
        if "querySelectorAll('input').forEach" in e:
            return [dict(i) for i in self.inputs]
        if "el.focus(); el.value = ''" in e:
            self.focused = int(re.search(r"querySelectorAll\('input'\)\[(\d+)\]", e).group(1))
            self.shows = False
            return True
        if "innerText) || '').toLowerCase().includes" in e:
            return self.shows
        if "__anyuseFormGetUrl" in e:
            how = self.answers.get(self.focused)
            return {"method": "get", "url": "https://h.test/zoeken?trefwoord=red+fox"} if how == "form" else None
        if "String(el.value" in e:
            return self.typed[-1] if self.typed else ""
        if "__anyuseSubmitSearch" in e:
            return {"clicked": False, "how": "no submit control"}
        if "location.href" in e:
            return "https://h.test/"
        return None

    def typed_into(self, text, session):
        self.typed.append(text)
        how = self.answers.get(self.focused)
        if how == "request":
            session.budget.sent.append(("h.test", 0.0, "https://h.test/api/sugestoes?termo=red%20fox", "XHR"))
        elif how == "text":
            self.shows = True


class WS:
    def __init__(self, page, session):
        self.page, self.session, self.inbox, self.sent = page, session, [], []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m, p = msg.get("method"), msg.get("params") or {}
        if m in ("Fetch.continueRequest", "Fetch.failRequest"):
            return
        val = None
        if m == "Runtime.evaluate":
            val = self.page.evaluate(p["expression"], self.session)
        elif m == "Input.insertText":
            self.page.typed_into(p["text"], self.session)
        self.inbox.append({"id": msg["id"], "result": {"result": {"value": val}}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))


def session(page):
    s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(D.Pacer(1.0)))
    s._ws = WS(page, s)
    return s


def row(i, rank, name="", visible=True):
    return {"index": i, "visible": visible, "name": name, "id": "", "type": "text", "rank": rank, "search": rank < 3}


class TheSearchBox(unittest.TestCase):

    def test_structure_decides_first(self):
        pg = Page([row(0, 3, "email"), row(1, 0, "q"), row(2, 2, "query")])
        out = run(session(pg).type_search("red fox"))
        self.assertEqual(out["input"]["index"], 1)
        self.assertEqual(out["chosen_by"], "structure")
        self.assertEqual(pg.typed, ["red fox"])

    def test_the_input_whose_typing_the_page_answers_is_the_search_box(self):
        # a store finder named like a search, a postcode field, and the Portuguese search box with suggestions
        for how in ("request", "text", "form"):
            pg = Page([row(0, 2, "find-a-store"), row(1, 3, "cep"), row(2, 3, "termo")], answers={2: how})
            out = run(session(pg).type_search("red fox"))
            self.assertEqual(out["input"]["index"], 2, (how, out))
            self.assertTrue(out["chosen_by"].startswith("typing"), out["chosen_by"])
            self.assertEqual(pg.typed, ["red fox"] * 3, how)     # each tried once; the search box not again
            if how == "request":
                self.assertIn("https://h.test/api/sugestoes?termo=red%20fox", out["xhr"])

    def test_without_an_answer_a_name_decides_and_without_one_nothing_is_typed(self):
        pg = Page([row(0, 3, "cep"), row(1, 2, "keyword")])
        out = run(session(pg).type_search("red fox"))
        self.assertEqual((out["input"]["index"], out["chosen_by"]), (1, "name"))
        # only a newsletter field: typed once to see, emptied, and no search box is used
        pg = Page([row(0, 3, "nieuwsbrief")])
        out = run(session(pg).type_search("red fox"))
        self.assertIsNone(out["input"])
        self.assertEqual(out["note"], "no visible search input")

    def test_one_input_named_like_a_search_is_used_without_a_trial(self):
        pg = Page([row(0, 2, "query"), row(1, 3, "x", visible=False)])
        out = run(session(pg).type_search("red fox"))
        self.assertEqual((out["input"]["index"], out["chosen_by"]), (0, "name"))
        self.assertEqual(pg.typed, ["red fox"])

    def test_a_recorded_input_replays_as_before(self):
        pg = Page([row(0, 3, "termo"), row(1, 2, "q")])
        out = run(session(pg).type_search("red fox", want={"name": "q"}))
        self.assertEqual(out["input"]["index"], 1)
        self.assertNotIn("chosen_by", out)
        self.assertEqual(pg.typed, ["red fox"])

    def test_a_recorded_input_the_word_rules_no_longer_name_replays_by_its_name(self):
        # a tool recorded the box "sitesearch" when a word inside a name still counted; the whole-word rule ranks it
        # as any text input now, and the replay still finds it by its recorded name
        pg = Page([row(0, 3, "email"), row(1, 3, "sitesearch")])
        out = run(session(pg).type_search("red fox", want={"name": "sitesearch", "index": 1}))
        self.assertEqual(out["input"]["index"], 1)
        self.assertNotIn("chosen_by", out)
        self.assertEqual(pg.typed, ["red fox"])
        # the index fallback reads the search-like inputs only: a field no rule names is never typed into by index
        pg = Page([row(0, 3, "email")])
        out = run(session(pg).type_search("red fox", want={"name": "zoek", "index": 0}))
        self.assertIsNone(out["input"])
        self.assertEqual(pg.typed, [])


class TheIconOnlySearch(unittest.TestCase):

    def test_the_next_candidate_is_clicked_when_no_input_appeared(self):
        pg = Page([], reveal=["Menu", "Zoeken", "Winkelwagen"], after_reveal=(1, [row(0, 0, "q")]))
        out = run(session(pg).type_search("red fox"))
        self.assertEqual(pg.clicked, ["Menu", "Zoeken"])
        self.assertEqual(out["revealed"]["label"], "Zoeken")
        self.assertEqual(out["input"]["index"], 0)

    def test_it_stops_at_the_last_candidate_and_at_the_cap(self):
        pg = Page([], reveal=["Menu"])
        out = run(session(pg).type_search("red fox"))
        self.assertEqual(pg.clicked, ["Menu"])
        self.assertEqual(out["note"], "no visible search input")
        pg = Page([], reveal=["a", "b", "c", "d", "e"])
        run(session(pg).type_search("red fox"))
        self.assertEqual(pg.clicked, ["a", "b", "c"])


class TheScripts(unittest.TestCase):

    def test_the_word_lists_are_the_last_rung_and_read_whole_words(self):
        self.assertIn("rank", G.SEARCH_INPUTS_JS)
        rank_rule = G.SEARCH_INPUTS_JS.split("const rank =", 1)[1].split(";", 1)[0]
        for js in (G.SEARCH_INPUTS_JS, G.REVEAL_SEARCH_JS):
            self.assertIn("t === w || t.startsWith(w)", js)
        for rule in (rank_rule, G.REVEAL_SEARCH_JS):
            self.assertNotIn("/search|", rule)
        # the substring rule of web tools before version 15 is read only as such a tool's own flag (``named14``)
        self.assertEqual(G.SEARCH_INPUTS_JS.count("/search|"), 1)
        self.assertIn("const named14 = t === 'search'", G.SEARCH_INPUTS_JS)
        self.assertIn("[role=search], search", G.SEARCH_INPUTS_JS)
        self.assertIn("aria-controls", G.REVEAL_SEARCH_JS)
        self.assertEqual(G._ranked({"type": "search"})["rank"], 0)
        self.assertEqual(G._ranked({"type": "text"})["rank"], 2)
        # the submit words (the last rung among several controls) read word starts; "go" only as a whole word
        for label in ("Search", "btnSearch", "Go", "goButton", "Zoeken"):
            self.assertTrue(G.SUBMIT_WORDS.search(label), label)
        for label in ("Category", "logo", "Google sign in", "research", "GOOGLE", "GOLD MEMBERS"):
            self.assertFalse(G.SUBMIT_WORDS.search(label), label)
        for label in ("GO", "GoSearch", "go!"):
            self.assertTrue(G.SUBMIT_WORDS.search(label), label)
        # the agent's element list ranks search controls by structure first, then by whole words
        from anyuse.execution import agent_browser as AB
        for js in (AB.OBSERVE_JS, AB.PICK_JS, AB.BOUND_PICK_FN):
            self.assertIn("searchNamed(", js)
            self.assertIn("controlsInput(n)", js)
            self.assertNotIn("/search|suche", js)


def node_binary() -> str:
    return os.environ.get("ANYUSE_TEST_NODE") or shutil.which("node") or ""


def _bench_dom_js() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench", "generalize_scope_bench.py")
    spec = importlib.util.spec_from_file_location("generalize_scope_bench", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DOM_JS


@unittest.skipUnless(node_binary(), "needs a Node binary (ANYUSE_TEST_NODE or node on PATH)")
class TheRevealOrder(unittest.TestCase):
    """``REVEAL_SEARCH_JS`` itself, run in Node against the bench's DOM stand-in: the control each try clicks."""

    def clicks(self, dom, tries=3):
        req = json.dumps({"dom": dom, "inputs": G.SEARCH_INPUTS_JS,
                          "reveals": [G.REVEAL_SEARCH_JS % k for k in range(tries)]})
        proc = subprocess.run([node_binary(), "-e", _bench_dom_js()], input=req, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        res = json.loads(proc.stdout)[0]["reveal"]
        return [(r["clicks"] or [""])[-1] if (r["result"] or {}).get("clicked") else "" for r in res]

    def test_a_control_named_like_a_search_comes_before_other_header_icons(self):
        icons = [["button", {"class": "i", "data-name": n}, "", [["svg", {}]]] for n in ("menu", "account", "cart")]
        dom = [["header", {}, "", icons + [["button", {"data-toggle": "sp", "data-name": "Search"}, "Search"],
                                          ["div", {"id": "sp", "hidden": ""}, "",
                                           [["input", {"type": "search", "name": "q"}]]]]]]
        self.assertEqual(self.clicks(dom), ["Search", "menu", "account"])

    def test_an_icon_only_header_link_is_never_a_candidate(self):
        dom = [["header", {}, "", [["a", {"href": "/konto", "data-name": "konto"}, "", [["svg", {}]]],
                                   ["a", {"href": "/suche", "data-name": "Suche"}, "Suche"]]]]
        self.assertEqual(self.clicks(dom), ["Suche", "", ""])

    def test_unnamed_header_buttons_are_tried_in_page_order(self):
        dom = [["header", {}, "", [["button", {"class": "btn-icon", "data-name": "cart"}, "", [["svg", {}]]],
                                   ["button", {"class": "btn-icon", "data-name": "loep"}, "", [["svg", {}]]]]]]
        self.assertEqual(self.clicks(dom), ["cart", "loep", ""])


if __name__ == "__main__":
    unittest.main()
