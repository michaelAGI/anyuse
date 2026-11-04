"""Layout-independent keystroke typing for the AX rung's text-entry fallback.

Two facts measured in a macOS 26.6.2 VM with a German keyboard layout shape
this module:

* **Keycode typing is wrong on a non-US layout.** Posting virtual keycodes from
  a US table — the way ``pyautogui.typewrite`` does it — typed ``anzuse kezs``
  for ``anyuse keys``: Y and Z are swapped on a German layout, and every symbol
  moves. Typing through ``CGEventKeyboardSetUnicodeString`` instead attaches the
  character itself to the event, so the layout never gets a say.
* **A posted shortcut can leave a modifier stuck.** A probe posted ⌘F (a key
  event carrying the Command flag, with no matching modifier key-up). From then
  on the HID system state reported Command as held, every typed character was
  delivered as a ⌘-shortcut, and six consecutive text-entry attempts typed
  nothing — while the AX tree, the frontmost app and the focused element all
  looked perfectly right. :func:`stuck_modifiers` reads that state and
  :func:`release_modifiers` posts the key-ups that clear it; :func:`type_unicode`
  does both before typing and reports what it found, so a stuck modifier is
  recorded evidence rather than an invisible cause.

A shortcut cannot carry its character the way typed text does: an event with
``CGEventKeyboardSetUnicodeString`` fires no menu item, because AppKit derives the
characters from the keycode, the flags and the CURRENT layout. So a chord is sent
by the key that types its character on the layout in use (:class:`Layout`,
:func:`current_layout`, :func:`press_chord`): ⌘Z is keycode 6 on US, 16 on
QWERTZ, 13 on AZERTY and 44 on Dvorak. A Control chord goes by the control
character its key types (^C is key 8 on "Dvorak - QWERTY ⌘", Russian and Greek,
whatever the key types without Control), and a shifted symbol by the symbol
(⌘+ is ⌘⇧= on US and ⌘+ on German). The layout is read through HIToolbox
(Text Input Sources + ``UCKeyTranslate``, ctypes, read only; :func:`build_layout`)
behind the replaceable :data:`layout_reader`; keys that type no character (Return,
Tab, arrows, F-keys) have the same keycode on every layout (:data:`FIXED_KEYS`).

Nothing here runs without ``ANYUSE_DRIVE_ENABLED`` (checked by the caller in
``AccessibilityTree.type_text``); every function degrades to a harmless value
when Quartz cannot be imported, which is what the host-side desktop guard
enforces. Every function that posts an event checks the foreground interlock
itself (``execution/foreground_interlock.py``) before posting anything: these
are the lowest-level input primitives, and some callers reach them directly.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Virtual keycodes of the modifier keys, left and right, plus Fn.
_MODIFIER_KEYCODES = (55, 54, 56, 60, 58, 61, 59, 62, 63)


def _quartz():
    import Quartz  # pyobjc-framework-Quartz; refused by the host guard
    return Quartz


def _interlock(where: str):
    from anyuse.execution.foreground_interlock import refusal
    return refusal(where)


def _masks(Q) -> Dict[str, int]:
    return {"cmd": Q.kCGEventFlagMaskCommand, "shift": Q.kCGEventFlagMaskShift,
            "opt": Q.kCGEventFlagMaskAlternate, "ctrl": Q.kCGEventFlagMaskControl,
            "fn": Q.kCGEventFlagMaskSecondaryFn}


def stuck_modifiers() -> List[str]:
    """Modifier keys the HID system state currently reports as held.

    On a machine nobody is touching, anything here is stuck — left behind by a
    posted shortcut whose modifier key-up was never sent.
    """
    try:
        Q = _quartz()
        flags = int(Q.CGEventSourceFlagsState(Q.kCGEventSourceStateHIDSystemState))
        return [name for name, mask in _masks(Q).items() if flags & mask]
    except Exception:
        return []


def release_modifiers() -> bool:
    """Post a key-up for every modifier key so the HID state reads none held."""
    if _interlock("keyboard.release_modifiers"):
        return False
    try:
        Q = _quartz()
        src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        for keycode in _MODIFIER_KEYCODES:
            ev = Q.CGEventCreateKeyboardEvent(src, keycode, False)
            Q.CGEventSetFlags(ev, 0)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.005)
        return True
    except Exception:
        return False


def type_unicode(text: str, per_char: float = 0.012) -> dict:
    """Type ``text`` into whatever has keyboard focus, one Unicode character per
    key event, with an explicit empty modifier set on every event.

    Returns ``{"typed": bool, "chars": int, "stuck_modifiers": [...]}`` — the
    modifiers that were found held (and released) before typing, because that
    is the one condition under which this call silently types nothing.

    The interlock is asked again before every character (its probe is cached
    for 0.25 s), so a person arriving at the console mid-text stops the typing
    within that window instead of after the whole string.
    """
    refused = _interlock("keyboard.type_unicode")
    if refused:
        return {"typed": False, "chars": 0, "stuck_modifiers": [], "refused": True,
                "error": refused}
    stuck = stuck_modifiers()
    if stuck:
        release_modifiers()
        time.sleep(0.05)
    done = 0
    try:
        Q = _quartz()
        src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        for i, ch in enumerate(text):
            refused = _interlock("keyboard.type_unicode") if i else None
            if refused:
                return {"typed": False, "chars": i, "stuck_modifiers": stuck, "refused": True,
                        "error": refused}
            for down in (True, False):
                ev = Q.CGEventCreateKeyboardEvent(src, 0, down)
                Q.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                Q.CGEventSetFlags(ev, 0)
                Q.CGEventPost(Q.kCGHIDEventTap, ev)
            done = i + 1
            time.sleep(per_char)
        return {"typed": True, "chars": len(text), "stuck_modifiers": stuck}
    except Exception as exc:  # noqa: BLE001 - ``chars``: what was posted before the failure
        return {"typed": False, "chars": done, "stuck_modifiers": stuck,
                "error": f"{type(exc).__name__}: {exc}"}


def event_flags(keycode: int, flags: int = 0, down: bool = True) -> int:
    """The CGEventFlags :func:`press_key` sets on a key's down (``flags``) or up (none) event; a keypad
    key's events carry the numeric-pad bit on both, as a real keypad press does."""
    pad = FLAG_NUMPAD if int(keycode) in KEYPAD else 0
    return (int(flags) | pad) if down else pad


