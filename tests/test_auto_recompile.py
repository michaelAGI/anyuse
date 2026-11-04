"""Automatic recompilation in the runtime cascade (``execution/auto_recompile.py`` through
``mcpserver/compiled_tools.run_compiled``).

Offline: the record is compiled from four real crates.io attach sessions
(``testdata/attach_crates``); the site's API, its browser pages and its robots.txt are fakes.
Nothing here opens a socket, starts a browser, calls a model or touches the desktop.
"""
import json
import os
import sys
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from anyuse.execution import api_permission as perm
from anyuse.execution import attach as A
from anyuse.execution.api_compile import (compiled_record, load_compiled, persist_compiled,
                                          with_runtime_rungs)
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_recompile import (PREVIOUS_FILE, ApiHealth, RecompilePolicy,
                                            recompile_allowed)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor, traffic_from_events
from anyuse.mcpserver import compiled_tools as CT
from anyuse.mcpserver.compiled_tools import run_compiled

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "testdata", "attach_crates")
CHANGED = '{"results": []}'                      # what the old endpoint answers now


def _moved(body: str) -> str:
    """The same crates under the shape the site's new endpoint serves."""
    data = json.loads(body)
    return json.dumps({"data": {"packages": [
        {"title": c["name"], "summary": c.get("description"), "latest": c.get("max_version")}
        for c in data["crates"]]}})


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


class Site:
    """The site's HTTP side: the old endpoint answers ``CHANGED``, the new one (``new_host`` +
    ``/v2/crates``) the moved shape; ``broken_new`` makes the new one answer nothing."""

    def __init__(self, captured, moved=True, new_host="crates.io", broken_new=False, flaky=0):
        self.captured, self.moved, self.new_host = captured, moved, new_host
        self.broken_new, self.flaky, self.sent = broken_new, flaky, []

    def new_url(self, q):
        prefix = "/api/v2/crates" if self.new_host == "crates.io" else "/v2/crates"
        return f"https://{self.new_host}{prefix}?q={urllib.parse.quote(q)}&per_page=10"

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(sp.query).get("q", [""])[0]
        self.sent.append((sp.netloc + sp.path, q))
        if sp.path.endswith("/v2/crates"):
            text = '{"data": {"packages": []}}' if self.broken_new else _moved(self.captured[q])
            return ApiResponse(200, {"Content-Type": "application/json"}, text)
        if self.moved or self.flaky > 0:
            self.flaky -= 1
            return ApiResponse(200, {"Content-Type": "application/json"}, CHANGED)
        return ApiResponse(200, {"Content-Type": "application/json"}, self.captured[q])


class Browser:
    """A browser driver serving each value's recorded result page; with ``capture_traffic`` it
    also returns the XHR the page made to the site's new endpoint."""

    def __init__(self, doms, site, capture=True):
        self.doms, self.site, self.capture, self.opened = doms, site, capture, []

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        self.opened.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        dom = self.doms.get(q, "")
        network = []
        if capture_traffic and self.capture and dom:
            network = [{"url": url, "method": "GET", "status": 200, "mime": "text/html",
                        "body": "<html><body><div id=app></div></body></html>",
                        "resource_type": "Document", "headers": {}, "bytes": 48},
                       {"url": self.site.new_url(q), "method": "GET", "status": 200,
                        "mime": "application/json", "body": _moved(self.site.captured[q]),
                        "resource_type": "Fetch", "headers": {}, "bytes": 2000}]
        return PageResult(url=url, dom=dom, text="results" if dom else "", status=200,
                          elapsed_ms=640.0, network=network)


def _agent(calls):
    def run(task, **kw):
        calls.append(task)
        return {"ok": True, "outcome": "done", "result": ["tokio", "tokio-util", "tokio-stream"],
                "model_calls": 2, "cost_usd": 0.02, "steps": 3}
    return run


ROBOTS = {"crates.io": "User-agent: *\nAllow: /\n", "api.crates.io": "User-agent: *\nAllow: /\n"}


