"""Linux new-user regressions, with platform and browser discovery faked.

These cases exercise the public CLI and MCP results without starting a browser,
contacting a socket, or importing a desktop framework.
"""
import asyncio
import json
import pathlib
import platform
import shutil
import sys
import tempfile
import urllib.error

import click
import pytest
from click.testing import CliRunner

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-linux-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_ROOT, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import anyuse.cli as C


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_linux_doctor_detects_an_installed_container_runtime(monkeypatch, tmp_path, runtime):
    binary = tmp_path / runtime
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda name: str(binary) if name == runtime else None)

    result = CliRunner().invoke(C.cli, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    rows = {row["check"]: row for row in json.loads(result.output)["checks"]}
    assert rows["container runtime"]["ok"] is True, rows["container runtime"]
    assert runtime in rows["container runtime"]["found"]


def test_linux_doctor_omits_desktop_settings_and_permission_probes(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def desktop_probe():
        raise AssertionError("a Linux doctor must not query desktop state")

    monkeypatch.setattr(C, "_desktop_profile", desktop_probe)
    monkeypatch.setattr(C, "_desktop_probe", desktop_probe)
    result = CliRunner().invoke(C.cli, ["doctor", "--json"])

    assert result.exit_code == 0, (result.output, result.exception)
    rows = {row["check"] for row in json.loads(result.output)["checks"]}
    assert rows.isdisjoint({"desktop profile", "pyobjc", "accessibility permission",
                            "screen recording permission", "desktop input"})


@pytest.mark.parametrize("error", [urllib.error.URLError("Connection refused"),
                                    OSError("Network unreachable")])
def test_attach_without_a_browser_returns_an_actionable_cli_error(monkeypatch, error):
    from anyuse.execution import attach

    class NoBrowser:
        def open(self, url, timeout):
            raise error

    monkeypatch.setattr(attach, "_no_redirect_opener", NoBrowser)
    result = CliRunner().invoke(C.cli, ["attach", "--cdp", "http://127.0.0.1:9222",
                                       "--max-seconds", "1"])

    assert result.exit_code == 2, (result.output, result.exception)
    assert "attach refused:" in result.output
    assert "9222" in result.output and "remote debugging" in result.output
    assert "Traceback" not in result.output


def test_linux_mcp_desktop_status_explains_that_macos_is_required(monkeypatch):
    from anyuse.mcpserver import server

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setitem(server._current_task, "busy", False)
    response = asyncio.run(server._desktop_status())
    status = json.loads(response[0].text)

    assert status["status"] == "unavailable", status
    assert status["current_task"] is None
    assert "macOS" in status["reason"]


def test_linux_recording_refuses_before_querying_macos_permissions(monkeypatch):
    from anyuse.context import accessibility
    from anyuse.watcher import step_capture

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv(step_capture.ENV_GATE, "1")

    def permission_probe():
        raise AssertionError("Linux must be refused before querying Accessibility")

    monkeypatch.setattr(accessibility, "ax_trusted", permission_probe)
    reason = step_capture.capture_refusal()

    assert reason and "macOS" in reason, reason
    assert "not granted" not in reason


def test_unrun_proof_report_does_not_claim_the_planned_inputs_were_proved():
    from anyuse.execution.api_compile import CompileResult
    from anyuse.execution.api_verify import EquivalenceResult

    compiled = CompileResult(reason="no eligible request")
    proof = EquivalenceResult(
        reason="host not acknowledged; no proof requests sent",
        split={"compile": 1, "validation": 2, "held_out": True})

    @click.command()
    def render():
        C._finish_compile("search", compiled, proof, {}, ["one.har"], [], [],
                          json_output=False, no_persist=True, transition_spec="",
                          no_permission_check=True, paginate=False, explain=True,
                          robots_policy_flag=None, har_report=None)

    result = CliRunner().invoke(render)

    assert result.exit_code == 1, (result.output, result.exception)
    assert "NOT PASSED (0/0 cases)" in result.output
    assert "; proved on " not in result.output, result.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
