"""An identity refusal is TERMINAL for the step.

Before this, ``_execute_ax`` returned ``{"refused": True, "outcome": "refused"}``
with no ``outcome_kind``; ``classify_outcome`` read neither key, called it a
``retryable_failure``, and ``replay_transitions`` fell through to the next rung:
the recorded coordinate click, which had no identity check at all. These tests
pin the fix with fakes (no AX, no mouse): a refusal never reaches a coordinate
click, a coordinate rung hit-tests the point against the recorded identity, and
the planned path stops on a refusal without asking the intervention model.

Run from the anyuse package dir:
    cd anyuse && python3 test_refusal_terminal.py
"""
import asyncio
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, "wrong checkout (D9)"

from anyuse.execution import outcome as O  # noqa: E402
from anyuse.execution.deterministic import impl_to_step, replay_transitions  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.plan_types import ExecutionPlan, PlannedStep  # noqa: E402
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UITransition  # noqa: E402
from anyuse.memory.models import WorkflowIntent, WorkflowTemplate  # noqa: E402
from anyuse.context.accessibility import AXElementDesc  # noqa: E402

DIVIDE = {"role": "AXButton", "desc": "Dividieren", "id": "Divide", "xy": (10, 10)}
MULTIPLY = {"role": "AXButton", "desc": "Multiplizieren", "id": "Multiply", "xy": (50, 10)}


def _sel(row):
    return AXElementDesc(role=row["role"], ax_description=row["desc"],
                         identifier=row["id"]).to_selector().to_dict()


class _Tree:
    """A fixed AX world. ``resolve`` = what ``find_selector`` returns."""

    def __init__(self, rows, resolve="correct"):
        self.rows, self.resolve = rows, resolve
        self.pressed = []

    def to_selector(self, row):
        return AXElementDesc(role=row["role"], ax_description=row["desc"],
                             identifier=row["id"]).to_selector()

    def find_selector(self, selector):
        if self.resolve == "wrong":
            return next(r for r in self.rows if r["id"] != selector.identifier)
        if self.resolve == "none":
            return None
        return next((r for r in self.rows if r["id"] == selector.identifier), None)

    def find(self, role=None, title=None):
        return [r for r in self.rows if (role is None or r["role"] == role)
                and (title is None or r["desc"] == title)]

    def find_at(self, x, y):
        return next((r for r in self.rows if r["xy"] == (x, y)), None)

    def perform_action(self, el, action):
        self.pressed.append((el["id"], action))
        return True


class _Clicker:
    """Stands in for the ActionExecutor: records every coordinate actuation."""

    def __init__(self):
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        return {"success": True}


def _transition(ax_proven=False, coord_proven=False):
    ax = TransitionImpl(modality="ax", command={"role": "AXButton", "action": "press",
                                                "selector": _sel(DIVIDE)},
                        times_used=2 if ax_proven else 0, times_succeeded=2 if ax_proven else 0)
    coord = TransitionImpl(modality="coordinate",
                           command={"action": "click_at", "absolute_x": 10, "absolute_y": 10},
                           times_used=3 if coord_proven else 0,
                           times_succeeded=3 if coord_proven else 0)
    return UITransition(from_state="s0", to_state="s1", intent="divide", implementations=[ax, coord])


def test_classify_legacy_refusal_is_refused_and_stops():
    assert O.classify_outcome({"success": False, "outcome": "refused"}) == O.REFUSED
    assert O.classify_outcome({"success": False, "refused": True}) == O.REFUSED
    assert O.REFUSED in O.STOP_KINDS and O.DEVIATION in O.STOP_KINDS
    assert O.classify_outcome({"success": False}) == O.RETRYABLE_FAILURE


def test_ax_refusal_never_reaches_the_coordinate_click():
    tree, clicker = _Tree([DIVIDE, MULTIPLY], resolve="wrong"), _Clicker()
    ex = StateGraphExecutor(ax_tree=tree, action_executor=clicker)
    out = asyncio.run(replay_transitions([_transition()], ex))
    assert out["success"] is False and out["outcome_kind"] == O.REFUSED, out
    tried = out["steps"][0]["tried"]
    assert [t["modality"] for t in tried] == ["ax"], tried
    assert clicker.calls == [] and tree.pressed == [], (clicker.calls, tree.pressed)


