"""Reloaded learned workflows retain their own commands after app-guide merging."""

import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

_PKGROOT = TemporaryDirectory(prefix="anyuse-recorded-graph-")
Path(_PKGROOT.name, "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, _PKGROOT.name)

import pytest

from anyuse.context.accessibility import Selector
from anyuse.execution.orchestrator import ExecutionOrchestrator, OrchestratorResult
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UIElement, UIState, UITransition, generate_state_id
from anyuse.memory.models import Workflow, WorkflowIntent, WorkflowStep, WorkflowTemplate
from anyuse.memory.recorder import StateGraphRecorder


class _Executor:
    def __init__(self):
        self.texts = []

    async def _execute_step(self, step):
        self.texts.append(step.command["text"])
        return {"success": True, "llm_calls": 0}


def _orchestrator():
    return object.__new__(ExecutionOrchestrator)


def _lookup_recording(value, directory):
    directory.mkdir()
    recorder = object.__new__(StateGraphRecorder)
    recorder._state_path, recorder._states_discovered = [], {}
    recorder._last_after, recorder._previous_state_id, recorder._pending = None, None, None
    before = UIState(state_id=generate_state_id([
        {"role": "AXTextField"}, {"role": "AXButton"}], "App"), app="App")
    after = UIState(state_id=generate_state_id([
        {"role": "AXTextField"}, {"role": "AXButton"}, {"role": "AXButton"}], "App"), app="App")
    # The original serialization failure was a Selector nested inside UIElement.
    before.elements = [UIElement(role="AXTextField", title="Search", ax_path=Selector(
        canonical_role="textfield", name="Search", raw_role="AXTextField"))]
    start = recorder._chain_before(before)
    recorder._state_path.append(start)
    implementation = TransitionImpl(modality="coordinate", command={
        "action": "type_text_at", "text": value, "press_enter": True})
    end = recorder._chain_after(after, start, [implementation])
    recorder._state_path.append(end)
    transition = UITransition(from_state=start, to_state=end, intent="Look up the supplied value",
                              implementations=[implementation])
    recorder._template_steps = [WorkflowIntent(
        intent=transition.intent, target_element="Search", input_data=value,
        expected_before_state=start, expected_after_state=end)]
    recorder._template_params = []
    recorder._transitions_discovered = [transition]
    recorder._steps_meta = [{"action": "type_text_at", "blank_line": False}]
    recorder._current_app, recorder._current_url = "App", ""
    recorder._target_app = ("App", "example.App")
    recorder._guide_storage = None
    workflow = Workflow(id=value, name=f"lookup_{value}", steps=[WorkflowStep(
        action="type_text_at", args={"text": value, "press_enter": True})])
    recorder._legacy_recorder = SimpleNamespace(
        steps=workflow.steps, save=lambda: workflow,
        storage=SimpleNamespace(get_workflow_dir=lambda workflow_id: directory))
    return recorder


def test_separate_recordings_persist_reload_and_replay_their_own_values(tmp_path):
    recordings = [_lookup_recording(value, tmp_path / value) for value in ("alpha", "beta")]
    shared = StateGraph()
    loaded = []
    for recorder in recordings:
        legacy, template = recorder.save()
        for state in recorder._states_discovered.values():
            shared.add_state(state)
        for transition in recorder._transitions_discovered:
            shared.add_transition(transition)
        path = tmp_path / legacy.id / "template.json"
        loaded.append(WorkflowTemplate.from_dict(json.loads(path.read_text())))

    assert len(shared.transitions) == 1  # The same path merges in the app guide.
    assert loaded[0].state_path == loaded[1].state_path
    assert loaded[0].recorded_graph and loaded[1].recorded_graph
    selector = next(iter(loaded[0].recorded_graph["states"].values()))["elements"][0]["ax_path"]
    assert selector["canonical_role"] == "textfield"
    executor = _Executor()
    for workflow in loaded:
        result = asyncio.run(_orchestrator().replay_deterministic(
            workflow, state_graph=shared, executor=executor))
        assert result["success"] is True and result["llm_calls"] == 0
    assert executor.texts == ["alpha", "beta"]
    assert loaded[0].recorded_graph["transitions"][0]["implementations"][0]["times_used"] == 0


def _snapshot_workflow(repeated=False):
    states = {name: UIState(state_id=name, app="App") for name in ("start", "middle", "end")}
    transitions = [
        UITransition(from_state="start", to_state="middle", intent="first", implementations=[
            TransitionImpl(modality="coordinate", command={"action": "type_text_at", "text": "alpha"})]),
        UITransition(from_state="middle", to_state="end", intent="second", implementations=[
            TransitionImpl(modality="coordinate", command={"action": "type_text_at", "text": "beta"})]),
    ]
    if repeated:
        for transition in transitions:
            transition.from_state = transition.to_state = "middle"
            transition.intent = "type value"
    return WorkflowTemplate(
        id="test", name="recorded", state_path=["middle"] if repeated else ["start", "middle", "end"],
        steps=[WorkflowIntent(intent=transition.intent, target_element="Search",
                              expected_before_state=transition.from_state,
                              expected_after_state=transition.to_state) for transition in transitions],
        recorded_graph=StateGraph(states=states, transitions=transitions).to_dict())


def test_incomplete_new_recording_cannot_adopt_an_older_workflows_commands(tmp_path):
    good = _lookup_recording("alpha", tmp_path / "alpha")
    shared = StateGraph(states=dict(good._states_discovered),
                        transitions=list(good._transitions_discovered))
    incomplete = _lookup_recording("beta", tmp_path / "beta")
    incomplete._transitions_discovered = []
    legacy, template = incomplete.save()
    loaded = WorkflowTemplate.from_dict(json.loads(
        (tmp_path / legacy.id / "template.json").read_text()))
    assert loaded.recorded_graph == {"states": {}, "transitions": []}
    executor = _Executor()
    result = asyncio.run(_orchestrator().replay_deterministic(
        loaded, state_graph=shared, executor=executor))
    assert result["success"] is False and result["outcome_kind"] == "refused"
    assert executor.texts == []


def test_raw_order_preserves_distinct_commands_with_identical_transition_keys():
    workflow = _snapshot_workflow(repeated=True)
    workflow = WorkflowTemplate.from_dict(json.loads(json.dumps(workflow.to_dict())))
    executor = _Executor()
    result = asyncio.run(_orchestrator().replay_deterministic(workflow, executor=executor))
    assert result["success"] is True and result["path_len"] == 2 and result["llm_calls"] == 0
    assert executor.texts == ["alpha", "beta"]


@pytest.mark.parametrize("damage", ["missing_transition", "reversed", "wrong_intent", "missing_state", "missing_implementation"])
def test_snapshot_mismatches_stop_before_any_command_is_dispatched(damage):
    workflow = _snapshot_workflow()
    if damage == "missing_transition":
        workflow.recorded_graph["transitions"].pop()
    elif damage == "reversed":
        workflow.recorded_graph["transitions"].reverse()
    elif damage == "wrong_intent":
        workflow.recorded_graph["transitions"][1]["intent"] = "unrelated"
    elif damage == "missing_state":
        del workflow.recorded_graph["states"]["end"]
    else:
        workflow.recorded_graph["transitions"][1]["implementations"] = []
    executor = _Executor()
    result = asyncio.run(_orchestrator().replay_deterministic(workflow, executor=executor))
    assert result["success"] is False and result["outcome_kind"] == "refused"
    assert result["llm_calls"] == 0 and executor.texts == []


def test_run_uses_its_saved_graph_without_loading_an_app_guide():
    workflow = _snapshot_workflow()
    orchestrator = _orchestrator()
    orchestrator._verbose = False
    captured = []

    async def execute(task, template, params, graph, guide):
        captured.append(graph.transitions[1].implementations[0].command["text"])
        return OrchestratorResult(success=True)

    def no_guide():
        raise AssertionError("the workflow already carries its recorded graph")

    orchestrator._execute_workflow = execute
    result = asyncio.run(orchestrator.run(
        "lookup", workflow, app_guide=SimpleNamespace(get_state_graph=no_guide), use_clean_room=False))
    assert result.success is True and captured == ["beta"]


def test_invalid_snapshot_stops_before_environment_preparation():
    workflow = _snapshot_workflow()
    workflow.recorded_graph["transitions"].pop()
    orchestrator = _orchestrator()
    orchestrator._verbose = False
    prepared = []
    orchestrator._environment = SimpleNamespace(prepare=lambda app: prepared.append(app))
    workflow.required_app = "App"
    result = asyncio.run(orchestrator.run("lookup", workflow))
    assert result.success is False and result.outcome_kind == "refused" and prepared == []


def test_template_graph_roundtrip_is_optional_and_does_not_share_nested_objects():
    legacy = WorkflowTemplate(id="legacy", name="legacy")
    assert "recorded_graph" not in legacy.to_dict()
    assert WorkflowTemplate.from_dict(legacy.to_dict()).recorded_graph == {}
    original = _snapshot_workflow()
    payload = original.to_dict()
    loaded = WorkflowTemplate.from_dict(payload)
    payload["recorded_graph"]["transitions"][0]["intent"] = "changed"
    assert original.recorded_graph["transitions"][0]["intent"] == "first"
    assert loaded.recorded_graph["transitions"][0]["intent"] == "first"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
