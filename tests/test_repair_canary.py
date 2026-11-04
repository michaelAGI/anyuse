"""The INPUT-vs-DRIFT canary and the loop guards of the repair ladder (``execution/repair_canary.py``,
``execution/repair_ladder.py``), offline on the fake shop of
``test_repair_ladder``: no browser, no network, no model.

What must hold:

1. OFF by default: ``LadderConfig()`` has the canary, the probe and both loop guards off;
2. the canary says INPUT: a new input fails at tier 0 on an undrifted shop, the freshest OTHER input with a last known
   good answer is re-run and answers as before - the run keeps its failure, reported as the input's own
   (``input_failure``), no repair tier runs, no revision is written, the state stays healthy;
3. the canary says DRIFT: the shop renamed its classes - the known-good input fails too and the ladder proceeds to
   tier 1 exactly as without the canary;
4. no LKG: no canary, the ladder exactly as without it (the same outcome, the same revisions);
5. the cache: one canary per tool and revision per ``canary_ttl_min``; a new revision or the TTL's end runs a fresh
   one; by default an input with a positive last known good answer of its own gets no canary (its
   failure is drift evidence) - with ``canary_own_lkg=True`` a cached INPUT verdict is not
   reused for it;
6. traffic booking: the canary's page comes from the ladder's own page factory (the site's budget counts it) and its
   page loads are booked in the run's ledger row;
7. a tool with a typed search, a submit / pay step or an unknown field gets NO re-run canary
   (refused, with a typed reason; no page opened for it); the PROBE canary walks such a tool's path only up to the
   first step that is not read-only and never triggers it (asserted on a recording page);
8. loop guard 1: a repair byte-identical to the active revision is no drift - nothing written (no revision, no LKG,
   no rewrite counted); loop guard 2: a failure within 10 minutes of an accepted repair marks the tool STALE for 30
   minutes (needs human, no repair attempt), later a repair runs again.
"""
import asyncio
import copy
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_canary as RC  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import BASE, RENAMED, Shop, Store, compiled, ladder_run, trace  # noqa: E402
from test_repair_state import Clock  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"
V2 = "Makita DF333 Akkuschrauber"
MISSING = "Festool TXS Akkuschrauber"       # the shop lists no Festool: the run fails ("no result names the input")


def cfg(**kw):
    return L.LadderConfig(enabled=True, canary=True, **kw)


class Factory:
    """The ladder's page factory with a site budget: every page it opens is counted, every page load booked."""

    def __init__(self, site):
        self.site, self.pages = site, []

    def __call__(self):
        p = D.DomPage(self.site)
        self.pages.append(p)
        return p

    def loads(self):
        return sum(len(p.gotos) for p in self.pages)


def run(factory, tool, value, store, **kw):
    c = kw.pop("config", None) or cfg()
    return asyncio.run(L.run_with_ladder(factory, tool, value, D.no_settle, config=c, store=store, result_wait_s=0.0,
                                         **kw))


def test_off_by_default():
    c = L.LadderConfig()
    assert (c.canary, c.probe_canary, c.identical_no_drift, c.stale_guard) == (False, False, False, False)
    assert (c.canary_ttl_min, c.stale_after_repair_min, c.stale_for_min) == (30.0, 10.0, 30.0)
    assert c.canary_own_lkg is False             # the own-LKG exemption is part of the canary unless switched back


