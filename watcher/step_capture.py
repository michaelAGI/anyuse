"""Structured step capture: what the user did, as replayable AX steps.

The batch watcher's model describes a session in prose ("clicked Save"); the draft seam
(``execution/draft_compile.py``) needs an action verb and the
target's AX identity for every step. This module records exactly that, beside the prose:

* **Event source** - a listen-only Quartz event tap (``kCGEventTapOptionListenOnly``: it
  observes, it cannot inject or alter an event) for left/right mouse down, left mouse up and
  drag, and key down. A key event carries its key code and modifier flags only; the
  character is read ONLY for a command/control chord (to find the menu item a shortcut
  stands for), from the event and from the keyboard layout in use (``layout_char``: the
  character the chord stands for, Option ignored, and the layout's id). Typed text is
  never taken from key events.
* **Shortcuts** - a command/control chord becomes the menu item that owns it
  (``AXMenuItemCmdChar`` / ``AXMenuItemCmdModifiers``), with its menu bar item before it. An
  app that builds a menu only when it opens (Chess's "Spiel") reports no items for it: when
  actuation is allowed (the drive gate and the foreground interlock, i.e. in a VM guest) such
  a menu is opened once by AX, read, and closed (``MacAXProbe.open_menu_for_shortcut``), the
  answer kept per app and version for the recording (memory only). A chord no menu item owns
  stays a ``key`` step with its key code, flags, character and layout id; the compiler replays
  it as the shortcut itself when the recording shows its effect (``draft_compile``: "replayed
  as a shortcut"), by the key that types that character on the replay's layout.
* **Text** - a text field's FINAL value, read from the field (``AXValue``) when it loses
  focus or is committed (a click elsewhere, Tab, Return, Escape, a shortcut, an app switch,
  stop). Keys typed while no text field has focus produce one marker step per burst
  ("typing outside a text field is not recorded"), with no character in it. The final value
  is compared with the value the field held when focus entered it (read on the click into
  it, after the trigger that focused it, or with the last fingerprint; memory only):
  the old value plus a suffix is ``input_mode: append`` with the suffix as the value; one
  changed line of a text area is ``input_mode: replace_line`` with the new line as the
  value and an ``anchor`` (an unchanged part of that line, in no other line); anything else
  is today's whole value (``set``, not written).
* **The front app** (``front_application``, shared with the VM harness): AX's
  ``AXFocusedApplication`` first; on an error or no value NSWorkspace's front app after one
  run-loop tick, cross-checked with the menu bar's owner. Unknown (nothing answers, or the two
  disagree) withholds the surface (``surface_unknown``), except for a click in the menu bar of the
  frontmost app (its own menu bar, its AX element frontmost): that app is the front, also when it
  has no window, and a window list that answered empty has no secure field to show.
* **Identity** - per step: the verb (click / choose / toggle / select / type / key), app,
  bundle id, the target's AX role, subrole, identifier, title, description, a role-index
  ancestor path and its frame, plus the fingerprint of the screen before the step (the
  drift baseline ``draft_compile`` reads as ``ax_states``; each element's role, title and
  AX identifier). A click on a list's row, or in a cell of one, records the row's label
  (its first cell's text) as the selector's name, the list's identity
  (``container``: role, AX identifier, description, path below the window, column count) and
  the cell's ``column``. A typing step into a field with no title, description or
  identifier that the step right before it (a click, a menu item, a key: the trigger)
  focused is recorded as ``focused`` (selector kind ``focused`` with the field's role, the
  app's bundle id and a salted key of the window's AXDocument; ``focused_after`` names the
  trigger): the element that has focus is read ``FOCUS_SETTLE_S`` after the trigger, on the
  worker, after a fresh secure-field scan, and only when it belongs to the front app.
* **Privacy** - a secure field (role or subrole ``AXSecureTextField``, or a field whose label,
  placeholder or titling element (AXTitleUIElement) says password / PIN / TAN / one-time
  code ...) is recorded as ``secure: true`` with its role only; its value is never read.
  Every other typed value goes into the entity vault (``EntityVault.tokenize``, class
  ``typed_text``, a presence-tier class) and the step carries only the marker
  ``[[ent:typed_text:<12 hex>]]`` in ``value_ref``; a ``replace_line`` anchor likewise (the
  compiler makes it a parameter); the window title as a ``window_title`` marker. A list
  row's label and its list's identifier and description are ``content_label`` markers
  (presence-tier: the compiler reads them only with presence, an export only with
  ``--release-content``); one with machine-shaped personal data in it is not kept
  (``content_problem``). UI labels and fingerprint titles and identifiers are ``ui_label``
  markers (the compiler on this machine resolves them; ``--release-labels``). A document is
  kept only as a salted key. A value shaped like a payment card (the vault's
  own regex net) is withheld. No vault means no value (``withheld: no_vault``). The guard
  the watcher passes in decides per surface: denylisted apps / titles and credential
  typing drop the event entirely (a key event is marked as credential typing when it is
  observed, not when the worker reaches it); a surface the phishing guard flagged and a
  meeting surface without far-side consent keep only the step's kind and role
  (``withheld: <reason>``). The recorder itself withholds a focused window that shows a
  secure field (the probe scans roles and subroles) and a terminal emulator (its text
  area is the whole scrollback, not a typed value).
* **Steps -> drafts** - steps are grouped into recordings (``steps_<first app>_<n>``): a new
  one starts after ``ANYUSE_WATCH_STEPS_IDLE_SPLIT`` seconds (default 300) without a step; a
  step in another app stays in the recording (each step carries its app and bundle id, so the
  draft is a multi-app one), unless ``ANYUSE_WATCH_STEPS_SPLIT_BY_APP=1`` starts a new one
  there as before. Each recording is framed by WORKFLOW_START / WORKFLOW_END
  tags and kept in ``WatchSession.step_events`` / ``ax_states`` (session.json, encrypted
  with k_raw); ``WatchSession.structured_draft`` gives the draft ``draft_compile`` reads
  (``anyuse workflow from-draft SESSION/steps_textedit_1``).

Gate: ``ANYUSE_WATCH_STRUCTURED_STEPS=1`` (default off), and the process must be
Accessibility-trusted (without AX the secure-field check is blind: capture refuses).

    python -m anyuse.watcher.step_capture list
    python -m anyuse.watcher.step_capture export SESSION/steps_textedit_1 --out draft.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from anyuse.watcher.models import TagEvent, TagType, to_snake_case

logger = logging.getLogger("anyuse.watcher.step_capture")

ENV_GATE = "ANYUSE_WATCH_STRUCTURED_STEPS"
IDLE_SPLIT_ENV = "ANYUSE_WATCH_STEPS_IDLE_SPLIT"
DEFAULT_IDLE_SPLIT_S = 300.0
#: "1": a step in another app starts a new recording (the behaviour before multi-app drafts)
SPLIT_BY_APP_ENV = "ANYUSE_WATCH_STEPS_SPLIT_BY_APP"
SOURCE = "ax_capture"
#: the vault class of a typed value: unknown to the vault's tiers, so presence-gated
VALUE_CLASS = "typed_text"
TITLE_CLASS = "window_title"
#: the vault class of an element's title / description (a standing-grant class): kept as a
#: marker at rest, resolved in-process by the compiler on this machine (grantee below)
LABEL_CLASS = "ui_label"
LABEL_GRANTEE = "draft_compile"
LABEL_MARKER = re.compile(r"\[\[ent:ui_label:([0-9a-f]{12})\]\]")
#: the vault class of content a target is found by (a list row's label, a list's identifier
#: and description): presence-tier. The compiler resolves it only by a presence-gated deref and
#: only where the replay needs the text; an export writes it only with ``--release-content``
CONTENT_CLASS = "content_label"
CONTENT_MARKER = re.compile(r"\[\[ent:content_label:([0-9a-f]{12})\]\]")
MAX_VALUE = 20000                  # draft_schema.MAX_TYPED
#: a field value held in memory for the input-mode comparison at most this long (else unknown)
MAX_INITIAL = 100000
FP_DELAY_S = 0.35                  # a screen fingerprint after a structural event
VALUE_DELAY_S = 0.15               # the focused field's value after typing pauses
FOCUS_SETTLE_S = 0.3               # the focused element after a trigger step
#: a shortcut's own effect is given this long before a menu is opened to find its item: the app
#: handles the key on its main thread, and a menu opened first would take the key into its tracking
SHORTCUT_SETTLE_S = 0.4
#: a focus that has not moved yet is read again this often, up to this long after the trigger
#: (Notes focuses a new note's body later than 0.3 s in the guest), until input arrives
FOCUS_RETRY_S, FOCUS_WAIT_S = 0.15, 1.2
PHISHING_HOLD_S = 600.0
#: steps whose effect may move keyboard focus into a field (the trigger of a ``focused`` step)
TRIGGER_ACTIONS = frozenset({"click", "choose", "toggle", "select", "key"})
#: a ``replace_line`` anchor is at least this many characters of the line's unchanged part
MIN_ANCHOR = 3
_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")    # draft_compile._CONTROL

#: guard reasons that drop an event entirely (nothing of it is kept)
DROP = frozenset({"denylist_app", "denylist_title", "credential_typing"})
#: guard reasons that keep only a step's kind and role
WITHHOLD = frozenset({"secure_field", "phishing_suspected", "meeting_surface", "terminal", "surface_unknown"})
#: roles whose title is the app's content rather than a control's name: a list row or cell (a
#: file, a note, a mail), a link (a webmail subject or sender), static text. Their titles are
#: ``content_label`` (presence-tier), never ``ui_label``; a row's or cell's stays out of the
#: step's target altogether (its label lives in the selector's name)
CONTENT_ROLES = frozenset({"AXRow", "AXCell", "AXOutlineRow", "AXLink", "AXStaticText"})
ROW_CELL_ROLES = frozenset({"AXRow", "AXCell", "AXOutlineRow"})
#: UI labels the recorder keeps in memory (text -> marker) at most; more clears the cache
MAX_LABEL_CACHE = 5000

TEXT_ENTRY_ROLES = frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox",
                              "AXSecureTextField"})
MULTILINE_ROLES = frozenset({"AXTextArea"})
SECURE = "AXSecureTextField"
#: a field whose label says it holds a secret is treated as a secure field
_SECRET_LABEL = re.compile(
    r"(?i)(passw|kennwort|passcode|passkey|passphrase|\bpin\b|\bpuk\b|geheimzahl"
    r"|\b(?:m|sms|push|chip|photo|app|i|e)?tan\b|\botp\b|one[- ]time|einmal(code|passwort)"
    r"|\bcvc\b|\bcvv\b|security code|sicherheitscode|verification code|bestätigungscode"
    r"|auth(?:entication|entifizierungs)?[- ]?code|\b2fa\b|zwei-faktor|two-factor"
    r"|\bsecret\b|\btoken\b|api[- _]?key|api[- ]?schlüssel|access[- ]key|private[- ]key"
    r"|privater schlüssel|recovery[- ](?:code|key|phrase)|wiederherstellungs(?:code|schlüssel)"
    r"|seed[- ]phrase|mnemonic)")
#: terminal emulators: a terminal's text area holds its whole scrollback (output as well as
#: what was typed), so its "final value" is not a typed value; the surface is withheld
TERMINAL_BUNDLES = frozenset({
    "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "net.kovidgoyal.kitty",
    "org.alacritty", "io.alacritty", "com.mitchellh.ghostty", "co.zeit.hyper",
    "com.github.wez.wezterm"})
#: a visible secure field withholds its surface; the focused window is scanned at most this often
SECURE_SCAN_TTL_S = 2.0
#: a scan the AX API did not answer (``AXScanError``: an out-of-process panel coming up, measured in
#: the guest) is asked again this often, this far apart, before the surface is withheld
SCAN_RETRIES, SCAN_RETRY_S = 3, 0.2
SECURE_SCAN_MAX = 2500             # elements of one focused-window scan (roles and subroles only)

# key codes (ANSI layout positions; layout-independent for these keys)
_RETURN, _ENTER, _TAB, _SPACE, _DELETE, _ESCAPE, _FWD_DELETE = 36, 76, 48, 49, 51, 53, 117
_NAV = {123: "left", 124: "right", 125: "down", 126: "up", 115: "home", 119: "end",
        116: "pageup", 121: "pagedown", 114: "help",
        122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5", 97: "f6", 98: "f7", 100: "f8",
        101: "f9", 109: "f10", 103: "f11", 111: "f12"}
# CGEventFlags
FLAG_SHIFT, FLAG_CONTROL, FLAG_OPTION, FLAG_COMMAND = 0x20000, 0x40000, 0x80000, 0x100000
#: command chords that edit the focused field's text (no step: the field's value says it)
EDIT_CHARS = frozenset("acvxz")
#: system chords (cmd-Tab, cmd-Space, cmd-`): the system switches apps or windows; the
#: focused field is committed, no step is recorded
_SYSTEM_CHORD_KEYS = frozenset({_TAB, _SPACE, 50})
_STEP_VERB = {"AXMenuItem": "choose", "AXCheckBox": "toggle", "AXSwitch": "toggle",
              "AXRadioButton": "select", "AXTab": "select"}


def enabled() -> bool:
    return (os.environ.get(ENV_GATE) or "").strip().lower() in ("1", "true", "yes", "on")


def idle_split_s() -> float:
    try:
        v = float(os.environ.get(IDLE_SPLIT_ENV) or DEFAULT_IDLE_SPLIT_S)
        return v if v > 0 else DEFAULT_IDLE_SPLIT_S
    except ValueError:
        return DEFAULT_IDLE_SPLIT_S


def split_by_app() -> bool:
    return (os.environ.get(SPLIT_BY_APP_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def capture_refusal(ax_trusted: Optional[Callable[[], bool]] = None) -> Optional[str]:
    """Why structured capture must not start here, or None."""
    if not enabled():
        return f"{ENV_GATE} is not set (structured step capture is opt-in)"
    import platform as _platform
    if _platform.system() != "Darwin":
        return "recording Mac apps needs macOS (run it in a macOS VM); the web path works on this system"
    try:
        if ax_trusted is None:
            from anyuse.context.accessibility import ax_trusted as ax_trusted  # noqa: F811
        trusted = bool(ax_trusted())
    except Exception:  # noqa: BLE001 - unknown is refused like denied
        trusted = False
    if not trusted:
        return ("Accessibility is not granted to this process: without AX a secure field "
                "cannot be recognised, so nothing is recorded")
    return None


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def classify_key(keycode: int, flags: int) -> Tuple[str, str]:
    """``(kind, name)`` from the key code and modifier flags only - never a character.
    kind: shortcut | return | escape | tab | delete | nav | char."""
    keycode, flags = int(keycode), int(flags)
    if flags & (FLAG_COMMAND | FLAG_CONTROL):
        return "shortcut", ""
    if keycode in (_RETURN, _ENTER):
        return "return", "return"
    if keycode == _ESCAPE:
        return "escape", "escape"
    if keycode == _TAB:
        return "tab", "tab"
    if keycode in (_DELETE, _FWD_DELETE):
        return "delete", "delete" if keycode == _DELETE else "forward_delete"
    if keycode in _NAV:
        return "nav", _NAV[keycode]
    if keycode == _SPACE:
        return "char", "space"
    return "char", ""


def ax_modifiers(flags: int) -> int:
    """CGEventFlags -> ``AXMenuItemCmdModifiers`` (0 = command only; +1 shift, +2 option,
    +4 control, +8 no command)."""
    flags = int(flags)
    m = 0
    if flags & FLAG_SHIFT:
        m |= 1
    if flags & FLAG_OPTION:
        m |= 2
    if flags & FLAG_CONTROL:
        m |= 4
    if not flags & FLAG_COMMAND:
        m |= 8
    return m


def _edit_chord(flags: int, char: Optional[str]) -> bool:
    """A chord that edits the focused field's text (cmd-A/C/V/X/Z, a control chord)."""
    flags = int(flags)
    ch = (char or "").strip().lower()[:1]
    cmd_only = bool(flags & FLAG_COMMAND) and not flags & (FLAG_CONTROL | FLAG_OPTION)
    ctrl_only = bool(flags & FLAG_CONTROL) and not flags & FLAG_COMMAND
    return (bool(ch) and ch in EDIT_CHARS and cmd_only) or ctrl_only


