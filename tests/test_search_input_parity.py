"""Offline replays choose the search input the live replay chooses (``execution/dom_page.py``,
``execution/cdp_guard.py``), and the repair canary looks for it the same way (``execution/repair_canary.py``).

What must hold:

1. ``dom_page.text_inputs_of`` is ``cdp_guard.SEARCH_INPUTS_JS`` on a parsed page: the page's structure first
   (``type=search``, ``role=searchbox``, a ``role=search`` or ``<search>`` landmark), then the names as whole words or
   word starts, and the rule of web tools before version 15 as its own flag - checked against the script itself in
   Node when a Node binary is there;
2. a typed replay finds the recorded input among the search-like inputs and the one with its recorded name or id; a
   tool before version 15 also among the inputs its own rule named, so an input it recorded by its index alone, that
   only that rule named, is typed into as before - live and offline alike; a version-15 tool's index fallback reads
   the search-like inputs only;
3. the probe canary finds a tool's recorded input by the identity its replay uses, so it starts no repair for an
   input that is still there; a renamed input is still a broken path;
4. without a recorded input the offline page chooses as the live session does: the structure, then a GET form that
   submits the typed value, then the names.

Offline: recorded pages, a scripted fake CDP websocket; no browser, no network.
"""
from __future__ import annotations

import asyncio
import copy
import html
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_canary as RC  # noqa: E402
import test_repair_ladder as TL  # noqa: E402
import test_search_box_evidence as SB  # noqa: E402

KEYS = ("index", "visible", "name", "id", "type", "rank", "named14")

#: DOMs as the Node stand-in builds them (``tests/bench/generalize_scope_bench.DOM_JS``): ``[tag, attrs, text, kids]``
PAGES = [
    [["input", {"type": "search", "name": "a"}], ["input", {"role": "searchbox", "name": "b"}],
     ["div", {"role": "search"}, "", [["input", {"name": "c"}]]], ["search", {}, "", [["input", {"name": "d"}]]],
     ["input", {"role": "search", "name": "e"}], ["input", {"name": "q"}], ["input", {"name": "s"}]],
    [["input", {"name": "plz", "placeholder": "Search products"}], ["input", {"id": "txtSearch"}],
     ["input", {"name": "searchTerm"}], ["input", {"class": "sitesearch-field"}], ["input", {"name": "research_topic"}],
     ["input", {"name": "KEYWORDS"}], ["input", {"name": "my-query"}], ["input", {"aria-label": "findStore"}],
     ["input", {"name": "Suchbegriff"}], ["input", {"name": "HTMLSearchField"}], ["input", {"name": "2search"}]],
    [["form", {}, "", [["input", {"name": "user"}], ["input", {"type": "password", "name": "pw"}]]],
     ["input", {"type": "hidden", "name": "token"}], ["input", {"type": "checkbox", "name": "search"}],
     ["input", {"type": "email", "name": "query"}], ["input", {"name": "q", "hidden": ""}],
     ["div", {"hidden": ""}, "", [["input", {"type": "search", "name": "q2"}]]],
     ["input", {"name": "q3", "disabled": ""}],
     ["input", {"name": "q4", "readonly": ""}], ["input", {"name": "termo"}]],
]


def markup(spec):
    out = []
    for tag, attrs, *rest in spec:
        text, kids = (rest + ["", []])[:2]
        a = "".join(f' {k}="{html.escape(str(v))}"' if v != "" else f" {k}" for k, v in attrs.items())
        out.append(f"<{tag}{a}>" if tag == "input" else f"<{tag}{a}>{html.escape(text)}{markup(kids)}</{tag}>")
    return "".join(out)


def ported(spec):
    root, _b = K.parse(f"<html><body>{markup(spec)}</body></html>")
    return [{k: r[k] for k in KEYS} for r in D.text_inputs_of(root)]


def test_the_offline_rule_reads_structure_first_and_names_as_whole_words():
    rows = {r["name"] or r["id"] or r["index"]: r for r in ported(PAGES[0]) + ported(PAGES[1])}
    for name in ("a", "b", "c", "d", "e"):
        assert rows[name]["rank"] == 0, rows[name]
    for name in ("q", "s", "plz", "txtSearch", "searchTerm", "KEYWORDS", "my-query", "HTMLSearchField"):
        assert rows[name]["rank"] == 2, rows[name]
    for name in ("research_topic", "Suchbegriff", "2search"):
        assert rows[name]["rank"] == 3, rows[name]
    # the rule of web tools before version 15: a search word anywhere, no <search> element, no searchbox role
    assert rows["research_topic"]["named14"] and not rows["d"]["named14"] and not rows["b"]["named14"]
    side = next(r for r in ported(PAGES[1]) if r["rank"] == 3 and r["named14"] and not r["name"])
    assert side["index"] == 3                                 # class="sitesearch-field": only the earlier rule named it
    third = ported(PAGES[2])
    assert [r["name"] for r in third] == ["q", "q2", "q3", "q4", "termo"], third
    assert [r["visible"] for r in third] == [False, False, False, False, True]
    assert [r["index"] for r in third] == [5, 6, 7, 8, 9]      # every input counts, as querySelectorAll('input')


