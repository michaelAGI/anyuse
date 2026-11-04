"""Which result row a compiled search -> open chain binds.

A recording that opened a later result whose name is what was typed compiles
to "open the result with that name"; recordings that all opened the first
result keep "open the first result"; a recording neither rule repeats refuses
the chain. Entirely offline: the transport and the storage are injected.
"""
import json
import os
import shutil
import tempfile
import urllib.parse

from anyuse.execution.api_compile import CHAIN_PARAM, compile_recording
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import (
    PICK_BY_NAME,
    PICK_NONE_NAMED,
    ApiResponse,
    ApiStepExecutor,
    PlanStep,
    RequestPlan,
    RequestTemplate,
    load_command,
    pick_row,
)
from anyuse.execution.api_verify import compile_and_verify

ORIGIN = "https://registry.test"

#: popularity-ranked search results per query
RESULTS = {
    "serde": ["serde_json", "serde", "serde_derive"],
    "tokio": ["tokio", "tokio-util"],
    "log": ["env_logger", "log", "log4rs"],
    "rand": ["rand", "rand_core"],
    "regex": ["regex-syntax", "fancy-regex", "regex"],
    "clap": ["clap", "clap_builder"],
    "async runtime": ["tokio", "async-std", "smol"],
    "web framework": ["axum", "actix-web"],
    "nothing here": ["foo-bar", "baz"],
}
NAMES = sorted({name for rows in RESULTS.values() for name in rows})
IDS = {name: f"p{100 + i}" for i, name in enumerate(NAMES)}
BY_ID = {ident: name for name, ident in IDS.items()}


def search_body(q):
    return {"crates": [{"id": IDS[n], "name": n} for n in RESULTS[q]]}


def detail_body(name):
    ident = IDS[name]
    return {"item": {"id": ident, "title": f"{name} package",
                     "summary": f"what {name} does",
                     "version": f"1.{len(name)}.{int(ident[1:]) % 7}"}}


class Registry:
    """Serves the search and the detail endpoints of the fixture registry."""

    def __init__(self):
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/api/search":
            q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps(search_body(q)))
        ident = parts.path.rsplit("/", 1)[-1]
        return ApiResponse(200, {"Content-Type": "application/json"},
                           json.dumps(detail_body(BY_ID[ident])))


def recording(q, opened):
    """A recording of searching ``q`` and opening the result ``opened``."""
    item = detail_body(opened)["item"]
    return {
        "param": {"name": "q", "value": q},
        "ui": {"items": [item["title"], item["summary"], item["version"]], "text": "",
               "fields": {"field_1": item["title"], "field_2": item["summary"],
                          "field_3": item["version"]}},
        "bindings": {CHAIN_PARAM: IDS[opened]},
        "network": [
            {"url": f"{ORIGIN}/api/search?q={urllib.parse.quote(q)}", "method": "GET",
             "status": 200, "mime": "application/json", "bytes": 100,
             "body": json.dumps(search_body(q))},
            {"url": f"{ORIGIN}/api/package/{IDS[opened]}", "method": "GET", "status": 200,
             "mime": "application/json", "bytes": 80, "body": json.dumps(detail_body(opened))},
        ],
    }


def compiled(base, *fitting):
    return compile_recording(base, chain_recordings=[base, *fitting])


def opened_for(plan, q):
    tr = Registry()
    res = ApiStepExecutor(transport=tr).execute(plan, {"q": q})
    bound = ((res.get("plan_trace") or [{}])[0].get("bound") or {}).get(CHAIN_PARAM)
    return res, BY_ID.get(bound), tr


# ---------------------------------------------------------------------------
# compile: which rule the recordings support
# ---------------------------------------------------------------------------

