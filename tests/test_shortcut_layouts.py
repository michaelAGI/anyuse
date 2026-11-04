"""A shortcut sends the same CHARACTER on every keyboard layout.

AppKit derives a key event's characters from its keycode, its flags and the layout in use, so a
keycode means a different shortcut on another layout: keycode 6 is ⌘Z on US and ⌘Y on QWERTZ,
keycode 12 is ⌘Q on US and ⌘A on AZERTY. A recorded shortcut and the computer-use agent's
``key_combination`` are sent by the key that types their character on the layout in use
(``context.keyboard``); quit and close stay refused by that character on every layout.

Offline: fixed layout tables (US, German, French AZERTY, Dvorak, Dvorak - QWERTY ⌘, Russian), fake
AX trees and a fake key poster; the real layout reader is never called, nothing is posted.

Pinned:
  - a ⌘Z recorded on German (key 16) replays as the key that types z on each layout (US 6,
    AZERTY 13, Dvorak 44; the Command map on Dvorak - QWERTY ⌘ and Russian: 6), also from an
    older recording that has only the name; the compiled step carries the recorded character
    and layout;
  - ``key_combination('cmd+z')`` posts the layout's z key with the Command flag; key names are
    matched without case ("cmd+Z" is ⌘Z); Shift is added only when named or where the layout needs
    it (AZERTY digits); keys that type no character keep their keycode;
  - ⌘Q and ⌘W are refused on every layout: by the recorded character when the recorded keycode is
    harmless elsewhere, and by what the key sent types when a recording has no character; an
    AZERTY ⌘A recorded at the US q position is sent as ⌘A, not refused;
  - an unreadable layout sends the recorded keycode and says so (the US positions decide the
    refusal, and a key that types q or w on another common layout is not sent); an unknown key, or
    a character no key types, is refused and nothing is sent;
  - the recording side reads the chord's character from the layout (Option ignored; Control alone:
    the letter of the control character the key types).

The same on Apple's real layout tables: test_shortcut_layouts_real.py.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh tests/test_shortcut_layouts.py
"""
import json
import os
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.context import keyboard as KB  # noqa: E402
from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution.actions import ActionExecutor, _Backend  # noqa: E402
from anyuse.execution.plan_types import PlannedStep  # noqa: E402
from anyuse.watcher import entity_vault as EV  # noqa: E402
from anyuse.watcher import step_capture as SC  # noqa: E402

CMD, SHIFT, CTRL, OPT = KB.FLAG_COMMAND, KB.FLAG_SHIFT, KB.FLAG_CONTROL, KB.FLAG_OPTION
BID = "com.example.Editor"
CHESS = _HERE / "testdata" / "drafts" / "chess_cmd_n_draft.json"

# ── layout tables ────────────────────────────────────────────────────────────────────────────
#: the keycodes of the letter and digit rows, in US order (q w e r t y u i o p, a s d f g h j k l ;,
#: z x c v b n m , . /, 1 2 3 4 5 6 7 8 9 0)
ROWS = ((12, 13, 14, 15, 17, 16, 32, 34, 31, 35), (0, 1, 2, 3, 5, 4, 38, 40, 37, 41),
        (6, 7, 8, 9, 11, 45, 46, 43, 47, 44), (18, 19, 20, 21, 23, 22, 26, 28, 25, 29))
_SHIFTED = {";": ":", ",": "<", ".": ">", "/": "?", "'": '"', "-": "_", "=": "+", ":": "/"}


def table(rows, digits_shifted="!@#$%^&*()", **extra):
    """``{keycode: (plain, shifted)}`` from the layout's three letter rows and its digit row (``extra``:
    ``k<keycode>=(plain, shifted)``)."""
    out = {int(k[1:]): v for k, v in extra.items()}
    for codes, chars in zip(ROWS[:3], rows[:3]):
        for code, ch in zip(codes, chars):
            out[code] = (ch, ch.upper() if ch.isalpha() else _SHIFTED.get(ch, ""))
    for code, lo, hi in zip(ROWS[3], rows[3], digits_shifted):
        out[code] = (lo, hi)
    return out


