"""Learned replay keeps the demonstrated action order, even within one UI state.

All dispatch is a spy: no desktop, model, network, or application is used.
"""
import asyncio
import pathlib
import sys
import tempfile
from types import SimpleNamespace

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-replay-tests-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_ROOT, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_ROOT))

import pytest

from anyuse.execution.orchestrator import ExecutionOrchestrator, OrchestratorResult
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UIState, UITransition
from anyuse.memory.models import WorkflowIntent, WorkflowTemplate


class _Executor:
    def __init__(self):
        self.steps = []

    async def _execute_step(self, step):
        self.steps.append(step)
        return {"success": True, "llm_calls": 0}


def _orchestrator():
    # Only replay_deterministic is used. Its injected executor avoids platform
    # construction; a model client must never be needed for this path.
    return object.__new__(ExecutionOrchestrator)


def _recording(rows):
    graph = StateGraph()
    workflow = WorkflowTemplate(id="test", name="learned")
    for before, after, intent, command in rows:
        graph.add_state(UIState(state_id=before, app="Test"))
        graph.add_state(UIState(state_id=after, app="Test"))
        graph.add_transition(UITransition(
            from_state=before, to_state=after, intent=intent,
            implementations=[TransitionImpl(modality="coordinate", command=command)]))
        workflow.steps.append(WorkflowIntent(
            intent=intent, target_element="", expected_before_state=before, expected_after_state=after))
        if not workflow.state_path:
            workflow.state_path.append(before)
        if workflow.state_path[-1] != after:
            workflow.state_path.append(after)
    return workflow, graph


def _replay(workflow, graph, executor=None, **kwargs):
    executor = executor or _Executor()
    result = asyncio.run(_orchestrator().replay_deterministic(
        workflow, state_graph=graph, executor=executor, **kwargs))
    return result, executor


def test_learned_launch_and_typing_replay_all_intents_in_order():
    workflow, graph = _recording([
        ("terminal", "spotlight", "open search", {"action": "key_combination", "keys": "cmd+space"}),
        ("spotlight", "spotlight", "find TextEdit", {"action": "type_text_at", "text": "TextEdit"}),
        ("spotlight", "editor", "launch", {"action": "key_combination", "keys": "enter"}),
        ("editor", "editor", "write document", {"action": "type_text_at", "text": "Hello from AnyUse"}),
    ])
    result, executor = _replay(workflow, graph)
    assert result["success"] is True and result["llm_calls"] == 0
    assert result["path_len"] == 4
    assert [step.intent for step in executor.steps] == [step.intent for step in workflow.steps]
    assert executor.steps[-1].input_data == "Hello from AnyUse"


def test_single_state_recording_repeats_the_same_key_twice():
    workflow, graph = _recording([
        ("editor", "editor", "move cursor", {"action": "key_combination", "keys": "right"}),
        ("editor", "editor", "move cursor", {"action": "key_combination", "keys": "right"}),
    ])
    assert workflow.state_path == ["editor"]
    result, executor = _replay(workflow, graph)
    assert result["success"] is True and result["path_len"] == 2
    assert [step.command["keys"] for step in executor.steps] == ["right", "right"]


