"""`set_value` returns evidence, not a return code.

What the guest VM showed (macOS 26.6.2 / 25G83, de_DE, German
keyboard layout; every value below is in ``testdata/ax/textedit_text_entry_probes.json``
verbatim as observed):

* An ``AXValue`` write to TextEdit's text area returns ``kAXErrorSuccess``,
  READS BACK through ``AXValue``, ``AXNumberOfCharacters`` and
  ``AXStringForRange``, and — when the target document is the one in front —
  is what the app saves (probe 101-A, diag 102 R1/RAW: 6/6 on disk). An earlier
  0/10 was therefore not the write being discarded: it was the write landing in
  a DIFFERENT document. Every TextEdit document window carries the same text
  area (``First Text View`` at ``window[0]/scrollarea[0]/textarea[0]``), so an
  unscoped selector resolves to whichever document is in front, and read-back
  from that element is truthful about the wrong document. No read-back can
  see that; only an accessor outside the UI (the file) can.
* Keystrokes typed NOTHING six times in a row while the AX tree, the frontmost
  app and the focused element all looked right (diag 102 KEYS/KEYS+raise):
  the HID modifier state had ⌘ stuck from an earlier posted ⌘F (diag 103:
  flags ``0x20100000`` → ``0x20000000`` after posting the modifier key-ups),
  so every character arrived as a ⌘-shortcut.
* Keycode typing from a US table produced ``anzuse kezs kezcodesus`` on the
  German layout (diag 103); Unicode-string key events typed the text.
* In the document-scoped repair, AXValue reached the background target while
  the keystroke fallback typed into the FRONT document and saved it
  (flows_p_extra_doc): a wrong-target write the keystroke rung must refuse
  unless the element holds keyboard focus.

Run from the anyuse package dir:
    cd anyuse && python3 test_text_entry_evidence.py
"""
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"   # the fake AX below is the only "desktop" here
# The fake AX / Quartz below send nothing; the foreground interlock would refuse on a
# desktop where someone is logged in. Its test flag works only under the guarded runner.
os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = "1"

from anyuse.context import accessibility as acc  # noqa: E402
from anyuse.context import keyboard as kb        # noqa: E402

FIXTURE = json.loads((_HERE / "testdata" / "ax" / "textedit_text_entry_probes.json").read_text())


# ---------------------------------------------------------------------------
# A fake AX server that behaves the way the guest's did
# ---------------------------------------------------------------------------

class El:
    _n = 0

    def __init__(self, role, parent=None, **attrs):
        El._n += 1
        self.id = El._n
        self.attrs = {"AXRole": role, **attrs}
        self.parent = parent
        self.children = []
        if parent is not None:
            parent.children.append(self)

    def __eq__(self, other):
        return isinstance(other, El) and other.id == self.id

    def __hash__(self):
        return self.id


class Doc:
    """A document model behind a text area: what the app would SAVE."""

    def __init__(self, text, follows_ax_value=True):
        self.text = text
        self.follows_ax_value = follows_ax_value   # False = the write-discarded hypothesis