US_ROWS = ("qwertyuiop", "asdfghjkl;", "zxcvbnm,./", "1234567890")
US = KB.Layout("com.apple.keylayout.US", table(US_ROWS))
DE = KB.Layout("com.apple.keylayout.German",
               table(("qwertzuiop", "asdfghjklö", "yxcvbnm,.-", "1234567890"), '!"§$%&/()=', k30=("+", "*"),
                     k24=("´", "`"), k10=("^", "°")),
               dead={(10, False), (24, False), (24, True)})
FR = KB.Layout("com.apple.keylayout.French",
               table(("azertyuiop", "qsdfghjklm", "wxcvbn,;:=", "&é\"'(§è!çà"), "1234567890"))
DVORAK = KB.Layout("com.apple.keylayout.Dvorak", table(("',.pyfgcrl", "aoeuidhtns", ";qjkxbmwvz", "1234567890")))
#: the letter of the control character each key types with Control: QWERTY on both (^C is key 8)
US_CONTROL = {k: v for k, v in US.keys.items() if v[0].isalpha()}
DVORAK_QWERTY_CMD = KB.Layout("com.apple.keylayout.DVORAK-QWERTYCMD", DVORAK.keys, command_keys=US.keys,
                              control_keys=US_CONTROL)
RU = KB.Layout("com.apple.keylayout.Russian",
               table(("йцукенгшщз", "фывапролдж", "ячсмитьбю.", "1234567890")), command_keys=US.keys,
               control_keys=US_CONTROL)
LAYOUTS = (US, DE, FR, DVORAK, DVORAK_QWERTY_CMD, RU)
#: the key that types z with Command held, per layout
Z_KEY = {US: 6, DE: 16, FR: 13, DVORAK: 44, DVORAK_QWERTY_CMD: 6, RU: 6}


def test_the_tables_rank_the_key_that_types_a_character():
    assert (US.key_for("z"), DE.key_for("z"), FR.key_for("z"), DVORAK.key_for("z")) == (
        (6, False), (16, False), (13, False), (44, False))
    assert FR.key_for("1") == (18, True) and FR.key_for("&") == (18, False)    # AZERTY digits need Shift
    assert DE.key_for("Z") == (16, True) and DE.key_for("@") is None           # @ needs Option on German
    # the Command map first under Command; only the plain map without it
    assert RU.key_for("z") == (6, False) and RU.key_for("z", ("plain",)) is None
    assert DVORAK_QWERTY_CMD.key_for("c") == (8, False) and DVORAK_QWERTY_CMD.key_for("c", ("plain",)) == (34, False)
    assert DE.char_for(6) == "y" and DE.char_for(16, shift=True) == "Z" and RU.char_for(6, command=False) == "я"
    assert FR.chars_of(0) == {"q", "Q"} and DVORAK_QWERTY_CMD.chars_of(12) == {"'", '"', "q", "Q"}
    # a key that types the character directly before a dead key, the main block before the keypad
    lay = KB.Layout("x", {24: ("`", "~"), 50: ("`", "~"), 18: ("&", "1"), 83: ("1", "")}, dead={(24, False)})
    assert lay.key_for("`") == (50, False) and lay.key_for("1") == (18, True)


def test_an_unreadable_layout_is_none_and_chords_fall_back_to_the_us_positions(monkeypatch):
    def broken():
        raise OSError("no HIToolbox here")
    monkeypatch.setattr(KB, "layout_reader", broken)
    assert KB.current_layout() is None
    assert KB.keycode_for_char("z") is None and KB.char_for_keycode(6) is None
    plan = KB.resolve_chord("cmd+n")
    assert (plan["keycode"], plan["flags"]) == (45, CMD) and "could not be read" in plan["how"]
    # the US z key is AZERTY's w: a guess there could close a window, so it is not sent
    plan = KB.resolve_chord("cmd+z")
    assert "keycode" not in plan and "w on French AZERTY" in plan["refused"]
    monkeypatch.setattr(KB, "layout_reader", lambda: DE)
    assert KB.keycode_for_char("z") == (16, False) and KB.char_for_keycode(16) == "z"


# ── the recorded shortcut (press_shortcut) ───────────────────────────────────────────────────

class Tree:
    """The app in front, its layout (``keyboard_layout``), and the keys sent to it."""

    def __init__(self, layout=None):
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


class NoLayoutTree(Tree):
    keyboard_layout = None


