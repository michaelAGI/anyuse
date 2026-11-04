"""Compiled desktop tools over MCP, run in the GUEST (``execution/desktop_tools.py``,
``mcpserver/desktop_compiled_tools.py``, the job agent's ``run_tool``; docs/DESKTOP_TOOLS.md), offline with FAKES.

Pinned here:

* a routine or a draft workflow proved IN THE GUEST (at least two correct replays on values that were not recorded,
  0 model calls) writes ``compiled_desktop.json`` next to its storage entry: name, typed parameters, the proof
  record, the app, the approval level, the payload -- pinned by the artifact digest (a changed payload, a simulated
  proof, one proof run: not listed);
* the MCP server lists it in the default profile (``desktop__<name>``) and the meta profile (``call_operation``);
* a call with values runs the replay with NO approval prompt; a deviation stops with its reason (a bad value, the
  guest's typed interlock refusal); an approval level that holds steps is not run over MCP;
* no desktop tool ever runs on the host. With no guest runner the call is refused with the foreground
  interlock's typed reason (the older compile classes too, which used to build an executor with no AX tree); with
  one, the call reaches the guest job agent, which binds the AX tree to the recorded app's one process and runs the
  checked steps there.

No desktop, no AX, no subprocess, no network, no VM.
"""
import asyncio
import copy
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from anyuse.execution import choice_identity as CI
from anyuse.execution import desktop_routine as R
from anyuse.execution import desktop_tools as DTools
from anyuse.execution import foreground_interlock as FI
from anyuse.memory.storage import WorkflowStorage
from anyuse.mcpserver import desktop_compiled_tools as DT
from anyuse.mcpserver.server import create_server

import test_desktop_choice_identity as TC
import test_desktop_job_interlock as TJ
import test_launch_mcp_profile as launch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VM = os.path.join(HERE, "execution", "guest")
HOST = TJ.guest(is_vm=False)
FINDER = "com.apple.finder"


def _job():
    if VM not in sys.path:
        sys.path.insert(0, VM)
    return importlib.import_module("desktop_e2e_job")


async def _called(server, name, arguments=None):
    req = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
    return (await server.request_handlers[CallToolRequest](req)).root


def call(server, name, arguments=None):
    return json.loads(asyncio.run(_called(server, name, arguments)).content[0].text)


def listed(server):
    return asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest())).root.tools


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv(FI.TEST_BYPASS_ENV, raising=False)
    monkeypatch.delenv(DTools.GUEST_SESSION_ENV, raising=False)
    monkeypatch.setenv("ANYUSE_MCP_PROFILE", "default")
    state = {"now": HOST}
    monkeypatch.setattr(FI, "probe_state", lambda: state["now"])
    tmp = tempfile.mkdtemp(prefix="anyuse-dtools-")
    storage = WorkflowStorage(base_dir=pathlib.Path(tmp, "workflows"))
    return types.SimpleNamespace(state=state, storage=storage, tmp=tmp, mp=monkeypatch)


# ── the routine tool ──────────────────────────────────────────────────────────────────────────────────────

def sort_routine():
    rt = TC.recorded("set A", "SG04")
    return R.derive_routine(rt["steps"], rt["goal"], task="SG04")


def sort_keys(r):
    return CI.stable_keys(r["steps"][1]["resolved"]["entry"]["options"])


def proved_rows(r):
    k = sort_keys(r)
    return [{"values": {"choice": k[5]}, "ok": True, "model_calls": 0, "variant": "v1"},
            {"values": {"choice": "Zuletzt geöffnet"}, "ok": True, "model_calls": 0, "variant": "v2"}]


def write_sort_tool(storage, rows=None, level="auto"):
    r = sort_routine()
    proof = DTools.guest_proof(rows or proved_rows(r), recorded={"choice": "Änderungsdatum"},
                               job="2300", session="dq1", commit="abc1234")
    rec = DTools.compiled_record_for_routine(r, proof, name="finder_sort", approval_level=level,
                                             app={"name": "Finder", "bundle_id": FINDER})
    DTools.write_routine_tool(storage, r, rec)
    return r, rec


