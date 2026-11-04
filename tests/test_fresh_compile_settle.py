"""Compiling on unseen sites (settling, replays, titles, corrections), offline. Pinned:
* ONE settle rule (``typed_readiness.decide``) for every browser path: document complete, the DOM quiet and no XHR /
  fetch in flight, a long poll ended by a stable DOM, bounded; ``web_arch_run.settle_decision`` is the same rule; the
  product's agent, its proof replays and the MCP route's replays wait with ``PageSettle`` and keep each wait's report;
* a replay keeps the final page's DOM beside its text, and its settle reports;
* the result wait goes on (bounded) while the page's own scripts or data requests are still in flight
  (www.drako.it: its list is fetched by its scripts);
* the run's site cache is on for the product's agent / compile page factory (a proof replay renders with the scripts
  the run already fetched);
* a search whose form sends to another site and leaves the page unchanged is the typed stop ``off_site_search``
  (www.universetoday.com: a form to duckduckgo.com in a new tab);
* a result link that wraps its whole card is titled by its heading or its ``itemprop=name`` element, never by the
  card's text (www.fotointern.ch: heading + teaser; www.lampesdirect.fr: name + price + "Hors Taxe"): in the agent's
  answer, in a tool compiled with the rule (``region["link_names"]``) and on its replays; an older tool reads as before;
* a lookup that follows a menu or category link before it searched, on a page with a search box, gets ONE corrective
  observation (no step used; www.holzprofi.com); a lookup's done without result rows on a page that lists links gets
  the page's links ONCE (www.crescendo.de: {"search", "url"}); a flow's clicks are never corrected; the prompt says to
  search first.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_fresh_compile_settle.py
"""
import asyncio
import html
import pathlib
import sys
import tempfile
import urllib.parse

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as DT  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import site_canon as SC  # noqa: E402
from anyuse.execution import typed_readiness as TR  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeSettlePage:
    """DOM mutating until ``mutate_until`` s; ``inflight`` XHRs that never answer (a long poll)."""

    def __init__(self, clock, mutate_until=0.0, inflight=0):
        self.clock, self.mutate_until, self.t0 = clock, mutate_until, clock()
        self._events = [{"method": "Network.requestWillBeSent",
                         "params": {"requestId": f"r{i}", "type": "XHR", "request": {"url": "x"}}}
                        for i in range(inflight)]

    async def js(self, expr, read_only=False):
        el = self.clock() - self.t0
        return {"quiet_ms": 0 if el < self.mutate_until else (el - self.mutate_until) * 1000, "mutations": 3,
                "ready": "complete"}

    async def settle(self, s):
        self.clock.t += s


# ------------------------------------------------------------------------------------------------- one settle rule
def test_one_settle_rule_for_every_path():
    ok = {"ready": "complete", "quiet_ms": 600}
    assert TR.decide(ok, 0.5, ceiling_s=5) == "quiet"
    assert TR.decide(ok, 0.5, ceiling_s=5, inflight=1) == "wait"                       # a data request still open
    assert TR.decide({"ready": "complete", "quiet_ms": 1300}, 2, ceiling_s=5, inflight=1, stable_ms=1200) == \
        "dom_stable"                                                                    # a long poll: a stable DOM
    assert TR.decide({"ready": "complete", "quiet_ms": 1300}, 2, ceiling_s=5, inflight=1, inflight_value=1,
                     stable_ms=1200) == "wait"                                          # the value's own request
    assert TR.decide(None, 5.0, ceiling_s=5) == "ceiling"
    # the benches' bounded settle is the same rule
    assert RR.settle_decision(ok, 1.0, 0) == "idle" and RR.settle_decision(ok, 1.0, 2) == "wait"
    assert RR.settle_decision({"ready": "complete", "quiet_ms": 1300}, 2.0, 2) == "dom_stable"


def test_page_settle_waits_and_keeps_its_report():
    clock = Clock()
    s = TR.PageSettle()
    asyncio.run(TR.settle_page(FakeSettlePage(clock, mutate_until=1.0), trail=s.trail, clock=clock))
    assert s.trail[-1]["ended"] == "quiet" and 1.0 <= s.trail[-1]["waited_s"] < 2.0
    clock2 = Clock()
    asyncio.run(TR.settle_page(FakeSettlePage(clock2, mutate_until=99, inflight=1), trail=s.trail, clock=clock2))
    assert s.trail[-1]["ended"] == "ceiling" and s.trail[-1]["waited_s"] <= TR.PAGE_SETTLE_CEILING_S + 0.2
    # an offline (recorded) page is settled at once
    assert asyncio.run(D.DomPage(D.DomSite([])).js(TR.SETTLE_PROBE_JS))["quiet_ms"] > 1000


