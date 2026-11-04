"""A chain's intermediate binding traced from the recording's own traffic.

Recordings made from real traffic (a HAR, an attach session, a recording
directory) carry no ``bindings``. The proof traces each one: the value the
chain's first step reads (with its row rule) in the recorded search response,
carried by a later recorded request of the second step, whose response shows
what the page showed. Also: a producer whose keys are JSON numbers, and named
fields whose values are numbers. Entirely offline: the transport is injected.
"""
import json
import urllib.parse

from anyuse.execution.api_compile import CHAIN_PARAM, values_match
from anyuse.execution.api_step import (
    ApiResponse,
    ApiStepExecutor,
    RequestPlan,
    load_command,
)
from anyuse.execution.api_verify import (
    compare_named_fields,
    compile_and_verify,
    derive_bindings,
)

ORIGIN = "https://registry.test"

#: popularity-ranked search results per query
RESULTS = {
    "serde": ["serde", "serde_json", "serde_derive"],
    "tokio": ["tokio", "tokio-util"],
    "rand": ["rand", "rand_core"],
    "regex": ["regex", "regex-syntax"],
    "log": ["env_logger", "log", "log4rs"],
    "toml": ["toml_edit", "toml"],
    "clap": ["clap", "clap_builder"],
    "ra": ["rayon", "rand"],
}
NAMES = sorted({name for rows in RESULTS.values() for name in rows})


def ident(name, numeric=False):
    index = NAMES.index(name)
    return 4000 + index if numeric else f"p{100 + index}"


def search_body(q, numeric=False):
    return {"crates": [{"id": ident(n, numeric), "name": n} for n in RESULTS[q]]}


def detail_body(name, numeric=False):
    return {"item": {"id": ident(name, numeric), "title": f"{name} package",
                     "summary": f"what {name} does",
                     "downloads": 1000 + 37 * len(name) + ord(name[0])}}


def releases_body(name):
    return {"releases": [{"title": f"{name} {len(name)}.{k}.0", "yanked": False}
                         for k in (3, 2, 1)]}


class Registry:
    """The search, package and releases endpoints of the fixture registry."""

    def __init__(self, numeric=False):
        self.numeric = numeric
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        parts = urllib.parse.urlsplit(url)
        ok = lambda doc: ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(doc))
        query = urllib.parse.parse_qs(parts.query)
        if parts.path == "/api/search":
            return ok(search_body(query["q"][0], self.numeric))
        if parts.path == "/api/item":
            name = next(n for n in NAMES if str(ident(n, self.numeric)) == query["id"][0])
            return ok(detail_body(name, self.numeric))
        segments = parts.path.split("/")
        name = next(n for n in NAMES if str(ident(n, self.numeric)) == segments[3])
        if segments[-1] == "releases":
            return ok(releases_body(name))
        return ok(detail_body(name, self.numeric))


def _get(url, body):
    return {"url": url, "method": "GET", "status": 200, "mime": "application/json",
            "bytes": 100, "body": json.dumps(body)}


def recording(q, opened, numeric=False, final="detail", named_fields=True,
              prefetch=None, typed_first=None):
    """Searching ``q`` and opening the result ``opened``, with no bindings.

    ``prefetch``: a result whose detail the page fetched before ``opened``.
    ``typed_first``: a shorter query the search box sent while typing.
    ``final="query"``: the detail is read from ``/api/item?id=…&view=full``."""
    network = []
    if typed_first:
        network.append(_get(f"{ORIGIN}/api/search?q={typed_first}",
                            search_body(typed_first, numeric)))
    network.append(_get(f"{ORIGIN}/api/search?q={urllib.parse.quote(q)}",
                        search_body(q, numeric)))
    if prefetch:
        network.append(_get(f"{ORIGIN}/api/package/{ident(prefetch, numeric)}",
                            detail_body(prefetch, numeric)))
    key = ident(opened, numeric)
    if final == "releases":
        rows = releases_body(opened)["releases"]
        network.append(_get(f"{ORIGIN}/api/package/{key}/releases", releases_body(opened)))
        return {"param": {"name": "q", "value": q},
                "ui": {"items": [r["title"] for r in rows], "text": ""}, "network": network}
    item = detail_body(opened, numeric)["item"]
    detail_url = (f"{ORIGIN}/api/item?view=full&id={key}" if final == "query"
                  else f"{ORIGIN}/api/package/{key}")
    network.append(_get(detail_url, detail_body(opened, numeric)))
    shown = [item["title"], item["summary"], str(item["downloads"])]
    ui = {"items": shown, "text": ""}
    if named_fields:
        ui["fields"] = {"field_1": shown[0], "field_2": shown[1], "field_3": shown[2]}
    return {"param": {"name": "q", "value": q}, "ui": ui, "network": network}


