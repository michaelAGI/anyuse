"""HTML5 implied end tags in the html_extract tree builder.

Server HTML may leave out the end tags the HTML standard lets it omit
(``<li>a<li>b``, ``<p>`` before a block, table cells and rows, options). A
browser closes those elements itself; with ``implied_end_tags=True`` the tree
builder does the same, so the rows of a list are siblings and the
sibling-group search finds them. The option is off by default: a DOM snapshot
is already the browser's tree and is read as it is.

Offline: synthetic markup only. No desktop, no network.
"""
from __future__ import annotations

import unittest

from anyuse.execution import html_extract as X

ROWS = '<ul class=r><li><a>Alpha</a> one<li><a>Beta</a> two<li><a>Gamma</a> three</ul>'


def build(markup: str, visible_only: bool = False,
          implied_end_tags: bool = True) -> X._TreeBuilder:
    builder = X._TreeBuilder(visible_only=visible_only, implied_end_tags=implied_end_tags)
    builder.feed(markup)
    builder.close()
    return builder


def parse(markup: str, **kw) -> X.Node:
    """The tree of a raw server response body: the implied end tags on."""
    return X.parse(markup, implied_end_tags=True, **kw)


def tags(node: X.Node):
    return [c.tag for c in node.children]


def dump(node: X.Node):
    return (node.tag, node.classes, node.node_id, list(node._text), list(node._text_at),
            [dump(c) for c in node.children])


def only(root: X.Node, tag: str) -> X.Node:
    found = [n for n in root.walk() if n.tag == tag]
    assert len(found) == 1, (tag, len(found))
    return found[0]


class ListItems(unittest.TestCase):

    def test_unclosed_li_are_siblings(self):
        ul = only(parse(ROWS), "ul")
        self.assertEqual(tags(ul), ["li", "li", "li"])
        sel, cov, vals = X.derive_selector_repeated(ROWS, ["Alpha", "Beta", "Gamma"],
                                                    implied_end_tags=True)
        self.assertEqual(cov, 1.0)
        self.assertEqual(vals, ["Alpha", "Beta", "Gamma"])
        self.assertTrue(X.selector_is_one_list(ROWS, "ul.r li", implied_end_tags=True))

    def test_the_default_keeps_the_old_nesting(self):
        # without the rules each li opens inside the one before it
        ul = only(X.parse(ROWS), "ul")
        self.assertEqual(tags(ul), ["li"])
        self.assertFalse(X.selector_is_one_list(ROWS, "ul.r li"))
        self.assertEqual(build(ROWS, implied_end_tags=False).implied, {})

    def test_nested_list_stays_nested(self):
        root = parse("<ul><li>a<ul><li>a1<li>a2</ul><li>b</ul>")
        outer = [n for n in root.walk() if n.tag == "ul"][0]
        self.assertEqual(tags(outer), ["li", "li"])
        inner = outer.children[0].children[0]
        self.assertEqual((inner.tag, tags(inner)), ("ul", ["li", "li"]))

    def test_li_closes_through_a_div(self):
        ul = only(parse("<ul><li><div>x<li>y</ul>"), "ul")
        self.assertEqual(tags(ul), ["li", "li"])
        self.assertEqual([c.text() for c in ul.children], ["x", "y"])

    def test_li_does_not_close_across_a_section(self):
        # section is a special element other than address, div or p
        section = only(parse("<ol><li>a<section><li>b</section></ol>"), "section")
        self.assertEqual(tags(section), ["li"])

    def test_dt_and_dd_are_siblings(self):
        dl = only(parse("<dl><dt>a<dd>1<dt>b<dd>2<dt>c<dd>3</dl>"), "dl")
        self.assertEqual(tags(dl), ["dt", "dd", "dt", "dd", "dt", "dd"])

    def test_li_closes_an_open_p(self):
        ul = only(parse("<ul><li><p>one<li><p>two</ul>"), "ul")
        self.assertEqual(tags(ul), ["li", "li"])
        self.assertEqual([tags(li) for li in ul.children], [["p"], ["p"]])


class Paragraphs(unittest.TestCase):

    def test_block_start_closes_p(self):
        div = parse("<div><p>one<p>two<div>3</div></div>").children[0]
        self.assertEqual(tags(div), ["p", "p", "div"])

    def test_inline_stays_inside_p(self):
        p = only(parse("<div><p>one <span>two</span> three<div>4</div></div>"), "p")
        self.assertEqual(tags(p), ["span"])
        self.assertEqual(p.text(), "one two three")

    def test_button_scope_keeps_the_p_open(self):
        root = parse("<p>a<button><div>b</div></button>")
        p = only(root, "p")
        self.assertEqual(tags(p), ["button"])
        self.assertEqual(tags(p.children[0]), ["div"])

    def test_heading_closes_p_and_an_open_heading(self):
        body = parse("<body><p>intro<h2>One<h3>Two</body>").children[0]
        self.assertEqual(tags(body), ["p", "h2", "h3"])

    def test_table_does_not_close_p(self):
        # a table closes p only in no-quirks mode; left out
        p = only(parse("<p>a<table><tr><td>b</table>"), "p")
        self.assertEqual(tags(p), ["table"])

    def test_a_block_in_a_table_cell_keeps_an_outer_p_open(self):
        # the cell bounds the p's scope: the div closes nothing outside the cell
        p = only(parse("<p>intro<table><tr><td><div>x</div><td>y</table>"), "p")
        tr = only(p, "tr")
        self.assertEqual(tags(tr), ["td", "td"])
        self.assertEqual(tags(tr.children[0]), ["div"])


