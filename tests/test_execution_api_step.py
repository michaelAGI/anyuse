"""The compiled ``api`` rung: request template, postcondition, HIL classification,
ladder ordering and the fall-back to the recorded UI path.

Everything here is offline — the transport is injected, so the deliberate
4xx/5xx/shape-change failure injections run with no network at all.
"""
import asyncio
import os

from anyuse.execution.api_step import (
    ApiResponse,
    ApiStepExecutor,
    HttpxTransport,
    MAX_JSON_DEPTH,
    MAX_RESPONSE_BYTES,
    ResourceLimitError,
    RequestTemplate,
    api_hil_action,
    check_expect,
    json_leaves,
    json_path,
    validate_json_limits,
)
from anyuse.execution.deterministic import impl_to_step, rank_implementations, replay_transitions
from anyuse.execution.hil_gate import StepClass, classify_step
from anyuse.guides.state_graph import TransitionImpl, UITransition

CRATES = {"crates": [{"id": "serde", "downloads": 9}, {"id": "serde_json", "downloads": 8}],
          "meta": {"total": 2}}


class FakeTransport:
    """Records what was sent; returns a canned response (or raises)."""

    def __init__(self, status=200, text="{}", content_type="application/json", raises=None):
        self.status, self.text, self.content_type, self.raises = status, text, content_type, raises
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append({"method": method, "url": url, "headers": dict(headers),
                          "body": body, "timeout_s": timeout_s})
        if self.raises:
            raise self.raises
        return ApiResponse(status=self.status, headers={"Content-Type": self.content_type},
                           text=self.text)


def _tmpl(**kw):
    base = dict(method="GET", url="https://crates.io/api/v1/crates",
                query={"q": "{q}", "per_page": "10"}, headers={"accept": "application/json"},
                extract={"items": "crates[*].id"},
                expect={"status_in": [200], "content_type": "json", "path": "crates",
                        "min_items": 1, "required_keys": ["id"]},
                params=["q"])
    base.update(kw)
    return RequestTemplate(**base)


# ---------------------------------------------------------------------------
# json path
# ---------------------------------------------------------------------------

def test_json_path_maps_star_over_the_rest_of_the_path():
    # The bug this guards: a '*' that returned the list and dropped the tail
    # turned an ordered extraction into a bag of whole objects.
    assert json_path(CRATES, "crates[*].id") == ["serde", "serde_json"]
    assert json_path(CRATES, "crates[0].id") == "serde"
    assert json_path(CRATES, "meta.total") == 2
    assert json_path(CRATES, "") == CRATES


def test_json_path_returns_none_for_anything_unresolvable():
    assert json_path(CRATES, "nope") is None
    assert json_path(CRATES, "crates[9].id") is None
    assert json_path(CRATES, "meta.total.deeper") is None
    assert json_path([1, 2, 3], "a[*].b") is None


def test_json_path_maps_a_bare_key_over_a_top_level_list():
    assert json_path([{"name": "a"}, {"name": "b"}], "[*].name") == ["a", "b"]


def test_json_leaves_flattens_with_indexed_paths():
    leaves = dict(json_leaves(CRATES))
    assert leaves["crates[0].id"] == "serde"
    assert leaves["meta.total"] == 2


# ---------------------------------------------------------------------------
# template rendering and validation
# ---------------------------------------------------------------------------

def test_render_binds_query_and_encodes():
    url, headers, body = _tmpl().render({"q": "rust async"})
    assert url == "https://crates.io/api/v1/crates?q=rust+async&per_page=10"
    assert headers == {"accept": "application/json"}
    assert body is None


def test_render_encodes_a_path_parameter_with_no_safe_characters():
    url, _h, _b = _tmpl(url="https://crates.io/api/v1/crates/{crate}", query={},
                        params=["crate"]).render({"crate": "a/b?c"})
    assert url == "https://crates.io/api/v1/crates/a%2Fb%3Fc"


def test_render_refuses_a_half_bound_url():
    try:
        _tmpl().render({})
    except KeyError as exc:
        assert "q" in str(exc)
    else:
        raise AssertionError("a missing parameter must not reach the wire")