def test_canary_says_input():
    tool = compiled(V)
    clock = Clock()
    st = Store(tool, clock=clock)
    try:
        assert ladder_run(Shop(), tool, V2, st.s)["status"] == "ok"
        clock.advance(minutes=1)
        assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
        clock.advance(minutes=1)
        f = Factory(Shop())
        out = run(f, tool, MISSING, st.s)
        lad = out["ladder"]
        assert out["status"] == "failed" and lad["outcome"] == "input" and lad["tier"] == 0, lad
        assert out["input_failure"]["reason"] == "not found: no result the tool reads names the input"
        c = lad["evidence"]["canary"]
        assert c["verdict"] == "input" and c["cached"] is False and c["status"] == "ok"
        # the FRESHEST other LKG (V, answered last), never the failing input; its text never leaves the store
        assert st.s.canaries()[-1]["value"] == V and V not in str(out["input_failure"])
        assert lad["evidence"]["attempts"] == [] and len(f.pages) == 2          # tier 0 + the canary, no repair
        assert [r["rev"] for r in st.s.revisions()] == [0] and st.s.lkg(MISSING) is None
        assert lad["line"].startswith("tier 0: INPUT") and lad["state"] == "healthy"
        # without the canary the same failure climbs to tier 1
        st2 = Store(tool)
        try:
            ladder_run(Shop(), tool, V, st2.s)
            f2 = Factory(Shop())
            out2 = run(f2, tool, MISSING, st2.s, config=L.LadderConfig(enabled=True))
            assert out2["ladder"]["outcome"] in ("needs_human", "changed_unverified") and len(f2.pages) > 2
            assert any(a.get("tier") == 1 for a in out2["ladder"]["evidence"]["attempts"])
        finally:
            st2.close()
    finally:
        st.close()


def test_canary_says_drift_and_the_ladder_proceeds():
    tool = compiled(V)
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
        out = run(Factory(Shop(cls=RENAMED)), tool, V2, st.s)
        lad = out["ladder"]
        c = lad["evidence"]["canary"]
        assert c["verdict"] == "drift" and "fails too" in c["why"], c
        assert any(a.get("tier") == 1 for a in lad["evidence"]["attempts"]) and lad["outcome"] != "input"
        assert lad["canary"]["verdict"] == "drift" and lad["canary"]["gotos"] >= 1
    finally:
        st.close()
    # the verdict rule alone: another product, a collapsed list, a known wrong answer are DRIFT; an overlap is INPUT
    lkg = {"answer": {"name": V, "price": 89.9, "currency": "EUR", "availability": "x", "url": BASE + "/p/bosch-gsr-12v"},
           "count": 0, "echo": False}
    ok = {"status": "ok", "output": {"name": V, "price": 79.0, "url": BASE + "/p/bosch-gsr-12v"}}
    assert RC.canary_verdict(tool, V, ok, lkg)[0] == "input"            # a new price is no drift
    other = {"status": "ok", "output": {"name": V2, "url": BASE + "/p/makita-df333"}}
    assert RC.canary_verdict(tool, V, other, lkg)[0] == "drift"
    assert RC.canary_verdict(tool, V, ok, lkg, known_wrong=True)[0] == "drift"
    lt = compiled(V, workflow="site_search")
    llkg = {"answer": {"results": [BASE + f"/p/{s}" for s in "abcd"]}, "count": 4, "echo": False}
    assert RC.canary_verdict(lt, "x", {"status": "ok", "output": {"results": [{"url": BASE + "/p/a"}]}}, llkg)[0] == "drift"
    two = {"status": "ok", "output": {"results": [{"url": BASE + "/p/z"}, {"url": BASE + "/p/a"}]}}
    assert RC.canary_verdict(lt, "x", two, llkg)[0] == "input"
    none = {"status": "ok", "output": {"results": [{"url": BASE + "/p/y"}, {"url": BASE + "/p/z"}]}}
    assert RC.canary_verdict(lt, "x", none, llkg)[0] == "drift"
    # the readout in plain words (the measurement's wording fix: an unreadable result is never "not found")
    reason = RC.input_reason
    assert reason({"status": "incomplete", "why": "incomplete: no result names the input (best share 0.60 < 0.75); a "
                                                  "result the compiled structure cannot read names it"}).startswith(
        "some results for this input could not be read")
    assert reason({"status": "failed", "why": "membership: no item of the compiled region on the result page and no "
                                              "'no results' statement"}).startswith("the tool reads no result")
    assert reason({"status": "incomplete", "why": "incomplete: identity: the asked-for variant is not listed - x"}) == \
        "the asked-for variant is not listed"
    assert reason({"status": "incomplete", "why": "ladder sentinel: no results where the last known good answer had "
                                                  "results"}) == "no results for this input (it had results before)"


