"""The Platform protocol + the abstract action vocabulary.

ABSTRACT VERBS lift the action vocabulary BELOW the seam. The map layer stores an
OS-agnostic verb (``invoke`` / ``toggle`` / ``set_value`` …); the platform
translates it to the native action (macOS ``AXPress`` / Windows ``InvokePattern``).
Previously the raw ``AXPress`` constant was baked into the shared TransitionImpl
schema and the executor — which would have forced a rewrite to add Windows. This
is the indirection that makes "mac-first, Windows-additive" actually true.
"""
from __future__ import annotations

from typing import Any, List, Optional, Protocol, Tuple, runtime_checkable


class Verb:
    """OS-agnostic action verbs. The platform maps these → native actions."""
    INVOKE = "invoke"        # press a button, activate a link / menu item
    TOGGLE = "toggle"        # checkbox / switch / radio
    SET_VALUE = "set_value"  # type into a field / set a control's value
    INCREMENT = "increment"  # stepper up
    DECREMENT = "decrement"  # stepper down
    EXPAND = "expand"        # disclosure triangle / popup
    CONFIRM = "confirm"      # default / OK
    CANCEL = "cancel"        # escape / Cancel
    SHOW_MENU = "show_menu"  # open a context / popup menu


ABSTRACT_VERBS = frozenset({
    Verb.INVOKE, Verb.TOGGLE, Verb.SET_VALUE, Verb.INCREMENT, Verb.DECREMENT,
    Verb.EXPAND, Verb.CONFIRM, Verb.CANCEL, Verb.SHOW_MENU,
})


@runtime_checkable
class Platform(Protocol):
    """The per-OS perception + actuation surface. PROVISIONAL — extracted from the
    working macOS implementation; Windows (UIA) slots in by implementing this."""

    name: str

    # ── coordinate space ──
    def screen_scale(self) -> float: ...
    def px_to_pt(self, x: float, y: float) -> Tuple[float, float]: ...

    # ── perception ──
    def capture(self) -> bytes: ...                       # screenshot PNG bytes
    def find_at(self, x: float, y: float) -> Any: ...     # logical-point hit-test → element ref
    def selector_at(self, x: float, y: float) -> Any: ... # → Selector | None (durable locator)
    def find_selector(self, selector: Any) -> Any: ...    # Selector → live element ref
    def ocr(self, image_bytes: bytes, region: Optional[Tuple[int, int, int, int]] = None) -> List[dict]: ...

    # ── actuation ──
    def actuate(self, action: str, params: dict) -> dict: ...        # coordinate ops (click/type/scroll…)
    def perform_verb(self, element: Any, verb: str, value: Any = None) -> bool: ...  # abstract verb on an element

    # ── native callable surface (Tier 0; provisional, per-source) ──
    def native_actions(self, app: str) -> List[dict]: ...