def test_problems_rejects_unsafe_and_unreplayable_templates():
    assert any("https" in p for p in _tmpl(url="http://crates.io/x").problems())
    assert any("read method" in p for p in _tmpl(method="POST").problems())
    assert any("credential" in p for p in _tmpl(headers={"Authorization": "x"}).problems())
    assert any("allowlist" in p for p in _tmpl(headers={"X-Weird": "x"}).problems())
    assert any("signed" in p for p in _tmpl(query={"token": "abc"}).problems())
    assert any("placeholder" in p for p in _tmpl(query={"q": "{nope}"}).problems())
    assert _tmpl().problems() == []


def test_render_drops_any_header_outside_the_allowlist():
    t = _tmpl()
    t.headers = {"accept": "application/json", "cookie": "session=1"}
    _url, headers, _b = t.render({"q": "x"})
    assert "cookie" not in headers


def test_template_round_trips_through_dict():
    t = _tmpl(shape="fields")
    assert RequestTemplate.from_dict(t.to_dict()).to_dict() == t.to_dict()


# ---------------------------------------------------------------------------
# postcondition
# ---------------------------------------------------------------------------

def test_check_expect_accepts_the_recorded_shape():
    ok, reason = check_expect(200, "application/json", CRATES, _tmpl().expect)
    assert ok, reason


def test_check_expect_catches_status_shape_and_missing_keys():
    exp = _tmpl().expect
    assert check_expect(404, "application/json", CRATES, exp)[0] is False
    assert check_expect(200, "text/html", CRATES, exp)[0] is False
    assert check_expect(200, "application/json", {"crates": []}, exp)[0] is False
    assert check_expect(200, "application/json", {"crates": [{"x": 1}]}, exp)[0] is False
    assert check_expect(200, "application/json", {"crates": {}}, exp)[0] is False


def test_check_expect_required_keys_follow_escaped_dotted_paths():
    npm = {"objects": [{"package": {"name": "chalk"}}]}
    nested = {"status_in": [200], "path": "objects", "min_items": 1,
              "required_keys": ["package.name"]}
    assert check_expect(200, "application/json", npm, nested)[0] is True

    flat = {"status_in": [200], "path": "objects", "min_items": 1,
            "required_keys": ["name"]}
    assert check_expect(200, "application/json", {"objects": [{"name": "chalk"}]},
                        flat)[0] is True

    literal_dot = {"status_in": [200], "path": "objects", "min_items": 1,
                   "required_keys": [r"package\.name"]}
    dotted_key = {"objects": [{"package.name": "chalk"}]}
    assert check_expect(200, "application/json", dotted_key, literal_dot)[0] is True
    assert check_expect(200, "application/json", dotted_key, nested)[0] is False


def test_check_expect_non_empty_on_a_field_shaped_template():
    exp = {"status_in": [200], "path": "crate", "non_empty": True}
    assert check_expect(200, "application/json", {"crate": {"id": "serde"}}, exp)[0] is True
    assert check_expect(200, "application/json", {"crate": {}}, exp)[0] is False


# ---------------------------------------------------------------------------
# executor: success and every failure mode
# ---------------------------------------------------------------------------

def test_execute_success_extracts_and_reports():
    tr = FakeTransport(text='{"crates":[{"id":"serde"}]}')
    res = ApiStepExecutor(transport=tr).execute(_tmpl(), {"q": "serde"})
    assert res["success"] is True
    assert res["method"] == "api"
    assert res["extracted"]["items"] == ["serde"]
    assert tr.sent[0]["url"].endswith("q=serde&per_page=10")


def test_execute_fails_softly_on_4xx_5xx_non_json_and_shape_change():
    # FAILURE INJECTION: each of these must return success=False (never raise),
    # which is what makes the ladder fall through to the recorded UI path.
    cases = {
        "404": FakeTransport(status=404, text="not found"),
        "500": FakeTransport(status=500, text="oops"),
        "html": FakeTransport(status=200, text="<html>hi</html>", content_type="text/html"),
        "shape": FakeTransport(status=200, text='{"results":[{"id":"serde"}]}'),
        "empty": FakeTransport(status=200, text='{"crates":[]}'),
        "network": FakeTransport(raises=OSError("connection reset")),
    }
    for name, tr in cases.items():
        res = ApiStepExecutor(transport=tr).execute(_tmpl(), {"q": "serde"})
        assert res["success"] is False, name
        assert res["error"], name
        assert res["method"] == "api", name


