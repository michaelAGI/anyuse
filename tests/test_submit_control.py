"""The submit cascade of a typed search whose Enter does nothing, offline against a
scripted fake CDP websocket (no browser, no model):

* the form's default button, else the one control next to the input, is clicked
  by the page script itself (``SUBMIT_SEARCH_JS``);
* among several controls: the recorded label (a replay), then the configured
  small model's pick (a demonstration's session only, inside an open model-check
  scope), then the submit words, else nothing; the pick is re-checked on the page
  (``SUBMIT_PICK_JS``);
* then the input's own GET form URL (same site only);
* a web tool before version 14 replays the old rule (``SUBMIT_SEARCH_JS_V13``); one before version 15 reads
  its own submit words (``SUBMIT_WORDS_V14``).
"""
from __future__ import annotations

import asyncio
import json

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D
from anyuse.execution import model_check as MC

CANDS = {"clicked": False, "how": "candidates",
         "candidates": [{"k": 0, "label": "Filter"}, {"k": 1, "label": "Los"}, {"k": 2, "label": "Clear"}],
         "field": {"name": "q", "placeholder": "Produkt suchen", "label": ""}}
INPUT = [{"index": 0, "visible": True, "name": "q", "id": "", "type": "search"}]
QUIET = {"quiet_ms": 900, "mutations": 1, "ready": "complete"}


def run(c):
    return asyncio.run(c)


class WS:
    def __init__(self, script):
        self.script, self.inbox, self.sent, self.evals = script, [], [], []

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
                    self.evals.append((marker, e))
                    val = ans(e) if callable(ans) else (ans.pop(0) if len(ans) > 1 else ans[0])
                    break
        self.inbox.append({"id": msg["id"], "result": {"result": {"value": val}}})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))


def session(script, url="https://h.test/"):
    s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(D.Pacer(1.0)))
    base = dict(script)
    base.update({"__anyuseSettle": [QUIET], "String(el.value": lambda e: "red fox",
                 "location.href": [url]})
    base["el.focus(); el.value = ''"] = [True]
    s._ws = WS(base)
    return s


def picked(s):
    return [e for marker, e in s._ws.evals if marker == "__anyuseSubmitPick"]


def test_js_templates_format():
    assert "(3)" in G.SUBMIT_SEARCH_JS % 3 and "(-1)" in G.SUBMIT_SEARCH_JS % -1
    assert '(3, 1, "Los")' in G.SUBMIT_PICK_JS % (3, 1, json.dumps("Los"))
    assert "(3)" in G.FORM_GET_URL_JS % 3
    assert "(3)" in G.SUBMIT_SEARCH_JS_V13 % 3
    # the word list is no longer the page script's rule; it is the last fallback in Python
    assert "suche|recherche" not in G.SUBMIT_SEARCH_JS and "suche|recherche" in G.SUBMIT_SEARCH_JS_V13


def test_the_one_control_or_the_form_button_is_clicked_by_the_page():
    s = session({"__anyuseSubmitSearch": [{"clicked": True, "how": "the one control next to the input",
                                           "label": "Go"}]})
    assert run(s.click_submit(0))["clicked"]
    assert picked(s) == []


def test_the_models_pick_among_several_is_checked_on_the_page():
    s = session({"__anyuseSubmitSearch": [CANDS], "__anyuseSubmitPick": [{"clicked": True}]})
    s.submit_chooser = True
    asked = []

    def fake(instructions, state, obs):
        asked.append(state.splitlines()[-1])
        return 0.95 if state.endswith(">> Los") else 0.02
    with MC.scope("agent"), MC.using(fake):
        res = run(s.click_submit(0))
    assert res == {"clicked": True, "how": "the control the model chose", "label": "Los"}
    assert len(asked) == 3
    assert picked(s) and picked(s)[0].rstrip().endswith('(0, 1, "Los")')


def test_an_abstaining_model_falls_to_the_submit_words():
    s = session({"__anyuseSubmitSearch": [dict(CANDS, candidates=[{"k": 0, "label": "Filter"},
                                                                   {"k": 1, "label": "Suche starten"}])],
                 "__anyuseSubmitPick": [{"clicked": True}]})
    s.submit_chooser = True
    with MC.scope("agent"), MC.using(lambda i, st, o: 0.5):
        res = run(s.click_submit(0))
    assert res["clicked"] and res["how"] == "a control named like a search"
    assert picked(s)[0].rstrip().endswith('(0, 1, "Suche starten")')


