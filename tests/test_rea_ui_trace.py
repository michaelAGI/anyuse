import json
import os
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from anyuse.execution import rea_ui_trace as T
from anyuse.execution.learning import LearningOrchestrator, ReaFallbackTracker


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "default")
    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "3")
    monkeypatch.setenv("ANYUSE_REA_GUI_CAPTURE", "auto")


def _record(trace, *, action="type_text_at", params=None, result=None, index=0):
    trace.record(action, params or {"x": 10, "y": 20, "text": "hunter2", "press_enter": True},
        {"name": "hunter2", "bundle_id": "com.example.Canvas", "pid": 123, "window_title": "hunter2"},
        result if result is not None else {"success": True},
        started=10.0 + index, ended=10.25 + index, wall_time_ms=1700000000000 + index * 1000)


def test_capture_trigger_uses_effort_or_same_persistent_threshold_store(tmp_path, monkeypatch):
    tracker = ReaFallbackTracker(storage_dir=tmp_path / "workflows")
    assert T.ReaUiTraceCapture.for_task("task", tracker=tracker) is None
    assert not tracker.path.parent.exists()
    for _ in range(3):
        tracker.note_vision_fallback("task")
    trace = T.ReaUiTraceCapture.for_task("task", tracker=tracker)
    assert trace.directory == tracker.path.parent / "rea_candidates"
    assert not trace.directory.exists(), "trigger alone should not create artifacts"
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "rea-fast")
    assert T.ReaUiTraceCapture.for_task("other task", tracker=tracker) is not None


def test_learning_capture_uses_its_explicit_workflow_storage(tmp_path):
    class Orchestrator:
        pass
    orchestrator = Orchestrator()
    storage = type("Storage", (), {"base_dir": tmp_path})()
    learner = LearningOrchestrator(orchestrator, storage=storage)
    assert orchestrator.rea_trace_tracker is learner._rea_fallback_tracker
    assert orchestrator.rea_trace_tracker.path == tmp_path / "vision_fallbacks.json"


def test_no_task_text_typed_secrets_arbitrary_keys_or_display_names_are_retained(tmp_path):
    trace = T.ReaUiTraceCapture("type password hunter2", directory=tmp_path / "trace")
    _record(trace, params={"text": "hunter2", "keys": "hunter2", "hunter2": "hunter2",
        "x": 11, "y": 12, "button": "right", "direction": "down", "press_enter": True,
        "magnitude": float("nan"), "window_title": "hunter2", "token": "hunter2"})
    raw = trace.path.read_text()
    assert "hunter2" not in raw + trace.path.name
    assert T.FILE_NAME.fullmatch(trace.path.name)
    row = T.load_candidate_trace(trace.path)["ui_actuation_trace"][0]
    assert row == {"timestamp_monotonic": 10., "end_monotonic": 10.25, "wall_time_ms": 1700000000000,
        "action": "type_text_at", "params_redacted": {"text": T.REDACTED, "keys": T.REDACTED,
            "x": 11, "y": 12, "button": "right", "direction": "down", "press_enter": True},
        "front_app": T.REDACTED, "bundle_id": "com.example.Canvas", "pid": 123}
    assert os.stat(trace.path).st_mode & 0o777 == 0o600
    assert os.stat(trace.directory).st_mode & 0o777 == 0o700


@pytest.mark.parametrize("result", [
    {"success": False}, {"success": True, "gated": True}, {"success": True, "blocked": True},
    {"success": True, "refused": True}, {"success": 1}, {},
])
def test_only_successful_actual_input_creates_trace(tmp_path, result):
    trace = T.ReaUiTraceCapture("task", directory=tmp_path / "trace")
    _record(trace, result=result)
    assert not trace.directory.exists() and not trace.rows


def test_action_limit_marks_truncated_without_unbounded_growth(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "MAX_ACTIONS", 2)
    trace = T.ReaUiTraceCapture("task", directory=tmp_path / "trace")
    for i in range(4):
        _record(trace, index=i)
    value = json.loads(trace.path.read_text())
    assert len(value["ui_actuation_trace"]) == 2 and value["truncated"] is True
    assert T.load_candidate_trace(trace.path)["truncated"] is True