def test_oversized_response_is_refused_before_parsing():
    body = "x" * (MAX_RESPONSE_BYTES + 1)
    out = ApiStepExecutor(transport=FakeTransport(
        text=body, content_type="application/json")).execute(_tmpl(), {"q": "serde"})
    assert out["success"] is False
    assert out["outcome_kind"] == "terminal_refusal"
    assert "byte cap" in out["error"]


def test_transport_and_json_parser_enforce_resource_caps():
    class Client:
        def __init__(self):
            self.cookies = type("Cookies", (), {"clear": lambda self: None})()
        def request(self, *args, **kwargs):
            return type("Response", (), {"status_code": 200, "headers": {},
                                          "text": "x" * (MAX_RESPONSE_BYTES + 1)})()

    transport = HttpxTransport()
    transport._client = Client()
    try:
        transport.send_with_policy(
            "GET", "https://93.184.216.34/x", {}, None, 1,
            allowed_origins={"https://93.184.216.34"})
    except ResourceLimitError:
        pass
    else:
        raise AssertionError("transport buffered an oversized response")

    nested = "leaf"
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = [nested]
    ok, reason = validate_json_limits(nested)
    assert ok is False and "depth cap" in reason


def test_request_timeout_is_bounded():
    assert any("timeout" in problem for problem in _tmpl(timeout_s=31).problems())


def test_partial_extraction_and_identity_mismatch_are_incomplete():
    partial = _tmpl()
    partial.expect["min_items"] = 3
    out = ApiStepExecutor(transport=FakeTransport(
        text='{"crates":[{"id":"serde"},{"id":"serde-json"}]}')).execute(
            partial, {"q": "serde"})
    assert out["success"] is False
    assert out["outcome_kind"] == "incomplete"

    wrong = _tmpl()
    wrong.expect["identity"] = {"parameter": "q", "relation": "normalized_match"}
    out = ApiStepExecutor(transport=FakeTransport(
        text='{"crates":[{"id":"tokio"}]}')).execute(wrong, {"q": "serde"})
    assert out["success"] is False
    assert out["outcome_kind"] == "incomplete"
    assert "identity failed" in out["error"]


def test_kill_switch_disables_the_rung():
    prev = os.environ.get("ANYUSE_API_MODALITY")
    os.environ["ANYUSE_API_MODALITY"] = "off"
    try:
        res = ApiStepExecutor(transport=FakeTransport(text='{"crates":[{"id":"x"}]}')).execute(
            _tmpl(), {"q": "x"})
        assert res["success"] is False and "disabled" in res["error"]
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_API_MODALITY", None)
        else:
            os.environ["ANYUSE_API_MODALITY"] = prev


def test_a_write_method_is_never_sent_and_is_routed_to_approval():
    tr = FakeTransport()
    res = ApiStepExecutor(transport=tr).execute(_tmpl(method="POST"), {"q": "x"})
    assert res["success"] is False
    assert res["requires_approval"] is True and res["blocked"] is True
    assert res["hil_action"]["action"] == "api_post"
    assert tr.sent == [], "a non-read method must never reach the transport"


def test_boundary_checker_can_deny_an_api_request():
    class Deny:
        def check_action(self, action):
            return (False, "domain not allowed")

    tr = FakeTransport(text='{"crates":[{"id":"x"}]}')
    res = ApiStepExecutor(transport=tr, compliance_checker=Deny()).execute(_tmpl(), {"q": "x"})
    assert res["success"] is False and res["blocked"] is True
    assert tr.sent == []


def test_a_broken_boundary_checker_fails_closed():
    class Broken:
        def check_action(self, action):
            raise RuntimeError("boom")

    tr = FakeTransport(text='{"crates":[{"id":"x"}]}')
    res = ApiStepExecutor(transport=tr, compliance_checker=Broken()).execute(_tmpl(), {"q": "x"})
    assert res["success"] is False and tr.sent == []


def test_nonpublic_destinations_require_an_explicit_exact_fixture_origin():
    from anyuse.execution.request_policy import destination_refusal
    url = "https://127.0.0.1:8443/api"
    assert "nonpublic" in destination_refusal(url, ["https://127.0.0.1:8443"])
    assert destination_refusal(
        url, ["https://127.0.0.1:8443"],
        local_fixture_origins=["https://127.0.0.1:8443"]) == ""
    assert "nonpublic" in destination_refusal(
        "https://service.internal/x", ["https://service.internal"])


