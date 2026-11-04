"""The drift gate runs on recorded-graph replay.

``replay_transitions`` called ``execute_checked_step(step)`` with no context,
so the pre-step drift gate never ran on a recorded graph although each
transition's ``from_state`` is exactly the state the step must start from. Now
``replay_path`` hands the graph down, every attempt passes ``from_state`` as the
expected state, and a fired gate STOPS the replay (a ``refused`` outcome with
reason ``drift``; nothing dispatched, no vision model). The orchestrator does
not hand a stopped replay to the planner. Fakes only.

Run from the anyuse package dir:
    cd anyuse && python3 test_replay_drift_context.py
"""
import asyncio
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

from anyuse.execution.deterministic import replay_path  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.orchestrator import ExecutionOrchestrator  # noqa: E402
from anyuse.guides.state_graph import (  # noqa: E402
    StateGraph, TransitionImpl, UIFingerprint, UIState, compute_structural_hash)
from anyuse.context.accessibility import AXElementDesc, AXSnapshot  # noqa: E402

LOGIN = [("AXTextField", "Email"), ("AXButton", "Sign In")]
OTHER = [("AXButton", "Help"), ("AXStaticText", "Something else")]


def _state(sid, rows):
    els = [{"role": r, "title": t} for r, t in rows]
    return UIState(state_id=sid, app="App", times_seen=3, fingerprint=UIFingerprint(
        required_elements=[e for e in els if e["role"] != "AXStaticText"],
        element_count_range=(max(0, len(els) - 1), len(els) + 1),
        structural_hash=compute_structural_hash(els)))


def _graph():
    g = StateGraph()
    g.add_state(_state("login", LOGIN))
    g.add_state(_state("home", OTHER))
    g.add_implementation("login", "home", "sign in", TransitionImpl(
        modality="ax", command={"role": "AXButton", "title": "Sign In", "action": "press"}))
    return g


class _Tree:
    """A screen showing ``rows``; pressing records the title."""

    def __init__(self, rows):
        self.rows, self.pressed = rows, []

    def snapshot(self):
        return AXSnapshot(elements=[AXElementDesc(role=r, title=t) for r, t in self.rows])

    def diff(self, before, after):
        from anyuse.context.accessibility import AccessibilityTree
        return AccessibilityTree.diff(self, before, after)

    def find(self, role=None, title=None):
        return [{"role": r, "title": t} for r, t in self.rows
                if (role is None or r == role) and (title is None or t == title)]

    def perform_action(self, el, action):
        self.pressed.append(el["title"])
        self.rows = list(OTHER)     # signing in shows the home screen (the recorded to_state)
        return True


class _Vision:
    calls = 0

    async def resolve(self, step, ax_json):
        _Vision.calls += 1
        raise AssertionError("a recorded replay never hands a drifted step to vision")


def test_on_the_recorded_screen_the_replay_acts():
    tree = _Tree(LOGIN)
    ex = StateGraphExecutor(ax_tree=tree, drift_gate_enabled=True, visual_resolver=_Vision())
    out = asyncio.run(replay_path(_graph(), "login", "home", ex))
    assert out["success"] is True and tree.pressed == ["Sign In"], out
    assert out["steps"][0]["tried"][0]["drift_gate"] == {"fired": False, "note": None}


def test_a_drifted_screen_stops_the_replay_before_anything_is_dispatched():
    tree = _Tree(OTHER + [("AXButton", "Sign In")])     # same button, wrong screen
    ex = StateGraphExecutor(ax_tree=tree, drift_gate_enabled=True, visual_resolver=_Vision())
    out = asyncio.run(replay_path(_graph(), "login", "home", ex))
    assert out["success"] is False and out["outcome_kind"] == "refused", out
    assert out["reason_code"] == "drift" and "login" in out["reason"], out
    assert tree.pressed == [] and _Vision.calls == 0 and out["llm_calls"] == 0
    assert out["steps"][0]["tried"][0]["drift_gate"]["fired"] is True


def test_the_gate_stays_opt_in():
    tree = _Tree(OTHER + [("AXButton", "Sign In")])
    ex = StateGraphExecutor(ax_tree=tree, drift_gate_enabled=False)
    out = asyncio.run(replay_path(_graph(), "login", "home", ex))
    assert out["success"] is True and tree.pressed == ["Sign In"], out


def test_no_recorded_baseline_means_no_gate():
    g = _graph()
    g.add_state(UIState(state_id="pending", app="App", fingerprint=UIFingerprint()))
    g.add_implementation("pending", "home", "go", TransitionImpl(
        modality="ax", command={"role": "AXButton", "title": "Sign In", "action": "press"}))
    tree = _Tree(OTHER + [("AXButton", "Sign In")])
    ex = StateGraphExecutor(ax_tree=tree, drift_gate_enabled=True, visual_resolver=_Vision())
    out = asyncio.run(replay_path(g, "pending", "home", ex))
    assert out["success"] is True and tree.pressed == ["Sign In"], out


def test_execute_checked_step_takes_the_expected_state_directly():
    tree = _Tree(OTHER)
    ex = StateGraphExecutor(ax_tree=tree, drift_gate_enabled=True, visual_resolver=_Vision())
    t = _graph().transitions[0]
    from anyuse.execution.deterministic import impl_to_step
    step = impl_to_step(t, t.implementations[0])
    res, ver = asyncio.run(ex.execute_checked_step(step, state_graph=_graph(),
                                                   expected_state_id="login", on_drift="stop"))
    assert res["reason_code"] == "drift" and ver["drift_gate"]["fired"] is True


class _StoppingExecutor:
    """execute_checked_step with the full keyword surface; always refuses."""

    async def execute_checked_step(self, step, step_idx=None, workflow=None, state_graph=None, *,
                                   expected_state_id=None, on_drift="escalate"):
        assert expected_state_id == "login" and on_drift == "stop" and state_graph is not None
        return ({"success": False, "outcome_kind": "refused", "reason_code": "drift",
                 "error": "the screen is not the recorded state login"}, {})


class _Planner:
    calls = 0

    async def plan(self, **kw):
        _Planner.calls += 1
        raise AssertionError("a stopped replay is not handed to the planner")


def test_the_orchestrator_does_not_hand_a_stopped_replay_to_the_planner():
    from anyuse.memory.models import WorkflowTemplate
    orch = ExecutionOrchestrator.__new__(ExecutionOrchestrator)
    orch._verbose, orch._planner = False, _Planner()

    async def _replay(workflow, state_graph=None, ax_tree=None, executor=None):
        return await replay_path(_graph(), "login", "home", _StoppingExecutor())

    orch.replay_deterministic = _replay
    wf = WorkflowTemplate.create_new("t", "t")
    wf.state_path = ["login", "home"]
    os.environ["ANYUSE_DETERMINISTIC_REPLAY"] = "1"
    res = asyncio.run(orch._execute_workflow("t", wf, {}, _graph(), None))
    assert res.success is False and "stopped (refused)" in res.message, res.message
    assert _Planner.calls == 0 and res.mode == "replay-deterministic"


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
