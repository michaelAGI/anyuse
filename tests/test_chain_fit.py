"""A search-then-open chain fitted on an input generated from the site's own search
(``execution/chain_fit.py``).

With three recorded inputs no recording is free to fit a chain on. The compile takes
another result's name from the first recording's search response, sends the chain's own
search and open requests for it, and fits the chain on that input when its traffic shows
the same relation. The generated input is no proof case. The fixture registry
(``OpenRegistry``) also answers names that are no recorded input; its variants draw the
result page with a script, open another result's page, or answer only the recorded words.
Entirely offline: the transport is injected, the HARs are temporary files, models are fakes.
"""
import copy
import json
import os
import sys
import urllib.parse

import pytest

import test_chain_sources as TCS
from anyuse.execution import chain_fit as CF
from anyuse.execution import model_check as MC
from anyuse.execution.api_compile import (CHAIN_UNCORROBORATED, chain_observations,
                                          compiled_record, proof_matches_template)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.attach import compile_sessions
from anyuse.execution.har_import import (HarCase, compile_from_har, har_to_recording,
                                         load_har)

SHELL = "<html><head><title>Registry</title></head><body><div id=app></div></body></html>"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_CHAIN_FIT",
                 "ANYUSE_OPENJEV_URL", "ANYUSE_JUDGE", "ANYUSE_CONFIRMER_LAYA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_MODEL_CHECKS", "none")


def name_of(ident):
    return next((n for n in TCS.NAMES if TCS.ident(n) == ident), None)


class OpenRegistry:
    """The fixture registry, answering every name of its index.

    ``/api/search?q=`` answers the recorded word's rows, else the rows whose names start
    with ``q``. ``mode``: ``shell`` serves every result page as an empty script shell;
    ``wrong`` serves a result page that is no recorded word's first result with another
    package's releases; ``same`` serves serde's page for every result; ``deaf`` answers
    no rows for a word that was not recorded; ``echo`` answers serde's rows for it."""

    def __init__(self, mode=""):
        self.mode, self.sent = mode, []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append((method, url))
        parts = urllib.parse.urlsplit(url)
        ok = lambda doc: ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(doc))
        if parts.path == "/api/search":
            q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
            if q in TCS.RESULTS:
                return ok(TCS.search_doc(q))
            if self.mode == "echo":
                return ok(TCS.search_doc("serde"))
            rows = [] if self.mode == "deaf" else [n for n in TCS.NAMES if n.startswith(q)]
            return ok({"results": [{"id": TCS.ident(n), "name": n} for n in rows]})
        seg = parts.path.split("/")
        if parts.path.startswith("/api/package/") and parts.path.endswith("/releases"):
            name = name_of(seg[3])
            return ok({"releases": [{"title": t} for t in TCS.releases(name)]})
        if parts.path.startswith("/package/"):
            name = name_of(seg[2])
            if self.mode == "shell":
                return ApiResponse(200, {"Content-Type": "text/html"}, SHELL)
            firsts = {rows[0] for rows in TCS.RESULTS.values()}
            if self.mode == "wrong" and name not in firsts:
                name = "regex-lite"
            if self.mode == "same":
                name = "serde"
            return ApiResponse(200, {"Content-Type": "text/html"}, TCS.page(name))
        return ApiResponse(404, {"Content-Type": "text/plain"}, "not found")


def shell_traffic(q):
    """A search whose opened result's page is drawn by a script."""
    return [(u, m, SHELL if m == "text/html" and "/package/" in u else t)
            for u, m, t in TCS.traffic(q)]


def hars(tmp, values, fetch=TCS.traffic):
    return [HarCase(path=TCS.write_har(tmp, q, fetch(q)), param_value=q,
                    expected=TCS.releases(TCS.RESULTS[q][0])) for q in values]


def compile_three(tmp_path, mode="", fetch=TCS.traffic, values=("serde", "tokio", "rand")):
    site = OpenRegistry(mode)
    compiled, proof, report = compile_from_har(hars(str(tmp_path), values, fetch),
                                               executor=ApiStepExecutor(transport=site),
                                               preflight=lambda _c: "")
    return compiled, proof, report, site


