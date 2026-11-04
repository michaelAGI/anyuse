"""Breadth tools are frozen per version (``execution/demo_breadth.py``): a saved version-14 tool's run reads its
pages exactly as before, a version-15 tool reads the site's own evidence first.

What must hold:

1. version-14 tools compiled by the code of an earlier release (``testdata/breadth_frozen/decisions.json``: the
   tools and every run's decision, recorded with that code) decide every run here exactly as recorded, on shops whose
   pager names its page links "Seite N" or has no next link, whose next-page word points elsewhere than the page
   number, that states its total only beside the quoted input, that marks its dates only in markup, that shows a
   numeric date after a month-name date, and with a sort value that holds a sort word inside a longer word;
2. the same demonstrations compiled now (version 15) read the next page by the number its URL increments, the total
   beside the echo, the date in the markup and the numeric date first, and a sort value by whole words;
3. a heal is no replay: it reads page 1's next page and a sort option by the current rules for a tool of any version.

Offline: ``test_demo_breadth_filtered``'s fake shop; no browser, no network, no model; the verifier pinned off (its
own tests cover it).
"""
import os as _os_arm
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
import test_demo_breadth_filtered as F  # noqa: E402
import test_demo_breadth_paged as P  # noqa: E402

FIXTURE = os.path.join(HERE, "testdata", "breadth_frozen", "decisions.json")
MONTHS_DE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
             "November", "Dezember")


class Variant(F.Shop):
    """The fake shop with one change: ``seite`` (pages of 2; page links named "Seite N", no next link), ``textnum``
    (no rel; the next-page word skips a page, the page number does not), ``echo`` (no pager summary; the total beside
    the quoted input), ``markup`` (each card's date only in a ``<time datetime>``), ``mixed`` (a month-name date,
    then a numeric "Stand" date in reverse order)."""

    def __init__(self, kind="plain", **kw):
        super().__init__(**dict(kw, **({"total": False} if kind == "echo" else {"per": 2} if kind == "seite" else {})))
        self.kind = kind

    def card(self, c):
        out = super().card(c)
        y, m, d = c[4].split("-")
        shown = f'<div class="date">{d}.{m}.{y}</div>'
        if self.kind == "markup":
            out = out.replace(shown, f'<div class="date"><time datetime="{c[4]}">kürzlich</time></div>')
        elif self.kind == "mixed":
            out = out.replace(shown, f'<div class="date">{int(d)}. {MONTHS_DE[int(m) - 1]} {y} · Stand '
                                     f'2026-{13 - int(m):02d}-01</div>')
        return out

    def search_dom(self, url, state):
        dom = super().search_dom(url, state)
        if self.kind == "seite":
            dom = re.sub(r'<a rel="next" href="[^"]*">Weiter</a>', "", dom)
            dom = re.sub(r'(<a href="/search\?[^"]*page=\d+[^"]*">)(\d+)</a>', r"\1Seite \2</a>", dom)
        elif self.kind == "textnum":
            def nav(m):
                nxt = int(m.group(2))
                return (f'<a href="/search?{m.group(1)}cursor=c{nxt + 1}">Weiter</a>'
                        f'<a href="/search?{m.group(1)}cursor=c{nxt}">{nxt}</a>')
            dom = re.sub(r'<a rel="next" href="/search\?([^"]*?)cursor=c(\d+)">Weiter</a>', nav, dom)
        elif self.kind == "echo":
            q = state.get("q", "")
            n = len(self.hits(q, state.get("sort", ""), state.get("brand", ""), state.get("instock", "")))
            dom = dom.replace("</h1>", f"</h1><p class=\"echo\">„{q}“: {n} sonuç</p>", 1)
        return dom


#: (tool, shop, value, parameters)
CASES = [
    ("param", "plain", "Akku", {"n": 20}), ("param", "seite", "Akku", {"n": 20}), ("param", "seite", "Bosch", {}),
    ("param", "echo", "Akku", {"n": 20}), ("param", "echo", "Bosch", {}),
    ("cursor", "plain", "Akkuschrauber", {}), ("cursor", "textnum", "Akku", {"n": 20}),
    ("cursor", "echo", "Akku", {"n": 20}),
    ("newest", "plain", "Bosch", {}), ("newest", "markup", "Bosch", {}), ("newest", "mixed", "Bosch", {}),
    ("newest", "mixed", "Akkuschrauber", {}), ("newest", "plain", "Bosch", {"sort": "lowpricefirst"}),
    ("price", "plain", "Bosch", {}), ("price", "mixed", "Akkuschrauber", {}),
    ("price", "plain", "Akkuschrauber", {"sort": "lowpricefirst"}),
]


def shop(tool, kind):
    return Variant(kind, pager="cursor") if tool == "cursor" else Variant(kind)


def compile_tools(version=None):
    """The four breadth tools, compiled from their demonstrations (``version``: the tree's default)."""
    kw = {} if version is None else {"version": version}
    traces = {"param": P.paged_trace(F.Shop()), "cursor": P.paged_trace(F.Shop(pager="cursor")),
              "newest": F.filtered_trace(F.Shop(), option="Neueste"), "price": F.filtered_trace(F.Shop())}
    out = {}
    for key, tr in traces.items():
        g = C.compile_trace(tr, **kw)
        assert g["ok"], (key, g.get("why"))
        out[key] = g["tool"]
    return out


