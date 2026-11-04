"""Which HTML tree each path reads.

A raw server response body is parsed with the HTML5 implied end tags, as a
browser parses it: the HTML rung at compile, a compiled HTML template at call
time when the template carries ``html_parser: "html5"``, and step repair on the
pages it fetches itself. A template without the marker keeps the tree it was
proved on. A DOM snapshot is the browser's tree already, and a script can build
trees the HTML parser never would (a ``p`` holding a ``div``), so the snapshot
paths keep the default and read the snapshot as it is.

Offline: synthetic markup and an injected transport. No desktop, no network.
"""
import urllib.parse

from anyuse.execution import learn_loop as LL
from anyuse.execution import proof_split as PS
from anyuse.execution import result_contract as RC
from anyuse.execution import step_repair as SR
from anyuse.execution.api_compile import compile_html, compiled_record
from anyuse.execution.api_step import (ApiResponse, ApiStepExecutor, RequestTemplate,
                                       check_expect_html, load_command)
from anyuse.execution.cdp_replay import ready_read_passes
from anyuse.execution.html_extract import HTML5_PARSER, HtmlLimitError, extract, parse, select
from anyuse.execution.learn_loop import derive_ground_truth


def names(q, n=6):
    return [f"{q}-lib-{i}" for i in range(n)]


def cards_dom(q):
    """The serialised DOM of a script-built list: each p.card holds a div,
    which the HTML parser would never put inside a p."""
    rows = "".join(f'<p class="card"><div class="title">{n}</div><span>by {q}</span></p>'
                   for n in names(q, 4))
    return (f'<html><head></head><body><main><h1>{q}</h1><section class="list">{rows}'
            f'</section></main></body></html>')


def unclosed_page(q, count=6):
    """A raw server page that leaves out the optional end tags of its rows."""
    rows = "".join(f"<li class='hit'><a class='name' href='/p/{n}'>{n}</a> v1"
                   for n in names(q, count))
    return (f"<!DOCTYPE html><html><head><title>Search</title></head><body>"
            f"<header><a>Home</a></header><main><h1>Results for {q}</h1>"
            f"<ul class='hits'>{rows}</ul></main><footer><a>Terms</a></footer></body></html>")


def closed_page(q):
    rows = "".join(f"<tr class='row'><td><a class='name'>{n}</a></td><td class='d'>v1</td></tr>"
                   for n in names(q))
    return (f"<html><body><header><a>Home</a></header><main><h1>Results for {q}</h1>"
            f"<table class='results'><tbody>{rows}</tbody></table></main>"
            f"<footer><a>Terms</a></footer></body></html>")


class Site:
    """``https://ex.test/search?q=``: closed table rows, or after the drift
    the same results as a list without end tags."""

    def __init__(self, unclosed=False, rows=6):
        self.unclosed = unclosed
        self.rows = rows

    def send(self, method, url, headers, body, timeout_s):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("q", "")
        page = unclosed_page(q, self.rows) if self.unclosed else closed_page(q)
        return ApiResponse(200, {"content-type": "text/html; charset=utf-8"}, page)


#: more unclosed rows than html_extract.MAX_HTML_DEPTH: read without the
#: implied end tags each row opens inside the one before it, and the parser
#: refuses the page
DEEP = 150


def refused_without_rules(markup):
    try:
        parse(markup)
    except HtmlLimitError:
        return True
    return False


def html_template(selector, html_parser=""):
    return RequestTemplate(method="GET", url="https://ex.test/search", query={"q": "{q}"},
                           extract={"items": selector}, extract_kind="html", shape="list",
                           params=["q"], html_parser=html_parser,
                           expect={"status_in": [200], "content_type": "html",
                                   "selector": selector, "min_items": 1})


def run(template, site, value):
    return ApiStepExecutor(transport=site).execute(template, {"q": value})


def dom_recording(q):
    return {"param": {"name": "q", "value": q}, "dom": cards_dom(q), "ui": {"items": []},
            "network": []}


# ---------------------------------------------------------------------------
# DOM snapshots: read as they are
# ---------------------------------------------------------------------------

def test_a_dom_snapshot_keeps_a_p_that_holds_a_div():
    dom = cards_dom("alpha")
    titles = [n.text() for n in select(parse(dom), "p.card div.title")]
    assert titles == names("alpha", 4)
    assert extract(dom, "p.card div.title") == names("alpha", 4)
    # the parser's rules close the p at the div, so the selector would match nothing
    assert extract(dom, "p.card div.title", implied_end_tags=True) == []


def test_the_browser_replay_reads_the_snapshot_tree():
    ok, why = ready_read_passes(cards_dom("alpha"),
                                {"selector": "p.card div.title", "min_items": 4})
    assert ok, why