def test_a_recording_that_opened_a_later_result_of_that_name_compiles_a_name_rule():
    res = compiled(recording("serde", "serde"), recording("tokio", "tokio"))
    assert res.status == "chained", res.reason
    assert res.template.steps[0].bind == {CHAIN_PARAM: "crates[*].id"}
    assert res.template.steps[0].pick == {CHAIN_PARAM: {
        "by": PICK_BY_NAME, "input": "q", "name_path": "name", "else_first": False}}
    assert "the row whose name is the input" in res.template.notes
    assert res.template.problems() == []


def test_the_name_rule_opens_the_named_result_not_the_most_popular_one():
    plan = compiled(recording("serde", "serde"), recording("tokio", "tokio")).template
    for q in ("serde", "log", "regex", "tokio", "rand"):
        res, opened, _tr = opened_for(plan, q)
        assert res["success"] is True, res.get("error")
        assert opened == q, (q, opened)
        assert res["plan_trace"][0]["picked"][CHAIN_PARAM]["rule"] == "exact"


def test_the_name_rule_refuses_when_no_result_has_that_name():
    plan = compiled(recording("serde", "serde"), recording("tokio", "tokio")).template
    res, opened, tr = opened_for(plan, "nothing here")
    assert res["success"] is False and opened is None
    assert res["outcome_kind"] == "refused" and res["reason_code"] == "not_found"
    assert "no result is named 'nothing here'" in res["error"]
    assert len(tr.sent) == 1, "the item request is never sent"


class Reshaped(Registry):
    """The registry after a change to its search response (``body(q)``)."""

    def __init__(self, body):
        super().__init__()
        self.body = body

    def send(self, method, url, headers, body, timeout_s):
        parts = urllib.parse.urlsplit(url)
        if parts.path != "/api/search":
            return super().send(method, url, headers, body, timeout_s)
        self.sent.append(url)
        q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
        return ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(self.body(q)))


def _renamed(q):
    return {"crates": [{"id": IDS[n], "title": n} for n in RESULTS[q]]}


def _moved(q):
    return {"results": search_body(q)["crates"]}


def test_search_rows_without_their_name_are_a_changed_response_not_not_found():
    by_name = compiled(recording("serde", "serde"), recording("tokio", "tokio")).template
    else_first = compiled(recording("serde", "serde"),
                          recording("async runtime", "tokio")).template
    assert else_first.steps[0].pick[CHAIN_PARAM]["else_first"] is True
    for plan in (by_name, else_first):
        for body in (_renamed, _moved):
            tr = Reshaped(body)
            res = ApiStepExecutor(transport=tr).execute(plan, {"q": "serde"})
            assert res["success"] is False, (body.__name__, res.get("plan_trace"))
            assert res.get("reason_code") != "not_found", (body.__name__, res)
            assert res["outcome_kind"] == "retryable_failure", (body.__name__, res)
            assert len(tr.sent) == 1, "no result is opened"
    from anyuse.mcpserver.compiled_tools import run_compiled
    tmp = tempfile.mkdtemp()
    try:
        storage, hosts = _tool(tmp, by_name)
        out = run_compiled("pkg", {"q": "serde"}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=Reshaped(_renamed)), cascade=False)
        assert out["ok"] is False and not out.get("not_found"), out
        assert "no result is named" not in out["error"], out["error"]
        assert int(_health(tmp).get("consecutive_failures") or 0) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_first_row_is_kept_where_a_recording_showed_it_with_no_such_name():
    plan = compiled(recording("serde", "serde"),
                    recording("async runtime", "tokio")).template
    assert plan.steps[0].pick[CHAIN_PARAM]["else_first"] is True
    res, opened, _tr = opened_for(plan, "web framework")
    assert res["success"] is True and opened == "axum"
    assert res["plan_trace"][0]["picked"][CHAIN_PARAM]["rule"] == "first"
    assert opened_for(plan, "log")[1] == "log"


def test_recordings_that_all_opened_the_first_result_keep_the_first_entry_binding():
    plan = compiled(recording("tokio", "tokio"), recording("rand", "rand")).template
    assert plan.steps[0].pick == {}
    assert "pick" not in plan.steps[0].to_dict()
    assert "(first entry)" in plan.notes
    assert opened_for(plan, "serde")[1] == "serde_json"


