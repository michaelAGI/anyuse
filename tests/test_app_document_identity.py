"""App and document identity before every actuation,
and the cascade refuses an ambiguous match instead of taking the first.

A recorded ``Selector`` now carries the app's bundle id and the FULL document
path/URL of the element's window. ``find_selector`` scopes to that document
(full path, never the basename) and returns nothing when two or more candidates
remain; the executor re-checks the app and the document immediately before it
acts. The AX layer below is a fake at the pyobjc boundary (the real
``AccessibilityTree`` code runs on it): no host desktop, no input.

Run from the anyuse package dir:
    cd anyuse && python3 test_app_document_identity.py
"""
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"            # the fake AX below is the only "desktop"
os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = "1"    # honoured only under the guarded runner

from anyuse.context import accessibility as acc  # noqa: E402
from anyuse.execution.draft_compile import DraftSource, DraftStep, bind_steps  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.plan_types import PlannedStep  # noqa: E402

ROOT = "file:///Users/guest/anyuse-guest/scratch"


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


class FakeAX:
    def __init__(self, app):
        self.app, self.actions, self.sets = app, [], []

    def AXUIElementCreateApplication(self, pid):
        return self.app

    def AXUIElementCopyAttributeValue(self, el, name, _out):
        if name == "AXChildren":
            return 0, list(el.children)
        if name == "AXParent":
            return 0, el.parent
        if name == "AXFocusedWindow":
            return 0, self.app.attrs.get("_front")
        if name in el.attrs:
            return 0, el.attrs[name]
        return -25205, None

    def AXUIElementSetAttributeValue(self, el, name, value):
        self.sets.append((el, name, value))
        if name == "AXValue":
            el.attrs["AXValue"] = value
        return 0

    def AXUIElementPerformAction(self, el, action):
        self.actions.append((el.attrs.get("AXTitle") or el.attrs.get("AXIdentifier"), action))
        return 0


class Tree(acc.AccessibilityTree):
    """The real tree, told which app it is bound to and which app is in front."""

    def __init__(self, bundle="com.apple.TextEdit", front=None):
        super().__init__(pid=1)
        self._bundle, self._front = bundle, front or bundle

    def bundle_id(self):
        return self._bundle

    def frontmost_bundle_id(self):
        return self._front


def app_with(windows):
    """``windows``: [(document path or None, [(role, attrs), ...]), ...], front first."""
    app = El("AXApplication")
    made = []
    for doc, controls in windows:
        win = El("AXWindow", app, AXTitle=os.path.basename(doc or "Ohne Titel"),
                 **({"AXDocument": f"{ROOT}{doc}"} if doc else {}))
        group = El("AXGroup", win)
        els = [El(role, group, AXWindow=win, **attrs) for role, attrs in controls]
        made.append((win, els))
    app.attrs["_front"] = made[0][0]
    return app, made


def install(app):
    fake = FakeAX(app)
    acc._ax = lambda: fake
    acc.AX_AVAILABLE = True
    return fake


def press(sel, **cmd):
    return PlannedStep(intent="press", source="workflow", target_element="", modality="ax",
                       command={"role": sel.raw_role or "AXButton", "action": "press",
                                "selector": sel.to_dict(), **cmd})


TEXTAREA = ("AXTextArea", {"AXIdentifier": "First Text View", "AXValue": "seed"})


def test_selector_round_trip_keeps_old_dicts_and_adds_app_and_document():
    old = {"canonical_role": "button", "name": "OK", "identifier": "", "path": "", "raw_role": "AXButton"}
    assert acc.Selector.from_dict(old).to_dict() == old, "a pre-F13 selector is written back unchanged"
    new = dict(old, app="com.apple.TextEdit", document=f"{ROOT}/a/notes.txt")
    assert acc.Selector.from_dict(new).to_dict() == new
    assert acc.normalize_document("file:///Users/x/Neue%20Notiz.txt") == "/Users/x/Neue Notiz.txt"
    assert acc.same_document("/a/notes.txt", f"file:///a/notes.txt")
    assert not acc.same_document("/a/notes.txt", "file:///b/notes.txt"), "never by basename"
    assert acc.same_document("notes.txt", "file:///b/notes.txt"), "a bare name keeps the old rule"


def test_the_real_tree_records_app_and_full_document():
    app, [(win, [area])] = app_with([("/a/notes.txt", [TEXTAREA])])
    install(app)
    sel = Tree().to_selector(area)
    assert sel.app == "com.apple.TextEdit" and sel.document == f"{ROOT}/a/notes.txt", sel