@pytest.fixture
def crates(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_ROBOTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))      # its own skills registry
    robots = dict(ROBOTS)
    fetched = []

    def fetch(robots_url, *args, **kwargs):
        host = urllib.parse.urlsplit(robots_url).netloc
        fetched.append(host)
        return robots.get(host)
    monkeypatch.setattr(perm, "_default_fetch", fetch)
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    sessions = [json.load(open(os.path.join(DATA, n))) for n in sorted(os.listdir(DATA))]
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]
    compiled, proof, report, recs = A.compile_sessions(
        sessions, executor=ApiStepExecutor(transport=Site(captured, moved=False)),
        preflight=lambda _c: "")
    assert proof.passed and proof.effective_status == "proved", proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "crates_search", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("crates.io", path=hosts)
    doms = {r["param"]["value"]: r["dom"] for r in recs}
    labels = {r["param"]["value"]: (r.get("ui") or {}).get("labels") or [] for r in recs}
    yield SimpleNamespace(storage=storage, hosts=hosts, record=record, captured=captured,
                          doms=doms, labels=labels, sessions=sessions, robots=robots,
                          fetched=fetched, tmp=tmp_path)
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


def _stored(crates):
    return load_compiled(crates.storage, "crates_search")


def _call(crates, site, browser=None, calls=None, q="tokio"):
    return run_compiled("crates_search", {"q": q}, storage=crates.storage, hosts_path=crates.hosts,
                        executor=ApiStepExecutor(transport=site),
                        browser_executor=ReplayExecutor(driver=browser) if browser else None,
                        agent_runner=_agent(calls if calls is not None else []))


def test_the_record_knows_its_held_out_inputs_and_what_its_selector_was_fitted_on(crates):
    from anyuse.execution.auto_recompile import proof_values
    assert crates.record["browser"]["fitted_on"] == ["serde", "regex"]
    assert proof_values(crates.record) == ["tokio", "clap"]


def test_a_changed_api_is_recompiled_from_the_browser_replays_traffic(crates):
    site, calls = Site(crates.captured), []
    browser = Browser(crates.doms, site)
    out = _call(crates, site, browser, calls)
    assert out["ok"] and out["served_by"] == "browser_replay", out
    assert out["result"]["items"][:3] == crates.labels["tokio"][:3]
    rec = out["recompiled"]
    assert rec["swapped"] is True and rec["from"] == "the browser replay's traffic", rec
    assert rec["attempt"] == 1
    assert "next call is served by the compiled request again" in out["next"]
    stored = _stored(crates)
    assert "/api/v2/crates" in stored["template"]["url"]
    assert stored["proof"]["passed"] and stored["proof"]["status"] == "proved"
    assert stored["health"]["preferred"] is True and stored["health"]["consecutive_failures"] == 0
    assert stored["recompiled"]["from"] == "the browser replay's traffic"
    # compiled from an input the selector was fitted on, re-proved on both held-out inputs
    assert sorted(c["value"] for c in stored["proof"]["cases"]) == ["clap", "tokio"]
    previous = json.load(open(os.path.join(crates.storage.get_workflow_dir("crates_search"),
                                           PREVIOUS_FILE)))
    assert "/api/v1/crates" in previous["template"]["url"]
    # the first compile's time, set apart from the recompile's (both fall in one second here)
    path = os.path.join(crates.storage.get_workflow_dir("crates_search"), "compiled_api.json")
    json.dump(dict(stored, compiled_at="2000-01-01T00:00:00+00:00"), open(path, "w"))
    # the next call is served by the new compiled request: no browser, no agent
    browser.opened.clear()
    site.sent.clear()
    out = _call(crates, site, browser, calls, q="clap")
    assert out["ok"] and out["served_by"] == "api", out
    assert browser.opened == [] and calls == [] and out["model_calls"] == 0
    assert site.sent == [("crates.io/api/v2/crates", "clap")]
    # the provenance dates the request in use, not the one it replaced
    assert out["provenance"]["compiled_at"] == stored["recompiled_at"]
    row = CT.list_compiled(crates.storage, crates.hosts)[0]
    assert row["recompile"]["recompiled"]["from"] == "the browser replay's traffic"
    assert row["available_via"] == "api" and row["proof"]["current"]


