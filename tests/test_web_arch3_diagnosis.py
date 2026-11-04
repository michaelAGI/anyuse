"""The causes of the plain agent's lead (runs 69 % vs the arch's 59 %) that
the code can fix, generic and for the arm they belong to. Offline (fakes only).

The diagnosis (from the saved records): 74 runs of both arms started on a page
the byte ceiling had killed; gate misses; planner refusals after "no visible search
input"; category pages answered; picker hand-offs, most on gated goals; a
script-rendered result list read before it rendered; 5.5 model calls per arch
run against 2.1 (354 shadow confirmer calls and the screenshot picker, never
acted on); and a judge that let a run ending on the START page
pass a search task (it compared the final page's full text with the first
page's main text, which never matched).

What must hold:

1. the search judge (every arm): the start page - fragment or not - is never a
   search's result page; a result page on the site naming the value passes;
2. after a search the engine waits (bounded, local reads only) for a result
   list that names the value, and records how long;
3. the web_arch3_live arch arm makes no shadow model call: no confirmer, no
   screenshot picker; its engine gets the gate check, the result wait and the
   recalibrated picker rule.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))

from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, FakePage, FakePlanner, S, run  # noqa: E402


def _rec(final_url, text, links):
    return {"start_url": BASE + "/", "stop": "done", "pages": [{"url": BASE + "/", "text_head": "Welcome"}],
            "final": {"url": final_url, "text": text, "links": links}}


def test_the_start_page_is_never_a_search_result():
    task = {"id": "T", "kind": "search", "value": "Kompostkvarn", "utterance": "Sök efter Kompostkvarn."}
    for u in (BASE + "/", BASE + "/#", BASE + "/#search"):
        j = W.judge_run(task, _rec(u, "Trädgård 2399:- Kompostkvarn 18-698", ["2399:- Kompostkvarn 18-698"]))
        assert j["verdict"] == "fail" and "start page" in j["why"], (u, j)
    j = W.judge_run(task, _rec(BASE + "/soksida/?query=Kompostkvarn", "3 träffar Kompostkvarn 18-698",
                               ["Kompostkvarn 18-698"]))
    assert j["verdict"] == "ok", j


class SlowResults(FakePage):
    """The fake shop whose result list renders only after two settles (a script list)."""

    def __init__(self):
        super().__init__()
        self.settles = 0

    async def settle(self, s):
        self.settles += 1

    async def js(self, expr, read_only=False):
        r = await super().js(expr, read_only)
        if expr == R.READ_JS and isinstance(r, dict) and "/search" in self.cur and self.settles < 3:
            r["elements"] = [e for e in r["elements"] if e.get("area") != "main"]
        return r


def test_the_engine_waits_for_a_script_rendered_result_list():
    page = SlowResults()
    rec = run(R.WebArch(FakePlanner([S("search", value="merino")]), shadow_images=False, result_wait_s=5.0).run(
        page, {"id": "T", "utterance": "Search for merino", "kind": "search"}, BASE + "/"))
    step = next(s for s in rec["steps"] if s["step"]["effect"] == "search")
    assert step.get("results_after_s") is not None, step
    assert any("Merino" in x for x in rec["final"]["links"]), rec["final"]["links"]
    page = SlowResults()
    rec = run(R.WebArch(FakePlanner([S("search", value="merino")]), shadow_images=False).run(
        page, {"id": "T", "utterance": "Search for merino", "kind": "search"}, BASE + "/"))
    step = next(s for s in rec["steps"] if s["step"]["effect"] == "search")
    assert "results_after_s" not in step                    # off by default


def test_the_prior_arch_arm_makes_no_shadow_calls():
    import web_arch3_live as W3
    seen = {}

    class FakeEngine:
        def __init__(self, planner, **kw):
            seen.update(kw)

        async def run(self, page, task, start):
            return {"stop": "done"}

    class Sess:
        def __init__(self, *a, **k):
            self.guard = type("G", (), {"failed": []})()
            self.fresh_check = {"fresh": True}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

    class Ctx:
        site = "www.shop.example"
        budget = type("B", (), {"lanes": {}, "site_of": lambda s, h: h})()

        def allowed(self, u):
            return True

        def used(self):
            return 3

        def nbytes(self):
            return 10
    old = (R.WebArch, W3.session_class)
    try:
        R.WebArch = FakeEngine
        W3.session_class = lambda: Sess
        arm = object.__new__(W3.Arm)
        arm.arm, arm.endpoint = "arch", "http://127.0.0.1:1"
        arm.planner = arm.extractor = arm.chooser = arm.gate_check = lambda *a, **k: ("", {})
        arm.t2, arm.spec, arm.picker_rule = 0.0, W.load_planner_spec(), W.load_picker_rule()
        rec = arm.run_task(Ctx(), {"id": "T", "kind": "search", "utterance": "x"}, BASE + "/", "/tmp")
    finally:
        R.WebArch, W3.session_class = old
    assert seen["confirmer"] is None and seen["shadow_images"] is False
    assert seen["gate_check"] is not None and seen["result_wait_s"] == W3.RESULT_WAIT_S > 0
    assert seen["picker_rule"]["tie_break"] == "pairwise"
    assert rec["requests_after"] == 3 and rec["fresh"] == [{"fresh": True}]


if __name__ == "__main__":
    import inspect
    import traceback
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn()
            p += 1
            print("OK  ", n)
        except Exception as e:                            # noqa: BLE001
            f += 1
            print("FAIL", n, "::", repr(e)[:400])
            traceback.print_exc(limit=6)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
