"""Deterministic API extraction and the fresh-input equivalence proof.

Runs entirely offline against ``anyuse/testdata/compile/*.json`` — minimal,
redacted recordings of PUBLIC responses (headers, cookies and timings removed;
JSON bodies truncated). Both the positive cases and the honest negatives
(server-rendered HTML; a POST carrying a per-client API key) are fixtures, so
the reasons the compiler gives are tested, not just the successes.
"""
import json
import os
import shutil
import tempfile
import urllib.parse

from anyuse.execution.api_compile import (
    COMPILED_FILE,
    add_api_implementation,
    build_template,
    compile_recording,
    compiled_record,
    cover_paths,
    diagnose_failure,
    eligible_requests,
    generalise_path,
    load_compiled,
    locate_param,
    normalise,
    persist_compiled,
    redacted_summary,
    score_candidate,
    urlify,
    values_match,
    visible_items,
)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.api_verify import (
    cases_from_recordings,
    compare,
    compile_and_verify,
    verify_template,
)
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UITransition

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "compile")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# normalisation and matching
# ---------------------------------------------------------------------------

def test_normalise_folds_case_and_punctuation():
    assert normalise("Lazy-Regex") == "lazy regex"
    assert normalise("  A/B  c ") == "a b c"
    assert normalise(None) == ""


def test_values_match_requires_containment_to_be_substantial():
    assert values_match("lazy regex", "lazy regex")
    assert values_match("regex lite", "regex lite crate")
    # The guard that stopped the extractor compiling crates.io's KEYWORD list:
    # a one-word leaf must not "explain" a whole sentence that contains it.
    assert not values_match("serde", "a generic serialization deserialization framework")


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------

def test_eligible_requests_keeps_only_replayable_json_reads():
    rec = fixture("crates_search.rec.json")
    urls = [r["url"] for r in eligible_requests(rec)]
    assert any("/api/v1/crates?" in u for u in urls)
    for r in eligible_requests(rec):
        assert r["method"].upper() in ("GET", "HEAD")
        assert 200 <= int(r["status"]) < 300
        assert r["url"].startswith("https://")


def test_a_post_is_never_eligible_even_when_it_explains_the_screen():
    rec = fixture("hn_search.rec.json")
    assert eligible_requests(rec) == []


def test_score_candidate_picks_the_request_the_ui_rendered_from():
    rec = fixture("crates_search.rec.json")
    items = visible_items(rec)
    scored = sorted((score_candidate(r, items) for r in eligible_requests(rec)),
                    key=lambda c: -c.coverage)
    assert "/api/v1/crates" in scored[0].url
    assert scored[0].coverage >= 0.9
    assert scored[0].best_path() == "crates[*].id"


def test_generalise_path_replaces_indices():
    assert generalise_path("crates[3].name") == "crates[*].name"


def test_cover_paths_uses_several_fields_for_a_detail_screen():
    rec = fixture("crates_detail.rec.json")
    items = visible_items(rec)
    cand = [score_candidate(r, items) for r in eligible_requests(rec)]
    best = max(cand, key=lambda c: c.coverage)
    paths = cover_paths(best)
    assert paths and all(p.startswith("crate.") for p in paths)


# ---------------------------------------------------------------------------
# parameter location and volatile dropping
# ---------------------------------------------------------------------------

def test_locate_param_finds_the_query_carrier():
    site = locate_param("https://crates.io/api/v1/crates?q=serde&page=1", "serde")
    assert (site.where, site.key) == ("query", "q")


def test_locate_param_finds_a_path_segment_carrier():
    site = locate_param("https://crates.io/api/v1/crates/serde", "serde")
    assert (site.where, site.key) == ("path", "3")


def test_locate_param_normalises_url_encoded_values():
    site = locate_param("https://x.test/s?query=rust+async", "rust async")
    assert (site.where, site.key) == ("query", "query")


def test_locate_param_reports_none_when_the_value_is_nowhere():
    assert locate_param("https://x.test/s?a=1", "serde").where == "none"


