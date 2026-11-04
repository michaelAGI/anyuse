"""The AppleScript route: Apple Events before clicks, wherever an app's scripting dictionary covers a step or a
read (the Automation permission is granted once per app).

PURE. This module never sends an Apple Event, never runs ``osascript`` and never reads AX. Every effect goes through
an injected callable: a permission check that never asks (``AEDeterminePermissionToAutomateTarget`` with
``askUserIfNeeded=false`` in the guest), an ``osascript`` runner, a process starter for the grant's one event. Host
tests use fakes; the guest supplies the real ones (the measurement harness; the job agent's
``scope_read`` and ``script_step`` ops in ``execution/guest/desktop_e2e_job.py``).

1. **Automation status**: what a permission code means (granted / not granted / not running).
2. **Answers**: ``osascript -s s`` prints its result as AppleScript source; :func:`parse_result` reads it back
   (lists, records, text, numbers, booleans, ``missing value``) without evaluating anything.
3. **The grant** (:func:`grant_app`): ONE harmless read-only Apple Event per target app (:data:`GRANT_EVENTS`).
   Sending it raises the system's Automation prompt for the user; the result is read from the permission status
   itself (polled without asking), never guessed from the event's answer. Nothing else is touched.

Every script here is a CONSTANT: raw ``«event»``/``«class»`` codes, inputs only as ``on run argv`` items, codes and
bundle ids validated before they enter a script (``osa_compile._code`` / ``_bundle``), the same discipline as
``execution/osa_compile.py``.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import osa_compile as oc

FINDER = "com.apple.finder"
NOTES = "com.apple.Notes"
TEXTEDIT = "com.apple.TextEdit"

OSASCRIPT = oc.OSASCRIPT
#: ``-s s``: the result printed as AppleScript source (strings quoted), so a list of names cannot be confused
#: with a name that holds a comma
OSASCRIPT_ARGV = (OSASCRIPT, "-s", "s", "-")

# ---------------------------------------------------------------------------
# 1. Automation status
# ---------------------------------------------------------------------------

GRANTED = "granted"
NOT_GRANTED = "not_granted"
NOT_RUNNING = "not_running"
ERROR = "error"

#: errAEEventNotPermitted (the user said no, or turned it off), errAEEventWouldRequireUserConsent (never answered)
NOT_GRANTED_CODES = (-1743, -1744)
#: procNotFound, connectionInvalid
NOT_RUNNING_CODES = (-600, -609)


def classify_status(code: Any) -> str:
    """A permission code (``AEDeterminePermissionToAutomateTarget``) as ``granted`` / ``not_granted`` /
    ``not_running`` / ``error``."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return ERROR
    if code == oc.AUTOMATION_GRANTED:
        return GRANTED
    if code in NOT_GRANTED_CODES:
        return NOT_GRANTED
    if code in NOT_RUNNING_CODES:
        return NOT_RUNNING
    return ERROR


def status_reason(code: Any) -> str:
    """The reason recorded when a script is not sent: ``automation_not_granted: -1743 ...``,
    ``not_running: -600 ...`` or ``automation_status: <code>``."""
    cls = classify_status(code)
    label = oc.AUTOMATION_STATUS.get(code, "") if isinstance(code, int) else ""
    head = {NOT_GRANTED: "automation_not_granted", NOT_RUNNING: "not_running"}.get(cls, "automation_status")
    return f"{head}: {code} {label}".strip()


_ERRNUM = re.compile(r"\((-?\d+)\)\s*$")


