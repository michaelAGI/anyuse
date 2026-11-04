"""ExecutionOrchestrator wires the AX drift gate + HIL hold into the executor.

Both were built and tested but never PASSED to StateGraphExecutor, so neither was
reachable. These tests pin the wiring + the safe defaults. Offline: api_key is
forced empty so no planner / genai client is constructed.
"""
import os

import config
from anyuse.execution.hil_gate import HILGate


def _orch(**kw):
    old = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = ""  # skip ExecutionPlanner/InterventionHandler (offline)
    try:
        from anyuse.execution.orchestrator import ExecutionOrchestrator
        return ExecutionOrchestrator(api_key="", action_executor=None, **kw)
    finally:
        config.GEMINI_API_KEY = old


def test_drift_gate_default_on():
    os.environ.pop("ANYUSE_DRIFT_GATE_ENABLED", None)
    assert _orch()._drift_gate_enabled is True


def test_drift_gate_env_off():
    os.environ["ANYUSE_DRIFT_GATE_ENABLED"] = "0"
    try:
        assert _orch()._drift_gate_enabled is False
    finally:
        os.environ.pop("ANYUSE_DRIFT_GATE_ENABLED", None)


def test_hil_gate_default_dormant():
    os.environ.pop("ANYUSE_HIL_MODE", None)
    g = _orch()._hil_gate
    assert isinstance(g, HILGate) and g.mode == "off", g.mode


def test_hil_gate_reads_env_mode():
    os.environ["ANYUSE_HIL_MODE"] = "destructive"
    try:
        assert _orch()._hil_gate.mode == "destructive"
    finally:
        os.environ.pop("ANYUSE_HIL_MODE", None)


def test_explicit_overrides_win():
    o = _orch(drift_gate_enabled=False, hil_gate=HILGate(mode="all"))
    assert o._drift_gate_enabled is False and o._hil_gate.mode == "all"


def test_shared_deadline_stops_every_modality_before_dispatch():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock
    from anyuse.execution.actions import ActionExecutor
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.outcome import classify_outcome, STOP_KINDS
    from anyuse.core.restriction import ComplianceChecker, ExecutionControl
    actuator = ActionExecutor(backend=SimpleNamespace())
    actuator.set_compliance_checker(ComplianceChecker(control=ExecutionControl(0, clock=lambda: 1)))
    executor = StateGraphExecutor(action_executor=actuator)
    for modality, method in (("api", "_execute_api"), ("ax", "_execute_ax"), ("js", "_execute_js"),
                             ("applescript", "_execute_applescript"), ("coordinate", "_execute_coordinate"),
                             ("computer_use", "_execute_visual")):
        spy = Mock(return_value={"success": True})
        setattr(executor, method, spy)
        result = asyncio.run(executor._execute_step(SimpleNamespace(modality=modality, command={})))
        assert not result["success"] and classify_outcome(result) in STOP_KINDS, result
        spy.assert_not_called()


def test_native_and_script_modalities_share_action_boundary():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock
    from anyuse.execution.actions import ActionExecutor
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.core.restriction import ComplianceChecker, RestrictionPolicy
    for modality, method in (("ax", "_execute_ax"), ("js", "_execute_js"), ("applescript", "_execute_applescript")):
        actuator = ActionExecutor(backend=SimpleNamespace())
        checker = ComplianceChecker(RestrictionPolicy.from_boundary({"max_actions": 1}))
        actuator.set_compliance_checker(checker)
        executor = StateGraphExecutor(action_executor=actuator)
        spy = Mock(return_value={"success": True})
        setattr(executor, method, spy)
        step = SimpleNamespace(modality=modality, command={"action": "press"})
        assert asyncio.run(executor._execute_step(step))["success"]
        assert not asyncio.run(executor._execute_step(step))["success"]
        assert checker.actions_executed == 1 and spy.call_count == 1


def _coordinate_step():
    from anyuse.execution.plan_types import PlannedStep
    return PlannedStep(intent="press measured control", source="workflow", target_element="control",
        modality="coordinate", command={"action": "click", "x": 12, "y": 24,
                                        "selector": {"role": "AXButton", "name": "control"}})


