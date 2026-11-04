"""A read-only task is never refused because the search box was not found,
offline (the fake shop of test_web_arch.py; fake planner).

Measured: 9 arch runs refused a read-only task after "no visible search input"
(the box needs the page's scripts; keepsakequilting, truffaut, biltema,
promessedefleurs), and a refusal was accepted as the plan.

What must hold:

1. the site's own search URL patterns are read from the page only (a
   schema.org SearchAction target, a GET search form, a same-site link whose
   query carries a search parameter on a search path); another site's pattern
   and a pattern the page does not show are never used;
2. when typing finds no box (or changes nothing), the engine opens the site's
   search URL with the words; the compiled op records it (``via: url`` with its
   template) and replays with a bound value without typing;
3. with no search URL, the map's navigation by the words (then the picker);
4. a planner that refuses a read-only goal twice gets the fallback plan
   (search the item's words, open it, read it for a question); a goal that
   needs something outside the site keeps its refusal.
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, FakePage, FakePlanner, S, page_for, run  # noqa: E402

SEARCH_LD = json.dumps({"@context": "https://schema.org", "@type": "WebSite", "url": BASE + "/",
                        "potentialAction": {"@type": "SearchAction",
                                            "target": BASE + "/search?q={search_term_string}",
                                            "query-input": "required name=search_term_string"}})


def test_templates_come_from_the_page_only():
    t = W.search_url_templates("", BASE + "/", [SEARCH_LD])
    assert t and t[0]["template"] == BASE + "/search?q={q}" and t[0]["how"] == "schema.org SearchAction"
    other = json.dumps({"@type": "WebSite", "potentialAction": {"@type": "SearchAction",
                                                                "target": "https://elsewhere.example/s?q={x}"}})
    assert W.search_url_templates("", BASE + "/", [other]) == []
    form = f'<html><body><form action="{BASE}/suche" method="get"><input type="search" name="suchbegriff"></form></body></html>'
    t = W.search_url_templates(form, BASE + "/", [])
    assert t and t[0]["template"].startswith(BASE + "/suche?suchbegriff={q}"), t
    link = f'<html><body><a href="{BASE}/search?query=wool+socks&page=2">wool socks</a></body></html>'
    t = W.search_url_templates(link, BASE + "/", [])
    assert t and t[0]["template"] == BASE + "/search?query={q}", t
    # a query parameter on a non-search path is not a search (a filter, a tracking link)
    assert W.search_url_templates(f'<a href="{BASE}/collections/socks?q=red">red</a>', BASE + "/", []) == []
    assert W.search_url_templates("<html></html>", BASE + "/", []) == []
    assert W.fill_search_url(BASE + "/search?q={q}", "Kona Cotton") == BASE + "/search?q=Kona+Cotton"


class NoBoxPage(FakePage):
    """The fake shop whose search box never renders (its script was not loaded)."""

    def __init__(self, ld=True):
        super().__init__()
        self.ld = ld

    async def type_search(self, value, **kw):
        self.typed.append(value)
        return {"input": None, "document": "", "note": "no visible search input"}

    async def search_inputs(self):
        return []

    async def js(self, expr, read_only=False):
        r = await super().js(expr, read_only)
        if expr == R.READ_JS and isinstance(r, dict) and self.ld and urllib.parse.urlsplit(self.cur).path in ("", "/"):
            r["jsonld"] = list(r.get("jsonld") or []) + [SEARCH_LD]
        return r


def test_no_box_the_site_search_url_is_opened_and_compiled():
    page = NoBoxPage()
    rec = run(R.WebArch(FakePlanner([S("search", value="merino")]), shadow_images=False).run(
        page, {"id": "T", "utterance": "Search for merino", "kind": "search"}, BASE + "/"))
    assert page.gotos[-1] == BASE + "/search?q=merino", page.gotos
    op = next(o for o in rec["program"]["ops"] if o["op"] == "search")
    assert op["via"] == "url" and op["template"] == BASE + "/search?q={q}" and op["value"] == "merino"
    assert rec["stop"] != "refused" and "Merino" in rec["final"]["text"]
    # the planner was told the site's search page exists
    assert rec["summary"]["search"] is True and "search page" in rec["summary"]["search_how"]
    # the compiled tool replays with another value, no typing, no model
    tool = W.proof_tool(rec["program"], "search")
    page2 = NoBoxPage()
    rep = run(R.replay(page2, W.bind(tool["ops"], "wool"), by_rule=True))
    assert page2.gotos[-1] == BASE + "/search?q=wool" and not page2.typed and rep["model_calls"] == 0
    assert rep["search_url"] == BASE + "/search?q=wool"


def test_no_box_and_no_search_url_navigates_by_the_words():
    page = NoBoxPage(ld=False)
    rec = run(R.WebArch(FakePlanner([S("search", value="Socks")]), shadow_images=False).run(
        page, {"id": "T", "utterance": "Find socks", "kind": "search"}, BASE + "/"))
    assert page.gotos[-1] == BASE + "/collections/socks", page.gotos
    step = next(s for s in rec["steps"] if s["step"]["effect"] == "search")
    assert step["search_via"] == "navigation" and rec["stop"] not in ("refused", "failed")


def test_a_refused_read_only_goal_gets_the_fallback_plan_but_an_outside_goal_keeps_its_refusal():
    refuse = [S("refuse", "Merino Crew Sock stock", "unsupported")]

    class Refuser(FakePlanner):
        def __call__(self, messages):
            self.calls.append(messages)
            return json.dumps({"steps": refuse}), {"cost": 0.0}
    page = FakePage()
    rec = run(R.WebArch(Refuser(refuse), shadow_images=False).run(
        page, {"id": "T", "utterance": "Is the Merino Crew Sock in stock?", "kind": "answer_question"},
        BASE + "/"))
    assert any("fallback plan" in c for c in rec["changes"]), rec["changes"]
    assert rec["plan"][0] == S("search", value="merino crew sock")
    # the fallback searched and opened the item (what the planner says after reading it is its own)
    assert page.typed == ["merino crew sock"] and page.gotos[-1].endswith("/products/merino-crew-sock"), page.gotos
    rec = run(R.WebArch(Refuser(refuse), shadow_images=False).run(
        FakePage(), {"id": "T", "utterance": "Email me the Merino Crew Sock page", "kind": "answer_question"},
        BASE + "/"))
    assert rec["stop"] == "refused"
    assert W.goal_item_words("Is Kona Cotton in stock?") == "kona cotton"
    assert W.fallback_plan("Vad kostar Kompostkvarn 18-698?")[0]["value"] == "kompostkvarn 18 698"
    assert [s["effect"] for s in W.fallback_plan("What does the Wool Beanie cost?")] == \
        ["search", "open_item", "read_content"]


def test_the_page_summary_says_search_when_only_the_url_is_known():
    read = {"url": BASE + "/", "title": "T", "elements": [], "inputs": []}
    assert W.planner_messages("g", R.page_summary(read, [{"template": BASE + "/s?q={q}", "how": "x"}]))[1][
        "content"].count("Search box on this page: yes") == 1
    assert "Search box on this page: no" in W.planner_messages("g", R.page_summary(read))[1]["content"]


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