def test_a_recompile_that_does_not_prove_leaves_the_tool_demoted_and_waits(crates):
    site = Site(crates.captured, broken_new=True)
    browser = Browser(crates.doms, site)
    out = _call(crates, site, browser)
    assert out["ok"] and out["served_by"] == "browser_replay"
    rec = out["recompiled"]
    assert rec["attempted"] and rec["swapped"] is False and rec["next_attempt_after"], rec
    assert "automatic recompile" in out["next"] and "record the search again" in out["next"]
    stored = _stored(crates)
    assert stored["health"]["preferred"] is False and stored["health"]["recompile_attempts"] == 1
    assert "/api/v1/crates" in stored["template"]["url"]            # nothing was swapped in
    assert "automatic recompile did not restore it" in stored["health"]["demoted_reason"]
    assert stored["recompile"]["swapped"] is False
    # inside the policy window: the call is served, nothing is recompiled, no extra page or request
    browser.opened.clear()
    site.sent.clear()
    out = _call(crates, site, browser)
    assert out["ok"] and out["served_by"] == "browser_replay"
    assert out["recompiled"]["attempted"] is False and "allowed after" in out["recompiled"]["reason"]
    assert len(browser.opened) == 1 and site.sent == []
    assert _stored(crates)["health"]["recompile_attempts"] == 1
    row = CT.list_compiled(crates.storage, crates.hosts)[0]
    assert row["recompile"]["last_attempt"]["swapped"] is False
    assert row["recompile"]["next_attempt_after"]


def test_an_unacknowledged_new_host_blocks_the_swap(crates):
    site = Site(crates.captured, new_host="api.crates.io")
    out = _call(crates, site, Browser(crates.doms, site))
    assert out["ok"] and out["served_by"] == "browser_replay"
    assert out["recompiled"]["swapped"] is False
    assert "is not acknowledged" in out["recompiled"]["reason"], out["recompiled"]
    stored = _stored(crates)
    assert stored["health"]["preferred"] is False and "/api/v1/" in stored["template"]["url"]
    # the proof sent nothing to the host nobody acknowledged
    assert not [s for s in site.sent if s[0].startswith("api.crates.io")]


def test_a_robots_refusal_blocks_the_swap(crates):
    crates.robots["crates.io"] = "User-agent: *\nDisallow: /api/v2\n"
    site = Site(crates.captured)
    out = _call(crates, site, Browser(crates.doms, site))
    assert out["ok"] and out["served_by"] == "browser_replay"
    assert out["recompiled"]["swapped"] is False and "robots" in out["recompiled"]["reason"]
    assert _stored(crates)["health"]["preferred"] is False
    assert not [s for s in site.sent if "/api/v2/" in s[0]]


def test_without_a_browser_it_recompiles_from_the_stored_sessions(crates):
    # a one-off changed answer took the request out of service; the stored sessions re-prove it
    paths = []
    for i, session in enumerate(crates.sessions):
        path = crates.tmp / f"s{i}.json"
        path.write_text(json.dumps(session))
        paths.append(str(path))
    record = _stored(crates)
    record["source"]["paths"] = {"recording": paths[0], "selection": [paths[1]],
                                 "fresh": paths[2:]}
    persist_compiled(crates.storage, "crates_search", record)
    site, calls = Site(crates.captured, moved=False, flaky=1), []
    out = _call(crates, site, None, calls)
    assert out["ok"] and out["served_by"] == "agent" and calls
    rec = out["recompiled"]
    assert rec["swapped"] is True and rec["from"] == "the stored attach sessions", rec
    assert _stored(crates)["health"]["preferred"] is True
    out = _call(crates, site, None, calls)
    assert out["served_by"] == "api" and len(calls) == 1


def test_no_evidence_is_said_and_uses_no_attempt(crates):
    # nothing to compile from and nothing loaded or sent: the attempt is given back
    site, calls = Site(crates.captured), []
    out = _call(crates, site, None, calls)
    assert out["served_by"] == "agent"
    rec = out["recompiled"]
    assert rec["swapped"] is False and "were not kept" in rec["reason"]
    stored = _stored(crates)
    assert stored["health"]["recompile_attempts"] == 0 and stored["health"]["preferred"] is False
    assert stored["recompile"]["counted"] is False
    # so the next call looks again instead of waiting an hour
    out = _call(crates, site, None, calls)
    assert out["recompiled"]["attempted"] is False and "were not kept" in out["recompiled"]["reason"]


