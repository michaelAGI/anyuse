"""The commands a new user meets in the first hour, and what they say when something is missing.

Offline: a temp data directory, a fake transport for the compiled request, a stand-in pyautogui that
records calls and sends nothing. No network, no desktop, no model call.
"""
import importlib.metadata
import json
import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from anyuse.cli import cli
from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution.api_permission import acknowledge_host, host_acknowledged, load_hosts
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.mcpserver import compiled_tools as CT

BODY = '{"results":[{"name":"amber"},{"name":"amber ale"},{"name":"amberjack"}]}'
HOST = "127.0.0.1:28601"
#: run_compiled as shipped, before a test puts the fake transport behind it
RUN_COMPILED = CT.run_compiled

MODEL_ENV = ["ANYUSE_MODEL_PROVIDER", "ANYUSE_MODEL_BASE_URL", "ANYUSE_MODEL_NAME", "ANYUSE_MODEL_API_KEY_ENV",
             "GEMINI_API_KEY", "CEREBRAS_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
             "ANYUSE_DEMO_MODEL"]
MODEL_ENV += [f"ANYUSE_{role}_MODEL_{kind}" for role in ("PLANNER", "VISUAL", "AGENT", "JUDGE")
              for kind in ("PROVIDER", "BASE_URL", "NAME", "API_KEY_ENV")]


class FakeTransport:
    def __init__(self, raises=None):
        self.raises = raises
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        if self.raises:
            raise self.raises
        return ApiResponse(200, {"Content-Type": "application/json"}, BODY)


@pytest.fixture
def home(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(data))
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(data / "api_hosts.json"))
    monkeypatch.setenv("ANYUSE_MODEL_ENV_FILE", os.devnull)
    # the fixture host is loopback, which the pre-send policy allows only as a named offline fixture
    monkeypatch.setenv("ANYUSE_LOCAL_FIXTURE_ORIGINS", f"https://{HOST}")
    for name in MODEL_ENV + ["ANYUSE_MCP_PROFILE", "ANYUSE_DRIVE_ENABLED"]:
        monkeypatch.delenv(name, raising=False)
    return data


def _storage():
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage()


def _tool(name="s1", param="q", folder=None):
    """A proved read-only web tool on the local fixture host, as `workflow compile` saves it (in
    workflow_<folder> when it is compiled for a recorded workflow, else in workflow_<name>)."""
    template = RequestTemplate(
        method="GET", url=f"https://{HOST}/api/search", query={"q": "{%s}" % param},
        headers={"accept": "application/json"}, extract={"items": "results[*].name"},
        expect={"status_in": [200], "content_type": "json", "path": "results", "min_items": 1,
                "required_keys": ["name"]},
        params=[param])
    proof = {"passed": True, "status": "proved", "passed_count": 2, "case_count": 2,
             "reason": "passed on 2 fresh value(s) not present in the recording"}
    return persist_compiled(_storage(), folder or name, compiled_record(template, proof, workflow=name))


def _desktop(name, wid):
    """A saved desktop workflow, as `workflow record` saves it (its folder is workflow_<id>)."""
    from anyuse.memory.models import Workflow
    _storage().save(Workflow(id=wid, name=name))


def _offline(monkeypatch, transport):
    """`workflow play` runs the real run_compiled with the fake transport behind the compiled request."""
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: RUN_COMPILED(
        name, bound, executor=ApiStepExecutor(transport=transport), **kw))


def _listed():
    res = CliRunner().invoke(cli, ["workflow", "list", "--json"])
    assert res.exit_code == 0, res.output
    return [row["name"] for row in json.loads(res.stdout)]


# ---------------------------------------------------------------------------
# credential use obeys the drive switch
# ---------------------------------------------------------------------------

def _stand_in_pyautogui(monkeypatch, tmp_path):
    from anyuse.credentials import vault as V
    calls = []

    class PG:
        @staticmethod
        def write(value, interval=0.0):
            calls.append(("write", len(value)))

        @staticmethod
        def press(name):
            calls.append(("press", name))

        @staticmethod
        def hotkey(*names):
            calls.append(("hotkey", names))
    monkeypatch.setattr(V, "pyautogui", PG)
    monkeypatch.setattr(V, "PYAUTOGUI_AVAILABLE", True)
    monkeypatch.setattr(V.CredentialVault, "TYPING_SUPPRESSION_FLAG", tmp_path / ".typing_suppression")
    monkeypatch.setattr(V.CredentialVault, "list_aliases", lambda self: ["demo"])
    monkeypatch.setattr(V.CredentialVault, "get_credential", lambda self, alias: "secret")
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return calls