def test_build_template_drops_cache_busters_and_keeps_real_query_params():
    from anyuse.execution.api_compile import Candidate
    cand = Candidate(url="https://x.test/api?q=serde&per_page=10&_=1712345678901&utm_source=ui",
                     method="GET", total_items=1, matched_items=1,
                     path_hits={"items[*].id": 1}, item_paths=[["items[*].id"]])
    tmpl, why = build_template(cand, "q", "serde")
    assert tmpl is not None, why
    assert tmpl.query == {"q": "{q}", "per_page": "10"}
    assert "dropped volatile query params" in tmpl.notes


def test_build_template_refuses_a_signed_request():
    from anyuse.execution.api_compile import Candidate
    cand = Candidate(url="https://x.test/api?q=serde&token=abc123", method="GET",
                     total_items=1, matched_items=1,
                     path_hits={"items[*].id": 1}, item_paths=[["items[*].id"]])
    tmpl, why = build_template(cand, "q", "serde")
    assert tmpl is None and "signed" in why


# ---------------------------------------------------------------------------
# compile_recording end to end, on fixtures
# ---------------------------------------------------------------------------

def test_compile_a_list_workflow():
    res = compile_recording(fixture("crates_search.rec.json"))
    assert res.status == "ok", res.reason
    assert res.template.url == "https://crates.io/api/v1/crates"
    assert res.template.query["q"] == "{q}"
    assert res.template.shape == "list"
    assert res.template.extract == {"items": "crates[*].id"}
    assert res.template.expect["path"] == "crates"
    assert res.template.problems() == []


def test_compile_a_path_parameter_workflow_as_fields():
    res = compile_recording(fixture("crates_detail.rec.json"))
    assert res.template is not None, res.reason
    assert res.template.url == "https://crates.io/api/v1/crates/{crate}"
    assert res.template.params == ["crate"]
    assert res.template.shape == "fields"


def test_compile_a_typeahead_workflow():
    res = compile_recording(fixture("wikipedia_typeahead.rec.json"))
    assert res.status == "ok", res.reason
    assert res.template.url.endswith("/w/rest.php/v1/search/title")
    assert res.template.extract == {"items": "pages[*].key"}


def test_npm_nested_required_key_rung_can_be_reproved_offline():
    def body(names):
        return json.dumps({"objects": [{"package": {"name": name}} for name in names]})

    base_names = ["chalk", "chalk-template", "chalk-animation"]
    recording = {
        "param": {"name": "q", "value": "chalk"},
        "ui": {"items": base_names, "text": "\n".join(base_names)},
        "network": [{"url": "https://registry.npmjs.org/-/v1/search?text=chalk&size=10",
                     "method": "GET", "status": 200, "mime": "application/json",
                     "bytes": 100, "body": body(base_names)}],
    }
    compiled = compile_recording(recording)
    assert compiled.template is not None, compiled.reason
    assert compiled.template.extract == {"items": "objects[*].package.name"}
    assert compiled.template.expect["required_keys"] == ["package.name"]

    answers = {
        "lodash": ["lodash", "lodash.get", "lodash.merge"],
        "axios": ["axios", "axios-retry", "axios-mock-adapter"],
    }

    class NpmTransport:
        def send(self, method, url, headers, request_body, timeout_s):
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            names = answers[query["text"][0]]
            return ApiResponse(200, {"Content-Type": "application/json"}, body(names))

    cases = [{"value": value, "ui_items": names} for value, names in answers.items()]
    proof = verify_template(compiled.template, cases,
                            ApiStepExecutor(transport=NpmTransport()))
    assert proof.passed, proof.reason


def test_compile_reports_server_rendered_html_plainly():
    res = compile_recording(fixture("npm_search.rec.json"))
    assert res.status == "failed" and res.template is None
    assert "server-rendered" in res.reason


def test_compile_reports_a_keyed_post_plainly():
    res = compile_recording(fixture("hn_search.rec.json"))
    assert res.status == "failed" and res.template is None
    assert "POST" in res.reason and "API key" in res.reason