def test_no_lkg_is_the_ladder_as_today():
    tool = compiled(V)
    outs = []
    for c in (L.LadderConfig(enabled=True), cfg()):
        st = Store(tool)
        try:
            out = run(Factory(Shop()), tool, MISSING, st.s, config=c)
            lad = out["ladder"]
            outs.append((out["status"], lad["outcome"], lad["tier"], len(st.s.revisions()),
                         [a.get("tier") for a in lad["evidence"]["attempts"]]))
            if c.canary:
                assert lad["evidence"]["canary"]["verdict"] == "no_lkg" and lad["canary"]["gotos"] == 0
        finally:
            st.close()
    assert outs[0] == outs[1], outs
    # only a POSITIVE answer can be a canary: "no results" answering as before proves nothing
    assert RC.pick_canary({"a": {"answer": {"no_results": True}, "at": "2026-10-09T10:00:00Z"}}, "b") is None
    assert RC.pick_canary({"a": {"answer": {"url": "u"}, "at": "2026-10-09T10:00:00Z"},
                           "c": {"answer": {"url": "v"}, "at": "2026-10-09T11:00:00Z"}}, "c") == "a"


def test_the_cache_and_traffic_booking():
    tool = compiled(V)
    clock = Clock()
    st = Store(tool, clock=clock)
    try:
        assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
        f = Factory(Shop())
        out = run(f, tool, MISSING, st.s)
        lad = out["ladder"]
        # traffic: the canary ran on a page of the ladder's own factory; its loads are booked in the ledger row
        assert len(f.pages) == 2 and lad["canary"]["gotos"] == len(f.pages[1].gotos) >= 1
        assert f.loads() == len(f.pages[0].gotos) + lad["canary"]["gotos"]
        assert st.s.ledger()[-1]["canary"] == {"verdict": "input", "cached": False, "gotos": lad["canary"]["gotos"]}
        clock.advance(minutes=10)
        f = Factory(Shop())
        out = run(f, tool, "Bosch GSR 18V Profi Koffer", st.s)
        assert out["ladder"]["outcome"] == "input" and out["ladder"]["evidence"]["canary"]["cached"] is True
        assert len(f.pages) == 1 and out["ladder"]["canary"]["gotos"] == 0          # no second canary run
        # an input with a positive last known good answer of its own (the default): no canary at all -
        # its failure is drift evidence; the ladder proceeds as without the canary
        st.s.set_lkg("Bosch GSR 18V Profi Koffer", "product_lookup", ladder_run(Shop(), tool, V, st.s), 0)
        f = Factory(Shop())
        out = run(f, tool, "Bosch GSR 18V Profi Koffer", st.s, config=cfg(tier1=False, tier3=False))
        c = out["ladder"]["evidence"]["canary"]
        assert c["verdict"] == "own_lkg" and len(f.pages) == 1 and out["ladder"]["outcome"] == "needs_human", c
        # canary_own_lkg=True: a canary runs, and a cached INPUT is not reused for such an input
        f = Factory(Shop())
        out = run(f, tool, "Bosch GSR 18V Profi Koffer", st.s, config=cfg(canary_own_lkg=True))
        assert out["ladder"]["evidence"]["canary"]["cached"] is False and len(f.pages) == 2
        st.s.drop_lkg("Bosch GSR 18V Profi Koffer")
        # after the TTL a fresh canary runs
        clock.advance(minutes=31)
        f = Factory(Shop())
        out = run(f, tool, MISSING, st.s)
        assert out["ladder"]["evidence"]["canary"]["cached"] is False and len(f.pages) == 2
        # a cached DRIFT is reused (the ladder proceeds without a second canary run)
        clock.advance(minutes=31)
        f = Factory(Shop(cls=RENAMED))
        run(f, tool, MISSING, st.s, config=cfg(tier1=False, tier3=False))
        clock.advance(minutes=1)
        f = Factory(Shop(cls=RENAMED))
        out = run(f, tool, "Bosch GSR 18V Profi Koffer", st.s, config=cfg(tier1=False, tier3=False))
        c = out["ladder"]["evidence"]["canary"]
        assert c["verdict"] == "drift" and c["cached"] is True and len(f.pages) == 1
    finally:
        st.close()


