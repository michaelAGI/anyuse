"""The agent's first-try answers on 51 not-correct answers and 9 runs
without an answer, read step by step - the causes in the HARNESS, fixed generally. Synthetic pages, no browser, network
or model.

* the observation order: a search's first results that the start page also teased (same text, same target) were
  listed after the menus ("kept, so chrome") - www.fotomagazin.de, www.u-run.fr (its first seven past the 60 shown),
  www.windows-faq.de. When the page names link containers, a link whose FIRST container on the page is new is new;
  never a link only in the chrome, never a target the header / navigation links (a card's category tag);
* the answer checks (``agent_harness.answer_problem``, ONE corrective note per kind, no step used): rows the
  observation never showed, rows that re-run the site's own search, a minority of rows that leave the site, an empty
  page, a page the search did not change, "no results" against a stated count;
* the page's own search URL named when no search box is listed (www.fairplay-online.de, www.whiskyshop.it);
* a catalogued search provider's call refused by its robots.txt is kept like a refused provider (www.purexbox.com:
  Google Programmable Search, cse.google.com disallows it) - a "no results" answer after it is the typed stop;
* an empty read of any page is read once more (www.nomadicmatt.com, www.czajnikowy.com.pl).
"""
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402

BASE = "https://mag.example"


# ------------------------------------------------------------------------------------------------ observation order
def _rec(links):
    """links: (text, href, area, [sigs in document order])"""
    return {"elements": [{"i": i, "tag": "a", "text": t, "href": h, "area": a} for i, (t, h, a, _s) in enumerate(links)],
            "link_sigs": {h: s for _t, h, _a, s in links}}


def _els(links):
    return [T.Element(index=i, kind="link", text=t, href=h, tag="a") for i, (t, h, _a, _s) in enumerate(links)]


HOME = [("Home", BASE + "/", "nav", ["body.home>header>nav>ul>li"]),
        ("Tests", BASE + "/tests/", "nav", ["body.home>header>nav>ul>li"]),
        ("Tamron 20-40 jetzt für Nikon", BASE + "/tamron-20-40/", "main", ["body.home>main>div.teasers>article"]),
        ("Newest camera", BASE + "/newest/", "main", ["body.home>main>div.teasers>article"]),
        ("Sidebar promo", BASE + "/promo/", "aside", ["body.home>aside>div.widget>ul>li"])]
RESULTS = [("Home", BASE + "/", "nav", ["body.search>header>nav>ul>li"]),
           ("Tests", BASE + "/tests/", "nav", ["body.search>header>nav>ul>li", "body.search>main>ol.hits>li>p.cat"]),
           ("Sidebar promo", BASE + "/promo/", "aside", ["body.search>aside>div.widget>ul>li"]),
           ("Tamron 20-40 jetzt für Nikon", BASE + "/tamron-20-40/", "main", ["body.search>main>ol.hits>li"]),
           ("Tests", BASE + "/tests/", "main", ["body.search>main>ol.hits>li>p.cat"]),
           ("Nikon Z5 test", BASE + "/z5/", "main", ["body.search>main>ol.hits>li"])]


def _order(new_by_container):
    tools = HT.BrowserTools(None, BASE + "/")
    tools.NEW_BY_CONTAINER = new_by_container
    tools._new_first(_els(HOME), BASE + "/", _rec(HOME))
    got = tools._new_first(_els(RESULTS), BASE + "/search?q=Nikon", _rec(RESULTS))
    return [e.text for e in got]


def test_a_result_the_start_page_teased_is_listed_with_the_other_results():
    order = _order(True)
    assert order[:2] == ["Tamron 20-40 jetzt für Nikon", "Nikon Z5 test"], order
    # the menu's category repeated as a result card's tag stays with the chrome, after the results
    assert order.index("Tests") > order.index("Nikon Z5 test")
    # the old reading: the teased result came after the menus
    old = _order(False)
    assert old[0] == "Nikon Z5 test" and old.index("Tamron 20-40 jetzt für Nikon") > old.index("Home"), old