def press_key(keycode: int, flags: int = 0) -> bool:
    """One key down+up by virtual keycode (Return = 36, Escape = 53, Tab = 48).

    The modifier flags travel ON the event and a key-up with empty flags follows,
    so nothing is left held — the defect :func:`stuck_modifiers` exists for.
    """
    if _interlock(f"keyboard.press_key({int(keycode)})"):
        return False
    try:
        Q = _quartz()
        src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(src, int(keycode), down)
            Q.CGEventSetFlags(ev, event_flags(keycode, flags, down))
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.01)
        if flags:
            release_modifiers()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The keyboard layout (read only)
# ---------------------------------------------------------------------------

#: CGEventFlags modifier bits
FLAG_SHIFT, FLAG_CONTROL, FLAG_OPTION, FLAG_COMMAND, FLAG_FN = 0x20000, 0x40000, 0x80000, 0x100000, 0x800000
#: the CGEventFlags bit a keypad key's events carry (``kCGEventFlagMaskNumericPad``)
FLAG_NUMPAD = 0x200000
#: keypad keycodes: a character the main block types too is sent from the main block
#: (keypad events carry the numeric-pad flag: :func:`event_flags`)
KEYPAD: FrozenSet[int] = frozenset((65, 67, 69, 71, 75, 76, 78, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92))
#: the JIS keyboard's own keys (Yen, Underscore, keypad comma): ranked with the keypad, after the main block
JIS_KEYS: FrozenSet[int] = frozenset((93, 94, 95))
#: keycodes Apple's layouts give characters to although no Apple keyboard has such a key (72 is
#: kVK_VolumeUp; on US they type * + = / with Shift): never in a table, never sent for a character
NO_KEY: FrozenSet[int] = frozenset((66, 70, 72, 77))
#: the tables a chord's character is looked up in, by its modifiers: with Command the Command map
#: (AppKit's NSEvent.characters under Command), then the plain map; with Control alone the Control
#: map (the letter of the control character a key types: ^C), then the plain map, then the Command
#: map; otherwise only the plain map
_MAPS_COMMAND: Tuple[str, ...] = ("command", "plain")
_MAPS_CONTROL: Tuple[str, ...] = ("control", "plain", "command")
_MAPS_PLAIN: Tuple[str, ...] = ("plain",)

