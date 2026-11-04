"""The runner contract in the executor and the AX tree (execution/ax_actions.py):
select_row, the focused selector kind, append_text / replace_line
and open_app through ``StateGraphExecutor`` on the REAL ``AccessibilityTree``, whose pyobjc
boundary is a fake (``FakeAX``; ``FakeAppKit`` for the running apps, the front app and the app
switch). No host desktop, no input.

Pinned here:
* a row is found in its IDENTIFIED list (Finder's sidebar and its file list are both outlines:
  a sidebar row of the label is never taken), by its first cell's text, or by an identifier of
  its own; none, two, a list over the row cap and an app over the element cap refuse; the
  selection of THAT list is read back, and a row added to a selection is deselected again and
  is a deviation;
* a row recorded BEFORE the row kind (its path and the reuse id every AppKit row shares) still
  resolves through ``find_selector`` / ``to_selector`` exactly as it did;
* the focused kind takes the focused text field (another role, no focus or a password field
  refuses), and its recorded document and app are checked;
* a mode's read-back mismatch is ``uncertain_completion`` with ``acted``: the ladder's next rung
  never writes a second time; line breaks of every kind are kept;
* open_app reads the front app back; after a real switch, a tree of the wrong app is
  ``wrong_app`` (read through the fake AppKit, no stubbed ``bundle_id``);
* with the foreground interlock refusing (no test bypass), nothing is actuated.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_ax_runner_executor.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import types

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"            # the fake AX below is the only "desktop"
os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = "1"    # honoured only under the guarded runner

from anyuse.context import accessibility as acc  # noqa: E402
from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import foreground_interlock as FI  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.plan_types import PlannedStep  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class El:
    _n = 0

    def __init__(self, role, parent=None, **attrs):
        El._n += 1
        self.id, self.attrs, self.parent, self.children = El._n, {"AXRole": role, **attrs}, parent, []
        if parent is not None:
            parent.children.append(self)

    def __eq__(self, other):
        return isinstance(other, El) and other.id == self.id

    def __hash__(self):
        return self.id


def app_of(el):
    while el.parent is not None:
        el = el.parent
    return el


class FakeAX:
    """ApplicationServices at the pyobjc boundary for one or more apps (``apps``: pid -> root):
    attributes, a list's selection, a text range edit.

    ``additive``: the list refuses AXSelectedRows and AXSelected ADDS the row to the selection;
    ``frozen``: a field accepts AXSelectedText and keeps its value; ``mangle``: the app changes
    what was typed (appends "!")."""

    kAXValueCFRangeType = 4

    def __init__(self, apps, additive=False, frozen=False, mangle=False, misselect=False, on_press=None):
        self.apps, self.additive, self.frozen, self.mangle, self.sets = apps, additive, frozen, mangle, []
        self.misselect, self.on_press, self.reads = misselect, on_press, 0
        #: the pid keyboard focus is in (the system-wide AXFocusedApplication); ``install`` follows the AppKit front
        self.focused_pid = lambda: None

    def AXUIElementCreateApplication(self, pid):
        return self.apps[pid]

    def AXUIElementCreateSystemWide(self):
        return "system-wide"

    def AXUIElementGetPid(self, el, _out):
        return 0, app_of(el).attrs.get("_pid", 1)

    def AXValueCreate(self, kind, rng):
        return tuple(rng)

    def AXUIElementCopyAttributeValue(self, el, name, _out):
        self.reads += 1
        if el == "system-wide":
            pid = self.focused_pid() if name == "AXFocusedApplication" else None
            return (0, self.apps[pid]) if pid in self.apps else (-25212, None)
        if el is None:
            return -25205, None
        if name == "AXChildren":
            return 0, list(el.children)
        if name == "AXParent":
            return 0, el.parent
        if name == "AXSelectedRows":
            return 0, [r for r in rows_of(el) if r.attrs.get("AXSelected")]
        if name in el.attrs:
            return 0, el.attrs[name]
        return -25205, None

    def AXUIElementSetAttributeValue(self, el, name, value):
        self.sets.append((el.id, name, value))
        if name == "AXSelectedRows":
            if self.additive:
                return -25200
            if self.misselect:
                # the list selects ANOTHER row of the same label (the one before it)
                value = [r for r in rows_of(el) if r is not value[0] and
                         r.children[0].children[-1].attrs.get("AXValue") ==
                         value[0].children[0].children[-1].attrs.get("AXValue")][:1]
            for r in rows_of(el):
                r.attrs["AXSelected"] = r in value
        elif name == "AXSelected":
            el.attrs["AXSelected"] = bool(value)
        elif name == "AXSelectedTextRange":
            el.attrs["_range"] = value
        elif name == "AXSelectedText":
            if not self.frozen:
                start, length = el.attrs.get("_range") or (len(el.attrs.get("AXValue") or ""), 0)
                v = el.attrs.get("AXValue") or ""
                el.attrs["AXValue"] = v[:start] + value + ("!" if self.mangle else "") + v[start + length:]
        elif name == "AXValue":
            el.attrs["AXValue"] = value
        return 0

    def AXUIElementPerformAction(self, el, action):
        self.sets.append((el.id, "perform", action))
        if self.on_press is not None:
            self.on_press(el)
        return 0


def rows_of(box):
    return [c for c in (box.children if box is not None else []) if c.attrs.get("AXRole") == "AXRow"]


class FakeAppKit:
    """AppKit: running apps by pid and bundle id, the front app, activation and launch."""

    NSApplicationActivateIgnoringOtherApps = 2

    def __init__(self, apps=None, front="com.apple.TextEdit", obey=True):
        self.by_pid = dict(apps or {1: ("com.apple.TextEdit", "TextEdit")})
        self.front, self.calls = front, []
        kit = self

        class _App:
            def __init__(self, pid, bid, name):
                self.pid, self.bid, self.name = pid, bid, name

            def bundleIdentifier(self):
                return self.bid

            def localizedName(self):
                return self.name

            def processIdentifier(self):
                return self.pid

            def activateWithOptions_(self, opts):
                kit.calls.append(("activate", self.bid))
                if obey:
                    kit.front = self.bid
                return True

        class _Running:
            @staticmethod
            def runningApplicationsWithBundleIdentifier_(bid):
                return [_App(p, b, n) for p, (b, n) in kit.by_pid.items() if b == bid]

            @staticmethod
            def runningApplicationWithProcessIdentifier_(pid):
                b, n = kit.by_pid.get(int(pid), (None, None))
                return _App(int(pid), b, n) if b else None

        class _WS:
            def launchAppWithBundleIdentifier_options_additionalEventParamDescriptor_launchIdentifier_(
                    self, bid, opts, desc, ident):
                kit.calls.append(("launch", bid))
                if obey:
                    kit.front = bid
                return True, None

            def frontmostApplication(self):
                return types.SimpleNamespace(bundleIdentifier=lambda: kit.front)

        self.NSRunningApplication = _Running
        self.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: _WS())


def install(apps, kit=None, **kw):
    """``apps``: a root element, or {pid: root}."""
    apps = apps if isinstance(apps, dict) else {1: apps}
    for pid, root in apps.items():
        root.attrs["_pid"] = pid
    fake = FakeAX(apps, **kw)
    acc._ax = lambda: fake
    acc.AX_AVAILABLE = True
    kit = kit or FakeAppKit()
    acc._appkit = lambda: kit
    # the live AX focus follows the app AppKit put in front (an activation that obeys moves both)
    fake.focused_pid = lambda: next((p for p, (b, _n) in kit.by_pid.items() if b == kit.front), None)
    return fake, kit


def outline(labels, box_attrs=None, row_ids=None):
    """An app with one window: an outline of rows, each AXRow > AXCell > (image,) text, plus a
    second column cell. ``row_ids``: each row's AXIdentifier (default: AppKit's shared reuse id)."""
    app = El("AXApplication")
    win = El("AXWindow", app, AXTitle="Dokumente")
    box = El("AXOutline", El("AXScrollArea", win), **(box_attrs or {}))
    made = []
    for i, label in enumerate(labels):
        row = El("AXRow", box, AXIdentifier=(row_ids[i] if row_ids else "NSTableViewRowViewKey"), AXWindow=win)
        cell = El("AXCell", row, AXWindow=win)
        El("AXImage", cell)
        El("AXTextField" if i % 2 == 0 else "AXStaticText", cell, AXValue=label)
        El("AXStaticText", El("AXCell", row), AXValue=f"Spalte 2 von {label}")
        made.append(row)
    return app, win, box, made


LIST = {"role": "outline", "identifier": "", "description": "", "path": "scrollarea[0]/outline[0]"}
#: the same list as the live tree reads it (no AXColumn child: 0 columns)
LIVE = dict(LIST, columns=0)


def step(action, selector=None, text=None, **cmd):
    c = {"role": "", "title": "", "action": action, **cmd}
    if selector is not None:
        c["selector"] = selector
        c["title"] = selector.get("name", "")
    return PlannedStep(intent=action, source="workflow", target_element="", modality="ax", command=c,
                       input_data=text)


def tree(pid=1):
    return acc.AccessibilityTree(pid=pid)


def run_ax(t, st):
    return StateGraphExecutor(ax_tree=t)._execute_ax(st)


# ── rows ───────────────────────────────────────────────────────────────────────────────────

def test_a_row_is_found_in_its_identified_list_by_its_first_cell_and_its_selection_read_back():
    app, win, box, rows = outline(["Rechnung.pdf", "Angebot.pdf"])
    fake, _kit = install(app)
    t = tree()
    assert t.container_identity(box) == LIVE
    assert [t._row_label(r) for r in rows] == ["Rechnung.pdf", "Angebot.pdf"]     # the FIRST cell's text
    res = run_ax(t, step("select_row", AX.row_selector("Angebot.pdf", LIST)))
    assert res["success"] and res["readback"] == ["Angebot.pdf"], res
    assert rows[1].attrs["AXSelected"] and not rows[0].attrs.get("AXSelected")
    # a second column's text is no row's label
    res = run_ax(t, step("select_row", AX.row_selector("Spalte 2 von Angebot.pdf", LIST)))
    assert res["refused"] and res["reason_code"] == "not_found", res
    # the list by another mark: description, identifier
    app, win, box, rows = outline(["Rechnung.pdf"], {"AXIdentifier": "fileList", "AXDescription": "Dateien"})
    install(app)
    for mark in ({"role": "outline", "identifier": "fileList"}, {"role": "outline", "description": "Dateien"}):
        assert run_ax(tree(), step("select_row", AX.row_selector("Rechnung.pdf", mark)))["success"], mark


def test_a_sidebar_row_of_the_label_is_never_taken_for_the_lists():
    """Finder: the sidebar and the file list are both outlines. "Downloads" is in the sidebar
    only; the recorded list (the file list) has no such row: not found, nothing selected."""
    app = El("AXApplication")
    win = El("AXWindow", app, AXTitle="Finder")
    split = El("AXSplitGroup", win)
    side = El("AXOutline", El("AXScrollArea", split), AXDescription="Seitenleiste")
    files = El("AXOutline", El("AXScrollArea", split))
    for box, label in ((side, "Downloads"), (files, "Rechnung.pdf")):
        El("AXStaticText", El("AXCell", El("AXRow", box, AXWindow=win)), AXValue=label)
    fake, _kit = install(app)
    file_list = {"role": "outline", "path": "splitgroup[0]/scrollarea[1]/outline[0]"}
    res = run_ax(tree(), step("select_row", AX.row_selector("Downloads", file_list)))
    assert res["refused"] and res["reason_code"] == "not_found", res
    assert not any(n in ("AXSelectedRows", "AXSelected") for _i, n, _v in fake.sets)
    # a role alone names both lists: no identity, refused before any lookup
    res = run_ax(tree(), step("select_row", AX.row_selector("Downloads", {"role": "outline"})))
    assert res["refused"] and res["reason_code"] == "identity", res
    # the sidebar by its description: that row
    res = run_ax(tree(), step("select_row", AX.row_selector("Downloads", {"role": "outline",
                                                                         "description": "Seitenleiste"})))
    assert res["success"], res


def test_two_rows_two_lists_long_lists_and_large_apps_refuse():
    app, win, box, rows = outline(["Bericht.pdf", "Bericht.pdf"])
    fake, _kit = install(app)
    res = run_ax(tree(), step("select_row", AX.row_selector("Bericht.pdf", LIST)))
    assert res["refused"] and res["reason_code"] == "ambiguous" and fake.sets == [], res
    # two lists of one identity (two windows with the same outline path)
    app, win, box, rows = outline(["A.pdf"])
    win2 = El("AXWindow", app, AXTitle="Zweites")
    El("AXStaticText", El("AXCell", El("AXRow", El("AXOutline", El("AXScrollArea", win2)))), AXValue="B.pdf")
    install(app)
    res = run_ax(tree(), step("select_row", AX.row_selector("A.pdf", LIST)))
    assert res["refused"] and res["reason_code"] == "ambiguous" and "2 lists" in res["error"], res
    # a list longer than the row cap: a twin could hide in the rest
    app, win, box, rows = outline([f"Datei {i}.pdf" for i in range(6)])
    install(app)
    t = tree()
    t.ROW_SCAN_CAP = 5
    res = run_ax(t, step("select_row", AX.row_selector("Datei 1.pdf", LIST)))
    assert res["refused"] and res["reason_code"] == "ambiguous" and "6 rows" in res["error"], res
    # more elements outside the lists than are read: another list cannot be ruled out
    t = tree()
    t.CONTAINER_SCAN_CAP = 2
    res = run_ax(t, step("select_row", AX.row_selector("Datei 1.pdf", LIST)))
    assert res["refused"] and res["reason_code"] == "ambiguous", res


def test_a_row_with_an_identifier_of_its_own_is_selectable_by_it():
    app, win, box, rows = outline(["Bericht.pdf", "Bericht.pdf"], row_ids=["row-a", "row-b"])
    install(app)
    res = run_ax(tree(), step("select_row", AX.row_selector("Bericht.pdf", LIST, identifier="row-b")))
    assert res["success"] and rows[1].attrs["AXSelected"] and not rows[0].attrs.get("AXSelected"), res
    # the shared reuse id names every row: the label decides
    app, win, box, rows = outline(["Rechnung.pdf", "Angebot.pdf"])
    install(app)
    res = run_ax(tree(), step("select_row", AX.row_selector("Angebot.pdf", LIST, identifier="NSTableViewRowViewKey")))
    assert res["success"] and rows[1].attrs["AXSelected"], res


def test_a_row_added_to_a_selection_is_deselected_and_is_a_deviation():
    app, win, box, rows = outline(["Rechnung.pdf", "Angebot.pdf"])
    rows[0].attrs["AXSelected"] = True                     # already selected
    fake, _kit = install(app, additive=True)               # the list adds instead of replacing
    res = run_ax(tree(), step("select_row", AX.row_selector("Angebot.pdf", LIST)))
    assert not res["success"] and res["outcome_kind"] == "deviation" and res["acted"], res
    assert res["undone"] and not rows[1].attrs["AXSelected"] and rows[0].attrs["AXSelected"]


def test_an_old_recorded_row_by_its_path_alone_is_refused_and_the_row_kind_resolves_it():
    """A row recorded before the row kind: the selector ``to_selector`` gave it then - no name
    (a row has no title), AppKit's shared reuse id, its path. Every row is identical in what a selector
    compares, so a re-sorted list puts another row at that path and a press by the path alone
    goes to it; such a selector is refused as ambiguous, with or without the
    recorded namesake count. The ``row`` kind, by the list and the row's label, still resolves it."""
    from anyuse.execution.ax_actions import row_selector
    app, win, box, rows = outline(["Rechnung.pdf", "Angebot.pdf", "Bericht.pdf"])
    fake, _kit = install(app)
    t = tree()
    old = t.to_selector(rows[1]).to_dict()
    assert (old["canonical_role"], old["name"], old["identifier"]) == ("row", "", "NSTableViewRowViewKey")
    assert "container" not in old and "kind" not in old              # recorded as before
    assert old["namesakes"] == 2 and old["shared_id"] == 2
    assert t.find_selector(acc.Selector.from_dict(old)) is None and t.last_ambiguity["n"] == 3
    legacy = {k: old[k] for k in ("canonical_role", "name", "identifier", "path", "raw_role")}
    assert t.find_selector(acc.Selector.from_dict(legacy)) is None and t.last_ambiguity["n"] == 3
    res = run_ax(t, step("press", old))
    assert res["reason_code"] == "ambiguous" and fake.sets == [], res
    assert t.find_row(row_selector("Angebot.pdf", t.row_container_identity(rows[1]))) == rows[1]


