"""Ladder-only: PACING and RATE LIMITS (``execution/repair_pace.py``,
``LadderConfig(pacing=True)``, ``OracleChecker(rate_limit=True)``), offline on fakes built from the live run's saved
www.woodworker.de pages (no browser, no network, no model).

What must hold:

1. ``rate_limit_why``: the site's "Sie dürfen nur alle 10 Sekunden eine Suche starten." page is a rate-limit page
   (wait 10 s), so is an HTTP 429 and a short "too many requests" page in other languages; the same site's "Ihre
   Suche erbrachte keine Ergebnisse" page, a long article that mentions "too many requests" and a result page are not;
2. the ``Pacer``: a per-host minimum interval (10 s, or a longer robots.txt crawl-delay) between openings; a rate
   limit's stated wait; a stated wait over the cap is not waited for;
3. through the ladder (the live case): the version-12 upgrade's shadow run met the rate-limit page - with pacing OFF
   the gate refuses the candidate as WORSE (exactly as live); with pacing ON the run is retried once after the wait
   and the upgrade is accepted; a rate limit that persists DEFERS the decision (nothing logged, the run is
   ``rate_limited``, the next run tries again) - never a refusal;
4. the checker: ``OracleChecker(rate_limit=True)`` reads a 429 / rate-limit render as ``undeterminable`` with
   ``rate_limited`` (off by default: as before); the paced ladder asks it once more after the wait.
"""
import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_oracle as O  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import repair_check as RC  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import repair_pace as RP  # noqa: E402
from anyuse.execution import verdict_store as VS  # noqa: E402

# the live run's saved www.woodworker.de pages (the shadow run and the checker)
LIMIT = "Folgende Fehler traten auf\nSie dürfen nur alle 10 Sekunden eine Suche starten.\nZurück zur vorhergehenden Seite"
EMPTY = "Suchergebnisse:\nIhre Suche erbrachte keine Ergebnisse.\nZurück zur vorhergehenden Seite"
RESULTS = ("News\nForum\nLinks\nBücher\nSuchergebnis\nZukunft des Handwerks: Messe 2026\nDie Zukunft der Holztechnik\n"
           "mehr News\n")
ARTICLE = ("Warum Server mit 429 antworten\n" + "Ein Server antwortet mit 'Too many requests', wenn ... " * 80)


def test_rate_limit_pages():
    why, wait = RP.rate_limit_why(LIMIT)
    assert why and wait == 10.0, (why, wait)
    assert RP.rate_limit_why("", "HTTP 429 on the document https://www.woodworker.de/")[0]
    assert RP.rate_limit_why("Too Many Requests\nPlease try again in 30 seconds.")[1] == 30.0
    assert RP.rate_limit_why("Trop de requêtes. Veuillez patienter 20 secondes avant une nouvelle recherche.")[1] == 20.0
    assert RP.rate_limit_why("Zu viele Anfragen")[0]
    assert RP.rate_limit_why("Wacht 15 seconden voordat u opnieuw zoekt.")[1] == 15.0
    for text in (EMPTY, RESULTS, ARTICLE, "Lieferzeit: 1 bis 3 Werktage\nIn 10 Sekunden erklärt: der Hobel"):
        assert RP.rate_limit_why(text) == ("", 0.0), text[:60]
    assert RP.rate_limit_why(EMPTY, "HTTP 403 on the document x")[0] == ""      # a 403 is no rate limit
    # pacing and the checker's rate-limit reading are ON by default (the ladder stays off by default)
    assert L.LadderConfig().pacing is True and RP.PACING_BY_DEFAULT is True and L.LadderConfig().enabled is False
    assert RC.RATE_LIMIT_CHECK_BY_DEFAULT is True


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(round(s, 2))
        self.t += s


def test_the_pacer():
    c = Clock()
    p = RP.Pacer(clock=c, sleep=c.sleep, crawl_delay=lambda h: 15.0 if h == "slow.test" else None)
    asyncio.run(p.wait("a.test"))
    c.t += 4
    assert asyncio.run(p.wait("a.test")) == 6.0                 # 10 s since the last opening
    asyncio.run(p.wait("b.test"))                                # another host: no wait
    asyncio.run(p.wait("slow.test"))
    assert asyncio.run(p.wait("slow.test")) == 15.0              # the crawl-delay is longer
    assert p.backoff("a.test", 30.0) == 30.0
    assert asyncio.run(p.wait("a.test")) == 30.0                 # the stated wait
    assert p.backoff("a.test", 600.0) == -1.0                    # over the cap: not waited for


