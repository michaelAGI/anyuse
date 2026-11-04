"""Desktop engine, generation 5: fixing ``desktop_e2e4``'s
failure causes.   Builds on ``desktop_e2e`` ...
``desktop_e2e4`` (all left byte-identical) and is PURE like them (no pyobjc,
no subprocess, no network): the guest, the planner and the picker are
injected callables.

``desktop_e2e4`` failed its rule on the planner (the note's TITLE searched
instead of the word; the note shown on the screen opened instead of the one
the user described; the whole note appended where one detail was wanted), on
the picker (Jev took the older invoice for the newer one because Finder shows
"Vorgestern"; OpenJev handed off right picks below its threshold) and on
wrong actions above the baseline's. The generic pieces added here:

1. **Absolute dates** for picker candidates: files carry their modification
   and creation dates read from the file system in the guest (``os.stat``,
   read-only); relative labels ("Vorgestern, 08:30", "17:42", "Montag") are
   turned into dates with the guest's clock (Notes' list); the state names
   today's date.
2. **Picker acceptance by consistency**: act only when both option orders
   agree AND the minimum confidence reaches t2, t2 calibrated per picker on
   existing data only (``desktop_e2e5_picker.json``).
3. **extract** (a new step type) after read_content: a phrase, date, name or
   number taken from the text read, verified to occur in it (bound in the
   text's own spelling), never the item's title; later searches and writes use
   the detail, not the title or the whole text. The item the user DESCRIBES is
   chosen by content (the picker), not the one the screen shows.
4. **Write safety**: before any write to existing content the old text goes
   into the run's journal; a failed post-write check or identity check
   restores it (the run is a refusal); replacing existing content without an
   explicit instruction to replace is gated.
5. ``desktop_e2e4``'s remaining wrong-action causes (a setting the planner named with
   its own words is cross-checked by the picker).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import desktop_e2e as X
from . import desktop_e2e2 as Y
from . import desktop_e2e3 as Z
from . import desktop_e2e4 as W

HERE = os.path.dirname(os.path.abspath(__file__))
PICKER5_PATH = os.path.join(HERE, "desktop_e2e5_picker.json")
PLANNER5_PATH = os.path.join(HERE, "desktop_e2e5_planner.json")
TASKS5_PATH = os.path.join(HERE, "desktop_e2e5_tasks.json")
GEN5 = X.GEN + "/e5"
OPTION_CHARS = 240           # a candidate's text for the picker (desktop_e2e: 120; dates and content need more)

# ---------------------------------------------------------------------------
# 1. Absolute dates (Finder rows, the Open panel, Notes rows)
# ---------------------------------------------------------------------------

_REL_DAYS = {"heute": 0, "today": 0, "gestern": 1, "yesterday": 1, "vorgestern": 2}
_WEEKDAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5,
             "sonntag": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}
_TIME = r"(?:,?\s*(?:um\s+|at\s+)?(\d{1,2}):(\d{2}))?"


def parse_now(now: Any) -> Optional[_dt.datetime]:
    """The guest's local time "YYYY-MM-DD HH:MM" (op ``item_dates``)."""
    try:
        return _dt.datetime.strptime(str(now or "").strip()[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _fmt(d: _dt.date, hh: Optional[str], mm: Optional[str]) -> str:
    return d.strftime("%Y-%m-%d") + (f" {int(hh):02d}:{int(mm):02d}" if hh is not None else "")


def absolute_label(cell: Any, now: Optional[_dt.datetime]) -> Optional[str]:
    """A date label as Finder or Notes shows it, as an absolute date
    ("YYYY-MM-DD[ HH:MM]"), or None when the cell is not a date: "Vorgestern,
    08:30", "Heute, 10:00", "17:42" (today), "Montag" (the last Monday before
    today), "20.09.26, 10:00", "20.08.2026", "2026-09-20 10:00"."""
    s = " ".join(unicodedata.normalize("NFC", str(cell or "")).casefold().split())
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})" + _TIME.replace(",?", ""), s)
    if m:
        try:
            return _fmt(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4), m.group(5))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})" + _TIME, s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return _fmt(_dt.date(y, int(m.group(2)), int(m.group(1))), m.group(4), m.group(5))
        except ValueError:
            return None
    if now is None:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        return _fmt(now.date(), m.group(1), m.group(2))
    m = re.fullmatch(r"([a-zäöü]+)" + _TIME, s)
    if m and m.group(1) in _REL_DAYS:
        return _fmt(now.date() - _dt.timedelta(days=_REL_DAYS[m.group(1)]), m.group(2), m.group(3))
    if m and m.group(1) in _WEEKDAYS:
        back = (now.weekday() - _WEEKDAYS[m.group(1)]) % 7 or 7
        return _fmt(now.date() - _dt.timedelta(days=back), m.group(2), m.group(3))
    return None


def dated_row_text(text: str, info: Optional[Dict[str, Any]], now: Optional[_dt.datetime]) -> Tuple[str, str]:
    """(the candidate's text with ABSOLUTE dates, how): the file system's
    modification and creation dates when the guest read them (``info``), else
    Finder's / Notes' own date cell turned into a date; every other cell (name,
    size, kind, first line, content snippet) kept in order."""
    cells = [c for c in str(text or "").split(" | ")]
    if not cells:
        return text, "none"
    head, rest = cells[0], cells[1:]
    date_i = next((i for i, c in enumerate(rest) if not c.startswith("content:") and absolute_label(c, now)), None)
    if info and info.get("modified"):
        stamp = f"modified {info['modified']}, created {info.get('created') or '?'}"
        if date_i is not None:
            rest = rest[:date_i] + rest[date_i + 1:]
        return " | ".join([head, stamp] + rest), "file system"
    if date_i is not None:
        rest = list(rest)
        rest[date_i] = f"modified {absolute_label(rest[date_i], now)}"
        return " | ".join([head] + rest), "label"
    return text, "none"


def cand_kind(state: str) -> Optional[str]:
    """Which candidate list a picker state describes (the texts of ``desktop_e2e`` ... ``desktop_e2e4``)."""
    s = state or ""
    if "Open panel" in s:
        return "panel"
    if s.startswith("Frontmost application: Finder.") and "rows of its file list" in s:
        return "finder"
    if "notes in its note list" in s:
        return "notes"
    return None


def dated_state(kind: str, state: str, now: Optional[_dt.datetime]) -> str:
    """The picker's state with the dated columns named and today's date."""
    if kind == "finder":
        state = state.replace("name | modification date | size | kind",
                              "name | modified and created (absolute dates) | size | kind | content")
    elif kind == "notes":
        state = state.replace("title | first line | time", "title | first line | modified (absolute date)")
    elif kind == "panel":
        state = state.rstrip() + " Each file is listed as: name | modified and created (absolute dates)."
    if now is not None:
        state = state.rstrip() + f" Dates are absolute (YYYY-MM-DD HH:MM); today is {now:%Y-%m-%d} ({now:%A})."
    return state


NEWEST_WORDS = {"neueste", "neuesten", "neuester", "neuestes", "neuste", "neusten", "neustes", "newest", "latest",
                "jüngste", "jüngsten", "aktuellste", "aktuellsten"}
OLDEST_WORDS = {"älteste", "ältesten", "ältester", "ältestes", "oldest", "earliest", "früheste", "frühesten"}
_MOD_RE = re.compile(r"modified (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)")


def date_superlative(target: str, goal: str = "") -> Optional[str]:
    """"newest" / "oldest" when the step's target asks for it (or the goal does and
    the target is the goal's own words), else None."""
    tt = set(X.tokens(target))
    for words, which in ((NEWEST_WORDS, "newest"), (OLDEST_WORDS, "oldest")):
        if tt & words:
            return which
    gl = X.tokens(goal)
    bare = set(Z.bare_tokens(target))
    for k, t in enumerate(gl):             # "die neueste Checkliste": the superlative right before a target word
        for words, which in ((NEWEST_WORDS, "newest"), (OLDEST_WORDS, "oldest")):
            if t in words and bare & set(gl[k + 1:k + 3]):
                return which
    return None