def error_number(stderr: str) -> Optional[int]:
    """The AppleScript error number at the end of ``osascript``'s error line (``... (-1728)``), or None."""
    m = _ERRNUM.search((stderr or "").strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 2. Answers: ``osascript -s s`` output read back
# ---------------------------------------------------------------------------

#: a record field's label: a term, a |quoted| term or a raw «class xxxx»
_RECORD_KEY = re.compile(r"(«[^»]*»|\|[^|]*\||[A-Za-z_][A-Za-z0-9_ ]*?)\s*:(?!:)")


class _Parser:
    def __init__(self, text: str):
        self.t, self.i = text, 0

    def ws(self) -> None:
        while self.i < len(self.t) and self.t[self.i] in " \t\r\n":
            self.i += 1

    def value(self) -> Any:
        self.ws()
        if self.i >= len(self.t):
            raise ValueError("unexpected end")
        c = self.t[self.i]
        if c == "{":
            return self.braces()
        if c == '"':
            return self.string()
        return self.token()

    def braces(self) -> Any:
        self.i += 1
        items: List[Any] = []
        record: Dict[str, Any] = {}
        self.ws()
        if self.t.startswith("}", self.i):
            self.i += 1
            return items
        while True:
            self.ws()
            m = _RECORD_KEY.match(self.t, self.i)
            if m and not self.t.startswith('"', self.i):
                self.i = m.end()
                record[m.group(1).strip("|").strip()] = self.value()
            else:
                items.append(self.value())
            self.ws()
            if self.t.startswith(",", self.i):
                self.i += 1
                continue
            if self.t.startswith("}", self.i):
                self.i += 1
                break
            raise ValueError(f"expected , or }} at {self.i}")
        if record and items:
            raise ValueError("a list mixing record fields and items")
        return record if record else items

    def string(self) -> str:
        self.i += 1
        out: List[str] = []
        esc = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
        while self.i < len(self.t):
            c = self.t[self.i]
            if c == "\\" and self.i + 1 < len(self.t):
                out.append(esc.get(self.t[self.i + 1], self.t[self.i + 1]))
                self.i += 2
                continue
            if c == '"':
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise ValueError("unterminated string")

    def token(self) -> Any:
        depth, start = 0, self.i
        while self.i < len(self.t):
            c = self.t[self.i]
            if c == '"':
                self.string()
                continue
            if c in "{«":
                depth += 1
            elif c in "}»":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            self.i += 1
        raw = self.t[start:self.i].strip()
        if raw == "missing value":
            return None
        if raw in ("true", "false"):
            return raw == "true"
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if re.fullmatch(r"-?\d+\.\d*(E[+-]?\d+)?|-?\d+E[+-]?\d+", raw):
            return float(raw)
        return raw


def parse_result(text: str) -> Any:
    """``osascript -s s`` output -> Python: a list, a dict (a record), text, int / float, bool, None
    (``missing value``); any other term (an alias, an object specifier) is kept as its raw source text. Nothing
    is evaluated. Raises ``ValueError`` on text that is not one value."""
    p = _Parser((text or "").strip())
    if not p.t:
        return ""
    v = p.value()
    p.ws()
    if p.i != len(p.t):
        raise ValueError(f"trailing text at {p.i}")
    return v


# ---------------------------------------------------------------------------
# 3. The grant: one harmless read-only Apple Event per target app
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OsaScript:
    """One constant script: ``purpose`` ``read`` or ``action``, the codes it relies on, a human description."""
    id: str
    bundle_id: str
    purpose: str
    script: str
    uses: Tuple[str, ...]
    describe: str

    def argv(self, values: Sequence[Any] = ()) -> List[str]:
        return list(OSASCRIPT_ARGV) + [str(v) for v in values]


def _grant_script(bundle: str, expr: str) -> str:
    # argv 1: seconds the event waits for the user's answer (the prompt holds the event until answered)
    return ("on run argv\n"
            "\twith timeout of ((item 1 of argv) as integer) seconds\n"
            f"\t\ttell application id \"{oc._bundle(bundle)}\"\n"
            f"\t\t\treturn {expr}\n"
            "\t\tend tell\n"
            "\tend timeout\n"
            "end run\n")


#: per target app, the ONE read the grant sends (codes from the apps' own dictionaries)
GRANT_EVENTS: Dict[str, OsaScript] = {
    FINDER: OsaScript("grant/finder", FINDER, "read",
                      _grant_script(FINDER, f"«class {oc._code('pnam')}» of «class {oc._code('brow')}» 1"),
                      ("pnam", "brow"), "name of front window (Finder window 1)"),
    NOTES: OsaScript("grant/notes", NOTES, "read",
                     _grant_script(NOTES, f"«class {oc._code('pnam')}» of «class {oc._code('dfac')}»"),
                     ("pnam", "dfac"), "name of default account"),
    TEXTEDIT: OsaScript("grant/textedit", TEXTEDIT, "read",
                        _grant_script(TEXTEDIT, f"«class {oc._code('pnam')}» of every «class {oc._code('docu')}»"),
                        ("pnam", "docu"), "name of documents"),
}
#: the apps the route needs, in the order the grant asks
GRANT_ORDER = (FINDER, NOTES, TEXTEDIT)


def grant_event(bundle: str) -> OsaScript:
    """The ONE read the grant sends to an app: :data:`GRANT_EVENTS` for the route's first three apps; for any other
    app (the general rung) ``count of every window`` (``«class cwin»``, the standard
    suite's window): harmless, read-only, and its answer (a number) says nothing about the user's data. An app that
    has no such class still receives the event, which is what raises the prompt."""
    if bundle in GRANT_EVENTS:
        return GRANT_EVENTS[bundle]
    return OsaScript(f"grant/{oc._bundle(bundle)}", bundle, "read",
                     _grant_script(bundle, f"count of every «class {oc._code('cwin')}»"), ("cwin",),
                     "count of windows")


def grant_app(bundle: str, *, permission: Callable[[str], int], running: Callable[[str], bool],
              start_event: Callable[[List[str], str], Any], wait_s: float = 300.0, poll_s: float = 1.0,
              clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Ask for one app's Automation permission by sending its ONE read (:data:`GRANT_EVENTS`).

    * not running: ``not_running``, nothing sent (the grant never launches an app);
    * already granted (status 0): the read is sent once to confirm delivery, ``granted``;
    * denied before (-1743): ``not_granted``, nothing sent (macOS does not ask twice; the user turns it on in
      System Settings > Privacy & Security > Automation);
    * never answered (-1744): the read is started (the system shows the prompt and holds the event), then the
      status is polled WITHOUT asking every ``poll_s`` until 0 (``granted``), -1743 (``not_granted``) or
      ``wait_s`` (``not_granted``, ``timeout``; the pending event is stopped).

    ``start_event(argv, script)`` returns a handle with ``done() -> bool``, ``finish(timeout) -> (rc, stdout,
    stderr)`` and ``kill()``. The read's answer (a window or account name) is never recorded: only whether it was
    answered and the error number of an error answer."""
    try:
        ev: Optional[OsaScript] = grant_event(bundle)
    except ValueError:
        ev = None
    out: Dict[str, Any] = {"bundle_id": bundle, "event": ev.describe if ev else None, "status": ERROR,
                           "code": None, "sent": False, "waited_s": 0.0, "timeout": False, "answered": None,
                           "event_error": None, "asked_before": None}
    if ev is None:
        out["detail"] = "not a bundle id"
        return out
    if not running(bundle):
        out.update(status=NOT_RUNNING, code=-600, detail="not running: start it first (the grant never launches)")
        return out
    code = permission(bundle)
    out["code"], out["asked_before"] = code, code != -1744
    cls = classify_status(code)
    if cls in (NOT_RUNNING, ERROR) or code == -1743:
        out["status"] = NOT_GRANTED if code == -1743 else cls
        out["detail"] = ("denied before: turn it on in System Settings > Privacy & Security > Automation"
                         if code == -1743 else status_reason(code))
        return out
    handle = start_event(ev.argv([int(max(1, wait_s))]), ev.script)
    out["sent"] = True
    t0 = clock()
    while code == -1744:
        if clock() - t0 >= wait_s:
            out["timeout"] = True
            break
        sleep(poll_s)
        code = permission(bundle)      # an event that ended without a decision: polling goes on until wait_s
    out["waited_s"] = round(clock() - t0, 1)
    out["code"] = code
    out["status"] = classify_status(code)
    if out["timeout"] and not handle.done():
        handle.kill()
        out["answered"] = False
    else:
        try:
            rc, _stdout, stderr = handle.finish(30.0)
        except Exception as exc:  # noqa: BLE001 - a hung event is stopped, its answer is not needed
            handle.kill()
            rc, stderr = None, f"{type(exc).__name__}"
        out["answered"] = rc == 0
        out["event_error"] = None if rc == 0 else error_number(stderr)
    if out["status"] == NOT_GRANTED:
        out["detail"] = "the owner did not answer in time" if out["timeout"] else "the owner said no"
    return out


def grant_all(bundles: Sequence[str] = GRANT_ORDER, **kw: Any) -> Dict[str, Any]:
    """:func:`grant_app` for each app in order; a summary of the statuses."""
    rows = [grant_app(b, **kw) for b in bundles]
    return {"apps": rows, "granted": [r["bundle_id"] for r in rows if r["status"] == GRANTED],
            "not_granted": [r["bundle_id"] for r in rows if r["status"] == NOT_GRANTED],
            "not_running": [r["bundle_id"] for r in rows if r["status"] == NOT_RUNNING],
            "all_granted": all(r["status"] == GRANTED for r in rows)}


# ---------------------------------------------------------------------------
# 4. Container identity from Apple Events (the reader ``context/container_scope`` asks first)
# ---------------------------------------------------------------------------

#: A loop over an app's objects first gets them into a LOCAL list and walks it by index. ``repeat with x in
#: (every Finder window)`` sends the app ``count ... each item`` and ``item N of every Finder window``, which the app
#: resolves with ITS OWN ``item`` class: Finder answered the files shown in the windows (live: the rows
#: were the folder's items, and -1728 once a window held fewer items than the count). Every route script follows
#: this (test_applescript_scope.test_no_route_script_iterates_an_app_reference).


def _finder_windows_script() -> str:
    w, t, n, b = (oc._code(c) for c in ("brow", "fvtg", "pnam", "pbnd"))
    return ("on run argv\n"
            f"\ttell application id \"{oc._bundle(FINDER)}\"\n"
            "\t\tset theRows to {}\n"
            f"\t\tset theWindows to every «class {w}»\n"
            "\t\trepeat with i from 1 to count of theWindows\n"
            "\t\t\tset theWindow to item i of theWindows\n"
            "\t\t\tset thePath to \"\"\n"
            "\t\t\ttry\n"
            f"\t\t\t\tset thePath to POSIX path of ((«class {t}» of theWindow) as alias)\n"
            "\t\t\tend try\n"
            f"\t\t\tset end of theRows to {{«class {n}» of theWindow, «class {b}» of theWindow, thePath}}\n"
            "\t\tend repeat\n"
            "\t\treturn theRows\n"
            "\tend tell\n"
            "end run\n")


def _notes_selection_script() -> str:
    sel, ctr, ident, name, fol, acct, cls = (oc._code(c) for c in ("seln", "cntr", "ID  ", "pnam", "cfol", "acct",
                                                                    "pcls"))
    return ("on run argv\n"
            f"\ttell application id \"{oc._bundle(NOTES)}\"\n"
            f"\t\tset theNotes to «class {sel}»\n"
            "\t\tif (count of theNotes) is 0 then return {}\n"
            f"\t\tset theFolder to «class {ctr}» of (item 1 of theNotes)\n"
            f"\t\tset theId to «class {ident}» of theFolder\n"
            f"\t\tset theName to «class {name}» of theFolder\n"
            "\t\tset theChain to {theName}\n"
            f"\t\tset theParent to «class {ctr}» of theFolder\n"
            f"\t\tset theTwins to count of (every «class {fol}» of theParent whose «class {name}» is theName)\n"
            "\t\trepeat 12 times\n"
            f"\t\t\tset beginning of theChain to «class {name}» of theParent\n"
            f"\t\t\tif («class {cls}» of theParent) is «class {acct}» then return {{theId, theTwins, theChain}}\n"
            f"\t\t\tset theParent to «class {ctr}» of theParent\n"
            "\t\tend repeat\n"
            "\t\treturn {}\n"
            "\tend tell\n"
            "end run\n")


#: the scope reads (read-only, constant, no argv input)
SCOPE_SCRIPTS: Dict[str, OsaScript] = {
    FINDER: OsaScript("scope/finder_windows", FINDER, "read", _finder_windows_script(),
                      ("brow", "fvtg", "pnam", "pbnd"),
                      "every Finder window: name, bounds, POSIX path of (target as alias)"),
    NOTES: OsaScript("scope/notes_selection", NOTES, "read", _notes_selection_script(),
                     ("seln", "cntr", "ID  ", "pnam", "cfol", "acct", "pcls"),
                     "the selected note's folder: id, same-named siblings, account and parents"),
}

#: pixels a Finder window's bounds may differ from its AX frame and still be that window
FRAME_TOLERANCE = 2.0


def _nfc(text: Any) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", str(text or "")).strip()


def _notes_folder_title(window_title: str) -> str:
    from anyuse.context import container_scope as CS
    return _nfc(CS.scope_title(NOTES, window_title))


def match_finder_window(rows: Sequence[Any], title: str,
                        frame: Optional[Tuple[float, float, float, float]],
                        tolerance: float = FRAME_TOLERANCE) -> Dict[str, Any]:
    """The ONE Finder window (``[name, {l, t, r, b}, path]`` rows of :data:`SCOPE_SCRIPTS`) that is the AX window
    titled ``title`` with ``frame`` ``(x, y, w, h)``: same name (NFC), and with a frame the same bounds within
    ``tolerance``. Several matches, none, or a window whose target is no folder: no answer (AX decides)."""
    want = _nfc(title)
    named = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) != 3:
            continue
        name, bounds, path = r
        if _nfc(name) == want:
            named.append((bounds, str(path or "")))
    if frame is not None:
        x, y, w, h = frame

        def near(b):
            try:
                return all(abs(float(u) - v) <= tolerance for u, v in zip(b, (x, y, x + w, y + h))) and len(b) == 4
            except (TypeError, ValueError):
                return False
        named = [m for m in named if near(m[0])]
    if not named:
        return {"status": "no_answer", "why": "no Finder window with this name and frame"}
    if len(named) > 1:
        return {"status": "no_answer", "why": f"{len(named)} Finder windows with this name and frame"}
    path = named[0][1]
    if not path.startswith("/"):
        return {"status": "no_answer", "why": "the window's target is not a folder"}
    return {"status": "ok", "key": path, "title": want}


def notes_answer(value: Any, window_title: str) -> Dict[str, Any]:
    """The Notes read (``{folder id, same-named siblings, [account, parents..., folder]}``) as a scope answer.
    The folder must be the one the window names (a smart folder, a search or "all notes" shows notes of other
    folders: no answer). Two same-named folders under one parent are told apart by the folder's id."""
    if not isinstance(value, list) or len(value) != 3 or not isinstance(value[2], list) or len(value[2]) < 2:
        return {"status": "no_answer", "why": "no single selected note with a folder and an account"}
    fid, twins, chain = value
    chain = [_nfc(c) for c in chain]
    if not all(chain):
        return {"status": "no_answer", "why": "an unnamed folder or account"}
    shown = _notes_folder_title(window_title)
    if shown and chain[-1] != shown:
        return {"status": "no_answer", "why": "the selected note's folder is not the folder the window names"}
    key = "/".join(chain)
    try:
        twins = int(twins)
    except (TypeError, ValueError):
        twins = 2
    if twins > 1:
        key = f"{key}#{fid}"
    return {"status": "ok", "key": key, "title": chain[-1], "folder_id": str(fid)}


class ScriptScopeReader:
    """The Apple-Events source for ``context/container_scope.set_script_reader`` (the guest builds it with the real
    permission check and ``osascript`` runner; tests with fakes).

    Per query: the permission is read WITHOUT asking (``permission(bundle)``); anything but 0 answers
    ``automation_not_granted`` / ``not_running`` with the code, and nothing is sent: the reader never prompts
    (only the grant job raises the prompt). Then the app's read (:data:`SCOPE_SCRIPTS`) runs through
    ``runner(argv, script) -> (rc, stdout, stderr)``: Finder lists every window (name, bounds, target path) and the
    query's window is matched by name and frame (``frame_of(query)`` when the query has none); Notes reads the
    selected note's folder chain. Reads only; a failure is an answer, never an exception."""

    def __init__(self, permission: Callable[[str], int], runner: Callable[[List[str], str], Tuple[int, str, str]],
                 frame_of: Optional[Callable[[Dict[str, Any]], Any]] = None):
        self.permission, self.runner, self.frame_of = permission, runner, frame_of
        self.calls = 0

    def _run(self, app: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
        code = self.permission(app)
        if code != oc.AUTOMATION_GRANTED:
            return None, {"status": status_reason(code).split(":")[0], "code": code, "reason": status_reason(code)}
        sc = SCOPE_SCRIPTS[app]
        self.calls += 1
        rc, out, err = self.runner(sc.argv(), sc.script)
        if rc != 0:
            return None, {"status": "no_answer", "why": "osascript failed", "error": error_number(err)}
        try:
            return parse_result(out), None
        except ValueError:
            return None, {"status": "no_answer", "why": "unreadable answer"}

    def finder_windows(self) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        """Every Finder window's ``[name, bounds, path]`` (for the guest's raw report), or the reason none."""
        value, why = self._run(FINDER)
        return (value if isinstance(value, list) else []), why

    def __call__(self, query: Dict[str, Any]) -> Dict[str, Any]:
        app = query.get("app")
        if app not in SCOPE_SCRIPTS:
            return {"status": "no_answer", "why": "no Apple-Events scope read for this app"}
        try:
            value, why = self._run(app)
            if why is not None:
                return why
            if app == FINDER:
                frame = query.get("frame")
                if frame is None and self.frame_of is not None:
                    frame = self.frame_of(query)
                return match_finder_window(value if isinstance(value, list) else [], query.get("title") or "",
                                           tuple(frame) if frame else None)
            return notes_answer(value, query.get("title") or "")
        except Exception as exc:  # noqa: BLE001 - no answer; AX decides
            return {"status": "error", "why": type(exc).__name__}


def plug_scope_reader(permission: Callable[[str], int], runner: Callable[[List[str], str], Tuple[int, str, str]],
                      frame_of: Optional[Callable[[Dict[str, Any]], Any]] = None) -> ScriptScopeReader:
    """THE wiring of the Apple-Events container read: a :class:`ScriptScopeReader` over
    ``permission`` and ``runner`` plugged into ``context/container_scope`` for this whole process, so every container
    read (the recorder's ``scope``, the resolver's place filter, the executor's check before acting, the row lane's
    ``scope_refusal``, the watcher's row scope) asks Apple Events first. Returns the plugged reader.

    App-neutral: the reader answers for each app with a scope read (:data:`SCOPE_SCRIPTS`); an app it cannot read, an
    app not granted (``automation_not_granted``; the permission is read without asking, nothing is sent), not running
    or not answering keeps the AX reading. ``container_scope.set_script_reader(None)`` unplugs it. The guest calls
    this with the real permission check and ``osascript`` runner (``execution/guest/applescript_guest.plug_scope_reader``);
    the offline benches with a fake guest."""
    from anyuse.context import container_scope as CS
    reader = ScriptScopeReader(permission, runner, frame_of=frame_of)
    CS.set_script_reader(reader)
    return reader


# ---------------------------------------------------------------------------
# 5. The script rung: a recorded step as Apple Events with OBJECT identity, planned from the app's dictionary
# ---------------------------------------------------------------------------
#
# A step of a recorded or compiled run (``{"step": {effect, app, target, value, mode, find, scope}, "resolved":
# {op, label, via, route, option, ...}}``, the format of recorded runs and of compiled routines) becomes an app-neutral
# INTENT (:func:`intent_from_step`: create X in Y, set / read P of O, open / show / select O, a window shows a
# container, an enumerated window setting by its UI label, write a document's text and save it). The planner
# (``execution/script_planner.py``) maps the intent onto the app's OWN dictionary (``execution/script_dictionary.py``,
# read from the bundle at runtime): no per-app table of codes. Its program names objects by path, id or unique name
# (never a UI position), declares the expected effect and reads it back through the dictionary; today's check after
# the step runs unchanged on top (the rung replaces the ACTION, not the proof). A step the dictionary does not cover
# (or that stays ambiguous without a chooser) stays on the AX rung with the reason. Not granted / not running: the
# AX rung, nothing sent. A failure AFTER an action was sent is reported, never retried on the AX rung. Irreversible
# commands are never emitted unless the recorded step itself was that action.

from anyuse.execution import script_dictionary as SD   # noqa: E402
from anyuse.execution import script_planner as SP      # noqa: E402

DICTIONARY = "com.apple.Dictionary"

#: app name in a recorded step -> bundle id (the recording's naming, not dictionary facts)
APP_BUNDLES = {"Finder": FINDER, "Notes": NOTES, "TextEdit": TEXTEDIT, "Dictionary": DICTIONARY}
#: the recording's CONTEXT per app: items named in the file system folder the run is in
#: (a path), or objects in the container the app shows (a name, resolved to an id). Not dictionary facts.
FILE_CONTEXT_APPS = ("Finder", "TextEdit")

#: an app's dictionary (``script_dictionary.AppDictionary``); the old name kept for the job agent and the bench
RouteDictionary = SD.AppDictionary
Plan = SP.PlanOut

#: commands the rung never emits unless the recorded step was exactly that action (code -> what it does); the
#: planner also refuses any command whose dictionary NAME is irreversible (``script_planner.IRREVERSIBLE_VERBS``)
IRREVERSIBLE = {"coredelo": "delete", "fndrempt": "empty trash", "fndrfera": "erase", "aevtquit": "quit",
                "fndrejct": "eject", "fndrshut": "shut down", "fndrrest": "restart", "fndrslep": "sleep",
                "coreclos+no": "close without saving"}


def irreversible_uses(script: str, rd: Optional[SD.AppDictionary] = None) -> List[str]:
    """The irreversible commands a script sends: any ``«event»`` of :data:`IRREVERSIBLE`, a close that does not save
    (``«event coreclos»`` with ``saving no``), and (given the app's dictionary) any event whose command name is
    irreversible (send, delete, remove, ...)."""
    found = sorted({c for c in IRREVERSIBLE if "+" not in c and f"«event {c}»" in script})
    if "«event coreclos»" in script and "«constant ****no  »" in script:
        found.append("coreclos+no")
    if rd is not None:
        for code in re.findall(r"«event (.{8})»", script):
            cmd = rd.command(code)
            if cmd is not None and SP.is_irreversible_name(cmd.name) and code not in found:
                found.append(code)
    return found


def route_dictionary(dump: Dict[str, Any], cocoa_standard_xml: Optional[str] = None,
                     nscore: Optional[Dict[str, Any]] = None) -> SD.AppDictionary:
    """An app's dictionary from the ``discover_desktop.py dump`` shape (``script_dictionary.from_dump``); ``nscore``:
    the legacy NSCoreSuite (Foundation's files) when the dump did not carry it."""
    if nscore is not None and dump.get("script_suite") and not dump.get("nscore_suite"):
        dump = {**dump, "nscore_suite": nscore}
    return SD.from_dump(dump, cocoa_standard_xml)


@dataclass
class RungContext:
    """What a replay knows about where it is: the file system folder the run is in (a POSIX path), the container an
    object app shows and the object shown (names, and ids once resolved), the open document (a POSIX path)."""
    finder_folder: str = ""
    notes_folder: str = ""
    notes_folder_id: str = ""
    note_name: str = ""
    note_id: str = ""
    document: str = ""


_CHECK_PATH = re.compile(r"^container verified: (/.+)$")

#: effects that are not an app step on either rung
NEUTRAL_EFFECTS = ("launch_app", "extract", "refuse", "human_gate")


def _join(folder: str, name: str) -> str:
    return folder.rstrip("/") + "/" + name if folder and name else ""


def _norm_path(p: str) -> str:
    p = _nfc(p)
    return (p.rstrip("/") or "/") if p.startswith("/") else p


def _option(step: Dict[str, Any], res: Dict[str, Any]) -> Tuple[str, str]:
    """(group, option label) of a toggle step: the resolution's option and its menu path, else the step's."""
    opt = res.get("option") or {}
    label = _nfc(opt.get("label") or step.get("value") or "")
    path = _nfc(opt.get("path") or "")
    group = path.rsplit(" > ", 1)[0] if " > " in path else _nfc(((res.get("entry") or {}).get("group")) or "")
    return group, label


def _fs_navigate(rec: Dict[str, Any]) -> bool:
    """A navigation in the file system (the recording names a path, or moves relative to the folder it is in)."""
    res = rec.get("resolved") or {}
    return res.get("via") is not None or bool(_CHECK_PATH.match(_nfc(rec.get("check") or "")))


def intent_from_step(rec: Dict[str, Any], ctx: RungContext) -> Any:
    """A recorded step as an app-neutral ``script_planner.Intent``, or a ``Plan`` with rung ``none`` (no app action
    on either rung) / ``ax`` (no identity for it in the run's context). Pure: reads ``rec`` and ``ctx`` only."""
    step, res = rec.get("step") or {}, rec.get("resolved") or {}
    eff, app = step.get("effect") or "", step.get("app") or ""
    if eff in NEUTRAL_EFFECTS or app in ("", "none"):
        return Plan("none", reason=f"{eff or 'step'}: not an app action on either rung")
    bundle = APP_BUNDLES.get(app, app)
    label = _nfc(res.get("label") or "")
    files = app in FILE_CONTEXT_APPS
    here = _norm_path(ctx.finder_folder)

    def ax(why: str) -> Any:
        return Plan("ax", bundle_id=bundle, reason=why)

    folder_ref = SP.ObjRef(noun="folder", name=_nfc(ctx.notes_folder or step.get("scope") or ""),
                           id=ctx.notes_folder_id)
    if eff == "navigate":
        if _fs_navigate(rec):
            via = res.get("via")
            m = _CHECK_PATH.match(_nfc(rec.get("check") or ""))
            if via in ("shown", None) and here and os.path.basename(here) == label:
                return SP.Intent(bundle, "navigate", SP.ObjRef(path=here), read_only=True)
            if via in ("shown", None) and m and os.path.basename(_norm_path(m.group(1))) == label:
                return SP.Intent(bundle, "navigate", SP.ObjRef(path=_norm_path(m.group(1))), read_only=True)
            if via == "sub" and here and label:
                return SP.Intent(bundle, "navigate", SP.ObjRef(path=_join(here, label)))
            if via == "up" and here:
                return SP.Intent(bundle, "navigate", SP.ObjRef(path=os.path.dirname(here) or "/"))
            return ax(f"navigate via {via!r}: no object path from the label alone")
        name = label or _nfc(step.get("target") or "")
        if not name:
            return ax("no container name")
        return SP.Intent(bundle, "navigate", SP.ObjRef(noun="folder", name=name))
    if eff in ("open_item", "select_item"):
        if files:
            if not (ctx.finder_folder and label):
                return ax("no folder path for the item")
            p = _join(here, label)
            if eff == "select_item":
                return SP.Intent(bundle, "select", SP.ObjRef(path=p))
            # a document is opened by the app the step is FOR, through its own open; a recording "via Finder" too
            # (Finder's open of a document is refused live; a file manager never opens a path: OPEN_BY_OWNER)
            return SP.Intent(bundle, "command", SP.ObjRef(path=p), verb="open")
        if not (label and (folder_ref.name or folder_ref.id)):
            return ax("no item name or container")
        return SP.Intent(bundle, "show", SP.ObjRef(name=label, parent=folder_ref))
    if eff == "object_create":
        text = str(step.get("value") or "")
        if files:
            name = _nfc(text)
            if not name or "/" in name or "\n" in name:
                return ax("not a single item name")
            if not ctx.finder_folder:
                return ax("no folder path to create in")
            return SP.Intent(bundle, "create", SP.ObjRef(path=here), noun=step.get("target") or "folder",
                             values={"name": name})
        title, _sep, body = text.partition("\n")
        if not (title.strip() and (folder_ref.name or folder_ref.id)):
            return ax("no title or container")
        return SP.Intent(bundle, "create", folder_ref, noun=step.get("target") or "note",
                         values={"name": title.strip(), SP.CONTENT: body})
    if eff == "toggle_select":
        group, opt = _option(step, res)
        return SP.Intent(bundle, "option", SP.ObjRef(front=True, noun="window"), option=opt, group=group)
    if eff == "read_content":
        if files:
            if not ctx.document:
                return ax("no document path")
            return SP.Intent(bundle, "get", SP.ObjRef(noun=step.get("target") or "document", path=ctx.document),
                             read="plain text")
        if not (ctx.note_id or (ctx.note_name and (folder_ref.name or folder_ref.id))):
            return ax("no object identity to read")
        obj = SP.ObjRef(noun=step.get("target") or "", id=ctx.note_id, name="" if ctx.note_id else ctx.note_name,
                        parent=folder_ref)
        return SP.Intent(bundle, "get", obj, read="plain text")
    if eff == "doc_save":
        if not ctx.document:
            return ax("no document path")
        value = str(step.get("value") or "")
        return SP.Intent(bundle, "set_save", SP.ObjRef(noun="document", path=ctx.document), values={"text": value},
                         compose={"mode": step.get("mode") or "replace", "value": value,
                                  "find": str(step.get("find") or "")})
    return ax(f"{app} {eff}: no intent for this effect (menu setting, search)")


def plan_step(rec: Dict[str, Any], ctx: RungContext, dictionaries: Dict[str, Optional[SD.AppDictionary]],
              chooser: Optional[SP.Chooser] = None) -> Plan:
    """The rung for one recorded step: ``script`` with a program (identity, declared effect, read-back) planned from
    the app's own dictionary; ``ax`` with the reason; ``none`` for a step that is no app action on either rung."""
    step = rec.get("step") or {}
    app = step.get("app") or ""
    if (step.get("effect") or "") not in NEUTRAL_EFFECTS and app not in ("", "none"):
        rd = dictionaries.get(APP_BUNDLES.get(app, app))
        if rd is None or rd.empty:
            return Plan("ax", bundle_id=APP_BUNDLES.get(app, app), reason="no scripting dictionary")
    it = intent_from_step(rec, ctx)
    if not isinstance(it, SP.Intent):
        return it
    return SP.plan_intent(it, dictionaries.get(it.app), chooser)


def advance(ctx: RungContext, rec: Dict[str, Any], result: Optional["RungResult"] = None) -> None:
    """Update ``ctx`` after a step ran (on either rung): the folder entered, the object shown, the document open."""
    step, res = rec.get("step") or {}, rec.get("resolved") or {}
    eff, app = step.get("effect") or "", step.get("app") or ""
    label = _nfc(res.get("label") or "")
    ok_script = result is not None and result.rung == "script" and result.ok
    if eff == "navigate" and _fs_navigate(rec):
        m = _CHECK_PATH.match(_nfc(rec.get("check") or ""))
        if ok_script and result.expect.get("path"):
            ctx.finder_folder = result.expect["path"]
        elif m:
            ctx.finder_folder = _norm_path(m.group(1))
        elif res.get("via") == "sub" and ctx.finder_folder and label:
            ctx.finder_folder = _join(_norm_path(ctx.finder_folder), label)
        elif res.get("via") == "up" and ctx.finder_folder:
            ctx.finder_folder = os.path.dirname(_norm_path(ctx.finder_folder)) or "/"
    elif eff == "open_item" and app in FILE_CONTEXT_APPS and ctx.finder_folder and label:
        ctx.document = _join(_norm_path(ctx.finder_folder), label)
    elif eff == "navigate" and app not in FILE_CONTEXT_APPS:
        name = label or _nfc(step.get("target") or "")
        if name != ctx.notes_folder:
            ctx.notes_folder, ctx.notes_folder_id = name, ""
        if ok_script and result.observed_ids.get("object_id"):
            ctx.notes_folder_id = result.observed_ids["object_id"]
        ctx.note_name, ctx.note_id = "", ""
    elif eff in ("open_item", "select_item") and app not in FILE_CONTEXT_APPS:
        ctx.note_name = label
        ctx.note_id = result.observed_ids.get("object_id", "") if ok_script else ""
        if ok_script and result.observed_ids.get("container_id"):
            ctx.notes_folder_id = result.observed_ids["container_id"]
        if not ctx.notes_folder and step.get("scope"):
            ctx.notes_folder = _nfc(step.get("scope"))


# -- running: the guards of osa_compile.run_osa, per script ---------------------------------------------------

class _RungCommand(oc.OsaCommand):
    """An ``osa_compile.OsaCommand`` whose argv asks ``osascript`` for its answer as source (``-s s``)."""

    def argv(self, values: Sequence[Any]) -> List[str]:
        if len(values) != len(self.args):
            raise ValueError(f"{self.id}: expected {len(self.args)} argument(s), got {len(values)}")
        return list(OSASCRIPT_ARGV) + [str(v) for v in values]


@dataclass
class OsaCall:
    ok: bool
    value: Any = None
    refused: Optional[str] = None
    sent: bool = False
    error: Optional[int] = None
    ms: float = 0.0


class OsaCaller:
    """Runs the rung's scripts with today's guards (``osa_compile.run_osa``): the permission read without asking
    (anything but 0 refuses, nothing sent); an action also needs the drive gate, every path argument under the
    disposable root, an actuate decision of the human gate (``hil_gate.HILGate``) and the foreground interlock.
    Irreversible commands are refused unless the recorded step was that action (``allow``)."""

    def __init__(self, permission: Callable[[str], int], runner: Callable[[List[str], str], Tuple[int, str, str]],
                 gate: Any = None, drive_enabled: bool = False, scratch_root: Optional[str] = None):
        self.permission, self.runner = permission, runner
        self.gate, self.drive_enabled, self.scratch_root = gate, drive_enabled, scratch_root
        self.sent: List[str] = []

    def status(self, bundle: str) -> int:
        return self.permission(bundle)

    def call(self, script: Any, values: Sequence[Any] = (), path_args: Sequence[int] = (),
             allow: Sequence[str] = ()) -> OsaCall:
        if isinstance(script, oc.OsaCommand):
            sid, bundle, body, purpose = script.id, script.bundle_id, script.script, "action"
            specs = tuple(oc.ArgSpec(a.name, a.type, a.under_scratch) for a in script.args)
        else:
            sid, bundle, body, purpose = script.id, script.bundle_id, script.script, script.purpose
            specs = tuple(oc.ArgSpec(f"arg{i + 1}", "text", i in path_args) for i in range(len(values)))
        bad = [c for c in irreversible_uses(body) if c not in allow]
        if bad:
            return OsaCall(False, refused=f"irreversible command(s) {bad}: never used by the script rung")
        cmd = _RungCommand(sid, bundle, "script_rung", "script_rung", "read" if purpose == "read" else "write",
                           specs, body, (), "", "", "read_only" if purpose == "read" else "side_effecting")
        res = oc.run_osa(cmd, list(values), permission=self.permission, runner=self.runner, gate=self.gate,
                         drive_enabled=self.drive_enabled, scratch_root=self.scratch_root)
        if res.refused:
            return OsaCall(False, refused=res.refused, sent=False)
        if purpose != "read":
            self.sent.append(sid)
        if not res.ok:
            return OsaCall(False, sent=purpose != "read", error=error_number(res.stderr), ms=res.ms,
                           refused=None)
        try:
            value = parse_result(res.stdout)
        except ValueError:
            value = res.stdout.strip()
        return OsaCall(True, value=value, sent=purpose != "read", ms=res.ms)


@dataclass
class RungResult:
    rung: str                                   # "script" | "ax" | "none"
    shape: str = ""
    reason: str = ""
    ok: Optional[bool] = None                   # the script ran and its declared effect was met
    sent: bool = False                          # an action event was sent
    value: Any = None                           # a read's value
    expect: Dict[str, Any] = None               # type: ignore[assignment]
    observed: Any = None
    effect_met: Optional[bool] = None
    identity: str = ""
    observed_ids: Dict[str, str] = None         # type: ignore[assignment]
    scripts: List[str] = None                   # type: ignore[assignment]

    def __post_init__(self):
        self.expect = dict(self.expect or {})
        self.observed_ids = dict(self.observed_ids or {})
        self.scripts = list(self.scripts or [])

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _ax(plan: Plan, reason: str, **kw: Any) -> RungResult:
    return RungResult("ax", plan.shape, reason, identity=plan.identity, expect=plan.expect, **kw)


#: an app without its own document class (a file manager) never opens a path on the script rung (live:
#: Finder's open of a document raised a CoreServicesUIAgent "(null) has no permission" dialog; nothing opened)
FINDER_OPEN_REFUSED = SP.OPEN_BY_OWNER
#: seconds a POLLED read-back (an open's document) is read again before its effect counts as not met (measured live)
OPEN_WAIT_S = 10.0
POLL_S = 0.25

_TAG = re.compile(r"<[^>]+>")


def _text_norm(s: Any) -> str:
    import html
    return " ".join(html.unescape(_TAG.sub(" ", _nfc(s))).split())


def text_meets(read: Any, want: Any) -> bool:
    """A text read-back meets the value: equal, or equal / containing it once markup and spacing are folded (an app
    may store rich text: Notes' body reads back as HTML)."""
    if read is None:
        return False
    if str(read) == str(want):
        return True
    r, w = _text_norm(read), _text_norm(want)
    return r == w or (bool(w) and w in r)


def _compare(chk: SP.Check, v: Any, env: Dict[str, Any]) -> Optional[bool]:
    k = chk.kind
    if k == "value":
        return v is not None
    if k == "true":
        return v is True
    if k == "count1":
        try:
            return int(v) == 1
        except (TypeError, ValueError):
            return False
    if k == "path":
        return _norm_path(str(v or "")) == _norm_path(str(env.get(chk.sym, "")))
    as_list = v if isinstance(v, list) else ([] if v in (None, "") else [v])
    if k == "paths":
        return [_norm_path(str(x)) for x in as_list] == [_norm_path(str(env.get(s, ""))) for s in chk.syms]
    if k == "ids":
        return [str(x) for x in as_list] == [str(env.get(s, "")) for s in chk.syms]
    if k == "id":
        return str(v) == str(env.get(chk.sym, ""))
    if k == "contains_id":
        return str(env.get(chk.sym, "")) in [str(x) for x in as_list]
    if k == "text":
        return text_meets(v, env.get(chk.sym, ""))
    if k == "date":
        return isinstance(v, list) and [int(x) for x in v] == list(env.get(chk.sym) or ())
    if k == "scalar":
        want = str(env.get(chk.sym, ""))
        if str(v).casefold() == want.casefold():
            return True
        try:
            return float(v) == float(want)
        except (TypeError, ValueError):
            return want.casefold() in ("true", "false") and str(bool(v)).casefold() == want.casefold()
    return None


def _argv(script: SP.Script, env: Dict[str, Any]) -> List[str]:
    out = []
    for sym in script.symbols:
        if "#" in sym:
            base, i = sym.rsplit("#", 1)
            out.append(str(int(env[base][int(i)])))
        else:
            out.append(str(env[sym]))
    return out


#: bind symbols -> the observed ids the context follows
_OBSERVED = {"$id:object": "object_id", "$id:container": "container_id", "$created": "created_id",
             "$id:window": "window_id"}


def run_plan(plan: Plan, caller: OsaCaller, dictionaries: Optional[Dict[str, Optional[SD.AppDictionary]]] = None, *,
             read_file: Optional[Callable[[str], Optional[str]]] = None,
             front_document: Optional[Callable[[], Optional[str]]] = None,
             exists: Optional[Callable[[str], bool]] = None, open_wait_s: float = OPEN_WAIT_S,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> RungResult:
    """Run one planned program, or say why the step stays on the AX rung.

    Order: the permission (not granted / not running: ``ax``, nothing sent); the identity reads (a name to an id,
    the front window to its id; none or several: ``ax``, nothing sent); the read of the old value a write composes
    on (not composable: ``ax``); the ONE action through :class:`OsaCaller` (a guard's refusal: ``ax`` with the
    refusal, nothing sent); the declared effect read back through the dictionary, then the observers the dictionary
    cannot show (``read_file`` / ``exists`` / ``front_document``, the guest's). An action that was sent and failed
    answers ``script`` with ``ok`` False: it is never retried on the AX rung. A read-back that cannot be read is
    UNKNOWN (``effect_met`` None), a mismatch is a deviation (``ok`` False). A read-back the plan marks ``poll`` (an
    open's document: the app may answer the open first) is read again every ``POLL_S`` up to ``open_wait_s``."""
    if plan.rung != "script" or plan.program is None:
        return RungResult(plan.rung, plan.shape, plan.reason, identity=plan.identity, expect=plan.expect)
    code = caller.status(plan.bundle_id)
    if code != oc.AUTOMATION_GRANTED:
        return _ax(plan, status_reason(code))
    prog = plan.program
    env: Dict[str, Any] = dict(prog.literals)
    out = RungResult("script", plan.shape, identity=plan.identity, expect=plan.expect)

    def read(script: SP.Script) -> OsaCall:
        out.scripts.append(script.id)
        return caller.call(script, _argv(script, env))

    for script, bind, kind in prog.resolves:
        r = read(script)
        if not r.ok:
            return _ax(plan, f"identity read failed (error {r.error}): {script.describe}", scripts=out.scripts)
        if kind == "ids":
            ids = r.value if isinstance(r.value, list) else ([r.value] if r.value not in (None, "") else [])
            if len(ids) != 1:
                return _ax(plan, f"{len(ids)} objects match ({script.describe}): the identity is not unique",
                           scripts=out.scripts)
            env[bind] = str(ids[0])
        elif kind == "count":
            n = r.value if isinstance(r.value, int) else -1
            if n != 1:
                return _ax(plan, f"{n} objects match ({script.describe}): the identity is not unique",
                           scripts=out.scripts)
            env[bind] = ""
        else:
            if r.value in (None, ""):
                return _ax(plan, f"no {script.describe}", scripts=out.scripts)
            env[bind] = str(r.value)
        if bind in _OBSERVED and env[bind]:
            out.observed_ids[_OBSERVED[bind]] = env[bind]
    old = ""
    if prog.pre_read is not None:
        from anyuse.execution import desktop_e2e4 as W
        r = read(prog.pre_read[0])
        if not r.ok:
            return _ax(plan, f"the old value could not be read (error {r.error})", scripts=out.scripts)
        old = str(r.value or "")
        c = prog.compose or {}
        new, err = W.compose(old, c.get("mode") or "replace", c.get("value") or "", c.get("find") or "")
        if err:
            return _ax(plan, f"the write is not composable: {err}", scripts=out.scripts)
        env["@old"], env["@new"] = old, new
        out.value = new
    if prog.action is not None:
        a = prog.action
        path_args = tuple(i for i, s in enumerate(a.symbols) if s.startswith("@path"))
        out.scripts.append(a.id)
        r = caller.call(a, _argv(a, env), path_args, allow=prog.allow)
        out.sent = out.sent or r.sent
        if r.refused:
            return _ax(plan, f"script rung refused before sending: {r.refused}", scripts=out.scripts)
        if not r.ok:
            out.ok = False
            out.reason = f"{plan.shape} failed after sending (error {r.error}); not retried on the AX rung"
            return out
        out.observed = r.value
        if prog.result_sym:
            env[prog.result_sym] = "" if r.value is None else str(r.value)
            if env[prog.result_sym]:
                out.observed_ids[_OBSERVED[prog.result_sym]] = env[prog.result_sym]
    met: Optional[bool] = True
    first: Any = None
    for i, (script, chk) in enumerate(prog.readbacks):
        r = read(script)
        if chk.poll:
            t0 = clock()
            while (not r.ok or _compare(chk, r.value, env) is False) and clock() - t0 < open_wait_s:
                sleep(POLL_S)
                r = caller.call(script, _argv(script, env))
        if not r.ok:
            if prog.action is None:
                return _ax(plan, f"read failed (error {r.error})", scripts=out.scripts)
            if chk.poll:                    # polled to the bound and never there: the effect did not happen
                met = False
                continue
            met = met if met is False else None
            continue
        if chk.kind == "value":
            out.value = r.value
        elif i == 0:
            first = ([_norm_path(str(x)) for x in r.value] if chk.kind == "paths" and isinstance(r.value, list)
                     else _norm_path(str(r.value)) if chk.kind == "path" else r.value)
        ok = _compare(chk, r.value, env)
        if ok is False:
            met = False
        elif ok is None and met is True:
            met = None
    doc: Any = None
    for ob in prog.observers:
        k, path = ob.get("kind"), ob.get("path") or ""
        if k == "file_text":
            from anyuse.execution import desktop_e2e4 as W
            got = read_file(path) if read_file is not None else None
            if got is not None:
                c = prog.compose or {}
                if W.check_text(old, str(got), c.get("mode") or "replace", c.get("value") or "",
                                c.get("find") or "") is not None:
                    met = False
        elif k == "exists":
            if ob.get("returned") and _norm_path(str(out.observed or "")) != _norm_path(path):
                met = False
            if exists is not None and not exists(path):
                met = False
        elif k == "front_document":
            doc = front_document() if front_document is not None else None
            if doc is None:
                met = met if met is False else None
            elif _norm_path(doc) != _norm_path(path):
                met = False
    if prog.result_sym and out.observed is not None:
        out.observed = _norm_path(str(out.observed)) if str(out.observed).startswith("/") else out.observed
    elif first is not None:
        out.observed = first
    else:
        out.observed = doc
    out.effect_met = met
    out.ok = met is not False
    if met is False:
        out.reason = "the declared effect was not observed (a deviation: stop)"
    return out


def try_step(rec: Dict[str, Any], ctx: RungContext, caller: OsaCaller,
             dictionaries: Dict[str, Optional[SD.AppDictionary]], chooser: Optional[SP.Chooser] = None,
             **observers: Any) -> RungResult:
    """Plan and run one recorded step on the script rung, then update ``ctx``. A result with rung ``ax`` means:
    run today's AX step (its reason is recorded); ``none``: not an app step."""
    plan = plan_step(rec, ctx, dictionaries, chooser)
    res = run_plan(plan, caller, dictionaries, **observers)
    advance(ctx, rec, res)
    return res