SITE = "www.woodworker.test"
BASE = f"https://{SITE}"
GOOD = [{"title": "Zukunft des Handwerks: Messe 2026", "url": f"{BASE}/news/1"},
        {"title": "Die Zukunft der Holztechnik", "url": f"{BASE}/news/2"}]
SIDE = [{"title": "Neuer digitaler Branchentreffpunkt", "url": f"{BASE}/news/9"}]
NEW = [{"title": "Branchentreffpunkt Holz 2026", "url": f"{BASE}/news/5"}]


def _tool(version):
    return {"workflow": "site_search", "site": SITE, "version": version, "start_url": BASE + "/",
            "search": {"mode": "url", "template": BASE + "/suche?q={q}"}, "region": {"cards": [{"kind": "title"}]},
            "checks": {}, "params": [], "hosts": [SITE]}


class Page:
    def __init__(self):
        self._text = ""

    async def text(self):
        return self._text

    async def dom(self):
        return "<html><body></body></html>"

    async def url(self):
        return BASE + "/"


def _ok(value, res, text=RESULTS):
    return {"value": value, "status": "ok", "why": "query: items", "model_calls": 0, "output": {"results": res},
            "checks": {"result_page": {"n_items": len(res)}}, "pages": [{"title": f"Suche {value}", "h1": [value]}]}


def _install(limits):
    """run_tool answering the live run's shapes; ``limits``: how many times the CANDIDATE's run of "Zukunft" meets the
    rate-limit page first."""
    left = {"n": limits}
    saved = T.run_tool

    async def fake(page, tool, value, settle, **kw):
        if value == "Zukunft":
            if tool["version"] >= 12 and left["n"] > 0:
                left["n"] -= 1
                page._text = LIMIT
                return {"value": value, "status": "failed", "why": "membership: no item of the compiled region",
                        "output": None, "model_calls": 0, "checks": {}, "pages": [{"title": "", "h1": []}]}
            page._text = RESULTS
            return _ok(value, GOOD)
        if value == "Nixpelbrak Brakomnix":
            page._text = EMPTY
            return {"value": value, "status": "no_results", "why": "the page says no results", "output": None,
                    "model_calls": 0, "checks": {}, "pages": [{"title": "", "h1": []}]}
        page._text = RESULTS
        return _ok(value, NEW if tool["version"] >= 12 else SIDE)
    T.run_tool = fake
    return lambda: setattr(T, "run_tool", saved)


def _world(limits, pacing):
    d = tempfile.mkdtemp(prefix="anyuse-pace-test-")
    t9 = _tool(9)
    st = L.RepairStore(os.path.join(d, "store"), t9)
    restore = _install(limits)
    try:
        for v in ("Zukunft", "Nixpelbrak Brakomnix"):
            out = asyncio.run(T.run_tool(Page(), t9, v, None))
            st.set_lkg(v, "site_search", out, 0)
        side = asyncio.run(T.run_tool(Page(), t9, "Branchentreffpunkt", None))
    finally:
        restore()
    vs = VS.VerdictStore(os.path.join(d, "verdicts.jsonl"))
    from anyuse.execution import demo_compile as C
    vs.add(tool=C.tool_digest(t9), site=SITE, value="Branchentreffpunkt", status="ok", output=side["output"],
           verdict="wrong", source="audit", ref="r8|delayed")
    c = Clock()
    RP.reset(RP.Pacer(clock=c, sleep=c.sleep))
    agree = lambda t, v, o: {"verdict": "agrees", "why": "fake render lists them"}   # noqa: E731
    cfg = L.LadderConfig(enabled=True, tier1=False, tier3=False, pacing=pacing)
    return d, t9, st, vs, c, agree, cfg


