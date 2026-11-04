"""Tool-free reasoning choice through the actual adapter and inert HTTP transport."""
import copy
from dataclasses import replace
import json

import httpx
import pytest

from anyuse.execution import model_adapter as M
from anyuse.execution import rea_reasoning_choice as C

QUESTIONS = {"pick": {"type": "choice", "instructions": "Which action matches the task?",
                       "criteria": {"a0": "Pause playback", "none": "No supported action"}}}
GOOD = {"pick": {"choice": "a0", "confidence": .93}}


def model(replies, *, budget=None, key="", retries=0, price=1, finish_reason="stop",
          structured_output_mode="json_schema", request_hook=None):
    replies = list(replies)
    seen = []
    def post(request):
        seen.append(json.loads(request.content))
        if request_hook is not None:
            request_hook(seen[-1])
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if type(reply) is int:
            return httpx.Response(reply, json={"error": "fixture refusal"})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": reply if isinstance(reply, str) else json.dumps(reply)}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}})
    config = M.ModelConfig("local", "http://fixture.invalid/v1", "inert-reasoner", key,
                           timeout=.1, max_tokens=256, retries=retries, price_in=price, price_out=price,
                           structured_output_mode=structured_output_mode)
    adapter = M.ModelAdapter(config, transport=httpx.MockTransport(post), budget=budget, feature=C.FEATURE)
    return adapter, seen