KeyTable = Mapping[int, Tuple[str, str]]
#: a ranked key in one table: ``(tier, dead, shifted, keycode)``; tier 0 is the main block, 1 the
#: keypad and the JIS keys
_Rank = Tuple[int, bool, bool, int]


def _best_keys(keys: KeyTable, dead: FrozenSet[Tuple[int, bool]]) -> Dict[str, _Rank]:
    """``{char: rank}`` of the best key in one table that types each character: the main block
    before the keypad, a key that types the character directly before a dead key, unshifted before
    shifted, then the lower keycode."""
    best: Dict[str, _Rank] = {}
    for code, pair in keys.items():
        if code in NO_KEY:
            continue
        tier = 1 if code in KEYPAD or code in JIS_KEYS else 0
        for shift, ch in ((False, pair[0]), (True, pair[1])):
            if not ch:
                continue
            rank = (tier, (code, shift) in dead, shift, code)
            if ch not in best or rank < best[ch]:
                best[ch] = rank
    return best


def _table(keys: Optional[KeyTable]) -> Dict[int, Tuple[str, str]]:
    return {int(k): (str(v[0] or ""), str(v[1] or "")) for k, v in (keys or {}).items()}


def _deads(pairs: Optional[Iterable[Tuple[int, bool]]]) -> FrozenSet[Tuple[int, bool]]:
    return frozenset((int(c), bool(s)) for c, s in (pairs or ()))


def _case_only(lo: str, hi: str) -> bool:
    """Shift changes nothing on this key but the letter's case (``z``/``Z``), or nothing at all."""
    return not hi or hi.lower() == lo.lower()


class Layout:
    """One keyboard layout's key tables.

    ``keys``: per keycode the character it types without and with Shift (``("z", "Z")``;
    ``""`` where it types nothing printable). ``command_keys``: the same with Command held,
    which differs from ``keys`` on Apple's non-Latin layouts (Latin under Command) and on
    "Dvorak - QWERTY ⌘" (QWERTY under Command); None means the same table. ``control_keys``: the
    letter of the control character each key types with Control held (^C is ``c``), which is
    QWERTY on "Dvorak - QWERTY ⌘" and Latin on Russian or Greek; None means the plain table.
    ``dead`` / ``command_dead`` / ``control_dead``: the ``(keycode, shifted)`` pairs that start an
    accent instead of typing. ``fallback``: the tables are the ASCII-capable layout an input method
    types through.
    """

    def __init__(self, source_id: str, keys: KeyTable, command_keys: Optional[KeyTable] = None,
                 dead: Iterable[Tuple[int, bool]] = (), command_dead: Optional[Iterable[Tuple[int, bool]]] = None,
                 fallback: bool = False, control_keys: Optional[KeyTable] = None,
                 control_dead: Optional[Iterable[Tuple[int, bool]]] = None):
        self.source_id = str(source_id or "")
        self.fallback = bool(fallback)
        self.keys = _table(keys)
        self.dead = _deads(dead)
        if command_keys is None:
            self.command_keys, self.command_dead = self.keys, self.dead
        else:
            self.command_keys = _table(command_keys)
            self.command_dead = self.dead if command_dead is None else _deads(command_dead)
        if control_keys is None:
            self.control_keys, self.control_dead = self.keys, self.dead
        else:
            self.control_keys = _table(control_keys)
            self.control_dead = _deads(control_dead)
        self._tables = {"plain": (self.keys, self.dead), "command": (self.command_keys, self.command_dead),
                        "control": (self.control_keys, self.control_dead)}
        plain = _best_keys(self.keys, self.dead)
        self._by_char = {
            "plain": plain,
            "command": plain if command_keys is None else _best_keys(self.command_keys, self.command_dead),
            "control": plain if control_keys is None else _best_keys(self.control_keys, self.control_dead)}

    def __repr__(self) -> str:
        return f"Layout({self.source_id!r})"

    def find(self, ch: str, maps: Sequence[str] = _MAPS_COMMAND) -> Optional[Tuple[int, bool, str]]:
        """``(keycode, needs_shift, table)`` of the key that types ``ch`` in one of ``maps``
        (``"command"``, ``"control"``, ``"plain"``), ranked across them: a main-block key before a
        keypad key whichever table it is in, then the earlier table, then a key that types the
        character directly before a dead key, unshifted before shifted, the lower keycode. None when
        no key types it."""
        best: Optional[Tuple[Tuple[int, int, bool, bool, int], str]] = None
        for i, name in enumerate(maps):
            r = self._by_char.get(name, {}).get(ch)
            if r is None:
                continue
            rank = (r[0], i, r[1], r[2], r[3])
            if best is None or rank < best[0]:
                best = (rank, name)
        if best is None:
            return None
        (_tier, _i, _dead, shift, code), name = best
        return code, shift, name

    def key_for(self, ch: str, maps: Sequence[str] = _MAPS_COMMAND) -> Optional[Tuple[int, bool]]:
        """``(keycode, needs_shift)`` of the key that types ``ch`` in ``maps`` (:meth:`find`); None
        when no key types it."""
        hit = self.find(ch, maps)
        return None if hit is None else (hit[0], hit[1])

    def char_for(self, keycode: int, command: bool = True, shift: bool = False, table: str = "") -> str:
        """The character ``keycode`` types (with Command held: the Command map; ``table`` names one),
        "" for none."""
        keys = self._tables[table or ("command" if command else "plain")][0]
        pair = keys.get(int(keycode))
        return pair[1 if shift else 0] if pair else ""

    def chord_char(self, keycode: int, flags: int) -> str:
        """The character a chord's key stands for with the chord's modifiers (Option ignored), lower
        case; "" when it types none. It comes from the first of :func:`chord_maps` that has a
        character for the key. With Shift held it is the shifted character when Shift makes a
        different one (⌘⇧= is ⌘+ on US, ⌘⇧ on AZERTY's & key is ⌘1), and the key's own character
        when Shift changes only a letter's case (⌘⇧Z stays z, with Shift as a modifier)."""
        code, shift = int(keycode), bool(int(flags) & FLAG_SHIFT)
        for name in chord_maps(flags):
            lo, hi = self._tables[name][0].get(code, ("", ""))
            if shift and hi and not _case_only(lo, hi):
                return hi.lower()
            if lo:
                return lo.lower()
        return ""

    def chars_of(self, keycode: int) -> FrozenSet[str]:
        """Every character ``keycode`` types: plain, under Command or under Control, with or without Shift."""
        code = int(keycode)
        return frozenset(c for t in (self.keys, self.command_keys, self.control_keys)
                         for c in t.get(code, ("", "")) if c)

    def is_dead(self, keycode: int, shift: bool = False, command: bool = True, table: str = "") -> bool:
        return (int(keycode), bool(shift)) in self._tables[table or ("command" if command else "plain")][1]


