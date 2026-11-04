"""Chained requests, recompilation on demotion, the stricter evidence rule and
the robots/acknowledgement courtesy gate.

Entirely offline: transports, storages, recording loaders and the robots fetcher
are all injected.
"""
import json
import os
import shutil
import tempfile

from anyuse.execution.api_compile import (
    CHAIN_PARAM,
    compile_recording,
    find_producer,
    score_candidate,
    visible_items,
    eligible_requests,
)
from anyuse.execution.api_permission import (
    PermissionVerdict,
    acknowledge_host,
    check_permission,
    host_acknowledged,
    robots_allows,
)
from anyuse.execution.api_recompile import (
    ApiHealth,
    RecompileManager,
    RecompilePolicy,
    apply_to_graph,
    record_outcome,
    should_recompile,
)
from anyuse.execution.api_step import (
    ApiResponse,
    ApiStepExecutor,
    PlanStep,
    RequestPlan,
    RequestTemplate,
    api_hil_action,
    load_command,
)
from anyuse.execution.api_verify import MIN_COMPARED, compare, verify_template
from anyuse.execution.hil_gate import StepClass, classify_step
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UITransition

SEARCH = 'https://ex.test/api/search'
DETAIL = 'https://ex.test/api/item/{chain_id}'


class ChainTransport:
    """Serves a two-call chain; any URL can be made to fail."""

    def __init__(self, fail_urls=(), detail_body=None):
        self.fail_urls = set(fail_urls)
        self.sent = []
        self.detail_body = detail_body

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        for bad in self.fail_urls:
            if bad in url:
                return ApiResponse(503, {"Content-Type": "text/html"}, "<h1>down</h1>")
        if "/api/search" in url:
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps({"hits": [{"id": "abc-1", "name": "first hit"},
                                                    {"id": "abc-2", "name": "second hit"}]}))
        return ApiResponse(200, {"Content-Type": "application/json"},
                           self.detail_body or json.dumps(
                               {"item": {"id": "abc-1", "title": "First Hit",
                                         "summary": "a thing", "version": "2.0"}}))


class VaryingChainTransport(ChainTransport):
    """A chain whose DETAIL body depends on the query, as a real one does.

    The recompile fixtures need this since the cross-recording page-chrome
    filter landed: three recordings of three different queries that show the
    SAME three labels are, correctly, treated as a page that does not respond
    to its parameter (see api_compile.page_chrome). A synthetic transport that
    answers identically for every value was encoding exactly that pathology,
    so it could no longer prove anything — which is the right behaviour and the
    wrong fixture.
    """

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        for bad in self.fail_urls:
            if bad in url:
                return ApiResponse(503, {"Content-Type": "text/html"}, "<h1>down</h1>")
        import urllib.parse as _u
        q = _u.parse_qs(_u.urlsplit(url).query).get("q", ["thing"])[0]
        if "/api/search" in url:
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps({"hits": [{"id": f"{q}-1", "name": f"{q} one"},
                                                    {"id": f"{q}-2", "name": f"{q} two"}]}))
        ident = _u.urlsplit(url).path.rsplit("/", 1)[-1]
        stem = ident.rsplit("-", 1)[0]
        return ApiResponse(200, {"Content-Type": "application/json"},
                           self.detail_body or json.dumps(
                               {"item": {"id": ident, "title": f"Hit {stem}",
                                         "summary": f"a thing about {stem}",
                                         "version": f"2.{len(stem)}"}}))


def _varying_fresh():
    """Fresh recordings whose visible list actually changes with the query."""
    return [{"param": {"name": "q", "value": "alpha"},
             "ui": {"items": ["Hit alpha", "a thing about alpha", "2.5"],
                    "fields": {"field_1": "Hit alpha", "field_2": "a thing about alpha",
                               "field_3": "2.5"}},
             "bindings": {CHAIN_PARAM: "alpha-1"}, "network": []},
            {"param": {"name": "q", "value": "beta"},
             "ui": {"items": ["Hit beta", "a thing about beta", "2.4"],
                    "fields": {"field_1": "Hit beta", "field_2": "a thing about beta",
                               "field_3": "2.4"}},
             "bindings": {CHAIN_PARAM: "beta-1"}, "network": []}]


