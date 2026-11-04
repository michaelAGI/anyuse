"""Local typed-choice transport, tested without starting a model or container."""
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_laya as L
from anyuse.execution import rea_laya_worker as W
from anyuse.execution import model_adapter as M

QUESTIONS = {"pick": {"type": "choice", "instructions": "Choose the matching action",
                       "criteria": {"a0": "Pause playback", "none": "No suitable action"}}}
ANSWER = {"pick": {"type": "choice", "choice": "a0", "confidence": .91,
                    "answer_confidence": .98, "probabilities": {"a0": .98, "none": .02}}}


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "assets"
    (root / "wheels").mkdir(parents=True)
    for f in ("model.safetensors", "rl_agent_config.json", "encoder/config.json",
              "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"):
        path = root / "models" / "typed" / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    return root


class Transport:
    def __init__(self, mutate=None, timeout=False):
        self.commands = []
        self.processes = []
        self.mutate = mutate
        self.timeout = timeout
        self.reads = 0

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(stdout=json.dumps([{"configuration": {
                "name": "docker.io/library/python:3.12-slim", "descriptor": {
                    "digest": "sha256:" + "1" * 64}}}]).encode())
        return SimpleNamespace(returncode=0)

    def popen(self, cmd, **kwargs):
        self.commands.append(cmd)
        assert kwargs["stderr"] == L.subprocess.DEVNULL
        proc = SimpleNamespace(stdin=io.BytesIO(), waited=False, killed=False)
        def wait(timeout):
            proc.waited = True
            return 0
        proc.wait = wait
        proc.kill = lambda: setattr(proc, "killed", True)
        self.processes.append(proc)
        return proc

    def read(self, proc, timeout, cap):
        self.reads += 1
        if self.reads == 1:
            return W.encode({"ready": True, "metadata": {
                "protocol": W.PROTOCOL, "checkpoint": "typed", "laya_version": "0.3.20",
                "warmup_calls": 0, "device": "cpu", "model_files": {}}})
        if self.timeout:
            raise TimeoutError("fixture timeout")
        request = json.loads(proc.stdin.getvalue().splitlines()[-1])
        reply = {"protocol": W.PROTOCOL, "id": request["id"],
                 "request_sha256": W.digest(request), "answers": copy.deepcopy(ANSWER),
                 "token_budget": {"truncated": False}, "inference_calls": 1,
                 "server_ms": 4, "usage": {"input_tokens": 32}}
        if self.mutate:
            self.mutate(reply)
        return W.encode(reply)


def client(assets, transport):
    return L.LayaClient(assets, container="container-fixture", popen=transport.popen,
                        runner=transport.run, line_reader=transport.read,
                        writer=lambda proc, data, timeout: proc.stdin.write(data))


def test_typed_choice_preserves_actual_answer_and_receipt_then_closes_owned_runtime(assets):
    transport = Transport()
    with client(assets, transport) as chooser:
        answer = chooser.decide({"task": "Pause playback"}, QUESTIONS)
        assert not answer.error and answer.answers == ANSWER
        assert answer.input_tokens == 32 and answer.server_ms == 4
        assert chooser.last_receipt["response"]["inference_calls"] == 1
        assert chooser.metadata["worker_sha256"]
        assert len(transport.processes) == 1
        assert chooser.decide("same task", QUESTIONS).answers == ANSWER
        assert len(transport.processes) == 1
    assert transport.processes[0].waited
    assert chooser.cleanup["confirmed"] is True
    assert chooser.last_receipt["cleanup"]["stop_returncode"] == 0
    assert transport.commands[-1][1] == "stop"
    assert transport.commands[-1][2].startswith("anyuse-rea-laya-")