class FakeAX:
    kAXValueCFRangeType = 4

    def __init__(self, app):
        self.app = app
        self.sets = []          # every attribute write, in order
        self.actions = []

    def AXUIElementCreateApplication(self, pid):
        return self.app

    def AXUIElementCopyAttributeValue(self, el, name, _out):
        if name == "AXChildren":
            return 0, list(el.children)
        if name == "AXParent":
            return 0, el.parent
        if name == "AXFocusedUIElement":
            return 0, self.app.attrs.get("_focused")
        if name == "AXFocusedWindow":
            return 0, self.app.attrs.get("_front")
        if name in el.attrs:
            return 0, el.attrs[name]
        return -25205, None

    def AXUIElementSetAttributeValue(self, el, name, value):
        self.sets.append((el, name, value))
        if name == "AXValue" and el.attrs.get("AXRole") == "AXTextArea":
            el.attrs["AXValue"] = value
            el.attrs["AXNumberOfCharacters"] = len(value)
            doc = el.attrs.get("_doc")
            if doc is not None and doc.follows_ax_value:
                doc.text = value
            return 0
        if name == "AXSelectedText" and self.app.attrs.get("_focused") == el:
            el.attrs["AXValue"] = value
            el.attrs["AXNumberOfCharacters"] = len(value)
            doc = el.attrs.get("_doc")
            if doc is not None and doc.follows_ax_value:
                doc.text = value
            return 0
        if name == "AXFocused":
            # focus moves only within the FRONT window, as on a real desktop
            if el.attrs.get("AXWindow") == self.app.attrs.get("_front"):
                self.app.attrs["_focused"] = el
            return 0
        if name in ("AXSelectedTextRange", "AXMain"):
            return 0
        return -25205

    def AXUIElementPerformAction(self, el, action):
        self.actions.append((el, action))
        if action == "AXRaise" and el.attrs.get("AXRole") == "AXWindow":
            self.app.attrs["_front"] = el
            return 0
        return 0

    def AXValueCreate(self, kind, value):
        return ("cfrange", value)


def textedit(fake_docs):
    """An app with one window per document, each with a text area, in the
    order the guest showed them (front window first)."""
    app = El("AXApplication")
    windows = []
    for i, (name, doc) in enumerate(fake_docs):
        win = El("AXWindow", app, AXTitle=name, AXDocument=f"file:///Users/guest/anyuse-guest/scratch/flows/{name}")
        scroll = El("AXScrollArea", win)
        area = El("AXTextArea", scroll, AXIdentifier="First Text View", AXValue=doc.text,
                  AXNumberOfCharacters=len(doc.text), AXWindow=win, _doc=doc)
        windows.append((win, area))
    app.attrs["_front"] = windows[0][0]
    app.attrs["_focused"] = None
    return app, windows


def typer_that_types_into(app, log):
    """A keystroke actuator that, like the real one, types into whatever holds
    focus — and records every call."""
    def typer(text):
        log.append(text)
        el = app.attrs.get("_focused")
        if el is not None:
            el.attrs["AXValue"] = text
            el.attrs["AXNumberOfCharacters"] = len(text)
            el.attrs["_doc"].text = text
        return {"typed": True, "chars": len(text), "stuck_modifiers": []}
    return typer


def install(fake):
    acc._ax = lambda: fake
    acc.AX_AVAILABLE = True


# ---------------------------------------------------------------------------
# the fixture says what happened
# ---------------------------------------------------------------------------

def test_fixture_records_the_probes():
    p = FIXTURE["probe101_axvalue_vs_accessors"]
    assert p["A_after_write"]["value"] == "evidence probe alpha"
    assert p["A_after_write"]["nchars"] == 20
    assert p["A_disk"] == "evidence probe alpha"           # the AXValue write WAS saved
    assert p["C_disk"] == "seed\n"                          # keystrokes typed nothing ...
    d = FIXTURE["diag103"]
    assert d["flags_before"]["hid_set"] == ["cmd"]          # ... because ⌘ was stuck
    assert d["flags_after_release"]["hid_set"] == []
    keys = {k["tag"]: k for k in d["keys"]}
    assert keys["hid_unicode_zeroflags"]["success"] is True
    assert keys["keycodes_us"]["readback"] == "anzuse kezs kezcodesus"   # US keycodes on a German layout
    assert keys["keycodes_us"]["success"] is False
    r1 = d["prior_repro"]
    assert r1["windows"][0] == "k103_target.txt" and r1["resolved_window"] == "k103_target.txt"
    variants = FIXTURE["diag102"]["variants"]
    assert all(v["success"] for v in variants if v["variant"] in ("r1_product_path", "raw_ax"))
    assert not any(v["success"] for v in variants if v["variant"].startswith("keys"))
    runs = FIXTURE["flow1_verified"]["runs"]
    assert len(runs) == 15 and all(r["success"] and r["rung"] == "ax_value"
                                   and r["verified_independently"] for r in runs)
    assert sum(r["wrong_target"] for r in runs) == 0