def serde_recording(tmp_path):
    case = hars(str(tmp_path), ["serde"])[0]
    return har_to_recording(load_har(case.path), "q", "serde", case.expected)


def observation(rec):
    return next(o for o in chain_observations(rec) if o.literal != o.input_value)


def generated_sent(site):
    """The requests sent for a value that was not recorded: a search for another
    name, or the releases or page of a result no recorded search opened."""
    opened = {rows[0] for rows in TCS.RESULTS.values()}
    out = []
    for _method, url in site.sent:
        parts = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
        seg = parts.path.split("/")
        ident = (seg[3] if parts.path.startswith("/api/package/") else
                 seg[2] if parts.path.startswith("/package/") else "")
        if (q and q not in TCS.RESULTS) or (ident and name_of(ident) not in opened):
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# three HARs
# ---------------------------------------------------------------------------

def test_three_hars_compile_a_chain_fitted_on_a_name_from_the_first_hars_search(tmp_path):
    compiled, proof, report, site = compile_three(tmp_path)
    assert compiled.status == "chained", compiled.reason
    assert proof.passed and proof.effective_status == "proved", proof.reason
    split = proof.split
    assert (split["fitting"], split.get("fitting_generated"), split["validation"]) == (1, 1, 2)
    assert sorted(c.value for c in proof.cases) == ["rand", "tokio"]
    assert all(c.contract.get("bindings_derived") for c in proof.cases)
    fit = proof.chain_fit
    assert fit["value"] in ("serde_json", "serde_derive")
    assert (fit["from"], fit["via"], fit["ui_from"]) == ("search_rows", "http", "page")
    assert report["fitting"] == [] and report["generated_fitting"] == fit
    # search, open and the result page for the one value it was fitted on
    assert len(generated_sent(site)) == 3, generated_sent(site)
    assert "1 generated from the site's own search" in proof.scope


def test_the_generated_fit_is_never_a_proof_case_nor_a_recorded_input(tmp_path):
    rec = serde_recording(tmp_path)
    obs = observation(rec)
    values, source = CF.fit_values(obs, rec, exclude=["tokio", "serde_json"])
    assert (values, source) == (["serde_derive"], CF.FROM_ROWS)
    _compiled, proof, _report, _site = compile_three(tmp_path)
    assert proof.chain_fit["value"] not in {c.value for c in proof.cases}
    assert proof.split["validation"] == 2


def test_a_script_drawn_result_page_fits_on_the_open_requests_own_answer(tmp_path):
    compiled, proof, _report, _site = compile_three(tmp_path, mode="shell", fetch=shell_traffic)
    assert compiled.status == "chained", (compiled.reason, compiled.chain_fit_note)
    assert proof.passed and proof.effective_status == "proved", proof.reason
    assert proof.chain_fit["ui_from"] == "response"


def test_a_result_page_that_disagrees_with_the_open_request_fits_nothing(tmp_path):
    compiled, proof, report, _site = compile_three(tmp_path, mode="wrong")
    assert compiled.template is None and not proof.passed
    assert compiled.chain_needs_fitting
    for value in ("serde_json", "serde_derive"):
        assert repr(value) in compiled.chain_fit_note, compiled.chain_fit_note
    assert "does not show what opening it answered" in compiled.chain_fit_note
    assert "needs 4 HARs" in proof.reason and "the second fits the chain" in proof.reason
    assert report["fitting"] == [] and "generated_fitting" not in report


def test_a_result_page_that_does_not_change_with_the_opened_result_fits_nothing(tmp_path):
    # the page answers, but shows the recorded result whatever was opened: the open
    # request's answer does not stand in for it
    compiled, proof, _report, site = compile_three(tmp_path, mode="same")
    assert compiled.template is None and not proof.passed
    for value in ("serde_json", "serde_derive"):
        assert f"{value!r}: {CF.NO_LIST_CHANGED}" in compiled.chain_fit_note, \
            compiled.chain_fit_note
    # search, open and page for each value tried, and no proof request
    assert len(generated_sent(site)) == 6 and len(site.sent) == 6, site.sent


