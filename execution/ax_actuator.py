"""Checked AX actions: every actuation goes through the desktop executor's checked path,
``StateGraphExecutor.execute_checked_step`` (identity refusal, before/after evidence,
keystone gate). Used by the guest-VM desktop harness (``execution/guest/desktop_gen*.py``).

* :class:`ElementInfo` — one live element: AX reference, role, label, selector, pid.
* ``make_scoped_tree`` — an ``AccessibilityTree`` whose ``find_selector`` first returns the
  element the caller already found for exactly that selector (the executor still checks its
  live identity against the recorded selector, so a stale element is refused, not trusted),
  and whose before/after ``snapshot`` is scoped to the step's target element instead of the
  whole app.
* :class:`ExecutorActuator` — builds one ``PlannedStep`` (modality ``ax``) per action and
  runs it through ``execute_checked_step`` on a persistent loop.

Nothing here imports AppKit, Quartz or ApplicationServices at module import.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class ElementInfo:
    ref: Any                       # the live element (AX ref in the guest, anything in tests)
    role: str                      # raw role, e.g. AXTextArea
    label: str = ""                # human label: title / description / placeholder / title element
    value: str = ""
    selector: Dict[str, Any] = field(default_factory=dict)   # context.accessibility.Selector dict
    app: str = ""                  # bundle id
    window: str = ""
    enabled: bool = True
    pid: Optional[int] = None

    def describe(self) -> str:
        return f"{self.role} '{self.label or self.selector.get('identifier') or ''}'"


def _tree_base():
    from anyuse.context.accessibility import AccessibilityTree
    return AccessibilityTree


def make_scoped_tree(pid: int):
    """A ScopedAXTree bound to ``pid`` (subclass built lazily so this module
    imports without the AX framework)."""
    base = _tree_base()

    class ScopedAXTree(base):
        def __init__(self, pid_):
            super().__init__(pid_)
            self._primed: Dict[tuple, Any] = {}
            self.scope = None
            self.primed_hits = 0

        @staticmethod
        def _key(sel) -> tuple:
            d = sel.to_dict() if hasattr(sel, "to_dict") else dict(sel)
            return tuple(sorted((k, str(v)) for k, v in d.items()))

        def prime(self, selector: Dict[str, Any], element) -> None:
            self._primed[self._key(selector)] = element

        def find_selector(self, selector, document=None, mode=None):
            el = self._primed.get(self._key(selector))
            if el is not None and document is None:
                self.primed_hits += 1
                return el
            return super().find_selector(selector, document=document, mode=mode)

        def snapshot(self):
            if self.scope is None:
                return super().snapshot()
            from anyuse.context.accessibility import AXSnapshot
            try:
                return AXSnapshot(elements=[self._describe(self.scope)], app_name="")
            except Exception:  # noqa: BLE001
                return AXSnapshot(elements=[])

    return ScopedAXTree(pid)


class ExecutorActuator:
    """Runs one AX action as one checked executor step."""

    def __init__(self, view: Any = None, *, executor_factory: Optional[Callable[[Any], Any]] = None,
                 tree_factory: Optional[Callable[[int], Any]] = None,
                 clock: Callable[[], float] = time.perf_counter):
        self.view = view
        self._executor_factory = executor_factory or self._default_executor
        self._tree_factory = tree_factory or make_scoped_tree
        self._trees: Dict[int, Any] = {}
        self._executors: Dict[int, Any] = {}
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()      # callers may act from more than one thread
        self._clock = clock

    @staticmethod
    def _default_executor(tree):
        from anyuse.execution.executor import StateGraphExecutor
        return StateGraphExecutor(ax_tree=tree)

    def _for(self, pid: int):
        if pid not in self._trees:
            self._trees[pid] = self._tree_factory(pid)
            self._executors[pid] = self._executor_factory(self._trees[pid])
        return self._trees[pid], self._executors[pid]

    def close(self) -> None:
        self._loop.close()

    def run(self, info: ElementInfo, action: str, *, intent: str = "", text: Optional[str] = None,
            **command: Any) -> Dict[str, Any]:
        from anyuse.execution.plan_types import PlannedStep
        t0 = self._clock()
        selector = info.selector
        if not selector and self.view is not None and hasattr(self.view, "selector_for"):
            selector = self.view.selector_for(info)
        tree, executor = self._for(int(info.pid or 0))
        if hasattr(tree, "prime") and selector:
            tree.prime(selector, info.ref)
        if hasattr(tree, "scope"):
            tree.scope = info.ref
        cmd = {"role": info.role, "title": info.label, "selector": selector, "action": action}
        cmd.update({k: v for k, v in command.items() if v is not None})
        step = PlannedStep(intent=intent or action, source="ax_actuator", target_element=info.describe(),
                           modality="ax", command=cmd, input_data=text)
        with self._lock:
            exec_result, verification = self._loop.run_until_complete(
                executor.execute_checked_step(step))
        out = dict(exec_result)
        out["verification"] = verification
        out["execute_ms"] = round((self._clock() - t0) * 1000.0, 2)
        return out

    # convenience verbs
    def edit(self, info, text, *, rng=None, expect_value=None, intent="") -> Dict[str, Any]:
        action = "replace_range" if rng is not None else "insert_text"
        return self.run(info, action, intent=intent, text=text,
                        range=list(rng) if rng is not None else None, expect_value=expect_value)

    def press(self, info, *, intent="") -> Dict[str, Any]:
        return self.run(info, "press", intent=intent)

    def key(self, info, keycode: int, flags: int = 0, *, intent="") -> Dict[str, Any]:
        return self.run(info, "press_key", intent=intent, keycode=int(keycode), flags=int(flags))

    def open_app(self, name: str) -> Dict[str, Any]:
        from anyuse.execution.actions import drive_enabled
        if not drive_enabled():
            return {"success": False, "gated": True, "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)"}
        from anyuse.execution.foreground_interlock import refusal
        refused = refusal(f"ax_actuator.open_app({name})")
        if refused:
            return {"success": False, "refused": True, "error": refused}
        import AppKit
        ok = bool(AppKit.NSWorkspace.sharedWorkspace().launchApplication_(name))
        return {"success": ok, "method": "workspace", "app": name}