def test_credential_use_types_nothing_while_desktop_input_is_off(home, monkeypatch, tmp_path):
    calls = _stand_in_pyautogui(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli, ["credential", "use", "demo", "--enter"])
    assert res.exit_code != 0, res.output
    assert calls == [], calls
    assert "ANYUSE_DRIVE_ENABLED=on anyuse credential use demo" in res.output, res.output
    assert "Credential typed" not in res.output


def test_the_vault_itself_refuses_to_type_while_desktop_input_is_off(home, monkeypatch, tmp_path):
    from anyuse.credentials.vault import CredentialVault
    calls = _stand_in_pyautogui(monkeypatch, tmp_path)
    out = CredentialVault(use_keychain=False).use_credential("demo", press_enter=True)
    assert out["success"] is False and "ANYUSE_DRIVE_ENABLED" in out["error"], out
    assert calls == []
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "on")
    out = CredentialVault(use_keychain=False).use_credential("demo", press_enter=True)
    assert out["success"] is True and calls == [("write", 6), ("press", "return")], (out, calls)


# ---------------------------------------------------------------------------
# delete, rename and export find the compiled web tools the listing shows
# ---------------------------------------------------------------------------

def test_delete_removes_a_compiled_web_tool_and_an_unknown_name_fails(home):
    path = _tool("s1")
    assert _listed() == ["s1"]
    res = CliRunner().invoke(cli, ["workflow", "delete", "s1", "-y"])
    assert res.exit_code == 0 and "deleted" in res.output, res.output
    assert not os.path.exists(os.path.dirname(path)) and _listed() == []
    res = CliRunner().invoke(cli, ["workflow", "delete", "s1", "-y"])
    assert res.exit_code != 0 and "not found" in res.output, res.output


def test_delete_keeps_the_saved_workflow_whose_folder_holds_the_compiled_tool(home):
    _desktop("other", "d0d0")
    path = _tool("s1", folder="d0d0")
    assert sorted(_listed()) == ["other", "s1"]
    res = CliRunner().invoke(cli, ["workflow", "rename", "s1", "s2"])
    assert res.exit_code != 0 and "kept with another saved workflow" in res.output, res.output
    res = CliRunner().invoke(cli, ["workflow", "delete", "s1", "-y"])
    assert res.exit_code == 0, res.output
    assert not os.path.exists(path) and os.path.isfile(os.path.join(os.path.dirname(path), "workflow.json"))
    assert _listed() == ["other"]


def test_rename_moves_a_compiled_web_tool_and_it_still_runs(home, monkeypatch):
    _tool("s1")
    res = CliRunner().invoke(cli, ["workflow", "rename", "s1", "pkg_search"])
    assert res.exit_code == 0, res.output
    assert _listed() == ["pkg_search"]
    acknowledge_host(HOST)
    tr = FakeTransport()
    out = CT.run_compiled("pkg_search", {"q": "amber"}, executor=ApiStepExecutor(transport=tr))
    assert out["ok"] is True, out
    assert sorted(os.listdir(home / "workflows")) == ["workflow_pkg_search"]
    _tool("other")
    res = CliRunner().invoke(cli, ["workflow", "rename", "pkg_search", "other"])
    assert res.exit_code != 0 and "exists already" in res.output, res.output


def test_export_writes_a_compiled_web_tool_and_an_unknown_name_fails(home, tmp_path):
    _tool("s1")
    target = tmp_path / "s1.json"
    res = CliRunner().invoke(cli, ["workflow", "export", "s1", str(target)])
    assert res.exit_code == 0, res.output
    record = json.loads(target.read_text())
    assert record["workflow"] == "s1" and record["template"]["url"] == f"https://{HOST}/api/search"
    res = CliRunner().invoke(cli, ["workflow", "export", "nope", str(tmp_path / "nope.json")])
    assert res.exit_code != 0 and "not found" in res.output, res.output
    assert not (tmp_path / "nope.json").exists()


# ---------------------------------------------------------------------------
# a model kept in settings.toml counts as configured
# ---------------------------------------------------------------------------

def test_a_model_set_with_the_settings_command_counts_as_configured(home):
    import anyuse.cli as C
    assert C._model_configured() is False
    res = CliRunner().invoke(cli, ["settings", "set", "ANYUSE_MODEL_PROVIDER", "claude-cli"])
    assert res.exit_code == 0, res.output
    assert "ANYUSE_MODEL_PROVIDER" not in os.environ
    assert C._model_configured() is True