def test_a_search_that_ignores_its_input_refuses_before_opening_anything(tmp_path):
    compiled, proof, _report, site = compile_three(tmp_path, mode="echo")
    assert compiled.template is None and not proof.passed
    assert ("'serde_json': the search answered the same results as for the recorded input"
            in compiled.chain_fit_note), compiled.chain_fit_note
    # one search per value tried, nothing opened
    assert [urllib.parse.urlsplit(url).path for url in generated_sent(site)] == [
        "/api/search", "/api/search"], site.sent


def test_only_an_uncorroborated_chain_generates_a_fitting_input():
    from anyuse.execution.api_compile import CompileResult
    from anyuse.execution.api_verify import _may_generate
    chain = CompileResult(reason=CHAIN_UNCORROBORATED, chain_needs_fitting=True)
    assert _may_generate(chain, None) and not _may_generate(chain, False)
    assert not _may_generate(CompileResult(reason=CHAIN_UNCORROBORATED), True)
    other = CompileResult(reason="the landing request is uncorroborated",
                          chain_needs_fitting=True)
    assert not _may_generate(other, True)
    # a recording that shows no chain sends nothing, not even the host question
    site, asked = OpenRegistry(), []
    gen = CF.generate({"param": {"name": "q", "value": "serde"}, "ui": {"items": []},
                       "network": []}, executor=ApiStepExecutor(transport=site),
                      preflight=lambda c: asked.append(c) or "")
    assert gen.recording is None and gen.reason == "the first recording shows no chain to fit"
    assert site.sent == [] and asked == []


def test_a_search_that_does_not_answer_other_names_refuses_and_says_what_was_tried(tmp_path):
    compiled, proof, _report, _site = compile_three(tmp_path, mode="deaf")
    assert compiled.template is None and not proof.passed
    assert "'serde_json': the search listed nothing to open" in compiled.chain_fit_note
    assert "did not fit it ('serde_json': the search listed nothing to open" in proof.reason


def test_typed_proof_inputs_send_no_fit_requests(tmp_path):
    site = OpenRegistry()
    cases = hars(str(tmp_path), ["serde"]) + [
        HarCase(path="", param_value=q, expected=TCS.releases(q)) for q in ("tokio", "rand")]
    compiled, proof, _report = compile_from_har(cases, executor=ApiStepExecutor(transport=site),
                                                preflight=lambda _c: "")
    assert not proof.passed and site.sent == []
    assert "carry no traffic" in compiled.chain_fit_note
    assert "A typed --expect case cannot prove a chain" in proof.reason


def test_chain_fit_off_keeps_the_refusal_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_CHAIN_FIT", "off")
    compiled, proof, report, site = compile_three(tmp_path)
    assert compiled.template is None and not proof.passed
    assert compiled.reason.startswith(CHAIN_UNCORROBORATED) and not compiled.chain_fit_note
    assert site.sent == [] and "needs 4 HARs" in proof.reason


def test_a_preflight_refusal_sends_nothing_and_is_the_reason(tmp_path):
    site = OpenRegistry()
    asked = []

    def gate(compiled):
        asked.append(compiled.proof_requests)
        return "proof not run: you declined registry.test"
    compiled, proof, _report = compile_from_har(hars(str(tmp_path), ["serde", "tokio", "rand"]),
                                                executor=ApiStepExecutor(transport=site),
                                                preflight=gate)
    assert site.sent == [] and not proof.passed
    assert compiled.chain_fit_note == "proof not run: you declined registry.test"
    # the host question names what the fitting input and the proof would send
    assert asked == [(6, 13)]


# ---------------------------------------------------------------------------
# which field names a result
# ---------------------------------------------------------------------------

def _no_model(*_a):
    raise AssertionError("the model was asked")


def test_the_name_field_is_the_row_field_that_names_the_input(tmp_path):
    rec = serde_recording(tmp_path)
    with MC.scope("compile"), MC.using(_no_model):
        assert CF.name_field(observation(rec), rec) == ("name", CF.FROM_ROWS)