def test_a_later_result_that_does_not_carry_the_typed_name_refuses_the_chain():
    res = compiled(recording("serde", "serde"), recording("tokio", "tokio-util"))
    assert res.template is None
    assert "opened a result other than the first" in res.reason
    assert "open the result whose name is what you typed" in res.reason


# ---------------------------------------------------------------------------
# proof: the rule is checked per fresh input and recorded
# ---------------------------------------------------------------------------

def test_the_proof_checks_and_records_the_row_rule_on_fresh_inputs():
    compiled_res, proof = compile_and_verify(
        recording("serde", "serde"), [recording("log", "log"), recording("regex", "regex")],
        executor=ApiStepExecutor(transport=Registry()),
        fitting_recordings=[recording("tokio", "tokio")])
    assert proof.passed, proof.reason
    assert "result row chosen by name" in proof.reason
    rules = [case.contract["row_rules"][CHAIN_PARAM] for case in proof.cases]
    assert rules == [{"row": 1, "rule": "exact"}, {"row": 2, "rule": "exact"}]
    assert compiled_res.template.steps[0].pick[CHAIN_PARAM]["name_path"] == "name"


# ---------------------------------------------------------------------------
# the rule itself, the plan model and the MCP surface
# ---------------------------------------------------------------------------

RULE = {"by": PICK_BY_NAME, "input": "q", "name_path": "name", "else_first": False}


def test_an_exact_name_wins_over_a_normalised_one_and_a_normalised_one_over_none():
    data = {"rows": [{"id": 1, "name": "Serde"}, {"id": 2, "name": "serde"},
                     {"id": 3, "name": "Serde JSON"}]}
    assert pick_row(data, "rows[*].id", RULE, "serde") == (2, 1, "exact")
    assert pick_row(data, "rows[*].id", RULE, "SERDE") == (1, 0, "normalised")
    assert pick_row(data, "rows[*].id", RULE, "serde-json") == (3, 2, "normalised")
    assert pick_row(data, "rows[*].id", RULE, "toml") == (None, -1, PICK_NONE_NAMED)
    assert pick_row(data, "rows[*].id", {**RULE, "else_first": True}, "toml") == (1, 0, "first")
    # rows without the name field, or no list: nothing to choose by, even with else_first
    unnamed = {"rows": [{"id": 1, "title": "Serde"}, {"id": 2, "title": "serde"}]}
    for rule in (RULE, {**RULE, "else_first": True}):
        assert pick_row(unnamed, "rows[*].id", rule, "serde") == (None, -1, "")
        assert pick_row({"other": []}, "rows[*].id", rule, "serde") == (None, -1, "")
    keys = {"ids": ["serde_json", "serde"]}
    assert pick_row(keys, "ids[*]", {**RULE, "name_path": ""}, "serde") == ("serde", 1, "exact")


def _named_plan(rule=None):
    return RequestPlan(steps=[
        PlanStep("find", RequestTemplate(
            url=ORIGIN + "/api/search", query={"q": "{q}"}, params=["q"],
            extract={"items": "crates[*].id"},
            expect={"status_in": [200], "path": "crates", "min_items": 1}),
            {CHAIN_PARAM: "crates[*].id"}, {CHAIN_PARAM: dict(rule or RULE)}),
        PlanStep("open", RequestTemplate(
            url=ORIGIN + "/api/package/{chain_id}", params=[CHAIN_PARAM], shape="fields",
            extract={"field_1": "item.title"},
            expect={"status_in": [200], "path": "item", "non_empty": True})),
    ], params=["q"], shape="fields")


def test_a_plan_with_a_row_rule_round_trips_and_shows_it():
    plan = _named_plan()
    back = load_command(plan.to_dict())
    assert back.to_dict() == plan.to_dict()
    assert back.steps[0].pick == {CHAIN_PARAM: RULE}
    assert back.problems() == []
    assert plan.expression() == "open(chain_id=find(q).items[name=q])"
    binding = plan.dataflow_contract()["steps"][0]["bindings"][0]
    assert binding["row"] == RULE