def test_a_teaser_kept_above_the_results_keeps_its_links_unchanged():
    home = HOME + [("Bosch GSR", BASE + "/p/gsr/", "main", ["body.home>main>div.strip>a"])]
    res = [("Bosch GSR", BASE + "/p/gsr/", "main", ["body.search>main>div.strip>a", "body.search>main>ol.hits>li"]),
           ("Bosch GSR 12V Akkuschrauber", BASE + "/p/gsr/", "main", ["body.search>main>div.strip>a",
                                                                       "body.search>main>ol.hits>li"]),
           ("Makita DF333", BASE + "/p/df333/", "main", ["body.search>main>ol.hits>li"])]
    tools = HT.BrowserTools(None, BASE + "/")
    tools._new_first(_els(home), BASE + "/", _rec(home))
    order = [e.text for e in tools._new_first(_els(res), BASE + "/search?q=Bosch", _rec(res))]
    assert order.index("Bosch GSR") > order.index("Makita DF333"), order


# ---------------------------------------------------------------------------------------------------- answer checks
def _obs(links, url=BASE + "/search?q=Nikon", text="Suche\n2 Ergebnisse für Nikon", n_shown=None):
    els = _els(links)
    mapping = {e.index: {"src": "el", "href": e.href, "text": e.text, "i": e.index} for e in els}
    return T.Observation(kind="web", url=url, text=text, elements=els, mapping=mapping, page=_rec(links))


def test_the_answer_checks_each_catch_their_case_and_pass_a_good_answer():
    start = _obs(HOME, url=BASE + "/", text="Home")
    page = _obs(RESULTS)
    good = {"results": [{"element": 3}, {"element": 5}]}
    assert H.answer_problem(good, page, start, "Nikon", searched=True) == ("", "")
    # rows the observation never showed (not on the page; past the shown elements)
    assert H.answer_problem({"results": [{"element": 99}]}, page, start, "Nikon", True)[0] == "unseen"
    many = [(f"Item {i}", f"{BASE}/i{i}/", "main", ["body.s>main>ol>li"]) for i in range(70)]
    assert H.answer_problem({"results": [{"element": 65}]}, _obs(many), start, "Nikon", True)[0] == "unseen"
    # a row that re-runs the site's own search (a suggestion), or the page itself with a fragment (a sort link)
    sugg = RESULTS + [("nikon z6", BASE + "/search?q=nikon+z6", "main", ["body.search>main>div.related>a"]),
                      ("Neueste", BASE + "/search?q=Nikon#sort=new", "main", ["body.search>main>div.sort>a"])]
    p2 = _obs(sugg)
    assert H.answer_problem({"results": [{"element": 6}, {"element": 3}]}, p2, start, "Nikon", True)[0] == "search_links"
    assert H.answer_problem({"results": [{"element": 7}, {"element": 3}]}, p2, start, "Nikon", True)[0] == "search_links"
    # a minority of rows that leave the site (a partner shop)
    off = RESULTS + [("3.99 bei Partner", "https://partner.example/x", "main", ["body.search>main>ol.hits>li"])]
    assert H.answer_problem({"results": [{"element": 3}, {"element": 6}, {"element": 5}]}, _obs(off), start, "Nikon",
                            True)[0] == "offsite"
    # an empty page; a page the search did not change; "no results" against a stated count
    empty = T.Observation(kind="web", url=BASE + "/search?q=Nikon")
    assert H.answer_problem({"no_results": True}, empty, start, "Nikon", True)[0] == "empty"
    same = _obs(HOME, url=BASE + "/?s=Nikon", text="Home")
    assert H.answer_problem({"results": [{"element": 2}]}, same, start, "Nikon", True)[0] == "unchanged"
    assert H.answer_problem({"results": [{"element": 2}]}, same, start, "Nikon", False) == ("", "")   # no search yet
    stated = _obs(RESULTS, text="Home\nSearch: 776 results found for \"Nikon\"")
    assert H.answer_problem({"no_results": True}, stated, start, "Nikon", True)[0] == "stated_count"
    assert H.answer_problem({"no_results": True}, _obs(RESULTS, text="Keine Ergebnisse"), start, "Nikon", True) == \
        ("", "")
    # each check can be switched off
    assert H.answer_problem({"results": [{"element": 99}]}, page, start, "Nikon", True, checks=()) == ("", "")


class _Tools:
    """A toolbox stub: the start page, then (after the search) the same page - the search changed nothing."""
    kind, site, app, bundle, start_url = "web", "mag.example", "", "", BASE + "/"

    def __init__(self):
        self.obs = _obs(HOME, url=BASE + "/", text="Home")

    def observe(self):
        return self.obs

    def available(self):
        return ["search", "click", "open", "done", "fail"]

    def effect_of(self, action, obs):
        return T.READ

    def gate(self, action, obs):
        return "", None

    def dispatch(self, action, obs):
        return T.Outcome(True, url_after=BASE + "/?s=Nikon", observation=_obs(HOME, url=BASE + "/?s=Nikon", text="Home"))

    def correction(self, action, obs):
        return ""


