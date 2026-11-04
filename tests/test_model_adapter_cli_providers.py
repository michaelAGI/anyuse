"""The claude-cli and codex-cli providers, against an injected fake runner (no CLI is ever started)."""
import json
import subprocess

import sys

import pytest

from anyuse.execution import model_adapter as M
from anyuse.execution.model_adapter import ModelAdapter, RunBudget, resolve_model_config


class FakeRunner:
    def __init__(self, rc=0, out="", err="", raise_exc=None, write_last=None):
        self.rc, self.out, self.err, self.raise_exc, self.write_last = rc, out, err, raise_exc, write_last
        self.calls = []

    def __call__(self, argv, stdin, cwd, timeout):
        self.calls.append({"argv": list(argv), "stdin": stdin, "cwd": cwd, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.write_last is not None:
            path = argv[argv.index("--output-last-message") + 1]
            with open(path, "w") as f:
                f.write(self.write_last)
        return self.rc, self.out, self.err


def adapter(provider, runner, model=None):
    overrides = {"provider": provider}
    if model is not None:
        overrides["model"] = model
    return ModelAdapter(resolve_model_config("planner", **overrides), cli_runner=runner,
                        budget=RunBudget("unlimited"))


MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "SECRET-PROMPT plan it"}]
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_explicit_http_reasoning_effort_is_refused_before_any_cli_call():
    before = M.process_call_count()
    for provider in M.CLI_PROVIDERS:
        runner = FakeRunner()
        assert resolve_model_config("agent", provider=provider).reasoning_effort is None
        with pytest.raises(ValueError, match="HTTP providers"):
            ModelAdapter.for_role("agent", provider=provider, reasoning_effort="none", cli_runner=runner)
        with pytest.raises(ValueError, match="HTTP providers"):
            M.ModelConfig(provider, "", "fake", "", reasoning_effort="high")
        assert runner.calls == []
    assert M.process_call_count() == before


def claude_json(**fields):
    base = {"type": "result", "is_error": False, "result": "plain answer", "total_cost_usd": 0.0125,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}}
    base.update(fields)
    return json.dumps(base)


def test_claude_cli_prompt_on_stdin_with_schema_and_model():
    runner = FakeRunner(out=claude_json(structured_output={"ok": True}))
    result = adapter("claude-cli", runner, model="opus").complete(MESSAGES, json_schema=SCHEMA)
    call = runner.calls[0]
    assert call["argv"][:4] == ["claude", "-p", "--model", "opus"]
    assert "--tools" in call["argv"] and "--safe-mode" in call["argv"]
    assert call["argv"][call["argv"].index("--json-schema") + 1] == json.dumps(SCHEMA)
    assert call["argv"][call["argv"].index("--system-prompt") + 1] == "be brief"
    assert not any("SECRET-PROMPT" in a for a in call["argv"]) and "SECRET-PROMPT" in call["stdin"]
    assert call["timeout"] >= M.CLI_TIMEOUT_S
    assert result.ok and result.parsed == {"ok": True} and result.llm_calls == 1
    assert result.usage.prompt_tokens == 15 and result.usage.completion_tokens == 7
    assert result.usage.cost_usd == pytest.approx(0.0125)


def test_claude_cli_plain_result_and_default_model():
    runner = FakeRunner(out=claude_json())
    result = adapter("claude-cli", runner).complete(MESSAGES)
    assert runner.calls[0]["argv"][3] == "sonnet"
    assert result.ok and result.text == "plain answer"


def test_claude_cli_image_goes_through_stream_json():
    stream = "\n".join([json.dumps({"type": "system"}), claude_json(result="saw it")])
    runner = FakeRunner(out=stream)
    url = M.image_data_url(b"\x89PNG fake", "image/png")
    result = adapter("claude-cli", runner).complete(MESSAGES, images=[url])
    call = runner.calls[0]
    argv = call["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    message = json.loads(call["stdin"])
    blocks = message["message"]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1] == {"type": "text", "text": "SECRET-PROMPT plan it"}
    assert result.ok and result.text == "saw it"


def test_missing_binary_is_not_an_attempt():
    runner = FakeRunner(raise_exc=FileNotFoundError("claude"))
    before = M.process_call_count()
    result = adapter("claude-cli", runner).complete(MESSAGES)
    assert not result.ok and result.llm_calls == 0 and result.provider_unreachable
    assert "not found" in result.error and M.process_call_count() == before


def test_nonzero_exit_and_cli_error_are_errors():
    result = adapter("claude-cli", FakeRunner(rc=1, out="", err="logged out")).complete(MESSAGES)
    assert not result.ok and result.llm_calls == 1 and "exited 1" in result.error
    result = adapter("claude-cli", FakeRunner(out=claude_json(is_error=True))).complete(MESSAGES)
    assert not result.ok and result.provider_unreachable