ROWS = [{"id": "x7001", "slug": "fastjson", "title": "Fast JSON",
         "summary": "streams JSON quickly"},
        {"id": "x7002", "slug": "jsonkit", "title": "Json Kit",
         "summary": "a toolkit for documents"},
        {"id": "x7003", "slug": "parsejs", "title": "Parse Js", "summary": "reads records lazily"}]


def custom(shown):
    """A search for 'json parser' whose rows name no field 'json parser'; the opened
    result's page shows ``shown``."""
    page = (f"<html><body><main><h1>{shown}</h1><ul>"
            + "".join(f"<li>{shown} 1.{k}.0</li>" for k in (3, 2, 1))
            + "</ul></main></body></html>")
    net = TCS.network([
        ("https://lib.test/api/search?q=json+parser", "application/json",
         json.dumps({"hits": ROWS})),
        ("https://lib.test/api/lib/x7001/versions", "application/json",
         json.dumps({"versions": [{"label": f"{shown} 1.{k}.0"} for k in (3, 2, 1)]})),
        ("https://lib.test/lib/x7001", "text/html", page)])
    items = [f"{shown} 1.{k}.0" for k in (3, 2, 1)]
    rec = {"param": {"name": "q", "value": "json parser"},
           "ui": {"items": items, "text": "\n".join(items)},
           "final_url": "https://lib.test/lib/x7001", "site": "lib.test", "network": net}
    return rec, observation(rec)


def test_a_field_the_opened_page_shows_names_the_rows_without_a_model():
    rec, obs = custom("Fast JSON")
    with MC.scope("compile"), MC.using(_no_model):
        assert CF.name_field(obs, rec) == ("title", CF.FROM_ROWS)


def test_without_a_naming_field_the_model_picks_one_of_the_rows_fields():
    rec, obs = custom("Release notes")
    asked = []

    def fake(instructions, state, _obs):
        line = state.rsplit(">> ", 1)[1]
        asked.append(line.split(":")[0])
        return 0.9 if line.startswith("title:") else 0.05
    with MC.using(fake):
        assert CF.name_field(obs, rec, model_scope="compile") == ("title", CF.FROM_MODEL)
    # the options are exactly the rows' own text fields, the key left out
    assert asked == ["slug", "title", "summary"]
    # a scope opened further out (a tool call whose failure starts a recompile) is not
    # the caller naming one: nothing is asked
    for outer in ("product", "compile"):
        with MC.scope(outer), MC.using(fake):
            assert CF.name_field(obs, rec) == ("slug", CF.FROM_FALLBACK)
    assert asked == ["slug", "title", "summary"]


def test_a_model_answer_that_is_unclear_failing_or_missing_is_no_opinion():
    rec, obs = custom("Release notes")

    def low(*_a):
        return 0.3

    def failing(*_a):
        raise RuntimeError("the model is down")

    for fake in (low, failing, None):
        with MC.using(fake):
            assert CF.name_field(obs, rec, model_scope="compile") == (
                "slug", CF.FROM_FALLBACK), fake
    # without a scope named nothing is asked, even a model that would answer
    asked = []

    def sure(_instructions, state, _obs):
        asked.append(state)
        return 0.9 if state.rsplit(">> ", 1)[1].startswith("title:") else 0.05
    with MC.using(sure):
        assert CF.name_field(obs, rec) == ("slug", CF.FROM_FALLBACK)
    assert asked == []


# ---------------------------------------------------------------------------
# sessions, learn, a recording folder
# ---------------------------------------------------------------------------

def test_three_sessions_and_three_learn_values_reach_checked_with_a_generated_fit(monkeypatch):
    from anyuse.execution.learn_loop import _learned_source
    monkeypatch.setattr(TCS, "executor", lambda: ApiStepExecutor(transport=OpenRegistry()))
    compiled, proof, report, _recs = compile_sessions(
        [TCS.session(q) for q in ("serde", "tokio", "rand")],
        executor=ApiStepExecutor(transport=OpenRegistry()), preflight=lambda _c: "")
    assert compiled.status == "chained", (compiled.reason, compiled.chain_fit_note)
    assert proof.passed and proof.effective_status == "checked (selection not held out)"
    assert proof.split.get("fitting_generated") == 1
    assert report["chain_fit"]["value"] in ("serde_json", "serde_derive")
    assert "chain_fitted_on" not in report
    res, kept = TCS.learn(["serde", "tokio", "rand"])
    assert res.outcome == "tier1", res.reason
    assert kept["compiled"].status == "chained"
    assert res.proof_status == "checked (selection not held out)"
    assert res.split.get("fitting_generated") == 1
    source = _learned_source(kept["recordings"], res)
    assert (source["fitting"], source["selection"], source["fresh"]) == (
        [], [], ["releases.agent1.json", "releases.agent2.json"])


