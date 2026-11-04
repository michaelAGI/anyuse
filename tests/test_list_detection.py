"""Result-list detection on the compiler side (offline, saved pages).

Fixtures under ``testdata/web_sessions/`` are pages captured by a live bench run, trimmed to what
the rules read. No browser, no network.
"""
from __future__ import annotations

import gzip
import json
import os
import unittest

from anyuse.execution import attach as A
from anyuse.execution import html_extract as X
from anyuse.execution.learn_loop import derive_ground_truth, no_list_diagnosis

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fixture(name: str) -> dict:
    with gzip.open(os.path.join(HERE, "testdata", "web_sessions", name), "rt") as fh:
        return json.load(fh)


def recordings(name: str):
    f = fixture(name)
    return [A.session_to_recording(s, "q", v, f"attach{i}")
            for i, (s, v) in enumerate(zip(f["sessions"], f["values"]))]


def page(query: str, rows, side, main=True, aside_tag="aside"):
    lis = "".join(f'<li class="hit"><a>{r}</a> <span>result about {r}</span></li>' for r in rows)
    box = "".join(f'<li class="rec"><a>{s} {s} {s}</a> <span>{("long sidebar text " + s + " ") * 3}'
                  '</span></li>' for s in side)
    wrap = ("<main>", "</main>") if main else ("<div>", "</div>")
    return (f"<html><body><header><a>Home</a></header>{wrap[0]}<h1>Results for {query}</h1>"
            f'<ul class="results">{lis}</ul>{wrap[1]}'
            f'<{aside_tag} class="box"><ul class="recent">{box}</ul></{aside_tag}>'
            "<footer><a>Terms</a></footer></body></html>")