def shortcut(keys, keycode, char=None, layout=None, flags=CMD):
    out = {"action": "press_shortcut", "keys": keys, "keycode": keycode, "flags": flags, "bundle_id": BID}
    if char:
        out["char"] = char
    if layout:
        out["layout"] = layout
    return out


def test_a_cmd_z_recorded_on_german_replays_by_the_key_that_types_z():
    recorded = shortcut("cmd+z", 16, char="z", layout=DE.source_id)
    for lay in LAYOUTS:
        t = Tree(lay)
        out = AX.press_shortcut(t, None, None, recorded)
        assert out["success"] and t.sent == [(Z_KEY[lay], CMD)], (lay, out)
        assert out["sent_as"]["layout"] == lay.source_id
        if lay is DE:
            assert out["sent_as"]["how"] == "recorded key" and "note" not in out
        else:
            assert out["sent_as"]["how"] == "layout"
            assert out["note"] == (f"recorded as key 16 on {DE.source_id}; sent as key {Z_KEY[lay]}, which "
                                   f"types 'z' on {lay.source_id}")
    # an older recording: only the name holds the character
    t = Tree(US)
    assert AX.press_shortcut(t, None, None, shortcut("cmd+z", 16))["success"] and t.sent == [(6, CMD)]
    # recorded on US, replayed on German: key 6 is y there
    t = Tree(DE)
    assert AX.press_shortcut(t, None, None, shortcut("shift+cmd+z", 6, char="z", flags=CMD | SHIFT))["success"]
    assert t.sent == [(16, CMD | SHIFT)]


def test_the_command_map_decides_what_the_recorded_key_types_now():
    # ⌘C recorded on Dvorak (key 34); on Dvorak - QWERTY ⌘ key 34 types i under Command, c is key 8
    t = Tree(DVORAK_QWERTY_CMD)
    assert AX.press_shortcut(t, None, None, shortcut("cmd+c", 34, char="c", layout=DVORAK.source_id))["success"]
    assert t.sent == [(8, CMD)]
    # a control chord goes by the letter of the control character its key types (Latin on Russian)
    t = Tree(RU)
    assert AX.press_shortcut(t, None, None, shortcut("ctrl+n", 37, char="n", layout=DVORAK.source_id,
                                                     flags=CTRL))["success"]
    assert t.sent == [(45, CTRL)]


def test_shift_is_added_only_where_the_layout_needs_it():
    t = Tree(FR)
    assert AX.press_shortcut(t, None, None, shortcut("cmd+1", 18, char="1", layout=US.source_id))["success"]
    assert t.sent == [(18, CMD | SHIFT)]                       # AZERTY types 1 with Shift
    t = Tree(US)
    assert AX.press_shortcut(t, None, None, shortcut("cmd+1", 18, char="1", layout=FR.source_id))["success"]
    assert t.sent == [(18, CMD)]
    # a character only a dead key types there: sent, and the note says so
    t = Tree(DE)
    out = AX.press_shortcut(t, None, None, shortcut("cmd+`", 50, char="`", layout=US.source_id))
    assert out["success"] and t.sent == [(24, CMD | SHIFT)] and "dead key" in out["note"]


def test_quit_and_close_are_refused_on_every_layout():
    cases = [
        shortcut("cmd+q", 12, char="q", layout=US.source_id),          # US q: AZERTY's a
        shortcut("cmd+q", 0, char="q", layout=FR.source_id),           # AZERTY q: US's a
        shortcut("cmd+q", 7, char="q", layout=DVORAK.source_id),       # Dvorak q: US's x
        shortcut("cmd+w", 6, char="w", layout=FR.source_id),           # AZERTY w: US's z
        shortcut("cmd+w", 43, char="w", layout=DVORAK.source_id),      # Dvorak w: US's comma
        shortcut("opt+cmd+w", 13, char="w", flags=CMD | OPT),          # the event's own string is "∑"
        shortcut("shift+cmd+q", 12, char="q", flags=CMD | SHIFT),
        shortcut("ctrl+q", 12, char="q", flags=CTRL),
    ]
    for cmd in cases:
        for lay in LAYOUTS + (None,):
            t = Tree(lay)
            out = AX.press_shortcut(t, None, None, cmd)
            assert out["refused"] and out["reason_code"] == "identity" and t.sent == [], (cmd, lay, out)
            assert "never replayed as a key" in out["error"]
    # an older recording whose name holds the Option glyph goes by its recorded key, so the key decides:
    # refused wherever key 13 types w; elsewhere it is not ⌥⌘W and is sent by its position, with a note
    for lay in LAYOUTS:
        t = Tree(lay)
        out = AX.press_shortcut(t, None, None, shortcut("opt+cmd+∑", 13, flags=CMD | OPT))
        if "w" in lay.chars_of(13):
            assert out["refused"] and "types 'w'" in out["error"] and t.sent == [], (lay, out)
        else:
            assert out["success"] and t.sent == [(13, CMD | OPT)] and "Option chord" in out["note"], (lay, out)