class Tables(unittest.TestCase):

    def test_unclosed_cells_and_rows(self):
        table = only(parse("<table><tr><td>a<td>b<tr><td>c<td>d<tr><td>e<td>f</table>"),
                     "table")
        self.assertEqual(tags(table), ["tr", "tr", "tr"])
        self.assertEqual([tags(tr) for tr in table.children], [["td", "td"]] * 3)

    def test_th_and_td_share_a_row(self):
        tr = parse("<table><tr><th>h<td>v</table>").children[0].children[0]
        self.assertEqual(tags(tr), ["th", "td"])

    def test_sections_close_each_other(self):
        table = only(parse("<table><thead><tr><th>h<tbody><tr><td>a<tr><td>b"
                             "<tfoot><tr><td>f</table>"), "table")
        self.assertEqual(tags(table), ["thead", "tbody", "tfoot"])
        self.assertEqual(tags(table.children[1]), ["tr", "tr"])

    def test_nested_table_stays_in_its_cell(self):
        root = parse("<table><tr><td>a<table><tr><td>x<td>y</table><td>b</table>")
        outer = [n for n in root.walk() if n.tag == "table"][0]
        tr = outer.children[0]
        self.assertEqual(tags(tr), ["td", "td"])
        inner = tr.children[0].children[0]
        self.assertEqual(inner.tag, "table")
        self.assertEqual(tags(inner.children[0]), ["td", "td"])

    def test_a_section_in_a_nested_table_stays_there(self):
        root = parse("<table><tr><td>a<table><thead><tr><th>h<tbody><tr><td>x</table>"
                       "<td>b</table>")
        outer = [n for n in root.walk() if n.tag == "table"][0]
        self.assertEqual(tags(outer.children[0]), ["td", "td"])
        inner = outer.children[0].children[0].children[0]
        self.assertEqual((inner.tag, tags(inner)), ("table", ["thead", "tbody"]))

    def test_colgroup_and_caption_close(self):
        table = only(parse("<table><caption>cap<colgroup><col><col><tr><td>a</table>"),
                     "table")
        self.assertEqual(tags(table), ["caption", "colgroup", "tr"])
        self.assertEqual(tags(table.children[1]), ["col", "col"])


class Options(unittest.TestCase):

    def test_options_and_optgroups(self):
        sel = only(parse("<select><optgroup label=a><option>1<option>2"
                           "<optgroup label=b><option>3</select>"), "select")
        self.assertEqual(tags(sel), ["optgroup", "optgroup"])
        self.assertEqual([tags(g) for g in sel.children], [["option", "option"], ["option"]])


class HeadAndRuby(unittest.TestCase):

    def test_body_content_closes_an_open_head(self):
        # the head's end tag is optional; its text is never read, so a body
        # left inside it would lose every word
        root = parse("<html><head><title>T</title><meta charset=utf-8>"
                       "<body><ul class=r><li>one<li>two<li>three</ul>")
        html = root.children[0]
        self.assertEqual(tags(html), ["head", "body"])
        self.assertEqual(root.text(), "one two three")

    def test_text_closes_an_open_head(self):
        root = parse("<html><head><title>T</title>Hello <b>world</b>")
        self.assertEqual(tags(root.children[0]), ["head", "b"])
        self.assertEqual(root.text(), "Hello world")

    def test_ruby_annotations(self):
        ruby = only(parse("<ruby>a<rt>x<rp>(<rt>y<rp>)</ruby>"), "ruby")
        self.assertEqual(tags(ruby), ["rt", "rp", "rt", "rp"])

    def test_rt_and_rp_stay_in_an_open_rtc(self):
        rtc = only(parse("<ruby>a<rtc>b<rt>c<rp>(</ruby>"), "rtc")
        self.assertEqual(tags(rtc), ["rt", "rp"])

    def test_rt_outside_a_ruby_closes_nothing(self):
        # without a ruby in scope an rt start tag pops no li or p
        ul = only(parse("<ul><li>a<rt>b</rt> c<li>d</ul>"), "ul")
        self.assertEqual(tags(ul), ["li", "li"])
        self.assertEqual(tags(ul.children[0]), ["rt"])