class FurnitureAndNaming(unittest.TestCase):

    def test_saved_opendata_nav_facets_lose_to_the_list_naming_the_query(self):
        # opendata.swiss (ARM L pair): the old rule picked the format
        # facets in a <nav> ("HTML 9", "PDF 6") as the result list
        f = fixture("opendata_swiss_facets.json.gz")
        sel, items = X.derive_list_items(f["pages"], values=f["values"])
        self.assertIn("li.dataset-item", sel)
        self.assertNotIn("nav", sel)
        self.assertTrue(any("Darlehen" in i for i in items))

    def test_saved_opendata_without_values_still_skips_the_nav(self):
        f = fixture("opendata_swiss_facets.json.gz")
        sel, _items = X.derive_list_items(f["pages"])
        self.assertTrue(sel)
        self.assertFalse(sel.startswith("nav "), sel)

    def test_saved_quotationspage_keeps_the_quote_field(self):
        # the author field (dd) names an author query; it is a second
        # FIELD of the same dt/dd rows, so naming must not replace the quotes
        recs = recordings("quotationspage_fields.sessions.json.gz")
        self.assertEqual(derive_ground_truth(recs), "td dl dt.quote")

    def test_changed_aside_box_loses_to_the_main_list(self):
        a = page("alpha", ["alpha one", "alpha two", "alpha three"],
                 ["x1", "x2", "x3", "x4"])
        b = page("beta", ["beta one", "beta two", "beta three"],
                 ["y1", "y2", "y3", "y4"])
        sel, items = X.derive_list_items([a, b])
        self.assertIn("li.hit", sel)
        self.assertEqual(items[0].split()[0:2], ["alpha", "one"])

    def test_sidebar_class_counts_as_furniture(self):
        a = page("alpha", ["alpha one", "alpha two", "alpha three"], ["x1", "x2", "x3"],
                 aside_tag='div id="sidebar"').replace("</div id=\"sidebar\">", "</div>")
        b = page("beta", ["beta one", "beta two", "beta three"], ["y1", "y2", "y3"],
                 aside_tag='div id="sidebar"').replace("</div id=\"sidebar\">", "</div>")
        sel, _ = X.derive_list_items([a, b], values=["alpha", "beta"])
        self.assertIn("li.hit", sel)

    def test_rows_that_name_the_query_are_never_dropped_as_furniture(self):
        # the result rows sit under a class a list calls furniture; a "popular" box outside
        # it changes too but does not name the query
        def pg(q, pop):
            hits = "".join(f'<li class="hit"><a>{q} {n}</a> <span>{q} {n} details</span></li>'
                           for n in ("uno", "dos", "tres"))
            box = "".join(f'<li class="pop"><a>{p}</a></li>' for p in pop)
            return (f'<html><body><div class="sidebar"><ul class="hits">{hits}</ul></div>'
                    f'<div class="col"><ul class="popular">{box}</ul>'
                    f'<p>{"static shop text " * 30}</p></div></body></html>')
        a = pg("lampara", ["mesa", "silla", "sofa"])
        b = pg("cortina", ["cama", "espejo", "alfombra"])
        sel, items = X.derive_list_items([a, b], values=["lampara", "cortina"])
        self.assertIn("li.hit", sel)
        self.assertTrue(items[0].startswith("lampara uno"), items)
        # without the values nothing is known to answer the input: furniture decides as before
        sel, _items = X.derive_list_items([a, b])
        self.assertIn("li.pop", sel)
        # typed pages read the same way
        sel, _items, _how = X.derive_typed_items([("lampara", a), ("cortina", b)])
        self.assertIn("li.hit", sel)

    def test_furniture_breaks_the_tie_between_two_groups_that_name_the_query(self):
        # a "related searches" box in an <aside> names the query too and changes more than
        # the result list (whose first row is pinned): the landmark still decides
        def pg(q):
            rows = "<li class='hit'><a href='/p/top'>Aanbieding van de week</a></li>" + "".join(
                f"<li class='hit'><a href='/p/{q}-{i}'>{q} model {i}</a></li>" for i in range(2))
            rel = "".join(f"<li class='rel'><a href='/s/{q}+{w}'>{q} {w}</a></li>"
                          for w in ("goedkoop", "tweedehands", "nieuw", "kinder", "elektrisch"))
            return (f"<html><body><main><ul class='results'>{rows}</ul></main>"
                    f"<aside><ul class='related'>{rel}</ul></aside></body></html>")
        vals = ["fiets", "lamp"]
        sel, _items = X.derive_list_items([pg(v) for v in vals], values=vals)
        self.assertIn("li.hit", sel)
        sel, _items, _how = X.derive_typed_items([(v, pg(v)) for v in vals])
        self.assertIn("li.hit", sel)

    def test_only_furniture_changed_falls_back_to_it(self):
        # nothing outside the furniture changed: the old answer stands
        a = page("q", ["same one", "same two", "same three"], ["x1", "x2", "x3"])
        b = page("q", ["same one", "same two", "same three"], ["y1", "y2", "y3"])
        sel, _ = X.derive_list_items([a, b])
        self.assertIn("li.rec", sel)

    def test_typed_region_prefers_main_over_aside(self):
        before = page("", ["all one", "all two", "all three"], ["z1", "z2", "z3"])
        a = page("alpha", ["alpha one", "alpha two", "alpha three"], ["x1", "x2", "x3"])
        b = page("beta", ["beta one", "beta two", "beta three"], ["y1", "y2", "y3"])
        sel, _items, _how = X.derive_typed_items([("alpha", a), ("beta", b)], before=before)
        self.assertIn("li.hit", sel)

    def test_unclosed_header_is_not_furniture(self):
        # a page that never closes <header> nests everything inside it
        a = page("alpha", ["alpha one", "alpha two", "alpha three"], []).replace(
            "<header><a>Home</a></header>", "<header><a>Home</a>")
        b = page("beta", ["beta one", "beta two", "beta three"], []).replace(
            "<header><a>Home</a></header>", "<header><a>Home</a>")
        sel, _ = X.derive_list_items([a, b])
        self.assertIn("li.hit", sel)