def test_an_azerty_cmd_a_at_the_us_q_position_is_sent_as_cmd_a():
    cmd = shortcut("cmd+a", 12, char="a", layout=FR.source_id)
    assert AX.shortcut_refusal(cmd["keys"], 12, CMD, char="a") is None
    for lay, code in ((US, 0), (DE, 0), (FR, 12), (DVORAK, 0)):
        t = Tree(lay)
        out = AX.press_shortcut(t, None, None, cmd)
        assert out["success"] and t.sent == [(code, CMD)], (lay, out)
    # but on a layout where key 12 types q under Command, a recorded ⌘' at key 12 is ⌘Q
    t = Tree(DVORAK_QWERTY_CMD)
    out = AX.press_shortcut(t, None, None, shortcut("cmd+'", 12, char="'", layout=DVORAK_QWERTY_CMD.source_id))
    assert out["refused"] and "types 'q'" in out["error"] and t.sent == []


def test_a_recording_without_a_character_is_checked_by_what_its_key_types():
    cmd = shortcut("cmd+key12", 12)
    t = Tree(US)
    out = AX.press_shortcut(t, None, None, cmd)
    assert out["refused"] and "types 'q'" in out["error"] and "quits" in out["error"] and t.sent == []
    t = Tree(FR)                                                # key 12 is a on AZERTY
    out = AX.press_shortcut(t, None, None, cmd)
    assert out["success"] and t.sent == [(12, CMD)] and "no character was recorded" in out["note"]
    t = Tree(None)                                              # unreadable: the US positions decide
    out = AX.press_shortcut(t, None, None, cmd)
    assert out["refused"] and "com.apple.keylayout.US" in out["error"] and t.sent == []
    # compiled: a gap (the US positions), as a key never sent
    assert "quits" in AX.shortcut_refusal("cmd+key12", 12, CMD)


def test_an_unreadable_layout_sends_the_recorded_keycode_with_a_note():
    for t in (Tree(None), NoLayoutTree()):
        out = AX.press_shortcut(t, None, None, shortcut("cmd+z", 16, char="z", layout=DE.source_id))
        assert out["success"] and t.sent == [(16, CMD)], out
        assert out["sent_as"] == {"keycode": 16, "flags": CMD, "how": "recorded key position", "layout": None}
        assert "could not be read" in out["note"]
    # the US positions still refuse: German ⌘Z recorded at key 16 is fine, key 13 is w there
    t = Tree(None)
    out = AX.press_shortcut(t, None, None, shortcut("cmd+z", 13, char="z", layout=FR.source_id))
    assert out["refused"] and "types 'w'" in out["error"] and t.sent == []


def test_a_character_no_key_types_is_refused_and_nothing_is_sent():
    t = Tree(US)
    out = AX.press_shortcut(t, None, None, shortcut("cmd+ö", 41, char="ö", layout=DE.source_id))
    assert out["refused"] and "no key types 'ö'" in out["error"] and DE.source_id in out["error"]
    assert out["acted"] is False and t.sent == []


def test_keys_that_type_no_character_keep_their_keycode():
    for lay in LAYOUTS:
        t = Tree(lay)
        out = AX.press_shortcut(t, None, None, shortcut("cmd+key123", 123))
        assert out["success"] and t.sent == [(123, CMD)] and "no character" in out["note"]
    for code in (51, 117, 53):                                  # delete, forward delete, escape: refused
        t = Tree(US)
        assert AX.press_shortcut(t, None, None, shortcut(f"cmd+key{code}", code))["refused"] and t.sent == []


