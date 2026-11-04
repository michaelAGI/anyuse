"""MacOSPlatform — the macOS implementation of the Platform seam.

A thin adapter over the already-working primitives (AccessibilityTree,
ScreenshotCapture, ActionExecutor, context.coordinates). Its job is to (a) present
one clean OS-agnostic surface and (b) translate ABSTRACT VERBS → native AX actions,
so the raw ``AXPress`` constant lives ONLY here, below the seam — not in the shared
map/executor layers. Windows gets a sibling WindowsPlatform with the same surface.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from anyuse.osplatform.base import Verb

logger = logging.getLogger(__name__)

# Abstract verb → native macOS AX action constant. The ONLY place AX action
# strings appear (below the seam). SET_VALUE is special-cased (uses AXValue set).
_VERB_TO_AX = {
    Verb.INVOKE: "AXPress",
    Verb.TOGGLE: "AXPress",
    Verb.EXPAND: "AXPress",
    Verb.CONFIRM: "AXConfirm",
    Verb.CANCEL: "AXCancel",
    Verb.INCREMENT: "AXIncrement",
    Verb.DECREMENT: "AXDecrement",
    Verb.SHOW_MENU: "AXShowMenu",
}


class MacOSPlatform:
    name = "macos"

    def __init__(self, pid: Optional[int] = None, action_executor=None, ax_tree=None):
        if ax_tree is not None:
            self._ax = ax_tree
        else:
            from anyuse.context.accessibility import AccessibilityTree
            self._ax = AccessibilityTree(pid)
        if action_executor is not None:
            self._actuator = action_executor
        else:
            from anyuse.execution.actions import ActionExecutor
            self._actuator = ActionExecutor()

    # ── coordinate space ──
    def screen_scale(self) -> float:
        from anyuse.context.coordinates import screen_scale
        return screen_scale()

    def px_to_pt(self, x: float, y: float) -> Tuple[float, float]:
        from anyuse.context.coordinates import px_to_pt
        return px_to_pt(x, y)

    # ── perception ──
    def capture(self) -> bytes:
        from anyuse.context.screenshot import ScreenshotCapture
        return ScreenshotCapture().capture()

    def find_at(self, x: float, y: float) -> Any:
        return self._ax.find_at(x, y)

    def selector_at(self, x: float, y: float) -> Any:
        return self._ax.selector_at(x, y)

    def find_selector(self, selector: Any) -> Any:
        return self._ax.find_selector(selector)

    def ocr(self, image_bytes: bytes, region: Optional[Tuple[int, int, int, int]] = None) -> List[dict]:
        # Apple Vision OCR (VNRecognizeTextRequest) lands in Phase A's perception
        # frame. Stubbed here so the seam is complete; returns [] (no boxes) rather
        # than claiming a capability that isn't wired yet.
        logger.debug("MacOSPlatform.ocr not yet implemented (Phase A); returning [].")
        return []

    # ── actuation ──
    def actuate(self, action: str, params: dict) -> dict:
        return self._actuator.execute(action, params)

    def perform_verb(self, element: Any, verb: str, value: Any = None) -> bool:
        """Translate an abstract verb to the native AX action and perform it."""
        if verb == Verb.SET_VALUE:
            return self._ax.set_value(element, value)
        ax_action = _VERB_TO_AX.get(verb)
        if ax_action is None:
            logger.debug("MacOSPlatform.perform_verb: unknown verb %r", verb)
            return False
        return self._ax.perform_action(element, ax_action)

    # ── native callable surface (Tier 0; deferred to Phase B) ──
    def native_actions(self, app: str) -> List[dict]:
        # Tier-0a (AppleScript sdef parsing) + Tier-0b (donate-your-own) land in
        # Phase B. App Intents are NOT runtime-enumerable for third-party apps.
        return []
