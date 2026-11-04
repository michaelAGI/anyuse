"""The intervention's AX fix acts only on a uniquely
identified element in the expected app.

``InterventionHandler._run_ax_fix`` used to take the frontmost app's pid and
press ``find(role, title)[0]``. It now runs the fix as a step through the bound
executor's ``execute_checked_step`` on the run's app-bound tree. Fakes only:
the model adapters are never called, and ``context.macos`` is poisoned so a
fix that still asked for the frontmost app would fail loudly.

Run from the anyuse package dir:
    cd anyuse && python3 test_intervention_identity.py
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

import anyuse.context.macos as _macos  # noqa: E402


class _Poisoned:
    def __init__(self, *a, **k):
        raise AssertionError("an AX fix must never ask for the frontmost app")


_macos.MacOSContext = _Poisoned

from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.intervention import InterventionHandler  # noqa: E402
from anyuse.execution.plan_types import ExecutionPlan, PlannedStep  # noqa: E402
from anyuse.guides.state_graph import StateGraph  # noqa: E402
from anyuse.memory.models import WorkflowIntent, WorkflowTemplate  # noqa: E402


class _Tree:
    def __init__(self, rows, bundle="com.apple.TextEdit"):
        self.rows, self.bundle, self.pressed = rows, bundle, []

    def bundle_id(self):
        return self.bundle

    def find(self, role=None, title=None):
        return [r for r in self.rows if (role is None or r["role"] == role)
                and (title is None or r["title"] == title)]

    def perform_action(self, el, action):
        self.pressed.append((el["title"], el.get("n"), action)); return True


def _handler():
    return InterventionHandler(adapter=object(), visual_adapter=object())


def _fix(handler, command, original=None):
    return asyncio.run(handler._run_ax_fix(command, original))


SAVE = {"role": "AXButton", "title": "Sichern"}


def test_no_bound_executor_means_no_fix():
    assert _fix(_handler(), dict(SAVE, action="press")) is False


def test_a_unique_element_in_the_bound_tree_is_pressed():
    tree = _Tree([dict(SAVE, n=1), {"role": "AXButton", "title": "Abbrechen", "n": 2}])
    h = _handler()
    h.bind_executor(StateGraphExecutor(ax_tree=tree))
    assert _fix(h, dict(SAVE, action="AXPress")) is True
    assert tree.pressed == [("Sichern", 1, "AXPress")]


def test_an_ambiguous_fix_is_refused():
    tree = _Tree([dict(SAVE, n=1), dict(SAVE, n=2)])
    h = _handler()
    h.bind_executor(StateGraphExecutor(ax_tree=tree))
    assert _fix(h, dict(SAVE, action="press")) is False
    assert tree.pressed == [], "never the first of two"


def test_the_fix_stays_in_the_app_the_failed_step_was_recorded_in():
    tree = _Tree([dict(SAVE, n=1)], bundle="com.apple.Notes")
    h = _handler()
    h.bind_executor(StateGraphExecutor(ax_tree=tree))
    original = PlannedStep(intent="save", source="workflow", target_element="", modality="ax",
                           command={"role": "AXButton", "action": "press",
                                    "selector": {"canonical_role": "button", "name": "Sichern",
                                                 "app": "com.apple.TextEdit"}})
    assert _fix(h, dict(SAVE, action="press"), original) is False
    assert tree.pressed == []


def test_an_unsupported_fix_verb_is_refused():
    tree = _Tree([dict(SAVE, n=1)])
    h = _handler()
    h.bind_executor(StateGraphExecutor(ax_tree=tree))
    assert _fix(h, dict(SAVE, action="AXShowDefaultUI")) is False and tree.pressed == []


class _Intervening(InterventionHandler):
    """The model is replaced by a fixed answer: press 'Sichern'."""

    def __init__(self):
        super().__init__(adapter=object(), visual_adapter=object())

    async def intervene(self, failed_step, **kw):
        return await self._execute_fix("ax_command", {"role": "AXButton", "title": "Sichern",
                                                      "action": "press"}, failed_step, "fixed")


def test_the_executor_binds_itself_before_asking_for_a_fix():
    # step 1 fails retryably (its AX action returns False), the fix acts through
    # the same executor's checked path on the same tree
    class _FailOnce(_Tree):
        def perform_action(self, el, action):
            if el["title"] == "Weiter":
                return False
            return super().perform_action(el, action)

    tree = _FailOnce([dict(SAVE, n=1), {"role": "AXButton", "title": "Weiter", "n": 2}])
    handler = _Intervening()
    ex = StateGraphExecutor(ax_tree=tree, intervention_handler=handler)
    step = PlannedStep(intent="next", source="workflow", target_element="", modality="ax",
                       command={"role": "AXButton", "title": "Weiter", "action": "press"})
    wf = WorkflowTemplate.create_new("t", "t")
    wf.steps = [WorkflowIntent(intent="next", target_element="")]
    res = asyncio.run(ex.execute(ExecutionPlan(current_state="s0", steps=[step]), wf, StateGraph()))
    assert handler._checked_executor is ex
    assert res.success is True and tree.pressed == [("Sichern", 1, "AXPress")], (res.to_dict(), tree.pressed)


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