# ── the focused kind ────────────────────────────────────────────────────────────────────────

def form(focus_role="AXTextField", subrole=None, value="Notiz", document=None):
    app = El("AXApplication")
    win = El("AXWindow", app, AXTitle="Neu", **({"AXDocument": document} if document else {}))
    field = El(focus_role, win, AXValue=value, AXWindow=win, **({"AXSubrole": subrole} if subrole else {}))
    El("AXButton", win, AXTitle="Fertig", AXWindow=win)
    app.attrs["AXFocusedUIElement"] = field
    return app, field


def test_the_focused_kind_takes_the_focused_text_field_and_checks_role_focus_password_and_document():
    app, field = form()
    install(app)
    res = run_ax(tree(), step("append_text", AX.focused_selector("textfield"), " 2026", input_mode="append"))
    assert res["success"] and field.attrs["AXValue"] == "Notiz 2026", res
    for app, code, words in ((form("AXButton")[0], "identity", "button"),
                             (form("AXTextField", subrole="AXSecureTextField", value="")[0], "identity", "password")):
        fake, _kit = install(app)
        res = run_ax(tree(), step("append_text", AX.focused_selector("textfield"), "x", input_mode="append"))
        assert res["refused"] and res["reason_code"] == code and words in res["error"], res
        assert not any(n == "AXSelectedText" for _i, n, _v in fake.sets)
    app, field = form()
    app.attrs.pop("AXFocusedUIElement")
    install(app)
    res = run_ax(tree(), step("append_text", AX.focused_selector("textfield"), "x", input_mode="append"))
    assert res["refused"] and res["reason_code"] == "not_found", res
    # the recorded document: the focused field of another document is refused
    app, field = form(document="file:///Users/x/b.txt")
    fake, _kit = install(app)
    res = run_ax(tree(), step("append_text", AX.focused_selector("textfield", document="file:///Users/x/a.txt"), "x",
                              input_mode="append"))
    assert res["refused"] and res["reason_code"] == "wrong_document" and fake.sets == [], res


