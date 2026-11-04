"""The MCP surface for compiled workflows.

Offline: a fake transport, a temp workflow directory and a temp host-ack file.
Nothing here touches the desktop, the network, the keychain or the real
``~/.anyuse``.
"""
import json
import os
import shutil
import tempfile

from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_recompile import ApiHealth
from anyuse.execution.api_step import (ApiResponse, ApiStepExecutor, PlanStep,
                                       RequestPlan, RequestTemplate)
from anyuse.mcpserver import compiled_tools
from anyuse.mcpserver.compiled_tools import (
    RUNNABLE_CLASS,
    describe_compiled,
    list_compiled,
    proof_id,
    risk_class,
    run_compiled,
)

BODY = '{"crates":[{"id":"serde"},{"id":"serde_json"},{"id":"serde_yaml"}]}'


class FakeTransport:
    def __init__(self, status=200, text=BODY, ctype="application/json", raises=None):
        self.status, self.text, self.ctype, self.raises = status, text, ctype, raises
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        if self.raises:
            raise self.raises
        return ApiResponse(self.status, {"Content-Type": self.ctype}, self.text)


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


def _template(method="GET"):
    return RequestTemplate(
        method=method, url="https://crates.io/api/v1/crates", query={"q": "{q}"},
        headers={"accept": "application/json"}, extract={"items": "crates[*].id"},
        expect={"status_in": [200], "content_type": "json", "path": "crates",
                "min_items": 1, "required_keys": ["id"]},
        params=["q"])