def test_ground_truth_on_dom_snapshots_keeps_the_cards():
    recs = [dom_recording("alpha"), dom_recording("beta")]
    sel = derive_ground_truth(recs)
    assert sel.endswith("p.card"), sel
    assert recs[0]["ui"]["items"] == [f"{n} by alpha" for n in names("alpha", 4)]


# ---------------------------------------------------------------------------
# the HTML rung: compile writes the marker, call time honours it
# ---------------------------------------------------------------------------

def raw_recording(q, body=""):
    url = f"https://ex.test/search?q={q}"
    body = body or unclosed_page(q)
    return {"param": {"name": "q", "value": q}, "ui": {"items": names(q), "text": ""},
            "network": [{"url": url, "method": "GET", "status": 200,
                         "mime": "text/html; charset=utf-8", "bytes": len(body),
                         "body": body}]}


def test_compile_writes_the_html5_marker():
    tmpl, why = compile_html(raw_recording("alpha"), names("alpha"), "q", "alpha")
    assert tmpl is not None, why
    assert tmpl.html_parser == HTML5_PARSER and tmpl.implied_end_tags
    assert tmpl.to_dict()["html_parser"] == HTML5_PARSER
    assert RequestTemplate.from_dict(tmpl.to_dict()).html_parser == HTML5_PARSER
    # compile and call time read one tree: the proved rows come back
    out = run(tmpl, Site(unclosed=True), "delta")
    assert out["success"], out.get("error")
    assert out["extracted"]["items"] == names("delta")


def deep_list_page(q):
    """DEEP rows without end tags, each row's text one name."""
    rows = "".join(f"<li class='hit'>{n}" for n in names(q, DEEP))
    return (f"<!DOCTYPE html><html><body><main><h1>Results for {q}</h1>"
            f"<ul class='hits'>{rows}</ul></main></body></html>")


class Serve:
    """``https://ex.test/search?q=``: ``page(q)`` for every query."""

    def __init__(self, page):
        self.page = page

    def send(self, method, url, headers, body, timeout_s):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("q", "")
        return ApiResponse(200, {"content-type": "text/html; charset=utf-8"}, self.page(q))


def test_compile_and_call_read_a_list_longer_than_the_depth_cap():
    body = deep_list_page("alpha")
    assert refused_without_rules(body)
    tmpl, why = compile_html(raw_recording("alpha", body), names("alpha", 20), "q", "alpha")
    assert tmpl is not None, why
    assert tmpl.implied_end_tags
    out = run(tmpl, Serve(deep_list_page), "delta")
    assert out["success"], out.get("error")
    assert out["extracted"]["items"] == names("delta", 50)


def test_a_template_without_the_marker_serialises_as_before():
    old = html_template("ul.hits li.hit").to_dict()
    assert "html_parser" not in old
    back = RequestTemplate.from_dict(old)
    assert back.html_parser == "" and not back.implied_end_tags
    assert back.to_dict() == old


def test_call_time_reads_the_tree_the_template_names():
    rows = [f"{n} v1" for n in names("delta")]
    marked = run(html_template("ul.hits li.hit", HTML5_PARSER), Site(unclosed=True), "delta")
    assert marked["success"], marked.get("error")
    assert marked["extracted"]["items"] == rows
    # without the marker each row nests in the one before it, as when it was proved
    plain = run(html_template("ul.hits li.hit"), Site(unclosed=True), "delta")
    assert plain["success"], plain.get("error")
    assert plain["extracted"]["items"][0] == " ".join(rows)
    assert plain["extracted"]["items"][-1] == rows[-1]


def test_the_postcondition_reads_the_template_tree():
    page = "<ul class=r>" + "<li class=x><span class=n>a</span>" * 200 + "</ul>"
    expect = {"status_in": [200], "selector": "ul.r li.x span.n", "min_items": 3}
    assert check_expect_html(200, "text/html", page, expect, implied_end_tags=True)[0]
    ok, why = check_expect_html(200, "text/html", page, expect)
    assert not ok and "depth cap" in why


def test_the_result_contract_reads_the_template_tree():
    seen = []
    real = RC.check_html

    def spy(*args, **kw):
        seen.append(kw.get("implied_end_tags", False))
        return real(*args, **kw)
    RC.check_html = spy
    try:
        for marker in (HTML5_PARSER, ""):
            tmpl = html_template("ul.hits li.hit a.name", marker)
            RC.attach(tmpl)
            assert run(tmpl, Site(unclosed=True), "delta")["success"]
    finally:
        RC.check_html = real
    assert seen == [True, False]