def test_a_guest_proof_needs_two_correct_replays_on_new_values_and_no_model_call():
    r = sort_routine()
    good = proved_rows(r)
    assert DTools.guest_proof(good, recorded={"choice": "Änderungsdatum"})["proved"]
    for rows, why in ((good[:1], "2 are needed"), ([dict(good[0], ok=False), good[1]], "failed"),
                      ([good[0], dict(good[1], model_calls=1)], "model calls"),
                      ([good[0], dict(good[1], values={"choice": "Änderungsdatum"})], "without a new value"),
                      (good + [{"values": {"choice": "x"}, "ok": True, "designed_to_fail": True}], "designed")):
        p = DTools.guest_proof(rows, recorded={"choice": "Änderungsdatum"})
        assert not p["proved"] and any(why in x for x in p["problems"]), (why, p["problems"])
    # a proof row that names the recorded option by its key is no new value: the writer refuses it
    same = [dict(good[0], values={"choice": sort_keys(r)[4]}), good[1]]
    assert not DTools.guest_proof(same, recorded={"choice": "Änderungsdatum"}, same=DTools.routine_same(r))["proved"]
    with pytest.raises(ValueError, match="new values"):
        DTools.compiled_record_for_routine(r, DTools.guest_proof(same), name="x", app={"name": "Finder"})
    # several parameters: a row may keep one, but each must take a new value in some correct replay
    gg10 = R.derive_routine(TC.recorded("set A", "GG10")["steps"], TC.recorded("set A", "GG10")["goal"])
    rows = [{"values": {"folder": "Drafts", "name": "Print proof", "choice": "id:_NS:120"}, "ok": True},
            {"values": {"folder": "Final", "name": "Archive copy", "choice": "id:_NS:123"}, "ok": True}]
    rec_vals = {p["name"]: p["value"] for p in gg10["params"]}
    assert DTools.guest_proof(rows, recorded=rec_vals, same=DTools.routine_same(gg10))["proved"]
    kept = [dict(rows[0], values={"folder": "Final", "name": "Print proof", "choice": "id:_NS:120"}), rows[1]]
    p = DTools.guest_proof(kept, recorded=rec_vals, same=DTools.routine_same(gg10))
    assert not p["proved"] and "['folder']" in p["problems"][0]
    gate = copy.deepcopy(r)
    gate["steps"].append({"step": {"effect": "human_gate", "app": "Finder"}, "resolved": {"op": "gate"}})
    with pytest.raises(ValueError, match="gate"):
        DTools.compiled_record_for_routine(gate, DTools.guest_proof(good), name="x", app={"name": "Finder"})


def test_the_record_is_written_next_to_its_storage_entry_and_pinned(env):
    r, rec = write_sort_tool(env.storage)
    d = env.storage.get_workflow_dir(DTools.routine_storage_id(r))
    assert sorted(os.listdir(d)) == ["compiled_desktop.json", "routine.json"]
    assert rec["compile_class"] == "routine" and rec["app"]["bundle_id"] == FINDER
    assert rec["approval_level"] == "auto" and rec["proof"]["harness"] == "guest" and rec["proof"]["fresh_inputs"] == 2
    assert DT.verify_record(rec)
    tools = DT.executable_typed_tools(env.storage)
    assert [t["name"] for t in tools] == ["desktop__finder_sort"] and tools[0]["route"] == "guest"
    choice = tools[0]["input_schema"]["properties"]["choice"]
    assert "id:_NS:123" in choice["enum"] and "Erstellungsdatum" in choice["enum"]
    assert tools[0]["input_schema"]["required"] == []
    assert tools[0]["ready"] is False and "no guest runner" in tools[0]["why_not"]
    # the pin: a changed payload, a simulated proof or one proof run is not listed
    for mutate in (lambda x: x["payload"]["routine"]["steps"][1]["resolved"]["option"].update(label="Art"),
                   lambda x: x["proof"].update(harness="sim_ax"),
                   lambda x: x["proof"].update(fresh_inputs=1)):
        bad = copy.deepcopy(rec)
        mutate(bad)
        assert not DT.verify_record(bad)
    # the workflow listing and Agent.play never see a routine entry (no workflow.json)
    assert WorkflowStorage(base_dir=env.storage.base_dir).list_names() == []