def test_a_recording_folder_without_fit1_compiles_on_a_generated_fit(tmp_path):
    from anyuse.execution.api_verify import compile_and_verify, compile_and_verify_with_fit
    recs = [har_to_recording(load_har(c.path), "q", c.param_value, c.expected)
            for c in hars(str(tmp_path), ["serde", "tokio", "rand", "regex"])]
    executor = ApiStepExecutor(transport=OpenRegistry())
    compiled, proof, fit = compile_and_verify_with_fit(
        recs[0], recs[2:], [], executor=executor, preflight=lambda _c: "")
    assert compiled.status == "chained" and proof.effective_status == "proved", proof.reason
    assert fit is not None and fit["origin"] == "generated_fit"
    # a recorded fitting input is used as before, and nothing is generated
    site = OpenRegistry()
    _c, given, _f = compile_and_verify_with_fit(recs[0], recs[2:], [recs[1], *recs[2:]],
                                                executor=ApiStepExecutor(transport=site),
                                                preflight=lambda _c: "")
    assert given.passed and given.chain_fit is None and generated_sent(site) == []
    _c, plain = compile_and_verify(recs[0], recs[2:], ApiStepExecutor(transport=OpenRegistry()),
                                   fitting_recordings=[recs[1]], preflight=lambda _c: "")
    plain_d, given_d = plain.to_dict(), given.to_dict()
    for d in (plain_d, given_d):
        for c in d.get("cases") or []:
            c["elapsed_ms"] = 0.0
        if isinstance(d.get("drift_anchor"), dict):
            d["drift_anchor"].pop("at", None)
    assert plain_d == given_d


def test_a_post_search_carries_its_method_and_body_into_the_fitting_recording():
    from anyuse.execution.api_step import RequestTemplate
    tmpl = RequestTemplate(method="POST", url="https://lib.test/api/search",
                           body='{"query": "{q}"}', params=["q"], readonly_post=True,
                           headers={"content-type": "application/json"})
    entry = CF._network_entry(tmpl, {"q": "json parser"},
                              {"url": "https://lib.test/api/search", "status": 200,
                               "data": {"hits": []}}, mime="application/json", kind="XHR")
    assert entry["method"] == "POST"
    assert json.loads(entry["post_data"]) == {"query": "json parser"}
    assert json.loads(entry["body"]) == {"hits": []}


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------

class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")


def test_the_record_keeps_the_generated_value_outside_the_proof(tmp_path):
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.execution.api_recompile import RecompileManager
    from anyuse.execution.auto_recompile import stored_evidence
    paths = []
    for q in ("serde", "tokio", "rand"):
        case = hars(str(tmp_path), [q])[0]
        paths.append(str(tmp_path / f"{q}.rec.json"))
        with open(paths[-1], "w", encoding="utf-8") as fh:
            json.dump(har_to_recording(load_har(case.path), "q", q, case.expected), fh)
    recs = [json.load(open(p)) for p in paths]
    from anyuse.execution.api_verify import compile_and_verify_with_fit
    compiled, proof, _fit = compile_and_verify_with_fit(
        recs[0], recs[1:], [], executor=ApiStepExecutor(transport=OpenRegistry()),
        preflight=lambda _c: "")
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="releases",
                             source={"paths": {"recording": paths[0], "fitting": [],
                                               "selection": [], "fresh": paths[1:]}})
    assert set(record["chain_fit"]) == {"value", "from", "via", "ui_from"}
    assert "chain_fit" not in record["proof"] and proof_matches_template(record)
    stored_value = record["chain_fit"]["value"]
    other = "serde_derive" if stored_value == "serde_json" else "serde_json"
    record["chain_fit"]["value"] = other
    record["health"] = {"preferred": False, "demoted_reason": "3 failures in a row"}
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "releases", record)
    site = OpenRegistry()
    manager = RecompileManager(storage, lambda wid: stored_evidence(load_compiled(storage, wid)),
                               executor=ApiStepExecutor(transport=site),
                               proof_preflight=lambda _c: "")
    outcome = manager.recompile_now("releases", "the compiled request failed")
    stored = load_compiled(storage, "releases")
    assert outcome.persisted, outcome.reason
    # the stored value is tried first, and it stays outside the proof
    first = next(url for url in generated_sent(site))
    assert f"q={other}" in first
    assert stored["chain_fit"]["value"] == other and "chain_fit" not in stored["proof"]
    assert proof_matches_template(stored)