def chord_maps(flags: int) -> Tuple[str, ...]:
    """The tables a chord with these CGEventFlags looks its character up in."""
    flags = int(flags)
    if flags & FLAG_COMMAND:
        return _MAPS_COMMAND
    if flags & FLAG_CONTROL:
        return _MAPS_CONTROL
    return _MAPS_PLAIN


def is_letter(ch: str) -> bool:
    """A character with a case: Shift on its key is a modifier of a shortcut, never part of it."""
    return len(ch) == 1 and ch.lower() != ch.upper()


#: the US ANSI positions (``kVK_ANSI_*``): the table used when no layout can be read
_US_ANSI: Dict[int, Tuple[str, str]] = {
    0: ("a", "A"), 1: ("s", "S"), 2: ("d", "D"), 3: ("f", "F"), 4: ("h", "H"), 5: ("g", "G"),
    6: ("z", "Z"), 7: ("x", "X"), 8: ("c", "C"), 9: ("v", "V"), 11: ("b", "B"), 12: ("q", "Q"),
    13: ("w", "W"), 14: ("e", "E"), 15: ("r", "R"), 16: ("y", "Y"), 17: ("t", "T"), 18: ("1", "!"),
    19: ("2", "@"), 20: ("3", "#"), 21: ("4", "$"), 22: ("6", "^"), 23: ("5", "%"), 24: ("=", "+"),
    25: ("9", "("), 26: ("7", "&"), 27: ("-", "_"), 28: ("8", "*"), 29: ("0", ")"), 30: ("]", "}"),
    31: ("o", "O"), 32: ("u", "U"), 33: ("[", "{"), 34: ("i", "I"), 35: ("p", "P"), 37: ("l", "L"),
    38: ("j", "J"), 39: ("'", '"'), 40: ("k", "K"), 41: (";", ":"), 42: ("\\", "|"), 43: (",", "<"),
    44: ("/", "?"), 45: ("n", "N"), 46: ("m", "M"), 47: (".", ">"), 50: ("`", "~"),
}
US_LAYOUT = Layout("com.apple.keylayout.US", _US_ANSI)