class Recorder(D.DomPage):
    """A page that records every action: the probe may look, never type, click, choose or submit."""

    def __init__(self, site):
        super().__init__(site)
        self.typed, self.clicked = [], []

    async def type_search(self, value, want=None, **kw):
        self.typed.append(value)
        return await super().type_search(value, want, **kw)

    async def js(self, expr, read_only=False):
        if "n.click()" in str(expr) or "dispatchEvent(new Event('change'" in str(expr):
            self.clicked.append(str(expr)[-60:])
        return await super().js(expr, read_only)


def typed_tool():
    t = compiled(V)
    t = copy.deepcopy(t)
    t["search"] = {"mode": "typed", "input": {"index": 0, "name": "q"}, "fallback_templates": [],
                   "start_url": BASE + "/", "overlay": False}
    return t


def test_only_read_only_tools_get_a_rerun_canary():
    tool = compiled(V)
    assert RC.rerun_refusal(tool) is None and RC.effect_class(tool)["class"] == "read_only"
    # a typed search is an action on a control (the HIL gate: side effecting) - fail closed
    r = RC.rerun_refusal(typed_tool())
    assert r["code"] == "not_read_only" and r["class"] == "side_effecting" and r["step"] == "search", r
    # a submit / pay step: destructive by the HIL gate's words or a gated control
    pay = copy.deepcopy(tool)
    pay["search"]["template"] = BASE + "/checkout/pay?item={q}"
    r = RC.rerun_refusal(pay)
    assert r["code"] == "not_read_only" and r["class"] == "destructive" and r["step"] == "search", r
    sub = copy.deepcopy(tool)
    sub["refine"] = {"mode": "controls", "controls": [{"tag": "button", "text": "Bestellung absenden"}]}
    r = RC.rerun_refusal(sub)
    assert r["code"] == "not_read_only" and r["class"] == "destructive" and r["step"] == "refine.controls[0]", r
    post = copy.deepcopy(tool)
    post["search"]["method"] = "POST"
    assert RC.rerun_refusal(post)["class"] == "destructive"
    # an UNKNOWN effect fails closed: a field the classification does not know, an unknown search mode
    for k, v in (("submit_form", {"fields": ["email"]}), ("login", True)):
        unk = dict(tool, **{k: v})
        r = RC.rerun_refusal(unk)
        assert r["code"] == "unknown_effect" and r["step"] == k and r["class"] == "destructive", r
    odd = copy.deepcopy(tool)
    odd["search"]["mode"] = "graphql"
    assert RC.rerun_refusal(odd)["code"] == "unknown_effect"
    # a declared effect can only make it stricter
    assert RC.rerun_refusal(dict(tool, effects={"class": "side_effecting", "reversible": False}))["code"] == \
        "not_read_only"
    assert RC.rerun_refusal(dict(tool, effects={"class": "read_only", "reversible": True})) is None
    # never from the name: a "site_search" label does not make a paying tool read-only
    assert RC.rerun_refusal(dict(pay, workflow="site_search"))["code"] == "not_read_only"
    # through the ladder: a refused tool's failure opens NO canary page and climbs as without the canary
    st = Store(pay)
    try:
        st.s.set_lkg(V, "product_lookup", ladder_run(Shop(), tool, V, Store(tool).s), 0)
        f = Factory(Shop())
        out = run(f, pay, MISSING, st.s, config=cfg(tier1=False, tier3=False))
        c = out["ladder"]["evidence"]["canary"]
        assert c["verdict"] == "refused" and c["refusal"]["code"] == "not_read_only" and len(f.pages) == 1
        assert out["ladder"]["outcome"] == "needs_human"
    finally:
        st.close()