def test_coordinate_capture_brackets_existing_actuator_and_rechecks_target():
    from types import SimpleNamespace
    from anyuse.execution.executor import StateGraphExecutor
    events = []
    front = {"name": "Canvas", "bundle_id": "org.example.Canvas", "pid": 44}
    class Trace:
        def execute(self, executor, action, params, observed, *, front_reader, before_dispatch):
            assert action == "click_at" and params == {"x": 12, "y": 24, "button": "left"}
            assert observed == front and front_reader() == front
            events.append("before")
            denied = before_dispatch()
            result = denied if denied is not None else executor.execute(action, params)
            events.append("after")
            return result
    def execute(action, params):
        events.append("execute")
        return {"success": True}
    executor = StateGraphExecutor(action_executor=SimpleNamespace(execute=execute),
        rea_ui_trace=Trace(), rea_front_reader=lambda: dict(front))
    executor._hit_test_refusal = lambda *a: events.append("hit_test")
    result = executor._execute_coordinate(_coordinate_step())
    assert result["success"] and result["method"] == "coordinate"
    assert events == ["hit_test", "before", "hit_test", "execute", "after"]


def test_capture_setup_cannot_send_into_changed_focus_or_changed_coordinate_target():
    from types import SimpleNamespace
    from anyuse.execution.executor import StateGraphExecutor
    for changed in ("focus", "target"):
        calls, phase = [], [False]
        front = {"name": "Canvas", "bundle_id": "org.example.Canvas", "pid": 44}
        class Trace:
            def execute(self, executor, action, params, observed, *, front_reader, before_dispatch):
                phase[0] = True
                if changed == "focus":
                    front["pid"] += 1
                denied = before_dispatch()
                return denied if denied is not None else executor.execute(action, params)
        executor = StateGraphExecutor(
            action_executor=SimpleNamespace(execute=lambda *a: calls.append(a)),
            rea_ui_trace=Trace(), rea_front_reader=lambda: dict(front))
        executor._hit_test_refusal = lambda *a: (("target changed", "identity")
            if changed == "target" and phase[0] else None)
        result = executor._execute_coordinate(_coordinate_step())
        assert not result["success"] and result["outcome_kind"] == "refused"
        assert calls == []


def test_capture_failure_after_dispatch_never_repeats_coordinate_action():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.actions import ActionExecutor
    from anyuse.execution.executor import StateGraphExecutor
    calls = []
    class Trace:
        def execute(self, executor, action, params, observed, *, front_reader, before_dispatch):
            def around(dispatch):
                denied = before_dispatch()
                if denied is not None:
                    return denied
                dispatch()
                raise RuntimeError("capture persistence unavailable")
            return executor.execute(action, params, around=around)
    actuator = ActionExecutor(backend=SimpleNamespace(click_at=lambda **args: calls.append(args)))
    executor = StateGraphExecutor(action_executor=actuator, rea_ui_trace=Trace())
    executor._hit_test_refusal = lambda *a: None
    with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}):
        result = executor._execute_coordinate(_coordinate_step())
    assert result["success"] and len(calls) == 1


def test_capture_failure_before_dispatch_keeps_single_original_gui_action():
    from types import SimpleNamespace
    from anyuse.execution.executor import StateGraphExecutor
    calls = []
    class Trace:
        def execute(self, *args, **kwargs):
            raise RuntimeError("capture setup unavailable")
    def execute(*args):
        calls.append(args)
        return {"success": True}
    executor = StateGraphExecutor(action_executor=SimpleNamespace(execute=execute), rea_ui_trace=Trace())
    executor._hit_test_refusal = lambda *a: None
    assert executor._execute_coordinate(_coordinate_step())["success"]
    assert len(calls) == 1


def test_native_observer_hook_is_unreachable_before_drive_boundary_and_backend_gates():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from anyuse.execution.actions import ActionExecutor
    for condition in ("drive", "boundary", "unknown"):
        backend = Mock()
        if condition == "unknown":
            backend = SimpleNamespace()
        actuator = ActionExecutor(backend=backend)
        checker = SimpleNamespace(check_action=Mock(return_value=(condition != "boundary", "denied")))
        actuator.set_compliance_checker(checker)
        around = Mock()
        with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "0" if condition == "drive" else "1"}):
            result = actuator.execute("click_at", {"x": 1, "y": 2}, around=around)
        assert not result["success"]
        around.assert_not_called()
        assert checker.check_action.call_count == (0 if condition == "drive" else 1)


