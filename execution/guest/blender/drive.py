"""Real keystroke/mouse driving for a live Blender GUI session (guest only).

Blender's own UI is context-sensitive BY MOUSE POSITION, not click-focus -- a
keyboard shortcut (``G``, ``F2``, ``F3``, ...) is dispatched to whichever
area/editor the pointer currently sits over, never to a previously clicked
widget the way a normal AppKit app works. So every driven step here first
warps the pointer into the target area with ``CGWarpMouseCursorPosition``
before posting the shortcut -- the one Blender-specific fact this module
exists to encode. Everything else re-uses ``context/keyboard.py``'s
layout-independent ``type_unicode``/``press_key`` unchanged (Unicode typing
sidesteps the German-keyboard keycode-remap defect that module's docstring
already documents; the digits/`.`/`-` this module types are unaffected by
layout either way).

This never runs on the host: ``ANYUSE_DRIVE_ENABLED``-style gating is the
caller's job (the guest job script), same convention as
``context/keyboard.py`` itself.
"""
from __future__ import annotations

import time


def _quartz():
    import Quartz  # pyobjc-framework-Quartz; only present in the guest venv
    return Quartz


def move_mouse(x: float, y: float) -> None:
    """Reposition the pointer AND post a real ``mouseMoved`` HID event.

    ``CGWarpMouseCursorPosition`` alone repositions the hardware cursor but
    does not reliably deliver a motion event through the HID event stream a
    GHOST/Cocoa app like Blender reads its "current mouse position" from --
    without it, Blender's own idea of where the pointer is stays stale, so a
    click posted straight after a warp can hit-test against the OLD
    position and miss a small button (this round's exploration found this
    exact failure mode driving Blender's tiny Text-Editor "Run Script"
    icon: coordinates were confirmed correct by zooming into a screenshot,
    and the click still did nothing, until this explicit motion event was
    added). Posting the motion event first is what actually fixes it.
    """
    Q = _quartz()
    Q.CGWarpMouseCursorPosition((x, y))
    ev = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), Q.kCGMouseButtonLeft)
    Q.CGEventPost(Q.kCGHIDEventTap, ev)
    time.sleep(0.08)


def click(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    Q = _quartz()
    move_mouse(x, y)
    down_t = Q.kCGEventLeftMouseDown if button == "left" else Q.kCGEventRightMouseDown
    up_t = Q.kCGEventLeftMouseUp if button == "left" else Q.kCGEventRightMouseUp
    btn = Q.kCGMouseButtonLeft if button == "left" else Q.kCGMouseButtonRight
    for _ in range(clicks):
        for t in (down_t, up_t):
            ev = Q.CGEventCreateMouseEvent(None, t, (x, y), btn)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.03)
        time.sleep(0.05)


# Virtual keycodes: physical key position. Only used for keys with no
# printable character (Return/Tab/Escape/F-keys); every printable character
# this module ever sends goes through type_unicode instead, exactly per
# context/keyboard.py's layout-independence rule.
RETURN, TAB, ESCAPE, DELETE = 36, 48, 53, 51
F2, F3 = 120, 99


def press(keycode: int, flags: int = 0) -> bool:
    from anyuse.context import keyboard as kb
    return kb.press_key(keycode, flags)


def type_text(text: str) -> dict:
    from anyuse.context import keyboard as kb
    return kb.type_unicode(text)


def search_menu(query: str, viewport_xy, confirm: bool = True) -> None:
    """F3 (Blender's operator search popup), type ``query``, Enter on the
    top-ranked hit. Works from any editor area and does not depend on which
    menu/submenu a given operator would otherwise live under."""
    move_mouse(*viewport_xy)
    press(F3)
    time.sleep(0.35)
    type_text(query)
    time.sleep(0.25)
    if confirm:
        press(RETURN)
        time.sleep(0.35)


#: Blender's modal numeric-input redraw step listens for specific KEYCODE
#: events (its own mini keymap: digit/minus/period/Tab/Return), not generic
#: typed Unicode text the way a real text FIELD (filename, rename) does.
#: This round's exploration found that out empirically: typing "-1.0" via
#: ``type_unicode`` into an active ``transform.translate`` modal silently
#: dropped the minus sign (the operator's logged ``value`` came back
#: positive) -- so numbers fed into a modal transform go through raw
#: physical keycodes instead, never through type_text.
_DIGIT_KEYCODES = {"0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
                  "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
                  ".": 47, "-": 78}  # 78 = Keypad Minus; the main-row hyphen
                                     # (keycode 27) did not toggle the sign
                                     # in this round's modal-transform tests


def numeric_modal(values, viewport_xy) -> None:
    """Feed a sequence of numbers into an already-active modal numeric input
    (e.g. the translate operator :func:`search_menu` just launched),
    Tab-ing between successive fields (X, then Y, then Z), Enter to confirm.
    Digits/`.`/`-` go through raw keycodes (see ``_DIGIT_KEYCODES``), not
    ``type_text`` -- Blender's modal numeric entry needs the KEY, not a
    Unicode character."""
    move_mouse(*viewport_xy)
    for i, v in enumerate(values):
        if i:
            press(TAB)
            time.sleep(0.05)
        for ch in str(v):
            press(_DIGIT_KEYCODES[ch])
            time.sleep(0.05)
        time.sleep(0.1)
    press(RETURN)
    time.sleep(0.25)


def rename_active(new_name: str, viewport_xy) -> None:
    """F2 ("Rename Active Object" in the 3D viewport): the field opens with
    the old name pre-selected, so typing replaces it outright."""
    move_mouse(*viewport_xy)
    press(F2)
    time.sleep(0.35)
    type_text(new_name)
    press(RETURN)
    time.sleep(0.25)


def cmd_key(keycode: int, shift: bool = False, alt: bool = False) -> None:
    Q = _quartz()
    flags = Q.kCGEventFlagMaskCommand
    if shift:
        flags |= Q.kCGEventFlagMaskShift
    if alt:
        flags |= Q.kCGEventFlagMaskAlternate
    press(keycode, flags)