def prove(base, fit, fresh, numeric=False):
    return compile_and_verify(base, fresh, executor=ApiStepExecutor(transport=Registry(numeric)),
                              fitting_recordings=[fit])


def first_rule(**kw):
    return (recording("serde", "serde", **kw), recording("tokio", "tokio", **kw))


# ---------------------------------------------------------------------------
# bindings traced from the traffic
# ---------------------------------------------------------------------------

def test_a_chain_recorded_without_bindings_is_proved_from_its_traffic():
    base, fit = first_rule()
    compiled, proof = prove(base, fit, [recording("rand", "rand"), recording("regex", "regex")])
    assert compiled.status == "chained", compiled.reason
    assert proof.passed, proof.reason
    assert "traced from the recorded traffic" in proof.reason
    how = proof.cases[0].contract["bindings_derived"][CHAIN_PARAM]
    assert how == {"step": "find", "path": "crates[*].id", "row": 0, "rule": "first",
                   "requests": [0, 1], "consumer": "open"}
    assert "p1" not in json.dumps(how), "no response value is stored with the derivation"


def test_a_list_page_recorded_as_a_har_is_proved_without_named_fields_or_bindings():
    base, fit = first_rule(final="releases")
    compiled, proof = prove(base, fit, [recording("rand", "rand", final="releases"),
                                        recording("regex", "regex", final="releases")])
    assert compiled.status == "chained" and compiled.template.shape == "list"
    assert proof.passed, proof.reason


def test_a_detail_page_recorded_as_a_har_is_proved_without_named_fields():
    base, fit = first_rule(named_fields=False)
    compiled, proof = prove(base, fit, [recording("rand", "rand", named_fields=False),
                                        recording("regex", "regex", named_fields=False)])
    assert compiled.status == "chained" and compiled.template.shape == "fields", compiled.reason
    assert proof.passed, proof.reason
    for case in proof.cases:
        assert case.contract["fields_named_by"] == {"field_1": "json_key", "field_2": "json_key",
                                                    "field_3": "json_key"}, case.contract


def test_a_value_the_opened_response_does_not_hold_is_not_named():
    base, fit = first_rule(named_fields=False)
    altered = recording("rand", "rand", named_fields=False)
    altered["ui"]["items"][2] = "99999"
    _compiled, proof = prove(base, fit, [altered, recording("regex", "regex", named_fields=False)])
    assert not proof.passed
    case = next(c for c in proof.cases if c.value == "rand")
    assert not case.passed and "item.downloads" in case.reason and "field_3" in case.reason, case.reason
    assert next(c for c in proof.cases if c.value == "regex").passed


def test_named_fields_compare_normalised_text():
    expected = {"field_1": "Serde Package", "field_2": "What serde does.", "field_3": "1307"}
    got = {"field_1": "serde package", "field_2": "what serde does", "field_3": 1307}
    assert compare_named_fields(expected, got).passed
    swapped = compare_named_fields(expected, {**got, "field_1": got["field_2"], "field_2": got["field_1"]})
    assert not swapped.passed and swapped.missing == ["field_1", "field_2"], swapped.missing


