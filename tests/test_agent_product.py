"""The agent harness's product entry points (execution/harness_product.py, mcpserver/agent_tools.py,
execution/harness_sandbox_runner.py, `anyuse agent run`), offline with
fakes. Pinned here: the policies (scripted from a JSON list; the Claude CLI through the product runner's argv
with a clean environment, never spawned here; Cerebras refusing without a key before any call); the tool sets
(``--site`` needs a container endpoint and refuses a gov host; ``--app`` with no guest runner is the typed
interlock refusal, with one it reaches the guest's ``ax_elements``/``ax_step``); one run's report; the
CLI's rules (an unasked irreversible action stops; the spend ledger is armed by the CLI
group); the MCP tool ``agent_run`` in the default profile and the meta ``call_operation`` on site ``agent``
(the outcome, the reason, the model calls, the cost, the compiled tool); the container sandbox runner that
refuses unless enabled AND no tart VM runs, and runs the exact plan through an injected ``run``.
No subprocess, no network, no model, no desktop.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_agent_product.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

import pytest  # noqa: E402

from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_sandbox_runner as SR  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_toolwrite as TW  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_adapter as M  # noqa: E402
from anyuse.mcpserver import agent_tools as AT  # noqa: E402
from anyuse.mcpserver import meta_tools as MT  # noqa: E402

import test_agent_harness as TA  # noqa: E402
import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE


@pytest.fixture
def env(monkeypatch):
    d = tempfile.mkdtemp(prefix="anyuse-prod-")
    monkeypatch.setenv("ANYUSE_DATA_DIR", d)
    monkeypatch.delenv("ANYUSE_DESKTOP_GUEST_SESSION", raising=False)
    monkeypatch.delenv("ANYUSE_LEARN_ENDPOINT", raising=False)
    monkeypatch.delenv("ANYUSE_AGENT_CONTAINER_SANDBOX", raising=False)
    monkeypatch.delenv("ANYUSE_AGENT_UNASKED_IRREVERSIBLE", raising=False)
    return d


# ---------------------------------------------------------------------------
# the sandbox runner
# ---------------------------------------------------------------------------
def test_tart_running_parses_ps_output():
    assert SR.tart_running("/usr/bin/python x\n/opt/homebrew/bin/tart run anyuse-vm --no-graphics\n") == "anyuse-vm"
    assert SR.tart_running("tart list\n/bin/sh -c tart\n") is None
    assert SR.tart_running("") is None


def test_the_runner_refuses_unless_enabled_and_no_vm_and_never_spawns():
    spec, _ = TW.ToolSpec.from_dict(__import__("test_harness_toolwrite").table_spec())
    plan = TW.ContainerSandbox().plan(spec, tempfile.mkdtemp())
    started = []

    def fake_run(argv, stdin_text, timeout):
        started.append(argv)
        return 0, json.dumps({"outputs": {"csv": "a\n", "n": 0}}), ""
    # off (the default setting): refused
    rc, out, err = SR.container_runner(ps_reader=lambda: "", run=fake_run)(plan, "", 10)
    assert rc == SR.REFUSED_RC and "is off" in err and started == []
    assert SR.refusal(enabled_now=False).startswith(SR.SETTING)
    # on, but a tart VM runs and the vsock fix is not confirmed: refused
    rc, out, err = SR.container_runner(enabled_now=True, ps_reader=lambda: "/opt/tart run vm1", run=fake_run,
                                       vsock_confirmed=False)(plan, "", 10)
    assert rc == SR.REFUSED_RC and "tart VM is running (vm1)" in err and started == []
    assert SR.VSOCK_FIX_CONFIRMED is True   # flipped by main after the vsock immunity test
    # on, no VM: the exact plan goes to the injected run
    rc, out, err = SR.container_runner(enabled_now=True, ps_reader=lambda: "ps\n", run=fake_run)(plan, "", 10)
    assert rc == 0 and started == [plan]
    # on, a VM, the fix confirmed: allowed
    rc, _o, _e = SR.container_runner(enabled_now=True, ps_reader=lambda: "tart run vm1", run=fake_run,
                                     vsock_confirmed=True)(plan, "", 10)
    assert rc == 0
    # anything that is not a sandbox plan is refused even when enabled
    for bad in (["ls"], ["container", "run", "--rm", "img"], plan[:-1] + ["--privileged"]):
        rc, _o, err = SR.container_runner(enabled_now=True, ps_reader=lambda: "", run=fake_run)(bad, "", 10)
        assert rc == SR.REFUSED_RC and "not a container sandbox plan" in err
    # through ContainerSandbox: a refused runner is a typed failure, nothing started
    r = TW.ContainerSandbox(runner=SR.container_runner(ps_reader=lambda: "", run=fake_run)).run(
        spec, spec.examples[0]["inputs"], workspace=tempfile.mkdtemp())
    assert not r.ok and "refused" in r.why and len(started) == 2


def test_the_setting_is_security_relevant_and_off_by_default(env):
    from anyuse import settings as S
    spec = S.SETTINGS[SR.SETTING]
    assert spec.default is False and spec.security_relevant and spec.needs_confirmation(True)
    assert SR.enabled() is False
    st = SR.status(ps_text="tart run vm9")
    assert st == {"setting": SR.SETTING, "enabled": False, "tart_vm": "vm9", "vsock_fix_confirmed": True,
                  "would_start": False}


# ---------------------------------------------------------------------------
# policies
# ---------------------------------------------------------------------------
def test_make_policy_scripted_claude_and_cerebras(env):
    pol = HP.make_policy("scripted", script=[{"action": "done", "result": {"x": 1}}])
    d = pol.decide("t", T.Observation(kind="none"), [], [])
    assert isinstance(d.action, T.Done)
    calls = []

    def runner(argv, stdin_text, timeout):
        calls.append(argv)
        return 0, json.dumps({"result": '{"action": "done"}', "total_cost_usd": 0.02}), ""
    pol = HP.make_policy("claude", runner=runner, model="sonnet")
    mark = M.process_call_count()
    d = pol.decide("t", T.Observation(kind="none"), [], [])
    assert isinstance(d.action, T.Done) and d.cost_usd == 0.02 and M.calls_since(mark) == 1
    assert calls[0][:4] == ["claude", "-p", "--model", "sonnet"] and "--tools" in calls[0]
    env_clean = HP.clean_env()
    assert not any("API_KEY" in k or k.startswith("ANYUSE_") for k in env_clean) and "HOME" in env_clean
    with pytest.raises(ValueError):
        HP.make_policy("gemini")
    # Cerebras without a key: the adapter refuses before any call; the run stops as a policy error
    import httpx
    pol = HP.make_policy("cerebras", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    tools, _ = TA.shop_tools()
    out = HP.run_agent_task("t", toolbox=HT.Toolbox(browser=tools), policy_obj=pol)
    assert out["outcome"] == "policy_error" and out["model_calls"] == 0


def test_load_script_rejects_what_is_not_an_action_list(tmp_path=None):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.json")
    with open(p, "w") as fh:
        json.dump({"action": "done"}, fh)
    with pytest.raises(ValueError):
        HP.load_script(p)
    with open(p, "w") as fh:
        json.dump([{"action": "read_table"}, {"action": "done"}], fh)
    assert len(HP.load_script(p)) == 2


# ---------------------------------------------------------------------------
# tool sets and one run
# ---------------------------------------------------------------------------
def test_site_needs_a_container_endpoint_and_refuses_a_gov_host(env, monkeypatch):
    with pytest.raises(ValueError, match="ANYUSE_LEARN_ENDPOINT"):
        HP.make_toolbox(site="https://www.shop.test/", run_id="r")
    # by default a government host is a site like any other (it needs the container endpoint too)
    with pytest.raises(ValueError, match="ANYUSE_LEARN_ENDPOINT"):
        HP.make_toolbox(site="https://example.gov/", run_id="r")
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    made = HP.make_toolbox(site="https://example.gov/", run_id="r")
    assert made["toolbox"] is None and made["refusal"]["kind"] == "gov_host"
    out = HP.run_agent_task("t", site="https://example.gov/", policy="scripted", script=[])
    assert out["outcome"] == "refused" and "government host" in out["reason"] and out["model_calls"] == 0


def test_app_without_a_guest_runner_is_the_typed_interlock_refusal(env):
    out = HP.run_agent_task("t", app="TextEdit", policy="scripted", script=[{"action": "done"}])
    assert out["outcome"] == "refused" and out["refusal"]["kind"] == "foreground_interlock"
    assert "guest" in out["reason"] and out["steps"] == 0


def test_app_with_a_guest_runner_reaches_ax_elements_and_ax_step(env):
    g = TA.FakeAXGuest()
    seen = []

    def runner(op, timeout=420, **args):
        seen.append((op, args))
        return g(op, **{k: v for k, v in args.items() if k not in ("app", "bundle")})
    script = [{"action": "ax_read"}, lambda t, o, h: T.AxEnterText(TA.idx(o, role="AXTextArea"), "Hallo"),
              {"action": "done", "result": {"typed": True}}]
    out = HP.run_agent_task("type Hallo", app="TextEdit", policy="scripted", script=script, guest=runner)
    assert out["ok"], out["reason"]
    assert seen[0] == ("ax_elements", {"app": "TextEdit", "bundle": ""}) and any(op == "ax_step" for op, _a in seen)
    assert g.text == "Hallo" and out["effects"] == ["read", "write", "read"] and out["model_calls"] == 0
    # a bundle id goes as `bundle`
    seen.clear()
    HP.run_agent_task("t", app="com.apple.TextEdit", policy="scripted", script=[{"action": "done"}], guest=runner)
    assert seen[0] == ("ax_elements", {"app": "", "bundle": "com.apple.TextEdit"})
    # the guest's "no such app" answer stops the run before any policy call
    out = HP.run_agent_task("t", app="Nope", policy="scripted", script=[{"action": "done"}],
                            guest=lambda op, **a: {"status": "done", "ok": False, "detail": "0 running processes"})
    assert out["outcome"] == "blocked" and "0 running processes" in out["reason"] and out["decisions"] == 0


@pytest.mark.parametrize("attempted", [False, True])
@pytest.mark.parametrize("mode", ["configured_denials", "rules", "llm", "allow_all"])
def test_configured_desktop_prompt_boundary_uses_the_configured_guest_runner(env, monkeypatch, attempted, mode):
    from anyuse.execution import desktop_tools as DT
    from anyuse.execution import system_prompt_policy as SP
    guest, calls = TA.FakeAXGuest(), []
    monkeypatch.setenv(SP.MODE_ENV, mode)
    monkeypatch.setenv(SP.POLICY_ENV, "/guest-only/private/prompt-policy.json")
    monkeypatch.setattr(SP, "resolve_configured", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a remote desktop must never resolve on the host")))

    def runner(op, **args):
        calls.append((op, args))
        if op == "resolve_system_prompt":
            return {"status": "done", "resolution": {
                "format": SP.RESOLUTION_FORMAT, "status": ("denial_attempted" if mode == "configured_denials" else
                                                              "action_attempted") if attempted else "no_prompt",
                "reason": "guest checked", "attempted": attempted, "reobserve_required": attempted,
                "next_action": "observe"}}
        return guest(op, **{key: value for key, value in args.items() if key not in ("app", "bundle")})

    monkeypatch.setattr(DT, "configured_runner", lambda: runner)
    out = HP.run_agent_task("inspect TextEdit", app="TextEdit", policy="scripted", script=[{"action": "done"}])
    assert [op for op, _ in calls] == ["resolve_system_prompt", "ax_elements"]
    expected_args = {"where": "agent_harness.before_observation", "mode": mode,
                     "policy_path": "/guest-only/private/prompt-policy.json"}
    if mode == "llm":
        expected_args["task_context"] = "inspect TextEdit"
        limits = calls[0][1]["decision_limits"]
        assert set(limits) == {"remaining_usd", "remaining_calls", "timeout_s"}
        assert limits["remaining_calls"] > 0 and limits["timeout_s"] > 0
        expected_args["decision_limits"] = limits
    assert calls[0][1] == expected_args
    assert not any(op == "ax_step" for op, _ in calls)
    if attempted:
        assert out["outcome"] == "uncertain" and out["decisions"] == 0
        assert out["result"]["reobserve_required"] and out["result"]["automatic_retry"] is False
    else:
        assert out["ok"] and out["decisions"] == 1


def test_default_desktop_prompt_mode_does_not_send_a_remote_probe_or_resolve_locally(env, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    guest, calls = TA.FakeAXGuest(), []
    monkeypatch.delenv(SP.MODE_ENV, raising=False)
    monkeypatch.setattr(SP, "resolve_configured", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("default remote desktop must not invoke the local resolver")))

    def runner(op, **args):
        calls.append(op)
        return guest(op, **{key: value for key, value in args.items() if key not in ("app", "bundle")})

    out = HP.run_agent_task("inspect TextEdit", app="TextEdit", policy="scripted",
                            script=[{"action": "done"}], guest=runner)
    assert out["ok"] and calls == ["ax_elements"]


def test_a_web_run_on_a_page_factory_compiles_with_fresh_values(env):
    from anyuse.execution import dom_page as D
    from anyuse.memory.storage import WorkflowStorage
    site = SHOP.Shop()
    st = WorkflowStorage(base_dir=pathlib.Path(env, "workflows"))
    script = [{"action": "search", "element": 0, "text": "Bosch"}, {"action": "read_table"}, {"action": "done"}]
    out = HP.run_agent_task("search the shop for {q}", values={"q": "Bosch"}, site=BASE + "/", policy="scripted",
                            script=script, page_factory=lambda: D.DomPage(site), compile=True,
                            fresh=["Makita", "Ryobi"], storage=st, name="shop_search")
    assert out["ok"] and out["compiled"]["status"] == "ready" and out["compiled"]["name"] == "shop_search", out
    assert out["model_calls"] == 0 and out["config"]["unasked_irreversible"] == "ask"
    # no --fresh needed - the proof takes its values from the site's own vocabulary
    out = HP.run_agent_task("search the shop for {q}", values={"q": "Bosch"}, site=BASE + "/", policy="scripted",
                            script=list(script), page_factory=lambda: D.DomPage(site), compile=True, storage=st)
    assert out["compiled"]["status"] == "ready", out["compiled"]


def test_the_owners_rule_holds_in_a_product_run_and_hold_asks_the_approver(env, monkeypatch):
    g = TA.FakeAXGuest()
    runner = lambda op, timeout=420, **a: g(op, **{k: v for k, v in a.items() if k not in ("app", "bundle")})  # noqa: E731
    script = [lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")), lambda t, o, h: T.AxPress(TA.idx(o, text="Löschen")),
              {"action": "done"}]
    out = HP.run_agent_task("tidy up", app="TextEdit", policy="scripted", script=list(script), guest=runner)
    assert out["outcome"] == "irreversible_not_asked" and out["stopped_action"]["action"] == "ax_press"
    monkeypatch.setenv("ANYUSE_AGENT_UNASKED_IRREVERSIBLE", "hold")
    asked = []
    out = HP.run_agent_task("tidy up", app="TextEdit", policy="scripted", script=list(script), guest=runner,
                            approver=lambda ctx: asked.append(ctx) or False)
    # open tasks: ``hold`` is read as ``ask``; a declined question stops the run
    assert out["outcome"] == "confirm_declined" and len(asked) == 1 and out["config"]["unasked_irreversible"] == "ask"
    out = HP.run_agent_task("tidy up", app="TextEdit", policy="scripted", script=list(script), guest=runner)
    assert out["outcome"] == "irreversible_not_asked"            # no one to ask means stop
    monkeypatch.setenv("ANYUSE_AGENT_UNASKED_IRREVERSIBLE", "stop")
    asked.clear()
    out = HP.run_agent_task("tidy up", app="TextEdit", policy="scripted", script=list(script), guest=runner,
                            approver=lambda ctx: asked.append(ctx) or True)
    assert out["outcome"] == "irreversible_not_asked" and not asked   # stop never asks


def test_the_default_policy_is_the_configured_model(env, monkeypatch):
    # policy "model" (the default, over MCP and the CLI) runs on whatever ANYUSE_MODEL_PROVIDER resolves to
    assert HP.DEFAULT_POLICY == "model" and {"model", "cerebras", "claude"} <= set(HP.POLICIES)
    monkeypatch.setenv("ANYUSE_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("ANYUSE_MODEL_NAME", "some-model")
    assert HP.provider_chosen()
    assert HP.make_policy("model").name == "some-model" == HP.make_policy("cerebras").name
    monkeypatch.setenv("ANYUSE_MODEL_PROVIDER", "claude-cli")
    monkeypatch.delenv("ANYUSE_MODEL_NAME")
    assert HP.make_policy("model").name == "sonnet"
    monkeypatch.delenv("ANYUSE_MODEL_PROVIDER")
    assert not HP.provider_chosen()
    seen = {}
    AT.handle({"task": "search for {q}", "site": "www.shop.test", "values": {"q": "x"}},
              run=lambda task, **kw: seen.update(kw) or {"ok": True, "outcome": "done", "reason": "",
                                                          "model_calls": 0, "cost_usd": 0.0, "compiled": None})
    assert seen["policy"] == "model"


@pytest.mark.parametrize("task", ["tidy up", "delete the document"])
def test_a_commit_step_is_handed_to_the_user_when_commits_need_a_human(env, task):
    g = TA.FakeAXGuest()
    runner = lambda op, timeout=420, **a: g(op, **{k: v for k, v in a.items() if k not in ("app", "bundle")})  # noqa: E731
    script = [lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")), lambda t, o, h: T.AxPress(TA.idx(o, text="Löschen")),
              {"action": "done"}]
    asked = []
    out = HP.run_agent_task(task, app="TextEdit", policy="scripted", script=list(script), guest=runner,
                            commits_need_human=True, asker=lambda q: asked.append(q))
    assert out["outcome"] == "human_required" and out["stopped_action"]["action"] == "ax_press", out
    assert out["reason"].startswith("hand to the user (do not run it yourself)") and not asked
    assert out["config"]["commits_need_human"] is True
    assert sum(op == "ax_step" for op, _args in g.log) == 1  # opened the menu; did not press Delete


def test_agent_run_over_mcp_hands_commits_to_the_user_unless_the_setting_allows_the_agent(env, monkeypatch):
    seen = {}

    def run(task, **kw):
        seen.update(kw)
        return {"ok": True, "outcome": "done", "reason": "", "model_calls": 0, "cost_usd": 0.0, "compiled": None}
    monkeypatch.delenv("ANYUSE_MCP_AGENT_COMMITS", raising=False)
    AT.handle({"task": "buy the cheapest one", "site": "www.shop.test"}, run=run)
    assert seen["commits_need_human"] is True
    monkeypatch.setenv("ANYUSE_MCP_AGENT_COMMITS", "agent")
    AT.handle({"task": "buy the cheapest one", "site": "www.shop.test"}, run=run)
    assert seen["commits_need_human"] is False
    assert "human_required" in AT.TOOL_DESCRIPTION and "purchases" not in AT.TOOL_DESCRIPTION


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------
def _cli(args, data_dir, **env):
    from click.testing import CliRunner
    from anyuse.cli import cli
    return CliRunner().invoke(cli, args, env={"ANYUSE_DATA_DIR": data_dir, "ANYUSE_DRIVE_ENABLED": "", **env})


def test_prompt_cli_inspection_is_read_only_and_resolution_is_explicit(env, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    calls = []
    snapshot = {"format": "anyuse.system-prompt-observation/1", "status": "prompt", "reason": "observed",
                "fingerprint_sha256": "a" * 64}
    monkeypatch.setattr(SP, "inspect_current_prompt", lambda: calls.append("inspect") or snapshot)
    monkeypatch.setattr(SP, "resolve_configured", lambda where, **kwargs: calls.append(where) or {
        "status": "denial_attempted", "reason": "denial accepted", "attempted": True,
        "reobserve_required": True, "next_action": "observe"})
    inspected = _cli(["agent", "system-prompt", "inspect", "--json"], env)
    assert inspected.exit_code == 0 and json.loads(inspected.output) == snapshot
    assert calls == ["inspect"]
    resolved = _cli(["agent", "system-prompt", "resolve"], env)
    assert resolved.exit_code == 0 and "Observe the screen again" in resolved.output
    assert calls == ["inspect", "cli.agent.system-prompt.resolve"]


def test_prompt_cli_reports_manual_stop_and_disabled_default(env, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setattr(SP, "resolve_configured", lambda where, **kwargs: {
        "status": "manual", "reason": "grant requires a person", "attempted": False,
        "reobserve_required": False, "next_action": "stop"})
    stopped = _cli(["agent", "system-prompt", "resolve", "--json"], env)
    assert stopped.exit_code == 1 and json.loads(stopped.output)["next_action"] == "stop"
    monkeypatch.setattr(SP, "resolve_configured", lambda where, **kwargs: {
        "status": "disabled", "reason": "manual mode", "attempted": False,
        "reobserve_required": False, "next_action": "unchanged"})
    disabled = _cli(["agent", "system-prompt", "resolve"], env)
    assert disabled.exit_code == 0 and "disabled" in disabled.output


def test_prompt_cli_passes_only_an_explicit_originating_task_to_the_resolver(env, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    seen = []

    def resolve(where, *, task_context):
        seen.append((where, task_context))
        return {"status": "manual", "reason": "test stop", "attempted": False,
                "reobserve_required": False, "next_action": "stop"}

    monkeypatch.setattr(SP, "resolve_configured", resolve)
    assert _cli(["agent", "system-prompt", "resolve", "--task", "Open the requested document"], env).exit_code == 1
    assert _cli(["agent", "system-prompt", "resolve"], env).exit_code == 1
    assert seen == [("cli.agent.system-prompt.resolve", "Open the requested document"),
                    ("cli.agent.system-prompt.resolve", "")]


def test_the_cli_runs_a_scripted_task_arms_the_ledger_and_reports(env):
    from anyuse.execution import spend
    script = os.path.join(env, "s.json")
    with open(script, "w") as fh:
        json.dump([{"action": "list_files"}, {"action": "write_file", "name": "note.txt", "text": "hi"},
                   {"action": "done", "result": {"wrote": "note.txt"}}], fh)
    spend.disarm()
    res = _cli(["agent", "run", "write a note", "--policy", "scripted", "--script", script, "--json"], env)
    assert res.exit_code == 0, res.output
    out = json.loads(res.output[res.output.index("{"):])
    assert out["ok"] and out["outcome"] == "done" and out["result"] == {"wrote": "note.txt"} and out["model_calls"] == 0
    assert out["effects"] == ["read", "write", "read"]
    assert spend.armed()                                            # the CLI group armed the ledger
    spend.disarm()
    res = _cli(["agent", "run", "x", "--policy", "scripted"], env)
    assert res.exit_code == 2 and "--script" in res.output
    res = _cli(["agent", "run", "x", "--policy", "scripted", "--script", script, "--value", "novalue"], env)
    assert res.exit_code == 2
    res = _cli(["agent", "run", "x", "--site", "https://www.shop.test/", "--policy", "scripted", "--script", script], env)
    assert res.exit_code == 2 and "ANYUSE_LEARN_ENDPOINT" in res.output
    res = _cli(["agent", "sandbox"], env)
    assert res.exit_code == 0 and "would start: False" in res.output


def test_the_cli_stops_an_unasked_irreversible_action_with_exit_1(env):
    g = TA.FakeAXGuest()
    runner = lambda op, timeout=420, **a: g(op, **{k: v for k, v in a.items() if k not in ("app", "bundle")})  # noqa: E731
    script = os.path.join(env, "s.json")
    with open(script, "w") as fh:
        # element 0 opens the Ablage menu; with it open, element 5 is "Löschen"
        json.dump([{"action": "ax_press", "element": 0}, {"action": "ax_press", "element": 5}, {"action": "done"}], fh)
    with mock.patch.object(HP, "make_toolbox", lambda **kw: {"toolbox": HT.Toolbox(desktop=HT.DesktopTools(runner, "TextEdit")),
                                                             "close": lambda: None, "refusal": None, "workspace": env}):
        res = _cli(["agent", "run", "tidy up", "--app", "TextEdit", "--policy", "scripted", "--script", script], env)
    assert res.exit_code == 1 and "irreversible_not_asked" in res.output and "Löschen" in res.output


# ---------------------------------------------------------------------------
# MCP: agent_run and the meta call_operation
# ---------------------------------------------------------------------------
def _server(profile, run):
    from anyuse.mcpserver.server import create_server
    return create_server(profile=profile, agent_runner=run, compiled_storage=MT._WorkflowDir(),
                         desktop_storage=MT._WorkflowDir())


def _call(server, name, arguments=None):
    from mcp.types import CallToolRequest, CallToolRequestParams

    async def go():
        req = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
        return (await server.request_handlers[CallToolRequest](req)).root
    return json.loads(asyncio.run(go()).content[0].text)


def _listed(server):
    from mcp.types import ListToolsRequest

    async def go():
        return (await server.request_handlers[ListToolsRequest](ListToolsRequest())).root
    return [t.name for t in asyncio.run(go()).tools]


def fake_run(task, **kw):
    return {"ok": True, "outcome": "done", "reason": "the policy finished", "steps": 3, "decisions": 3,
            "model_calls": 3, "cost_usd": 0.0012, "result": {"rows": 2}, "effects": ["read"] * 3, "held": 0,
            "stopped_action": None, "compiled": {"name": "shop.site_search", "status": "ready", "reasons": []},
            "run_id": "abc", "policy": kw.get("policy"), "_kw": kw}


def test_agent_run_is_listed_in_the_default_profile_and_returns_the_report(env):
    srv = _server("default", fake_run)
    assert AT.TOOL_NAME in _listed(srv)
    out = _call(srv, AT.TOOL_NAME, {"task": "search for {q}", "site": "www.shop.test", "values": {"q": "x"},
                                    "policy": "claude", "max_steps": 5, "compile": True, "fresh": ["a", "b"]})
    assert out["ok"] and out["outcome"] == "done" and out["model_calls"] == 3 and out["cost_usd"] == 0.0012
    assert out["compiled"] == {"name": "shop.site_search", "status": "ready", "reasons": []} and out["policy"] == "claude"
    assert "_kw" not in out                                          # never the internals, never a page record
    out = _call(srv, AT.TOOL_NAME, {"task": ""})
    assert not out["ok"] and "task is required" in out["reason"]
    out = _call(srv, AT.TOOL_NAME, {"task": "x", "policy": "scripted"})
    assert not out["ok"] and "scripted" in out["reason"]
    # the meta profile keeps its four fixed tools; the agent is not a compiled site (list_sites is unchanged)
    # but is described by list_operations {"site": "agent"} and run by call_operation
    meta = _server("meta", fake_run)
    assert AT.TOOL_NAME not in _listed(meta) and len(_listed(meta)) == 4
    sites = _call(meta, "list_sites")
    assert not any(s["site"] == MT.AGENT_SITE for s in sites["sites"])
    ops = _call(meta, "list_operations", {"site": MT.AGENT_SITE})
    assert ops["ok"] and ops["operations"][0]["operation"] == "run" and ops["operations"][0]["kind"] == "agent"
    out = _call(meta, "call_operation", {"site": "agent", "operation": "run", "args": {"task": "search", "site": "www.shop.test"}})
    assert out["ok"] and out["outcome"] == "done" and out["model_calls"] == 3 and out["compiled"]["name"] == "shop.site_search"
    assert out["served_by"] == "agent_harness" and out["kind"] == "agent"
    out = _call(meta, "call_operation", {"site": "agent", "operation": "run", "args": {"nope": 1}})
    assert not out["ok"] and out["class"] == "input"


def test_a_stopped_run_over_mcp_is_a_typed_failure_with_the_reason(env):
    def stopped(task, **kw):
        return {"ok": False, "outcome": "irreversible_not_asked", "reason": "step 2: the irreversible action ...",
                "steps": 2, "decisions": 2, "model_calls": 2, "cost_usd": 0.001, "result": None, "compiled": None}
    srv = _server("meta", stopped)
    out = _call(srv, "call_operation", {"site": "agent", "operation": "run", "args": {"task": "tidy up"}})
    assert not out["ok"] and out["class"] == "failed" and out["outcome"] == "irreversible_not_asked"
    assert "irreversible" in out["reason"] and out["model_calls"] == 2
    out = _call(_server("default", stopped), AT.TOOL_NAME, {"task": "tidy up"})
    assert not out["ok"] and out["outcome"] == "irreversible_not_asked"

    def boom(task, **kw):
        raise RuntimeError("no endpoint")
    out = _call(_server("default", boom), AT.TOOL_NAME, {"task": "t"})
    assert not out["ok"] and out["outcome"] == "error" and "no endpoint" in out["reason"]


def test_the_server_passes_its_guest_runner_to_the_harness(env):
    seen = {}

    def run(task, **kw):
        seen.update(kw)
        return {"ok": True, "outcome": "done", "reason": "", "model_calls": 0, "cost_usd": 0.0, "compiled": None}
    from anyuse.mcpserver.server import create_server
    guest = lambda op, **a: {"status": "done"}  # noqa: E731
    srv = create_server(profile="default", agent_runner=run, desktop_guest_runner=guest,
                        compiled_storage=MT._WorkflowDir(), desktop_storage=MT._WorkflowDir())
    _call(srv, AT.TOOL_NAME, {"task": "t", "app": "TextEdit"})
    assert seen["guest"] is guest and seen["app"] == "TextEdit"


def test_public_runs_persist_fallback_counts_and_mcp_preserves_recommendation(env, monkeypatch):
    from anyuse.execution.learning import ReaFallbackTracker

    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "3")
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "default")

    def run(task, **kwargs):
        guest = TA.FakeAXGuest()
        def runner(op, **args):
            return guest(op, **{k: v for k, v in args.items() if k not in ("app", "bundle", "timeout")})
        return HP.run_agent_task(task, app="TextEdit", policy="scripted", guest=runner,
                                 script=[{"action": "ax_read"}, {"action": "done"}])

    for expected in (1, 2):
        out = run("read my test window")
        assert out["ok"], out
        assert out["rea_escalation"]["vision_fallbacks"] == expected
        assert not out["rea_escalation"]["escalate_to_rea"]
    out = _call(_server("default", run), AT.TOOL_NAME, {"task": "read my test window"})
    assert out["ok"] and out["rea_escalation"]["vision_fallbacks"] == 3
    assert out["rea_escalation"]["escalate_to_rea"]
    assert ReaFallbackTracker().rea_escalation_status("read my test window")["vision_fallbacks"] == 3


@pytest.mark.parametrize("ui_ok,native_ok,expected", [(False, False, 0), (True, True, 0), (True, False, 1)])
def test_public_fallback_tracking_excludes_refusals_and_successful_rea(env, monkeypatch, ui_ok, native_ok, expected):
    from anyuse.execution.learning import ReaFallbackTracker

    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "3")
    observation = T.Observation(kind="ax")
    trace = T.Trace(kind="desktop", task="test task", stop=T.Stop.DONE)
    trace.steps.append(T.Step(1, observation, T.AxRead(), T.READ, T.Outcome(ui_ok)))
    if native_ok:
        trace.steps.append(T.Step(2, observation, T.RunReaAction("test.native", {}), T.WRITE,
                                  T.Outcome(True, data={"ok": True})))
    monkeypatch.setattr(HP.H.Harness, "run", lambda *args, **kw: trace)
    out = HP.run_agent_task("test task", toolbox=object(), policy="scripted", script=[])
    assert out["ok"]
    assert ("rea_escalation" in out) == bool(expected)
    assert ReaFallbackTracker().rea_escalation_status("test task")["vision_fallbacks"] == expected


def test_cli_reports_rea_recommendation_in_text_and_json(env, monkeypatch):
    recommendation = {"vision_fallbacks": 3, "escalate_to_rea": True, "threshold": 3}
    def run(task, **kwargs):
        return dict(fake_run(task, **kwargs), rea_escalation=recommendation)
    monkeypatch.setattr(HP, "run_agent_task", run)
    result = _cli(["agent", "run", "test task"], env)
    assert result.exit_code == 0, result.output
    assert "REA mapping recommended after 3 computer-use runs" in result.output
    result = _cli(["agent", "run", "test task", "--json"], env)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rea_escalation"] == recommendation


@pytest.mark.parametrize("effect,commit,retry", [(T.READ, None, True), (T.WRITE, None, False),
    (T.UNKNOWN, None, False), (T.IRREVERSIBLE, {"class": "submit"}, False)])
def test_audit_demo_cascade_only_restarts_read_only_runs(env, monkeypatch, effect, commit, retry):
    trace = T.Trace(kind="web", task="Submit one report", stop=T.Stop.MAX_STEPS)
    trace.steps.append(T.Step(0, T.Observation(kind="web"), T.Click(0), effect, T.Outcome(True), commit=commit))
    monkeypatch.setenv("ANYUSE_DEMO_MODEL", "cascade")
    monkeypatch.setattr(HP.H.Harness, "run", lambda *a, **k: trace)
    monkeypatch.setattr(HP, "make_policy", lambda *a, **k: object())
    retries = []
    monkeypatch.setattr(HP, "_cascade_retry", lambda out, *a, **k: retries.append(True) or out)
    HP.run_agent_task("Submit one report", toolbox=object(), site="example.invalid", policy="cerebras",
                      compile=True, judge=None)
    assert bool(retries) is retry


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
