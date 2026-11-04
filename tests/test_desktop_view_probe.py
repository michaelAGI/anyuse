"""The GF10 probe, offline with a FAKE accessibility tree (no desktop, no
AX, no subprocess, no network).

Arch12 on GF10: Notes kept "Darstellung > Als Liste" disabled after the declared precondition had left the
search. The probe's guest ops are pinned here: ``view_probe`` READS (the View menu's items with the menu closed and
open, the first responder and its ancestors, the sidebar's selection and smart folders, the note list / gallery grid
/ note body, Arch8's gallery-note-open test, the search field, the toolbar) and acts on nothing;
``probe_act`` is a CLOSED set of single generic actions (keys and presses through the checked executor; a real click
only after a hit test names the row; the irreversible-action guard on every label); the host's probe uses GF10's own
triggers from the effect map, makes no model call and runs only as guest jobs.
"""
import importlib
import os
import sys
import types

import pytest

from anyuse.execution import desktop_e2e as X

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VM = os.path.join(HERE, "execution", "guest")
PID = 4242


def _job():
    if VM not in sys.path:
        sys.path.insert(0, VM)
    return importlib.import_module("desktop_e2e_job")


class El:
    """A fake AX element: a dict of attributes; children get their parent."""

    def __init__(self, role, kids=(), **attrs):
        self.a = {"AXRole": role, **attrs}
        self.a["AXChildren"] = list(kids)
        for k in kids:
            k.a["AXParent"] = self

    def __repr__(self):
        return f"El({self.a.get('AXRole')}, {self.a.get('AXIdentifier') or self.a.get('AXDescription') or ''})"


class FakeAS:
    def __init__(self, app):
        self.app, self.at_point, self.calls = app, None, []

    def AXUIElementCreateApplication(self, pid):
        return self.app

    def AXUIElementPerformAction(self, el, action):
        self.calls.append(("action", action))
        return 0

    def AXUIElementSetAttributeValue(self, el, name, value):
        self.calls.append(("set", name, value))
        el.a[name] = value
        return 0

    def AXUIElementCopyElementAtPosition(self, app, x, y, _out):
        self.calls.append(("hit", round(x), round(y)))
        return (0, self.at_point) if self.at_point is not None else (-25204, None)

    def AXUIElementGetPid(self, el, _out):
        return 0, PID


class FakeEX:
    def __init__(self):
        self.runs = []

    def run(self, el, pid, selector, action, **kw):
        self.runs.append((el, action, kw))
        return {"success": True, "refused": False, "outcome": "ok", "error": None}


def _row(label, selected=False):
    return El("AXRow", [El("AXCell", [El("AXStaticText", AXValue=label)], AXDescription=f"{label}, 3 Notizen")],
              AXSelected=selected)


def gf10_state(search="", table=False):
    """GF10's failing state as Arch12 left it: the search field empty and focused, the folder shown again, a
    note opened from the gallery (no grid, no list, its body shown)."""
    sf = El("AXTextField", [El("AXButton", AXDescription="Suchen")], AXSubrole="AXSearchField", AXValue=search,
            AXFocused=True, AXDescription="Suchen")
    toolbar = El("AXToolbar", [El("AXButton", AXDescription="Neue Notiz", AXEnabled=True), El("AXGroup", [sf])])
    rows = [_row("Alle auf meinem Mac"), _row("Notizen"), _row("Werkstatt Dichtungen", True), _row("Geteilt")]
    outline = El("AXOutline", rows, AXDescription="Ordner", AXRows=rows, AXFocused=False)
    body = El("AXTextArea", AXIdentifier="Note Body Text View", AXFocused=False,
              AXValue="Kaffee in der Werkstatt\nErsatzdichtung für die Kaffeemaschine: 18 mm, roter Ring.")
    content = [body]
    if table:
        content = [El("AXTable", AXIdentifier="ICMNoteListTableView", AXRows=[], AXFocused=False), body]
    w = El("AXWindow", [toolbar, El("AXSplitGroup", [El("AXScrollArea", [outline])] + content)],
           AXTitle="Werkstatt Dichtungen – 3 Notizen", AXMain=True, AXFocused=True)
    items = [El("AXMenuItem", AXTitle="Als Liste", AXEnabled=True), El("AXMenuItem", AXTitle="Als Galerie",
                                                                       AXEnabled=True)]
    menubar = El("AXMenuBar", [El("AXMenuBarItem", [El("AXMenu", items)], AXTitle="Darstellung")])
    app = El("AXApplication", [w], AXFocusedUIElement=sf, AXFocusedWindow=w, AXMenuBar=menubar, AXWindows=[w])
    return {"app": app, "w": w, "sf": sf, "outline": outline, "rows": rows, "body": body}


