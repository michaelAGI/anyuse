"""A learned recording preserves the demonstrated gesture and text layout."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

_PKGROOT = TemporaryDirectory(prefix="anyuse-recording-semantics-")
Path(_PKGROOT.name, "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, _PKGROOT.name)

import pytest

from anyuse.execution.agent_orchestrator import AgentOrchestrator
from anyuse.guides.state_graph import TransitionImpl, UIState, UITransition
from anyuse.memory.models import Workflow, WorkflowIntent, WorkflowStep
from anyuse.memory.recorder import StateGraphRecorder


def test_dragging_between_toolbar_buttons_stays_a_drag():
    recorder = object.__new__(StateGraphRecorder)
    start = {"canonical_role": "button", "name": "Undo", "identifier": "undo"}
    end = {"canonical_role": "button", "name": "Save", "identifier": "save"}
    recorder._element_at = lambda x, y: (
        SimpleNamespace(role="AXButton", title="Undo" if x == 10 else "Save",
                        identifier="undo" if x == 10 else "save"),
        start if x == 10 else end)
    implementations = recorder._drag_implementations({"x1": 10, "y1": 20, "x2": 30, "y2": 40})
    assert [implementation.modality for implementation in implementations] == ["coordinate"]
    assert implementations[0].command == {
        "action": "drag", "x1": 10, "y1": 20, "x2": 30, "y2": 40,
        "from_selector": start, "to_selector": end}


def test_saving_an_intentional_blank_first_line_preserves_the_return():
    recorder = object.__new__(StateGraphRecorder)
    steps = [WorkflowStep(action="key_combination", args={"keys": "enter"}),
             WorkflowStep(action="type_text_at", args={"text": "Hello"})]
    workflow = Workflow.create_new("blank_first_line", "Insert a blank first line, then type Hello")
    workflow.steps = steps
    recorder._legacy_recorder = SimpleNamespace(steps=steps, save=lambda: workflow)
    recorder._pending = None
    recorder._template_steps = [
        WorkflowIntent(intent="Insert a blank first line", target_element="document",
                       expected_before_state="empty", expected_after_state="empty~newline"),
        WorkflowIntent(intent="Write greeting", target_element="document",
                       expected_before_state="empty~newline", expected_after_state="empty~greeting"),
    ]
    recorder._template_params = []
    recorder._state_path = ["empty", "empty~newline", "empty~greeting"]
    recorder._states_discovered = {sid: UIState(state_id=sid, app="TextEdit") for sid in recorder._state_path}
    recorder._transitions_discovered = [
        UITransition(from_state=intent.expected_before_state, to_state=intent.expected_after_state,
                     intent=intent.intent, implementations=[TransitionImpl(modality="coordinate", command={})])
        for intent in recorder._template_steps]
    recorder._steps_meta = [
        {"action": "key_combination", "keys": "enter", "legacy_index": 0, "blank_line": True,
         "transition": recorder._transitions_discovered[0]},
        {"action": "type_text_at", "keys": None, "legacy_index": 1, "blank_line": False,
         "transition": recorder._transitions_discovered[1]},
    ]
    recorder._last_after = ("empty~greeting", "empty")
    recorder._current_app = "TextEdit"
    recorder._target_app = ("TextEdit", "com.apple.TextEdit")
    recorder._guide_storage = None
    recorder._save_template = lambda template, legacy: None

    saved, template = recorder.save()
    assert len(saved.steps) == 2 and saved.steps[0].args == {"keys": "enter"}
    assert [intent.intent for intent in template.steps] == ["Insert a blank first line", "Write greeting"]
    assert len(recorder._transitions_discovered) == 2


def test_failed_after_state_capture_discards_recording_but_preserves_task_success():
    class Recorder:
        def __init__(self):
            self.saved = self.discarded = 0

        def start(self):
            pass

        def record_action(self, **kwargs):
            pass

        def record_after_state(self, **kwargs):
            raise RuntimeError("after-state capture failed")

        def stop(self):
            pass

        def save(self):
            self.saved += 1

        def discard(self):
            self.discarded += 1

    recorder = Recorder()
    decisions = iter([
        {"status": "continue", "action": "key", "keys": "right", "thought": "Move cursor"},
        {"status": "done", "thought": "Task complete"},
    ])
    orchestrator = AgentOrchestrator(
        adapter=object(), pixel_scale=1.0,
        action_executor=SimpleNamespace(execute=lambda *args, **kwargs: {"success": True}),
        screenshot_capture=SimpleNamespace(capture=lambda: b"fake image"),
        front_reader=lambda: {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 10})
    orchestrator._decide = lambda *args: next(decisions)
    orchestrator._running_app_names = lambda: []
    orchestrator._ensure_focus = lambda target: None
    orchestrator._settle_after = lambda *args: 0
    orchestrator._sleep = lambda seconds: None

    output = orchestrator.run("Move the cursor right", recorder=recorder)
    assert output.startswith("Task complete") and "not recorded" in output
    assert recorder.saved == 0 and recorder.discarded == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