def test_runtime_uses_exact_custom_network_readonly_assets_cached_digest_and_no_credentials(assets):
    transport = Transport()
    with client(assets, transport) as chooser:
        assert not chooser.decide("state", QUESTIONS).error
    cmd = transport.commands[1]
    assert cmd[cmd.index("--network") + 1] == "anyuse-net"
    assert "--no-dns" in cmd and "--publish" not in cmd and "--ssh" not in cmd
    assert "readonly" in cmd[cmd.index("--mount") + 1]
    assert "docker.io/library/python@sha256:" + "1" * 64 in cmd
    assert "--no-index" in cmd[-4] and "laya==0.3.20" in cmd[-4]
    assert all("API_KEY" not in part for part in cmd)


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(id="0" * 32),
    lambda r: r.update(request_sha256="0" * 64),
    lambda r: r["answers"]["pick"].update(choice="unoffered"),
    lambda r: r["answers"]["pick"].update(confidence=1.1),
    lambda r: r["answers"]["pick"]["probabilities"].update(a0=.1),
    lambda r: r.update(token_budget={"truncated": True}),
    lambda r: r.update(inference_calls=0),
])
def test_malformed_or_unbound_response_is_abstention_and_cleanup(assets, mutation):
    transport = Transport(mutate=mutation)
    chooser = client(assets, transport)
    answer = chooser.decide("state", QUESTIONS)
    assert answer.error and answer.answers == {} and chooser.proc is None
    assert transport.processes[0].waited


def test_timeout_abstains_and_cleans_owned_runtime(assets):
    transport = Transport(timeout=True)
    chooser = client(assets, transport)
    mark = M.process_call_count()
    assert "TimeoutError" in chooser.decide("state", QUESTIONS).error
    assert chooser.proc is None and transport.processes[0].waited
    assert M.calls_since(mark) == 1
    assert chooser.last_call == {"model_calls": 1, "inference_confirmed": False}


def test_confirmed_worker_preflight_zero_is_not_counted_as_inference(assets):
    transport = Transport(mutate=lambda r: r.update(error="state would be truncated", inference_calls=0))
    chooser = client(assets, transport)
    mark = M.process_call_count()
    assert chooser.decide("state", QUESTIONS).error
    assert M.calls_since(mark) == 0
    assert chooser.last_call == {"model_calls": 0, "inference_confirmed": True}


def test_request_pipe_timeout_is_bounded_abstention_with_uncertain_dispatch(assets):
    transport = Transport()
    chooser = client(assets, transport)
    def timeout(proc, data, seconds):
        raise TimeoutError("fixture pipe timeout")
    chooser._writer = timeout
    assert "TimeoutError" in chooser.decide("state", QUESTIONS).error
    assert chooser.last_call == {"model_calls": 1, "inference_confirmed": False}
    assert chooser.cleanup["confirmed"] is True and chooser.proc is None


def test_successful_inference_counts_once_per_request(assets):
    transport = Transport()
    with client(assets, transport) as chooser:
        mark = M.process_call_count()
        assert not chooser.decide("state", QUESTIONS).error
        assert M.calls_since(mark) == 1
        assert chooser.last_call == {"model_calls": 1, "inference_confirmed": True}


def test_cleanup_failure_remains_explicit_despite_client_process_exit(assets):
    transport = Transport()
    original = transport.run
    def fail_stop(cmd, **kwargs):
        if cmd[1] == "stop":
            return SimpleNamespace(returncode=1)
        return original(cmd, **kwargs)
    transport.run = fail_stop
    chooser = client(assets, transport)
    assert not chooser.decide("state", QUESTIONS).error
    chooser.close()
    assert chooser.cleanup["process_exited"] is True
    assert chooser.cleanup["confirmed"] is False
    assert chooser.cleanup["stop_returncode"] == 1


def test_registered_settings_are_read_without_environment_variables(assets, monkeypatch):
    settings = {L.DIR_ENV: str(assets), L.CHECKPOINT_ENV: "typed",
                L.CONTAINER_ENV: "configured-container", L.IMAGE_ENV: "configured-image"}
    monkeypatch.setattr(L.O, "_setting", lambda name, default="": settings.get(name, default))
    chooser = L.LayaClient()
    assert chooser.available and chooser.cli == "configured-container"
    assert chooser.image == "configured-image"


