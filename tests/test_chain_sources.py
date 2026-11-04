"""A search-then-open chain compiled from the sources that declare no fitting
recording: a list of HARs, attach sessions and the learn loop's pages.

Each source fits the chain on its second recorded input (the second HAR, the
selection session, the selection page). That input is then no proof case: the
chain is proved on the later inputs, with each intermediate binding traced
from their own traffic. With one input too few, the fitting input is generated
from the site's own search (tests/test_chain_fit.py). This registry answers no
search for another result's name, so the compile refuses, names the values it
tried and says what to record. A compile that is not a chain keeps every proof
case.
Entirely offline: the transport is injected, the HARs are temporary files.
"""
import json
import os
import shutil
import tempfile
import urllib.parse

from anyuse.execution.agent_browser import AgentRun
from anyuse.execution.api_compile import compile_recording
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.attach import SESSION_KIND, compile_sessions
from anyuse.execution.har_import import (HarCase, compile_from_har, har_to_recording,
                                         load_har, typed_recording)
from anyuse.execution.learn_loop import (Candidate, _learned_source, continue_learning,
                                         page_title)

ORIGIN = "https://registry.test"

#: popularity-ranked search results per query; each query's first row is named it
RESULTS = {
    "serde": ["serde", "serde_json", "serde_derive"],
    "tokio": ["tokio", "tokio-util", "tokio-stream"],
    "rand": ["rand", "rand_core", "rand_chacha"],
    "regex": ["regex", "regex-syntax", "regex-lite"],
    "clap": ["clap", "clap_builder", "clap_derive"],
}
NAMES = sorted({name for rows in RESULTS.values() for name in rows})


def ident(name):
    return f"p{100 + NAMES.index(name)}"


def search_doc(q):
    return {"results": [{"id": ident(n), "name": n} for n in RESULTS[q]]}


def releases(name):
    return [f"{name} {len(name)}.{k}.0" for k in (3, 2, 1)]


def page(name):
    rows = "".join(f'<li class="release">{t}</li>' for t in releases(name))
    return (f"<html><head><title>{name}</title></head><body><nav>Registry</nav>"
            f'<main><h1>{name}</h1><ul class="releases">{rows}</ul></main>'
            f"<footer>About</footer></body></html>")


HOME = ("<html><head><title>Registry</title></head><body><nav>Registry</nav>"
        "<form><input name=\"q\"></form><footer>About</footer></body></html>")


def traffic(q):
    """What a browser fetched to search ``q`` and open its first result."""
    key = ident(RESULTS[q][0])
    return [(f"{ORIGIN}/", "text/html", HOME),
            (f"{ORIGIN}/api/search?q={q}", "application/json", json.dumps(search_doc(q))),
            (f"{ORIGIN}/api/package/{key}/releases", "application/json",
             json.dumps({"releases": [{"title": t} for t in releases(RESULTS[q][0])]})),
            (f"{ORIGIN}/package/{key}", "text/html", page(RESULTS[q][0]))]


def single_traffic(q):
    """A search whose own response is the result list the page shows."""
    return [(f"{ORIGIN}/", "text/html", HOME),
            (f"{ORIGIN}/api/search?q={q}", "application/json", json.dumps(search_doc(q)))]


class Registry:
    """The search and releases endpoints of the fixture registry."""

    def __init__(self):
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        parts = urllib.parse.urlsplit(url)
        ok = lambda doc: ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(doc))
        if parts.path == "/api/search":
            return ok(search_doc(urllib.parse.parse_qs(parts.query)["q"][0]))
        segments = parts.path.split("/")
        name = next(n for n in NAMES if ident(n) == segments[3])
        return ok({"releases": [{"title": t} for t in releases(name)]})


def executor():
    return ApiStepExecutor(transport=Registry())


def write_har(directory, q, fetched):
    entries = [{"startedDateTime": "2026-10-01T10:00:00.000Z", "time": 1,
                "request": {"method": "GET", "url": url, "headers": [], "cookies": [],
                            "queryString": []},
                "response": {"status": 200, "statusText": "OK",
                             "headers": [{"name": "Content-Type", "value": mime}],
                             "content": {"size": len(text), "mimeType": mime, "text": text},
                             "redirectURL": ""},
                "cache": {}, "timings": {}} for url, mime, text in fetched]
    path = os.path.join(directory, f"{q}.har")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"log": {"version": "1.2", "creator": {"name": "DevTools", "version": "1"},
                           "entries": entries}}, fh)
    return path


