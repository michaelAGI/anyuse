"""Shortcuts on Apple's real keyboard layouts.

``testdata/keyboard/apple_layouts.json`` holds UCKeyTranslate's output for ten Apple layouts (read only on
macOS 26): per keycode what it types plain, with Shift, Command, Command + Shift, Control and Control +
Shift. ``context.keyboard.build_layout`` builds a ``Layout`` from those strings exactly as the live reader
does (``_build`` hands it UCKeyTranslate), so these tests see the tables a replay on that layout sees. The
fake tables in test_shortcut_layouts.py cannot show the real layouts' quirks: the Command map ignores
Shift on US punctuation (⌘⇧= types "="), keycodes no keyboard has (72) and the keypad type shifted
symbols, and a Control map is QWERTY on "Dvorak - QWERTY ⌘" and Latin on Russian and Greek.

Fake trees and a fake key poster: the real layout reader is never called, nothing is posted.

Pinned:
  - a Control chord goes by the control character its key types: ctrl+c is key 8 on Dvorak - QWERTY ⌘,
    Russian and Greek (not the key that types c without Control); the recorder stores that letter, and the
    replay on US sends ^C;
  - a shifted symbol goes to the main-block key that types it (⌘+ is ⌘⇧= on US, ⌘= is ⌘⇧0 on German), never
    to a keycode no keyboard has (66 70 72 77) and to the keypad only when nothing else types it (then
    with the numeric-pad flag);
  - a chord recorded with Shift keeps the character Shift made (⌘⇧ on AZERTY's & key is ⌘1; ⌘⇧= on US is
    ⌘+, key 30 without Shift on German); a letter keeps Shift as a modifier;
  - the agent's key names are matched without case, as pyautogui's were: "Cmd+W" is ⌘W, never ⌘⇧W;
  - "delete" / "del" stay forward delete (117), as pyautogui sent them; "backspace" is 51;
  - the PyAutoGUI fail-safe corner stops a chord sent without pyautogui;
  - while the layout cannot be read, a chord whose US key types q or w on another common layout is not
    sent (US ⌘A is AZERTY ⌘Q); the agent's own ⌘Q / ⌘W is sent as asked;
  - a recording older than the step's ``char`` replays on its own layout as recorded (⌥⌘∂ by key 2, ⇧⌘+
    on key 24), still refused where the key types q or w;
  - the credential vault selects the field by the key that types a (AZERTY key 12), never the US
    position (AZERTY ⌘Q), and types nothing when it cannot.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh tests/test_shortcut_layouts_real.py
"""
import json
import pathlib
import sys
import tempfile
import types

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.context import keyboard as KB  # noqa: E402
from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution.actions import ActionExecutor, _Backend, _parse_keys  # noqa: E402
from anyuse.watcher import step_capture as SC  # noqa: E402

CMD, SHIFT, CTRL, OPT = KB.FLAG_COMMAND, KB.FLAG_SHIFT, KB.FLAG_CONTROL, KB.FLAG_OPTION
BID = "com.example.Editor"
_RAW = json.loads((_HERE / "testdata" / "keyboard" / "apple_layouts.json").read_text())
#: the column of each UCKeyTranslate modifier state (``EventModifiers >> 8``) in the testdata rows
_COL = {m: i for i, m in enumerate(_RAW["mods"])}
PLAIN, S, C, CS, K, KS = 0, 2, 1, 3, 16, 18
#: the main block: the letter, digit and punctuation keys (not Return 36, Tab 48, Space 49)
MAIN = frozenset(range(0, 51)) - {36, 48, 49}


def raw(sid, code, mods):
    """What ``code`` types on ``sid`` in the modifier state ``mods`` (UCKeyTranslate's display string)."""
    row = _RAW["layouts"][sid]["keys"].get(str(int(code)))
    return row[_COL[mods]] if row else ""


def real(sid):
    d = _RAW["layouts"][sid]
    dead = {int(k): set(v) for k, v in d["dead"].items()}

    def read(code, mods):
        return raw(sid, code, mods), mods in dead.get(code, ())
    return KB.build_layout(read, sid)


LAYOUTS = {sid: real(sid) for sid in _RAW["layouts"]}
US, DE, FR = (LAYOUTS[f"com.apple.keylayout.{n}"] for n in ("US", "German", "French"))
DVQ, RU, GR = (LAYOUTS[f"com.apple.keylayout.{n}"] for n in ("DVORAK-QWERTYCMD", "Russian", "Greek"))
QWERTZ, SWISS, ES = (LAYOUTS[f"com.apple.keylayout.{n}"] for n in ("ABC-QWERTZ", "SwissGerman", "Spanish"))


