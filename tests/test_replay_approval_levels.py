"""Approval levels on every replay path.

A replay started with its values asks NOTHING; holds are opt-in per workflow
(levels auto / all / destructive / off, default auto); a deviation stops with a
reason, it does not ask. The draft pipeline already did this
(``draft_compile.approval_level`` / ``effective_hil_mode`` / ``_gate_action``).
Outside it, the orchestrator's replays ran under the global ``ANYUSE_HIL_MODE``
and the executor showed the classifier the raw verb ``press``, which it does
not know, so under ``destructive`` EVERY AX step was held whatever its label.
Now the orchestrator builds the gate from the workflow's own level with the
draft pipeline's resolution, and the executor uses the draft pipeline's
projection and keywords. The learning phase (fill / proof) still holds every
step. Fakes only.

Run from the anyuse package dir:
    cd anyuse && python3 test_replay_approval_levels.py
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

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"   # fakes only: nothing below reaches an OS layer
os.environ.pop("ANYUSE_HIL_MODE", None)

from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution.hil_gate import HILGate  # noqa: E402
from anyuse.execution.orchestrator import ExecutionOrchestrator  # noqa: E402
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UIState  # noqa: E402
from anyuse.memory.models import WorkflowTemplate  # noqa: E402
from anyuse.context.accessibility import AXElementDesc  # noqa: E402


class _Tree:
    def __init__(self, rows):
        self.rows, self.pressed = rows, []

    def find_selector(self, sel):
        return next((r for r in self.rows if r.title == sel.name), None)

    def to_selector(self, el):
        return el.to_selector()

    def find(self, role=None, title=None):
        return [r for r in self.rows if title is None or r.title == title]

    def perform_action(self, el, action):
        self.pressed.append(el.title); return True


class _Approver:
    def __init__(self, answer=True, forbidden=False):
        self.answer, self.forbidden, self.asked = answer, forbidden, []

    async def __call__(self, ctx):
        if self.forbidden:
            raise AssertionError(f"a replay started with its values must not ask: {ctx}")
        self.asked.append(ctx["args"].get("target") or ctx["args"].get("intent"))
        return self.answer


def _workflow(level=None):
    wf = WorkflowTemplate.create_new("aufräumen", "")
    wf.state_path = ["s0", "s1", "s2"]
    if level:
        wf.approval = {"level": level}
    return wf


def _graph(labels=("Weiter", "Löschen")):
    g = StateGraph()
    for i in range(len(labels) + 1):
        g.add_state(UIState(state_id=f"s{i}", app="App"))
    for i, label in enumerate(labels):
        sel = AXElementDesc(role="AXButton", title=label).to_selector().to_dict()
        g.add_implementation(f"s{i}", f"s{i + 1}", f"press {label}", TransitionImpl(
            modality="ax", command={"role": "AXButton", "title": label, "action": "press",
                                    "selector": sel}))
    return g


def _replay(level, approver, rows=("Weiter", "Löschen")):
    orch = ExecutionOrchestrator(verbose=False, drift_gate_enabled=False,
                                 hil_gate=HILGate(mode="off", approver=approver))
    tree = _Tree([AXElementDesc(role="AXButton", title=t) for t in rows])
    out = asyncio.run(orch.replay_deterministic(_workflow(level), state_graph=_graph(), ax_tree=tree))
    return out, tree


def test_levels_resolve_like_the_draft_pipeline():
    assert DC.workflow_hil_mode(_workflow()) == "off"                 # auto
    assert DC.workflow_hil_mode(_workflow("auto")) == "off"
    assert DC.workflow_hil_mode(_workflow("all")) == "all"
    assert DC.workflow_hil_mode(_workflow("destructive")) == "destructive"
    assert DC.workflow_hil_mode(_workflow("off")) == "off"
    assert DC.workflow_hil_mode(_workflow("sometimes")) == "all", "an unknown level fails closed"
    assert WorkflowTemplate.from_dict(_workflow("all").to_dict()).approval == {"level": "all"}
    assert "approval" not in _workflow().to_dict(), "no level, no key: old templates round-trip unchanged"


def test_an_initiated_replay_with_values_holds_nothing():
    os.environ["ANYUSE_HIL_MODE"] = "all"          # the global setting does not raise a workflow's holds
    try:
        out, tree = _replay(None, _Approver(forbidden=True))
    finally:
        os.environ.pop("ANYUSE_HIL_MODE", None)
    assert out["success"] is True and tree.pressed == ["Weiter", "Löschen"], out


def test_level_all_holds_every_step():
    approver = _Approver(answer=True)
    out, tree = _replay("all", approver)
    assert out["success"] is True and len(approver.asked) == 2, approver.asked
    denied = _Approver(answer=False)
    out, tree = _replay("all", denied)
    assert out["success"] is False and out["outcome_kind"] == "terminal_refusal", out
    assert tree.pressed == [] and len(denied.asked) == 1


def test_level_destructive_holds_by_label_not_every_ax_step():
    approver = _Approver(answer=False)
    out, tree = _replay("destructive", approver)
    assert tree.pressed == ["Weiter"], "an ordinary label is not held"
    assert len(approver.asked) == 1 and "Löschen" in approver.asked[0], approver.asked
    assert out["success"] is False and out["stopped_at"] == "press Löschen"


def test_a_deviation_stops_with_a_reason_and_asks_nobody():
    out, tree = _replay(None, _Approver(forbidden=True), rows=("Weiter", "Entfernen"))
    assert out["success"] is False and out["outcome_kind"] == "refused", out
    assert "Löschen" in out["reason"] and tree.pressed == ["Weiter"], out


def test_a_recorded_workflow_takes_a_level_through_the_same_setter():
    from anyuse.memory.models import Workflow
    from anyuse.memory.storage import WorkflowStorage
    with tempfile.TemporaryDirectory() as d:
        st = WorkflowStorage(base_dir=pathlib.Path(d))
        wf = Workflow.create_new("aufräumen", "") if hasattr(Workflow, "create_new") else Workflow(
            name="aufräumen", description="")
        st.save(wf)
        tpl = _workflow()
        st.save_template(wf.id, tpl)
        out = DC.set_approval_level("aufräumen", "destructive", storage=st)
        assert out == {"workflow": "aufräumen", "level": "destructive", "mode": "destructive",
                       "supervised_runs": 0}, out
        again = WorkflowStorage(base_dir=pathlib.Path(d)).get_template(wf.id)
        assert again.approval["level"] == "destructive"


def test_the_learning_phase_still_holds_every_step():
    assert DC.HIL_MODE == "all"
    import inspect
    assert inspect.signature(DC.run_steps).parameters["hil_mode"].default == "all"


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