# ── compile ──────────────────────────────────────────────────────────────────────────────────

def _chess_with(**key):
    EV._default_vault, EV._default_vault_failed = None, True
    data = json.loads(CHESS.read_text())
    step = next(e for e in data["events"] if e["tag_type"] == "STEP" and e["data"]["action"] == "key")
    step["data"].update(key)
    return data


def test_the_compiled_step_carries_the_recorded_character_and_layout():
    data = _chess_with(char="n", layout=US.source_id)
    rec = DC.compile_record(DC.load_source(data), [], [])
    cmd = rec["steps"][0]["command"]
    assert (cmd["action"], cmd["keys"], cmd["keycode"], cmd["char"], cmd["layout"]) == (
        "press_shortcut", "cmd+n", 45, "n", US.source_id)
    t = Tree(DVORAK)
    t.bundle_id = t.frontmost_bundle_id = lambda: "com.apple.Chess"
    t.window_counts = lambda: {"windows": len(t.sent), "sheets": len(t.sent)}
    out = DC.checked_executor(t)._execute_ax(PlannedStep.from_dict(rec["steps"][0]))
    assert out["success"] and t.sent == [(37, CMD)], out       # n is key 37 on Dvorak
    # a recording without them compiles as before
    cmd = DC.compile_record(DC.load_source(_chess_with()), [], [])["steps"][0]["command"]
    assert "char" not in cmd and "layout" not in cmd and cmd["keycode"] == 45


def test_compile_refuses_by_the_recorded_character():
    st = DC.load_source(_chess_with(keys="cmd+a", keycode=12, char="a", layout=FR.source_id)).steps[0]
    assert (st.action, st.problem, st.char) == ("press_shortcut", "", "a")
    st = DC.load_source(_chess_with(keys="cmd+q", keycode=0, char="q", layout=FR.source_id)).steps[0]
    assert (st.action, st.gap) == ("press_key", "key") and "quits" in st.problem
    st = DC.load_source(_chess_with(keys="cmd+key12", keycode=12)).steps[0]
    assert (st.action, st.gap) == ("press_key", "key") and "quits" in st.problem


# ── the agent's key_combination ──────────────────────────────────────────────────────────────

@pytest.fixture
def posted(monkeypatch):
    sent = []
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    monkeypatch.setenv("ANYUSE_INTERLOCK_TEST_BYPASS", "1")
    monkeypatch.setattr(KB, "press_key", lambda code, flags=0: sent.append((code, flags)) or True)
    return sent


def _executor(native=True):
    be = _Backend()
    be._native_keys = native
    return ActionExecutor(backend=be)


def test_key_combination_posts_the_layouts_key_for_the_character(monkeypatch, posted):
    for lay in LAYOUTS:
        monkeypatch.setattr(KB, "layout_reader", lambda lay=lay: lay)
        for keys in ("cmd+z", "command+z", "command,z", "cmd z", ["command", "z"], "CMD+z"):
            del posted[:]
            out = _executor().execute("key_combination", {"keys": keys})
            assert out["success"] and posted == [(Z_KEY[lay], CMD)], (lay, keys, out)
    monkeypatch.setattr(KB, "layout_reader", lambda: FR)
    for keys, want in (("cmd+a", (12, CMD)), ("cmd+1", (18, CMD | SHIFT)), ("cmd+shift+1", (18, CMD | SHIFT)),
                       ("cmd+Z", (13, CMD)), ("cmd+shift+z", (13, CMD | SHIFT)), ("ctrl+a", (12, CTRL))):
        del posted[:]
        assert _executor().execute("key_combination", {"keys": keys})["success"] and posted == [want], keys
    monkeypatch.setattr(KB, "layout_reader", lambda: DE)
    for keys, want in (("cmd+y", (6, CMD)), ("cmd++", (30, CMD)), ("cmd+plus", (30, CMD)),
                       ("cmd+-", (44, CMD)), ("a", (0, 0))):
        del posted[:]
        assert _executor().execute("key_combination", {"keys": keys})["success"] and posted == [want], keys