# ---------------------------------------------------------------------------
# set_value / set_value_evidence
# ---------------------------------------------------------------------------

def test_set_value_is_true_only_when_the_value_reads_back():
    app, [(win, area)] = textedit([("doc.txt", Doc("seed\n"))])
    install(FakeAX(app))
    tree = acc.AccessibilityTree(pid=1)
    ev = tree.set_value_evidence(area, "evidence probe alpha")
    assert ev.accepted and ev.readback == "evidence probe alpha" and ev.nchars == 20
    assert ev.verified and bool(ev)
    assert ev.window_title == "doc.txt" and ev.document.endswith("/doc.txt")
    assert ev.independent is None                    # nobody asked for proof; none is claimed
    assert tree.set_value(area, "again") is True

    # accepted, but the app shows something else: the old code said True here
    class Liar(FakeAX):
        def AXUIElementSetAttributeValue(self, el, name, value):
            self.sets.append((el, name, value))
            return 0                                   # rc success, nothing changes
    install(Liar(app))
    ev = acc.AccessibilityTree(pid=1).set_value_evidence(area, "never shown")
    assert ev.accepted and not ev.readback_matches and not ev.verified and not bool(ev)
    assert acc.AccessibilityTree(pid=1).set_value(area, "never shown") is False


def test_gate_off_writes_nothing_and_says_so():
    app, [(win, area)] = textedit([("doc.txt", Doc("seed\n"))])
    fake = FakeAX(app)
    install(fake)
    os.environ["ANYUSE_DRIVE_ENABLED"] = "0"
    try:
        ev = acc.AccessibilityTree(pid=1).set_value_evidence(area, "x")
        res = acc.AccessibilityTree(pid=1).enter_text(area, "x")
    finally:
        os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    assert not ev.accepted and "gated" in ev.error and fake.sets == []
    assert not res.success and "gated" in res.error


# ---------------------------------------------------------------------------
# enter_text: the ladder, with and without an independent accessor
# ---------------------------------------------------------------------------

def test_enter_text_stops_at_the_first_rung_the_independent_accessor_confirms():
    doc = Doc("seed\n")
    app, [(win, area)] = textedit([("flow1_gamma_0.txt", doc)])
    install(FakeAX(app))
    line = "anyuse desktop validation gamma 0"
    res = acc.AccessibilityTree(pid=1).enter_text(area, line, verify=lambda: line in doc.text,
                                                 expect_document="flow1_gamma_0.txt", settle=0)
    assert res.success and res.rung == "ax_value" and res.verified_independently
    assert res.fall_throughs == 0 and not res.wrong_target
    assert res.rungs[0].independent is True and res.window_raised is True
    d = res.to_dict()
    assert d["rung"] == "ax_value" and d["verified_independently"] and d["wrong_target"] is False


def test_enter_text_falls_through_when_readback_lies_about_the_document():
    """The first hypothesis: the AX side shows the text, the model never got it."""
    doc = Doc("seed\n", follows_ax_value=False)
    app, [(win, area)] = textedit([("doc.txt", doc)])
    fake = FakeAX(app)
    install(fake)
    typed = []
    line = "anyuse shaped"
    res = acc.AccessibilityTree(pid=1).enter_text(
        area, line, verify=lambda: line in doc.text, typer=typer_that_types_into(app, typed), settle=0)
    assert [r.method for r in res.rungs] == ["ax_value", "ax_selected_text", "keystrokes"]
    assert res.rungs[0].readback_matches and res.rungs[0].independent is False   # reads back, not saved
    assert res.rungs[1].readback_matches and res.rungs[1].independent is False
    assert res.rung == "keystrokes" and res.verified_independently and res.fall_throughs == 2
    assert typed == [line] and doc.text == line

    # without an accessor the ladder stops at read-back — and SAYS it was not proved
    doc2 = Doc("seed\n", follows_ax_value=False)
    app2, [(w2, a2)] = textedit([("doc.txt", doc2)])
    install(FakeAX(app2))
    res2 = acc.AccessibilityTree(pid=1).enter_text(a2, line, settle=0)
    assert res2.success and res2.rung == "ax_value" and res2.verified_independently is False
    assert doc2.text == "seed\n"                         # which is exactly why it must say so