def _plan():
    return RequestPlan(
        steps=[
            PlanStep(name="find",
                     template=RequestTemplate(url=SEARCH, query={"q": "{q}"}, params=["q"],
                                              extract={"items": "hits[*].id"},
                                              expect={"status_in": [200], "path": "hits",
                                                      "min_items": 1}),
                     bind={CHAIN_PARAM: "hits[*].id"}),
            PlanStep(name="open",
                     template=RequestTemplate(url=DETAIL, params=[CHAIN_PARAM],
                                              shape="fields",
                                              extract={"field_1": "item.title",
                                                       "field_2": "item.summary",
                                                       "field_3": "item.version"},
                                              expect={"status_in": [200], "path": "item",
                                                      "non_empty": True})),
        ],
        params=["q"], shape="fields")


# ---------------------------------------------------------------------------
# plan model
# ---------------------------------------------------------------------------

def test_a_plan_round_trips_and_is_recognised_by_load_command():
    plan = _plan()
    back = load_command(plan.to_dict())
    assert isinstance(back, RequestPlan)
    assert back.to_dict() == plan.to_dict()
    assert isinstance(load_command(RequestTemplate(url=SEARCH).to_dict()), RequestTemplate)


def test_a_plan_exposes_the_same_surface_as_a_single_template():
    plan = _plan()
    assert plan.params == ["q"]
    assert plan.shape == "fields"
    assert plan.extract == plan.steps[-1].template.extract
    assert plan.method == "GET"


def test_a_plan_rejects_a_step_that_reads_a_binding_nobody_produced():
    plan = _plan()
    plan.steps[0].bind = {}
    problems = plan.problems()
    assert any(CHAIN_PARAM in p and "no earlier step provides" in p for p in problems)


def test_an_empty_plan_is_a_problem_not_a_silent_success():
    assert RequestPlan().problems() == ["plan has no steps"]


def test_a_plan_over_the_request_budget_is_refused():
    step = PlanStep(name="read", template=RequestTemplate(
        url="https://ex.test/x", extract={"items": "items[*]"}))
    plan = RequestPlan(steps=[step] * 9)
    assert any("cap is 8" in problem for problem in plan.problems())


# ---------------------------------------------------------------------------
# plan execution
# ---------------------------------------------------------------------------

def test_a_chain_binds_the_first_result_and_returns_the_last_step():
    tr = ChainTransport()
    res = ApiStepExecutor(transport=tr).execute(_plan(), {"q": "thing"})
    assert res["success"] is True
    assert res["steps_run"] == 2
    assert [t["name"] for t in res["plan_trace"]] == ["find", "open"]
    assert res["plan_trace"][0]["bound"][CHAIN_PARAM] == "abc-1"
    assert "/api/item/abc-1" in tr.sent[1]
    assert res["extracted"]["field_1"] == "First Hit"


def test_a_chain_fails_at_the_first_broken_step_and_names_it():
    tr = ChainTransport(fail_urls=["/api/search"])
    res = ApiStepExecutor(transport=tr).execute(_plan(), {"q": "thing"})
    assert res["success"] is False and res["failed_step"] == 1
    assert len(tr.sent) == 1, "a chain must not continue past a failed step"


def test_a_chain_whose_second_call_fails_is_a_failure_not_a_partial_success():
    # FAILURE INJECTION: first call fine, second 503 → the whole plan fails, so
    # the ladder falls back to the recorded UI path.
    tr = ChainTransport(fail_urls=["/api/item/"])
    res = ApiStepExecutor(transport=tr).execute(_plan(), {"q": "thing"})
    assert res["success"] is False and res["failed_step"] == 2
    assert "chain step 2" in res["error"]
    assert len(tr.sent) == 2


def test_a_chain_fails_when_the_first_step_produces_nothing_to_bind():
    class Empty(ChainTransport):
        def send(self, method, url, headers, body, timeout_s):
            self.sent.append(url)
            if "/api/search" in url:
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   json.dumps({"hits": [{"other": 1}]}))
            return super().send(method, url, headers, body, timeout_s)

    tr = Empty()
    res = ApiStepExecutor(transport=tr).execute(_plan(), {"q": "thing"})
    assert res["success"] is False
    assert "produced nothing" in res["error"] or res["failed_step"] == 1


