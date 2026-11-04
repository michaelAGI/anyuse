"""Policy robustness (execution/agent_harness.py, harness_tools / web_flow corrections; final-web-fresh defect 5),
offline. Pinned here: an unreadable answer is re-asked ONCE with the parse error in the observation's note, then the
typed stop; an action naming an element that is not there (or not of that kind) gets a corrective observation that
lists the valid elements and uses NO step (at most HarnessConfig.index_corrections per run, each still a policy call
that the call cap allows for); a GET form's text field the open-task browser listed in flow numbering is searched like
the page's search box (www.fotografia.it), a POST form's or a login form's never. No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_policy_robustness.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import web_flow as WF  # noqa: E402

import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE


def run(script, cfg=None):
    tools = HT.BrowserTools(D.DomPage(SHOP.Shop()), BASE + "/", SITE)
    pol = P.ScriptedPolicy(script)
    tr = H.Harness(HT.Toolbox(browser=tools), pol, cfg or H.HarnessConfig(max_steps=6), run_id="rob").run(
        "search for {q}", {"q": "Bosch"})
    return tr, pol


def test_an_unreadable_answer_is_re_asked_once_with_the_parse_error_then_a_typed_stop():
    seen = []

    def check(t, o, h):
        seen.append(o.note)
        return T.Done({"no_results": True})
    # a "no results" the page does not state gets ONE corrective note (agent_harness.answer_problem); the
    # repeated answer is taken
    tr, _ = run(["I think the answer is", check, check])
    assert tr.ok and "COULD NOT BE READ (no JSON object in the answer)" in seen[0] and "element numbers" in seen[0]
    assert '"action": "done"' in seen[0]
    tr, _ = run(["not json", "still not json", T.Done({})])
    assert tr.stop == T.Stop.UNREADABLE and "no JSON object" in tr.stop_reason


def test_a_bare_result_object_is_read_as_done_and_nothing_else_is_guessed():
    d = P.parse_decision('{"results": [{"element": 9}, {"element": 12}]}')
    assert d.action is not None and d.action.kind == "done" and d.action.result["results"][1] == {"element": 12}
    assert P.parse_decision('{"no_results": true}').action.kind == "done"
    assert P.parse_decision('{"name": "x", "price": 3}').action is None          # a fact object is not guessed
    assert P.parse_decision('{"action": "search", "element": 0, "text": "a"}').action.kind == "search"


def test_a_wrong_element_gets_a_corrective_observation_and_no_step():
    seen = []

    def fixed(t, o, h):
        seen.append(o.note)
        return T.Search(0, "Bosch")
    tr, _ = run([T.Search(7, "Bosch"), fixed, lambda t, o, h: T.Done({"results": [{"element": e.index} for e in
                                                                                 o.elements if "/p/" in e.href][:2]})])
    w = tr.corrections[0]["why"] if tr.corrections else ""
    assert tr.ok and len(tr.corrections) == 1 and ("is not on this page" in w or "is not a search box" in w)
    assert "YOUR LAST ACTION WAS NOT RUN" in seen[0] and "The search boxes on this page: [0]" in seen[0]
    assert [s.k for s in tr.steps] == [0, 1]                  # the corrected action used no step
    assert tr.to_dict()["corrections"][0]["action"]["action"] == "search"
    # a link click on a search box is corrected too
    tr, _ = run([T.Click(0), T.Search(0, "Bosch"), T.Done({"no_results": True}), T.Done({"no_results": True})])
    assert "is a search box" in tr.corrections[0]["why"] and [s.k for s in tr.steps] == [0, 1]
    # the unstated "no results" gets its one corrective note, no step used; the repeated answer is taken
    assert len(tr.corrections) == 2 and tr.corrections[1]["why"].startswith("answer: ")


def test_corrections_are_capped_and_the_call_cap_allows_for_them():
    tr, pol = run([T.Search(7, "x")] * 6 + [T.Done({"no_results": True})], H.HarnessConfig(max_steps=3))
    assert len(tr.corrections) == 3                            # the 4th wrong index is a refused step
    assert tr.decisions <= 3 + 3 and tr.stop in (T.Stop.MAX_STEPS, T.Stop.CALL_CAP, T.Stop.NO_PROGRESS)
    assert H.is_index_refusal("element 7 is not on this page") and H.is_index_refusal("element(s) 3, 4 are not form "
                                                                                       "fields on this page")
    assert not H.is_index_refusal("blocked: robots.txt disallows this URL")


def test_a_results_page_lists_the_links_the_search_brought_first():
    tools = HT.BrowserTools(D.DomPage(SHOP.Shop()), BASE + "/", SITE)
    o1 = tools.observe()
    assert not any("/p/" in e.href for e in o1.elements)
    tools.dispatch(T.Search(0, "Akkuschrauber"), o1)
    o2 = tools.observe()
    kinds = [("box" if e.kind == "search box" else "p" if "/p/" in e.href else "other") for e in o2.elements]
    first_other = kinds.index("other")
    assert kinds[0] == "box" and "p" in kinds and all(k != "p" for k in kinds[first_other:])
    assert sorted(e.index for e in o2.elements) == sorted(o2.mapping)          # the numbers never change


def test_the_type_action_runs_on_the_sessions_loop():
    src = (pathlib.Path(__file__).resolve().parent.parent / "execution" / "harness_tools.py").read_text(encoding="utf-8")
    assert 'run_sync(self.page.focus(int((m.get("input") or {}).get("index", 0))), self.loop)' in src


def flow_obs(method="get", password=False, ftype="text"):
    tools = WF.FlowBrowserTools(D.DomPage(SHOP.Shop()), BASE + "/", SITE)
    tools.flow = {"forms": [{"key": "#searchform", "method": method, "action": "/", "has_password": password}]}
    f = {"k": 0, "ii": 8, "ci": 3, "tag": "input", "type": ftype, "name": "s", "label": "s", "form": "#searchform"}
    obs = T.Observation(kind="web", url=BASE + "/", elements=[T.Element(index=8, kind="text field [form searchform]",
                                                                      text="s")],
                        mapping={8: {"src": "field", "field": f, "form": "#searchform"}})
    return tools, obs


def test_a_get_forms_text_field_is_searched_like_the_search_box():
    tools, obs = flow_obs()
    assert tools.gate(T.Search(8, "Nikon"), obs) == ("", None)
    got = {}
    real = HT.BrowserTools.dispatch

    def capture(self, action, o):
        got["m"] = o.mapping[8]
        return T.Outcome(True, how="typed", url_after=BASE + "/?s=Nikon")
    HT.BrowserTools.dispatch = capture
    try:
        out = tools.dispatch(T.Search(8, "Nikon"), obs)
    finally:
        HT.BrowserTools.dispatch = real
    assert out.ok and got["m"]["src"] == "input" and got["m"]["input"]["index"] == 8 and got["m"]["input"]["name"] == "s"
    assert obs.mapping[8]["src"] == "field"                  # the recorded observation is unchanged
    for kw in ({"method": "post"}, {"password": True}, {"ftype": "email"}):
        t2, o2 = flow_obs(**kw)
        why, _hard = t2.gate(T.Search(8, "Nikon"), o2)
        assert why and "search box" in why, kw
    t3, o3 = flow_obs()
    assert "Search fields of a GET form: [8] s" in t3.correction(T.Search(1, "x"), o3)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