def test_a_selector_that_resolves_into_the_other_document_is_refused_not_written():
    """Two documents open, the other one in front: the stored selector resolves
    to the front window's text area (identifier and path identical)."""
    target, other = Doc("seed\n"), Doc("the other document\n")
    app, [(w_other, a_other), (w_target, a_target)] = textedit(
        [("flow1_other_gamma_0.txt", other), ("flow1_gamma_0.txt", target)])
    fake = FakeAX(app)
    install(fake)
    tree = acc.AccessibilityTree(pid=1)
    sel = acc.Selector.from_dict(FIXTURE["flow1_verified"]["recorded_steps"]["area"]["selector"])
    got = tree.find_selector(sel)
    # the unscoped selector used to resolve to the FRONT
    # document's text area (as in the guest). Two windows hold a candidate, so
    # the cascade now refuses the ambiguity instead of taking the first.
    assert got is None and tree.last_ambiguity and tree.last_ambiguity["n"] == 2, tree.last_ambiguity
    got = a_other                                        # had a caller picked the front one anyway
    line = "anyuse desktop validation gamma 0"
    res = tree.enter_text(got, line, verify=lambda: line in target.text,
                          expect_document="flow1_gamma_0.txt", settle=0)
    assert res.wrong_target and not res.success and res.rungs == []
    assert fake.sets == [] and other.text == "the other document\n"   # NOTHING was written

    # scoped to the document, the same selector finds the right window ...
    scoped = tree.find_selector(sel, document="flow1_gamma_0.txt")
    assert scoped == a_target
    # ... and enter_text brings that window to the front before doing anything
    typed = []
    res = tree.enter_text(scoped, line, verify=lambda: line in target.text,
                          typer=typer_that_types_into(app, typed), expect_document="flow1_gamma_0.txt", settle=0)
    assert res.window_raised is True and (w_target, "AXRaise") in fake.actions
    assert res.success and res.verified_independently and target.text == line
    assert other.text == "the other document\n"


def test_keystroke_rung_refuses_to_type_when_the_element_does_not_hold_focus():
    """What went wrong in the extra-document run: AXValue reached the background
    target, then the keystroke fallback typed into the FRONT document."""
    target, other = Doc("seed\n", follows_ax_value=False), Doc("the other document\n")
    app, [(w_other, a_other), (w_target, a_target)] = textedit(
        [("other.txt", other), ("target.txt", target)])

    class NoRaise(FakeAX):                              # a window that will not come forward
        def AXUIElementPerformAction(self, el, action):
            self.actions.append((el, action))
            return 0
    fake = NoRaise(app)
    install(fake)
    typed = []
    line = "must not land in the other document"
    res = acc.AccessibilityTree(pid=1).enter_text(
        a_target, line, verify=lambda: line in target.text, typer=typer_that_types_into(app, typed), settle=0)
    assert res.window_raised is False
    ks = [r for r in res.rungs if r.method == "keystrokes"][0]
    assert ks.refused and "focus" in ks.error and not ks.accepted
    assert typed == []                                   # the actuator was never called
    assert other.text == "the other document\n"
    assert not res.success and res.refused_rungs == 1 and res.to_dict()["refused_rungs"] == 1


def test_focus_through_a_field_editor_counts_and_another_control_does_not():
    """A search field hands keyboard focus to a child text element (a field
    editor); that is still the field holding focus. A different control — the
    Help menu's search field, once recorded by mistake — is not."""
    app, [(win, area)] = textedit([("doc.txt", Doc("seed\n"))])
    editor = El("AXTextField", area, AXWindow=win)          # a child of the target
    other = El("AXTextField", win, AXWindow=win)            # a sibling control
    fake = FakeAX(app)
    install(fake)
    tree = acc.AccessibilityTree(pid=1)
    app.attrs["_focused"] = editor
    assert tree.holds_focus(area) and tree.holds_focus(editor)
    app.attrs["_focused"] = other
    assert not tree.holds_focus(area)
    app.attrs["_focused"] = None
    assert not tree.holds_focus(area)


