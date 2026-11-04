"""Shortcut read-back and lazy-menu races, with no live UI or key events."""
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-shortcut-review-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_ROOT, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import ax_actions as AX
from anyuse.watcher import step_capture as SC


@pytest.mark.parametrize("after", [None, {}, {"sheets": 0}])
def test_an_unreadable_after_counter_is_not_evidence_a_window_closed(monkeypatch, after):
    class Tree:
        sent = False

        def bundle_id(self):
            return "com.example.Editor"

        frontmost_bundle_id = bundle_id

        def actuation_refusal(self, _where):
            return None

        def window_counts(self):
            return after if self.sent else {"windows": 1, "sheets": 0}

        def press_key(self, _keycode, _flags):
            self.sent = True
            return True

    monkeypatch.setattr(AX, "SHORTCUT_EFFECT_S", 0)
    tree = Tree()
    result = AX.press_shortcut(tree, None, None, {
        "keys": "ctrl+n", "keycode": 45, "flags": AX.SHORTCUT_FLAGS["ctrl"],
        "bundle_id": "com.example.Editor", "expect": {"windows": "fewer"}})

    assert tree.sent
    assert result["success"] is False, result
    assert result["acted"] is True
    assert result["outcome_kind"] == "uncertain_completion"


def test_an_explicit_effect_with_no_before_counter_refuses_before_sending():
    class Tree:
        sent = False

        def bundle_id(self):
            return "com.apple.Chess"

        frontmost_bundle_id = bundle_id

        def actuation_refusal(self, _where):
            return None

        def window_counts(self):
            return None

        def press_key(self, _keycode, _flags):
            self.sent = True
            return True

    tree = Tree()
    result = AX.press_shortcut(tree, None, None, {
        "keys": "cmd+n", "keycode": 45, "flags": AX.SHORTCUT_FLAGS["cmd"],
        "bundle_id": "com.apple.Chess", "expect": {"windows": "more"}})

    assert result["success"] is False, result
    assert result["refused"] is True
    assert result["acted"] is False
    assert not tree.sent


def test_new_input_queued_during_one_menu_prevents_opening_the_next():
    pending = [False]

    class FakeAX:
        opened = None
        attributes = {
            "app": {"AXMenuBar": "bar"},
            "bar": {"AXChildren": ["apple", "file", "game"]},
            "apple": {"AXRole": "AXMenuBarItem", "AXChildren": []},
            "file": {"AXRole": "AXMenuBarItem", "AXChildren": ["file_menu"]},
            "game": {"AXRole": "AXMenuBarItem", "AXChildren": ["game_menu"]},
            "file_menu": {"AXRole": "AXMenu", "AXChildren": []},
            "game_menu": {"AXRole": "AXMenu"},
            "new": {"AXRole": "AXMenuItem", "AXMenuItemCmdChar": "n", "AXMenuItemCmdModifiers": 0},
        }

        def AXUIElementCreateApplication(self, _pid):
            return "app"

        def AXUIElementCopyAttributeValue(self, element, attribute, _none):
            if element == "game_menu" and attribute == "AXChildren":
                return 0, ["new"] if self.opened == "game" else []
            values = self.attributes.get(element, {})
            return (0, values[attribute]) if attribute in values else (-25212, None)

    ax = FakeAX()

    class Tree:
        def __init__(self):
            self.opened = []

        def actuation_refusal(self, _where):
            return None

        def with_menu_open(self, top, read):
            self.opened.append(top)
            ax.opened = top
            try:
                return True, read(top)
            finally:
                ax.opened = None
                pending[0] = True  # the user's next click arrived while the first menu was read

    tree = Tree()
    probe = object.__new__(SC.MacAXProbe)
    probe._A = ax
    probe._acc = SimpleNamespace(
        AccessibilityTree=lambda **_kwargs: tree,
        _attr=lambda element, attribute: ax.AXUIElementCopyAttributeValue(element, attribute, None)[1],
        _s=lambda value: str(value or ""))
    status, _item = probe.open_menu_for_shortcut(404, "n", 0, lambda item: item,
                                                ready=lambda: not pending[0])

    assert tree.opened == ["file"], tree.opened
    assert status != "found"


def test_simulated_counters_follow_recorded_states_and_keep_missing_evidence_unknown():
    from anyuse.execution.draft_proof import SimAXWorld

    record = {
        "state_graph": {"states": {
            "before": {"fingerprint": {"role_frequency": {"AXApplication": 1, "AXMenuBar": 1}}},
            "after": {"fingerprint": {"role_frequency": {"AXApplication": 1, "AXWindow": 1, "AXSheet": 1}}},
        }},
        "workflow_template": {"steps": [
            {"expected_before_state": "before"}, {"expected_before_state": "after"}]},
    }
    world = SimAXWorld(record)
    assert world.window_counts() == {"windows": 0, "sheets": 0}
    world.at_step(0)
    assert world.window_counts() == {"windows": 0, "sheets": 0}
    world.press_key(45, AX.SHORTCUT_FLAGS["cmd"])
    assert world.window_counts() == {"windows": 1, "sheets": 1}
    world.at_step(1)
    assert world.window_counts() == {"windows": 1, "sheets": 1}
    del record["state_graph"]["states"]["after"]["fingerprint"]["role_frequency"]
    assert world.window_counts() is None
    record["state_graph"]["states"]["after"]["fingerprint"]["role_frequency"] = {}
    assert world.window_counts() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