def _fixture(tmp, name="crates_search", passed=True, health=None, method="GET"):
    storage = Storage(tmp)
    proof = {"passed": passed, "passed_count": 2 if passed else 0, "case_count": 2,
             "reason": "passed on 2 fresh value(s) not present in the recording"}
    record = compiled_record(_template(method), proof, workflow=name,
                             source={"recording": f"{name}.rec.json"})
    if health is not None:
        record["health"] = health.to_dict()
    persist_compiled(storage, name, record)
    return storage, record


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def test_listing_reports_parameters_proof_risk_and_host_state():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        rows = list_compiled(storage, hosts)
        assert len(rows) == 1
        row = rows[0]
        assert row["workflow"] == "crates_search"
        assert row["parameters"] == ["q"]
        assert row["kind"] == "request"
        assert row["host"] == "crates.io"
        assert row["host_acknowledged"] is False
        assert row["risk_class"] == RUNNABLE_CLASS
        assert row["runnable_without_human"] is False
        assert row["proof"]["passed"] is True and row["proof"]["fresh_cases_passed"] == 2
        assert row["proof"]["what_was_checked"]
        assert len(row["proof"]["id"]) == 12
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_write_template_is_listed_but_not_runnable_without_a_human():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp, method="POST")
        row = list_compiled(storage, os.path.join(tmp, "hosts.json"))[0]
        assert row["risk_class"] != RUNNABLE_CLASS
        assert row["runnable_without_human"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_risk_class_comes_from_the_existing_hil_gate():
    from anyuse.execution.hil_gate import StepClass
    assert risk_class(_template("GET")) == StepClass.READ_ONLY
    assert risk_class(_template("DELETE")) == StepClass.DESTRUCTIVE


def test_risk_class_uses_the_complete_bound_graphql_request():
    from anyuse.execution.hil_gate import StepClass
    tmpl = RequestTemplate(
        method="POST", url="https://crates.io/graphql",
        headers={"content-type": "application/json"},
        body='{"query":"{operation}"}', params=["operation"], readonly_post=True)
    assert risk_class(tmpl, {"operation": "query Search { crates { id } }"}) \
        == StepClass.READ_ONLY
    assert risk_class(tmpl, {"operation": "mutation Delete { deleteEverything }"}) \
        == StepClass.DESTRUCTIVE


def test_proof_id_changes_when_the_template_or_the_proof_changes():
    a = compiled_record(_template(), {"passed": True})
    b = compiled_record(_template(), {"passed": True})
    assert proof_id(a) == proof_id(b), "the id must not depend on the timestamp"
    c = compiled_record(_template(), {"passed": False})
    assert proof_id(c) != proof_id(a)
    t = _template()
    t.query = {"q": "{q}", "page": "2"}
    assert proof_id(compiled_record(t, {"passed": True})) != proof_id(a)


def test_an_unreadable_record_is_skipped_not_fatal():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        bad = os.path.join(tmp, "workflow_broken")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "compiled_api.json"), "w") as fh:
            fh.write("{not json")
        assert len(list_compiled(storage, os.path.join(tmp, "hosts.json"))) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def test_running_a_proved_read_only_workflow_returns_values_and_provenance():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        tr = FakeTransport()
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=tr), hosts_path=hosts)
        assert res["ok"] is True, res
        assert res["served_by"] == "api"
        assert res["result"]["items"] == ["serde", "serde_json", "serde_yaml"]
        assert res["llm_calls"] == 0
        prov = res["provenance"]
        assert prov["host"] == "crates.io" and prov["kind"] == "request"
        assert prov["rungs_attempted"] == ["api"] and len(prov["proof_id"]) == 12
        assert isinstance(res["seconds"], float)
        assert tr.sent and tr.sent[0].endswith("q=serde")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unacknowledged_host_is_refused_with_the_command_to_fix_it():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport()),
                           hosts_path=os.path.join(tmp, "hosts.json"))
        assert res["ok"] is False and "acknowledge-host" in res["error"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_chain_requires_acknowledgement_for_every_host_before_sending():
    tmp = tempfile.mkdtemp()
    try:
        storage = Storage(tmp)
        first = _template()
        second = _template()
        second.url = "https://related.example/api/v1/crates"
        second.query = {}
        second.params = []
        plan = RequestPlan(steps=[PlanStep("search", first), PlanStep("detail", second)],
                           params=["q"], shape="list")
        persist_compiled(storage, "chain", compiled_record(
            plan, {"passed": True, "passed_count": 2, "case_count": 2},
            workflow="chain"))
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        tr = FakeTransport()
        out = run_compiled("chain", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=tr), hosts_path=hosts)
        assert out["ok"] is False
        assert out["unacknowledged_hosts"] == ["related.example"]
        assert tr.sent == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_write_workflow_is_refused_by_the_hil_classification():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp, method="POST")
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        tr = FakeTransport()
        res = run_compiled("crates_search", {"q": "x"}, storage=storage,
                           executor=ApiStepExecutor(transport=tr), hosts_path=hosts)
        assert res["ok"] is False and "read_only" in res["error"]
        assert tr.sent == [], "a non-read workflow must never reach the transport"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unproved_or_demoted_workflow_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp, name="unproved", passed=False)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        res = run_compiled("unproved", {"q": "x"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport()),
                           hosts_path=hosts)
        assert res["ok"] is False and "never passed the fresh-input proof" in res["error"]

        storage2, _ = _fixture(tempfile.mkdtemp(), name="demoted",
                               health=ApiHealth(preferred=False,
                                                demoted_reason="endpoint changed"))
        res2 = run_compiled("demoted", {"q": "x"}, storage=storage2,
                            executor=ApiStepExecutor(transport=FakeTransport()),
                            hosts_path=hosts)
        assert res2["ok"] is False and "demoted" in res2["error"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_missing_parameter_is_refused_before_anything_is_sent():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        tr = FakeTransport()
        res = run_compiled("crates_search", {}, storage=storage,
                           executor=ApiStepExecutor(transport=tr), hosts_path=hosts)
        assert res["ok"] is False and "missing parameter" in res["error"]
        assert tr.sent == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_failing_api_rung_reports_the_fallback_and_never_drives_the_desktop():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(
                               transport=FakeTransport(status=503, text="down")),
                           hosts_path=hosts)
        assert res["ok"] is False
        assert "503" in res["error"]
        # a server error says nothing about the request: try again later, never record again or click
        assert res["has_recorded_path"] is False and res.get("transient") is True
        assert "did not answer" in res["fallback"] and "again later" in res["fallback"], res["fallback"]
        assert "record the search again" not in res["fallback"]
        assert res["provenance"]["rungs_attempted"] == ["api"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_refused_connection_is_a_site_that_did_not_answer():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport(
                               raises=ConnectionRefusedError(61, "Connection refused"))),
                           hosts_path=hosts, record_outcome=False)
        assert res["ok"] is False and res.get("transient") is True, res
        assert "did not answer" in res["fallback"] and "record the search again" not in res["fallback"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_changed_response_is_the_failure_that_says_record_again():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport(text='{"packages": []}')),
                           hosts_path=hosts, record_outcome=False)
        assert res["ok"] is False and "transient" not in res and "policy_refused" not in res, res
        assert "record the search again" in res["fallback"] and "did not answer" not in res["fallback"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_destination_the_send_policy_refuses_says_nothing_was_sent():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        # the real transport raises this before anything is sent; the executor reports it as a transport error
        refused = PermissionError("pre-send policy refused request: nonpublic destination '10.0.0.7' is refused "
                                  "by default")
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport(raises=refused)),
                           hosts_path=hosts, record_outcome=False)
        assert res["ok"] is False and res.get("policy_refused") is True and "transient" not in res, res
        assert "nonpublic destination" in res["error"]
        assert "nothing was sent" in res["fallback"], res["fallback"]
        assert "record the search again" not in res["fallback"] and "did not answer" not in res["fallback"]
        # a name that does not resolve is a site that did not answer
        unresolved = PermissionError("pre-send policy refused request: destination address resolution failed "
                                     "closed: gaierror: [Errno 8] nodename nor servname provided, or not known")
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport(raises=unresolved)),
                           hosts_path=hosts, record_outcome=False)
        assert res.get("transient") is True and "did not answer" in res["fallback"], res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_redirect_the_send_policy_refuses_is_a_site_that_changed():
    from anyuse.execution.api_step import HttpxTransport

    class Moved:
        """The site answers every request with a redirect to another origin."""

        def __init__(self):
            self.urls = []

            class Cookies:
                def clear(self):
                    pass
            self.cookies = Cookies()

        def request(self, method, url, **kwargs):
            self.urls.append(url)

            class Response:
                status_code, headers, text = 301, {"location": "https://1.1.1.1/moved"}, ""
            return Response()

    tmp = tempfile.mkdtemp()
    try:
        storage = Storage(tmp)
        tmpl = _template()
        # a literal public address: the first hop's check needs no name lookup
        tmpl.url = "https://93.184.216.34/api/v1/crates"
        persist_compiled(storage, "crates_search", compiled_record(tmpl, {
            "passed": True, "passed_count": 2, "case_count": 2, "reason": "passed"}, workflow="crates_search"))
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("93.184.216.34", path=hosts)
        transport = HttpxTransport()
        transport._client = Moved()
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=transport), hosts_path=hosts)
        # the request was sent and the site pointed somewhere else: not "nothing was sent", not a site that is down
        assert len(transport._client.urls) == 1 and "refused a redirect" in res["error"], res
        assert res["ok"] is False and "policy_refused" not in res and "transient" not in res, res
        assert "record the search again" in res["fallback"] and "nothing was sent" not in res["fallback"]
        # and it counts against the compiled request
        with open(os.path.join(tmp, "workflow_crates_search", "compiled_api.json")) as fh:
            health = ApiHealth.from_dict(json.load(fh).get("health"))
        assert health.consecutive_failures == 1 and health.total_runs == 1, health
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_upper_case_host_in_the_tool_url_runs_once_its_host_is_acknowledged():
    from anyuse.execution.api_permission import host_key
    tmp = tempfile.mkdtemp()
    try:
        storage = Storage(tmp)
        tmpl = _template()
        tmpl.url = "https://Crates.IO/api/v1/crates"
        persist_compiled(storage, "crates_search", compiled_record(tmpl, {
            "passed": True, "passed_count": 2, "case_count": 2, "reason": "passed"}, workflow="crates_search"))
        hosts = os.path.join(tmp, "hosts.json")
        # what `anyuse workflow acknowledge-host Crates.IO` stores
        acknowledge_host(host_key("Crates.IO"), path=hosts)
        assert list_compiled(storage, hosts)[0]["host_acknowledged"] is True
        tr = FakeTransport()
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=tr), hosts_path=hosts, record_outcome=False)
        assert res["ok"] is True, res
        assert res["result"]["items"] == ["serde", "serde_json", "serde_yaml"] and len(tr.sent) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unknown_workflow_says_so():
    tmp = tempfile.mkdtemp()
    try:
        res = run_compiled("nope", {}, storage=Storage(tmp),
                           hosts_path=os.path.join(tmp, "hosts.json"))
        assert res["ok"] is False and "no compiled workflow" in res["error"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_running_records_the_outcome_so_demotion_can_notice():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        for _ in range(2):
            run_compiled("crates_search", {"q": "serde"}, storage=storage,
                         executor=ApiStepExecutor(
                             transport=FakeTransport(text='{"packages": []}')),
                         hosts_path=hosts)
        with open(os.path.join(tmp, "workflow_crates_search", "compiled_api.json")) as fh:
            health = ApiHealth.from_dict(json.load(fh)["health"])
        assert health.consecutive_failures == 2 and health.total_failures == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_three_failures_in_a_row_take_the_rung_out_of_service():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        # answers that fail the request's checks; a site that does not answer never counts
        broken = FakeTransport(text='{"packages": []}')
        for _ in range(3):
            run_compiled("crates_search", {"q": "serde"}, storage=storage,
                         executor=ApiStepExecutor(transport=broken), hosts_path=hosts)
        with open(os.path.join(tmp, "workflow_crates_search", "compiled_api.json")) as fh:
            health = ApiHealth.from_dict(json.load(fh)["health"])
        assert health.preferred is False and "record the search again" in health.demoted_reason
        sent = len(broken.sent) if hasattr(broken, "sent") else None
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=broken), hosts_path=hosts)
        assert res["ok"] is False and "demoted" in res["error"]
        if sent is not None:
            assert len(broken.sent) == sent                   # refused before sending
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_send_policy_refusals_are_not_counted_against_the_compiled_request():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        refused = FakeTransport(raises=PermissionError(
            "pre-send policy refused request: nonpublic destination '10.0.0.7' is refused by default"))
        for _ in range(4):
            res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                               executor=ApiStepExecutor(transport=refused), hosts_path=hosts)
            # nothing was sent, so nothing says the request changed: the rung stays in service
            assert res.get("policy_refused") is True and "demoted" not in res["error"], res
        with open(os.path.join(tmp, "workflow_crates_search", "compiled_api.json")) as fh:
            health = ApiHealth.from_dict(json.load(fh).get("health"))
        assert health.preferred is True and health.consecutive_failures == 0 and health.total_runs == 0
        res = run_compiled("crates_search", {"q": "serde"}, storage=storage,
                           executor=ApiStepExecutor(transport=FakeTransport()), hosts_path=hosts)
        assert res["ok"] is True, res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _OwnHostState:
    """A block or rate limit writes a host hold; keep it out of the shared test data."""

    NAMES = ("ANYUSE_DATA_DIR", "ANYUSE_BLOCKED_HOSTS_FILE", "ANYUSE_BROWSER_ONLY_HOSTS_FILE")

    def __init__(self, tmp):
        self.values = {"ANYUSE_DATA_DIR": os.path.join(tmp, "data"),
                       "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(tmp, "blocked.json"),
                       "ANYUSE_BROWSER_ONLY_HOSTS_FILE": os.path.join(tmp, "browser_only.json")}

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.NAMES}
        os.environ.update(self.values)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_a_block_never_counts_towards_taking_the_rung_out_of_service():
    tmp = tempfile.mkdtemp()
    try:
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        with _OwnHostState(tmp):
            for _ in range(4):
                run_compiled("crates_search", {"q": "serde"}, storage=storage,
                             executor=ApiStepExecutor(transport=FakeTransport(status=429, text="slow down")),
                             hosts_path=hosts)
        with open(os.path.join(tmp, "workflow_crates_search", "compiled_api.json")) as fh:
            health = ApiHealth.from_dict(json.load(fh).get("health"))
        assert health.preferred is True, health.demoted_reason
        assert health.consecutive_failures == 0, health
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_rate_limit_leaves_one_card_with_its_host_and_says_to_wait():
    tmp = tempfile.mkdtemp()
    own = _OwnHostState(tmp).__enter__()
    try:
        from anyuse.execution import human_inbox
        storage, _ = _fixture(tmp)
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        outs = [run_compiled("crates_search", {"q": "serde"}, storage=storage,
                             executor=ApiStepExecutor(transport=FakeTransport(status=429, text="slow")),
                             hosts_path=hosts) for _ in range(2)]
        assert all(o.get("status") == "needs_human" for o in outs), outs
        assert "rate limit" in outs[0]["fallback"] and "browser" not in outs[0]["fallback"]
        cards = human_inbox.Inbox(hosts_path=hosts).cards()
        assert len(cards) == 1, [(c["host"], c["kind"]) for c in cards]
        assert cards[0]["host"] == "crates.io" and "rate limit" in cards[0]["needed"]
    finally:
        own.__exit__(None, None, None)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_the_two_tools_are_registered_with_usable_schemas():
    assert compiled_tools.TOOL_LIST_NAME == "list_compiled_workflows"
    assert compiled_tools.TOOL_RUN_NAME == "run_compiled_workflow"
    assert compiled_tools.TOOL_RUN_SCHEMA["required"] == ["workflow"]
    assert "never drives the desktop" in compiled_tools.TOOL_RUN_DESCRIPTION.lower() \
        or "NEVER" in compiled_tools.TOOL_RUN_DESCRIPTION
    # a demoted request is sent once when its check is due (run_compiled)
    assert "demoted with its check due" in compiled_tools.TOOL_RUN_DESCRIPTION
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "mcpserver", "server.py")).read()
    assert "compiled_tools.TOOL_LIST_NAME" in src and "compiled_tools.TOOL_RUN_NAME" in src


def test_handle_ignores_tools_that_are_not_ours():
    assert compiled_tools.handle("run_task", {}) is None


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