# ------------------------------------------------------------------------------------------------- the replay
BASE = "https://www.shop.test"
ITEMS = [("p1", "Red Kettle"), ("p2", "Blue Kettle"), ("p3", "Kettle Descaler")]


class Shop(D.DomSite):
    def __init__(self):
        super().__init__([])

    def page(self, q):
        rows = "".join(f'<li class="product"><h3><a href="/{s}">{html.escape(t)}</a></h3><span>12 €</span></li>'
                       for s, t in ITEMS if any(w in t.lower() for w in q.lower().split()))
        return (f'<html><body><header><form action="/search" method="get"><input type="search" name="q" '
                f'value="{html.escape(q)}"></form></header><main><h1>Results</h1><ul class="r">{rows}</ul></main>'
                f'</body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path == "/search":
            return D.Recorded(url, self.page(q.get("q", ""))), url
        return D.Recorded(url, self.page("")), url


def compiled_tool():
    site = Shop()
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", "www.shop.test")
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if e.href and "/p" in e.href]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="shop").run("search for {q}",
                                                                                        {"q": "Kettle"})
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    return site, c["tool"]


def test_a_replay_keeps_the_final_dom_and_its_settle_reports():
    site, tool = compiled_tool()
    settle = TR.PageSettle()
    out = asyncio.run(DT.run_tool(D.DomPage(site), tool, "Kettle", settle, result_wait_s=0.0))
    assert out["status"] == "ok"
    assert out["pages"][-1]["dom"].startswith("<html") and "Red Kettle" in out["pages"][-1]["dom"]
    assert out["settles"] and all(s["ended"] == "quiet" for s in out["settles"])


def test_the_result_wait_goes_on_while_the_pages_scripts_are_in_flight(monkeypatch):
    clock = Clock()

    class Page:
        _events = [{"method": "Network.requestWillBeSent", "params": {"requestId": "s1", "type": "Script",
                                                                       "request": {"url": "https://x/list.js"}}}]

        async def settle(self, s):
            clock.t += s

    async def read(page):
        return {"text": "x", "main_text": "x"}
    monkeypatch.setattr(RR, "read_page", read)
    items = [{"title": "Red Kettle", "url": BASE + "/p1", "ctx": ""}]
    monkeypatch.setattr(C, "region_items", lambda r, region: items if clock.t >= 11.0 else [])
    tool = {"region": {"kind": "cluster"}, "workflow": "site_search"}
    trail = []
    got = asyncio.run(DT.wait_results(Page(), tool, "Kettle", 8.0, clock=clock, trail=trail))
    assert got is not None and 11.0 <= got <= 8.0 + DT.RESULT_INFLIGHT_GRACE_S
    assert any(x[1] == "inflight" for x in trail)
    Page._events = []                                    # nothing in flight: the ceiling ends it as before
    clock.t = 0.0
    assert asyncio.run(DT.wait_results(Page(), tool, "Kettle", 8.0, clock=clock, trail=[])) is None


def test_the_products_run_keeps_a_site_cache(monkeypatch):
    from anyuse.execution import agent_browser as AB
    from anyuse.execution import harness_product as HP
    monkeypatch.setenv("ANYUSE_LEARN_ENDPOINT", "http://192.168.64.9:9222")
    monkeypatch.setattr(AB, "assert_container_endpoint", lambda ep, opener=None: {"Browser": "fake"})
    f = HP.guarded_page_factory(start_url="https://www.shop.test/", site_cap=50)
    assert f.budget.cache_enabled and f.budget.site_cache


# ------------------------------------------------------------------------------------------------- off-site search
class OffSite(D.DomSite):
    """A search box whose form sends to a search engine on another site, in a new tab (the page stays)."""

    def __init__(self):
        super().__init__([])

    def route(self, url):
        return D.Recorded(url, '<html><body><header><form action="https://search.other.test/" method="get" '
                               'target="_blank"><input type="hidden" name="sites" value="news.test"><input '
                               'type="text" name="q" placeholder="Search..."></form></header><main><article><h2>'
                               '<a href="/2026/mars-origins">Earth and Mars had different origins</a></h2>'
                               '</article></main></body></html>'), url