class Spy:
    """A guest runner: answers ops like the harness's fake guest (the Arch9 world, live menus)."""

    def __init__(self, guest, refuse_ops=()):
        self.guest, self.ops, self.refuse_ops = guest, [], set(refuse_ops)

    def __call__(self, op, timeout=None, **args):
        self.ops.append(op)
        if op in self.refuse_ops:
            return {"status": "refused", "refused_by": "foreground_interlock",
                    "refusal": {"kind": "foreground_interlock", "where": f"desktop_e2e_job: {op}",
                                "reason": "vm guest: a screen-sharing viewer is attached", "environment": "vm-guest",
                                "inputs_sent_before": 0},
                    "detail": "refused by foreground interlock: vm guest: a screen-sharing viewer is attached"}
        return self.guest(op, **args)


def english_finder():
    r = sort_routine()
    entry = r["steps"][1]["resolved"]["entry"]
    return TC.MenuGuest({FINDER: TC.live_items(entry, order=[3, 5, 0, 4, 1, 2], rename=TC.ENGLISH,
                                               marked="id:_NS:102")})


def test_a_routine_call_runs_in_the_guest_with_no_prompt_in_both_profiles(env):
    write_sort_tool(env.storage)
    asked = []

    async def approver(ctx):
        asked.append(ctx)
        return True
    for profile, name, args in (("default", "desktop__finder_sort", {"choice": "Erstellungsdatum"}),
                                ("meta", "call_operation", {"site": "Finder", "operation": "finder_sort",
                                                            "args": {"choice": "Erstellungsdatum"}})):
        g = english_finder()
        runner = Spy(g)
        server = create_server(profile=profile, desktop_storage=env.storage, desktop_approver=approver,
                               desktop_guest_runner=runner)
        out = call(server, name, args)
        assert out["ok"], out
        assert g.pressed == ["Date Created"] and "press" in runner.ops and "marks" in runner.ops
        if profile == "default":
            assert out["served_by"] == "guest:ReplayArch" and out["model_calls"] == 0 and out["route"] == "guest"
        else:
            assert out["served_by"] == "guest:ReplayArch"
    assert asked == []                                              # starting the call was the approval


def test_a_deviation_stops_with_its_reason(env):
    write_sort_tool(env.storage)
    runner = Spy(english_finder())
    server = create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner)
    # a live title names nothing: the schema's enum (keys and recorded labels) refuses it before anything runs
    text = asyncio.run(_called(server, "desktop__finder_sort", {"choice": "Date Created"})).content[0].text
    assert "Input validation error" in text and runner.ops == []
    meta = create_server(profile="meta", desktop_storage=env.storage, desktop_guest_runner=runner)
    out = call(meta, "call_operation", {"site": "Finder", "operation": "finder_sort", "args": {"choice": "nope"}})
    assert out["class"] == "input" and runner.ops == []
    # past the schema, the routine's own value check (an external option) stops before acting
    out = asyncio.run(DT.run_typed_tool("desktop__finder_sort", {"choice": "Collins"}, storage=env.storage,
                                        guest_runner=runner))
    assert not out["ok"] and out["error"].startswith("invalid parameters") and runner.ops == []
    # the guest refuses (a viewer attached): typed, and the replay stops there
    g = english_finder()
    runner = Spy(g, refuse_ops=("press",))
    server = create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner)
    out = call(server, "desktop__finder_sort", {"choice": "Erstellungsdatum"})
    assert not out["ok"] and out["refused_by"] == "foreground_interlock" and g.pressed == []
    assert out["refusal"]["reason"].startswith("vm guest: a screen-sharing viewer")
    meta = create_server(profile="meta", desktop_storage=env.storage, desktop_guest_runner=runner)
    out = call(meta, "call_operation", {"site": "Finder", "operation": "finder_sort",
                                        "args": {"choice": "Erstellungsdatum"}})
    assert out["class"] == "failed" and out["refusal"]["kind"] == "foreground_interlock" and "guest VM" in out["next"]