# UCKeyTranslate constants (UnicodeUtilities.h / Events.h)
_UC_DOWN, _UC_DISPLAY = 0, 3                     # kUCKeyActionDown, kUCKeyActionDisplay
_UC_NO_DEAD_KEYS = 1                             # kUCKeyTranslateNoDeadKeysMask
_UC_CMD, _UC_SHIFT, _UC_CONTROL = 0x01, 0x02, 0x10   # (EventModifiers >> 8) & 0xFF
_UTF8 = 0x08000100                               # kCFStringEncodingUTF8

#: TIS and LMGetKbdType are not thread safe (TextInputSources.h): every call holds this lock
_TIS_LOCK = threading.Lock()
_tis: List[Any] = []                             # the loaded frameworks, once
#: the last tables built: ``((source id, keyboard type, fallback), Layout)``, rebuilt when the source changes
_cached: List[Any] = []


def _tis_api() -> Any:
    """CoreFoundation, HIToolbox and CoreServices through ctypes, loaded on first use (raises
    OSError where they do not exist or may not be loaded)."""
    if _tis:
        return _tis[0]
    import ctypes
    from ctypes import POINTER, c_bool, c_char_p, c_int32, c_long, c_uint8, c_uint16, c_uint32, c_ulong, c_void_p
    from types import SimpleNamespace
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    hit = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Frameworks/HIToolbox.framework/HIToolbox")
    cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFRelease.argtypes, cf.CFRelease.restype = [c_void_p], None
    cf.CFDataGetBytePtr.argtypes, cf.CFDataGetBytePtr.restype = [c_void_p], c_void_p
    cf.CFStringGetCString.argtypes, cf.CFStringGetCString.restype = [c_void_p, c_char_p, c_long, c_uint32], c_bool
    for fn in ("TISCopyCurrentKeyboardLayoutInputSource", "TISCopyCurrentASCIICapableKeyboardLayoutInputSource"):
        getattr(hit, fn).argtypes, getattr(hit, fn).restype = [], c_void_p      # Copy rule: CFRelease
    hit.TISGetInputSourceProperty.argtypes = [c_void_p, c_void_p]
    hit.TISGetInputSourceProperty.restype = c_void_p                            # Get rule: never released
    hit.LMGetKbdType.argtypes, hit.LMGetKbdType.restype = [], c_uint8
    cs.UCKeyTranslate.argtypes = [c_void_p, c_uint16, c_uint16, c_uint32, c_uint32, c_uint32,
                                  POINTER(c_uint32), c_ulong, POINTER(c_ulong), POINTER(c_uint16)]
    cs.UCKeyTranslate.restype = c_int32                                         # UniCharCount: unsigned long
    api = SimpleNamespace(
        ctypes=ctypes, cf=cf, hit=hit, cs=cs,
        # ``extern const CFStringRef`` globals: the symbol holds the CFStringRef itself
        layout_data=c_void_p.in_dll(hit, "kTISPropertyUnicodeKeyLayoutData").value,
        source_id=c_void_p.in_dll(hit, "kTISPropertyInputSourceID").value)
    _tis.append(api)
    return api


def _translate(api: Any, layout: int, kbd: int, code: int, mods: int, action: int = _UC_DISPLAY,
               opts: int = _UC_NO_DEAD_KEYS) -> Tuple[str, int]:
    C = api.ctypes
    dead, n, buf = C.c_uint32(0), C.c_ulong(0), (C.c_uint16 * 8)()
    if api.cs.UCKeyTranslate(layout, code, action, mods, kbd, opts, C.byref(dead), 8, C.byref(n), buf):
        return "", 0
    return C.string_at(buf, 2 * n.value).decode("utf-16-le", "replace"), dead.value


def _printable(s: str) -> bool:
    # one code point, no control character, none of the function-key private use area
    return len(s) == 1 and s >= " " and s != "\x7f" and not "" <= s <= ""


def _control_letter(s: str) -> str:
    """The letter of a control character (``"\\x03"`` is ^C: ``c``), "" for anything else."""
    return chr(ord(s) + 0x60) if len(s) == 1 and 0x01 <= ord(s) <= 0x1A else ""


