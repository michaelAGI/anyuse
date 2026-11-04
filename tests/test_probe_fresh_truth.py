"""A request out of service comes back when its answer matches what the site shows now
(``fresh_truth.answer_holds_now``, through the probe in ``mcpserver/compiled_tools.run_compiled``
and the heal's restore in execution/step_heal.py), not only when it repeats the last good
answer.

Offline: the in-test ``Feed`` site of tests/test_heal_fresh_truth.py (a server-rendered page
whose list changes between reads), the crates.io attach sessions with a fake browser driver,
and a fake clock for the health record. The check model is a fake given to
``model_check.using``. Nothing here opens a socket, starts a browser or calls a model.
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
from anyuse.execution import model_check as MC
from anyuse.execution.api_compile import (compiled_record, load_compiled, persist_compiled,
                                          with_runtime_rungs)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.cdp_replay import ReplayExecutor
from anyuse.mcpserver.compiled_tools import run_compiled

_TESTS = pathlib.Path(__file__).resolve().parent
START = 1_800_000_000.0


def _load(name):
    spec = importlib.util.spec_from_file_location(f"probe_{name}", _TESTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HF = _load("test_heal_fresh_truth")
AR = _load("test_auto_recompile")


class Clock:
    def __init__(self):
        self.now = START

    def time(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_ROBOTS",
                 "ANYUSE_AUTO_HEAL", "ANYUSE_OPENJEV_URL", "ANYUSE_JUDGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_MODEL_CHECKS", "none")
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(perm, "_default_fetch", lambda url, *a, **k: "User-agent: *\nAllow: /\n")
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    clock = Clock()
    monkeypatch.setattr(R, "time", clock)
    hosts = str(tmp_path / "hosts.json")
    for host in (HF.HOST, "crates.io"):
        perm.acknowledge_host(host, path=hosts)
    yield SimpleNamespace(tmp=tmp_path, hosts=hosts, clock=clock,
                          storage=HF.Storage(str(tmp_path / "wf")))
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


def _demote(env, name, probe_due=True):
    """The stored request out of service on wrong answers; its probe is due (or not yet)."""
    record = load_compiled(env.storage, name)
    at = env.clock.now - (601 if probe_due else 0)
    record["health"] = {**(record.get("health") or {}), "preferred": False,
                        "demoted_reason": "3 consecutive failures (>= 3); last error: postcondition failed",
                        "consecutive_failures": 3, "total_runs": 3, "total_failures": 3,
                        "last_error": "postcondition failed", "demoted_at": R._at(at),
                        "probe_after": R._at(at + 600)}
    persist_compiled(env.storage, name, record)


def _feed_call(env, feed, q, **kw):
    return run_compiled(HF.NAME, {"q": q}, storage=env.storage, hosts_path=env.hosts,
                        executor=ApiStepExecutor(transport=feed), **kw)


# ---------------------------------------------------------------------------
# a server-rendered result page
# ---------------------------------------------------------------------------

def test_a_demoted_list_whose_items_changed_comes_back_when_the_page_shows_the_same_list_now(env):
    feed = HF.tool(env)
    _demote(env, HF.NAME)
    feed.epoch, feed.churn = 1, True
    out = _feed_call(env, feed, "amber")
    assert out["ok"] and out["served_by"] == "api" and out["restored"]["by"] == "probe", out
    assert out["result"] == HF.gold(feed, "amber")
    assert "matches what the result page shows" in out["restored"]["why"], out["restored"]
    api = [q for path, q in feed.log if path == "/api/search"]
    assert api == ["amber", "stone"] and feed.pages()[0] == "stone", feed.log
    assert load_compiled(env.storage, HF.NAME)["health"]["preferred"] is True


def test_a_probe_answer_the_page_contradicts_stays_out(env, monkeypatch):
    monkeypatch.setenv("ANYUSE_AUTO_HEAL", "off")        # the probe alone (a heal would recompile)
    feed = HF.tool(env)
    _demote(env, HF.NAME)
    feed.epoch = 1
    real = feed.api_body

    def lying(q):
        # the API answers another word's list for every input; the page lists the right one
        return real(feed._other(q))
    feed.api_body = lying
    out = _feed_call(env, feed, "amber")
    assert not out["ok"] and out["probe"]["restored"] is False, out
    assert "does not match what the result page shows" in out["probe"]["why"], out["probe"]
    assert load_compiled(env.storage, HF.NAME)["health"]["preferred"] is False


def test_the_heal_restore_uses_the_same_check(env):
    feed = HF.tool(env)
    _demote(env, HF.NAME, probe_due=False)
    feed.epoch = 1                         # the request answers; only the prices moved
    out = _feed_call(env, feed, "amber")
    assert out["ok"] and out["restored"]["by"] == "heal", out
    assert out["restored"]["heal"]["restored"] is True
    record = load_compiled(env.storage, HF.NAME)
    assert record["health"]["preferred"] is True
    assert record["drift_anchor"]["items"][0] != "stone-core 10 EUR"
    assert record["heal_guard"]["ops"]["heal"]["healed_at"]


# ---------------------------------------------------------------------------
# a page a script fills in: the browser replay is the ground truth
# ---------------------------------------------------------------------------

def _crates(env):
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
    persist_compiled(env.storage, "crates_search", record)
    return record, captured, {r["param"]["value"]: r["dom"] for r in recs}


def test_the_browser_replay_is_the_ground_truth_when_the_page_is_filled_by_a_script(env):
    record, captured, doms = _crates(env)
    assert not (record.get("page") or {}).get("html_holds_items")
    anchor = record["drift_anchor"]
    # the last good answer is a day old: the site lists other crates for it now
    record["drift_anchor"] = {**anchor, "items": ["old-a", "old-b", "old-c"]}
    persist_compiled(env.storage, "crates_search", record)
    _demote(env, "crates_search")
    site = AR.Site(captured, moved=False)
    browser = AR.Browser(doms, site)
    out = run_compiled("crates_search", {"q": anchor["value"]}, storage=env.storage,
                       hosts_path=env.hosts, executor=ApiStepExecutor(transport=site),
                       browser_executor=ReplayExecutor(driver=browser))
    assert out["ok"] and out["served_by"] == "api" and out["restored"]["by"] == "probe", out
    assert "browser replay" in out["restored"]["why"], out["restored"]
    assert len(browser.opened) == 1 and f"q={anchor['value']}" in browser.opened[0], browser.opened


# ---------------------------------------------------------------------------
# no page and no browser: the answer must change with the input, and the model decides
# ---------------------------------------------------------------------------

def _body(*names):
    return json.dumps({"crates": [{"id": n} for n in names]})


class Api:
    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.sent.append(q)
        return ApiResponse(200, {"Content-Type": "application/json"},
                           self.answers.get(q, _body()))


def _bare(env):
    template = RequestTemplate(
        method="GET", url="https://crates.io/api/v1/crates", query={"q": "{q}"},
        headers={"accept": "application/json"}, extract={"items": "crates[*].id"},
        expect={"status_in": [200], "content_type": "json", "path": "crates",
                "min_items": 1, "required_keys": ["id"]}, params=["q"])
    record = compiled_record(template, {"passed": True, "passed_count": 2, "case_count": 2,
                                        "reason": "passed on 2 fresh values"}, workflow="bare")
    record["drift_anchor"] = {"value": "serde", "items": ["serde", "serde_json", "serde_yaml"],
                              "count": 3, "at": "2026-01-01T00:00:00+00:00"}
    persist_compiled(env.storage, "bare", record)
    _demote(env, "bare")
    # serde lists other crates now; tokio's answer is another list
    return Api({"serde": _body("serde-new", "serde-next", "serde-core"),
                "tokio": _body("tokio", "tokio-util", "tokio-stream")})


def _bare_call(env, api):
    return run_compiled("bare", {"q": "tokio"}, storage=env.storage, hosts_path=env.hosts,
                        executor=ApiStepExecutor(transport=api))


def test_without_a_page_or_browser_two_different_lists_need_the_model(env):
    api = _bare(env)
    out = _bare_call(env, api)
    assert not out["ok"] and out["probe"]["restored"] is False, out
    assert "no page or browser replay shows the list now" in out["probe"]["why"]
    assert api.sent == ["tokio", "serde"]


def test_the_model_says_the_answer_is_this_searchs_list(env):
    api = _bare(env)

    def asker(instructions, state, guard_obs):
        line = state.rsplit(">> ", 1)[-1]
        return 0.95 if line.startswith("serde") else 0.05
    with MC.using(asker):
        out = _bare_call(env, api)
    assert out["ok"] and out["restored"]["by"] == "probe", out
    assert out["model_calls"] > 0 and api.sent == ["tokio", "serde"]


def test_a_made_up_input_the_site_does_not_answer_shows_nothing_and_the_model_is_not_asked(env):
    api = _bare(env)
    real = api.send

    def send(method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        if q not in api.answers:
            api.sent.append(q)
            return ApiResponse(503, {"Content-Type": "text/plain"}, "busy")
        return real(method, url, headers, body, timeout_s)
    api.send = send
    asked = []
    with MC.using(lambda i, s, o: asked.append(s) or 0.95):
        out = run_compiled("bare", {"q": "serde"}, storage=env.storage, hosts_path=env.hosts,
                           executor=ApiStepExecutor(transport=api))
    assert not out["ok"] and out["probe"]["restored"] is False, out
    assert "got no answer from the site" in out["probe"]["why"], out["probe"]
    assert asked == [] and len(api.sent) == 2, api.sent


def test_a_request_that_gives_the_same_list_for_every_input_stays_out(env):
    api = _bare(env)
    api.answers["tokio"] = api.answers["serde"]
    with MC.using(lambda i, s, o: 0.95):
        out = _bare_call(env, api)
    assert not out["ok"] and "the same list for 'serde' and 'tokio'" in out["probe"]["why"], out
    assert out.get("model_calls", 0) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