@pytest.mark.skipif(not SB.node_binary(), reason="needs a Node binary (ANYUSE_TEST_NODE or node on PATH)")
def test_the_offline_rule_is_the_live_script_on_the_same_page():
    req = "\n".join(json.dumps({"dom": spec, "inputs": G.SEARCH_INPUTS_JS, "reveals": []}) for spec in PAGES)
    proc = subprocess.run([SB.node_binary(), "-e", SB._bench_dom_js()], input=req, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr[:400]
    for spec, res in zip(PAGES, json.loads(proc.stdout)):
        live = [{k: r.get(k) for k in KEYS} for r in res["inputs"]]
        assert ported(spec) == live, (spec, live)
    # the word rule itself, beside the script's own namedBy
    words = list(G.SEARCH_INPUT_WORDS)
    texts = ["searchBox", "txtSearch", "sitesearch", "research", "HTMLSearchField", "KEYWORDS", "my-query",
             "findStore", "sucheBegriff", "search2", "2search", "ÜberSuche", "aQuery", "QUERYString"]
    js = G._SEARCH_TOKENS_JS + \
        f"process.stdout.write(JSON.stringify({json.dumps(texts)}.map(namedBy({json.dumps(words)}))));"
    proc = subprocess.run([SB.node_binary(), "-e", js], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:400]
    assert [D.named_by(words, t) for t in texts] == json.loads(proc.stdout)


class SubstringShop(TL.Shop):
    """The ladder's shop whose search box is named only by a class word inside a compound (no ``type=search``, no
    landmark, no search name): the rule of web tools before version 15 named it, the whole-word rule does not."""

    def __init__(self):
        super().__init__(qkey="qq")

    def start(self):
        return super().start().replace('role="search"', "").replace('type="search" ', 'class="sitesearch-field" ')

    def results(self, q):
        return super().results(q).replace('role="search"', "").replace('type="search" ', 'class="sitesearch-field" ')


def typed_tool(version):
    t = copy.deepcopy(TL.compiled())
    t["search"] = {"mode": "typed", "input": {"index": 0}, "fallback_templates": [], "start_url": TL.BASE + "/",
                   "overlay": False}
    return dict(t, version=version)


def test_a_frozen_tool_finds_the_input_it_recorded_by_its_index_as_before():
    v = "Bosch GSR 12V Akkuschrauber"
    out14 = D.run(SubstringShop(), typed_tool(14), v)
    assert out14["status"] == "ok" and out14["search_via"] == "typed", (out14["status"], out14["why"])
    # a version-15 tool's index fallback reads the search-like inputs only
    out15 = D.run(SubstringShop(), typed_tool(15), v)
    assert out15["status"] == "failed" and "found no search box" in out15["why"], out15["why"]
    # the live session chooses the same way
    rows = [SB.row(0, 3, "email"), dict(SB.row(1, 3, ""), named14=True)]
    out = SB.run(SB.session(SB.Page(rows)).type_search("red fox", want={"index": 1}, named14=True))
    assert out["input"]["index"] == 1
    page = SB.Page(rows)
    out = SB.run(SB.session(page).type_search("red fox", want={"index": 1}))
    assert out["input"] is None and page.typed == []


def test_the_probe_canary_finds_the_recorded_input_by_the_replays_identity():
    def present(rows, identity, named14=False):
        step = {"step": "search", "kind": "type", "identity": identity, "named14": named14}
        return SB.run(RC.control_present(SB.session(SB.Page(rows)), step))[0]
    # recorded under an earlier rule: its name, which the whole-word rule ranks as any text input
    assert present([SB.row(0, 3, "email"), SB.row(1, 3, "sitesearch")], {"name": "sitesearch", "index": 1})
    # recorded by its index alone: a frozen tool's own rule still names it; a version-15 tool's replay would not
    frozen = [SB.row(0, 3, "email"), dict(SB.row(1, 3, ""), named14=True)]
    assert present(frozen, {"index": 1}, named14=True) and not present(frozen, {"index": 1})
    # a renamed input is a broken path
    assert not present([SB.row(0, 2, "suchbegriff")], {"name": "q", "index": 0})
    # through the canary's plan: a frozen typed tool's steps say so
    plan = RC.step_plan(typed_tool(14))
    assert [s.get("named14") for s in plan if s["kind"] in ("type", "key")] == [True, True]
    assert all(s.get("named14") is False for s in RC.step_plan(typed_tool(15)) if s["kind"] in ("type", "key"))


class TwoBoxes(D.DomSite):
    """A newsletter field named like nothing, then a store finder named like a search; only the second box's own GET
    form submits the typed value to the site's search."""

    PAGE = ('<html><body><form action="/newsletter" method="post"><input name="email"></form>'
            '<form action="/zoeken" method="get"><input name="trefwoord"></form>'
            '<input name="find-a-store" placeholder="Find a store"></body></html>')

    def __init__(self):
        super().__init__([D.Recorded("https://h.test/", self.PAGE)])


def test_without_a_recorded_input_the_offline_page_chooses_as_the_live_session():
    page = D.DomPage(TwoBoxes())
    SB.run(page.goto("https://h.test/"))
    out = SB.run(page.type_search("red fox"))
    assert out["input"]["name"] == "trefwoord", out                 # the GET form that submits the value
    assert out["document"] == "https://h.test/zoeken?trefwoord=red+fox"
    # the page's structure decides first
    site = D.DomSite([D.Recorded("https://h.test/", TwoBoxes.PAGE.replace('name="email"', 'name="email" type="search"'))])
    page = D.DomPage(site)
    SB.run(page.goto("https://h.test/"))
    assert SB.run(page.type_search("red fox"))["input"]["name"] == "email"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