class Tree:
    """The app in front, its layout, and the keys sent to it."""

    def __init__(self, layout):
        self.layout, self.sent = layout, []

    def bundle_id(self):
        return BID

    frontmost_bundle_id = bundle_id

    def actuation_refusal(self, _where):
        return None

    def press_key(self, code, flags=0):
        self.sent.append((code, flags))
        return True

    def keyboard_layout(self):
        return self.layout


def shortcut(keys, keycode, char=None, flags=CMD, layout=None):
    out = {"action": "press_shortcut", "keys": keys, "keycode": keycode, "flags": flags, "bundle_id": BID}
    if char:
        out["char"] = char
    if layout:
        out["layout"] = layout
    return out


def recorded(lay, keycode, flags, monkeypatch):
    """The step the recorder makes of a chord pressed on ``lay``: its name, keycode, flags, char, layout."""
    monkeypatch.setattr(KB, "layout_reader", lambda: lay)
    got = SC.layout_char(keycode, flags)
    assert got is not None, (lay, keycode)
    return shortcut(SC.shortcut_name(flags, got["char"], keycode), keycode, got["char"], flags, got["layout"])


@pytest.fixture
def posted(monkeypatch):
    sent = []
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    monkeypatch.setenv("ANYUSE_INTERLOCK_TEST_BYPASS", "1")
    monkeypatch.setattr(KB, "press_key", lambda code, flags=0: sent.append((code, flags)) or True)
    return sent


def _executor():
    be = _Backend()
    be._native_keys = True
    return ActionExecutor(backend=be)


# ── Control chords ───────────────────────────────────────────────────────────────────────────

def test_a_control_chord_goes_by_the_control_character_its_key_types():
    for sid, lay in LAYOUTS.items():
        for letter in "abcdefghijklmnopqrstuvwxyz":
            plan = KB.resolve_chord(f"ctrl+{letter}", lay)
            code = plan.get("keycode")
            assert plan.get("flags") == CTRL, (sid, letter, plan)
            assert raw(sid, code, K) == chr(ord(letter) - 0x60), (sid, letter, code)
    # the key that types c without Control types ^I with it on Dvorak - QWERTY ⌘ (key 34)
    assert raw(DVQ.source_id, 34, K) == "\t"
    for lay in (DVQ, RU, GR, US):
        assert KB.resolve_chord("ctrl+c", lay)["keycode"] == 8, lay


def test_a_recorded_control_chord_keeps_its_control_letter_on_another_layout(monkeypatch):
    for lay in (DVQ, RU, GR):
        step = recorded(lay, 8, CTRL, monkeypatch)              # Control + key 8: ^C on all three
        assert (step["char"], step["keys"]) == ("c", "ctrl+c"), (lay, step)
        for target in LAYOUTS.values():
            t = Tree(target)
            out = AX.press_shortcut(t, None, None, step)
            assert out["success"], (lay, target, out)
            (code, flags), = t.sent
            assert flags == CTRL and raw(target.source_id, code, K) == "\x03", (lay, target, t.sent)
    # the menu item is looked up by the control letter too (⌃C, not ⌃J on Dvorak - QWERTY ⌘)
    monkeypatch.setattr(KB, "layout_reader", lambda: DVQ)
    assert SC.layout_char(8, CTRL | SHIFT)["char"] == "c"


# ── shifted symbols, the keypad, keys no keyboard has ────────────────────────────────────────

def test_a_shifted_symbol_goes_to_the_main_block_key_that_types_it():
    cases = ((US, "cmd++", 24), (US, "cmd+plus", 24), (US, "cmd+*", 28), (DE, "cmd+=", 29), (QWERTZ, "cmd+=", 29),
             (SWISS, "cmd+/", 26), (SWISS, "cmd+=", 29), (ES, "cmd+/", 22))
    for lay, keys, code in cases:
        plan = KB.resolve_chord(keys, lay)
        assert (plan["keycode"], plan["flags"]) == (code, CMD | SHIFT), (lay, keys, plan)
    assert (KB.resolve_chord("cmd++", DE)["keycode"], KB.resolve_chord("cmd++", DE)["flags"]) == (30, CMD)