class ForeignContent(unittest.TestCase):

    ICON = "<svg viewBox='0 0 9 9'>" + "<path d='M0 0h1'/>" * 200 + "</svg>"

    def test_self_closing_svg_children_are_siblings(self):
        builder = build("<div class=icon>" + self.ICON + "</div>")
        svg = only(builder.root, "svg")
        self.assertEqual(tags(svg), ["path"] * 200)
        self.assertEqual(builder.implied, {"path": 200})

    def test_a_long_icon_hits_the_depth_cap_without_the_rules(self):
        # each <path/> opened inside the one before it
        with self.assertRaises(X.HtmlLimitError):
            X.parse(self.ICON)

    def test_self_closing_svg_and_math_close(self):
        p = only(parse("<p>a<svg/>b<math/>c</p>"), "p")
        self.assertEqual(tags(p), ["svg", "math"])
        self.assertEqual([c.children for c in p.children], [[], []])
        self.assertEqual(p.text(), "a b c")

    def test_math_children_close_too(self):
        math = only(parse("<math><mrow><mi>x</mi><mspace width='1em'/><mn>2</mn></mrow></math>"),
                    "mrow")
        self.assertEqual(tags(math), ["mi", "mspace", "mn"])

    def test_the_slash_on_an_html_element_is_ignored(self):
        # outside svg and math a browser ignores the slash: the div stays open
        div = only(parse("<div class=a/><span>x</span></div>"), "div")
        self.assertEqual(tags(div), ["span"])

    def test_a_void_element_in_svg_closes_nothing_else(self):
        # a void element is never left open, so its slash has nothing to close
        g = only(parse("<svg><g><input/><path/></g></svg>"), "g")
        self.assertEqual(tags(g), ["input", "path"])

    def test_html_inside_foreign_object_ignores_the_slash(self):
        div = only(parse("<svg><foreignObject><div/><span>x</span></foreignObject></svg>"),
                   "div")
        self.assertEqual(tags(div), ["span"])

    def test_explicit_end_tags_in_svg_give_the_same_tree(self):
        closed = "<svg><g><path d='a'></path><path d='b'></path></g></svg>"
        self.assertEqual(build(closed).implied, {})
        self.assertEqual(dump(build(closed).root),
                         dump(build(closed, implied_end_tags=False).root))


class Limits(unittest.TestCase):

    LONG = "<ul>" + "".join(f"<li><a>row {i}</a>" for i in range(300)) + "</ul>"

    def test_long_unclosed_list_parses(self):
        ul = only(parse(self.LONG), "ul")
        self.assertEqual(len(ul.children), 300)

    def test_long_unclosed_list_hits_the_depth_cap_without_the_rules(self):
        with self.assertRaises(X.HtmlLimitError):
            X.parse(self.LONG)

    def test_real_nesting_still_hits_the_depth_cap(self):
        # nothing closes a div implicitly, so 200 open divs stay 200 deep
        with self.assertRaises(X.HtmlLimitError):
            parse("<div>" * 200 + "x")

    def test_long_unclosed_table_and_select_parse(self):
        rows = "<table>" + "<tr><td>a<td>b" * 200 + "</table>"
        self.assertEqual(len(only(parse(rows), "table").children), 200)
        opts = "<select>" + "<option>v" * 200 + "</select>"
        self.assertEqual(len(only(parse(opts), "select").children), 200)


class Unchanged(unittest.TestCase):

    # the well-formed page of test_html_rung
    PAGE = """<!DOCTYPE html><html><head><title>Search</title>
<script>var x = "Alpha Pkg";</script></head><body>
<header><h1>Results for <span class="q">Alpha Pkg</span></h1></header>
<main><div class="results">
  <li class="row"><a class="title" href="/a">Alpha Pkg</a><span class="desc">first one</span></li>
  <li class="row"><a class="title" href="/b">Beta Pkg</a><span class="desc">second one</span></li>
  <li class="row"><a class="title" href="/c">Gamma Pkg</a><span class="desc">third one</span></li>
</div></main>
<footer><a href="/x">Alpha Pkg</a></footer></body></html>"""

    def test_closed_page_gives_the_same_tree_and_fires_nothing(self):
        PAGE = self.PAGE
        builder = build(PAGE)
        self.assertEqual(builder.implied, {})
        old = build(PAGE, implied_end_tags=False)
        self.assertEqual(dump(builder.root), dump(old.root))

    def test_the_default_closes_nothing(self):
        # off, no rule fires on markup the rules would change
        for markup in (ROWS, "<div><p>one<div>two</div></div>",
                       "<table><tr><td>a<td>b<tr><td>c</table>",
                       "<select><option>a<option>b</select>",
                       "<html><head><title>T</title>Hello <b>world</b>",
                       "<svg><path d='a'/><path d='b'/></svg>"):
            self.assertEqual(build(markup, implied_end_tags=False).implied, {}, markup)

    def test_fired_rules_are_counted(self):
        builder = build(ROWS)
        self.assertEqual(builder.implied, {"li": 2})

    def test_visible_only_drops_a_hidden_unclosed_row(self):
        root = parse("<ul><li hidden>a<li>b<li>c</ul>", visible_only=True)
        ul = only(root, "ul")
        self.assertEqual([c.text() for c in ul.children], ["b", "c"])

    def test_unclosed_header_still_nests(self):
        # header closes a p, but nothing closes a header except its end tag
        root = parse("<body><header><a>Home</a><main><ul><li>x</ul></main></body>")
        header = only(root, "header")
        self.assertEqual(tags(header), ["a", "main"])


if __name__ == "__main__":
    unittest.main()