def network(fetched):
    return [{"url": url, "method": "GET", "status": 200, "mime": mime, "body": text,
             "bytes": len(text), "resource_type": "Document" if mime == "text/html" else "XHR",
             "headers": {"content-type": mime}} for url, mime, text in fetched]


def session(q, fetch=traffic):
    """An attach session of searching ``q`` and opening its first result; its
    network is what ``fetch`` says the browser fetched."""
    fetched = traffic(q)
    name = RESULTS[q][0]
    return {"kind": SESSION_KIND, "version": 1, "id": f"sess-{q}",
            "task": "package releases", "input": {"name": "q", "value": q},
            "started_at": "2026-10-01T10:00:00+0000",
            "snapshots": [{"url": f"{ORIGIN}/", "title": "", "dom": HOME, "text": "Registry",
                           "password_field": False},
                          {"url": fetched[-1][0], "title": "", "dom": fetched[-1][2],
                           "text": "\n".join([name, *releases(name)]),
                           "password_field": False}],
            "network": network(fetch(q)), "navigations": [f"{ORIGIN}/", fetched[-1][0]]}


def agent_run(q, fetch=traffic):
    """The learn loop's agent run of ``q``: the same result page, and ``fetch``'s traffic."""
    fetched = traffic(q)
    name = RESULTS[q][0]
    return AgentRun(value=q, task="package releases", start_url=f"{ORIGIN}/",
                    final_url=fetched[-1][0], dom=fetched[-1][2],
                    text="\n".join([name, *releases(name)]), network=network(fetch(q)),
                    steps=[{"action": "type", "index": 0, "text": q},
                           {"action": "submit", "index": 0}, {"action": "done"}],
                    typed=[q])


class FakeAgent:
    def __init__(self, values, search_only=()):
        self.runs = {v: agent_run(v, single_traffic if v in search_only else traffic)
                     for v in values}

    def run(self, task, start_url, value):
        return self.runs[value]


class HarDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="chain-sources-")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def har_cases(directory, values, fetch=traffic):
    return [HarCase(path=write_har(directory, q, fetch(q)), param_value=q,
                    expected=(releases(RESULTS[q][0]) if fetch is traffic else RESULTS[q]))
            for q in values]


# ---------------------------------------------------------------------------
# HARs
# ---------------------------------------------------------------------------

def test_four_hars_compile_the_chain_fitted_on_the_second():
    with HarDir() as tmp:
        cases = har_cases(tmp, ["serde", "tokio", "rand", "regex"])
        compiled, proof, report = compile_from_har(cases, executor=executor(),
                                                   preflight=lambda _c: "")
    assert compiled.status == "chained", compiled.reason
    assert proof.passed, proof.reason
    assert proof.effective_status == "proved"
    assert (proof.split["fitting"], proof.split["validation"]) == (1, 2)
    assert report["fitting"] == [cases[1].path]
    # the fitting HAR is no proof case; each proof case's binding was traced
    assert sorted(c.value for c in proof.cases) == ["rand", "regex"]
    assert all(c.contract.get("bindings_derived") for c in proof.cases)


def test_three_hars_refuse_a_chain_when_no_generated_value_fits_and_say_to_give_a_fourth():
    # this registry answers no search for another result's name
    with HarDir() as tmp:
        compiled, proof, report = compile_from_har(
            har_cases(tmp, ["serde", "tokio", "rand"]), executor=executor(),
            preflight=lambda _c: "")
    assert compiled.template is None and not proof.passed
    assert compiled.chain_needs_fitting
    assert "needs 4 HARs" in proof.reason and "the second fits the chain" in proof.reason
    # the values it tried, taken from the first HAR's search results
    assert "did not fit it ('serde_json': the search did not answer" in proof.reason
    assert "'serde_derive'" in compiled.chain_fit_note
    assert report["fitting"] == [] and "generated_fitting" not in report


def test_a_second_har_that_does_not_open_a_result_says_so():
    with HarDir() as tmp:
        cases = (har_cases(tmp, ["serde"]) + har_cases(tmp, ["tokio"], fetch=single_traffic)
                 + har_cases(tmp, ["rand", "regex"]))
        compiled, proof, report = compile_from_har(cases, executor=executor(),
                                                   preflight=lambda _c: "")
    assert compiled.template is None and not proof.passed
    assert report["fitting"] == [cases[1].path]
    assert ("tokio.har does not open a result the way the first HAR does, so it cannot fit "
            "the chain: export it again after opening the result") in proof.reason


