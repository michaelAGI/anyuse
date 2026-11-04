"""The desktop architecture test: turning the picker's SAFE hand-offs into
successes without giving up safety, plus ``desktop_e2e7``'s other failure causes.

Builds on ``desktop_e2e`` ... ``desktop_e2e7`` (all left
byte-identical) and is PURE like them (no pyobjc, no subprocess, no network):
the guest, the planner and the picker are injected callables.

``desktop_e2e7`` did not replicate ``desktop_e2e6`` (10/15 tasks): 12 of its 17 failed
runs were SAFE picker hand-offs (10 the two option orders disagreeing, 2 "none
of these" in one order), 3 an executor stop (Notes disables "Als Liste" while a
gallery note is open), 2 the planner (``desktop_e2e2``'s restriction check read "change
only the room on the … line" as a missing restriction; a settings contradiction).

The generic pieces added here (one per fix):

1. **The picker's hand-offs** (a) *richer option descriptions*: the kind in a
   mixed list, the parent container where rows differ in it, the absolute date
   AND its period relative to today ("last month", "last year"), a content
   snippet (a file's first words, a note's first line, a folder's contents), all
   within OpenJev's 120 characters; (b) *a disagreement protocol*: when the two
   orders disagree, a pairwise follow-up between the two chosen candidates only
   (A vs B, plus "none of these"; A vs "none" when one order chose none), in
   both orders, the fields that tell them apart given as evidence in the state;
   act only when the follow-up is consistent, otherwise hand off as before. The
   configuration and t2 are calibrated on SEEN data (the task pools of
   ``desktop_e2e3`` ... ``desktop_e2e7``) before the arm.
2. **A disabled item with a declared precondition**: a menu item the app
   disables in a declared UI state (Notes: a note opened from the gallery hides
   the grid; Finder: Quick Look open; TextEdit: the Open panel open) is retried
   ONCE after the engine left that state (back to the grid, Quick Look closed,
   the panel cancelled); the item shown before is shown again.
3. **Edit scopes are not missing restrictions**: "change only the room on the
   volunteer-training line" restricts WHAT the edit changes, not where the plan
   goes: it is not fed back as a missing restriction; the saved text may change
   only lines that carry the scope's words (else the old text is restored).
4. **Duplicate-named AX elements** are told apart by their window: the named
   document's or window's element, else the FRONT window's (two TextEdit
   documents both have a text area "First Text View"); never guessed.

Plus judge = judge + ``opened_set`` (the translation lists every document the
author wants open) + ``notes_shown_ok`` (a note the task
must read may stay shown, as ``opened_ok`` for a file).
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import desktop_e2e as X
from . import desktop_e2e2 as Y
from . import desktop_e2e3 as Z
from . import desktop_e2e4 as W
from . import desktop_e2e5 as V
from . import desktop_e2e6 as U
from . import desktop_e2e7 as R7

HERE = os.path.dirname(os.path.abspath(__file__))
PICKER8_PATH = os.path.join(HERE, "desktop_e2e8_picker.json")
TASKS8_PATH = os.path.join(HERE, "desktop_e2e8_tasks.json")
GEN8 = X.GEN + "/e8"
OPTION_CHARS8 = U.OPTION_CHARS6          # OpenJev's limit per option text

# ---------------------------------------------------------------------------
# 1a. Richer option descriptions
# ---------------------------------------------------------------------------

_MOD8 = re.compile(r"modified (\d{4})-(\d{2})-(\d{2})(?: \d{2}:\d{2})?")


def period(day: _dt.date, now: Optional[_dt.datetime]) -> str:
    """The period of a date relative to today, in words a user uses ("today",
    "yesterday", "3 days ago", "this month", "last month", "this year", "last
    year", "2 years ago"); "" without today or for a future date."""
    if now is None:
        return ""
    today = now.date()
    d = (today - day).days
    if d < 0:
        return ""
    if d == 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 7:
        return f"{d} days ago"
    if (day.year, day.month) == (today.year, today.month):
        return "this month"
    prev = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    if (day.year, day.month) == prev:
        return "last month"
    if day.year == today.year:
        return "this year"
    if day.year == today.year - 1:
        return "last year"
    return f"{today.year - day.year} years ago"


def _date_cell(c: str, now: Optional[_dt.datetime]) -> str:
    """A date cell as "modified YYYY-MM-DD HH:MM (period)"."""
    lc = c.casefold()
    if lc.startswith("modified "):
        d = "modified " + c[len("modified "):].split(",")[0].strip()
    else:
        lab = V.absolute_label(c, now)
        d = "modified " + lab if lab else c
    m = _MOD8.match(d)
    if m and now is not None:
        try:
            p = period(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), now)
        except ValueError:
            p = ""
        if p and not d.endswith(")"):
            d = f"{d} ({p})"
    return d


