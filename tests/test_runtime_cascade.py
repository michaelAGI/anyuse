"""The runtime cascade of a compiled web tool (``mcpserver/compiled_tools.run_compiled``):
compiled request -> browser replay -> agent -> the person.

Offline: fakes for the API transport, the browser driver and the agent; the record is compiled
from four real crates.io attach sessions (``testdata/attach_crates``). Nothing here opens a
socket, starts a browser, calls a model or touches the desktop.
"""
import json
import os
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

from anyuse.execution import attach as A
from anyuse.execution.api_compile import compiled_record, persist_compiled, with_runtime_rungs
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor
from anyuse.mcpserver import compiled_tools as CT
from anyuse.mcpserver.compiled_tools import run_compiled

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "testdata", "attach_crates")
CHANGED = '{"results": []}'                      # the API changed its shape


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


class Api:
    """A transport answering from the captured crates.io API bodies, or a fixed failure."""

    def __init__(self, captured, status=200, body=None):
        self.captured, self.status, self.body, self.sent = captured, status, body, []

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.sent.append(q)
        if self.body is not None or self.status != 200:
            return ApiResponse(self.status, {"Content-Type": "application/json"}, self.body or "{}")
        return ApiResponse(200, {"Content-Type": "application/json"}, self.captured[q])


class Browser:
    """A browser driver serving each value's recorded result page (or nothing)."""

    def __init__(self, doms, empty=False):
        self.doms, self.empty, self.opened = doms, empty, []

    def open(self, url, settle_s, timeout_s):
        self.opened.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        dom = "" if self.empty else self.doms.get(q, "")
        return PageResult(url=url, dom=dom, text="results" if dom else "", status=200,
                          elapsed_ms=812.0)


def _agent(calls, result=("tokio", "tokio-util", "tokio-stream"), ok=True, outcome="done"):
    def run(task, **kw):
        calls.append((task, kw))
        return {"ok": ok, "outcome": outcome, "reason": "" if ok else "the agent stopped",
                "result": list(result) if ok else None, "model_calls": 2, "cost_usd": 0.031,
                "steps": 3, "policy": kw.get("policy")}
    return run


@pytest.fixture
def crates(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_TIER2_READINESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    sessions = [json.load(open(os.path.join(DATA, n))) for n in sorted(os.listdir(DATA))]
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]
    compiled, proof, report, recs = A.compile_sessions(
        sessions, executor=ApiStepExecutor(transport=Api(captured)), preflight=lambda _c: "")
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "crates_search", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("crates.io", path=hosts)
    doms = {r["param"]["value"]: r["dom"] for r in recs}
    labels = {r["param"]["value"]: (r.get("ui") or {}).get("labels") or [] for r in recs}
    return SimpleNamespace(storage=storage, hosts=hosts, record=record, recs=recs,
                           captured=captured, doms=doms, labels=labels)


def _stored(crates):
    from anyuse.execution.api_compile import load_compiled
    return load_compiled(crates.storage, "crates_search")


def test_the_record_carries_a_browser_rung_and_a_task(crates):
    browser = crates.record["browser"]
    assert browser["template"]["url"] == "https://crates.io/search?q={q}"
    assert browser["read"] == "labels" and browser["template"]["read_labels"] is True
    assert browser["origin"] == "attach"
    task = crates.record["task"]
    assert "{q}" in task["text"] and task["site"] == "crates.io" and task["param"] == "q"


def test_the_browser_rung_reads_on_readiness_unless_switched_off(crates, monkeypatch):
    from anyuse.execution.api_compile import runtime_rungs
    ready = crates.record["browser"]["template"]["readiness"]
    assert ready["fallback"] == "timer", ready
    assert ready["selector"] == crates.recs[0]["ground_truth_selector"]
    assert ready["min_items"] == crates.record["browser"]["template"]["expect"]["min_items"]
    monkeypatch.setenv("ANYUSE_TIER2_READINESS", "off")
    rungs = runtime_rungs(crates.recs[0], origin="attach")
    assert rungs["browser"]["template"]["readiness"] == {}


def test_the_compiled_request_answers_first_and_nothing_else_runs(crates):
    browser, calls = Browser(crates.doms), []
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured)),
                       browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent(calls))
    assert out["ok"] and out["served_by"] == "api" and out["model_calls"] == 0
    assert [t["rung"] for t in out["rungs_tried"]] == ["api"]
    assert browser.opened == [] and calls == []