def build_layout(read: Callable[[int, int], Tuple[str, bool]], source_id: str, fallback: bool = False) -> Layout:
    """A layout's tables from ``read(keycode, modifiers) -> (characters, starts_an_accent)``, the
    modifiers in UCKeyTranslate's form (``EventModifiers >> 8``: 0x01 Command, 0x02 Shift, 0x10 Control).

    * plain: what each key types without and with Shift;
    * Command: what it types with Command held. Where the Command map ignores Shift on a key that
      types the same there as without Command (US: ⌘⇧= types "="), the shifted character is the plain
      map's: that is the character the chord stands for (⌘⇧= is ⌘+, the key equivalent AppKit matches);
    * Control: the letter of the control character a character key types with Control held (^C on
      key 8 of "Dvorak - QWERTY ⌘", Russian and Greek alike). Return, Tab, Escape and Delete type control
      characters too and are left out; a key whose Control character is no letter is looked up in the
      plain map.

    Keycodes no keyboard has (:data:`NO_KEY`) are left out."""
    S, C, K = _UC_SHIFT, _UC_CMD, _UC_CONTROL
    plain: Dict[int, Tuple[str, str]] = {}
    command: Dict[int, Tuple[str, str]] = {}
    control: Dict[int, Tuple[str, str]] = {}
    dead, command_dead = set(), set()
    for code in range(128):
        if code in NO_KEY:
            continue
        got = {m: read(code, m) for m in (0, S, C, C | S, K)}

        def ch(m: int) -> str:
            return got[m][0] if _printable(got[m][0]) else ""

        lo, hi = ch(0), ch(S)
        dead.update((code, s) for s, c, m in ((False, lo, 0), (True, hi, S)) if c and got[m][1])
        if lo or hi:
            plain[code] = (lo, hi)
        c_lo, c_hi = ch(C), ch(C | S)
        if c_lo and c_lo == lo and _case_only(c_lo, c_hi) and hi:
            c_hi = hi
        command_dead.update((code, s) for s, c, m in ((False, c_lo, C), (True, c_hi, C | S)) if c and got[m][1])
        if c_lo or c_hi:
            command[code] = (c_lo, c_hi)
        k = _control_letter(got[K][0]) if lo else ""
        if k:
            control[code] = (k, k.upper())
    return Layout(source_id, plain, command, dead, command_dead, fallback=fallback, control_keys=control)


def _build(api: Any, data_ptr: int, kbd: int, source_id: str, fallback: bool) -> Layout:
    def read(code: int, mods: int) -> Tuple[str, bool]:
        ch, _ = _translate(api, data_ptr, kbd, code, mods)
        # typed, does this key start an accent
        return ch, bool(_printable(ch) and _translate(api, data_ptr, kbd, code, mods, _UC_DOWN, 0)[1])
    return build_layout(read, source_id, fallback)


def _read_system_layout() -> Optional[Layout]:
    """The current keyboard layout's tables (the ASCII-capable one when the current input source
    has no ``uchr`` data, as input methods do). Cached per input source id: the tables are rebuilt
    only when the source, the keyboard type or the fallback changes."""
    with _TIS_LOCK:
        api = _tis_api()
        src, fallback = api.hit.TISCopyCurrentKeyboardLayoutInputSource(), False
        data = api.hit.TISGetInputSourceProperty(src, api.layout_data) if src else None
        if not data:
            if src:
                api.cf.CFRelease(src)
            src, fallback = api.hit.TISCopyCurrentASCIICapableKeyboardLayoutInputSource(), True
            data = api.hit.TISGetInputSourceProperty(src, api.layout_data) if src else None
        if not data:
            if src:
                api.cf.CFRelease(src)
            return None
        try:
            buf = api.ctypes.create_string_buffer(256)
            sid_ref = api.hit.TISGetInputSourceProperty(src, api.source_id)
            sid = buf.value.decode() if sid_ref and api.cf.CFStringGetCString(sid_ref, buf, 256, _UTF8) else ""
            kbd = int(api.hit.LMGetKbdType())
            key = (sid, kbd, fallback)
            if sid and _cached and _cached[0][0] == key:
                return _cached[0][1]
            # the byte pointer is valid while ``src`` is retained: the tables are built before the release
            layout = _build(api, api.cf.CFDataGetBytePtr(data), kbd, sid, fallback)
            if sid:
                _cached[:] = [(key, layout)]
            return layout
        finally:
            api.cf.CFRelease(src)


