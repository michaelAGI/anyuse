"""Planned labels are checked against the
labels on screen before a step acts; a mismatch stops with a reason.

A planner can name menu items in English on a German UI. A planned
``ax`` step with no recorded identity names its target only by a label; when
no element on screen carries it, the executor refuses with
``reason_code="label_mismatch"`` and the nearest observed labels, and the
planned run stops there (no intervention model). Also pinned: the planner's
AppleScript->AX steer reads the compact ``AXSnapshot.to_json()`` form the
orchestrator actually passes. Fakes only.

Run from the anyuse package dir:
    cd anyuse && python3 test_planned_labels.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"   # fakes only

from anyuse.execution import label_check as L  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.plan_types import ExecutionPlan, PlannedStep  # noqa: E402
from anyuse.execution.planner import _steer_away_from_applescript  # noqa: E402
from anyuse.guides.state_graph import StateGraph  # noqa: E402
from anyuse.memory.models import WorkflowIntent, WorkflowTemplate  # noqa: E402
from anyuse.context.accessibility import AXElementDesc, AXSnapshot  # noqa: E402

GERMAN_MENU = [("AXMenuBarItem", "Ablage"), ("AXMenuItem", "Neu"), ("AXMenuItem", "Öffnen …"),
               ("AXMenuItem", "Sichern"), ("AXMenuItem", "Sichern unter …"), ("AXButton", "Drucken")]


def _snap(rows=GERMAN_MENU):
    return AXSnapshot(elements=[AXElementDesc(role=r, title=t) for r, t in rows], app_name="TextEdit")


class _Tree:
    def __init__(self, rows=GERMAN_MENU):
        self.rows, self.pressed = rows, []

    def snapshot(self):
        return _snap(self.rows)

    def diff(self, before, after):
        from anyuse.context.accessibility import AccessibilityTree
        return AccessibilityTree.diff(self, before, after)

    def find(self, role=None, title=None):
        return [{"role": r, "title": t} for r, t in self.rows
                if (role is None or r == role) and (title is None or t == title)]

    def perform_action(self, el, action):
        self.pressed.append(el["title"]); return True


def _step(title, role="AXMenuItem"):
    return PlannedStep(intent=f"click {title}", source="ai_injected", target_element=title,
                       modality="ax", command={"role": role, "title": title, "action": "press"})


def test_observed_labels_read_every_shape():
    compact = _snap().to_json()
    raw = {"role": "AXApplication", "children": [{"role": "AXMenuItem", "title": "Sichern"},
                                                 {"role": "AXTextArea", "identifier": "First Text View"}]}
    assert "Sichern" in L.observed_labels(_snap())
    assert "Sichern" in L.observed_labels(compact) and "Drucken" in L.observed_labels(compact)
    assert {"Sichern", "First Text View"} <= L.observed_labels(raw)
    assert L.observed_labels(None) == set() and L.observed_labels("not json") == set()


def test_an_english_label_on_a_german_screen_is_a_mismatch_with_the_nearest_labels():
    prob = L.planned_label_problem("AXMenuItem", "Save As…", _snap())
    assert prob["code"] == "label_mismatch" and "not on screen" in prob["reason"], prob
    assert prob["near"] and all(n in L.observed_labels(_snap()) for n in prob["near"])
    assert L.planned_label_problem("AXMenuItem", "Sichern", _snap()) is None
    role = L.planned_label_problem("AXButton", "Sichern", _snap())
    assert role["code"] == "role_mismatch" and role["roles"] == ["AXMenuItem"], role
    assert L.planned_label_problem("AXMenuItem", "Save", AXSnapshot()) is None, "no evidence: no claim"


def test_the_executor_refuses_a_label_that_is_not_on_screen():
    tree = _Tree()
    res = StateGraphExecutor(ax_tree=tree)._execute_ax(_step("Save"))
    assert res["outcome_kind"] == "refused" and res["reason_code"] == "label_mismatch", res
    assert "'Save'" in res["error"] and "Sichern" in res["error"], res["error"]
    assert tree.pressed == []
    ok = StateGraphExecutor(ax_tree=tree)._execute_ax(_step("Sichern"))
    assert ok["success"] is True and tree.pressed == ["Sichern"]


class _NoIntervention:
    async def intervene(self, **kw):
        raise AssertionError("a label mismatch stops; it is not handed to a model")


def test_a_planned_run_stops_at_the_mismatch_with_its_reason():
    tree = _Tree()
    ex = StateGraphExecutor(ax_tree=tree, intervention_handler=_NoIntervention())
    plan = ExecutionPlan(current_state="s0", steps=[_step("Ablage", "AXMenuBarItem"), _step("Save"),
                                                     _step("Sichern")])
    wf = WorkflowTemplate.create_new("t", "t")
    wf.steps = [WorkflowIntent(intent=s.intent, target_element="", optional=True) for s in plan.steps]
    res = asyncio.run(ex.execute(plan, wf, StateGraph()))
    assert res.success is False and tree.pressed == ["Ablage"], tree.pressed
    assert res.stopped["reason_code"] == "label_mismatch" and res.stopped["step"] == 1, res.stopped


def test_plan_label_problems_is_a_preflight_over_one_screen():
    probs = L.plan_label_problems([_step("Ablage", "AXMenuBarItem"), _step("Save"),
                                   PlannedStep(intent="x", source="workflow", target_element="",
                                               modality="ax", command={"role": "AXButton", "title": "Gone",
                                                                       "selector": {"name": "Gone"}})],
                                  _snap())
    assert [p["step"] for p in probs] == [1], probs   # a recorded identity is checked elsewhere


def test_the_applescript_steer_reads_the_compact_json_the_orchestrator_passes():
    plan = ExecutionPlan(current_state="s0", steps=[PlannedStep(
        intent="save", source="ai_injected", target_element="", modality="applescript",
        command={"script": 'tell application "System Events" to click menu item "Sichern" of menu '
                           '"Ablage" of menu bar 1 of process "TextEdit"'})])
    out = _steer_away_from_applescript(plan, _snap().to_json())
    assert out.steps[0].modality == "ax" and out.steps[0].command["title"] == "Sichern", out.steps[0]
    json.loads(_snap().to_json())


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