@pytest.fixture
def fake(monkeypatch):
    J = _job()
    # the fakes send nothing; the job agent's foreground interlock is let through by the test flag,
    # honoured only under the guarded runner's desktop import guard (test_desktop_job_interlock.py pins it)
    monkeypatch.setenv("ANYUSE_INTERLOCK_TEST_BYPASS", "1")
    st = gf10_state()
    ax, ex, menu_reads = FakeAS(st["app"]), FakeEX(), []

    def read_menu(pid, ti):
        menu_reads.append(ti)
        return [{"path": "Darstellung > Als Liste", "title": "Als Liste", "enabled": False, "mark": False},
                {"path": "Darstellung > Als Galerie", "title": "Als Galerie", "enabled": False, "mark": True},
                {"path": "Darstellung > Sortieren nach > Titel", "title": "Titel", "enabled": True, "mark": False}]

    monkeypatch.setattr(J, "_at", lambda el, name: el.a.get(name) if isinstance(el, El) else None)
    monkeypatch.setattr(J, "_s", lambda v: None if v is None else str(v))
    monkeypatch.setattr(J, "_AS", lambda: ax)
    monkeypatch.setattr(J, "front_window", lambda pid: st["w"])
    monkeypatch.setattr(J, "activate", lambda bid: PID)
    monkeypatch.setattr(J, "guard_before", lambda pid, where: None)
    monkeypatch.setattr(J, "guard_after", lambda pid, where: None)
    monkeypatch.setattr(J, "read_menu", read_menu)
    monkeypatch.setattr(J, "_app_windows", lambda pid: [st["w"]])
    monkeypatch.setattr(J, "_frame", lambda el: (10.0, 100.0, 180.0, 24.0))
    monkeypatch.setattr(J, "EX", ex)
    monkeypatch.setattr(J, "G", types.SimpleNamespace(record_selector=lambda pid, el: {"role": J.role(el)}))
    monkeypatch.setattr(J.time, "sleep", lambda s: None)
    return types.SimpleNamespace(J=J, st=st, ax=ax, ex=ex, menu_reads=menu_reads)


def test_view_probe_reads_gf10s_candidate_causes_and_acts_on_nothing(fake):
    J = fake.J
    r = J.op_view_probe({"bundle": J.NOTES})
    assert r["status"] == "done"
    assert r["view_items"]["Als Liste"] == {"enabled": False, "enabled_closed": True, "mark": False}
    assert r["view_items"]["Als Galerie"]["mark"] is True
    assert [m["title"] for m in r["menu"]] == ["Als Liste", "Als Galerie"]          # top-level items only
    assert r["gallery_note_open"] is True                       # Arch8's declared state, op_precondition's test
    assert r["body"]["first_line"] == "Kaffee in der Werkstatt" and not r["note_list"]["present"]
    assert not r["gallery"]["present"] and r["searching"] is False
    assert r["focus"]["in"] == "search_field" and r["focus"]["chain"][-1].startswith("AXWindow")
    assert r["sidebar"]["selected"] == ["Werkstatt Dichtungen"] and r["shown_folder"] == "Werkstatt Dichtungen"
    assert {s["label"] for s in r["sidebar"]["special"]} == {"Alle auf meinem Mac", "Notizen", "Geteilt"}
    assert r["toolbar"]["present"] and any(i.get("description") == "Neue Notiz" for i in r["toolbar"]["items"])
    assert r["search"][0]["value"] == "" and r["search"][0]["focused"] is True
    assert fake.menu_reads == [0]                               # the View menu opened ONCE (read_menu cancels it)
    assert not [c for c in fake.ax.calls if c[0] == "set"] and fake.ex.runs == []      # nothing set, nothing run
    with pytest.raises(ValueError):
        J.op_view_probe({"bundle": "com.apple.finder"})


def test_focus_in_names_where_the_keyboard_focus_is():
    J = _job()
    assert J.focus_in(None, []) == "nothing"
    assert J.focus_in({"role": "AXTextField", "subrole": "AXSearchField"}, []) == "search_field"
    assert J.focus_in({"role": "AXTextArea", "identifier": "Note Body Text View"}, []) == "note_body"
    assert J.focus_in({"role": "AXRow"}, ["AXTable#ICMNoteListTableView", "AXWindow"]) == "note_list"
    assert J.focus_in({"role": "AXCell"}, ["AXGroup#ICMNoteGridCollectionView_asGallery"]) == "gallery"
    assert J.focus_in({"role": "AXRow"}, ["AXOutline(Ordner)", "AXWindow"]) == "sidebar"
    assert J.focus_in({"role": "AXButton"}, ["AXToolbar", "AXWindow"]) == "other"