def date_extreme(texts: Sequence[str], target: str, which: str) -> Optional[int]:
    """The ONE candidate with the newest (oldest) absolute modification date
    among those that share a word with the target (all dated ones when none
    does); None when there is no unique one."""
    words = {t for t in Z.bare_tokens(target) if len(t) >= 4} - NEWEST_WORDS - OLDEST_WORDS
    dated = {i: m.group(1) for i, t in enumerate(texts) for m in [_MOD_RE.search(t or "")] if m}
    related = [i for i in dated if words & set(X.tokens(texts[i]))]
    pool = related or list(dated)
    if len(pool) < 2:
        return None
    best = (max if which == "newest" else min)(dated[i] for i in pool)
    win = [i for i in pool if dated[i] == best]
    return win[0] if len(win) == 1 else None


# ---------------------------------------------------------------------------
# 2. The picker: both orders, act only on an ACCEPTED pick (calibrated t2)
# ---------------------------------------------------------------------------

def load_picker5(path: str = PICKER5_PATH) -> Dict[str, Any]:
    """The frozen acceptance calibration."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def t2_of(picker: str, spec: Optional[Dict[str, Any]] = None) -> float:
    """t2 of a picker ("jev" | "openjev"): picker-agnostic, the same rule."""
    t = ((spec or load_picker5())["pickers"].get(picker) or {}).get("t2")
    if t is None:
        raise KeyError(f"no calibrated t2 for the picker {picker!r}")
    return float(t)


def option_text5(s: Any, chars: int = OPTION_CHARS) -> str:
    return " ".join(str(s).split())[:chars] or "(empty)"


def consistent_pick5(chooser, goal: str, state: str, options: Sequence[str], threshold: float,
                     spend=None, question: Optional[str] = None, chars: int = OPTION_CHARS) -> X.Pick:
    """``desktop_e2e``'s rule (both orders, the same item, the minimum confidence >=
    the threshold) with longer option texts and an optional question."""
    opts = [option_text5(o, chars) for o in options]
    if not opts:
        return X.Pick(None, "no candidates")
    q = question or f"Which listed item should be used for this task: {goal}"
    answers, cost, calls, picks = [], 0.0, 0, []
    for order, lst in (("o0", opts), ("o1", opts[::-1])):
        if spend is not None and not spend("picker"):
            return X.Pick(None, "budget cap before a picker call", calls, cost, answers)
        choice, conf, meta = chooser(state, q, lst)
        calls += 1
        cost += float((meta or {}).get("cost") or 0.0)
        if spend is not None:
            spend("picker", actual=float((meta or {}).get("cost") or 0.0))
        idx = None
        if isinstance(choice, int) and 0 <= choice < len(lst):
            idx = choice if order == "o0" else len(lst) - 1 - choice
        answers.append({"order": order, "choice": choice, "index": idx, "confidence": conf,
                        "error": (meta or {}).get("error"), "ms": (meta or {}).get("ms"),
                        "server_ms": (meta or {}).get("server_ms")})
        picks.append((idx, float(conf or 0.0)))
    (i0, c0), (i1, c1) = picks
    if i0 is None or i1 is None:
        return X.Pick(None, "no valid answer in one order", calls, cost, answers)
    if i0 != i1:
        return X.Pick(None, f"orders disagree ({opts[i0][:60]!r} vs {opts[i1][:60]!r})", calls, cost, answers)
    if min(c0, c1) < threshold:
        return X.Pick(None, f"confidence {min(c0, c1):.2f} below t2 = {threshold}", calls, cost, answers)
    return X.Pick(i0, "accepted: both orders agree, minimum confidence >= t2", calls, cost, answers)


# ---------------------------------------------------------------------------
# 3. The planner w5, ``extract``, and the checks after a read
# ---------------------------------------------------------------------------

def load_planner5_spec(path: str = PLANNER5_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def planner5_messages(goal: str, spec: Optional[Dict[str, Any]] = None,
                      history: Optional[List[Dict[str, str]]] = None,
                      context: Optional[str] = None) -> List[Dict[str, str]]:
    """w5's messages (``desktop_e2e4``'s layout: the goal, the screen line as data)."""
    spec = spec or load_planner5_spec()
    user = spec["user_prefix"] + goal + ((spec["context_prefix"] + context) if context else "")
    return ([{"role": "system", "content": spec["w5_system"]}, {"role": "user", "content": user}]
            + list(history or []))


WRITES = ("doc_save", "field_rw")
_QUOTES = "\"'„“”‚‘’«»"
TITLE_WORDS = {"title", "titel", "name", "überschrift", "heading", "headline", "betreff"}
SHOWN_WORDS = {"offen", "offene", "offenen", "geöffnet", "geöffnete", "geöffneten", "angezeigt", "angezeigte",
               "gezeigt", "gezeigte", "open", "opened", "shown", "current", "aktuelle", "aktuellen", "this", "diese",
               "dieser", "diesen"}


def _fold_char(c: str) -> str:
    d = unicodedata.normalize("NFD", c)
    base = "".join(ch for ch in d if not unicodedata.combining(ch))
    return (" " if base.isspace() else base).casefold()


def _folded(text: str) -> Tuple[str, List[int]]:
    out, idx = [], []
    for i, c in enumerate(text):
        for ch in _fold_char(c):
            out.append(ch)
            idx.append(i)
    return "".join(out), idx


def locate(text: str, words: str) -> Optional[Tuple[int, int, str]]:
    """(start, end, how) of ``words`` in ``text``: exact, else ignoring case,
    else ignoring case and accents ("Croutons" finds "Croûtons"); None when the
    words do not occur. The span is the TEXT's own spelling."""
    w = unicodedata.normalize("NFC", (words or "").strip().strip(_QUOTES).strip())
    t = unicodedata.normalize("NFC", text or "")
    if not w or not t:
        return None
    k = t.find(w)
    if k >= 0:
        return k, k + len(w), "exact"
    k = t.casefold().find(w.casefold())
    if k >= 0 and len(t.casefold()) == len(t):
        return k, k + len(w), "ignoring case"
    ft, idx = _folded(t)
    fw = "".join(_fold_char(c) for c in w)
    k = ft.find(fw)
    if k < 0:
        return None
    return idx[k], idx[k + len(fw) - 1] + 1, "ignoring case and accents"


def asks_title(target: str) -> bool:
    return bool(set(X.tokens(target)) & TITLE_WORDS)


def extract_detail(texts: Sequence[str], words: str, target: str = "") -> Tuple[Optional[str], str]:
    """(the detail in the text's own spelling, how) or (None, why): the words
    must occur in a text read in this run (the latest read first); a detail
    that is the item's TITLE (the first line of a text of several lines) or the
    whole text is refused unless the field asks for a title or name."""
    if not (words or "").strip():
        return None, "extract without the words (find)"
    for text in reversed(list(texts)):
        hit = locate(text, words)
        if hit is None:
            continue
        span = text[hit[0]:hit[1]]
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) > 1 and span.strip() == lines[0] and not asks_title(target):
            return None, (f"{span.strip()!r} is the item's title (the first line of the text read), not the detail "
                          f"{target!r} from its text")
        if len(lines) > 1 and span.strip() == (text or "").strip():
            return None, "the whole text read, not a detail"
        return span, hit[2]
    return None, f"{words!r} does not occur in the text read"


def var_refs(value: Any) -> List[str]:
    return W.VAR_RE.findall(str(value or ""))


def _goal_tokens(goal: str) -> set:
    return set(X.tokens(goal))


def named_by_words(target: str, goal: str, texts: Sequence[str] = ()) -> bool:
    """Is the item named with the USER's words (the goal) or the words of a
    text read in this run? (a file extension and kind words aside)."""
    bare = Z.bare_tokens(W.FILE_RE.sub("", str(target or "")))
    if not bare:
        return False
    have = _goal_tokens(goal)
    for t in texts:
        have |= set(X.tokens(t))
    return set(bare) <= have


def names_shown_note(goal: str) -> bool:
    """Does the goal speak of the note SHOWN ("die offene Notiz", "this note")?"""
    toks = X.tokens(goal)
    return any(t in ("notiz", "note") and set(toks[max(0, i - 3):i]) & SHOWN_WORDS for i, t in enumerate(toks))