def test_store_limit_and_concurrent_writes_remain_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "MAX_ARTIFACTS", 3)
    directory = tmp_path / "trace"
    def capture(i):
        trace = T.ReaUiTraceCapture(f"task {i}", directory=directory)
        _record(trace)
    # Serial writes exercise pruning; concurrent contenders may skip persistence
    # rather than stall an active fallback after input has already been sent.
    for i in range(4):
        capture(i)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(capture, range(4, 16)))
    paths = list(directory.glob("task_*.json"))
    assert len(paths) == 3
    assert all(T.load_candidate_trace(path)["proof_created"] is False for path in paths)
    assert set(p.name for p in directory.iterdir()) == {p.name for p in paths} | {".trace.lock"}


def test_symlink_directory_and_lock_are_refused_without_touching_target(tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    trace = T.ReaUiTraceCapture("task", directory=link)
    _record(trace)
    assert trace.error and not list(destination.iterdir())
    secret = tmp_path / "secret"
    secret.write_text("untouched")
    (destination / ".trace.lock").symlink_to(secret)
    trace = T.ReaUiTraceCapture("task", directory=destination)
    _record(trace)
    assert trace.error and secret.read_text() == "untouched"


@pytest.mark.parametrize("change", [
    lambda value: value["ui_actuation_trace"][0]["params_redacted"].update(text="secret"),
    lambda value: value["ui_actuation_trace"][0].update(front_app="secret"),
    lambda value: value["ui_actuation_trace"][0].update(end_monotonic=0),
    lambda value: value.update(proof_created=True),
    lambda value: value.update(task="secret"),
])
def test_loader_refuses_unredacted_or_invalid_artifacts(tmp_path, change):
    trace = T.ReaUiTraceCapture("task", directory=tmp_path / "trace")
    _record(trace)
    value = json.loads(trace.path.read_text())
    change(value)
    trace.path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        T.load_candidate_trace(trace.path)


def test_loader_byte_bound_bundle_filter_and_cli_are_data_only(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    from anyuse.execution import rea_generic_workflow as W
    trace = T.ReaUiTraceCapture("task", directory=tmp_path / "trace")
    _record(trace)
    result = W.ui_trace_context(trace.path, bundle_id="com.example.Canvas")
    assert result["artifact"]["sha256"] and result["actuated"] is result["proof_created"] is False
    with pytest.raises(ValueError, match="no windows"):
        W.ui_trace_context(trace.path, bundle_id="com.other.App")
    out = CliRunner().invoke(rea_commands, ["workflow", "ui-trace", str(trace.path), "--bundle-id", "com.example.Canvas"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["basis"] == "historical_gui_windows_not_native_observations"
    monkeypatch.setattr(T, "MAX_BYTES", 2)
    with pytest.raises(ValueError, match="byte budget"):
        T.load_candidate_trace(trace.path)


def test_invalid_time_does_not_change_actuation_result_or_write(tmp_path):
    trace = T.ReaUiTraceCapture("task", directory=tmp_path / "trace")
    result = {"success": True}
    trace.record("click_at", {}, None, result, started=4, ended=3, wall_time_ms=100)
    assert result == {"success": True} and trace.error and not trace.directory.exists()


def test_trace_lock_contention_is_nonblocking_and_next_action_can_persist(tmp_path):
    import fcntl
    directory = tmp_path / "trace"
    directory.mkdir()
    trace = T.ReaUiTraceCapture("task", directory=directory)
    with (directory / ".trace.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _record(trace)
        assert trace.error and not trace.path.exists()
    _record(trace, index=1)
    assert trace.error is None
    assert len(T.load_candidate_trace(trace.path)["ui_actuation_trace"]) == 2


def _gui_orchestrator(monkeypatch, result, recorder=None):
    from types import SimpleNamespace
    from anyuse.execution import agent_orchestrator as O
    monkeypatch.setenv("ANYUSE_SYSTEM_PROMPT_MODE", "manual")
    monkeypatch.setattr(O, "drive_enabled", lambda: False)
    clock = [100.0]
    def execute(action, params):
        if action == "leave_failsafe_corner":
            return {"success": True}
        clock[0] = 100.25
        return result
    executor = SimpleNamespace(execute=execute, _compliance_checker=None)
    front = {"name": "Canvas", "bundle_id": "com.example.Canvas", "pid": 123, "has_window": True}
    orch = O.AgentOrchestrator(adapter=SimpleNamespace(config=None), action_executor=executor,
        screenshot_capture=SimpleNamespace(capture=lambda: b"inert screenshot"),
        front_reader=lambda: front, rea_store=SimpleNamespace(list_tools=lambda: []))
    decisions = iter([{"status": "continue", "action": "type", "text": "hunter2"},
                      {"status": "done", "thought": "finished"}])
    orch._decide = lambda *a: next(decisions)
    # Settling belongs outside the recorded actuation window.
    orch._settle_after = lambda *a: clock.__setitem__(0, 999.) or 0
    orch._sleep = lambda *a: None
    monkeypatch.setattr(O.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(O.time, "time_ns", lambda: 1700000000000000000)
    output = orch.run("type a credential", recorder=recorder)
    return orch, output


@pytest.mark.parametrize("recording", ["none", "working", "broken"])
def test_orchestrator_enabled_trace_brackets_only_input_and_is_independent_of_workflow_recorder(tmp_path, monkeypatch, recording):
    from unittest.mock import Mock
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "rea-fast")
    recorder = None if recording == "none" else Mock()
    if recording == "broken":
        recorder.record_action.side_effect = RuntimeError("workflow recorder unavailable")
    orch, output = _gui_orchestrator(monkeypatch, {"success": True}, recorder)
    paths = list((tmp_path / "data" / "workflows" / "rea_candidates").glob("task_*.json"))
    assert len(paths) == 1 and "finished" in output
    row, = T.load_candidate_trace(paths[0])["ui_actuation_trace"]
    assert row["timestamp_monotonic"] == 100. and row["end_monotonic"] == 100.25
    assert row["wall_time_ms"] == 1700000000000 and row["params_redacted"] == {"text": T.REDACTED, "press_enter": False}
    assert orch.history[0]["actuation"] == {"success": True}


@pytest.mark.parametrize("enabled,result", [
    (False, {"success": True}),
    (True, {"success": False, "gated": True}),
    (True, {"success": False, "blocked": True, "error": "boundary"}),
])
def test_orchestrator_disabled_gated_or_blocked_actions_never_create_candidate_trace(tmp_path, monkeypatch, enabled, result):
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "rea-fast" if enabled else "default")
    _gui_orchestrator(monkeypatch, result)
    assert not (tmp_path / "data" / "workflows" / "rea_candidates").exists()


@pytest.fixture
def live_capture(tmp_path, monkeypatch):
    from anyuse.execution import rea_mapping as R, rea_runtime_bridge as B, rea_gui_probe as G
    from anyuse.execution.actions import ActionExecutor
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    monkeypatch.setattr(R, "assert_guest", lambda: None)  # Inert collector boundary; no live process exists.
    monkeypatch.setattr(R, "host_allowed", lambda: False)
    clock, events = [100.0], []
    monkeypatch.setattr(T.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(T.time, "time_ns", lambda: 1700000000000000000)
    bundle = (tmp_path / "hunter2.app").resolve()
    executable = bundle / "Contents/MacOS/hunter2"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"inert source bytes, never loaded")
    epoch = {"bundle": str(bundle), "bundle_id": "com.example.Canvas", "executable": str(executable),
             "pid": 123, "uid": os.getuid(), "launch_time": 1000.25}
    front = {"bundle_id": epoch["bundle_id"], "pid": epoch["pid"], "name": "hunter2", "window_title": "hunter2"}
    pointer = "0x123456789abcdef"
    calls = [{"offset": "0x20", "x0": pointer, "scalar_args": [4815162342]},
             {"offset": "0x40", "x0": pointer},
             {"offset": "0x50", "x0": pointer, "vtable_slot_offset": 16, "this_adjustment": 8}]
    class Probe:
        target = {"bundle": str(bundle), "bundle_id": epoch["bundle_id"],
                  "module": {"file": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                             "bytes": executable.stat().st_size}}
        active = False
        mutate_before = None
        mutate_after = None
        fail = None
        def before(self, safe_front, **kwargs):
            events.append("before")
            assert safe_front == {"bundle_id": epoch["bundle_id"], "pid": epoch["pid"]}
            assert set(kwargs) == {"action", "expected_identity", "deadline_monotonic", "limits"}
            assert kwargs["expected_identity"] == epoch and kwargs["limits"]["max_rows"] == 256
            self.active = True
            self.captured_calls = []
            if self.fail == "before":
                raise RuntimeError("hunter2 must not be logged")
            value = {"format": G.BEFORE, "identity": copy.deepcopy(epoch), "module": copy.deepcopy(self.target["module"]),
                     "baseline_calls": [{"offset": "0x20"}],
                     "observed_getters": [{"offset": "0x10", "returned_x0": pointer, "vptr_rva": "0x30"}],
                     "read_only": True, "armed": True, "baseline_started_monotonic": clock[0] - .2,
                     "baseline_finished_monotonic": clock[0] - .1, "armed_monotonic": clock[0]}
            if self.mutate_before is not None:
                self.mutate_before(value)
            return value
        def after(self, **kwargs):
            events.append("after")
            assert self.active
            if self.fail == "after":
                raise RuntimeError("hunter2 must not be logged")
            value = {"format": G.AFTER, "identity": copy.deepcopy(epoch), "after_identity": copy.deepcopy(epoch),
                     "module": copy.deepcopy(self.target["module"]), "window": {
                         "started_monotonic": kwargs["started_monotonic"], "ended_monotonic": kwargs["ended_monotonic"]},
                     "actuated_calls": copy.deepcopy(self.captured_calls), "complete": True, "read_only": True}
            if self.mutate_after is not None:
                self.mutate_after(value)
            return value
        def close(self, **kwargs):
            events.append("close")
            self.active = False
            if self.fail == "close":
                raise RuntimeError("hunter2 must not be logged")
            return {"closed": True}
    probe = Probe()
    def identity_reader(*args):
        return copy.deepcopy(epoch)
    monkeypatch.setattr(B, "process_identity", identity_reader)
    def backend_action(**params):
        events.append("execute")
        if probe.active:
            probe.captured_calls = copy.deepcopy(calls)
        clock[0] += .25
    backend = SimpleNamespace(type_text_at=backend_action, click_at=backend_action)
    trace = T.ReaUiTraceCapture("type hunter2", directory=tmp_path / "trace", mode="live", differential_probe=probe)
    return SimpleNamespace(trace=trace, probe=probe, executor=ActionExecutor(backend=backend), epoch=epoch,
        front=front, clock=clock, events=events, pointer=pointer, calls=calls, executable=executable)


def _actuate(env):
    return env.trace.execute(env.executor, "type_text_at", {"text": "hunter2", "press_enter": False},
                             env.front, front_reader=lambda: env.front)


@pytest.mark.parametrize("mode,effort,escalated,expected", [
    ("off", "rea-fast", True, None), ("windows", "default", False, "windows"),
    ("live", "default", False, "live"), ("auto", "default", False, None),
    ("auto", "default", True, "windows"), ("auto", "rea-fast", False, "live"),
    ("auto", "rea-strict", False, "live"),
])
def test_explicit_capture_modes_and_auto_selection(tmp_path, monkeypatch, mode, effort, escalated, expected):
    monkeypatch.setenv("ANYUSE_REA_GUI_CAPTURE", mode)
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", effort)
    tracker = SimpleNamespace(path=tmp_path / "workflows/vision_fallbacks.json",
                             rea_escalation_status=lambda task: {"escalate_to_rea": escalated})
    trace = T.ReaUiTraceCapture.for_task("task", tracker=tracker)
    assert (trace.mode if trace is not None else None) == expected
    assert not tracker.path.parent.exists()


def test_live_probe_brackets_actual_gui_and_ranks_differential_candidates(live_capture, monkeypatch):
    from anyuse.execution import rea_native_receiver_v2 as V2
    env = live_capture
    correlate, seen = V2.correlate_differential_trace, []
    def rank(**kwargs):
        seen.append(copy.deepcopy(kwargs))
        return correlate(**kwargs)
    monkeypatch.setattr(V2, "correlate_differential_trace", rank)
    result = _actuate(env)
    assert result["success"] and env.events == ["before", "execute", "after", "close"]
    assert len(seen) == 1 and seen[0]["actuated_calls"] == env.calls
    assert "target_input_value" not in seen[0]
    value = T.load_candidate_trace(env.trace.path)
    assert value["basis"] == "historical_gui_differential_hypotheses"
    row, = value["ui_actuation_trace"]
    assert row["timestamp_monotonic"] == 100 and row["end_monotonic"] == 100.25
    assert row["differential_capture"]["status"] == "captured"
    assert row["differential_capture"]["process_epoch"]["launch_time"] == env.epoch["launch_time"]
    assert [(item["consumer_offset"], item["score"]) for item in row["differential_candidates"]] == [
        ("0x50", 6), ("0x40", 5), ("0x20", 2)]
    raw = env.trace.path.read_text()
    for private in ("hunter2", env.pointer, "4815162342", "receiver_address_ephemeral", "scalar_args", str(env.executable)):
        assert private not in raw
    assert row["differential_capture"]["proof_created"] is False


@pytest.mark.parametrize("failure", ["before", "after", "close"])
def test_probe_failures_keep_gui_window_and_never_log_native_text(live_capture, caplog, failure):
    env = live_capture
    env.probe.fail = failure
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["status"] == "degraded"
    assert "differential_candidates" not in row and env.events.count("execute") == 1
    assert env.events.count("close") == 1 and "hunter2" not in caplog.text + env.trace.path.read_text()
    if failure == "close":
        assert env.trace.differential_probe is None


def test_live_mode_without_probe_degrades_to_historical_windows(tmp_path, caplog):
    trace = T.ReaUiTraceCapture("task", directory=tmp_path / "trace", mode="live")
    trace.actuate("click_at", {"x": 1}, {"bundle_id": "com.example.Canvas", "pid": 123}, lambda: {"success": True})
    row, = T.load_candidate_trace(trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["reason"] == "probe_unavailable"
    assert "differential_candidates" not in row and "probe_unavailable" in caplog.text


def test_windows_mode_never_contacts_attached_probe(live_capture):
    env = live_capture
    env.trace.mode = "windows"
    assert _actuate(env)["success"] and env.events == ["execute"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert "differential_capture" not in row


@pytest.mark.parametrize("phase", ["before", "after", "actual_epoch"])
def test_live_capture_rejects_process_epoch_changes_without_rewriting_gui_result(live_capture, phase):
    env = live_capture
    if phase == "before":
        env.probe.mutate_before = lambda value: value["identity"].update(launch_time=999.)
    elif phase == "after":
        env.probe.mutate_after = lambda value: value["after_identity"].update(launch_time=999.)
    else:
        original = env.probe.after
        def change(**kwargs):
            value = original(**kwargs)
            env.epoch["launch_time"] += 1
            return value
        env.probe.after = change
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["status"] == "degraded" and "differential_candidates" not in row
    assert env.events.count("execute") == 1


@pytest.mark.parametrize("damage", ["module", "unarmed", "stale", "rows", "fields", "window"])
def test_incomplete_unbounded_or_unattributed_probe_data_degrades_to_windows(live_capture, damage):
    env = live_capture
    if damage == "module":
        env.probe.mutate_after = lambda value: value["module"].update(sha256="0" * 64)
    elif damage == "unarmed":
        env.probe.mutate_before = lambda value: value.update(armed=False)
    elif damage == "stale":
        env.probe.mutate_before = lambda value: value.update(baseline_started_monotonic=0., baseline_finished_monotonic=.1)
    elif damage == "rows":
        env.probe.mutate_after = lambda value: value.update(actuated_calls=[{"offset": "0x20", "x0": env.pointer}] * 257)
    elif damage == "fields":
        env.probe.mutate_after = lambda value: value["actuated_calls"][0].update(text="hunter2")
    else:
        env.probe.mutate_after = lambda value: value["window"].update(ended_monotonic=999.)
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["status"] == "degraded" and "differential_candidates" not in row


def test_differential_budget_keeps_plain_window_instead_of_stranding_artifact(live_capture, monkeypatch):
    env = live_capture
    monkeypatch.setattr(T, "MAX_BYTES", 850)
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["reason"] == "candidate_budget"
    assert "differential_candidates" not in row and env.trace.truncated


def test_live_candidate_limit_and_dedup_do_not_persist_ephemeral_receivers(live_capture):
    env = live_capture
    env.calls[:] = [{"offset": hex(0x100 + i), "x0": env.pointer} for i in range(20)]
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert len(row["differential_candidates"]) == 8
    assert row["differential_capture"]["candidates_truncated"] is True
    assert row["differential_capture"]["matched_candidate_count"] == 20


@pytest.mark.parametrize("gate", ["drive", "boundary"])
def test_native_probe_does_not_run_before_actuator_policy_admission(live_capture, monkeypatch, gate):
    env = live_capture
    if gate == "drive":
        monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
    else:
        env.executor.set_compliance_checker(SimpleNamespace(check_action=lambda descriptor: (False, "denied")))
    result = _actuate(env)
    assert not result["success"] and env.events == [] and not env.trace.path.exists()


def test_opaque_actuators_capture_windows_without_lending_unverified_native_access(live_capture):
    env = live_capture
    result = env.trace.execute(SimpleNamespace(execute=lambda *a: {"success": True}), "click_at", {}, env.front)
    assert result["success"] and not env.events
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["reason"] == "actuator_capture_unavailable"


def test_probe_is_closed_when_gui_dispatch_raises_without_sending_again(live_capture):
    env = live_capture
    def send():
        env.events.append("execute")
        raise RuntimeError("actuator exception")
    with pytest.raises(RuntimeError, match="actuator exception"):
        env.trace.actuate("click_at", {}, env.front, send)
    assert env.events == ["before", "execute", "close"] and not env.trace.path.exists()


def test_probe_cannot_move_focus_then_send_stale_gui_coordinates(live_capture):
    env = live_capture
    env.probe.mutate_before = lambda value: env.front.update(pid=999)
    result = env.trace.execute(env.executor, "click_at", {}, env.front, front_reader=lambda: env.front)
    assert not result["success"] and result["reason_code"] == "focus_changed"
    assert env.events == ["before", "close"]


def test_real_probe_runs_through_state_graph_coordinate_dispatch(live_capture):
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.plan_types import PlannedStep
    env = live_capture
    executor = object.__new__(StateGraphExecutor)
    executor._action_executor, executor._rea_ui_trace = env.executor, env.trace
    executor._rea_front_reader = lambda: env.front
    executor._coordinate_identity_refusal = lambda step: None
    step = PlannedStep(intent="click", modality="coordinate", source="workflow", target_element="",
                       command={"action": "click", "x": 1, "y": 2})
    result = executor._execute_coordinate(step)
    assert result["success"] and env.events == ["before", "execute", "after", "close"]
    assert T.load_candidate_trace(env.trace.path)["ui_actuation_trace"][0]["differential_capture"]["status"] == "captured"


def test_agent_orchestrator_injects_probe_at_the_same_actuator_boundary(live_capture, monkeypatch):
    from anyuse.execution import agent_orchestrator as O
    env = live_capture
    monkeypatch.setenv("ANYUSE_REA_GUI_CAPTURE", "live")
    monkeypatch.setattr(O, "drive_enabled", lambda: False)  # The fake backend needs no real app focus operation.
    orch = O.AgentOrchestrator(adapter=SimpleNamespace(config=None), action_executor=env.executor,
        differential_probe=env.probe, front_reader=lambda: env.front,
        screenshot_capture=SimpleNamespace(capture=lambda: b"inert screenshot"),
        rea_store=SimpleNamespace(list_tools=lambda: []))
    decisions = iter([{"status": "continue", "action": "type", "text": "hunter2"}, {"status": "done"}])
    orch._decide = lambda *args: next(decisions)
    orch._settle_after = lambda *args: 0
    orch.run("type a credential")
    row, = T.load_candidate_trace(orch.rea_ui_trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["status"] == "captured" and env.events == ["before", "execute", "after", "close"]


@pytest.mark.parametrize("damage", ["pointer", "scalar", "paths", "proof", "window", "ranking", "reason"])
def test_live_artifact_loader_rejects_private_fields_and_invalid_candidate_claims(live_capture, damage):
    env = live_capture
    _actuate(env)
    artifact = json.loads(env.trace.path.read_text())
    row = artifact["ui_actuation_trace"][0]
    if damage == "pointer":
        row["differential_candidates"][0]["receiver_address_ephemeral"] = env.pointer
    elif damage == "scalar":
        row["differential_candidates"][0]["scalar_args"] = [4815162342]
    elif damage == "paths":
        row["differential_capture"]["process_epoch"]["executable"] = str(env.executable)
    elif damage == "proof":
        row["differential_capture"]["proof_created"] = True
    elif damage == "window":
        row["differential_capture"]["action_window"]["ended_monotonic"] += 1
    elif damage == "ranking":
        row["differential_candidates"].reverse()
    else:
        row.pop("differential_candidates")
        row["differential_capture"] = {"status": "degraded", "reason": "hunter2",
            "evidence_only": True, "proof_created": False}
    env.trace.path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        T.load_candidate_trace(env.trace.path)


def test_independent_module_pin_detects_source_changes_even_when_probe_echoes_old_pin(live_capture):
    env = live_capture
    after = env.probe.after
    def update(**kwargs):
        result = after(**kwargs)
        env.executable.write_bytes(b"externally changed module")
        return result
    env.probe.after = update
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["reason"] == "module_changed" and "differential_candidates" not in row


@pytest.mark.parametrize("phase", ["before", "after", "close"])
def test_probe_deadline_overrun_cannot_create_differential_evidence(live_capture, phase):
    env = live_capture
    original = getattr(env.probe, phase)
    def overdue(*args, **kwargs):
        result = original(*args, **kwargs)
        env.clock[0] += 2
        return result
    setattr(env.probe, phase, overdue)
    assert _actuate(env)["success"] and env.events.count("execute") == 1
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["status"] == "degraded" and "differential_candidates" not in row


def test_capture_adapter_failure_after_send_does_not_repeat_an_opaque_actuator(live_capture, monkeypatch):
    env = live_capture
    calls = []
    def around(action, params, front, dispatch, **kwargs):
        dispatch()
        raise RuntimeError("capture adapter failed after sending")
    monkeypatch.setattr(env.trace, "actuate", around)
    result = env.trace.execute(SimpleNamespace(execute=lambda *args: calls.append(True) or {"success": True}),
                               "click_at", {}, env.front)
    assert result["success"] and calls == [True]
    assert len(T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]) == 1


def test_probe_drain_is_distinct_from_the_gui_dispatch_interval(live_capture):
    env = live_capture
    after = env.probe.after
    def drain(**kwargs):
        result = after(**kwargs)
        env.clock[0] += .1
        return result
    env.probe.after = drain
    assert _actuate(env)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    detail = row["differential_capture"]
    assert detail["action_window"] == {"started_monotonic": 100., "ended_monotonic": 100.25}
    assert detail["collector_window"] == {"started_monotonic": 100., "ended_monotonic": 100.35}
    assert row["end_monotonic"] == 100.25


def test_interrupted_probe_cleanup_is_quarantined_and_preserves_cancellation(live_capture):
    env = live_capture
    def interrupted(**kwargs):
        env.events.append("close")
        raise KeyboardInterrupt("cancel")
    env.probe.close = interrupted
    with pytest.raises(KeyboardInterrupt):
        _actuate(env)
    assert env.trace.probe_disabled and env.trace.differential_probe is None
    assert env.events.count("execute") == env.events.count("close") == 1


def test_probe_cannot_change_recorded_geometry_by_mutating_caller_params(live_capture):
    env = live_capture
    params = {"x": 1, "y": 2}
    env.probe.mutate_before = lambda value: params.update(x=999)
    assert env.trace.execute(env.executor, "click_at", params, env.front)["success"]
    row, = T.load_candidate_trace(env.trace.path)["ui_actuation_trace"]
    assert params["x"] == 999 and row["params_redacted"] == {"x": 1, "y": 2}


def test_front_read_failure_after_setup_refuses_stale_dispatch(live_capture):
    env = live_capture
    def unavailable():
        raise RuntimeError("front unavailable")
    result = env.trace.execute(env.executor, "click_at", {}, env.front, front_reader=unavailable)
    assert not result["success"] and result["reason_code"] == "focus_unavailable"
    assert env.events == ["before", "close"]


def test_cleanup_failure_disables_owner_probe_for_the_next_agent_run(live_capture, monkeypatch):
    from anyuse.execution import agent_orchestrator as O
    env = live_capture
    env.probe.fail = "close"
    monkeypatch.setenv("ANYUSE_REA_GUI_CAPTURE", "live")
    monkeypatch.setattr(O, "drive_enabled", lambda: False)
    orch = O.AgentOrchestrator(adapter=SimpleNamespace(config=None), action_executor=env.executor,
        differential_probe=env.probe, front_reader=lambda: env.front,
        screenshot_capture=SimpleNamespace(capture=lambda: b"inert screenshot"),
        rea_store=SimpleNamespace(list_tools=lambda: []))
    orch._settle_after = lambda *args: 0
    for _ in range(2):
        decisions = iter([{"status": "continue", "action": "type", "text": "hunter2"}, {"status": "done"}])
        orch._decide = lambda *args: next(decisions)
        orch.run("type a credential")
    assert env.events.count("before") == env.events.count("close") == 1
    assert env.events.count("execute") == 2 and orch._differential_probe is None
    row, = T.load_candidate_trace(orch.rea_ui_trace.path)["ui_actuation_trace"]
    assert row["differential_capture"]["reason"] == "probe_unavailable"


def test_trace_loader_rejects_private_values_hidden_in_duplicate_json_keys(live_capture):
    env = live_capture
    _actuate(env)
    raw = env.trace.path.read_text().replace('"proof_created": false',
        '"proof_created": "hunter2", "proof_created": false', 1)
    env.trace.path.write_text(raw)
    with pytest.raises(ValueError, match="duplicate GUI trace key"):
        T.load_candidate_trace(env.trace.path)


def test_same_process_window_change_during_probe_requires_fresh_observation(live_capture):
    env = live_capture
    env.probe.mutate_before = lambda value: env.front.update(window_title="Another window")
    result = env.trace.execute(env.executor, "click_at", {}, env.front, front_reader=lambda: env.front)
    assert not result["success"] and result["reason_code"] == "focus_changed"
    assert env.events == ["before", "close"]


def test_full_trace_stops_probe_work_but_keeps_running_gui_actions(live_capture, monkeypatch):
    env = live_capture
    monkeypatch.setattr(T, "MAX_ACTIONS", 1)
    assert _actuate(env)["success"]
    assert _actuate(env)["success"]
    assert env.events == ["before", "execute", "after", "close", "execute"]
    saved = T.load_candidate_trace(env.trace.path)
    assert saved["truncated"] is True and len(saved["ui_actuation_trace"]) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