def _post_rec(query_keys):
    url = "https://search.provider.test/1/indexes/*/queries?" + urllib.parse.urlencode(
        [(k, "v123") for k in query_keys])
    return {"param": {"name": "q", "value": "kafka"}, "ui": {"items": ["kafka"], "text": ""},
            "network": [{"url": url, "method": "POST", "status": 200, "mime": "application/json",
                         "body": "{}"}]}


def test_a_keyed_post_is_named_by_the_credential_rule_not_a_vendor_name():
    # a vendor no catalogue names, its key in a credential-shaped name
    assert "API key" in diagnose_failure(_post_rec(["x-search-api-key", "app"]))
    # an application id is not a key, whoever's it is
    assert "API key" not in diagnose_failure(_post_rec(["x-algolia-application-id"]))
    assert "API key" in diagnose_failure(_post_rec(["x-algolia-api-key"]))


def test_labels_and_a_later_body_carry_a_value_as_whole_words():
    from anyuse.execution.api_compile import _labels_in_html, _searched_after
    rec = {"param": {"name": "q", "value": "art"}, "ui": {"items": ["Art", "Ink"], "text": ""},
           "network": [{"url": "https://x.test/s", "mime": "text/html",
                        "body": "<p>Start the pinkest party</p>"}]}
    assert not _labels_in_html(rec)
    rec["network"][0]["body"] = "<ul><li>Art</li><li>Ink</li></ul>"
    assert _labels_in_html(rec)
    later = {"network": [{"url": "https://x.test/", "method": "GET"},
                         {"url": "https://x.test/api", "method": "POST",
                          "post_data": "action=start&page=1"}]}
    assert not _searched_after(later, 0, "art")
    later["network"][1]["post_data"] = "query=art&page=1"
    assert _searched_after(later, 0, "art")
    # a script written without spaces between words keeps its own boundaries
    later["network"][1]["post_data"] = '{"q": "東京タワー"}'
    assert _searched_after(later, 0, "東京")


def test_diagnose_failure_names_an_anti_bot_challenge():
    rec = {"network": [{"url": "https://x.test/cdn-cgi/challenge-platform/h/g", "method": "GET",
                        "status": 200, "mime": "text/plain"}],
           "ui": {"items": [], "text": "Performing security verification"}}
    assert "anti-bot" in diagnose_failure(rec)


# ---------------------------------------------------------------------------
# the redacted summary a generator may see
# ---------------------------------------------------------------------------

def test_redacted_summary_marks_the_param_and_leaks_no_values():
    summary = redacted_summary(fixture("crates_search.rec.json"))
    blob = json.dumps(summary["requests"])
    assert '"<PARAM>"' in blob
    assert "1418158102" not in blob        # no download counts, no response values
    for req in summary["requests"]:
        assert "headers" not in req and "body" not in req
        for shape in req["response_shape"]:
            assert set(shape) == {"path", "type"}


# ---------------------------------------------------------------------------
# equivalence comparison
# ---------------------------------------------------------------------------

def test_compare_passes_on_an_identical_ordered_list():
    res = compare(["Serde", "serde_json", "serde_yaml"], ["serde", "serde-json", "serde-yaml"])
    assert res.passed and res.coverage == 1.0 and res.order_ok


def test_compare_matching_is_bijective_so_order_is_judged_honestly():
    # Greedy substring matching used to collapse every "…regex…" row onto the
    # single entry "regex" and then report a bogus order violation.
    ui = ["regex", "lazy regex", "regex filtered", "real regex"]
    api = ["regex", "lazy-regex", "regex-filtered", "real-regex"]
    res = compare(ui, api)
    assert res.passed and res.order_ok and res.coverage == 1.0


def test_compare_fails_on_a_shuffled_list():
    res = compare(["a one", "b two", "c three"], ["c three", "b two", "a one"])
    assert res.passed is False and res.order_ok is False