def test_authorized_observer_runs_after_one_reservation_and_cannot_repeat_or_promote_action():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.actions import ActionExecutor
    from anyuse.core.restriction import ComplianceChecker, RestrictionPolicy
    for fails in (False, True):
        calls, events = [], []
        def click_at(**params):
            calls.append(params)
            events.append("backend")
            if fails:
                raise RuntimeError("inert backend failure")
        actuator = ActionExecutor(backend=SimpleNamespace(click_at=click_at))
        checker = ComplianceChecker(RestrictionPolicy.from_boundary({"max_actions": 1}))
        actuator.set_compliance_checker(checker)
        def around(dispatch):
            assert checker.actions_executed == 1
            events.append("before")
            result = dispatch()
            assert dispatch() == result
            events.append("after")
            result["success"] = True  # Cannot mutate/promote the authoritative result.
            return result
        with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}):
            result = actuator.execute("click_at", {"x": 1, "y": 2}, around=around)
        assert result["success"] is (not fails)
        assert len(calls) == 1 and checker.actions_executed == 1
        assert events == ["before", "backend", "after"]


def test_observer_failure_preserves_single_backend_dispatch_and_drive_revocation():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.actions import ActionExecutor
    for failure in ("before", "after", "revoked"):
        calls = []
        actuator = ActionExecutor(backend=SimpleNamespace(click_at=lambda **params: calls.append(params)))
        def around(dispatch):
            if failure == "before":
                raise RuntimeError("probe unavailable")
            if failure == "revoked":
                os.environ["ANYUSE_DRIVE_ENABLED"] = "0"
                return dispatch()
            dispatch()
            raise RuntimeError("probe cleanup failed")
        with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}):
            result = actuator.execute("click_at", {"x": 1, "y": 2}, around=around)
        assert result["success"] is (failure != "revoked")
        assert len(calls) == (0 if failure == "revoked" else 1)


def test_capture_setup_cannot_outlive_shared_execution_control():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.actions import ActionExecutor
    from anyuse.core.restriction import ComplianceChecker, ExecutionControl
    clock, calls = [0.0], []
    control = ExecutionControl(1, clock=lambda: clock[0])
    actuator = ActionExecutor(backend=SimpleNamespace(click_at=lambda **params: calls.append(params)))
    checker = ComplianceChecker(control=control)
    actuator.set_compliance_checker(checker)
    def around(dispatch):
        clock[0] = 2.0
        return dispatch()
    with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}):
        result = actuator.execute("click_at", {"x": 1, "y": 2}, around=around)
    assert not result["success"] and result["blocked"] and calls == []


def test_delayed_capture_cannot_reuse_changed_boundary_authority():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.actions import ActionExecutor
    from anyuse.core.restriction import ComplianceChecker, RestrictionPolicy, ExecutionControl
    for changed in ("checker", "policy", "exclude", "control", "guard"):
        calls = []
        actuator = ActionExecutor(backend=SimpleNamespace(click_at=lambda **params: calls.append(params)))
        policy = RestrictionPolicy.from_boundary({"max_actions": 1, "action_rules": {"click": "allow"}})
        checker = ComplianceChecker(policy)
        actuator.set_compliance_checker(checker)
        def around(dispatch):
            assert checker.actions_executed == 1
            if changed == "checker":
                actuator.set_compliance_checker(ComplianceChecker())
            elif changed == "policy":
                checker.policy = RestrictionPolicy.from_boundary({"action_rules": {"click": "exclude"}})
            elif changed == "exclude":
                checker.policy.action_rules["click"] = "exclude"
            elif changed == "control":
                checker.control = ExecutionControl(100, clock=lambda: 0)
            else:
                checker.guard_evaluator = lambda action: True
            return dispatch()
        with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}):
            result = actuator.execute("click_at", {"x": 1, "y": 2}, around=around)
        assert not result["success"] and result["blocked"] and calls == []
        assert "BOUNDARY changed" in result["error"] and checker.actions_executed == 1


def test_around_hook_cannot_mutate_the_authorized_gui_parameters():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.actions import ActionExecutor
    calls, checked = [], []
    params = {"x": 12, "y": 24, "text": "authorized", "press_enter": False}
    original = dict(params)
    def check_action(descriptor):
        checked.append(dict(descriptor["args"]))
        return True, ""
    actuator = ActionExecutor(backend=SimpleNamespace(type_text_at=lambda **values: calls.append(values)))
    actuator.set_compliance_checker(SimpleNamespace(check_action=check_action))
    def around(dispatch):
        params.update(x=999, y=999, text="unapproved", press_enter=True)
        return dispatch()
    with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}):
        result = actuator.execute("type_text_at", params, around=around)
    assert result["success"] and checked == [original] and calls == [original]