def parse_option(text: Any, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """A candidate's text (Finder, Notes, the Open panel, menus, places) as
    fields: name, kind ("folder" / None), other cells (a note's first line, a
    place's kind), date ("modified …" with its period), content ("content: …"
    or "contains: …"). Size, file kind and the creation date are left out
    (no information in this test's data)."""
    s = " ".join(str(text or "").split())
    cells = s.split(" | ") if s else [""]
    out: Dict[str, Any] = {"name": cells[0], "kind": None, "other": [], "date": "", "content": "", "parent": ""}
    for c in cells[1:]:
        lc = c.casefold().strip()
        if lc.startswith("content:") or lc.startswith("contains:"):
            out["content"] = c
        elif lc.startswith("parent: ") and not out["parent"]:
            out["parent"] = c[len("parent: "):]
        elif lc.startswith("modified ") or V.absolute_label(c, now):
            out["date"] = out["date"] or _date_cell(c, now)
        elif U._SIZE_RE.match(c.strip()) or c.strip() in ("--", "—", "-", "–"):
            continue
        elif lc in U._KIND_WORDS or lc in ("file", "datei"):
            if lc in U._FOLDER_KIND:
                out["kind"] = "folder"
            elif lc in ("file", "datei"):
                out["kind"] = out["kind"] or "file"
        else:
            out["other"].append(c)
    return out


def fit_option8(text: Any, now: Optional[_dt.datetime] = None, mixed: bool = False, parent: str = "",
                contains: Optional[Sequence[str]] = None, limit: int = OPTION_CHARS8) -> str:
    """``desktop_e2e6``'s fitted option with this module's fields, within ``limit`` characters:
    name | kind (folder; "file" only in a list that mixes files and folders) |
    parent: <container> (only where the rows differ in it) | other cells | modified
    <absolute date> (<period>) | content (a file's first words; a folder's
    "contains: …"), the content shortened last. On-screen words are kept as they
    are (never translated)."""
    f = parse_option(text, now)
    if contains is not None and not f["content"]:
        f["content"] = "contains: " + (", ".join(contains) if contains else "nothing")
    kind = f["kind"] or ("file" if mixed and W.FILE_RE.search(f["name"] or "") else None)
    par = parent or f["parent"]
    if par:
        f["other"] = [c for c in f["other"] if X.norm(c) != X.norm("in " + par)]   # the guest's "in <folder>" cell
    parts = [f["name"] or "(empty)"] + ([kind] if kind else []) + ([f"parent: {parent or f['parent']}"]
                                                                  if (parent or f["parent"]) else [])
    parts += f["other"] + ([f["date"]] if f["date"] else [])
    base = " | ".join(parts)
    if f["content"]:
        room = limit - len(base) - 3
        if room >= 12:
            c = f["content"]
            base = base + " | " + (c if len(c) <= room else c[:room - 1].rstrip() + "…")
    if len(base) > limit:
        base = base[:limit - 1].rstrip() + "…"
    return base


def rich_options(texts: Sequence[Any], now: Optional[_dt.datetime] = None,
                 parents: Optional[Sequence[str]] = None,
                 contains: Optional[Sequence[Optional[Sequence[str]]]] = None) -> List[str]:
    """The fitted options of one candidate list: the kind marked where the
    list mixes files and folders; the parent only where the rows differ in it
    (else the state names it once)."""
    parsed = [parse_option(t, now) for t in texts]
    folders = sum(1 for p in parsed if p["kind"] == "folder")
    files = sum(1 for p in parsed if p["kind"] != "folder" and W.FILE_RE.search(p["name"] or ""))
    mixed = bool(folders and files)
    par = list(parents or [""] * len(texts))
    if len({p for p in par if p}) <= 1:
        par = [""] * len(texts)
    cont = list(contains or [None] * len(texts))
    return [fit_option8(t, now, mixed, par[i], cont[i]) for i, t in enumerate(texts)]


def _cells(opt: str) -> Dict[str, str]:
    f = parse_option(opt)
    return {"name": f["name"], "kind": f["kind"] or "", "parent": f["parent"], "other": " | ".join(f["other"]),
            "modified": f["date"][len("modified "):] if f["date"].startswith("modified ") else f["date"],
            "content": f["content"].split(":", 1)[1].strip() if ":" in f["content"] else f["content"]}


def _word_diff(a: str, b: str, n: int = 6) -> Tuple[str, str]:
    ta, tb = a.split(), b.split()
    sm = difflib.SequenceMatcher(None, [t.casefold() for t in ta], [t.casefold() for t in tb], autojunk=False)
    da, db = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            da += ta[i1:i2]
            db += tb[j1:j2]
    return " ".join(da[:n]) or "(none)", " ".join(db[:n]) or "(none)"


def differences(a: str, b: str) -> List[str]:
    """The fields that tell two candidates apart, as evidence lines: the words of
    the name only one has, the dates with their periods, the kind, the parent,
    the content words only one has. Screen text travels here (the STATE), never in
    the question."""
    ca, cb = _cells(a), _cells(b)
    out = []
    for k, label in (("name", "name"), ("kind", "kind"), ("parent", "parent"), ("other", "other"),
                     ("modified", "modified"), ("content", "content")):
        if X.norm(ca[k]) == X.norm(cb[k]):
            continue
        if k in ("name", "content", "other"):
            x, y = _word_diff(ca[k], cb[k])
            out.append(f"{label}: A has {x!r}, B has {y!r}")
        else:
            out.append(f"{label}: A {ca[k] or '(none)'!r}, B {cb[k] or '(none)'!r}")
    return out


PAIR_STATE = (" A follow-up: the two orders of the full list disagreed. Only the two candidates they chose are "
              "listed now (A = the first listed here, B = the second). They differ in: {diffs}.")
VERIFY_STATE = (" A follow-up: one order of the full list chose this candidate, the other chose none of them. "
                "Only that candidate is listed now.")

# ---------------------------------------------------------------------------
# 1b. The disagreement protocol
# ---------------------------------------------------------------------------

#: the protocol's branches: "pair" = a follow-up when the orders chose two different items; "verify" = a
#: follow-up when one order chose an item and the other "none of these"
PROTOCOLS = {"off": (), "pair": ("pair",), "pair+verify": ("pair", "verify")}


def _first_round(answers: Sequence[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int], bool, bool]:
    a0, a1 = (list(answers) + [{}, {}])[:2]
    return a0.get("index"), a1.get("index"), bool(a0.get("none")), bool(a1.get("none"))


def consistent_pick8(chooser, goal: str, state: str, options: Sequence[str], threshold: float, spend=None,
                     question: Optional[str] = None, protocol: str = "pair+verify") -> X.Pick:
    """``consistent_pick7``'s rule (both orders with "none of these"; act only when both name the
    same item, neither names none, min confidence >= t2), then the disagreement
    protocol: when the orders chose two different items A and B, ask again with A,
    B and none only (both orders; the fields that tell A and B apart in the state);
    when one order chose A and the other none, ask again with A and none only. Act
    only when the follow-up is consistent by the same rule; otherwise hand off."""
    first = R7.consistent_pick7(chooser, goal, state, options, threshold, spend=spend, question=question)
    answers = [dict(a, round="first") for a in first.answers]
    if first.index is not None:
        return X.Pick(first.index, first.reason, first.calls, first.cost, answers)
    opts = [V.option_text5(o, OPTION_CHARS8) for o in options][:R7.MAX_OPTIONS7]
    i0, i1, n0, n1 = _first_round(first.answers)
    branches = PROTOCOLS.get(protocol, ())
    sub: List[int] = []
    kind = None
    if len(first.answers) == 2 and not any(a.get("error") for a in first.answers):
        if i0 is not None and i1 is not None and not n0 and not n1 and i0 != i1 and "pair" in branches:
            sub, kind = [i0, i1], "pair"
        elif (n0 != n1) and "verify" in branches:
            a = i1 if n0 else i0
            if a is not None and 0 <= a < len(opts):
                sub, kind = [a], "verify"
    if not sub or len({X.norm(opts[i]) for i in sub}) != len(sub):
        return X.Pick(None, first.reason, first.calls, first.cost, answers)
    if kind == "pair":
        st2 = state.rstrip() + PAIR_STATE.format(diffs="; ".join(differences(opts[sub[0]], opts[sub[1]])) or
                                                 "nothing shown")
    else:
        st2 = state.rstrip() + VERIFY_STATE
    p2 = R7.consistent_pick7(chooser, goal, st2, [opts[i] for i in sub], threshold, spend=spend, question=question)
    answers += [dict(a, round=kind, sub=sub) for a in p2.answers]
    calls, cost = first.calls + p2.calls, first.cost + p2.cost
    if p2.index is not None:
        return X.Pick(sub[p2.index], f"accepted after the {kind} follow-up ({first.reason}): {p2.reason}", calls, cost,
                      answers)
    return X.Pick(None, f"{first.reason}; {kind} follow-up: {p2.reason}", calls, cost, answers)


def load_picker8(path: str = PICKER8_PATH) -> Dict[str, Any]:
    """This module's frozen acceptance configuration."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def config_of8(picker: str, spec: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
    """(t2, protocol) of this module's calibration; none before it is frozen (the arm
    cannot run)."""
    p = ((spec or load_picker8())["pickers"].get(picker) or {})
    if p.get("t2") is None or p.get("protocol") not in PROTOCOLS:
        raise KeyError(f"no configuration for the picker {picker!r} (the calibration is not frozen)")
    return float(p["t2"]), str(p["protocol"])


# ---------------------------------------------------------------------------
# 2. A disabled menu item with a declared precondition
# ---------------------------------------------------------------------------

#: UI states that make an app disable menu items, and how the engine leaves them (declared; the guest's op
#: ``precondition`` detects and restores them; nothing task-specific)
PRECONDITIONS = {
    "com.apple.Notes": ({"id": "gallery_item_open", "state": "a note opened from the gallery view hides the grid",
                         "restore": "back to the grid (Escape, else the toolbar's Zurück); the note stays selected",
                         "reshow": "notes_notes"},),
    "com.apple.finder": ({"id": "quick_look_open", "state": "Quick Look is open", "restore": "close Quick Look"},),
    "com.apple.TextEdit": ({"id": "open_panel", "state": "the Open panel is open", "restore": "cancel the panel"},),
}


def is_disabled(r: Optional[Dict[str, Any]]) -> bool:
    """A press the guest did not do because the item is disabled."""
    return (r or {}).get("status") == "failed" and str((r or {}).get("detail") or "").endswith("is disabled")


# ---------------------------------------------------------------------------
# 3. Edit scopes are not missing restrictions
# ---------------------------------------------------------------------------

EDIT_VERBS = {"change", "changes", "set", "replace", "edit", "correct", "update", "fix", "modify", "adjust",
              "rewrite", "ändere", "änder", "ändern", "setze", "setz", "setzen", "ersetze", "ersetz", "ersetzen",
              "korrigiere", "korrigier", "korrigieren", "aktualisiere", "aktualisier", "bearbeite", "bearbeit",
              "pass", "passe", "trag", "trage", "schreib", "schreibe", "mach", "mache"}
TEXT_PARTS = {"line", "lines", "row", "entry", "field", "paragraph", "sentence", "word", "number", "date", "amount",
              "price", "value", "room", "zeile", "zeilen", "eintrag", "feld", "absatz", "satz", "wort", "zahl",
              "datum", "betrag", "preis", "wert", "raum"}
CONTAINERS = X.FOLDER_WORDS | {"subfolder", "unterordner", "dictionary", "wörterbuch", "lexikon", "app", "programm",
                               "notes", "notizen", "finder", "textedit"}


_ONLY = {"only", "just", "exclusively", "solely", "nur", "ausschließlich", "ausschliesslich", "lediglich", "allein"}
_PREPS = {"in", "inside", "within", "from", "on", "at", "with", "using", "via", "under", "to", "im", "ins", "aus", "auf",
          "bei", "beim", "mit", "über", "unter", "vom", "von", "per"}


def _edit_scope(goal: str, r: Dict[str, Any]) -> bool:
    """Is this restriction phrase a scope on an EDIT ("change only the room on the
    volunteer-training line", "ändere nur in der Zeile mit dem Treffpunkt …")
    rather than a place, document or source the plan must go to ("nur im Entwurf
    für …", "only in the Archive folder")? No container (folder, dictionary, app)
    named, and either a text part (line, Zeile, entry, number …) named, or an edit
    verb right before only/nur with the thing changed between only/nur and the
    preposition ("change ONLY THE ROOM on …"; "mach nur IM Entwurf" names a place)."""
    ph = X.tokens(r.get("phrase") or "")
    if set(ph) & CONTAINERS:
        return False
    if set(ph) & TEXT_PARTS:
        return True
    k = 1 if ph and ph[0] in _ONLY else 0
    while k < len(ph) and ph[k] == "noch":
        k += 1
    if k >= len(ph) or ph[k] in _PREPS:
        return False
    g = X.tokens(goal)
    for i, t in enumerate(g):
        if ph and t == ph[0] and g[i:i + len(ph)] == ph and set(g[max(0, i - 2):i]) & EDIT_VERBS:
            return True
    return False


def edit_scopes(goal: str) -> List[Dict[str, Any]]:
    """The goal's restrictions that scope an edit."""
    return [r for r in Y.restrictions(goal) if _edit_scope(goal, r)]


def missing_restrictions8(goal: str, steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``desktop_e2e2``'s check without the edit scopes: those restrict what the edit
    changes (checked on the saved text), never where the plan goes."""
    scoped = {tuple(r["key"]) for r in edit_scopes(goal)}
    return [r for r in Y.missing_restrictions(goal, steps) if tuple(r["key"]) not in scoped]


def edit_scope_problems(scopes: Sequence[Dict[str, Any]], old: str, new: str) -> List[str]:
    """The lines a save changed that carry none of the edit scope's words (the
    old line or the new one must name the scope's key: "the volunteer-training
    line")."""
    if not scopes:
        return []
    a, b = (old or "").split("\n"), (new or "").split("\n")
    probs = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        lines = a[i1:i2] + b[j1:j2]
        for sc in scopes:
            key = list(sc["key"])
            if not any(Y._contains(X.tokens(ln), key) for ln in lines):
                probs.append(f"changed {' / '.join(x[:40] for x in a[i1:i2]) or '(nothing)'!r} outside "
                             f"{sc['phrase']!r}")
    return probs


# ---------------------------------------------------------------------------
# 4. Duplicate-named AX elements: told apart by their window
# ---------------------------------------------------------------------------

def choose_by_window(cands: Sequence[Dict[str, Any]], document: Optional[str] = None,
                     window: Optional[str] = None) -> Tuple[Optional[int], str]:
    """(the index, how) of the ONE element among same-named candidates, each
    {"window": title, "document": path or None, "front": bool}: the element in the
    named document's window, else in the named window, else in the FRONT window
    (the one the model saw: the AX text shows the front window only); None when
    that is not exactly one (refused, never guessed)."""
    def one(pred, how):
        hit = [i for i, c in enumerate(cands) if pred(c)]
        return (hit[0], how) if len(hit) == 1 else (None, f"{len(hit)} candidates {how}")
    if document:
        return one(lambda c: X.norm(c.get("document")) == X.norm(document)
                   or X.norm(os.path.basename(str(c.get("document") or ""))) == X.norm(document),
                   "in the named document's window")
    if window:
        return one(lambda c: X.norm(c.get("window")) == X.norm(window), "in the named window")
    return one(lambda c: bool(c.get("front")), "in the front window")


# ---------------------------------------------------------------------------
# judge: judge + the documents the author wants open
# ---------------------------------------------------------------------------

def _opened_set(out: Dict[str, Any], task: X.Task, start, end) -> None:
    want = [str(p) for p in task.expect.get("opened_set") or []]
    k = U._probe(task, "front")
    if not want or k is None:
        return
    e0 = ((end or [])[k] if k < len(end or []) else {}) or {}
    s0 = ((start or [])[k] if k < len(start or []) else {}) or {}
    rel = X.guest_rel
    wantr = sorted({rel(p) for p in want})
    dirs = {os.path.dirname(p) for p in wantr}
    ours = sorted({rel(d) for d in e0.get("documents") or [] if os.path.dirname(rel(d)) in dirs})
    before = sorted({rel(d) for d in s0.get("documents") or [] if os.path.dirname(rel(d)) in dirs})
    st = "ok" if ours == wantr else ("missing" if ours == before else "wrong")
    names = {os.path.basename(p) for p in wantr}
    out["unexpected"] = [u for u in out.get("unexpected") or []
                         if not (u.startswith("document opened: ") and u[17:] in names)]
    out["items"].append({"what": " and ".join(sorted(names)) + " open", "status": st,
                         "judge_open": f"open in those folders: {[os.path.basename(x) for x in ours]}"})


def _notes_shown_ok(out: Dict[str, Any], task: X.Task, start, end) -> None:
    ok = task.expect.get("notes_shown_ok") or []
    k = U._probe(task, "notes")
    if not ok or k is None:
        return
    e0 = ((end or [])[k] if k < len(end or []) else {}) or {}
    shown = (e0.get("folder"), e0.get("shown_note"))
    allowed = {(w["folder"], w["note"]) for w in ok}
    keep = [u for u in out.get("unexpected") or []
            if not (u.startswith("shown note changed: ") and shown in allowed)]
    if len(keep) != len(out.get("unexpected") or []):
        out["allowed_shown"] = f"{shown[0]} / {shown[1]} (a note the task reads)"
    out["unexpected"] = keep


def judge_from(j: Dict[str, Any], task: X.Task, start, end) -> Dict[str, Any]:
    """judge plus two translation keys:

    * ``opened_set`` (e.g. the author wants the August AND the September
      file open; ``desktop_e2e``'s ``opened`` = "the ONLY open document of its folder"
      cannot say that): ok when the open documents of those folders are exactly
      the set; missing when they are what they were at the start; else wrong. The
      documents of the set opened during the run are no unexpected change.
    * ``notes_shown_ok`` (as ``opened_ok`` for a file the task must open): a note
      the task must READ (the author's steps name it) may be the note shown at the
      end although the author's end state does not name the note shown — neither
      required nor an unexpected change. Any other note shown stays unexpected.

    Nothing else changes."""
    if not (task.expect.get("opened_set") or task.expect.get("notes_shown_ok")):
        return j
    out = json.loads(json.dumps(j))
    _opened_set(out, task, start, end)
    _notes_shown_ok(out, task, start, end)
    out["judge"] = "judge_open"
    U._recount(out)
    return out


# ---------------------------------------------------------------------------
# The architecture arm
# ---------------------------------------------------------------------------

class Arch8(R7.Arch7):
    """``Arch7`` with this module's fixes (see the module docstring)."""

    def __init__(self, guest, emap, planner=None, picker_name: Optional[str] = None, config_override=None, **kw):
        real = picker_name not in (None, "none")
        # Arch7's constructor with its frozen t2 bypassed (this module's configuration replaces it below)
        super().__init__(guest, emap, planner=planner, picker_name=picker_name, t2_override=0.0 if real else None,
                         **kw)
        self.protocol = "off"
        if real:
            # the configuration (t2, protocol) calibrated on SEEN data with this module's options; a development
            # run on the local mock may pass an override (never a measured run: the host refuses it)
            t2, proto = config_override if config_override is not None else config_of8(picker_name)
            if proto not in PROTOCOLS:
                raise KeyError(f"unknown protocol {proto!r}")
            self.threshold, self.protocol = float(t2), proto

    # -- richer options and the disagreement protocol --------------------------------
    def _contents(self, cands: List[Dict[str, Any]], kind: str) -> Optional[List[Optional[List[str]]]]:
        """A folder's contents as its snippet: the folder shown and its subfolders
        (inside this test's folders only: the guest refuses any other path)."""
        path = getattr(self, "_loc_path", None)
        if kind != "finder_locations" or not path:
            return None
        out: List[Optional[List[str]]] = []
        for c in cands:
            p = path if c.get("via") == "shown" else (path + "/" + c["label"]) if c.get("via") == "sub" else None
            names = None
            if p:
                try:
                    r = self.call("listing", path=p)
                    names = [n for n in r.get("names") or []] if r.get("status") == "done" else None
                except X.GuestError:
                    names = None
            out.append(names[:6] if names is not None else None)
        return out

    def pick(self, rec: X.Rec, goal: str, state: str, cands: List[Dict[str, Any]], step_log: Dict[str, Any],
             question: Optional[str] = None):
        """``Arch7``'s pick (absolute dates, the language bridge, "none of these",
        the date veto) with this module's option descriptions and the disagreement
        protocol of the frozen configuration."""
        if self.picker_name == "none" and self.planner is not None:
            return super().pick(rec, goal, state, cands, step_log, question)
        if self.chooser is None:
            raise X.GuestError("model needed: no picker in compiled mode")
        kind = V.cand_kind(state)
        shown = cands
        if kind:
            shown = self._dates(kind, cands, step_log)
            state = U.fitted_state(kind, V.dated_state(kind, state, self._now))
        state = U.ui_state(state, getattr(self, "_ui_lang", None))
        loc_kind = "finder_locations" if state.startswith("Frontmost application: Finder. Window:") and \
            "places the window can show" in state else None
        contains = self._contents(shown, loc_kind) if loc_kind else None
        opts = rich_options([c["text"] for c in shown], self._now,
                            parents=[c.get("folder") or c.get("parent") or "" for c in shown], contains=contains)
        if contains is not None:
            step_log["contains"] = {c["label"]: n for c, n in zip(shown, contains) if n is not None}
        step = step_log.get("step") or {}
        bq = U.bridge_question(goal, step.get("target") or "", step.get("target_ui") or "",
                               getattr(self, "_ui_lang", None) or "de", question)
        q = R7.question7(bq or question, goal)
        sp = None
        if self.spend is not None:
            def sp(k, actual=None, _k=self.picker_kind):
                return self.spend(_k, actual=actual) if actual is not None else self.spend(_k)
        p = consistent_pick8(self.chooser, goal, state, opts, threshold=self.threshold, spend=sp, question=q,
                             protocol=self.protocol)
        rec.calls["picker"] += p.calls
        rec.cost += p.cost
        step_log["pick"] = dict(p.to_dict(), options=opts, state=state, t2=self.threshold, question=q,
                                none_option=R7.NONE_OPTION, protocol=self.protocol,
                                rule="both orders agree, neither chose none, min confidence >= t2; else the "
                                     f"{self.protocol} follow-up by the same rule")
        if bq:
            step_log["pick"]["bridge"] = {"target": step.get("target"), "target_ui": step.get("target_ui"),
                                          "ui_language": getattr(self, "_ui_lang", None)}
        target = step.get("target") or ""
        which = V.date_superlative(target, goal) if kind and p.index is not None else None
        if which:
            ext = V.date_extreme(opts, target, which)
            step_log["pick"]["date_check"] = {"asks": which, "extreme": ext, "pick": p.index}
            if ext is not None and ext != p.index:
                p = X.Pick(None, f"the pick contradicts the {which} modification date the target asks for "
                                 f"({opts[ext][:60]!r})", p.calls, p.cost, p.answers)
                step_log["pick"].update(index=None, reason=p.reason)
        if p.index is None:
            rec.handoffs += 1
            rec.gated += 1
            self._inbox(rec, "picker hand-off: " + p.reason, step_log)
            return None
        return cands[p.index]

    def call(self, op, **kw):
        r = super().call(op, **kw)
        if op == "rows" and kw.get("kind") == "finder_locations":
            # the folder shown (its path): the contents of the places the picker may be asked about
            self._loc_path = r.get("path")
        return r

    # -- a disabled item: leave the declared state, retry once --------------------------
    def _act(self, rec, log, op, **kw):
        r = super()._act(rec, log, op, **kw)
        if op != "press" or not is_disabled(r) or log.get("precondition"):
            return r
        bid = kw.get("bundle")
        declared = PRECONDITIONS.get(bid)
        log["precondition"] = {"disabled": str(r.get("detail") or "")[:120],
                               "declared": [d["id"] for d in declared or ()]}
        if not declared:
            return r
        p = super()._act(rec, log, "precondition", bundle=bid)
        log["precondition"].update(restored=p.get("restored"), detail=str(p.get("detail") or "")[:160],
                                   shown_before=p.get("shown_before"))
        if not p.get("restored"):
            return r
        r2 = super()._act(rec, log, op, **kw)          # the one retry
        log["precondition"]["retry"] = r2.get("status")
        rec.__dict__["precondition_retries"] = int(rec.__dict__.get("precondition_retries") or 0) + 1
        again = p.get("shown_before")
        if X._ok(r2) and again and p.get("reshow") == "notes_notes":
            rows = self.call("rows", bundle=bid, kind="notes_notes")
            if rows.get("shown_note") != again:
                r3 = super()._act(rec, log, "select", bundle=bid, kind="notes_notes", label=again)
                log["precondition"]["reshown"] = {"label": again, "status": r3.get("status")}
            else:
                log["precondition"]["reshown"] = {"label": again, "status": "still shown"}
        return r2

    # -- edit scopes are not missing restrictions ---------------------------------------
    def _checked_once(self, rec, goal, text, steps, err, entry, done=()):
        """``Arch7``'s restriction feedback and plan check with the edit scopes taken
        out of the missing restrictions (they are checked on the saved text)."""
        if not err:
            miss = missing_restrictions8(goal, list(done) + steps)
            entry["missing_restrictions"] = miss
            entry["edit_scopes"] = [r["phrase"] for r in edit_scopes(goal)]
            for _ in range(Y.MAX_CHECK_REPLANS):
                if not miss:
                    break
                hist = (entry.get("history") or []) + Y.feedback_messages(text, miss)
                text, steps, err, entry = self._call_planner(rec, goal, hist, entry.get("screen"))
                entry["history"] = hist
                if err:
                    break
                miss = missing_restrictions8(goal, list(done) + steps)
                entry["missing_restrictions"] = miss
            if not err and miss:
                err = "restriction not carried after feedback: " + "; ".join(m["phrase"] for m in miss)
        if err:
            return [], err, text, entry
        # Arch7's plan check, unchanged
        ref = R7.refine_plan7(goal, steps, self.m, getattr(self, "_screen_raw", None), done)
        W.route_by_screen(ref, getattr(self, "_screen_raw", None))
        U.route_settings_by_screen(ref, getattr(self, "_screen_raw", None) if not done else None, self.m)
        entry["refined"] = ref.to_dict()
        entry["pending"] = ref.pending
        if ref.problems:
            return [], "; ".join(ref.problems), text, entry
        self._pending = ref.pending
        return ref.steps, None, text, entry

    def _doc_save(self, task, rec, step, log, bid, fixed):
        out = super()._doc_save(task, rec, step, log, bid, fixed)
        scopes = edit_scopes(task.text)
        if out != "ok" or not scopes or log.get("journal") is None:
            return out
        j = rec.journal[log["journal"]]
        old = j["old_disk"] if j["old_disk"] is not None else j["old_shown"]
        after = self.call("file", path=j["where"])
        probs = edit_scope_problems(scopes, old or "", after.get("text") or "")
        log["edit_scope"] = {"scopes": [s["phrase"] for s in scopes], "problems": probs}
        if not probs:
            return out
        log["why"] = "edit scope check: " + "; ".join(probs) + " (the saved text; the old text restored)"
        area = next((e for e in self.m.entries(X.APP_OF[bid], "field_rw") if e.get("role") == "AXTextArea"), None)
        entry = (log.get("resolved") or {}).get("entry") or {}
        return self._restore_doc(rec, log, bid, area, j["where"], entry, j)


# ---------------------------------------------------------------------------
# The tasks (a NEW independent pool); judge for every arm
# ---------------------------------------------------------------------------

def load_tasks8(path: str = TASKS8_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_task8(t: Dict[str, Any], titles, baseline, plain_text_title="", gen: str = GEN8) -> X.Task:
    """``build_task5``'s translation conventions under this module's data root ({GEN8})."""
    t = json.loads(json.dumps(t).replace("{GEN8}", "{GEN5}"))
    return V.build_task5(t, titles, baseline, plain_text_title, gen)


def tasks8(data: Optional[Dict[str, Any]] = None, drawn: Optional[bool] = True) -> List[X.Task]:
    data = data or load_tasks8()
    out = [build_task8(t, data["te_prefs_titles"], data["te_prefs_baseline"], data.get("te_prefs_plain_text") or "")
           for t in data["tasks"]]
    return [t for t in out if drawn is None or (not t.dev) == drawn]


def task8_by_id(tid: str, data: Optional[Dict[str, Any]] = None) -> X.Task:
    for t in tasks8(data, drawn=None):
        if t.id == tid:
            return t
    raise KeyError(tid)


def statements8(tid: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = data or load_tasks8()
    t = next(x for x in data["tasks"] if x["author_task"]["id"] == tid)
    return {k.replace("{GEN8}", GEN8): v for k, v in (t["translation"].get("statements") or {}).items()}


def tasks8_sha256(data: Optional[Dict[str, Any]] = None) -> str:
    return X.tasks_sha256(tasks8(data, drawn=True))


def seeds8_spec(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return Z.seeds3_spec(data or load_tasks8())


def run_one8(task: X.Task, arm: str, run: int, guest, *, arch=None, baseline=None, compiled=None,
             mode: str = "measured", emap=None, reader=None, statements: Optional[Dict[str, str]] = None):
    """``run_one7`` (a clean start, twice at most; the arm; the reader; judge),
    judged by judge (= judge + ``opened_set`` + ``notes_shown_ok``) for EVERY arm."""
    out = R7.run_one7(task, arm, run, guest, arch=arch, baseline=baseline, compiled=compiled, mode=mode, emap=emap,
                      reader=reader, statements=statements)
    j = out.get("judge") or {}
    if j and not j.get("not_run") and (task.expect.get("opened_set") or task.expect.get("notes_shown_ok")):
        out["judge_notes"] = j
        out["judge"] = judge_from(j, task, out.get("read_start") or [], out.get("read_end") or [])
    return out