def test_the_policy_bounds_and_paces_attempts():
    policy = RecompilePolicy(max_attempts=3, min_interval_s=3600)
    now = time.time()
    assert recompile_allowed(ApiHealth(preferred=False), policy, now)[0]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now - 60))
    stamp = stamp[:-2] + ":" + stamp[-2:]
    ok, why = recompile_allowed(ApiHealth(recompile_attempts=1, last_recompile_at=stamp), policy, now)
    assert not ok and "allowed after" in why
    assert recompile_allowed(ApiHealth(recompile_attempts=1, last_recompile_at=stamp), policy,
                             now + 3600)[0]
    ok, why = recompile_allowed(ApiHealth(recompile_attempts=3, last_recompile_at=stamp), policy,
                                now + 10 * 3600)
    assert not ok and "record the search again" in why


def test_cli_play_says_the_tool_was_recompiled(crates, monkeypatch):
    from click.testing import CliRunner
    from anyuse import cli as C
    site = Site(crates.captured)
    served = _call(crates, site, Browser(crates.doms, site))
    monkeypatch.setattr(C, "_has_desktop_recording", lambda name: False)
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: served)
    res = CliRunner().invoke(C.cli, ["workflow", "play", "crates_search", "--param", "q=tokio"])
    assert res.exit_code == 0, res.output
    assert "Recompiled:" in res.output and "browser replay's traffic" in res.output
    from anyuse.mcpserver import meta_tools as MT
    assert MT._web_success(served)["recompiled"]["swapped"] is True


def test_the_replay_keeps_only_credential_free_finished_get_responses():
    events = [
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "1", "type": "Document",
            "request": {"url": "https://s.test/search?q=x", "method": "GET", "headers": {}}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "1", "type": "Document",
            "response": {"status": 200, "mimeType": "text/html"}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "1"}},
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "2", "type": "Fetch",
            "request": {"url": "https://s.test/api?q=x", "method": "GET",
                        "headers": {"Authorization": "Bearer abc"}}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "2", "response": {"status": 200, "mimeType": "application/json"}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "2"}},
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "3", "type": "XHR",
            "request": {"url": "https://s.test/graphql", "method": "POST", "headers": {}}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "3", "response": {"status": 200, "mimeType": "application/json"}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "3"}},
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "4", "type": "Image",
            "request": {"url": "https://s.test/a.png", "method": "GET", "headers": {}}}},
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "5", "type": "XHR",
            "request": {"url": "https://s.test/api/v2?q=x", "method": "GET", "headers": {}}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "5", "response": {"status": 200, "mimeType": "application/json"}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "5"}},
    ]
    kept = traffic_from_events(events)
    assert [(e["url"], e["_rid"]) for e in kept] == [("https://s.test/search?q=x", "1"),
                                                     ("https://s.test/api/v2?q=x", "5")]
    assert all(e["body"] is None and e["method"] == "GET" for e in kept)


def test_a_replay_without_capture_returns_no_evidence(crates):
    site = Site(crates.captured)
    from anyuse.execution.cdp_replay import ReplayTemplate
    template = ReplayTemplate.from_dict(crates.record["browser"]["template"])
    executor = ReplayExecutor(driver=Browser(crates.doms, site))
    assert "evidence" not in executor.execute(template, {"q": "tokio"})
    got = executor.execute(template, {"q": "tokio"}, capture_traffic=True)
    assert got["success"] and got["evidence"]["network"][1]["url"].startswith("https://crates.io/api/v2/")


class EmptyForUnknown(Browser):
    """The same pages; an input with no recorded page gets the site's "no results" page."""

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        if q in self.doms:
            return super().open(url, settle_s, timeout_s, capture_traffic=capture_traffic)
        self.opened.append(url)
        network = ([{"url": url, "method": "GET", "status": 200, "mime": "text/html",
                     "body": "<html><body>No results</body></html>", "resource_type": "Document",
                     "headers": {}, "bytes": 40}] if capture_traffic else [])
        return PageResult(url=url, dom=f"<html><body><p>No results for {q}</p></body></html>",
                          text=f"No results for {q}", status=200, elapsed_ms=500.0, network=network)