def test_typed_cases_never_fit_a_chain():
    from anyuse.execution.api_verify import chain_fitting
    site = Registry()
    with HarDir() as tmp:
        cases = har_cases(tmp, ["serde"]) + [
            HarCase(path="", param_value=q, expected=releases(q)) for q in ("tokio", "rand")]
        compiled, proof, report = compile_from_har(cases,
                                                   executor=ApiStepExecutor(transport=site),
                                                   preflight=lambda _c: "")
        base, fit = [har_to_recording(load_har(case.path), "q", case.param_value,
                                      case.expected)
                     for case in [cases[0], *har_cases(tmp, ["clap"])]]
    assert not proof.passed and report["fitting"] == []
    assert ("A typed --expect case cannot prove a chain: export another HAR and compile "
            "again") in proof.reason
    # typed cases carry no traffic, so no value is generated and nothing is sent
    assert site.sent == [] and "carry no traffic" in compiled.chain_fit_note
    typed = [typed_recording("q", q, releases(q)) for q in ("tokio", "rand")]
    refused = compile_recording(base)
    assert refused.chain_needs_fitting
    assert chain_fitting(refused, typed) is None
    assert chain_fitting(refused, [*typed, fit]) is fit


def test_a_single_request_har_compile_keeps_every_proof_case():
    with HarDir() as tmp:
        compiled, proof, report = compile_from_har(
            har_cases(tmp, ["serde", "tokio", "rand", "regex"], fetch=single_traffic),
            executor=executor(), preflight=lambda _c: "")
    assert compiled.status != "chained" and not compiled.chain_needs_fitting
    assert proof.passed, proof.reason
    assert (proof.split["fitting"], proof.split["validation"]) == (0, 3)
    assert report.get("fitting", []) == []


# ---------------------------------------------------------------------------
# attach sessions
# ---------------------------------------------------------------------------

def test_four_sessions_compile_the_chain_fitted_on_the_selection_session():
    compiled, proof, report, _recs = compile_sessions(
        [session(q) for q in ("serde", "tokio", "rand", "regex")], executor=executor(),
        preflight=lambda _c: "")
    assert compiled.status == "chained", compiled.reason
    assert proof.passed, proof.reason
    assert proof.effective_status == "proved"
    split = proof.split
    assert (split["fitting"], split["selection"], split["validation"]) == (1, 0, 2)
    assert report["chain_fitted_on"] == "sess-tokio"
    # the session order is kept, so a recompile from the stored sessions fits it again
    assert report["n_selection"] == 1
    assert sorted(c.value for c in proof.cases) == ["rand", "regex"]
    assert all(c.contract.get("bindings_derived") for c in proof.cases)


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")


def test_a_chain_from_sessions_is_recompiled_from_its_stored_sessions():
    from anyuse.execution.api_compile import compiled_record, load_compiled, persist_compiled
    from anyuse.execution.api_recompile import RecompileManager
    from anyuse.execution.auto_recompile import stored_evidence
    sessions = [session(q) for q in ("serde", "tokio", "rand", "regex")]
    with HarDir() as tmp:
        paths = []
        for s in sessions:
            paths.append(os.path.join(tmp, f"{s['id']}.session.json"))
            with open(paths[-1], "w", encoding="utf-8") as fh:
                json.dump(s, fh)
        compiled, proof, report, _recs = compile_sessions(sessions, executor=executor(),
                                                          preflight=lambda _c: "")
        assert proof.effective_status == "proved", proof.reason
        # the roles `compile last` stores (cli._finish_compile): the session order is kept
        n_sel = report["n_selection"]
        record = compiled_record(compiled.template, proof.to_dict(), workflow="releases",
                                 source={"origin": "attach", "paths": {
                                     "recording": paths[0], "selection": paths[1:1 + n_sel],
                                     "fresh": paths[1 + n_sel:]}})
        record["health"] = {"preferred": False, "demoted_reason": "3 failures in a row"}
        storage = Storage(tmp)
        persist_compiled(storage, "releases", record)
        manager = RecompileManager(storage, lambda wid: stored_evidence(
            load_compiled(storage, wid)), executor=executor(), proof_preflight=lambda _c: "")
        outcome = manager.recompile_now("releases", "the compiled request failed")
        stored = load_compiled(storage, "releases")
    assert outcome.persisted, outcome.reason
    assert stored["proof"]["status"] == "proved" and stored["health"]["preferred"] is True
    split = stored["proof"]["split"]
    assert (split["fitting"], split["selection"], split["validation"]) == (1, 0, 2)