def test_a_role_model_set_with_the_settings_command_counts_as_configured(home):
    import anyuse.cli as C
    from anyuse import settings
    settings.set_value("ANYUSE_PLANNER_MODEL_PROVIDER", "codex-cli")
    assert C._model_configured() is True


# ---------------------------------------------------------------------------
# agent run with no model: the no-key route, and the meta listing says it is not ready
# ---------------------------------------------------------------------------

def test_agent_run_without_a_model_names_the_no_key_route(home):
    from anyuse.execution import harness_product as HP
    out = HP.run_agent_task("find the price of tea")
    assert out["ok"] is False and out["model_calls"] == 0, out
    assert "claude-cli" in out["reason"] and "CEREBRAS_API_KEY" not in out["reason"], out["reason"]
    assert out["refusal"]["kind"] == "no_model"
    res = CliRunner().invoke(cli, ["agent", "run", "find the price of tea"])
    assert res.exit_code == 1, res.output
    assert "ANYUSE_MODEL_PROVIDER=claude-cli" in res.output and "CEREBRAS_API_KEY" not in res.output, res.output


def test_mcp_agent_run_without_a_model_names_the_no_key_route(home):
    from anyuse.mcpserver import agent_tools as AT
    out = AT.handle({"task": "find the price of tea"}, wait_s=30)
    assert out["ok"] is False, out
    assert "claude-cli" in out["reason"] and "CEREBRAS_API_KEY" not in out["reason"], out


def test_the_meta_listing_says_the_agent_is_ready_only_with_a_model(home, monkeypatch):
    from anyuse.mcpserver import meta_tools as MT
    row = MT.agent_operation().row
    assert row["ready"] is False and "claude-cli" in row["why_not"], row
    monkeypatch.setenv("CEREBRAS_API_KEY", "not-a-real-key")
    row = MT.agent_operation().row
    assert row["ready"] is True and "why_not" not in row, row


# ---------------------------------------------------------------------------
# acknowledge-host takes a URL
# ---------------------------------------------------------------------------

def test_acknowledge_host_stores_the_host_and_port_of_a_url(home):
    res = CliRunner().invoke(cli, ["workflow", "acknowledge-host", f"https://{HOST}/", "--note", "mine"])
    assert res.exit_code == 0, res.output
    assert host_acknowledged(HOST), load_hosts()
    assert list(load_hosts()) == [HOST]
    assert f"✓ {HOST} acknowledged" in res.output, res.output
    assert f"(the host and port of https://{HOST}/)" in res.output, res.output
    res = CliRunner().invoke(cli, ["workflow", "acknowledge-host", "https://Crates.IO/search?q=x"])
    assert res.exit_code == 0 and host_acknowledged("crates.io"), (res.output, load_hosts())
    assert "(the host of https://Crates.IO/search?q=x)" in res.output, res.output
    res = CliRunner().invoke(cli, ["workflow", "acknowledge-host", "example.org"])
    assert res.exit_code == 0 and host_acknowledged("example.org")
    # an empty port is the default port: the key is the bare host the tool's URL gives
    res = CliRunner().invoke(cli, ["workflow", "acknowledge-host", "https://example.net:/"])
    assert res.exit_code == 0 and host_acknowledged("example.net"), (res.output, load_hosts())


def test_acknowledge_host_refuses_what_is_not_a_host(home):
    for bad in ("ftp://files.example/", "https://", "https://host:notaport/", "two words"):
        res = CliRunner().invoke(cli, ["workflow", "acknowledge-host", bad])
        assert res.exit_code == 2, (bad, res.output)
        assert "✓" not in res.output, (bad, res.output)
    assert load_hosts() == {}


# ---------------------------------------------------------------------------
# workflow play: the hint uses the tool's parameters, a typo says unknown parameter
# ---------------------------------------------------------------------------

def test_the_list_hint_names_each_tools_own_parameter(home):
    _tool("pkg", param="term")
    res = CliRunner().invoke(cli, ["workflow", "list"])
    assert "anyuse workflow play pkg --param term=VALUE" in res.output, res.output
    assert "--param q=VALUE" not in res.output