def test_a_malformed_row_rule_is_a_plan_problem():
    assert any("unknown" in p for p in _named_plan({**RULE, "by": "guess"}).problems())
    assert any("not an input" in p for p in _named_plan({**RULE, "input": "x"}).problems())
    plan = _named_plan()
    plan.steps[0].pick = {"other": dict(RULE)}
    assert any("no binding" in p for p in plan.problems())
    plan = _named_plan()
    plan.steps[0].bind = {CHAIN_PARAM: "crates[*].versions[*].id"}
    assert any("needs a path into one list" in p for p in plan.problems())
    assert any("from inside a list" in p
               for p in _named_plan({**RULE, "name_path": "names[0]"}).problems())
    assert _named_plan().problems() == []


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


def _tool(tmp, template):
    """A proved tool for ``template`` in a temp store, its host acknowledged."""
    from anyuse.execution.api_compile import compiled_record, persist_compiled
    storage = Storage(tmp)
    persist_compiled(storage, "pkg", compiled_record(
        template, {"passed": True, "passed_count": 2, "case_count": 2, "status": "proved"},
        workflow="pkg"))
    hosts = os.path.join(tmp, "hosts.json")
    acknowledge_host("registry.test", path=hosts)
    return storage, hosts


def _health(tmp):
    with open(os.path.join(tmp, "workflow_pkg", "compiled_api.json")) as fh:
        return json.load(fh).get("health") or {}


