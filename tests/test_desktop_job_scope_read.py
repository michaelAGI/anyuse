"""The guest op ``scope_read`` (execution/guest/desktop_e2e_job.py), offline with the
job interlock test's fakes (the GF10 fake AX world, fake session states fed to the real interlock rules) plus a fake
Apple-Events source (a fake permission check and a fake osascript runner).

Pinned here: per window the container identity from Apple Events FIRST (strength ``path``, source ``applescript``),
the AX-only reading and the raw AX sources next to it; two same-named Finder folders read two paths (matched by
frame); without the grant the AX reading with ``automation_not_granted`` and no script sent; a visible guest
answers the typed interlock refusal and reads nothing; an app that is not running is a typed ``ok: false``; the
op sends no input. No desktop, no AX, no osascript, no network.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_desktop_job_scope_read.py
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_desktop_job_interlock as TJ  # noqa: E402
import test_desktop_view_probe as T13  # noqa: E402
from anyuse.execution import applescript_route as AR  # noqa: E402

world = TJ.world          # the fixture, registered in this module


class Tree:
    def attr(self, el, name):
        return el.a.get(name) if el is not None else None

    def children(self, el):
        return list(el.a.get("AXChildren") or []) if el is not None else []


class Runner:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, script):
        self.calls.append(argv)
        bid = next(b for b, sc in AR.SCOPE_SCRIPTS.items() if sc.script == script)
        return 0, self.answers[bid], ""


def _ready(w, monkeypatch, answers, code=0, windows=None):
    J = w.J
    runner = Runner(answers)
    monkeypatch.setattr(J, "app_tree", lambda pid: Tree())
    monkeypatch.setattr(J, "interlock_status", lambda: {"allowed": True})
    monkeypatch.setattr(J, "_frame", lambda el: el.a.get("frame"))
    monkeypatch.setattr(J, "guest_scope_reader",
                        lambda: AR.ScriptScopeReader(lambda b: code, runner,
                                                     frame_of=lambda q: J._frame(q["window"])))
    if windows is not None:
        monkeypatch.setattr(J, "front_window", lambda pid: windows[0])
        monkeypatch.setattr(J, "_app_windows", lambda pid: list(windows))
    return J, runner


def _finder_windows():
    def win(title, frame, files):
        lst = T13.El("AXOutline", [T13.El("AXRow", [T13.El("AXCell", [T13.El("AXStaticText", AXValue=f)])])
                                   for f in files], AXDescription="Listendarstellung")
        return T13.El("AXWindow", [lst], AXTitle=title, frame=frame)
    return [win("Kistenpreise", (10.0, 40.0, 800.0, 500.0), ["Preise.txt"]),
            win("Kistenpreise", (60.0, 90.0, 800.0, 500.0), ["Preise.txt"])]


FINDER_ROWS = ('{{"Kistenpreise", {10, 40, 810, 540}, "/Users/m/C/Kistenpreise/"}, '
               '{"Kistenpreise", {60, 90, 860, 590}, "/Users/m/D/Kistenpreise/"}}')


def test_finder_all_windows_read_two_paths_apple_events_first(world, monkeypatch):
    J, runner = _ready(world, monkeypatch, {AR.FINDER: FINDER_ROWS}, windows=_finder_windows())
    world.state["now"] = TJ.CLEAR
    r, stop = J.handle("scope_read", {"bundle": "com.apple.finder", "window": "all"})
    assert r["status"] == "done" and r["ok"] and not stop and r["window"] == "all"
    a, b = r["windows"]
    assert (a["scope"]["strength"], a["scope"]["source"], a["scope"]["key"]) == (
        "path", "applescript", "/Users/m/C/Kistenpreise")
    assert b["scope"]["key"] == "/Users/m/D/Kistenpreise" and b["script"]["status"] == "ok"
    assert a["ax_scope"] == {"kind": "finder_folder", "strength": "title", "key": "", "title": "Kistenpreise",
                             "source": "title"}
    assert a["raw"]["AXTitle"] == "Kistenpreise" and a["raw"]["url_elements"] == []
    assert len(r["finder_windows"]) == 2 and r["finder_windows_why"] is None
    assert len(runner.calls) == 3                                           # one per window, one listing
    assert [c for c in world.ax.calls if c[0] in ("action", "set")] == [] and world.ex.runs == []   # no input


def test_notes_front_window_reads_the_selected_notes_folder(world, monkeypatch):
    J, runner = _ready(world, monkeypatch,
                       {AR.NOTES: '{"x-coredata://F/ICFolder/p7", 1, {"Auf meinem Mac", "Werkstatt Dichtungen"}}'})
    world.state["now"] = TJ.CLEAR
    r, _stop = J.handle("scope_read", {"bundle": "com.apple.Notes"})
    (w,) = r["windows"]
    assert w["scope"] == {"kind": "notes_folder", "strength": "path", "key": "Auf meinem Mac/Werkstatt Dichtungen",
                          "title": "Werkstatt Dichtungen", "source": "applescript"}
    assert w["ax_scope"]["title"] == "Werkstatt Dichtungen" and "finder_windows" not in r
    labels = [row["label"] for row in w["raw"]["outline_rows"]]
    assert len(labels) == 4 and labels[2].startswith("Werkstatt Dichtungen")
    assert [row["selected"] for row in w["raw"]["outline_rows"]] == [False, False, True, False]


@pytest.mark.parametrize("code", [-1743, -1744])
def test_without_the_grant_the_ax_reading_and_nothing_sent(world, monkeypatch, code):
    J, runner = _ready(world, monkeypatch, {AR.FINDER: FINDER_ROWS}, code=code, windows=_finder_windows())
    world.state["now"] = TJ.CLEAR
    r, _stop = J.handle("scope_read", {"bundle": "com.apple.finder", "window": "all"})
    assert r["ok"] and runner.calls == []
    for w in r["windows"]:
        assert w["script"]["status"] == "automation_not_granted" and w["script"]["code"] == code
        assert w["scope"] == w["ax_scope"] and w["scope"]["source"] == "title"
    assert r["finder_windows"] == [] and r["finder_windows_why"]["status"] == "automation_not_granted"


@pytest.mark.parametrize("why", sorted(TJ.STATES))
def test_a_visible_guest_answers_the_typed_refusal_and_reads_nothing(world, monkeypatch, why):
    J, runner = _ready(world, monkeypatch, {AR.FINDER: FINDER_ROWS}, windows=_finder_windows())
    world.state["now"] = TJ.STATES[why]
    r, stop = J.handle("scope_read", {"bundle": "com.apple.finder", "window": "all"})
    assert r["status"] == "refused" and r["refusal"]["kind"] == "foreground_interlock" and not stop
    assert runner.calls == [] and r.get("windows") is None


def test_not_running_and_an_unmanaged_app(world, monkeypatch):
    J, runner = _ready(world, monkeypatch, {AR.FINDER: FINDER_ROWS}, windows=_finder_windows())
    world.state["now"] = TJ.CLEAR
    monkeypatch.setattr(J, "pid_of", lambda bid: None)
    r, _ = J.handle("scope_read", {"bundle": "com.apple.finder"})
    assert r["status"] == "done" and r["ok"] is False and r["detail"] == "not running" and runner.calls == []
    r, _ = J.handle("scope_read", {"bundle": "com.apple.Safari"})
    assert r["status"] == "error" and "not one of this test's apps" in r["detail"]


def test_the_op_is_registered_and_the_job_agent_starts_no_script_process_itself():
    J = TJ._job()
    assert J.OPS["scope_read"] is J.op_scope_read
    src = open(os.path.join(HERE, "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    assert "osascript" not in src and "subprocess.Popen" not in src          # the job agent's rule, kept
    import inspect
    import applescript_guest as AG
    assert "automation_status" in inspect.getsource(AG.permission)    # askUserIfNeeded=false: never asks


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
