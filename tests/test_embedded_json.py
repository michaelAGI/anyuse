"""The embedded-JSON rung: finding the payload, the non-executing assignment
parser, compilation, the preference order against the HTML rung, the executor
branch, failure injection and the same fresh-input proof.

Offline: synthetic markup and an injected transport. No desktop, no network.
"""
import json

from anyuse.execution.api_compile import (
    compile_embedded_json,
    compile_html,
    compile_recording,
    visible_items,
)
from anyuse.execution.api_step import (
    ApiResponse,
    ApiStepExecutor,
    RequestTemplate,
    check_expect_embedded,
)
from anyuse.execution.api_verify import verify_template
from anyuse.execution.embedded_json import (
    Blob,
    _balanced_slice,
    describe_locator,
    extract,
    find_embedded,
    select_blob,
)

ITEMS = ["Alpha Pkg", "Beta Pkg", "Gamma Pkg"]

_PAYLOAD = {
    "props": {"pageProps": {"results": [
        {"name": "Alpha Pkg", "version": "1.0.0"},
        {"name": "Beta Pkg", "version": "2.1.3"},
        {"name": "Gamma Pkg", "version": "0.9.1"},
    ]}},
    "query": {"q": "Alpha Pkg"},
}

NEXT_PAGE = (
    '<!DOCTYPE html><html><head><title>Search</title></head><body>'
    '<div id="__next"><ul><li>Alpha Pkg</li><li>Beta Pkg</li><li>Gamma Pkg</li></ul></div>'
    '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(_PAYLOAD) + '</script>'
    '</body></html>'
)

LD_PAGE = (
    '<!DOCTYPE html><html><head>'
    '<script type="application/ld+json">' + json.dumps(
        {"@type": "ItemList", "itemListElement": [
            {"name": "Alpha Pkg"}, {"name": "Beta Pkg"}, {"name": "Gamma Pkg"}]}) + '</script>'
    '</head><body><p>Alpha Pkg</p><p>Beta Pkg</p><p>Gamma Pkg</p></body></html>'
)

STATE_PAGE = (
    '<!DOCTYPE html><html><body><div id="app"></div>'
    '<script>window.__INITIAL_STATE__ = ' + json.dumps(
        {"search": {"hits": [{"title": "Alpha Pkg"}, {"title": "Beta Pkg"},
                             {"title": "Gamma Pkg"}]}}) + ';</script>'
    '</body></html>'
)


def _recording(body, url="https://ex.test/search?q=Alpha+Pkg&_=1712345678901"):
    return {"param": {"name": "q", "value": "Alpha Pkg"},
            "ui": {"items": ITEMS, "text": ""},
            "network": [{"url": url, "method": "GET", "status": 200,
                         "mime": "text/html; charset=utf-8", "bytes": len(body),
                         "body": body}]}


def _compile_emb(body, url=None):
    """compile_embedded_json the way compile_recording calls it: NORMALISED items.

    (The compiler scores against ``visible_items(recording)``, not the raw
    labels — passing raw ones would silently compare "Alpha Pkg" with
    "alpha pkg" and explain nothing.)
    """
    rec = _recording(body) if url is None else _recording(body, url)
    return compile_embedded_json(rec, visible_items(rec), "q", "Alpha Pkg")


def _compile_html(body):
    rec = _recording(body)
    return compile_html(rec, visible_items(rec), "q", "Alpha Pkg")


# ---------------------------------------------------------------------------
# finding the payload
# ---------------------------------------------------------------------------

def test_finds_a_next_data_island():
    blobs = find_embedded(NEXT_PAGE)
    assert len(blobs) == 1, [b.describe() for b in blobs]
    assert blobs[0].origin == "next_data"
    assert blobs[0].locator == {"kind": "script_id", "id": "__NEXT_DATA__"}
    assert blobs[0].data == _PAYLOAD


def test_finds_an_ld_json_island_and_indexes_it():
    blobs = find_embedded(LD_PAGE)
    assert [b.origin for b in blobs] == ["ld_json"]
    assert blobs[0].locator == {"kind": "script_type", "type": "application/ld+json", "index": 0}


def test_finds_a_window_assignment():
    blobs = find_embedded(STATE_PAGE)
    assert [b.origin for b in blobs] == ["assignment"]
    assert blobs[0].locator == {"kind": "assignment", "name": "window.__INITIAL_STATE__"}
    assert blobs[0].data["search"]["hits"][0]["title"] == "Alpha Pkg"