def test_the_contract_check_reads_the_tree_it_is_given():
    page = deep_list_page("delta")
    sel = "ul.hits li.hit"
    tmpl = html_template(sel, HTML5_PARSER)
    RC.attach(tmpl)
    contract = tmpl.expect[RC.CONTRACT_KEY]
    items = extract(page, sel, implied_end_tags=True)
    verdict = RC.check_html(contract, page, sel, {"q": "delta"}, items, implied_end_tags=True)
    assert verdict.ok, verdict.reason
    verdict = RC.check_html(contract, page, sel, {"q": "delta"}, items)
    assert not verdict.ok and "depth cap" in verdict.reason, verdict.reason


def test_an_unknown_parser_name_is_refused():
    problems = html_template("ul.hits li.hit", "html9").problems()
    assert any("html_parser" in p for p in problems), problems
    assert html_template("ul.hits li.hit", HTML5_PARSER).problems() == []


# ---------------------------------------------------------------------------
# step repair: its own captures are raw pages
# ---------------------------------------------------------------------------

def proved_record(template, value="alpha"):
    rec = compiled_record(template, {"passed": True, "status": PS.PROVED,
                                     "cases": [], "case_count": 2}, workflow="w")
    out = run(template, Site(), value)
    assert out["success"], out.get("error")
    assert SR.note_success(rec, value, out)
    return rec


def test_the_old_extraction_is_read_with_the_template_tree():
    cap = SR.capture(html_template("ul.hits li.hit"), "alpha",
                     ApiStepExecutor(transport=Site(unclosed=True)))
    rows = [f"{n} v1" for n in names("alpha")]
    assert SR._old_items(html_template("ul.hits li.hit", HTML5_PARSER), cap) == rows
    assert SR._old_items(html_template("ul.hits li.hit"), cap)[0] == " ".join(rows)


def test_a_repair_on_a_page_without_end_tags_writes_the_marker():
    selector = "table.results tbody tr.row a.name"
    tmpl = html_template(selector)
    tmpl.expect["min_items"] = 6
    RC.attach(tmpl)
    rec = proved_record(tmpl)
    site = Site(unclosed=True)
    assert not run(load_command(rec["template"]), site, "delta")["success"]
    new, out = SR.repair(rec, executor=ApiStepExecutor(transport=site), selection="beta",
                         held_out=["delta", "epsilon"])
    assert new is not None, out.reason
    assert out.localised == "extraction_region"
    assert new["template"]["html_parser"] == HTML5_PARSER
    got = run(load_command(new["template"]), site, "zeta")
    assert got["success"] and got["extracted"]["items"] == names("zeta"), got


def test_a_repair_reads_a_drifted_page_longer_than_the_depth_cap():
    # read without the rules the drifted page is refused: the localisation,
    # the re-derivation and the re-proof all read it with them. On the first
    # page a shortened old selector (a.name) still fits; the second has no
    # a.name, so only a derived selector does.
    for site, page in ((Site(unclosed=True, rows=DEEP), unclosed_page("alpha", DEEP)),
                       (Serve(deep_list_page), deep_list_page("alpha"))):
        assert refused_without_rules(page)
        tmpl = html_template("table.results tbody tr.row a.name")
        tmpl.expect["min_items"] = 6
        RC.attach(tmpl)
        rec = proved_record(tmpl)
        new, out = SR.repair(rec, executor=ApiStepExecutor(transport=site),
                             selection="beta", held_out=["delta", "epsilon"])
        assert new is not None, out.reason
        assert out.localised == "extraction_region", out.evidence
        got = run(load_command(new["template"]), site, "zeta")
        assert got["success"] and got["extracted"]["items"] == names("zeta", 50), got


def test_reprove_reads_a_ui_page_as_it_is():
    # a ui_page page may be a DOM snapshot; the pages reprove fetches itself are raw
    seen = []
    real = LL.derive_ground_truth

    def spy(recs, **kw):
        seen.append(kw.get("implied_end_tags", False))
        return real(recs, **kw)
    LL.derive_ground_truth = spy
    try:
        tmpl = html_template("ul.hits li.hit a.name", HTML5_PARSER)
        values = ["alpha", "beta", "delta", "epsilon"]
        executor = ApiStepExecutor(transport=Site(unclosed=True))
        SR.reprove(tmpl, values, executor)
        SR.reprove(tmpl, values, executor,
                   ui_page=lambda v: (f"https://ex.test/search?q={v}", cards_dom(v)))
    finally:
        LL.derive_ground_truth = real
    assert seen == [True, False]


def test_a_repaired_json_tool_carries_no_marker():
    t = RequestTemplate(method="GET", url="https://ex.test/api", query={"q": "{q}"},
                        extract={"items": "results[*].name"}, extract_kind="json",
                        shape="list", params=["q"])
    assert "html_parser" not in SR.with_extraction(t, "data[*].name", "test").to_dict()


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:400])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
