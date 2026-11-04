"""The one OpenAI-compatible model boundary, against an in-memory HTTP transport."""
import asyncio
import contextlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path

from anyuse.execution.agent_browser import CerebrasBrain
from anyuse.execution.judge import LlmJudge
from anyuse.execution.model_adapter import (
    ModelAdapter, ModelConfig, _is_unreachable,
)
from anyuse.execution.planner import ExecutionPlanner
from anyuse.execution.visual_step import VisualStepResolver
from anyuse.guides.state_graph import StateGraph
from anyuse.memory.models import WorkflowIntent, WorkflowTemplate


SECRET = "adapter-secret-that-must-never-leak"


class _State:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []


def _answer(text, prompt=11, completion=4):
    return {"body": {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
    }}


@contextlib.contextmanager
def _server(*replies, timeout=1.0, key_env="ANYUSE_TEST_MODEL_KEY", retries=0, sleeper=None,
            structured_output_mode="json_schema", reasoning_effort=None):
    import httpx
    from unittest import mock
    state = _State(replies)
    origin = "http://127.0.0.1:19001"
    old_key = os.environ.get(key_env)
    os.environ[key_env] = SECRET
    config = ModelConfig("local", origin + "/v1", "fake-model", key_env,
                         timeout=timeout, max_tokens=321, retries=retries,
                         structured_output_mode=structured_output_mode, reasoning_effort=reasoning_effort)
    def respond(request):
        headers = {k.title(): v for k, v in request.headers.items()}
        state.requests.append({"path": request.url.path, "headers": headers, "body": json.loads(request.content)})
        reply = state.replies.pop(0)
        if reply.get("delay", 0) > request.extensions.get("timeout", {}).get("read", timeout):
            raise httpx.ReadTimeout("inert delayed fixture", request=request)
        return httpx.Response(reply.get("status", 200), json=reply.get("body", {}), headers=reply.get("headers", {}))
    transport = httpx.MockTransport(respond)
    original = ModelAdapter._get_client
    def client(adapter):
        if adapter.config.base_url == config.base_url and adapter._transport is None:
            adapter._transport = transport
        return original(adapter)
    try:
        with mock.patch.object(ModelAdapter, "_get_client", client):
            yield ModelAdapter(config, sleeper=sleeper, transport=transport), state
    finally:
        if old_key is None:
            os.environ.pop(key_env, None)
        else:
            os.environ[key_env] = old_key


def test_success_usage_latency_user_agent_and_call_counter():
    with _server(_answer("hello")) as (adapter, state):
        result = adapter.complete([{"role": "user", "content": "hi"}])
        assert result.ok and result.text == "hello"
        assert result.usage.prompt_tokens == 11 and result.usage.completion_tokens == 4
        assert result.latency_ms >= 0 and result.llm_calls == 1 and adapter.call_count == 1
        req = state.requests[0]
        assert req["path"] == "/v1/chat/completions"
        assert req["headers"]["User-Agent"].startswith("anyuse-model-adapter/")
        assert req["body"]["model"] == "fake-model"
        assert req["body"] == {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}],
                               "temperature": 0.1, "max_tokens": 321}


def test_explicit_reasoning_effort_is_forwarded_without_changing_core_payload_or_json_validation():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    messages = [{"role": "user", "content": "Return the declared JSON object."}]
    for mode, effort in (("json_schema", "none"), ("json_object", "vendor-balanced-v2")):
        with _server(_answer('{"ok":true}'), _answer('invalid JSON'),
                     structured_output_mode=mode, reasoning_effort=effort) as (adapter, state):
            good = adapter.complete(messages, json_schema=schema)
            bad = adapter.complete(messages, json_schema=schema)
            assert good.ok and good.parsed == {"ok": True} and good.llm_calls == 1
            assert not bad.ok and bad.text == 'invalid JSON' and bad.llm_calls == 1
            assert len(state.requests) == 2
            for request in state.requests:
                body = request["body"]
                assert body["reasoning_effort"] == effort and body["messages"] == messages
                assert body["model"] == "fake-model" and body["max_tokens"] == 321
                assert body["temperature"] == 0.1 and body["response_format"]["type"] == mode
                if mode == "json_schema":
                    assert body["response_format"]["json_schema"]["schema"] == schema
                    assert body["response_format"]["json_schema"]["strict"] is True
                else:
                    assert body["response_format"] == {"type": "json_object"}