def test_actual_adapter_strict_schema_exact_criteria_usage_budget_counter_and_raw_receipt():
    adapter, seen = model([GOOD])
    chooser = C.ReasoningClient(adapter)
    mark = M.process_call_count()
    answer = chooser.decide("Pause the playing video", QUESTIONS)
    assert not answer.error and answer.answers == {"pick": {"type": "choice", **GOOD["pick"]}}
    assert answer.input_tokens == 17 and answer.server_ms is not None
    assert len(seen) == 1 and M.calls_since(mark) == adapter.call_count == chooser.last_call["model_calls"] == 1
    payload = seen[0]
    assert "tools" not in payload and "tool_choice" not in payload
    assert payload["temperature"] == 0 and payload["max_tokens"] == 256
    schema = payload["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["pick"]["properties"]["choice"]["enum"] == ["a0", "none"]
    assert schema["schema"]["additionalProperties"] is False
    receipt = chooser.last_receipt
    assert receipt["model_output"]["text"] == json.dumps(GOOD)
    assert receipt["model_output"]["usage"]["total_tokens"] == 26
    assert receipt["raw_output_sha256"] and receipt["request_sha256"] and receipt["messages_sha256"]
    assert receipt["budget_after"]["spent_usd"] > receipt["budget_before"]["spent_usd"]
    assert chooser.metadata["confidence_basis"] == "reasoner_self_reported_not_independently_calibrated"


@pytest.mark.parametrize("reply", [
    "not JSON", '{"pick":{"choice":"a0","choice":"none","confidence":0.9}}',
    {"pick": {"choice": "unoffered", "confidence": .9}},
    {"pick": {"choice": "a0", "confidence": True}},
    {"pick": {"choice": "a0", "confidence": -1}},
    {"pick": {"choice": "a0", "confidence": 1.01}},
    '{"pick":{"choice":"a0","confidence":NaN}}',
    {"pick": {"choice": "a0", "confidence": .9, "shell": "ignored"}},
    {"pick": {"choice": "a0"}}, {"other": {"choice": "a0", "confidence": .9}},
    {"pick": {"choice": ["a0"], "confidence": .9}},
])
def test_malformed_extra_unoffered_or_out_of_range_response_abstains_without_retry(reply):
    adapter, seen = model([reply, GOOD])
    chooser = C.ReasoningClient(adapter)
    answer = chooser.decide("state", QUESTIONS)
    assert answer.error and answer.answers == {}
    assert len(seen) == chooser.last_call["model_calls"] == 1
    assert "model_output" in chooser.last_receipt
    json.dumps(chooser.last_receipt, allow_nan=False)


@pytest.mark.parametrize("response", [503, 429, 400, httpx.ConnectError("fixture offline")])
def test_http_or_transport_failure_is_counted_once_and_not_retried(response):
    adapter, seen = model([response, GOOD])
    chooser = C.ReasoningClient(adapter)
    mark = M.process_call_count()
    assert chooser.decide("state", QUESTIONS).error
    assert len(seen) == M.calls_since(mark) == chooser.last_call["model_calls"] == 1
    assert chooser.last_receipt["model_output"]["llm_calls"] == 1


def test_budget_refuses_before_network_and_counter_increment():
    adapter, seen = model([GOOD], budget=M.RunBudget(0))
    chooser = C.ReasoningClient(adapter)
    mark = M.process_call_count()
    assert chooser.decide("state", QUESTIONS).error
    assert not seen and M.calls_since(mark) == chooser.last_call["model_calls"] == 0
    assert chooser.last_receipt["model_output"]["budget_exhausted"] is True


def test_zero_priced_explicit_local_model_can_use_zero_dollar_budget():
    adapter, seen = model([GOOD], budget=M.RunBudget(0), price=0)
    chooser = C.ReasoningClient(adapter)
    assert not chooser.decide("state", QUESTIONS).error
    assert len(seen) == 1 and chooser.last_call["budget_after"]["spent_usd"] == 0


def test_injected_adapter_keeps_its_budget_unless_shared_budget_is_explicit():
    adapter, seen = model([GOOD])
    original = adapter.budget
    C.ReasoningClient(adapter)
    assert adapter.budget is original
    shared = M.RunBudget(.0006)
    prior = shared.reserve(100, 1, 1)
    shared.settle(prior, .0002)
    chooser = C.ReasoningClient(adapter, budget=shared)
    assert adapter.budget is shared
    assert chooser.decide("state", QUESTIONS).error
    assert not seen and chooser.last_call["budget_before"]["spent_usd"] == .0002
    assert chooser.last_call["model_calls"] == 0


def test_missing_key_available_is_config_only_and_never_sends_or_leaks_secret(monkeypatch):
    name = "ANYUSE_TEST_REA_REASONER_MISSING_KEY"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(M, "_key_from_file", lambda *_: "")
    adapter, seen = model([GOOD], key=name)
    chooser = C.ReasoningClient(adapter)
    assert not chooser.available and not seen
    assert chooser.decide("state", QUESTIONS).error and not seen
    assert chooser.last_call["model_calls"] == 0
    assert "Authorization" not in json.dumps(chooser.last_receipt)


def test_available_config_check_does_not_create_http_client():
    adapter, seen = model([GOOD])
    chooser = C.ReasoningClient(adapter)
    assert chooser.available and chooser.available and not seen and adapter._client is None


@pytest.mark.parametrize("state,questions,kwargs", [
    ("word" * 10000, QUESTIONS, {}),
    ("state", {"pick": {"type": "noul", "instructions": "yes?"}}, {}),
    ("state", {"pick": {"type": "choice", "instructions": "pick", "criteria": {"one": "only"}}}, {}),
    ("state", QUESTIONS, {"images": ["private image"]}),
    ("state", QUESTIONS, {"steps": 2}),
    ("state", QUESTIONS, {"guard_obs": {}}),
])
def test_invalid_or_oversized_input_refuses_before_request(state, questions, kwargs):
    adapter, seen = model([GOOD])
    chooser = C.ReasoningClient(adapter)
    assert chooser.decide(state, questions, **kwargs).error
    assert not seen and chooser.last_call["model_calls"] == 0


def test_multiple_questions_and_nine_exact_labels_are_preserved():
    questions = copy.deepcopy(QUESTIONS)
    questions["pick"]["criteria"] = {"a" + str(i): "Action " + str(i) for i in range(8)}
    questions["pick"]["criteria"]["none"] = "No supported action"
    questions["other"] = copy.deepcopy(QUESTIONS["pick"])
    reply = {"pick": {"choice": "a7", "confidence": .9}, "other": {"choice": "none", "confidence": .8}}
    adapter, seen = model([reply])
    chooser = C.ReasoningClient(adapter)
    assert chooser.decide("state", questions).answers["pick"]["choice"] == "a7"
    schema = seen[0]["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["pick", "other"]
    assert len(schema["properties"]["pick"]["properties"]["choice"]["enum"]) == 9


def test_retrying_or_nonadapter_configuration_cannot_silently_broaden_behavior():
    adapter, _ = model([GOOD], retries=2)
    with pytest.raises(ValueError, match="retries=0"):
        C.ReasoningClient(adapter)
    with pytest.raises(ValueError, match="existing ModelAdapter"):
        C.ReasoningClient(object())


def test_adapter_reconfiguration_cannot_enable_retries_after_construction():
    adapter, seen = model([503, GOOD])
    chooser = C.ReasoningClient(adapter)
    adapter.config = replace(adapter.config, retries=2)
    assert not chooser.available
    assert "retries=0" in chooser.decide("state", QUESTIONS).error
    assert not seen and chooser.last_call["model_calls"] == 0


def test_embedded_endpoint_credentials_refuse_without_entering_receipts():
    adapter, seen = model([GOOD])
    adapter.config = replace(adapter.config, base_url="http://user:SECRET_SENTINEL@fixture.invalid/v1")
    chooser = C.ReasoningClient(adapter)
    assert not chooser.available and chooser.decide("state", QUESTIONS).error
    assert not seen and "SECRET_SENTINEL" not in json.dumps(chooser.last_receipt)


def test_oversized_raw_output_is_retained_but_never_accepted_or_sliced():
    text = " " * C.MAX_OUTPUT_BYTES + json.dumps(GOOD)
    adapter, seen = model([text])
    chooser = C.ReasoningClient(adapter)
    assert "byte budget" in chooser.decide("state", QUESTIONS).error
    assert chooser.last_receipt["model_output"]["text"] == text
    assert len(seen) == 1


def test_cli_provider_is_unavailable_instead_of_offering_filesystem_agent_tools():
    config = M.ModelConfig("codex-cli", "", "model", "", retries=0)
    adapter = M.ModelAdapter(config)
    chooser = C.ReasoningClient(adapter)
    assert not chooser.available and chooser.decide("state", QUESTIONS).error
    assert adapter.call_count == 0


def test_token_cutoff_is_refused_even_if_partial_result_is_valid_json():
    adapter, seen = model([GOOD], finish_reason="length")
    chooser = C.ReasoningClient(adapter)
    assert "cut off" in chooser.decide("state", QUESTIONS).error
    assert len(seen) == 1


def test_default_factory_is_explicit_single_attempt_agent_role_without_inference(monkeypatch):
    adapter, seen = model([GOOD])
    calls = []
    def factory(role, **kwargs):
        calls.append((role, kwargs))
        return adapter
    monkeypatch.setattr(M.ModelAdapter, "for_role", factory)
    budget = M.RunBudget(.1)
    with C.ReasoningClient(budget=budget) as chooser:
        assert chooser.available
    assert not seen
    assert calls == [("agent", {"retries": 0, "timeout": 60, "max_tokens": 4096,
                                "feature": C.FEATURE, "budget": budget})]



def test_json_object_local_adapter_receives_complete_schema_and_accepts_exact_nested_answer():
    def inspect(payload):
        assert payload['response_format']=={'type':'json_object'}
        assert 'tools' not in payload and 'tool_choice' not in payload
        request=json.loads(payload['messages'][1]['content'])
        assert request=={'state':'state','questions':QUESTIONS,'output_schema':C._schema(QUESTIONS)}
        assert 'exact question IDs' in payload['messages'][0]['content']
        assert request['output_schema']['required']==['pick']
        answer=request['output_schema']['properties']['pick']
        assert answer['required']==['choice','confidence'] and answer['additionalProperties'] is False
        assert answer['properties']['choice']['enum']==list(QUESTIONS['pick']['criteria'])
    adapter,seen=model([GOOD],structured_output_mode='json_object',request_hook=inspect)
    chooser=C.ReasoningClient(adapter)
    answer=chooser.decide('state',QUESTIONS)
    assert not answer.error and answer.answers=={'pick':{'type':'choice',**GOOD['pick']}}
    assert len(seen)==chooser.last_call['model_calls']==1
    receipt=chooser.last_receipt
    assert receipt['messages']==seen[0]['messages']
    assert receipt['request']==json.loads(seen[0]['messages'][1]['content'])
    assert receipt['request_sha256']==C._hash(receipt['request'])
    assert receipt['schema_sha256']==C._hash(receipt['request']['output_schema'])
    assert receipt['messages_sha256']==C._hash(seen[0]['messages'])


def test_json_object_wrong_flat_answer_remains_abstained_without_repair_or_retry():
    wrong={'answer':'a0','confidence':1.0}
    adapter,seen=model([wrong,GOOD],structured_output_mode='json_object')
    chooser=C.ReasoningClient(adapter)
    answer=chooser.decide('state',QUESTIONS)
    assert answer.answers=={} and 'answer question mismatch' in answer.error
    assert len(seen)==chooser.last_call['model_calls']==1
    assert chooser.last_receipt['model_output']['text']==json.dumps(wrong)


def test_strict_schema_default_keeps_original_messages_and_logical_request():
    adapter,seen=model([GOOD]);chooser=C.ReasoningClient(adapter)
    assert not chooser.decide('state',QUESTIONS).error
    original={'state':'state','questions':QUESTIONS}
    assert seen[0]['messages']==[{'role':'system','content':C.SYSTEM},
        {'role':'user','content':C._json(original)}]
    assert chooser.last_receipt['request']==original
    assert 'output_schema' not in json.loads(seen[0]['messages'][1]['content'])
    assert seen[0]['response_format']['json_schema']['schema']==C._schema(QUESTIONS)


def test_json_object_complete_visible_input_budget_refuses_before_http():
    questions={f'q{i}':{'type':'choice','instructions':'i'*2048,
        'criteria':{f'label{j}':'d'*1024 for j in range(9)}} for i in range(4)}
    state='é'*9500
    assert len(C._json(C._input(state,questions)).encode()) <= C.MAX_REQUEST_BYTES
    adapter,seen=model([GOOD],structured_output_mode='json_object')
    chooser=C.ReasoningClient(adapter);mark=M.process_call_count()
    answer=chooser.decide(state,questions)
    assert answer.error and 'complete visible choice input' in answer.error
    assert not seen and chooser.last_call['model_calls']==M.calls_since(mark)==0
    receipt=chooser.last_receipt
    assert len(C._json(receipt['messages']).encode()) > C.MAX_REQUEST_BYTES
    assert receipt['request']['output_schema']==C._schema(questions)
    assert receipt['messages_sha256']==C._hash(receipt['messages'])


def test_json_object_multiple_questions_keep_unicode_labels_and_closed_schema():
    questions={'document_1':{'type':'choice','instructions':'Choose exact document action',
        'criteria':{'Änderung':'Apply a measured change','停止🌍':'Stop the operation','none':'No supported operation'}},
        'window_2':{'type':'choice','instructions':'Choose the observed window',
        'criteria':{'Fenêtre':'The measured foreground window','none':'No supported window'}}}
    reply={'document_1':{'choice':'停止🌍','confidence':.8},'window_2':{'choice':'Fenêtre','confidence':.9}}
    def inspect(payload):
        schema=json.loads(payload['messages'][1]['content'])['output_schema']
        assert schema==C._schema(questions) and schema['required']==list(questions)
        assert schema['additionalProperties'] is False
        for key,question in questions.items():
            branch=schema['properties'][key]
            assert branch['required']==['choice','confidence'] and branch['additionalProperties'] is False
            assert branch['properties']['choice']['enum']==list(question['criteria'])
        assert 'tools' not in payload and payload['response_format']=={'type':'json_object'}
    adapter,seen=model([reply],structured_output_mode='json_object',request_hook=inspect)
    chooser=C.ReasoningClient(adapter);answer=chooser.decide('Unicode state 🌍',questions)
    assert not answer.error and {key:value['choice'] for key,value in answer.answers.items()}=={
        'document_1':'停止🌍','window_2':'Fenêtre'}
    assert len(seen)==1



def test_json_object_legal_question_id_answer_keeps_its_nested_mapping():
    questions={'answer':copy.deepcopy(QUESTIONS['pick'])}
    reply={'answer':{'choice':'a0','confidence':1.0}}
    def inspect(payload):
        request=json.loads(payload['messages'][1]['content'])
        assert request['output_schema']['required']==['answer']
        assert 'flat answer/confidence object' in payload['messages'][0]['content']
        assert 'Do not return a top-level answer' not in payload['messages'][0]['content']
    adapter,seen=model([reply],structured_output_mode='json_object',request_hook=inspect)
    chooser=C.ReasoningClient(adapter);answer=chooser.decide('state',questions)
    assert not answer.error and answer.answers=={'answer':{'type':'choice',**reply['answer']}}
    assert len(seen)==chooser.last_call['model_calls']==1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