def _go(d, t9, st, vs, cfg, agree, limits):
    restore = _install(limits)
    try:
        return asyncio.run(L.run_with_ladder(Page, t9, "Branchentreffpunkt", None, config=cfg, store=st, checker=agree,
                                             verdicts=vs, upgrade=lambda t: _tool(12) if t["version"] < 12 else None,
                                             result_wait_s=0.0))
    finally:
        restore()


def test_the_upgrade_gate_meets_the_rate_limit():
    # pacing OFF: the live refusal ("worse on 'Zukunft' (usable -> failure)")
    d, t9, st, vs, c, agree, cfg = _world(1, False)
    try:
        out = _go(d, t9, st, vs, cfg, agree, 1)
        assert out["status"] == "known_wrong" and st.active_rev() == 0
        assert "worse on 'Zukunft'" in st.upgrades()[-1]["why"], st.upgrades()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # pacing ON: the rate-limited shadow run is retried once after the stated 10 s; the upgrade is accepted
    d, t9, st, vs, c, agree, cfg = _world(1, True)
    try:
        out = _go(d, t9, st, vs, cfg, agree, 1)
        assert out["status"] == "ok" and out["ladder"]["outcome"] == L.UPGRADED, out.get("ladder")
        assert out["output"]["results"][0]["url"] == NEW[0]["url"] and st.active_rev() == 1
        assert c.slept and all(s >= 10.0 for s in c.slept), c.slept
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # pacing ON, the rate limit persists: the decision is DEFERRED (no log row), the run is rate_limited, the known
    # wrong answer stays withheld; the next run tries the candidate again (and is accepted)
    d, t9, st, vs, c, agree, cfg = _world(2, True)
    try:
        out = _go(d, t9, st, vs, cfg, agree, 2)
        assert out["status"] == "known_wrong" and out["output"] is None, out.get("status")
        assert out["ladder"]["outcome"] == L.RATE_LIMITED and "deferred" in out["ladder"]["why"], out["ladder"]
        assert st.upgrades() == [] and st.active_rev() == 0
        out = _go(d, t9, st, vs, cfg, agree, 0)
        assert out["ladder"]["outcome"] == L.UPGRADED and st.active_rev() == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)
        RP.reset()


def test_the_checker_reads_a_rate_limit():
    saved = O.oracle_search

    async def limited(page, start, value, templates, settle, wait_s=0):
        return {"how": "blocked", "blocked": "HTTP 429 on the document https://www.woodworker.test/suche", "url": ""}
    O.oracle_search = limited
    try:
        out = _ok("Zukunft", GOOD)
        off = asyncio.run(RC.OracleChecker(lambda: Page(), None, rate_limit=False)(_tool(12), "Zukunft", out))
        assert off["verdict"] == "undeterminable" and "rate_limited" not in off
        on = asyncio.run(RC.OracleChecker(lambda: Page(), None)(_tool(12), "Zukunft", out))
        assert on["verdict"] == "undeterminable" and on["rate_limited"] == 0.0, on
    finally:
        O.oracle_search = saved
    # the paced ladder asks a rate-limited checker once more after the wait
    c = Clock()
    RP.reset(RP.Pacer(clock=c, sleep=c.sleep))
    answers = [{"verdict": "undeterminable", "rate_limited": 10.0, "why": "rate limit"}, {"verdict": "agrees", "why": "x"}]
    pc = L._PacedChecker(lambda t, v, o: answers.pop(0))
    try:
        got = asyncio.run(pc(_tool(12), "Zukunft", _ok("Zukunft", GOOD)))
        assert got["verdict"] == "agrees" and got.get("rate_limit_retried") and 10.0 in c.slept, (got, c.slept)
        again = L._PacedChecker(lambda t, v, o: {"verdict": "undeterminable", "rate_limited": 0.0, "why": "429"})
        got = asyncio.run(again(_tool(12), "Zukunft", _ok("Zukunft", GOOD)))
        assert got["verdict"] == "undeterminable" and "after one retry" in got["why"]
    finally:
        RP.reset()


if __name__ == "__main__":
    test_rate_limit_pages()
    test_the_pacer()
    test_the_upgrade_gate_meets_the_rate_limit()
    test_the_checker_reads_a_rate_limit()
    print("test_repair_pace: 4 ok")