def test_a_no_results_call_recompiles_from_an_input_the_selector_was_fitted_on(crates):
    # the first call after the API moved finds nothing: its page compiles nothing, a fitted one does
    site = Site(crates.captured)
    browser = EmptyForUnknown(crates.doms, site)
    out = _call(crates, site, browser, q="zzz-no-such-crate")
    assert out["ok"] and out["served_by"] == "browser_replay" and out["empty_state"]
    rec = out["recompiled"]
    assert rec["swapped"] is True and rec["attempt"] == 1, rec
    assert "https://crates.io/search?q=serde" in browser.opened
    stored = _stored(crates)
    assert "/api/v2/crates" in stored["template"]["url"] and stored["proof"]["status"] == "proved"
    assert sorted(c["value"] for c in stored["proof"]["cases"]) == ["clap", "tokio"]


def test_a_signed_in_tool_is_not_recompiled_from_its_stored_sessions(crates, monkeypatch):
    from anyuse.execution import auto_recompile as AR
    paths = []
    for i, session in enumerate(crates.sessions):
        path = crates.tmp / f"s{i}.json"
        path.write_text(json.dumps({**session, "login_replay_site": "crates.io"}))
        paths.append(str(path))
    reached = []
    monkeypatch.setattr(A, "compile_sessions", lambda sessions, **kw: reached.append(sessions))

    def demoted(**template):
        record = _stored(crates)
        record["template"].pop("cookie_refs", None)
        record["template"].update(template)
        record["health"] = {"preferred": False, "demoted_reason": "out of service"}
        record["source"]["paths"] = {"recording": paths[0], "selection": [paths[1]],
                                     "fresh": paths[2:]}
        persist_compiled(crates.storage, "crates_search", record)

    def recompile():
        return AR.recompile(crates.storage, "crates_search", {"q": "tokio"},
                            executor=ApiStepExecutor(transport=Site(crates.captured)),
                            hosts_path=crates.hosts, why="out of service")

    demoted(cookie_refs=["session_id"])                 # a login=replay tool
    out = recompile()
    assert reached == [] and out["attempted"] is False and out["next_attempt_after"] == "never"
    assert "signed-in" in out["reason"], out
    stored = _stored(crates)
    assert stored["health"]["recompile_attempts"] == 0
    assert "automatic recompile skipped" in stored["health"]["demoted_reason"]
    # sessions captured with --login-replay are refused even when the template carries no cookie
    demoted()
    out = recompile()
    assert reached == [] and out["swapped"] is False and "signed-in capture" in out["reason"], out
    assert _stored(crates)["health"]["recompile_attempts"] == 0


def test_a_recompile_from_stored_sessions_keeps_the_keys_the_tool_was_published_with(crates, monkeypatch):
    # a tool published with positional field_N keys keeps them when it is compiled again from its stored attach
    # sessions, as from stored recordings: the sessions' compile does not name them after the page's labels
    from anyuse.execution import api_verify as V
    from anyuse.execution import auto_recompile as AR
    paths = []
    for i, session in enumerate(crates.sessions):
        path = crates.tmp / f"s{i}.json"
        path.write_text(json.dumps(session))
        paths.append(str(path))
    real_sessions, real_fit = A.compile_sessions, V.compile_and_verify_with_fit
    asked, fitted = [], []

    def sessions_spy(sessions, **kw):
        asked.append(kw.get("label_keys"))
        return real_sessions(sessions, **kw)

    def fit_spy(*a, **kw):
        fitted.append(kw.get("label_keys"))
        return real_fit(*a, **kw)
    monkeypatch.setattr(A, "compile_sessions", sessions_spy)
    monkeypatch.setattr(V, "compile_and_verify_with_fit", fit_spy)
    published = _stored(crates)
    for extract, want in (({"field_1": "crates[*].name"}, False), (None, True)):
        record = json.loads(json.dumps(published))
        if extract:
            record["template"]["extract"] = extract
        record["health"] = {"preferred": False, "demoted_reason": "out of service"}
        record["source"]["paths"] = {"recording": paths[0], "selection": [paths[1]], "fresh": paths[2:]}
        persist_compiled(crates.storage, "crates_search", record)
        AR.recompile(crates.storage, "crates_search", {"q": "tokio"},
                     executor=ApiStepExecutor(transport=Site(crates.captured)), hosts_path=crates.hosts,
                     why="out of service")
        assert asked[-1] is want and fitted[-1] is want, (asked, fitted)


