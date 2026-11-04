"""The guest job agent's MCP tool path (``execution/guest/desktop_e2e_job.py``: ``GuardedCheckedExecutor``,
``guest_tree_factory``, ``guest_executor_factory``), offline with FAKES (the interlock test's
fake session states; fake processes and trees).

Pinned here:

* the wrapper names the executor it wraps (``__wrapped_executor__``): ``ax_actions.executor_supports`` reads the
  checked executor's table through it, so a version-2 step (select_row on a row selector, open_app, ...) is not
  refused on the guest path; a wrapper that did not name it would refuse it (fail-closed);
* a multi-app draft's second app gets its OWN tree, bound to that app's one running process (by bundle id; nothing
  launched; the interlock asked first) and its own checked executor behind the interlock;
* an app the record does not name, one the restriction policy blocks, one with no single running process: no tree,
  ``AppRouter`` refuses the step and says why; a visible guest refuses before any process is looked up.

No desktop, no AX, no subprocess, no network.
"""
import asyncio
import sys

import pytest

from anyuse.core.restriction import RestrictionPolicy
from anyuse.execution import ax_actions as AX
from anyuse.execution import draft_compile as DC
from anyuse.execution.executor import StateGraphExecutor

import test_desktop_job_interlock as TI
from test_desktop_job_interlock import world  # noqa: F401  (the fixture: the job module, fake session states)

TE, NOTES, MAIL = "com.apple.TextEdit", "com.apple.Notes", "com.apple.mail"
V2 = {"modality": "ax", "command": {"action": "select_row",
                                    "selector": {"kind": "row", "canonical_role": "row", "name": "Rechnung"}}}


class Tree:
    def __init__(self, pid, bid):
        self.pid, self._bid = pid, bid

    def bundle_id(self):
        return self._bid


def _draft():
    return {"app": "TextEdit", "bundle_id": TE,
            "apps": [{"name": "TextEdit", "bundle_id": TE}, {"name": "Notizen", "bundle_id": NOTES}],
            "steps": [{"app": "TextEdit", "bundle_id": TE, "modality": "ax", "command": {"action": "enter_text"}},
                      {"app": "Notizen", "bundle_id": NOTES, "modality": "ax", "command": {"action": "press"}}]}


@pytest.fixture
def procs(world, monkeypatch):  # noqa: F811
    J = world.J
    asked = []
    pids = {TE: 501, NOTES: 502, MAIL: 503}

    def app_pid(app):
        asked.append(dict(app))
        pid = pids.get(app.get("bundle_id") or "")
        return (pid, None) if pid else (None, f"0 running processes of {app!r}")
    monkeypatch.setattr(J, "app_pid", app_pid)
    monkeypatch.setattr(J, "app_tree", lambda pid: Tree(pid, {v: k for k, v in pids.items()}[pid]))
    monkeypatch.setattr(RestrictionPolicy, "from_settings", classmethod(lambda cls, env=None: cls.permissive()))
    return J, asked


def test_a_version_2_step_passes_through_the_wrapper(world):  # noqa: F811
    J = world.J
    real = StateGraphExecutor(ax_tree=None)
    wrapped = J.GuardedCheckedExecutor(real, pid=501)
    assert wrapped.__wrapped_executor__ is real
    assert AX.executor_supports(wrapped) == AX.executor_supports(real)
    assert {"select_row", "selector:row", "open_app"} <= set(AX.executor_supports(wrapped))
    assert AX.runner_refusal(V2, wrapped) is None
    assert AX.record_runner_problem({"steps": [V2]}, wrapped) is None

    class Unnamed:                                       # a wrapper that does not name what it wraps
        def __init__(self, ex):
            self.ex = ex
    assert AX.runner_refusal(V2, Unnamed(real)) is not None


def test_a_second_app_gets_its_own_tree_and_its_own_guarded_executor(procs):
    J, asked = procs
    draft = _draft()
    first = J.GuardedCheckedExecutor(DC.checked_executor(Tree(501, TE)), pid=501)
    router = DC.AppRouter(draft, first, tree_factory=J.guest_tree_factory(draft),
                          executor_factory=J.guest_executor_factory, default_tree=Tree(501, TE))
    ex1, why1 = router.for_step(draft["steps"][0])
    ex2, why2 = router.for_step(draft["steps"][1])
    assert why1 is None and ex1 is first
    assert why2 is None and isinstance(ex2, J.GuardedCheckedExecutor) and ex2.pid == 502 and ex2 is not first
    assert ex2.__wrapped_executor__._ax_tree.bundle_id() == NOTES
    assert asked == [{"bundle_id": NOTES}]                                        # looked up by bundle id, once
    assert router.for_step(draft["steps"][1])[0] is ex2


def test_an_unknown_blocked_or_absent_app_gets_no_tree_and_the_step_is_refused(procs, monkeypatch):
    J, asked = procs
    draft = _draft()
    why = []
    f = J.guest_tree_factory(draft, why)
    assert f(DC.BundleId(MAIL)) is None and why == [f"{MAIL}: not one of the record's apps"] and asked == []
    blocked = RestrictionPolicy.from_boundary({"list_mode": "blocklist", "blocked_apps": [NOTES], "allowed_apps": []})
    assert not blocked.is_app_target_allowed(NOTES)[0]
    monkeypatch.setattr(RestrictionPolicy, "from_settings", classmethod(lambda cls, env=None: blocked))
    f2 = J.guest_tree_factory(draft, why)
    assert f2(DC.BundleId(NOTES)) is None and why[-1].startswith(f"{NOTES}: ") and asked == []
    router = DC.AppRouter(draft, object(), tree_factory=f2, executor_factory=J.guest_executor_factory)
    ex, refusal = router.for_step(draft["steps"][1])
    assert ex is None and refusal["reason_code"] == "no_tree"
    absent = dict(draft, apps=draft["apps"] + [{"name": "Kalender", "bundle_id": "com.apple.iCal"}])
    monkeypatch.setattr(RestrictionPolicy, "from_settings", classmethod(lambda cls, env=None: cls.permissive()))
    f3 = J.guest_tree_factory(absent, why)
    assert f3(DC.BundleId("com.apple.iCal")) is None and "0 running processes" in why[-1]


def test_a_visible_guest_refuses_before_any_process_is_looked_up(procs, world):  # noqa: F811
    J, asked = procs
    world.state["now"] = TI.STATES["a screen-sharing viewer"]
    with pytest.raises(J.InterlockRefused):
        J.guest_tree_factory(_draft())(DC.BundleId(NOTES))
    assert asked == []


def test_the_guarded_executor_of_a_second_app_asks_the_interlock_per_step(procs, world):  # noqa: F811
    J, _asked = procs

    class Inner:
        async def execute_checked_step(self, step, **kw):
            return {"success": True}, {}
    ex = J.GuardedCheckedExecutor(Inner(), pid=502)
    world.state["now"] = TI.STATES["a screen-sharing viewer"]
    with pytest.raises(J.InterlockRefused):
        asyncio.run(ex.execute_checked_step(object(), step_idx=1))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
