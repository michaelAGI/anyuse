"""A demonstration's typed search whose Enter does nothing, offline on recorded
pages (``dom_page``; no browser, no network, no model): the input's own GET form,
then the site's own search URL (its schema.org SearchAction, the model's pick
among several only inside an open model-check scope, else score order), and the
typed stop ``no_site_search`` when the search lands back on the unchanged start
page. A web tool before version 14 replays without the form fallback.
"""
import asyncio
import html
import json
import time
import urllib.parse

from anyuse.execution import demo_compile as C
from anyuse.execution import demo_policy as DPOL
from anyuse.execution import demo_tool as T
from anyuse.execution import dom_page as D
from anyuse.execution import model_check as MC

BASE = "https://dead.test"
ITEMS = ["Red fox plush", "Red fox mug", "Red fox poster"]


class DeadEnter(D.DomSite):
    """Typing and Enter do nothing on this site (its script swallows Enter)."""

    def __init__(self, start):
        super().__init__([])
        self.start = start

    def search(self, page, url, inp, value):
        return None

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path in ("", "/"):
            return D.Recorded(url, self.start), url
        term = q.get("term") or q.get("q") or ""
        if sp.path in ("/find", "/search") and term:
            rows = "".join(f'<li class="card"><a href="/p/{i}">{html.escape(t)}</a></li>'
                           for i, t in enumerate(ITEMS) if term.split()[0].lower() in t.lower())
            return D.Recorded(url, f"<html><body><main><h1>Results</h1><ul>{rows}</ul></main>"
                                   f"</body></html>"), url
        return D.Recorded(url, D.NOT_FOUND, {}, 404), url


FORM = ('<html><body><header><form action="/find" method="get"><input type="search" name="term">'
        '</form></header><main><h1>Welcome</h1><a href="/about">About us</a></main></body></html>')
ACTION = ('<html><head><script type="application/ld+json">' + json.dumps(
    {"@context": "https://schema.org", "@type": "WebSite", "url": BASE,
     "potentialAction": {"@type": "SearchAction", "target": BASE + "/find?term={search_term_string}",
                         "query-input": "required name=search_term_string"}}) +
          '</script></head><body><div><input type="search" name="q" placeholder="Search"></div>'
          '<main><h1>Welcome</h1></main></body></html>')
TWO_LINKS = ('<html><body><div><input type="search" name="q" placeholder="Search"></div><main><h1>Welcome</h1>'
             '<a href="/search?q=sale">Sale</a> <a href="/find?term=new">New in</a></main></body></html>')
HOME = ('<html><body><header><form action="/" method="get"><input type="search" name="term"></form></header>'
        '<main><h1>Welcome</h1><a href="/about">About us</a></main></body></html>')


def search(start, chooser=False):
    site = DeadEnter(start)
    page = D.DomPage(site)

    async def go():
        await page.goto(BASE + "/")
        mapping = {0: {"src": "input", "input": {"index": 0}}}
        return await DPOL.execute(page, {"action": "search", "element": 0, "text": "red fox"},
                                  mapping, "dead.test", D.no_settle, chooser=chooser)
    DPOL.SEARCH_WAIT_S, old = 0.0, DPOL.SEARCH_WAIT_S
    try:
        return asyncio.run(go()), page
    finally:
        DPOL.SEARCH_WAIT_S = old


def test_the_inputs_own_get_form_is_submitted():
    out, page = search(FORM)
    assert out["ok"] and out["how"] == "form_url", out
    assert out["url_after"] == BASE + "/find?term=red+fox"
    assert out["documents"] == [BASE + "/find?term=red+fox"]


def test_without_a_form_the_pages_search_action_is_used():
    out, page = search(ACTION)
    assert out["ok"] and out["how"] == "search_url", out
    assert out["search_url"] == BASE + "/find?term={q}"
    assert out["search_url_how"] == "schema.org SearchAction"
    assert page.gotos[-1] == BASE + "/find?term=red+fox"


def test_several_search_urls_go_to_the_model_inside_a_scope():
    seen = []

    def fake(instructions, state, obs):
        seen.append(state.splitlines()[-1])
        return 0.97 if state.endswith(">> " + BASE + "/find?term={q}") else 0.03
    with MC.scope("agent"), MC.using(fake):
        out, _page = search(TWO_LINKS, chooser=True)
    assert out["search_url"] == BASE + "/find?term={q}", out
    assert out["search_url_how"] == "the search URL the model chose"
    assert len(seen) == 2