def test_a_broken_request_falls_to_the_browser_replay(crates):
    browser, calls = Browser(crates.doms), []
    api = Api(crates.captured, body=CHANGED)
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=api),
                       browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent(calls))
    assert out["ok"] and out["served_by"] == "browser_replay", out
    assert out["result"]["items"][:3] == crates.labels["tokio"][:3]
    assert out["model_calls"] == 0 and calls == []
    assert [(t["rung"], t["ok"]) for t in out["rungs_tried"]] == [("api", False), ("browser_replay", True)]
    assert "recompile" in out["next"]
    # the site still answers through the browser, so the request changed: it is taken out of service
    assert _stored(crates)["health"]["preferred"] is False
    # this driver captures no traffic and no recordings were kept: the recompile says so, loads
    # no further page and counts no attempt
    assert out["recompiled"]["swapped"] is False and "no evidence" in out["recompiled"]["reason"]
    assert "does not capture traffic" in out["recompiled"]["reason"]
    assert browser.opened == ["https://crates.io/search?q=tokio"]
    assert _stored(crates)["health"]["recompile_attempts"] == 0
    # the next call skips the broken request and goes straight to the browser
    api.sent.clear()
    out = run_compiled("crates_search", {"q": "clap"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=api),
                       browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent(calls))
    assert out["ok"] and out["served_by"] == "browser_replay" and api.sent == []
    assert out["rungs_tried"][0]["skipped"] and "out of service" in out["rungs_tried"][0]["why"]
    assert out["recompiled"]["attempted"] is False and "no evidence" in out["recompiled"]["reason"]
    assert browser.opened == ["https://crates.io/search?q=tokio", "https://crates.io/search?q=clap"]


def test_a_server_error_does_not_take_the_request_out_of_service(crates):
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, status=503)),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms)),
                       agent_runner=_agent([]))
    assert out["ok"] and out["served_by"] == "browser_replay"
    assert _stored(crates)["health"].get("preferred", True) is True
    assert "this time" in out["next"]


def test_request_and_browser_broken_falls_to_the_agent(crates):
    calls = []
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms, empty=True)),
                       agent_runner=_agent(calls))
    assert out["ok"] and out["served_by"] == "agent", out
    assert out["result"] == {"items": ["tokio", "tokio-util", "tokio-stream"]}
    assert out["model_calls"] == 2 and out["cost_usd"] == pytest.approx(0.031)
    assert [(t["rung"], t["ok"]) for t in out["rungs_tried"]] == [
        ("api", False), ("browser_replay", False), ("agent", True)]
    task, kw = calls[0]
    assert "{q}" in task and kw["values"] == {"q": "tokio"} and kw["commits_need_human"] is True
    assert kw["site"] == "https://crates.io/"


def test_without_a_browser_or_a_model_it_hands_over_as_before(crates):
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)))
    assert out["ok"] is False and "record the search again" in out["fallback"]
    rungs = {t["rung"]: t for t in out["rungs_tried"]}
    assert rungs["browser_replay"]["skipped"] and "ANYUSE_CDP_ENDPOINT" in rungs["browser_replay"]["why"]
    assert rungs["agent"]["skipped"] and "no model configured" in rungs["agent"]["why"]


def test_a_block_goes_straight_to_the_person(crates):
    browser, calls = Browser(crates.doms), []
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, status=429)),
                       browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent(calls),
                       post_card=False)
    assert out["ok"] is False and out["action_required"] == "human"
    assert browser.opened == [] and calls == []


def test_the_agent_handing_a_step_to_the_person_ends_the_cascade(crates):
    calls = []
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms, empty=True)),
                       agent_runner=_agent(calls, ok=False, outcome="human_required"), post_card=False)
    assert out["ok"] is False and out["action_required"] == "human"
    assert [t["rung"] for t in out["rungs_tried"]] == ["api", "browser_replay", "agent"]


def test_an_old_record_without_lower_rungs_still_runs_and_derives_a_task(crates):
    record = dict(crates.record)
    record.pop("browser"), record.pop("task")
    persist_compiled(crates.storage, "crates_search", record)
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured)))
    assert out["ok"] and out["served_by"] == "api"
    calls = []
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       agent_runner=_agent(calls))
    assert out["ok"] and out["served_by"] == "agent"
    assert out["rungs_tried"][1]["skipped"] and "no browser replay" in out["rungs_tried"][1]["why"]
    assert calls[0][0].startswith("search crates.io for {q}")