def test_stuck_modifiers_are_reported_as_evidence():
    app, [(win, area)] = textedit([("doc.txt", Doc("seed\n", follows_ax_value=False))])
    install(FakeAX(app))

    def typer(text):
        el = app.attrs["_focused"]
        el.attrs["AXValue"] = text
        el.attrs["AXNumberOfCharacters"] = len(text)
        el.attrs["_doc"].text = text
        return {"typed": True, "chars": len(text), "stuck_modifiers": ["cmd"]}
    res = acc.AccessibilityTree(pid=1).enter_text(area, "x", rungs=("keystrokes",), typer=typer, settle=0)
    assert res.rung == "keystrokes" and res.rungs[0].stuck_modifiers == ["cmd"]
    assert res.to_dict()["rungs"][0]["stuck_modifiers"] == ["cmd"]


# ---------------------------------------------------------------------------
# keyboard: unicode events with empty flags, and the stuck-modifier release
# ---------------------------------------------------------------------------

class FakeQuartz:
    kCGEventSourceStateHIDSystemState = 1
    kCGHIDEventTap = 0
    kCGEventFlagMaskCommand = 0x100000
    kCGEventFlagMaskShift = 0x20000
    kCGEventFlagMaskAlternate = 0x80000
    kCGEventFlagMaskControl = 0x40000
    kCGEventFlagMaskSecondaryFn = 0x800000

    def __init__(self, flags):
        self.flags = flags
        self.posted = []          # (keycode, down, unicode, flags)

    def CGEventSourceFlagsState(self, _state):
        return self.flags

    def CGEventSourceCreate(self, _state):
        return "src"

    def CGEventCreateKeyboardEvent(self, _src, keycode, down):
        return {"keycode": keycode, "down": down, "unicode": None, "flags": None}

    def CGEventKeyboardSetUnicodeString(self, ev, n, s):
        ev["unicode"] = s

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventPost(self, _tap, ev):
        self.posted.append((ev["keycode"], ev["down"], ev["unicode"], ev["flags"]))
        if ev["keycode"] in kb._MODIFIER_KEYCODES and not ev["down"]:
            self.flags = 0        # a modifier key-up clears the stuck state


def test_type_unicode_releases_a_stuck_command_key_and_types_characters_not_keycodes():
    q = FakeQuartz(flags=0x20100000)                    # diag 103: ⌘ held, nobody touching it
    kb._quartz = lambda: q
    assert kb.stuck_modifiers() == ["cmd"]
    out = kb.type_unicode("anyuse keys", per_char=0)
    assert out["typed"] and out["chars"] == 11 and out["stuck_modifiers"] == ["cmd"]
    ups = [p for p in q.posted if p[0] in kb._MODIFIER_KEYCODES and not p[1]]
    assert len(ups) == len(kb._MODIFIER_KEYCODES)       # every modifier released first
    chars = [p for p in q.posted if p[2] is not None]
    assert "".join(c for _k, d, c, _f in chars if d) == "anyuse keys"
    assert all(k == 0 and f == 0 for k, _d, _c, f in chars)   # keycode 0, flags 0: the layout has no say
    assert kb.stuck_modifiers() == []


def test_press_key_carries_flags_on_the_event_and_leaves_nothing_held():
    q = FakeQuartz(flags=0)
    kb._quartz = lambda: q
    assert kb.press_key(3, q.kCGEventFlagMaskCommand)   # ⌘F, the shortcut that stuck in the guest
    down = [p for p in q.posted if p[0] == 3 and p[1]][0]
    up = [p for p in q.posted if p[0] == 3 and not p[1]][0]
    assert down[3] == q.kCGEventFlagMaskCommand and up[3] == 0
    assert any(p[0] == 55 and not p[1] for p in q.posted)   # left-⌘ key-up posted afterwards


if __name__ == "__main__":
    import inspect
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