def test_an_approval_level_that_holds_steps_is_not_run_over_mcp(env):
    write_sort_tool(env.storage, level="all")
    runner = Spy(english_finder())
    out = call(create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner),
               "desktop__finder_sort", {"choice": "Erstellungsdatum"})
    assert not out["ok"] and out["blocked"] and runner.ops == []
    out = call(create_server(profile="meta", desktop_storage=env.storage, desktop_guest_runner=runner),
               "list_operations", {"site": "Finder"})
    row = out["operations"][0]
    assert row["ready"] is False and "approval level" in row["why_not"]


# ── never on the host ──────────────────────────────────────────────────────────────────────────────────────

def test_with_no_guest_runner_every_desktop_tool_is_refused_on_the_host_with_a_typed_reason(env):
    write_sort_tool(env.storage)
    launch.record(env.storage, "read_setting", "read_value")                 # an older compile class
    for name, args in (("desktop__finder_sort", {"choice": "Erstellungsdatum"}),
                       ("desktop__read_setting", {"label": "Version"})):
        out = call(create_server(profile="default", desktop_storage=env.storage), name, args)
        assert not out["ok"] and out["refused_by"] == "foreground_interlock", out
        assert out["refusal"]["environment"] == "host" and out["refusal"]["reason"].startswith("host:")
        assert "nothing was sent" in out["error"] and "No AccessibilityTree" not in json.dumps(out)
    out = call(create_server(profile="meta", desktop_storage=env.storage), "call_operation",
               {"site": "Finder", "operation": "finder_sort", "args": {"choice": "Erstellungsdatum"}})
    assert out["class"] == "failed" and out["refusal"]["environment"] == "host"


def test_the_guest_runner_is_attached_by_the_session_setting(env):
    assert DTools.configured_runner({}) is None
    ch = DTools.configured_runner({DTools.GUEST_SESSION_ENV: "dq1", DTools.GUEST_SSH_ENV: "/x/vm-ssh"})
    assert isinstance(ch, DTools.GuestJobChannel) and ch.session == "dq1" and ch.ssh == "/x/vm-ssh"
    with pytest.raises(ValueError):
        DTools.GuestJobChannel("a b; rm -rf ~")
    sent = []

    class Done:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def run(argv, input=None, capture_output=True, timeout=None):
        sent.append((argv, input))
        if "grep -c json" in argv[1]:
            return Done(0, b"7\n")
        return Done(0, json.dumps({"status": "done", "front_bundle": FINDER}).encode())
    ch = DTools.GuestJobChannel("dq1", ssh="/x/vm-ssh", run=run)
    assert ch("front") == {"status": "done", "front_bundle": FINDER}
    argv, payload = sent[-1]
    assert argv[0] == "/x/vm-ssh" and "anyuse-guest/e2e/dq1" in argv[1] and "mv cmd/." in argv[1]
    assert json.loads(payload) == {"op": "front", "args": {}}


def test_prompt_policy_path_travels_as_json_through_the_actual_guest_job_channel(env):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    path, sent = "/guest/private/policy $(never-executed); 'data'.json", []
    env.mp.setenv(SP.MODE_ENV, "configured_denials")
    env.mp.setenv(SP.POLICY_ENV, path)
    resolution = {"format": SP.RESOLUTION_FORMAT, "status": "no_prompt", "reason": "guest is clear",
                  "attempted": False, "reobserve_required": False, "next_action": "observe"}

    def run(argv, input=None, **kwargs):
        sent.append((argv, input))
        cid = re.search(r"resp/(p[0-9a-f]{32})\.json", argv[1]).group(1)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({
            "status": "done", "id": cid, "op": "resolve_system_prompt", "resolution": resolution}).encode())

    channel = DTools.GuestJobChannel("prompt-routing", ssh="/inert/vm-ssh", run=run)
    desktop = HT.DesktopTools(channel, app="TextEdit")
    assert desktop.resolve_system_prompt("explicit-boundary") == resolution
    command, payload = sent[-1]
    assert path not in command[1]
    assert json.loads(payload) == {"op": "resolve_system_prompt", "args": {
        "where": "explicit-boundary", "mode": "configured_denials", "policy_path": path}}