def test_a_name_rule_is_traced_with_the_rule():
    compiled, proof = prove(recording("log", "log"), recording("tokio", "tokio"),
                            [recording("rand", "rand"), recording("toml", "toml")])
    assert compiled.template.steps[0].pick, compiled.reason
    assert proof.passed, proof.reason
    traced = {c.value: c.contract["bindings_derived"][CHAIN_PARAM] for c in proof.cases}
    assert (traced["toml"]["row"], traced["toml"]["rule"]) == (1, "exact")
    assert (traced["rand"]["row"], traced["rand"]["rule"]) == (0, "exact")


def test_a_recordings_own_bindings_are_kept():
    base, fit = first_rule()
    stated = recording("rand", "rand")
    stated["bindings"] = {CHAIN_PARAM: ident("rand_core")}
    _compiled, proof = prove(base, fit, [stated, recording("regex", "regex")])
    assert not proof.passed
    case = next(c for c in proof.cases if c.value == "rand")
    assert case.reason == "intermediate binding mismatch: chain_id"
    assert "bindings_derived" not in case.contract


def test_a_recording_that_opened_another_result_is_refused_with_what_to_do():
    base, fit = first_rule()
    _compiled, proof = prove(base, fit, [recording("rand", "rand"),
                                         recording("regex", "regex-syntax")])
    assert not proof.passed
    case = next(c for c in proof.cases if c.value == "regex")
    assert case.reason.startswith("chain proof requires named intermediate bindings per input: "
                                  "the recording did not open the first result")
    assert "open the first result" in case.reason.rsplit("Record this input again", 1)[1]


def test_a_detail_fetched_alongside_is_not_taken_for_the_page_that_was_opened():
    base, fit = first_rule()
    # the page prefetched the first result's detail, then the person opened the second
    shown = recording("regex", "regex-syntax", prefetch="regex")
    _compiled, proof = prove(base, fit, [recording("rand", "rand"), shown])
    assert not proof.passed
    case = next(c for c in proof.cases if c.value == "regex")
    assert "the recording fetched the first result" in case.reason
    assert "not what the page showed" in case.reason


def test_the_search_sent_while_typing_is_not_the_one_the_value_is_read_from():
    compiled, _proof = prove(*first_rule(), [recording("rand", "rand"),
                                             recording("regex", "regex")])
    rec = recording("rand", "rand", typed_first="ra")
    bindings, how, why = derive_bindings(compiled.template, rec)
    assert why == "" and bindings == {CHAIN_PARAM: ident("rand")}
    assert how[CHAIN_PARAM]["requests"] == [1, 2]


def test_a_value_the_second_request_carries_in_its_query_is_traced():
    base, fit = first_rule(final="query")
    compiled, proof = prove(base, fit, [recording("rand", "rand", final="query"),
                                        recording("regex", "regex", final="query")])
    assert compiled.template.steps[1].template.query["id"] == "{chain_id}", compiled.reason
    assert proof.passed, proof.reason
    other = recording("clap", "clap_builder", final="query")
    _b, _how, why = derive_bindings(compiled.template, other)
    assert why.startswith("the recording did not open the first result")


def test_a_search_for_text_with_a_slash_is_traced():
    compiled, _proof = prove(*first_rule(), [recording("rand", "rand"),
                                             recording("regex", "regex")])
    for sent in ("ac/dc", "ac%2Fdc"):
        rec = recording("rand", "rand")
        rec["param"]["value"] = "ac/dc"
        rec["network"][0]["url"] = f"{ORIGIN}/api/search?q={sent}"
        bindings, _how, why = derive_bindings(compiled.template, rec)
        assert why == "" and bindings == {CHAIN_PARAM: ident("rand")}, (sent, why)


def test_a_detail_fetched_before_the_search_is_not_the_one_that_was_opened():
    compiled, _proof = prove(*first_rule(), [recording("rand", "rand"),
                                             recording("regex", "regex")])
    rec = recording("rand", "rand")
    rec["network"].reverse()
    _b, _how, why = derive_bindings(compiled.template, rec)
    assert why.startswith("the recording did not open the first result"), why