def test_compare_reports_what_is_missing():
    res = compare(["alpha", "beta", "gamma"], ["alpha"])
    assert res.passed is False
    assert res.missing == ["beta", "gamma"]
    assert res.ui_count == 3 and res.api_count == 1


def test_compare_skips_the_order_axis_for_a_field_shaped_extraction():
    res = compare(["beta thing", "alpha thing", "gamma thing"],
                  ["alpha thing", "gamma thing", "beta thing"], ordered=False)
    assert res.passed and "order not checked" in res.reason


# ---------------------------------------------------------------------------
# verification glue
# ---------------------------------------------------------------------------

class _ReplayExecutor:
    """Returns a canned extraction per parameter value; counts the calls."""

    def __init__(self, by_value, fail=()):
        self.by_value, self.fail, self.calls = by_value, set(fail), []

    def execute(self, template, params):
        value = list(params.values())[0] if params else ""
        self.calls.append((template.url, value))
        if value in self.fail or template.url not in self.by_value:
            return {"success": False, "error": "injected failure", "method": "api"}
        return {"success": True, "method": "api", "elapsed_ms": 12.0, "bytes": 100,
                "extracted": {"items": self.by_value[template.url][value]}}


def test_verify_template_passes_only_on_enough_fresh_values():
    tmpl = RequestTemplate(url="https://x.test/api", query={"q": "{q}"}, params=["q"],
                           extract={"items": "r[*].id"})
    ex = _ReplayExecutor({"https://x.test/api": {
        "regex": ["regex", "regex lite", "regex automata"],
        "tokio": ["tokio", "tokio util", "tokio stream"]}})
    cases = [{"value": "regex", "ui_items": ["regex", "regex-lite", "regex-automata"]},
             {"value": "tokio", "ui_items": ["tokio", "tokio-util", "tokio-stream"]}]
    res = verify_template(tmpl, cases, ex)
    assert res.passed and res.passed_count == 2

    one = verify_template(tmpl, cases[:1], ex)
    assert one.passed is False and "required" in one.reason


def test_duplicate_normalized_parameter_tuples_do_not_count_as_two_inputs():
    tmpl = RequestTemplate(url="https://x.test/api", query={"q": "{q}"}, params=["q"],
                           extract={"items": "r[*].id"})
    ex = _ReplayExecutor({"https://x.test/api": {
        "Rust-Async": ["Rust Async", "runtime", "crate"],
        "rust async": ["Rust Async", "runtime", "crate"]}})
    cases = [
        {"value": "Rust-Async", "ui_items": ["Rust Async", "runtime", "crate"]},
        {"value": "rust async", "ui_items": ["Rust Async", "runtime", "crate"]},
    ]
    result = verify_template(tmpl, cases, ex)
    assert result.passed is False
    assert "1 distinct normalized" in result.reason
    assert len(ex.calls) == 1


def test_verify_template_fails_when_the_api_step_fails():
    tmpl = RequestTemplate(url="https://x.test/api", query={"q": "{q}"}, params=["q"],
                           extract={"items": "r[*].id"})
    ex = _ReplayExecutor({"https://x.test/api": {"regex": ["regex", "regex lite", "regex auto"],
                                                 "tokio": ["tokio", "tokio util", "tokio fs"]}},
                         fail=["tokio"])
    cases = [{"value": "regex", "ui_items": ["regex", "regex-lite", "regex-auto"]},
             {"value": "tokio", "ui_items": ["tokio", "tokio-util", "tokio-fs"]}]
    res = verify_template(tmpl, cases, ex)
    assert res.passed is False and "injected failure" in res.reason


def test_cases_from_recordings_excludes_the_recorded_value():
    recs = [fixture("crates_search.fresh1.json"), fixture("crates_search.fresh2.json"),
            fixture("crates_search.rec.json")]
    cases = cases_from_recordings(recs, "serde")
    assert [c["value"] for c in cases] == ["regex", "tokio"]
    assert all(c["ui_items"] for c in cases)