def test_probe_acts_are_a_closed_set_of_single_checked_actions(fake):
    J = fake.J
    assert J.PROBE_ACTS == ("leave_escape", "leave_clear", "leave_empty", "select_folder", "click_folder",
                            "focus_list", "escape_twice", "cmd_1", "cmd_2", "precondition", "relaunch")
    with pytest.raises(ValueError):
        J.op_probe_act({"act": "type_anything"})
    fake.st["sf"].a["AXValue"] = "Ersatzdichtung"
    r = J.op_probe_act({"act": "leave_escape"})
    assert r["status"] == "done" and r["act"] == "leave_escape" and r["searching"] is True    # the fake ignores keys
    assert [(a, kw) for _el, a, kw in fake.ex.runs] == [("press_key", {"keycode": 53})]
    fake.ex.runs.clear()
    r = J.op_probe_act({"act": "cmd_1"})
    assert [(el, a, kw) for el, a, kw in fake.ex.runs] == [(fake.st["w"], "press_key", {"keycode": 18,
                                                                                         "flags": 1 << 20})]
    fake.ex.runs.clear()
    J.op_probe_act({"act": "escape_twice"})
    assert [kw for _el, _a, kw in fake.ex.runs] == [{"keycode": 53}, {"keycode": 53}]


def test_focus_list_needs_a_list_or_grid(fake):
    J = fake.J
    r = J.op_probe_act({"act": "focus_list"})
    assert r["status"] == "failed" and "no note list" in r["detail"]          # a gallery note hides the grid
    st = gf10_state(table=True)
    fake.st.update(st)
    fake.ax.app = st["app"]
    r = J.op_probe_act({"act": "focus_list"})
    assert r["status"] == "done" and ("set", "AXFocused", True) in fake.ax.calls


def test_the_click_needs_a_hit_test_that_names_the_row(fake, monkeypatch):
    J = fake.J
    posted = []
    quartz = types.SimpleNamespace(CGPointMake=lambda x, y: (x, y), kCGEventMouseMoved=5, kCGEventLeftMouseDown=1,
                                   kCGEventLeftMouseUp=2, kCGMouseButtonLeft=0, kCGHIDEventTap=0,
                                   CGEventCreateMouseEvent=lambda src, t, pt, b: (t, pt),
                                   CGEventPost=lambda tap, ev: posted.append(ev))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    fake.ax.at_point = fake.st["body"]                                        # something else under the point
    r = J.op_probe_act({"act": "click_folder", "label": "Werkstatt Dichtungen"})
    assert r["status"] == "refused" and posted == []
    row = fake.st["rows"][2]
    fake.ax.at_point = row.a["AXChildren"][0].a["AXChildren"][0]              # the row's own text
    r = J.op_probe_act({"act": "click_folder", "label": "Werkstatt Dichtungen"})
    assert r["status"] == "done" and [ev[0] for ev in posted] == [5, 1, 2]
    posted.clear()
    r = J.op_probe_act({"act": "click_folder", "label": "Nicht da"})
    assert r["status"] == "refused" and posted == []
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
    r = J.op_probe_act({"act": "click_folder", "label": "Werkstatt Dichtungen"})
    assert r["status"] == "refused" and "drive gate" in r["detail"] and posted == []


def test_the_folder_label_passes_the_irreversible_action_guard(fake, monkeypatch):
    J = fake.J
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    fake.st["rows"][2].a["AXChildren"][0].a["AXChildren"][0].a["AXValue"] = "Alles löschen"
    r = J.op_probe_act({"act": "click_folder", "label": "Alles löschen"})
    assert r["status"] == "gated" and X.irreversible_reason("Alles löschen")


def test_the_new_ops_are_registered_and_read_or_act_only_on_notes():
    J = _job()
    assert J.OPS["view_probe"] is J.op_view_probe and J.OPS["probe_act"] is J.op_probe_act
    src = open(os.path.join(VM, "desktop_e2e_job.py"), encoding="utf-8").read()
    probe = src[src.index("def op_view_probe(a):"):src.index("def _click_sidebar_row(pid, label):")]
    assert "EX.run" not in probe and "SetAttributeValue" not in probe and "PerformAction" not in probe
    state = src[src.index("def view_state(pid"):src.index("def op_view_probe(a):")]
    assert "EX.run" not in state and "SetAttributeValue" not in state and "PerformAction" not in state


def _host():
    if VM not in sys.path:
        sys.path.insert(0, VM)
    return importlib.import_module("desktop_gf10_host")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