@pytest.mark.parametrize("state,questions,kwargs", [
    ("s" * 32769, QUESTIONS, {}),
    ("state", {"pick": {"type": "noul", "instructions": "Is it?"}}, {}),
    ("state", QUESTIONS, {"images": [b"image"]}),
    ("state", QUESTIONS, {"steps": 2}),
    ("state", QUESTIONS, {"guard_obs": {"private": "unsupported"}}),
])
def test_invalid_input_never_starts_model_runtime(assets, state, questions, kwargs):
    transport = Transport()
    assert client(assets, transport).decide(state, questions, **kwargs).error
    assert transport.commands == [] and transport.reads == 0


def test_missing_assets_abstains_without_container_access(tmp_path):
    transport = Transport()
    chooser = client(tmp_path, transport)
    assert not chooser.available and chooser.decide("state", QUESTIONS).error
    assert transport.commands == []


def test_eight_actions_plus_none_preserve_ninth_option_and_last_action_mapping(assets):
    criteria = {"a" + str(i): "Action " + str(i) for i in range(8)}
    criteria["none"] = "No matching action"
    questions = {"pick": {"type": "choice", "instructions": "Choose the matching action",
                           "criteria": criteria}}
    # Choosing a7 also checks that abstention does not replace/drop the eighth action.
    answer = {"pick": {"type": "choice", "choice": "a7", "confidence": 1.,
                       "answer_confidence": 1., "probabilities": {
                           k: float(k == "a7") for k in criteria}}}
    transport = Transport(mutate=lambda r: r.update(answers=answer))
    with client(assets, transport) as chooser:
        result = chooser.decide("The task requires Action7", questions)
        assert not result.error and result.answers == answer
    assert len(result.answers["pick"]["probabilities"]) == 9
    agent = SimpleNamespace(tok=Tokenizer(), cfg={"max_len": 256, "head_max_len": 64})
    budget = W.token_budget(agent, "The task requires Action7", questions)
    assert len(budget["questions"]["pick"]["option_tokens"]) == 9
    assert budget["truncated"] is False


def test_ten_options_refuse_before_runtime_and_nine_verbose_options_still_refuse_truncation(assets):
    questions = {"pick": {"type": "choice", "instructions": "Choose an action",
                           "criteria": {"a" + str(i): "Action " + str(i) for i in range(10)}}}
    transport = Transport()
    assert client(assets, transport).decide("state", questions).error
    assert transport.commands == []
    questions["pick"]["criteria"].pop("a9")
    questions["pick"]["criteria"] = {k: "word " * 30 for k in questions["pick"]["criteria"]}
    agent = SimpleNamespace(tok=Tokenizer(), cfg={"max_len": 1024, "head_max_len": 256})
    with pytest.raises(ValueError, match="options would be truncated"):
        W.token_budget(agent, "state", questions)


class Tokenizer:
    mask_token = "[MASK]"
    def __call__(self, value, **kwargs):
        assert kwargs == {"add_special_tokens": False}
        return {"input_ids": list(range(len(value.split())))}


def test_token_budget_accepts_complete_input_and_reports_actual_lengths():
    agent = SimpleNamespace(tok=Tokenizer(), cfg={"max_len": 64, "head_max_len": 32})
    budget = W.token_budget(agent, "one two three", QUESTIONS)
    assert budget["truncated"] is False
    assert budget["questions"]["pick"]["state_tokens"] == 3
    assert budget["questions"]["pick"]["sequence_tokens"] < 64


@pytest.mark.parametrize("location", ["state", "instructions", "option", "options", "mask"])
def test_sdk_silent_truncation_or_input_replacement_is_refused(location):
    agent = SimpleNamespace(tok=Tokenizer(), cfg={"max_len": 64, "head_max_len": 32})
    questions = copy.deepcopy(QUESTIONS)
    state = "state"
    if location == "state":
        state = "word " * 65
    elif location == "instructions":
        questions["pick"]["instructions"] = "word " * 32
    elif location == "option":
        questions["pick"]["criteria"]["a0"] = "word " * 49
    elif location == "options":
        questions["pick"]["criteria"] = {str(i): "several option words" for i in range(8)}
    else:
        state = "literal [MASK]"
    with pytest.raises(ValueError, match="truncated|mask token"):
        W.token_budget(agent, state, questions)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