def test_the_model_is_asked_off_the_event_loop():
    # a check blocks its thread until it answers; the page's connection lives on the loop
    from anyuse.execution import web_arch as W
    loops = []
    real = W.site_search_url

    def spy(*a, **kw):
        try:
            loops.append(asyncio.get_running_loop())
        except RuntimeError:
            loops.append(None)
        return real(*a, **kw)
    W.site_search_url = spy
    try:
        with MC.scope("agent"), MC.using(lambda i, s, o: 0.03):
            out, _page = search(TWO_LINKS, chooser=True)
    finally:
        W.site_search_url = real
    assert out["how"] == "search_url" and loops == [None], (out, loops)


def test_without_a_model_the_first_by_score_is_used():
    out, _page = search(TWO_LINKS, chooser=True)
    assert out["search_url"] == BASE + "/search?q={q}", out
    calls = []
    with MC.scope("agent"), MC.using(lambda i, s, o: calls.append(1) or 0.5):
        out, _page = search(TWO_LINKS, chooser=True)
    assert out["search_url"] == BASE + "/search?q={q}" and len(calls) == 2


def test_a_search_that_lands_on_the_unchanged_home_page_is_a_typed_stop():
    out, _page = search(HOME)
    assert not out["ok"] and out["why"].startswith("no_site_search"), out
    assert out["stop"] == "no_site_search"


def test_an_overlay_that_adds_text_below_a_long_page_is_not_a_typed_stop():
    # the search draws its answer in place, as plain text appended after a long page
    long_home = HOME.replace("<main><h1>Welcome</h1>",
                             "<main><h1>Welcome</h1><p>" + "Our shop story. " * 200 + "</p>")

    class Overlay(DeadEnter):
        typed = False

        def search(self, page, url, inp, value):
            self.typed = True
            return BASE + "/"

        def route(self, url):
            if self.typed and urllib.parse.urlsplit(url).path in ("", "/"):
                return D.Recorded(url, long_home.replace(
                    "</main>", "</main><div class=overlay>3 matches for red fox</div>")), url
            return super().route(url)
    page = D.DomPage(Overlay(long_home))

    async def go():
        await page.goto(BASE + "/")
        return await DPOL.execute(page, {"action": "search", "element": 0, "text": "red fox"},
                                  {0: {"src": "input", "input": {"index": 0}}}, "dead.test", D.no_settle)
    DPOL.SEARCH_WAIT_S, old = 0.0, DPOL.SEARCH_WAIT_S
    try:
        out = asyncio.run(go())
    finally:
        DPOL.SEARCH_WAIT_S = old
    assert out["ok"], out


def test_the_agent_harness_stops_with_no_site_search():
    from anyuse.execution import agent_harness as H
    from anyuse.execution import harness_policies as P
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import harness_types as T
    page = D.DomPage(DeadEnter(HOME))
    asyncio.run(page.goto(BASE + "/"))
    tools = HT.BrowserTools(page, BASE + "/", "dead.test")
    DPOL.SEARCH_WAIT_S, old = 0.0, DPOL.SEARCH_WAIT_S
    try:
        tr = H.Harness(HT.Toolbox(browser=tools), P.ScriptedPolicy([T.Search(0, "red fox"), T.Fail("x")]),
                       H.HarnessConfig(), run_id="home").run("search for {q}", {"q": "red fox"})
    finally:
        DPOL.SEARCH_WAIT_S = old
    assert tr.stop == T.Stop.NO_SITE_SEARCH and "no_site_search" in tr.stop_reason, (tr.stop, tr.stop_reason)


def test_a_version_13_tool_replays_without_the_form_fallback():
    site = DeadEnter(FORM)

    def replay(version):
        page = D.DomPage(site)
        tool = {"version": version, "site": "dead.test", "start_url": BASE + "/",
                "search": {"mode": "typed", "input": {"index": 0, "name": "term"},
                           "fallback_templates": [], "start_url": BASE + "/"}}
        out = {"pages": []}
        res = asyncio.run(T._search(page, tool, "red fox", D.no_settle, out, time.monotonic()))
        return res, out, page
    res13, out13, page13 = replay(13)
    assert "done" in res13 and out13["status"] == "failed", out13
    assert page13.gotos == [BASE + "/"]
    res14, out14, page14 = replay(14)
    assert "done" not in res14 and out14["search_via"] == "typed"
    assert page14.gotos == [BASE + "/", BASE + "/find?term=red+fox"]
    assert page14.submit_chooser is False           # a replay never puts a control to the model


def test_the_compiled_tool_keeps_the_submit_label_from_version_14():
    step = {"outcome": {"input": {"index": 2, "name": "q"},
                        "submit": {"how": "the control the model chose", "label": "Los"}}}
    assert C._input_descriptor(step) == {"index": 2, "name": "q", "submit_label": "Los"}
    assert C._input_descriptor(step, 13) == {"index": 2, "name": "q"}
    assert C._input_descriptor({"outcome": {"input": {"index": 1}}}) == {"index": 1}


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