def test_validation_cases_exclude_fitting_values_and_normalized_duplicates():
    recs = [
        {"param": {"value": "Rust-Async"}, "ui": {"items": ["one"]}},
        {"param": {"value": "rust async"}, "ui": {"items": ["duplicate"]}},
        {"param": {"value": "tokio"}, "ui": {"items": ["two"]}},
    ]
    cases = cases_from_recordings(recs, "serde", excluded_values=["RUST ASYNC"])
    assert [case["value"] for case in cases] == ["tokio"]


def test_compile_and_verify_lets_the_verifier_choose_among_ambiguous_candidates():
    rec = fixture("crates_search.rec.json")
    fresh = [fixture("crates_search.fresh1.json"), fixture("crates_search.fresh2.json")]
    ui = {c["value"]: c["ui_items"] for c in cases_from_recordings(fresh, "serde")}
    winner = "https://crates.io/api/v1/crates"
    ex = _ReplayExecutor({winner: {v: items for v, items in ui.items()}})
    compiled, proof = compile_and_verify(rec, fresh, ex)
    assert compiled.template.url == winner
    assert proof.passed, proof.reason


def test_proof_preflight_runs_before_any_proof_request():
    rec = fixture("crates_search.rec.json")
    fresh = [fixture("crates_search.fresh1.json"), fixture("crates_search.fresh2.json")]
    ex = _ReplayExecutor({})
    seen = []
    _compiled, proof = compile_and_verify(
        rec, fresh, ex, preflight=lambda compiled: seen.append(compiled.template.url)
        or "host was not approved")
    assert seen
    assert "not approved" in proof.reason
    assert ex.calls == [], "permission/origin refusal must precede proof traffic"


def test_compile_and_verify_reports_honestly_when_extraction_failed():
    compiled, proof = compile_and_verify(fixture("npm_search.rec.json"), [])
    assert compiled.template is None
    assert proof.passed is False and "extraction failed" in proof.reason


# ---------------------------------------------------------------------------
# persistence + the ladder
# ---------------------------------------------------------------------------

class _FakeStorage:
    def __init__(self, base):
        self.base = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base, f"workflow_{workflow_id}")


def test_compiled_record_round_trips_with_its_evidence():
    tmp = tempfile.mkdtemp()
    try:
        storage = _FakeStorage(tmp)
        tmpl = compile_recording(fixture("crates_search.rec.json")).template
        rec = compiled_record(tmpl, {"passed": True, "passed_count": 2}, workflow="crates_search")
        path = persist_compiled(storage, "abc123", rec)
        assert os.path.basename(path) == COMPILED_FILE
        back = load_compiled(storage, "abc123")
        assert back["template"]["url"] == tmpl.url
        assert back["proof"]["passed"] is True
        assert back["version"] == 2 and back["compiled_at"]
        assert back["template"]["parameter_schema"][0]["kind"] == "input"
        assert back["proof"]["template_digest"]
        assert load_compiled(storage, "missing") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_add_api_implementation_puts_the_compiled_rung_first():
    from anyuse.execution.deterministic import rank_implementations

    graph = StateGraph()
    graph.add_transition(UITransition(
        from_state="s1", to_state="s2", intent="search",
        implementations=[TransitionImpl(modality="ax", command={"title": "Search"})]))
    tmpl = compile_recording(fixture("crates_search.rec.json")).template
    add_api_implementation(graph, "s1", "s2", "search", tmpl)

    transition = graph.transitions[0]
    assert [i.modality for i in transition.implementations] == ["ax", "api"]
    assert rank_implementations(transition)[0].modality == "api"
    # Round-trips through the EXISTING persistence with no schema change.
    restored = StateGraph.from_dict(graph.to_dict()).transitions[0]
    api_impl = [i for i in restored.implementations if i.modality == "api"][0]
    assert RequestTemplate.from_dict(api_impl.command).url == tmpl.url