def test_a_typed_search_projects_every_step_its_replay_may_take():
    # version 14 on: when Enter moves nothing the replay clicks the search's own control (the recorded one first), then
    # loads the URL the input's own GET form gives; an older tool's plan lists the typing and Enter only
    t14 = dict(typed_tool(), version=14)
    t14["search"]["input"] = {"index": 0, "name": "q", "submit_label": "Suchen"}
    steps = {s["step"]: s for s in RC.step_plan(t14)}
    assert [k for k in steps if k.startswith("search")] == ["search", "search.submit", "search.control", "search.form"]
    ctl, form = steps["search.control"], steps["search.form"]
    assert ctl["kind"] == "click" and ctl["identity"] == {"text": "Suchen"} and RC.classify(ctl)[0] == "side_effecting"
    assert form["kind"] == "goto" and form["needs_value"] and RC.classify(form)[0] == "read_only"
    ec = RC.effect_class(t14)
    assert ec["class"] == "side_effecting" and ec["steps"][ec["first_irreversible"]]["step"] == "search", ec
    # a recorded control the gate reads as destructive makes the tool so
    t14["search"]["input"]["submit_label"] = "Absenden"
    assert RC.effect_class(t14)["class"] == "destructive"
    t13 = dict(typed_tool(), version=13)
    assert [s["step"] for s in RC.step_plan(t13) if s["step"].startswith("search")] == ["search", "search.submit"]

def test_the_probe_never_triggers_the_first_irreversible_step():
    t = typed_tool()
    page = Recorder(Shop())
    pr = asyncio.run(RC.probe_path(page, t, D.no_settle, value=V))
    assert pr["verdict"] == "path_intact" and pr["stopped_before"] == "search", pr
    assert page.typed == [] and page.clicked == [] and page.gotos == [BASE + "/"]
    assert pr["checks"][-1]["triggered"] is False and pr["checks"][-1]["present"] is True
    # the recorded input is gone (renamed): the path is broken - still nothing typed
    page = Recorder(Shop(qkey="suchbegriff"))
    pr = asyncio.run(RC.probe_path(page, t, D.no_settle))
    assert pr["verdict"] == "path_broken" and page.typed == [] and page.clicked == []

    # a plan with a COMMIT control (a pay button): the walk stops before it, looks, never clicks
    class Checkout(Shop):
        def route(self, url):
            if url.endswith("/kasse"):
                return D.Recorded(url, "<html><head><title>Kasse</title></head><body><main><h1>Kasse</h1>"
                                       "<form method=\"post\" action=\"/pay\"><input name=\"email\">"
                                       "<button type=\"submit\">Jetzt bezahlen</button></form></main></body></html>"), url
            if url.endswith("/pay"):
                raise AssertionError("the probe triggered the commit step")
            return super().route(url)
    plan = [{"step": "open", "kind": "goto", "action": {"action": "read", "args": {"url": BASE + "/kasse"}},
             "url": BASE + "/kasse", "identity": {}, "needs_value": False, "gated": ""},
            {"step": "pay", "kind": "click", "action": {"action": "click", "args": {"text": "Jetzt bezahlen"}},
             "url": "", "identity": {"tag": "button", "text": "Jetzt bezahlen"}, "needs_value": False, "gated": ""}]
    assert RC.classify(plan[1])[0] == "destructive"
    page = Recorder(Checkout())
    pr = asyncio.run(RC.probe_path(page, plan, D.no_settle))
    assert pr["verdict"] == "path_intact" and pr["stopped_before"] == "pay", pr
    assert page.clicked == [] and page.typed == [] and page.gotos == [BASE + "/kasse"]
    # in the ladder (probe_canary on): the refused tool gets the probe's evidence, never an INPUT verdict
    st = Store(t)
    try:
        st.s.set_lkg(V, "product_lookup", ladder_run(Shop(), compiled(V), V, Store(compiled(V)).s), 0)
        rec = []

        def factory():
            p = Recorder(Shop(qkey="suchbegriff"))
            rec.append(p)
            return p
        out = asyncio.run(L.run_with_ladder(factory, t, MISSING, D.no_settle,
                                            config=cfg(probe_canary=True, tier1=False, tier3=False), store=st.s,
                                            result_wait_s=0.0))
        c = out["ladder"]["evidence"]["canary"]
        assert c["refusal"]["code"] == "not_read_only" and c["probe"]["verdict"] == "path_broken"
        assert c["verdict"] == "drift" and out["ladder"]["outcome"] != "input"
        assert all(p.clicked == [] for p in rec) and rec[1].typed == []       # rec[0] is tier 0's own run
    finally:
        st.close()