def test_key_combination_sends_keys_without_a_character_by_their_fixed_keycode(monkeypatch, posted):
    fixed = (("enter", (36, 0)), ("return", (36, 0)), ("cmd+space", (49, CMD)), ("shift+tab", (48, SHIFT)),
             ("cmd+left", (123, CMD)), ("down", (125, 0)), ("escape", (53, 0)), ("f5", (96, 0)),
             ("cmd+backspace", (51, CMD)), ("fn+forwarddelete", (117, KB.FLAG_FN)), ("pagedown", (121, 0)))
    for lay in LAYOUTS:
        monkeypatch.setattr(KB, "layout_reader", lambda lay=lay: lay)
        for keys, want in fixed:
            del posted[:]
            assert _executor().execute("key_combination", {"keys": keys})["success"] and posted == [want], keys


def test_key_combination_refuses_an_unknown_key_and_sends_nothing(monkeypatch, posted):
    monkeypatch.setattr(KB, "layout_reader", lambda: DE)
    for keys, why in (("cmd+foo", "unknown key 'foo'"), ("win+z", "more than one key"), ("cmd", "names no key"),
                      ("", "names no key"), ("cmd+@", "no key types '@'"), ("cmd+z+x", "more than one key")):
        out = _executor().execute("key_combination", {"keys": keys})
        assert out["success"] is False and why in out["error"], (keys, out)
    assert posted == []


def test_key_combination_uses_pyautogui_only_without_quartz(monkeypatch, posted):
    calls = []

    class PG:
        def hotkey(self, *names):
            calls.append(names)

    class Plain(_Backend):
        pg = None
    be = Plain()
    be.pg, be._native_keys = PG(), False
    assert ActionExecutor(backend=be).execute("key_combination", {"keys": "cmd+z"})["success"]
    assert calls == [("command", "z")] and posted == []


def test_a_key_press_that_fails_is_an_error(monkeypatch):
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    monkeypatch.setenv("ANYUSE_INTERLOCK_TEST_BYPASS", "1")
    monkeypatch.setattr(KB, "layout_reader", lambda: US)
    monkeypatch.setattr(KB, "press_key", lambda code, flags=0: False)
    out = _executor().execute("key_combination", {"keys": "cmd+n"})
    assert out["success"] is False and "failed or was refused" in out["error"]


# ── the recording side ───────────────────────────────────────────────────────────────────────

def test_the_recording_reads_the_chords_character_from_the_layout(monkeypatch):
    monkeypatch.setattr(KB, "layout_reader", lambda: DE)
    assert SC.layout_char(16, SC.FLAG_COMMAND) == {"char": "z", "layout": DE.source_id}
    assert SC.layout_char(16, SC.FLAG_COMMAND | SC.FLAG_SHIFT) == {"char": "z", "layout": DE.source_id}
    assert SC.layout_char(13, SC.FLAG_COMMAND | SC.FLAG_OPTION) == {"char": "w", "layout": DE.source_id}
    assert SC.layout_char(123, SC.FLAG_COMMAND) is None                # an arrow types no character
    monkeypatch.setattr(KB, "layout_reader", lambda: RU)
    assert SC.layout_char(6, SC.FLAG_COMMAND)["char"] == "z"           # the Command map
    assert SC.layout_char(6, SC.FLAG_CONTROL)["char"] == "z"           # Control alone: the control letter (^Z)
    monkeypatch.setattr(KB, "layout_reader", lambda: None)
    assert SC.layout_char(16, SC.FLAG_COMMAND) is None


def test_the_capture_hands_the_layout_character_to_the_recorder():
    got = []

    class Rec:
        input_pending = None

        def start(self):
            pass

        def due_in(self):
            return 0.01

        def on_idle(self):
            pass

        def on_key(self, keycode, flags, char, credential=False, layout=None):
            got.append((keycode, flags, char, credential, layout))

        def close(self, _why):
            pass

    cap = SC.StepCapture(Rec(), source_factory=lambda put: None, suppressed=lambda: False)
    lay = {"char": "z", "layout": DE.source_id}
    cap._put(("key", 16, SC.FLAG_COMMAND, "z", lay))
    cap._put(("key", 36, 0, None))
    cap._q.put(("stop",))
    cap._work()
    assert got == [(16, SC.FLAG_COMMAND, "z", False, lay), (36, 0, None, False, None)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