_QUOTED_RE = re.compile(r"[„“\"'‚‘«»]([^„“”\"'‚‘’«»]{1,80})[“”\"'‘’«»]")
#: words that ask for a SEARCH (typing a pattern); "find" / "finde" (locate) and "look up" (a dictionary look-up)
#: are not among them ("the squeaky hinge, then look up" named the note as a search pattern)
_SEARCH_TOKENS = (Y.SEARCH_WORDS | {"sucht", "suchst", "searching"}) - {"find", "finde", "finden", "lookup"}
_SEARCH_BEFORE = {"suchen", "sucht", "suche", "durchsuchen", "filtern"}      # "nach X suchen"
_FILLER = {"for", "nach", "the", "den", "die", "das", "dem", "einem", "einen", "a", "an", "mal", "bitte", "please",
           "word", "wort", "begriff", "term"}


def goal_names_search(goal: str, pattern: str) -> bool:
    """Does the goal ask for a search FOR THIS pattern (quoted, or right after a
    search word: "search for X", "such nach X", "nach X suchen")? "Such sie bitte
    raus" (find it) names none."""
    pv = X.norm(pattern)
    if not pv:
        return False
    if any(pv == X.norm(q) or pv in X.norm(q) for q in _QUOTED_RE.findall(goal or "")):
        return True
    toks, pt = X.tokens(goal), X.tokens(pattern)
    if not pt:
        return False
    for k, t in enumerate(toks):
        if t in _SEARCH_TOKENS and set(pt) <= set(toks[k + 1:k + 6]):
            return True
        win = toks[max(0, k - 4):k]
        if t in _SEARCH_BEFORE and "nach" in win and set(pt) <= set(win[win.index("nach") + 1:]):
            return True
    return False


def refine_plan5(goal: str, steps: Sequence[Dict[str, Any]], emap) -> Y.Refined:
    """(r6) a read_content followed by steps ENDS the plan unless every later
    use of its variable is a whole copy (a write whose value is exactly
    ``{{name}}``): a detail, a search word, a place or a text inside a larger
    text is taken with ``extract`` AFTER the text is seen (task GA12: the
    whole note appended as "Ersatzschlüssel: {{place}}"); an extract planned
    before its text was read ends the plan at that read. Then ``desktop_e2e4``'s check;
    extract steps (no app, no container) are kept out of it and put back before
    the step that followed them."""
    st = [dict(s, mode=s.get("mode") or "", find=s.get("find") or "", scope=s.get("scope") or "") for s in steps]
    changes: List[str] = []
    pending: List[Dict[str, Any]] = []
    for i, s in enumerate(st):
        if s["effect"] != "read_content" or i + 1 >= len(st):
            continue
        name = W.var_name(s["value"])
        later = st[i + 1:]
        users = [t for t in later if W.uses_var(t, name)]
        whole = all(t["effect"] in WRITES and str(t.get("value") or "").strip() == "{{" + name + "}}"
                    and not W.VAR_RE.search(str(t.get("find") or "") + str(t.get("target") or ""))
                    for t in users)
        ex = [t for t in later if t["effect"] == "extract"]
        if users and whole and not ex:
            continue
        why = ("an extract needs the text first" if ex else
               f"{{{{{name}}}}} is used inside a larger text or as a word" if users else
               f"no later step uses {{{{{name}}}}}")
        changes.append(f"ended the plan after read_content({name!r}): {why} (dropped {len(later)} step(s) "
                       "planned before the text was seen)")
        pending = later
        st = st[:i + 1]
        break
    # (r7) tasks G04 and GA07: a search in Notes / Finder right before the select or
    # open of a row narrows the list the picker reads anyway and stays behind as a changed search field (a
    # wrong action); it is dropped unless the goal asks for a search FOR this pattern
    i = 0
    while i + 1 < len(st):
        s, n = st[i], st[i + 1]
        if s["effect"] == "scoped_find" and s["app"] in Y.CONTENT_APPS and n["effect"] in ("select_item", "open_item") \
                and n["app"] == s["app"] and not goal_names_search(goal, s["value"]):
            changes.append(f"dropped scoped_find({s['value'][:40]!r}) before {n['effect']}: the goal names no search "
                           "for this pattern (the picker reads the whole list; a search stays behind)")
            del st[i]
            continue
        i += 1
    # extract steps are not the map's business: out of desktop_e2e4's check, back in afterwards
    ext: List[Tuple[Dict[str, Any], Optional[int]]] = []
    core: List[Dict[str, Any]] = []
    for i, s in enumerate(st):
        if s["effect"] == "extract":
            nxt = next((j for j in range(i + 1, len(st)) if st[j]["effect"] != "extract"), None)
            ext.append((s, id(st[nxt]) if nxt is not None else None))
        else:
            core.append(s)
    ref = W.refine_plan4(goal, core, emap)
    for s, anchor in ext:
        k = next((j for j, t in enumerate(ref.steps) if id(t) == anchor), None)
        if k is None and anchor is not None:
            # the step after it was rewritten (a copy): the first later write or search using its variable
            name = W.var_name(s["value"])
            k = next((j for j, t in enumerate(ref.steps) if W.uses_var(t, name)), None)
        ref.steps.insert(len(ref.steps) if k is None else k, dict(s))
    ref.changes = changes + ref.changes
    ref.pending = pending or getattr(ref, "pending", [])
    return ref


def check_plan5(steps: Sequence[Dict[str, Any]], goal: str, reads: Dict[str, str], extracted: Sequence[str],
                done: Sequence[Dict[str, Any]] = (), screen: Optional[Dict[str, Any]] = None) -> List[str]:
    """Problems of a plan (or continuation) the planner is told about once:
    (a) after a read, a search uses a detail taken with extract, never a word
    the planner typed, the whole text or the item's title (GA02, GA07);
    (b) a write uses a read variable only as a whole copy, and words it copies
    from a text read come through extract (GA12);
    (c) an extract names words that occur in the text, not its title;
    (d) reading "the note" in Notes reads the note SHOWN: when the goal does not
    name it, the note the user describes is opened first (GA07)."""
    probs: List[str] = []
    ex = set(extracted)
    reads_now = dict(reads)
    texts = list(reads_now.values())
    gt = _goal_tokens(goal)
    opened_notes = any(t["effect"] in ("open_item", "select_item") and t["app"] == "Notes" for t in done)
    for s in steps:
        eff = s["effect"]
        if eff == "extract":
            name = W.var_name(s["value"])
            if texts:
                span, why = extract_detail(texts, s.get("find") or "", s.get("target") or "")
                if span is None:
                    probs.append(f"extract({s.get('target')!r}): {why}")
            ex.add(name)
            continue
        if eff == "read_content":
            if s["app"] == "Notes" and not opened_notes and screen and screen.get("notes_note") \
                    and X.norm(s.get("target")) in ("note", "notiz", "the note", "die notiz") \
                    and not named_by_words(screen["notes_note"], goal) and not names_shown_note(goal):
                probs.append(f"read_content(note) reads the note shown on the screen ({screen['notes_note']!r}), "
                             "which the goal does not name: open the note the user describes first (open_item with "
                             "the user's own words; the engine chooses it by its content)")
            reads_now[W.var_name(s["value"])] = ""          # read later: its variable is a whole text
            continue
        if eff in ("open_item", "select_item") and s["app"] == "Notes":
            opened_notes = True
        if not reads:
            continue
        refs = var_refs(s.get("value"))
        if eff == "scoped_find":
            if not refs or not all(r in ex for r in refs):
                probs.append(f"scoped_find({s.get('value')!r}) after reading: search the detail taken from the text "
                             "with extract ({{name}}), not the item's title, a description or the whole text")
            continue
        if eff in WRITES and s.get("mode") in ("append", "insert", "replace", ""):
            whole = [r for r in refs if r not in ex]
            if whole and str(s.get("value") or "").strip() != "{{" + whole[0] + "}}":
                probs.append(f"{eff}({s.get('value')!r}): {{{{{whole[0]}}}}} is the whole text read; take the "
                             "detail with extract and write {{detail}}")
                continue
            lit = W.VAR_RE.sub(" ", str(s.get("value") or ""))
            copied = [t for t in X.tokens(lit) if len(t) >= 3 and t not in gt
                      and any(t in X.tokens(x) for x in texts)]
            if copied:
                probs.append(f"{eff}({s.get('value')!r}): the words {copied[:4]} are copied from the text read; take "
                             "them with extract and write {{detail}}")
    return probs