def test_meta_and_cli_show_the_serving_rung(crates, monkeypatch):
    from click.testing import CliRunner
    from anyuse import cli as C
    from anyuse.mcpserver import meta_tools as MT
    served = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                          executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                          browser_executor=ReplayExecutor(driver=Browser(crates.doms)),
                          agent_runner=_agent([]))
    meta = MT._web_success(served)
    assert meta["served_by"] == "browser_replay" and meta["rungs_tried"] and "recompile" in meta["next"]
    monkeypatch.setattr(C, "_has_desktop_recording", lambda name: False)
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: served)
    res = CliRunner().invoke(C.cli, ["workflow", "play", "crates_search", "--param", "q=tokio"])
    assert res.exit_code == 0, res.output
    assert "served by browser replay" in res.output
    assert "rungs: api ✗" in res.output and "browser replay ✓" in res.output
    assert "Note:" in res.output and "recompile" in res.output


def test_learn_and_har_style_recordings_get_their_rungs():
    from anyuse.execution.api_compile import runtime_rungs
    rec = {"param": {"name": "q", "value": "serde"}, "final_url": "https://crates.io/search?q=serde",
           "site": "crates.io", "ground_truth_selector": "", "ui": {"items": ["serde"]}}
    rungs = runtime_rungs(rec, task="search this site for {q}", start_url="https://crates.io/",
                          origin="learn")
    assert "browser" not in rungs                      # no derived selector: no browser replay
    assert rungs["task"]["text"] == "search this site for {q}" and rungs["task"]["given"] is True
    rungs = runtime_rungs({"param": {"name": "query", "value": "x"}, "final_url": "",
                           "site": "pkg.example"})
    assert rungs["task"]["text"] == ("search pkg.example for {query} and list the names of the "
                                     "results in page order")


def test_a_request_out_of_service_after_an_outage_comes_back_by_itself_while_the_browser_serves(crates):
    import time
    from anyuse.execution.api_recompile import _at
    now = time.time()
    record = dict(_stored(crates))
    record["health"] = {**(record.get("health") or {}), "preferred": False,
                        "demoted_reason": (f"3 consecutive failures (>= 3); last error: non-2xx status 503; "
                                           f"{CT._DEMOTED_NEXT}"),
                        "last_error": "non-2xx status 503", "demoted_at": _at(now),
                        "probe_after": _at(now + 600)}
    persist_compiled(crates.storage, "crates_search", record)
    api = Api(crates.captured)
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=api),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms)), agent_runner=_agent([]))
    assert out["ok"] and out["served_by"] == "browser_replay" and api.sent == [], out
    assert f"AnyUse tries the compiled request again after {_at(now + 600)}" in out["next"], out["next"]
    assert "record the search again" not in out["next"]


def test_a_request_held_after_the_site_did_not_answer_lets_the_browser_serve(crates):
    browser, down = Browser(crates.doms), Api(crates.captured, status=503)
    for _ in range(3):
        out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                           executor=ApiStepExecutor(transport=down),
                           browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent([]))
        assert out["ok"] and out["served_by"] == "browser_replay", out
    # the second call the site did not answer holds the request: the third sends nothing to it
    assert down.sent == ["tokio", "tokio"], down.sent
    assert "not sent again before" in out["next"] and out["rungs_tried"][0]["skipped"], out
    assert _stored(crates)["health"].get("preferred", True) is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# truthful labels, discovery and result shape (live run on crates.io)
# ---------------------------------------------------------------------------

def _demote(crates):
    record = dict(_stored(crates))
    record["health"] = {**(record.get("health") or {}), "preferred": False, "demoted_reason": "live test"}
    persist_compiled(crates.storage, "crates_search", record)


def test_each_answer_carries_the_tier_of_the_rung_that_served_it(crates):
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms)), agent_runner=_agent([]))
    assert out["served_by"] == "browser_replay" and out["ladder_tier"] == CT.TIER_REPLAY
    assert "zero model calls" in out["ladder_tier_means"] and "money" not in out["ladder_tier_means"]
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms, empty=True)),
                       agent_runner=_agent([]))
    assert out["served_by"] == "agent" and out["ladder_tier"] == CT.TIER_AGENT
    # a record taken out of service with a browser rung is listed at the replay tier
    assert CT.ladder_tier(_stored(crates), CT._template_of(_stored(crates))) == CT.TIER_REPLAY