def test_a_recompile_from_stored_sessions_tries_the_stored_value_first(tmp_path):
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.execution.api_recompile import RecompileManager
    sessions = [TCS.session(q) for q in ("serde", "tokio", "rand")]
    compiled, proof, _report, _recs = compile_sessions(
        copy.deepcopy(sessions), executor=ApiStepExecutor(transport=OpenRegistry()),
        preflight=lambda _c: "")
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="releases",
                             source={"site": "registry.test", "origin": "attach"})
    other = "serde_derive" if record["chain_fit"]["value"] == "serde_json" else "serde_json"
    record["chain_fit"]["value"] = other
    record["health"] = {"preferred": False, "demoted_reason": "3 failures in a row"}
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "releases", record)
    site = OpenRegistry()
    manager = RecompileManager(
        storage, lambda wid: {"sessions": copy.deepcopy(sessions),
                              "from": "the stored attach sessions"},
        executor=ApiStepExecutor(transport=site), proof_preflight=lambda _c: "")
    outcome = manager.recompile_now("releases", "the compiled request failed")
    assert outcome.persisted, outcome.reason
    assert f"q={other}" in generated_sent(site)[0], site.sent
    assert load_compiled(storage, "releases")["chain_fit"]["value"] == other


def test_a_signed_in_session_chain_never_generates_a_fit(monkeypatch):
    from anyuse.execution import api_verify as AV
    from anyuse.execution import attach as A
    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop")
    monkeypatch.setattr(AV, "compile_and_verify_with_fit", spy)
    monkeypatch.setattr(A, "_response_ground_truth", lambda fit, site: "data[*].name")
    monkeypatch.setattr(A, "_signed_in_list_values", lambda r, site: {})
    sessions = [dict(TCS.session(q), login_replay_site="registry.test")
                for q in ("serde", "tokio", "rand")]
    with pytest.raises(RuntimeError):
        A._compile_login_replay_sessions(sessions, "q", ["serde", "tokio", "rand"], "typed",
                                         "registry.test", preflight=lambda _c: "")
    assert seen["generate"] is False


# ---------------------------------------------------------------------------
# the automatic recompile from the browser replay's traffic
# ---------------------------------------------------------------------------

FIND = f"{TCS.ORIGIN}/find"


def find_traffic(q, endpoint="/api/package"):
    """What a browser fetched for ``/find?q=``: a search page that shows the found
    result's releases, the search it made, and those releases under ``endpoint``."""
    key = TCS.ident(TCS.RESULTS[q][0] if q in TCS.RESULTS else q)
    name = TCS.RESULTS[q][0] if q in TCS.RESULTS else q
    rows = (TCS.search_doc(q) if q in TCS.RESULTS else
            {"results": [{"id": TCS.ident(n), "name": n} for n in TCS.NAMES if n.startswith(q)]})
    return [(f"{FIND}?q={q}", "text/html", TCS.page(name)),
            (f"{TCS.ORIGIN}/api/search?q={q}", "application/json", json.dumps(rows)),
            (f"{TCS.ORIGIN}{endpoint}/{key}/releases", "application/json",
             json.dumps({"releases": [{"title": t} for t in TCS.releases(name)]}))]


