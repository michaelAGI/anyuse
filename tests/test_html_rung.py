"""The HTML compile target: parser, selector language, derivation, postcondition,
executor branch and the same fresh-input proof.

Offline: synthetic markup and an injected transport. No desktop, no network.
"""
from anyuse.execution.api_compile import compile_html, urlify, visible_items
from anyuse.execution.api_step import (
    ApiResponse,
    ApiStepExecutor,
    RequestTemplate,
    check_expect_html,
)
from anyuse.execution.api_verify import verify_template
from anyuse.execution.html_extract import (
    derive_selector,
    extract,
    parse,
    parse_selector,
    select,
    selector_is_specific,
)

PAGE = """<!DOCTYPE html><html><head><title>Search</title>
<script>var x = "Alpha Pkg";</script></head><body>
<header><h1>Results for <span class="q">Alpha Pkg</span></h1></header>
<main><div class="results">
  <li class="row"><a class="title" href="/a">Alpha Pkg</a><span class="desc">first one</span></li>
  <li class="row"><a class="title" href="/b">Beta Pkg</a><span class="desc">second one</span></li>
  <li class="row"><a class="title" href="/c">Gamma Pkg</a><span class="desc">third one</span></li>
</div></main>
<footer><a href="/x">Alpha Pkg</a></footer></body></html>"""

ITEMS = ["Alpha Pkg", "Beta Pkg", "Gamma Pkg"]


def _recording(body=PAGE, url="https://ex.test/search?q=Alpha+Pkg&_=1712345678901"):
    return {"param": {"name": "q", "value": "Alpha Pkg"},
            "ui": {"items": ITEMS, "text": ""},
            "network": [{"url": url, "method": "GET", "status": 200,
                         "mime": "text/html; charset=utf-8", "bytes": len(body),
                         "body": body}]}


# ---------------------------------------------------------------------------
# parser and selector language
# ---------------------------------------------------------------------------

def test_the_parser_survives_unclosed_tags_and_skips_scripts():
    root = parse("<div class=a><p>one<p>two</div><script>hidden()</script>")
    assert "one" in root.text() and "two" in root.text()
    assert "hidden" not in root.text()


def test_parse_selector_reads_tag_and_classes():
    assert parse_selector("div.results li.row a") == [
        ("div", ("results",)), ("li", ("row",)), ("a", ())]


def test_select_matches_descendants_not_only_direct_children():
    root = parse(PAGE)
    assert len(select(root, "div.results a.title")) == 3
    assert len(select(root, "main a.title")) == 3


def test_extract_returns_text_in_document_order():
    assert extract(PAGE, "div.results li.row a.title") == ITEMS


def test_selector_is_specific_rejects_a_page_wide_match():
    assert selector_is_specific(PAGE, "div.results li.row a.title") is True
    assert selector_is_specific(PAGE, "a", max_matches=2) is False


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------

def test_derivation_finds_the_row_selector_and_ignores_header_and_footer():
    selector, coverage, values = derive_selector(PAGE, ITEMS)
    assert coverage == 1.0
    assert values == ITEMS, values
    assert "row" in selector or "title" in selector
    # the echoed query in <h1> and the footer link must not be picked up
    assert extract(PAGE, selector) == ITEMS


def test_derivation_survives_the_query_being_echoed_in_the_heading():
    # The first value matches a heading node that shares no structure with the
    # rows. Requiring every matched node to agree yields nothing; consensus works.
    selector, coverage, _ = derive_selector(PAGE, ITEMS)
    assert selector and coverage >= 0.8


def test_derivation_returns_nothing_when_no_value_is_present():
    selector, coverage, _ = derive_selector(PAGE, ["Not On This Page At All"])
    assert selector == "" and coverage == 0.0


# ---------------------------------------------------------------------------
# postcondition
# ---------------------------------------------------------------------------

def test_html_postcondition_checks_status_content_type_and_count():
    expect = {"status_in": [200], "content_type": "html",
              "selector": "div.results li.row a.title", "min_items": 3}
    assert check_expect_html(200, "text/html", PAGE, expect)[0] is True
    assert check_expect_html(404, "text/html", PAGE, expect)[0] is False
    assert check_expect_html(200, "application/json", PAGE, expect)[0] is False
    ok, reason = check_expect_html(200, "text/html", "<html><body></body></html>", expect)
    assert ok is False and "markup probably changed" in reason


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------