class Flaky(Shop):
    """A shop whose FIRST result page is served without its results (a transient render failure)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.n = 0

    def route(self, url):
        if "/search" in url:
            self.n += 1
            if self.n == 1:
                return D.Recorded(url, self.start()), url
        return super().route(url)


class SameDemo:
    """A re-demonstration that records the same demonstration again (the recompile is byte-identical)."""

    def __init__(self, value):
        self.value = value

    def __call__(self, goal, hint):
        return {"trace": trace(Shop(), self.value), "calls": 2, "usd": 0.0}


def test_loop_guard_byte_identical_repair_is_no_drift():
    tool = compiled(V)
    for guard in (False, True):
        st = Store(tool)
        try:
            assert ladder_run(Shop(), tool, V, st.s)["status"] == "ok"
            site = Flaky()
            out = asyncio.run(L.run_with_ladder(lambda: D.DomPage(site), tool, V, D.no_settle,
                                                config=L.LadderConfig(enabled=True, tier1=False, tier3=True,
                                                                      identical_no_drift=guard),
                                                store=st.s, demonstrator=SameDemo(V), result_wait_s=0.0))
            lad = out["ladder"]
            if not guard:
                # today: the identical recompile is written as revision 1 and counts against the rewrite cap
                assert lad["outcome"] == "repaired" and len(st.s.revisions()) == 2 and st.s.rewrites_within(7) == 1
            else:
                assert lad["outcome"] == "no_drift" and lad["tier"] == 3 and out["status"] == "ok", lad
                assert len(st.s.revisions()) == 1 and st.s.rewrites_within(7) == 0 and "NO DRIFT" in lad["line"]
                assert lad["state"] == "healthy"
        finally:
            st.close()


def test_loop_guard_stale_after_a_repair():
    tool = compiled(V)
    clock = Clock()
    st = Store(tool, clock=clock)
    c = L.LadderConfig(enabled=True, stale_guard=True, max_rewrites=5)
    try:
        assert ladder_run(Shop(), tool, V, st.s, config=c)["status"] == "ok"
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=c)
        assert out["ladder"]["outcome"] == "repaired" and st.s.active_rev() == 1
        # 5 minutes later the repaired revision fails (the search parameter renamed): STALE, no repair attempt
        clock.advance(minutes=5)
        out = ladder_run(Shop(cls=RENAMED, qkey="suchbegriff"), tool, V, st.s, config=c)
        lad = out["ladder"]
        assert lad["outcome"] == "needs_human" and lad.get("stale") is True and "STALE" in lad["why"], lad
        assert lad["evidence"]["attempts"] == [] and "STALE" in lad["line"] and lad["state"] == "needs_human"
        # still stale 20 minutes later (stale for 30 minutes)
        clock.advance(minutes=20)
        assert ladder_run(Shop(cls=RENAMED, qkey="suchbegriff"), tool, V, st.s, config=c)["ladder"].get("stale")
        # after the 30 minutes a repair is attempted (and accepted) again
        clock.advance(minutes=11)
        out = ladder_run(Shop(cls=RENAMED, qkey="suchbegriff"), tool, V, st.s, config=c)
        assert not out["ladder"].get("stale") and out["ladder"]["outcome"] == "repaired", out["ladder"]
        # a failure MORE than 10 minutes after an accepted repair is not stale
        clock.advance(minutes=11)
        out = ladder_run(Shop(qkey="sb"), tool, V, st.s, config=c)
        assert not out["ladder"].get("stale") and any(a.get("tier") == 1 for a in out["ladder"]["evidence"]["attempts"])
    finally:
        st.close()


if __name__ == "__main__":
    test_off_by_default()
    test_canary_says_input()
    test_canary_says_drift_and_the_ladder_proceeds()
    test_no_lkg_is_the_ladder_as_today()
    test_the_cache_and_traffic_booking()
    test_only_read_only_tools_get_a_rerun_canary()
    test_a_typed_search_projects_every_step_its_replay_may_take()
    test_the_probe_never_triggers_the_first_irreversible_step()
    test_loop_guard_byte_identical_repair_is_no_drift()
    test_loop_guard_stale_after_a_repair()
    print("test_repair_canary: 10 ok")