def find_session(q):
    fetched = find_traffic(q)
    name = TCS.RESULTS[q][0]
    return {"kind": TCS.SESSION_KIND, "version": 1, "id": f"sess-{q}",
            "task": "package releases", "input": {"name": "q", "value": q},
            "started_at": "2026-10-01T10:00:00+0000",
            "snapshots": [{"url": f"{TCS.ORIGIN}/", "title": "", "dom": TCS.HOME,
                           "text": "Registry", "password_field": False},
                          {"url": fetched[0][0], "title": "", "dom": fetched[0][2],
                           "text": "\n".join([name, *TCS.releases(name)]),
                           "password_field": False}],
            "network": TCS.network(fetched), "navigations": [f"{TCS.ORIGIN}/", fetched[0][0]]}


class MovedRegistry(OpenRegistry):
    """The registry after its releases endpoint moved to ``/api/v2/package``."""

    def send(self, method, url, headers, body, timeout_s):
        parts = urllib.parse.urlsplit(url)
        if parts.path.startswith("/api/package/"):
            self.sent.append((method, url))
            return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
        if parts.path.startswith("/api/v2/package/"):
            url = url.replace("/api/v2/package/", "/api/package/")
        return super().send(method, url, headers, body, timeout_s)


class FindBrowser:
    """A browser driver serving each value's ``/find`` page; with ``capture_traffic`` it
    also returns the requests the page made (its releases under ``endpoint``)."""

    def __init__(self, endpoint="/api/v2/package"):
        self.endpoint, self.opened = endpoint, []

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        from anyuse.execution.cdp_replay import PageResult
        self.opened.append((url, capture_traffic))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        fetched = find_traffic(q, endpoint=self.endpoint)
        network = TCS.network(fetched) if capture_traffic else []
        name = TCS.RESULTS[q][0] if q in TCS.RESULTS else q
        return PageResult(url=url, dom=fetched[0][2],
                          text="\n".join([name, *TCS.releases(name)]), status=200,
                          elapsed_ms=200.0, network=network)