# ---------------------------------------------------------------------------
# 4. Write safety: the journal, the restore, the replace gate
# ---------------------------------------------------------------------------

OVERWRITE_WORDS = {"overwrite", "overwrites", "überschreibe", "überschreiben", "überschreib", "überschreibt"}
REPLACE_WORDS = {"replace", "ersetze", "ersetzen", "ersetz", "rewrite", "neuschreiben"}
WHOLE_WORDS = {"whole", "entire", "all", "everything", "ganzen", "ganze", "gesamten", "gesamte", "kompletten",
               "komplette", "komplett", "alles", "vollständig"}


def explicit_replace(goal: str) -> bool:
    """Does the USER ask to replace the whole existing text? ("overwrite",
    "überschreiben", or a replace word together with a whole-text word)."""
    t = set(X.tokens(goal))
    return bool(t & OVERWRITE_WORDS) or bool(t & REPLACE_WORDS and t & WHOLE_WORDS)


def replaces_existing(old: str, mode: str, value: str) -> bool:
    """A write that would REPLACE existing content (not add to it, not change
    one named part of it)."""
    return (mode or "replace") == "replace" and bool((old or "").strip()) and \
        (old or "").rstrip("\n") != (value or "").rstrip("\n")


# ---------------------------------------------------------------------------
# 5. desktop_e2e4's remaining wrong-action cause: a setting the PLANNER named
# ---------------------------------------------------------------------------

def user_named_option(label: str, goal: str) -> bool:
    """Does the goal itself use a word of the option's label? (kind and stop
    words aside; "Duden" names "Duden-Wissensnetz deutsche Sprache"; "German-
    English" names "Oxford German Dictionary"; "Prüf-Markierung" names neither
    TextEdit spelling flag: the label is then the planner's own reading.)"""
    bare = [t for t in Z.bare_tokens(label) if len(t) >= 3]
    return bool(set(bare) & _goal_tokens(goal))


# ---------------------------------------------------------------------------
# 7. The tasks (a NEW independent pool); the judge is desktop_e2e4's judge
# ---------------------------------------------------------------------------