def test_a_replay_clicks_the_recorded_label_and_asks_no_model():
    s = session({"__anyuseSubmitSearch": [CANDS], "__anyuseSubmitPick": [{"clicked": True}]})
    calls = []
    with MC.scope("product"), MC.using(lambda i, st, o: calls.append(1) or 0.9):
        res = run(s.click_submit(0, prefer_label="Los"))
    assert res["how"] == "the recorded control" and calls == []


def test_no_session_chooser_means_no_model_even_in_a_scope():
    s = session({"__anyuseSubmitSearch": [CANDS], "__anyuseSubmitPick": [{"clicked": True}]})
    calls = []
    with MC.scope("product"), MC.using(lambda i, st, o: calls.append(1) or 0.9):
        res = run(s.click_submit(0))
    assert calls == [] and not res["clicked"] and "none chosen" in res["how"]
    assert picked(s) == []


def test_a_pick_the_page_no_longer_shows_is_not_clicked():
    s = session({"__anyuseSubmitSearch": [CANDS], "__anyuseSubmitPick": [{"clicked": False,
                                                                          "how": "the control is no longer there"}]})
    res = run(s.click_submit(0, prefer_label="Los"))
    assert not res["clicked"]


def test_a_dead_enter_and_control_submit_the_inputs_own_get_form():
    s = session({"querySelectorAll('input').forEach": [INPUT],
                 "__anyuseSubmitSearch": [{"clicked": False, "how": "no submit control"}],
                 "__anyuseFormGetUrl": [{"method": "get", "url": "https://h.test/find?term=red+fox"}]})
    gone = []

    async def goto(url):
        gone.append(url)
        s.budget.sent.append(("h.test", 0.0, url, "Document"))
    s.goto = goto
    out = run(s.type_search("red fox"))
    assert gone == ["https://h.test/find?term=red+fox"]
    assert out["form_url"] == "https://h.test/find?term=red+fox"
    assert out["documents"] == ["https://h.test/find?term=red+fox"]
    assert "moved" in out


def test_a_form_url_on_another_site_is_not_loaded():
    s = session({"querySelectorAll('input').forEach": [INPUT],
                 "__anyuseSubmitSearch": [{"clicked": False, "how": "no submit control"}],
                 "__anyuseFormGetUrl": [{"method": "get", "url": "https://other.test/find?term=red+fox"}]})
    gone = []

    async def goto(url):
        gone.append(url)
    s.goto = goto
    out = run(s.type_search("red fox"))
    assert gone == [] and "form_url" not in out and out["moved"] is False


def test_a_form_url_without_the_typed_value_is_not_loaded():
    # the one control next to the box emptied it (a clear button): its form now submits nothing
    s = session({"querySelectorAll('input').forEach": [INPUT],
                 "__anyuseSubmitSearch": [{"clicked": True, "how": "the one control next to the input",
                                           "label": "Clear"}],
                 "__anyuseFormGetUrl": [{"method": "get", "url": "https://h.test/find?term="}]})
    gone = []

    async def goto(url):
        gone.append(url)
    s.goto = goto
    out = run(s.type_search("red fox"))
    assert gone == [] and "form_url" not in out, out


def test_a_tool_before_version_14_replays_the_old_rule():
    s = session({"querySelectorAll('input').forEach": [INPUT],
                 "__anyuseSubmitSearch": [{"clicked": False}],
                 "__anyuseFormGetUrl": [{"method": "get", "url": "https://h.test/find?term=red+fox"}]})
    out = run(s.type_search("red fox", form_fallback=False))
    exprs = [e for marker, e in s._ws.evals if marker == "__anyuseSubmitSearch"]
    assert exprs and "search button next to the input" in exprs[0]
    assert "__anyuseFormGetUrl" not in [m for m, _e in s._ws.evals]
    assert "form_url" not in out and "moved" not in out


def test_a_tool_before_version_15_reads_its_own_submit_words():
    # among several controls with no recorded label, the last rung's words: a tool before version 15 keeps its
    # substring rule ("go" inside "Category"), a version-15 tool reads word starts
    cands = dict(CANDS, candidates=[{"k": 0, "label": "Category"}, {"k": 1, "label": "Suche"}])
    want = {"name": "q", "id": "", "index": 0}
    for named14, label in ((True, "Category"), (False, "Suche")):
        s = session({"querySelectorAll('input').forEach": [INPUT], "__anyuseSubmitSearch": [cands],
                     "__anyuseSubmitPick": [{"clicked": True}], "__anyuseFormGetUrl": [{}]})
        out = run(s.type_search("red fox", want=want, named14=named14))
        assert out["submit"] == {"clicked": True, "how": "a control named like a search", "label": label}, out
    assert G.SUBMIT_WORDS_V14.search("Category") and not G.SUBMIT_WORDS.search("Category")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
