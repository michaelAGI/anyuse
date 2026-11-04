"""Automation target preflight without AppKit, Apple Events, or subprocesses."""
import asyncio
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-osa-targets-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_ROOT, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.context import macos as M


def _fake_grants(monkeypatch):
    probes, runs = [], []
    monkeypatch.setattr(M, "bundle_resolver", lambda app: "com.example." + app.replace(" ", ""))
    monkeypatch.setattr(M, "automation_probe", lambda app: probes.append(app) or M.AE_GRANTED)
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **kw: runs.append(a) or
                        SimpleNamespace(returncode=0, stdout=b"ok", stderr=b""))
    return probes, runs


@pytest.mark.parametrize("script", [
    'set targetApp to "TextEdit"\ntell application targetApp to make new document',
    'set targetId to "com.apple.TextEdit"\ntell application id targetId to make new document',
    'tell application ("TextEdit") to make new document',
    'tell app targetApp to activate',
    'tell application "Text" & "Edit" to make new document',
    'tell application id "com.apple." & "TextEdit" to make new document',
    'tell application "Text" (* explanation *) & "Edit" to make new document',
])
def test_unresolved_application_targets_never_start_osascript(monkeypatch, script):
    _probes, runs = _fake_grants(monkeypatch)

    assert M.applescript_refusal(script), script
    assert M._osascript(script) is None
    assert not runs


@pytest.mark.parametrize("script,targets", [
    ('tell application "TextEdit" to make new document', ["com.example.TextEdit"]),
    ('tell application id "com.apple.TextEdit" to make new document', ["com.apple.TextEdit"]),
    ('tell app "TextEdit"\nactivate\nend tell', ["com.example.TextEdit"]),
    ('return 1 + 1', []),
    ('tell application "System Events"\nset p to first application process whose frontmost is true\n'
     'return name of front window of p\nend tell', ["com.example.SystemEvents"]),
    ('-- tell application "Unrelated" to activate\ntell application "TextEdit" to activate',
     ["com.example.TextEdit"]),
    ('tell application "TextEdit" (* a comment *)\nactivate\nend tell', ["com.example.TextEdit"]),
    ('tell application "Safari" to do JavaScript "const app = \\"TextEdit\\";" in front document',
     ["com.example.Safari"]),
])
def test_static_targets_keep_working_with_existing_grants(monkeypatch, script, targets):
    probes, runs = _fake_grants(monkeypatch)

    assert M.applescript_targets(script) == targets
    assert M.applescript_refusal(script) is None
    assert probes == targets
    assert M._osascript(script) == "ok"
    assert len(runs) == 1


def test_every_static_target_is_checked_without_requesting_a_grant(monkeypatch):
    _probes, runs = _fake_grants(monkeypatch)
    seen = []

    def grant(app):
        seen.append(app)
        return M.AE_GRANTED if app.endswith("TextEdit") else M.AE_WOULD_ASK

    monkeypatch.setattr(M, "automation_probe", grant)
    script = 'tell application "TextEdit" to activate\ntell application "Finder" to activate'
    assert M._osascript(script) is None
    assert seen == ["com.example.TextEdit", "com.example.Finder"]
    assert not runs


def test_executor_refuses_a_dynamic_target_before_starting_a_process(monkeypatch):
    from anyuse.execution import actions, foreground_interlock
    from anyuse.execution.executor import StateGraphExecutor

    _probes, runs = _fake_grants(monkeypatch)
    monkeypatch.setattr(actions, "drive_enabled", lambda: True)
    monkeypatch.setattr(foreground_interlock, "refusal", lambda _where: None)
    executor = object.__new__(StateGraphExecutor)
    step = SimpleNamespace(command={"script": 'tell application targetApp to make new document'})
    result = executor._execute_applescript(step)

    assert result["success"] is False, result
    assert not runs


def test_intervention_refuses_a_dynamic_target_before_starting_a_process(monkeypatch):
    from anyuse.execution import intervention

    _probes, runs = _fake_grants(monkeypatch)
    monkeypatch.setattr(intervention, "_fix_refused", lambda _where: False)
    handler = object.__new__(intervention.InterventionHandler)
    result = asyncio.run(handler._run_applescript_fix({
        "script": 'tell application id targetId to make new document'}))

    assert result is False
    assert not runs


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