def test_a_name_rule_input_that_names_no_result_says_so():
    compiled, _proof = prove(recording("log", "log"), recording("tokio", "tokio"),
                             [recording("rand", "rand"), recording("toml", "toml")])
    assert compiled.template.steps[0].pick, compiled.reason
    _b, _how, why = derive_bindings(compiled.template, recording("ra", "rayon"))
    assert why.startswith("no result in the recorded response of step 1 (find) is named 'ra'")
    assert why.endswith("Use an input that has a result of that name")


def test_a_case_typed_without_traffic_says_it_needs_a_recording():
    base, fit = first_rule(final="releases")
    typed = {"param": {"name": "q", "value": "regex"},
             "ui": {"items": [r["title"] for r in releases_body("regex")["releases"]]},
             "network": []}
    _compiled, proof = prove(base, fit, [recording("rand", "rand", final="releases"), typed])
    case = next(c for c in proof.cases if c.value == "regex")
    assert "has no JSON traffic to trace the chain from" in case.reason


# ---------------------------------------------------------------------------
# a producer whose keys are numbers
# ---------------------------------------------------------------------------

def _schema_type(rows):
    return next(row["type"] for row in rows if row["name"] == CHAIN_PARAM)


def test_numeric_result_keys_type_the_derived_value_and_the_chain_runs():
    base, fit = first_rule(numeric=True)
    compiled, proof = prove(base, fit, [recording("rand", "rand", numeric=True),
                                        recording("regex", "regex", numeric=True)],
                            numeric=True)
    plan = compiled.template
    assert _schema_type(plan.parameter_schema) == "integer"
    assert _schema_type(plan.steps[1].template.parameter_schema) == "integer"
    assert proof.passed, proof.reason
    stored = load_command(json.loads(json.dumps(plan.to_dict())))
    assert isinstance(stored, RequestPlan)
    assert _schema_type(stored.parameter_schema) == "integer"
    res = ApiStepExecutor(transport=Registry(numeric=True)).execute(stored, {"q": "clap"})
    assert res["success"] is True, res.get("error")
    assert res["plan_trace"][0]["bound"][CHAIN_PARAM] == ident("clap", numeric=True)
    assert res["extracted"]["field_1"] == "clap package"


def test_the_derived_value_is_a_number_only_when_every_recording_returned_one():
    from anyuse.execution.api_compile import _ChainObservation, _derived_type, _key_type
    data = {"crates": [{"id": 4021}, {"id": 40.5}, {"id": "p7"}]}
    assert [_key_type(data, "crates[*].id", literal) for literal in ("4021", "40.5", "p7")] \
        == ["integer", "number", "string"]
    seen = lambda *types: [_ChainObservation(plan=None, literal="", input_value="",
                                             signature=(), key_type=t) for t in types]
    assert _derived_type(seen("integer", "integer")) == "integer"
    assert _derived_type(seen("integer", "number")) == "number"
    assert _derived_type(seen("integer", "string")) == "string"


def test_string_result_keys_stay_strings():
    base, fit = first_rule()
    compiled, _proof = prove(base, fit, [recording("rand", "rand"), recording("regex", "regex")])
    assert _schema_type(compiled.template.parameter_schema) == "string"


# ---------------------------------------------------------------------------
# non-text values compared by value
# ---------------------------------------------------------------------------

def test_values_match_compares_numbers_and_booleans_by_value():
    assert values_match("1005", 1005) and values_match(1005, "1005")
    assert values_match(2.5, "2.5") and values_match(3, 3.0)
    assert not values_match("1006", 1005) and not values_match("about 1005", 1005)
    assert values_match(True, "true") and not values_match(True, 1)
    assert not values_match(None, "x") and not values_match(0, None)
    assert values_match([1, 2], [1, 2]) and not values_match("1", [1])
    assert values_match("regex lite", "regex lite crate"), "text keeps the containment rule"


def test_a_named_field_holding_a_number_is_compared_by_value():
    expected = {"field_1": "serde package", "field_2": "what serde does", "field_3": "1307"}
    result = compare_named_fields(expected, {**expected, "field_3": 1307})
    assert result.passed, result.reason
    wrong = compare_named_fields(expected, {**expected, "field_3": 1308})
    assert not wrong.passed and wrong.missing == ["field_3"]


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