def test_discovery_says_a_tool_still_answers_through_a_lower_rung(crates, monkeypatch):
    from anyuse.mcpserver import meta_tools as MT
    _demote(crates)
    row = CT.list_compiled(crates.storage, crates.hosts)[0]
    assert row["available_via"] is None and not row["runnable_without_human"]     # no browser configured
    op = MT._web_operation("crates_search", _stored(crates), crates.hosts).row
    assert op["ready"] is False and "demoted" in op["why_not"]
    monkeypatch.setenv("ANYUSE_CDP_ENDPOINT", "http://192.168.70.2:9222")
    row = CT.list_compiled(crates.storage, crates.hosts)[0]
    assert row["available_via"] == "browser_replay" and row["runnable_without_human"]
    assert "served by the browser replay" in row["degraded"]
    op = MT._web_operation("crates_search", _stored(crates), crates.hosts).row
    assert op["ready"] is True and op["served_by_expected"] == "browser_replay" and op["degraded"]
    tools = [t for s in CT.host_tool_sets(crates.storage, crates.hosts) for t in s["tools"]]
    assert [t["workflow"] for t in tools] == ["crates_search"]
    assert "served by the browser replay" in tools[0]["description"]


def test_the_agent_answers_in_the_tools_declared_shape(crates):
    answer = {"results": [{"title": "rand v0.10.3", "url": "https://crates.io/crates/rand"},
                          {"title": "bevy_rand v0.16.0", "url": "https://crates.io/crates/bevy_rand"},
                          {"title": "tinyrand", "url": "https://crates.io/crates/tinyrand"}]}
    out = run_compiled("crates_search", {"q": "rand"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       browser_executor=ReplayExecutor(driver=Browser(crates.doms, empty=True)),
                       agent_runner=lambda task, **kw: {"ok": True, "outcome": "done", "result": answer,
                                                        "model_calls": 2, "cost_usd": 0.03, "steps": 3})
    key = list(CT._template_of(crates.record).extract)[0]
    assert out["served_by"] == "agent" and out["result"] == {key: ["rand", "bevy_rand", "tinyrand"]}
    assert out["agent"]["answer"] == answer                         # the raw answer is kept beside it


def test_without_fallback_only_the_compiled_request_runs(crates):
    browser, calls = Browser(crates.doms), []
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                       executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
                       browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent(calls),
                       cascade=False)
    assert out["ok"] is False and browser.opened == [] and calls == []
    assert [t["why"] for t in out["rungs_tried"][1:]] == ["fall-back turned off"] * 2


def test_a_saved_web_tool_is_called_by_the_name_agent_run_reports_with_the_tasks_input(tmp_path):
    from anyuse.execution import harness_compile as HC
    from anyuse.execution import web_tool_call as WT
    from anyuse.mcpserver import meta_tools as MT
    tool = {"version": 1, "workflow": "site_search", "site": "crates.io",
            "start_url": "https://crates.io/",
            "search": {"mode": "url", "template": "https://crates.io/search?q={q}",
                       "start_url": "https://crates.io/"}, "hosts": ["crates.io"]}
    name = CT.typed_tool_name("crates.io", "site_search")
    rec = {"format": WT.FORMAT, "workflow": name, "workflow_id": "w9", "kind": "web", "site": "crates.io",
           "value": "rand", "param": "q", "tool": tool, "tool_sha256": WT.tool_sha(tool),
           "proof": {"passed": True, "independent": True, "model_calls": 0,
                     "cases": [{"value": "regex", "passed": True}, {"value": "tokio", "passed": True}]},
           "status": "ready", "status_reasons": []}
    d = tmp_path / "workflow_w9"
    d.mkdir()
    (d / HC.WEB_TOOL_FILE).write_text(json.dumps(rec))
    spec = WT.tool_specs(Storage(str(tmp_path)))[0]
    assert spec["name"] == name == "crates_io__site_search"           # no doubled host
    assert spec["input_schema"]["required"] == ["q"] and spec["param"] == "q"
    op = MT._web_tool_operation(spec, True, WT)
    assert op.row["parameters"][0]["name"] == "q"


@pytest.mark.parametrize("agent_ok", [False, True])
def test_audit_agent_escalation_survives_compiled_cascade(crates, monkeypatch, agent_ok):
    recommendation = {"escalate_to_rea": True, "vision_fallbacks": 3}
    def agent(*args, **kwargs):
        return {"ok": agent_ok, "result": ["tokio", "tokio-util"], "model_calls": 0,
                "rea_escalation": recommendation}
    monkeypatch.setattr(CT, "_agent_policy", lambda: "inert")
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
        executor=ApiStepExecutor(transport=Api(crates.captured, body=CHANGED)),
        browser_executor=ReplayExecutor(driver=Browser(crates.doms, empty=True)), agent_runner=agent,
        record_outcome=False)
    assert out["rea_escalation"] == recommendation
    if agent_ok:
        assert out["agent"]["rea_escalation"] == recommendation