# ── text modes ──────────────────────────────────────────────────────────────────────────────

def document(value):
    app = El("AXApplication")
    win = El("AXWindow", app, AXTitle="Notiz.txt")
    area = El("AXTextArea", win, AXIdentifier="First Text View", AXValue=value, AXWindow=win)
    return app, area


AREA = {"canonical_role": "textarea", "name": "", "identifier": "First Text View", "path": "",
        "raw_role": "AXTextArea"}


def test_replace_line_keeps_every_kind_of_line_break():
    for before, after in (("a\r\nStatus: offen\r\nb", "a\r\nStatus: zu\r\nb"),
                          ("a\rStatus: offen\rb", "a\rStatus: zu\rb"),
                          ("a Status: offen b", "a Status: zu b"),
                          ("Status: offen", "Status: zu")):
        app, area = document(before)
        install(app)
        res = run_ax(tree(), step("replace_line", AREA, "Status: zu", input_mode="replace_line", anchor="Status:"))
        assert res["success"] and area.attrs["AXValue"] == after, (before, res, area.attrs["AXValue"])
    # a break it does not know (a form feed): refused, nothing written
    app, area = document("a\x0cStatus: offen")
    fake, _kit = install(app)
    res = run_ax(tree(), step("replace_line", AREA, "Status: zu", input_mode="replace_line", anchor="Status:"))
    assert res["refused"] and fake.sets == [], res