#: reads the current layout (None: it cannot be read); tests replace it with fixed tables
layout_reader: Callable[[], Optional[Layout]] = _read_system_layout


def current_layout() -> Optional[Layout]:
    """The keyboard layout in use now, or None when it cannot be read (no HIToolbox, no layout
    data): callers then fall back to the recorded key position or the US table, and say so."""
    try:
        got = layout_reader()
    except Exception as exc:  # noqa: BLE001 - unreadable is None, never an exception
        logger.debug("keyboard layout unreadable: %s", type(exc).__name__)
        return None
    return got if isinstance(got, Layout) else None


def keycode_for_char(ch: str, command: bool = True, layout: Optional[Layout] = None) -> Optional[Tuple[int, bool]]:
    """``(keycode, needs_shift)`` that types ``ch`` on ``layout`` (default the current one), with
    Command held when ``command``; None when no key types it or the layout cannot be read."""
    lay = layout if layout is not None else current_layout()
    if lay is None:
        return None
    return lay.key_for(ch, _MAPS_COMMAND if command else _MAPS_PLAIN)


def char_for_keycode(keycode: int, command: bool = True, shift: bool = False,
                     layout: Optional[Layout] = None) -> Optional[str]:
    """The character ``keycode`` types on ``layout`` (default the current one); None when it types
    none or the layout cannot be read."""
    lay = layout if layout is not None else current_layout()
    if lay is None:
        return None
    return lay.char_for(keycode, command=command, shift=shift) or None


# ---------------------------------------------------------------------------
# Chords
# ---------------------------------------------------------------------------

#: CGEventFlags of a chord's modifier names
CHORD_MODIFIERS: Dict[str, int] = {
    "cmd": FLAG_COMMAND, "command": FLAG_COMMAND, "ctrl": FLAG_CONTROL, "control": FLAG_CONTROL,
    "opt": FLAG_OPTION, "option": FLAG_OPTION, "alt": FLAG_OPTION, "shift": FLAG_SHIFT, "fn": FLAG_FN,
}
#: keys that type no character, by the keycode they have on every layout; "delete" and "del" are forward
#: delete (117) as they were through pyautogui (and are in xdotool's names), "backspace" is the Mac's Delete key
FIXED_KEYS: Dict[str, int] = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51,
    "escape": 53, "esc": 53, "delete": 117, "forwarddelete": 117, "forward_delete": 117, "del": 117,
    "home": 115, "end": 119, "pageup": 116, "pgup": 116, "pagedown": 121, "pgdn": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "arrowleft": 123, "arrowright": 124, "arrowdown": 125, "arrowup": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64,
    "f18": 79, "f19": 80, "f20": 90,
}
#: character keys a chord may name in words
CHAR_NAMES: Dict[str, str] = {"plus": "+", "minus": "-", "comma": ",", "period": ".", "slash": "/",
                              "equal": "=", "equals": "="}
#: keys that type q or w on a common layout, ``{keycode: (character, layout)}``: while the layout cannot be
#: read, a Command or Control chord on one of them could be ⌘Q or ⌘W, and it is not sent unless that
#: character is the one asked for (:func:`unread_layout_risk`)
UNREAD_QW_KEYS: Dict[int, Tuple[str, str]] = {12: ("q", "US"), 0: ("q", "French AZERTY"), 7: ("q", "Dvorak"),
                                              13: ("w", "US"), 6: ("w", "French AZERTY"), 43: ("w", "Dvorak")}


def unread_layout_risk(keycode: int, flags: int, char: str = "") -> Optional[str]:
    """Why a chord is not sent while the keyboard layout cannot be read (None: it may be): a Command or
    Control chord on a key that types q or w on a common layout could quit the app or close a window there.
    ``char``: the character the chord asks for; a chord that asks for that very q or w is not refused here
    (the agent's own ⌘Q is sent as asked; a recorded one is refused by its character elsewhere)."""
    hit = UNREAD_QW_KEYS.get(int(keycode))
    if hit is None or not int(flags) & (FLAG_COMMAND | FLAG_CONTROL) or str(char or "").lower() == hit[0]:
        return None
    return (f"the keyboard layout could not be read, and key {int(keycode)} types {hit[0]} on {hit[1]}: the "
            "chord could quit or close, so nothing was sent")


