"""The guest op ``ax_elements`` (execution/guest/desktop_e2e_job.py), offline with the
job interlock test's fakes (the GF10 fake AX world, fake session states fed to the real interlock rules).
Pinned here: the op lists the front window's and the menu bar's actionable elements with role, title, identifier,
path, value (never a secure field's), enabled and in_menubar, plus the static text; a visible guest (a viewer, the
screen locked, another console session, the host) answers the typed interlock refusal and reads nothing; an app
with no single running process is a typed ``ok: false``; the harness's ``DesktopTools.observe`` over the op
yields the recorder's fingerprint. No desktop, no AX, no subprocess, no network.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_desktop_job_ax_elements.py
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_desktop_job_interlock as TJ  # noqa: E402
import test_desktop_view_probe as T13  # noqa: E402

world = TJ.world          # the fixture, registered in this module


def _ready(w, monkeypatch):
    J = w.J
    monkeypatch.setattr(J, "app_pid", lambda app: (T13.PID, None))
    monkeypatch.setattr(J, "interlock_status", lambda: {"allowed": True})
    return J


def test_the_op_lists_actionable_elements_of_the_window_and_the_menu_bar(world, monkeypatch):
    J = _ready(world, monkeypatch)
    world.state["now"] = TJ.CLEAR
    r, stop = J.handle("ax_elements", {"app": "Notes"})
    assert r["status"] == "done" and r["ok"] and not stop
    by = {(e["role"], e["title"]): e for e in r["elements"]}
    assert ("AXButton", "Neue Notiz") in by and by[("AXButton", "Neue Notiz")]["enabled"] is True
    assert ("AXTextField", "Suchen") in by and by[("AXTextField", "Suchen")]["in_menubar"] is False
    assert ("AXTextArea", "") in by and by[("AXTextArea", "")]["identifier"] == "Note Body Text View"
    assert "Kaffee in der Werkstatt" in by[("AXTextArea", "")]["value"]
    assert by[("AXMenuBarItem", "Darstellung")]["in_menubar"] and by[("AXMenuItem", "Als Liste")]["in_menubar"]
    assert all("secure" in e and not e["secure"] for e in r["elements"])
    assert r["window"] == "Werkstatt Dichtungen – 3 Notizen" and r["pid"] == T13.PID and r["app"] == "Notes"
    assert "AXMenuBar" not in {e["role"] for e in r["elements"]}     # containers are not elements
    assert world.ax.calls == [] or all(c[0] not in ("action", "set") for c in world.ax.calls)   # a read sends nothing


def test_a_secure_field_keeps_its_value_to_itself(world, monkeypatch):
    J = _ready(world, monkeypatch)
    world.state["now"] = TJ.CLEAR
    pw = T13.El("AXTextField", AXSubrole="AXSecureTextField", AXValue="hunter2", AXDescription="Passwort")
    world.st["w"].a["AXChildren"].append(pw)
    r, _ = J.handle("ax_elements", {"app": "Notes"})
    row = next(e for e in r["elements"] if e["title"] == "Passwort")
    assert row["secure"] and row["value"] == ""


@pytest.mark.parametrize("why", sorted(TJ.STATES))
def test_a_visible_guest_answers_the_typed_refusal_and_reads_nothing(world, monkeypatch, why):
    J = _ready(world, monkeypatch)
    world.state["now"] = TJ.STATES[why]
    reads = []
    monkeypatch.setattr(J, "front_window", lambda pid: reads.append(pid) or world.st["w"])
    r, stop = J.handle("ax_elements", {"app": "Notes"})
    assert r["status"] == "refused" and r["refusal"]["kind"] == "foreground_interlock" and not stop
    assert reads == [] and r.get("elements") is None


def test_an_app_without_one_running_process_is_a_typed_answer(world, monkeypatch):
    J = _ready(world, monkeypatch)
    world.state["now"] = TJ.CLEAR
    monkeypatch.setattr(J, "app_pid", lambda app: (None, "0 running processes named 'Nope'"))
    r, _ = J.handle("ax_elements", {"app": "Nope"})
    assert r["status"] == "done" and r["ok"] is False and "0 running" in r["detail"] and r["elements"] == []


def test_the_harness_desktop_tool_observes_through_the_op(world, monkeypatch):
    from anyuse.execution import harness_tools as HT
    J = _ready(world, monkeypatch)
    world.state["now"] = TJ.CLEAR

    def guest(op, **a):
        r, _stop = J.handle(op, a)
        return r
    obs = HT.DesktopTools(guest, "Notes").observe()
    assert obs.kind == "ax" and obs.app == "Notes" and obs.window.startswith("Werkstatt") and not obs.blocked
    roles = {e.role for e in obs.elements}
    assert {"AXButton", "AXTextField", "AXTextArea", "AXMenuBarItem", "AXMenuItem"} <= roles
    fp = obs.fingerprint["fingerprint"]
    assert all(r["role"] not in ("AXMenuBarItem", "AXMenuItem") for r in fp["required_elements"])
    assert obs.state_id.startswith("notes_") and fp["structural_hash"]
    world.state["now"] = TJ.STATES["a screen-sharing viewer"]
    obs = HT.DesktopTools(guest, "Notes").observe()
    assert obs.blocked.startswith("the guest reports refused")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