def test_http_redirect_is_policy_checked_before_the_next_hop():
    class Response:
        def __init__(self, status, headers=None, text=""):
            self.status_code, self.headers, self.text = status, headers or {}, text

    class Client:
        def __init__(self):
            self.urls = []
            class Cookies:
                def clear(self):
                    pass
            self.cookies = Cookies()
        def request(self, method, url, **kwargs):
            self.urls.append(url)
            return Response(302, {"location": "https://1.1.1.1/private"})

    transport = HttpxTransport()
    transport._client = Client()
    try:
        transport.send_with_policy("GET", "https://93.184.216.34/start", {}, None, 1,
                                   allowed_origins={"https://93.184.216.34"})
    except PermissionError as exc:
        # the first request was sent and answered: the refusal names the redirect
        assert "exact-origin allowlist" in str(exc)
        assert str(exc).startswith("pre-send policy refused a redirect: "), str(exc)
    else:
        raise AssertionError("an off-origin redirect must be refused")
    assert transport._client.urls == ["https://93.184.216.34/start"]
    # a refusal before the first request: nothing was sent
    try:
        transport.send_with_policy("GET", "https://10.0.0.7/start", {}, None, 1,
                                   allowed_origins={"https://10.0.0.7"})
    except PermissionError as exc:
        assert str(exc).startswith("pre-send policy refused request: nonpublic destination"), str(exc)
    else:
        raise AssertionError("a nonpublic destination must be refused")
    assert transport._client.urls == ["https://93.184.216.34/start"]


def test_transport_cookie_jar_never_stores_or_transmits_response_cookies():
    import httpx

    transport = HttpxTransport()
    client = transport._get_client()
    response = httpx.Response(
        302, headers={"Set-Cookie": "sid=secret; Path=/"},
        request=httpx.Request("GET", "https://example.com/start"))
    client.cookies.extract_cookies(response)
    assert list(client.cookies.jar) == [], "the transport jar must reject storage"

    class FakeClient:
        def __init__(self):
            self.cookies = client.cookies
            self.seen_cookie_headers = []
        def request(self, method, url, headers, **kwargs):
            self.seen_cookie_headers.append(
                [v for k, v in headers.items() if k.lower() == "cookie"])
            return type("R", (), {"status_code": 200,
                                   "headers": {"Set-Cookie": "sid=secret"},
                                   "text": "{}"})()

    fake = FakeClient()
    transport._client = fake
    result = transport.send_with_policy(
        "GET", "https://93.184.216.34/start", {"Cookie": "caller=bad"}, None, 1,
        allowed_origins={"https://93.184.216.34"})
    assert fake.seen_cookie_headers == [[]]
    assert "Set-Cookie" not in result.headers
    assert list(fake.cookies.jar) == []


# ---------------------------------------------------------------------------
# HIL classification through the EXISTING gate
# ---------------------------------------------------------------------------

def test_a_read_request_classifies_read_only_in_the_existing_gate():
    assert classify_step(api_hil_action(_tmpl(), {"q": "serde"})) == StepClass.READ_ONLY


def test_a_write_request_classifies_destructive_in_the_existing_gate():
    assert classify_step(api_hil_action(_tmpl(method="POST"), {"q": "x"})) == StepClass.DESTRUCTIVE
    assert classify_step(api_hil_action(_tmpl(method="DELETE"), {"q": "x"})) == StepClass.DESTRUCTIVE


def test_a_destructive_keyword_in_the_url_still_promotes_a_get():
    # Conservative on purpose: GET https://…/delete?… is held, not waved through.
    action = api_hil_action(_tmpl(url="https://example.org/api/delete", query={}, params=[]))
    assert classify_step(action) == StepClass.DESTRUCTIVE


# ---------------------------------------------------------------------------
# ladder ordering + fall-back through the REAL replay path
# ---------------------------------------------------------------------------

def _transition_with_api_and_ui(template):
    return UITransition(
        from_state="s1", to_state="s2", intent="search crates",
        implementations=[
            TransitionImpl(modality="coordinate", command={"absolute_x": 5, "absolute_y": 6,
                                                           "action": "click_at"}),
            TransitionImpl(modality="ax", command={"role": "AXButton", "title": "Search"}),
            TransitionImpl(modality="api", command=template.to_dict()),
        ],
    )


def test_the_api_rung_ranks_above_every_ui_modality():
    t = _transition_with_api_and_ui(_tmpl())
    assert [i.modality for i in rank_implementations(t)] == ["api", "ax", "coordinate"]