def test_every_character_the_main_block_types_is_sent_from_the_main_block():
    for sid, lay in LAYOUTS.items():
        chars = {c for code in MAIN for m in (PLAIN, S, C, CS) for c in [raw(sid, code, m)]
                 if KB._printable(c) and not c.isspace() and c == c.lower()}
        for ch in sorted(chars):
            plan = KB.resolve_chord(["cmd", ch], lay)
            code, shift = plan.get("keycode"), bool(plan.get("flags", 0) & SHIFT)
            assert code in MAIN, (sid, ch, plan)
            # the key sent types the character: under Command, or (where the Command map ignores Shift) plain
            typed = {raw(sid, code, CS if shift else C), raw(sid, code, S if shift else PLAIN)}
            assert ch in {t.lower() for t in typed}, (sid, ch, code, shift, typed)


#: keycodes Apple's layouts type characters on although no Apple keyboard has such a key (72 is kVK_VolumeUp)
PHANTOM = frozenset((66, 70, 72, 77))


def test_no_character_is_sent_by_a_keycode_no_keyboard_has():
    assert raw(US.source_id, 72, S) == "=" and raw(DE.source_id, 72, CS) == "="     # what the uchr data holds
    for sid, lay in LAYOUTS.items():
        assert not PHANTOM & (set(lay.keys) | set(lay.command_keys) | set(lay.control_keys)), sid
        for code in range(128):
            for m in (PLAIN, S, C, CS):
                ch = raw(sid, code, m)
                if KB._printable(ch):
                    for maps in (("command", "plain"), ("plain",), ("control", "plain", "command")):
                        hit = lay.find(ch, maps)
                        assert hit is None or hit[0] not in PHANTOM, (sid, ch, hit)
    # a table that has one anyway: the keypad, never the phantom key
    lay = KB.Layout("x", {72: ("", "="), 81: ("=", "=")})
    assert KB.resolve_chord("cmd+=", lay)["keycode"] == 81


def test_the_main_block_wins_across_the_tables():
    # a Command map that ignores Shift (⌘⇧= types "=") has + only on the keypad; the plain map's
    # main-block key (Shift + 24) still wins over it
    lay = KB.Layout("x", {24: ("=", "+"), 69: ("+", "+")}, command_keys={24: ("=", "="), 69: ("+", "+")})
    plan = KB.resolve_chord("cmd++", lay)
    assert (plan["keycode"], plan["flags"]) == (24, CMD | SHIFT), plan
    # between two main-block keys the chord's own table decides (Dvorak - QWERTY ⌘: c is key 8 under Command)
    assert DVQ.find("c", ("command", "plain"))[:2] == (8, False) and DVQ.find("c", ("plain",))[:2] == (34, False)


def test_a_keypad_key_is_sent_with_the_numeric_pad_flag():
    lay = KB.Layout("x", {69: ("+", "+"), 24: ("=", "=")})        # only the keypad types +
    assert KB.resolve_chord("cmd++", lay)["keycode"] == 69
    assert KB.event_flags(69, CMD) == CMD | KB.FLAG_NUMPAD and KB.event_flags(69, CMD, down=False) == KB.FLAG_NUMPAD
    assert KB.event_flags(24, CMD | SHIFT) == CMD | SHIFT and KB.event_flags(24, CMD, down=False) == 0


# ── Shift on a recorded chord ────────────────────────────────────────────────────────────────

def test_a_chord_recorded_with_shift_keeps_the_character_shift_made(monkeypatch):
    # ⌘⇧ on AZERTY's & key is ⌘1: on US that is key 18 without Shift (⌘⇧7 would be ⌘&)
    step = recorded(FR, 18, CMD | SHIFT, monkeypatch)
    assert step["char"] == "1"
    t = Tree(US)
    assert AX.press_shortcut(t, None, None, step)["success"] and t.sent == [(18, CMD)]
    # and back: US ⌘1 is ⌘⇧ + key 18 on AZERTY, which records as 1 again
    t = Tree(FR)
    assert AX.press_shortcut(t, None, None, recorded(US, 18, CMD, monkeypatch))["success"]
    assert t.sent == [(18, CMD | SHIFT)] and recorded(FR, 18, CMD | SHIFT, monkeypatch)["char"] == "1"
    # ⌘⇧= on US is ⌘+: key 30 without Shift on German; on US the recorded key itself
    step = recorded(US, 24, CMD | SHIFT, monkeypatch)
    assert step["char"] == "+"
    t = Tree(DE)
    out = AX.press_shortcut(t, None, None, step)
    assert out["success"] and t.sent == [(30, CMD)] and "without Shift" in out["note"], out
    t = Tree(US)
    out = AX.press_shortcut(t, None, None, step)
    assert t.sent == [(24, CMD | SHIFT)] and out["sent_as"]["how"] == "recorded key"
    # ⌘⇧7 on German is ⌘/: key 44 without Shift on US
    step = recorded(DE, 26, CMD | SHIFT, monkeypatch)
    t = Tree(US)
    assert step["char"] == "/" and AX.press_shortcut(t, None, None, step)["success"] and t.sent == [(44, CMD)]
    # a letter: Shift is a modifier of the shortcut (⌘⇧Z on US is key 16 with Shift on German)
    step = recorded(US, 6, CMD | SHIFT, monkeypatch)
    t = Tree(DE)
    assert step["char"] == "z" and AX.press_shortcut(t, None, None, step)["success"]
    assert t.sent == [(16, CMD | SHIFT)]