def test_an_extra_parameter_is_an_unknown_parameter_not_a_safety_block(home, monkeypatch):
    _tool("s1")
    acknowledge_host(HOST)
    tr = FakeTransport()
    out = CT.run_compiled("s1", {"q": "maple", "x": "1"}, executor=ApiStepExecutor(transport=tr))
    assert out["ok"] is False and "unknown parameter 'x'" in out["error"], out
    assert "destructive" not in out["error"] and out["parameters"] == ["q"] and tr.sent == []
    _offline(monkeypatch, tr)
    res = CliRunner().invoke(cli, ["workflow", "play", "s1", "--param", "q=maple", "--param", "x=1",
                                   "--no-fallback"])
    assert res.exit_code == 1, res.output
    assert "unknown parameter 'x'" in res.output and "destructive" not in res.output, res.output
    assert "record the search again" not in res.output
    assert "anyuse workflow play s1 --param q=VALUE" in res.output, res.output


def test_a_wrong_parameter_name_says_which_one_the_tool_takes(home, monkeypatch):
    _tool("pkg", param="term")
    acknowledge_host(HOST)
    _offline(monkeypatch, FakeTransport())
    res = CliRunner().invoke(cli, ["workflow", "play", "pkg", "--param", "q=amber", "--no-fallback"])
    assert res.exit_code == 1, res.output
    assert "unknown parameter 'q'" in res.output and "record the search again" not in res.output, res.output
    assert "anyuse workflow play pkg --param term=VALUE" in res.output, res.output


def test_a_typed_mcp_tool_names_an_unknown_parameter(home):
    _tool("s1")
    acknowledge_host(HOST)
    tr = FakeTransport()
    tool = CT.executable_typed_tools()[0]
    out = CT.run_typed_tool(tool["name"], {"q": "maple", "x": "1"}, executor=ApiStepExecutor(transport=tr))
    assert out["ok"] is False and "unknown parameter 'x'" in out["error"], out
    assert tr.sent == []


def test_record_again_is_not_the_next_step_for_a_host_or_a_site_that_is_down(home, monkeypatch):
    _tool("s1")
    _offline(monkeypatch, FakeTransport())
    res = CliRunner().invoke(cli, ["workflow", "play", "s1", "--param", "q=amber", "--no-fallback"])
    assert res.exit_code == 1 and "not acknowledged" in res.output, res.output
    assert "record the search again" not in res.output
    assert f"anyuse workflow acknowledge-host {HOST}" in res.output
    acknowledge_host(HOST)
    _offline(monkeypatch, FakeTransport(raises=ConnectionRefusedError(61, "Connection refused")))
    res = CliRunner().invoke(cli, ["workflow", "play", "s1", "--param", "q=amber", "--no-fallback"])
    assert res.exit_code == 1 and "Connection refused" in res.output, res.output
    assert "record the search again" not in res.output and "did not answer" in res.output, res.output


def test_a_destination_the_send_policy_refuses_is_neither_down_nor_changed(home, monkeypatch):
    _tool("s1")
    acknowledge_host(HOST)
    play = ["workflow", "play", "s1", "--param", "q=amber", "--no-fallback"]
    # the real transport raises this before anything is sent; the executor reports it as a transport error
    refused = PermissionError(f"pre-send policy refused request: nonpublic destination '127.0.0.1' is refused by "
                              f"default; for an offline release fixture, set ANYUSE_LOCAL_FIXTURE_ORIGINS to this "
                              f"exact origin")
    _offline(monkeypatch, FakeTransport(raises=refused))
    res = CliRunner().invoke(cli, play)
    assert res.exit_code == 1 and "ANYUSE_LOCAL_FIXTURE_ORIGINS" in res.output, res.output
    assert "did not answer" not in res.output and "record the search again" not in res.output, res.output
    unresolved = PermissionError("pre-send policy refused request: destination address resolution failed closed: "
                                 "gaierror: [Errno 8] nodename nor servname provided, or not known")
    _offline(monkeypatch, FakeTransport(raises=unresolved))
    res = CliRunner().invoke(cli, play)
    assert res.exit_code == 1 and "did not answer" in res.output, res.output


# ---------------------------------------------------------------------------
# an unknown MCP profile is named
# ---------------------------------------------------------------------------

def test_doctor_install_fails_the_settings_row_for_an_unknown_profile(home, monkeypatch):
    from anyuse import selfcheck
    monkeypatch.setenv("ANYUSE_MCP_PROFILE", "mta")
    row = selfcheck.check_settings()
    assert row["ok"] is False, row
    assert "mta" in row["found"] and "default, meta, full" in row["found"] + row["fix"], row
    monkeypatch.setenv("ANYUSE_MCP_PROFILE", "meta")
    assert selfcheck.check_settings()["ok"] is True