def test_a_read_back_mismatch_stops_and_the_next_rung_never_writes_twice():
    from anyuse.execution.deterministic import replay_transitions
    from anyuse.execution.outcome import STOP_KINDS, classify_outcome
    from anyuse.guides.state_graph import TransitionImpl, UITransition
    app, area = document("Notizen")
    fake, _kit = install(app, mangle=True)                  # the app changes what was typed
    cmd = {"action": "append_text", "role": "AXTextArea", "selector": AREA, "text": " neu", "input_mode": "append"}
    t = UITransition(from_state="", to_state="", intent="append a line",
                     implementations=[TransitionImpl(modality="ax", command=dict(cmd)),
                                      TransitionImpl(modality="ax", command=dict(cmd))])
    out = run(replay_transitions([t], StateGraphExecutor(ax_tree=tree())))
    assert not out["success"] and out["outcome_kind"] == "uncertain_completion", out
    assert len(out["steps"][0]["tried"]) == 1                                    # no second rung
    assert sum(1 for _i, n, _v in fake.sets if n == "AXSelectedText") == 1
    assert area.attrs["AXValue"] == "Notizen neu!"
    res = AX.append_text(tree(), area, {"input_data": "x"}, {"action": "append_text"})
    assert classify_outcome(res) in STOP_KINDS and res["acted"]