def shortcut_name(flags: int, char: Optional[str], keycode: int = -1) -> str:
    bits = [n for f, n in ((FLAG_CONTROL, "ctrl"), (FLAG_OPTION, "opt"), (FLAG_SHIFT, "shift"),
                           (FLAG_COMMAND, "cmd")) if int(flags) & f]
    key = (char or "").strip().lower()[:1] or (f"key{int(keycode)}" if keycode >= 0 else "?")
    return "+".join(bits + [key])


def layout_char(keycode: int, flags: int) -> Optional[Dict[str, str]]:
    """``{"char", "layout"}``: the character a chord's key stands for on the layout in use
    (``Layout.chord_char``): with Command the Command map (what a menu's key equivalent is matched by),
    with Control alone the letter of the control character the key types (^C), Option ignored; with
    Shift the shifted character when Shift makes a different one (⌘⇧= is ``+`` on US, ⌘⇧ on AZERTY's &
    key is ``1``), the letter when Shift only changes its case. Plus the layout's input source id; None
    when the layout cannot be read or the key types no character. The event's own string is the Option
    glyph under Option (``∑`` for opt+cmd+w) and a control character under Control; a replay on another
    layout sends the key that types this character there."""
    from anyuse.context import keyboard as KB
    lay = KB.current_layout()
    if lay is None:
        return None
    ch = lay.chord_char(int(keycode), int(flags))
    ch = ch if len(ch) == 1 and ch.isprintable() and not ch.isspace() else ""
    return {"char": ch, "layout": lay.source_id} if ch else None


#: the keys of an element description that say what a field is for: its own label, its
#: placeholder (``label_hint``) and the text of the element that titles it (``title_hint``,
#: AXTitleUIElement: an unlabeled field next to "TAN"). The hints decide only; never stored
LABEL_KEYS = ("identifier", "title", "description", "label_hint", "title_hint")


def is_secure(desc: Optional[Dict[str, Any]]) -> bool:
    if not desc:
        return False
    if desc.get("role") == SECURE or desc.get("subrole") == SECURE:
        return True
    if desc.get("role") not in TEXT_ENTRY_ROLES:
        return False
    return any(_SECRET_LABEL.search(str(desc.get(k) or "")) for k in LABEL_KEYS)


def is_text_entry(desc: Optional[Dict[str, Any]]) -> bool:
    return bool(desc) and (desc.get("role") in TEXT_ENTRY_ROLES or desc.get("subrole") == SECURE)


def own_identity(desc: Optional[Dict[str, Any]]) -> bool:
    """Does the element carry a name or an identifier of its own (not only its path)?"""
    return bool(desc) and any(str(desc.get(k) or "").strip() for k in ("title", "description", "identifier"))


def outside_windows(desc: Optional[Dict[str, Any]]) -> bool:
    """The element hangs off the application, outside every window and the menu bar (its path has
    no window): an app's field editor, such as Finder's inline name field (``application/
    textfield[0]``, identifier ``ShrinkToFit Text Field``, measured in the guest). No
    tree walk lists it (the app's children are its windows and menu bar), so its own identity
    cannot be found again: it is reachable only as the focused element."""
    path = str((desc or {}).get("path") or "")
    return bool(path) and "window[" not in path and not (desc or {}).get("in_menubar")


def line_anchor(old_line: str, new_line: str, old_lines: List[str]) -> str:
    """The text a ``replace_line`` step finds its line by: the longer of the line's unchanged
    start and unchanged end (in the line before the edit and still in it after: not typed),
    trimmed, at least ``MIN_ANCHOR`` characters, in exactly ONE line of the value before, with
    no control character, no ``${`` and nothing the vault's regex net calls personal data.
    "" when neither part qualifies."""
    from anyuse.execution import ax_actions as AX
    head = os.path.commonprefix([old_line, new_line])
    tail = os.path.commonprefix([old_line[::-1], new_line[::-1]])[::-1]
    for a in sorted((head.strip()[:AX.MAX_ANCHOR], tail.strip()[-AX.MAX_ANCHOR:]), key=len, reverse=True):
        if (len(a) >= MIN_ANCHOR and sum(1 for t in old_lines if a in t) == 1
                and not _CONTROL_CHARS.search(a) and "${" not in a and content_problem(a) is None):
            return a
    return ""


#: an amount with its currency before or after it ("1.234,56 €", "EUR 12.50"), or a German
#: thousands-grouped amount; the vault's regex net knows only "€12.50"
_AMOUNT_TEXT = re.compile(r"(?i)(?:[$€£]|\b(?:eur|usd|chf|gbp)\b)\s?\d(?:[\d.,' ]*\d)?"
                          r"|\d(?:[\d.,' ]*\d)?\s?(?:[$€£]|\b(?:eur|usd|chf|gbp)\b)"
                          r"|\b\d{1,3}(?:[.' ]\d{3})+,\d{2}\b")
_IBAN_START = re.compile(r"[A-Z]{2}\d{2}")


def embedded_iban(text: str) -> bool:
    """A checksum-valid IBAN anywhere in ``text`` (spaces inside it allowed)."""
    t = (text or "").upper()
    for m in _IBAN_START.finditer(t):
        compact = re.sub(r"[ ]", "", t[m.start():m.start() + 44])
        compact = re.match(r"[A-Z0-9]*", compact).group(0)
        if any(iban_valid(compact[:n]) for n in range(15, min(34, len(compact)) + 1)):
            return True
    return False


def content_problem(text: Optional[str]) -> Optional[str]:
    """What makes a text taken from the app's content unfit to be kept as the name of a target
    (a row's label, a list's identifier or description, a line's anchor): machine-shaped personal
    data - the vault's whole regex net (e-mail, card, phone, amount), an IBAN anywhere in it, an
    amount with its currency after it, a card expiry. None when clean; a net that cannot be read
    counts as a hit."""
    text = text or ""
    if not text.strip():
        return None
    try:
        from anyuse.watcher.entity_vault import regex_net
        hits = regex_net(text)
    except Exception:  # noqa: BLE001
        return "unchecked"
    if hits:
        return hits[0][3]
    if embedded_iban(text):
        return "iban"
    if _AMOUNT_TEXT.search(text):
        return "amount"
    return payment_shape(text)