def decision(out):
    """What a run decided: its status, why, answer and checks (no times)."""
    keep = {k: out.get(k) for k in ("status", "why", "output", "checks", "missing", "fields_seen", "accepted")}
    return json.loads(json.dumps(keep, sort_keys=True, default=str))


def run_cases(tools):
    rows = []
    for tool, kind, value, params in CASES:
        out = F.run(shop(tool, kind), tools[tool], value, params=dict(params) or None)
        rows.append({"tool": tool, "shop": kind, "value": value, "params": params, "decision": decision(out)})
    return rows


def recorded():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_a_version_14_breadth_tool_decides_every_run_exactly_as_recorded():
    rec = recorded()
    assert {t["version"] for t in rec["tools"].values()} == {14}
    assert [(r["tool"], r["shop"], r["value"], r["params"]) for r in rec["runs"]] == \
        [(t, k, v, p) for t, k, v, p in CASES]
    now = run_cases(rec["tools"])
    for was, got in zip(rec["runs"], now):
        assert got["decision"] == was["decision"], (was["tool"], was["shop"], was["value"], got["decision"]["why"],
                                                     was["decision"]["why"])


def _by(rows, tool, kind, value, params=None):
    return next(r["decision"] for r in rows if (r["tool"], r["shop"], r["value"]) == (tool, kind, value) and
                (params is None or r["params"] == params))


def test_a_version_15_breadth_tool_reads_the_sites_own_evidence():
    tools = compile_tools()
    assert {t["version"] for t in tools.values()} == {C.TOOL_VERSION} and C.TOOL_VERSION >= 15
    rows = run_cases(tools)
    old = {(r["tool"], r["shop"], r["value"], json.dumps(r["params"])): r["decision"] for r in recorded()["runs"]}

    def was(tool, kind, value, params=None):
        return old[(tool, kind, value, json.dumps(params or {}))]
    akku = [f"{F.BASE}/p/{c[0]}" for c in F.Shop().hits("Akku")]
    # "Seite N" page links: version 15 numbers the pager by the page links; version 14 stopped after page 1
    d = _by(rows, "param", "seite", "Akku")
    assert d["status"] == "ok" and [r["url"] for r in d["output"]["results"]] == akku, d["why"]
    assert len(was("param", "seite", "Akku", {"n": 20})["output"]["results"]) == 2
    # the next-page word skips a page; the page number is the next page
    d = _by(rows, "cursor", "textnum", "Akku")
    assert [r["url"] for r in d["output"]["results"]] == akku, (d["status"], d["why"])
    assert [r["url"] for r in was("cursor", "textnum", "Akku", {"n": 20})["output"]["results"]] != akku
    # the total beside the quoted input
    d = _by(rows, "param", "echo", "Akku")
    assert d["checks"]["paged"]["stated_total"] == 7 and d["output"]["total"] == 7, d["checks"]["paged"]
    assert was("param", "echo", "Akku", {"n": 20})["checks"]["paged"]["stated_total"] is None
    # the date in the card's markup
    d = _by(rows, "newest", "markup", "Bosch")
    assert d["status"] == "ok" and all(r.get("date") for r in d["output"]["results"]), d["why"]
    assert was("newest", "markup", "Bosch")["status"] == "incomplete"
    # a numeric date before a month name: the "Stand" dates run the other way, so the sort check fails
    d = _by(rows, "newest", "mixed", "Bosch")
    assert d["status"] == "failed" and d["why"].startswith("sort:"), d["why"]
    assert was("newest", "mixed", "Bosch")["status"] == "ok"
    # a sort value holding "price" inside a longer word names no price sort: the compiled order is checked
    d = _by(rows, "newest", "plain", "Bosch", {"sort": "lowpricefirst"})
    assert "date" in d["why"] and "price" not in d["why"], d["why"]
    assert "price" in was("newest", "plain", "Bosch", {"sort": "lowpricefirst"})["why"]


def test_a_heal_reads_the_next_page_and_a_sort_option_by_the_current_rules_for_every_version():
    # a heal is no replay: it re-derives a step from the page now, the page's structure first, whatever the version;
    # a page mechanism it reads replays by its own URLs
    from anyuse.execution import repair_relocate as R
    base = "https://www.shop.test"
    read = {"url": base + "/search?q=akku", "dom": "<html><body></body></html>", "elements": [
        {"tag": "a", "text": "Weiter", "href": base + "/search?q=akku&cursor=c3", "area": "main"},
        {"tag": "a", "text": "2", "href": base + "/search?q=akku&page=2", "area": "main"}]}
    sort = {"url": base + "/search?q=akku", "dom": "<html></html>",
            "elements": [{"tag": "select", "options": [{"text": "Relevanz"}, {"text": "lowpricefirst"}]}]}
    for version in (14, 15):
        paged = {"version": version, "site": "www.shop.test", "workflow": "paged_search", "region": {},
                 "pager": {"mode": "param", "key": "p", "first": 1, "stride": 1}}
        got = R.candidates("pager", paged, "akku", {"result": read})
        assert [c["patch"]["ops"] for c in got] == [[{"op": "rename_url_param", "scope": "pager", "from": "p",
                                                       "to": "page"}]], (version, got)
        filtered = {"version": version, "site": "www.shop.test", "workflow": "filtered_search",
                    "refine": {"mode": "controls", "controls": [{"tag": "select", "option_text": "Preis aufsteigend"}]},
                    "applied": [{"kind": "sort", "key": "price_asc", "mode": "controls"}]}
        assert R.candidates("refine", filtered, "akku", {"result": sort}) == [], version


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