def test_timeout_counts_one_attempt_and_never_raises():
    runner = FakeRunner(raise_exc=subprocess.TimeoutExpired(["claude"], 1))
    result = adapter("claude-cli", runner).complete(MESSAGES)
    assert not result.ok and result.llm_calls == 1 and "TimeoutExpired" in result.error


def test_codex_cli_reads_the_last_message_and_uses_the_output_schema():
    runner = FakeRunner(write_last='```json\n{"ok": true}\n```')
    result = adapter("codex-cli", runner).complete(MESSAGES, json_schema=SCHEMA)
    argv = runner.calls[0]["argv"]
    assert argv[:2] == ["codex", "exec"] and "-s" in argv and argv[argv.index("-s") + 1] == "read-only"
    assert "--ignore-user-config" in argv and argv[-1] == "-" and "-m" not in argv
    assert "--output-schema" in argv
    assert not any("SECRET-PROMPT" in a for a in argv)
    assert runner.calls[0]["stdin"].startswith("be brief") and "SECRET-PROMPT" in runner.calls[0]["stdin"]
    assert result.ok and result.parsed == {"ok": True} and result.llm_calls == 1


def test_codex_cli_model_and_images_refused():
    runner = FakeRunner(write_last="fine")
    assert adapter("codex-cli", runner, model="o4-mini").complete(MESSAGES).ok
    assert runner.calls[0]["argv"][runner.calls[0]["argv"].index("-m") + 1] == "o4-mini"
    refused = adapter("codex-cli", FakeRunner()).complete(MESSAGES, images=[M.image_data_url(b"x")])
    assert not refused.ok and refused.llm_calls == 0 and "text only" in refused.error


def test_provider_selected_from_environment(monkeypatch):
    monkeypatch.setenv("ANYUSE_MODEL_PROVIDER", "claude-cli")
    cfg = resolve_model_config("agent")
    assert cfg.provider == "claude-cli" and cfg.model == "sonnet" and cfg.api_key_env == ""
    assert M.credential_status(cfg) == "not required"
    monkeypatch.setenv("ANYUSE_PLANNER_MODEL_PROVIDER", "codex-cli")
    assert resolve_model_config("planner").provider == "codex-cli"
    monkeypatch.setenv("ANYUSE_MODEL_NAME", "haiku")
    assert resolve_model_config("judge").model == "haiku"


def test_the_default_run_budget_fits_several_sonnet_class_calls(monkeypatch):
    monkeypatch.delenv("ANYUSE_RUN_BUDGET_USD", raising=False)
    assert M.DEFAULT_RUN_BUDGET_USD >= 0.5          # one Sonnet-class decision is about US$0.03
    for provider in ("claude-cli", "local"):
        adapter = M.ModelAdapter(M.ModelConfig(provider=provider, base_url="http://127.0.0.1:9/v1",
                                               model="sonnet", api_key_env=None))
        assert adapter.budget.limit_usd == M.DEFAULT_RUN_BUDGET_USD
    budget = M.RunBudget()
    reservations = [budget.reserve(600, 3.0, 15.0) for _ in range(20)]   # a Sonnet-priced decision, 20 times
    assert all(reservations), "the default budget refused a multi-step run"
    cli = M.ModelAdapter(M.ModelConfig(provider="claude-cli", base_url="", model="sonnet", api_key_env=None))
    monkeypatch.setenv("ANYUSE_RUN_BUDGET_USD", "0.5")
    assert M.ModelAdapter(M.ModelConfig(provider="claude-cli", base_url="", model="sonnet",
                                        api_key_env=None)).budget.limit_usd == 0.5



def test_openai_and_anthropic_presets_are_plug_and_play(monkeypatch):
    for k in ("ANYUSE_MODEL_NAME", "ANYUSE_MODEL_BASE_URL", "ANYUSE_MODEL_API_KEY_ENV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANYUSE_MODEL_PROVIDER", "anthropic")
    cfg = resolve_model_config("agent")
    assert cfg.base_url == "https://api.anthropic.com/v1/" and cfg.api_key_env == "ANTHROPIC_API_KEY"
    assert cfg.model == "claude-sonnet-5-5"
    monkeypatch.setenv("ANYUSE_MODEL_PROVIDER", "openai")
    with pytest.raises(ValueError, match="set ANYUSE_MODEL_NAME"):
        resolve_model_config("agent")
    monkeypatch.setenv("ANYUSE_MODEL_NAME", "some-model")
    cfg = resolve_model_config("agent")
    assert cfg.base_url == "https://api.openai.com/v1" and cfg.api_key_env == "OPENAI_API_KEY"


def test_settings_accept_every_adapter_preset():
    from anyuse import settings as ST
    assert set(ST.MODEL_PROVIDERS) - {""} == set(M.PROVIDER_PRESETS)
    for name in M.PROVIDER_PRESETS:
        assert ST._model_provider(name, True) == name
    with pytest.raises(ST.SettingError):
        ST._model_provider("nope", True)

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