def test_a_not_found_answer_is_not_counted_against_the_tool_or_sent_down_the_ladder():
    from anyuse.mcpserver.compiled_tools import run_compiled

    class NotFound(ApiStepExecutor):
        def execute(self, template, params=None):
            return {"success": False, "refused": True, "outcome_kind": "refused",
                    "reason_code": "not_found", "method": "api", "modality": "api",
                    "error": "no result is named 'x'"}

    tmp = tempfile.mkdtemp()
    try:
        storage, hosts = _tool(tmp, RequestTemplate(
            url=ORIGIN + "/api/search", query={"q": "{q}"}, params=["q"],
            extract={"items": "crates[*].id"},
            expect={"status_in": [200], "path": "crates", "min_items": 1}))
        calls = []
        out = run_compiled("pkg", {"q": "x"}, storage=storage, executor=NotFound(),
                           hosts_path=hosts, browser_executor=object(),
                           agent_runner=lambda *a, **k: calls.append(a) or {})
        assert out["ok"] is False and out["not_found"] is True, out
        assert "no result with that name" in out["fallback"]
        assert [t["rung"] for t in out["rungs_tried"]] == ["api"]
        assert calls == []
        assert int(_health(tmp).get("consecutive_failures") or 0) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_named_chain_tool_answers_and_refuses_through_the_mcp_surface():
    from anyuse.mcpserver.compiled_tools import run_compiled
    tmp = tempfile.mkdtemp()
    try:
        storage, hosts = _tool(tmp, _named_plan())
        out = run_compiled("pkg", {"q": "nothing here"}, storage=storage,
                           executor=ApiStepExecutor(transport=Registry()), hosts_path=hosts)
        assert out["ok"] is False and out["not_found"] is True, out
        found = run_compiled("pkg", {"q": "log"}, storage=storage,
                             executor=ApiStepExecutor(transport=Registry()), hosts_path=hosts)
        assert found["ok"] is True, found.get("error")
        assert found["result"]["field_1"] == "log package"
        assert found["provenance"]["chain"][0]["picked"][CHAIN_PARAM]["rule"] == "exact"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_call_operation_says_to_ask_again_not_to_record_again_on_not_found():
    from anyuse.mcpserver import meta_tools
    from anyuse.mcpserver.compiled_tools import NOT_FOUND_NEXT, run_compiled
    tmp = tempfile.mkdtemp()
    try:
        storage, hosts = _tool(tmp, _named_plan())
        payload = run_compiled("pkg", {"q": "nothing here"}, storage=storage,
                               executor=ApiStepExecutor(transport=Registry()), hosts_path=hosts)
        out = meta_tools._web_failure(payload, "registry.test", "pkg")
        assert out["ok"] is False and out["class"] == meta_tools.INPUT, out
        assert out["next"] == payload["fallback"] == NOT_FOUND_NEXT, out
        assert out["detail"] == {"not_found": True}, out
        assert "record" not in out["next"] and "workflow play" not in out["next"], out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_watched_chain_reads_not_found_as_no_results_not_as_drift():
    from anyuse.execution import watch as W
    from anyuse.mcpserver.compiled_tools import run_compiled
    tmp = tempfile.mkdtemp()
    try:
        storage, hosts = _tool(tmp, _named_plan())
        payload = run_compiled("pkg", {"q": "nothing here"}, storage=storage, hosts_path=hosts,
                               executor=ApiStepExecutor(transport=Registry()),
                               cascade=False, record_outcome=False, post_card=False)
        rec = W.api_run_record(payload)
        assert rec["status"] == "no_results", rec
        item = {"id": "i1", "tool_name": "pkg", "tool_sha256": "0" * 64,
                "input": {"q": "nothing here"}, "workflow": W.API_WORKFLOW}
        snap = W.api_snapshot_of(item, {}, rec, W.utcnow())
        assert snap["good"] and snap["content"] == {"no_results": True}, snap
        assert snap["drift"] is None, snap["drift"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_routine_step_does_not_retry_a_not_found_answer():
    from anyuse.execution.routine import CompiledToolRunner
    from anyuse.mcpserver.compiled_tools import run_compiled
    tmp = tempfile.mkdtemp()
    try:
        storage, hosts = _tool(tmp, _named_plan())
        pid = run_compiled("pkg", {"q": "log"}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=Registry()))["provenance"]["proof_id"]
        runner = CompiledToolRunner(storage=storage, hosts_path=hosts,
                                    executor=ApiStepExecutor(transport=Registry()))
        step = {"action": {"kind": "compiled_tool", "ref": "pkg", "proof_id": pid}}
        res = runner.execute(step, {"q": "nothing here"}, {})
        assert res["ok"] is False and res["transient"] is False, res
        assert "no result is named" in res["error"], res
        assert runner.execute(step, {"q": "log"}, {})["ok"] is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_workflow_play_stops_on_not_found_instead_of_replaying_the_ui_path():
    from click.testing import CliRunner

    import anyuse.core.agent as A
    from anyuse.cli import cli
    from anyuse.memory.models import Workflow
    from anyuse.memory.storage import WorkflowStorage
    from anyuse.mcpserver import compiled_tools as CT

    tmp = tempfile.mkdtemp()
    saved = {key: os.environ.get(key) for key in ("ANYUSE_DATA_DIR",)}
    real_run, real_agent = CT.run_compiled, A.Agent
    replays = []

    class Replays:
        def __init__(self, **_kw):
            replays.append("constructed")

        def play(self, name):
            replays.append(name)

    try:
        os.environ["ANYUSE_DATA_DIR"] = tmp
        WorkflowStorage().save(Workflow(id="d1d1", name="pkg"))
        CT.run_compiled = lambda name, bound, **kw: {
            "ok": False, "workflow": name, "served_by": "none", "not_found": True,
            "error": "no result is named 'x'", "fallback": CT.NOT_FOUND_NEXT, "rungs_tried": []}
        A.Agent = Replays
        res = CliRunner().invoke(cli, ["workflow", "play", "pkg", "--param", "q=x"])
        assert res.exit_code == 1, res.output
        assert f"Next: {CT.NOT_FOUND_NEXT}." in res.output, res.output
        assert "continuing with the recorded UI path" not in res.output
        assert replays == []
    finally:
        CT.run_compiled, A.Agent = real_run, real_agent
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


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