def test_recompiling_replaces_the_api_rung_and_leaves_the_ui_rungs_alone():
    graph = StateGraph()
    graph.add_transition(UITransition(
        from_state="s1", to_state="s2", intent="search",
        implementations=[TransitionImpl(modality="coordinate", command={"x": 1, "y": 2})]))
    first = compile_recording(fixture("crates_search.rec.json")).template
    add_api_implementation(graph, "s1", "s2", "search", first)
    second = RequestTemplate.from_dict({**first.to_dict(), "url": "https://crates.io/api/v2/crates"})
    add_api_implementation(graph, "s1", "s2", "search", second)

    impls = graph.transitions[0].implementations
    assert [i.modality for i in impls] == ["coordinate", "api"]
    assert impls[1].command["url"] == "https://crates.io/api/v2/crates"


# ---------------------------------------------------------------------------
# anti-bot diagnosis is shape-aware (a ladder re-proof false positive)
# ---------------------------------------------------------------------------

def test_served_result_page_that_mentions_captcha_is_not_an_interstitial():
    # A served result page whose RESULTS mention "recaptcha"/"CAPTCHA", with a
    # login-widget reCAPTCHA script and a CDN-injected challenge script among
    # its sub-requests. The old substring rule called this an interstitial.
    rec = fixture("served_page_mentions_captcha.rec.json")
    assert "captcha" in rec["ui"]["text"].lower()          # the old trigger is present
    assert "anti-bot" not in diagnose_failure(rec)
    res = compile_recording(rec, "q", "vibe")
    assert "anti-bot" not in (res.reason or "")


def test_real_challenge_pages_are_still_named():
    # Cloudflare-style interstitial: short text, challenge-platform request, no list.
    cf = {"final_url": "https://x.test/search?q=a",
          "network": [{"url": "https://x.test/cdn-cgi/challenge-platform/h/g/orchestrate",
                       "method": "GET", "status": 200, "mime": "text/javascript"}],
          "ui": {"items": [], "text": "Just a moment...\nx.test needs to review the security of your connection."}}
    assert "anti-bot" in diagnose_failure(cf)
    # A bare CAPTCHA page with no challenge request at all.
    bare = {"final_url": "https://x.test/search?q=a", "network": [],
            "ui": {"items": [], "text": "Please complete the CAPTCHA below to continue."}}
    assert "anti-bot" in diagnose_failure(bare)
    # A self-hosted anti-scraper that redirected to a challenge URL, even if the
    # page carries some list-looking decoration.
    goaway = {"final_url": "https://x.test/p?__goaway_challenge=meta-refresh&__goaway_id=1",
              "network": [], "ui": {"items": ["a", "b"], "text": "Loading"}}
    assert "anti-bot" in diagnose_failure(goaway)
    # A long page with no result list that merely mentions a captcha is not one.
    long_page = {"final_url": "https://x.test/about", "network": [],
                 "ui": {"items": [], "text": "About our captcha policy. " + "x " * 3000}}
    assert "anti-bot" not in diagnose_failure(long_page)


def test_urlify_keeps_a_trailing_slash():
    # the ladder re-proof compiled ".../result/?search=htop" to ".../result",
    # which the server answered with 404
    url, query, _d, _u, why = urlify("https://x.test/result/?search=htop&sv=15.0", "q", "htop")
    assert why == "" and url == "https://x.test/result/"
    assert query["search"] == "{q}"
    url2, *_ = urlify("https://x.test/result?search=htop", "q", "htop")
    assert url2 == "https://x.test/result"


def test_error_document_reads_the_final_pages_status():
    from anyuse.execution.api_compile import error_document
    rec = {"final_url": "https://x.test/search?q=a",
           "network": [{"url": "https://x.test/search?q=a", "status": 404}]}
    assert error_document(rec) == "HTTP 404"
    rec["network"].append({"url": "https://x.test/search?q=a", "status": 200})
    assert error_document(rec) == ""                     # the last response counts
    assert error_document({"final_url": "https://x.test/", "network": [
        {"url": "https://x.test/app.js", "status": 500}]}) == ""   # not the page