def test_a_search_form_to_another_site_is_a_typed_stop():
    assert SC.search_form_target('<form action="https://duckduckgo.com/"><input name="q"></form>',
                                 "https://www.news.test/", "q") == "https://duckduckgo.com/"
    assert SC.leaves_site("https://duckduckgo.com/", "https://www.news.test/")
    assert not SC.leaves_site("https://search.news.test/", "https://www.news.test/")
    assert SC.search_form_target('<form action="/search"><input name="q"></form>', "https://www.news.test/", "q") == \
        "https://www.news.test/search"
    site = OffSite()
    tools = HT.BrowserTools(D.DomPage(site), "https://www.news.test/", "www.news.test")
    pol = P.ScriptedPolicy([T.Search(0, "Mars"), lambda t, o, h: T.Done({"results": [{"element": 1}]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="off").run("search for {q}",
                                                                                       {"q": "Mars"})
    assert tr.stop == T.Stop.OFF_SITE_SEARCH and "search.other.test" in tr.stop_reason


# ------------------------------------------------------------------------------------------------- titles
NEWS = [("2026/09/24/nikon-z5iic", "Nikon Z5IIC: starke, handliche Vollformat-Systemkamera",
         "Mit der einsteigerfreundlichen Kamera bringt Nikon ein Vollformatmodell."),
        ("2026/09/25/ttartisan-85", "Lichtstarkes Portraet-Tele fuer wenig Geld",
         "Zu spiegellosen Nikon-Kameras hat der Hersteller eine neue Serie lanciert."),
        ("2026/09/18/think-tank", "Think Tank FocusPoint Crossbody Schultertasche",
         "Die Tasche fasst eine Nikon mit Objektiv.")]


class TeaserSite(D.DomSite):
    """Each result is ONE link around the card: a heading, the teaser, the author and date (www.fotointern.ch)."""

    def __init__(self):
        super().__init__([])

    def page(self, q):
        cards = "".join(f'<article class="teaser"><a href="/archiv/{p}/" class="teaser-link"><div class="thumb"><img '
                        f'src="/i.jpg" alt=""></div><div class="wrap"><header><h2 class="teaser-title">'
                        f'{html.escape(t)}</h2></header><p class="excerpt">{html.escape(x)}</p><div class="meta">'
                        f'<span>Markus Zitt</span>, <span>24. September 2026</span></div></div></a></article>'
                        for p, t, x in NEWS if q and q.lower() in (t + x).lower())
        return (f'<html><body><header><form action="/" method="get"><input type="search" name="s" '
                f'value="{html.escape(q)}"></form></header><main><h1>Suche</h1>{cards}</main></body></html>')

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        return D.Recorded(url, self.page(q.get("s", ""))), url


def test_a_card_wrapping_link_is_titled_by_its_heading():
    from anyuse.execution import demo_cards as K
    n = K.parse('<a href="/x"><h2>The title</h2><p>A long teaser line</p></a>')[0]
    a = next(x for x in K.walk(n) if x.tag == "a")
    assert K.link_name(a) == "The title"
    b = next(x for x in K.walk(K.parse('<a href="/y"><p itemprop="name">Kit de suspension</p><span>0,72 € Hors '
                                       'Taxe</span></a>')[0]) if x.tag == "a")
    assert K.link_name(b) == "Kit de suspension"
    assert K.link_name(next(x for x in K.walk(K.parse('<h3><a href="/z">Only a title</a></h3>')[0])
                            if x.tag == "a")) == ""                      # a plain title link names itself
    site = TeaserSite()
    tools = HT.BrowserTools(D.DomPage(site), "https://www.foto.test/", "www.foto.test")
    pol = P.ScriptedPolicy([T.Search(0, "Nikon"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if e.href and "/archiv/" in e.href]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="teaser").run("search for {q}",
                                                                                          {"q": "Nikon"})
    assert [r["title"] for r in tr.result["results"]] == [t for _p, t, _x in NEWS]
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    assert c["tool"]["region"].get("link_names") >= 1
    out = HC.replay_web(c["tool"], site, "Nikon")
    assert out["status"] == "ok"
    assert [r["title"] for r in out["output"]["results"]] == [t for _p, t, _x in NEWS]
    assert HC.independent_verdict(c["tool"], out)["verdict"] == "accept"
    # an older tool (compiled before the rule) reads its titles as it did
    old = dict(c["tool"], region={k: v for k, v in c["tool"]["region"].items() if k != "link_names"})
    out_old = HC.replay_web(old, site, "Nikon")
    t0 = out_old["output"]["results"][0]["title"]
    assert out_old["status"] == "ok" and t0.startswith(NEWS[0][1]) and len(t0) > len(NEWS[0][1])


# ------------------------------------------------------------------------------------------------- corrections
def test_a_lookup_that_browses_a_category_first_is_told_to_search():
    assert "search box (the search action) first" in P.SYSTEM
    site = Shop()
    menu = Shop.page

    def page_with_menu(self, q):
        return menu(self, q).replace("<main>", '<main><nav class="cats"><a href="/kettles">Kettles</a> '
                                               '<a href="/teapots">Teapots</a></nav>')
    site.page = page_with_menu.__get__(site, Shop)
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", "www.shop.test")

    def click_category(t, o, h):
        return T.Click(next(e.index for e in o.elements if e.text == "Teapots"))
    pol = P.ScriptedPolicy([click_category, T.Search(0, "Kettle"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if e.href and "/p" in e.href]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="cat").run("search for {q}",
                                                                                       {"q": "Kettle"})
    assert tr.stop == T.Stop.DONE and [c["why"] for c in tr.corrections] == ["browsing before searching"]
    assert pol.calls and len(tr.steps) == 2                              # the category click used no step
    assert "Kettle" in tr.result["results"][0]["title"]
    # a flow (credentials) is never corrected for its clicks
    tools2 = HT.BrowserTools(D.DomPage(site), BASE + "/", "www.shop.test")
    tr2 = H.Harness(HT.Toolbox(browser=tools2), P.ScriptedPolicy([click_category, T.Fail("x")]), H.HarnessConfig(),
                    run_id="flow").run("log in and open {username}", {"username": "me", "password": "x"})
    assert not tr2.corrections


def test_a_lookups_done_without_rows_gets_the_pages_links_once():
    site = Shop()
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", "www.shop.test")
    seen = []

    def rows_after_note(t, o, h):
        seen.append(o.note)
        return T.Done({"results": [{"element": e.index} for e in o.elements if e.href and "/p" in e.href]})
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.Done({"search": "Kettle", "url": BASE + "/search?q=Kettle"}),
                            rows_after_note])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="rows").run("search for {q}",
                                                                                        {"q": "Kettle"})
    assert tr.stop == T.Stop.DONE and len(tr.result["results"]) == 3
    assert [c["why"] for c in tr.corrections] == ["done without result rows"]
    assert "no result rows" in seen[0] and "Red Kettle" in seen[0]
    # given once: a second rowless done ends the run as before
    pol2 = P.ScriptedPolicy([T.Search(0, "Kettle"), T.Done({"search": "Kettle"}), T.Done({"search": "Kettle"})])
    tr2 = H.Harness(HT.Toolbox(browser=HT.BrowserTools(D.DomPage(site), BASE + "/", "www.shop.test")), pol2,
                    H.HarnessConfig(), run_id="rows2").run("search for {q}", {"q": "Kettle"})
    assert tr2.stop == T.Stop.DONE and tr2.result == {"search": "Kettle"} and len(tr2.corrections) == 1
    # a product answer and a no_results answer are never corrected
    from anyuse.execution import agent_harness as AH
    assert not AH.rowless_answer({"name": "Red Kettle", "price": "12"}, "price of {q}")
    assert not AH.rowless_answer({"no_results": True}) and AH.rowless_answer({"search": "x", "url": "u"}, "search x")



def test_a_model_driven_runs_question_about_its_task_is_asked_in_the_runs_own_scope(monkeypatch):
    from anyuse.execution import model_check as MC
    site = Shop()
    scopes, real = [], MC.check

    def check(question, *a, **kw):
        # the scope the task's question is asked in (the asker itself runs on the check's own thread)
        if question.kind == HC._question().kind:
            scopes.append(MC.current_scope())
        return real(question, *a, **kw)
    monkeypatch.setattr(MC, "check", check)

    def no(instructions, state, obs):
        return 0.05                                            # a list answer: the task asks for no item's attribute

    def run(cost):
        rows = lambda t, o, h: T.Done({"results": [{"element": e.index} for e in o.elements
                                                    if e.href and "/p" in e.href]})
        pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.Done({"search": "Kettle", "url": BASE + "/search?q=Kettle"}),
                                rows], cost_per_call=cost)
        tools = HT.BrowserTools(D.DomPage(site), BASE + "/", "www.shop.test")
        return H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id=f"rows-{cost}").run(
            "search for {q}", {"q": "Kettle"})
    with MC.using(no):
        tr = run(0.001)                                      # a policy that stands in for a model
        assert scopes == ["agent"], scopes
        assert [c["why"] for c in tr.corrections] == ["done without result rows"]
        scopes.clear()
        tr = run(0.0)                                        # no model drives the run: the lexicon decides
        assert scopes == [] and [c["why"] for c in tr.corrections] == ["done without result rows"]
    assert MC.current_scope() == ""

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
