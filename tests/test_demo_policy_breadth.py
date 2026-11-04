"""Web tool version 10, part 2, the DEMONSTRATOR LOOP, offline (the fake shop of ``test_demo_breadth_filtered``; a
scripted CLI that reads the observation it is given; no browser, no network, no model): the three breadth workflows
are demonstrated through ``demo_policy.run_demo`` - the templated goals, version 10's system prompt (the drop-down
choice ``select`` and the ``read`` note), the observation listing drop-downs with their options, checkboxes with their
state and the next-page link right after the search box - and the traces are valid and compile.

What must hold:

1. filtered_search: the scripted demonstrator chooses "Preis aufsteigend" in the sort drop-down by its number; the
   trace records the option's text and value; valid, compiled to the URL parameter, the tool replays sorted;
2. paged_search: it follows the next-page link (rel=next) and reports page 2; valid, compiled;
3. visit_results: it opens the first result carrying the second's URL in a "read" note (shown back in the steps so
   far), opens the second, reports both; valid, compiled, the tool reads a table;
4. the old workflows' prompt and observation are unchanged (no options, no "read", ``SYSTEM``); a select is refused
   for them, and a malformed select / read note is refused or dropped.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_visit_identity.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_policy as P  # noqa: E402
from test_demo_breadth_filtered import BASE, SITE, FakePage, Shop, run  # noqa: E402


async def _settle(page):
    return None


def num(prompt, pattern):
    """The element number of the observation line matching ``pattern``."""
    m = re.search(r"^\[(\d+)\] " + pattern, prompt, re.M)
    assert m, (pattern, prompt[:2000])
    return int(m.group(1))


def elements_block(prompt):
    return prompt.split("ELEMENTS:", 1)[1].split("VISIBLE TEXT", 1)[0]


class Demonstrator:
    """A scripted CLI: one answer per step, computed from the prompt it gets (as a model would)."""

    def __init__(self, plan):
        self.plan, self.prompts = plan, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.plan[len(self.prompts) - 1](prompt)), {"seconds": 0.1}


def demo(shop, workflow, value, plan, max_steps=6):
    cli = Demonstrator(plan)
    tr = asyncio.run(P.run_demo(FakePage(shop), workflow, value, BASE + "/", SITE, cli, _settle, max_steps=max_steps,
                                cli_name="scripted"))
    return tr, cli


def results_of(prompt, n=5):
    out = []
    for m in re.finditer(r'^\[\d+\] link "([^"]+)"(?: \(card: [^\n]*?\))? -> (/p/[^\s]+)', elements_block(prompt), re.M):
        out.append({"title": m.group(1), "url": BASE + m.group(2)})
    return out[:n]


def test_filtered_search_through_the_loop():
    shop = Shop()
    plan = [lambda p: {"action": "search", "element": 0, "text": "Akkuschrauber"},
            lambda p: {"action": "select", "element": num(p, r'select "Sortieren nach" options: Relevanz\*'),
                       "option": "Preis aufsteigend"},
            lambda p: {"action": "done", "result": {"results": results_of(p),
                                                    "applied": [{"kind": "sort", "control": "Sortieren nach",
                                                                 "option": "Preis aufsteigend"}]}}]
    tr, cli = demo(shop, "filtered_search", "Akkuschrauber", plan)
    assert tr["stop"] == "done" and tr["calls"] == 3, (tr["stop"], tr["calls"])
    assert "sort the results by price, lowest first" in cli.prompts[0] and '"select"' in P.system_for("filtered_search")
    # the observation: the drop-down right after the search box, with its options; the checkbox with its state
    els = elements_block(cli.prompts[1]).strip().splitlines()
    assert els[0].startswith("[0] search box") and "options: Relevanz* | Preis aufsteigend" in els[1], els[:4]
    assert any("[not checked]" in x for x in els[:6]), els[:6]
    oc = tr["steps"][1]["outcome"]
    assert oc["how"] == "select" and oc["option_text"] == "Preis aufsteigend" and oc["option_value"] == "price-asc", oc
    assert C.demo_validity(tr)[0]
    g = C.compile_trace(tr)
    assert g["ok"] and g["tool"]["refine"] == {"mode": "url", "params": {"sort": "price-asc"}}, g.get("why")
    r = run(shop, g["tool"], "Bohrhammer")
    assert r["status"] == "ok" and [x["price"] for x in r["output"]["results"]] == [69.0, 119.0, 159.0], r


def test_paged_search_through_the_loop():
    shop = Shop()
    plan = [lambda p: {"action": "search", "element": 0, "text": "Akkuschrauber"},
            lambda p: {"action": "click", "element": num(p, r'link "Weiter" -> [^\n]* \(rel=next\)')},
            lambda p: {"action": "done", "result": {"results": results_of(p), "total": "7"}}]
    tr, cli = demo(shop, "paged_search", "Akkuschrauber", plan)
    assert tr["stop"] == "done" and C.demo_validity(tr)[0], C.demo_validity(tr)
    assert "go to the NEXT page" in cli.prompts[0]
    g = C.compile_trace(tr)
    assert g["ok"] and g["tool"]["pager"]["mode"] == "param" and tr["result"]["total"] == "7", g.get("why")


def test_visit_results_through_the_loop():
    shop = Shop()
    seen = {}

    def first(p):
        res = results_of(p, 2)
        seen["second"], seen["second_title"] = res[1]["url"], res[1]["title"]
        return {"action": "click", "element": num(p, r'link "' + re.escape(res[0]["title"]) + '"'),
                "read": {"next": res[1]}}

    def second(p):
        m = re.search(r'"name": "([^"]+)".*?"price": ([\d.]+)', json.dumps(json.loads(
            re.search(r'<script type="application/ld\+json">(.*?)</script>', FakePage(shop).shop.dom(
                re.search(r"^URL: (\S+)", p, re.M).group(1))[0]).group(1))))
        item = {"name": m.group(1), "price": m.group(2), "currency": "EUR", "availability": "Auf Lager",
                "url": re.search(r"^URL: (\S+)", p, re.M).group(1)}
        seen["first"] = item
        return {"action": "goto", "url": seen["second"], "read": {"item": item}}

    def done(p):
        m = re.search(r'"name": "([^"]+)".*?"price": ([\d.]+)', json.dumps(json.loads(
            re.search(r'<script type="application/ld\+json">(.*?)</script>', FakePage(shop).shop.dom(
                re.search(r"^URL: (\S+)", p, re.M).group(1))[0]).group(1))))
        item = {"name": m.group(1), "price": m.group(2), "currency": "EUR", "availability": "Auf Lager",
                "url": re.search(r"^URL: (\S+)", p, re.M).group(1)}
        return {"action": "done", "result": {"items": [seen["first"], item]}}
    plan = [lambda p: {"action": "search", "element": 0, "text": "Bohrhammer"}, first, second, done]
    tr, cli = demo(shop, "visit_results", "Bohrhammer", plan)
    assert tr["stop"] == "done" and tr["calls"] == 4, (tr["stop"], tr["calls"])
    # the read note is recorded with the action and shown back in the steps so far
    assert tr["steps"][1]["action"]["read"] == {"next": {"title": seen["second_title"], "url": seen["second"]}}
    assert seen["second_title"] == "Makita Bohrhammer HR2470", seen
    assert "read: {\"next\"" in cli.prompts[2] and seen["second"] in cli.prompts[2], cli.prompts[2][-600:]
    ok, why, det = C.demo_validity(tr)
    assert ok and len(det["opened"]) == 2, (why, det)
    g = C.compile_trace(tr)
    assert g["ok"] and g["tool"]["k"] == 2, g.get("why")
    r = run(shop, g["tool"], "Akkuschrauber")
    assert r["status"] == "ok" and len(r["output"]["items"]) == 2, r


def test_the_old_workflows_are_unchanged():
    r = {"url": BASE + "/search?q=x", "title": "t", "inputs": [{"index": 0, "name": "q"}],
         "elements": [{"i": 1, "tag": "select", "text": "Sortieren nach", "area": "main",
                       "options": [{"value": "", "text": "Relevanz", "selected": True}]},
                      {"i": 2, "tag": "a", "text": "Weiter", "href": BASE + "/s?page=2", "area": "main", "rel": "next"}],
         "text": "x"}
    old, _m = P.build_observation(r)
    assert all("options" not in it and "rel" not in it for it in old["elements"]), old["elements"]
    assert "options:" not in P.render_observation(old) and P.system_for("site_search") == P.SYSTEM
    assert "read" not in P.SYSTEM and "select" not in P.ACTIONS
    a, why = P.parse_action('{"action": "select", "element": 1, "option": "x"}', "site_search")
    assert a is None and "unknown action" in why, why
    a, _w = P.parse_action('{"action": "click", "element": 2, "read": {"x": 1}}', "site_search")
    assert a == {"action": "click", "element": 2}
    a, why = P.parse_action('{"action": "select", "element": 1}', "filtered_search")
    assert a is None and "needs an element number and an option" in why
    a, _w = P.parse_action('{"action": "click", "element": 2, "read": "not an object"}', "paged_search")
    assert a == {"action": "click", "element": 2}
    a, _w = P.parse_action(json.dumps({"action": "goto", "url": "/x", "read": {"t": "y" * 4000}}), "visit_results")
    assert a == {"action": "goto", "url": "/x"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