def test_ignores_ordinary_scripts_and_non_json_types():
    page = ('<script>var a = 1;</script>'
            '<script type="text/javascript">{"looks": "jsonish"}</script>'
            '<script type="application/json">{"real": "payload", "n": 12345}</script>')
    blobs = find_embedded(page)
    assert [b.origin for b in blobs] == ["script_json"]
    assert blobs[0].data == {"real": "payload", "n": 12345}


def test_biggest_payload_comes_first():
    page = ('<script type="application/ld+json">{"@type":"WebSite","name":"x"}</script>'
            '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(_PAYLOAD) + '</script>')
    blobs = find_embedded(page)
    assert [b.origin for b in blobs] == ["next_data", "ld_json"], [b.describe() for b in blobs]


def test_a_document_with_no_payload_yields_nothing():
    assert find_embedded("<html><body><p>plain</p></body></html>") == []
    assert find_embedded("") == []


# ---------------------------------------------------------------------------
# the assignment parser executes nothing
# ---------------------------------------------------------------------------

def test_balanced_slice_respects_strings_and_escapes():
    text = 'x = {"a": "}{ not a brace", "b": "quote \\" inside", "c": [1,2]} ; more'
    got = _balanced_slice(text, text.index("{"))
    assert json.loads(got) == {"a": "}{ not a brace", "b": 'quote " inside', "c": [1, 2]}


def test_a_javascript_object_literal_is_rejected_not_interpreted():
    """Unquoted keys are JS, not JSON. We refuse rather than reach for an
    evaluator — running the page's code is the thing this approach avoids."""
    page = "<script>window.__INITIAL_STATE__ = {hits: [1,2,3], when: new Date()};</script>"
    assert find_embedded(page) == []


def test_nothing_is_executed_even_for_hostile_looking_payloads():
    page = ('<script>window.__INITIAL_STATE__ = '
            '{"cmd": "__import__(\'os\').system(\'echo pwned\')", "n": 1};</script>')
    blobs = find_embedded(page)
    assert len(blobs) == 1
    # It is a STRING. It was parsed, never evaluated.
    assert blobs[0].data["cmd"].startswith("__import__")


def test_an_unterminated_literal_is_skipped():
    assert find_embedded('<script>window.__INITIAL_STATE__ = {"a": [1,2</script>') == []


# ---------------------------------------------------------------------------
# re-finding the payload in a FRESH response — the replay half
# ---------------------------------------------------------------------------

def test_select_blob_refinds_the_same_payload_in_a_new_response():
    fresh = NEXT_PAGE.replace("Alpha Pkg", "Delta Pkg")
    data = select_blob(fresh, {"kind": "script_id", "id": "__NEXT_DATA__"})
    assert data["props"]["pageProps"]["results"][0]["name"] == "Delta Pkg"


def test_select_blob_returns_none_when_the_payload_is_gone():
    assert select_blob("<html><body>nothing</body></html>",
                       {"kind": "script_id", "id": "__NEXT_DATA__"}) is None
    assert select_blob(NEXT_PAGE, {"kind": "assignment", "name": "window.__NUXT__"}) is None


def test_extract_walks_a_json_path_into_the_payload():
    got = extract(NEXT_PAGE, {"kind": "script_id", "id": "__NEXT_DATA__"},
                  "props.pageProps.results[*].name")
    assert got == ITEMS


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------

def test_compiles_a_next_data_page_to_a_json_path():
    tmpl, why = _compile_emb(NEXT_PAGE)
    assert tmpl is not None, why
    assert tmpl.extract_kind == "embedded_json"
    assert tmpl.embedded == {"kind": "script_id", "id": "__NEXT_DATA__"}
    assert tmpl.extract["items"] == "props.pageProps.results[*].name", tmpl.extract
    assert tmpl.url == "https://ex.test/search" and tmpl.query == {"q": "{q}"}
    assert tmpl.headers == {"accept": "text/html"}
    assert tmpl.expect["content_type"] == "html"
    assert tmpl.problems() == []


def test_compiles_an_assignment_page():
    tmpl, why = _compile_emb(STATE_PAGE)
    assert tmpl is not None, why
    assert tmpl.embedded["kind"] == "assignment"
    assert tmpl.extract["items"] == "search.hits[*].title", tmpl.extract


# ---------------------------------------------------------------------------
# any assignment a page's script makes, found by its structure
# ---------------------------------------------------------------------------

_HITS = {"search": {"hits": [{"title": t} for t in ITEMS]}}


def _script_page(script, text="".join(f"<li>{t}</li>" for t in ITEMS)):
    return (f"<!DOCTYPE html><html><body><ul>{text}</ul>"
            f"<script>{script}</script></body></html>")


def test_a_payload_under_the_pages_own_name_is_found_and_refound():
    for script, name in [
            ("window.APP_STATE = " + json.dumps(_HITS) + ";", "window.APP_STATE"),
            ('window["__dati__"] = ' + json.dumps(_HITS) + ";", "window.__dati__"),
            ("var risultati = " + json.dumps(_HITS["search"]["hits"]) + ";", "risultati"),
            ("App.store.initial = " + json.dumps(_HITS) + ";", "App.store.initial")]:
        page = _script_page(script)
        blobs = find_embedded(page)
        assert [b.locator for b in blobs] == [{"kind": "assignment", "name": name}], script
        fresh = page.replace("Alpha Pkg", "Delta Pkg")
        got = select_blob(fresh, blobs[0].locator)
        assert "Delta Pkg" in json.dumps(got), script


def test_a_json_parse_payload_is_read_by_the_string_rules_alone():
    text = json.dumps(_HITS)
    for script in ["window.__APOLLO_STATE__ = JSON.parse(" + json.dumps(text) + ");",
                   "self.estado = JSON.parse('" + text.replace("A", "\\x41") + "')",
                   "const dane = JSON.parse(`" + text.replace("G", "\\u0047") + "`);"]:
        blobs = find_embedded(_script_page(script))
        assert len(blobs) == 1 and blobs[0].data == _HITS, script
    # a template that runs code, a call around the string, an octal escape: refused
    for script in ["x.y = JSON.parse(`{\"a\": \"${secret}\", \"b\": 1234567890}`)",
                   "x.y = JSON.parse(atob('eyJhIjogMX0gICAgICAgICAgICAgICAgICAgIA=='))",
                   "x.y = JSON.parse('{\"a\": \"\\101\", \"b\": \"1234567890123\"}')"]:
        assert find_embedded(_script_page(script)) == [], script


def test_only_a_script_assignment_counts_and_other_syntax_is_not_one():
    payload = json.dumps(_HITS)
    # the page's text, an external script, a JSON island's text: no assignment there
    assert find_embedded(f"<p>app.data = {payload}</p>") == []
    assert find_embedded(f"<script src='/a.js'>app.data = {payload}</script>") == []
    # a comparison, an arrow function, a JavaScript object literal: not JSON assigned
    for script in [f"if (a == {payload}) go()", f"const f = (a) => {payload}",
                   "window.STATE = {hits: [1, 2, 3], when: new Date()};"]:
        assert find_embedded(_script_page(script)) == [], script


def test_the_visible_list_chooses_the_payload_not_its_name():
    menu = {"nav": [{"label": x} for x in ("Home", "Help", "About", "Shop")]}
    page = _script_page("window.__INITIAL_STATE__ = " + json.dumps(menu) + ";\n"
                        "window.zoekResultaten = " + json.dumps(_HITS) + ";")
    tmpl, why = _compile_emb(page)
    assert tmpl is not None, why
    assert tmpl.embedded == {"kind": "assignment", "name": "window.zoekResultaten"}
    assert tmpl.extract["items"] == "search.hits[*].title", tmpl.extract


def test_javascript_literals_in_a_large_script_cost_a_short_walk_each():
    # a bundle full of array literals whose brackets never balance as text (a regular
    # expression's "\[" inside each): every walk stops at the first character JSON cannot
    # hold, so the page's payload is found again quickly on every call
    import time
    filler = "function f(n){return n*2}" * 1000
    bundle = "".join(f'x{i}=[t.replace(/\\[/g,"")];' + filler for i in range(150))
    page = _script_page(bundle + "\nwindow.APP_STATE = " + json.dumps(_HITS) + ";")
    t0 = time.monotonic()
    got = select_blob(page, {"kind": "assignment", "name": "window.APP_STATE"})
    assert got == _HITS
    assert time.monotonic() - t0 < 3.0, time.monotonic() - t0


def test_compiles_an_ld_json_page():
    tmpl, why = _compile_emb(LD_PAGE)
    assert tmpl is not None, why
    assert tmpl.embedded == {"kind": "script_type", "type": "application/ld+json", "index": 0}


def test_volatile_query_params_are_dropped_the_same_way():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    assert "_" not in tmpl.query, tmpl.query


def test_a_page_with_no_payload_says_so():
    page = "<html><body><ul><li>Alpha Pkg</li></ul></body></html>"
    tmpl, why = _compile_emb(page)
    assert tmpl is None and "no embedded JSON payload" in why


def test_a_payload_that_does_not_explain_the_screen_says_so():
    page = ('<script type="application/json">' + json.dumps(
        {"config": {"locale": "en-GB", "theme": "dark-mode-default"}}) + '</script>')
    tmpl, why = _compile_emb(page)
    assert tmpl is None and "no payload explained the visible list" in why


# ---------------------------------------------------------------------------
# the preference order: JSON API > embedded JSON > HTML selectors
# ---------------------------------------------------------------------------

BOTH_PAGE = (
    '<!DOCTYPE html><html><body><div class="results">'
    '<li class="row"><a class="title">Alpha Pkg</a></li>'
    '<li class="row"><a class="title">Beta Pkg</a></li>'
    '<li class="row"><a class="title">Gamma Pkg</a></li>'
    '</div>'
    '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(_PAYLOAD) + '</script>'
    '</body></html>'
)


def test_embedded_json_is_preferred_over_a_selector_when_both_work():
    rec = _recording(BOTH_PAGE)
    html_tmpl, _ = _compile_html(BOTH_PAGE)
    assert html_tmpl is not None, "the fixture must be compilable BOTH ways"

    res = compile_recording(rec)
    assert res.status == "embedded_json", (res.status, res.reason)
    assert res.template.extract_kind == "embedded_json"
    assert "reads the page's DATA, not its markup" in res.template.notes


def test_the_html_rung_still_wins_when_there_is_no_payload():
    res = compile_recording(_recording(
        '<div class="results"><li class="row"><a class="title">Alpha Pkg</a></li>'
        '<li class="row"><a class="title">Beta Pkg</a></li>'
        '<li class="row"><a class="title">Gamma Pkg</a></li></div>'))
    assert res.status == "html", (res.status, res.reason)


def test_a_real_json_api_still_outranks_both():
    rec = _recording(BOTH_PAGE)
    rec["network"].append({
        "url": "https://ex.test/api/search?q=Alpha+Pkg", "method": "GET", "status": 200,
        "mime": "application/json", "bytes": 120,
        "body": json.dumps({"results": [{"name": n} for n in ITEMS]})})
    res = compile_recording(rec)
    assert res.status == "ok", (res.status, res.reason)
    assert res.template.extract_kind == "json"
    assert res.template.url == "https://ex.test/api/search"


# ---------------------------------------------------------------------------
# execution, failure injection, proof
# ---------------------------------------------------------------------------

class HtmlTransport:
    def __init__(self, body=NEXT_PAGE, status=200, ctype="text/html"):
        self.body, self.status, self.ctype = body, status, ctype
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        return ApiResponse(self.status, {"Content-Type": self.ctype}, self.body)


def test_the_executor_runs_an_embedded_rung_and_extracts():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    tr = HtmlTransport()
    res = ApiStepExecutor(transport=tr).execute(tmpl, {"q": "Alpha Pkg"})
    assert res["success"] is True, res.get("error")
    assert res["extracted"]["items"] == ITEMS
    assert tr.sent[0].endswith("q=Alpha+Pkg")


def test_a_redesign_that_breaks_a_selector_does_not_break_the_payload():
    """The reason this rung ranks above the HTML one, as a test rather than a claim."""
    redesigned = (BOTH_PAGE
                  .replace('class="row"', 'class="listing-item"')
                  .replace('class="title"', 'class="pkg-name"')
                  .replace('class="results"', 'class="grid"'))
    rec = _recording(BOTH_PAGE)

    html_tmpl, _ = _compile_html(BOTH_PAGE)
    html_res = ApiStepExecutor(transport=HtmlTransport(body=redesigned)).execute(
        html_tmpl, {"q": "Alpha Pkg"})
    assert html_res["success"] is False, "the fixture must actually break the selector"

    emb_tmpl, _ = _compile_emb(BOTH_PAGE)
    emb_res = ApiStepExecutor(transport=HtmlTransport(body=redesigned)).execute(
        emb_tmpl, {"q": "Alpha Pkg"})
    assert emb_res["success"] is True, emb_res.get("error")
    assert emb_res["extracted"]["items"] == ITEMS


def test_a_vanished_payload_fails_the_postcondition_and_names_the_locator():
    # FAILURE INJECTION: the site stops shipping the hydration payload.
    tmpl, _ = _compile_emb(NEXT_PAGE)
    stripped = "<html><body><ul><li>Alpha Pkg</li></ul></body></html>"
    res = ApiStepExecutor(transport=HtmlTransport(body=stripped)).execute(
        tmpl, {"q": "Alpha Pkg"})
    assert res["success"] is False
    assert "no longer carries" in res["error"] and "__NEXT_DATA__" in res["error"]


def test_a_renamed_field_inside_the_payload_fails_too():
    # FAILURE INJECTION: the payload survives but the data model moved.
    tmpl, _ = _compile_emb(NEXT_PAGE)
    moved = NEXT_PAGE.replace('"results"', '"hits"').replace('"name"', '"label"')
    res = ApiStepExecutor(transport=HtmlTransport(body=moved)).execute(tmpl, {"q": "Alpha Pkg"})
    assert res["success"] is False and "postcondition failed" in res["error"]


def test_a_non_2xx_is_an_ordinary_failure():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    res = ApiStepExecutor(transport=HtmlTransport(status=503)).execute(tmpl, {"q": "Alpha Pkg"})
    assert res["success"] is False and "503" in res["error"]


def test_check_expect_embedded_reports_which_half_broke():
    expect = {"status_in": [200], "content_type": "html", "path": "props.pageProps.results",
              "min_items": 1}
    loc = {"kind": "script_id", "id": "__NEXT_DATA__"}
    ok, data, reason = check_expect_embedded(200, "text/html", NEXT_PAGE, loc, expect)
    assert ok is True and reason == "ok" and data == _PAYLOAD

    ok, data, reason = check_expect_embedded(200, "text/html", "<html></html>", loc, expect)
    assert ok is False and data is None and "no longer carries" in reason

    moved = NEXT_PAGE.replace('"results"', '"hits"')
    ok, data, reason = check_expect_embedded(200, "text/html", moved, loc, expect)
    assert ok is False and data is not None, "the payload was found; the PATH was not"


def test_an_embedded_rung_takes_the_same_fresh_input_proof():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    ex = ApiStepExecutor(transport=HtmlTransport())
    cases = [{"value": "fresh one", "ui_items": ITEMS},
             {"value": "fresh two", "ui_items": ITEMS}]
    res = verify_template(tmpl, cases, ex)
    assert res.passed, res.reason
    assert res.passed_count == 2


def test_the_proof_fails_when_the_payload_does_not_match_the_ui():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    ex = ApiStepExecutor(transport=HtmlTransport())
    cases = [{"value": "fresh one", "ui_items": ["Totally", "Different", "Things"]},
             {"value": "fresh two", "ui_items": ["Totally", "Different", "Things"]}]
    assert verify_template(tmpl, cases, ex).passed is False


def test_one_fresh_case_is_never_enough():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    ex = ApiStepExecutor(transport=HtmlTransport())
    res = verify_template(tmpl, [{"value": "only one", "ui_items": ITEMS}], ex)
    assert res.passed is False and "required" in res.reason


def test_the_template_round_trips_through_persistence():
    tmpl, _ = _compile_emb(NEXT_PAGE)
    again = RequestTemplate.from_dict(tmpl.to_dict())
    assert again.extract_kind == "embedded_json"
    assert again.embedded == tmpl.embedded
    res = ApiStepExecutor(transport=HtmlTransport()).execute(again, {"q": "x"})
    assert res["extracted"]["items"] == ITEMS


def test_describe_locator_is_readable():
    assert "__NEXT_DATA__" in describe_locator({"kind": "script_id", "id": "__NEXT_DATA__"})
    assert "window.__INITIAL_STATE__" in describe_locator(
        {"kind": "assignment", "name": "window.__INITIAL_STATE__"})
    assert "ld+json" in describe_locator(
        {"kind": "script_type", "type": "application/ld+json", "index": 0})


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