def test_chain_preserves_terminal_and_uncertain_step_outcomes():
    class Terminal(ApiStepExecutor):
        def execute(self, template, params=None):
            if isinstance(template, RequestPlan):
                return self.execute_plan(template, params)
            return {"success": False, "blocked": True, "requires_human": True,
                    "outcome_kind": "terminal_refusal", "error": "human required"}

    terminal = Terminal().execute(_plan(), {"q": "x"})
    assert terminal["outcome_kind"] == "terminal_refusal"
    assert terminal["requires_human"] is True
    assert terminal["plan_trace"][0]["outcome_kind"] == "terminal_refusal"

    class Uncertain(Terminal):
        def execute(self, template, params=None):
            if isinstance(template, RequestPlan):
                return self.execute_plan(template, params)
            return {"success": False, "uncertain": True,
                    "outcome_kind": "uncertain_completion", "error": "unknown"}

    uncertain = Uncertain().execute(_plan(), {"q": "x"})
    assert uncertain["outcome_kind"] == "uncertain_completion"
    assert uncertain["uncertain"] is True


def test_a_chain_of_reads_is_read_only_and_a_write_anywhere_is_destructive():
    plan = _plan()
    assert classify_step(api_hil_action(plan, {"q": "x"})) == StepClass.READ_ONLY
    plan.steps[1].template.method = "POST"
    action = api_hil_action(plan, {"q": "x"})
    assert classify_step(action) == StepClass.DESTRUCTIVE
    assert len(action["args"]["chain"]) == 2


def test_a_plan_with_a_write_step_is_refused_before_anything_is_sent():
    plan = _plan()
    plan.steps[1].template.method = "DELETE"
    tr = ChainTransport()
    res = ApiStepExecutor(transport=tr).execute(plan, {"q": "x"})
    assert res["success"] is False and res["requires_approval"] is True
    assert tr.sent == []


# ---------------------------------------------------------------------------
# chain extraction
# ---------------------------------------------------------------------------

def _chain_recording(query="thing", ident="abc-1"):
    stem = ident.rsplit("-", 1)[0]
    search_body = json.dumps({"hits": [{"id": ident, "name": f"{stem} hit"},
                                       {"id": f"{stem}-2", "name": f"{stem} second"}]})
    version = f"2.{len(stem)}"
    detail_body = json.dumps({"item": {"id": ident, "title": f"Hit {stem}",
                                       "summary": f"a useful thing about {stem}",
                                       "version": version}})
    return {
        "param": {"name": "q", "value": query},
        "ui": {"items": [f"Hit {stem}", f"a useful thing about {stem}", version],
               "text": "",
               "fields": {"field_1": f"Hit {stem}",
                          "field_2": f"a useful thing about {stem}",
                          "field_3": version}},
        "network": [
            {"url": SEARCH + f"?q={query}", "method": "GET", "status": 200,
             "mime": "application/json", "bytes": 100, "body": search_body},
            {"url": f"https://ex.test/api/item/{ident}", "method": "GET", "status": 200,
             "mime": "application/json", "bytes": 80, "body": detail_body},
        ],
    }


def _chain_fitting_recording():
    return _chain_recording("another query", "defgh-1")


def _compiled_chain():
    return compile_recording(
        _chain_recording(), chain_recordings=[_chain_fitting_recording()]).template


def test_find_producer_locates_the_response_that_made_the_id():
    rec = _chain_recording()
    items = visible_items(rec)
    scored = [score_candidate(r, items) for r in eligible_requests(rec)]
    consumer = [c for c in scored if "/item/" in c.url][0]
    producer, path = find_producer("abc-1", scored, consumer)
    assert producer is not None and "/api/search" in producer.url
    assert path == "hits[*].id"


def test_compile_emits_a_two_step_plan_when_the_url_carries_a_produced_value():
    res = compile_recording(
        _chain_recording(), chain_recordings=[_chain_fitting_recording()])
    assert res.status == "chained", res.reason
    plan = res.template
    assert isinstance(plan, RequestPlan)
    assert [s.name for s in plan.steps] == ["find", "open"]
    assert plan.steps[0].bind == {CHAIN_PARAM: "hits[*].id"}
    assert plan.steps[1].template.url == "https://ex.test/api/item/{chain_id}"
    assert plan.params == ["q"]
    assert plan.problems() == []


def test_chain_refuses_a_one_recording_derived_binding_inference():
    result = compile_recording(_chain_recording())
    assert result.template is None
    assert "at least 2 distinct recorded inputs" in result.reason


