"""Regression: `_execute_ax` needed a keystroke-
capable text-entry action and a gated raw-key-press action to port Finder's
rename/search flows (3/4) to the product path. `set_value` is the AXValue-
write rung ALONE, which Finder's rename/search fields
ignore; Finder's rename field commits on a literal Return, which nothing in
`_execute_ax`'s action map could reach before this fix.

A fake `ax_tree` is enough to prove the dispatch is correct — no
guest needed. It stands in for
`context.accessibility.AccessibilityTree`: `find()` resolves by role/title
(the no-recorded-identity path other tests already cover), `enter_text()`/
`press_key()` are recorded calls returning a scripted result, matching the
shape those methods actually return.

Run from the anyuse package dir:
    cd anyuse && python3 test_execute_ax_text_and_keys.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINKDIR = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
_PKGROOT = pathlib.Path(_LINKDIR.name)
(_PKGROOT / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, str(_PKGROOT))

from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.planner import PlannedStep  # noqa: E402


class _TextEntryResult:
    """Matches context.accessibility.TextEntryResult's observable surface:
    __bool__ is success, .rung names which rung served, .to_dict() exists."""

    def __init__(self, rung="", wrong_target=False):
        self.rung = rung
        self.wrong_target = wrong_target

    def __bool__(self):
        return bool(self.rung) and not self.wrong_target

    def to_dict(self):
        return {"rung": self.rung, "wrong_target": self.wrong_target}


class _FakeAxTree:
    """One AXButton/AXTextField named "Field", so find(role, title) can be
    unambiguous. Records every enter_text/press_key call."""

    def __init__(self, entry_result=None, key_result=True):
        self.rows = [{"role": "AXTextField", "title": "Field"}]
        self.entry_result = entry_result if entry_result is not None else _TextEntryResult("ax_value")
        self.key_result = key_result
        self.entry_calls = []
        self.key_calls = []

    def find(self, role=None, title=None):
        return [r for r in self.rows
                if (role is None or r["role"] == role)
                and (title is None or r["title"] == title)]

    def enter_text(self, element, text):
        self.entry_calls.append((element, text))
        return self.entry_result

    def press_key(self, keycode, flags=0):
        self.key_calls.append((keycode, flags))
        return self.key_result


def _step(command, input_data=None):
    return PlannedStep(intent="i", source="workflow", target_element="",
                       modality="ax", command=command, input_data=input_data)


def test_enter_text_climbs_the_ladder_and_reports_the_serving_rung():
    tree = _FakeAxTree(entry_result=_TextEntryResult("keystrokes"))
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "enter_text"},
        input_data="anyuse desktop validation",
    ))

    assert res["success"] is True, res
    assert res["rung"] == "keystrokes", res
    assert tree.entry_calls == [(tree.rows[0], "anyuse desktop validation")], tree.entry_calls
    print("OK  enter_text dispatches to AccessibilityTree.enter_text and reports the rung")


def test_enter_text_failure_is_reported_not_silently_true():
    tree = _FakeAxTree(entry_result=_TextEntryResult(""))  # no rung served
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "enter_text"},
        input_data="x",
    ))

    assert res["success"] is False, res
    assert res["rung"] == "", res
    print("OK  a text-entry ladder that serves nothing reports failure, not success")


def test_enter_text_wrong_target_is_a_failure():
    tree = _FakeAxTree(entry_result=_TextEntryResult("ax_value", wrong_target=True))
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "enter_text"},
        input_data="x",
    ))

    assert res["success"] is False, res
    print("OK  a wrong-target text entry (refused before writing) is a failure")


def test_press_key_reaches_the_gated_wrapper():
    tree = _FakeAxTree(key_result=True)
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "press_key", "keycode": 36},
    ))

    assert res["success"] is True, res
    assert tree.key_calls == [(36, 0)], tree.key_calls
    print("OK  press_key dispatches to AccessibilityTree.press_key with the given keycode")


def test_press_key_with_flags():
    tree = _FakeAxTree(key_result=True)
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "key", "keycode": 53, "flags": 1 << 20},
    ))

    assert res["success"] is True, res
    assert tree.key_calls == [(53, 1 << 20)], tree.key_calls
    print("OK  'key' is an accepted alias for 'press_key', flags pass through")


def test_press_key_without_a_keycode_fails_closed():
    tree = _FakeAxTree(key_result=True)  # would succeed if ever called
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "press_key"},
    ))

    assert res["success"] is False, res
    assert tree.key_calls == [], "must not press an unspecified key"
    print("OK  press_key with no keycode fails closed instead of pressing something")


def test_press_key_gated_off_never_reaches_the_keyboard():
    """AccessibilityTree.press_key is itself gated on ANYUSE_DRIVE_ENABLED; this
    fake stands in for that gate returning False, proving _execute_ax reports
    the refusal rather than assuming success."""
    class _GatedOffTree(_FakeAxTree):
        def press_key(self, keycode, flags=0):
            self.key_calls.append((keycode, flags))
            return False

    tree = _GatedOffTree()
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_step(
        {"role": "AXTextField", "title": "Field", "action": "press_key", "keycode": 36},
    ))

    assert res["success"] is False, res
    print("OK  a gated-off press_key reports failure, not success")


if __name__ == "__main__":
    test_enter_text_climbs_the_ladder_and_reports_the_serving_rung()
    test_enter_text_failure_is_reported_not_silently_true()
    test_enter_text_wrong_target_is_a_failure()
    test_press_key_reaches_the_gated_wrapper()
    test_press_key_with_flags()
    test_press_key_without_a_keycode_fails_closed()
    test_press_key_gated_off_never_reaches_the_keyboard()
    print("\nD15 ENTER_TEXT/PRESS_KEY DISPATCH TEST PASSED SUCCESSFULLY!")
