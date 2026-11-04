"""Coordinate-space conversion — screenshot PIXELS ↔ logical POINTS.

THE BUG THIS FIXES: on a Retina display the screen capture is in PHYSICAL pixels
(2× the logical size), but `pyautogui` click/move and the AX hit-test
(AXUIElementCopyElementAtPosition) work in LOGICAL POINTS. The computer-use vision
model sees the pixel screenshot and returns PIXEL coordinates. Feeding those pixel
coordinates straight to pyautogui / AX (as the code did) lands every click ~2× off
on Retina — silently producing wrong clicks and junk selectors.

THE CONVENTION (used everywhere downstream):
  * The vision model emits SCREENSHOT PIXELS.
  * Recorded clicks, stored coordinates, pyautogui, and AX all use LOGICAL POINTS.
  * Convert PIXELS → POINTS at the moment a vision-model coordinate is consumed
    (in VisualStepResolver / AgentOrchestrator), so everything downstream is points.

This is the macOS implementation; it will sit behind `Platform.screen_scale()` /
`Platform.px_to_pt()` in the cross-platform seam. (Windows needs per-monitor
mixed-DPI handling — a single scalar is not enough there — so this module is
deliberately the macOS-specific primitive, not the shared abstraction.)
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

_DEFAULT_SCALE = 1.0


def screen_scale() -> float:
    """The main display's backing scale factor (2.0 on Retina, 1.0 otherwise).

    Falls back to 1.0 (no-op conversion) when AppKit is unavailable, so non-mac /
    headless environments degrade safely instead of crashing.
    """
    try:
        import AppKit  # pyobjc-framework-Cocoa
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return _DEFAULT_SCALE
        scale = float(screen.backingScaleFactor())
        return scale if scale > 0 else _DEFAULT_SCALE
    except Exception as exc:  # pragma: no cover - platform/pyobjc dependent
        logger.debug("screen_scale() unavailable, defaulting to 1.0: %s", exc)
        return _DEFAULT_SCALE


def px_to_pt(x: float, y: float, scale: float | None = None) -> Tuple[float, float]:
    """Convert SCREENSHOT-PIXEL coords (what the vision model returns) → LOGICAL POINTS
    (what pyautogui / AX expect). Pass an explicit ``scale`` to avoid a repeated
    AppKit lookup in a tight loop."""
    s = screen_scale() if scale is None else scale
    if s <= 0:
        s = _DEFAULT_SCALE
    return (x / s, y / s)


def pt_to_px(x: float, y: float, scale: float | None = None) -> Tuple[float, float]:
    """Inverse of ``px_to_pt`` — LOGICAL POINTS → SCREENSHOT PIXELS (e.g. to crop a
    region out of a pixel screenshot given a point-space rect)."""
    s = screen_scale() if scale is None else scale
    if s <= 0:
        s = _DEFAULT_SCALE
    return (x * s, y * s)