def start_shape(before: str, anchors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The field's start value as the proof needs it, without its text (the shape
    ``ax_actions.start_shape`` describes on feat/focus-anchor-hooks): its line count and, per
    anchor MARKER, the lines whose text contains that anchor (at most 3); ``breaks: other`` when
    it holds a break ``split_lines`` does not know. ``anchors`` maps marker -> text in memory only."""
    from anyuse.execution import ax_actions as AX
    lines = AX.split_lines(before or "")
    if lines is None:
        return {"lines": 0, "anchors": {}, "breaks": "other"}
    return {"lines": len(lines),
            "anchors": {m: [i for i, (ln, _b) in enumerate(lines) if t in ln][:3] for m, t in (anchors or {}).items()}}


def input_mode_of(before: str, after: str, multiline: bool = False) -> Tuple[str, str, str]:
    """``(mode, text, anchor)``: how a field's value ``after`` came from ``before`` (the value
    when focus entered it), in the runner's text modes (``ax_actions.apply_mode``):

    * ``append``: ``after`` is ``before`` plus a suffix; ``text`` is the suffix;
    * ``replace_line``: a text area (or a value with line breaks) in which exactly ONE line
      changed, its break kept; ``text`` is the new line, ``anchor`` an unchanged part of it
      (``line_anchor``), or "" when none qualifies (the compiler leaves the step a gap). The
      anchor is kept only when the runner's own edit reproduces ``after`` exactly;
    * ``set`` otherwise (an empty ``before``, a change in several lines): ``text`` is ``after``."""
    from anyuse.execution import ax_actions as AX
    if not before or after == before:
        return "set", after, ""
    if after.startswith(before):
        return "append", after[len(before):], ""
    if not multiline and not any(b in before for b in AX.LINE_BREAKS):
        return "set", after, ""
    old, new = AX.split_lines(before), AX.split_lines(after)
    if old is None or new is None or len(old) != len(new):
        return "set", after, ""
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    if len(changed) != 1:
        return "set", after, ""
    (o_text, o_break), (n_text, n_break) = old[changed[0]], new[changed[0]]
    if o_break != n_break or not n_text.strip():
        return "set", after, ""
    anchor = line_anchor(o_text, n_text, [t for t, _b in old])
    if anchor:
        got, _rng, why, _code = AX.apply_mode("replace_line", before, n_text, anchor)
        if why or got != after:
            anchor = ""
    return "replace_line", n_text, anchor


def _actionable(role: Optional[str]) -> bool:
    from anyuse.context.accessibility import ACTIONABLE_ROLES
    return bool(role) and role in ACTIONABLE_ROLES


def selector_of(desc: Dict[str, Any]) -> Dict[str, str]:
    """The identity ``draft_compile`` replays by. The name is the title or the description,
    never the value (a text field's value is what was typed)."""
    from anyuse.context.accessibility import canonical_role
    role = str(desc.get("role") or "")
    return {"canonical_role": canonical_role(role), "raw_role": role,
            "name": str(desc.get("title") or desc.get("description") or "").strip()[:500],
            "identifier": str(desc.get("identifier") or "").strip()[:200],
            "path": str(desc.get("path") or "")[:200]}


def target_of(desc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: desc.get(k) for k in ("role", "subrole", "identifier", "title", "description", "path")
           if desc.get(k)}
    if desc.get("frame"):
        out["frame"] = [round(float(v), 1) for v in desc["frame"]][:4]
    return out


def target_text(desc: Optional[Dict[str, Any]]) -> str:
    """The step's stored one-line target: the role and a plain identifier, no label."""
    if not desc:
        return "unknown target"
    ident = str(desc.get("identifier") or "")
    bits = [str(desc.get("role") or "unknown")]
    if ident and not LABEL_MARKER.fullmatch(ident) and not identifier_is_content(ident):
        bits.append(f"id={ident}")
    return " ".join(bits)[:200]


def identifier_is_content(ident: str) -> bool:
    """An AXIdentifier is assigned by the app's developer and kept plain - unless the
    vault's regex net finds machine-shaped personal data in it (a web app's DOM id with an
    e-mail address, a phone number, an amount): then it is vaulted like a label."""
    if not ident:
        return False
    try:
        from anyuse.watcher.entity_vault import regex_net
        return bool(regex_net(ident))
    except Exception:  # noqa: BLE001 - an unreadable net vaults it
        return True


def ensure_label_grant(vault, content: bool = False) -> None:
    """The grants that let the compiler on this machine read labels back: ``ui_label`` standing
    (audited, no presence); with ``content``, ``content_label`` only with presence (every read
    asks; audited)."""
    want = [(LABEL_CLASS, False)] + ([(CONTENT_CLASS, True)] if content else [])
    try:
        grants = vault.list_grants(LABEL_GRANTEE)
        for cls, presence in want:
            if any(cls in g["classes"] and not g["revoked_at"] and g["requires_presence"] == presence
                   for g in grants):
                continue
            vault.grant(LABEL_GRANTEE, [cls], mode="deref", requires_presence=presence,
                        created_via="step_capture")
    except Exception as exc:  # noqa: BLE001
        logger.warning("step capture: label grant failed (%s)", type(exc).__name__)


def release_labels(data: Dict[str, Any], resolve: Callable[[str], Optional[str]],
                   content_resolve: Optional[Callable[[str], Optional[str]]] = None) -> Tuple[Dict[str, Any], int]:
    """A copy of a structured draft with its ``ui_label`` markers resolved by ``resolve``
    (unresolved ones stay markers): selector names and identifiers, and the titles and
    identifiers of fingerprints. ``content_label`` markers (a row's label, its list's identifier
    and description) only with ``content_resolve``. Typed values, a line's anchor (a typed
    value), window titles and documents (a salted key) are never touched."""
    import copy
    out = copy.deepcopy(data)
    n = 0

    def rel(obj: Any, key: str) -> None:
        nonlocal n
        v = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(v, str):
            return
        how = (resolve if LABEL_MARKER.fullmatch(v) else
               content_resolve if content_resolve is not None and CONTENT_MARKER.fullmatch(v) else None)
        r = how(v) if how is not None else None
        if r is not None:
            obj[key], n = r, n + 1
    for ev in out.get("events") or []:
        data_ = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(data_, dict):
            continue
        for k in ("name", "identifier"):
            rel(data_.get("selector"), k)
        for k in ("identifier", "description"):
            rel(data_.get("container"), k)
    for st in (out.get("ax_states") or {}).values():
        for el in ((st or {}).get("fingerprint") or {}).get("required_elements") or []:
            rel(el, "title")
            rel(el, "identifier")
    return out, n


def vault_label_resolver(vault, purpose: str = "compile a recorded workflow",
                         marker: "re.Pattern[str]" = LABEL_MARKER) -> Optional[Callable[[str], Optional[str]]]:
    if vault is None:
        return None
    cache: Dict[str, Optional[str]] = {}

    def resolve(m: str) -> Optional[str]:
        hit = marker.fullmatch(m or "")
        if not hit:
            return None
        if m not in cache:
            try:
                out = vault.deref(hit.group(1), LABEL_GRANTEE, purpose)
            except Exception:  # noqa: BLE001
                out = {}
            cache[m] = out.get("value") if out.get("ok") else None
        return cache[m]
    return resolve


def vault_content_resolver(vault, purpose: str = "compile a recorded workflow") -> Optional[Callable[[str], Optional[str]]]:
    """``content_label`` markers resolved by a deref under the presence-gated grant (the vault
    asks for presence on each read, audited); one read per marker within this resolver."""
    return vault_label_resolver(vault, purpose, marker=CONTENT_MARKER)


def describe_target(desc: Optional[Dict[str, Any]]) -> str:
    if not desc:
        return "unknown target"
    sel = selector_of(desc)
    bits = [sel["raw_role"] or "unknown"]
    if sel["name"]:
        bits.append(repr(sel["name"]))
    if sel["identifier"]:
        bits.append(f"id={sel['identifier']}")
    return " ".join(bits)[:200]


def fingerprint_from_descs(descs: List[Dict[str, Any]], app: str) -> Tuple[str, Dict[str, Any]]:
    """A screen fingerprint made the way the guest drive harness makes it: the actionable elements outside
    the menu bar (role + title, and the AX identifier when the probe read it: a choice over
    them is then bound by identity, ``draft_compile.choice_domain``; matching reads role +
    title) are required, the count range and role hash come from the whole tree. Never a
    value."""
    from anyuse.context.accessibility import ACTIONABLE_ROLES
    from anyuse.guides import state_graph as sg
    live = [{"role": d.get("role"), "title": d.get("title") or ""} for d in descs]
    actionable = [dict({"role": d.get("role"), "title": d.get("title") or ""},
                       **({"identifier": d.get("identifier") or ""} if "identifier" in d else {}))
                  for d in descs if d.get("role") in ACTIONABLE_ROLES and not d.get("in_menubar")]
    fp = sg.UIFingerprint(required_elements=actionable,
                          element_count_range=(int(len(live) * 0.6), int(len(live) * 1.4) + 10),
                          structural_hash=sg.compute_structural_hash(live),
                          role_frequency=sg._role_frequency(live))
    # generate_state_id hashes roles only: two screens that differ in titles share it, and
    # one fingerprint would overwrite the other's required titles. A digest of the
    # fingerprint keeps them apart.
    digest = hashlib.sha256(json.dumps(fp.to_dict(), sort_keys=True, ensure_ascii=False)
                            .encode()).hexdigest()[:6]
    sid = f"{sg.generate_state_id(actionable, app)}-{digest}"
    return sid, sg.UIState(state_id=sid, app=app, fingerprint=fp).to_dict()


#: a field for a payment card's number, expiry or code (label, identifier, placeholder)
_CARD_LABEL = re.compile(
    r"(?i)(card ?number|card-?num|\bcc-?(?:number|num|exp|csc|cvc)\b|cardnumber|kartennummer"
    r"|kreditkarte|credit ?card|debit ?card|karteninhaber|expir|ablaufdatum|gültig bis"
    r"|valid (?:thru|until)|\bcvc\b|\bcvv\b|\bcsc\b|prüfnummer)")
_EXPIRY = re.compile(r"^\s*(0[1-9]|1[0-2])\s*[/\-.]\s*(\d{2}|\d{4})\s*$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def iban_valid(value: str) -> bool:
    v = re.sub(r"\s+", "", value or "").upper()
    if not _IBAN.match(v):
        return False
    digits = "".join(str(int(c, 36)) for c in v[4:] + v[:4])
    return int(digits) % 97 == 1


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def payment_shape(value: str, desc: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Payment data the vault must never hold: a field labelled for a card, an IBAN
    (checksum-valid), a card expiry (MM/YY), or a card number (the vault's regex net)."""
    if desc and any(_CARD_LABEL.search(str(desc.get(k) or "")) for k in LABEL_KEYS):
        return "payment_card"
    if iban_valid(value):
        return "iban"
    if _EXPIRY.match(value or ""):
        return "card_expiry"
    return never_captured_shape(value)


_REWRITE_LOCK = threading.Lock()


def _markers_in(obj: Any) -> List[Tuple[str, str]]:
    """Every ``(class, token)`` vault marker anywhere in ``obj`` (its JSON text)."""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return []
    return [(m.group(1), m.group(2)) for m in MARKER_ANY.finditer(text)]


def withhold_events(events, match: Callable[[TagEvent], bool], reason: str, vault=None,
                    states: Optional[Dict[str, Any]] = None, forgotten: Optional[set] = None,
                    classes: Optional[frozenset] = None,
                    drop_state: Optional[Callable[[str, Dict[str, Any]], bool]] = None) -> int:
    """Rewrite matching STEP tags in place to their withheld form (kind and role only, no
    fingerprint), drop from ``states`` the fingerprints only they used, and forget from
    ``vault`` EVERY marker the rewritten steps and dropped fingerprints held (typed values,
    window titles, anchors, row labels, list names, UI labels, fingerprint titles and
    identifiers) that no kept step or fingerprint still names, when nothing else wrote it
    (``EntityVault.forget``): a value written per occurrence by the number of references, a
    label the recorder writes once per text and app (``ui_label``) once. ``forgotten`` collects
    the tokens deleted; ``classes`` limits forgetting to those classes (a caller that passes only
    some of the session's steps). ``drop_state(state_id, state)`` also drops the fingerprints it
    names (the flagged app's, taken in its hold window) that no kept step names. Returns how
    many steps."""
    n = 0
    withdrawn: Dict[Tuple[str, str], int] = {}
    with _REWRITE_LOCK:
        used: set = set()
        for ev in list(events):
            if ev.tag_type != TagType.STEP or not match(ev) or ev.data.get("withheld") == reason:
                continue
            data = ev.data
            for key in _markers_in(data):
                withdrawn[key] = withdrawn.get(key, 0) + 1
            if ev.ax_state_id:
                used.add(ev.ax_state_id)
            role = ((data.get("target") or {}).get("role") or "")
            kept = {k: data[k] for k in ("action", "source", "app", "bundle_id", "keys") if k in data}
            ev.data = dict(kept, withheld=reason, target={"role": role} if role else {})
            ev.target = f"{role or 'element'} (withheld: {reason})"
            ev.ax_state_id = None
            n += 1
        if states is not None:
            still = {e.ax_state_id for e in events if getattr(e, "ax_state_id", None)}
            gone = used - still
            if drop_state is not None:
                gone |= {sid for sid, st in list(states.items())
                         if sid not in still and isinstance(st, dict) and drop_state(sid, st)}
            for sid in gone:
                for key in _markers_in(states.pop(sid, None)):
                    withdrawn[key] = withdrawn.get(key, 0) + 1
        # what a kept step or fingerprint still names stays
        named = set(_markers_in([e.data for e in events if e.tag_type == TagType.STEP]))
        if states is not None:
            named |= set(_markers_in(states))
        for (cls, token), refs in withdrawn.items():
            if vault is None or (cls, token) in named or (classes is not None and cls not in classes):
                continue
            try:
                if vault.forget(token, max_seen=1 if cls == LABEL_CLASS else refs) and forgotten is not None:
                    forgotten.add(token)
            except Exception:  # noqa: BLE001
                pass
    return n


MARKER_ANY = re.compile(r"\[\[ent:([a-z_]{1,32}):([0-9a-f]{12})\]\]")


def never_captured_shape(value: str) -> Optional[str]:
    """The vault's regex net, for the classes that are never written (a card number)."""
    try:
        from anyuse.watcher.entity_vault import NEVER_CAPTURED, regex_net
        for _s, _e, _span, cls in regex_net(value):
            if cls in NEVER_CAPTURED:
                return cls
    except Exception:  # noqa: BLE001 - an unreadable net withholds
        return "unchecked"
    return None


def _scan_unanswered(exc: BaseException) -> bool:
    """The AX API did not answer the secure scan (``context.accessibility.AXScanError``)."""
    try:
        from anyuse.context.accessibility import AXScanError
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, AXScanError)


def _scan_error_code(exc: BaseException) -> str:
    """A scan error as a cause: the AX error code it names, else its exception's name (never a
    message's other text)."""
    m = re.search(r"-\d{4,6}", str(exc) or "")
    return m.group(0) if m else type(exc).__name__


def secure_cause(desc: Optional[Dict[str, Any]]) -> str:
    """Why a field counts as secure: its role, its subrole, else its label (``is_secure``)."""
    d = desc or {}
    if d.get("role") == SECURE:
        return "secure_found:role"
    if d.get("subrole") == SECURE:
        return "secure_found:subrole"
    return "label_secret"


# ---------------------------------------------------------------------------
# the recorder (pure logic over a probe)
# ---------------------------------------------------------------------------

@dataclass
class _Moved:
    """The text field a trigger focused, read ``FOCUS_SETTLE_S`` after it (memory only)."""
    el: Any
    desc: Dict[str, Any]
    initial: Optional[str] = None    # its value then, before any key reached it
    known: bool = False
    document: str = ""               # the AXDocument of its window


@dataclass
class _Trigger:
    """A step whose effect may have moved keyboard focus (``TRIGGER_ACTIONS``)."""
    ev: TagEvent
    front: Dict[str, Any]
    pre: Any                         # the element focused before it (memory only)
    read: bool = False               # the focus after it was read, or can no longer be
    moved: Optional[_Moved] = None
    at: float = 0.0                  # when it was emitted (the focus read retries up to FOCUS_WAIT_S)


@dataclass
class _Focus:
    el: Any
    desc: Dict[str, Any]
    front: Dict[str, Any]
    initial: Optional[str] = None    # in memory only: change detection, the input mode
    last: Optional[str] = None       # in memory only: the latest read, if the field vanishes
    dirty: bool = False
    secure: bool = False
    initial_known: bool = False      # the value before this focus span was read
    trigger: Optional[_Trigger] = None   # the step whose effect focused this field
    initial_via: str = ""            # how the value before was decided (diagnostics, no content)
    secure_via: str = ""             # "secure_input": secure only because secure event input was on


@dataclass
class _Click:
    front: Dict[str, Any]
    reason: Optional[str]
    el: Any
    desc: Optional[Dict[str, Any]]
    x: float
    y: float
    button: str
    clicks: int
    state_id: str
    moved: bool = False
    pre: Any = None                  # the element focused at mouse down (memory only)
    row: Optional[Dict[str, Any]] = None   # the list row the click landed in (``_row_at``)
    cause: str = ""                  # the guard's cause of ``reason`` (no content)


@dataclass
class _Segment:
    name: str
    app: str
    bundle_id: str
    steps: int = 0
    states: List[str] = field(default_factory=list)
    last_app: str = ""               # the app key of its last step
    guarded: bool = False            # it began at a guarded surface in another app


class StructuredStepRecorder:
    """Turns observed input events into STEP tags with AX identity.

    ``probe`` reads the AX world (``MacAXProbe`` live, a fake in tests): ``frontmost()``,
    ``element_at(x, y)``, ``focused_element()``, ``describe(el)``, ``parent(el)``,
    ``value(el)``, ``same(a, b)``, ``snapshot_descs(pid)``,
    ``menu_item_for_shortcut(pid, char, mods)``, and optionally ``row_facts(el)`` (the list row
    a click landed in), ``row_scope(row, app)`` (the folder / note list its window shows, in
    memory; stored only salted), ``document_of(el)`` (its window's AXDocument),
    ``open_menu_for_shortcut(pid, char, mods, read, ready)`` (a shortcut's item in a menu that is
    built only when it opens), ``app_version(pid)`` and ``menu_bar_front(el)`` (the frontmost app
    whose menu bar a clicked element sits in, also without a window). ``guard(front)``
    returns a content-free reason (see ``DROP`` / ``WITHHOLD``) or None. ``sink(TagEvent)``
    and ``state_sink(state_id, state_dict)`` receive the output. One thread calls every
    method. ``input_pending()`` (set by ``StepCapture``): an observed event is still queued,
    so the focus read after a trigger waits for it."""

    def __init__(self, probe, *, vault=None, guard: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                 sink: Optional[Callable[[TagEvent], None]] = None,
                 state_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 clock: Callable[[], float] = time.time, idle_split: Optional[float] = None,
                 split_apps: Optional[bool] = None, guard_cause: Optional[Callable[[], str]] = None):
        self.probe = probe
        self.vault = vault
        self.guard = guard
        #: the guard's sub-cause of its last reason (no content; ``BatchWatcher``: why a window
        #: counts as showing a secure field: key_unreadable, not_scanned_yet, scan_failed, found)
        self.guard_cause = guard_cause
        self.sink = sink or (lambda ev: None)
        self.state_sink = state_sink or (lambda sid, st: None)
        self.clock = clock
        self.idle_split = idle_split if idle_split is not None else idle_split_s()
        self.split_apps = split_by_app() if split_apps is None else bool(split_apps)
        self.input_pending: Optional[Callable[[], bool]] = None
        self.counts = {"steps": 0, "suppressed": 0, "withheld": 0, "secure": 0, "states": 0}
        #: per front-app source (``FRONT_SOURCES``, "unknown", "probe" for a probe that names none):
        #: how often each answered
        self.front_sources: Dict[str, int] = {}
        #: per withheld or secure step, its cause (no content): ``pid_none``, ``scan_error:<AX code or
        #: exception>``, ``secure_found:<role|subrole|unknown>``, ``label_secret``, ``guard:<reason>``,
        #: ``terminal``, else the withheld reason itself
        self.withhold_causes: Dict[str, int] = {}
        self._guard_cause = ""          # the cause of the reason ``_guard`` returned last
        #: per text-area typing withheld for its value before: how that value was decided (no
        #: content: role, a plain identifier, the source, whether the value was readable, its length)
        self.focus_diagnostics: List[Dict[str, Any]] = []
        self.scan_sleep: Callable[[float], None] = time.sleep
        self._focus: Optional[_Focus] = None
        self._click: Optional[_Click] = None
        self._segment: Optional[_Segment] = None
        self._segments = 0
        self._state_id = ""
        self._state_app = ""
        self._states_sent: set = set()
        self._last_step_at: Optional[float] = None
        self._last_sel: Optional[Dict[str, str]] = None
        self._outside_typing_for: Optional[str] = None
        self._last_nav: Optional[Tuple[str, str]] = None
        self._focused_other: Tuple[Any, Optional[Dict[str, Any]]] = (None, None)
        self._fp_due: Optional[float] = None
        self._val_due: Optional[float] = None
        self._secure_scan: Optional[Tuple[Any, float, Optional[str], str]] = None
        self._labels: Dict[Tuple[str, str], str] = {}     # (label, app) -> marker, memory only
        # the focused text field and its value at the last fingerprint (memory only): the
        # value before typing when focus is first seen at a key
        self._baseline: Optional[Tuple[Any, Optional[str]]] = None
        # short digit-only values just recorded (value, step, window title, time): memory
        # only, to catch a card number split over several fields
        self._digit_groups: List[Tuple[str, TagEvent, Any, float]] = []
        self._label_grant = False
        self._content_grant = False
        # the last step emitted, and the last one that may have moved the focus (memory only)
        self._last_ev: Optional[TagEvent] = None
        self._trigger: Optional[_Trigger] = None
        self._focus_due: Optional[float] = None
        self._bundles: Dict[Tuple[Any, str], str] = {}    # (pid, app) -> bundle id
        #: (app, version, char, AX modifiers) -> the (item, menu bar item) descriptions a menu opened to
        #: read a shortcut gave, None when it held no such item (memory only, for the recording)
        self._shortcut_menus: Dict[Tuple[str, str, str, int], Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]] = {}
        self.closed = False

    # -- timing (the worker asks; tests force) --------------------------------------------
    def start(self) -> None:
        self._fp_due = self.clock()

    def due_in(self, now: Optional[float] = None) -> Optional[float]:
        now = self.clock() if now is None else now
        due = [d - now for d in (self._fp_due, self._val_due, self._focus_due) if d is not None]
        return max(0.0, min(due)) if due else None

    def on_idle(self, now: Optional[float] = None, force: bool = False) -> None:
        now = self.clock() if now is None else now
        if self._val_due is not None and (force or now >= self._val_due):
            self._val_due = None
            self.refresh_value()
        if self._focus_due is not None and (force or now >= self._focus_due):
            self._focus_due = None
            self._read_trigger_focus()
        if self._fp_due is not None and (force or now >= self._fp_due):
            self._fp_due = None
            self.capture_state()

    def _structural(self) -> None:
        self._fp_due = self.clock() + FP_DELAY_S
        self._secure_scan = None         # the screen changes: the window's last scan is stale

    # -- context ----------------------------------------------------------------------------
    def _front(self) -> Dict[str, Any]:
        try:
            f = self.probe.frontmost() or {}
        except Exception:  # noqa: BLE001
            f = {}
        out = {"app": str(f.get("app") or ""), "bundle_id": str(f.get("bundle_id") or ""),
               "pid": f.get("pid"), "window_title": str(f.get("window_title") or ""),
               "front_source": str(f.get("front_source") or "")}
        # which source answered (memory only; a recording's diagnostics): "" is unknown
        key = out["front_source"] or ("unknown" if out["pid"] is None else "probe")
        self.front_sources[key] = self.front_sources.get(key, 0) + 1
        # every step carries its app's bundle id (a multi-app draft switches apps by it): a
        # front read that missed it takes the one this process was seen with
        key = (out["pid"], out["app"])
        if out["bundle_id"]:
            self._bundles[key] = out["bundle_id"]
        elif out["app"] and key in self._bundles:
            out["bundle_id"] = self._bundles[key]
        return out

    def _guard(self, front: Dict[str, Any], fresh: bool = False) -> Optional[str]:
        """The surface's reason not to read it, or None. ``fresh``: the window is scanned
        again for a secure field (a read of a value or of the focus right now)."""
        self._guard_cause = ""
        if self.guard is not None:
            try:
                reason = self.guard(front)
            except Exception:  # noqa: BLE001 - a broken guard drops (fail closed)
                self._guard_cause = "guard_error"
                return "denylist_app"
            if reason:
                try:
                    sub = str(self.guard_cause() or "") if self.guard_cause is not None else ""
                except Exception:  # noqa: BLE001
                    sub = ""
                self._guard_cause = f"guard:{reason}" + (f":{sub}" if sub else "")
                return reason
        if front.get("bundle_id") in TERMINAL_BUNDLES:
            self._guard_cause = "terminal"
            return "terminal"
        if front.get("pid") is None:
            # no source named the front app (``front_application``), or two disagreed: nothing of the
            # surface is read (fail closed); its window cannot be scanned either
            self._guard_cause = "pid_none"
            return "surface_unknown"
        return self._secure_visible(front, fresh=fresh)

    def _secure_visible(self, front: Dict[str, Any], fresh: bool = False) -> Optional[str]:
        """``secure_field`` while the focused window shows a secure field (a login form: its
        user name and every other step there keep only kind and role). The probe scans
        roles and subroles; the answer is reused for ``SECURE_SCAN_TTL_S`` per window, but
        never across a structural event or a trigger (a sheet in a window of the same title)
        nor for a read of a value or of the focus (``fresh``). A scan the AX API did not answer
        (``AXScanError`` only) is asked again up to ``SCAN_RETRIES`` times, ``SCAN_RETRY_S``
        apart; one that never answers withholds. A found secure field withholds at once. The cause
        is kept for the step (``_guard_cause``)."""
        rich = getattr(self.probe, "secure_scan", None)
        plain = getattr(self.probe, "secure_field_visible", None)
        if rich is None and plain is None:
            return None
        if front.get("windowless") is True:
            return None                  # an app whose window list answered empty: no window to show one
        key = (front.get("pid"), front.get("window_title"))
        now = self.clock()
        cached = self._secure_scan
        if not fresh and cached is not None and cached[0] == key and now - cached[1] < SECURE_SCAN_TTL_S:
            if cached[2]:
                self._guard_cause = cached[3]
            return cached[2]
        reason, cause = None, ""
        for attempt in range(SCAN_RETRIES + 1):
            try:
                found, kind = rich(front.get("pid")) if rich is not None else (bool(plain(front.get("pid"))), "unknown")
                reason, cause = ("secure_field", f"secure_found:{kind or 'unknown'}") if found else (None, "")
                break
            except Exception as exc:  # noqa: BLE001 - an unreadable window withholds
                if _scan_unanswered(exc) and attempt < SCAN_RETRIES:
                    self.scan_sleep(SCAN_RETRY_S)
                    continue
                reason, cause = "secure_field", f"scan_error:{_scan_error_code(exc)}"
                break
        self._secure_scan = (key, now, reason, cause)
        if reason:
            self._guard_cause = cause
        return reason

    def _drop(self, front: Optional[Dict[str, Any]] = None) -> None:
        self.counts["suppressed"] += 1
        self._focus = None              # its value is never read
        self._click = None
        self._trigger, self._focus_due = None, None
        if front is not None:
            self._guarded_detour(front)

    def _guarded_detour(self, front: Dict[str, Any], now: Optional[float] = None) -> None:
        """A guarded surface (here: a dropped event: a denylisted app such as a banking app or a
        password manager, a denylisted title) in another app than the recording's first or last
        step ends the recording there: a detour does not become part of a multi-app draft."""
        seg = self._segment
        if seg is not None and self._elsewhere(seg, front.get("bundle_id") or front.get("app") or ""):
            self._close_segment("guarded_surface", now)

    @staticmethod
    def _elsewhere(seg: _Segment, app_key: str) -> bool:
        """``app_key`` is another app than the recording's first or its last step's."""
        return bool(seg.last_app) and app_key not in ((seg.bundle_id or seg.app),) or (
            bool(seg.last_app) and app_key != seg.last_app)

    def _menu_bar_owner(self, el, front: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The front for a click on ``el`` when it sits in the menu bar of the frontmost app
        (``probe.menu_bar_front``: that app's own menu bar, its AX element frontmost, its name read,
        ``windowless`` when its window list answered empty), and the front read named no window:
        no app (pid None), or the same app. None otherwise (a menu extra of another process, a
        probe without the read)."""
        read = getattr(self.probe, "menu_bar_front", None)
        if read is None:
            return None
        try:
            owner = read(el)
        except Exception:  # noqa: BLE001
            return None
        if not owner or owner.get("pid") is None or not (owner.get("app") or owner.get("bundle_id")):
            return None
        if front.get("pid") is not None and front.get("pid") != owner["pid"]:
            return None
        out = {"app": str(owner.get("app") or ""), "bundle_id": str(owner.get("bundle_id") or ""),
               "pid": owner["pid"], "window_title": "", "front_source": "menubar_element",
               "windowless": owner.get("windowless") is True}
        self.front_sources["menubar_element"] = self.front_sources.get("menubar_element", 0) + 1
        if out["bundle_id"]:
            self._bundles[(out["pid"], out["app"])] = out["bundle_id"]
        return out

    def _in_front(self, el, front: Dict[str, Any]) -> bool:
        """Is ``el`` in the front app the guard judged (``probe.pid_of``)? The system-wide focus
        can belong to another app after a switch between the front read and the focus read.
        A probe without the pid read is trusted; a pid that cannot be read is not."""
        pid_of = getattr(self.probe, "pid_of", None)
        if pid_of is None or front.get("pid") is None:
            return True
        try:
            return pid_of(el) == front.get("pid")
        except Exception:  # noqa: BLE001
            return False

    def _read_value(self, el, front: Dict[str, Any]) -> Optional[str]:
        """A field's value, read only when the field is in the guarded front app; None when it
        cannot be read. The caller has asked the guard."""
        if not self._in_front(el, front):
            return None
        try:
            return self.probe.value(el)
        except Exception:  # noqa: BLE001
            return None

    def _describe(self, el) -> Optional[Dict[str, Any]]:
        if el is None:
            return None
        try:
            return self.probe.describe(el)
        except Exception:  # noqa: BLE001
            return None

    def _actionable_target(self, el, desc) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """The element itself when its role is actionable, else the nearest actionable
        ancestor (a label inside a button), at most four levels up."""
        cur, d = el, desc
        for _ in range(5):
            if d is None:
                return el, desc
            if _actionable(d.get("role")):
                return cur, d
            try:
                cur = self.probe.parent(cur)
            except Exception:  # noqa: BLE001
                cur = None
            if cur is None:
                break
            d = self._describe(cur)
        return el, desc

    def _menu_bar_item(self, el) -> Tuple[Any, Optional[Dict[str, Any]]]:
        cur = el
        for _ in range(4):
            try:
                cur = self.probe.parent(cur)
            except Exception:  # noqa: BLE001
                return None, None
            if cur is None:
                return None, None
            d = self._describe(cur)
            if d and d.get("role") == "AXMenuBarItem":
                return cur, d
        return None, None

    def _tok(self, value: str, cls: str, app: str) -> Optional[str]:
        if not value or self.vault is None:
            return None
        try:
            from anyuse.watcher.entity_vault import marker
            return marker(cls, self.vault.tokenize(value, cls, app))
        except Exception as exc:  # noqa: BLE001 - no token, no value
            logger.warning("step capture: tokenize failed (%s); value withheld", type(exc).__name__)
            return None

    def _label(self, text: Optional[str], app: str) -> str:
        """A control's title / description as a ``ui_label`` marker (the standing grant); ""
        without a vault, and "" for a text with machine-shaped personal data in it
        (``content_problem``: a UI label never holds one)."""
        text = (text or "").strip()
        if not text:
            return ""
        key = (text, app)
        if key not in self._labels:
            if content_problem(text) is not None:
                return ""
            if self.vault is not None and not self._label_grant:
                ensure_label_grant(self.vault)
                self._label_grant = True
            if len(self._labels) >= MAX_LABEL_CACHE:
                self._labels.clear()
            self._labels[key] = self._tok(text, LABEL_CLASS, app) or ""
        return self._labels[key]

    def _title_of(self, text: Optional[str], role: Optional[str], app: str) -> str:
        """An element's title / description as stored: a content role's as ``content_label``
        (``_content_label``), any other's as ``ui_label`` (``_label``)."""
        return self._content_label(text, app) if role in CONTENT_ROLES else self._label(text, app)

    def _identifier_of(self, ident: Optional[str], app: str) -> str:
        """An AX identifier with personal data in it (the vault's regex net) as presence-tier
        content, never a standing-grant label."""
        ident = (ident or "").strip()
        if not ident:
            return ""
        if self.vault is not None and not self._content_grant:
            ensure_label_grant(self.vault, content=True)
            self._content_grant = self._label_grant = True
        return self._tok(ident, CONTENT_CLASS, app) or ""

    def _content_label(self, text: Optional[str], app: str) -> str:
        """Content a target is found by (a list row's label, a list's identifier or description)
        as a ``content_label`` marker (presence-tier); "" when it holds machine-shaped personal
        data (``content_problem``: the field is dropped, the step stays a gap) or without a
        vault. Written per occurrence, never cached: no plaintext stays in memory."""
        text = (text or "").strip()
        if not text or content_problem(text) is not None:
            return ""
        if self.vault is not None and not self._content_grant:
            ensure_label_grant(self.vault, content=True)
            self._content_grant = self._label_grant = True
        return self._tok(text, CONTENT_CLASS, app) or ""

    def forget_cached(self, tokens) -> None:
        """Forget the UI labels whose vault entries were deleted (``withhold_events``): the next
        use of that text writes it again."""
        gone = {f"[[ent:{LABEL_CLASS}:{t}]]" for t in tokens or ()}
        for key in [k for k, m in self._labels.items() if m in gone]:
            self._labels.pop(key, None)

    def _vaulted(self, desc: Dict[str, Any], app: str) -> Dict[str, Any]:
        """The element description as stored: title and description as markers (``_title_of``:
        a control's as ``ui_label``, a link's or static text's as ``content_label``; a row's or
        cell's not at all - its label is the selector's name), an identifier with personal data
        in it as ``content_label``; role, subrole, plain identifier, path and frame as they are."""
        out = dict(desc)
        role = desc.get("role")
        if role in ROW_CELL_ROLES:
            out["title"] = out["description"] = ""
        else:
            out["title"] = self._title_of(desc.get("title"), role, app)
            out["description"] = self._title_of(desc.get("description"), role, app)
        ident = str(desc.get("identifier") or "")
        if identifier_is_content(ident):
            out["identifier"] = self._identifier_of(ident, app)
        out.pop("label_hint", None)
        out.pop("title_hint", None)
        return out

    # -- recordings -------------------------------------------------------------------------
    def _ensure_segment(self, front: Dict[str, Any], now: float, guarded: bool = False) -> _Segment:
        seg = self._segment
        app_key = front["bundle_id"] or front["app"]
        # a step in another app stays in the recording (it carries its own app: the draft is a
        # multi-app one), unless the per-app split is on; a guarded surface (``guarded``: a
        # withheld or secure step) in another app than the recording's first or last ends it,
        # and so does leaving a recording that began at one
        switched = seg is not None and self.split_apps and (seg.bundle_id or seg.app) != app_key
        idle = seg is not None and self._last_step_at is not None and now - self._last_step_at > self.idle_split
        detour = seg is not None and ((guarded and self._elsewhere(seg, app_key))
                                      or (seg.guarded and bool(seg.last_app) and app_key != seg.last_app))
        if switched or idle or detour:
            self._close_segment("app_switch" if switched else "idle" if idle else "guarded_surface", now)
            seg = None
        if seg is None:
            self._segments += 1
            seg = _Segment(name=f"steps_{to_snake_case(front['app'] or 'app')[:40]}_{self._segments}",
                           app=front["app"], bundle_id=front["bundle_id"], guarded=guarded)
            self._segment = seg
            self.sink(TagEvent(id=uuid.uuid4().hex[:12], tag_type=TagType.WORKFLOW_START,
                               timestamp=datetime.fromtimestamp(now), workflow_name=seg.name,
                               confidence=1.0,
                               data={"goal": f"steps recorded in {front['app'] or 'an app'}",
                                     "source": SOURCE, "app": front["app"],
                                     "bundle_id": front["bundle_id"]}))
        return seg

    def _close_segment(self, why: str, now: Optional[float] = None) -> None:
        seg, self._segment = self._segment, None
        if seg is None:
            return
        now = self.clock() if now is None else now
        self.sink(TagEvent(id=uuid.uuid4().hex[:12], tag_type=TagType.WORKFLOW_END,
                           timestamp=datetime.fromtimestamp(now), workflow_name=seg.name, confidence=1.0,
                           data={"source": SOURCE, "ended_by": why, "steps": seg.steps}))
        self._last_sel = None

    def _emit(self, front: Dict[str, Any], action: str, desc: Optional[Dict[str, Any]], *,
              state_id: Optional[str] = None, withheld: Optional[str] = None, secure: bool = False,
              identity: bool = True, extra: Optional[Dict[str, Any]] = None, cause: str = "") -> TagEvent:
        now = self.clock()
        if secure or withheld:
            # the cause of the withhold (no content): the guard's for a surface, the field's for a
            # secure step, else the reason itself
            key = (cause or secure_cause(desc)) if secure else (cause or str(withheld))
            self.withhold_causes[key] = self.withhold_causes.get(key, 0) + 1
        guarded = secure or bool(withheld and (withheld in WITHHOLD or withheld.startswith("never_captured")))
        seg = self._ensure_segment(front, now, guarded=guarded)
        seg.last_app = front["bundle_id"] or front["app"]
        data: Dict[str, Any] = {"action": action, "source": SOURCE, "app": front["app"],
                                "bundle_id": front["bundle_id"]}
        role = (desc or {}).get("role") or ""
        stored_target = target_text(desc)
        if secure:
            self.counts["secure"] += 1
            data["secure"] = True
            data["selector"] = {"canonical_role": "passwordfield", "raw_role": SECURE,
                                "name": "", "identifier": "", "path": ""}
            data["target"] = {"role": role or SECURE}
            stored_target = "secure text field"
        elif withheld:
            self.counts["withheld"] += 1
            data["withheld"] = withheld
            data["target"] = {"role": role} if role else {}
            stored_target = f"{role or 'element'} (withheld: {withheld})"
        else:
            title_ref = self._tok(front["window_title"], TITLE_CLASS, front["app"])
            if title_ref:
                data["window_title_ref"] = title_ref
            if desc is not None:
                vdesc = self._vaulted(desc, front["app"])
                data["target"] = target_of(vdesc)
                if identity and _actionable(desc.get("role")):
                    data["selector"] = selector_of(vdesc)
        if extra:
            data.update(extra)
        sid = self._state_id if state_id is None else state_id
        if sid and self._state_app != front["app"]:
            sid = ""
        if withheld or secure:
            sid = ""                     # no AX snapshot of a withheld surface
        ev = TagEvent(id=uuid.uuid4().hex[:12], tag_type=TagType.STEP,
                      timestamp=datetime.fromtimestamp(now), data=data, target=stored_target,
                      workflow_name=seg.name, confidence=1.0, ax_state_id=sid or None)
        seg.steps += 1
        if sid and sid not in seg.states:
            seg.states.append(sid)
        self.counts["steps"] += 1
        self._last_step_at = now
        self._last_ev = ev
        self._last_sel = data.get("selector")
        self._outside_typing_for = None
        if not (action == "key" and self._last_nav and data.get("keys") == self._last_nav[0]):
            self._last_nav = None
        self.sink(ev)
        return ev

    # -- the field a trigger focused ----------------------------------------------------------
    # A click, a menu item or a key (``TRIGGER_ACTIONS``) may move keyboard focus into a text
    # field (a "New Folder" button into the folder's name field). ``FOCUS_SETTLE_S`` after the
    # step, on this worker thread and only where the guard allows reading, AXFocusedUIElement
    # is read once: a text field other than the one focused before the trigger is the field the
    # trigger focused, and its value then is the value before typing. A typing step into that
    # field, when the trigger is still the step right before it and the field has no name or
    # identifier of its own, is recorded as ``focused`` (``_focused_extra``). An event the
    # worker has not processed yet (``input_pending``) may have moved the focus already: the
    # read waits for it; a key typed into the field reads the focus then, without its value; a
    # click, Tab, Return, Escape, an arrow or a shortcut ends the wait unread.

    def _arm(self, ev: Optional[TagEvent], front: Dict[str, Any], pre: Any) -> None:
        d = ev.data if ev is not None else {}
        if ev is None or d.get("action") not in TRIGGER_ACTIONS or d.get("withheld") or d.get("secure"):
            self._trigger, self._focus_due = None, None
            return
        now = self.clock()
        self._trigger = _Trigger(ev=ev, front=front, pre=pre, at=now)
        self._focus_due = now + FOCUS_SETTLE_S
        self._secure_scan = None         # its effect may open a login sheet in the same window

    def _cancel_trigger_read(self) -> None:
        t = self._trigger
        if t is not None and not t.read:
            t.read = True
        self._focus_due = None

    def _read_focused(self):
        try:
            return self.probe.focused_element()
        except Exception:  # noqa: BLE001
            return None

    def _read_trigger_focus(self, at_key: bool = False) -> None:
        t = self._trigger
        if t is None or t.read or t.ev is not self._last_ev:
            return
        if not at_key and self.input_pending is not None:
            try:
                pending = bool(self.input_pending())
            except Exception:  # noqa: BLE001
                pending = True
            if pending:
                return                   # the next event decides (a key reads it, a click ends it)
        t.read = True
        front = self._front()
        if ((front["bundle_id"] or front["app"]) != (t.front["bundle_id"] or t.front["app"])
                or self._guard(front, fresh=True)):
            return                       # another app, or a surface that is not read (scanned now)
        if not at_key and self._pending():
            t.read = False               # input arrived while the guard decided: the next event reads it
            return
        fel = self._read_focused()
        if fel is not None and not self._in_front(fel, front):
            return                       # another process's element: never the trigger's field
        desc = None
        if fel is not None and not (t.pre is not None and self._same(fel, t.pre)):
            desc = self._describe(fel)
        elif fel is not None and not at_key:
            # the focus stayed on the field it was on (an app that reuses one text view for each new
            # item, as Notes may): no "focused" field, but its value now is the value before typing
            # (the baseline, memory only; refreshed on each read again)
            self._take_baseline(front)
        if desc is None or not is_text_entry(desc):
            # nothing focused, the focus did not move (yet), or no text field: read again a little
            # later while no input arrived (an app that focuses its field late)
            if not at_key and self.clock() - t.at < FOCUS_WAIT_S:
                t.read = False
                self._focus_due = self.clock() + FOCUS_RETRY_S
            return
        if is_secure(desc):
            return
        moved = _Moved(el=fel, desc=desc)
        if not at_key:
            v = self._read_value(fel, front)
            moved.initial = v if v is None or len(v) <= MAX_INITIAL else None
            moved.known = moved.initial is not None
        doc = getattr(self.probe, "document_of", None)
        if doc is not None:
            try:
                # the window's document only as a salted key (the URL is not kept)
                from anyuse.context.accessibility import document_key
                moved.document = document_key(doc(fel), create=True)
            except Exception:  # noqa: BLE001
                moved.document = ""
        t.moved = moved

    def _pending(self) -> bool:
        """An observed event is still queued (unknown counts as queued)."""
        if self.input_pending is None:
            return False
        try:
            return bool(self.input_pending())
        except Exception:  # noqa: BLE001
            return True

    def _trigger_for(self, el) -> Optional[_Trigger]:
        """The trigger whose effect focused ``el``, if it is still the last step."""
        t = self._trigger
        if t is None or t.ev is not self._last_ev:
            return None
        if not t.read:
            self._read_trigger_focus(at_key=True)
        return t if t.moved is not None and self._same(t.moved.el, el) else None

    def _focused_extra(self, f: _Focus) -> Dict[str, Any]:
        """A typing step's ``focused`` data: its field has no name or identifier of its own and
        the step right before it focused it. The selector kind ``focused`` names the field's
        role, the app's bundle id and its window's document (a salted key, never the URL)."""
        t = f.trigger
        if (t is None or t.moved is None or t.ev is not self._last_ev
                or (own_identity(f.desc) and not outside_windows(f.desc))):
            return {}
        from anyuse.context.accessibility import canonical_role
        raw = str(f.desc.get("role") or "")
        sel: Dict[str, Any] = {"kind": "focused", "canonical_role": canonical_role(raw), "raw_role": raw,
                               "name": "", "identifier": "", "path": ""}
        if f.front["bundle_id"]:
            sel["app"] = f.front["bundle_id"]
        if t.moved.document:
            sel["document"] = t.moved.document
        return {"focused": True, "focused_after": t.ev.id, "role": raw, "selector": sel}

    # -- focus and text ---------------------------------------------------------------------
    def _secure_input(self) -> bool:
        """Secure event input is on (some app's password field has the keyboard; the probe's
        ``secure_input_enabled``); an unreadable answer is on. A probe without it: off."""
        read = getattr(self.probe, "secure_input_enabled", None)
        if read is None:
            return False
        try:
            return bool(read())
        except Exception:  # noqa: BLE001
            return True

    def _start_focus(self, el, desc, front, initial_known: bool) -> None:
        # a field outside every window (an app's field editor) is in no window the secure scan walks:
        # while secure event input is on, it is taken as a secure field
        by_input = not is_secure(desc) and outside_windows(desc) and self._secure_input()
        secure = is_secure(desc) or by_input
        initial, known, via = None, False, "guarded"
        if not secure and not self._guard(front, fresh=initial_known):
            if initial_known:
                initial = self._read_value(el, front)
                known = initial is not None
                via = "click_read" if known else "click_read_unreadable"
            elif self._baseline is not None and self._same(self._baseline[0], el):
                initial = self._baseline[1]
                known = initial is not None
                via = "baseline"
            else:
                via = "no_baseline" if self._baseline is None else "baseline_other_element"
        if initial is not None and len(initial) > MAX_INITIAL:
            initial, known, via = None, False, "too_long"     # too long to hold: the value before is unknown
        self._focus = _Focus(el=el, desc=desc, front=front, initial=initial, secure=secure,
                             initial_known=known, initial_via=via, secure_via="secure_input" if by_input else "")

    def _sync_focus(self, front: Dict[str, Any]) -> Optional[_Focus]:
        try:
            fel = self.probe.focused_element()
        except Exception:  # noqa: BLE001
            fel = None
        if fel is not None and not self._in_front(fel, front):
            fel = None                   # another process's element (a panel, a banner): not this app's
        if self._focus is not None and fel is not None and self._same(fel, self._focus.el):
            return self._focus
        if self._focus is not None:
            self._commit_focus()
            self._structural()
        if fel is None:
            return None
        desc = self._describe(fel)
        if is_text_entry(desc) or is_secure(desc):
            # first seen at a key event: that key may already be in the value, so the
            # value before typing is unknown - unless the focus read after the trigger that
            # focused this field took it before any key
            trig = self._trigger_for(fel)
            self._start_focus(fel, desc, front, initial_known=False)
            f = self._focus
            if trig is not None and f is not None and not f.secure:
                f.trigger = trig
                if trig.moved is not None and trig.moved.known:
                    f.initial, f.initial_known, f.initial_via = trig.moved.initial, True, "trigger_read"
                elif not f.initial_known:
                    f.initial_via = "trigger_value_unread"
            if trig is not None and trig.moved is not None:
                trig.moved.initial = None      # copied: not held twice
        else:
            self._focused_other = (fel, desc)
        return self._focus

    def _same(self, a, b) -> bool:
        try:
            return bool(self.probe.same(a, b))
        except Exception:  # noqa: BLE001
            return False

    def _commit_focus(self) -> None:
        f, self._focus = self._focus, None
        if f is None or not f.dirty:
            return
        reason = self._guard(f.front, fresh=not f.secure)
        cause = self._guard_cause
        if reason in DROP:
            self.counts["suppressed"] += 1
            return
        if f.secure:
            self._emit(f.front, "type", f.desc, secure=True, cause=f.secure_via)
            return
        if reason:
            self._emit(f.front, "type", f.desc, withheld=reason, cause=cause)
            return
        value = self._read_value(f.el, f.front)
        if value is None:
            value = f.last
        if value is None:
            self._emit(f.front, "type", f.desc, withheld="value_unreadable")
            return
        self._baseline = (f.el, value) if len(value) <= MAX_INITIAL else None
        if value == f.initial or value == "":
            return                       # unchanged, or cleared: nothing to replay
        # how the value came from the one the field held when focus entered it (both in memory
        # only): a suffix appended, one line replaced, or anything else (the whole value)
        multiline = f.desc.get("role") in MULTILINE_ROLES
        mode, text, anchor = "set", value, ""
        if f.initial_known and f.initial:
            mode, text, anchor = input_mode_of(f.initial, value, multiline)
        if multiline and mode == "set" and (not f.initial_known or f.initial):
            # a text area's value is the whole text, not what was typed now: with text in it
            # before (an open document, a mail thread) or an unknown before, the step would
            # store that text and replay it over another document. Only an append or a
            # one-line edit is a delta the runner replays; anything else is withheld.
            if len(self.focus_diagnostics) < 20:
                ident = str(f.desc.get("identifier") or "")
                self.focus_diagnostics.append({
                    "cause": "text_area_had_text" if f.initial_known else "text_area_initial_unknown",
                    "role": str(f.desc.get("role") or ""),
                    "identifier": "" if (identifier_is_content(ident) or LABEL_MARKER.fullmatch(ident)) else ident[:80],
                    "initial_via": f.initial_via, "trigger": f.trigger is not None,
                    "value_readable": value is not None, "length": len(value)})
            self._emit(f.front, "type", f.desc, withheld="text_area_had_text" if f.initial_known
                       else "text_area_initial_unknown")
            return
        if len(text) > MAX_VALUE:
            self._emit(f.front, "type", f.desc, withheld="too_long")
            return
        shape = payment_shape(value, f.desc) or (payment_shape(text) if text != value else None)
        if shape:
            self._emit(f.front, "type", f.desc, withheld=f"never_captured:{shape}")
            return
        if self._split_card(text, f.front, f.desc):
            return
        ref = self._tok(text, VALUE_CLASS, f.front["app"])
        if ref is None:
            self._emit(f.front, "type", f.desc, withheld="no_vault")
            return
        extra: Dict[str, Any] = {"value_ref": ref}
        if mode != "set":
            extra["input_mode"] = mode
            # the anchor is document text: a typed value's class (presence-tier), never released;
            # the compiler makes it a parameter given at run time
            anchor_ref = (self._tok(anchor, VALUE_CLASS, f.front["app"]) or "") if anchor else ""
            if anchor_ref:
                extra["anchor"] = anchor_ref
            # the start value's shape for the proof (no text): line count, the anchor's line
            extra["start"] = start_shape(f.initial or "", {anchor_ref: anchor} if anchor_ref else None)
        extra.update(self._focused_extra(f))
        ev = self._emit(f.front, "type", f.desc, extra=extra)
        if re.fullmatch(r"\d{3,6}", text.strip()):
            self._digit_groups.append((text.strip(), ev, f.front.get("window_title"), self.clock()))
            self._digit_groups = self._digit_groups[-4:]

    def _split_card(self, value: str, front: Dict[str, Any], desc: Optional[Dict[str, Any]] = None) -> bool:
        """A card number typed into several short fields: each group alone is just digits.
        When this group and the ones typed just before it in the same window (within two
        minutes) join into a Luhn-valid 13-19 digit number, every part is withheld - the
        earlier steps rewritten, their vault entries forgotten."""
        v = value.strip()
        if not re.fullmatch(r"\d{3,6}", v):
            return False
        now, title = self.clock(), front.get("window_title")
        recent = [g for g in self._digit_groups if g[2] == title and now - g[3] < 120.0]
        for k in range(1, len(recent) + 1):
            joined = "".join(g[0] for g in recent[-k:]) + v
            if 13 <= len(joined) <= 19 and luhn_ok(joined):
                parts = {id(g[1]) for g in recent[-k:]}
                # only these steps are at hand: their values and window titles are forgotten, a
                # UI label other steps of the session may name stays
                withhold_events([g[1] for g in recent[-k:]], lambda e: id(e) in parts,
                                "never_captured:payment_card", vault=self.vault,
                                classes=frozenset({VALUE_CLASS, TITLE_CLASS}))
                self._digit_groups = [g for g in self._digit_groups if id(g[1]) not in parts]
                self._emit(front, "type", desc, withheld="never_captured:payment_card")
                return True
        return False

    def refresh_value(self) -> None:
        f = self._focus
        if f is None or f.secure or not f.dirty or self._guard(f.front):
            return
        v = self._read_value(f.el, f.front)
        if v is not None:
            f.last = v if len(v) <= MAX_INITIAL else None

    def capture_state(self) -> None:
        front = self._front()
        if self._guard(front):
            return                       # no AX snapshot of an excluded or withheld surface
        try:
            descs = self.probe.snapshot_descs(front["pid"])
        except Exception:  # noqa: BLE001
            descs = []
        if not descs:
            return
        # the titles and AX identifiers the fingerprint requires (actionable elements outside
        # the menu bar) are stored as label markers ("" for none); the role counts and the hash
        # read roles only
        from anyuse.context.accessibility import ACTIONABLE_ROLES
        app = front["app"]

        def stored(d: Dict[str, Any]) -> Dict[str, Any]:
            # a content role's title (a webmail subject or sender) is presence-tier content, a
            # title or identifier with personal data in it is not kept as a UI label
            if d.get("role") not in ACTIONABLE_ROLES:
                return d
            out = dict(d, title=self._title_of(d.get("title"), d.get("role"), app)) if d.get("title") else dict(d)
            if "identifier" in d:
                ident = str(d.get("identifier") or "")
                out["identifier"] = ("" if d.get("in_menubar") else
                                     self._identifier_of(ident, app) if content_problem(ident) else
                                     self._label(ident, app))
            return out
        descs = [stored(d) for d in descs]
        sid, state = fingerprint_from_descs(descs, front["app"])
        self._state_id, self._state_app = sid, front["app"]
        self._take_baseline(front)
        if sid not in self._states_sent:
            self._states_sent.add(sid)
            self.counts["states"] += 1
            # when it was taken: the phishing reach-back drops the flagged app's fingerprints of its
            # hold window (``withhold_events``), referenced or not
            self.state_sink(sid, dict(state, captured_at=round(self.clock(), 3)))

    def _take_baseline(self, front: Dict[str, Any]) -> None:
        """The focused text field's value now, before anyone types into it (memory only)."""
        fel = self._read_focused()
        if fel is not None and not self._in_front(fel, front):
            return                       # another process's element: not described, not read
        desc = self._describe(fel) if fel is not None else None
        if desc is None or not is_text_entry(desc) or is_secure(desc):
            return
        if self._focus is not None and self._same(self._focus.el, fel) and self._focus.dirty:
            return                       # typing has started: this is no "before"
        if self._guard(front, fresh=True):
            return                       # the window is scanned again before a value is read
        if self._pending():
            return                       # input arrived while the guard decided: no "before" now
        v = self._read_value(fel, front)
        self._baseline = (fel, v) if v is not None and len(v) <= MAX_INITIAL else None

    # -- mouse ------------------------------------------------------------------------------
    def on_mouse_down(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        self._flush_click()
        self._cancel_trigger_read()      # this click may move the focus itself
        self._focused_other = (None, None)   # ... so what had focus before it is not known now
        front = self._front()
        # the hit test first: what was under the pointer at the click, before the guard's AX reads
        # and its bounded wait (a sheet the click closes is gone after them: the click would land on
        # the window behind, measured in the guest). Only a reference: nothing of it is
        # read before the guard decides
        try:
            el = self.probe.element_at(float(x), float(y))
        except Exception:  # noqa: BLE001
            el = None
        reason = self._guard(front)
        cause = self._guard_cause
        if reason in DROP:
            self._drop(front)
            return
        owner = self._menu_bar_owner(el, front) if el is not None and not front.get("window_title") else None
        if owner is not None:
            # an app with no window is named by no front source (the window server shows
            # none of its windows; off the main thread NSWorkspace is stale): a click in its menu bar names
            # it, the menu bar's owner, and a verified windowless app has no window to show a secure field
            front = owner
            reason = self._guard(front)
            cause = self._guard_cause
            if reason in DROP:
                self._drop(front)
                return
        if el is not None and not self._in_front(el, front):
            # the click may have just brought its app to the front (the first read came before the
            # switch): read it once more before calling it another process's panel
            again = self._front()
            if again.get("pid") is not None and self._in_front(el, again):
                front = again
                reason = self._guard(front)
                cause = self._guard_cause
                if reason in DROP:
                    self._drop(front)
                    return
        if el is not None and not self._in_front(el, front):
            # the hit test is system-wide: a panel of another process (a password manager's
            # menu extra, a notification banner) is not this app's step; nothing of it is read
            self.counts["suppressed"] += 1
            return
        desc = self._describe(el)
        f = self._focus
        if f is not None and el is not None and button == "left" and self._inside(el, f.el):
            return                       # a click inside the focused field moves the caret
        # the element focused before this click (memory only): the click's effect moved the
        # focus only when the field focused after it is another one
        pre = None if reason else (f.el if f is not None else self._read_focused())
        self._commit_focus()
        text_entry = button == "left" and (is_text_entry(desc) or is_secure(desc))
        tel, tdesc = (el, desc) if text_entry else self._actionable_target(el, desc)
        row = None
        if (button == "left" and int(clicks or 1) < 2 and not reason and not is_secure(desc)
                and (text_entry or not _actionable((tdesc or {}).get("role")))):
            row = self._row_at(el)
            if row is not None:
                row["scope"] = self._row_scope(front, row)
        if text_entry:
            t = self._trigger
            self._start_focus(el, desc, front, initial_known=True)
            nf = self._focus
            if (t is not None and t.moved is not None and t.ev is self._last_ev and nf is not None
                    and not nf.secure and self._same(el, t.moved.el)):
                nf.trigger = t           # a click into the field the trigger focused: still that field
            if row is None:
                return
            # a text field in a list's row (Finder's name cell): the click selects that row too
        if row is not None:
            tel, tdesc = row["el"], row["desc"]
        self._click = _Click(front=front, reason=reason, el=tel, desc=tdesc, x=float(x), y=float(y),
                             button=button, clicks=int(clicks or 1), state_id=self._state_id, pre=pre,
                             row=row, cause=cause)
        if button != "left":
            self._flush_click()

    def _row_at(self, el) -> Optional[Dict[str, Any]]:
        """The list row a click landed in: ``el`` is a row, a cell of one, or inside a cell (its
        text, image or name field) and no control of its own. ``probe.row_facts`` reads the row,
        the cell and its column, the row's label (its first cell's text) and its list's
        identity; None outside a row (or for a probe without it)."""
        facts_of = getattr(self.probe, "row_facts", None)
        if el is None or facts_of is None:
            return None
        try:
            facts = facts_of(el)
        except Exception:  # noqa: BLE001
            return None
        if not facts or facts.get("row") is None:
            return None
        tel = facts.get("cell") if facts.get("cell") is not None else facts["row"]
        tdesc = self._describe(tel)
        if tdesc is None:
            return None
        col = facts.get("column")
        return {"el": tel, "desc": tdesc, "column": col if isinstance(col, int) else None, "row_el": facts["row"],
                "label": str(facts.get("label") or ""), "container": dict(facts.get("container") or {})}

    def _row_scope(self, front: Dict[str, Any], row: Dict[str, Any]) -> str:
        """The scope of the window a clicked row is in (which folder or note list it shows), as
        the container scope module's key salted on this machine (``container_scope.scope_key``
        via ``ax_actions.scope_key``), or "". Read at the click, only from the guarded front app
        (``_in_front``) after a fresh secure-field scan (the window is read), and only when the
        module is plugged in; the plaintext scope (a folder's path, name or title) never leaves
        this call."""
        from anyuse.execution.ax_actions import _scope_api, scope_key
        reader = getattr(self.probe, "row_scope", None)
        row_el = row.get("row_el") if row.get("row_el") is not None else row.get("el")
        if (reader is None or row_el is None or _scope_api() is None or self._guard(front, fresh=True)
                or not self._in_front(row_el, front)):
            return ""
        try:
            scope = reader(row_el, front.get("bundle_id") or None)
        except Exception:  # noqa: BLE001
            return ""
        return scope_key(scope, create=True) if scope else ""

    def _row_extra(self, front: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
        """A row click's data: the list's identity (``container``: role, identifier and
        description as content markers, path below the window, column count), the cell's
        ``column``, and - when the row has a label - a selector named by that label (its
        first cell's text, a marker), with ``scope`` (the salted key of the folder / note list
        the window showed at the click), that ``draft_compile`` compiles to ``select_row``."""
        app = front["app"]
        box = row.get("container") or {}
        container: Dict[str, Any] = {"role": str(box.get("role") or "")[:40],
                                     "identifier": self._content_label(box.get("identifier"), app),
                                     "description": self._content_label(box.get("description"), app),
                                     "path": str(box.get("path") or "")[:200]}
        if isinstance(box.get("columns"), int) and not isinstance(box.get("columns"), bool):
            container["columns"] = box["columns"]
        extra: Dict[str, Any] = {"container": container}
        if row.get("column") is not None:
            extra["column"] = int(row["column"])
        label = self._content_label(row.get("label"), app)
        if label:
            sel = selector_of(self._vaulted(row["desc"], app))
            sel["name"] = label
            if row.get("scope"):
                sel["scope"] = str(row["scope"])        # the window's container scope, salted at the click
            extra["selector"] = sel
        return extra

    def _inside(self, el, ancestor) -> bool:
        cur = el
        for _ in range(4):
            if cur is None:
                return False
            if self._same(cur, ancestor):
                return True
            try:
                cur = self.probe.parent(cur)
            except Exception:  # noqa: BLE001
                return False
        return False

    def on_mouse_dragged(self, x: float, y: float) -> None:
        c = self._click
        if c is not None and (abs(float(x) - c.x) > 4 or abs(float(y) - c.y) > 4):
            c.moved = True

    def on_mouse_up(self, x: float, y: float) -> None:
        self._flush_click()

    def _flush_click(self) -> None:
        c, self._click = self._click, None
        if c is None:
            return
        self._structural()
        if c.reason:
            self._trigger, self._focus_due = None, None
            self._emit(c.front, "click", c.desc, withheld=c.reason, cause=c.cause)
            return
        if c.moved:
            self._emit(c.front, "drag", c.desc, state_id=c.state_id, identity=False)
            return
        if c.button != "left":
            self._emit(c.front, "right_click", c.desc, state_id=c.state_id, identity=False)
            return
        if c.clicks >= 2:
            self._emit(c.front, "double_click", c.desc, state_id=c.state_id, identity=False)
            return
        role = (c.desc or {}).get("role") or ""
        if role == "AXMenuItem" and (c.desc or {}).get("in_menubar"):
            bar_el, bar = self._menu_bar_item(c.el)
            ev = self._emit_menu_choice(c.front, c.desc, bar, state_id=c.state_id)
        elif c.row is not None:
            ev = self._emit(c.front, "click", c.desc, state_id=c.state_id, extra=self._row_extra(c.front, c.row))
        else:
            ev = self._emit(c.front, _STEP_VERB.get(role, "click"), c.desc, state_id=c.state_id)
        self._arm(ev, c.front, c.pre)

    def _emit_menu_choice(self, front, item_desc, bar_desc, *, state_id: Optional[str] = None,
                          via: str = "", opened: bool = False) -> TagEvent:
        """A menu bar item's menu holds its items in the AX tree only once it was opened, so a
        replay presses the menu bar item first: inserted when the step before was not it.
        ``opened``: the item was read from a menu the recorder opened (``menu_opened``)."""
        if bar_desc is not None:
            bar_sel = selector_of(self._vaulted(bar_desc, front["app"]))
            if self._last_sel != bar_sel:
                self._emit(front, "click", bar_desc, state_id=state_id, extra={"via": "menu path"})
        extra: Dict[str, Any] = {"via": via} if via else {}
        if opened:
            extra["menu_opened"] = True
        return self._emit(front, "choose", item_desc, state_id=state_id, extra=extra or None)

    # -- keys -------------------------------------------------------------------------------
    def on_key(self, keycode: int, flags: int = 0, char: Optional[str] = None,
               credential: bool = False, layout: Optional[Dict[str, str]] = None) -> None:
        """``char`` is used for a command/control chord only (to find its menu item).
        ``credential``: the key arrived while the credential vault was typing a secret (taken
        when the event was observed - the worker may run after the vault has finished).
        ``layout``: the chord's character and input source id on the layout in use when the key
        was observed (``layout_char``); its character replaces ``char``, and a key step keeps both."""
        if layout and layout.get("char"):
            char = str(layout["char"])
        self._flush_click()
        if credential:
            self._drop()                 # the focused field's value is never read
            return
        front = self._front()
        reason = self._guard(front)
        cause = self._guard_cause if reason else ""     # kept: the focus sync below asks the guard again
        if reason in DROP:
            self._drop(front)
            return
        before = self._last_ev           # a step this key commits (in the focus sync or below) follows it
        kind, name = classify_key(keycode, flags)
        if kind in ("tab", "return", "escape", "nav") or (kind == "shortcut" and not _edit_chord(flags, char)):
            self._cancel_trigger_read()  # this key may have moved the focus before it is read
        # the element focused before this key (memory only): the app has handled the key by the
        # time the worker reads the focus, so a key that moves it is judged against this one
        prior = self._focus.el if self._focus is not None else self._focused_other[0]
        self._focused_other = (None, None)
        f = self._sync_focus(front)
        if kind == "shortcut":
            self._on_shortcut(front, reason, int(keycode), int(flags), char, f, prior, cause, layout)
            return
        if kind in ("char", "delete") and f is not None:
            f.dirty = True
            self._val_due = self.clock() + VALUE_DELAY_S
            return
        if kind == "char":
            # one marker per burst, no character: typing outside a text field is not recorded
            _fel, fdesc = self._focused_other
            where = describe_target(fdesc)
            if self._outside_typing_for != where:
                self._emit(front, "type", fdesc, withheld=reason or "not_a_text_field", cause=cause)
                self._outside_typing_for = where
            return
        if kind in ("return", "tab") and f is not None and f.desc.get("role") in MULTILINE_ROLES:
            f.dirty = True               # a line break or a tab typed into a text area
            self._val_due = self.clock() + VALUE_DELAY_S
            return
        if kind == "nav" and f is not None:
            return                       # the caret moves inside the field
        if kind == "tab":
            self._commit_focus()
            self._structural()
            return
        # return / escape / delete / a navigation key outside a field: a key step on the
        # element that had focus
        target = f.desc if f is not None else self._focused_other[1]
        self._commit_focus()
        typed = self._last_ev if self._last_ev is not before else None
        self._structural()
        if kind == "nav":
            key = (name, describe_target(target))
            if self._last_nav == key:
                return                   # a run of the same key is one step
            self._last_nav = key
        else:
            self._last_nav = None
        if reason:
            self._emit(front, "key", target, withheld=reason, extra={"keys": name}, cause=cause)
            return
        if is_secure(target):            # a key pressed in a password field: role + secure only
            self._emit(front, "key", target, secure=True, extra={"keys": name})
            return
        extra: Dict[str, Any] = {"keys": name, "keycode": int(keycode), "flags": 0}
        tdata = typed.data if typed is not None else {}
        if prior is not None and tdata.get("action") == "type" and tdata.get("focused") is True:
            # the key that commits (or cancels) what was just typed into the field a trigger focused -
            # committed here or, when the app had already moved the focus, in the focus sync: the
            # same field, focused when the key was pressed (``trigger_kind: focused``, §12.7)
            extra.update(focused=True, focused_after=typed.id, role=tdata.get("role"),
                         selector=dict(tdata.get("selector") or {}))
        ev = self._emit(front, "key", target, extra=extra)
        self._arm(ev, front, prior)

    def _on_shortcut(self, front, reason, keycode: int, flags: int, char: Optional[str],
                     f: Optional[_Focus], pre: Any = None, cause: str = "",
                     layout: Optional[Dict[str, str]] = None) -> None:
        ch = (char or "").strip().lower()[:1]
        if f is not None and _edit_chord(flags, char):
            f.dirty = True               # an edit of the field (paste, cut, undo, select all)
            self._val_due = self.clock() + VALUE_DELAY_S
            return
        self._commit_focus()
        self._structural()
        if flags & (FLAG_COMMAND | FLAG_CONTROL) and keycode in _SYSTEM_CHORD_KEYS:
            return                       # the system switches apps / windows: not an app step
        name = shortcut_name(flags, ch, keycode)
        if reason:
            self._emit(front, "key", None, withheld=reason, extra={"keys": name}, cause=cause)
            return
        item = None
        if ch:
            try:
                item = self.probe.menu_item_for_shortcut(front["pid"], ch, ax_modifiers(flags))
            except Exception:  # noqa: BLE001
                item = None
        if item is not None:
            idesc = self._describe(item)
            if idesc is not None:
                _bar_el, bar = self._menu_bar_item(item)
                self._arm(self._emit_menu_choice(front, idesc, bar, via=name), front, pre)
                return
        got = self._opened_menu_item(front, ch, flags) if ch and item is None else None
        if got is not None:
            self._arm(self._emit_menu_choice(front, got[0], got[1], via=name, opened=True), front, pre)
            return
        target = f.desc if f is not None else None
        extra = {"keys": name, "keycode": keycode, "flags": flags}
        if layout and layout.get("char"):
            # the replay sends the key that types this character on its own layout
            extra.update(char=str(layout["char"])[:1], layout=str(layout.get("layout") or "")[:120])
        ev = self._emit(front, "key", target, identity=False, extra=extra)
        self._arm(ev, front, pre)

    def _opened_menu_item(self, front, ch: str, flags: int
                          ) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
        """``(item, menu bar item)`` descriptions of the shortcut's menu item in a menu the app builds only
        when it opens (the probe opens it, gated, and closes it; ``open_menu_for_shortcut``), or None. Asked
        once per app, version and chord for the recording; not asked while the next input is already
        queued (a menu opened under it would take it)."""
        opener = getattr(self.probe, "open_menu_for_shortcut", None)
        if opener is None or front.get("pid") is None:
            return None
        mods = ax_modifiers(flags)
        try:
            version = str(self.probe.app_version(front["pid"]) or "") if hasattr(self.probe, "app_version") else ""
        except Exception:  # noqa: BLE001
            version = ""
        key = (str(front.get("bundle_id") or front.get("app") or ""), version, ch, mods)
        if key in self._shortcut_menus:
            return self._shortcut_menus[key]

        def read(item):
            # while the menu is open: what the step records of the item and of its menu bar item
            idesc = self._describe(item)
            return (idesc, self._menu_bar_item(item)[1]) if idesc is not None else None

        settled = []

        def ready() -> bool:
            # asked before each menu is opened; the shortcut's own effect is waited for once
            if self._pending():
                return False
            if not settled:
                settled.append(True)
                self.scan_sleep(SHORTCUT_SETTLE_S)
            return not self._pending()
        try:
            status, got = opener(front["pid"], ch, mods, read, ready)
        except Exception as exc:  # noqa: BLE001
            logger.debug("step capture: opening a menu for a shortcut failed (%s)", type(exc).__name__)
            return None
        if status not in ("found", "none"):
            return None                  # refused by the gates, or input arrived: asked again next time
        self._shortcut_menus[key] = got if status == "found" else None
        return self._shortcut_menus[key]

    # -- end --------------------------------------------------------------------------------
    def close(self, why: str = "stop") -> None:
        if self.closed:
            return
        self._flush_click()
        self._commit_focus()
        self._close_segment(why)
        # nothing read from the app outlives the recording in memory
        self._trigger = self._baseline = self._focus_due = None
        self._digit_groups = []
        self._labels.clear()
        self._shortcut_menus.clear()
        self._focused_other, self._last_nav, self._outside_typing_for = (None, None), None, None
        self.closed = True



# ---------------------------------------------------------------------------
# the front app (one read for the recorder and the guest harness)
# ---------------------------------------------------------------------------

#: where the front app was read, in the order asked (``context/accessibility.py``'s
#: ``frontmost_bundle_id`` names its sources the same)
FRONT_SOURCES = ("ax", "nsworkspace_tick", "menubar_owner")
#: the run-loop tick before NSWorkspace is asked: without it a long-lived process reads the front
#: app one switch late (measured in the guest)
FRONT_TICK_S = 0.02
#: how long the probe's last front-app reading serves the focus fallback (it is read before each key)
FRONT_PID_FRESH_S = 2.0


def _ax_read(A, el, name: str):
    """One AX attribute through ``A`` (the probe's ApplicationServices), or None on an error."""
    try:
        err, val = A.AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _running_app_facts(app) -> Tuple[Optional[int], str, str]:
    """(pid, localized name, bundle id) of an NSRunningApplication, or (None, "", "")."""
    if app is None:
        return None, "", ""
    try:
        return int(app.processIdentifier()), str(app.localizedName() or ""), str(app.bundleIdentifier() or "")
    except Exception:  # noqa: BLE001
        return None, "", ""


def front_application(ax=None, appkit=None, tick_s: float = FRONT_TICK_S, quartz=None) -> Dict[str, Any]:
    """The front app: ``{app, bundle_id, pid, window_title, front_source}`` (plus ``front_why``
    when unknown). AX first: the system-wide ``AXFocusedApplication`` (in the guest it has no value
    for seconds after a launch, answers kAXErrorCannotComplete on first reads and around another
    process's panel such as a save sheet). On an error or no value: ``NSWorkspace``'s
    ``frontmostApplication`` after one run-loop tick, cross-checked with
    ``menuBarOwningApplication``; both answering and disagreeing is unknown, one answering alone
    is taken under its own source. Without the tick (``tick_s`` 0) NSWorkspace is never asked: its
    untickled answer lags one switch, and both reads then name the previous app. Off the main
    thread a tick refreshes nothing either: the window server's frontmost window answers there
    instead, when its app's ``AXFrontmost`` confirms it (``front_source`` "window_server";
    ``quartz`` for tests). Unknown (pid None, ``front_source`` "") when no source answers:
    the caller withholds then. The window title comes from the app's ``AXFocusedWindow``, read on
    ``AXUIElementCreateApplication(pid)`` when AX gave no app element. Reads only."""
    from anyuse.context import accessibility as acc
    A = ax if ax is not None else acc._ax()
    pid: Optional[int] = None
    app_el, source, why = None, "", ""
    name = bundle = ""
    try:
        app_el = _ax_read(A, A.AXUIElementCreateSystemWide(), "AXFocusedApplication")
        if app_el is not None:
            err, p = A.AXUIElementGetPid(app_el, None)
            pid = int(p) if err == 0 and p is not None else None
    except Exception:  # noqa: BLE001 - an AX error is no answer
        pid = None
    kit = None
    try:
        kit = appkit if appkit is not None else acc._appkit()
    except Exception:  # noqa: BLE001
        kit = None
    if pid is not None:
        source = "ax"
        if kit is not None:
            try:
                _p, name, bundle = _running_app_facts(
                    kit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))
            except Exception:  # noqa: BLE001
                pass
    elif tick_s > 0 and threading.current_thread() is not threading.main_thread():
        # NSWorkspace changes only when the MAIN run loop runs: in the recorder's worker thread a
        # tick refreshes nothing, and both reads agree on the PREVIOUS app (measured: a Cmd+N sent
        # to Chess 1.8 s after activating it was recorded as TextEdit's "Ablage > Neu"). The window
        # server answers in any thread; its frontmost window's owner counts only when that app's
        # own AX element says it is frontmost (an app in front without a window is not seen there)
        app_el = None
        cand = acc.window_server_front_pid(quartz)
        if cand is not None:
            try:
                cand_el = A.AXUIElementCreateApplication(cand)
                front_ok = bool(_ax_read(A, cand_el, "AXFrontmost"))
            except Exception:  # noqa: BLE001
                cand_el, front_ok = None, False
            if front_ok:
                pid, source, app_el = cand, "window_server", cand_el
                if kit is not None:
                    try:
                        _p, name, bundle = _running_app_facts(
                            kit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))
                    except Exception:  # noqa: BLE001
                        pass
            else:
                why = "AX did not answer, and the frontmost window's app is not frontmost"
        else:
            why = "AX did not answer, and no window server answer (NSWorkspace is stale off the main thread)"
    else:
        app_el = None
        tick = owner = (None, "", "")
        if kit is not None and tick_s > 0:
            # never without the tick: an untickled NSWorkspace lags one switch, and its two reads then
            # agree on the PREVIOUS app (the cross-check cannot see it). ``tick_s`` 0 (the input path,
            # which never runs its thread's run loop): AX only
            try:
                ws = kit.NSWorkspace.sharedWorkspace()
                kit.NSRunLoop.currentRunLoop().runUntilDate_(kit.NSDate.dateWithTimeIntervalSinceNow_(tick_s))
                tick = _running_app_facts(ws.frontmostApplication())
                owner = _running_app_facts(ws.menuBarOwningApplication())
            except Exception:  # noqa: BLE001 - a tick that cannot run is no NSWorkspace answer either
                tick = owner = (None, "", "")
        if tick[0] is not None and owner[0] is not None and tick[0] != owner[0]:
            why = "the front app and the menu bar's owner disagree"
        elif tick[0] is not None:
            (pid, name, bundle), source = tick, "nsworkspace_tick"
        elif owner[0] is not None:
            (pid, name, bundle), source = owner, "menubar_owner"
        else:
            why = "no source answered" if tick_s > 0 else "AX did not answer (no NSWorkspace read without a tick)"
        if pid is not None:
            try:
                app_el = A.AXUIElementCreateApplication(pid)
            except Exception:  # noqa: BLE001
                app_el = None
    title = ""
    if app_el is not None:
        win = _ax_read(A, app_el, "AXFocusedWindow")
        if win is not None:
            title = acc._s(_ax_read(A, win, "AXTitle")) or ""
    out = {"app": name, "bundle_id": bundle, "pid": pid, "window_title": title, "front_source": source}
    if why:
        out["front_why"] = why
    return out


# ---------------------------------------------------------------------------
# the live AX probe (macOS; never imported by the offline tests)
# ---------------------------------------------------------------------------

class MacAXProbe:
    """The recorder's view of the live AX world, through ``context/accessibility.py``.
    Reads only, with one gated exception: ``open_menu_for_shortcut`` has the tree open a menu the
    app builds only when it opens, read it and close it again (``AccessibilityTree.with_menu_open``)."""

    #: at most this many unbuilt menus are opened for one shortcut
    MAX_UNBUILT_MENUS = 4

    def __init__(self):
        from anyuse.context import accessibility as acc
        self._acc = acc
        self._A = acc._ax()
        self._sys = self._A.AXUIElementCreateSystemWide()
        self._tree = acc.AccessibilityTree()

    def _attr(self, el, name):
        return self._acc._attr(el, name)

    def _str(self, v) -> str:
        s = self._acc._s(v)
        return s or ""

    def frontmost(self) -> Dict[str, Any]:
        """``front_application`` (AX, else NSWorkspace after a run-loop tick, cross-checked with the
        menu bar's owner; off the main thread the window server), with this probe's AX."""
        f = front_application(self._A)
        self._front_pid = (f.get("pid"), time.monotonic())
        return f

    def element_at(self, x: float, y: float):
        return self._tree.find_at(x, y)

    def focused_element(self):
        """The focused element: the system-wide reading, else the front app's own. On a macOS 26
        guest every system-wide read answers kAXErrorCannotComplete while the app element answers,
        so without the fallback no typing is ever recorded."""
        el = self._attr(self._sys, "AXFocusedUIElement")
        if el is not None:
            return el
        pid, at = getattr(self, "_front_pid", (None, 0.0))
        if pid is None or time.monotonic() - at > FRONT_PID_FRESH_S:
            pid = front_application(self._A).get("pid")
        if pid is None:
            return None
        return self._attr(self._A.AXUIElementCreateApplication(int(pid)), "AXFocusedUIElement")

    def parent(self, el):
        return self._attr(el, "AXParent")

    def same(self, a, b) -> bool:
        return a is not None and b is not None and a == b

    def describe(self, el) -> Dict[str, Any]:
        role = self._str(self._attr(el, "AXRole"))
        pos = self._acc._ax_pair(self._attr(el, "AXPosition"), "point")
        size = self._acc._ax_pair(self._attr(el, "AXSize"), "size")
        in_menubar = False
        cur = el
        for _ in range(8):
            cur = self._attr(cur, "AXParent")
            if cur is None:
                break
            if self._str(self._attr(cur, "AXRole")) == "AXMenuBar":
                in_menubar = True
                break
        out = {"role": role, "subrole": self._str(self._attr(el, "AXSubrole")),
               "identifier": self._str(self._attr(el, "AXIdentifier")),
               "title": self._str(self._attr(el, "AXTitle")),
               "description": self._str(self._attr(el, "AXDescription")),
               "path": self._tree._ax_path(el), "in_menubar": in_menubar,
               "frame": [pos[0], pos[1], size[0], size[1]] if pos and size else None}
        if role in TEXT_ENTRY_ROLES:
            # the placeholder and the text of the element that titles the field (an unlabeled
            # field next to "TAN") only decide whether it holds a secret; never stored
            out["label_hint"] = self._str(self._attr(el, "AXPlaceholderValue"))
            titled_by = self._attr(el, "AXTitleUIElement")
            if titled_by is not None:
                out["title_hint"] = (self._str(self._attr(titled_by, "AXValue"))
                                     or self._str(self._attr(titled_by, "AXTitle")))
        return out

    def pid_of(self, el) -> Optional[int]:
        """The pid of the app ``el`` belongs to (AXUIElementGetPid), or None."""
        try:
            err, pid = self._A.AXUIElementGetPid(el, None)
            return int(pid) if err == 0 else None
        except Exception:  # noqa: BLE001
            return None

    def menu_bar_front(self, el) -> Optional[Dict[str, Any]]:
        """``{app, bundle_id, pid, windowless}`` of the app whose menu bar ``el`` (a menu bar item, a
        menu or a menu item) sits in, when that is the app's own ``AXMenuBar`` (not a menu extra's)
        and the app's AX element says it is frontmost: the menu bar's owner, the front app, also
        when it has no window. ``windowless``: its ``AXWindows`` answered an empty list. None
        otherwise. A read."""
        if el is None or self._str(self._attr(el, "AXRole")) not in ("AXMenuBarItem", "AXMenuItem", "AXMenu"):
            return None
        cur, bar = el, None
        for _ in range(12):
            cur = self._attr(cur, "AXParent")
            if cur is None:
                return None
            if self._str(self._attr(cur, "AXRole")) == "AXMenuBar":
                bar = cur
                break
        pid = self.pid_of(el)
        if bar is None or pid is None:
            return None
        app_el = self._A.AXUIElementCreateApplication(int(pid))
        if not bool(self._attr(app_el, "AXFrontmost")) or self._attr(app_el, "AXMenuBar") != bar:
            return None
        try:
            _p, name, bundle = _running_app_facts(
                self._acc._appkit().NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid)))
        except Exception:  # noqa: BLE001
            name = bundle = ""
        if not (name or bundle):
            return None
        try:
            wins = self._attr(app_el, "AXWindows")
            if wins is not None:
                windowless = len(list(wins)) == 0
            else:
                # no window list: its children, which hold the menu bar whenever the app answers
                kids = list(self._attr(app_el, "AXChildren") or [])
                windowless = bool(kids) and not any(
                    self._str(self._attr(k, "AXRole")) == "AXWindow" for k in kids)
        except TypeError:
            windowless = False
        return {"app": name, "bundle_id": bundle, "pid": int(pid), "windowless": windowless}

    def secure_field_visible(self, pid) -> bool:
        """Does the app's focused window hold a secure field? ``context/accessibility.
        focused_window_has_secure_field`` (roles and subroles, at most ``SECURE_SCAN_MAX``
        elements); an AX error raises, and the recorder withholds."""
        return self._acc.focused_window_has_secure_field(pid, SECURE_SCAN_MAX)

    @staticmethod
    def secure_input_enabled() -> bool:
        """Carbon's ``IsSecureEventInputEnabled()``: some app's password field has the keyboard."""
        import ctypes
        carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        return bool(carbon.IsSecureEventInputEnabled())

    def secure_scan(self, pid) -> Tuple[bool, str]:
        """``(found, kind)``: the decision is ``secure_field_visible``'s (it raises as that does);
        when it found one, ``kind`` says whether a role or a subrole made it secure (a second
        walk of roles and subroles only, for the recorder's diagnostics; "unknown" when that walk
        no longer finds it)."""
        if not self.secure_field_visible(pid):
            return False, ""
        return True, self._secure_kind(pid)

    def _secure_kind(self, pid) -> str:
        try:
            win = self._attr(self._A.AXUIElementCreateApplication(int(pid)), "AXFocusedWindow")
        except Exception:  # noqa: BLE001
            return "unknown"
        stack, seen = ([(win, 0)] if win is not None else []), 0
        while stack and seen < SECURE_SCAN_MAX:
            el, depth = stack.pop()
            seen += 1
            if self._str(self._attr(el, "AXRole")) == SECURE:
                return "role"
            if self._str(self._attr(el, "AXSubrole")) == SECURE:
                return "subrole"
            if depth < 30:
                kids = self._attr(el, "AXChildren")
                try:
                    stack.extend((k, depth + 1) for k in (list(kids) if kids is not None else []))
                except Exception:  # noqa: BLE001
                    continue
        return "unknown"

    def value(self, el) -> Optional[str]:
        role = self._str(self._attr(el, "AXRole"))
        if role == SECURE or self._str(self._attr(el, "AXSubrole")) == SECURE:
            return None                  # a secure field's value is never read
        v = self._attr(el, "AXValue")
        if v is None:
            # an empty text view may answer no value: its character count (a number, no content)
            # says it is empty
            if role in TEXT_ENTRY_ROLES:
                try:
                    n = self._attr(el, "AXNumberOfCharacters")
                    return "" if n is not None and int(n) == 0 else None
                except (TypeError, ValueError):
                    return None
            return None
        if isinstance(v, (str, bytes)) or hasattr(v, "UTF8String"):
            return str(v)
        return None

    def snapshot_descs(self, pid) -> List[Dict[str, Any]]:
        if pid is None:
            return []
        tree = self._acc.AccessibilityTree(pid=int(pid))
        descs: List[Any] = []
        tree._walk(tree._root(), 0, descs)
        return [{"role": d.role, "title": d.title or "", "identifier": d.identifier or "",
                 "in_menubar": d.in_menubar} for d in descs]

    def document_of(self, el) -> Optional[str]:
        """The full AXDocument (a file URL) of the window ``el`` sits in, or None. A read."""
        try:
            return self._tree.element_window(el)[1] or None
        except Exception:  # noqa: BLE001
            return None

    def row_facts(self, el) -> Optional[Dict[str, Any]]:
        """The list row a click landed in, or None: ``el`` is an AXRow, a cell of one or inside
        a cell (at most six levels down). The row, the cell and its column (its index among the
        row's AXCell children), the row's label (``AccessibilityTree._row_label``: its own title
        or description, else its FIRST cell's text) and its list's identity
        (``AccessibilityTree.container_identity``: canonical role, AXIdentifier, AXDescription,
        role-index path below the window, column count). Reads only."""
        tree = self._tree
        cur, cell, row = el, None, None
        for _ in range(7):
            if cur is None:
                return None
            role = self._str(self._attr(cur, "AXRole"))
            if role == "AXRow":
                row = cur
                break
            if role == "AXCell" and cell is None:
                cell = cur
            elif role in ("AXTable", "AXOutline", "AXList", "AXWindow", "AXApplication"):
                return None
            cur = self._attr(cur, "AXParent")
        if row is None:
            return None
        box = tree._row_container_element(row)
        if box is None:
            return None
        column = None
        if cell is not None:
            cells = [c for c in tree._children(row) if self._str(self._attr(c, "AXRole")) == "AXCell"]
            column = next((i for i, c in enumerate(cells) if c == cell), None)
        return {"row": row, "cell": cell, "column": column, "label": tree._row_label(row) or "",
                "container": tree.container_identity(box)}

    def row_scope(self, row, app: Optional[str] = None) -> Optional[Dict[str, str]]:
        """The container scope of ``row``'s window (``ax_actions.live_scope``: the module's
        ``container_scope`` on the row's window, for ``app``), in memory, or None. A read."""
        from anyuse.execution.ax_actions import live_scope
        return live_scope(self._tree, row, app=app)

    def menu_item_for_shortcut(self, pid, char: str, mods: int):
        if pid is None or not char:
            return None
        tree = self._acc.AccessibilityTree(pid=int(pid))
        descs: List[Any] = []
        refs: List[Any] = []
        tree._walk(tree._root(), 0, descs, refs)
        hits = []
        for el, d in refs:
            if d.role != "AXMenuItem" or not d.in_menubar:
                continue
            c = self._str(self._attr(el, "AXMenuItemCmdChar"))
            m = self._attr(el, "AXMenuItemCmdModifiers")
            try:
                m = int(m) if m is not None else 0
            except (TypeError, ValueError):
                continue
            if c.lower() == char.lower() and m == int(mods):
                hits.append(el)
        return hits[0] if len(hits) == 1 else None

    def _menu_items(self, el, depth: int = 0) -> List[Any]:
        """Every AXMenuItem below a menu bar item, its submenus' too. A read."""
        out: List[Any] = []
        if depth > 8:
            return out
        for kid in self._attr(el, "AXChildren") or []:
            role = self._str(self._attr(kid, "AXRole"))
            if role == "AXMenuItem":
                out.append(kid)
            if role in ("AXMenu", "AXMenuItem"):
                out += self._menu_items(kid, depth + 1)
        return out

    def _owns_shortcut(self, el, char: str, mods: int) -> bool:
        c = self._str(self._attr(el, "AXMenuItemCmdChar"))
        m = self._attr(el, "AXMenuItemCmdModifiers")
        try:
            m = int(m) if m is not None else 0
        except (TypeError, ValueError):
            return False
        return bool(c) and c.lower() == char.lower() and m == int(mods)

    def app_version(self, pid) -> str:
        """``<CFBundleShortVersionString> (<CFBundleVersion>)`` of the app ``pid`` runs, "" when unreadable."""
        try:
            from AppKit import NSBundle, NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            url = app.bundleURL() if app is not None else None
            info = NSBundle.bundleWithURL_(url).infoDictionary() if url is not None else None
            if not info:
                return ""
            return f"{info.get('CFBundleShortVersionString') or ''} ({info.get('CFBundleVersion') or ''})"
        except Exception:  # noqa: BLE001
            return ""

    def open_menu_for_shortcut(self, pid, char: str, mods: int, read: Callable[[Any], Any],
                               ready: Optional[Callable[[], bool]] = None) -> Tuple[str, Any]:
        """The shortcut's menu item in a menu the app builds only when it opens (Chess's "Spiel"
        reports no AX items until then). A top-level menu bar item without items (not the Apple
        menu) is opened by the tree (``with_menu_open``: gated ``AXPress``, behind the drive gate
        and the foreground interlock), its items are matched by ``AXMenuItemCmdChar`` /
        ``AXMenuItemCmdModifiers``, ``read(item)`` runs while the menu is open, and the tree closes
        it. ``ready()`` is asked before each menu is opened (False: not now, no further menu).

        ``("found", read(item))``; ``("none", None)``: no unbuilt menu, a built menu has the shortcut
        (``menu_item_for_shortcut`` decided), or the opened menus hold it zero or several times;
        ``("refused", reason)``: the gates refuse actuation now (nothing is opened);
        ``("skipped", None)``: not ready (no menu is opened after that)."""
        if pid is None or not char:
            return "none", None
        app = self._A.AXUIElementCreateApplication(int(pid))
        bar = self._attr(app, "AXMenuBar")
        tops = list(self._attr(bar, "AXChildren") or []) if bar is not None else []
        built = [(t, self._menu_items(t)) for t in tops]
        if any(self._owns_shortcut(it, char, mods) for _t, items in built for it in items):
            return "none", None
        unbuilt = [t for t, items in built[1:] if not items][:self.MAX_UNBUILT_MENUS]
        if not unbuilt:
            return "none", None
        tree = self._acc.AccessibilityTree(pid=int(pid))
        why = tree.actuation_refusal("recorder.open_menu")
        if why:
            return "refused", why
        def look(top):
            # while the menu is open
            hits = [it for it in self._menu_items(top) if self._owns_shortcut(it, char, mods)]
            return len(hits), (read(hits[0]) if len(hits) == 1 else None)
        for top in unbuilt:
            if ready is not None and not ready():
                return "skipped", None   # asked before EACH menu: input that arrived meanwhile stops it
            opened, got = tree.with_menu_open(top, look)
            if opened and got[0]:
                return ("found", got[1]) if got[1] is not None else ("none", None)
        return "none", None


# ---------------------------------------------------------------------------
# the event source: a listen-only tap + one worker thread
# ---------------------------------------------------------------------------

class _QuartzTapSource:
    """A LISTEN-ONLY session event tap on its own run-loop thread. The callback only queues
    ``(kind, ...)`` tuples: mouse positions, key codes, flags, and for a command/control chord
    its character and its layout character (``layout_char``). It returns every event unchanged
    (a listen-only tap cannot do otherwise)."""

    def __init__(self, put: Callable[[tuple], None],
                 layout_of: Optional[Callable[[int, int], Optional[Dict[str, str]]]] = None):
        self._put = put
        #: a chord's character on the layout in use, read when the key is observed (before the app
        #: handles it: a chord that opens a document can switch the input source)
        self._layout_of = layout_of if layout_of is not None else layout_char
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._tap = None
        self._src = None
        self._ready = threading.Event()
        self.failure: Optional[str] = None

    def start(self, timeout: float = 5.0) -> Optional[str]:
        self._thread = threading.Thread(target=self._run, name="anyuse-step-tap", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            return "the event tap did not start"
        return self.failure

    def _run(self) -> None:
        try:
            import Quartz as Q
            kinds = (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventLeftMouseDragged,
                     Q.kCGEventRightMouseDown, Q.kCGEventKeyDown)
            mask = 0
            for k in kinds:
                mask |= Q.CGEventMaskBit(k)

            def callback(_proxy, etype, event, _refcon):
                try:
                    if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                        if self._tap is not None:
                            Q.CGEventTapEnable(self._tap, True)
                        return event
                    if etype == Q.kCGEventKeyDown:
                        code = int(Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode))
                        flags = int(Q.CGEventGetFlags(event))
                        ch, lay = None, None
                        if flags & (FLAG_COMMAND | FLAG_CONTROL):
                            _n, ch = Q.CGEventKeyboardGetUnicodeString(event, 4, None, None)
                            try:
                                lay = self._layout_of(code, flags)
                            except Exception:  # noqa: BLE001 - no layout: the event's own string
                                lay = None
                        self._put(("key", code, flags, ch) + ((lay,) if lay else ()))
                    else:
                        loc = Q.CGEventGetLocation(event)
                        if etype == Q.kCGEventLeftMouseDown:
                            n = int(Q.CGEventGetIntegerValueField(event, Q.kCGMouseEventClickState))
                            self._put(("down", float(loc.x), float(loc.y), "left", n))
                        elif etype == Q.kCGEventRightMouseDown:
                            self._put(("down", float(loc.x), float(loc.y), "right", 1))
                        elif etype == Q.kCGEventLeftMouseDragged:
                            self._put(("drag", float(loc.x), float(loc.y)))
                        elif etype == Q.kCGEventLeftMouseUp:
                            self._put(("up", float(loc.x), float(loc.y)))
                except Exception:  # noqa: BLE001 - never break the tap
                    pass
                return event

            self._tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                           Q.kCGEventTapOptionListenOnly, mask, callback, None)
            if not self._tap:
                self.failure = ("the listen-only event tap was refused (Input Monitoring / "
                                "Accessibility not granted to this process)")
                self._ready.set()
                return
            src = Q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._src = src
            self._loop = Q.CFRunLoopGetCurrent()
            Q.CFRunLoopAddSource(self._loop, src, Q.kCFRunLoopCommonModes)
            Q.CGEventTapEnable(self._tap, True)
            self._ready.set()
            Q.CFRunLoopRun()
        except Exception as exc:  # noqa: BLE001
            self.failure = f"event tap failed: {type(exc).__name__}: {exc}"
            self._ready.set()

    def stop(self) -> None:
        """Disable the tap, take its source off the run loop, invalidate its mach port (the
        port is released, not just idle), stop the run loop and join the thread."""
        try:
            import Quartz as Q
            if self._tap is not None:
                Q.CGEventTapEnable(self._tap, False)
            if self._loop is not None and self._src is not None:
                Q.CFRunLoopRemoveSource(self._loop, self._src, Q.kCFRunLoopCommonModes)
            if self._tap is not None:
                Q.CFMachPortInvalidate(self._tap)
            if self._loop is not None:
                Q.CFRunLoopStop(self._loop)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._tap = self._src = self._loop = self._thread = None


