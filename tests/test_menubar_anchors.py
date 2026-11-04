"""Menu-bar descendants as required anchors — the option and its fixture.

Measured: 141-309 of every stock app's "actionable" elements are
menu-bar items, so a recorded fingerprint's required anchors are mostly the
menu bar. The consequence: a menu's items only exist in
the AX tree after that menu was opened once, so a fingerprint recorded right
after using the File menu declares `required_missing` on every later replay
of the same screen (15/15 runs, `gate_reasons == ['required_missing']`).
``ANYUSE_DRIFT_EXCLUDE_MENUBAR`` (opt-in, default off) leaves the menu bar out.

The fixture ``testdata/ax/menubar_anchors.json`` is a recorded corpus reduced
to what the gate consumes (role, title, in_menubar). Run:
    cd anyuse && python3 test_menubar_anchors.py
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

from anyuse.context import accessibility as acc  # noqa: E402
from anyuse.guides.state_graph import StateGraph, UIFingerprint, UIState, _role_frequency, compute_structural_hash  # noqa: E402
from anyuse.execution.drift_gate import evaluate_drift  # noqa: E402

FX = json.loads((_HERE / "testdata" / "ax" / "menubar_anchors.json").read_text())


def snapshot(rows):
    return acc.AXSnapshot(elements=[acc.AXElementDesc(role=r["role"], title=r["title"] or None,
                                                       in_menubar=r["in_menubar"]) for r in rows])


def graph_from(rows, exclude_menubar):
    os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = "1" if exclude_menubar else ""
    snap = snapshot(rows)
    live = [{"role": r["role"], "title": r["title"]} for r in rows]
    fp = UIFingerprint(required_elements=[{"role": e.role, "title": e.title or ""} for e in snap.anchor_elements],
                       element_count_range=(int(len(live) * 0.6), int(len(live) * 1.4) + 10),
                       structural_hash=compute_structural_hash(live), role_frequency=_role_frequency(live))
    g = StateGraph()
    g.add_state(UIState(state_id="s", app="x", fingerprint=fp))
    return g


def test_menu_bar_is_most_of_the_actionable_set():
    snap = snapshot(FX["TextEdit.default"])
    total = len(snap.actionable_elements)
    in_bar = sum(1 for e in snap.actionable_elements if e.in_menubar)
    assert total >= 250 and in_bar / total > 0.85, (total, in_bar)
    os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = ""
    assert len(snap.anchor_elements) == total                     # default: unchanged behaviour
    os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = "1"
    assert len(snap.anchor_elements) == total - in_bar and all(not e.in_menubar for e in snap.anchor_elements)
    os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = ""


def test_walk_marks_menu_bar_descendants():
    class El:
        def __init__(self, role, kids=()):
            self.role, self.kids = role, list(kids)

    class Fake:
        def AXUIElementCreateApplication(self, pid):
            return app
        def AXUIElementCopyAttributeValue(self, el, name, _o):
            if name == "AXChildren":
                return 0, el.kids
            if name == "AXRole":
                return 0, el.role
            return -25205, None
    app = El("AXApplication", [El("AXMenuBar", [El("AXMenuBarItem", [El("AXMenu", [El("AXMenuItem")])])]),
                               El("AXWindow", [El("AXButton")])])
    acc._ax = lambda: Fake()
    acc.AX_AVAILABLE = True
    descs = []
    acc.AccessibilityTree(pid=1)._walk(app, 0, descs)
    flags = {(d.role, d.in_menubar) for d in descs}
    assert ("AXMenuItem", True) in flags and ("AXMenuBarItem", True) in flags
    assert ("AXButton", False) in flags and ("AXWindow", False) in flags and ("AXMenuBar", False) in flags


def test_option_removes_the_menu_driven_false_fire_and_keeps_real_drift():
    live_same = [{"role": r["role"], "title": r["title"]} for r in FX["TextEdit.narrow"]]
    live_other = [{"role": r["role"], "title": r["title"]} for r in FX["Calculator.default"]]
    # a recording made right after a menu was opened: its items are in the anchors
    recorded = FX["TextEdit.content"]
    # at tolerance 0.0 (the former default) the menu-driven false fire, and the option's cure
    prev = os.environ.get("ANYUSE_DRIFT_MISSING_TOLERANCE")
    os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = "0.0"
    try:
        for exclude, expect_fire in ((False, True), (True, False)):
            g = graph_from(recorded, exclude)
            same = evaluate_drift(g, "s", live_same)
            other = evaluate_drift(g, "s", live_other)
            assert same.drifted is expect_fire, (exclude, same.reason, same.signals.get("required_missing"))
            assert other.drifted and other.reason in ("required_missing", "count_out_of_range",
                                                      "low_structural_similarity")
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_DRIFT_MISSING_TOLERANCE", None)
        else:
            os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = prev
    # the default 0.05 tolerates the few menu items that were
    # only loaded at record time, menu bar in, and still sees another app's screen
    g = graph_from(recorded, False)
    same, other = evaluate_drift(g, "s", live_same), evaluate_drift(g, "s", live_other)
    assert not same.drifted, (same.reason, same.signals.get("required_missing"))
    assert other.drifted
    os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = ""


if __name__ == "__main__":
    test_menu_bar_is_most_of_the_actionable_set()
    test_walk_marks_menu_bar_descendants()
    test_option_removes_the_menu_driven_false_fire_and_keeps_real_drift()
    print("ok")