class TierOneTypedRegion(unittest.TestCase):
    """An in-page search: the result URL does not carry the value, the capture
    holds the page before typing; tier 1 now uses the typed changed region."""

    ROWS = ["alpha parser", "beta server", "gamma client", "delta parser", "epsilon tool"]

    def dom(self, show=None):
        lis = []
        for r in self.ROWS:
            hide = show is not None and not any(w in r for w in show.split())
            lis.append(f'<li class="pkg"{" hidden" if hide else ""}><a>{r}</a></li>')
        return f'<html><body><input id="q"><ul class="list">{"".join(lis)}</ul></body></html>'

    def session(self, value, show):
        return {"id": value, "task": "t", "input": {"name": "q", "value": value},
                "snapshots": [{"url": "about:blank", "dom": "<html></html>", "text": ""},
                              {"url": "https://ex.org/list", "dom": self.dom(), "text": "list"},
                              {"url": "https://ex.org/list", "dom": self.dom(show),
                               "text": f"list {value}"}],
                "network": []}

    def test_before_dom_is_the_page_before_typing(self):
        rec = A.session_to_recording(self.session("parser", "parser"), "q", "parser", "a0")
        self.assertEqual(rec["before_dom"], self.dom())

    def test_navigation_search_keeps_no_before_dom(self):
        s = self.session("parser", "parser")
        s["snapshots"][-1]["url"] = "https://ex.org/list?q=parser"
        self.assertNotIn("before_dom", A.session_to_recording(s, "q", "parser", "a0"))

    def test_in_page_filter_yields_a_visible_only_ground_truth(self):
        recs = [A.session_to_recording(self.session(v, v), "q", v, f"a{i}")
                for i, v in enumerate(["parser", "server"])]
        sel = derive_ground_truth(recs)
        self.assertEqual(sel, "ul.list li.pkg")
        self.assertTrue(recs[0].get("ground_truth_visible_only"))
        self.assertEqual(recs[0]["ui"]["items"], ["alpha parser", "delta parser"])


class NamedNoListReasons(unittest.TestCase):

    def test_saved_curseforge_pages_are_unchanged(self):
        # ARM S: typing + Enter left the home page as it was
        recs = recordings("curseforge_unchanged.sessions.json.gz")
        self.assertEqual(derive_ground_truth(recs), "")
        self.assertEqual(recs[0]["no_list_class"], "unchanged")
        self.assertIn("(unchanged:", recs[0]["recorder_warning"])
        self.assertTrue(recs[0]["recorder_warning"].startswith(
            "no repeated group on the confirmed page family changed"))

    def test_saved_scryfall_empty_search_is_an_error_page(self):
        # ARM S: "Teenage" matched no card and was served as HTTP 404
        recs = recordings("scryfall_error_pages.sessions.json.gz")
        self.assertEqual(derive_ground_truth(recs), "")
        self.assertEqual(recs[0]["no_list_class"], "error_pages")
        self.assertIn("HTTP 404", recs[0]["recorder_warning"])

    def test_not_rendered_and_empty_results(self):
        def rec(v, text, dom):
            return {"param": {"value": v}, "ui": {"text": text}, "dom": dom}
        self.assertTrue(no_list_diagnosis([rec("a1", "Search results", "<p>1</p>"),
                                           rec("b2", "Search results page", "<p>2</p>")])
                        .startswith("not_rendered:"))
        self.assertTrue(no_list_diagnosis([rec("ab", "ab: No results found", "<p>1</p>"),
                                           rec("cd", "0 results for cd", "<p>2</p>")])
                        .startswith("empty_results:"))
        self.assertEqual(no_list_diagnosis([rec("ab", "ab one", "<p>1</p>"),
                                            rec("cd", "cd two", "<p>2</p>")]), "")


if __name__ == "__main__":
    unittest.main()