def test_the_harness_refuses_a_start_page_answer_once_and_takes_the_next():
    tools = _Tools()
    pol = P.ScriptedPolicy([{"action": "search", "element": 0, "text": "Nikon"},
                            {"action": "done", "result": {"results": [{"element": 2}]}},
                            {"action": "done", "result": {"no_results": True}}])
    tr = H.Harness(tools, pol, H.HarnessConfig(), run_id="v").run("search this site for {q}", {"q": "Nikon"})
    assert any(c["why"] == "answer: unchanged" for c in tr.corrections), tr.corrections
    assert tr.stop == T.Stop.DONE and tr.result == {"no_results": True}


# --------------------------------------------------------------------------------------------- the page's search URL
def test_the_pages_own_search_url_is_named_when_no_search_box_is_listed():
    rec = {"search_urls": ["https://www.fairplay.example/?s={q}"]}
    links = [T.Element(index=0, kind="link", text="Blog", href="https://www.fairplay.example/blog")]
    line = HT.search_url_line(links, rec)
    assert line and "https://www.fairplay.example/?s={q}" in line[0]
    assert HT.search_url_line([T.Element(index=0, kind="search box", text="Suche")] + links, rec) == []
    assert HT.search_url_line(links, {}) == []
    tools = HT.BrowserTools(None, "https://www.fairplay.example/")
    obs = T.Observation(kind="web", url="https://www.fairplay.example/", elements=links,
                        mapping={0: {"src": "el", "href": "https://www.fairplay.example/blog"}}, page=rec)
    note = tools.correction(T.Search(0, "Catan"), obs)
    assert "https://www.fairplay.example/?s={q}" in note, note


# ------------------------------------------------------------------------------------------ a provider's robots wall
class _Guard:
    def __init__(self, failed):
        self.failed = failed
        self.failed_types = {}


class _Page:
    def __init__(self, failed):
        self.guard = _Guard(failed)


def test_a_catalogued_providers_robots_refusal_is_kept_as_a_refused_provider():
    page = _Page([("https://cse.google.com/cse.js?cx=0123:abc", "robots: disallowed by robots.txt")])
    got = HT.search_refusal(page, {"failed": 0}, "Halo", "https://www.purexbox.example/",
                            url_after="https://www.purexbox.example/search?q=Halo")
    assert got and got[0] == "provider_search" and "cse.google.com" in got[1]
    # not a provider call (a challenge script on the same host), not a robots refusal: nothing
    assert HT.search_refusal(_Page([("https://www.google.com/recaptcha/api.js", "robots: disallowed")]), {"failed": 0},
                             "Halo", "https://www.purexbox.example/") is None
    assert HT.search_refusal(_Page([("https://cse.google.com/cse.js?cx=1", "budget: cap")]), {"failed": 0}, "Halo",
                             "https://www.purexbox.example/") is None


# ---------------------------------------------------------------------------------------------- an empty read again
def test_an_empty_read_of_any_page_is_read_once_more(monkeypatch):
    from anyuse.execution import demo_policy as DPOL
    reads = []

    async def observe(page, breadth=False):
        reads.append(1)
        if len(reads) == 2:                                  # the results page's first read: nothing drawn yet
            return {"url": BASE + "/search?q=x", "elements": []}, {"elements": [], "text": ""}, {}
        return ({"url": BASE + "/search?q=x" if len(reads) > 1 else BASE + "/", "elements": []},
                {"elements": [{"n": 0, "kind": "link", "text": "Result"}], "text": "Result"},
                {0: {"src": "el", "href": BASE + "/r", "text": "Result", "tag": "a"}})

    class _P:
        async def goto(self, url):
            return None

        async def settle(self, s):
            return None

        async def network(self, want_bodies=False):
            return []

    monkeypatch.setattr(DPOL, "observe", observe)
    monkeypatch.setattr(HT, "runaway_stop", lambda page: "")
    tools = HT.BrowserTools(_P(), BASE + "/")
    tools.observe()
    o = tools.observe()
    assert len(reads) == 3 and [e.text for e in o.elements] == ["Result"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