def test_impl_to_step_attaches_params_without_mutating_the_stored_command():
    t = _transition_with_api_and_ui(_tmpl())
    api_impl = [i for i in t.implementations if i.modality == "api"][0]
    step = impl_to_step(t, api_impl, {"q": "serde"})
    assert step.modality == "api"
    assert step.command["bound_params"] == {"q": "serde"}
    assert "bound_params" not in api_impl.command, "the persisted command must stay clean"


class _LadderExecutor:
    """Minimal stand-in for StateGraphExecutor: real api rung, stubbed UI rungs."""

    def __init__(self, transport):
        self.api = ApiStepExecutor(transport=transport)
        self.dispatched = []

    async def _execute_step(self, step):
        self.dispatched.append(step.modality)
        if step.modality == "api":
            cmd = dict(step.command)
            params = cmd.pop("bound_params", {})
            return self.api.execute(RequestTemplate.from_dict(cmd), params)
        return {"success": True, "method": step.modality}


def test_a_healthy_api_rung_runs_and_the_ui_path_is_never_touched():
    ex = _LadderExecutor(FakeTransport(text='{"crates":[{"id":"serde"}]}'))
    t = _transition_with_api_and_ui(_tmpl())
    res = asyncio.run(replay_transitions([t], ex, {"q": "serde"}))
    assert res["success"] is True
    assert res["llm_calls"] == 0
    assert ex.dispatched == ["api"]
    assert res["steps"][0]["modality_used"] == "api"


def test_a_broken_api_falls_back_to_the_recorded_ui_path():
    for tr in (FakeTransport(status=500, text="oops"),
               FakeTransport(raises=OSError("dns"))):
        ex = _LadderExecutor(tr)
        t = _transition_with_api_and_ui(_tmpl())
        res = asyncio.run(replay_transitions([t], ex, {"q": "serde"}))
        assert res["success"] is True
        assert ex.dispatched == ["api", "ax"], ex.dispatched
        assert res["steps"][0]["modality_used"] == "ax"
        assert res["steps"][0]["tried"][0]["success"] is False


def test_an_incomplete_api_answer_stops_instead_of_falling_back():
    ex = _LadderExecutor(FakeTransport(status=200, text='{"unexpected":[]}'))
    result = asyncio.run(replay_transitions(
        [_transition_with_api_and_ui(_tmpl())], ex, {"q": "serde"}))
    assert result["success"] is False
    assert result["outcome_kind"] == "incomplete"
    assert ex.dispatched == ["api"]


def test_the_failing_api_rung_loses_success_rate_so_the_ladder_learns():
    ex = _LadderExecutor(FakeTransport(status=503, text="down"))
    t = _transition_with_api_and_ui(_tmpl())
    asyncio.run(replay_transitions([t], ex, {"q": "serde"}))
    api_impl = [i for i in t.implementations if i.modality == "api"][0]
    ax_impl = [i for i in t.implementations if i.modality == "ax"][0]
    assert api_impl.times_used == 1 and api_impl.times_succeeded == 0
    assert ax_impl.success_rate == 1.0
    # ... and on the next replay the proven ax rung is tried first.
    assert rank_implementations(t)[0].modality == "ax"


# ---------------------------------------------------------------------------
# executor wiring
# ---------------------------------------------------------------------------

def test_state_graph_executor_dispatches_the_api_modality():
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.planner import PlannedStep

    tr = FakeTransport(text='{"crates":[{"id":"serde"}]}')
    ex = StateGraphExecutor(api_executor=ApiStepExecutor(transport=tr))
    cmd = _tmpl().to_dict()
    cmd["bound_params"] = {"q": "serde"}
    step = PlannedStep(intent="search", source="workflow", target_element="",
                       modality="api", command=cmd)
    res = asyncio.run(ex._execute_step(step))
    assert res["success"] is True and res["extracted"]["items"] == ["serde"]


def test_executor_hil_projection_of_an_api_step_is_read_only_for_a_get():
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.planner import PlannedStep

    step = PlannedStep(intent="search crates", source="workflow", target_element="",
                       modality="api", command=_tmpl().to_dict())
    action = StateGraphExecutor._step_to_hil_action(step)
    assert action["action"] == "read"
    assert classify_step(action) == StepClass.READ_ONLY


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