def test_missing_guest_prompt_reply_stops_with_reobserve_required_and_no_host_fallback(env):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    env.mp.setenv(SP.MODE_ENV, "configured_denials")
    env.mp.setattr(SP, "resolve_configured", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("no host fallback")))
    calls = []
    desktop = HT.DesktopTools(lambda op, **args: calls.append((op, args)) or {"status": "error", "detail": "lost reply"})
    result = desktop.resolve_system_prompt("boundary")
    assert result["next_action"] == "stop" and result["reobserve_required"] and result["attempted"]
    assert len(calls) == 1 and calls[0][0] == "resolve_system_prompt"


@pytest.mark.parametrize("first_failure", ["timeout", "oserror", "ssh_error", "empty", "malformed", "wrong_id"])
def test_actual_prompt_channel_retrieves_same_receipt_without_resubmitting_after_failure(env, first_failure):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    env.mp.setenv(SP.MODE_ENV, "configured_denials")
    calls, submissions = [], []
    resolution = {"format": SP.RESOLUTION_FORMAT, "status": "denial_attempted", "reason": "first window denied",
                  "attempted": True, "reobserve_required": True, "next_action": "observe"}

    def run(argv, input=None, **kwargs):
        calls.append((argv[1], input))
        cid = re.search(r"resp/(p[0-9a-f]{32})\.json", argv[1]).group(1)
        if input is not None:
            submissions.append(json.loads(input))
            if first_failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if first_failure == "oserror":
                raise OSError("transport disconnected after dispatch")
            if first_failure == "ssh_error":
                return types.SimpleNamespace(returncode=255, stdout=b"")
            if first_failure == "empty":
                return types.SimpleNamespace(returncode=0, stdout=b"")
            if first_failure == "malformed":
                return types.SimpleNamespace(returncode=0, stdout=b"incomplete {")
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({
                "id": "wrong-id", "op": "resolve_system_prompt", "status": "done"}).encode())
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({
            "id": cid, "op": "resolve_system_prompt", "status": "done", "resolution": resolution}).encode())

    channel = DTools.GuestJobChannel("prompt-once", ssh="/inert/vm-ssh", run=run)
    result = HT.DesktopTools(channel).resolve_system_prompt("boundary")
    assert result == resolution and len(submissions) == 1 and len(calls) == 2
    assert all(input is None and "cat >" not in command and " mv " not in command and "ls cmd/" not in command
               for command, input in calls[1:])
    ids = {re.search(r"resp/(p[0-9a-f]{32})\.json", command).group(1) for command, _ in calls}
    assert len(ids) == 1


def test_actual_prompt_channel_never_rewrites_command_when_every_reply_is_unknown(env):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    env.mp.setenv(SP.MODE_ENV, "configured_denials")
    calls = []

    def run(argv, input=None, **kwargs):
        calls.append((argv[1], input))
        return types.SimpleNamespace(returncode=255, stdout=b"")

    channel = DTools.GuestJobChannel("prompt-unknown", ssh="/inert/vm-ssh", run=run)
    result = HT.DesktopTools(channel).resolve_system_prompt("boundary")
    assert result["next_action"] == "stop" and result["attempted"] and result["reobserve_required"]
    assert len(calls) == 4 and sum(payload is not None for _, payload in calls) == 1
    assert all("cat >" not in command and " mv " not in command and payload is None for command, payload in calls[1:])


