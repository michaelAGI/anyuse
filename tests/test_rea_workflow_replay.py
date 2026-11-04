"""Mixed UI/REA demonstrations keep literal proofs and stop every replay route."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_replay as RR
from anyuse.execution.deterministic import replay_transitions
from anyuse.execution.executor import StateGraphExecutor
from anyuse.execution.orchestrator import ExecutionOrchestrator, _recorded_graph_problem
from anyuse.execution.plan_types import PlannedStep
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UIState, UITransition
from anyuse.memory.models import Workflow, WorkflowIntent, WorkflowStep, WorkflowTemplate
from anyuse.memory.recorder import StateGraphRecorder


def command(name="rea.canvas.a0", text="literal", tier="deterministic"):
    return {"name": name, "inputs": {"label": text}, "catalog_sha256": "a" * 64,
            "verification_tier": tier}


class Store:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "status": "completed", "model_calls": 0}
        self.specs = [dict(command(), inputs=None), dict(command("rea.canvas.a1"), inputs=None)]

    def list_tools(self):
        return copy.deepcopy(self.specs)

    def execute(self, name, inputs, **kwargs):
        self.calls.append((name, copy.deepcopy(inputs), kwargs))
        return copy.deepcopy(self.result)

    def execute_plan(self, steps, *, execute_step):
        executions = []
        for index, step in enumerate(steps):
            result = execute_step(**step, expected_identity={"pid": 44, "epoch": "current"})
            executions.append(result)
            if result.get("ok") is not True:
                return dict(result, completed_steps=index, failed_step_index=index, executions=executions)
        return {"ok": True, "status": "completed", "executions": executions}


@pytest.fixture(autouse=True)
def gates(monkeypatch):
    from anyuse.execution import foreground_interlock
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    monkeypatch.setenv("ANYUSE_SYSTEM_PROMPT_MODE", "manual")
    monkeypatch.setattr(foreground_interlock, "require_clear", lambda where: None)


def step(args=None, action="rea_action"):
    return PlannedStep(intent="Insert one vertex", source="workflow", target_element="", modality="rea",
                       command={"action": action, "args": args or command()})


def test_executor_uses_catalog_pin_and_literal_inputs_without_workflow_substitutions():
    store = Store()
    args = command(text="{{var:label}}")
    executor = StateGraphExecutor(rea_store=store)
    result, verification = asyncio.run(executor.execute_checked_step(step(args)))
    assert result["success"] and result["verification_tier"] == "deterministic"
    assert store.calls == [("rea.canvas.a0", {"label": "{{var:label}}"},
                            {"expected_catalog_sha256": "a" * 64})]
    assert verification["clearly_matched"] is False


@pytest.mark.parametrize("damage", ["missing", "digest", "tier"])
def test_registration_drift_stops_without_dispatch(damage):
    store = Store()
    if damage == "missing":
        store.specs = []
    elif damage == "digest":
        store.specs[0]["catalog_sha256"] = "b" * 64
    else:
        store.specs[0]["verification_tier"] = "model_judged"
    result = RR.execute_recorded("rea_action", command(), store=store)
    assert result["outcome_kind"] == "refused" and not result["success"]
    assert store.calls == []


def test_batch_replay_keeps_process_epoch_and_fail_fast_receipts():
    store = Store({"ok": False, "status": "uncertain", "reason": "postcondition missing"})
    result = RR.execute_recorded("rea_plan", {"steps": [command(), command("rea.canvas.a1")]}, store=store)
    assert result["outcome_kind"] == "uncertain_completion"
    assert result["completed_steps"] == 0 and result["failed_step_index"] == 0
    assert len(store.calls) == 1
    assert store.calls[0][2]["expected_identity"] == {"pid": 44, "epoch": "current"}


def test_batch_checks_all_recorded_pins_before_running_prefix():
    store = Store()
    args = {"steps": [command(), command("rea.canvas.a1")]}
    args["steps"][1]["catalog_sha256"] = "b" * 64
    result = RR.execute_recorded("rea_plan", args, store=store)
    assert result["outcome_kind"] == "refused" and store.calls == []


@pytest.mark.parametrize("gate", ["drive", "boundary", "system_prompt"])
def test_replay_gates_precede_runtime_dispatch(gate, monkeypatch):
    from anyuse.execution import system_prompt_policy
    store = Store()
    checker = None
    if gate == "drive":
        monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
    elif gate == "boundary":
        checker = SimpleNamespace(check_action=lambda action: (False, "outside allowed app"))
    else:
        monkeypatch.setattr(system_prompt_policy, "resolve_configured", lambda *args, **kw: {
            "attempted": True, "reobserve_required": True, "next_action": "observe"})
    result = RR.execute_recorded("rea_action", command(), store=store, checker=checker)
    assert result["outcome_kind"] == "refused" and store.calls == []


def test_batch_checks_interlock_without_answering_midplan_prompts(monkeypatch):
    from anyuse.execution import foreground_interlock, system_prompt_policy
    store = Store()
    boundaries = []
    def resolve(where, **kwargs):
        boundaries.append(where)
        return {"attempted": False}
    def clear(where):
        if len(store.calls) == 1:
            raise PermissionError("a system prompt appeared after the first operation")
    monkeypatch.setattr(system_prompt_policy, "resolve_configured", resolve)
    monkeypatch.setattr(foreground_interlock, "require_clear", clear)
    result = RR.execute_recorded("rea_plan", {"steps": [command(), command("rea.canvas.a1")]}, store=store)
    assert result["completed_steps"] == 1 and result["failed_step_index"] == 1
    assert boundaries == ["rea_replay.before_operation"] and len(store.calls) == 1


def test_prompt_decision_calls_are_not_lost_when_replay_stops(monkeypatch):
    from anyuse.execution import system_prompt_policy
    monkeypatch.setattr(system_prompt_policy, "resolve_configured", lambda *args, **kw: {
        "attempted": True, "reobserve_required": True, "decision": {"model_calls": 2}})
    store = Store()
    result = RR.execute_recorded("rea_action", command(), store=store)
    assert result["outcome_kind"] == "refused" and result["model_calls"] == 2 and store.calls == []


def test_rea_failure_never_retries_through_alternate_ui_implementation():
    store = Store({"ok": False, "status": "refused", "reason": "proof changed"})
    transition = UITransition(from_state="a", to_state="b", intent="insert", implementations=[
        TransitionImpl(modality="coordinate", command={"action": "click", "x": 5, "y": 5},
                       times_used=10, times_succeeded=10),
        TransitionImpl(modality="rea", command=step().command)])
    calls = []
    executor = StateGraphExecutor(rea_store=store,
        action_executor=SimpleNamespace(execute=lambda *args: calls.append(args)))
    result = asyncio.run(replay_transitions([transition], executor))
    assert result["outcome_kind"] == "refused" and calls == []
    assert len(store.calls) == 1


@pytest.mark.parametrize("value", [{"x": float("nan")}, {"x": (1, 2)}, {1: "key"}])
def test_recorded_inputs_require_bounded_pure_json(value):
    args = command()
    args["inputs"] = value
    with pytest.raises((ValueError, TypeError)):
        RR.literal_command("rea_action", args)


def test_recorded_plan_rejects_runtime_handles_before_adding_a_workflow_step():
    recorder = _recorder()
    with pytest.raises(ValueError, match="runtime handles"):
        recorder.record_rea_plan([command(text="$v0")])
    assert recorder._legacy_recorder.steps == [] and recorder.template_steps == []


def _recorder():
    recorder = object.__new__(StateGraphRecorder)
    recorder._state_path, recorder._states_discovered = [], {}
    recorder._last_after, recorder._previous_state_id, recorder._pending = None, None, None
    recorder._transitions_discovered, recorder._template_steps = [], []
    recorder._template_params, recorder._steps_meta = [], []
    recorder._current_app, recorder._current_bundle_id, recorder._current_pid = "Canvas", "org.canvas", 44
    recorder._ax_tree, recorder._current_url = None, ""
    recorder._refresh_app_context = lambda: None
    recorder._take_snapshot = lambda: SimpleNamespace(name="viewport")
    recorder._snapshot_to_ui_state = lambda snapshot: UIState(state_id="canvas", app="Canvas")
    steps = []
    recorder._legacy_recorder = SimpleNamespace(steps=steps, record_action=lambda **kw: steps.append(
        WorkflowStep(action=kw["action"], args=kw["args"])))
    return recorder


def test_mixed_recording_keeps_ui_transitions_and_literal_rea_transition():
    recorder = _recorder()
    recorder.record_action("key_combination", {"keys": "cmd+n"}, defer_after=True)
    recorder.record_after_state()
    inputs = {"label": "vertex"}
    recorder.record_rea_action("rea.canvas.a0", inputs, catalog_sha256="a" * 64)
    inputs["label"] = "caller mutation"
    recorder.record_after_state()
    recorder.record_action("key_combination", {"keys": "cmd+s"}, defer_after=True)
    recorder.record_after_state()
    assert [s.action for s in recorder._legacy_recorder.steps] == ["key_combination", "rea_action", "key_combination"]
    transitions = recorder._transitions_discovered
    assert [t.implementations[0].modality for t in transitions] == ["coordinate", "rea", "coordinate"]
    assert transitions[1].implementations[0].command["args"]["inputs"] == {"label": "vertex"}
    graph = recorder._recorded_graph()
    assert len(graph["transitions"]) == 3 and len(recorder.template_steps) == 3
    template = WorkflowTemplate(id="mixed", name="mixed", steps=recorder.template_steps)
    assert _recorded_graph_problem(template, StateGraph.from_dict(graph)) is None


def test_model_judged_recording_retains_tier_and_cannot_be_batched():
    recorder = _recorder()
    args = command(tier="model_judged")
    recorder.record_rea_action(args["name"], args["inputs"], catalog_sha256=args["catalog_sha256"],
                               verification_tier="model_judged")
    recorder.record_after_state()
    assert recorder._transitions_discovered[0].implementations[0].command["args"]["verification_tier"] == "model_judged"
    with pytest.raises(ValueError, match="deterministic"):
        recorder.record_rea_plan([args])


def test_recorded_mixed_graph_replays_each_modality_in_its_original_order():
    recorder = _recorder()
    recorder.record_action("key_combination", {"keys": "cmd+n"}, defer_after=True)
    recorder.record_after_state()
    recorder.record_rea_plan([command(), command("rea.canvas.a1")])
    recorder.record_after_state()
    recorder.record_action("key_combination", {"keys": "cmd+s"}, defer_after=True)
    recorder.record_after_state()
    graph = recorder._recorded_graph()
    template = WorkflowTemplate(id="mixed", name="mixed", steps=recorder.template_steps,
                                 state_path=recorder._state_path, recorded_graph=graph)
    store, calls = Store(), []
    executor = StateGraphExecutor(rea_store=store, drift_gate_enabled=True)
    executor._execute_coordinate = lambda step: calls.append(step.command["keys"]) or {"success": True}
    result = asyncio.run(object.__new__(ExecutionOrchestrator).replay_deterministic(template, executor=executor))
    assert result["success"] and result["path_len"] == 3
    assert calls == ["cmd+n", "cmd+s"] and len(store.calls) == 2


@pytest.mark.parametrize("mode,kind", [("disabled", "refused"), ("miss", "incomplete"),
                                        ("exception", "uncertain_completion")])
def test_mixed_replay_cannot_fall_through_to_a_model_after_a_miss_or_exception(monkeypatch, mode, kind):
    from anyuse.execution import orchestrator as O
    from anyuse.context import live_accessibility, macos
    monkeypatch.setattr(macos, "MacOSContext", lambda: SimpleNamespace())
    monkeypatch.setattr(live_accessibility, "FrontmostAccessibilityTree", lambda *a: SimpleNamespace(snapshot=lambda: None))
    monkeypatch.setattr(O, "_deterministic_replay_enabled", lambda: mode != "disabled")
    graph = StateGraph(states={"a": UIState(state_id="a", app="Canvas"),
                               "b": UIState(state_id="b", app="Canvas")}, transitions=[
        UITransition(from_state="a", to_state="b", intent="insert", implementations=[
            TransitionImpl(modality="rea", command=step().command)])])
    template = WorkflowTemplate(id="mixed", name="mixed", steps=[
        WorkflowIntent(intent="insert", target_element="", expected_before_state="a", expected_after_state="b")])
    orchestrator = object.__new__(ExecutionOrchestrator)
    orchestrator._verbose = False
    orchestrator._planner = SimpleNamespace(plan=lambda **kw: pytest.fail("a model must never resume a REA recording"))
    async def replay(*a, **kw):
        if mode == "exception":
            raise RuntimeError("receipt interrupted after an external effect")
        return {"success": False, "steps": [{"success": True}, {"success": False}]}
    orchestrator.replay_deterministic = replay
    result = asyncio.run(orchestrator._execute_workflow("insert", template, {}, graph, None))
    assert not result.success and result.outcome_kind == kind


def test_legacy_player_has_explicit_store_and_never_hands_rea_failure_to_agent(monkeypatch, tmp_path):
    import sys
    sys.modules.setdefault("pyautogui", SimpleNamespace())
    from anyuse.memory.player import WorkflowPlayer
    store = Store({"ok": False, "status": "uncertain"})
    called = []
    storage = SimpleNamespace(get_workflow_dir=lambda wid: tmp_path)
    player = WorkflowPlayer(SimpleNamespace(execute=lambda *args: called.append(args)), rea_store=store,
        agent=SimpleNamespace(run=lambda *args: called.append(args)), storage=storage)
    wf = Workflow(id="mixed", name="mixed", steps=[WorkflowStep(action="rea_action", args=command(), optional=True)])
    result = player.play(wf, prepare_window=False)
    assert not result.success and result.status.value == "uncertain"
    assert len(store.calls) == 1 and called == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
