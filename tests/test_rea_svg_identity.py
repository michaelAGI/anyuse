"""Caller epochs reach the SVG DOM reader, inspector actor and restoration latch."""
import copy
from pathlib import Path

import pytest

from test_rea_harness import fixture, runtime, svg_fixture, svg_gui_context, _H_RUNTIME
from test_rea_actions import execution_catalog, execution_recovery
from test_rea_svg_observer import environment
from anyuse.execution import rea_actions as A
from anyuse.execution import rea_harness as H
from anyuse.execution import rea_inspector_bridge as I
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_svg_observer as S


@pytest.mark.parametrize("field", ["pid", "launch_time"])
def test_svg_runtime_refuses_caller_epoch_before_observer_or_actor(svg_gui_context, monkeypatch, field):
    data, candidate, _, control = svg_gui_context
    candidate["backend"] = H.SVG_BACKEND
    expected = copy.deepcopy(control.identity)
    expected[field] += 1
    monkeypatch.setattr(S, "collect", lambda *a, **kw: pytest.fail("epoch mismatch reached DOM reader"))
    monkeypatch.setattr(I, "invoke", lambda *a, **kw: pytest.fail("epoch mismatch reached actor"))
    with pytest.raises(ValueError, match="supplied epoch"):
        _H_RUNTIME(data["app"], candidate, expected_identity=expected)
    assert not control.forbidden


def test_svg_context_rechecks_epoch_after_runtime_preflight(svg_gui_context, monkeypatch):
    data, candidate, _, control = svg_gui_context
    candidate["backend"] = H.SVG_BACKEND
    expected = copy.deepcopy(control.identity)
    count = []
    def process(*args):
        count.append(True)
        return expected if len(count) == 1 else {**expected, "launch_time": expected["launch_time"] + 1}
    monkeypatch.setattr(B, "process_identity", process)
    with pytest.raises(ValueError, match="SVG context differs"):
        _H_RUNTIME(data["app"], candidate, expected_identity=expected)
    assert len(count) == 2 and not control.forbidden


def test_bound_runtime_passes_full_copied_epoch_to_dom_and_inspector(svg_gui_context, monkeypatch, tmp_path):
    data, candidate, inputs, control = svg_gui_context
    candidate["backend"] = H.SVG_BACKEND
    expected = copy.deepcopy(control.identity)
    bound = _H_RUNTIME(data["app"], candidate, expected_identity=expected)
    expected["pid"] += 1
    leaked_copy = bound.expected_identity
    leaked_copy["launch_time"] += 1
    calls = []
    def read(*args, expected_identity, **kwargs):
        calls.append(("read", expected_identity))
        return {"evidence_only": True}
    def invoke(*args, expected_identity, **kwargs):
        calls.append(("invoke", expected_identity))
        return {**data["receipt"](candidate, inputs), "expected_identity": copy.deepcopy(expected_identity)}
    monkeypatch.setattr(S, "collect", read)
    monkeypatch.setattr(I, "invoke", invoke)
    bound.read_svg(candidate, inputs, tmp_path / "read", 5)
    bound.receipt_dir = tmp_path / "actor"
    assert bound.send(candidate, inputs, 5)["returncode"] == 0
    assert calls == [("read", control.identity), ("invoke", control.identity)]
    assert not control.forbidden


@pytest.mark.parametrize("field", ["expected_identity", "identity", "after_identity"])
def test_svg_runtime_refuses_incoherent_inspector_epoch(svg_gui_context, monkeypatch, tmp_path, field):
    data, candidate, inputs, control = svg_gui_context
    candidate["backend"] = H.SVG_BACKEND
    disposition = R._ReplayDisposition()
    bound = _H_RUNTIME(data["app"], candidate, expected_identity=control.identity, _disposition=disposition)
    bound.receipt_dir = tmp_path / "actor"
    receipt = {**data["receipt"](candidate, inputs), "expected_identity": copy.deepcopy(control.identity)}
    receipt[field] = {**receipt[field], "launch_time": control.identity["launch_time"] + 1}
    monkeypatch.setattr(I, "invoke", lambda *a, **kw: receipt)
    with pytest.raises(R.ResourceReviewRequired, match="saved process pin"):
        bound.send(candidate, inputs, 5)
    assert disposition.worker_started and not disposition.cleanup_confirmed


