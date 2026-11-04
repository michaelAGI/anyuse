"""The MCP desktop tools' steps carry the record's app in their selectors (``desktop_compiled_tools.bind_step``),
offline with fakes.

A compiled desktop record names its app (``app.bundle_id``); its steps were compiled before selectors carried their
app, so the executor's app check right before acting had nothing to compare. Pinned here:

* an AX step's selector and command get the record's bundle id when they name none, in BOTH execution paths: the
  injected executor (tests) and the guest job agent's ``run_tool`` (the shipped path);
* a selector or command that names ANOTHER app is refused before anything runs;
* a record without a bundle id, a non-AX step, the call's values: unchanged; the proof pin is unchanged (the app
  is added when the step is bound, not stored).

No desktop, no AX, no subprocess, no network, no VM.
"""
import asyncio
import copy
import json
import pathlib
import sys
import tempfile
import types

import pytest

from anyuse.execution import foreground_interlock as FI
from anyuse.execution import desktop_tools as DTools
from anyuse.memory.storage import WorkflowStorage
from anyuse.mcpserver import desktop_compiled_tools as DT

import test_desktop_job_interlock as TJ
import test_desktop_tools_mcp as TM
import test_launch_mcp_profile as launch
from test_desktop_tools_mcp import env, guest_job  # noqa: F401  (fixtures)

PREFS = "com.apple.systempreferences"
SEL = {"canonical_role": "statictext", "name": "", "identifier": "version", "path": "window[0]/statictext[2]",
       "raw_role": "AXStaticText"}


def _record(storage, name="read_version", selector=None, cmd_app=None, bundle=PREFS):
    cmd = {"role": "AXStaticText", "title": "${label}", "action": "read", "selector": dict(selector or SEL)}
    if cmd_app:
        cmd["app"] = cmd_app
    rec = DT.compiled_desktop_record(
        workflow=name, compile_class="read_value",
        parameters=[{"name": "label", "type": "string", "required": True}],
        steps=[{"intent": "read the version", "source": "workflow", "target_element": "value", "modality": "ax",
                "command": cmd}],
        workflow_template={"id": name, "name": name, "required_app": "Systemeinstellungen",
                           "steps": [{"intent": "read the version", "target_element": "value"}]},
        state_graph={"states": {}, "transitions": []}, proof={"proved": True, "fresh_inputs": 2},
        extra={"app": {"name": "Systemeinstellungen", "bundle_id": bundle}})
    DT.persist_compiled_desktop(storage, name, rec)
    return rec


def test_bind_step_stamps_the_records_app_and_refuses_another():
    rec = {"app": {"bundle_id": PREFS, "name": "Systemeinstellungen"}}
    raw = {"modality": "ax", "command": {"title": "${label}", "selector": dict(SEL)}}
    step, why = DT.bind_step(raw, {"label": "Version"}, rec)
    assert why is None and step["command"]["selector"]["app"] == PREFS and step["command"]["app"] == PREFS
    assert step["command"]["title"] == "Version" and "app" not in raw["command"]["selector"]      # raw untouched
    _s, why = DT.bind_step({"modality": "ax", "command": {"selector": dict(SEL, app="com.apple.finder")}}, {}, rec)
    assert why == f"the step's selector names the app 'com.apple.finder', the tool is bound to {PREFS!r}"
    _s, why = DT.bind_step({"modality": "ax", "command": {"app": "com.apple.TextEdit"}}, {}, rec)
    assert "the step names the app 'com.apple.TextEdit'" in why
    same, why = DT.bind_step({"modality": "ax", "command": {"selector": dict(SEL, app=PREFS)}}, {}, rec)
    assert why is None and same["command"]["selector"]["app"] == PREFS
    # no bundle id, a non-AX step: the values only
    plain = {"modality": "applescript", "command": {"app": "TextEdit", "script": "${label}"}}
    assert DT.bind_step(plain, {"label": "x"}, rec) == ({"modality": "applescript",
                                                         "command": {"app": "TextEdit", "script": "x"}}, None)
    assert DT.bind_step(raw, {"label": "V"}, {"app": {"name": "Settings"}})[0]["command"]["selector"] == SEL


def test_the_injected_executor_path_sees_the_app_and_a_conflict_runs_nothing(env):  # noqa: F811
    rec = _record(env.storage)
    fake = launch.CheckedExecutorFake()
    out = asyncio.run(DT.run_typed_tool("desktop__read_version", {"label": "Version"}, storage=env.storage,
                                        executor=fake))
    assert out["ok"], out
    step = fake.calls[0][0]
    assert step.command["selector"]["app"] == PREFS and step.command["app"] == PREFS
    assert DT.verify_record(DT.executable_typed_tools(env.storage)[0]["record"])        # the pin is unchanged
    assert "app" not in rec["steps"][0]["command"]["selector"]
    _record(env.storage, name="read_other", selector=dict(SEL, app="com.apple.finder"))
    fake2 = launch.CheckedExecutorFake()
    out = asyncio.run(DT.run_typed_tool("desktop__read_other", {"label": "Version"}, storage=env.storage,
                                        executor=fake2))
    assert not out["ok"] and out["refused"] and "com.apple.finder" in out["error"] and fake2.calls == []


def test_the_guests_run_tool_binds_the_app_too(env, guest_job, monkeypatch):  # noqa: F811
    from anyuse.execution import draft_compile as DC
    J, runner, asked = guest_job
    _record(env.storage)
    fake = launch.CheckedExecutorFake()
    monkeypatch.setattr(DC, "checked_executor", lambda tree: fake)
    monkeypatch.setattr(J, "app_tree", lambda pid: ("tree-of", pid))
    monkeypatch.setattr(J, "front_window", lambda pid: None)
    rec = DT.executable_typed_tools(env.storage)[0]["record"]
    resp, _ = J.handle("run_tool", {"record": json.loads(json.dumps(rec)), "arguments": {"label": "Version"}})
    assert resp["status"] == "done" and resp["ok"], resp
    assert fake.calls[0][0].command["selector"]["app"] == PREFS and asked["apps"][0]["bundle_id"] == PREFS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