def test_a_hit_tested_element_of_another_app_names_that_app():
    """find_at is system-wide: the selector of what is under the point names ITS app."""
    app, [(win, [ok])] = app_with([("/a/notes.txt", [("AXButton", {"AXTitle": "OK"})])])
    fake = install(app)
    fake.AXUIElementGetPid = lambda el, _o: (0, 4242)

    class Other(Tree):
        def bundle_id(self):
            return "com.apple.TextEdit"

        def element_bundle_id(self, element):
            err, pid = acc._ax().AXUIElementGetPid(element, None)
            return "com.apple.Notes" if pid == 4242 else self.bundle_id()

    sel = Other().to_selector(ok)
    assert sel.app == "com.apple.Notes", sel
    del fake.AXUIElementGetPid
    assert Tree().to_selector(ok).app == "com.apple.TextEdit", "no pid lookup: the tree's own app"


def test_wrong_app_is_refused_before_acting():
    app, [(win, [ok])] = app_with([("/a/notes.txt", [("AXButton", {"AXTitle": "OK"})])])
    fake = install(app)
    recorded = Tree("com.apple.TextEdit").to_selector(ok)
    res = StateGraphExecutor(ax_tree=Tree("com.apple.Notes"))._execute_ax(press(recorded))
    assert res["outcome_kind"] == "refused" and res["reason_code"] == "wrong_app", res
    assert fake.actions == []
    # the same step in the recorded app acts
    res = StateGraphExecutor(ax_tree=Tree("com.apple.TextEdit"))._execute_ax(press(recorded))
    assert res["success"] is True and fake.actions == [("OK", "AXPress")], (res, fake.actions)


def test_a_key_press_needs_the_recorded_app_in_front():
    app, [(win, [area])] = app_with([("/a/notes.txt", [TEXTAREA])])
    install(app)
    recorded = Tree().to_selector(area)
    step = PlannedStep(intent="return", source="workflow", target_element="", modality="ax",
                       command={"role": "AXTextArea", "action": "press_key", "keycode": 36,
                                "selector": recorded.to_dict()})
    res = StateGraphExecutor(ax_tree=Tree(front="com.apple.finder"))._execute_ax(step)
    assert res["reason_code"] == "wrong_app" and "keystrokes" in res["error"], res


def test_wrong_document_is_refused_by_the_scoped_resolution():
    app, [(win, [area])] = app_with([("/b/notes.txt", [TEXTAREA])])
    fake = install(app)
    recorded = acc.Selector(canonical_role="textarea", identifier="First Text View", raw_role="AXTextArea",
                            app="com.apple.TextEdit", document=f"{ROOT}/a/notes.txt")
    res = StateGraphExecutor(ax_tree=Tree())._execute_ax(press(recorded))
    assert res["outcome_kind"] == "refused" and res["reason_code"] == "not_found", res
    assert fake.actions == []


def test_wrong_document_is_refused_immediately_before_acting():
    """The window changed between resolution and action: the pre-act check sees it."""
    app, [(win_b, [area_b])] = app_with([("/b/notes.txt", [TEXTAREA])])
    fake = install(app)
    recorded = acc.Selector(canonical_role="textarea", identifier="First Text View", raw_role="AXTextArea",
                            app="com.apple.TextEdit", document=f"{ROOT}/a/notes.txt")

    class Stale(Tree):
        def find_selector(self, selector, document=None, mode=None):
            return area_b                     # what a resolver without the scope returned

        def to_selector(self, element):
            s = super().to_selector(element)
            s.document = ""                   # a describer that cannot see the window
            return s

    res = StateGraphExecutor(ax_tree=Stale())._execute_ax(press(recorded))
    assert res["outcome_kind"] == "refused" and res["reason_code"] == "wrong_document", res
    # the refusal names neither document: a path or a title is the person's data
    assert "another document" in res["error"] and "notes.txt" not in res["error"] and fake.actions == []


def test_two_documents_with_the_same_name_resolve_only_by_full_path():
    app, [(wa, [area_a]), (wb, [area_b])] = app_with([("/b/notes.txt", [TEXTAREA]),
                                                     ("/a/notes.txt", [TEXTAREA])])
    fake = install(app)
    tree = Tree()
    sel_a = tree.to_selector(area_b)                     # recorded in the /a document
    assert sel_a.document.endswith("/a/notes.txt")
    assert tree.find_selector(sel_a) == area_b, "the recorded document decides, not the z-order"
    step = PlannedStep(intent="type", source="workflow", target_element="", modality="ax",
                       command={"role": "AXTextArea", "action": "set_value", "selector": sel_a.to_dict()},
                       input_data="neu")
    res = StateGraphExecutor(ax_tree=tree)._execute_ax(step)
    assert res["success"] is True, res
    assert [(e, v) for e, n, v in fake.sets if n == "AXValue"] == [(area_b, "neu")]
    assert area_a.attrs["AXValue"] == "seed"
    # the old basename scope would have accepted both windows: never again
    assert tree.find_selector(sel_a, document="notes.txt") is None
    assert tree.last_ambiguity["n"] == 2


