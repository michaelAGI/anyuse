"""SVG reads retain the inspector's live, bounded app-ancestry ownership gate.

Only each renderer's original row survives in a receipt. Its immediate parent
may be another app helper: live collection, rather than a direct-parent claim
in a retained validator, establishes that the bounded chain reaches the main
process. These fixtures never open a CDP connection or inspect a real process.
"""
import copy
import os
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_inspector_bridge as I
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_svg_observer as S
from test_rea_svg_observer import environment


@pytest.fixture
def owned_renderer(tmp_path, monkeypatch):
    bundle = (tmp_path / "Owned Canvas.app").resolve()
    (bundle / "Contents/MacOS").mkdir(parents=True)
    executable = bundle / "Contents/MacOS/Owned Canvas"
    executable.write_bytes(b"inert ownership fixture; never executed")
    main = {"bundle_id": "org.fixture.canvas", "bundle": str(bundle), "executable": str(executable),
            "pid": 9000, "launch_time": 42.5, "uid": os.getuid()}
    processes = {
        9002: {"pid": 9002, "ppid": 9001, "uid": main["uid"], "started_at": "renderer-start",
               "executable": str(bundle / "Contents/Frameworks/Canvas Renderer.app/Contents/MacOS/Canvas Renderer")},
        9001: {"pid": 9001, "ppid": main["pid"], "uid": main["uid"], "started_at": "helper-start",
               "executable": str(bundle / "Contents/Frameworks/Canvas Helper.app/Contents/MacOS/Canvas Helper")},
    }
    reads = []
    def process(pid, deadline):
        reads.append(pid)
        if pid not in processes:
            raise I.Unavailable("fixture ancestor is outside the owned process tree")
        return copy.deepcopy(processes[pid])
    monkeypatch.setattr(I, "_process", process)
    rows = [{"type": "browser", "id": main["pid"]}, {"type": "renderer", "id": 9002}]
    def call(method):
        assert method == "SystemInfo.getProcessInfo"
        return {"processInfo": copy.deepcopy(rows)}
    return SimpleNamespace(bundle=bundle, main=main, processes=processes, rows=rows,
                           browser=SimpleNamespace(call=call), reads=reads)


def renderers(env):
    return I._renderer_processes(env.browser, env.bundle, env.main, deadline=100)


def test_owned_indirect_renderer_ancestry_is_admitted_without_exporting_helper_handles(owned_renderer):
    env = owned_renderer
    assert renderers(env) == [env.processes[9002]]
    assert env.reads == [9002, 9001]
    assert env.processes[9002]["ppid"] != env.main["pid"]


@pytest.mark.parametrize("member", [9002, 9001])
@pytest.mark.parametrize("fault", ["uid", "executable"])
def test_foreign_renderer_or_intermediate_helper_is_refused(owned_renderer, member, fault):
    env = owned_renderer
    env.processes[member][fault] = env.main["uid"] + 1 if fault == "uid" else "/foreign/Canvas Helper"
    with pytest.raises(I.Unavailable, match="foreign|outside"):
        renderers(env)


@pytest.mark.parametrize("parent", [9002, 9999])
def test_renderer_ancestry_cannot_cycle_or_end_in_an_unowned_process(owned_renderer, parent):
    env = owned_renderer
    env.processes[9001]["ppid"] = parent
    with pytest.raises(I.Unavailable, match="foreign|outside"):
        renderers(env)


def test_renderer_ancestry_budget_refuses_unfinished_chain(owned_renderer):
    env = owned_renderer
    for pid in range(9001, 9020):
        env.processes[pid] = {"pid": pid, "ppid": pid + 1, "uid": env.main["uid"],
                              "started_at": f"helper-{pid}", "executable": env.main["executable"]}
    with pytest.raises(I.Unavailable, match="ancestry exceeds"):
        renderers(env)
    assert len(env.reads) <= 17


def test_browser_pid_must_match_pinned_main_before_renderer_process_reads(owned_renderer):
    env = owned_renderer
    env.rows[0]["id"] += 100
    with pytest.raises(I.Unavailable, match="browser PID differs"):
        renderers(env)
    assert env.reads == []


@pytest.mark.parametrize("fault", ["listener_changed", "renderer_changed"])
def test_epoch_bound_svg_read_still_refuses_endpoint_ownership_changes(environment, fault):
    env = environment
    expected = B.process_identity(env.app, env.schema["target"]["bundle_id"])
    env.server.fault = fault
    result = S.collect(str(env.app), env.schema, str(env.root / "pinned-svg-observation"),
                       config_file=str(env.config), expected_identity=expected)
    assert result["complete"] is False and result["matches"] is None
    assert result["status"] == "unknown" and env.server.read_count == 1
    assert result["app_evaluations"] == result["method_calls"] == result["getters_invoked"] == 0
    assert result["actuated"] is result["proof_created"] is False
    assert all(channel.closed for channel in env.server.connections)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