def test_learn_ground_truth_skips_an_error_page():
    from anyuse.execution.learn_loop import derive_ground_truth
    rows = "".join(f"<li class='r'>{{v}} {i}</li>" for i in range(4))

    def page(v, status):
        url = f"https://x.test/s?q={v}"
        return {"param": {"name": "q", "value": v}, "final_url": url, "ui": {"items": []},
                "network": [{"url": url, "status": status}],
                "dom": f"<html><body><ul>{rows.format(v=v)}</ul></body></html>"}
    recs = [page("aa", 404), page("bb", 404)]
    assert derive_ground_truth(recs) == ""
    assert "error page (HTTP 404)" in recs[0]["recorder_warning"]
    ok = [page("aa", 200), page("bb", 200), page("cc", 404)]
    assert derive_ground_truth(ok)
    assert ok[0]["ui"]["items"] and ok[2]["ui"]["items"] == []



def test_row_label_reads_a_rows_heading_then_its_link_then_the_row():
    from anyuse.execution.html_extract import extract
    html = ("<ul>"
            "<li class='r'><h3><a>alpha</a></h3><p>first package, 12 downloads</p></li>"
            "<li class='r'><a class='name'>beta</a><span>v1.0</span><a>Homepage</a></li>"
            "<li class='r'><span>gamma plain row</span></li>"
            "<li class='r'><a>1</a><a>delta</a> notes</li></ul>")
    assert extract(html, "li.r", labels=True) == ["alpha", "beta", "gamma plain row", "delta"]
    assert extract(html, "li.r")[0] == "alpha first package, 12 downloads"


def _rows_and_labels_recording(value, names):
    rows = [f"{n} v1.0.{i} a {n} library All-Time Downloads : {1000 + i}" for i, n in enumerate(names)]
    body = json.dumps({"crates": [{"id": n, "description": f"a {n} library"} for n in names]})
    return {"param": {"name": "q", "value": value},
            "ui": {"items": rows, "labels": list(names), "text": "\n".join(rows)},
            "final_url": f"https://reg.test/search?q={value}", "site": "reg.test",
            "network": [{"url": "https://reg.test/search?q=" + value, "method": "GET", "status": 200,
                         "mime": "text/html", "body": "<html><div id=app></div></html>"},
                        {"url": f"https://reg.test/api/v1/crates?q={value}&per_page=10", "method": "GET",
                         "status": 200, "mime": "application/json", "body": body}]}


def test_whole_rows_that_explain_no_request_fall_back_to_the_row_names():
    sets = {"serde": ["serde", "serde_json", "serde_yaml"], "regex": ["regex", "regex-lite", "regex-syntax"],
            "tokio": ["tokio", "tokio-util", "tokio-stream"]}
    rec = _rows_and_labels_recording("serde", sets["serde"])
    rows_only = dict(rec, ui={k: v for k, v in rec["ui"].items() if k != "labels"})
    assert compile_recording(rows_only).template is None              # the old reading alone fails
    res = compile_recording(rec)
    assert res.template is not None and res.ground_truth_reading == "labels"
    assert res.template.extract == {"items": "crates[*].id"}

    class Reg:
        def send(self, method, url, headers, body, timeout_s):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps({"crates": [{"id": n} for n in sets[q]]}))
    fresh = [_rows_and_labels_recording(v, sets[v]) for v in ("regex", "tokio")]
    compiled, proof = compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=Reg()),
                                         preflight=lambda _c: "")
    assert proof.passed, proof.reason                                   # held to the same reading


def test_the_server_rendered_diagnosis_needs_the_labels_in_the_html():
    rec = _rows_and_labels_recording("serde", ["serde", "serde_json", "serde_yaml"])
    rec["network"][1]["body"] = json.dumps({"facets": ["a", "b"]})      # no JSON explains the list
    spa = diagnose_failure(rec)
    assert "server-rendered" not in spa and "none carries the visible labels" in spa
    rec["network"][0]["body"] = "<html><ul>" + "".join(
        f"<li>{r}</li>" for r in rec["ui"]["items"]) + "</ul></html>"
    assert "server-rendered" in diagnose_failure(rec)

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