def load_tasks5(path: str = TASKS5_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_task5(t: Dict[str, Any], titles, baseline, plain_text_title="", gen: str = GEN5) -> X.Task:
    """``desktop_e2e4``'s translation conventions under this module's data root ({GEN5})."""
    a, tr = t["author_task"], t["translation"]
    tid = a["id"]
    expect = json.loads(json.dumps(tr["expect"]).replace("{GEN5}", gen))
    extra = json.loads(json.dumps(tr.get("extra_reads") or []).replace("{GEN5}", gen))
    s = a["start"]
    if s.get("textedit_document") and not s.get("finder_folder"):
        # the author names the document open at the start but neither its folder nor its text (SB15):
        # it is created in the task's own data folder with a placeholder text (no Finder window added)
        start = W.build_start4(tid, dict(a, start=dict(s, textedit_document=None)), titles, baseline,
                               plain_text_title, gen)
        start["te_prefs"] = {k: v for k, v in baseline.items()}
        start["textedit"] = {"doc": f"{gen}/{tid}/{s['textedit_document']}",
                             "seed": undeclared_text(s["textedit_document"]),
                             "flags": {Z.TE_MENU[k]: bool(v) for k, v in (s.get("textedit_settings") or {}).items()}}
    else:
        start = W.build_start4(tid, a, titles, baseline, plain_text_title, gen)
    return X.Task(tid, a["utterance"], Z._kind(a), start, W.build_reads4(tid, a, extra, gen), expect,
                  dev=not t["drawn"])


def undeclared_text(name: str) -> str:
    """The placeholder text of a document the author declared open but gave no
    text: its own name without the extension."""
    return os.path.splitext(name)[0] + "\n"


def tasks5(data: Optional[Dict[str, Any]] = None, drawn: Optional[bool] = True) -> List[X.Task]:
    data = data or load_tasks5()
    out = [build_task5(t, data["te_prefs_titles"], data["te_prefs_baseline"], data.get("te_prefs_plain_text") or "")
           for t in data["tasks"]]
    return [t for t in out if drawn is None or (not t.dev) == drawn]


def task5_by_id(tid: str, data: Optional[Dict[str, Any]] = None) -> X.Task:
    for t in tasks5(data, drawn=None):
        if t.id == tid:
            return t
    raise KeyError(tid)


def statements5(tid: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = data or load_tasks5()
    t = next(x for x in data["tasks"] if x["author_task"]["id"] == tid)
    return {k.replace("{GEN5}", GEN5): v for k, v in (t["translation"].get("statements") or {}).items()}


def tasks5_sha256(data: Optional[Dict[str, Any]] = None) -> str:
    return X.tasks_sha256(tasks5(data, drawn=True))


def seeds5_spec(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return Z.seeds3_spec(data or load_tasks5())


def judge(task: X.Task, start, end, record=None, emap=None, reader=None,
           statements: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """``desktop_e2e4``'s judge (the same independent reader and rules) plus ONE
    observable a drawn task needs: TextEdit's settings for new documents (read
    ``te_prefs``: the Einstellungen checkboxes). ``expect["te_prefs"]`` =
    {checkbox title: wanted state}: ok / missing (as at the start) / wrong; any
    other checkbox that changed is an unexpected change."""
    out = W.judge(task, start, end, record, emap, reader, statements)
    want = task.expect.get("te_prefs") or {}
    k = next((i for i, p in enumerate(task.read) if p.get("kind") == "te_prefs"), None)
    if k is None:
        return out
    s0 = ((start or [])[k] if k < len(start or []) else {}) or {}
    e0 = ((end or [])[k] if k < len(end or []) else {}) or {}
    sp, ep = s0.get("prefs") or {}, e0.get("prefs") or {}
    if not (s0.get("ok") and e0.get("ok")):
        out["items"].append({"what": "TextEdit settings readable", "status": "missing"})
    else:
        for title, on in want.items():
            st = "ok" if ep.get(title) == bool(on) else ("missing" if ep.get(title) == sp.get(title) else "wrong")
            out["items"].append({"what": f"TextEdit setting {title!r} {'on' if on else 'off'}", "status": st})
        for title in sorted(set(sp) | set(ep)):
            if title not in want and sp.get(title) != ep.get(title):
                out["unexpected"].append(f"TextEdit setting {title!r} changed ({sp.get(title)} -> {ep.get(title)})")
    items = out["items"]
    out["success"] = bool(items) and all(i["status"] == "ok" for i in items) and not out["unexpected"] \
        and not out["irreversible"]
    out["wrong_actions"] = sum(1 for i in items if i["status"] == "wrong") + len(out["unexpected"])
    return out


def run_one5(task: X.Task, arm: str, run: int, guest, *, arch=None, baseline=None, compiled=None,
             mode: str = "measured", emap=None, reader=None, statements: Optional[Dict[str, str]] = None):
    """``desktop_e2e4``'s run (a clean start, twice at most; the arm; the reader), judged by judge."""
    out = W.run_one4(task, arm, run, guest, arch=arch, baseline=baseline, compiled=compiled, mode=mode, emap=emap,
                     reader=reader, statements=statements)
    if "judge" in out and not (out["judge"] or {}).get("not_run") and any(p.get("kind") == "te_prefs" for p in task.read):
        out["judge_prev"] = out["judge"]
        out["judge"] = judge(task, out.get("read_start") or [], out.get("read_end") or [], out, emap, reader, statements)
    return out


# ---------------------------------------------------------------------------
# 6. The architecture arm
# ---------------------------------------------------------------------------

class _Recorder:
    """The guest channel as the arm sees it, remembering the folder paths the
    guest verified (Finder rows, the Open panel) for the date read."""

    def __init__(self, guest):
        self.g = guest
        self.panel_path: Optional[str] = None
        self.notes_back: Optional[Dict[str, Any]] = None

    def __call__(self, op, **kw):
        r = self.g(op, **kw)
        if op == "rows" and kw.get("kind") == "notes_notes" and kw.get("back") and isinstance(r, dict):
            self.notes_back = r
        if op == "panel" and isinstance(r, dict) and kw.get("action") in ("show", "enter", "goto_finder"):
            self.panel_path = r.get("path") or (self.panel_path if kw.get("action") == "goto_finder" else None)
        return r


class Arch5(W.Arch4):
    """``desktop_e2e4``'s arm with the fixes of the module docstring."""

    def __init__(self, guest, emap, planner=None, picker_name: Optional[str] = None, **kw):
        if picker_name == "none":             # no picker: where the map does not decide,
            kw["threshold"] = 1.01            # the step hands off (asks) without any picker call
            kw["chooser"] = None
        elif picker_name is not None:         # the calibrated t2 of this picker, the same rule for all
            kw["threshold"] = t2_of(picker_name)
        super().__init__(_Recorder(guest), emap, planner=planner, **kw)
        self.picker_name = picker_name
        self._now: Optional[_dt.datetime] = None
        self._reads: Dict[str, str] = {}              # the texts read in this run (whole texts)
        self._extracted: List[str] = []               # variables bound by extract

    # -- the picker: absolute dates, longer options, the accepted-pick rule -------------
    def _dates(self, kind: str, cands: List[Dict[str, Any]], log: Dict[str, Any]) -> List[Dict[str, Any]]:
        labels = [c["label"] for c in cands] if kind in ("finder", "panel") else []
        args = {"labels": labels}
        if kind == "panel" and self.g.panel_path:
            args["path"] = self.g.panel_path
        try:
            r = self.call("item_dates", **args)
        except X.GuestError:
            r = {}
        self._now = parse_now(r.get("now")) or self._now
        items = r.get("items") or {}
        out, how = [], []
        for c in cands:
            info = items.get(unicodedata.normalize("NFC", c["label"])) if kind != "notes" else None
            text, h = dated_row_text(c["text"], info, self._now)
            out.append(dict(c, text=text))
            how.append(h)
        log["dates"] = {"kind": kind, "now": r.get("now"), "path": r.get("path"),
                        "how": {h: how.count(h) for h in sorted(set(how))}}
        return out

    def pick(self, rec: X.Rec, goal: str, state: str, cands: List[Dict[str, Any]], step_log: Dict[str, Any],
             question: Optional[str] = None):
        if self.picker_name == "none" and self.planner is not None:
            # picker mode "none": the map did not decide; a person chooses (a hand-off), no model is asked
            step_log["pick"] = {"index": None, "reason": "no picker (mode none): hand-off", "calls": 0, "cost": 0.0,
                                "answers": [], "options": [option_text5(c["text"]) for c in cands], "state": state}
            rec.handoffs += 1
            rec.gated += 1
            self._inbox(rec, "picker hand-off: no picker (mode none)", step_log)
            return None
        if self.chooser is None:
            raise X.GuestError("model needed: no picker in compiled mode")
        kind = cand_kind(state)
        shown = cands
        if kind:
            shown = self._dates(kind, cands, step_log)
            state = dated_state(kind, state, self._now)
        sp = None
        if self.spend is not None:
            def sp(k, actual=None, _k=self.picker_kind):
                return self.spend(_k, actual=actual) if actual is not None else self.spend(_k)
        p = consistent_pick5(self.chooser, goal, state, [c["text"] for c in shown], threshold=self.threshold,
                             spend=sp, question=question)
        rec.calls["picker"] += p.calls
        rec.cost += p.cost
        step_log["pick"] = dict(p.to_dict(), options=[option_text5(c["text"]) for c in shown], state=state,
                                t2=self.threshold, rule="both orders agree and min confidence >= t2")
        if question:
            step_log["pick"]["question"] = question
        target = ((step_log.get("step") or {}).get("target") or "")
        which = date_superlative(target, goal) if kind and p.index is not None else None
        if which:
            ext = date_extreme([c["text"] for c in shown], target, which)
            step_log["pick"]["date_check"] = {"asks": which, "extreme": ext, "pick": p.index}
            if ext is not None and ext != p.index:
                # seen task S01: "die neueste Checkliste" -> the file NAMED "neu", modified in 2025,
                # picked confidently over the one modified in 2026: a pick against the dates the target asks
                # for is not taken (a hand-off; the dates never make a pick by themselves)
                p = X.Pick(None, f"the pick contradicts the {which} modification date the target asks for "
                                 f"({option_text5(shown[ext]['text'])[:60]!r})", p.calls, p.cost, p.answers)
                step_log["pick"].update(index=None, reason=p.reason)
        if p.index is None:
            rec.handoffs += 1
            rec.gated += 1
            self._inbox(rec, "picker hand-off: " + p.reason, step_log)
            return None
        return cands[p.index]

    # -- the planner w5: parse, check (one feedback), continue ---------------------------
    def _spec5(self) -> Dict[str, Any]:
        if getattr(self, "_spec5_cache", None) is None:
            self._spec5_cache = load_planner5_spec()
        return self._spec5_cache

    def _call_planner(self, rec, goal, history=None, screen=None):
        if self.spend is not None and not self.spend("planner4"):
            raise X.RunStop("budget cap reached before a planner call")
        text, meta = self.planner(goal, history, screen) if screen else self.planner(goal, history)
        rec.calls["planner"] += 1
        rec.plans += 1
        c = float((meta or {}).get("cost") or 0.0)
        rec.cost += c
        if self.spend is not None:
            self.spend("planner4", actual=c)
        steps, err = W.parse_plan4(text, self._spec5())
        entry = {"text": (text or "")[:4000], "steps": steps, "error": err or (meta or {}).get("error"),
                 "usage": (meta or {}).get("usage"), "feedback": bool(history), "screen": screen}
        rec.plan.append(entry)
        return text, steps, err, entry

    def _checked_once(self, rec, goal, text, steps, err, entry, done=()):
        """``desktop_e2e4``'s restriction feedback and plan check, with refine_plan5."""
        if not err:
            miss = Y.missing_restrictions(goal, list(done) + steps)
            entry["missing_restrictions"] = miss
            for _ in range(Y.MAX_CHECK_REPLANS):
                if not miss:
                    break
                hist = (entry.get("history") or []) + Y.feedback_messages(text, miss)
                text, steps, err, entry = self._call_planner(rec, goal, hist, entry.get("screen"))
                entry["history"] = hist
                if err:
                    break
                miss = Y.missing_restrictions(goal, list(done) + steps)
                entry["missing_restrictions"] = miss
            if not err and miss:
                err = "restriction not carried after feedback: " + "; ".join(m["phrase"] for m in miss)
        if err:
            return [], err, text, entry
        ref = refine_plan5(goal, steps, self.m)
        W.route_by_screen(ref, getattr(self, "_screen_raw", None))
        entry["refined"] = ref.to_dict()
        entry["pending"] = ref.pending
        if ref.problems:
            return [], "; ".join(ref.problems), text, entry
        self._pending = ref.pending
        return ref.steps, None, text, entry

    def _checked(self, rec, goal, text, steps, err, entry, done=()):
        """The plan check, then the checks after a read (check_plan5): the
        planner is told the problems ONCE; a second answer with problems is not
        executed (no action: the run stops)."""
        out, err, text, entry = self._checked_once(rec, goal, text, steps, err, entry, done)
        if err:
            return [], err
        probs = check_plan5(out, goal, self._reads, self._extracted, list(done), self._screen_raw)
        entry["check5"] = probs
        if not probs:
            return out, None
        hist = (entry.get("history") or []) + [
            {"role": "assistant", "content": text or ""},
            {"role": "user", "content": self._spec5()["check_feedback_template"].format(
                problems="\n".join("- " + p for p in probs))}]
        cont = bool(entry.get("continuation"))
        text2, steps2, err2, entry2 = self._call_planner(rec, goal, hist, entry.get("screen"))
        entry2.update(history=hist, check_feedback=True, continuation=cont)
        rec.__dict__["check_feedbacks"] = int(rec.__dict__.get("check_feedbacks") or 0) + 1
        out2, err2, _t, entry2 = self._checked_once(rec, goal, text2, steps2, err2, entry2, done)
        if err2:
            return [], err2
        probs2 = check_plan5(out2, goal, self._reads, self._extracted, list(done), self._screen_raw)
        entry2["check5"] = probs2
        if probs2:
            return [], "plan check after feedback: " + "; ".join(probs2)
        return out2, None

    def continue_plan(self, rec: X.Rec, goal: str, done: List[Tuple[Dict[str, Any], Any]]):
        if self.planner is None:
            raise X.GuestError("model needed: no planner in compiled mode")
        prev = next((p.get("text") for p in reversed(rec.plan) if p.get("text")), "")
        hist = W.continuation_messages(prev, done, self._reads, self._spec5(), pending=self._pending)
        text, steps, err, entry = self._call_planner(rec, goal, hist)
        entry["continuation"] = True
        entry["history"] = hist
        return self._checked(rec, goal, text, steps, err, entry, done=[s for s, _r in done])

    # -- one run -------------------------------------------------------------------------
    def run(self, task: X.Task, rec: X.Rec, compiled: Optional[List[Dict[str, Any]]] = None) -> X.Rec:
        """``desktop_e2e4``'s loop (Arch4.run) with this module's state (reads, extracts, the
        write journal) and two more stops: a write whose check failed and whose
        old text was RESTORED is a refusal; a failed restore stops the run."""
        clock = X.Clock(self.clock_fn)
        self._vars, self._where, self._opened, self._pending, self._screen = {}, {}, None, [], None
        self._reads, self._extracted = {}, []
        rec.__dict__.setdefault("extracts", [])
        rec.__dict__.setdefault("journal", [])
        rec.__dict__.setdefault("restores", 0)
        rec.__dict__.setdefault("location_refusals", 0)
        rec.__dict__.setdefault("continuations", 0)
        rec.__dict__.setdefault("reads", [])
        done_all: List[Tuple] = []
        done_steps: List[Tuple[Dict[str, Any], Any]] = []
        carried: List[Tuple] = []
        replans = 0
        if compiled is not None:
            plan = [c["step"] for c in compiled]
        else:
            plan, err = self.plan(rec, task.text)
            if err or not plan:
                rec.stopped = f"no executable plan: {err or 'empty'}"
                rec.wall_s = round(clock.elapsed(), 2)
                return rec
        i = 0
        while i < len(plan):
            step = plan[i]
            if clock.elapsed() > X.TASK_MAX_S:
                rec.stopped = "task wall time (5 min)"
                break
            if rec.actions_executed + rec.gate_stops >= X.TASK_MAX_STEPS:
                rec.stopped = "15 steps"
                break
            key = (step["effect"], step["app"], X.norm(step["target"]), X.norm(step["value"]))
            if compiled is None and key in carried:
                carried.remove(key)          # a re-plan repeats a step already checked: not done twice
                i += 1
                if step["effect"] == "read_content" and i == len(plan):
                    plan, i, stop = self._continue(rec, task, plan, done_steps)
                    if stop:
                        rec.stopped = stop
                        break
                continue
            concrete, missing = W.substitute(step, self._vars)
            log = {"i": len(rec.steps), "step": concrete}
            if concrete != step:
                log["planned"] = step
            rec.steps.append(log)
            if missing:
                log["outcome"] = "failed"
                log["why"] = f"plan variable(s) not bound: {missing}"
                rec.stopped = f"failed step: {log['why']}"
                break
            fixed = compiled[i]["resolved"] if compiled is not None else None
            outcome = self.do_step(task, rec, concrete, log, fixed)
            log["outcome"] = outcome
            if outcome == "ok":
                done_all.append(key)
                done_steps.append((step, log.get("resolved")))
                if compiled is None:
                    rec.compiled.append({"step": step, "resolved": log.get("resolved")})
                i += 1
                if compiled is None and step["effect"] == "read_content" and i == len(plan):
                    plan, i, stop = self._continue(rec, task, plan, done_steps)
                    if stop:
                        rec.stopped = stop
                        break
                continue
            if outcome in ("refuse", "gate", "location", "restored") and compiled is None:
                rec.compiled.append({"step": step, "resolved": {"op": outcome}})
            if outcome in ("refuse", "gate", "handoff", "prompt", "gated", "location", "restored", "restore_failed"):
                rec.stopped = {"refuse": "refused (plan)", "gate": "human gate (declined)",
                               "handoff": "picker hand-off (declined)", "prompt": "auth prompt seen",
                               "gated": "irreversible-action guard (declined)",
                               "location": "refused (location check)",
                               "restored": "refused (write check failed: the old text restored)",
                               "restore_failed": "failed step: write check failed and the restore failed"}[outcome]
                if outcome == "location":
                    rec.location_refusals += 1
                break
            if compiled is None and replans < X.MAX_REPLANS:
                replans += 1
                new, err = self.plan(rec, task.text)
                if err or not new:
                    rec.stopped = f"failed step, re-plan gave no plan: {err or 'empty'}"
                    break
                plan, i, carried = new, 0, list(done_all)
                continue
            rec.stopped = f"failed step: {log.get('why')}"
            break
        rec.wall_s = round(clock.elapsed(), 2)
        if not rec.stopped:
            rec.status = "completed"
        return rec


    def do_step(self, task, rec, step, log, fixed=None):
        if step["effect"] == "extract":
            return self._extract(task, rec, step, log, fixed)
        return super().do_step(task, rec, step, log, fixed)

    def _read_content(self, task, rec, step, log, bid, fixed):
        out = super()._read_content(task, rec, step, log, bid, fixed)
        if out == "ok":
            name = W.var_name(step["value"])
            self._reads.pop(name, None)
            self._reads[name] = self._vars[name]          # the latest read last
        return out

    def _extract(self, task, rec, step, log, fixed):
        """The detail from the text read: the words must occur in it (bound in
        the text's spelling), never its title or the whole text. The compiled
        replay extracts again from the text re-read (no model)."""
        name = W.var_name(step["value"])
        span, how = extract_detail(list(self._reads.values()), step.get("find") or "", step.get("target") or "")
        if span is None:
            log["why"] = f"extract: {how}"
            return "failed"
        self._vars[name] = span
        self._extracted.append(name)
        log["resolved"] = {"op": "extract", "var": name, "label": span}
        log["extract"] = {"var": name, "field": step.get("target"), "words": step.get("find"), "bound": span, "how": how}
        log["check"] = f"the detail {span!r} occurs in the text read ({how})"
        rec.extracts.append({"var": name, "field": step.get("target"), "bound": span, "how": how})
        return "ok"

    # -- the item the user describes -------------------------------------------------------
    def _pick_file(self, rec, task, step, log, cands, state, previews: bool):
        """``desktop_e2e4``'s rule (the map when the target names exactly one row) only
        when the target is the USER's words (the goal) or a text read in this
        run; a name the planner took from the screen goes to the picker, which
        reads every row's content (task GA07)."""
        hit = X.label_match(step["target"], [c["label"] for c in cands])
        user = named_by_words(step["target"], task.text, list(self._reads.values()))
        if len(hit) == 1 and user:
            log["resolution"] = {"by": "map", "why": "one row label named by the target (the user's words)"}
            return cands[hit[0]]
        why = (f"{len(hit)} row labels fit the target" if len(hit) != 1 else
               f"the target {step['target']!r} names a row but not with the user's words: chosen by content")
        log["resolution"] = {"by": "picker", "why": why}
        if previews:
            files = [c["label"] for c in cands if c.get("kind") == "file"][:W.MAX_PREVIEWS]
            if files:
                pv = self.call("previews", labels=files)
                snip = pv.get("previews") or {}
                log["previews"] = {k: (v or "")[:120] for k, v in snip.items()}
                cands = [dict(c, text=c["text"] + (f" | content: {' '.join((snip.get(c['label']) or '').split())[:160]}"
                                                   if snip.get(c["label"]) else "")) for c in cands]
        return self.pick(rec, task.text, state, cands, log)

    # -- writes to existing content are journalled, checked, restored --------------------
    def _gate_replace(self, rec, log, what: str) -> str:
        rec.gate_stops += 1
        rec.gated += 1
        why = f"replace of the existing text of {what} without an explicit instruction to replace: gated"
        self._inbox(rec, "write gate: " + why, log)
        log["why"] = why
        return "gate"

    def _journal(self, rec, log, kind: str, where: str, old_disk: Optional[str], old_shown: str, mode: str,
                 **extra) -> Dict[str, Any]:
        j = {"i": log.get("i"), "kind": kind, "where": where, "mode": mode, "old_disk": old_disk,
             "old_shown": old_shown, "sha256": hashlib.sha256((old_disk if old_disk is not None else old_shown)
                                                              .encode()).hexdigest(), "restored": None, **extra}
        rec.journal.append(j)
        log["journal"] = len(rec.journal) - 1
        return j

    def _restore_doc(self, rec, log, bid, area, doc, entry, j) -> str:
        """Write the journalled text back and save; verified on disk."""
        old = j["old_disk"] if j["old_disk"] is not None else j["old_shown"]
        try:
            r1 = self._act(rec, log, "enter", bundle=bid, target={"role": area["role"], "identifier": area["identifier"],
                                                                  "document": doc}, text=old, commit=False)
            r2 = self._act(rec, log, "press", bundle=bid, trigger=entry["trigger"], label=entry.get("path")) \
                if X._ok(r1) else r1
        except X._Prompt:
            return "prompt"
        back = self.call("file", path=doc)
        ok = X._ok(r2) and back.get("ok") and (back.get("text") or "").rstrip("\n") == old.rstrip("\n")
        j["restored"] = bool(ok)
        j["restore_check"] = "the bytes on disk are the journalled text again" if ok else \
            f"after the restore the file reads {(back.get('text') or '')[:80]!r}"
        rec.restores += 1 if ok else 0
        return "restored" if ok else "restore_failed"

    def _doc_save(self, task, rec, step, log, bid, fixed):
        """``desktop_e2e4``'s read-modify-write save, plus: replacing existing text without
        an explicit instruction is GATED; the old text goes into the journal
        before the write; a failed check (the bytes, the mode's invariant) or a
        different front document after the save RESTORES it (a refusal)."""
        from anyuse.execution import desktop_contracts as C
        if fixed is not None and fixed.get("op") in ("location", "gate", "restored"):
            return fixed["op"]
        entry = self._resolve_single(task, rec, step, log, bid, "doc_save", fixed)
        if entry in (None, "handoff"):
            return "handoff" if entry == "handoff" else "failed"
        area = next((e for e in self.m.entries(X.APP_OF[bid], "field_rw") if e.get("role") == "AXTextArea"), None)
        if area is None:
            log["why"] = "no text area mapped for the document"
            return "failed"
        front = self.call("front")
        if front.get("front_bundle") != bid:
            self.call("launch", bundle=bid)
            front = self.call("front")
            log["activated"] = X.APP_OF[bid]
        doc = front.get("document")
        if front.get("front_bundle") != bid or not doc:
            log["why"] = f"no front document of {X.APP_OF[bid]}"
            return "failed"
        why = self._document_check(rec, step, log, doc)
        if why:
            log["why"] = why
            return "location"
        mode, value, find = step.get("mode") or "replace", step["value"], step.get("find") or ""
        log["resolved"] = {"op": "doc_save", "entry": entry, "mode": mode}
        before = self.call("file", path=doc)
        shown = self.call("field", bundle=bid, role=area["role"], identifier=area["identifier"])
        if not shown.get("ok"):
            log["why"] = "the document's current text could not be read"
            return "failed"
        old = shown.get("value") or ""
        if mode == "replace" and value == "":
            new, write = old, False
        else:
            if replaces_existing(old, mode, value) and not explicit_replace(task.text):
                return self._gate_replace(rec, log, repr(os.path.basename(doc)))
            new, err = W.compose(old, mode, value, find)
            if err:
                log["why"] = f"{mode}: {err}"
                return "failed"
            write = True
        log["write"] = {"mode": mode, "old_chars": len(old), "new_chars": len(new)}
        j = self._journal(rec, log, "doc_save", doc, before.get("text") if before.get("ok") else None, old, mode) \
            if write and (old or (before.get("text") or "")) else None
        try:
            if write:
                r = self._act(rec, log, "enter", bundle=bid, target={"role": area["role"],
                                                                      "identifier": area["identifier"],
                                                                      "document": doc}, text=new, commit=False)
                if r.get("status") == "gated":
                    return "gated"
                if not X._ok(r):
                    log["why"] = f"write: {r.get('detail')}"
                    return self._restore_doc(rec, log, bid, area, doc, entry, j) if j else "failed"
            r = self._act(rec, log, "press", bundle=bid, trigger=entry["trigger"], label=entry.get("path"))
            if r.get("status") == "gated":
                return "gated"
            if not X._ok(r):
                log["why"] = f"save: {r.get('detail')}"
                return self._restore_doc(rec, log, bid, area, doc, entry, j) if j else "failed"
        except X._Prompt:
            return "prompt"
        after = self.call("file", path=doc)
        s_obs = {"identity": {"bundle_id": bid, "document": doc}, "complete": True,
                 "target": {"text": before.get("text") or ""}, "sentinel": None}
        obs = {"identity": {"bundle_id": bid, "document": doc},
               "complete": bool(after.get("ok")) and after.get("mtime") != before.get("mtime"),
               "target": {"text": after.get("text") or ""}, "sentinel": None}
        why = C.DOC_SAVE.validate(obs) or C.DOC_SAVE.expect(s_obs, new.rstrip("\n"), 0, obs) \
            or (W.check_text(old, after.get("text") or "", mode, value, find) if write else None)
        # identity after the write: the document saved is still the one the run checked
        front2 = self.call("front")
        if not why and front2.get("document") and front2.get("document") != doc:
            why = f"after the save the front document is {os.path.basename(front2.get('document'))!r}, not the one written"
        log["check"] = why or (f"doc_save contract met (the bytes on disk; {mode}: the old text preserved)"
                               if mode != "replace" else "doc_save contract met (the bytes on disk)")
        if why:
            log["why"] = why
            if j is not None:
                return self._restore_doc(rec, log, bid, area, doc, entry, j)
            return "failed"
        return "ok"

    def _field_rw(self, task, rec, step, log, bid, fixed):
        """``desktop_e2e4``'s field write with the replace gate, the journal and the
        restore (a field keeps no file: the old value is written back and read)."""
        from anyuse.execution import desktop_contracts as C
        if fixed is not None and fixed.get("op") in ("location", "gate", "restored"):
            return fixed["op"]
        entry = self._resolve_single(task, rec, step, log, bid, "field_rw", fixed)
        if entry in (None, "handoff"):
            return "handoff" if entry == "handoff" else "failed"
        mode, value, find = step.get("mode") or "replace", step["value"], step.get("find") or ""
        log["resolved"] = {"op": "field_rw", "entry": entry, "mode": mode}
        tgt = {"role": entry["role"], "identifier": entry["identifier"]}
        if bid == "com.apple.TextEdit":
            front = self.call("front")
            why = self._document_check(rec, step, log, front.get("document"))
            if why:
                log["why"] = why
                return "location"
        cur = self.call("field", bundle=bid, **tgt)
        if not cur.get("ok"):
            log["why"] = "the field could not be read"
            return "failed"
        old = cur.get("value") or ""
        if replaces_existing(old, mode, value) and not explicit_replace(task.text):
            return self._gate_replace(rec, log, f"the field {entry.get('path') or entry['role']!r}")
        new, err = W.compose(old, mode, value, find)
        if err:
            log["why"] = f"{mode}: {err}"
            return "failed"
        j = self._journal(rec, log, "field_rw", f"{X.APP_OF[bid]}:{entry['role']}", None, old, mode) if old else None
        try:
            r = self._act(rec, log, "enter", bundle=bid, target=tgt, text=new, commit=False)
        except X._Prompt:
            return "prompt"
        if r.get("status") == "gated":
            return "gated"
        got = self.call("field", bundle=bid, **tgt)
        obs = {"identity": {"bundle_id": bid}, "complete": bool(got.get("ok")), "target": got.get("value"),
               "sentinel": None}
        why = (None if X._ok(r) else f"write: {r.get('detail')}") or C.FIELD_RW.validate(obs) \
            or C.FIELD_RW.expect({"identity": {"bundle_id": bid}}, new, 0, obs) \
            or W.check_text(old, got.get("value") or "", mode, value, find)
        log["check"] = why or f"field_rw contract met ({mode}: the old text preserved)"
        if not why:
            return "ok"
        log["why"] = why
        if j is None:
            return "failed"
        try:
            self._act(rec, log, "enter", bundle=bid, target=tgt, text=old, commit=False)
        except X._Prompt:
            return "prompt"
        back = self.call("field", bundle=bid, **tgt)
        ok = (back.get("value") or "") == old
        j["restored"] = ok
        j["restore_check"] = "the field holds the journalled text again" if ok else "the field differs after the restore"
        rec.restores += 1 if ok else 0
        return "restored" if ok else "restore_failed"

    # -- a setting the planner named in its own words is cross-checked -------------------
    def _crosscheck(self, rec, task, log, bid, cands, planned: Dict[str, Any], question: str) -> Optional[str]:
        if self.picker_name == "none":
            log["crosscheck"] = {"verdict": "unsure (no picker: the planner's option stands)", "planned": planned.get("label")}
            return None
        """The picker reads the USER's words for the option the planner named in its
        own: a CONFIDENT contradiction (both orders agree on another option, the
        minimum confidence >= t2) stops the step as a hand-off; agreement or an
        unsure picker leaves the planner's option (e.g. GA11: the document
        check switched on instead of the check while typing)."""
        state = X.STATES["menus"].format(app=X.UI_NAMES.get(bid, X.APP_OF[bid]))
        sp = None
        if self.spend is not None:
            def sp(k, actual=None, _k=self.picker_kind):
                return self.spend(_k, actual=actual) if actual is not None else self.spend(_k)
        p = consistent_pick5(self.chooser, task.text, state, [c["text"] for c in cands], threshold=self.threshold,
                             spend=sp, question=question)
        rec.calls["picker"] += p.calls
        rec.cost += p.cost
        chosen = cands[p.index]["option"] if p.index is not None else None
        verdict = ("agrees" if chosen is not None and chosen["key"] == planned["key"] else
                   "contradicts" if chosen is not None else "unsure (the planner's option stands)")
        log["crosscheck"] = dict(p.to_dict(), options=[option_text5(c["text"]) for c in cands], question=question,
                                 planned=planned.get("label"), verdict=verdict)
        if verdict == "contradicts":
            rec.handoffs += 1
            rec.gated += 1
            self._inbox(rec, f"cross-check: the planner named {planned.get('label')!r}, the user's words name "
                             f"{chosen.get('label')!r}", log)
            return "handoff"
        return None

    def _flag_toggle(self, task, rec, step, log, bid, fixed):
        if fixed is None and (self.chooser is not None or self.picker_name == "none") and self.planner is not None:
            app = X.APP_OF[bid]
            res = self.m.resolve_flag(app, step["target"])
            if res.by == "map" and res.option and not user_named_option(res.option["label"], task.text):
                want = X.on_off(step["value"])
                cands = self.m.resolve_flag(app, "").candidates or []
                q = (f"Which listed setting does the user want switched {'on' if want else 'off'}? "
                     f"Task: {task.text}")
                stop = self._crosscheck(rec, task, log, bid, cands, res.option, q) if len(cands) > 1 else None
                if stop:
                    return stop
        return super()._flag_toggle(task, rec, step, log, bid, fixed)

    def _toggle_select(self, task, rec, step, log, bid, fixed):
        if fixed is None and (self.chooser is not None or self.picker_name == "none") and self.planner is not None:
            app = X.APP_OF[bid]
            res = self.m.resolve_toggle(app, step["target"], step["value"])
            if res.by == "map" and res.option and not user_named_option(res.option["label"], task.text):
                cands = [{"entry": res.entry, "option": o, "text": o["path"]} for o in (res.entry or {}).get("options") or []]
                q = f"Which listed option does the user want? Task: {task.text}"
                stop = self._crosscheck(rec, task, log, bid, cands, res.option, q) if len(cands) > 1 else None
                if stop:
                    return stop
        return super()._toggle_select(task, rec, step, log, bid, fixed)

    # -- a folder the picker chose for a described FILE: go in and pick the file there ----------
    def _open_finder(self, task, rec, step, log, fixed):
        """``desktop_e2e4``'s Finder route. When the PICKER chose a subfolder for a target
        that does not ask for a folder (seen task G05: "den Entwurf für den
        Elternabend" -> the folder "Entwürfe"), the engine goes into it and picks the
        file among ITS rows (content snippets, dates); the compiled replay enters
        the folder and opens the recorded file."""
        out = super()._open_finder(task, rec, step, log, fixed)
        res = log.get("resolved") or {}
        if out != "ok" or fixed is not None or not res.get("folder") or (log.get("resolution") or {}).get("by") != "picker" \
                or set(X.tokens(step.get("target"))) & X.FOLDER_WORDS:
            return out
        folder = res["label"]
        rows = self.call("rows", bundle="com.apple.finder", kind="finder_files")
        cands = [{"text": r["text"], "label": r["label"], "kind": r.get("kind")} for r in rows.get("rows") or []]
        if not any(c.get("kind") == "file" for c in cands):
            return out
        log["folder_pick"] = {"entered": folder, "pick": log.pop("pick", None), "dates": log.pop("dates", None)}
        ch = self._pick_file(rec, task, step, log, cands,
                             X.STATES["finder_files"].format(where=rows.get("where") or ""), previews=True)
        if ch is None:
            return "handoff"
        if ch.get("kind") == "folder":
            log["why"] = f"the picker chose another folder ({ch['label']!r}) inside {folder!r}"
            return "failed"
        log["resolved"] = {"op": "open", "route": "finder", "label": ch["label"], "enter": [folder]}
        try:
            r = self._act(rec, log, "select", bundle="com.apple.finder", kind="finder_files", label=ch["label"])
        except X._Prompt:
            return "prompt"
        if not X._ok(r):
            log["why"] = f"select {ch['label']!r}: {r.get('detail')}"
            return "failed"
        trig = self.m.open_trigger("Finder")
        try:
            r = self._act(rec, log, "press", bundle="com.apple.finder", trigger=trig["trigger"], label=trig["path"])
        except X._Prompt:
            return "prompt"
        if r.get("status") == "gated":
            return "gated"
        if not X._ok(r):
            log["why"] = f"open: {r.get('detail')}"
            return "failed"
        return self._await_document(log, ch["label"])

    # -- a hand-off leaves the screen as it was ------------------------------------------------
    def _notes_row(self, task, rec, step, log, fixed, open_it: bool):
        """``desktop_e2e4``'s note row. Listing the candidates of a GALLERY folder goes back
        to the grid and so closes the note the user had open; when the step then
        hands off (nobody chose), that note is opened again: the hand-off leaves the
        screen as it found it (task GA02 in picker mode "none")."""
        self.g.notes_back = None
        out = super()._notes_row(task, rec, step, log, fixed, open_it)
        nb = self.g.notes_back or {}
        if out == "handoff" and nb.get("went_back") and nb.get("shown_before"):
            try:
                r = self._act(rec, log, "select", bundle="com.apple.Notes", kind="notes_notes", label=nb["shown_before"])
            except X._Prompt:
                return "prompt"
            log["view_restored"] = {"note": nb["shown_before"], "status": r.get("status")}
        return out
