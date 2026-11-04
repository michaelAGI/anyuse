"""The result-region model (html_extract.derive_result_region), the
fallback when no repeated group of identical siblings changed, and the
selector steps it needs (``>``, ``tag#id``).

Positive cases are synthetic (heterogeneous cards, one short answer); the
negative cases are saved bench pages where the region model must decline: an
empty results container under a heading that echoes the query (gapminder), a
page that did not change (curseforge).
"""
from __future__ import annotations

import gzip
import json
import os
import unittest

from anyuse.execution import attach as A
from anyuse.execution import html_extract as X
from anyuse.execution.learn_loop import derive_ground_truth

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def recordings(name):
    with gzip.open(os.path.join(HERE, "testdata", "web_sessions", name), "rt") as fh:
        f = json.load(fh)
    return [A.session_to_recording(s, "q", v, f"attach{i}")
            for i, (s, v) in enumerate(zip(f["sessions"], f["values"]))]


def cards(q, n):
    kinds = ['<article class="card video"><h3><a>{t}</a></h3><p>clip about {t}</p></article>',
             '<div class="hit"><a>{t}</a><span>{t} and more text</span></div>',
             '<section><strong>{t}</strong><em>reference</em></section>']
    body = "".join(kinds[i % 3].format(t=f"{q} item {i}") for i in range(n))
    side = '<aside><ul><li><a>About</a></li><li><a>Help</a></li><li><a>Contact</a></li></ul></aside>'
    filler = "<p>" + "static site text " * 40 + "</p>"
    return (f'<html><body><header><a>Home</a></header><main><h1>Results for {q}</h1>'
            f'<div id="results">{body}</div>{filler}</main>{side}</body></html>')