#: how long ``StepCapture.stop`` lets the worker finish the events already observed
STOP_DRAIN_S = 5.0


class StepCapture:
    """Owns the source and the worker. ``source_factory(put)`` makes the event source
    (default: the listen-only Quartz tap); the worker feeds the recorder in order and runs
    its delayed reads (``due_in`` / ``on_idle``). ``stop`` stops the source, then lets the
    worker handle every event already observed (up to ``STOP_DRAIN_S``) before the recording
    ends; what it could not handle in that time is counted (``dropped_at_stop``), not handled."""

    def __init__(self, recorder: StructuredStepRecorder,
                 source_factory: Optional[Callable[[Callable[[tuple], None]], Any]] = None,
                 suppressed: Optional[Callable[[], bool]] = None):
        self.recorder = recorder
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=10000)
        self._source_factory = source_factory or _QuartzTapSource
        self._source = None
        self._worker: Optional[threading.Thread] = None
        self._suppressed = suppressed
        self.dropped = 0
        self.dropped_at_stop = 0
        self._abandon = False
        # the recorder's focus read after a trigger waits while an observed event is queued
        recorder.input_pending = lambda: not self._q.empty()

    def _put(self, item: tuple) -> None:
        """Called by the source as each event is observed. A key event is stamped here with
        whether the credential vault is typing (its flag exists only while it types; the
        worker may reach the event later)."""
        if item and item[0] == "key":
            try:
                cred = bool(self._suppressed()) if self._suppressed is not None else False
            except Exception:  # noqa: BLE001 - unknown is treated as typing
                cred = True
            item = tuple(item[:4]) + (cred,) + tuple(item[4:5])     # ..., credential, layout?
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def start(self) -> Optional[str]:
        if not enabled():
            return f"{ENV_GATE} is not set"
        self.recorder.start()
        self._worker = threading.Thread(target=self._work, name="anyuse-step-worker", daemon=True)
        self._worker.start()
        self._source = self._source_factory(self._put)
        why = self._source.start()
        if why:
            try:
                self._source.stop()          # a tap that came up late is not left running
            except Exception:  # noqa: BLE001
                pass
            self._source = None
            self._q.put(("stop",))
            self._worker.join(timeout=5)
            self._worker = None
            return why
        return None

    def _work(self) -> None:
        rec = self.recorder
        while True:
            try:
                item = self._q.get(timeout=rec.due_in() if rec.due_in() is not None else 1.0)
            except queue.Empty:
                try:
                    rec.on_idle()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("step capture idle task failed: %s", type(exc).__name__)
                continue
            kind = item[0]
            if kind == "stop":
                break
            if self._abandon:
                continue                 # stop gave up waiting: counted there, not handled
            try:
                if kind == "down":
                    rec.on_mouse_down(item[1], item[2], button=item[3], clicks=item[4])
                elif kind == "up":
                    rec.on_mouse_up(item[1], item[2])
                elif kind == "drag":
                    rec.on_mouse_dragged(item[1], item[2])
                elif kind == "key":
                    rec.on_key(item[1], item[2], item[3], credential=bool(item[4]) if len(item) > 4 else False,
                               layout=item[5] if len(item) > 5 else None)
                rec.on_idle()
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop capture
                # the exception's type only: its message may quote what the app showed
                logger.debug("step capture event failed: %s", type(exc).__name__)
        try:
            rec.close("stop")
        except Exception as exc:  # noqa: BLE001
            logger.debug("step capture close failed: %s", type(exc).__name__)

    def stop(self, drain_s: float = STOP_DRAIN_S) -> None:
        if self._source is not None:
            self._source.stop()
            self._source = None
        if self._worker is not None:
            try:
                self._q.put(("stop",), timeout=drain_s)
            except queue.Full:
                pass
            self._worker.join(timeout=drain_s)
            if self._worker.is_alive():
                # the worker did not get through the events in time: the rest is counted, not handled
                self.dropped_at_stop = self._q.qsize()
                self._abandon = True
                self._worker.join(timeout=1.0)
            self._worker = None
        elif not self.recorder.closed:
            self.recorder.close("stop")