def test_unresolvable_recorded_identity_stops_the_ladder_too():
    tree, clicker = _Tree([MULTIPLY], resolve="none"), _Clicker()
    ex = StateGraphExecutor(ax_tree=tree, action_executor=clicker)
    out = asyncio.run(replay_transitions([_transition()], ex))
    assert out["outcome_kind"] == O.REFUSED and clicker.calls == [], (out, clicker.calls)


def test_coordinate_rung_gets_the_transition_identity():
    t = _transition()
    step = impl_to_step(t, t.implementations[1])
    assert step.command["selector"] == _sel(DIVIDE), step.command
    assert "selector" not in t.implementations[1].command, "the stored impl is never mutated"


def test_proven_coordinate_rung_first_hit_tests_and_refuses_another_element():
    # The coordinate impl is proven, so the ladder tries it FIRST; the button
    # under the recorded point is now "Multiply" (the layout moved).
    moved = dict(MULTIPLY, xy=(10, 10))
    tree, clicker = _Tree([moved, dict(DIVIDE, xy=(90, 90))]), _Clicker()
    ex = StateGraphExecutor(ax_tree=tree, action_executor=clicker)
    out = asyncio.run(replay_transitions([_transition(coord_proven=True)], ex))
    assert out["outcome_kind"] == O.REFUSED, out
    assert [t["modality"] for t in out["steps"][0]["tried"]] == ["coordinate"], out
    assert clicker.calls == [] and tree.pressed == []


def test_coordinate_rung_clicks_when_the_point_still_holds_the_recorded_element():
    tree, clicker = _Tree([DIVIDE, MULTIPLY]), _Clicker()
    ex = StateGraphExecutor(ax_tree=tree, action_executor=clicker)
    out = asyncio.run(replay_transitions([_transition(coord_proven=True)], ex))
    assert out["success"] is True, out
    assert clicker.calls == [("click_at", {"x": 10, "y": 10, "button": "left"})], clicker.calls


def test_coordinate_rung_without_a_tree_refuses_a_recorded_identity():
    clicker = _Clicker()
    ex = StateGraphExecutor(action_executor=clicker)
    step = PlannedStep(intent="divide", source="workflow", target_element="", modality="coordinate",
                       command={"action": "click", "x": 10, "y": 10, "selector": _sel(DIVIDE)})
    res = ex._execute_coordinate(step)
    assert res["outcome_kind"] == O.REFUSED and res["reason_code"] == "no_tree", res
    assert clicker.calls == []


class _Intervention:
    def __init__(self):
        self.calls = 0

    async def intervene(self, **kw):
        self.calls += 1
        raise AssertionError("a refusal must never be handed to the intervention model")


def test_planned_path_stops_on_a_refusal_without_intervention():
    tree, handler = _Tree([DIVIDE, MULTIPLY], resolve="wrong"), _Intervention()
    ex = StateGraphExecutor(ax_tree=tree, intervention_handler=handler)
    steps = [PlannedStep(intent=f"press {i}", source="workflow", target_element="", modality="ax",
                         command={"role": "AXButton", "action": "press", "selector": _sel(DIVIDE)})
             for i in range(2)]
    wf = WorkflowTemplate.create_new("t", "t")
    wf.steps = [WorkflowIntent(intent=s.intent, target_element="", optional=True) for s in steps]
    res = asyncio.run(ex.execute(ExecutionPlan(current_state="s0", steps=steps), wf, StateGraph()))
    assert res.success is False and handler.calls == 0
    assert len(res.steps) == 1, "an optional step's refusal still stops the run"
    assert res.stopped["outcome_kind"] == O.REFUSED, res.stopped
    assert res.steps[0].to_dict()["outcome_kind"] == O.REFUSED
    assert tree.pressed == []


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