def test_a_recording_with_no_produced_value_still_compiles_to_one_request():
    rec = _chain_recording()
    rec["network"] = rec["network"][:1]
    rec["ui"]["items"] = ["abc hit", "abc second"]
    res = compile_recording(rec)
    assert res.status in ("ok", "ambiguous")
    assert isinstance(res.template, RequestTemplate)


def test_allow_chain_false_forces_the_single_request_behaviour():
    res = compile_recording(_chain_recording(), allow_chain=False)
    assert res.status != "chained"


def test_a_compiled_chain_verifies_on_fresh_values():
    plan = _compiled_chain()
    ex = ApiStepExecutor(transport=ChainTransport())
    fields = {"field_1": "First Hit", "field_2": "a thing", "field_3": "2.0"}
    cases = [{"value": "alpha", "ui_fields": fields,
              "bindings": {CHAIN_PARAM: "abc-1"}},
             {"value": "beta", "ui_fields": fields,
              "bindings": {CHAIN_PARAM: "abc-1"}}]
    res = verify_template(plan, cases, ex)
    assert res.passed, res.reason


def _same_key_crates_recording(query, hit):
    crate = {"id": hit, "name": hit, "description": f"description for {hit}",
             "downloads": 42, "max_version": "1.2.3"}
    return {
        "param": {"name": "q", "value": query},
        "ui": {"items": [crate["description"], str(crate["downloads"]),
                           crate["max_version"]], "text": ""},
        "network": [
            {"url": f"https://crates.test/api/v1/crates?q={query}", "method": "GET",
             "status": 200, "mime": "application/json", "bytes": 100,
             "body": json.dumps({"crates": [crate]})},
            {"url": f"https://crates.test/api/v1/crates/{hit}", "method": "GET",
             "status": 200, "mime": "application/json", "bytes": 80,
             "body": json.dumps({"crate": crate})},
        ],
    }


def test_chain_binding_uses_two_recordings_when_top_hit_equals_the_base_input():
    base = _same_key_crates_recording("figment", "figment")
    fitting = _same_key_crates_recording(
        "async runtime", "pyo3-async-runtimes-macros")
    result = compile_recording(base, chain_recordings=[fitting])
    assert result.status == "chained", result.reason
    assert result.template.steps[0].bind == {CHAIN_PARAM: "crates[*].id"}
    assert result.template.steps[1].template.url.endswith("/crates/{chain_id}")


def test_chain_refuses_when_derived_binding_is_indistinguishable_from_input():
    base = _same_key_crates_recording("figment", "figment")
    fitting = _same_key_crates_recording("anyhow", "anyhow")
    result = compile_recording(base, chain_recordings=[fitting])
    assert result.template is None
    assert "cannot be distinguished from the caller input" in result.reason


# ---------------------------------------------------------------------------
# the stricter evidence rule (item 4a)
# ---------------------------------------------------------------------------

def test_a_single_matching_field_is_no_longer_a_proof():
    res = compare(["one lonely description"], ["one lonely description"], ordered=False)
    assert res.passed is False
    assert "insufficient evidence" in res.reason
    assert MIN_COMPARED == 3


def test_three_matching_fields_still_prove():
    res = compare(["alpha thing", "beta thing", "gamma thing"],
                  ["alpha thing", "beta thing", "gamma thing"], ordered=False)
    assert res.passed is True


def test_the_minimum_is_a_parameter_not_a_hard_wired_rule():
    res = compare(["only one"], ["only one"], ordered=False, min_compared=1)
    assert res.passed is True


# ---------------------------------------------------------------------------
# health, demotion, recompilation (item 3)
# ---------------------------------------------------------------------------

def test_record_outcome_tracks_streaks_and_is_pure():
    h = ApiHealth()
    h2 = record_outcome(h, False, "boom")
    assert h.total_runs == 0, "record_outcome must not mutate its input"
    assert h2.consecutive_failures == 1 and h2.last_error == "boom"
    h3 = record_outcome(record_outcome(h2, False, "boom"), True)
    assert h3.consecutive_failures == 0 and h3.total_runs == 3 and h3.total_failures == 2