# ── open_app ────────────────────────────────────────────────────────────────────────────────

def two_apps():
    te = El("AXApplication")
    El("AXTextArea", El("AXWindow", te, AXTitle="Liste.txt"), AXIdentifier="First Text View", AXValue="Liste")
    notes = El("AXApplication")
    El("AXButton", El("AXWindow", notes, AXTitle="Notizen"), AXTitle="Neue Notiz")
    kit = FakeAppKit({1: ("com.apple.TextEdit", "TextEdit"), 2: ("com.apple.Notes", "Notizen")},
                     front="com.apple.TextEdit")
    return {1: te, 2: notes}, kit


def multi_record():
    """A compiled multi-app record: append to TextEdit's text, switch, press Notes' button."""
    te, notes = {"app": "TextEdit", "bundle_id": "com.apple.TextEdit"}, {"app": "Notes", "bundle_id": "com.apple.Notes"}
    ev = [{"id": "e0", "tag_type": "WORKFLOW_START", "timestamp": "2026-10-10T10:00:00", "data": {}},
          {"id": "e1", "tag_type": "STEP", "timestamp": "2026-10-10T10:00:01", "reason": "append",
           "data": dict(te, action="type", text=" und Brot", input_mode="append", selector=dict(AREA),
                        start={"lines": 1, "anchors": {}})},
          {"id": "e2", "tag_type": "STEP", "timestamp": "2026-10-10T10:00:02", "reason": "new note",
           "data": dict(notes, action="click", selector={"canonical_role": "button", "name": "Neue Notiz",
                                                         "raw_role": "AXButton"})},
          {"id": "e3", "tag_type": "WORKFLOW_END", "timestamp": "2026-10-10T10:00:03", "data": {}}]
    draft = {"name": "liste", "goal": "g", "session_id": "s", "apps_involved": ["TextEdit"], "urls_visited": [],
             "app": "TextEdit", "bundle_id": "com.apple.TextEdit", "events": ev, "anchors": [], "ax_states": {}}
    rep = run(DC.draft_to_workflow(draft, overrides=[{"name": "zeile", "ref": "@c1"}], save=False))
    assert rep["status"] == "ready", rep["reasons"]
    src = DC.load_source(draft)
    cands = DC.DP.collect_candidates(src.steps)
    params = DC.choose_parameters(src, cands, overrides=[{"name": "zeile", "ref": "@c1"}])["parameters"]
    return DC.compile_record(src, params, cands)


