"""Ladder-only, OFF by default: the BLOCK-AWARE list rule (``LadderConfig(list_rule2=True)``,
``repair_ladder._BlockAwareChecker``, ``repair_check.judge``'s ``coverage_ok``), offline on fakes shaped like the live
run's pages (no browser, no network, no model).

The live run: probation withheld 10 correct answers, 8 of them site-search answers the checker refused only because
"answered result(s) name no word of the input" - the site's search matches the article text, not the titles
(www.woodworker.de, www.traumflieger.de, www.ladige.it, skitour.fr, e-hausaufgaben.de); five of
them were values where the saved tool answered a SIDEBAR (wrong) and the upgraded revision the real results. The naming
rule exists for sidebar / promotion lists; the block sentinel's reading of the answer's own page now decides that.

What must hold:

1. ``judge``: an unnamed-only disagreement carries ``coverage_ok`` (the order, membership and coverage rules hold);
2. the block-aware checker: the same disagreement is an agreement when no answered result sits in a block on the
   answer's own page; a sidebar answer (www.traumflieger.de's "NEUE REPORTS" aside) stays a disagreement; no
   own page: a disagreement;
3. through the ladder (probation on an accepted revision): withheld with the rule off (as in the live run), reported
   with it on; the sidebar answer is withheld either way.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import repair_check as RC  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import Store  # noqa: E402

BASE = "https://www.woodworker.test"
REAL = [(f"{BASE}/news/messe-holz-2026", "Holz-Handwerk 2026: die Messe öffnet"),
        (f"{BASE}/news/ausbildung-schreiner", "Schreiner-Ausbildung: neue Ordnung"),
        (f"{BASE}/news/cnc-portal", "CNC-Portal für kleine Betriebe")]
SIDE = [(f"{BASE}/news/imm-cologne", "imm cologne abgesagt"), (f"{BASE}/news/tannenholzdekor", "Tannenholzdekor"),
        (f"{BASE}/news/perfect-sortiment", "Perfect-Sortiment")]
VALUE = "Zukunft"


def page_dom(main, aside=()):
    m = "".join(f'<div class="news"><a href="{u}">{t}</a><p>... die Zukunft des Handwerks ...</p></div>' for u, t in main)
    a = "".join(f'<li><a href="{u}">{t}</a></li>' for u, t in aside)
    return (f'<html><body><div id="content"><h1>Suchergebnis</h1>{m}</div><aside id="sidebar-a"><h3>weiter News</h3>'
            f'<ul>{a}</ul></aside></body></html>')


def render(links):
    return {"how": "url", "links": [{"url": u, "text": t, "card": ""} for u, t in links]}


TOOL = {"workflow": "site_search", "site": "www.woodworker.test", "version": 12, "top_k": 5, "start_url": BASE + "/",
        "search": {"mode": "url", "template": BASE + "/suche?q={q}"}, "region": {"cards": [{"kind": "title"}]},
        "checks": {}, "params": [], "hosts": ["www.woodworker.test"]}


def answer(xs):
    return {"status": "ok", "output": {"results": [{"title": t, "url": u} for u, t in xs]},
            "checks": {"result_page": {"n_items": len(xs), "url": BASE + "/suche?q=Zukunft"}},
            "pages": [{"title": "Suchergebnis", "h1": ["Suchergebnis"]}], "why": "query: 3 items"}


def test_judge_marks_an_unnamed_only_disagreement():
    v = RC.judge(TOOL, VALUE, answer(REAL), render(REAL + SIDE), {})
    assert v["verdict"] == "disagrees" and v["unnamed"] and v["coverage_ok"] is True, v
    v = RC.judge(TOOL, VALUE, answer(SIDE), render(REAL + SIDE), {})
    assert v["verdict"] == "disagrees"
    assert L.LadderConfig().list_rule2 is False


class FakeLadder:
    def __init__(self):
        self.doms = {}


def test_the_block_aware_checker():
    inner = lambda t, v, o: RC.judge(t, v, o, render(REAL + SIDE), {})               # noqa: E731
    lad = FakeLadder()
    chk = L._BlockAwareChecker(inner, lad)
    good = answer(REAL)
    lad.doms[id(good)] = (page_dom(REAL, SIDE), BASE + "/suche?q=Zukunft")
    v = asyncio.run(chk(TOOL, VALUE, good))
    assert v["verdict"] == "agrees" and "waived" in v, v
    side = answer(SIDE)
    lad.doms[id(side)] = (page_dom(REAL, SIDE), BASE + "/suche?q=Zukunft")
    assert asyncio.run(chk(TOOL, VALUE, side))["verdict"] == "disagrees"            # the sidebar stays refused
    assert asyncio.run(chk(TOOL, VALUE, answer(REAL)))["verdict"] == "disagrees"    # no own page known


class Page:
    def __init__(self, dom):
        self._dom = dom

    async def dom(self):
        return self._dom

    async def url(self):
        return BASE + "/suche?q=Zukunft"

    async def text(self):
        return "Suchergebnis"


def _probation_run(reads, rule2):
    saved = T.run_tool

    async def fake(page, tool, value, settle, **kw):
        return dict(answer(reads), value=value, model_calls=0)
    T.run_tool = fake
    st = Store(dict(TOOL, version=9))
    try:
        st.s.add_revision(TOOL, 0, {"upgrade": {"from_version": 9, "to_version": 12}}, {}, 0.0, 0, kind="upgrade")
        cfg = L.LadderConfig(enabled=True, tier1=False, tier3=False, probation=True, list_rule2=rule2)
        chk = lambda t, v, o: RC.judge(t, v, o, render(REAL + SIDE), {})             # noqa: E731
        return asyncio.run(L.run_with_ladder(lambda: Page(page_dom(REAL, SIDE)), TOOL, VALUE,
                                             lambda p: None, config=cfg, store=st.s, checker=chk, result_wait_s=0.0))
    finally:
        T.run_tool = saved
        st.close()


def test_probation_with_the_block_aware_rule():
    out = _probation_run(REAL, False)
    assert out["status"] == "incomplete" and "probation" in out["why"] and "name no word" in out["why"], out["why"]
    out = _probation_run(REAL, True)
    assert out["status"] == "ok" and out["output"]["results"][0]["url"] == REAL[0][0], out.get("why")
    for rule2 in (False, True):
        out = _probation_run(SIDE, rule2)
        assert out["status"] != "ok", (rule2, out.get("why"))


if __name__ == "__main__":
    test_judge_marks_an_unnamed_only_disagreement()
    test_the_block_aware_checker()
    test_probation_with_the_block_aware_rule()
    print("test_repair_list_rule2: 3 ok")