def test_should_recompile_on_a_failure_streak_and_on_a_sagging_success_rate():
    policy = RecompilePolicy()
    assert should_recompile(ApiHealth(consecutive_failures=3), policy)[0] is True
    assert should_recompile(ApiHealth(consecutive_failures=2), policy)[0] is False
    assert should_recompile(ApiHealth(total_runs=10, total_failures=8), policy)[0] is True
    assert should_recompile(ApiHealth(total_runs=10, total_failures=1), policy)[0] is False
    # Too few samples for the RATE trigger; a streak is what catches an early death.
    assert should_recompile(ApiHealth(total_runs=2, total_failures=2), policy)[0] is False
    assert should_recompile(ApiHealth(total_runs=2, total_failures=2,
                                      consecutive_failures=2), policy)[0] is False


def test_should_recompile_stops_after_the_attempt_budget_and_when_demoted():
    policy = RecompilePolicy(max_attempts=2)
    h = ApiHealth(consecutive_failures=9, recompile_attempts=2)
    assert should_recompile(h, policy) == (False, "2 recompile attempts already made (max 2)")
    assert should_recompile(ApiHealth(consecutive_failures=9, preferred=False), policy)[0] is False


class _Storage:
    def __init__(self, base):
        self.base = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base, f"workflow_{workflow_id}")


def _manager(tmp, recording, fresh, executor):
    from anyuse.execution.api_compile import compiled_record, persist_compiled
    storage = _Storage(tmp)
    fitting = _chain_fitting_recording()
    tmpl = compile_recording(recording, chain_recordings=[fitting]).template
    persist_compiled(storage, "w1", compiled_record(tmpl, {"passed": True}, workflow="w1"))
    return storage, RecompileManager(storage, lambda _id: (recording, [fitting], fresh), executor,
                                     proof_preflight=lambda _compiled: "")


def test_a_healthy_rung_is_not_recompiled():
    tmp = tempfile.mkdtemp()
    try:
        rec = _chain_recording()
        _s, mgr = _manager(tmp, rec, [], ApiStepExecutor(transport=ChainTransport()))
        mgr.note_outcome("w1", True)
        out = mgr.maybe_recompile("w1")
        assert out.attempted is False and out.reason == "healthy"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_failure_streak_triggers_a_recompile_that_is_kept_when_it_proves():
    tmp = tempfile.mkdtemp()
    try:
        rec = _chain_recording()
        fresh = _varying_fresh()
        _s, mgr = _manager(tmp, rec, fresh,
                           ApiStepExecutor(transport=VaryingChainTransport()))
        for _ in range(3):
            mgr.note_outcome("w1", False, "non-2xx status 503")
        out = mgr.maybe_recompile("w1")
        assert out.attempted is True and out.persisted is True, out.reason
        assert "consecutive failures" in out.reason
        health = ApiHealth.from_dict(mgr.load_record("w1")["health"])
        assert health.preferred is True and health.consecutive_failures == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_recompile_that_cannot_prove_itself_demotes_instead_of_replacing():
    tmp = tempfile.mkdtemp()
    try:
        rec = _chain_recording()
        # The endpoint now returns a different shape, so the fresh-input proof
        # cannot pass: the rung must be demoted, never replaced unproved.
        broken = VaryingChainTransport(detail_body=json.dumps({"other": {"x": 1}}))
        fresh = _varying_fresh()
        _s, mgr = _manager(tmp, rec, fresh, ApiStepExecutor(transport=broken))
        for _ in range(3):
            mgr.note_outcome("w1", False, "postcondition failed")
        out = mgr.maybe_recompile("w1")
        assert out.attempted is True and out.persisted is False and out.demoted is True
        health = ApiHealth.from_dict(mgr.load_record("w1")["health"])
        assert health.preferred is False and health.demoted_reason
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_missing_recording_demotes_rather_than_guessing():
    tmp = tempfile.mkdtemp()
    try:
        from anyuse.execution.api_compile import compiled_record, persist_compiled
        storage = _Storage(tmp)
        tmpl = _compiled_chain()
        persist_compiled(storage, "w1", compiled_record(tmpl, {"passed": True}))

        def missing(_id):
            raise FileNotFoundError("no recording kept for this workflow")

        mgr = RecompileManager(storage, missing, ApiStepExecutor(transport=ChainTransport()))
        for _ in range(3):
            mgr.note_outcome("w1", False, "503")
        out = mgr.maybe_recompile("w1")
        assert out.demoted is True and "no recording available" in out.reason
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_to_graph_removes_a_demoted_rung_so_the_ui_path_is_preferred():
    from anyuse.execution.deterministic import rank_implementations

    graph = StateGraph()
    graph.add_transition(UITransition(from_state="a", to_state="b", intent="go",
                                      implementations=[TransitionImpl(modality="ax",
                                                                      command={"title": "Go"})]))
    tmpl = _compiled_chain()
    record = {"template": tmpl.to_dict(), "health": ApiHealth().to_dict()}
    assert "preferred" in apply_to_graph(graph, "a", "b", "go", record)
    assert rank_implementations(graph.transitions[0])[0].modality == "api"

    record["health"] = ApiHealth(preferred=False, demoted_reason="endpoint changed").to_dict()
    note = apply_to_graph(graph, "a", "b", "go", record)
    assert "removed" in note and "endpoint changed" in note
    assert [i.modality for i in graph.transitions[0].implementations] == ["ax"]