def test_unrelated_shorter_graph_route_cannot_replace_the_recorded_actions():
    workflow, graph = _recording([
        ("start", "dialog", "open dialog", {"action": "key_combination", "keys": "cmd+n"}),
        ("dialog", "end", "confirm dialog", {"action": "key_combination", "keys": "enter"}),
    ])
    graph.add_transition(UITransition(
        from_state="start", to_state="end", intent="some other operation",
        implementations=[TransitionImpl(modality="coordinate", command={"action": "click"})]))
    result, executor = _replay(workflow, graph)
    assert result["success"] is True
    assert [step.intent for step in executor.steps] == ["open dialog", "confirm dialog"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_incomplete_or_ambiguous_recording_dispatches_nothing(ambiguous):
    workflow, graph = _recording([
        ("start", "middle", "first", {"action": "key_combination", "keys": "cmd+n"}),
        ("middle", "end", "second", {"action": "key_combination", "keys": "enter"}),
    ])
    if ambiguous:
        graph.transitions.append(UITransition(
            from_state="middle", to_state="end", intent="second",
            implementations=[TransitionImpl(modality="coordinate", command={"action": "click"})]))
    else:
        graph.transitions.pop()
    result, executor = _replay(workflow, graph)
    assert result["success"] is False and result["llm_calls"] == 0
    assert result["step_index"] == 1 and executor.steps == []


def test_runtime_parameters_reach_the_compiled_request_without_mutating_it():
    workflow, graph = _recording([("start", "end", "lookup", {})])
    implementation = graph.transitions[0].implementations[0]
    implementation.modality = "api"
    implementation.command = {"url": "https://example.invalid/search?q={query}"}
    result, executor = _replay(workflow, graph, params={"query": "new value"})
    assert result["success"] is True
    assert executor.steps[0].command["bound_params"] == {"query": "new value"}
    assert implementation.command == {"url": "https://example.invalid/search?q={query}"}


def test_workflow_execution_forwards_runtime_parameters_to_deterministic_replay(monkeypatch):
    from anyuse.context import macos
    from anyuse.execution import orchestrator as module

    def read_pid(*, live=False):
        assert live is True
        return None

    monkeypatch.setattr(macos, "MacOSContext", lambda: SimpleNamespace(get_frontmost_pid=read_pid))
    monkeypatch.setattr(module, "_deterministic_replay_enabled", lambda: True)
    orchestrator = _orchestrator()
    orchestrator._verbose = False
    received = []

    async def replay(workflow, **kwargs):
        received.append(kwargs.get("params"))
        return {"success": True, "llm_calls": 0, "path_len": 1}

    orchestrator.replay_deterministic = replay
    result = asyncio.run(orchestrator._execute_workflow(
        "look up", WorkflowTemplate(id="test", name="learned"),
        {"query": "new value"}, StateGraph(), None))
    assert result.success is True and received == [{"query": "new value"}]


def test_workflow_observations_follow_the_app_launched_during_replay(monkeypatch):
    from anyuse.context import accessibility, macos
    from anyuse.execution import orchestrator as module

    active = [10]

    def read_pid(*, live=False):
        assert live is True
        return active[0]

    monkeypatch.setattr(macos, "MacOSContext", lambda: SimpleNamespace(get_frontmost_pid=read_pid))
    monkeypatch.setattr(module, "_deterministic_replay_enabled", lambda: True)

    class Tree:
        def __init__(self, pid):
            self.pid = pid

        def snapshot(self):
            return SimpleNamespace(app_name=f"App {self.pid}", to_json=lambda: "{}")

    monkeypatch.setattr(accessibility, "AccessibilityTree", Tree)
    orchestrator = _orchestrator()
    orchestrator._verbose = False

    async def replay(workflow, **kwargs):
        tree = kwargs["ax_tree"]
        assert tree.snapshot().app_name == "App 10"
        active[0] = 20
        assert tree.snapshot().app_name == "App 20"
        return {"success": True, "llm_calls": 0, "path_len": 2}

    orchestrator.replay_deterministic = replay
    result = asyncio.run(orchestrator._execute_workflow(
        "launch app", WorkflowTemplate(id="test", name="learned"), {}, StateGraph(), None))
    assert result.success is True


@pytest.mark.parametrize("entry_known,expected_app", [(True, "Terminal"), (False, "TextEdit")])
def test_replay_prepares_the_recorded_entry_app(entry_known, expected_app):
    graph = StateGraph()
    if entry_known:
        graph.add_state(UIState(state_id="terminal", app="Terminal"))
    workflow = WorkflowTemplate(
        id="test", name="learned", required_app="TextEdit", state_path=["terminal", "editor"])
    prepared, restored = [], []
    orchestrator = _orchestrator()
    orchestrator._verbose = False
    orchestrator._environment = SimpleNamespace(
        prepare=lambda app: prepared.append(app) or "saved",
        restore=lambda saved: restored.append(saved))

    async def execute(*args):
        return OrchestratorResult(success=True)

    orchestrator._execute_workflow = execute
    result = asyncio.run(orchestrator.run(
        "launch app", workflow, app_guide=SimpleNamespace(get_state_graph=lambda: graph)))
    assert result.success is True and prepared == [expected_app] and restored == ["saved"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
