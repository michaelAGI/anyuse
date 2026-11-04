"""The guest job agent's menu presses go through the CHECKED executor, behind the foreground interlock
(``execution/guest/desktop_e2e_job.py``: ``GuardedAS`` -> ``checked_menu_action``), offline with FAKES
(the interlock test's fake AX world, fake checked executor and fake session states).

Pinned here:

* opening a menu bar item (AXPress), pressing a menu item and closing a menu (AXCancel) never reach the raw AX
  action: each is ONE checked executor step (``EX.run``: StateGraphExecutor.execute_checked_step through the
  ExecutorActuator in the guest), with the element's recorded selector, verb ``press`` / ``cancel``;
* the checked step asks the interlock first: a visible guest refuses the menu press with the typed refusal and
  nothing is sent;
* a refused or failed checked step answers the caller like a failed AX call (``AX_FAILURE``), it is recorded;
* without a checked executor nothing is pressed; any other AX action (a window, a button) keeps its raw call behind
  the interlock; the helper modules' menu reads (``desktop_gen2.read_group_marks2`` through ``install_guard``) are
  routed the same way;
* the source: every AX action of the job goes through the guarded accessor ``_AS()`` (no raw accessor call).

No desktop, no AX, no subprocess, no network.
"""
import os
import re
import sys

import pytest

import test_desktop_view_probe as T13
import test_desktop_job_interlock as TI
from test_desktop_job_interlock import world  # noqa: F401  (the fixture: fake AX world, fake EX, session states)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bar(w):
    return w.st["app"].a["AXMenuBar"].a["AXChildren"][0]


def _raw(w):
    return [c for c in w.ax.calls if c[0] == "action"]


def test_a_menu_bar_item_is_opened_and_closed_through_the_checked_executor(world):  # noqa: F811
    J, bar = world.J, _bar(world)
    item = bar.a["AXChildren"][0].a["AXChildren"][0]
    assert J._AS().AXUIElementPerformAction(bar, "AXPress") == 0
    assert J._AS().AXUIElementPerformAction(item, "AXPress") == 0
    assert J._AS().AXUIElementPerformAction(bar, "AXCancel") == 0
    assert [(e, a) for e, a, _k in world.ex.runs] == [(bar, "press"), (item, "press"), (bar, "cancel")]
    assert _raw(world) == []                                    # nothing raw
    assert [x["action"] for x in J.STATE["menu_checked"][-3:]] == ["press", "press", "cancel"]


def test_the_checked_menu_press_asks_the_interlock_first(world):  # noqa: F811
    J = world.J
    world.state["now"] = TI.STATES["a screen-sharing viewer"]
    with pytest.raises(J.InterlockRefused) as exc:
        J._AS().AXUIElementPerformAction(_bar(world), "AXPress")
    assert exc.value.verdict.where == "desktop_e2e_job: executor press"
    assert world.ex.runs == [] and _raw(world) == []


def test_a_refused_checked_press_answers_like_a_failed_ax_call(world, monkeypatch):  # noqa: F811
    J = world.J

    class Refusing(T13.FakeEX):
        def run(self, el, pid, selector, action, **kw):
            self.runs.append((el, action, kw))
            return {"success": False, "refused": True, "outcome": "refused", "error": "identity"}
    ex = Refusing()
    monkeypatch.setattr(J, "EX", J.GuardedExec(ex))
    assert J._AS().AXUIElementPerformAction(_bar(world), "AXPress") == J.AX_FAILURE
    assert J.STATE["menu_checked"][-1] == {"action": "press", "success": False, "refused": True}
    assert _raw(world) == []


def test_without_a_checked_executor_nothing_is_pressed_and_other_actions_stay_raw(world, monkeypatch):  # noqa: F811
    J = world.J
    J._AS().AXUIElementPerformAction(world.st["w"], "AXRaise")          # a window: the raw call behind the interlock
    assert _raw(world) == [("action", "AXRaise")] and world.ex.runs == []
    monkeypatch.setattr(J, "EX", None)
    with pytest.raises(RuntimeError):
        J._AS().AXUIElementPerformAction(_bar(world), "AXPress")
    assert _raw(world) == [("action", "AXRaise")]


def test_the_helper_modules_menu_presses_are_routed_the_same_way(world):  # noqa: F811
    J = world.J
    J.install_guard()
    J.G2._AS().AXUIElementPerformAction(_bar(world), "AXPress")
    J.G._AS().AXUIElementPerformAction(_bar(world), "AXCancel")
    assert [a for _e, a, _k in world.ex.runs] == ["press", "cancel"] and _raw(world) == []


def test_every_ax_action_of_the_job_goes_through_the_guarded_accessor():
    src = open(os.path.join(HERE, "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    calls = re.findall(r"(\w+(?:\(\))?)\.AXUIElementPerformAction\(", src)
    assert calls and set(calls) == {"_AS()"}, set(calls)
    assert "_AS_BASE().AXUIElementPerformAction" not in src and "G._AS_BASE" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