# ── the agent's key names ────────────────────────────────────────────────────────────────────

def test_the_agents_key_names_are_matched_without_case(monkeypatch, posted):
    monkeypatch.setattr(KB, "layout_reader", lambda: US)
    assert KB.parse_chord("Cmd+W") == (CMD, "w") and _parse_keys("Cmd+W") == ["command", "w"]
    for keys, want in (("Cmd+W", (13, CMD)), ("Cmd+Q", (12, CMD)), ("Command+Q", (12, CMD)),
                       ("opt+cmd+Q", (12, CMD | OPT)), ("CMD+L", (37, CMD)), ("Cmd+A", (0, CMD)),
                       ("Cmd+S", (1, CMD)), ("cmd+shift+Z", (6, CMD | SHIFT)), ("Cmd+?", (44, CMD | SHIFT))):
        del posted[:]
        out = _executor().execute("key_combination", {"keys": keys})
        assert out["success"] and posted == [want], (keys, out, posted)


def test_delete_is_forward_delete_as_pyautogui_sent_it(monkeypatch, posted):
    # pyautogui's macOS table: 'delete' and 'del' are 0x75 (kVK_ForwardDelete), 'backspace' 0x33
    monkeypatch.setattr(KB, "layout_reader", lambda: US)
    assert _parse_keys("cmd+del") == ["command", "delete"]
    for keys, want in (("delete", (117, 0)), ("del", (117, 0)), ("cmd+delete", (117, CMD)),
                       ("cmd+opt+shift+delete", (117, CMD | OPT | SHIFT)), ("backspace", (51, 0)),
                       ("cmd+backspace", (51, CMD)), ("forwarddelete", (117, 0))):
        del posted[:]
        assert _executor().execute("key_combination", {"keys": keys})["success"] and posted == [want], keys


# ── the fail-safe corner ─────────────────────────────────────────────────────────────────────

def test_the_failsafe_corner_stops_a_chord_sent_without_pyautogui(monkeypatch, posted):
    monkeypatch.setattr(KB, "layout_reader", lambda: US)

    class FailSafeException(Exception):
        pass

    pg = types.ModuleType("pyautogui")
    pg.FAILSAFE, pg.FAILSAFE_POINTS, pg.PAUSE, pg.pointer = True, [(0, 0)], 0.0, (0, 0)
    pg.position = lambda: pg.pointer

    def failSafeCheck():                                       # pyautogui.failSafeCheck
        if pg.FAILSAFE and tuple(pg.position()) in pg.FAILSAFE_POINTS:
            raise FailSafeException("PyAutoGUI fail-safe triggered from mouse moving to a corner of the screen")
    pg.failSafeCheck = failSafeCheck
    monkeypatch.setitem(sys.modules, "pyautogui", pg)
    out = _executor().execute("key_combination", {"keys": "cmd+n"})
    assert out["success"] is False and "fail-safe" in out["error"] and posted == [], out
    pg.pointer = (400, 300)
    assert _executor().execute("key_combination", {"keys": "cmd+n"})["success"] and posted == [(45, CMD)]


# ── an unreadable layout ─────────────────────────────────────────────────────────────────────

def _unreadable():
    raise OSError("no HIToolbox here")