def test_state_graph_run_owns_one_capture_and_clears_it_on_completion():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution.orchestrator import OrchestratorResult
    from anyuse.execution.rea_ui_trace import ReaUiTraceCapture
    capture, probe, tracker = object(), object(), object()
    reader = lambda: None
    orchestrator = _orch(differential_probe=probe, rea_trace_tracker=tracker, rea_front_reader=reader)
    async def execute(*args):
        assert orchestrator._active_rea_ui_trace is capture
        return OrchestratorResult(success=True, message="done")
    orchestrator._execute_workflow = execute
    workflow = SimpleNamespace(name="workflow", required_app="", state_path=[], steps=[], recorded_graph=None)
    with patch.object(ReaUiTraceCapture, "for_task", return_value=capture) as factory:
        result = asyncio.run(orchestrator.run("task", workflow, use_clean_room=False))
    factory.assert_called_once_with("task", tracker=tracker, differential_probe=probe)
    assert result.success and orchestrator.rea_ui_trace is capture
    assert orchestrator._active_rea_ui_trace is None


def test_failed_probe_cleanup_quarantines_collector_across_state_graph_runs():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from anyuse.execution.orchestrator import OrchestratorResult
    from anyuse.execution.rea_ui_trace import ReaUiTraceCapture
    probe = object()
    orchestrator = _orch(differential_probe=probe)
    async def execute(*args):
        return OrchestratorResult(success=True, message="done")
    orchestrator._execute_workflow = execute
    workflow = SimpleNamespace(name="workflow", required_app="", state_path=[], steps=[], recorded_graph=None)
    factory = Mock(side_effect=[SimpleNamespace(probe_disabled=True), SimpleNamespace(probe_disabled=False)])
    with patch.object(ReaUiTraceCapture, "for_task", new=factory):
        assert asyncio.run(orchestrator.run("first", workflow, use_clean_room=False)).success
        assert orchestrator._differential_probe is None and orchestrator._active_rea_ui_trace is None
        assert asyncio.run(orchestrator.run("second", workflow, use_clean_room=False)).success
    assert factory.call_args_list[0].kwargs["differential_probe"] is probe
    assert factory.call_args_list[1].kwargs["differential_probe"] is None


def test_explore_propagates_child_probe_quarantine_to_persistent_orchestrator():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    probe = object()
    orchestrator = _orch(differential_probe=probe)
    child = SimpleNamespace(run=lambda task: "done", model_calls=0,
                            rea_ui_trace=SimpleNamespace(probe_disabled=True))
    with patch("anyuse.execution.agent_orchestrator.AgentOrchestrator", new=Mock(return_value=child)) as factory:
        result = asyncio.run(orchestrator.explore("task"))
    assert result.success and factory.call_args.kwargs["differential_probe"] is probe
    assert orchestrator._differential_probe is None


def test_recorded_replay_executor_receives_the_current_run_capture():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch
    from anyuse.execution import orchestrator as O
    from anyuse.guides.state_graph import StateGraph, UIState, UITransition, TransitionImpl
    capture, reader = object(), lambda: None
    orchestrator = _orch(rea_front_reader=reader)
    orchestrator._active_rea_ui_trace = capture
    orchestrator._make_visual_verifier = lambda: (None, None)
    orchestrator._make_platform = lambda tree: None
    orchestrator._gate_for = lambda workflow: None
    graph = StateGraph(states={"a": UIState(state_id="a", app="Canvas"), "b": UIState(state_id="b", app="Canvas")}, transitions=[
        UITransition(from_state="a", to_state="b", intent="click", implementations=[
            TransitionImpl(modality="coordinate", command={"action": "click", "x": 1, "y": 2})])])
    workflow = SimpleNamespace(recorded_graph=None, state_path=["a", "b"], steps=[
        SimpleNamespace(expected_before_state="a", expected_after_state="b", intent="click")])
    executor = Mock()
    with patch.object(O, "StateGraphExecutor", return_value=executor) as factory, \
            patch("anyuse.context.macos.MacOSContext", return_value=SimpleNamespace()), \
            patch("anyuse.execution.deterministic.replay_transitions", new=AsyncMock(return_value={"success": True})):
        result = asyncio.run(orchestrator.replay_deterministic(workflow, state_graph=graph, ax_tree=object()))
    assert result["success"]
    assert factory.call_args.kwargs["rea_ui_trace"] is capture
    assert factory.call_args.kwargs["rea_front_reader"] is reader


if __name__ == "__main__":
    import inspect, sys
    mod = sys.modules[__name__]
    fns = [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]
    p = f = 0
    for n, fn in fns:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:160])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