def test_open_app_switches_and_a_tree_of_the_wrong_app_is_wrong_app_afterwards():
    acc.ACTIVATE_SETTLE_S = 0.2
    apps, kit = two_apps()
    fake, _ = install(apps, kit=kit)
    rec = multi_record()

    async def yes(_ctx):
        return True
    # the right trees, by bundle id: the whole run on the real AX tree (no stubbed bundle_id)
    by_bid = {"com.apple.TextEdit": 1, "com.apple.Notes": 2}
    out = run(DC.run_steps(rec, {"zeile": " und Milch"}, executor=DC.checked_executor(tree(1)), approver=yes,
                           decide_as_driving=True, tree_factory=lambda bid: tree(by_bid[bid]), default_tree=tree(1)))
    assert out["ok"], out["stopped"]
    assert kit.calls == [("activate", "com.apple.Notes")] and kit.front == "com.apple.Notes"
    assert ("perform", "AXPress") in [(n, v) for _i, n, v in fake.sets]
    # the switch happened (a fake activate), then the factory hands out TextEdit's tree for Notes:
    # its real bundle_id() (read through AppKit) is not the step's - wrong_app, nothing pressed
    apps, kit = two_apps()
    fake, _ = install(apps, kit=kit)
    out = run(DC.run_steps(rec, {"zeile": " und Milch"}, executor=DC.checked_executor(tree(1)), approver=yes,
                           decide_as_driving=True, tree_factory=lambda bid: tree(1), default_tree=tree(1)))
    assert not out["ok"] and out["steps"][-1]["reason_code"] == "wrong_app", out
    assert kit.calls == [("activate", "com.apple.Notes")]
    assert not any(v == "AXPress" for _i, _n, v in fake.sets)
    # the activation did not bring it to the front: the read-back stops the run there
    apps, kit = two_apps()
    kit2 = FakeAppKit(kit.by_pid, front="com.apple.TextEdit", obey=False)
    install(apps, kit=kit2)
    out = run(DC.run_steps(rec, {"zeile": " und Milch"}, executor=DC.checked_executor(tree(1)), approver=yes,
                           decide_as_driving=True, tree_factory=lambda bid: tree(by_bid[bid]), default_tree=tree(1)))
    assert not out["ok"] and "not in front" in out["stopped"], out


def test_an_app_switch_outside_the_records_apps_or_the_policy_is_refused_before_it_is_held():
    from anyuse.core.restriction import RestrictionPolicy
    apps, kit = two_apps()
    install(apps, kit=kit)
    rec = multi_record()
    held = []

    async def note(ctx):
        held.append(ctx["args"].get("target"))
        return True
    by_bid = {"com.apple.TextEdit": 1, "com.apple.Notes": 2}
    # the approval line names the bundle id
    run(DC.run_steps(rec, {"zeile": " x"}, executor=DC.checked_executor(tree(1)), approver=note, decide_as_driving=True,
                     tree_factory=lambda bid: tree(by_bid[bid]), default_tree=tree(1)))
    assert "app Notes (com.apple.Notes)" in held
    # a policy that blocks Notes (by name or bundle id): refused, not held, not switched
    for blocked in ("Notes", "com.apple.Notes"):
        apps, kit = two_apps()
        install(apps, kit=kit)
        held.clear()
        policy = RestrictionPolicy.from_boundary({"blocked_apps": [blocked]})
        out = run(DC.run_steps(rec, {"zeile": " x"}, executor=DC.checked_executor(tree(1)), approver=note,
                               decide_as_driving=True, tree_factory=lambda bid: tree(by_bid[bid]),
                               default_tree=tree(1), restriction=policy))
        assert not out["ok"] and out["steps"][-1]["reason_code"] == "restricted" and kit.calls == [], out
        assert len(held) == 1                                   # only the first step was held
    # an app the recording never used
    bad = dict(rec, steps=[dict(s, command=dict(s["command"], bundle_id="com.evil.App"))
                           if s["command"]["action"] == "open_app" else s for s in rec["steps"]])
    apps, kit = two_apps()
    install(apps, kit=kit)
    out = run(DC.run_steps(bad, {"zeile": " x"}, executor=DC.checked_executor(tree(1)), approver=note,
                           decide_as_driving=True, tree_factory=lambda bid: tree(by_bid[bid]), default_tree=tree(1)))
    assert not out["ok"] and "not one of the recording's apps" in out["stopped"] and kit.calls == []
    # under ``destructive`` the switch to another app is held, the first app's own steps by their labels
    action = DC._gate_action(DC.bound_steps(rec, {"zeile": "x"})[1], "destructive", "com.apple.TextEdit")
    assert "com.apple.Notes" in action["args"]["hold"]
    from anyuse.execution.hil_gate import StepClass, classify_step
    assert classify_step(action, DC.gate_keywords("destructive")) == StepClass.DESTRUCTIVE


# ── the gates ───────────────────────────────────────────────────────────────────────────────