def parse_chord(keys: Any) -> Tuple[int, str]:
    """``(modifier flags, key)`` of a chord: ``"cmd+z"``, ``"command,z"``, ``"cmd z"``,
    ``["command", "z"]``, ``"cmd++"``. Every part is matched without case, as pyautogui's names were:
    ``"Cmd+Q"`` is Command+Q, and Shift is sent only when the chord names it or the layout needs it for
    the character (AZERTY digits). ValueError unless the chord names exactly one key and only known
    modifiers besides it."""
    if isinstance(keys, (list, tuple)):
        parts = [str(k) for k in keys]
    else:
        s = str(keys or "").strip()
        # "+" separates; a "+" with nothing after it is the key itself ("cmd++")
        parts = re.split(r"\+(?=.)", s) if "+" in s else re.split(r"[,\s]+(?=.)", s)
    flags, key = 0, None
    for raw in parts:
        part = raw if len(raw) == 1 else raw.strip()
        if not part:
            continue
        low = part.lower()
        name = low if len(low) == len(part) else part      # "İ".lower() is two code points
        if name in CHORD_MODIFIERS:
            flags |= CHORD_MODIFIERS[name]
            continue
        if key is not None:
            raise ValueError(f"the chord {keys!r} names more than one key, or a modifier this module "
                             f"does not know ({key!r}, {name!r})")
        key = name
    if key is None:
        raise ValueError(f"the chord {keys!r} names no key")
    return flags, key


def resolve_chord(keys: Any, layout: Optional[Layout] = None) -> Dict[str, Any]:
    """What a chord is sent as: ``{"keycode", "flags", "key", "char", "how", "layout"}``, or
    ``{"refused": why}`` for a chord that names an unknown key or a character no key types.

    A key that types no character goes by its fixed keycode. A character goes by the key that
    types it on ``layout`` (default the current one; the US table, said in ``how``, when it cannot
    be read, except on a key that types q or w on another common layout: :func:`unread_layout_risk`),
    with Shift added when that layout needs it for the character."""
    try:
        flags, key = parse_chord(keys)
    except ValueError as exc:
        return {"refused": f"{exc}: nothing was sent", "keys": str(keys)}
    out: Dict[str, Any] = {"keys": str(keys), "key": key}
    code = FIXED_KEYS.get(key.lower()) if len(key) > 1 else None
    if code is not None:
        return dict(out, keycode=code, flags=flags, char=None, how="fixed key", layout=None)
    ch = CHAR_NAMES.get(key.lower(), key) if len(key) > 1 else key
    if len(ch) != 1 or not _printable(ch) or ch.isspace():
        return dict(out, refused=f"unknown key {key!r} in the chord {str(keys)!r}: nothing was sent")
    lay, how = layout if layout is not None else current_layout(), "layout"
    unread = lay is None
    if unread:
        lay, how = US_LAYOUT, "US key positions (the keyboard layout could not be read)"
    hit = lay.find(ch, chord_maps(flags))
    if hit is None:
        return dict(out, char=ch, layout=lay.source_id,
                    refused=(f"no key types {ch!r} on the keyboard layout {lay.source_id or 'in use'} "
                             "without Option: nothing was sent"))
    code, shift, table = hit
    risk = unread_layout_risk(code, flags, ch) if unread else None
    if risk:
        return dict(out, char=ch, layout=None, refused=risk)
    if shift:
        flags |= FLAG_SHIFT
    return dict(out, keycode=code, flags=flags, char=ch, how=how, layout=lay.source_id,
                dead=lay.is_dead(code, shift, table=table))


def press_chord(keys: Any, layout: Optional[Layout] = None,
                presser: Optional[Callable[[int, int], bool]] = None) -> Dict[str, Any]:
    """Send one chord (key down with the modifier flags, key up) by :func:`resolve_chord`, through
    :func:`press_key` and its interlock check. ``sent`` says whether it went out; nothing is sent
    for a refused chord."""
    plan = resolve_chord(keys, layout)
    if plan.get("refused"):
        return dict(plan, sent=False)
    send = presser if presser is not None else press_key
    if send(int(plan["keycode"]), int(plan["flags"])):
        return dict(plan, sent=True)
    return dict(plan, sent=False, error=f"the key press for {str(keys)!r} failed or was refused by the interlock")
