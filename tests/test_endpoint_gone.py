"""A compiled request that keeps answering a server error while the site's own pages answer
is taken out of service as gone, then healed or recompiled (``api_recompile.gone_check_due``
through ``mcpserver/compiled_tools.run_compiled``); a site that is down everywhere never is.

Offline: the quickstart fixture site and the crates.io attach sessions behind fake
transports, a fake browser driver and a fake clock for the health record. Nothing here
opens a socket, starts a browser or calls a model.
"""
import importlib.util
import json
import os
import pathlib
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

from anyuse.execution import api_permission as perm
from anyuse.execution import api_recompile as R
from anyuse.execution import attach as A
from anyuse.execution.api_compile import (compiled_record, load_compiled, persist_compiled,
                                          with_runtime_rungs)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.cdp_replay import ReplayExecutor
from anyuse.mcpserver.compiled_tools import run_compiled

_TESTS = pathlib.Path(__file__).resolve().parent
START = 1_800_000_000.0


def _load(name):
    spec = importlib.util.spec_from_file_location(f"gone_{name}", _TESTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SH = _load("test_step_heal")          # the fixture site, its recordings and its storage
AR = _load("test_auto_recompile")     # the crates.io sessions, their API and their browser pages
HOST = SH.HOST


class Clock:
    def __init__(self):
        self.now = START

    def time(self):
        return self.now


class Down(SH.Site):
    """The fixture site whose API answers 503; with ``everywhere`` every path does."""

    def __init__(self, shape="original", everywhere=False):
        super().__init__(shape)
        self.api_down, self.everywhere = True, everywhere

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        if self.everywhere or (self.api_down and sp.path == "/api/search"):
            self.log.append((sp.path, urllib.parse.parse_qs(sp.query).get("q", [""])[0]))
            return ApiResponse(503, {"Content-Type": "text/plain"}, "busy")
        return super().send(method, url, headers, body, timeout_s)


def _robots(monkeypatch, robots):
    def fetch(robots_url, *args, **kwargs):
        return robots.get(urllib.parse.urlsplit(robots_url).netloc)
    monkeypatch.setattr(perm, "_default_fetch", fetch)
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_ROBOTS",
                 "ANYUSE_AUTO_HEAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    _robots(monkeypatch, {HOST: "User-agent: *\nAllow: /\n"})
    clock = Clock()
    monkeypatch.setattr(R, "time", clock)
    hosts = str(tmp_path / "hosts.json")
    perm.acknowledge_host(HOST, path=hosts)
    yield SimpleNamespace(tmp=tmp_path, hosts=hosts, clock=clock,
                          storage=SH.Storage(str(tmp_path / "wf")))
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


def _call(env, site, q, **kw):
    return run_compiled("fixture_search", {"q": q}, storage=env.storage, hosts_path=env.hosts,
                        executor=ApiStepExecutor(transport=site), **kw)


def _stored(env):
    return load_compiled(env.storage, "fixture_search")


def _health(env):
    return R.ApiHealth.from_dict(_stored(env).get("health"))


def _previous(env):
    path = os.path.join(env.storage.get_workflow_dir("fixture_search"), R.PREVIOUS_FILE)
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------

def test_an_endpoint_that_keeps_answering_503_while_the_page_answers_is_taken_out_and_healed(env):
    SH._tool(env)
    site = Down()
    for q in ("amber", "birch", "cedar"):
        out = _call(env, site, q)
        assert not out["ok"] and out["transient"] and "heal" not in out, out
        env.clock.now += 600
    assert site.pages() == [], "no site check before 30 minutes of failures"
    out = _call(env, site, "delta")
    assert out["ok"] and out["served_by"] == "api" and out["result"] == SH._gold("delta"), out
    assert out["healed"]["swapped"] and out["healed"]["rung"] == "recompile", out["healed"]
    # the 4th failure, then one read of the site's own page, then the heal's reads
    first_page = next(i for i, (path, _q) in enumerate(site.log) if path == "/search")
    assert site.log[first_page - 1] == ("/api/search", "delta")
    previous = R.ApiHealth.from_dict(_previous(env)["health"])
    assert R.demotion_cause(previous) == "gone" and previous.endpoint_removed, previous
    assert "while the site's own page" in previous.endpoint_removed
    stored = _stored(env)
    assert stored["template"]["url"] == f"{SH.ORIGIN}/search" and stored["health"]["preferred"]
    site.log = []
    assert _call(env, site, "ember")["result"] == SH._gold("ember")
    assert site.log == [("/search", "ember")]


def test_a_site_that_is_down_everywhere_is_never_taken_out(env):
    SH._tool(env)
    site = Down(everywhere=True)
    checks = []
    for i in range(30):                              # every 10 minutes for 5 hours
        before = len(site.pages())
        out = _call(env, site, "amber")
        assert not out["ok"] and "heal" not in out, out
        if len(site.pages()) > before:
            checks.append(env.clock.now)
        env.clock.now += 600
    health = _health(env)
    assert health.preferred is True and health.consecutive_failures == 0, health
    assert health.endpoint_removed == "" and health.last_site_check, health
    assert checks and all(b - a >= 1800 for a, b in zip(checks, checks[1:])), checks
    assert len(site.pages()) == len(checks)


def test_gone_check_due_and_first_outage_at_are_pure():
    h = R.ApiHealth()
    for i in range(3):
        h = R.record_outage(h, "non-2xx status 503", now=START + 600 * i)
    assert h.first_outage_at == R._at(START) and h.consecutive_outages == 3
    assert not R.gone_check_due(h, now=START + 1800)
    h = R.record_outage(h, "non-2xx status 503", now=START + 1700)
    assert not R.gone_check_due(h, now=START + 1700), "4 failures, but not over 30 minutes"
    assert R.gone_check_due(h, now=START + 1800)
    checked = R.note_site_check(h, now=START + 1800)
    assert not R.gone_check_due(checked, now=START + 3500)
    assert R.gone_check_due(checked, now=START + 3600)
    gone = R.mark_gone(checked, "the request failed while the page answered", now=START + 3600)
    assert not gone.preferred and R.demotion_cause(gone) == "gone"
    assert not R.gone_check_due(gone, now=START + 9999), "only a request in service"
    answered = R.record_outcome(h, False, "postcondition failed")
    assert answered.first_outage_at == "" and answered.consecutive_outages == 0
    restored = R.mark_restored({"health": gone.to_dict()}, "it answers", "probe")["health"]
    assert restored["endpoint_removed"] == restored["first_outage_at"] == restored["last_site_check"] == ""
    # a rate-limited probe says so, whatever took the request out
    limited = R.ApiHealth.from_dict({**gone.to_dict(), "last_probe": "429",
                                     "last_probe_rate_limited": True})
    assert R.demotion_cause(limited) == "rate_limit"


def _gone_record(env, offset=-601.0):
    """The stored request taken out as gone ``-offset`` seconds ago; its probe is due."""
    record = _stored(env)
    health = R.ApiHealth.from_dict(record.get("health"))
    for i in range(4):
        health = R.record_outage(health, "non-2xx status 503", now=START + offset - 1800 + 600 * i)
    health = R.mark_gone(R.note_site_check(health, now=START + offset),
                         "the compiled request failed 4 times over 30 min (non-2xx status 503) "
                         "while the site's own page answered", now=START + offset)
    record["health"] = health.to_dict()
    persist_compiled(env.storage, "fixture_search", record)


def test_a_probe_of_a_gone_endpoint_that_still_answers_503_keeps_the_gone_cause(env, monkeypatch):
    SH._tool(env)
    _gone_record(env)
    seen = []
    real = R.note_probe

    def noted(*args, **kwargs):
        health = real(*args, **kwargs)
        seen.append(health)
        return health
    monkeypatch.setattr(R, "note_probe", noted)
    site = Down()
    out = _call(env, site, "amber")
    assert site.log[0] == ("/api/search", "amber"), site.log
    assert seen and seen[0].last_probe_outage is False and R.demotion_cause(seen[0]) == "gone"
    # the heal runs (a request that is gone is not waited out like a site that is down)
    assert out["ok"] and out["healed"]["rung"] == "recompile", out


def test_a_gone_endpoint_that_answers_again_is_restored_and_forgets_the_outage(env):
    site = SH._tool(env)
    _gone_record(env)
    out = _call(env, site, "amber")
    assert out["ok"] and out["restored"]["by"] == "probe", out
    health = _health(env)
    assert health.preferred and health.endpoint_removed == "" and health.first_outage_at == ""
    assert health.last_site_check == "" and health.consecutive_outages == 0


# ---------------------------------------------------------------------------
# a lower rung that answers through the outage
# ---------------------------------------------------------------------------

class CratesDown(AR.Site):
    """The crates.io fake whose old endpoint answers 503 forever; the new one answers."""

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        if sp.path == "/api/v1/crates":
            self.sent.append((sp.netloc + sp.path, urllib.parse.parse_qs(sp.query).get("q", [""])[0]))
            return ApiResponse(503, {"Content-Type": "text/plain"}, "busy")
        if not sp.path.endswith("/v2/crates"):
            self.sent.append((sp.netloc + sp.path, ""))
            return ApiResponse(200, {"Content-Type": "text/html"}, "<html><body>crates</body></html>")
        return super().send(method, url, headers, body, timeout_s)


@pytest.fixture
def crates(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_ROBOTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    _robots(monkeypatch, dict(AR.ROBOTS))
    clock = Clock()
    monkeypatch.setattr(R, "time", clock)
    sessions = [json.load(open(os.path.join(AR.DATA, n))) for n in sorted(os.listdir(AR.DATA))]
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]
    compiled, proof, _report, recs = A.compile_sessions(
        sessions, executor=ApiStepExecutor(transport=AR.Site(captured, moved=False)),
        preflight=lambda _c: "")
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    storage = AR.Storage(str(tmp_path))
    persist_compiled(storage, "crates_search", record)
    hosts = str(tmp_path / "hosts.json")
    perm.acknowledge_host("crates.io", path=hosts)
    yield SimpleNamespace(storage=storage, hosts=hosts, clock=clock, captured=captured,
                          doms={r["param"]["value"]: r["dom"] for r in recs})
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


def test_the_browser_replay_answering_through_the_outage_counts_as_the_site_answering(crates):
    site = CratesDown(crates.captured)
    browser = AR.Browser(crates.doms, site)

    def call():
        return run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage,
                            hosts_path=crates.hosts, executor=ApiStepExecutor(transport=site),
                            browser_executor=ReplayExecutor(driver=browser))
    for _ in range(3):
        out = call()
        assert out["ok"] and out["served_by"] == "browser_replay" and "recompiled" not in out, out
        crates.clock.now += 600
    out = call()
    assert out["ok"] and out["served_by"] == "browser_replay", out
    assert out["recompiled"]["swapped"] is True, out["recompiled"]
    assert out["recompiled"]["from"] == "the browser replay's traffic"
    stored = load_compiled(crates.storage, "crates_search")
    assert "/api/v2/crates" in stored["template"]["url"] and stored["health"]["preferred"]
    assert not [s for s in site.sent if not s[0].endswith("/crates")], "no site-check request"
    site.sent.clear()
    browser.opened.clear()
    out = call()
    assert out["served_by"] == "api" and browser.opened == [], out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