def test_a_selection_session_that_does_not_open_a_result_says_so():
    sessions = [session("serde"), session("tokio", fetch=single_traffic),
                session("rand"), session("regex")]
    compiled, proof, report, _recs = compile_sessions(sessions, executor=executor(),
                                                      preflight=lambda _c: "")
    assert compiled.template is None and not proof.passed
    assert report["chain_fitted_on"] == "sess-tokio"
    assert ("Session sess-tokio does not open a result the way the first session does, so "
            "it cannot fit the chain: record it again") in proof.reason


def test_three_sessions_refuse_a_chain_and_say_to_record_another():
    compiled, proof, report, _recs = compile_sessions(
        [session(q) for q in ("serde", "tokio", "rand")], executor=executor(),
        preflight=lambda _c: "")
    assert compiled.template is None and not proof.passed
    assert "needs 4 sessions" in proof.reason
    assert "Record another session and compile again" in proof.reason
    assert "did not fit it ('serde_json'" in proof.reason
    assert "chain_fitted_on" not in report and "chain_fit" not in report


# ---------------------------------------------------------------------------
# the learn loop
# ---------------------------------------------------------------------------

def learn(values, search_only=()):
    first = agent_run(values[0])
    cand = Candidate(workflow="releases", task=first.task, start_url=first.start_url,
                     param="q", value=first.value, final_url=first.final_url,
                     title=page_title(first.dom),
                     recording=first.as_recording("releases", "q", "agent0"))
    kept = {}

    def persist(compiled, proof, recordings, _res):
        kept["compiled"], kept["recordings"] = compiled, recordings
        return "releases"
    res = continue_learning(cand, list(values[1:]), agent=FakeAgent(values[1:], search_only),
                            executor=executor(), persist=persist, confirmed_by="test",
                            proof_preflight=lambda _c: "")
    return res, kept


def test_the_learn_loop_compiles_the_chain_fitted_on_the_selection_page():
    res, kept = learn(["serde", "tokio", "rand", "regex"])
    assert res.outcome == "tier1", res.reason
    assert kept["compiled"].status == "chained"
    assert res.proof_status == "proved"
    assert (res.split["fitting"], res.split["selection"], res.split["validation"]) == (1, 0, 2)
    # the stored source lists the fitting page apart from the pages the chain was proved on
    source = _learned_source(kept["recordings"], res)
    assert (source["fitting"], source["selection"], source["fresh"]) == (
        ["releases.agent1.json"], [], ["releases.agent2.json", "releases.agent3.json"])


def test_the_learned_source_of_a_flow_that_is_not_a_chain_is_unchanged():
    from anyuse.execution.learn_loop import LearnResult
    recordings = [{"tag": f"agent{i}"} for i in range(4)]
    held = LearnResult(workflow="w", split={"fitting": 0, "selection": 1, "validation": 2})
    source = _learned_source(recordings, held)
    assert (source["fitting"], source["selection"], source["fresh"]) == (
        [], ["w.agent1.json"], ["w.agent2.json", "w.agent3.json"])
    # nothing held out: the later pages are counted as fitting and validation both
    degraded = LearnResult(workflow="w", split={"fitting": 2, "selection": 0, "validation": 2})
    source = _learned_source(recordings[:3], degraded)
    assert (source["fitting"], source["selection"], source["fresh"]) == (
        [], [], ["w.agent1.json", "w.agent2.json"])


def test_a_selection_page_that_does_not_open_a_result_says_so():
    res, kept = learn(["serde", "tokio", "rand", "regex"], search_only=("tokio",))
    assert res.outcome == "nothing" and "compiled" not in kept
    assert ("The page for 'tokio' does not open a result the way the first page does, so it "
            "cannot fit the chain: learn again") in res.reason


def test_the_learn_loop_with_three_values_says_to_give_another():
    res, kept = learn(["serde", "tokio", "rand"])
    assert res.outcome == "nothing" and "compiled" not in kept
    assert "needs 4 values" in res.reason and "Give another value" in res.reason
    assert "did not fit it ('serde_json'" in res.reason


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