def test_with_the_interlock_refusing_nothing_is_actuated():
    """No test bypass: the foreground interlock itself refuses (a person is looking)."""
    app, win, box, rows = outline(["Rechnung.pdf"])
    area_app, area = document("Notizen")
    fake, kit = install({1: app, 2: area_app}, kit=FakeAppKit({1: ("com.apple.finder", "Finder")}))
    real, prev = FI.refusal, os.environ.pop("ANYUSE_INTERLOCK_TEST_BYPASS", None)
    FI.refusal = lambda where: f"refused by foreground interlock: a person is looking ({where})"
    try:
        t = tree(1)
        assert t.select_row(rows[0]) is False and t.deselect_row(rows[0]) is False
        assert t.activate_app("com.apple.Notes") is False
        assert not t.edit_text(area, "x", replace_range=(0, 0))
        # a primitive the gates refuse did not act: a refusal, not an uncertain completion
        for t2, st in ((tree(1), step("select_row", AX.row_selector("Rechnung.pdf", LIST))),
                       (tree(2), step("append_text", AREA, " neu", input_mode="append")),
                       (tree(1), step("open_app", bundle_id="com.apple.Notes", app_name="Notes"))):
            res = run_ax(t2, st)
            assert res["outcome_kind"] == "refused" and res["acted"] is False and res["reason_code"] == "gated", res
            assert "foreground interlock" in res["error"]
        assert area.attrs["AXValue"] == "Notizen"
        assert fake.sets == [] and kit.calls == []
        # the reads stay reads
        assert t.focused_element() is None and t.selected_row_labels(rows[0]) == []
    finally:
        FI.refusal = real
        if prev is not None:
            os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = prev


def test_with_the_drive_gate_off_nothing_is_actuated():
    app, win, box, rows = outline(["Rechnung.pdf"])
    fake, kit = install(app, kit=FakeAppKit({1: ("com.apple.finder", "Finder")}))
    prev = os.environ.get("ANYUSE_DRIVE_ENABLED")
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""
    try:
        t = tree()
        assert t.select_row(rows[0]) is False and t.activate_app("com.apple.Notes") is False
        assert fake.sets == [] and kit.calls == []
    finally:
        os.environ["ANYUSE_DRIVE_ENABLED"] = prev or ""


def test_rows_are_read_only_on_request_and_never_change_keys_diffs_or_dumps():
    import dataclasses
    app, win, box, rows = outline(["Rechnung.pdf", "Angebot.pdf"])
    install(app)
    t = tree()
    plain = t.snapshot()
    assert all(not hasattr(d, "row_label") for d in plain.elements)            # no extra reads by default
    snap = t.snapshot(row_labels=True)
    got = [(d.row_label, d.row_container) for d in snap.elements if d.role == "AXRow"]
    assert got == [("Rechnung.pdf", LIVE), ("Angebot.pdf", LIVE)]
    assert [d.key() for d in snap.elements] == [d.key() for d in plain.elements]
    assert all("row_label" not in dataclasses.asdict(d) for d in snap.elements)
    assert t.diff(plain, snap).is_empty()


# ── edge cases ──────────────────────────────────────────────────────────────────────────────

def test_a_same_label_row_selected_instead_does_not_pass():
    """Two rows "Bericht.pdf" with identifiers of their own; the step names row-b. The list
    selects row-a instead: the labels read back alike, the ELEMENT is not the target."""
    app, win, box, rows = outline(["Bericht.pdf", "Bericht.pdf"], row_ids=["row-a", "row-b"])
    install(app, misselect=True)
    res = run_ax(tree(), step("select_row", AX.row_selector("Bericht.pdf", LIST, identifier="row-b")))
    assert not res["success"] and res["outcome_kind"] == "uncertain_completion", res
    assert rows[0].attrs["AXSelected"] and not rows[1].attrs.get("AXSelected")


def test_a_list_known_by_its_path_must_also_have_the_recorded_column_count():
    """The file list (4 columns) was recorded by its path; the sidebar was hidden, the window's
    layout moved, and now the SIDEBAR (1 column) sits at that path with a row "Downloads"."""
    app = El("AXApplication")
    win = El("AXWindow", app, AXTitle="Finder")
    side = El("AXOutline", El("AXScrollArea", El("AXSplitGroup", win)))
    El("AXColumn", side)
    El("AXStaticText", El("AXCell", El("AXRow", side, AXWindow=win)), AXValue="Downloads")
    fake, _kit = install(app)
    recorded = {"role": "outline", "path": "splitgroup[0]/scrollarea[0]/outline[0]", "columns": 4}
    res = run_ax(tree(), step("select_row", AX.row_selector("Downloads", recorded)))
    assert res["refused"] and res["reason_code"] == "not_found" and fake.sets == [], res
    assert run_ax(tree(), step("select_row", AX.row_selector("Downloads", dict(recorded, columns=1))))["success"]