def test_reasoning_effort_explicit_role_override_and_invalid_values_refuse_before_call():
    from dataclasses import asdict
    from anyuse.execution.model_adapter import process_call_count, resolve_model_config
    before = process_call_count()
    assert resolve_model_config("agent", provider="local").reasoning_effort is None
    assert ModelAdapter.for_role("agent", provider="local", reasoning_effort="none").config.reasoning_effort == "none"
    assert resolve_model_config("planner", provider="local", reasoning_effort="provider-defined").reasoning_effort == "provider-defined"
    assert ModelConfig("local", "http://127.0.0.1:1/v1", "fake", "", reasoning_effort="x" * 64).reasoning_effort == "x" * 64
    for value in (None, "none", "Provider-Defined-V2"):
        config = resolve_model_config("agent", provider="local", reasoning_effort=value)
        assert ModelConfig(**json.loads(json.dumps(asdict(config)))) == config
    for value in ("", " ", " none", "none ", "low\nmedium", "bad\x00", "x" * 65, False, 1, [], {}):
        for configure in (lambda: ModelConfig("local", "http://127.0.0.1:1/v1", "fake", "", reasoning_effort=value),
                          lambda: ModelAdapter.for_role("agent", provider="local", reasoning_effort=value)):
            try:
                configure()
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid reasoning effort accepted: {value!r}")
    assert process_call_count() == before