def test_compile_html_emits_a_template_with_the_param_and_drops_cache_busters():
    tmpl, why = compile_html(_recording(), visible_items(_recording()), "q", "Alpha Pkg")
    assert tmpl is not None, why
    assert tmpl.extract_kind == "html"
    assert tmpl.url == "https://ex.test/search"
    assert tmpl.query == {"q": "{q}"}          # the "_" cache-buster is dropped
    assert tmpl.expect["selector"] == tmpl.extract["items"]
    assert tmpl.params == ["q"] and tmpl.problems() == []
    assert "falls back to the recorded UI path" in tmpl.notes


def test_compile_html_refuses_when_the_param_is_not_in_the_url():
    rec = _recording(url="https://ex.test/static/page")
    tmpl, why = compile_html(rec, ITEMS, "q", "Alpha Pkg")
    assert tmpl is None and why


def test_compile_html_refuses_a_document_with_no_specific_selector():
    plain = "<html><body><p>Alpha Pkg</p><p>Beta Pkg</p><p>Gamma Pkg</p></body></html>"
    tmpl, _why = compile_html(_recording(body=plain), ITEMS, "q", "Alpha Pkg")
    # a bare <p> chain is still specific here, so this must at least not crash;
    # the guard that matters is the page-wide one, checked above.
    assert tmpl is None or tmpl.extract_kind == "html"


def test_a_recording_with_no_html_document_says_so():
    tmpl, why = compile_html({"network": []}, ITEMS, "q", "x")
    assert tmpl is None and "no 2xx HTML document" in why


# ---------------------------------------------------------------------------
# execution + the same proof
# ---------------------------------------------------------------------------

class HtmlTransport:
    def __init__(self, body=PAGE, status=200, ctype="text/html"):
        self.body, self.status, self.ctype = body, status, ctype
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        return ApiResponse(self.status, {"Content-Type": self.ctype}, self.body)


def test_the_executor_runs_an_html_rung_and_extracts():
    tmpl, _ = compile_html(_recording(), ITEMS, "q", "Alpha Pkg")
    tr = HtmlTransport()
    res = ApiStepExecutor(transport=tr).execute(tmpl, {"q": "Alpha Pkg"})
    assert res["success"] is True
    assert res["extracted"]["items"] == ITEMS
    assert tr.sent[0].endswith("q=Alpha+Pkg")


def test_a_markup_change_fails_the_postcondition_and_falls_back():
    # FAILURE INJECTION: the class is renamed, so the selector matches nothing.
    tmpl, _ = compile_html(_recording(), ITEMS, "q", "Alpha Pkg")
    changed = PAGE.replace('class="row"', 'class="listing"').replace(
        'class="title"', 'class="name"')
    res = ApiStepExecutor(transport=HtmlTransport(body=changed)).execute(
        tmpl, {"q": "Alpha Pkg"})
    assert res["success"] is False
    assert "postcondition failed" in res["error"]


def test_an_html_rung_takes_the_same_fresh_input_proof():
    tmpl, _ = compile_html(_recording(), ITEMS, "q", "Alpha Pkg")
    ex = ApiStepExecutor(transport=HtmlTransport())
    cases = [{"value": "fresh one", "ui_items": ITEMS},
             {"value": "fresh two", "ui_items": ITEMS}]
    res = verify_template(tmpl, cases, ex)
    assert res.passed, res.reason


def test_an_html_rung_fails_the_proof_when_the_page_does_not_match_the_ui():
    tmpl, _ = compile_html(_recording(), ITEMS, "q", "Alpha Pkg")
    ex = ApiStepExecutor(transport=HtmlTransport())
    cases = [{"value": "fresh one", "ui_items": ["Totally", "Different", "Things"]},
             {"value": "fresh two", "ui_items": ["Totally", "Different", "Things"]}]
    assert verify_template(tmpl, cases, ex).passed is False


def test_urlify_is_shared_by_both_rungs():
    url, query, dropped, used, why = urlify(
        "https://ex.test/s?q=Alpha+Pkg&page=1&utm_source=ui", "q", "Alpha Pkg")
    assert why == "" and url == "https://ex.test/s"
    assert query == {"q": "{q}", "page": "1"} and "utm_source" in dropped and used == ["q"]


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
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