def test_llm_prompt_context_is_bounded_utf8_data_and_uses_the_actual_channel(env):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    env.mp.setenv(SP.MODE_ENV, "llm")
    calls = []
    resolution = {"format": SP.RESOLUTION_FORMAT, "status": "action_attempted", "reason": "supported choice",
                  "attempted": True, "reobserve_required": True, "next_action": "observe"}

    def run(argv, input=None, **kwargs):
        calls.append(json.loads(input))
        cid = re.search(r"resp/(p[0-9a-f]{32})\.json", argv[1]).group(1)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({
            "id": cid, "op": "resolve_system_prompt", "status": "done", "resolution": resolution}).encode())

    desktop = HT.DesktopTools(DTools.GuestJobChannel("prompt-task", ssh="/inert/vm-ssh", run=run))
    task = "Open Reader using ${secret:AUTH}"
    assert desktop.resolve_system_prompt("boundary", task_context=task) == resolution
    assert calls[0]["args"]["task_context"] == task
    assert set(calls[0]["args"]) <= {"where", "mode", "policy_path", "task_context"}
    for invalid in ("", {"callback": "anything"}, "é" * (SP.MAX_TASK_CONTEXT_BYTES // 2 + 1)):
        result = desktop.resolve_system_prompt("boundary", task_context=invalid)
        assert result["next_action"] == "stop" and result["attempted"] is False
    assert len(calls) == 1


def test_llm_channel_translates_deadline_conservatively_without_assuming_clock_sync(env):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    env.mp.setenv(SP.MODE_ENV, "llm")
    ticks, submitted = iter([100.0, 103.0]), []
    env.mp.setattr(DTools.time, "monotonic", lambda: next(ticks))

    def run(argv, input=None, **kwargs):
        message = json.loads(input)
        submitted.append(message)
        cid = re.search(r"resp/(p[0-9a-f]{32})\.json", argv[1]).group(1)
        receipt = {"status": "done", "id": cid, "op": "resolve_system_prompt"}
        if message.get("clock_probe"):
            receipt["clock"] = {"server_id": "a" * 32, "monotonic_s": 500.0}
        else:
            receipt["resolution"] = {"format": SP.RESOLUTION_FORMAT, "status": "manual", "reason": "test stop",
                                      "attempted": False, "reobserve_required": False, "next_action": "stop"}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode())

    desktop = HT.DesktopTools(DTools.GuestJobChannel("prompt-expiry", ssh="/inert/vm-ssh", run=run))
    result = desktop.resolve_system_prompt("boundary", task_context="Inspect Reader", decision_limits={
        "remaining_usd": 0.1, "remaining_calls": 2, "timeout_s": 10.0})
    assert result["next_action"] == "stop" and len(submitted) == 2
    assert submitted[0] == {"op": "resolve_system_prompt", "args": {"where": "transport.clock", "mode": "manual"},
                            "clock_probe": True}
    assert submitted[1]["decision_deadline"] == {"server_id": "a" * 32, "monotonic_s": 507.0}
    assert submitted[1]["args"]["decision_limits"]["timeout_s"] == 7.0


def test_llm_clock_probe_failure_never_submits_a_mutating_job(env):
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import system_prompt_policy as SP
    env.mp.setenv(SP.MODE_ENV, "llm")
    submitted = []

    def run(argv, input=None, **kwargs):
        if input is not None:
            submitted.append(json.loads(input))
        return types.SimpleNamespace(returncode=255, stdout=b"")

    desktop = HT.DesktopTools(DTools.GuestJobChannel("prompt-no-clock", ssh="/inert/vm-ssh", run=run))
    result = desktop.resolve_system_prompt("boundary", task_context="Inspect Reader", decision_limits={
        "remaining_usd": 0.1, "remaining_calls": 2, "timeout_s": 10.0})
    assert result["next_action"] == "stop" and result["attempted"] is False
    assert len(submitted) == 1 and submitted[0]["clock_probe"] is True


@pytest.fixture
def guest_job(env):
    """The REAL job agent as the guest runner, in process: its interlock reads a fake guest state, its app lookup
    and AX tree are fakes (no AppKit, no pgrep, no AX)."""
    J = _job()
    env.state["now"] = TJ.CLEAR
    asked = {"apps": [], "trees": []}

    def app_pid(app):
        asked["apps"].append(dict(app))
        return 4242, None
    env.mp.setattr(J, "app_pid", app_pid)
    env.mp.setenv("ANYUSE_DRIVE_ENABLED", "1")

    def runner(op, timeout=None, **args):
        resp, _stop = J.handle(op, json.loads(json.dumps(args, default=str)))
        return resp
    return J, runner, asked


def saved_draft(storage):
    from anyuse.execution import draft_compile as DC
    ovs = [{"name": "name", "ref": "@c1"}, {"name": "betrag", "ref": "@c2", "type": "number"},
           {"name": "zahlungsart", "ref": "@c3"}]
    rep = asyncio.run(DC.draft_to_workflow(TC.web_draft(), overrides=ovs, save=True, storage=storage,
                                           name="spende_erfassen"))
    assert rep["status"] == "ready", rep["reasons"]
    _wf, draft = DC.find_saved("spende_erfassen", storage)
    rows = [{"values": {"name": "A", "betrag": "1,00", "zahlungsart": "Rechnung"}, "ok": True, "model_calls": 0},
            {"values": {"name": "B", "betrag": "2,00", "zahlungsart": "Lastschrift"}, "ok": True, "model_calls": 0}]
    rec = DTools.compiled_record_for_draft(draft, DTools.guest_proof(rows))
    DTools.write_draft_tool(storage, rec)
    return draft, rec


def test_a_draft_workflow_call_reaches_the_guest_with_an_ax_tree_bound_to_the_recorded_app(env, guest_job):
    from anyuse.execution import draft_proof as PR
    J, runner, asked = guest_job
    draft, rec = saved_draft(env.storage)
    assert rec["approval_level"] == "auto" and rec["app"]["name"] == draft["app"]
    worlds = []

    def app_tree(pid):
        w = PR.SimAXWorld(draft)
        asked["trees"].append(pid)
        worlds.append(w)
        return w
    env.mp.setattr(J, "app_tree", app_tree)
    values = {"name": "Neu", "betrag": "9,99", "zahlungsart": "Rechnung"}
    for profile, name, args in (("default", "desktop__spende_erfassen", values),
                                ("meta", "call_operation", {"site": draft["app"], "operation": "spende_erfassen",
                                                            "args": values})):
        out = call(create_server(profile=profile, desktop_storage=env.storage, desktop_guest_runner=runner),
                   name, args)
        assert out["ok"], out
        w = worlds[-1]
        assert [e["value"] for e in w.effects if e["op"] == "enter_text"] == ["Neu", "9,99"]
        assert "radiobutton:#pay-invoice" in [e["target"] for e in w.effects]
    assert asked["apps"] == [{"bundle_id": draft.get("bundle_id") or "", "name": draft["app"]}] * 2
    assert asked["trees"] == [4242, 4242]
    # the user raises the saved workflow's approval level after the proof: it holds over MCP too
    from anyuse.execution import draft_compile as DC
    DC.set_approval_level("spende_erfassen", "destructive", storage=env.storage)
    out = call(create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner),
               "desktop__spende_erfassen", values)
    assert not out["ok"] and out["blocked"] and "destructive" in out["error"]
    DC.set_approval_level("spende_erfassen", "auto", storage=env.storage)
    # a viewer attaches in the guest: a typed refusal, nothing done
    env.state["now"] = TJ.STATES["a screen-sharing viewer"]
    n = len(worlds)
    out = call(create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner),
               "desktop__spende_erfassen", values)
    assert not out["ok"] and out["refused_by"] == "foreground_interlock" and len(worlds) == n