# ---------------------------------------------------------------------------
# robots / acknowledgement courtesy gate (item 4b)
# ---------------------------------------------------------------------------

def _fetcher(text):
    def fetch(url):
        return text
    return fetch


def test_robots_allow_and_disallow_are_both_reported():
    import anyuse.execution.api_permission as perm
    perm._ROBOTS_CACHE.clear()
    allowed, note = robots_allows("https://a.test/api/x",
                                  _fetcher("User-agent: *\nDisallow: /private\n"))
    assert allowed is True and "allows" in note
    perm._ROBOTS_CACHE.clear()
    allowed, note = robots_allows("https://b.test/private/x",
                                  _fetcher("User-agent: *\nDisallow: /private\n"))
    assert allowed is False and "disallows" in note


def test_a_missing_robots_txt_is_unknown_not_a_prohibition():
    import anyuse.execution.api_permission as perm
    tmp = tempfile.mkdtemp()
    saved = os.environ.get("ANYUSE_ROBOTS_CACHE_FILE")
    os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(tmp, "robots.json")
    perm._ROBOTS_CACHE.clear()
    try:
        allowed, note = robots_allows("https://c.test/x", _fetcher(None))
        assert allowed is None and "no robots.txt" in note, note
        # …and the same answer from the on-disk cache, in a fresh "process".
        perm._ROBOTS_CACHE.clear()
        again = robots_allows("https://c.test/x", _fetcher(None))
        assert again == (allowed, note), (again, (allowed, note))
    finally:
        perm._ROBOTS_CACHE.clear()
        if saved is None:
            os.environ.pop("ANYUSE_ROBOTS_CACHE_FILE", None)
        else:
            os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unacknowledged_host_compiles_but_is_not_preferred():
    import anyuse.execution.api_permission as perm
    tmp = tempfile.mkdtemp()
    try:
        perm._ROBOTS_CACHE.clear()
        path = os.path.join(tmp, "hosts.json")
        os.environ["ANYUSE_API_HOSTS_FILE"] = path
        v = check_permission("https://d.test/api/x", hosts_path=path,
                             fetcher=_fetcher("User-agent: *\nAllow: /\n"))
        assert v.may_prefer is False and v.acknowledged is False
        assert v.reason == "host 'd.test' is not acknowledged; run `anyuse workflow acknowledge-host d.test`"

        acknowledge_host("d.test", note="public API", path=path)
        assert host_acknowledged("d.test", path) is True
        v2 = check_permission("https://d.test/api/x", hosts_path=path,
                              fetcher=_fetcher("User-agent: *\nAllow: /\n"))
        assert v2.may_prefer is True
    finally:
        os.environ.pop("ANYUSE_API_HOSTS_FILE", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_robots_disallow_beats_an_acknowledgement():
    import anyuse.execution.api_permission as perm
    tmp = tempfile.mkdtemp()
    try:
        perm._ROBOTS_CACHE.clear()
        path = os.path.join(tmp, "hosts.json")
        acknowledge_host("e.test", path=path)
        v = check_permission("https://e.test/private/x", hosts_path=path,
                             fetcher=_fetcher("User-agent: *\nDisallow: /private\n"))
        assert v.may_prefer is False and v.robots_allowed is False
        assert isinstance(v, PermissionVerdict)
    finally:
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
