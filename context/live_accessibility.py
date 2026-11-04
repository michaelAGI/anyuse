"""Keep workflow observations bound to the app currently in front.

A replay can switch apps while its executor and platform adapter stay alive.
Refresh the underlying tree at each snapshot; action methods then use that
snapshot's tree, including the normal identity and permission checks.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class FrontmostAccessibilityTree:
    """An AX tree that follows app changes at observation boundaries.

    The injected readers make this usable without importing macOS frameworks
    in tests. No app is activated and no permission is requested here. If the
    foreground app cannot be read, discard the previous binding so later
    actions cannot accidentally use a stale app.
    """

    def __init__(self, read_pid: Callable[[], Optional[int]], make_tree: Callable[[int], Any]):
        self._read_pid = read_pid
        self._make_tree = make_tree
        self._bound_pid: Optional[int] = None
        self._tree: Any = None

    def snapshot(self):
        try:
            pid = self._read_pid()
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                self._bound_pid, self._tree = None, None
                return None
            if pid != self._bound_pid or self._tree is None:
                tree = self._make_tree(pid)
                self._tree, self._bound_pid = tree, pid
            return self._tree.snapshot()
        except Exception:
            self._bound_pid, self._tree = None, None
            raise

    def __getattr__(self, name: str):
        tree = self._tree
        if tree is None:
            raise AttributeError(f"no foreground Accessibility tree for {name}")
        return getattr(tree, name)