def test_two_identical_documents_without_a_recorded_document_are_ambiguous():
    app, [(wa, [area_a]), (wb, [area_b])] = app_with([("/b/notes.txt", [TEXTAREA]),
                                                     ("/a/notes.txt", [TEXTAREA])])
    fake = install(app)
    legacy = acc.Selector(canonical_role="textarea", identifier="First Text View", raw_role="AXTextArea",
                          path="application/window[0]/group[0]/textarea[0]")
    res = StateGraphExecutor(ax_tree=Tree())._execute_ax(press(legacy))
    assert res["outcome_kind"] == "refused" and res["reason_code"] == "ambiguous", res
    assert "2 candidates" in res["error"] and fake.actions == [] and fake.sets == []
    # the weighted matcher refuses the same cross-window duplicate (no z-order pick)
    tree = Tree()
    assert tree.find_selector(legacy, mode="weighted") is None and tree.last_ambiguity["n"] == 2


def test_two_equal_candidates_in_one_window_are_refused_even_with_the_recorded_path():
    ok = ("AXButton", {"AXTitle": "OK"})
    app, [(win, [first, second])] = app_with([("/a/form.txt", [ok, ok])])
    fake = install(app)
    tree = Tree()
    bare = acc.Selector(canonical_role="button", name="OK", raw_role="AXButton")
    res = StateGraphExecutor(ax_tree=tree)._execute_ax(press(bare))
    assert res["reason_code"] == "ambiguous" and fake.actions == [], res
    # the recorded path below the window does not break this tie: two
    # identical buttons that trade places leave the window exactly as recorded, so the path
    # points at the twin; a recording knows its element had a namesake and refuses
    recorded = tree.to_selector(second)
    assert acc.path_within_window(recorded.path) == "group[0]/button[1]", recorded.path
    assert recorded.namesakes == 1
    assert tree.find_selector(recorded) is None and tree.last_ambiguity["n"] == 2
    legacy = acc.Selector(canonical_role="button", name="OK", raw_role="AXButton", path=recorded.path)
    assert tree.find_selector(legacy) is None and tree.last_ambiguity["n"] == 2


def test_an_editable_fields_value_is_not_its_identity():
    """In an offline replay, a Finder rename field recorded empty was refused as
    'another element' for the Return after the text had been typed into it."""
    sel = acc.selector_from_attrs(role="AXTextField", value="Archiv 2025", path="window[0]/textfield[0]")
    assert sel.name == "" and sel.canonical_role == "textfield"
    assert acc.selector_from_attrs(role="AXTextField", title="Betreff", value="x").name == "Betreff"
    assert acc.selector_from_attrs(role="AXStaticText", value="Version 2").name == "Version 2"
    app, [(win, [field])] = app_with([("/a/f.txt", [("AXTextField", {"AXValue": ""})])])
    install(app)
    tree = Tree()
    recorded = tree.to_selector(field)
    field.attrs["AXValue"] = "Archiv 2025"          # the step typed the new name
    step = PlannedStep(intent="return", source="workflow", target_element="", modality="ax",
                       command={"role": "AXTextField", "action": "press_key", "keycode": 36,
                                "selector": recorded.to_dict()})
    t = Tree()
    t.press_key = lambda code, flags=0, presser=None: True
    res = StateGraphExecutor(ax_tree=t)._execute_ax(step)
    assert res["success"] is True, res


def test_single_app_draft_names_its_app_in_every_selector():
    step = DraftStep(index=0, action="press", intent="press OK", raw_role="AXButton",
                     selector={"canonical_role": "button", "name": "OK", "identifier": "",
                               "path": "", "raw_role": "AXButton"})
    src = DraftSource(ref="r", kind="draft_file", name="w", app="TextEdit",
                      bundle_id="com.apple.TextEdit", apps=["TextEdit"], steps=[step])
    steps, recorded = bind_steps(src, [], [])
    assert steps[0]["command"]["selector"]["app"] == "com.apple.TextEdit"
    multi = DraftSource(ref="r", kind="draft_file", name="w", app="TextEdit",
                        bundle_id="com.apple.TextEdit", apps=["TextEdit", "Finder"],
                        steps=[DraftStep(index=0, action="press", intent="x", raw_role="AXButton",
                                         selector={"canonical_role": "button", "name": "OK"})])
    assert "app" not in bind_steps(multi, [], [])[0][0]["command"]["selector"]


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