def test_the_mcp_server_warns_on_stderr_about_an_unknown_profile(home):
    env = dict(os.environ, ANYUSE_MCP_PROFILE="mta")
    proc = subprocess.run([sys.executable, "-P", "-m", "anyuse.mcpserver"], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    assert "ANYUSE_MCP_PROFILE" in proc.stderr and "'mta'" in proc.stderr, proc.stderr[-2000:]
    assert "default, meta, full" in proc.stderr, proc.stderr[-2000:]
    assert proc.stdout.strip() == "", proc.stdout
    env["ANYUSE_MCP_PROFILE"] = "meta"
    proc = subprocess.run([sys.executable, "-P", "-m", "anyuse.mcpserver"], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    assert "ANYUSE_MCP_PROFILE" not in proc.stderr, proc.stderr[-2000:]


# ---------------------------------------------------------------------------
# --json prints JSON on every path
# ---------------------------------------------------------------------------

def test_an_empty_workflow_list_prints_json(home):
    for argv in (["workflow", "list", "--json"], ["--json", "workflow", "list"]):
        res = CliRunner().invoke(cli, argv)
        assert res.exit_code == 0 and json.loads(res.stdout) == [], (argv, res.output)


def _json_failure(argv, code):
    res = CliRunner().invoke(cli, ["--json"] + argv)
    assert res.exit_code == code, (argv, res.output)
    data = json.loads(res.stdout)
    assert data["ok"] is False and data["reason"], (argv, data)
    return data


def test_json_play_prints_json_on_every_failure(home, monkeypatch):
    _json_failure(["workflow", "play", "nope"], 1)
    _json_failure(["workflow", "play", "nope", "--param", "q=x"], 1)
    _json_failure(["workflow", "play", "s1", "--param", "no-equals-sign"], 2)
    _json_failure(["workflow", "play", "s1", "--verify"], 2)
    _tool("s1")
    data = _json_failure(["workflow", "play", "s1"], 2)
    assert "--param q=VALUE" in data["next"], data
    _offline(monkeypatch, FakeTransport())
    data = _json_failure(["workflow", "play", "s1", "--param", "q=amber"], 1)
    assert data["unacknowledged_hosts"] == [HOST] and "acknowledge-host" in data["next"], data
    acknowledge_host(HOST)
    data = _json_failure(["workflow", "play", "s1", "--param", "q=amber", "--param", "x=1"], 1)
    assert "unknown parameter 'x'" in data["reason"], data
    res = CliRunner().invoke(cli, ["--json", "workflow", "play", "s1", "--param", "q=amber"])
    assert res.exit_code == 0 and json.loads(res.stdout)["ok"] is True, res.output


def test_json_play_of_a_desktop_workflow_prints_one_json_object(home, monkeypatch):
    from anyuse.core import agent as A
    from anyuse.core.result import AgentResult
    _desktop("desk", "d1d1")

    class Replays:
        def __init__(self, **kw):
            print("loading the recorded steps")

        def play(self, name):
            print("step 2 of 2 failed")
            return AgentResult.failure_result("the Save button was not found")
    monkeypatch.setattr(A, "Agent", Replays)
    res = CliRunner().invoke(cli, ["--json", "workflow", "play", "desk"])
    assert res.exit_code == 1, res.output
    data = json.loads(res.stdout)
    assert data["ok"] is False and "Save button" in data["reason"], data
    assert "step 2 of 2 failed" in res.stderr, res.stderr

    class Crashes(Replays):
        def play(self, name):
            raise RuntimeError("no screen")
    monkeypatch.setattr(A, "Agent", Crashes)
    data = _json_failure(["workflow", "play", "desk"], 1)
    assert "RuntimeError: no screen" in data["reason"], data


def test_json_play_of_a_compiled_recording_says_why_it_stopped(home, monkeypatch):
    import anyuse.cli as C
    from anyuse.execution import draft_compile as DC
    monkeypatch.setattr(C, "_is_compiled_recording", lambda name: True)
    monkeypatch.setattr(DC, "replay_saved", lambda *a, **kw: None)
    monkeypatch.setattr(DC, "run_sync", lambda coro: {"ok": False, "stopped": "desktop input is off"})
    data = _json_failure(["workflow", "play", "rec", "--param", "x=1"], 1)
    assert data["reason"] == "desktop input is off", data


# ---------------------------------------------------------------------------
# --version from a clone without installed metadata
# ---------------------------------------------------------------------------

def test_version_works_without_installed_metadata(monkeypatch):
    from anyuse import __version__

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata, "version", missing)
    res = CliRunner().invoke(cli, ["--version"])
    assert res.exit_code == 0, (res.output, repr(res.exception))
    assert __version__ in res.output


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