def test_an_older_class_runs_its_checked_steps_in_the_guest_on_the_app_bound_tree(env, guest_job):
    from anyuse.execution import draft_compile as DC
    J, runner, asked = guest_job
    launch.record(env.storage, "read_setting", "read_value")
    fake = launch.CheckedExecutorFake()
    built = []

    def checked_executor(tree):
        built.append(tree)
        return fake
    env.mp.setattr(DC, "checked_executor", checked_executor)
    env.mp.setattr(J, "app_tree", lambda pid: ("tree-of", pid))
    out = call(create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner),
               "desktop__read_setting", {"label": "Version"})
    assert out["ok"] and out["served_by"] == "guest:execute_checked_step" and out["route"] == "guest"
    assert asked["apps"] == [{"bundle_id": "", "name": "Settings"}] and built == [("tree-of", 4242)]
    step, idx, wf, _graph = fake.calls[0]
    assert step.command["title"] == "Version" and idx == 0 and wf.name == "read_setting"
    # one running process or refused, nothing launched
    env.mp.setattr(J, "app_pid", lambda app: (None, "0 running processes named 'Settings'; exactly one is needed"))
    out = call(create_server(profile="default", desktop_storage=env.storage, desktop_guest_runner=runner),
               "desktop__read_setting", {"label": "Version"})
    assert not out["ok"] and "exactly one" in out["error"] and len(fake.calls) == 1
    # a record whose pin does not verify is refused in the guest too
    bad = copy.deepcopy(DT.executable_typed_tools(env.storage)[0]["record"])
    bad["steps"][0]["command"]["action"] = "press"
    resp, _ = J.handle("run_tool", {"record": bad, "arguments": {"label": "x"}})
    assert resp["status"] == "refused" and "proof pin" in resp["detail"]


