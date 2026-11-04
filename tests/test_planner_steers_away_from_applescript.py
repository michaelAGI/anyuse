"""The planner steers away from
"applescript" when an "ax" action already covers the step.

A live Cerebras plan for
"TextEdit write+save" chose an ``applescript`` modality for the Save step
every one of 3 runs (``click menu item "Save" of menu "File" of menu bar 1
of process "TextEdit"`` via System Events) and every one timed out in the
guest (~30 s each) — most of the measured "act" latency was that timeout,
not real work, even though the AX tree the plan was built from already
exposed that exact menu item.

``execution/planner.py``'s ``_steer_away_from_applescript`` is the
deterministic backstop: it rewrites ONLY a plain
``click menu item "X" of menu "Y" ...`` script to an "ax" press on an
AXMenuItem titled X, and only when that title is actually present in the AX
tree the plan was built from. This is a pure, offline, no-model-call test of
that function.
"""
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, str(pathlib.Path(_LINK.name)))

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, (
    "resolved 'anyuse' to the wrong checkout — (D9)")

from anyuse.execution.plan_types import ExecutionPlan, PlannedStep  # noqa: E402
from anyuse.execution.planner import _steer_away_from_applescript  # noqa: E402

# The exact AppleScript that was measured.
_SAVE_SCRIPT = 'tell application "System Events" to click menu item "Save" of menu "File" of menu bar 1 of process "TextEdit"'

_AX_DUMP_WITH_SAVE_MENU_ITEM = {
    "role": "AXApplication", "children": [
        {"role": "AXMenuBar", "children": [
            {"role": "AXMenuBarItem", "title": "File", "children": [
                {"role": "AXMenuItem", "title": "New"},
                {"role": "AXMenuItem", "title": "Save"},
            ]},
        ]},
        {"role": "AXWindow", "children": [{"role": "AXTextArea", "identifier": "First Text View"}]},
    ],
}


def _applescript_step(script):
    return PlannedStep(intent="save the document", source="workflow", target_element="File > Save",
                       modality="applescript", command={"script": script},
                       reasoning="chose applescript for the menu click")


def test_a_plain_menu_click_with_a_live_ax_match_is_rewritten_to_ax():
    plan = ExecutionPlan(steps=[_applescript_step(_SAVE_SCRIPT)])
    out = _steer_away_from_applescript(plan, json.dumps(_AX_DUMP_WITH_SAVE_MENU_ITEM))
    step = out.steps[0]
    assert step.modality == "ax", step.modality
    assert step.command == {"role": "AXMenuItem", "title": "Save", "action": "press"}, step.command
    assert "D19" in step.reasoning
    print("OK  a plain 'click menu item \"Save\"...' step with a live AXMenuItem match is rewritten to ax")


def test_no_matching_ax_menu_item_leaves_the_step_unchanged():
    ax_no_save = {"role": "AXApplication", "children": [
        {"role": "AXMenuBar", "children": [
            {"role": "AXMenuBarItem", "title": "File", "children": [{"role": "AXMenuItem", "title": "New"}]},
        ]},
    ]}
    plan = ExecutionPlan(steps=[_applescript_step(_SAVE_SCRIPT)])
    out = _steer_away_from_applescript(plan, json.dumps(ax_no_save))
    step = out.steps[0]
    assert step.modality == "applescript", "must not rewrite when the AX tree has no matching AXMenuItem"
    assert step.command == {"script": _SAVE_SCRIPT}
    print("OK  no live AXMenuItem match -> the applescript step is left completely unchanged")


def test_a_non_menu_click_script_is_never_touched():
    """This function never interprets AppleScript generally — only the exact,
    narrow 'click menu item "X" of menu "Y"' shape is eligible at all."""
    other_script = 'tell application "TextEdit" to make new document with properties {text:"hello"}'
    plan = ExecutionPlan(steps=[_applescript_step(other_script)])
    out = _steer_away_from_applescript(plan, json.dumps(_AX_DUMP_WITH_SAVE_MENU_ITEM))
    step = out.steps[0]
    assert step.modality == "applescript"
    assert step.command == {"script": other_script}
    print("OK  a script that is not a plain menu-item click is left untouched, whatever the AX tree contains")


def test_non_applescript_steps_are_never_touched():
    ax_step = PlannedStep(intent="press equals", source="workflow", target_element="=",
                          modality="ax", command={"role": "AXButton", "title": "Equals", "action": "press"})
    plan = ExecutionPlan(steps=[ax_step])
    out = _steer_away_from_applescript(plan, json.dumps(_AX_DUMP_WITH_SAVE_MENU_ITEM))
    assert out.steps[0].command == {"role": "AXButton", "title": "Equals", "action": "press"}
    print("OK  a step that is already 'ax' is left alone")


def test_malformed_ax_json_does_not_raise_and_leaves_the_plan_unchanged():
    plan = ExecutionPlan(steps=[_applescript_step(_SAVE_SCRIPT)])
    out = _steer_away_from_applescript(plan, "{not valid json")
    assert out.steps[0].modality == "applescript"
    print("OK  malformed AX JSON is handled without raising, plan left unchanged")


def test_empty_ax_dump_leaves_the_plan_unchanged():
    plan = ExecutionPlan(steps=[_applescript_step(_SAVE_SCRIPT)])
    out = _steer_away_from_applescript(plan, json.dumps({}))
    assert out.steps[0].modality == "applescript"
    print("OK  an AX dump with no AXMenuItem at all leaves every applescript step unchanged")


TESTS = [
    test_a_plain_menu_click_with_a_live_ax_match_is_rewritten_to_ax,
    test_no_matching_ax_menu_item_leaves_the_step_unchanged,
    test_a_non_menu_click_script_is_never_touched,
    test_non_applescript_steps_are_never_touched,
    test_malformed_ax_json_does_not_raise_and_leaves_the_plan_unchanged,
    test_empty_ax_dump_leaves_the_plan_unchanged,
]


def main():
    print("=" * 70)
    print("planner steers away from applescript when ax already covers the step (D19)")
    print("=" * 70)
    for t in TESTS:
        t()
    print("\nPLANNER APPLESCRIPT STEERING TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