def test_audit_auth_terminal_refusal_never_opens_browser(crates, monkeypatch):
    browser, calls = Browser(crates.doms), []
    executor = ApiStepExecutor(transport=Api(crates.captured))
    stop = executor._auth_failure({}, ApiResponse(401, {}, "{}"), "https://crates.io/api/v1/crates",
                                  "crates.io", 1, 2, "inert session")
    monkeypatch.setattr(executor, "execute", lambda *a, **k: stop)
    out = run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage, hosts_path=crates.hosts,
                      executor=executor, browser_executor=ReplayExecutor(driver=browser), agent_runner=_agent(calls))
    assert not out["ok"] and out["requires_human"] and out["auth_required"]
    assert out["outcome_kind"] == "terminal_refusal" and not browser.opened and not calls


@pytest.mark.parametrize("body", [{"item": {"unrelated": 1}}, {"item": {"title": "ok", "score": None}}])
def test_audit_api_fields_require_non_null_extractions(body):
    from anyuse.execution.api_step import RequestTemplate
    template = RequestTemplate(url="https://api.example/item", shape="fields",
        extract={"title": "item.title", "score": "item.score"}, expect={"status_in": [200], "path": "item"})
    transport = SimpleNamespace(send=lambda *a: ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(body)))
    result = ApiStepExecutor(transport=transport).execute(template)
    assert not result["success"] and "fields" in result["error"]
    assert result["outcome_kind"] == "incomplete"
    assert result["reason_code"] == "missing_required_fields"


def test_audit_response_capture_does_not_inherit_final_field_contract():
    from anyuse.execution.api_step import RequestTemplate
    from anyuse.execution.step_repair import capture
    template = RequestTemplate(url="https://api.example/item", shape="fields",
        params=["q"], query={"q": "{q}"},
        extract={"title": "item.title"}, expect={"status_in": [200], "path": "item"})
    document = {"new_shape": ["response is available"]}
    transport = SimpleNamespace(send=lambda *a: ApiResponse(
        200, {"Content-Type": "application/json"}, json.dumps(document)))
    executor = ApiStepExecutor(transport=transport)
    response = capture(template, "fixture", executor)
    assert response.ok and response.data == document
    assert template.shape == "fields" and template.extract == {"title": "item.title"}
    assert not executor.execute(template, {"q": "fixture"})["success"]


@pytest.mark.parametrize("producer_ok", [False, True])
def test_audit_chain_distinguishes_missing_producer_fields_from_final_answer(producer_ok):
    from anyuse.execution.api_step import PlanStep, RequestPlan, RequestTemplate
    sent = []
    def send(method, url, headers, body, timeout_s):
        sent.append(url)
        data = {"title": "producer"} if producer_ok and url.endswith("producer") else {}
        return ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(data))
    steps = [PlanStep(name, RequestTemplate(url="https://api.example/" + name,
             shape="fields", extract={"title": "title"}, expect={"status_in": [200]}))
             for name in ("producer", "consumer")]
    result = ApiStepExecutor(transport=SimpleNamespace(send=send)).execute(
        RequestPlan(steps=steps, shape="fields"))
    assert not result["success"]
    assert result["outcome_kind"] == ("incomplete" if producer_ok else "retryable_failure")
    assert result["failed_step"] == len(sent) == (2 if producer_ok else 1)


@pytest.mark.parametrize("answer,accepted", [({"answer": "absent"}, False),
    ({"title": "good", "score": "wrong type"}, False), ({"title": "good", "score": 2}, True)])
def test_audit_field_schema_applies_to_agent_fallback(tmp_path, monkeypatch, answer, accepted):
    from anyuse.execution.api_step import RequestTemplate
    template = RequestTemplate(url="https://api.example/item", shape="fields",
        extract={"title": "item.title", "score": "item.score"}, expect={"status_in": [200], "path": "item"})
    schema = {"type": "object", "properties": {"title": {"type": "string"}, "score": {"type": "number"}},
              "required": ["title", "score"], "additionalProperties": False}
    record = compiled_record(template, {"passed": True, "passed_count": 2, "case_count": 2,
        "cases": [{"contract": {"output_schema": schema}}]}, workflow="detail", source={"site": "api.example"})
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("api.example", path=hosts)
    executor = ApiStepExecutor(transport=SimpleNamespace(send=lambda *a: pytest.fail("unexpected transport")))
    monkeypatch.setattr(executor, "execute", lambda *a, **k: {"success": False, "outcome_kind": "incomplete", "status": 200})
    monkeypatch.setattr(CT, "_agent_policy", lambda: "inert")
    out = run_compiled("detail", {}, record=record, hosts_path=hosts, executor=executor, record_outcome=False,
                      agent_runner=lambda *a, **k: {"ok": True, "result": answer, "model_calls": 0})
    assert out["ok"] is accepted, out