# ── the guest development check's host script ─────────────────────────────────────────────────────────────

def _host():
    if VM not in sys.path:
        sys.path.insert(0, VM)
    # the host modules make their folders at import: keep them in a temporary research folder
    os.environ.setdefault("DESKTOP_QUEUE_RESEARCH", tempfile.mkdtemp(prefix="anyuse-dq-research-"))
    return importlib.import_module("desktop_tools_host")


# ── the older file classes follow the same rule: values are the approval ─────────────────────────────────────

def test_an_older_file_class_started_with_values_asks_nothing_and_a_holding_level_is_refused(env, guest_job):
    from anyuse.execution import draft_compile as DC
    J, runner, asked = guest_job
    launch.record(env.storage, "write_file", "file_write")
    launch.record(env.storage, "write_held", "file_write", extra={"approval_level": "all"})
    fake = launch.CheckedExecutorFake()
    env.mp.setattr(DC, "checked_executor", lambda tree: fake)
    env.mp.setattr(J, "app_tree", lambda pid: ("tree-of", pid))
    approvals = []

    async def approver(ctx):
        approvals.append(ctx)
        return True
    for profile, name, args in (("default", "desktop__write_file", {"label": "x"}),
                                ("meta", "call_operation", {"site": "Settings", "operation": "write_file",
                                                            "args": {"label": "x"}})):
        out = call(create_server(profile=profile, desktop_storage=env.storage, desktop_approver=approver,
                                 desktop_guest_runner=runner), name, args)
        assert out["ok"], out
    assert approvals == [] and len(fake.calls) == 2 and len(asked["apps"]) == 2      # ran in the guest, unasked
    out = call(create_server(profile="default", desktop_storage=env.storage, desktop_approver=approver,
                             desktop_guest_runner=runner), "desktop__write_held", {"label": "x"})
    assert not out["ok"] and out["blocked"] and out["refused_by"] == "approval_level" and out["approval_level"] == "all"
    out = call(create_server(profile="meta", desktop_storage=env.storage, desktop_guest_runner=runner),
               "call_operation", {"site": "Settings", "operation": "write_held", "args": {"label": "x"}})
    assert out["class"] == "needs_approval" and "approval level" in out["next"]
    assert approvals == [] and len(fake.calls) == 2 and len(asked["apps"]) == 2      # nothing sent to the guest


# ── the listing's readiness sees the server's own runner ─────────────────────────────────────────────────

def test_the_listing_is_ready_with_a_runner_injected_into_the_server(env):
    write_sort_tool(env.storage)
    launch.record(env.storage, "read_setting", "read_value")
    env.mp.delenv("ANYUSE_DRIVE_ENABLED", raising=False)          # the host's drive gate does not decide a guest tool
    assert DTools.configured_runner() is None                     # no session setting either
    for runner, ready in ((None, False), (Spy(english_finder()), True)):
        server = create_server(profile="meta", desktop_storage=env.storage, desktop_guest_runner=runner)
        rows = {}
        for site in ("Finder", "Settings"):
            rows.update({r["operation"]: r for r in call(server, "list_operations", {"site": site})["operations"]})
        for op in ("finder_sort", "read_setting"):
            assert rows[op]["ready"] is ready, (op, rows[op])
            assert ("why_not" in rows[op]) is (not ready)
            if not ready:
                assert "no guest runner is attached" in rows[op]["why_not"]
        sites = {r["site"]: r["ready"] for r in call(server, "list_sites")["sites"]}
        assert sites == {"Finder": int(ready), "Settings": int(ready)}
    # the default profile's spec agrees (both profiles read the server's own listing)
    specs = DT.executable_typed_tools(env.storage, guest_runner=Spy(english_finder()))
    assert all(s["ready"] for s in specs) and {s["route"] for s in specs} == {"guest"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