def test_svg_collector_checks_expected_epoch_before_endpoint_or_socket_io(environment, monkeypatch):
    env = environment
    expected = B.process_identity(env.app, env.schema["target"]["bundle_id"])
    expected["launch_time"] += 1
    monkeypatch.setattr(I, "_listener", lambda *a: pytest.fail("foreign epoch reached listener lookup"))
    result = S.collect(str(env.app), env.schema, str(env.root / "foreign"),
                       config_file=str(env.config), expected_identity=expected)
    assert result["complete"] is False and result["status"] == "unknown"
    assert result["expected_identity"] == expected
    assert "expected identity" in result["error"]["message"]
    assert env.server.connections == [] and env.server.requests == []


def test_svg_collector_retains_caller_epoch_and_remains_read_only(environment):
    env = environment
    expected = B.process_identity(env.app, env.schema["target"]["bundle_id"])
    result = S.collect(str(env.app), env.schema, str(env.root / "bound"),
                       config_file=str(env.config), expected_identity=expected)
    assert result["complete"] is True and result["expected_identity"] == expected
    assert result["identity"] == result["after_identity"] == expected
    assert result["actuated"] is False and result["proof_created"] is False
    assert result["app_evaluations"] == result["method_calls"] == result["getters_invoked"] == 0


@pytest.mark.parametrize("interrupt", [False, True])
def test_svg_restoration_uses_dispatch_disposition_for_inspector_cleanup(
        execution_recovery, svg_gui_context, monkeypatch, tmp_path, interrupt):
    env = execution_recovery
    data, candidate, inputs, control = svg_gui_context
    candidate["backend"] = H.SVG_BACKEND
    assert env.identity == control.identity
    attempted = []
    primary = KeyboardInterrupt("inspector dispatch interrupted")
    def invoke(*args, expected_identity, **kwargs):
        attempted.append(expected_identity)
        if interrupt:
            raise primary
        return {**data["receipt"](candidate, inputs), "expected_identity": copy.deepcopy(expected_identity)}
    monkeypatch.setattr(I, "invoke", invoke)
    def replay(c, proof, values, bundle, out, *, expected_identity, _disposition, **kwargs):
        actual = _H_RUNTIME(Path(bundle), c, expected_identity=expected_identity, _disposition=_disposition)
        actual.receipt_dir = Path(out) / "runtime"
        assert actual.send(c, values, 5)["returncode"] == 0
        return {"verdict": "verified", "model_calls": 0}
    monkeypatch.setattr(R, "replay", replay)
    output = tmp_path / "restored-svg"
    if interrupt:
        with pytest.raises(KeyboardInterrupt) as raised:
            A._execute_restored(candidate, {}, str(data["app"]), inputs, str(output), 30, env.declaration, env.backend)
        assert raised.value is primary
        report = R.load_json(output / "execution.json")
        assert report["restoration"]["status"] == "unknown"
        assert "close" not in env.calls and "reopen" not in env.calls
        assert report["inspector_dispatch"] == {"started": True, "cleanup_confirmed": False, "diagnostic_only": True}
    else:
        report = A._execute_restored(candidate, {}, str(data["app"]), inputs, str(output), 30, env.declaration, env.backend)
        assert report["completion_status"] == "completed" and report["restoration"]["status"] == "restored"
        assert report["inspector_dispatch"] == {"started": True, "cleanup_confirmed": True, "diagnostic_only": True}
    assert "native_worker" not in report
    assert attempted == [control.identity]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
