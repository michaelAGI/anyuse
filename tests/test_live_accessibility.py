"""A long-running replay must observe the app launched by its previous step."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

_PKGROOT = TemporaryDirectory(prefix="anyuse-live-ax-")
Path(_PKGROOT.name, "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, _PKGROOT.name)

import pytest

from anyuse.context.live_accessibility import FrontmostAccessibilityTree


class Tree:
    def __init__(self, pid):
        self.pid = pid

    def snapshot(self):
        return {"pid": self.pid}

    def bundle_id(self):
        return f"app.{self.pid}"


def test_snapshot_and_actions_follow_app_switch_without_rebuilding_executor():
    active = [10]
    created = []

    def make(pid):
        created.append(pid)
        return Tree(pid)

    tree = FrontmostAccessibilityTree(lambda: active[0], make)
    assert tree.snapshot() == {"pid": 10}
    assert tree.bundle_id() == "app.10"
    assert tree.snapshot() == {"pid": 10}
    active[0] = 20
    assert tree.snapshot() == {"pid": 20}
    assert tree.bundle_id() == "app.20"
    assert created == [10, 20]


def test_unreadable_app_discards_previous_action_target():
    active = [10]
    tree = FrontmostAccessibilityTree(lambda: active[0], Tree)
    tree.snapshot()
    active[0] = None
    assert tree.snapshot() is None
    with pytest.raises(AttributeError, match="no foreground"):
        tree.bundle_id()
    active[0] = 20
    assert tree.snapshot() == {"pid": 20}


def test_failed_observation_discards_previous_action_target():
    active = [10]

    def read():
        if active[0] == 20:
            raise RuntimeError("foreground read failed")
        return active[0]

    tree = FrontmostAccessibilityTree(read, Tree)
    tree.snapshot()
    active[0] = 20
    with pytest.raises(RuntimeError, match="foreground read failed"):
        tree.snapshot()
    with pytest.raises(AttributeError, match="no foreground"):
        tree.bundle_id()


def test_failed_tree_creation_can_be_retried_without_using_previous_app():
    active = [10]
    refused = [False]

    def make(pid):
        if refused[0]:
            raise RuntimeError("app exited")
        return Tree(pid)

    tree = FrontmostAccessibilityTree(lambda: active[0], make)
    tree.snapshot()
    active[0], refused[0] = 20, True
    with pytest.raises(RuntimeError, match="app exited"):
        tree.snapshot()
    with pytest.raises(AttributeError):
        tree.bundle_id()
    refused[0] = False
    assert tree.snapshot() == {"pid": 20}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