def test_an_unreadable_layout_never_lets_another_layout_make_a_chord_quit_or_close(monkeypatch, posted):
    monkeypatch.setattr(KB, "layout_reader", _unreadable)
    for keys, why in (("cmd+a", "q on French AZERTY"), ("cmd+z", "w on French AZERTY"), ("cmd+x", "q on Dvorak"),
                      ("cmd+,", "w on Dvorak"), ("ctrl+a", "q on French AZERTY")):
        out = _executor().execute("key_combination", {"keys": keys})
        assert out["success"] is False and why in out["error"] and "could not be read" in out["error"], (keys, out)
    assert posted == []
    # the agent's own quit and close are sent as asked; other keys by the US positions
    for keys, want in (("cmd+q", (12, CMD)), ("cmd+w", (13, CMD)), ("cmd+n", (45, CMD)), ("cmd+shift+t", (17, CMD | SHIFT))):
        del posted[:]
        assert _executor().execute("key_combination", {"keys": keys})["success"] and posted == [want], keys
    # a replay: US ⌘A at key 0, AZERTY ⌘A at key 12 (US q) are not sent; ⌘N is, with a note
    for step in (shortcut("cmd+a", 0, "a", layout=US.source_id), shortcut("cmd+a", 12, "a", layout=FR.source_id),
                 shortcut("cmd+z", 6, "z", layout=US.source_id), shortcut("cmd+key0", 0)):
        t = Tree(None)
        out = AX.press_shortcut(t, None, None, step)
        assert out["refused"] and t.sent == [], (step, out)
    t = Tree(None)
    out = AX.press_shortcut(t, None, None, shortcut("cmd+n", 45, "n", layout=US.source_id))
    assert out["success"] and t.sent == [(45, CMD)] and "could not be read" in out["note"]


# ── recordings older than the step's char ────────────────────────────────────────────────────

def test_an_older_recording_replays_on_its_own_layout_as_recorded():
    for step, want in ((shortcut("opt+cmd+∂", 2, flags=CMD | OPT), (2, CMD | OPT)),
                       (shortcut("shift+cmd++", 24, flags=CMD | SHIFT), (24, CMD | SHIFT)),
                       (shortcut("shift+cmd+=", 24, flags=CMD | SHIFT), (24, CMD | SHIFT)),
                       (shortcut("shift+cmd+*", 28, flags=CMD | SHIFT), (28, CMD | SHIFT)),
                       (shortcut("cmd+n", 45), (45, CMD))):
        t = Tree(US)
        out = AX.press_shortcut(t, None, None, step)
        assert out["success"] and t.sent == [want], (step, out)
    out = AX.press_shortcut(Tree(US), None, None, shortcut("opt+cmd+∂", 2, flags=CMD | OPT))
    assert "older recording of an Option chord" in out["note"]
    # its key still decides the refusal: ⌥⌘∑ at key 13 is ⌥⌘W wherever key 13 types w
    for lay in (US, DE, RU, DVQ):
        t = Tree(lay)
        out = AX.press_shortcut(t, None, None, shortcut("opt+cmd+∑", 13, flags=CMD | OPT))
        assert out["refused"] and "types 'w'" in out["error"] and t.sent == [], (lay, out)


# ── the credential vault ─────────────────────────────────────────────────────────────────────

def _vault(monkeypatch, tmp_path):
    from anyuse.credentials import vault as V
    seen = {"hotkey": [], "write": [], "press": []}

    class PG:
        @staticmethod
        def hotkey(*names):
            seen["hotkey"].append(names)

        @staticmethod
        def press(name):
            seen["press"].append(name)

        @staticmethod
        def write(value, interval=0.0):
            seen["write"].append(len(value))
    monkeypatch.setattr(V, "pyautogui", PG)
    monkeypatch.setattr(V, "PYAUTOGUI_AVAILABLE", True)
    monkeypatch.setattr(V.sys, "platform", "darwin")
    monkeypatch.setattr(V.CredentialVault, "TYPING_SUPPRESSION_FLAG", tmp_path / ".typing_suppression")
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "on")             # the vault types only with desktop input on
    v = V.CredentialVault(use_keychain=False)
    monkeypatch.setattr(v, "get_credential", lambda alias: "secret123")
    return v, seen


def test_the_vault_selects_the_field_by_the_layouts_a_key(monkeypatch, posted, tmp_path):
    v, seen = _vault(monkeypatch, tmp_path)
    monkeypatch.setattr(KB, "layout_reader", lambda: FR)
    out = v.use_credential("t", clear_first=True)
    assert out["success"] and posted == [(12, CMD)], (out, posted)         # AZERTY's a; key 0 is q there
    assert seen["hotkey"] == [] and seen["press"] == ["backspace"] and seen["write"] == [9]
    # select-all not sent (the layout cannot be read, key 0 is AZERTY q): nothing is typed
    del posted[:]
    monkeypatch.setattr(KB, "layout_reader", _unreadable)
    v, seen = _vault(monkeypatch, tmp_path)
    out = v.use_credential("t", clear_first=True)
    assert out["success"] is False and "could not clear the field" in out["error"], out
    assert posted == [] and seen["write"] == [] and seen["press"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