class Region(unittest.TestCase):

    def test_heterogeneous_cards_form_one_region(self):
        # no two siblings share (tag, classes): the repeated-group rule finds nothing
        self.assertEqual(X.derive_list_items([cards("alpha", 3), cards("beta", 3)])[0], "")
        sel, items, how = X.derive_result_region([("alpha", cards("alpha", 3)),
                                                  ("beta", cards("beta", 3))])
        self.assertEqual(sel, "div#results > *", how)
        self.assertEqual(len(items), 3)
        self.assertTrue(all("alpha" in i for i in items))
        self.assertEqual(len(X.extract(cards("gamma", 5), sel)), 5)

    def test_a_single_answer_is_raised_to_its_region(self):
        sel, items, _ = X.derive_result_region([("alpha", cards("alpha", 1)),
                                                ("beta", cards("beta", 4))])
        self.assertTrue(sel.endswith(" > *"))
        self.assertTrue(any("alpha" in i for i in items))

    def test_ground_truth_uses_the_region_when_no_group_changed(self):
        recs = [{"param": {"value": v}, "dom": cards(v, 3), "ui": {"text": v},
                 "final_url": f"https://ex.org/s?q={v}", "network": []}
                for v in ("alpha", "beta")]
        self.assertEqual(derive_ground_truth(recs), "div#results > *")
        self.assertTrue(recs[0]["ground_truth_visible_only"])
        self.assertIn("result region", recs[0]["ground_truth_how"])

    def test_saved_gapminder_heading_echo_is_no_region(self):
        # ARM S: 'Search Results for: <value>' over empty containers
        recs = recordings("gapminder_echo_only.sessions.json.gz")
        pages = [(r["param"]["value"], r["dom"]) for r in recs]
        self.assertEqual(X.derive_result_region(pages)[0], "")
        self.assertEqual(derive_ground_truth(recs), "")

    def test_saved_unchanged_pages_are_no_region(self):
        recs = recordings("curseforge_unchanged.sessions.json.gz")
        pages = [(r["param"]["value"], r["dom"]) for r in recs]
        self.assertEqual(X.derive_result_region(pages)[0], "")

    def test_an_input_bound_region_in_page_furniture_is_not_refused(self):
        # the only region that answers the input sits in an <aside>: its records name the
        # value on every page and change with it, so the landmark does not refuse it
        def page(q):
            return (f'<html><body><main><p>{"static " * 50}</p></main><aside id="box">'
                    f'<div><a>{q} one</a></div><p>{q} two</p><span>{q} three</span>'
                    f'</aside></body></html>')
        sel, items, how = X.derive_result_region([("alpha", page("alpha")), ("beta", page("beta"))])
        self.assertTrue(sel.startswith("aside"), how)
        self.assertEqual(items, ["alpha one", "alpha two", "alpha three"])
        self.assertIn("furniture", how)

    def test_a_furniture_box_that_names_the_value_once_is_still_refused(self):
        # a "recent searches" box: one entry names this search, the others earlier ones
        def page(q):
            return (f'<html><body><main><p>{"static " * 50}</p></main><aside id="recent">'
                    f'<div><a>{q}</a></div><p>older search one</p><span>older search two</span>'
                    f'</aside></body></html>')
        self.assertEqual(X.derive_result_region([("alpha", page("alpha")),
                                                 ("beta", page("beta"))])[0], "")

    def test_furniture_breaks_the_tie_between_two_input_bound_regions(self):
        def page(q):
            rows = "".join(f"<section><b>{q} hit {i}</b><i>about {q} {i * 7}</i></section>"
                           for i in range(4))
            side = f"<aside><div><a>{q} tip</a></div><p>{q} hint</p></aside>"
            return (f'<html><body><main><div id="hits">{rows}</div><p>{"static " * 40}</p>'
                    f'</main>{side}</body></html>')
        sel, items, how = X.derive_result_region([("alpha", page("alpha")), ("beta", page("beta"))])
        self.assertEqual(len(items), 4, how)
        self.assertTrue(all(i.startswith("alpha hit") for i in items), items)
        self.assertNotIn("furniture", how)

    def test_a_breadcrumb_every_page_repeats_does_not_stretch_the_region(self):
        # a breadcrumb under a class name no list knows: the comparison of the pages finds it
        # (the same line on every page, its value masked), so the region stays the result list
        def page(q, rows):
            hits = "".join(f"<div><a>{q} {r}</a><span>{len(r) * 3} stars</span></div>"
                           for r in rows)
            return (f'<html><body><div id="kopf"><p>{"static " * 60}</p></div>'
                    f'<div id="inhalt"><div class="brotkrumen">Start › Suche › {q}</div>'
                    f'<div class="treffer">{hits}</div></div></body></html>')
        pages = [("alpha", page("alpha", ["eins", "zwei", "drei", "vier"])),
                 ("beta", page("beta", ["fuenf", "sechs", "sieben", "acht"]))]
        sel, items, how = X.derive_result_region(pages)
        self.assertEqual(sel, "div.treffer > *", how)
        self.assertEqual(len(items), 4)

    def test_a_breadcrumb_inside_the_result_region_is_no_record(self):
        # the result container also holds a <nav> breadcrumb that names the value: outside
        # page furniture the region's records stay its cards
        def page(q, n):
            crumb = f'<nav class="breadcrumb"><a>Home</a> / <a>Search</a> / {q}</nav>'
            body = "".join(f'<div class="hit"><a>{q} item {i}</a><span>{q} {i} text</span></div>'
                           if i % 2 else f'<section><b>{q} item {i}</b><i>reference</i></section>'
                           for i in range(n))
            return (f'<html><body><main><div id="results">{crumb}{body}</div>'
                    f'<p>{"static site text " * 40}</p></main></body></html>')
        sel, items, how = X.derive_result_region([("alpha", page("alpha", 1)),
                                                  ("beta", page("beta", 3))])
        self.assertEqual(sel, "div#results > *", how)
        self.assertEqual(items, ["alpha item 0 reference"])


class SelectorSteps(unittest.TestCase):

    DOM = ('<div id="a" class="x"><p>one</p><span><p>deep</p></span></div>'
           '<div id="b" class="x"><p>two</p></div>')

    def test_child_combinator(self):
        root = X.parse(self.DOM)
        self.assertEqual([n.text() for n in X.select(root, "div.x > p")], ["one", "two"])
        self.assertEqual([n.text() for n in X.select(root, "div.x p")], ["one", "deep", "two"])
        self.assertEqual([n.text() for n in X.select(root, "div.x > *")],
                         ["one", "deep", "two"])

    def test_id_step(self):
        root = X.parse(self.DOM)
        self.assertEqual([n.text() for n in X.select(root, "div#b.x > p")], ["two"])
        self.assertEqual(X.parse_selector("div#b.x"), [("div", ("x", "#b"))])
        # old selectors are unchanged
        self.assertEqual(X.parse_selector("div.md:flex li"), [("div", ("md:flex",)), ("li", ())])


if __name__ == "__main__":
    unittest.main()