def _find_tool(tmp_path, monkeypatch, fitted_on=None):
    from anyuse.execution import api_permission as perm
    from anyuse.execution.api_compile import persist_compiled, with_runtime_rungs
    from anyuse.execution.api_permission import acknowledge_host
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(perm, "_default_fetch", lambda url, *a, **kw: "User-agent: *\nAllow: /\n")
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    sessions = [find_session(q) for q in ("serde", "tokio", "rand", "regex")]
    compiled, proof, _report, recs = compile_sessions(
        sessions, executor=ApiStepExecutor(transport=OpenRegistry()), preflight=lambda _c: "")
    assert compiled.status == "chained" and proof.effective_status == "proved", proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="find",
                             source={"site": "registry.test", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    assert record["browser"]["template"]["url"] == f"{FIND}?q={{q}}"
    if fitted_on is not None:
        record["browser"]["fitted_on"] = fitted_on
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "find", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("registry.test", path=hosts)
    return storage, hosts


def _run(storage, hosts, site, browser, q):
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    return run_compiled("find", {"q": q}, storage=storage, hosts_path=hosts,
                        executor=ApiStepExecutor(transport=site),
                        browser_executor=ReplayExecutor(driver=browser),
                        agent_runner=lambda task, **kw: {"ok": False, "outcome": "failed"})


def test_a_chain_is_rebuilt_from_the_browser_replays_traffic_with_a_second_captured_page(
        tmp_path, monkeypatch):
    from anyuse.execution.api_compile import load_compiled
    storage, hosts = _find_tool(tmp_path, monkeypatch)
    site, browser = MovedRegistry(), FindBrowser()
    out = _run(storage, hosts, site, browser, "clap")
    assert out["ok"] and out["served_by"] == "browser_replay", out
    rec = out["recompiled"]
    assert rec["swapped"] is True and rec["from"] == "the browser replay's traffic", rec
    stored = load_compiled(storage, "find")
    assert "/api/v2/package/" in json.dumps(stored["template"])
    assert stored["proof"]["status"] == "proved"
    assert sorted(c["value"] for c in stored["proof"]["cases"]) == ["rand", "regex"]
    # the chain was fitted on an input the browser rung was fitted on, captured with its traffic
    assert (f"{FIND}?q=serde", True) in browser.opened
    assert stored["proof"]["split"]["fitting"] == 1
    assert "fitting_generated" not in stored["proof"]["split"] and "chain_fit" not in stored


def test_a_chain_with_no_second_fitted_input_replays_a_generated_value_to_fit_it(
        tmp_path, monkeypatch):
    from anyuse.execution.api_compile import load_compiled
    # the browser rung was fitted on the call's own input only
    storage, hosts = _find_tool(tmp_path, monkeypatch, fitted_on=["clap"])
    site, browser = MovedRegistry(), FindBrowser()
    out = _run(storage, hosts, site, browser, "clap")
    assert out["recompiled"]["swapped"] is True, out["recompiled"]
    stored = load_compiled(storage, "find")
    assert stored["proof"]["status"] == "proved"
    fit = stored["chain_fit"]
    assert fit["via"] == "browser" and fit["value"] in ("clap_builder", "clap_derive")
    assert (f"{FIND}?q={fit['value']}", True) in browser.opened
    assert stored["proof"]["split"].get("fitting_generated") == 1


class DeafBrowser(FindBrowser):
    """A driver that, asked to capture, returns no traffic for the held-out pages."""

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        page = super().open(url, settle_s, timeout_s, capture_traffic)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        if q in ("rand", "regex"):
            page.network = []
        return page


def test_a_chain_whose_held_out_pages_carry_no_traffic_is_not_rebuilt_from_them(
        tmp_path, monkeypatch):
    storage, hosts = _find_tool(tmp_path, monkeypatch)
    site, browser = MovedRegistry(), DeafBrowser()
    out = _run(storage, hosts, site, browser, "clap")
    rec = out["recompiled"]
    assert rec["swapped"] is False, rec
    # the live evidence is set aside for the stored recordings, which this tool did not keep
    assert "captured no traffic to trace the chain's result id in" in rec["reason"], rec
    assert "were not kept" in rec["reason"], rec


def test_a_compile_with_a_browser_replay_captures_the_generated_values_page():
    from anyuse.execution.api_verify import compile_and_verify_with_fit
    from anyuse.execution.attach import session_to_recording
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.execution.learn_loop import derive_ground_truth
    recs = [session_to_recording(find_session(q), "q", q, f"attach{i}")
            for i, q in enumerate(("serde", "tokio", "rand"))]
    assert derive_ground_truth(recs)
    browser = FindBrowser(endpoint="/api/package")
    site = OpenRegistry()
    compiled, proof, fit = compile_and_verify_with_fit(
        recs[0], recs[1:], [], executor=ApiStepExecutor(transport=site),
        preflight=lambda _c: "",
        browse=CF.browser_route(recs[0], executor=ReplayExecutor(driver=browser)))
    assert compiled.status == "chained" and proof.passed, (compiled.reason, proof.reason)
    assert proof.chain_fit["via"] == "browser"
    assert (f"{FIND}?q={proof.chain_fit['value']}", True) in browser.opened
    # the browser captured it, so no request was sent for it over plain HTTP
    assert generated_sent(site) == []
    # without a configured browser there is no route
    assert CF.browser_route(recs[0]) is None


def test_without_the_browser_a_stored_chain_is_rebuilt_over_plain_http(tmp_path):
    # the stored recordings carry no fitting input: the recompile generates one
    from anyuse.execution.api_verify import compile_and_verify_with_fit
    recs = [har_to_recording(load_har(c.path), "q", c.param_value, c.expected)
            for c in hars(str(tmp_path), ["serde", "tokio", "rand"])]
    compiled, proof, _fit = compile_and_verify_with_fit(
        copy.deepcopy(recs[0]), copy.deepcopy(recs[1:]), [],
        executor=ApiStepExecutor(transport=OpenRegistry()), preflight=lambda _c: "",
        prefer_fit=["serde_derive"])
    assert proof.passed and proof.chain_fit["value"] == "serde_derive", proof.reason


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
