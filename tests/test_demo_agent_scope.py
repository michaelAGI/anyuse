"""A demonstration's agent steps run inside the ``agent`` model-check scope, offline (``dom_page``; no browser, no
network; the check model is faked with ``model_check.using``).

What must hold:

1. a step the harness dispatches for a run a model drives opens the ``agent`` scope, so a search whose Enter does
   nothing puts the page's several search URLs to the configured small model; a replay (a given mandate, a policy
   without a model) opens none and asks nothing; inside a scope that is already open (a product call's) the step
   keeps that scope; no scope is left open after the run;
2. the demonstration loop (``demo_policy.run_demo``) runs its steps inside the ``agent`` scope the same way;
3. a drop-down option is chosen by its text, then by the one option that holds the text's words as whole words, then
   (in the ``agent`` scope only) by the model among the options, then by the earlier first-option rules;
4. the commit gate's "asked for" test reads the task's words whole: a task word that only holds a destructive keyword
   inside it ("dropdown", "undelete") asks for nothing, a label that holds one as part of a word still counts.
"""
import asyncio
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import demo_policy as DPOL  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from test_search_url_fallback_dead_enter import BASE, TWO_LINKS, DeadEnter  # noqa: E402

FIND = BASE + "/find?term={q}"


class Spy:
    """The fake model (it prefers ``/find``) and the scope ``web_arch.site_search_url`` ran in."""

    def __init__(self):
        self.asked, self.scopes = [], []
        self.real = W.site_search_url

    def model(self, instructions, state, obs):
        self.asked.append(state.splitlines()[-1])
        return 0.97 if state.endswith(">> " + FIND) else 0.03

    def __enter__(self):
        def spy(*a, **kw):
            self.scopes.append(MC.current_scope())
            return self.real(*a, **kw)
        W.site_search_url = spy
        DPOL.SEARCH_WAIT_S, self.wait = 0.0, DPOL.SEARCH_WAIT_S
        return self

    def __exit__(self, *exc):
        W.site_search_url = self.real
        DPOL.SEARCH_WAIT_S = self.wait


def harness_search(policy, mandate=None):
    page = D.DomPage(DeadEnter(TWO_LINKS))
    asyncio.run(page.goto(BASE + "/"))
    tools = HT.BrowserTools(page, BASE + "/", "dead.test")
    tr = H.Harness(HT.Toolbox(browser=tools), policy, H.HarnessConfig(max_steps=3), run_id="scope",
                   mandate=mandate).run("search for {q}", {"q": "red fox"})
    return tr.steps[0].outcome


def test_a_model_driven_harness_step_asks_inside_the_agent_scope():
    with Spy() as spy, MC.using(spy.model):
        out = harness_search(P.ScriptedPolicy([T.Search(0, "red fox"), T.Fail("x")], cost_per_call=0.001))
    assert out.data.get("search_url") == FIND, out
    assert spy.scopes == ["agent"] and len(spy.asked) == 2, (spy.scopes, spy.asked)
    assert MC.current_scope() == ""


def test_a_replay_asks_nothing():
    from anyuse.execution import commit_policy as CP
    for policy, mandate in ((P.ScriptedPolicy([T.Search(0, "red fox"), T.Fail("x")]), None),
                            (P.ScriptedPolicy([T.Search(0, "red fox"), T.Fail("x")], cost_per_call=0.001),
                             CP.Mandate.from_task("search for red fox", {}))):
        with Spy() as spy, MC.using(spy.model):
            out = harness_search(policy, mandate)
        assert out.data.get("search_url") == BASE + "/search?q={q}", out                 # the first by score
        assert spy.scopes == [""] and spy.asked == [], (spy.scopes, spy.asked)


def test_an_open_scope_is_kept():
    with Spy() as spy, MC.using(spy.model), MC.scope("product"):
        out = harness_search(P.ScriptedPolicy([T.Search(0, "red fox"), T.Fail("x")], cost_per_call=0.001))
        assert MC.current_scope() == "product"
    assert spy.scopes == ["product"] and out.data.get("search_url") == FIND, (spy.scopes, out)


def test_the_demonstration_loop_runs_its_steps_in_the_agent_scope():
    def cli(prompt):
        if not cli.done:
            cli.done = True
            k = int(re.search(r"\[(\d+)\] search box", prompt).group(1))
            return json.dumps({"action": "search", "element": k, "text": "red fox"}), {}
        return json.dumps({"action": "done", "result": {"results": [{"title": "Red fox plush",
                                                                     "url": BASE + "/p/0"}]}}), {}
    cli.done = False
    with Spy() as spy, MC.using(spy.model):
        page = D.DomPage(DeadEnter(TWO_LINKS))
        tr = asyncio.run(DPOL.run_demo(page, "site_search", "red fox", BASE + "/", "dead.test", cli, D.no_settle))
    first = tr["steps"][0]["outcome"]
    assert first.get("search_url") == FIND and spy.scopes == ["agent"], (first, spy.scopes)
    assert MC.current_scope() == ""


def test_a_drop_down_option_by_whole_words_then_the_model():
    m = {"text": "Sorteren", "options": [{"text": "Vernieuwd", "value": "a"}, {"text": "Nieuw binnen", "value": "b"},
                                         {"text": "Prijs oplopend", "value": "c"}, {"text": "Prijs aflopend",
                                                                                     "value": "d"}]}
    assert DPOL.choose_option(m, "Nieuw binnen")["value"] == "b"                     # its text
    assert DPOL.choose_option(m, "nieuw")["value"] == "b"                            # whole words (was "Vernieuwd")
    assert DPOL.choose_option(m, "oplopend")["value"] == "c"
    assert DPOL.choose_option(m, "prijs")["value"] == "c"                            # two hold it: the first
    assert DPOL.choose_option(m, "ernieu")["value"] == "a"                           # inside a word, as before
    # a text no option holds: the model in the agent scope only
    seen = []

    def fake(instructions, state, obs):
        seen.append(1)
        return 0.95 if state.endswith(">> Prijs oplopend") else 0.04
    assert DPOL.choose_option(m, "Preis aufsteigend") is None
    with MC.using(fake):
        assert DPOL.choose_option(m, "Preis aufsteigend") is None and seen == []
        with MC.scope("product"):
            assert DPOL.choose_option(m, "Preis aufsteigend") is None and seen == []
        with MC.scope("agent"):
            assert DPOL.choose_option(m, "Preis aufsteigend")["value"] == "c"
            assert DPOL.choose_option(m, "prijs")["value"] == "c"
        # the model's no leaves the first rule
        with MC.scope("agent"), MC.using(lambda i, s, o: 0.02):
            assert DPOL.choose_option(m, "prijs")["value"] == "c"
            assert DPOL.choose_option(m, "Preis aufsteigend") is None


def test_asked_for_reads_the_tasks_words_whole():
    assert H.asked_for("Delete the draft note", "Delete", "draft")
    assert H.asked_for("deleting the draft", "Delete", "draft")
    assert H.asked_for("Lösche die Notiz Entwurf", "Löschen", "Entwurf")
    assert H.asked_for("empty the trash", "Empty Trash", "")
    assert H.asked_for("pay the invoice", "PayNow", "")                              # a label part still counts
    assert H.destructive_words("PayNow") == ["pay"] and "delete" in H.destructive_words("Deleteall")
    # a task word that holds the keyword inside it
    assert not H.asked_for("choose the size in the dropdown", "Drop table", "")
    assert not H.asked_for("undelete my file", "Delete", "")
    assert not H.asked_for("show the buyer's guide", "Buy now", "")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