def test_explicit_reasoning_control_does_not_turn_reasoning_only_reply_into_an_answer():
    reply = {"body": {"choices": [{"finish_reason": "length", "message": {
        "role": "assistant", "content": "", "reasoning_content": "fixture reasoning must not become an action"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 321}}}
    with _server(reply, reasoning_effort="none") as (adapter, state):
        result = adapter.complete([{"role": "user", "content": "Return JSON."}])
        assert state.requests[0]["body"]["reasoning_effort"] == "none"
        assert not result.ok and result.empty_answer and result.text == "" and result.parsed is None
        assert result.finish_reason == "length" and "reasoning only" in result.error
        assert result.llm_calls == 1 and len(state.requests) == 1


def test_structured_output_and_image_are_openai_compatible():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"]}
    with _server(_answer('{"ok":true}')) as (adapter, state):
        result = adapter.complete(
            [{"role": "user", "content": "look"}], json_schema=schema,
            images=["data:image/png;base64,UE5H"],
        )
        assert result.ok and result.parsed == {"ok": True}
        body = state.requests[0]["body"]
        assert body["response_format"]["type"] == "json_schema"
        content = body["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "look"}
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_explicit_json_object_compatibility_keeps_json_parsing_and_one_call():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with _server(_answer('{"ok":true}'), _answer('invalid JSON'),
                 structured_output_mode="json_object") as (adapter, state):
        messages = [{"role": "user", "content": "Return the declared JSON object."}]
        good = adapter.complete(messages, json_schema=schema)
        bad = adapter.complete(messages, json_schema=schema)
        assert good.ok and good.parsed == {"ok": True} and good.llm_calls == 1
        assert not bad.ok and bad.text == 'invalid JSON' and bad.llm_calls == 1
        assert len(state.requests) == 2
        assert all(r["body"]["response_format"] == {"type": "json_object"} for r in state.requests)
        assert all(r["body"]["messages"] == messages for r in state.requests)


def test_structured_output_mode_role_precedence_and_invalid_mode_refusal():
    from unittest.mock import patch
    from anyuse.execution.model_adapter import resolve_model_config
    with patch.dict(os.environ, {"ANYUSE_MODEL_STRUCTURED_OUTPUT_MODE": "json_object",
                                 "ANYUSE_AGENT_MODEL_STRUCTURED_OUTPUT_MODE": "json_schema"}):
        # _env uses ANYUSE_<ROLE>_MODEL_<SUFFIX> for role-specific settings.
        assert resolve_model_config("agent", provider="local").structured_output_mode == "json_schema"
        assert resolve_model_config("planner", provider="local").structured_output_mode == "json_object"
        assert resolve_model_config("agent", provider="local", structured_output_mode="json_object").structured_output_mode == "json_object"
        for invalid in ("none", "auto"):
            try:
                resolve_model_config("agent", provider="local", structured_output_mode=invalid)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid mode accepted")
    try:
        ModelAdapter(ModelConfig("local", "http://127.0.0.1:1/v1", "fake", "",
                                 structured_output_mode="auto"))
    except ValueError:
        pass
    else:
        raise AssertionError("invalid direct configuration accepted")


def test_402_and_429_are_provider_unreachable():
    with _server({"status": 402}, {"status": 429}) as (adapter, _state):
        for expected in (402, 429):
            result = adapter.complete([{"role": "user", "content": "x"}])
            assert not result.ok and result.status_code == expected
            assert result.provider_unreachable and result.llm_calls == 1


def test_429_with_retry_after_is_retried_and_every_attempt_is_counted():
    waits = []
    with _server({"status": 429, "headers": {"Retry-After": "0"}}, _answer("ok"),
                 retries=2, sleeper=waits.append) as (adapter, state):
        result = adapter.complete([{"role": "user", "content": "x"}])
    assert result.ok and result.text == "ok"
    assert result.llm_calls == 2 and adapter.call_count == 2
    assert waits == [0.0]
    assert len(state.requests) == 2 and state.requests[0]["body"] == state.requests[1]["body"]


def test_retries_exhausted_is_provider_unreachable_with_our_own_backoff():
    waits = []
    with _server({"status": 503}, {"status": 503}, {"status": 503},
                 retries=2, sleeper=waits.append) as (adapter, _state):
        result = adapter.complete([{"role": "user", "content": "x"}])
    assert not result.ok and result.provider_unreachable and result.status_code == 503
    assert result.llm_calls == 3
    assert waits == [1.0, 2.0], "1 s doubling when there is no Retry-After"
    assert "3 attempts" in result.error and SECRET not in result.error


def test_402_and_400_are_never_retried():
    waits = []
    with _server({"status": 402}, {"status": 400}, retries=2,
                 sleeper=waits.append) as (adapter, _state):
        for expected in (402, 400):
            result = adapter.complete([{"role": "user", "content": "x"}])
            assert result.status_code == expected and result.llm_calls == 1
    assert waits == []


def test_retry_after_is_capped_and_a_date_falls_back_to_backoff():
    waits = []
    with _server({"status": 429, "headers": {"Retry-After": "600"}},
                 {"status": 500, "headers": {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}},
                 _answer("ok"), retries=2, sleeper=waits.append) as (adapter, _state):
        result = adapter.complete([{"role": "user", "content": "x"}])
    assert result.ok and result.llm_calls == 3
    assert waits == [8.0, 1.0]


def test_retries_come_from_the_environment_and_are_bounded():
    os.environ["ANYUSE_MODEL_RETRIES"] = "1"
    os.environ["ANYUSE_JUDGE_MODEL_RETRIES"] = "0"
    try:
        from anyuse.execution.model_adapter import resolve_model_config
        assert resolve_model_config("agent").retries == 1
        assert resolve_model_config("judge").retries == 0
        assert resolve_model_config("agent", retries=4).retries == 4
        os.environ["ANYUSE_MODEL_RETRIES"] = "9"
        try:
            resolve_model_config("agent")
        except ValueError as exc:
            assert "retries" in str(exc)
        else:
            raise AssertionError("9 retries must be refused")
    finally:
        os.environ.pop("ANYUSE_MODEL_RETRIES", None)
        os.environ.pop("ANYUSE_JUDGE_MODEL_RETRIES", None)


def test_timeout_and_dns_are_provider_unreachable():
    with _server({"delay": 0.2, "body": {}}, timeout=0.03) as (adapter, _state):
        result = adapter.complete([{"role": "user", "content": "x"}])
        assert not result.ok and result.provider_unreachable and result.llm_calls == 1
        assert "Timeout" in result.error and SECRET not in result.error
    import httpx
    assert _is_unreachable(httpx.ConnectError("DNS lookup failed"))


def test_missing_key_refuses_before_request_and_key_never_leaks():
    missing = "ANYUSE_DELIBERATELY_MISSING_MODEL_KEY"
    os.environ.pop(missing, None)
    with _server(_answer("unused"), key_env="ANYUSE_TEMP_SERVER_KEY") as (_unused, state):
        # Use the running server's URL from the unused adapter without sharing its key name.
        base = _unused.config.base_url
        adapter = ModelAdapter(ModelConfig("local", base, "fake", missing))
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            result = adapter.complete([{"role": "user", "content": SECRET}])
        finally:
            logging.getLogger().removeHandler(handler)
        assert not result.ok and result.llm_calls == 0 and adapter.call_count == 0
        assert state.requests == []
        assert SECRET not in result.error and SECRET not in repr(result) and SECRET not in stream.getvalue()


def test_name_value_file_is_read_at_call_time():
    file_key = "ANYUSE_FILE_ONLY_MODEL_KEY"
    old_file = os.environ.get("ANYUSE_MODEL_ENV_FILE")
    os.environ.pop(file_key, None)
    with _server(_answer("from-file"), key_env="ANYUSE_TEMP_SERVER_KEY") as (base_adapter, state):
        adapter = ModelAdapter(ModelConfig(
            "local", base_adapter.config.base_url, "fake", file_key
        ))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.env"
            os.environ["ANYUSE_MODEL_ENV_FILE"] = str(path)
            # Constructed before the file exists: lookup must happen on complete().
            path.write_text(f"{file_key}={SECRET}\n")
            result = adapter.complete([{"role": "user", "content": "x"}])
        if old_file is None:
            os.environ.pop("ANYUSE_MODEL_ENV_FILE", None)
        else:
            os.environ["ANYUSE_MODEL_ENV_FILE"] = old_file
        assert result.ok and result.text == "from-file" and len(state.requests) == 1


def _plan_json():
    return json.dumps({
        "current_state": "s1", "navigation_needed": [], "reasoning": "AX button exists",
        "steps": [{"intent": "save", "source": "workflow", "target_element": "Save",
                   "modality": "ax", "command": {"role": "AXButton", "title": "Save",
                                                        "action": "press"},
                   "input_data": None, "needs_visual": False,
                   "expected_after": "saved", "expected_after_state": None,
                   "reasoning": "direct"}],
        "skipped_steps": [], "injected_steps": [],
    })


def test_planner_visual_agent_and_judge_all_cross_the_same_adapter():
    visual_json = json.dumps({"action": "coordinate_command",
                              "command": {"x": 20, "y": 40, "action": "click"},
                              "reasoning": "visible"})
    action_json = '{"action":"click","index":0}'
    # seed=0 maps opt0 deterministically; only the fact of an answered verdict matters.
    judge_json = '{"choice":"opt0","confidence":0.9}'
    with _server(_answer(_plan_json()), _answer(visual_json), _answer(action_json),
                 _answer(judge_json)) as (adapter, state):
        wf = WorkflowTemplate(id="w", name="save", steps=[
            WorkflowIntent(intent="save", target_element="Save", expected_after="saved")
        ])
        plan = asyncio.run(ExecutionPlanner(adapter=adapter).plan(
            "save", {}, wf, "{}", StateGraph(), {}, None))
        assert plan.steps[0].modality == "ax" and plan.llm_calls == 1

        class Shot:
            def capture(self):
                return b"PNG"
        visual = asyncio.run(VisualStepResolver(
            adapter=adapter, screenshot_capture=Shot(), pixel_scale=1.0
        ).resolve(plan.steps[0]))
        assert visual["ok"] and visual["command"]["x"] == 20 and visual["llm_calls"] == 1

        action, usage, cached = CerebrasBrain(adapter=adapter)([
            {"role": "user", "content": "page"}
        ])
        assert action == {"action": "click", "index": 0} and not cached
        assert usage["prompt_tokens"] == 11

        verdict = LlmJudge(adapter=adapter, seed=0).triage({"screen": "changed"})
        assert verdict.answered and verdict.llm_calls == 1
        assert len(state.requests) == 4 and adapter.call_count == 4
        assert any(
            part.get("type") == "image_url"
            for part in state.requests[1]["body"]["messages"][0]["content"]
        )


if __name__ == "__main__":
    import inspect
    import sys
    module = sys.modules[__name__]
    passed = failed = 0
    for name, fn in [(n, f) for n, f in vars(module).items()
                     if n.startswith("test_") and callable(f)]:
        if inspect.signature(fn).parameters:
            continue
        try:
            fn(); passed += 1; print("OK ", name)
        except Exception as exc:
            import traceback
            failed += 1; print("FAIL", name, "::", repr(exc)[:300])
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