def focus_app(move_to=None):
    """A window with a search field that has focus, a button "Neuer Ordner", a name field; a
    second window with a field. ``move_to``: what the press focuses ("name", "other" or None)."""
    app = El("AXApplication")
    win = El("AXWindow", app, AXTitle="Dokumente")
    search = El("AXTextField", win, AXDescription="Suche", AXValue="", AXWindow=win)
    btn = El("AXButton", win, AXTitle="Neuer Ordner", AXWindow=win)
    name = El("AXTextField", win, AXValue="", AXWindow=win)
    win2 = El("AXWindow", app, AXTitle="Anderes")
    other = El("AXTextField", win2, AXValue="", AXWindow=win2)
    app.attrs["AXFocusedUIElement"] = search
    target = {"name": name, "other": other}.get(move_to)

    def on_press(el):
        if el is btn and target is not None:
            app.attrs["AXFocusedUIElement"] = target
    return app, on_press, {"search": search, "name": name, "other": other}


def focus_record():
    ev = [{"id": "e0", "tag_type": "WORKFLOW_START", "timestamp": "2026-10-10T10:00:00", "data": {}},
          {"id": "e1", "tag_type": "STEP", "timestamp": "2026-10-10T10:00:01", "reason": "new folder",
           "data": {"action": "click", "focuses": "AXTextField",
                    "selector": {"canonical_role": "button", "name": "Neuer Ordner", "raw_role": "AXButton"}}},
          {"id": "e2", "tag_type": "STEP", "timestamp": "2026-10-10T10:00:02", "reason": "name it",
           "data": {"action": "type", "text": "Archiv 2025"}},
          {"id": "e3", "tag_type": "WORKFLOW_END", "timestamp": "2026-10-10T10:00:03", "data": {}}]
    draft = {"name": "folder", "goal": "g", "session_id": "s", "apps_involved": ["Finder"], "urls_visited": [],
             "app": "Finder", "bundle_id": "com.apple.finder", "events": ev, "anchors": [], "ax_states": {}}
    src = DC.load_source(draft)
    cands = DC.DP.collect_candidates(src.steps)
    params = DC.choose_parameters(src, cands, overrides=[{"name": "name", "ref": "@c1"}])["parameters"]
    return DC.compile_record(src, params, cands)


def test_the_focused_field_must_be_one_the_trigger_moved_the_focus_to_in_its_window():
    rec = focus_record()
    assert rec["steps"][1]["command"]["selector"]["trigger"] == 0

    async def yes(_ctx):
        return True
    kit = FakeAppKit({1: ("com.apple.finder", "Finder")}, front="com.apple.finder")
    for move_to, ok, words in (("name", True, None), (None, False, "did not move"),
                               ("other", False, "another window")):
        app, on_press, els = focus_app(move_to)
        install(app, kit=kit, on_press=on_press)
        out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(tree()), approver=yes,
                               decide_as_driving=True))
        assert out["ok"] is ok, (move_to, out["stopped"])
        if ok:
            assert els["name"].attrs["AXValue"] == "Neu" and els["search"].attrs["AXValue"] == ""
        else:
            assert words in out["stopped"] and els["search"].attrs["AXValue"] == "", out["stopped"]
    # the step before a focused step is not its trigger (an edited record): refused
    edited = dict(rec, steps=[dict(rec["steps"][0], draft_index=9)] + rec["steps"][1:])
    app, on_press, els = focus_app("name")
    install(app, kit=kit, on_press=on_press)
    out = run(DC.run_steps(edited, {"name": "Neu"}, executor=DC.checked_executor(tree()), approver=yes,
                           decide_as_driving=True))
    assert not out["ok"] and "not the trigger" in out["stopped"] and els["name"].attrs["AXValue"] == ""


def test_a_plain_snapshot_reads_no_row_attributes_and_row_labels_read_only_rows():
    """Counted at the pyobjc boundary: a snapshot reads exactly what it read before the row kind;
    ``row_labels`` adds reads for the rows only."""
    app, win, box, rows = outline(["Rechnung.pdf", "Angebot.pdf"])
    fake, _kit = install(app)
    t = tree()
    fake.reads = 0
    plain = t.snapshot()
    n_plain = fake.reads
    per_element = 7                                  # _describe: role, title, value, role_desc, id, desc + subrole
    assert n_plain <= per_element * len(plain.elements) + len(plain.elements) + 2, n_plain
    t._row_label = lambda el: (_ for _ in ()).throw(AssertionError("a row label read in a plain snapshot"))
    t.snapshot()                                     # never calls _row_label
    del t._row_label
    fake.reads = 0
    t.snapshot(row_labels=True)
    extra = fake.reads - n_plain
    assert 0 < extra <= 40 * len(rows), extra       # the rows' cells and lists, nothing else


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
            import traceback
            f += 1; print("FAIL", n, "::", repr(e)[:600]); traceback.print_exc()
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f or p != 18 else 0)