def test_a_tool_a_registered_skill_pins_is_not_swapped_automatically(crates, monkeypatch):
    from anyuse.execution.routine import CompiledToolRunner
    # a browser replay is configured, so a demoted tool stays runnable through it (never reached)
    monkeypatch.setenv("ANYUSE_CDP_ENDPOINT", "http://127.0.0.1:9")
    from anyuse.execution.skill_manifest import MANIFEST_SCHEMA, step_from_compiled, validate_manifest
    from anyuse.mcpserver.skill_tools import load_registry, skills_dir
    row = CT.list_compiled(crates.storage, crates.hosts)[0]
    search = step_from_compiled("search", row, "crates")
    manifest = {"schema": MANIFEST_SCHEMA, "kind": "skill", "name": "crates_names", "version": "0.1.0",
                "summary": "Search crates.io.",
                "inputs": {"q": {"type": "string", "maxLength": 100, "minLength": 1}},
                "apps": {"crates": {"origin": "https://crates.io", "tenant": "public"}},
                "data_transfers": [], "steps": [search],
                "outputs": {"items": {"type": "array", "items": {"type": "scalar"},
                                      "from": {"step": "search", "output": "items"}}}}
    assert validate_manifest(manifest) == []
    os.makedirs(skills_dir(), exist_ok=True)
    with open(os.path.join(skills_dir(), "crates_names.manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    assert "crates_names" in load_registry()
    runner = CompiledToolRunner(storage=crates.storage, hosts_path=crates.hosts,
                                executor=ApiStepExecutor(transport=Site(crates.captured)))
    assert runner.fresh_check(search, {"q": "tokio"}) is None
    site = Site(crates.captured)
    browser = Browser(crates.doms, site)
    out = _call(crates, site, browser)
    assert out["ok"] and out["served_by"] == "browser_replay"
    rec = out["recompiled"]
    assert rec["swapped"] is False and "'crates_names'" in rec["reason"], rec
    assert "pin" in out["next"] and browser.opened == ["https://crates.io/search?q=tokio"]
    stored = _stored(crates)
    assert "/api/v1/crates" in stored["template"]["url"]
    assert stored["health"]["recompile_attempts"] == 0
    # the skill keeps running through the browser replay
    assert runner.fresh_check(search, {"q": "tokio"}) is None


def test_concurrent_calls_to_an_out_of_service_tool_make_one_attempt(crates, monkeypatch):
    import threading
    from anyuse.execution import api_recompile as R
    from anyuse.execution import auto_recompile as AR
    record = _stored(crates)
    record["health"] = {"preferred": False, "demoted_reason": "out of service"}
    persist_compiled(crates.storage, "crates_search", record)
    first_checking, second_coming = threading.Event(), threading.Event()
    real_allowed, real_signed_in = R.recompile_allowed, AR.signed_in

    def allowed(health, policy=None, now=None):
        if threading.current_thread().name == "first":
            # the first call is at the pacing check; the second one heads for the same check
            first_checking.set()
            assert second_coming.wait(20)
            time.sleep(0.3)
        return real_allowed(health, policy, now)

    def signed_in(rec):
        if threading.current_thread().name == "second":
            second_coming.set()
        return real_signed_in(rec)
    monkeypatch.setattr(R, "recompile_allowed", allowed)
    monkeypatch.setattr(AR, "signed_in", signed_in)
    site = Site(crates.captured, broken_new=True)        # no swap, so the count stays visible
    browsers = {"first": Browser(crates.doms, site), "second": Browser(crates.doms, site)}
    outs = {}

    def call(name, q):
        outs[name] = _call(crates, site, browsers[name], q=q)
    first = threading.Thread(name="first", target=call, args=("first", "tokio"))
    second = threading.Thread(name="second", target=call, args=("second", "clap"))
    first.start()
    assert first_checking.wait(20)
    second.start()
    first.join(30)
    second.join(30)
    attempted = sorted(name for name, out in outs.items() if out["recompiled"]["attempted"])
    assert attempted == ["first"], {n: o["recompiled"] for n, o in outs.items()}
    assert "allowed after" in outs["second"]["recompiled"]["reason"]
    assert browsers["second"].opened == ["https://crates.io/search?q=clap"]
    assert _stored(crates)["health"]["recompile_attempts"] == 1


def test_a_calls_outcome_keeps_a_concurrent_recompiles_count_and_swap(crates):
    from anyuse.execution.api_recompile import save_call_outcome
    stale = _stored(crates)                               # what a call loaded before the recompile
    record = _stored(crates)
    record["health"] = {"preferred": False, "recompile_attempts": 1,
                        "last_recompile_at": "2026-10-20T12:00:00+02:00"}
    record["recompile"] = {"attempt": 1}
    persist_compiled(crates.storage, "crates_search", record)
    stale["health"] = {"preferred": False, "consecutive_failures": 1, "total_runs": 1}
    assert save_call_outcome(crates.storage, "crates_search", json.loads(json.dumps(stale)))
    stored = _stored(crates)
    assert stored["health"]["recompile_attempts"] == 1 and stored["health"]["total_runs"] == 1
    assert stored["recompile"] == {"attempt": 1}
    # a request swapped in meanwhile is not overwritten with the one the call ran
    record = _stored(crates)
    record["template"]["url"] = record["template"]["url"].replace("/api/v1/", "/api/v2/")
    persist_compiled(crates.storage, "crates_search", record)
    assert save_call_outcome(crates.storage, "crates_search", stale) is False
    assert "/api/v2/" in _stored(crates)["template"]["url"]


def _page(value, n, traffic=True):
    """A browser replay result for ``value`` with ``n`` rows, as the executor returns it."""
    items = [f"{value}-{i}" if i else value for i in range(n)]
    url = f"https://crates.io/search?q={value}"
    network = ([{"url": f"https://crates.io/api/v2/crates?q={value}", "method": "GET",
                 "status": 200, "mime": "application/json", "body": json.dumps(items)}]
               if traffic else [])
    return {"success": True, "url": url, "extracted": {"items": items, "labels": items},
            "evidence": {"final_url": url, "dom": "<html></html>", "text": " ".join(items),
                         "network": network}}


def _evidence(record, call, pages):
    from anyuse.execution.auto_recompile import live_evidence
    opened = []

    def replay(params, capture):
        opened.append(params["q"])
        return pages.get(params["q"]) or {"success": False}
    return live_evidence(record, {"q": call["url"].split("=")[-1]}, call, replay), opened


def test_a_sparse_call_page_is_not_the_compile_recording_when_a_fitted_list_page_has_traffic(crates):
    record = _stored(crates)
    floor = record["browser"]["template"]["expect"]["min_items"]
    assert floor >= 3 and record["browser"]["fitted_on"] == ["serde", "regex"]
    lists = {v: _page(v, floor) for v in ("serde", "regex", "tokio", "clap")}
    # a correct one-row answer the contract served: proof material, not the compile page
    ev, opened = _evidence(record, _page("tokio-macros", 1), lists)
    assert ev["recording"]["param"]["value"] == "serde"
    assert [r["param"]["value"] for r in ev["selection"]] == ["regex"]
    assert [r["param"]["value"] for r in ev["fresh"]] == ["tokio", "clap"]
    assert "tokio-macros" not in opened
    # a fitted page below the floor is passed over for the next fitted list page
    ev, _ = _evidence(record, _page("tokio-macros", 1), {**lists, "serde": _page("serde", 2)})
    assert ev["recording"]["param"]["value"] == "regex"
    # a call page with the floor's rows is still compiled from first
    ev, opened = _evidence(record, _page("tokio-macros", floor), lists)
    assert ev["recording"]["param"]["value"] == "tokio-macros"
    assert [r["param"]["value"] for r in ev["selection"]] == ["serde"]


def test_a_sparse_call_page_is_the_compile_recording_when_nothing_else_has_traffic(crates):
    record = _stored(crates)
    floor = record["browser"]["template"]["expect"]["min_items"]
    lists = {v: _page(v, floor, traffic=False) for v in ("serde", "regex", "tokio", "clap")}
    ev, _ = _evidence(record, _page("tokio-macros", 1), lists)
    assert ev["recording"]["param"]["value"] == "tokio-macros"
    # a fitted page with traffic, even a small one, comes before the call's small page
    ev, _ = _evidence(record, _page("tokio-macros", 1), {**lists, "serde": _page("serde", 2)})
    assert ev["recording"]["param"]["value"] == "serde"


def test_the_replayed_recordings_carry_the_browser_rungs_visible_only_reading(crates):
    from anyuse.execution.cdp_replay import template_from_recording
    record = _stored(crates)
    floor = record["browser"]["template"]["expect"]["min_items"]
    lists = {v: _page(v, floor) for v in ("serde", "regex", "tokio", "clap")}
    ev, _ = _evidence(record, _page("tokio-macros", floor), lists)
    assert not any(r.get("ground_truth_visible_only")
                   for r in (ev["recording"], *ev["selection"], *ev["fresh"]))
    record["browser"]["template"]["visible_only"] = True
    ev, _ = _evidence(record, _page("tokio-macros", floor), lists)
    recs = (ev["recording"], *ev["selection"], *ev["fresh"])
    assert len(recs) == 4 and all(r["ground_truth_visible_only"] is True for r in recs)
    # a browser rung rebuilt from the recording reads and waits visible-only
    rebuilt = template_from_recording(ev["recording"], "q", readiness=True)
    assert rebuilt.visible_only and rebuilt.readiness["visible_only"]


def test_a_tool_without_fitted_inputs_waits_for_a_non_held_input_without_burning_attempts(crates):
    record = _stored(crates)
    record["browser"].pop("fitted_on")                    # compiled before fitted_on was kept
    persist_compiled(crates.storage, "crates_search", record)
    site = Site(crates.captured)
    browser = Browser(crates.doms, site)
    out = _call(crates, site, browser, q="tokio")         # a held-out proof input
    rec = out["recompiled"]
    assert rec["attempted"] is False and "held-out" in rec["reason"], rec
    assert _stored(crates)["health"]["recompile_attempts"] == 0
    assert browser.opened == ["https://crates.io/search?q=tokio"]
    out = _call(crates, site, browser, q="serde")         # not held out: it compiles from it
    assert out["recompiled"]["swapped"] is True and out["recompiled"]["attempt"] == 1, out["recompiled"]
    assert _stored(crates)["proof"]["status"] == "proved"


def test_the_replay_drops_responses_sent_with_a_session_cookie():
    def request(rid, url, extra=None, before=False):
        sent = {"method": "Network.requestWillBeSent", "params": {
            "requestId": rid, "type": "XHR",
            "request": {"url": url, "method": "GET", "headers": {}}}}
        info = ([{"method": "Network.requestWillBeSentExtraInfo",
                  "params": {"requestId": rid, **extra}}] if extra else [])
        return [*(info if before else []), sent, *([] if before else info),
                {"method": "Network.responseReceived", "params": {
                    "requestId": rid, "response": {"status": 200, "mimeType": "application/json"}}},
                {"method": "Network.loadingFinished", "params": {"requestId": rid}}]
    events = [
        *request("9", "https://s.test/api/me?q=x", {
            "headers": {"Cookie": "sessionid=abc; csrftoken=x"},
            "associatedCookies": [{"cookie": {"name": "sessionid"}, "blockedReasons": []}]}),
        *request("10", "https://s.test/api/orders?q=x",
                 {"headers": {"cookie": "auth_token=abc"}}, before=True),
        *request("11", "https://s.test/api/search?q=x", {
            "headers": {"Cookie": "_ga=1"},
            "associatedCookies": [{"cookie": {"name": "_ga"}, "blockedReasons": []},
                                  {"cookie": {"name": "sessionid"},
                                   "blockedReasons": ["SameSiteStrict"]}]}),
        *request("12", "https://s.test/api/v2?q=x", {"headers": {"X-Api-Key": "k"}}),
    ]
    assert [e["_rid"] for e in traffic_from_events(events)] == ["11"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