# ---------------------------------------------------------------------------
# export (a draft file for another machine: values stay vault markers)
# ---------------------------------------------------------------------------

def export_draft(session, name: str, path: str, release: bool = False, vault=None,
                 release_content: bool = False) -> Dict[str, Any]:
    """Write ``session.structured_draft(name)`` as JSON. Typed values, line anchors and window
    titles in it are vault markers and documents salted keys; their text stays on this machine.
    UI labels are markers too unless ``release`` (to compile on another machine): then selector
    names / identifiers and fingerprint titles / identifiers are resolved from ``vault``
    (default: this machine's) under the ``ui_label`` standing grant, audited. Content - a list
    row's label, a list's identifier and description (``content_label``) - only with
    ``release_content`` as well, each read under the presence-gated grant."""
    data = session.structured_draft(name)
    released = 0
    if release or release_content:
        if vault is None:
            from anyuse.watcher.entity_vault import get_default_vault
            vault = get_default_vault()
        resolve = vault_label_resolver(vault, purpose="export a recorded workflow")
        content = vault_content_resolver(vault, purpose="export a recorded workflow") if release_content else None
        if resolve is not None:
            data, released = release_labels(data, resolve if release else (lambda m: None), content)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return {"path": path, "steps": sum(1 for e in data["events"] if e["tag_type"] == "STEP"),
            "partial": data.get("is_partial", False), "labels_released": released}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="structured watcher drafts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    ex = sub.add_parser("export")
    ex.add_argument("ref", help="SESSION_ID/steps_<app>_<n>")
    ex.add_argument("--out", required=True)
    ex.add_argument("--release-labels", action="store_true",
                    help="resolve UI labels (button / menu titles, the titles and AX identifiers of "
                         "screen fingerprints) for compiling on another machine; typed values, line "
                         "anchors, window titles, list row labels and list names stay vault markers, "
                         "documents stay salted keys")
    ex.add_argument("--release-content", action="store_true",
                    help="also resolve list row labels and list identifiers / descriptions (content: "
                         "each read asks for presence and is audited); the file then holds them in "
                         "the clear")
    a = ap.parse_args(argv)
    from anyuse.watcher.storage import SessionStorage
    store = SessionStorage()
    if a.cmd == "list":
        for meta in store.list_sessions():
            s = store.load(meta["id"])
            for n in (s.structured_workflow_names if s else []):
                print(f"{s.id}/{n}")
        return 0
    sid, _, name = a.ref.partition("/")
    s = store.load(sid)
    if s is None or name not in s.structured_workflow_names:
        print(f"no structured recording {a.ref!r}")
        return 2
    print(json.dumps(export_draft(s, name, a.out, release=a.release_labels,
                                  release_content=a.release_content)))
    return 0



# Meeting surfaces are withheld by the structured capture guard.
MEETING_APPS = (
    "zoom.us", "zoom", "microsoft teams", "facetime", "webex", "cisco webex",
    "skype", "google meet",
)
MEETING_TITLE_HINTS = (
    "meet.google.com", "google meet", "zoom meeting", "huddle",
    "teams meeting", "webex meeting", " call", "in a call", "voice connected",
)


def is_meeting_surface(app_name: str, window_title: str = "") -> bool:
    """Whether the front app/title identifies a call or meeting."""
    app, title = (app_name or "").lower(), (window_title or "").lower()
    return any(name in app for name in MEETING_APPS) or any(hint in title for hint in MEETING_TITLE_HINTS)


if __name__ == "__main__":
    import sys
    sys.exit(main())
